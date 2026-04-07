import asyncio
import re
import random
import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


async def analyze_products(
    candidates: list,
    include_seller_score: bool,
    include_shipping_time: bool,
    include_choice: bool,
    progress_cb,
) -> list:
    """
    Visit each candidate product page and extract detailed data.
    Enriches the candidates list in-place and returns it.
    """
    if not candidates:
        return []

    total = len(candidates)

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="aliexpress_state.json")
        page = await context.new_page()

        for i, product in enumerate(candidates):
            url = product.get("URL", "")
            pct = 35 + int((i / total) * 45)
            progress_cb(pct, f"Deep dive {i + 1}/{total}: {product.get('Title', '')[:50]}...", "Phase 2: Deep Dive")

            try:
                await page.goto(url)
                await page.wait_for_timeout(4000)

                # --- Bug fix 4: scroll before extracting so lazy-loaded content renders ---
                await page.evaluate("window.scrollBy(0, 2000)")
                await page.wait_for_timeout(2000)

                body_text = await page.inner_text("body")

                # --- Extract high-res product image from og:image meta tag ---
                og_image = None
                try:
                    og_image = await page.get_attribute('meta[property="og:image"]', "content")
                except Exception:
                    pass
                # Use og:image if available, otherwise fall back to thumbnail from search
                product["Image_URL"] = og_image or product.get("Image")

                # --- Extract shipping cost ---
                if re.search(r"free\s*shipping", body_text, re.IGNORECASE):
                    product["Shipping_RWF"] = 0.0
                else:
                    shipping_match = re.search(
                        r"(?:delivery|shipping).*?(?:RWF)?\s*([0-9]+(?:,[0-9]{3})*)",
                        body_text,
                        re.IGNORECASE,
                    )
                    if shipping_match:
                        product["Shipping_RWF"] = float(shipping_match.group(1).replace(",", ""))
                    else:
                        product["Shipping_RWF"] = None

                # --- Bug fix 3: context-anchored rating extraction ---
                rating = None
                # First try: explicit "X.X out of 5"
                m = re.search(r"([0-5]\.\d)\s*out\s*of\s*5", body_text, re.IGNORECASE)
                if m:
                    rating = float(m.group(1))
                else:
                    # Second try: number followed by "stars" or "rating"
                    m = re.search(r"([0-5]\.\d)\s*(?:stars?|rating)", body_text, re.IGNORECASE)
                    if m:
                        rating = float(m.group(1))
                # Clamp to valid range
                if rating is not None and not (0.0 <= rating <= 5.0):
                    rating = None
                product["Rating"] = rating

                # --- Extract review count ---
                review_match = re.search(
                    r"([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s*(?:customer\s*)?reviews?",
                    body_text,
                    re.IGNORECASE,
                )
                product["Review_Count"] = (
                    int(review_match.group(1).replace(",", "")) if review_match else 0
                )

                # --- Optional: seller positive feedback score ---
                if include_seller_score:
                    seller_match = re.search(
                        r"(\d+(?:\.\d+)?)\s*%\s*[Pp]ositive", body_text
                    )
                    product["Seller_Score"] = (
                        float(seller_match.group(1)) if seller_match else None
                    )

                # --- Optional: estimated shipping / delivery time ---
                if include_shipping_time:
                    delivery_match = re.search(
                        r"(?:Delivery|Arrives?|Ships?\s+in)\s*:?\s*"
                        r"([A-Za-z]{3}\s+\d{1,2}(?:\s*[-\u2013]\s*[A-Za-z]{3}\s+\d{1,2})?|\d+[-\u2013]\d+\s+days?)",
                        body_text,
                        re.IGNORECASE,
                    )
                    product["Shipping_Time"] = (
                        delivery_match.group(1).strip() if delivery_match else None
                    )

                # --- Optional: AliExpress Choice badge ---
                if include_choice:
                    product["Choice_Badge"] = bool(
                        re.search(r"\bAliExpress\s+Choice\b", body_text, re.IGNORECASE)
                    )

            except Exception as e:
                print(f"Error loading {url}: {e}")
                product.setdefault("Image_URL", product.get("Image"))
                product.setdefault("Shipping_RWF", None)
                product.setdefault("Rating", None)
                product.setdefault("Review_Count", 0)

            # Anti-bot delay
            delay = random.uniform(3, 7)
            await page.wait_for_timeout(delay * 1000)

        await browser.close()

    progress_cb(80, f"Deep dive complete for {total} products.")
    return candidates


# ---------------------------------------------------------------------------
# Standalone entry point (for testing without the web app)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    def cli_progress(pct, msg, phase=""):
        print(f"[{pct:3d}%] {phase + ': ' if phase else ''}{msg}")

    try:
        df = pd.read_csv("candidates.csv")
        candidates = df.to_dict(orient="records")
    except FileNotFoundError:
        print("candidates.csv not found. Run scraper.py first.")
        exit(1)

    results = asyncio.run(
        analyze_products(candidates, include_seller_score=True,
                         include_shipping_time=True, include_choice=True,
                         progress_cb=cli_progress)
    )
    pd.DataFrame(results).to_csv("scored_products.csv", index=False)
    print(f"\nSaved {len(results)} enriched products to scored_products.csv")
