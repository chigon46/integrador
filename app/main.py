import base64
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import unicodedata
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from openpyxl import load_workbook

from app.bcu_client import BCUClient
from app.blueprints.ml import router as mercadolibre_router
from app.image_finder import InternetImageFinder
from app.odoo_client import OdooClient
from app.product_image_generator import ProductImageGenerator
from app.settings import settings
from app.woocommerce_client import WooCommerceClient, _order_sync_metadata

logger = logging.getLogger("integrator")


SYNC_STATUS: dict[str, Any] = {
    "background_enabled": False,
    "background_task_running": False,
    "last_background_tick_at": None,
    "last_inventory_sync": None,
    "last_orders_sync": None,
    "last_error": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    logger.info(
        "Startup config: background=%s interval=%s orders=%s inventory=%s",
        settings.enable_background_sync,
        settings.background_sync_interval_seconds,
        settings.background_sync_orders,
        settings.background_sync_inventory,
    )
    SYNC_STATUS["background_enabled"] = settings.enable_background_sync
    if settings.enable_background_sync:
        task = asyncio.create_task(_background_sync_loop())
        SYNC_STATUS["background_task_running"] = True
        logger.info(
            "Background sync enabled every %s seconds",
            settings.background_sync_interval_seconds,
        )
    yield
    if task:
        task.cancel()
        SYNC_STATUS["background_task_running"] = False
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Odoo WooCommerce Integrator", lifespan=lifespan)
app.include_router(mercadolibre_router)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/health/odoo")
async def health_odoo() -> dict:
    return OdooClient().health()


@app.get("/health/woocommerce")
async def health_woocommerce() -> dict:
    return await WooCommerceClient().health()


@app.get("/debug/woocommerce/brands")
async def debug_woocommerce_brands(search: str | None = None) -> dict:
    brands = await WooCommerceClient().list_brands(search=search)
    return {"count": len(brands), "brands": brands}


@app.get("/debug/woocommerce/orders")
async def debug_woocommerce_orders(limit: int = 50, status: str = "any") -> dict:
    orders = await WooCommerceClient().list_orders_for_debug(
        limit=limit,
        status=status,
    )
    return {
        "count": len(orders),
        "status_filter": status,
        "orders": [
            {
                "woocommerce_id": order.get("id"),
                "status": order.get("status"),
                "date_created_gmt": order.get("date_created_gmt"),
                "total": order.get("total"),
                "billing_email": (order.get("billing") or {}).get("email"),
                "line_items": [
                    {
                        "name": item.get("name"),
                        "sku": item.get("sku"),
                        "quantity": item.get("quantity"),
                    }
                    for item in order.get("line_items", [])
                ],
                "sync": _order_sync_metadata(order),
            }
            for order in orders
        ],
    }


@app.get("/debug/bcu/cotizaciones")
async def debug_bcu_cotizaciones(
    monedas: str = "1",
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    grupo: int = 1,
) -> dict:
    moneda_ids = [int(part.strip()) for part in monedas.split(",") if part.strip()]
    result = await BCUClient().get_cotizaciones(
        monedas=moneda_ids,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        grupo=grupo,
    )
    return result


@app.post("/sync/currency-rates")
async def sync_currency_rates(
    monedas: str = "1",
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    grupo: int = 1,
) -> dict:
    moneda_ids = [int(part.strip()) for part in monedas.split(",") if part.strip()]
    result = await BCUClient().get_cotizaciones(
        monedas=moneda_ids,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        grupo=grupo,
    )

    if result.get("status") not in (0,):
        return {"status": "error", "result": result}

    try:
        odoo = OdooClient()
        for cot in result.get("items") or []:
            if cot.get("CodigoISO"):
                rate = cot.get("TCC") or cot.get("TCV")
                if rate:
                    odoo.execute(
                        "res.currency.rate",
                        "create",
                        {
                            "name": cot.get("Fecha"),
                            "currency_id": 2,
                            "rate": float(rate),
                        },
                    )
    except Exception as exc:  # pragma: no cover
        return {"status": "odoo_error", "error": str(exc), "result": result}

    return {"status": "ok", "result": result}


@app.get("/debug/odoo/sale-orders")
async def debug_odoo_sale_orders(limit: int = 10) -> dict:
    orders = OdooClient().list_recent_sale_orders(limit=limit)
    return {"count": len(orders), "orders": orders}


@app.get("/debug/odoo/product-fields")
async def debug_odoo_product_fields(contains: str = "brand") -> dict:
    fields = OdooClient().list_product_fields(contains=contains)
    return {"count": len(fields), "fields": fields}


@app.get("/debug/sync-status")
async def debug_sync_status() -> dict:
    return {
        "config": {
            "enable_background_sync": settings.enable_background_sync,
            "background_sync_interval_seconds": settings.background_sync_interval_seconds,
            "background_sync_orders": settings.background_sync_orders,
            "background_sync_inventory": settings.background_sync_inventory,
            "background_orders_limit": settings.background_orders_limit,
            "background_products_limit": settings.background_products_limit,
        },
        "status": SYNC_STATUS,
    }


@app.post("/sync/products")
async def sync_products(limit: int = 50, update_content: bool = True) -> dict:
    logger.info("Manual sync/products called limit=%s update_content=%s", limit, update_content)
    return await _sync_products(limit=limit, update_content=update_content)


@app.post("/sync/inventory")
async def sync_inventory(limit: int = 500) -> dict:
    logger.info("Manual sync/inventory called limit=%s", limit)
    return await _sync_products(limit=limit, update_content=False)


@app.post("/sync/product-images")
async def sync_product_images(
    request: Request,
    search_online: bool = False,
    generate_with_ai: bool = False,
    dry_run: bool = False,
    auto_approve: bool = False,
    only_with_image: bool = False,
    only_without_image: bool = False,
    limit: int = 100,
    offset: int = 0,
    x_integrator_secret: str | None = Header(default=None),
) -> dict:
    if generate_with_ai and not _is_valid_integrator_secret(x_integrator_secret):
        raise HTTPException(status_code=401, detail="Secreto de integracion invalido")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit debe estar entre 1 y 100")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset no puede ser negativo")
    if auto_approve and not generate_with_ai:
        raise HTTPException(status_code=400, detail="auto_approve requiere generate_with_ai=true")
    if (only_with_image or only_without_image) and not generate_with_ai:
        raise HTTPException(
            status_code=400,
            detail="only_with_image y only_without_image requieren generate_with_ai=true",
        )
    if only_with_image and only_without_image:
        raise HTTPException(
            status_code=400,
            detail="only_with_image y only_without_image no se pueden usar juntos",
        )
    if auto_approve and not (only_with_image or only_without_image):
        raise HTTPException(
            status_code=400,
            detail="auto_approve requiere only_with_image=true o only_without_image=true para limitar el lote",
        )
    if auto_approve and dry_run:
        raise HTTPException(status_code=400, detail="auto_approve no se puede combinar con dry_run")

    entries, brand_sources = await _parse_product_images_request(request)
    batch_entries = entries[offset : offset + limit] if generate_with_ai else entries

    woocommerce = WooCommerceClient()
    image_finder = InternetImageFinder(brand_sources=brand_sources)
    image_generator = ProductImageGenerator(
        api_key=settings.bfl_api_key,
        logo_url=settings.image_generation_logo_url,
        storage_path=settings.image_generation_storage_path,
        internal_base_url=settings.image_generation_internal_base_url,
    )
    synced = []
    discovered = []
    previews = []
    candidates = []
    skipped_no_image = []
    skipped_with_image = []
    not_found = []
    missing_image = []
    errors = []

    for entry in batch_entries:
        sku = entry["sku"]
        image_url = entry.get("image_url") or ""

        if generate_with_ai:
            try:
                product_image = await woocommerce.get_product_image_by_sku(sku)
            except Exception as exc:
                errors.append(
                    {
                        "sku": sku,
                        "image_url": "",
                        "error": f"Error obteniendo imagen actual de WooCommerce: {exc}",
                    }
                )
                continue

            if not product_image:
                not_found.append({"sku": sku, "image_url": ""})
                continue

            current_image_url = product_image["image_url"]
            if only_with_image and not current_image_url:
                skipped_no_image.append(
                    {
                        "sku": sku,
                        "brand": entry.get("brand"),
                        "reason": "El producto no tiene una imagen en WooCommerce",
                    }
                )
                continue
            if only_without_image and current_image_url:
                skipped_with_image.append(
                    {
                        "sku": sku,
                        "brand": entry.get("brand"),
                        "reason": "El producto ya tiene una imagen en WooCommerce",
                    }
                )
                continue
            if only_with_image or not image_url:
                image_url = current_image_url

        if not image_url and search_online:
            try:
                found_image = await image_finder.find_image_url(
                    sku=sku,
                    brand=entry.get("brand"),
                    name=entry.get("name"),
                    search_reference=entry.get("search_reference") or None,
                )
                if found_image:
                    image_url = found_image
                    discovered.append(
                        {
                            "sku": sku,
                            "brand": entry.get("brand"),
                            "name": entry.get("name"),
                            "search_reference": entry.get("search_reference"),
                            "image_url": image_url,
                        }
                    )
            except Exception as exc:
                errors.append(
                    {
                        "sku": sku,
                        "image_url": image_url,
                        "error": f"Error buscando imagen online: {exc}",
                    }
                )
                continue

        if generate_with_ai:
            try:
                preview = await image_generator.generate_preview(
                    sku=sku,
                    brand=entry.get("brand"),
                    name=entry.get("name"),
                    source_image_url=image_url or None,
                    preview_base_url=str(request.base_url).rstrip("/"),
                )
                if auto_approve:
                    generated_image_url = image_generator.get_internal_image_url(
                        preview["generation_id"]
                    )
                    updated = await woocommerce.update_product_image_by_sku(
                        sku=sku,
                        image_url=generated_image_url,
                    )
                    if not updated:
                        not_found.append({"sku": sku, "image_url": generated_image_url})
                        continue

                    image_generator.mark_approved(preview["generation_id"], preview)
                    synced.append(
                        {
                            "sku": sku,
                            "woocommerce_id": updated["id"],
                            "generation_id": preview["generation_id"],
                            "source_image_url": image_url,
                            "generation_mode": preview["generation_mode"],
                            "image_url": generated_image_url,
                        }
                    )
                else:
                    previews.append(preview)
            except Exception as exc:
                errors.append(
                    {
                        "sku": sku,
                        "image_url": image_url,
                        "error": f"Error generando vista previa con IA: {exc}",
                    }
                )
            continue

        if not image_url:
            missing_image.append(
                {
                    "sku": sku,
                    "brand": entry.get("brand"),
                    "name": entry.get("name"),
                }
            )
            continue

        if dry_run:
            candidates.append(
                {
                    "sku": sku,
                    "brand": entry.get("brand"),
                    "name": entry.get("name"),
                    "search_reference": entry.get("search_reference"),
                    "image_url": image_url,
                }
            )
            continue

        try:
            updated = await woocommerce.update_product_image_by_sku(
                sku=sku,
                image_url=image_url,
            )
            if not updated:
                not_found.append({"sku": sku, "image_url": image_url})
                continue

            synced.append(
                {
                    "sku": sku,
                    "woocommerce_id": updated["id"],
                    "image_url": image_url,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "sku": sku,
                    "image_url": image_url,
                    "error": str(exc),
                }
            )

    return {
        "received": len(entries),
        "processed": len(batch_entries),
        "offset": offset,
        "next_offset": offset + len(batch_entries) if offset + len(batch_entries) < len(entries) else None,
        "search_online": search_online,
        "generate_with_ai": generate_with_ai,
        "auto_approve": auto_approve,
        "only_with_image": only_with_image,
        "only_without_image": only_without_image,
        "dry_run": dry_run,
        "brand_sources": len(brand_sources),
        "synced": synced,
        "discovered": discovered,
        "previews": previews,
        "candidates": candidates,
        "skipped_no_image": skipped_no_image,
        "skipped_with_image": skipped_with_image,
        "not_found": not_found,
        "missing_image": missing_image,
        "errors": errors,
    }


@app.get("/generated-images/{generation_id}.jpg")
async def get_generated_image(generation_id: str) -> FileResponse:
    image_generator = ProductImageGenerator(
        api_key=settings.bfl_api_key,
        logo_url=settings.image_generation_logo_url,
        storage_path=settings.image_generation_storage_path,
        internal_base_url=settings.image_generation_internal_base_url,
    )
    image_path = image_generator.get_image_path(generation_id)
    if not image_path:
        raise HTTPException(status_code=404, detail="Imagen generada no encontrada")
    return FileResponse(image_path, media_type="image/jpeg")


@app.post("/product-images/generations/{generation_id}/approve")
async def approve_generated_product_image(
    generation_id: str,
    x_integrator_secret: str | None = Header(default=None),
) -> dict:
    if not _is_valid_integrator_secret(x_integrator_secret):
        raise HTTPException(status_code=401, detail="Secreto de integracion invalido")

    image_generator = ProductImageGenerator(
        api_key=settings.bfl_api_key,
        logo_url=settings.image_generation_logo_url,
        storage_path=settings.image_generation_storage_path,
        internal_base_url=settings.image_generation_internal_base_url,
    )
    metadata = image_generator.load_generation(generation_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="Vista previa no encontrada")
    if metadata.get("status") != "pending":
        raise HTTPException(status_code=409, detail="La vista previa ya fue aprobada")

    image_url = image_generator.get_internal_image_url(generation_id)
    updated = await WooCommerceClient().update_product_image_by_sku(
        sku=metadata["sku"],
        image_url=image_url,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Producto no encontrado en WooCommerce")

    image_generator.mark_approved(generation_id, metadata)
    return {
        "generation_id": generation_id,
        "sku": metadata["sku"],
        "woocommerce_id": updated["id"],
        "image_url": image_url,
        "status": "approved",
    }


def _is_valid_integrator_secret(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, settings.webhook_secret)


async def _sync_products(limit: int = 50, update_content: bool = True) -> dict:
    odoo = OdooClient()
    woocommerce = WooCommerceClient()
    products = odoo.list_saleable_products(limit=limit)

    synced = []
    for product in products:
        result = await woocommerce.upsert_product(
            product,
            update_content=update_content,
        )
        odoo_category = product.get("categ_id")
        woo_categories = result.get("categories") or []
        synced.append(
            {
                "sku": product["default_code"],
                "woocommerce_id": result["id"],
                "category": (
                    odoo_category[1]
                    if isinstance(odoo_category, list) and len(odoo_category) > 1
                    else None
                ),
                "woocommerce_category_id": (
                    woo_categories[0].get("id") if woo_categories else None
                ),
            }
        )

    return {
        "count": len(synced),
        "update_content": update_content,
        "products": synced,
    }


@app.post("/sync/orders")
async def sync_orders(limit: int = 50) -> dict:
    logger.info("Manual sync/orders called limit=%s", limit)
    return await _sync_orders(limit=limit)


async def _sync_orders(limit: int = 50) -> dict:
    odoo = OdooClient()
    woocommerce = WooCommerceClient()
    orders = await woocommerce.list_paid_orders(limit=limit)

    synced = []
    errors = []
    for order in orders:
        order_id = order["id"]
        try:
            sale_order_id = odoo.create_sale_order_from_wc(order)
            await woocommerce.mark_order_synced(order_id, sale_order_id)
            synced.append(
                {
                    "woocommerce_id": order_id,
                    "odoo_sale_order_id": sale_order_id,
                }
            )
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "Order sync failed woocommerce_id=%s error=%s",
                order_id,
                error,
            )
            await woocommerce.mark_order_sync_error(order_id, error)
            errors.append({"woocommerce_id": order_id, "error": error})

    return {
        "checked": len(orders),
        "synced": synced,
        "errors": errors,
    }


async def _background_sync_loop() -> None:
    while True:
        try:
            SYNC_STATUS["background_task_running"] = True
            SYNC_STATUS["last_background_tick_at"] = _now_iso()
            if settings.background_sync_orders:
                result = await _sync_orders(limit=settings.background_orders_limit)
                SYNC_STATUS["last_orders_sync"] = {
                    "at": _now_iso(),
                    "checked": result["checked"],
                    "synced": len(result["synced"]),
                    "errors": len(result["errors"]),
                }
                logger.info(
                    "Background orders sync checked=%s synced=%s errors=%s",
                    result["checked"],
                    len(result["synced"]),
                    len(result["errors"]),
                )

            if settings.background_sync_inventory:
                result = await _sync_products(
                    limit=settings.background_products_limit,
                    update_content=False,
                )
                SYNC_STATUS["last_inventory_sync"] = {
                    "at": _now_iso(),
                    "count": result["count"],
                }
                logger.info(
                    "Background inventory sync products=%s",
                    result["count"],
                )
        except Exception:
            SYNC_STATUS["last_error"] = {
                "at": _now_iso(),
                "message": "Background sync failed",
            }
            logger.exception("Background sync failed")

        await asyncio.sleep(settings.background_sync_interval_seconds)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@app.post("/webhooks/woocommerce/order")
async def woocommerce_order_webhook(
    request: Request,
    x_wc_webhook_signature: str | None = Header(default=None),
    x_integrator_secret: str | None = Header(default=None),
) -> dict:
    body = await request.body()
    if not _valid_woocommerce_signature(body, x_wc_webhook_signature) and not _valid_secret(
        x_integrator_secret
    ):
        raise HTTPException(status_code=401, detail="Webhook secret invalido")

    order = json.loads(body)
    status = order.get("status")
    if status not in {"processing", "completed"}:
        return {"ignored": True, "status": status}

    sale_order_id = OdooClient().create_sale_order_from_wc(order)
    await WooCommerceClient().mark_order_synced(order["id"], sale_order_id)
    return {"created": True, "sale_order_id": sale_order_id}


def _valid_secret(value: str | None) -> bool:
    if not value:
        return False
    return hmac.compare_digest(value, settings.webhook_secret)


def _valid_woocommerce_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False

    digest = hmac.new(
        settings.webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(signature, expected)


def _parse_product_images_payload(body: bytes, content_type: str | None) -> list[dict[str, str]]:
    if _is_xlsx_payload(body=body, content_type=content_type):
        return _parse_product_images_xlsx(body)

    text = body.decode("utf-8-sig").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Payload vacio")

    if _is_json_payload(text=text, content_type=content_type):
        return _parse_product_images_json(text)
    return _parse_product_images_csv(text)


async def _parse_product_images_request(request: Request) -> tuple[list[dict[str, str]], dict[str, str]]:
    content_type = request.headers.get("content-type") or ""
    if "multipart/form-data" in content_type.lower():
        return await _parse_product_images_multipart(request)

    body = await request.body()
    if _is_xlsx_payload(body=body, content_type=content_type):
        return _parse_product_images_xlsx(body), {}

    text = body.decode("utf-8-sig").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Payload vacio")

    if _is_json_payload(text=text, content_type=content_type):
        return _parse_product_images_json_request(text)

    return _parse_product_images_csv(text), {}


async def _parse_product_images_multipart(request: Request) -> tuple[list[dict[str, str]], dict[str, str]]:
    form = await request.form()
    products_file = _get_upload_file(
        form,
        ["products_file", "products", "file", "xlsx_file"],
    )
    if not products_file:
        raise HTTPException(
            status_code=400,
            detail="multipart invalido: falta products_file",
        )

    products_body = await products_file.read()
    entries = _parse_product_images_payload(
        body=products_body,
        content_type=products_file.content_type,
    )

    brand_sources: dict[str, str] = {}
    brand_sources_file = _get_upload_file(
        form,
        ["brand_sources_file", "brand_map_file", "sources_file"],
    )
    if brand_sources_file:
        brand_sources_body = await brand_sources_file.read()
        brand_sources = _parse_brand_sources_payload(
            body=brand_sources_body,
            content_type=brand_sources_file.content_type,
        )
    elif form.get("brand_sources_json"):
        brand_sources = _parse_brand_sources_json(str(form.get("brand_sources_json")))

    return entries, brand_sources


def _get_upload_file(form: Any, keys: list[str]) -> Any | None:
    for key in keys:
        value = form.get(key)
        if hasattr(value, "read") and hasattr(value, "filename"):
            return value
    return None


def _is_json_payload(text: str, content_type: str | None) -> bool:
    if content_type and "application/json" in content_type.lower():
        return True
    return text.startswith("[") or text.startswith("{")


def _is_xlsx_payload(body: bytes, content_type: str | None) -> bool:
    if content_type and "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in content_type.lower():
        return True
    return len(body) >= 2 and body[:2] == b"PK"


def _parse_product_images_json(text: str) -> list[dict[str, str]]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON invalido: {exc.msg}") from exc

    rows: Any = raw
    if isinstance(raw, dict):
        rows = raw.get("items") or raw.get("products") or raw.get("data")

    if not isinstance(rows, list):
        raise HTTPException(
            status_code=400,
            detail="JSON invalido: se esperaba una lista o un objeto con items/products/data",
        )

    normalized_rows = []
    for row in rows:
        normalized_rows.append(_normalize_product_image_row(row))

    return _validate_product_image_rows(normalized_rows)


def _parse_product_images_json_request(text: str) -> tuple[list[dict[str, str]], dict[str, str]]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON invalido: {exc.msg}") from exc

    if isinstance(raw, list):
        normalized_rows = [_normalize_product_image_row(row) for row in raw]
        return _validate_product_image_rows(normalized_rows), {}

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON invalido: se esperaba lista o objeto",
        )

    rows = raw.get("items") or raw.get("products") or raw.get("data") or []
    brand_sources_raw = (
        raw.get("brand_sources")
        or raw.get("brandSources")
        or raw.get("search_sources")
        or raw.get("searchSources")
        or {}
    )

    normalized_rows = [_normalize_product_image_row(row) for row in rows]
    return _validate_product_image_rows(normalized_rows), _normalize_brand_sources(brand_sources_raw)


def _parse_product_images_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [name.strip().lower() for name in reader.fieldnames or []]

    if "sku" not in fieldnames:
        raise HTTPException(
            status_code=400,
            detail="CSV invalido: encabezado requerido sku",
        )

    normalized_rows = []
    for row in reader:
        normalized_rows.append(_normalize_product_image_row(row))

    return _validate_product_image_rows(normalized_rows)


def _parse_product_images_xlsx(body: bytes) -> list[dict[str, str]]:
    try:
        workbook = load_workbook(filename=io.BytesIO(body), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"XLSX invalido: {exc}") from exc

    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        raise HTTPException(status_code=400, detail="XLSX invalido: sin encabezados")

    headers = [_normalize_header_name(value) for value in header_row]
    normalized_rows = []
    for row in rows_iter:
        raw = {
            headers[index]: "" if value is None else str(value).strip()
            for index, value in enumerate(row)
            if index < len(headers) and headers[index]
        }
        # Ignora filas totalmente vacias.
        if not any(raw.values()):
            continue
        normalized_rows.append(_normalize_product_image_row(raw))

    return _validate_product_image_rows(normalized_rows)


def _parse_brand_sources_payload(body: bytes, content_type: str | None) -> dict[str, str]:
    text = body.decode("utf-8-sig").strip()
    if not text:
        return {}

    if _is_json_payload(text=text, content_type=content_type):
        return _parse_brand_sources_json(text)
    return _parse_brand_sources_csv(text)


def _parse_brand_sources_json(text: str) -> dict[str, str]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON invalido en brand_sources: {exc.msg}") from exc

    return _normalize_brand_sources(raw)


def _parse_brand_sources_csv(text: str) -> dict[str, str]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}

    rows = []
    for row in reader:
        rows.append(
            {
                _normalize_header_name(key): "" if value is None else str(value).strip()
                for key, value in row.items()
                if key is not None
            }
        )

    return _normalize_brand_sources(rows)


def _normalize_brand_sources(raw: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}

    if isinstance(raw, dict):
        for key, value in raw.items():
            brand = str(key or "").strip()
            search_url = str(value or "").strip()
            if brand and search_url:
                normalized[brand] = search_url
        return normalized

    if not isinstance(raw, list):
        return normalized

    for row in raw:
        if not isinstance(row, dict):
            continue
        row_normalized = {
            _normalize_header_name(key): "" if value is None else str(value).strip()
            for key, value in row.items()
            if key is not None
        }
        brand = _find_first_value(row_normalized, ["brand", "marca", "marcas"])
        search_url = _find_first_value(
            row_normalized,
            ["search_url", "url", "url_busqueda", "search", "busqueda_url", "search_template"],
        )
        if brand and search_url:
            normalized[brand] = search_url

    return normalized


def _normalize_product_image_row(row: Any) -> dict[str, str]:
    if not isinstance(row, dict):
        return {
            "sku": "",
            "image_url": "",
            "brand": "",
            "name": "",
            "search_reference": "",
        }

    normalized_map = {
        _normalize_header_name(key): "" if value is None else str(value).strip()
        for key, value in row.items()
        if key is not None
    }

    return {
        "sku": _find_first_value(
            normalized_map,
            ["sku_limpio", "sku", "default_code", "codigo", "codigo_sku", "cod", "item"],
        ),
        "image_url": _find_first_value(
            normalized_map,
            ["image_url", "imagen_url", "url_imagen", "image", "imagen", "img_url", "foto_url"],
        ),
        "brand": _find_first_value(normalized_map, ["brand", "marca", "marcas"]),
        "name": _find_first_value(normalized_map, ["name", "nombre", "producto", "descripcion", "title"]),
        "search_reference": _find_first_value(
            normalized_map,
            [
                "search_reference",
                "manufacturer_sku",
                "supplier_sku",
                "manufacturer_model",
                "sku_original",
                "model",
                "modelo",
                "referencia_fabricante",
                "codigo_fabricante",
            ],
        ),
    }


def _find_first_value(values: dict[str, str], aliases: list[str]) -> str:
    for alias in aliases:
        if alias in values and values[alias]:
            return values[alias]
    return ""


def _normalize_header_name(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace(" ", "_").replace("-", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _validate_product_image_rows(rows: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Fila {index} invalida: se esperaba un objeto",
            )

        sku = str(row.get("sku") or "").strip()
        image_url = str(row.get("image_url") or "").strip()
        brand = str(row.get("brand") or "").strip()
        name = str(row.get("name") or "").strip()
        search_reference = str(row.get("search_reference") or "").strip()

        if not sku:
            raise HTTPException(
                status_code=400,
                detail=f"Fila {index} invalida: sku es obligatorio",
            )

        normalized.append(
            {
                "sku": sku,
                "image_url": image_url,
                "brand": brand,
                "name": name,
                "search_reference": search_reference,
            }
        )

    return normalized
