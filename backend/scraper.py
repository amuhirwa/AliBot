import asyncio
import re
import pandas as pd
import numpy as np
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


async def sweep_search_page(
    search_term: str,
    negative_keywords: list,
    min_sales: int,
    price_floor_pct: float,
    progress_cb,
) -> list:
    """
    Scrape AliExpress search results for the given term.
    Returns a list of product dicts: Title, Price, Sales, URL, Image.
    """
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="aliexpress_state.json")
        page = await context.new_page()

        formatted_term = search_term.replace(" ", "+")
        target_url = f"https://www.aliexpress.com/wholesale?SearchText={formatted_term}"

        progress_cb(2, f"Navigating to AliExpress: {search_term}", "Phase 1: Search Scraping")
        await page.goto(target_url)
        await page.wait_for_timeout(4000)

        # Scroll to load all lazy images and product cards
        progress_cb(8, "Scrolling page to load all products...")
        for _ in range(8):
            await page.evaluate("window.scrollBy(0, 1000)")
            await page.wait_for_timeout(1500)

        progress_cb(18, "Page fully loaded. Extracting product cards...")

        raw_products = []
        product_elements = await page.locator('a[href*="/item/"]').all()
        progress_cb(20, f"Found {len(product_elements)} product links on the page.")

        for el in product_elements:
            try:
                text_content = await el.inner_text()
                link = await el.get_attribute("href")

                if not text_content or not link:
                    continue

                # Normalize link
                if link.startswith("//"):
                    link = "https:" + link
                link = link.split(".html")[0] + ".html"

                # Extract thumbnail image
                image_url = None
                try:
                    img_el = el.locator("img").first
                    image_url = await img_el.get_attribute("src")
                    if not image_url:
                        image_url = await img_el.get_attribute("data-src")
                except Exception:
                    pass

                # Price extraction strategy:
                # RWF prices are always in the thousands+, so we require either:
                #   a) A comma-formatted number (e.g. 12,500 or 1,080,280) — the
                #      standard AliExpress display format for large numbers, or
                #   b) "RWF" as an explicit anchor followed by any digit sequence.
                # This prevents matching small stray numbers in titles/specs (e.g.
                # "60 Inch tripod" → 60, "70 sold" → 70).
                price_match = re.search(
                    r"RWF\s*([0-9]+(?:,[0-9]{3})*)", text_content
                ) or re.search(
                    r"([0-9]{1,3}(?:,[0-9]{3})+)", text_content
                )
                if price_match:
                    price = float(price_match.group(1).replace(",", ""))
                else:
                    price = None

                # --- Bug fix 2: normalize "1K+", "2.5K", "10K+" → raw digit strings ---
                text_normalized = re.sub(
                    r"(\d+(?:\.\d+)?)\s*[Kk]\+?\s*sold",
                    lambda m: f"{int(float(m.group(1)) * 1000)} sold",
                    text_content,
                )
                sales_match = re.search(r"(\d+)\+?\s*sold", text_normalized.lower())
                sales = int(sales_match.group(1)) if sales_match else 0

                if price:
                    raw_products.append(
                        {
                            "Title": text_content.split("\n")[0],
                            "Price": price,
                            "Sales": sales,
                            "URL": link,
                            "Image": image_url,
                        }
                    )
            except Exception as e:
                print(f"Error parsing product card: {e}")
                continue

        await browser.close()

    progress_cb(25, f"Extracted {len(raw_products)} raw items. Applying filters...")

    if not raw_products:
        progress_cb(35, "No products extracted. AliExpress may have changed their layout.")
        return []

    df = pd.DataFrame(raw_products)

    # Filter 1: Negative keywords
    if negative_keywords:
        pattern = "|".join(re.escape(k) for k in negative_keywords if k)
        if pattern:
            df = df[~df["Title"].str.lower().str.contains(pattern, na=False)]

    # Filter 2: Minimum sales
    df = df[df["Sales"] >= min_sales]

    # Filter 3: 30% median price floor
    if not df.empty:
        median_price = df["Price"].median()
        price_floor = median_price * (price_floor_pct / 100.0)
        df = df[df["Price"] >= price_floor]
        progress_cb(30, f"Median price: {median_price:,.0f} RWF. Dropped items below {price_floor:,.0f} RWF.")

    # Remove duplicate URLs
    df = df.drop_duplicates(subset=["URL"])

    candidates = df.to_dict(orient="records")
    progress_cb(35, f"Filtered to {len(candidates)} candidates.")
    return candidates


# ---------------------------------------------------------------------------
# Standalone entry point (for testing without the web app)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    SEARCH_TERM = "arduino starter kit"
    NEGATIVE_KEYWORDS = ["case", "cable", "cover", "box", "empty"]
    MIN_SALES = 10
    PRICE_FLOOR_PCT = 30.0

    def cli_progress(pct, msg, phase=""):
        print(f"[{pct:3d}%] {phase + ': ' if phase else ''}{msg}")

    results = asyncio.run(
        sweep_search_page(SEARCH_TERM, NEGATIVE_KEYWORDS, MIN_SALES, PRICE_FLOOR_PCT, cli_progress)
    )
    pd.DataFrame(results).to_csv("candidates.csv", index=False)
    print(f"\nSaved {len(results)} candidates to candidates.csv")
