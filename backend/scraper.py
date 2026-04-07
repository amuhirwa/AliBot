import asyncio
import re
import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


async def sweep_search_page(
    search_term: str,
    negative_keywords: list,
    min_sales: int,
    price_floor_pct: float,
    progress_cb,
    num_pages: int = 1,
) -> list:
    """
    Scrape AliExpress search results across num_pages pages.
    Returns a filtered list of product dicts: Title, Price, Sales, URL, Image.
    """
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="aliexpress_state.json")
        page = await context.new_page()

        formatted_term = search_term.replace(" ", "+")
        all_cards = []

        for page_num in range(1, num_pages + 1):
            # Progress slice: pages share the 2%–28% band equally
            page_start_pct = 2 + int((page_num - 1) / num_pages * 26)
            page_end_pct   = 2 + int(page_num / num_pages * 26)

            url = (
                f"https://www.aliexpress.com/wholesale"
                f"?SearchText={formatted_term}&page={page_num}"
            )

            progress_cb(
                page_start_pct,
                f"Page {page_num}/{num_pages} — navigating...",
                "Phase 1: Search Scraping",
            )
            await page.goto(url)
            await page.wait_for_timeout(4000)

            # Scroll to trigger lazy-loading
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, 1000)")
                await page.wait_for_timeout(1000)

            # Single JS round-trip: collect href, text, and image src for every
            # product link. This replaces the old per-element loop that was making
            # ~5 CDP calls per element (dozens of seconds for 72 elements).
            cards = await page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('a[href*="/item/"]').forEach(el => {
                        const img = el.querySelector('img');
                        results.push({
                            href:   el.getAttribute('href') || '',
                            text:   el.innerText || '',
                            imgSrc: img
                                     ? (img.getAttribute('src') ||
                                        img.getAttribute('data-src') ||
                                        img.getAttribute('data-lazy-src') || '')
                                     : ''
                        });
                    });
                    return results;
                }
            """)

            progress_cb(
                page_end_pct,
                f"Page {page_num}/{num_pages} — found {len(cards)} links.",
            )
            all_cards.extend(cards)

        await browser.close()

    progress_cb(28, f"Collected {len(all_cards)} raw links across {num_pages} page(s). Parsing...")

    # ---------------------------------------------------------------------------
    # Parse the raw JS data in Python (no more browser round-trips)
    # ---------------------------------------------------------------------------
    raw_products = []
    for card in all_cards:
        try:
            link = card["href"]
            text_content = card["text"]
            image_url = card["imgSrc"] or None

            if not text_content or not link:
                continue

            # Normalize link
            if link.startswith("//"):
                link = "https:" + link
            link = link.split(".html")[0] + ".html"

            # Price: require comma-formatted number (≥1,000) or explicit RWF prefix.
            # Avoids matching stray small numbers in titles/specs/dimensions.
            price_match = re.search(
                r"RWF\s*([0-9]+(?:,[0-9]{3})*)", text_content
            ) or re.search(
                r"([0-9]{1,3}(?:,[0-9]{3})+)", text_content
            )
            price = float(price_match.group(1).replace(",", "")) if price_match else None

            # Normalize "1K+", "2.5K", "10K+" sold → plain integers before matching
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
            print(f"Error parsing card: {e}")
            continue

    progress_cb(30, f"Parsed {len(raw_products)} products with prices. Applying filters...")

    if not raw_products:
        progress_cb(35, "No products extracted. AliExpress may have changed their layout.")
        return []

    df = pd.DataFrame(raw_products)

    # Deduplicate across pages before filtering
    df = df.drop_duplicates(subset=["URL"])

    # Filter 1: Negative keywords
    if negative_keywords:
        pattern = "|".join(re.escape(k) for k in negative_keywords if k)
        if pattern:
            df = df[~df["Title"].str.lower().str.contains(pattern, na=False)]

    # Filter 2: Minimum sales
    df = df[df["Sales"] >= min_sales]

    # Filter 3: Price floor (% of median)
    if not df.empty:
        median_price = df["Price"].median()
        price_floor = median_price * (price_floor_pct / 100.0)
        df = df[df["Price"] >= price_floor]
        progress_cb(33, f"Median price: {median_price:,.0f} RWF. Dropped items below {price_floor:,.0f} RWF.")

    candidates = df.to_dict(orient="records")
    progress_cb(35, f"Filtered to {len(candidates)} candidates.")
    return candidates


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    SEARCH_TERM = "tripod"
    NEGATIVE_KEYWORDS = ["case", "cable", "cover", "box", "empty"]
    MIN_SALES = 10
    PRICE_FLOOR_PCT = 30.0
    NUM_PAGES = 3

    def cli_progress(pct, msg, phase=""):
        print(f"[{pct:3d}%] {phase + ': ' if phase else ''}{msg}")

    results = asyncio.run(
        sweep_search_page(
            SEARCH_TERM, NEGATIVE_KEYWORDS, MIN_SALES, PRICE_FLOOR_PCT,
            cli_progress, num_pages=NUM_PAGES,
        )
    )
    pd.DataFrame(results).to_csv("candidates.csv", index=False)
    print(f"\nSaved {len(results)} candidates to candidates.csv")
