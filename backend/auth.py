import asyncio
from playwright.async_api import async_playwright

async def generate_session():
    async with async_playwright() as p:
        # headless=False means you can actually see the browser
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        print("Opening AliExpress...")
        await page.goto("https://www.aliexpress.com")
        
        print("\n*** ACTION REQUIRED ***")
        print("1. Log into your account if you want.")
        print("2. Ensure your shipping destination/currency is correct.")
        print("3. Solve any slide-captchas that appear.")
        input("Press ENTER in this console when you are completely done... ")
        
        # This saves your cookies, local storage, and session data
        await context.storage_state(path="aliexpress_state.json")
        print("Session successfully saved to 'aliexpress_state.json'!")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(generate_session())