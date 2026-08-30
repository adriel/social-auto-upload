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

            for wait_s in (3, 6, 10):
                await page.wait_for_timeout(3000)
                print(f"--- after ~{wait_s}s: url = {page.url}")

            # Look for logged-in-only DOM signals
            avatar = await page.locator("#avatar-btn, ytcp-header ytcp-account-avatar").count()
            signin_link = await page.locator("a[href*='ServiceLogin'], a[href*='accounts.google.com']").count()
            print(f"avatar elements found: {avatar}")
            print(f"sign-in links found: {signin_link}")

            body_text = await page.locator("body").inner_text()
            snippet = body_text[:300].replace("\n", " | ")
            print(f"body text snippet: {snippet}")

            await page.screenshot(path="studio_debug.png", full_page=False)
            print("Saved screenshot to studio_debug.png")

        except Exception as e:
            print(f"EXCEPTION: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
