#!/usr/bin/env python3
"""
Generate engagement-optimized TikTok + Instagram Reels captions and hashtags
from story JSON files.

Usage:
  python3 generate_captions.py                      # all JSONs in output/scripts/ (skips ones already done)
  python3 generate_captions.py path/to/story.json   # one or more specific JSON files
  python3 generate_captions.py --force              # regenerate even if a caption already exists
  python3 generate_captions.py --force path/to.json # force a specific file

Output: a <name>_caption.txt file in output/captions/ for each story, AND prints
to the console so you can copy-paste straight into TikTok / Reels.

Captions are written to maximize watch-time, comments, shares and saves — i.e.
to get people to watch to the end and clip/share your videos.
"""
import asyncio
import glob
import json
import sys
from pathlib import Path

from config import SCRIPTS_OUTPUT_DIR, OUTPUT_DIR
from script import ScriptGenerator

CAPTIONS_DIR = OUTPUT_DIR / "captions"

PROMPT = """You are a viral short-form caption writer for a faceless AI-horror / sci-fi story channel that posts to TikTok and Instagram Reels. Your captions are engineered to maximize WATCH-TIME, COMMENTS, SHARES and SAVES — to make people watch to the very end and clip/share the video.

STORY TITLE: {title}

STORY:
{story}

Write captions for this video. Output EXACTLY the two blocks below and NOTHING else:

TIKTOK:
<one or two SHORT punchy lines. Line 1 = a curiosity hook that teases the dread but does NOT spoil the twist or ending. Line 2 = an engagement call-to-action: a question viewers will want to answer, or "watch till the end 👀", or "tag someone who needs to see this". Use 1-2 fitting emojis. Keep it tight.>
<ONE line of 10-12 hashtags, space-separated, mixing niche tags (#AIHorror #scarystories #aistories #techhorror #creepy #scifi) with broad-reach tags (#fyp #foryou #foryoupage #viral)>

REELS:
<same punchy style, may differ slightly from the TikTok hook>
<ONE line of about 10 hashtags tuned for Instagram Reels, including #reels #reelsinstagram #explore plus the niche horror/sci-fi tags>

Hard rules:
- NEVER spoil the ending or the twist.
- No quotation marks around the caption text.
- Output only the TIKTOK: and REELS: blocks exactly as shown."""


async def generate_for(path: str, sg: ScriptGenerator, force: bool = False) -> bool:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠ skip {p.name}: cannot read JSON ({e})")
        return False

    title = data.get("seo_title") or data.get("title") or ""
    story = (data.get("full_text") or "").strip()
    if not story:
        print(f"  ⚠ skip {p.name}: no 'full_text' in JSON")
        return False

    out_path = CAPTIONS_DIR / (p.stem + "_caption.txt")
    if out_path.exists() and not force:
        print(f"  ✓ already done: {out_path.name}  (use --force to regenerate)")
        return False

    prompt = PROMPT.format(title=title, story=story[:1400])
    result = await sg._call_ai(prompt)
    if not result or not result.strip():
        print(f"  ✗ FAILED {p.name}: the AI returned nothing")
        return False

    CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    header = f"# Captions for: {title}\n# Source JSON: {p.name}\n\n"
    out_path.write_text(header + result.strip() + "\n", encoding="utf-8")

    print(f"\n========== {title} ==========")
    print(result.strip())
    print(f"\n(saved -> {out_path})\n")
    return True


async def main():
    raw_args = sys.argv[1:]
    force = "--force" in raw_args
    files = [a for a in raw_args if not a.startswith("--")]

    if not files:
        files = sorted(glob.glob(str(SCRIPTS_OUTPUT_DIR / "*.json")))

    if not files:
        print("No story JSON files found.")
        print(f"Looked in: {SCRIPTS_OUTPUT_DIR}")
        print("Generate some videos first, or pass a JSON path directly.")
        return

    sg = ScriptGenerator()
    print(f"Generating captions for {len(files)} story file(s)...\n")
    done = 0
    for f in files:
        if await generate_for(f, sg, force=force):
            done += 1

    print(f"Done. {done} caption file(s) written to {CAPTIONS_DIR}")
    if done:
        print("Open them with:  explorer.exe output\\captions")


if __name__ == "__main__":
    asyncio.run(main())
