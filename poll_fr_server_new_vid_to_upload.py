# mac_uploader.py
import hashlib, json, subprocess
from pathlib import Path
import requests

MANIFEST_URL = "https://usernmaejkhgdfys:4hgj354g3j5h@ljhgfdhtryjfygjh.lionfabric.page/GOES/manifest.json"
STATE_FILE = Path.home() / ".sau_uploader_state.json"
DOWNLOAD_DIR = Path.home() / "sat_downloads"
SAU_BIN = Path.home() / "s/social-auto-upload/.venv/bin/sau"
SAU_ACCOUNT = "me"

def load_state():
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()

def save_state(done):
    STATE_FILE.write_text(json.dumps(sorted(done)))

def main():
    done = load_state()
    manifest = requests.get(MANIFEST_URL, timeout=30).json()

    for entry in manifest:
        if entry["filename"] in done:
            continue

        local_path = DOWNLOAD_DIR / entry["filename"]
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        with requests.get(entry["url"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

        # verify before trusting it -- don't upload a truncated download
        if hashlib.sha256(local_path.read_bytes()).hexdigest() != entry["sha256"]:
            print(f"checksum mismatch for {entry['filename']}, will retry next run")
            local_path.unlink(missing_ok=True)
            continue

        cmd = [str(SAU_BIN), "youtube", "upload-video",
               "--account", SAU_ACCOUNT, "--file", str(local_path),
               "--title", entry["title"][:100], "--desc", entry["description"],
               "--tags", ",".join(entry["tags"]),
               "--playlist", entry["playlist_title"], "--visibility", "public"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            done.add(entry["filename"])
            save_state(done)
            local_path.unlink()  # server can clean up its own copy on its own retention schedule
        else:
            print(f"sau upload failed for {entry['filename']}:\n{result.stdout}\n{result.stderr}")

if __name__ == "__main__":
    main()