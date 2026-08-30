# -*- coding: utf-8 -*-
"""YouTube uploader (browser automation via YouTube Studio).

Unlike the other platforms here, YouTube also offers an official Data API. We deliberately
use browser automation instead, because videos uploaded through an *unaudited* API project
are force-locked to private and cannot be made public without passing Google's compliance
audit (which is impractical for personal/single-channel use). Browser automation has no such
restriction and can publish public videos right away, and it matches the cookie-based pattern
used by every other uploader in this project.

Login is interactive (Google account, no QR code): the browser opens, the user signs in, and
the storage_state is saved. Reuse it afterwards for fully unattended uploads.
"""
import asyncio
import re
from pathlib import Path

from patchright.async_api import Page, Playwright, async_playwright

from conf import DEBUG_MODE
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.log import youtube_logger

try:
    # Chrome launched by Patchright may not use the system proxy. Set YT_PROXY in
    # conf.py (for example, "http://127.0.0.1:7890") when an explicit proxy is needed.
    from conf import YT_PROXY
except Exception:
    YT_PROXY = None

STUDIO_URL = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"
VISIBILITY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _build_login_result(success, status, message, account_file, current_url=""):
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "current_url": current_url,
    }


async def cookie_auth(account_file) -> bool:
    """Return whether the saved session opens a YouTube Studio channel."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(
                storage_state=account_file,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
                ),
            )
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(STUDIO_URL, wait_until="domcontentloaded")
            for _ in range(60):
                url = page.url
                if "accounts.google.com" in url or "/signin" in url.lower():
                    return False
                if "/channel/" in url:
                    return True
                await page.wait_for_timeout(250)
            return False
        except Exception:
            return False
        finally:
            await browser.close()


async def youtube_cookie_gen(account_file, headless: bool = False):
    """Open an interactive login window and save its browser storage state."""
    async with async_playwright() as playwright:
        # Login must remain headed for passwords and two-factor authentication.
        browser = await playwright.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()
        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
        youtube_logger.info(_msg("🔐", "Sign in to Google / YouTube in the browser window; the session will be saved automatically"))
        ok = False
        for _ in range(600):  # Wait up to 10 minutes.
            if "/channel/" in page.url:
                await page.wait_for_timeout(2000)  # Allow final cookies to settle.
                ok = True
                break
            await asyncio.sleep(1)
        if ok:
            await context.storage_state(path=account_file)
            youtube_logger.success(_msg("✅", f"YouTube session saved: {account_file}"))
        else:
            youtube_logger.error(_msg("😵", "Login timed out; the session was not saved"))
        await browser.close()
        return _build_login_result(ok, "logged_in" if ok else "timeout",
                                   "Login succeeded" if ok else "Login timed out", account_file, page.url)


async def youtube_setup(account_file, handle: bool = False, return_detail: bool = False, headless: bool = False):
    """Validate the saved session and optionally open an interactive login."""
    if not Path(account_file).exists() or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "The session is missing or invalid", account_file)
            return result if return_detail else False
        youtube_logger.info(_msg("🥹", "The YouTube session is missing or invalid; opening the login window"))
        result = await youtube_cookie_gen(account_file, headless=headless)
        return result if return_detail else result["success"]
    result = _build_login_result(True, "cookie_valid", "The session is valid", account_file)
    return result if return_detail else True


async def _dismiss_autocomplete(page: Page):
    """Dismiss hashtag or mention autocomplete if it obscures later buttons.

    Blur the active field first. Press Escape only when a dropdown remains visible,
    because an unconditional Escape can close the entire upload dialog.
    """
    try:
        await page.evaluate("() => { const a = document.activeElement; if (a && a.blur) a.blur(); }")
    except Exception:
        pass
    try:
        dropdown = page.locator("tp-yt-iron-dropdown:visible")
        if await dropdown.count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    except Exception:
        pass


async def _fill_editable(page: Page, selector: str, text: str):
    """Clear and fill a YouTube Studio contenteditable title or description.

    Prefer fill() over typing character by character. A hashtag such as #Shorts
    can otherwise keep the autocomplete dropdown open and block later controls.
    """
    box = page.locator(selector).first
    await box.wait_for(state="visible", timeout=30000)
    await box.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    try:
        await box.fill(text)
    except Exception:
        await box.type(text, delay=6)  # Fallback for contenteditables that reject fill().
    await page.wait_for_timeout(400)
    await _dismiss_autocomplete(page)


async def _click_if_present(page: Page, selector: str, timeout: int = 4000) -> bool:
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.click(timeout=timeout)
        return True
    except Exception:
        return False


async def _select_visibility(page: Page, visibility: str, max_polls: int = 20) -> bool:
    """Select a visibility option and verify that Studio accepted it."""
    radio = page.locator(
        f"tp-yt-paper-radio-button[name='{VISIBILITY[visibility]}']"
    ).first
    await radio.wait_for(state="visible", timeout=10000)
    await radio.click()
    for _ in range(max_polls):
        if await radio.get_attribute("aria-checked") == "true":
            return True
        await page.wait_for_timeout(250)
    return False


async def _open_upload_page(page: Page):
    """Open YouTube's upload page and return its real file input when ready."""
    await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        raise RuntimeError("The YouTube session has expired; run the login command again")
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=60000)
    return file_input


async def _wait_upload_complete(page: Page, max_polls: int = 1800) -> bool:
    """Wait for the browser-to-YouTube file transfer to finish.

    YouTube enables Publish before the transfer reaches 100%, and content checks can
    start while the transfer is still running. Read every progress label and require
    either an explicit upload-complete marker or two consecutive polls where an
    observed "Uploading N%" status has disappeared. max_polls*1s is a 30-minute cap.
    """
    last = ""
    saw_uploading = False
    missing_after_upload = 0
    progress = page.locator(
        "ytcp-video-upload-progress, .progress-label, span.progress-label"
    )
    for _ in range(max_polls):
        try:
            texts = await progress.all_inner_texts()
            status = " | ".join(text.strip() for text in texts if text.strip())
        except Exception:
            try:
                status = (await progress.first.inner_text()).strip()
            except Exception:
                status = ""

        lower_status = status.lower()
        percent_match = re.search(r"\buploading(?:\s+video)?\s*(\d{1,3})%", lower_status)
        if percent_match:
            saw_uploading = True
            missing_after_upload = 0
            percent = min(int(percent_match.group(1)), 100)
            if percent >= 100:
                youtube_logger.info(_msg("✅", "File upload reached 100%"))
                return True
        elif any(marker in lower_status for marker in ("upload complete", "uploaded", "processing")):
            youtube_logger.info(_msg("✅", "File upload is complete"))
            return True
        elif saw_uploading:
            missing_after_upload += 1
            if missing_after_upload >= 2:
                youtube_logger.info(_msg("✅", "The upload progress indicator has completed"))
                return True

        if status and status != last:
            youtube_logger.info(_msg("⏳", f"Upload status: {status[:100]}"))
            last = status
        await page.wait_for_timeout(1000)
    youtube_logger.error(_msg("😵", "The file upload did not finish within 30 minutes"))
    return False


async def _publish_video(page: Page, max_polls: int = 240) -> str:
    """Publish the uploaded video and return its confirmed public URL."""
    done_button = page.locator("#done-button").first
    await done_button.wait_for(state="visible", timeout=15000)
    for _ in range(max_polls):
        if await done_button.is_enabled():
            break
        await page.wait_for_timeout(250)
    else:
        raise RuntimeError("The YouTube Publish button remained disabled; the video was not published")

    await done_button.click(timeout=15000)
    publish_anyway = page.locator(
        "ytcp-button:has-text('Publish anyway'), "
        "tp-yt-paper-button:has-text('Publish anyway'), "
        "button:has-text('Publish anyway')"
    ).first
    link = page.locator(
        "ytcp-video-share-dialog a[href*='youtu.be'], "
        "ytcp-video-share-dialog a[href*='watch?v=']"
    ).first
    for _ in range(max_polls):
        try:
            if await link.is_visible():
                return await link.get_attribute("href") or ""
        except Exception:
            pass
        try:
            if await publish_anyway.is_visible():
                youtube_logger.info(_msg("ℹ️", "YouTube checks are still running; selecting Publish anyway"))
                await publish_anyway.click(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(250)
    raise RuntimeError("YouTube did not confirm publication; the video may still be a draft")


class YouTubeVideo(BaseVideoUploader):
    def __init__(self, title, file_path, tags, account_file, *,
                 description="", thumbnail_path=None, playlist=None,
                 visibility="public", debug=DEBUG_MODE, headless=False):
        self.title = title
        self.file_path = str(file_path)
        self.tags = tags or []
        self.account_file = str(account_file)
        self.description = description or ""
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.playlist = playlist
        self.visibility = visibility if visibility in VISIBILITY else "public"
        self.debug = debug
        self.headless = headless

    async def upload(self, playwright: Playwright) -> None:
        browser = await playwright.chromium.launch(
            headless=self.headless, channel="chrome",
            proxy={"server": YT_PROXY} if YT_PROXY else None,
        )
        context = await browser.new_context(
            storage_state=self.account_file,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ) if self.headless else None,
        )
        context = await set_init_script(context)

        async def _bring_new_pages_to_front(new_page):
            """The "Verify that it's you" reauth step opens a NEW tab
            (window.open), which nothing here otherwise tracks or
            attaches to. Left alone, that new target can sit stuck --
            observed in practice as Chrome's own "Debugger paused in
            another tab" banner, which only clears once a human manually
            clicks over to that tab. Doing that click programmatically,
            the instant the tab exists, avoids needing a human to notice
            and switch to it themselves."""
            try:
                await new_page.bring_to_front()
            except Exception:
                pass

        context.on("page", lambda p: asyncio.create_task(_bring_new_pages_to_front(p)))

        page = await context.new_page()
        page.set_default_timeout(60000)

        youtube_logger.info(_msg("🎬", f"Starting upload: {Path(self.file_path).name}"))
        youtube_logger.info(_msg("🌐", "Opening the YouTube upload page"))
        file_input = await _open_upload_page(page)
        youtube_logger.info(_msg("✅", "Upload page ready; selecting the video file"))

        # 1) Select the video file.
        await file_input.set_input_files(self.file_path)

        # 2) Wait for the details editor.
        youtube_logger.info(_msg("⏳", "Waiting for the video details editor"))
        await page.locator("#title-textarea").wait_for(state="visible", timeout=120000)

        # 3) Title.
        youtube_logger.info(_msg("✍️", "Entering title"))
        await _fill_editable(page, "#title-textarea #textbox", self.title[:100])

        # 4) Description.
        if self.description.strip():
            youtube_logger.info(_msg("✍️", "Entering description"))
            await _fill_editable(page, "#description-textarea #textbox", self.description)

        # 5) Thumbnail. YouTube may reject it until initial processing has advanced;
        # failure is non-fatal.
        if self.thumbnail_path and Path(self.thumbnail_path).exists():
            try:
                thumb_input = page.locator(
                    "#file-loader input[type='file'], ytcp-thumbnail-uploader input[type='file']"
                ).first
                await thumb_input.wait_for(state="attached", timeout=20000)
                await thumb_input.set_input_files(self.thumbnail_path)
                await page.wait_for_timeout(2000)
                youtube_logger.info(_msg("🖼️", "Thumbnail uploaded"))
            except Exception as exc:
                youtube_logger.warning(_msg("⚠️", f"Thumbnail skipped; publishing can continue: {exc}"))

        # 6) Add the video to a playlist. Always close the playlist dialog because
        # it blocks later controls.
        if self.playlist:
            try:
                await _click_if_present(
                    page, "#basics ytcp-text-dropdown-trigger, ytcp-video-metadata-playlists ytcp-dropdown-trigger", 8000)
                await page.wait_for_timeout(1200)
                existing = page.locator(
                    f"tp-yt-paper-checkbox:has-text('{self.playlist}'), "
                    f"ytcp-checkbox-group:has-text('{self.playlist}')").first
                if await existing.count():
                    await existing.click()
                else:
                    if await _click_if_present(page, "ytcp-button:has-text('New playlist')", 4000):
                        await page.wait_for_timeout(800)
                        await _click_if_present(page, "tp-yt-paper-item:has-text('New playlist')", 3000)
                        title_box = page.locator("ytcp-playlist-metadata-editor #textbox, #create-playlist-form #textbox").first
                        if await title_box.count():
                            await title_box.click()
                            await title_box.type(self.playlist, delay=6)
                            await _click_if_present(page, "ytcp-button#create-button, tp-yt-paper-dialog ytcp-button:has-text('Create')", 4000)
            except Exception as exc:
                youtube_logger.warning(_msg("⚠️", f"Playlist step skipped; publishing can continue: {exc}"))
            finally:
                await _click_if_present(page, "ytcp-playlist-dialog #save-button, ytcp-button:has-text('Done')", 3000)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)

        # 7) Audience: not made for children (required).
        if not await _click_if_present(page, "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']", 10000):
            await _click_if_present(page, "tp-yt-paper-radio-button:has-text('not made for kids')", 6000)

        # 8) Tags, under Show more.
        if self.tags:
            try:
                await _click_if_present(page, "#toggle-button", 6000)
                await page.wait_for_timeout(800)
                tag_input = page.locator("#tags-container #text-input, ytcp-form-input-container#tags-container input").first
                await tag_input.click()
                await tag_input.type(",".join(self.tags)[:500] + ",", delay=4)
            except Exception as exc:
                youtube_logger.warning(_msg("⚠️", f"Tags skipped; publishing can continue: {exc}"))

        # 9) Advance to Visibility.
        for _ in range(5):
            vis = page.locator("tp-yt-paper-radio-button[name='PUBLIC']")
            if await vis.count() and await vis.first.is_visible():
                break
            if not await _click_if_present(page, "#next-button", 6000):
                await page.wait_for_timeout(1200)
            await page.wait_for_timeout(1000)

        # 10) Visibility.
        youtube_logger.info(_msg("🌐", f"Setting visibility = {self.visibility}"))
        if not await _select_visibility(page, self.visibility):
            raise RuntimeError(f"YouTube did not accept visibility = {self.visibility}")

        # 10.5) Keep the browser open until the actual file transfer is complete.
        # Closing the browser at 47% or 76% terminates the upload and leaves a draft.
        youtube_logger.info(_msg("📤", "Waiting for the file upload to reach 100% before publishing"))
        if not await _wait_upload_complete(page):
            raise RuntimeError("The YouTube file upload did not complete; the video was not published")

        # 11) Publish and require YouTube's success confirmation.
        video_url = await _publish_video(page)
        await _click_if_present(
            page,
            "ytcp-video-share-dialog ytcp-button:has-text('Close'), #close-button",
            8000,
        )
        youtube_logger.success(
            _msg("🥳", f"Publication confirmed ({self.visibility}){(' ' + video_url) if video_url else ''}")
        )

        # Refresh the saved browser session.
        try:
            await context.storage_state(path=self.account_file)
        except Exception:
            pass
        await browser.close()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
