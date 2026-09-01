# mac_uploader.py
import argparse
import fcntl
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path

import requests

# MANIFEST_URL = "https://user:pass@sub.domain.com/GOES/manifest.json"
MANIFEST_URL = "https://usernmaejkhgdfys:4hgj354g3j5h@ljhgfdhtryjfygjh.lionfabric.page/GOES/manifest.json"

STATE_FILE = Path.home() / ".sau_uploader_state.json"
LOCK_FILE = Path.home() / ".sau_uploader.lock"
DOWNLOAD_DIR = Path.home() / "sat_downloads"
SAU_BIN = Path("/Users/adriel/Downloads/social-auto-upload/.venv/bin/sau")
SAU_ACCOUNT = "me"
SAU_YOUTUBE_CHANNEL = "@USA_weather_sat"  # NotBROLL is the Google account's default/last-active
                                          # channel in Chrome; this pins uploads to the right one.

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


def main(args):
    log = (lambda msg: print(msg, flush=True)) if args.verbose else None

    acquire_lock_or_exit(log=log)

    done = load_state()
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

        cmd = [str(SAU_BIN), "youtube", "upload-video",
               "--account", SAU_ACCOUNT, "--file", str(local_path),
               "--channel", SAU_YOUTUBE_CHANNEL,
               "--title", entry["title"][:100], "--desc", entry["description"],
               "--tags", ",".join(entry["tags"]),
               "--playlist", entry["playlist_title"], "--visibility", "public"]
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
        else:
            msg = f"sau upload failed for {filename}:\n{output}"
            print(msg)
            notify_discord(f"GOES uploader: sau upload failed\n```\n{msg[:1500]}\n```")
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
