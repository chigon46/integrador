import asyncio
import base64
import io
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image


MAX_SOURCE_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
BFL_IMAGE_ENDPOINT = "https://api.bfl.ai/v1/flux-3-image"
MAX_BFL_POLL_SECONDS = 240
DEFAULT_SOVAI_LOGO_URL = (
    "https://sovai.com.uy/wp-content/uploads/2026/03/"
    "cropped-Logo-SOVAi-1-159x37.png"
)


class ProductImageGenerator:
    def __init__(
        self,
        *,
        api_key: str,
        logo_url: str,
        storage_path: str | Path,
        internal_base_url: str,
    ) -> None:
        self.api_key = api_key
        self.logo_url = logo_url
        self.storage_path = Path(storage_path)
        self.internal_base_url = internal_base_url.rstrip("/")

    async def generate_preview(
        self,
        *,
        sku: str,
        brand: str | None,
        name: str | None,
        source_image_url: str,
        preview_base_url: str,
    ) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("Falta configurar BFL_API_KEY")
        self._validate_public_url(self.logo_url, "URL del logo SOVAI")

        source_image, source_mime = await self._download_source_image(source_image_url)
        prompt = self._build_prompt(brand=brand, name=name, sku=sku)
        image_data = base64.b64encode(source_image).decode("ascii")

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.post(
                BFL_IMAGE_ENDPOINT,
                headers={
                    "accept": "application/json",
                    "x-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "prompt": prompt,
                    "images": [image_data, self.logo_url],
                    "aspect_ratio": "auto",
                    "resolution": "match_input",
                    "grounding": False,
                },
            )
            if response.is_error:
                raise RuntimeError(self._format_bfl_error(response)) from None

            submission = response.json()
            polling_url = submission.get("polling_url")
            if not polling_url:
                raise RuntimeError(f"BFL no devolvió polling_url: {submission}")
            self._validate_bfl_url(polling_url)
            result = await self._poll_bfl_result(client, polling_url)

            result_url = (result.get("result") or {}).get("sample")
            if not result_url:
                raise RuntimeError(f"BFL marcó la tarea como lista sin result.sample: {result}")
            self._validate_bfl_url(result_url)

        generated_bytes, _ = await self._download_source_image(result_url)
        with Image.open(io.BytesIO(generated_bytes)) as generated_image:
            if generated_image.width * generated_image.height > MAX_IMAGE_PIXELS:
                raise ValueError("La imagen generada excede el limite de pixeles")
            final_image = generated_image.convert("RGB")
        output = io.BytesIO()
        final_image.save(output, format="PNG", optimize=True)
        final_image_bytes = output.getvalue()

        generation_id = uuid.uuid4().hex
        self.storage_path.mkdir(parents=True, exist_ok=True)
        image_path = self._image_path(generation_id)
        image_path.write_bytes(final_image_bytes)
        metadata = {
            "generation_id": generation_id,
            "sku": sku,
            "brand": brand or "",
            "name": name or "",
            "source_image_url": source_image_url,
            "status": "pending",
        }
        self._metadata_path(generation_id).write_text(
            json.dumps(metadata, ensure_ascii=True),
            encoding="utf-8",
        )

        return {
            **metadata,
            "preview_url": f"{preview_base_url.rstrip('/')}/generated-images/{generation_id}.png",
            "approval_endpoint": f"/product-images/generations/{generation_id}/approve",
        }

    async def _poll_bfl_result(
        self,
        client: httpx.AsyncClient,
        polling_url: str,
    ) -> dict:
        deadline = time.monotonic() + MAX_BFL_POLL_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            response = await client.get(
                polling_url,
                headers={"accept": "application/json", "x-key": self.api_key},
            )
            if response.is_error:
                raise RuntimeError(self._format_bfl_error(response)) from None

            result = response.json()
            status = result.get("status")
            if status == "Ready":
                return result
            if status in {"Error", "Failed", "Request Moderated", "Content Moderated", "No Card Generated"}:
                details = result.get("details") or result.get("error") or result
                raise RuntimeError(f"BFL terminó la tarea con estado {status}: {details}")
            if status not in {"Pending", "Reasoning", "Generating"}:
                raise RuntimeError(f"Estado de tarea BFL no reconocido: {result}")

        raise TimeoutError(
            f"BFL no terminó la generación dentro de {MAX_BFL_POLL_SECONDS} segundos"
        )

    @staticmethod
    def _validate_bfl_url(url: str) -> None:
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not (parsed_url.hostname or "").endswith(".bfl.ai"):
            raise ValueError("BFL devolvió una URL HTTPS fuera del dominio bfl.ai")

    @staticmethod
    def _format_bfl_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload
        else:
            detail = response.text[:500]
        message = f"BFL respondió HTTP {response.status_code}: {detail}"
        retry_after = response.headers.get("retry-after")
        if retry_after:
            message += f"; retry_after={retry_after}s"
        return message

    async def _download_source_image(self, image_url: str) -> tuple[bytes, str]:
        self._validate_public_url(image_url, "URL de imagen")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(image_url)
            response.raise_for_status()

        image_bytes = response.content
        if not image_bytes or len(image_bytes) > MAX_SOURCE_IMAGE_BYTES:
            raise ValueError("La imagen de origen esta vacia o excede 15 MB")

        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("La imagen de origen excede el limite de pixeles")
            normalized = source.convert("RGB")
        output = io.BytesIO()
        normalized.save(output, format="PNG")
        return output.getvalue(), "image/png"

    @staticmethod
    def _validate_public_url(image_url: str, label: str) -> None:
        parsed_url = urlparse(image_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError(f"{label} debe usar una URL HTTPS publica")
        if parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{label} no puede apuntar a localhost")

    @staticmethod
    def _build_prompt(*, brand: str | None, name: str | None, sku: str) -> str:
        product = " ".join(part for part in (brand, name, f"SKU {sku}") if part)
        return (

            "Create a new, original, high-quality commercial product photograph using the supplied "
            "image strictly as a visual reference for the physical product itself.\n\n"

            "PRODUCT REFERENCE:\n"
            "Study the reference image to understand the product's physical appearance, design, "
            "construction, proportions, colors, materials, controls, and functional details. "
            "Use it exclusively to understand what the product looks like. Do not reproduce "
            "the original photograph, its composition, branding, graphic elements, or layout.\n"
            "Generate a completely new photographic composition of the same product.\n\n"

            "CAMERA AND COMPOSITION:\n"
            "Show the product from a different, attractive three-quarter perspective. "
            "Create a professional commercial photography look. Preserve the product's actual "
            "construction and recognizable characteristics. Do not invent components or details "
            "that cannot reasonably be inferred from the reference. Show one complete product, "
            "centered and clearly visible, with generous surrounding space. Use realistic "
            "perspective, natural proportions, and professional framing.\n\n"

            "SOVAI VISUAL IDENTITY:\n"
            "Apply a consistent visual style across all product photographs:\n"
            "- Clean white or very light gray studio background.\n"
            "- Soft graphite shadows and subtle depth.\n"
            "- Refined, realistic studio lighting.\n"
            "- Balanced highlights, contrast, brightness, and reflections.\n"
            "- A restrained red accent inspired by the Sovai brand identity.\n"
            "- Keep the product's original colors and materials unchanged.\n\n"

            "BRANDING:\n"
            "Do not Include the supplied Sovai logo as the only external brand identity.\n\n"

            "FINAL RESULT:\n"
            "Produce a realistic, premium-quality commercial product photograph suitable "
            "for the Sovai online store and product catalog. The result must look like a "
            "newly commissioned studio photograph, not an edited version of the reference.\n"
            "Do not include additional logos, watermarks, credits, promotional text, "
            "decorative elements, or unrelated objects.\n\n"

            f"Product reference: {product}."



        )

        

    def load_generation(self, generation_id: str) -> dict[str, str] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", generation_id):
            return None
        metadata_path = self._metadata_path(generation_id)
        image_path = self._image_path(generation_id)
        if not metadata_path.is_file() or not image_path.is_file():
            return None
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def mark_approved(self, generation_id: str, metadata: dict[str, str]) -> None:
        metadata["status"] = "approved"
        self._metadata_path(generation_id).write_text(
            json.dumps(metadata, ensure_ascii=True),
            encoding="utf-8",
        )

    def get_internal_image_url(self, generation_id: str) -> str:
        return f"{self.internal_base_url}/generated-images/{generation_id}.png"

    def get_image_path(self, generation_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{32}", generation_id):
            return None
        image_path = self._image_path(generation_id)
        return image_path if image_path.is_file() else None

    def _image_path(self, generation_id: str) -> Path:
        return self.storage_path / f"{generation_id}.png"

    def _metadata_path(self, generation_id: str) -> Path:
        return self.storage_path / f"{generation_id}.json"