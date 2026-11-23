import logging
import re
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx


logger = logging.getLogger(__name__)


class InternetImageFinder:
    def __init__(
        self,
        timeout_seconds: int = 12,
        brand_sources: dict[str, str] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self._priority_site = "https://baudinequipamientos.com"
        self._brand_sources = {
            self._normalize_brand(brand): url
            for brand, url in (brand_sources or {}).items()
            if brand and url
        }

    async def find_image_url(
        self,
        *,
        sku: str,
        brand: str | None = None,
        name: str | None = None,
        search_reference: str | None = None,
    ) -> str | None:
        lookup_sku = (search_reference or sku).strip()
        search_name = self._normalize_search_name(name)
        queries = self._build_queries(sku=lookup_sku, brand=brand, name=search_name)
        if not queries:
            return None

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            try:
                brand_image = await self._search_brand_source(
                    client=client,
                    sku=lookup_sku,
                    brand=brand,
                    name=search_name,
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                logger.warning(
                    "Brand image search failed for brand=%r sku=%r: %s",
                    brand,
                    sku,
                    exc,
                )
                brand_image = None
            if brand_image:
                return self._normalize_image_url(brand_image)

            site_image = await self._search_priority_site(
                client=client,
                sku=lookup_sku,
                brand=brand,
                name=search_name,
            )
            if site_image:
                return self._normalize_image_url(site_image)

            for query in queries:
                image_url = await self._search_bing_images(client=client, query=query)
                if image_url:
                    return self._normalize_image_url(image_url)

        return None

    @staticmethod
    def _normalize_search_name(name: str | None) -> str | None:
        normalized_name = (name or "").strip()
        if " - " in normalized_name:
            normalized_name = normalized_name.rsplit(" - ", maxsplit=1)[0].strip()
        return normalized_name or None

    async def _search_brand_source(
        self,
        *,
        client: httpx.AsyncClient,
        sku: str,
        brand: str | None,
        name: str | None,
    ) -> str | None:
        brand_key = self._normalize_brand(brand)
        if not brand_key:
            return None

        url_template = self._brand_sources.get(brand_key)
        if not url_template:
            return None

        html = await self._fetch_search_html(
            client=client,
            search_url=self._render_search_url(
                url_template=url_template,
                sku=sku,
                brand=brand,
                name=name,
            ),
        )
        return await self._extract_image_from_search_html(
            client=client,
            html=html,
            sku=sku,
        )

    def _build_queries(self, *, sku: str, brand: str | None, name: str | None) -> list[str]:
        normalized_sku = (sku or "").strip()
        normalized_brand = (brand or "").strip()
        normalized_name = (name or "").strip()

        if not normalized_sku:
            return []

        queries = []
        if normalized_brand and normalized_name:
            queries.append(f"{normalized_brand} {normalized_name} {normalized_sku}")
        if normalized_brand:
            queries.append(f"{normalized_brand} {normalized_sku}")
        if normalized_name:
            queries.append(f"{normalized_name} {normalized_sku}")
        queries.append(f"{normalized_sku} product image")

        seen = set()
        unique = []
        for query in queries:
            key = query.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(query)
        return unique

    async def _search_priority_site(
        self,
        *,
        client: httpx.AsyncClient,
        sku: str,
        brand: str | None,
        name: str | None,
    ) -> str | None:
        terms = [sku.strip()]
        if brand:
            terms.append(f"{brand.strip()} {sku.strip()}")
        if name:
            terms.append(f"{name.strip()} {sku.strip()}")

        checked_links: set[str] = set()
        for term in terms:
            if not term:
                continue

            html = await self._fetch_priority_search_html(client=client, term=term)
            image_url = await self._extract_image_from_search_html(
                client=client,
                html=html,
                sku=sku,
                checked_links=checked_links,
            )
            if image_url:
                return image_url

        return None

    async def _extract_image_from_search_html(
        self,
        *,
        client: httpx.AsyncClient,
        html: str,
        sku: str,
        checked_links: set[str] | None = None,
    ) -> str | None:
        direct_image = self._extract_image_for_sku_from_search_html(html=html, sku=sku)
        if direct_image:
            return direct_image

        if self._text_contains_sku(html=html, sku=sku):
            og_image = self._extract_meta_content(
                html,
                meta_name="og:image",
                property_attr="property",
            )
            if og_image and self._is_valid_image_url(og_image):
                return og_image

            twitter_image = self._extract_meta_content(
                html,
                meta_name="twitter:image",
                property_attr="name",
            )
            if twitter_image and self._is_valid_image_url(twitter_image):
                return twitter_image

        links = self._extract_product_links(html)
        visited = checked_links if checked_links is not None else set()
        for link in links:
            if link in visited:
                continue
            visited.add(link)
            image_url = await self._extract_product_page_image(
                client=client,
                product_url=link,
                expected_sku=sku,
            )
            if image_url:
                return image_url

        return None

    async def _fetch_priority_search_html(self, *, client: httpx.AsyncClient, term: str) -> str:
        search_urls = [
            f"{self._priority_site}/tienda/?q={quote_plus(term)}",
            f"{self._priority_site}/tienda?q={quote_plus(term)}",
            f"{self._priority_site}/?s={quote_plus(term)}&post_type=product",
        ]

        last_error: Exception | None = None
        for search_url in search_urls:
            try:
                response = await client.get(search_url, headers={"User-Agent": self._user_agent})
                response.raise_for_status()
                if response.text:
                    return response.text
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        return ""

    async def _fetch_search_html(self, *, client: httpx.AsyncClient, search_url: str) -> str:
        response = await client.get(search_url, headers={"User-Agent": self._user_agent})
        response.raise_for_status()
        return response.text

    def _render_search_url(
        self,
        *,
        url_template: str,
        sku: str,
        brand: str | None,
        name: str | None,
    ) -> str:
        values = {
            "sku": quote_plus((sku or "").strip()),
            "brand": quote_plus((brand or "").strip()),
            "name": quote_plus((name or "").strip()),
            "query": quote_plus(
                " ".join(part for part in [(brand or "").strip(), (name or "").strip(), (sku or "").strip()] if part)
            ),
        }
        return url_template.format(**values)

    def _extract_image_for_sku_from_search_html(self, *, html: str, sku: str) -> str | None:
        target_sku = self._normalize_sku(sku)
        if not html or not target_sku:
            return None

        normalized_html = html.replace('\\"', '"')
        product_blocks = re.findall(
            r'"product"\s*:\s*\{(.*?)\}\s*,\s*"priority"',
            normalized_html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        for block in product_blocks:
            sku_match = re.search(r'"sku"\s*:\s*"([^"]*)"', block, flags=re.IGNORECASE)
            if not sku_match:
                continue

            candidate_sku = self._normalize_sku(sku_match.group(1))
            if candidate_sku != target_sku:
                continue

            src_match = re.search(
                r'"images"\s*:\s*\[\s*\{.*?"src"\s*:\s*"([^"]+)"',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if src_match:
                candidate_url = src_match.group(1).strip()
                if self._is_valid_image_url(candidate_url):
                    return candidate_url

        return None

    def _extract_product_links(self, html: str) -> list[str]:
        normalized_html = html.replace('\\"', '"')

        raw_links = re.findall(r'href=["\']([^"\']+/(?:producto|product)/[^"\']+)["\']', normalized_html)
        raw_links.extend(
            re.findall(
                r'"permalink"\s*:\s*"([^"]+/(?:producto|product)/[^"]+)"',
                normalized_html,
                flags=re.IGNORECASE,
            )
        )

        links: list[str] = []
        seen: set[str] = set()
        for raw in raw_links:
            full = urljoin(f"{self._priority_site}/", raw)
            if full not in seen:
                seen.add(full)
                links.append(full)
        return links[:5]

    async def _extract_product_page_image(
        self,
        *,
        client: httpx.AsyncClient,
        product_url: str,
        expected_sku: str | None = None,
    ) -> str | None:
        response = await client.get(product_url, headers={"User-Agent": self._user_agent})
        response.raise_for_status()
        html = response.text

        # Reject product pages that do not reference the target SKU.
        if expected_sku and not self._text_contains_sku(html=html, sku=expected_sku):
            return None

        og_image = self._extract_meta_content(
            html,
            meta_name="og:image",
            property_attr="property",
        )
        if og_image and self._is_valid_image_url(og_image):
            return og_image

        twitter_image = self._extract_meta_content(
            html,
            meta_name="twitter:image",
            property_attr="name",
        )
        if twitter_image and self._is_valid_image_url(twitter_image):
            return twitter_image

        return None

    def _extract_meta_content(self, html: str, *, meta_name: str, property_attr: str) -> str | None:
        escaped = re.escape(meta_name)
        pattern = (
            rf'<meta[^>]+{property_attr}=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']'
        )
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    async def _search_bing_images(self, *, client: httpx.AsyncClient, query: str) -> str | None:
        url = f"https://www.bing.com/images/search?q={quote_plus(query)}"
        response = await client.get(url, headers={"User-Agent": self._user_agent})
        response.raise_for_status()

        html = response.text
        # Bing exposes image targets in page scripts as "murl":"https://...".
        candidates = re.findall(r'"murl"\s*:\s*"(https?:\\/\\/[^\"]+)"', html)
        for raw_url in candidates:
            image_url = raw_url.replace("\\/", "/")
            if self._is_valid_image_url(image_url):
                return image_url

        return None

    def _is_valid_image_url(self, url: str) -> bool:
        lowered = url.lower()
        if not lowered.startswith("http"):
            return False
        invalid_tokens = ["logo", "icon", "sprite"]
        if any(token in lowered for token in invalid_tokens):
            return False
        image_hints = [".jpg", ".jpeg", ".png", ".webp", "format="]
        return any(hint in lowered for hint in image_hints)

    def _normalize_image_url(self, url: str) -> str:
        parsed_url = urlparse(url)
        if parsed_url.path.rstrip("/").endswith("/api/og-image"):
            source_urls = parse_qs(parsed_url.query).get("src", [])
            if source_urls and source_urls[0].startswith(("http://", "https://")):
                return source_urls[0]
        return url

    def _normalize_sku(self, sku: str) -> str:
        text = (sku or "").strip().lower()
        return re.sub(r"[^a-z0-9]", "", text)

    def _text_contains_sku(self, *, html: str, sku: str) -> bool:
        target_sku = self._normalize_sku(sku)
        if not target_sku:
            return False

        normalized_html = self._normalize_sku(html)
        return target_sku in normalized_html

    def _normalize_brand(self, brand: str | None) -> str:
        text = (brand or "").strip().lower()
        return re.sub(r"\s+", " ", text)
