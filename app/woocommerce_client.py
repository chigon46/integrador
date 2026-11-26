import unicodedata
from typing import Any

import httpx

from app.settings import settings


class WooCommerceClient:
    def __init__(self) -> None:
        self.base_url = f"{settings.wc_url.rstrip('/')}/wp-json/wc/v3"
        self.auth_params = {
            "consumer_key": settings.wc_consumer_key,
            "consumer_secret": settings.wc_consumer_secret,
        }
        self._product_category_ids: dict[str, int] = {}

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{self.base_url}/products",
                params={**self.auth_params, "per_page": 1},
            )
            response.raise_for_status()
            return {
                "ok": True,
                "product_count_sample": len(response.json()),
            }

    async def upsert_product(
        self, product: dict[str, Any], update_content: bool = True
    ) -> dict[str, Any]:
        sku = product["default_code"]
        inventory_payload = {
            "regular_price": str(product.get("list_price") or 0),
            "manage_stock": True,
            "stock_quantity": int(product.get("qty_available") or 0),
        }
        category_name = _extract_product_category(product)
        content_payload = {
            "name": product["name"],
            "sku": sku,
            "type": "simple",
            "description": product.get("description_sale") or "",
        }
        brand = _extract_brand(product)
        async with httpx.AsyncClient(timeout=30) as client:
            if category_name:
                category_id = await self._get_or_create_product_category(
                    client,
                    category_name,
                )
                inventory_payload["categories"] = [{"id": category_id}]

            if brand and settings.wc_brand_sync_mode == "brand":
                brand_id = await self._get_or_create_brand(client, brand)
                if brand_id:
                    content_payload["brands"] = [{"id": brand_id}]
            elif brand and settings.wc_brand_sync_mode == "attribute":
                content_payload["attributes"] = [
                    {
                        "name": settings.wc_brand_attribute_name,
                        "visible": True,
                        "variation": False,
                        "options": [brand],
                    }
                ]

            existing = await client.get(
                f"{self.base_url}/products",
                params={**self.auth_params, "sku": sku},
            )
            existing.raise_for_status()
            matches = existing.json()

            if matches:
                product_id = matches[0]["id"]
                payload = inventory_payload
                if update_content:
                    payload = {**content_payload, **inventory_payload}
                response = await client.put(
                    f"{self.base_url}/products/{product_id}",
                    params=self.auth_params,
                    json=payload,
                )
            else:
                payload = {**content_payload, **inventory_payload}
                response = await client.post(
                    f"{self.base_url}/products",
                    params=self.auth_params,
                    json=payload,
                )

            response.raise_for_status()
            return response.json()

    async def _get_or_create_product_category(
        self,
        client: httpx.AsyncClient,
        category_name: str,
    ) -> int:
        normalized_name = _normalize_taxonomy_name(category_name)
        cached_id = self._product_category_ids.get(normalized_name)
        if cached_id:
            return cached_id

        categories_url = f"{self.base_url}/products/categories"
        existing = await client.get(
            categories_url,
            params={
                **self.auth_params,
                "search": category_name,
                "per_page": 100,
            },
        )
        existing.raise_for_status()
        for category in existing.json():
            if _normalize_taxonomy_name(category.get("name", "")) == normalized_name:
                category_id = category.get("id")
                if category_id:
                    self._product_category_ids[normalized_name] = category_id
                    return category_id

        created = await client.post(
            categories_url,
            params=self.auth_params,
            json={"name": category_name},
        )
        if created.is_error:
            # Another sync worker may have created the same category after our lookup.
            retry = await client.get(
                categories_url,
                params={
                    **self.auth_params,
                    "search": category_name,
                    "per_page": 100,
                },
            )
            retry.raise_for_status()
            for category in retry.json():
                if _normalize_taxonomy_name(category.get("name", "")) == normalized_name:
                    category_id = category.get("id")
                    if category_id:
                        self._product_category_ids[normalized_name] = category_id
                        return category_id
            created.raise_for_status()

        category_id = created.json().get("id")
        if not category_id:
            raise RuntimeError(f"WooCommerce no devolvió ID para categoría: {category_name}")
        self._product_category_ids[normalized_name] = category_id
        return category_id

    async def list_brands(self, search: str | None = None) -> list[dict[str, Any]]:
        params = {**self.auth_params, "per_page": 100}
        if search:
            params["search"] = search

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self._brand_url(),
                params=params,
            )
            response.raise_for_status()
            return response.json()

    async def _get_or_create_brand(
        self, client: httpx.AsyncClient, brand_name: str
    ) -> int | None:
        existing = await client.get(
            self._brand_url(),
            params={**self.auth_params, "search": brand_name, "per_page": 100},
        )
        existing.raise_for_status()

        for brand in existing.json():
            if brand.get("name", "").strip().lower() == brand_name.strip().lower():
                return brand.get("id")

        created = await client.post(
            self._brand_url(),
            params=self.auth_params,
            json={"name": brand_name},
        )
        created.raise_for_status()
        return created.json().get("id")

    def _brand_url(self) -> str:
        endpoint = settings.wc_brand_endpoint.strip("/")
        return f"{self.base_url}/{endpoint}"

    async def list_paid_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        per_page = min(limit, 100)
        order_statuses = ["pending", "on-hold", "processing", "completed"]

        async with httpx.AsyncClient(timeout=30) as client:
            for status in order_statuses:
                response = await client.get(
                    f"{self.base_url}/orders",
                    params={
                        **self.auth_params,
                        "status": status,
                        "per_page": per_page,
                        "orderby": "date",
                        "order": "asc",
                    },
                )
                response.raise_for_status()
                orders.extend(response.json())

        pending_orders = [
            order
            for order in orders
            if not _is_order_synced(order)
        ]
        pending_orders.sort(
            key=lambda order: order.get("date_created_gmt") or "",
            reverse=True,
        )
        return pending_orders[:limit]

    async def list_orders_for_debug(
        self, limit: int = 50, status: str = "any"
    ) -> list[dict[str, Any]]:
        statuses = [status] if status != "any" else [
            "pending",
            "on-hold",
            "processing",
            "completed",
            "cancelled",
            "failed",
            "refunded",
        ]
        orders: list[dict[str, Any]] = []
        per_page = min(limit, 100)

        async with httpx.AsyncClient(timeout=30) as client:
            for order_status in statuses:
                response = await client.get(
                    f"{self.base_url}/orders",
                    params={
                        **self.auth_params,
                        "status": order_status,
                        "per_page": per_page,
                        "orderby": "date",
                        "order": "desc",
                    },
                )
                response.raise_for_status()
                orders.extend(response.json())

        orders.sort(key=lambda order: order.get("date_created_gmt") or "", reverse=True)
        return orders[:limit]

    async def update_product_image_by_sku(
        self, sku: str, image_url: str
    ) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=30) as client:
            existing = await client.get(
                f"{self.base_url}/products",
                params={**self.auth_params, "sku": sku},
            )
            existing.raise_for_status()
            matches = existing.json()
            if not matches:
                return None

            product_id = matches[0]["id"]
            response = await client.put(
                f"{self.base_url}/products/{product_id}",
                params=self.auth_params,
                json={"images": [{"src": image_url}]},
            )
            if response.is_error:
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}

                if isinstance(error_payload, dict):
                    error_code = str(error_payload.get("code") or "")
                    error_message = str(error_payload.get("message") or "")
                else:
                    error_code = ""
                    error_message = ""

                if not error_message:
                    error_message = response.text[:500] or response.reason_phrase

                error_detail = f"HTTP {response.status_code}"
                if error_code:
                    error_detail += f" ({error_code})"
                raise RuntimeError(
                    f"WooCommerce rechazó la imagen: {error_detail}: {error_message[:500]}"
                ) from None

            return response.json()

    async def get_product_image_by_sku(self, sku: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/products",
                params={**self.auth_params, "sku": sku},
            )
            response.raise_for_status()
            matches = response.json()
            if not matches:
                return None

            product = matches[0]
            images = product.get("images") or []
            if not images or not images[0].get("src"):
                return {
                    "id": product.get("id"),
                    "sku": product.get("sku") or sku,
                    "image_url": "",
                }

            return {
                "id": product.get("id"),
                "sku": product.get("sku") or sku,
                "image_url": images[0]["src"],
            }

    async def mark_order_synced(self, order_id: int, sale_order_id: int) -> dict[str, Any]:
        metadata = [
            {"key": "_odoo_sale_order_id", "value": str(sale_order_id)},
            {"key": "_odoo_sync_status", "value": "synced"},
        ]
        return await self._update_order_metadata(order_id, metadata)

    async def mark_order_sync_error(self, order_id: int, error: str) -> dict[str, Any]:
        metadata = [
            {"key": "_odoo_sync_status", "value": "sync_error"},
            {"key": "_odoo_sync_error", "value": error[:500]},
        ]
        return await self._update_order_metadata(order_id, metadata)

    async def _update_order_metadata(
        self, order_id: int, metadata: list[dict[str, str]]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(
                f"{self.base_url}/orders/{order_id}",
                params=self.auth_params,
                json={"meta_data": metadata},
            )
            response.raise_for_status()
            return response.json()


def _extract_brand(product: dict[str, Any]) -> str | None:
    raw_brand = product.get(settings.odoo_brand_field)
    if not raw_brand:
        return None
    if isinstance(raw_brand, list) and len(raw_brand) >= 2:
        return str(raw_brand[1])
    if isinstance(raw_brand, dict):
        name = raw_brand.get("display_name") or raw_brand.get("name")
        return str(name) if name else None
    return str(raw_brand)


def _extract_product_category(product: dict[str, Any]) -> str | None:
    raw_category = product.get("categ_id")
    if not raw_category:
        return None
    if isinstance(raw_category, list) and len(raw_category) >= 2:
        return str(raw_category[1]).strip() or None
    if isinstance(raw_category, dict):
        name = raw_category.get("display_name") or raw_category.get("name")
        return str(name).strip() if name else None
    return str(raw_category).strip() or None


def _normalize_taxonomy_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return " ".join(
        "".join(char for char in decomposed if not unicodedata.combining(char))
        .casefold()
        .split()
    )


def _is_order_synced(order: dict[str, Any]) -> bool:
    return any(
        metadata.get("key") == "_odoo_sync_status"
        and metadata.get("value") == "synced"
        for metadata in order.get("meta_data", [])
    )


def _order_sync_metadata(order: dict[str, Any]) -> dict[str, str | None]:
    metadata = {
        item.get("key"): item.get("value")
        for item in order.get("meta_data", [])
        if item.get("key") in {
            "_odoo_sync_status",
            "_odoo_sale_order_id",
            "_odoo_sync_error",
        }
    }
    return {
        "status": metadata.get("_odoo_sync_status"),
        "odoo_sale_order_id": metadata.get("_odoo_sale_order_id"),
        "error": metadata.get("_odoo_sync_error"),
    }
