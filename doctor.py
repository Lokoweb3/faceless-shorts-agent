#!/usr/bin/env python3
"""
doctor.py — one-command health check for the Shorts agent.

Checks everything the pipeline needs, in dependency order, and prints
PASS / WARN / FAIL with the exact fix for each problem:

    system     Python version, ffmpeg/ffprobe, disk space
    deps       required Python packages importable
    config     .env present + the keys the ACTIVE CONTENT_MODE needs
    youtube    OAuth token actually refreshes (no upload happens)
    ai         the configured script provider answers a 1-token prompt
    images     Pollinations reachable (only when ASSET_PROVIDER=ai_image)
    state      state dir writable, corrupt-file backups, quota pause,
               pending uploads waiting

Run it:
    python3 doctor.py            (or: ./run.sh --doctor)
    python3 doctor.py --offline  # skip network checks (youtube/ai/images)

Exit code: 0 if nothing FAILED (warnings allowed), 1 otherwise.
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

GREEN, YELLOW, RED, DIM, NC = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

RESULTS = []  # (status, section, message, fix)


def report(status: str, section: str, message: str, fix: str = ""):
    RESULTS.append((status, section, message, fix))
    color = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[status]
    print(f"  {color}{status:<4}{NC} [{section}] {message}")
    if fix and status != "PASS":
        print(f"       {DIM}fix: {fix}{NC}")


# ─── System ───────────────────────────────────────────────────────────────────

def check_system():
    if sys.version_info >= (3, 10):
        report("PASS", "system", f"Python {sys.version.split()[0]}")
    else:
        report("FAIL", "system", f"Python {sys.version.split()[0]} is too old",
               "install Python 3.10+ (apt install python3.10 / brew install python)")

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            report("PASS", "system", f"{tool} on PATH")
        else:
            report("FAIL", "system", f"{tool} not found",
                   "Ubuntu/WSL: sudo apt install ffmpeg   |   Mac: brew install ffmpeg")

    free_gb = shutil.disk_usage(BASE_DIR).free / 1e9
    if free_gb < 0.5:
        report("FAIL", "system", f"only {free_gb:.1f} GB free disk",
               "free space — video assembly needs working room in output/")
    elif free_gb < 2:
        report("WARN", "system", f"{free_gb:.1f} GB free disk is getting low",
               "old videos in output/videos/ can be deleted after upload")
    else:
        report("PASS", "system", f"{free_gb:.0f} GB free disk")


# ─── Python deps ──────────────────────────────────────────────────────────────

def check_deps():
    required = [("aiohttp", "network calls"), ("PIL", "thumbnails (pillow)"),
                ("feedparser", "RSS discovery"), ("edge_tts", "free narration"),
                ("google_auth_oauthlib", "OAuth wizard")]
    missing = []
    for mod, why in required:
        try:
            __import__(mod)
            report("PASS", "deps", f"{mod} ({why})")
        except ImportError:
            missing.append(mod)
            report("FAIL", "deps", f"{mod} missing ({why})",
                   "pip install -r requirements.txt")
    return not missing


# ─── Config / .env ────────────────────────────────────────────────────────────

def check_config():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        report("FAIL", "config", ".env not found",
               "cp .env.example .env  then fill in your keys "
               "(and run python3 setup_oauth.py for YouTube)")
        return None

    from config import _get_env, CONTENT_MODE, config as cfg

    valid_modes = ("soccer", "horror", "scifi", "bible")
    if CONTENT_MODE in valid_modes:
        report("PASS", "config", f"CONTENT_MODE={CONTENT_MODE}")
    else:
        report("FAIL", "config", f"CONTENT_MODE={CONTENT_MODE!r} is not valid",
               f"set one of: {', '.join(valid_modes)}")

    # YouTube upload credentials
    missing = [k for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                           "YOUTUBE_REFRESH_TOKEN") if not _get_env(k)]
    if missing:
        report("FAIL", "config", f"YouTube credentials missing: {', '.join(missing)}",
               "run:  python3 setup_oauth.py   (guided; writes them into .env)")
    else:
        report("PASS", "config", "YouTube credentials present")

    # Script AI provider: any one of the four chains
    if (_get_env("XAI_API_KEY") or _get_env("GROK_API_KEY")
            or _get_env("OPENAI_API_KEY") or _get_env("ANTHROPIC_API_KEY")):
        report("PASS", "config", "script AI: hosted provider key set")
    else:
        endpoint = _get_env("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/api/generate")
        if "ollama.com" in endpoint and not _get_env("OLLAMA_API_KEY"):
            report("FAIL", "config", "Ollama Cloud endpoint set but OLLAMA_API_KEY is empty",
                   "get a key at https://ollama.com/settings/keys")
        else:
            report("PASS", "config", f"script AI: Ollama at {endpoint.split('/api')[0]}")

    # Visuals
    provider = cfg.video.asset_provider
    if provider == "pexels" and not _get_env("PEXELS_API_KEY"):
        report("FAIL", "config", "ASSET_PROVIDER=pexels but PEXELS_API_KEY is empty",
               "free key at https://www.pexels.com/api — or set ASSET_PROVIDER=ai_image")
    else:
        report("PASS", "config", f"visuals: {provider}")

    # Voice
    el_key, el_voice = _get_env("ELEVENLABS_API_KEY"), _get_env("ELEVENLABS_VOICE_ID")
    if el_key and not el_voice:
        report("WARN", "config", "ELEVENLABS_API_KEY set but ELEVENLABS_VOICE_ID empty "
               "— free edge-tts will be used instead",
               "copy a voice ID from your ElevenLabs voice library into .env")
    else:
        report("PASS", "config",
               f"voice: {'ElevenLabs' if el_key and el_voice else 'edge-tts (free)'}")

    privacy = _get_env("UPLOAD_PRIVACY", "private")
    if privacy not in ("private", "unlisted", "public"):
        report("FAIL", "config", f"UPLOAD_PRIVACY={privacy!r} is not valid",
               "use: private | unlisted | public")
    else:
        report("PASS", "config", f"upload privacy: {privacy}")
    return True


# ─── Network checks ───────────────────────────────────────────────────────────

async def check_youtube():
    from config import _get_env
    if not all(_get_env(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                                     "YOUTUBE_REFRESH_TOKEN")):
        report("WARN", "youtube", "skipped token check (credentials not set)")
        return
    from publisher import PublisherPipeline
    pub = PublisherPipeline()
    if await pub.verify_credentials():
        report("PASS", "youtube", "OAuth token refreshes — uploads will work")
    elif pub.auth_dead:
        report("FAIL", "youtube", "refresh token is expired or revoked (invalid_grant)",
               "python3 setup_oauth.py — and set the consent screen to 'In production' "
               "so tokens stop expiring after 7 days")
    else:
        report("FAIL", "youtube", "could not obtain an access token (network/creds)",
               "check your connection; if it persists, re-run python3 setup_oauth.py")


async def check_ai():
    from script import ScriptGenerator
    sg = ScriptGenerator()
    try:
        out = await asyncio.wait_for(
            sg._call_ai("Reply with the single word: ok"), timeout=60)
    except asyncio.TimeoutError:
        out = None
    if out and out.strip():
        report("PASS", "ai", f"script provider answered ({out.strip()[:30]!r})")
    else:
        report("FAIL", "ai", "no script provider answered",
               "check the AI key/endpoint/model in .env "
               "(Ollama Cloud: verify LOCAL_MODEL_NAME exists on your plan)")


async def check_images():
    from config import config as cfg
    if cfg.video.asset_provider != "ai_image":
        report("PASS", "images", f"skipped (ASSET_PROVIDER={cfg.video.asset_provider})")
        return
    import aiohttp
    url = "https://image.pollinations.ai/prompt/test?width=64&height=64&nologo=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status == 200:
                    report("PASS", "images", "Pollinations reachable")
                elif resp.status == 429:
                    report("WARN", "images", "Pollinations reachable but rate-limiting",
                           "the agent paces + caches images; a POLLINATIONS_API_KEY helps")
                else:
                    report("WARN", "images", f"Pollinations returned {resp.status}",
                           "transient errors are retried; cached images cover failures")
    except Exception as e:
        report("FAIL", "images", f"Pollinations unreachable ({type(e).__name__})",
               "check your connection; existing data/image_cache/ images still work")


# ─── State ────────────────────────────────────────────────────────────────────

def check_state():
    from config import STATE_DIR
    from storage import load_json

    probe = STATE_DIR / ".doctor_probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        report("PASS", "state", "state dir writable")
    except OSError as e:
        report("FAIL", "state", f"state dir not writable ({e})",
               f"check permissions on {STATE_DIR}")

    corrupt = list(STATE_DIR.glob("*.corrupt-*")) + \
        list((BASE_DIR / "data").glob("*.corrupt-*"))
    if corrupt:
        report("WARN", "state", f"{len(corrupt)} corrupt-file backup(s) found",
               f"a state file was damaged and reset; inspect {corrupt[0].name} "
               "if daily counts/history look wrong")
    else:
        report("PASS", "state", "no corruption backups")

    cooldown = load_json(STATE_DIR / "upload_cooldown.json", {})
    if cooldown.get("until"):
        from datetime import datetime, timezone
        try:
            until = datetime.fromisoformat(cooldown["until"])
            if datetime.now(timezone.utc) < until:
                report("WARN", "state", f"uploads quota-paused until {cooldown['until']}",
                       "normal after hitting YouTube's daily quota; resumes automatically")
            else:
                report("PASS", "state", "no active quota pause")
        except ValueError:
            report("PASS", "state", "no active quota pause")
    else:
        report("PASS", "state", "no active quota pause")

    pending = load_json(STATE_DIR / "pending_uploads.json", [])
    if pending:
        report("WARN", "state", f"{len(pending)} finished video(s) waiting to upload",
               "they retry automatically each cycle once quota/auth allows")
    else:
        report("PASS", "state", "no pending uploads")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    ap = argparse.ArgumentParser(description="Health check for the Shorts agent")
    ap.add_argument("--offline", action="store_true",
                    help="skip network checks (youtube/ai/images)")
    args = ap.parse_args()

    print("\n  Shorts agent — doctor\n")

    check_system()
    deps_ok = check_deps()
    config_ok = check_config() if deps_ok else None
    if config_ok and not args.offline:
        await check_youtube()
        await check_ai()
        await check_images()
    elif args.offline:
        print(f"  {DIM}(network checks skipped: --offline){NC}")
    if deps_ok:
        check_state()

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print()
    if fails:
        print(f"  {RED}{len(fails)} problem(s) need fixing{NC} "
              f"({len(warns)} warning(s)) — fixes are listed above.\n")
        return 1
    if warns:
        print(f"  {GREEN}Ready{NC} — {len(warns)} warning(s), nothing blocking.\n")
    else:
        print(f"  {GREEN}All checks passed — the agent is ready to run.{NC}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
