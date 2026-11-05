import json

from scraper import CatalogScraper, canonical_image, extract_tables, slug


class FakeResponse:
    def __init__(self, text="", json_data=None, content=b"file", content_type="text/html", url="https://shop.test"):
        self.text = text
        self.data = json_data
        self.content = content
        self.url = url
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self.data is None:
            raise ValueError("not JSON")
        return self.data

    def iter_content(self, chunk_size=65536):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


HOME = '''<header><nav><a href="/collections/custom-made-beds">Beds</a>
<a href="/collections/sofas">Sofas</a></nav></header>'''
LISTING_1 = '''<div class="product-card"><a href="/products/twin-panel-bed"><img src="/cdn/bed-thumb.png"></a></div>
<a href="/products/second-bed">Second Bed</a>'''
LISTING_2 = '''<div class="product-card"><a href="/products/third-bed">Third Bed</a></div>'''
PRODUCT_HTML = '''<h1>Twin Panel Bed</h1><div class="product-gallery">
<img src="/cdn/bed-main.png" alt="Front of Twin Panel Bed">
<img src="/cdn/bed-main.png?width=100" alt="Duplicate"></div>
<meta property="product:price:currency" content="AED">'''
SHOPIFY = {"title": "Twin Panel Bed", "description": "<p>Soft bed with curved panels.</p><ul><li>Velvet</li></ul>",
    "images": ["https://shop.test/cdn/bed-main.png", "https://shop.test/cdn/bed-side.png"],
    "vendor": "Maker", "product_type": "Bed", "options": ["Size"],
    "variants": [{"title": "Queen", "price": 420000, "available": True, "sku": "BED-Q"}]}


def mock_scraper(tmp_path, monkeypatch):
    crawler = CatalogScraper("https://shop.test", tmp_path / "downloads", delay=0)
    monkeypatch.setattr(crawler, "allowed", lambda url: True)
    seen = []

    def fetch(url, image=False):
        seen.append(url)
        if image:
            return FakeResponse(content=b"PNG", content_type="image/png", url=url)
        if url == "https://shop.test":
            return FakeResponse(HOME, url=url)
        if url == "https://shop.test/collections/custom-made-beds":
            return FakeResponse(LISTING_1, url=url)
        if url == "https://shop.test/collections/custom-made-beds?page=2":
            return FakeResponse(LISTING_2, url=url)
        if url == "https://shop.test/collections/custom-made-beds?page=3":
            return FakeResponse(LISTING_2, url=url)  # unchanged page -> stop
        if url.endswith(".js"):
            return FakeResponse(json_data=SHOPIFY, url=url)
        if "/products/" in url:
            return FakeResponse(PRODUCT_HTML, url=url)
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(crawler, "fetch", fetch)
    return crawler, seen


def test_category_filter_product_folder_images_and_csv(tmp_path, monkeypatch):
    crawler, seen = mock_scraper(tmp_path, monkeypatch)
    results = crawler.run("beds")
    assert len(results) == 3
    assert "https://shop.test/collections/custom-made-beds?page=2" in seen
    folder = tmp_path / "downloads" / "custom-made-beds" / "twin-panel-bed"
    assert folder.exists()
    data = json.loads((folder / "product.json").read_text())
    assert data["name"] == "Twin Panel Bed"
    assert data["price"] == 4200
    assert data["currency"] == "AED"
    assert data["description"].startswith("Soft bed")
    assert data["features"] == ["Velvet"]
    assert len(data["images"]) == 2
    assert all((folder / image["file"]).exists() for image in data["images"])
    assert data["images"][0]["description"] == "Front of Twin Panel Bed"
    assert (folder / data["thumbnail"]["file"]).exists()
    assert (tmp_path / "downloads" / "catalog.csv").exists()


def test_product_jsonld_fallback_and_tables(tmp_path, monkeypatch):
    html = '''<h1>Fallback</h1><script type="application/ld+json">
    {"@context":"https://schema.org", "@type":"Product", "name":"JSON LD bed",
     "description":"Comfortable fabric", "image":["https://shop.test/a.png"],
     "offers":{"price":"199.50","priceCurrency":"AED"}}
    </script>'''
    crawler = CatalogScraper("https://shop.test/product/bed", tmp_path, delay=0)
    monkeypatch.setattr(crawler, "fetch", lambda url, image=False: FakeResponse(
        text=html, content=b"PNG", content_type="image/png" if image else "text/html", url=url))
    assert crawler.run()[0]["price"] == 199.5
    data = json.loads((tmp_path / "uncategorized" / "bed" / "product.json").read_text())
    assert data["name"] == "JSON LD bed"
    assert data["images"][0]["description"] is None
    assert extract_tables("<table><tr><th>Size</th><th>Width</th></tr><tr><td>King</td><td>200</td></tr></table>") == [[{"Size": "King", "Width": "200"}]]


def test_slug_and_dedupe():
    assert slug("Modern Bed / 1") == "modern-bed-1"
    assert canonical_image("https://shop.test/abc.png?width=100") == canonical_image("https://shop.test/abc.png?width=1000")
