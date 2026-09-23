import asyncio
import time
from typing import Any

import httpx

from app.settings import settings


class CircuitBreaker:
    def __init__(self, failure_threshold: int, cooldown_seconds: int) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1, cooldown_seconds)
        self.failures = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def on_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.open_until = time.monotonic() + self.cooldown_seconds


class AsyncRateLimiter:
    def __init__(self, max_calls_per_second: int) -> None:
        self.max_calls_per_second = max(1, max_calls_per_second)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = 1.0 / self.max_calls_per_second
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + slot


class MercadoLibreClient:
    def __init__(self) -> None:
        self.base_url = settings.ml_api_url.rstrip("/")
        self.access_token = settings.ml_access_token
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=settings.ml_circuit_breaker_threshold,
            cooldown_seconds=settings.ml_circuit_breaker_cooldown_seconds,
        )
        self.rate_limiter = AsyncRateLimiter(settings.ml_rate_limit_per_second)

    def is_enabled(self) -> bool:
        return settings.ml_enabled

    def is_configured(self) -> bool:
        return bool(settings.ml_access_token and settings.ml_seller_id)

    async def health(self) -> dict[str, Any]:
        if not self.is_enabled():
            return {"enabled": False, "ok": False, "reason": "ML sync disabled"}

        if not self.is_configured():
            return {
                "enabled": True,
                "ok": False,
                "reason": "Missing ML credentials (ML_ACCESS_TOKEN or ML_SELLER_ID)",
            }

        profile = await self._guarded_profile_call()
        return {
            "enabled": True,
            "ok": True,
            "seller_id": settings.ml_seller_id,
            "nickname": profile.get("nickname"),
            "circuit_open": self.circuit_breaker.is_open(),
        }

    async def stage_product_sync(self, product: dict[str, Any], apply: bool = False) -> dict[str, Any]:
        sku = str(product.get("default_code") or "")

        if not self.is_enabled():
            return {"sku": sku, "status": "skipped", "reason": "ML sync disabled"}

        if not apply:
            return {"sku": sku, "status": "planned", "reason": "dry-run mode"}

        if not self.is_configured():
            return {
                "sku": sku,
                "status": "error",
                "reason": "Missing ML credentials (ML_ACCESS_TOKEN or ML_SELLER_ID)",
            }

        # Writer intentionally conservative in first iteration.
        # This call validates token health under rate-limit + circuit breaker rules.
        await self._guarded_profile_call()
        return {
            "sku": sku,
            "status": "pending_writer",
            "reason": "ML publish/update writer not implemented yet",
        }

    async def _guarded_profile_call(self) -> dict[str, Any]:
        if self.circuit_breaker.is_open():
            raise RuntimeError("MercadoLibre circuit breaker OPEN")

        await self.rate_limiter.wait_turn()

        try:
            profile = await self._fetch_profile()
            self.circuit_breaker.on_success()
            return profile
        except Exception:
            self.circuit_breaker.on_failure()
            raise

    async def _fetch_profile(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        url = f"{self.base_url}/users/{settings.ml_seller_id}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
