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
            await page.goto(STUDIO_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            url = page.url
            print(f"Landed on: {url}")
            if "accounts.google.com" in url or "/signin" in url.lower():
                print("-> Bounced to Google sign-in. Session was rejected/expired.")
            elif "/channel/" in url:
                print("-> Looks valid (contains /channel/).")
            else:
                print("-> Neither signin nor /channel/ in URL. Unexpected page state.")
                title = await page.title()
                print(f"Page title: {title}")
        except Exception as e:
            print(f"EXCEPTION: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
