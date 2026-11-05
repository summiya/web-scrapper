"""Rate-limited, robots-aware public product catalog and image scraper."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("web_scrapper")
USER_AGENT = "CatalogResearchBot/1.0 (+local, respectful crawling)"
CATEGORY_PREFIXES = ("/collections/", "/product-category/", "/category/")
PRODUCT_PREFIXES = ("/products/", "/product/")
GALLERY_SELECTORS = (
    '[data-product-gallery] img', '[class*="product-gallery"] img',
    '[class*="product__media"] img', '[class*="product-media"] img',
    '[class*="product__photo"] img', '[class*="product__image"] img',
    '[class*="woocommerce-product-gallery"] img',
    '[class*="product-gallery"] source',
)
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg"}


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (value[:100].strip("-") or "unnamed")


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def html_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for item in soup.select("script, style, noscript"):
        item.decompose()
    return "\n".join(line for line in (clean_text(x) for x in soup.get_text("\n").splitlines()) if line)


def absolute_url(base: str, value: str | None) -> str | None:
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return None
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def canonical_image(url: str) -> str:
    parsed = urlsplit(url)
    # Shopify image sizes are query parameters; ignore them when deduplicating.
    return f"{parsed.hostname}{parsed.path}".lower()


def first_image_source(tag) -> str | None:
    for key in ("data-zoom-image", "data-large-image", "data-src", "data-original"):
        if tag.get(key):
            return tag[key]
    for key in ("data-srcset", "srcset"):
        if tag.get(key):
            options = [item.strip().split()[0] for item in tag[key].split(",") if item.strip()]
            if options:
                return options[-1]
    return tag.get("src")


def extract_tables(html: str | None) -> list[list[dict[str, str]]]:
    result = []
    for table in BeautifulSoup(html or "", "html.parser").select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        headers = [clean_text(cell.get_text(" ", strip=True)) for cell in rows[0].select("th, td")]
        parsed_rows = []
        for row in rows[1:]:
            values = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th, td")]
            if len(values) == len(headers) and values:
                parsed_rows.append(dict(zip(headers, values)))
        if parsed_rows:
            result.append(parsed_rows)
    return result


def get_product_ld(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            item = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        pool = item if isinstance(item, list) else [item]
        while pool:
            entry = pool.pop(0)
            if not isinstance(entry, dict):
                continue
            types = entry.get("@type", [])
            if "Product" in ([types] if isinstance(types, str) else types):
                return entry
            graph = entry.get("@graph", [])
            pool.extend(graph if isinstance(graph, list) else [graph])
    return {}


class CatalogScraper:
    def __init__(self, url: str, output: Path, delay: float = 1.0, timeout: int = 20,
                 max_pages: int = 100, max_products: int = 0, max_image_mb: int = 25):
        self.url = url if urlsplit(url).scheme else "https://" + url
        self.url = absolute_url(self.url, self.url)
        if not self.url:
            raise ValueError("Enter an HTTP or HTTPS URL")
        host = urlsplit(self.url).hostname
        self.host = host.removeprefix("www.")
        self.base = f"{urlsplit(self.url).scheme}://{urlsplit(self.url).netloc}"
        self.output = Path(output)
        self.delay = delay
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_products = max_products
        self.max_image_bytes = max_image_mb * 1024 * 1024
        self.last_request = 0.0
        self.robots = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def same_site(self, url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.hostname.removeprefix("www.") == self.host

    def _polite_pause(self) -> None:
        remaining = self.delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request = time.monotonic()

    def allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        key = f"{parsed.scheme}://{parsed.netloc}"
        if key not in self.robots:
            robots_url = key + "/robots.txt"
            self._polite_pause()
            try:
                response = self.session.get(robots_url, timeout=self.timeout)
                parser = RobotFileParser()
                if response.status_code in (401, 403):
                    parser.disallow_all = True
                elif response.status_code == 404:
                    parser.allow_all = True
                elif response.ok:
                    parser.parse(response.text.splitlines())
                else:
                    LOG.warning("Cannot verify robots.txt (%s): %s", response.status_code, robots_url)
                    parser.disallow_all = True
            except requests.RequestException as exc:
                LOG.warning("Cannot verify robots.txt for %s: %s", key, exc)
                parser = RobotFileParser()
                parser.disallow_all = True
            self.robots[key] = parser
        return self.robots[key].can_fetch(USER_AGENT, url)

    def fetch(self, url: str, *, image: bool = False):
        if not image and not self.same_site(url):
            raise ValueError(f"Refusing external page: {url}")
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows: {url}")
        self._polite_pause()
        response = self.session.get(url, timeout=self.timeout, stream=image)
        response.raise_for_status()
        if not image and not self.same_site(response.url):
            raise ValueError(f"Redirected off-site: {response.url}")
        return response

    def page(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.fetch(url).text, "html.parser")

    def categories(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        result = {}
        # Menus first; category links elsewhere are useful for sites with custom headers.
        candidates = soup.select("header a[href], nav a[href], [role=navigation] a[href]")
        candidates += soup.select("a[href]")
        for a in candidates:
            url = absolute_url(self.base, a.get("href"))
            if not url or not self.same_site(url):
                continue
            path = urlsplit(url).path.rstrip("/")
            if not any(path.startswith(prefix) for prefix in CATEGORY_PREFIXES):
                continue
            category_id = path.split("/")[-1]
            if category_id in ("all", "frontpage", "home", "featured") or not category_id:
                continue
            category_url = self.base + path
            title = category_id.replace("-", " ").title()
            result.setdefault(category_url, title)
        return [(name, url) for url, name in result.items()]

    def listing(self, category_url: str) -> list[tuple[str, str | None]]:
        products = {}
        old_sets = set()
        for number in range(1, self.max_pages + 1):
            p = urlsplit(category_url)
            params = dict(parse_qsl(p.query, keep_blank_values=True))
            if number > 1:
                params["page"] = str(number)
            page_url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))
            try:
                soup = self.page(page_url)
            except (requests.RequestException, ValueError, PermissionError) as exc:
                LOG.warning("Stopping collection %s page %d: %s", category_url, number, exc)
                break
            found = {}
            for a in soup.select("a[href]"):
                url = absolute_url(self.base, a.get("href"))
                if not url or not self.same_site(url):
                    continue
                path = urlsplit(url).path
                if not any(path.startswith(prefix) for prefix in PRODUCT_PREFIXES):
                    continue
                product_url = self.base + path.rstrip("/")
                parent = a.find_parent(class_=re.compile(r"product|card|grid", re.I))
                img = a.find("img") or (parent.find("img") if parent else None)
                thumbnail = absolute_url(page_url, first_image_source(img)) if img else None
                if product_url not in found or (not found[product_url] and thumbnail):
                    found[product_url] = thumbnail
            signature = frozenset(found)
            if not found or signature in old_sets:
                break
            old_sets.add(signature)
            for url, thumbnail in found.items():
                products.setdefault(url, thumbnail)
            if self.max_products and len(products) >= self.max_products:
                break
        values = list(products.items())
        return values[:self.max_products] if self.max_products else values

    def product_images(self, url: str, soup: BeautifulSoup, shopify: dict, ld: dict) -> list[dict]:
        candidates = []
        for image in shopify.get("images", []):
            candidates.append(image if isinstance(image, dict) else {"url": image})
        if not candidates:
            ld_images = ld.get("image", [])
            for image in (ld_images if isinstance(ld_images, list) else [ld_images]):
                candidates.append(image if isinstance(image, dict) else {"url": image})
        # Product-specific HTML galleries; do not scrape logos, banners or recommendations.
        gallery = []
        for selector in GALLERY_SELECTORS:
            gallery.extend(soup.select(selector))
        for tag in gallery:
            img_url = absolute_url(url, first_image_source(tag))
            if img_url:
                candidates.append({"url": img_url, "alt": tag.get("alt") or tag.get("title")})
        if not candidates:
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image:
                candidates.append({"url": og_image.get("content")})
        result = {}
        for item in candidates:
            image_url = absolute_url(url, item.get("url") or item.get("src"))
            if not image_url:
                continue
            key = canonical_image(image_url)
            alt = clean_text(item.get("alt") or item.get("description"))
            if key not in result:
                result[key] = {"url": image_url, "alt": alt or None, "description": alt or None, "file": None}
            elif alt and not result[key]["alt"]:
                result[key]["alt"] = alt
                result[key]["description"] = alt
        return list(result.values())

    def extract_product(self, url: str, soup: BeautifulSoup, category: str) -> dict:
        ld = get_product_ld(soup)
        shopify = {}
        if urlsplit(url).path.startswith("/products/"):
            try:
                response = self.fetch(url + ".js")
                shopify = response.json()
                if not isinstance(shopify, dict) or "title" not in shopify:
                    shopify = {}
            except (requests.RequestException, ValueError, PermissionError):
                LOG.debug("Shopify product JSON unavailable for %s", url)
        h1 = soup.select_one("h1")
        title = clean_text(shopify.get("title") or ld.get("name") or (h1.get_text(" ", strip=True) if h1 else ""))
        body = shopify.get("description") or shopify.get("body_html")
        if not body:
            description_el = soup.select_one('[itemprop="description"], .product__description, .product-description, .woocommerce-product-details__short-description, [class*="product-description"]')
            body = str(description_el) if description_el else ld.get("description", "")
        offers = ld.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) and offers else offers
        offers = offers if isinstance(offers, dict) else {}
        currency_meta = soup.select_one('meta[property="product:price:currency"]')
        currency = offers.get("priceCurrency") or (currency_meta.get("content") if currency_meta else None)
        variants = []
        for item in shopify.get("variants", []):
            variants.append({
                "name": item.get("public_title") or item.get("title"),
                "price": (item.get("price") / 100) if isinstance(item.get("price"), (int, float)) else None,
                "available": item.get("available"),
                "sku": item.get("sku"),
            })
        price = min((v["price"] for v in variants if v["price"] is not None), default=None)
        if price is None:
            raw_price = offers.get("price") or (soup.select_one('meta[property="product:price:amount"]') or {}).get("content")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (ValueError, TypeError):
                price = None
        if not currency and shopify:
            # Storefront product.js has amounts but no currency; leave unspecified instead of guessing.
            currency = None
        images = self.product_images(url, soup, shopify, ld)
        features = [clean_text(li.get_text(" ", strip=True)) for li in BeautifulSoup(body or "", "html.parser").select("li")]
        return {
            "category": category, "name": title or urlsplit(url).path.split("/")[-1].replace("-", " ").title(),
            "source_url": url, "description": html_text(body), "features": features,
            "specifications": extract_tables(body), "price": price, "currency": currency,
            "vendor": shopify.get("vendor") or (ld.get("brand", {}).get("name") if isinstance(ld.get("brand"), dict) else None),
            "product_type": shopify.get("type") or shopify.get("product_type"),
            "options": shopify.get("options", []), "variants": variants, "thumbnail": None,
            "images": images, "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    def download_image(self, url: str, target: Path) -> str | None:
        try:
            with self.fetch(url, image=True) as response:
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in IMAGE_TYPES:
                    LOG.warning("Skipping non-image response: %s (%s)", url, content_type)
                    return None
                if int(response.headers.get("Content-Length", "0")) > self.max_image_bytes:
                    LOG.warning("Skipping image over size limit: %s", url)
                    return None
                filename = target.with_suffix(IMAGE_TYPES[content_type])
                if filename.is_file() and filename.stat().st_size:
                    return filename.name
                filename.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with filename.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        written += len(chunk)
                        if written > self.max_image_bytes:
                            raise ValueError("image exceeds size limit")
                        file.write(chunk)
                if not written:
                    filename.unlink(missing_ok=True)
                    return None
                return filename.name
        except (requests.RequestException, PermissionError, ValueError, OSError) as exc:
            LOG.warning("Cannot download image %s: %s", url, exc)
            for path in target.parent.glob(target.name + ".*"):
                path.unlink(missing_ok=True)
            return None

    def save_product(self, category: str, url: str, thumbnail_url: str | None) -> dict:
        soup = self.page(url)
        product = self.extract_product(url, soup, category)
        handle = urlsplit(url).path.rstrip("/").split("/")[-1]
        folder = self.output / slug(category) / slug(handle)
        folder.mkdir(parents=True, exist_ok=True)
        for number, image in enumerate(product["images"], 1):
            name = self.download_image(image["url"], folder / "images" / f"image-{number:02d}")
            if name:
                image["file"] = str(Path("images") / name)
        thumb_url = thumbnail_url or (product["images"][0]["url"] if product["images"] else None)
        if thumb_url:
            # Reuse the full-resolution first gallery file when URLs refer to the same photo.
            match = next((item for item in product["images"] if item["file"] and canonical_image(item["url"]) == canonical_image(thumb_url)), None)
            if match:
                source = folder / match["file"]
                thumb_file = "thumbnail" + source.suffix
                shutil.copyfile(source, folder / thumb_file)
            else:
                thumb_file = self.download_image(thumb_url, folder / "thumbnail")
            product["thumbnail"] = {"url": thumb_url, "file": thumb_file}
        with (folder / "product.json").open("w", encoding="utf-8") as f:
            json.dump(product, f, ensure_ascii=False, indent=2)
        LOG.info("Saved %s (%d images)", folder, sum(bool(x["file"]) for x in product["images"]))
        return {"category": category, "name": product["name"], "price": product["price"],
                "currency": product["currency"], "description": product["description"],
                "source_url": url, "folder": str(folder),
                "image_count": sum(bool(x["file"]) for x in product["images"])}

    def run(self, category_filter: str | None = None) -> list[dict]:
        self.output.mkdir(parents=True, exist_ok=True)
        path = urlsplit(self.url).path
        if any(path.startswith(prefix) for prefix in PRODUCT_PREFIXES):
            items = [("uncategorized", self.url, None)]
            categories = []
        elif any(path.startswith(prefix) for prefix in CATEGORY_PREFIXES):
            categories = [(path.rstrip("/").split("/")[-1].replace("-", " ").title(), self.url)]
            items = []
        else:
            soup = self.page(self.url)
            categories = self.categories(soup)
            items = []
            if not categories:
                # Shopify collection index if the menu is rendered entirely by JavaScript.
                try:
                    categories = self.categories(self.page(self.base + "/collections"))
                except (requests.RequestException, ValueError, PermissionError):
                    pass
        if category_filter:
            categories = [(name, url) for name, url in categories if category_filter.lower() in (name + " " + url).lower()]
        LOG.info("Found %d categories", len(categories))
        for category, url in categories:
            links = self.listing(url)
            LOG.info("%s: %d products", category, len(links))
            items.extend((category, product_url, thumb) for product_url, thumb in links)
        results = []
        for category, url, thumb in items:
            try:
                results.append(self.save_product(category, url, thumb))
            except (requests.RequestException, ValueError, PermissionError, OSError) as exc:
                LOG.error("Skipping %s: %s", url, exc)
        catalog = self.output / "catalog.csv"
        fields = ["category", "name", "price", "currency", "description", "source_url", "folder", "image_count"]
        with catalog.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(results)
        LOG.info("Done: %d products. Catalog: %s", len(results), catalog)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape public product data and galleries by category")
    parser.add_argument("url", nargs="?", help="Website, collection or product URL (prompted if omitted)")
    parser.add_argument("--output", default="downloads", help="Destination directory")
    parser.add_argument("--category", help="Only categories whose name/URL contains this text, e.g. beds")
    parser.add_argument("--max-products", type=int, default=0, help="Max products per category (0 = all)")
    parser.add_argument("--max-pages", type=int, default=100, help="Max collection pages per category")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between requests")
    args = parser.parse_args()
    url = args.url or input("Website URL: ").strip()
    if args.max_products < 0 or args.max_pages < 1 or args.delay < 0:
        parser.error("max-products must be >=0, max-pages >=1, and delay >=0")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    scraper = CatalogScraper(url, Path(args.output), delay=args.delay,
                             max_pages=args.max_pages, max_products=args.max_products)
    scraper.run(args.category)


if __name__ == "__main__":
    main()
