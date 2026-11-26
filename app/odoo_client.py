from typing import Any
from xmlrpc.client import ServerProxy

from app.settings import settings


class OdooClient:
    def __init__(self) -> None:
        base_url = settings.odoo_url.rstrip("/")
        self.common = ServerProxy(f"{base_url}/xmlrpc/2/common")
        self.models = ServerProxy(f"{base_url}/xmlrpc/2/object")
        self.db = settings.odoo_db
        self.user = settings.odoo_user
        self.password = settings.odoo_password
        self.context = {"lang": settings.odoo_lang}
        self._uid: int | None = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self.common.authenticate(self.db, self.user, self.password, {})
            if not uid:
                raise RuntimeError("No se pudo autenticar contra Odoo")
            self._uid = uid
        return self._uid

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self.models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            method,
            list(args),
            kwargs,
        )

    def health(self) -> dict[str, Any]:
        version = self.common.version()
        return {"ok": True, "version": version}

    def list_saleable_products(self, limit: int = 50) -> list[dict[str, Any]]:
        domain = [
            ["sale_ok", "=", True],
            ["default_code", "!=", False],
        ]
        fields = [
            "id",
            "name",
            "default_code",
            "list_price",
            "qty_available",
            "description_sale",
            "categ_id",
            "write_date",
        ]
        if settings.odoo_brand_field and self._product_field_exists(
            settings.odoo_brand_field
        ):
            fields.append(settings.odoo_brand_field)

        return self.execute(
            "product.product",
            "search_read",
            domain,
            fields=fields,
            limit=limit,
            context=self.context,
        )

    def create_sale_order_from_wc(self, order: dict[str, Any]) -> int:
        existing_id = self.find_sale_order_by_wc_id(order["id"])
        if existing_id:
            return existing_id

        partner_id = self._find_or_create_partner(order)
        lines = []

        for item in order.get("line_items", []):
            sku = item.get("sku")
            product_id = self._find_product_by_sku(sku) if sku else None
            if not product_id:
                raise ValueError(f"No existe producto en Odoo para SKU: {sku}")

            lines.append(
                [
                    0,
                    0,
                    {
                        "product_id": product_id,
                        "product_uom_qty": item.get("quantity", 1),
                        "price_unit": float(item.get("price") or 0),
                    },
                ]
            )

        values = {
            "partner_id": partner_id,
            "client_order_ref": f"WooCommerce #{order.get('id')}",
            "order_line": lines,
        }
        return self.execute("sale.order", "create", values)

    def find_sale_order_by_wc_id(self, order_id: int) -> int | None:
        order_ids = self.execute(
            "sale.order",
            "search",
            [["client_order_ref", "=", f"WooCommerce #{order_id}"]],
            limit=1,
        )
        return order_ids[0] if order_ids else None

    def list_recent_sale_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        fields = [
            "id",
            "name",
            "client_order_ref",
            "state",
            "partner_id",
            "amount_total",
            "date_order",
        ]
        return self.execute(
            "sale.order",
            "search_read",
            [],
            fields=fields,
            limit=limit,
            order="id desc",
        )

    def list_product_fields(self, contains: str = "brand") -> dict[str, Any]:
        fields = self.execute(
            "product.product",
            "fields_get",
            [],
            attributes=["string", "type", "relation"],
        )
        needle = contains.lower()
        return {
            name: definition
            for name, definition in fields.items()
            if needle in name.lower()
            or needle in str(definition.get("string", "")).lower()
        }

    def _product_field_exists(self, field_name: str) -> bool:
        fields = self.execute(
            "product.product",
            "fields_get",
            [],
            attributes=["string"],
        )
        return field_name in fields

    def _find_product_by_sku(self, sku: str) -> int | None:
        product_ids = self.execute(
            "product.product",
            "search",
            [["default_code", "=", sku]],
            limit=1,
        )
        return product_ids[0] if product_ids else None

    def _find_or_create_partner(self, order: dict[str, Any]) -> int:
        billing = order.get("billing") or {}
        email = billing.get("email")

        if email:
            partner_ids = self.execute(
                "res.partner",
                "search",
                [["email", "=", email]],
                limit=1,
            )
            if partner_ids:
                return partner_ids[0]

        name = " ".join(
            part
            for part in [billing.get("first_name"), billing.get("last_name")]
            if part
        ).strip()

        values = {
            "name": name or billing.get("company") or "Cliente WooCommerce",
            "email": email,
            "phone": billing.get("phone"),
            "street": billing.get("address_1"),
            "street2": billing.get("address_2"),
            "city": billing.get("city"),
            "zip": billing.get("postcode"),
        }
        return self.execute("res.partner", "create", values)
