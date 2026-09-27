# mac_uploader.py
import argparse
import fcntl
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests

# MANIFEST_URL = "https://user:pass@sub.domain.com/GOES/manifest.json"
MANIFEST_URL = "https://usernmaejkhgdfys:4hgj354g3j5h@ljhgfdhtryjfygjh.lionfabric.page/GOES/manifest.json"

STATE_FILE = Path.home() / ".sau_uploader_state.json"
# Kept separate from STATE_FILE on purpose: STATE_FILE is a plain JSON list of
# filenames, and any older copy of this script reading it as a dict-shaped file
# would see zero "done" entries and re-upload every video. Thumbnail outcomes
# ("set" / "failed" / "skipped" / "unknown") are informational only.
THUMB_STATE_FILE = Path.home() / ".sau_uploader_thumbnails.json"
LOCK_FILE = Path.home() / ".sau_uploader.lock"
DOWNLOAD_DIR = Path.home() / "sat_downloads"
SAU_BIN = Path("/Users/plex/sau-uploader/.venv/bin/sau")
SAU_ACCOUNT = "me"
SAU_YOUTUBE_CHANNEL = "@USA_weather_sat"  # NotBROLL is the Google account's default/last-active
                                          # channel in the local browser session (Safari by
                                          # default -- see YT_LOCAL_BROWSER in the youtube
                                          # uploader); this pins uploads to the right one.

DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1543864739627671643/"
    "qm7h3rxOILOBysHzQziVpV2ovvqtCFD6HTp562EQFhtU_GvNPQId2i-mBh5IOcbzwKrW"
)

# Module-level handle so the lock (see acquire_lock_or_exit) lives as
# long as the process -- a local variable could get garbage-collected
# and silently drop the lock while the run is still going.
_lock_handle = None

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

def _redact_url(url: str) -> str:
    """Mask basic-auth credentials embedded in a URL before it goes into
    any printed/logged/Discord-posted output -- MANIFEST_URL carries
    user:pass@, and that shouldn't end up sitting in plain text anywhere
    other than this file."""
    return re.sub(r"//[^@/]+@", "//***:***@", url)


def notify_discord(message: str):
    """Best-effort Discord alert. Failures here are printed, never
    raised -- a broken/rate-limited webhook shouldn't itself crash the
    uploader or mask whatever error triggered this call."""
    # Discord caps message content at ~2000 chars; truncate rather than
    # have a long traceback get the whole request rejected.
    content = message if len(message) <= 1900 else message[:1900] + "\n...(truncated)"
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        if resp.status_code >= 300:
            print(f"Discord webhook returned HTTP {resp.status_code}: {resp.text[:300]}")
    except requests.exceptions.RequestException as e:
        print(f"Couldn't send Discord alert ({e.__class__.__name__}: {e})")


def fatal(message: str):
    """Print an error, alert Discord, and exit(1) -- for conditions an
    unattended (launchd/cron) run can't recover from on its own, so the
    failure is visible somewhere other than a log file nobody's
    tailing."""
    print(message, file=sys.stderr, flush=True)
    notify_discord(f"GOES uploader error:\n```\n{message[:1800]}\n```")
    sys.exit(1)


def acquire_lock_or_exit(log=None):
    """Exclusive, non-blocking lock so two overlapping runs can't both
    load the same 'done' state, both conclude a video hasn't been
    uploaded yet, and both download + upload it a second time.

    This is almost certainly what happened the time this re-uploaded:
    `done` is only saved to disk AFTER sau finishes uploading (which
    isn't instant for a multi-GB 4K video), so a run that starts while
    an earlier one is still mid-upload sees stale state and redoes the
    whole thing. Same flock-based approach as the server pipeline's own
    lock.py -- the kernel releases it automatically no matter how this
    process exits, so there's no stale-lock cleanup to worry about.

    Not treated as an error (no Discord alert): overlap is an expected,
    handled condition, not a failure."""
    global _lock_handle
    handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        print("Another instance of mac_uploader.py is already running -- exiting.", flush=True)
        sys.exit(0)
    if log:
        log("  acquired lock")
    _lock_handle = handle


def fetch_manifest(url: str, log=None) -> list:
    """GET the manifest and parse it as JSON, or fatal() with a clear
    diagnostic instead of a raw traceback."""
    safe_url = _redact_url(url)
    if log:
        log(f"  fetching {safe_url}")

    try:
        resp = requests.get(url, timeout=30)
    except requests.exceptions.RequestException as e:
        fatal(
            f"Couldn't reach the manifest at {safe_url}\n"
            f"  ({e.__class__.__name__}: {e})\n"
            f"  -- check the server is up, the hostname/path is correct, and "
            f"this Mac has network access to it."
        )

    if resp.status_code != 200:
        snippet = resp.text[:300].replace("\n", " ")
        fatal(
            f"Manifest fetch returned HTTP {resp.status_code} for {safe_url}\n"
            f"  body starts: {snippet!r}\n"
            f"  -- a 401/403 (or a body that looks like a login page) usually "
            f"means the username/password embedded in MANIFEST_URL are wrong; "
            f"a 404 usually means the path is wrong."
        )

    if not resp.text.strip():
        fatal(
            f"Manifest at {safe_url} returned HTTP 200 but an EMPTY body.\n"
            f"  -- the server may not have published anything yet."
        )

    try:
        manifest = resp.json()
    except requests.exceptions.JSONDecodeError:
        snippet = resp.text[:300].replace("\n", " ")
        content_type = resp.headers.get("Content-Type", "(none)")
        fatal(
            f"Manifest at {safe_url} returned HTTP 200 but the body isn't valid JSON.\n"
            f"  Content-Type: {content_type}\n"
            f"  body starts: {snippet!r}\n"
            f"  -- this usually means a reverse proxy or auth layer is "
            f"intercepting the request and returning an HTML error/login page."
        )

    if log:
        log(f"  got {len(manifest)} manifest entr{'y' if len(manifest) == 1 else 'ies'}")
    return manifest


def _video_url_for(entry: dict) -> str:
    """Manifest entries carry just a path relative to the manifest's own
    directory (e.g. "/us_satellite_daily_2026-08-31.mp4"), not a full
    URL -- so the real download URL is MANIFEST_URL with 'manifest.json'
    swapped out for that path, keeping the same embedded basic-auth
    credentials (the video lives behind the same auth)."""
    if not MANIFEST_URL.endswith("manifest.json"):
        fatal(f"MANIFEST_URL is expected to end in 'manifest.json', got: {_redact_url(MANIFEST_URL)}")
    base = MANIFEST_URL[: -len("manifest.json")]
    return base + entry["url"].lstrip("/")


def _remote_content_length(url: str, log=None):
    """HEAD the video URL and return its Content-Length, or None if that can't be
    determined (server doesn't report one, HEAD isn't supported, network error, etc).
    None means "can't verify" -- callers should treat that as "assume incomplete"
    rather than risk uploading a truncated file."""
    try:
        resp = requests.head(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        if log:
            log(f"    HEAD request failed ({e.__class__.__name__}: {e}); can't verify existing file")
        return None
    length = resp.headers.get("Content-Length")
    if length is None or not length.isdigit():
        if log:
            log("    HEAD response has no usable Content-Length; can't verify existing file")
        return None
    return int(length)


def _run_sau_streaming(cmd, log=None):
    """Run the sau subprocess, streaming its output live line-by-line instead of
    buffering everything until it exits. subprocess.run(capture_output=True) (the
    old approach) produces nothing at all until the WHOLE upload -- multi-GB,
    tens of minutes -- has finished, even with --verbose, which is why nothing
    showed up while network traffic was visibly flowing. stdout/stderr are merged
    (stderr=STDOUT) so interleaved lines print in the order sau actually emitted
    them. Returns (returncode, combined_output) -- the combined text is still kept
    so a failure can be reported/alerted in full, same as before.
    """
    lines = []
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for raw_line in process.stdout:
        line = _strip_ansi(raw_line.rstrip("\n"))
        lines.append(line)
        if log:
            log(f"    {line}")
    process.wait()
    return process.returncode, "\n".join(lines)


def load_state():
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()


def save_state(done):
    # Atomic write (temp file + rename) so a crash or power loss
    # mid-write can't leave a truncated/corrupt state file behind --
    # rename is atomic on POSIX, so this file is always either the old
    # complete version or the new complete version, never half-written.
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(STATE_FILE)


def load_thumb_state() -> dict:
    try:
        data = json.loads(THUMB_STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_thumb_state(thumbs: dict):
    tmp = THUMB_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(sorted(thumbs.items())), indent=1))
    tmp.replace(THUMB_STATE_FILE)


THUMB_MAX_BYTES = 2_000_000  # YouTube rejects custom thumbnails over 2 MB
THUMB_SIZE = (1280, 720)     # (width, height)
_THUMB_MARKER_RE = re.compile(r"SAU_THUMBNAIL=(\w+)")
_SAU_WARNING_RE = re.compile(r"\| WARNING:\s*(.+)")


def _is_short_entry(entry: dict) -> bool:
    """Shorts don't get custom thumbnails. The server names them
    us_satellite_shorts_* and ends their title with #Shorts."""
    return ("#shorts" in entry.get("title", "").lower()
            or "_shorts_" in entry.get("filename", "").lower())


def _with_manifest_auth(url: str) -> str:
    """The thumbnail sits behind the same basic auth as the manifest and the
    videos (the video URL inherits it from MANIFEST_URL, but thumbnail_url is
    absolute). Add the manifest's credentials only when the host is the same
    one, so they never get sent to some other host."""
    target, manifest = urlsplit(url), urlsplit(MANIFEST_URL)
    if target.username or not manifest.username or target.hostname != manifest.hostname:
        return url
    creds = f"{quote(manifest.username, safe='')}:{quote(manifest.password or '', safe='')}"
    return urlunsplit(target._replace(netloc=f"{creds}@{target.netloc}"))


def _validate_thumbnail(path: Path):
    """Return None if the file is an acceptable YouTube thumbnail, otherwise a
    short human-readable reason it isn't."""
    size = path.stat().st_size
    if size > THUMB_MAX_BYTES:
        return f"{size:,} bytes is over the {THUMB_MAX_BYTES:,}-byte limit"
    with open(path, "rb") as f:
        if f.read(3) != b"\xff\xd8\xff":
            return "not a JPEG (bad file signature)"
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "can't validate (cv2/numpy not installed)"
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return "not a decodable JPEG"
    height, width = image.shape[:2]
    if (width, height) != THUMB_SIZE:
        return f"{width}x{height} is not {THUMB_SIZE[0]}x{THUMB_SIZE[1]}"
    return None


def _prepare_thumbnail(entry: dict, warnings: list, log=None):
    """Download + validate the entry's thumbnail, if it has one. Returns the
    local Path, or None (no thumbnail, a Short, or anything went wrong).

    Never raises: a thumbnail problem must not fail or block the video upload,
    so every failure becomes a warning (collected and sent to Discord once the
    video itself has uploaded) and the video goes up without one."""
    url, name = entry.get("thumbnail_url"), entry.get("thumbnail_filename")
    if not url and not name:
        return None
    filename = entry["filename"]
    if _is_short_entry(entry):
        if log:
            log(f"  {filename}: Short -- skipping custom thumbnail")
        return None

    dest = None
    try:
        if not url or not url.startswith(("http://", "https://")):
            url = MANIFEST_URL[: -len("manifest.json")] + (name or url).lstrip("/")
        url = _with_manifest_auth(url)
        # Path(...).name keeps a manifest-supplied name from escaping DOWNLOAD_DIR.
        dest = DOWNLOAD_DIR / Path(name or urlsplit(url).path).name
        DOWNLOAD_DIR.mkdir(exist_ok=True)

        if dest.exists() and dest.stat().st_size == _remote_content_length(url, log=log):
            if log:
                log(f"  {filename}: reusing already-downloaded thumbnail {dest.name}")
        else:
            if log:
                log(f"  {filename}: downloading thumbnail from {_redact_url(url)} ...")
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)

        reason = _validate_thumbnail(dest)
        if reason:
            dest.unlink(missing_ok=True)
            warnings.append(f"thumbnail rejected ({dest.name}): {reason} -- uploading without one")
            return None
        if log:
            log(f"  {filename}: thumbnail OK ({dest.name}, {dest.stat().st_size:,} bytes)")
        return dest
    except Exception as e:
        if dest is not None:
            dest.unlink(missing_ok=True)
        # requests' error text embeds the full URL, which carries the basic-auth
        # credentials -- redact before it can reach the log or Discord.
        detail = _redact_url(f"{e.__class__.__name__}: {e}")
        warnings.append(f"thumbnail unavailable ({detail}) -- uploading without one")
        return None


def _sau_warnings(output: str) -> list:
    """WARNING lines sau logged during an otherwise successful upload (skipped
    tags/playlist/thumbnail steps, etc)."""
    seen = []
    for line in output.splitlines():
        m = _SAU_WARNING_RE.search(line)
        if m and m.group(1).strip() not in seen:
            seen.append(m.group(1).strip()[:300])
    return seen[:8]


def main(args):
    log = (lambda msg: print(msg, flush=True)) if args.verbose else None

    acquire_lock_or_exit(log=log)

    done = load_state()
    thumbs = load_thumb_state()
    if log:
        log(f"  {len(done)} filename(s) already marked done")

    manifest = fetch_manifest(MANIFEST_URL, log=log)

    for entry in manifest:
        filename = entry["filename"]
        if filename in done:
            if log:
                log(f"  {filename}: already done, skipping")
            continue

        video_url = _video_url_for(entry)
        local_path = DOWNLOAD_DIR / filename
        DOWNLOAD_DIR.mkdir(exist_ok=True)

        # If a previous run already got the full file down (e.g. the download
        # succeeded but sau then failed, so the file was deliberately left on
        # disk -- see the comment at the bottom of this loop), reuse it instead
        # of spending several minutes re-pulling multiple GB over the network.
        # Only trust it as complete when its size matches the remote
        # Content-Length; anything else (partial file from a Ctrl-C, unknown
        # remote size, size mismatch) is treated as stale and re-downloaded.
        already_downloaded = False
        if local_path.exists():
            local_size = local_path.stat().st_size
            if log:
                log(f"  {filename}: found existing local file ({local_size / 1e6:.0f} MB), checking against remote ...")
            remote_size = _remote_content_length(video_url, log=log)
            if remote_size is not None and local_size == remote_size:
                already_downloaded = True
                if log:
                    log(f"  {filename}: existing file matches remote size, reusing it (skipping download)")
            else:
                if log:
                    reason = ("remote size unknown" if remote_size is None
                               else f"local {local_size / 1e6:.0f} MB != remote {remote_size / 1e6:.0f} MB")
                    log(f"  {filename}: existing file looks stale/partial ({reason}); deleting and re-downloading")
                local_path.unlink()

        if not already_downloaded:
            if log:
                log(f"  {filename}: downloading from {_redact_url(video_url)} ...")
            try:
                with requests.get(video_url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
            except requests.exceptions.RequestException as e:
                msg = (f"couldn't download {filename} from {_redact_url(video_url)}: "
                       f"{e.__class__.__name__}: {e} -- will retry next run")
                print(msg)
                notify_discord(f"GOES uploader: download failed\n```\n{msg}\n```")
                local_path.unlink(missing_ok=True)
                continue
            if log:
                log(f"  {filename}: downloaded {local_path.stat().st_size / 1e6:.0f} MB")

        # Honor the manifest's own visibility, falling back to public for a
        # missing/unrecognized value.
        visibility = entry.get("visibility")
        if visibility not in ("public", "unlisted", "private"):
            if visibility and log:
                log(f"  {filename}: unrecognized visibility {visibility!r}, using public")
            visibility = "public"

        warnings = []  # sent to Discord once, after the video has uploaded
        thumb_path = _prepare_thumbnail(entry, warnings, log=log)

        cmd = [str(SAU_BIN), "youtube", "upload-video",
               "--account", SAU_ACCOUNT, "--file", str(local_path),
               "--channel", SAU_YOUTUBE_CHANNEL,
               "--title", entry["title"][:100], "--desc", entry["description"],
               "--tags", ",".join(entry["tags"]),
               "--playlist", entry["playlist_title"], "--visibility", visibility]
        if thumb_path:
            cmd += ["--thumbnail", str(thumb_path)]
        if log:
            log(f"  {filename}: running sau upload-video ...")

        returncode, output = _run_sau_streaming(cmd, log=log)

        if returncode == 0:
            done.add(filename)
            save_state(done)
            local_path.unlink()
            print(f"uploaded {filename}")
            if log:
                log(f"  {filename}: state saved, local copy removed")

            if thumb_path:
                markers = _THUMB_MARKER_RE.findall(output)
                outcome = markers[-1] if markers else "unknown"
                thumbs[filename] = outcome
                try:
                    save_thumb_state(thumbs)
                except OSError as e:
                    warnings.append(f"couldn't save thumbnail state: {e}")
                if outcome != "set":
                    warnings.append(f"thumbnail was NOT set on the video (uploader reported: {outcome})")
                thumb_path.unlink(missing_ok=True)
            warnings += _sau_warnings(output)
            if warnings:
                lines = "\n".join(f"- {w}" for w in warnings)
                print(f"warnings for {filename}:\n{lines}")
                notify_discord(f"GOES uploader: uploaded {filename} with warnings\n```\n{lines[:1700]}\n```")
        else:
            msg = f"sau upload failed for {filename}:\n{output}"
            print(msg)
            # Head + tail: the failure reason and the "Saved diagnostics: <path>"
            # line sau prints are at the END of a long log.
            snippet = msg if len(msg) <= 1500 else msg[:500] + "\n...\n" + msg[-950:]
            notify_discord(f"GOES uploader: sau upload failed\n```\n{snippet}\n```")
            # Left on disk deliberately -- next run will reuse it (see the
            # already_downloaded check above) rather than re-downloading, since
            # sau wasn't the download's problem.


def parse_args():
    parser = argparse.ArgumentParser(
        description="Poll the GOES pipeline's manifest and upload new videos via sau."
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                         help="print each step as it happens (fetching manifest, downloading, "
                              "running sau, etc) -- handy when running by hand outside cron/launchd")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        main(args)
    except SystemExit:
        raise  # fatal()/sys.exit() already printed + notified; just propagate the exit code
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        notify_discord(f"mac_uploader.py crashed:\n```\n{tb[-1800:]}\n```")
        sys.exit(1)
