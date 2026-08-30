import asyncio
import sys
from patchright.async_api import async_playwright

STUDIO_URL = "https://studio.youtube.com"


async def main(account_file: str):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            page = await context.new_page()

            ua = await page.evaluate("() => navigator.userAgent")
            webdriver = await page.evaluate("() => navigator.webdriver")
            print(f"navigator.userAgent: {ua}")
            print(f"navigator.webdriver: {webdriver}")

            await page.goto(STUDIO_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            print(f"url: {page.url}")

        except Exception as e:
            print(f"EXCEPTION: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
