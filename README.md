# Web Scrapper — product images and structured data

Enter a shop URL and collect its **public product catalog** by category: product titles, descriptions, prices (when available), variants, specifications, listing thumbnails, and product-gallery images. Each product is saved in its own folder. No raw HTML or page headings are exported.

This first version targets **Shopify-style collections** (such as Frame Interiors UAE) and has basic HTML/JSON-LD support for other traditional ecommerce sites. It does **not** promise to work on every site: login-only content, heavy JavaScript frontends, anti-bot challenges, and unusual site layouts may require a site-specific adapter.

## Install

Python 3.10+ is required.

~~~bash
git clone https://github.com/summiya/web-scrapper.git
cd web-scrapper
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
~~~

## Run

For a whole shop (the program discovers collection links from the navigation menu, then visits every listing page and product detail page):

~~~bash
python scraper.py https://frameinteriorsuae.com --output downloads
~~~

Only beds, for a quick first test:

~~~bash
python scraper.py https://frameinteriorsuae.com --category beds --max-products 2
~~~

Or run 'python scraper.py' and paste a website URL when prompted. You can also pass a collection or a single product URL directly.

Options: --category TEXT, --max-products N (per category; 0 means unlimited), --max-pages N (default 100), --delay SECONDS (default 1), --output DIR.

## Output

~~~text
downloads/
├── catalog.csv
└── custom-made-beds/
    └── extended-twin-panel-upholstered-bed-with-curved-headboard/
        ├── thumbnail.png
        ├── product.json
        └── images/
            ├── image-01.png
            ├── image-02.png
            └── image-03.png
~~~

Actual image extensions depend on the server response. Product JSON includes category, name, description, features, specifications (HTML tables as structured records), price, currency, vendor, options, variants, and an images array containing original URLs, saved file paths, and existing alt/image descriptions. When the site provides no distinct description for an image, that field remains null: the scraper **does not invent image descriptions**. The catalog CSV summarizes each product for import into other tools.

Images are deduplicated per product, and existing downloads are reused where possible. Interrupted runs may be rerun.

## Responsible use

Scrape only public pages you are authorized to collect. The scraper checks robots.txt for pages and images, uses a one-second minimum delay and retry backoff, and will not bypass a login, CAPTCHA, rate limit, or access control. Check the target site's terms and image/content copyright before reuse, especially for commercial purposes. The resulting images and text are **not automatically licensed for republication**.

## Tests

~~~bash
pip install -r requirements-dev.txt
python -m pytest -q
~~~
