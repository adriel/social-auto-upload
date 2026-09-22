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

Session bootstrap order (when handle=True and no valid saved session exists):
    1. Try reusing the local machine's already-logged-in Chrome session (via browser_cookie3),
       if that library is installed and Chrome has a live Google session. This only ever
       succeeds when running on a machine with a real Chrome profile (e.g. your desktop Mac),
       not on the headless server.
    2. If that's unavailable or doesn't validate, fall back to the original interactive
       Playwright login window (youtube_cookie_gen), unchanged from before.
"""
import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import urljoin

from patchright.async_api import Page, Playwright, async_playwright

try:
    # Patchright only supports Chromium. Use the regular Playwright package for
    # the opt-in WebKit experiment instead of routing WebKit through Patchright.
    from playwright.async_api import async_playwright as _standard_async_playwright
except ImportError:
    _standard_async_playwright = None

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

try:
    from conf import YT_PUBLISH_TIMEOUT_S
except Exception:
    YT_PUBLISH_TIMEOUT_S = int(os.environ.get("YT_PUBLISH_TIMEOUT_S", "600"))

STUDIO_URL = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"
VISIBILITY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
DEFAULT_BROWSER_ENGINE = "webkit"


def _get_browser_engine() -> str:
    """Return the upload browser engine selected by the environment."""
    engine = os.environ.get("YT_BROWSER", DEFAULT_BROWSER_ENGINE).strip().lower()
    if engine not in {"chrome", "webkit"}:
        raise ValueError("YT_BROWSER must be 'chrome' or 'webkit'")
    return engine


def _get_browser_type(playwright: Playwright, engine=None):
    """Return the Patchright browser type for an upload engine."""
    engine = _get_browser_engine() if engine is None else engine
    if engine == "webkit":
        return playwright.webkit
    if engine == "chrome":
        return playwright.chromium
    raise ValueError("YT_BROWSER must be 'chrome' or 'webkit'")


def _get_async_playwright_factory(engine=None):
    """Choose the Playwright implementation that supports the selected engine."""
    engine = _get_browser_engine() if engine is None else engine
    if engine == "webkit":
        if _standard_async_playwright is None:
            raise RuntimeError(
                "YT_BROWSER=webkit requires the standard Playwright package; "
                "install it with: python -m pip install playwright"
            )
        return _standard_async_playwright
    if engine == "chrome":
        return async_playwright
    raise ValueError("YT_BROWSER must be 'chrome' or 'webkit'")


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


_KEY_AUTH_COOKIE_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO",
}


def _debug(msg: str):
    """Diagnostic line for the local-Chrome-cookie path. Always written to stderr,
    unconditionally -- NOT gated behind youtube_logger's configured level -- so it
    shows up even when this runs unattended via something like mac_uploader.py's
    subprocess.run(capture_output=True), which only prints stdout/stderr and only
    on failure."""
    print(f"[local-chrome-cookies] {msg}", file=sys.stderr, flush=True)


def _load_local_browser_cookiejar():
    """Pull Google/YouTube cookies out of the local machine's own Chrome or Safari
    profile -- selected by YT_LOCAL_BROWSER ("safari", the default, or "chrome").

    Only works where a real signed-in browser profile exists (e.g. your desktop Mac).
    Chrome's cookie DB is AES-encrypted and decrypted via the OS keychain (macOS will
    prompt for Keychain access the first time this runs); Safari's cookie file
    (~/Library/Containers/com.apple.Safari/.../Cookies.binarycookies) isn't encrypted
    at all, but reading it -- like Chrome's DB -- needs Full Disk Access granted to
    whatever process is running this (Terminal, or the launchd/cron agent's own
    executable) in System Settings > Privacy & Security. Raises on any failure;
    callers decide how to handle that.
    """
    import browser_cookie3  # Optional dependency; imported lazily so the rest of this
                             # module works fine without it installed.

    browser = os.environ.get("YT_LOCAL_BROWSER", "safari").strip().lower()
    if browser not in {"chrome", "safari"}:
        raise ValueError("YT_LOCAL_BROWSER must be 'chrome' or 'safari'")
    fetch = browser_cookie3.chrome if browser == "chrome" else browser_cookie3.safari

    _debug(f"browser_cookie3 module: {getattr(browser_cookie3, '__file__', 'unknown')} (source: {browser})")

    jar = fetch(domain_name="google.com")
    google_count = sum(1 for _ in jar)
    _debug(f"domain_name='google.com' -> {google_count} cookie(s)")

    yt_count = 0
    for cookie in fetch(domain_name="youtube.com"):
        jar.set_cookie(cookie)
        yt_count += 1
    _debug(f"domain_name='youtube.com' -> {yt_count} cookie(s)")

    found_key_names = sorted({c.name for c in jar if c.name in _KEY_AUTH_COOKIE_NAMES})
    _debug(f"key auth cookie names present: {found_key_names or 'NONE'}")
    if not found_key_names:
        _debug(f"no recognizable Google auth cookies -- {browser.capitalize()} likely isn't signed "
               "into a Google account in this profile (or it's a different profile "
               "than the one you're signed in on, e.g. a work profile vs personal)")

    return jar


def _cookiejar_to_storage_state(jar) -> dict:
    """Convert an http.cookiejar.CookieJar into Playwright's storage_state shape.

    SameSite isn't preserved by http.cookiejar, so it's reconstructed with a heuristic
    (secure cookies -> "None", everything else -> "Lax"). Good enough to replay the
    session; it doesn't need to match the original attribute exactly.
    """
    cookies = []
    for c in jar:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "expires": c.expires if c.expires else -1,
            "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly", False)),
            "secure": bool(c.secure),
            "sameSite": "None" if c.secure else "Lax",
        })
    return {"cookies": cookies, "origins": []}


async def _try_local_chrome_session(account_file) -> bool:
    """Attempt to bootstrap account_file from the local machine's logged-in Chrome (or,
    with YT_LOCAL_BROWSER=safari, Safari) session.

    Returns False (leaving account_file untouched) on any failure — missing dependency,
    no signed-in browser profile, no cookies found, or the resulting session not
    validating against YouTube Studio — so the caller can fall back to the interactive
    login unchanged. Set YT_SKIP_LOCAL_CHROME=1 to disable this path entirely (e.g. on
    the headless server).
    """
    if os.environ.get("YT_SKIP_LOCAL_CHROME"):
        _debug("skipped: YT_SKIP_LOCAL_CHROME is set")
        return False

    browser = os.environ.get("YT_LOCAL_BROWSER", "safari").strip().lower()
    _debug(f"attempting to reuse the local {browser} session ...")

    try:
        import browser_cookie3  # noqa: F401
    except ImportError as exc:
        _debug(f"skipped: browser_cookie3 is not installed ({exc})")
        youtube_logger.info(_msg("ℹ️", "browser_cookie3 not installed; skipping local browser cookie pull"))
        return False

    try:
        jar = _load_local_browser_cookiejar()
        storage_state = _cookiejar_to_storage_state(jar)
    except Exception as exc:
        _debug(f"failed reading local {browser} cookies: {exc.__class__.__name__}: {exc}")
        _debug(traceback.format_exc())
        youtube_logger.info(_msg("ℹ️", f"Could not read local {browser} cookies, skipping: {exc}"))
        return False

    _debug(f"built storage_state with {len(storage_state['cookies'])} cookie(s) total")
    if not storage_state["cookies"]:
        _debug("skipped: no Google/YouTube cookies found in local Chrome")
        youtube_logger.info(_msg("ℹ️", "No Google/YouTube cookies found in local Chrome"))
        return False

    candidate_file = f"{account_file}.local-chrome-tmp"
    try:
        with open(candidate_file, "w") as f:
            json.dump(storage_state, f)
        _debug(f"wrote candidate session to {candidate_file}, validating against YouTube Studio ...")
        valid = await cookie_auth(candidate_file)
        _debug(f"cookie_auth() -> {valid}")
        if valid:
            os.replace(candidate_file, account_file)
            youtube_logger.success(_msg("✅", f"Reused the local {browser} session for YouTube Studio"))
            return True
        _debug("cookie_auth() rejected the candidate session -- either the Chrome cookies "
               "aren't actually signed in to Studio's channel, or cookie_auth's own headless "
               "Chrome launch is itself getting blocked/redirected (e.g. the same UA/bot checks "
               "documented elsewhere in this file for headless sessions)")
        youtube_logger.info(_msg("ℹ️", f"Local {browser} cookies didn't produce a valid YouTube Studio session"))
        return False
    except Exception as exc:
        _debug(f"failed validating candidate session: {exc.__class__.__name__}: {exc}")
        _debug(traceback.format_exc())
        youtube_logger.info(_msg("ℹ️", f"Local Chrome session check failed, skipping: {exc}"))
        return False
    finally:
        try:
            if os.path.exists(candidate_file):
                os.remove(candidate_file)
        except Exception:
            pass


async def youtube_setup(account_file, handle: bool = False, return_detail: bool = False, headless: bool = False):
    """Validate the saved session and optionally open an interactive login.

    The local-Chrome-cookie attempt runs regardless of `handle` -- it's non-interactive
    and cheap, so it's worth trying even on the `upload-video` path (handle=False), which
    is the one that matters for an unattended run like a launchd/cron job: it lets that
    path silently recover using an already-logged-in local Chrome session instead of just
    failing with "run `sau youtube login` first". `handle` still gates ONLY the interactive
    Playwright login window, which obviously can't run unattended.
    """
    if not Path(account_file).exists() or not await cookie_auth(account_file):
        if await _try_local_chrome_session(account_file):
            result = _build_login_result(True, "cookie_valid", "Reused the local Chrome session", account_file)
            return result if return_detail else True
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


ACCOUNT_SWITCHER_ENDPOINT = f"{STUDIO_URL}/getAccountSwitcherEndpoint"


def _strip_xssi_prefix(text: str) -> str:
    """This endpoint (unlike the youtubei/v1/* APIs used elsewhere in this file)
    prefixes its JSON body with ")]}'" to prevent JSON-hijacking via a <script
    src=...> include. Strip it before parsing."""
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else text[4:]
    return text.lstrip()


async def _list_account_switcher_channels(page: Page) -> list:
    """List every identity (personal Google account + each brand-account channel)
    the current login can switch into -- display name, handle (if it has one),
    and the signin URL that actually performs the switch. This mirrors exactly
    what clicking Studio's avatar -> account-switcher menu does; the endpoint and
    response shape were captured from a HAR of that flow, not guessed."""
    resp = await page.request.get(ACCOUNT_SWITCHER_ENDPOINT)
    if resp.status != 200:
        raise RuntimeError(f"getAccountSwitcherEndpoint returned HTTP {resp.status}")
    data = json.loads(_strip_xssi_prefix(await resp.text()))
    try:
        contents = (data["data"]["actions"][0]["getMultiPageMenuAction"]["menu"]
                    ["multiPageMenuRenderer"]["sections"][0]["accountSectionListRenderer"]
                    ["contents"][0]["accountItemSectionRenderer"]["contents"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"getAccountSwitcherEndpoint's response shape wasn't what was expected ({exc}) "
            f"-- YouTube may have changed it since this was captured."
        )

    channels = []
    for entry in contents:
        item = entry.get("accountItem", {})
        tokens = (item.get("serviceEndpoint", {})
                      .get("selectActiveIdentityEndpoint", {})
                      .get("supportedTokens", []))
        signin_url = next(
            (t["accountSigninToken"]["signinUrl"] for t in tokens if "accountSigninToken" in t),
            None,
        )
        channels.append({
            "name": (item.get("accountName") or {}).get("simpleText", ""),
            "handle": (item.get("channelHandle") or {}).get("simpleText", ""),
            "is_selected": bool(item.get("isSelected")),
            "signin_url": signin_url,
        })
    return channels


async def _public_channel_id(page: Page, handle: str):
    """Best-effort lookup of a handle's canonical channel ID from its public
    youtube.com page. Used only as an independent cross-check in
    _switch_to_channel: the post-switch '/channel/<id>' redirect confirms
    *some* channel is active, but not that it's the one we asked for -- a
    switch that silently no-ops still redirects to a /channel/<id> URL (the
    previously-active one), which read as a false 'confirmed'. Returns None
    (skip the cross-check) on any failure rather than raising, since this is
    a bonus safeguard, not the primary mechanism."""
    try:
        resp = await page.request.get(f"https://www.youtube.com/@{handle.lstrip('@')}")
        if resp.status != 200:
            return None
        html = await resp.text()
    except Exception:
        return None
    m = re.search(r'"externalId":"(UC[0-9A-Za-z_-]{22})"', html)
    return m.group(1) if m else None


async def _switch_to_channel(page: Page, channel: str) -> str:
    """Switch the active identity to the given channel -- matched by handle (with
    or without '@') or exact display name against the account switcher's list --
    then confirm the switch via the same '/channel/<id>' redirect cookie_auth()
    already relies on elsewhere in this file, cross-checked (when the channel was
    matched by handle) against that handle's public canonical channel ID. Returns
    the new active channel's raw ID. Raises rather than silently continuing if the
    requested channel can't be found or the switch can't be confirmed -- this
    existing to prevent uploading to the wrong channel is the whole point."""
    target = channel.strip().lstrip("@").lower()
    channels = await _list_account_switcher_channels(page)

    match = next(
        (c for c in channels if c["handle"].lstrip("@").lower() == target
         or c["name"].strip().lower() == target),
        None,
    )
    if match is None:
        available = ", ".join(f"{c['name']!r} ({c['handle'] or 'no handle'})" for c in channels)
        raise RuntimeError(f"No channel matching '{channel}' in the account switcher. Available: {available}")
    if not match["signin_url"] and not match["is_selected"]:
        raise RuntimeError(f"Found channel '{channel}' in the account switcher but it had no signin URL to switch with")

    expected_id = await _public_channel_id(page, match["handle"]) if match["handle"] else None

    if match["is_selected"]:
        youtube_logger.info(_msg("📺", f"'{channel}' is already the active channel"))
    else:
        youtube_logger.info(_msg("📺", f"Switching active channel to '{match['name']}' ({match['handle'] or channel})"))
        # This signin_url comes from Studio's own account-switcher endpoint (its
        # HAR capture), but it's YouTube's masthead account-switcher link, which
        # is relative to www.youtube.com, not studio.youtube.com -- resolving it
        # against the wrong domain sends the browser to a no-op page, the active
        # identity never changes, and (without expected_id above) the redirect
        # check below would happily "confirm" the still-wrong channel.
        signin_url = urljoin("https://www.youtube.com/", match["signin_url"])
        await page.goto(signin_url, wait_until="domcontentloaded")

    # Confirm by the same mechanism cookie_auth() already trusts: loading Studio
    # plain redirects to '/channel/<id-of-whichever-channel-is-now-active>'.
    await page.goto(STUDIO_URL, wait_until="domcontentloaded")
    for _ in range(40):
        if "/channel/" in page.url:
            break
        await page.wait_for_timeout(250)
    id_match = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", page.url)
    if not id_match:
        raise RuntimeError(f"Switched to '{channel}' but Studio never redirected to a /channel/<id> URL "
                            f"(ended up at {page.url})")
    channel_id = id_match.group(1)
    if expected_id and channel_id != expected_id:
        raise RuntimeError(
            f"Switched to '{channel}' but Studio's active channel is {channel_id}, not the expected "
            f"{expected_id} -- the switch did not actually take effect. Aborting rather than risk "
            f"uploading to the wrong channel."
        )
    youtube_logger.info(_msg("✅", f"Active channel confirmed: {channel_id}"))
    return channel_id


async def _open_upload_page(page: Page, upload_url: str = UPLOAD_URL):
    """Open YouTube's upload page (optionally scoped to a specific channel via
    upload_url) and return its real file input when ready."""
    await page.goto(upload_url, wait_until="domcontentloaded")
    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        raise RuntimeError("The YouTube session has expired; run the login command again")
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=60000)
    return file_input


async def _wait_for_details_editor(page: Page):
    """Wait until the real editable title field is ready for input."""
    title_box = page.locator(
        "#title-textarea #textbox[contenteditable='true']"
    ).first
    await title_box.wait_for(state="visible", timeout=60000)


PROGRESS_LOG_BUCKET_PCT = 20  # Log a progress line at most once per 20% (~5 total for 0-100%).


async def _wait_upload_complete(page: Page, max_polls: int = 1800) -> bool:
    """Wait for the browser-to-YouTube file transfer to finish.

    YouTube enables Publish before the transfer reaches 100%, and content checks can
    start while the transfer is still running. Read every progress label and require
    either an explicit upload-complete marker or two consecutive polls where an
    observed "Uploading N%" status has disappeared. max_polls*1s is a 30-minute cap.
    Percent-based progress lines are throttled to roughly PROGRESS_LOG_BUCKET_PCT-sized
    steps -- polling still happens every second, but a multi-GB upload otherwise logs a
    near-identical "Uploading N% ... M minutes left" line every second for its whole
    duration, which is noise rather than signal.
    """
    last = ""
    last_logged_bucket = -1
    saw_uploading = False
    missing_after_upload = 0
    progress = page.locator(
        "ytcp-video-upload-progress, .progress-label, span.progress-label"
    )
    for _ in range(max_polls):
        try:
            texts = await progress.all_inner_texts()
            unique_texts = dict.fromkeys(text.strip() for text in texts if text.strip())
            status = " | ".join(unique_texts)
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
            bucket = percent // PROGRESS_LOG_BUCKET_PCT
            if bucket > last_logged_bucket:
                last_logged_bucket = bucket
                youtube_logger.info(_msg("⏳", f"Upload status: {status[:100]}"))
            last = status
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
        elif status and status != last:
            # Non-percent status text (rare) -- still worth logging on change; this
            # isn't part of the once-per-second percent spam being throttled above.
            youtube_logger.info(_msg("⏳", f"Upload status: {status[:100]}"))
            last = status

        await page.wait_for_timeout(1000)
    youtube_logger.error(_msg("😵", "The file upload did not finish within 30 minutes"))
    return False


PUBLISH_METADATA_UPDATE_PATH = "video_manager/metadata_update"


async def _publish_video(page: Page, timeout_s: int = YT_PUBLISH_TIMEOUT_S,
                          poll_interval_ms: int = 500) -> str:
    """Click Publish and return as soon as YouTube's own API confirms the publish
    itself succeeded -- NOT once the video has finished background processing.

    Per a captured HAR of a real successful publish: clicking Done/Publish triggers
    exactly one POST to `.../youtubei/v1/video_manager/metadata_update`, whose JSON
    body has `privacy.success: true` the moment the visibility change (draft ->
    public/unlisted/private) actually takes effect server-side -- that IS the
    publish. A `share/get_share_panel` call follows ~2s later, but that only
    populates the confirmation dialog's UI; it doesn't reflect anything about
    whether the video went live. Studio then keeps its own processing checks
    (copyright ID, monetization, etc.) running for minutes afterwards regardless of
    whether this browser is even still open -- there's nothing further worth
    waiting for. A run was previously seen stuck 5+ minutes on the share dialog's
    DOM (which doesn't reliably render/select the same way across every Studio UI
    variant this project has hit) while the video had already been public the
    whole time.

    Falls back to polling the DOM (share link / Close button) if the network
    response is somehow never seen, so this still works if Studio's response shape
    changes.
    """
    done_button = page.locator("#done-button").first
    await done_button.wait_for(state="visible", timeout=15000)
    for _ in range(240):
        if await done_button.is_enabled():
            break
        await page.wait_for_timeout(250)
    else:
        raise RuntimeError("The YouTube Publish button remained disabled; the video was not published")

    publish_result = {}  # "success" key present once the metadata_update response has been seen

    async def _on_response(response):
        if PUBLISH_METADATA_UPDATE_PATH not in response.url or response.status != 200:
            return
        try:
            body = await response.json()
        except Exception:
            return
        success = body.get("privacy", {}).get("success")
        if success is None:
            return  # not the shape expected; let the DOM fallback handle it
        publish_result["success"] = bool(success)

    def _on_response_sync(response):
        asyncio.create_task(_on_response(response))

    page.on("response", _on_response_sync)
    try:
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
        # Some Studio UI variants render the share dialog's Close button
        # without ever showing a matching <a href> (icon-only share link,
        # layout differences) -- a visible Close button is just as strong a
        # "publish actually finished" signal as the link itself, so it
        # counts as success too, just without a URL to report.
        close_button = page.locator(
            "ytcp-video-share-dialog ytcp-button:has-text('Close'), #close-button"
        ).first

        max_polls = max(1, int(timeout_s * 1000 / poll_interval_ms))
        next_heartbeat_s = 15
        logged_checks_running = False

        for i in range(max_polls):
            if "success" in publish_result:
                if not publish_result["success"]:
                    raise RuntimeError(
                        "YouTube's metadata_update response reported the publish request "
                        "itself failed -- the video was NOT published."
                    )
                video_url = ""
                try:
                    if await link.is_visible(timeout=2000):
                        video_url = await link.get_attribute("href") or ""
                except Exception:
                    pass
                youtube_logger.info(_msg("✅", "YouTube confirmed the publish "
                                               "(it'll finish processing in the background)"))
                return video_url

            # DOM fallback, kept in case the network response is ever missed.
            try:
                if await link.is_visible():
                    return await link.get_attribute("href") or ""
            except Exception:
                pass
            try:
                if await close_button.is_visible():
                    return ""
            except Exception:
                pass
            try:
                if await publish_anyway.is_visible():
                    if not logged_checks_running:
                        youtube_logger.info(_msg("ℹ️", "YouTube checks are still running; selecting Publish anyway"))
                        logged_checks_running = True
                    await publish_anyway.click(timeout=5000)
            except Exception:
                pass

            elapsed_s = i * poll_interval_ms / 1000
            if elapsed_s >= next_heartbeat_s:
                youtube_logger.info(_msg("⏳", f"Still waiting for YouTube's publish confirmation "
                                               f"({int(elapsed_s)}s elapsed, timeout {timeout_s}s)"))
                next_heartbeat_s += 15

            await page.wait_for_timeout(poll_interval_ms)

        raise RuntimeError(
            f"YouTube did not confirm publication within {timeout_s}s. The publish request was already "
            f"sent to YouTube by this point -- clicking Publish/Publish anyway completes server-side "
            f"regardless of whether this browser session is still watching -- so the video may well have "
            f"gone public anyway. Check YouTube Studio's Content list before assuming this actually failed."
        )
    finally:
        page.remove_listener("response", _on_response_sync)

class YouTubeVideo(BaseVideoUploader):
    def __init__(self, title, file_path, tags, account_file, *,
                 description="", thumbnail_path=None, playlist=None,
                 visibility="public", debug=DEBUG_MODE, headless=False, channel=None):
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
        # Which channel to post to, when the logged-in Google account manages more
        # than one (e.g. its default/last-active channel isn't the one you want).
        # Accepts a handle ('@USA_weather_sat') or a raw channel ID ('UC...').
        # None (the default) preserves the old behaviour: upload to whichever
        # channel is currently active for this session.
        self.channel = channel

    async def upload(self, playwright: Playwright) -> None:
        browser_engine = _get_browser_engine()
        browser_type = _get_browser_type(playwright, browser_engine)
        launch_options = {
            "headless": self.headless,
            "proxy": {"server": YT_PROXY} if YT_PROXY else None,
        }
        if browser_engine == "chrome":
            launch_options["channel"] = "chrome"
        browser = await browser_type.launch(**launch_options)
        context = await browser.new_context(
            storage_state=self.account_file,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ) if self.headless and browser_engine == "chrome" else None,
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
        youtube_logger.info(_msg("🌐", f"Browser engine: {browser_engine}"))

        if self.channel:
            # _switch_to_channel() switches the session's active identity and confirms
            # it via Studio's own '/channel/<id>' redirect. That's session-wide, not
            # scoped to studio.youtube.com -- the plain upload URL below already
            # targets "whichever channel is currently active" (that's how this worked
            # for the single-channel/default case from the start), so there's nothing
            # further to do here. A channel-scoped Studio URL
            # (studio.youtube.com/channel/<id>/videos/upload) was tried and doesn't
            # work: it just loads the normal dashboard, not the upload dialog --
            # reaching that dialog needs an actual "Create" -> "Upload videos" click,
            # which the plain /upload URL below bypasses entirely.
            await _switch_to_channel(page, self.channel)

        youtube_logger.info(_msg("🌐", "Opening the YouTube upload page"))
        file_input = await _open_upload_page(page, UPLOAD_URL)
        youtube_logger.info(_msg("✅", "Upload page ready; selecting the video file"))

        # 1) Select the video file.
        await file_input.set_input_files(self.file_path)

        # 2) Wait for the details editor.
        youtube_logger.info(_msg("⏳", "Waiting for the video details editor"))
        await _wait_for_details_editor(page)

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
        engine = _get_browser_engine()
        async with _get_async_playwright_factory(engine)() as playwright:
            await self.upload(playwright)
