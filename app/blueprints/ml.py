import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from app.mercadolibre_client import MercadoLibreClient
from app.odoo_client import OdooClient

router = APIRouter(prefix="/sync/ml", tags=["mercadolibre-sync"])

ML_SYNC_STATE: dict[str, Any] = {
    "last_run_at": None,
    "last_limit": None,
    "last_changed_only": True,
    "last_apply": False,
    "last_synced": 0,
    "last_skipped_unchanged": 0,
    "last_errors": 0,
}

# In-memory fingerprint store. Replace with persistent storage in production.
PRODUCT_FINGERPRINTS: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _product_fingerprint(product: dict[str, Any]) -> str:
    relevant = {
        "sku": product.get("default_code"),
        "name": product.get("name"),
        "price": product.get("list_price"),
        "stock": product.get("qty_available"),
        "description": product.get("description_sale"),
        "brand": product.get("product_brand_id") or product.get("brand"),
        "updated_at": product.get("write_date"),
    }
    payload = json.dumps(relevant, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.get("/status")
async def sync_status() -> dict[str, Any]:
    return {
        "state": ML_SYNC_STATE,
        "tracked_products": len(PRODUCT_FINGERPRINTS),
    }


@router.get("/health")
async def health_ml() -> dict[str, Any]:
    return await MercadoLibreClient().health()


@router.post("/products")
async def sync_ml_products(
    limit: int = 50,
    changed_only: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    odoo = OdooClient()
    ml = MercadoLibreClient()
    try:
        products = odoo.list_saleable_products(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo leer productos desde Odoo: {exc}",
        ) from exc

    synced = []
    skipped_unchanged = 0
    errors = []

    for product in products:
        sku = str(product.get("default_code") or "").strip()
        if not sku:
            continue

        fingerprint = _product_fingerprint(product)
        previous = PRODUCT_FINGERPRINTS.get(sku)
        if changed_only and previous == fingerprint:
            skipped_unchanged += 1
            continue

        try:
            result = await ml.stage_product_sync(product=product, apply=apply)
            synced.append(
                {
                    "sku": sku,
                    "status": result.get("status"),
                    "reason": result.get("reason"),
                }
            )
            PRODUCT_FINGERPRINTS[sku] = fingerprint
        except Exception as exc:
            errors.append({"sku": sku, "error": str(exc)})

    ML_SYNC_STATE.update(
        {
            "last_run_at": _now_iso(),
            "last_limit": limit,
            "last_changed_only": changed_only,
            "last_apply": apply,
            "last_synced": len(synced),
            "last_skipped_unchanged": skipped_unchanged,
            "last_errors": len(errors),
        }
    )

    return {
        "checked": len(products),
        "synced": synced,
        "skipped_unchanged": skipped_unchanged,
        "errors": errors,
        "mode": "apply" if apply else "dry_run",
    }
