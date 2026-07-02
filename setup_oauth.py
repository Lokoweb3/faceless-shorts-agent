#!/usr/bin/env python3
"""
Guided YouTube OAuth setup — creates the refresh token the agent needs to upload.

What it does:
  1. Walks you through the one-time Google Cloud console steps (direct links)
  2. Opens your browser for Google sign-in (loopback OAuth flow — no copy/paste
     of codes, no OAuth playground)
  3. Writes YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN
     into .env automatically
  4. Verifies the token by fetching your channel name

Run it:
    python3 setup_oauth.py          (or: ./run.sh --auth)

Re-run it any time a token expires or you switch Google accounts/channels.

IMPORTANT — the 7-day token trap:
  If your OAuth consent screen is left in "Testing" mode, Google EXPIRES the
  refresh token after 7 days and uploads silently stop. Set the app to
  "In production" (step 3 below). No Google verification is needed for that —
  you'll just see an "unverified app" warning once during sign-in, which is
  fine because you are the only user of your own app.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
ENV_FILE = BASE_DIR / ".env"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

STEPS = """
────────────────────────────────────────────────────────────────────
 One-time Google Cloud setup (~10 minutes). Skip any step you've done.
────────────────────────────────────────────────────────────────────

 STEP 1 — Create a Google Cloud project (any name, e.g. "yt-agent")
     https://console.cloud.google.com/projectcreate

 STEP 2 — Enable the YouTube Data API v3 for that project
     https://console.cloud.google.com/apis/library/youtube.googleapis.com

 STEP 3 — Configure the OAuth consent screen
     https://console.cloud.google.com/apis/credentials/consent
       - User type: External -> Create
       - Fill in only the required fields (app name, your email)
       - ***CRITICAL***: after creating it, click "PUBLISH APP" so the
         status says "In production". If you leave it in "Testing",
         your refresh token EXPIRES EVERY 7 DAYS and uploads stop.

 STEP 4 — Create OAuth credentials
     https://console.cloud.google.com/apis/credentials
       - "+ Create credentials" -> "OAuth client ID"
       - Application type: "Desktop app"
       - Copy the Client ID and Client Secret — you'll paste them below.
────────────────────────────────────────────────────────────────────
"""


def _read_env() -> dict:
    """Parse the existing .env (simple KEY=value lines)."""
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip("\"'")
    return values


def _write_env(new_values: dict) -> None:
    """Update KEY=value lines in .env in place; append any that are missing."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(new_values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- YouTube OAuth (written by setup_oauth.py) ----")
        for k, v in remaining.items():
            out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        import os
        os.chmod(ENV_FILE, 0o600)  # owner-only (no-op on Windows-mounted drives)
    except OSError:
        pass


def _mask(secret: str) -> str:
    return secret[:8] + "..." if len(secret) > 12 else "(set)"


def _prompt(label: str, current: str = "") -> str:
    suffix = f" [{_mask(current)} — Enter to keep]" if current else ""
    while True:
        val = input(f"  {label}{suffix}: ").strip()
        if not val and current:
            return current
        if val:
            return val
        print("  A value is required.")


def _verify_channel(access_token: str):
    """1-quota-unit sanity check: fetch the authorized channel's name."""
    url = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    items = data.get("items", [])
    return items[0]["snippet"]["title"] if items else None


def main() -> int:
    print(__doc__.split("IMPORTANT")[0])

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib is not installed.\n"
              "Run:  pip install -r requirements.txt   (or ./run.sh once)")
        return 1

    print(STEPS)
    input("Press Enter when steps 1-4 are done (or were already done)... ")

    env = _read_env()
    print("\nPaste your OAuth client credentials (from step 4):")
    client_id = _prompt("Client ID", env.get("YOUTUBE_CLIENT_ID", ""))
    client_secret = _prompt("Client Secret", env.get("YOUTUBE_CLIENT_SECRET", ""))

    if not re.search(r"\.apps\.googleusercontent\.com$", client_id):
        print("\n  WARNING: that Client ID doesn't end in .apps.googleusercontent.com —"
              "\n  double-check you copied the ID, not the name. Continuing anyway.\n")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("\nOpening your browser for Google sign-in...\n"
          "  - Pick the Google account that OWNS the YouTube channel.\n"
          "  - If you see 'Google hasn't verified this app', click\n"
          "    'Advanced' -> 'Go to <your app>' — it's YOUR app, that's expected.\n")

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    try:
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",  # guarantees a refresh_token is issued
            authorization_prompt_message=(
                "If the browser didn't open, visit this URL manually:\n{url}\n"),
            success_message=("Authorized! You can close this tab and return "
                             "to the terminal."),
        )
    except Exception as e:
        print(f"\nERROR: the OAuth flow failed: {e}\n"
              "Common causes: wrong client ID/secret, or the YouTube Data API\n"
              "isn't enabled for this project (step 2).")
        return 1

    if not creds.refresh_token:
        print("\nERROR: Google returned no refresh token. Re-run this script — "
              "the 'prompt=consent' retry usually fixes it.")
        return 1

    # Sanity check before writing anything.
    channel = None
    try:
        channel = _verify_channel(creds.token)
    except Exception as e:
        print(f"\nWARNING: could not verify the channel ({e}). "
              "The token was still issued; continuing.")

    _write_env({
        "YOUTUBE_CLIENT_ID": client_id,
        "YOUTUBE_CLIENT_SECRET": client_secret,
        "YOUTUBE_REFRESH_TOKEN": creds.refresh_token,
    })

    print("\n────────────────────────────────────────────────────────────────")
    if channel:
        print(f"  SUCCESS — authorized channel: {channel}")
    else:
        print("  SUCCESS — token issued and saved.")
    print(f"  Credentials written to: {ENV_FILE}")
    print("\n  Final reminders:")
    print("   - Consent screen MUST say 'In production', or this token dies in 7 days:")
    print("     https://console.cloud.google.com/apis/credentials/consent")
    print("   - Uploads default to UPLOAD_PRIVACY=private; flip to public in .env")
    print("     once you've checked a few videos.")
    print("  You're ready:  ./run.sh --once --dry-run")
    print("────────────────────────────────────────────────────────────────\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
