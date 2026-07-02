"""
Video Processing Module for the Autonomous YouTube Soccer Content Agent.
Handles downloading, clipping, overlays, transitions, and final video assembly.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from config import (config, DATA_DIR, VIDEO_OUTPUT_DIR, THUMBNAIL_OUTPUT_DIR,
                    AUDIO_OUTPUT_DIR, CONTENT_MODE, _get_env)

logger = logging.getLogger(__name__)


# ─── Pollinations request pacing ──────────────────────────────────────────────

class _MinIntervalLimiter:
    """Spaces out requests to a shared free API (per-process).

    Bursts of 6-12 back-to-back image requests per video are what trip
    Pollinations' 429s — a small fixed gap between requests avoids most of
    them. Tune with AI_IMAGE_MIN_INTERVAL (seconds) in .env.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


try:
    _AI_IMAGE_INTERVAL = float(_get_env("AI_IMAGE_MIN_INTERVAL", "4") or 4)
except ValueError:
    _AI_IMAGE_INTERVAL = 4.0
_POLLINATIONS_LIMITER = _MinIntervalLimiter(_AI_IMAGE_INTERVAL)

# ─── Utility ──────────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: List[str], timeout: int = 300) -> Tuple[bool, str]:
    """Run an ffmpeg command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg error: {result.stderr[:500]}")
            return False, result.stderr
        return True, result.stdout or ""
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out")
        return False, "timeout"
    except FileNotFoundError:
        logger.error("ffmpeg not found. Install with: apt install ffmpeg")
        return False, "ffmpeg not found"
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False, str(e)


def _redact(text) -> str:
    """Mask secret query params (e.g. Pollinations `&key=...`) before logging."""
    import re
    return re.sub(r'([?&](?:key|api_key|token)=)[^&\s]+', r'\1***', str(text))


async def _fetch_url(session, url, **kwargs):
    """Async GET helper."""
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.read()


# ─── Licensed Stock Asset Fetcher (COMPLIANT) ──────────────────────────────────

class StockAssetFetcher:
    """Sources background visuals from LICENSED stock or generates them.

    This replaces the old yt-dlp downloader. It never downloads or reuploads
    broadcast/match footage (that is copyright infringement + a YouTube ToS
    violation that Content ID catches automatically). Instead it pulls
    royalty-free vertical clips from Pexels, and falls back to neutral
    generated motion backgrounds when no provider/key is available.
    """

    PEXELS_VIDEO = "https://api.pexels.com/videos/search"
    CACHE_KEEP = 500  # max cached images per mode (oldest pruned beyond this)

    def __init__(self):
        self.asset_dir = VIDEO_OUTPUT_DIR / "assets"
        self.asset_dir.mkdir(parents=True, exist_ok=True)

    # ── On-disk image library (Pollinations fallback) ─────────────────────
    # Every successful generation is kept. When Pollinations rate-limits or
    # errors out, a previously generated on-theme image is reused instead of
    # the old coloured-noise gradient — a far better retention fallback, and
    # it works fully offline once a channel has produced a few videos.

    def _cache_dir(self) -> Path:
        d = DATA_DIR / "image_cache" / (CONTENT_MODE or "default")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cache_store(self, content: bytes, prompt: str, seed: int) -> None:
        try:
            import hashlib
            name = hashlib.sha1(f"{prompt}|{seed}".encode()).hexdigest()[:16] + ".jpg"
            path = self._cache_dir() / name
            if not path.exists():
                path.write_bytes(content)
            # Prune the oldest beyond the cap so the cache can't grow unbounded.
            files = sorted(self._cache_dir().glob("*.jpg"),
                           key=lambda p: p.stat().st_mtime)
            for old in files[:-self.CACHE_KEEP]:
                old.unlink()
        except OSError as e:
            logger.debug(f"Image cache write failed: {e}")

    def _cache_pick(self) -> Optional[Path]:
        """A random previously generated image for this mode, if any."""
        import random
        try:
            files = list(self._cache_dir().glob("*.jpg"))
        except OSError:
            return None
        return random.choice(files) if files else None

    async def fetch(self, query: str, seconds: float, output_name: str) -> Optional[Path]:
        """Return one vertical clip of ~`seconds` for `query` (licensed or generated)."""
        provider = config.video.asset_provider
        if provider == "ai_image":
            clip = await self._fetch_ai_image(query, seconds, output_name)
            if clip:
                return clip
            logger.warning("AI image miss for %r - using generated background", query)
        elif provider == "pexels" and config.api.pexels_api_key:
            clip = await self._fetch_pexels(query, seconds, output_name)
            if clip:
                return clip
            logger.warning("Pexels miss for %r - using generated background", query)
        elif provider == "pexels":
            logger.warning("No PEXELS_API_KEY set - using generated backgrounds")
        return await self._generate(query, seconds, output_name)

    async def _fetch_pexels(self, query: str, seconds: float,
                            output_name: str) -> Optional[Path]:
        import random
        try:
            import aiohttp
            headers = {"Authorization": config.api.pexels_api_key}
            params = {"query": query, "per_page": 5, "orientation": "portrait"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(self.PEXELS_VIDEO, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                videos = data.get("videos", [])
                if not videos:
                    return None
                pick = random.choice(videos[:min(5, len(videos))])
                files = sorted(pick["video_files"],
                               key=lambda f: (f.get("height") or 0), reverse=True)
                link = files[0]["link"]
                raw = self.asset_dir / f"{output_name}_raw.mp4"
                content = await _fetch_url(session, link,
                                          timeout=aiohttp.ClientTimeout(total=120))
                raw.write_bytes(content)
        except Exception as e:
            logger.error(f"Pexels fetch error: {e}")
            return None

        # trim to the requested length (no audio)
        out = self.asset_dir / f"{output_name}.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(raw), "-t", f"{seconds:.2f}", "-an", str(out)]
        ok, _ = await asyncio.get_event_loop().run_in_executor(None, _run_ffmpeg, cmd, 120)
        return out if ok and out.exists() else None

    async def _fetch_ai_image(self, query: str, seconds: float,
                              output_name: str) -> Optional[Path]:
        """Generate an AI 'story' image (Pollinations) and turn it into a clip.

        Free and key-less by default. The still gets Ken Burns motion later in
        _normalize, producing the illustrated 'animated story' look. Any failure
        returns None so the caller falls back to a generated background.
        """
        import random
        from urllib.parse import quote
        prompt = f"{query}, {config.video.ai_image_style}"
        w, h, fps = config.video.width, config.video.height, config.video.fps
        seed = random.randint(1, 9_999_999)
        url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}"
               f"?width={w}&height={h}&model={config.video.ai_image_model}"
               f"&nologo=true&seed={seed}")
        if config.api.pollinations_api_key:
            url += f"&key={config.api.pollinations_api_key}"

        img = self.asset_dir / f"{output_name}.jpg"
        import asyncio
        max_attempts = 4
        got_image = False
        for attempt in range(1, max_attempts + 1):
            try:
                import aiohttp
                # vary the seed each retry so a flaky prompt gets a fresh roll
                if attempt > 1:
                    used_seed = random.randint(1, 9_999_999)
                    retry_url = url.replace(f"seed={seed}", f"seed={used_seed}")
                else:
                    used_seed = seed
                    retry_url = url
                await _POLLINATIONS_LIMITER.wait()  # pace shared free API
                async with aiohttp.ClientSession() as session:
                    content = await _fetch_url(
                        session, retry_url, timeout=aiohttp.ClientTimeout(total=120))
                if content and len(content) >= 1000:
                    img.write_bytes(content)
                    self._cache_store(content, prompt, used_seed)
                    got_image = True
                    break  # success
                logger.warning("AI image came back empty for %r (attempt %d/%d)",
                               query, attempt, max_attempts)
            except Exception as e:
                is_rate = "429" in str(e) or "Too Many Requests" in str(e)
                if attempt < max_attempts:
                    # exponential backoff with jitter; longer waits for rate limits
                    base = 4.0 if is_rate else 1.5
                    delay = base * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
                    logger.warning("AI image %s for %r — retrying in %.1fs (attempt %d/%d)",
                                   "rate-limited" if is_rate else "failed",
                                   query, delay, attempt, max_attempts)
                    await asyncio.sleep(delay)
                    continue
                logger.warning(f"AI image fetch failed for {query!r} after "
                               f"{max_attempts} attempts: {_redact(e)}")
        if not got_image:
            # Fall back to a previously generated on-theme image (far better
            # than the generated-gradient fallback, and free/offline).
            cached = self._cache_pick()
            if cached is None:
                return None
            logger.warning("Using cached background for %r (Pollinations unavailable)",
                           query)
            try:
                shutil.copy(str(cached), str(img))
            except OSError:
                return None

        # Turn the still into a short clip; _normalize adds the Ken Burns motion.
        raw = self.asset_dir / f"{output_name}_raw.mp4"
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(img),
               "-t", f"{max(seconds + 0.5, 1.0):.2f}",
               "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                       f"crop={w}:{h},fps={fps}"),
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               str(raw)]
        ok, _ = await asyncio.get_event_loop().run_in_executor(None, _run_ffmpeg, cmd, 120)
        return raw if ok and raw.exists() else None

    async def _generate(self, query: str, seconds: float,
                        output_name: str) -> Optional[Path]:
        """License-clean animated gradient, colour derived from the query."""
        import hashlib
        h = int(hashlib.sha1(query.encode()).hexdigest(), 16)
        hue = h % 360
        r_, g_, b_ = (hue * 7) % 256, (hue * 13) % 256, (hue * 29) % 256
        color = f"0x{r_:02x}{g_:02x}{b_:02x}"
        out = self.asset_dir / f"{output_name}.mp4"
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s={config.video.width}x{config.video.height}"
                  f":d={seconds:.2f}:r={config.video.fps}",
            "-vf", "noise=alls=8:allf=t,vignette",
            "-t", f"{seconds:.2f}", str(out),
        ]
        ok, _ = await asyncio.get_event_loop().run_in_executor(None, _run_ffmpeg, cmd, 60)
        return out if ok and out.exists() else None


# ─── Video Clipper ────────────────────────────────────────────────────────────

class VideoClipper:
    """Clips and processes video segments."""

    def __init__(self):
        self.clip_dir = VIDEO_OUTPUT_DIR / "clips"
        self.clip_dir.mkdir(parents=True, exist_ok=True)

    async def clip_segment(self, input_path: Path, start_time: float,
                           duration: float, output_name: str) -> Optional[Path]:
        """Extract a segment from a video file."""
        output_path = self.clip_dir / f"{output_name}.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", str(input_path),
            "-t", str(duration),
            "-c:v", "libx264",
            "-c:a", "aac",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Clipped segment: {output_path}")
            return output_path
        return None

    async def crop_vertical(self, input_path: Path, output_name: str) -> Optional[Path]:
        """Crop video to 9:16 vertical format for Shorts."""
        output_path = self.clip_dir / f"{output_name}_vertical.mp4"

        # Get video dimensions
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(input_path),
        ]
        try:
            probe_result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=10
            )
            dims = probe_result.stdout.strip().split(",")
            if len(dims) == 2:
                width, height = int(dims[0]), int(dims[1])
            else:
                width, height = 1920, 1080
        except Exception:
            width, height = 1920, 1080

        # Calculate crop: center crop to 9:16 aspect ratio
        target_ratio = 9 / 16
        current_ratio = width / height

        if current_ratio > target_ratio:
            # Wider than 9:16 - crop width
            new_width = int(height * target_ratio)
            new_height = height
            x_offset = (width - new_width) // 2
            y_offset = 0
        else:
            # Taller than 9:16 - crop height
            new_width = width
            new_height = int(width / target_ratio)
            x_offset = 0
            y_offset = (height - new_height) // 2

        crop_filter = f"crop={new_width}:{new_height}:{x_offset}:{y_offset}"
        scale_filter = f"scale={config.video.width}:{config.video.height}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", f"{crop_filter},{scale_filter}",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Cropped to vertical: {output_path}")
            return output_path
        return None

    async def speed_ramp(self, input_path: Path, speed: float,
                         output_name: str) -> Optional[Path]:
        """Apply speed change (slow-mo or fast-forward)."""
        output_path = self.clip_dir / f"{output_name}_speed.mp4"

        setpts = f"setpts={1/speed}*PTS"
        atempo = f"atempo={speed}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", setpts,
            "-af", atempo,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Speed ramped ({speed}x): {output_path}")
            return output_path
        return None

    async def ken_burns(self, input_path: Path, output_name: str,
                        duration: float = 4.0) -> Optional[Path]:
        """Apply Ken Burns effect (slow zoom) to a static image or video."""
        output_path = self.clip_dir / f"{output_name}_kenburns.mp4"

        zoom = config.video.ken_burns_zoom
        zoom_filter = (
            f"zoompan=z='min(zoom+0.001,{zoom})':"
            f"d={int(duration * config.video.fps)}:"
            f"fps={config.video.fps}:"
            f"s={config.video.width}x{config.video.height}"
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(input_path),
            "-t", str(duration),
            "-vf", zoom_filter,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Ken Burns effect applied: {output_path}")
            return output_path
        return None


# ─── Overlay System ───────────────────────────────────────────────────────────

class OverlayEngine:
    """Adds text overlays, lower thirds, and score displays to videos."""

    def __init__(self):
        self.clip_dir = VIDEO_OUTPUT_DIR / "clips"
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.font_path = self._find_font()

    def _find_font(self) -> str:
        """Find a suitable font on the system."""
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return ""

    async def add_text_overlay(self, input_path: Path, text: str,
                               output_name: str, position: str = "center",
                               font_size: Optional[int] = None) -> Optional[Path]:
        """Add a text overlay to a video."""
        output_path = self.clip_dir / f"{output_name}_overlay.mp4"
        font_size = font_size or config.video.font_size_title

        # Escape text for ffmpeg drawtext
        safe_text = text.replace(":", "\\:").replace("'", "\\'")

        # Position
        pos_map = {
            "center": "(w-text_w)/2:(h-text_h)/2",
            "top": "(w-text_w)/2:50",
            "bottom": "(w-text_w)/2:h-text_h-50",
            "lower_third": "(w-text_w)/2:h*0.75",
        }
        pos = pos_map.get(position, pos_map["center"])

        drawtext = (
            f"drawtext=text='{safe_text}':"
            f"fontfile={self.font_path}:"
            f"fontsize={font_size}:"
            f"fontcolor={config.video.font_color}:"
            f"bordercolor={config.video.font_outline_color}:"
            f"borderw={config.video.font_outline_width}:"
            f"x={pos}:"
            f"y={pos}:"
            f"enable='between(t,0,60)'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", drawtext,
            "-c:v", "libx264",
            "-c:a", "copy",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Text overlay added: {output_path}")
            return output_path
        return None

    async def add_lower_third(self, input_path: Path, title: str,
                              subtitle: str = "",
                              output_name: str = "lower_third") -> Optional[Path]:
        """Add a lower-third overlay with title and subtitle."""
        output_path = self.clip_dir / f"{output_name}_lowerthird.mp4"

        # Create a semi-transparent bar at the bottom
        bar_height = 120
        bar_y = config.video.height - bar_height - 30

        # Draw bar using a colored box.
        # Pre-escape the ':' for ffmpeg drawtext OUTSIDE the f-string -- a
        # backslash inside an f-string expression is a SyntaxError before
        # Python 3.12, which is why this previously failed to import.
        esc_title = title.replace(":", "\\:")
        drawtext_title = (
            f"drawtext=text='{esc_title}':"
            f"fontfile={self.font_path}:"
            f"fontsize={config.video.font_size_lower_third}:"
            f"fontcolor=white:"
            f"bordercolor=black:borderw=1:"
            f"x=50:y={bar_y + 10}:"
            f"enable='between(t,0,60)'"
        )

        filters = [drawtext_title]
        if subtitle:
            esc_sub = subtitle.replace(":", "\\:")
            drawtext_sub = (
                f"drawtext=text='{esc_sub}':"
                f"fontfile={self.font_path}:"
                f"fontsize={config.video.font_size_lower_third - 6}:"
                f"fontcolor=#CCCCCC:"
                f"bordercolor=black:borderw=1:"
                f"x=50:y={bar_y + 50}:"
                f"enable='between(t,0,60)'"
            )
            filters.append(drawtext_sub)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-c:a", "copy",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Lower third added: {output_path}")
            return output_path
        return None

    async def add_countdown(self, input_path: Path, seconds: int,
                            output_name: str = "countdown") -> Optional[Path]:
        """Add a countdown timer overlay."""
        output_path = self.clip_dir / f"{output_name}_countdown.mp4"

        # Use ffmpeg's drawtext with time function
        drawtext = (
            f"drawtext=text='%{{eif\\:({seconds}-trunc(t))\\:d}}':"
            f"fontfile={self.font_path}:"
            f"fontsize=120:"
            f"fontcolor=white:"
            f"bordercolor=black:borderw=3:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable='between(t,0,{seconds})'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", drawtext,
            "-c:v", "libx264",
            "-c:a", "copy",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Countdown overlay added: {output_path}")
            return output_path
        return None

    async def add_score_display(self, input_path: Path, team1: str, team2: str,
                                score1: int, score2: int,
                                output_name: str = "score") -> Optional[Path]:
        """Add a score display overlay (top of video)."""
        output_path = self.clip_dir / f"{output_name}_score.mp4"

        score_text = f"{team1} {score1} - {score2} {team2}"
        safe_text = score_text.replace(":", "\\:")

        drawtext = (
            f"drawtext=text='{safe_text}':"
            f"fontfile={self.font_path}:"
            f"fontsize=36:"
            f"fontcolor=white:"
            f"bordercolor=black:borderw=2:"
            f"x=(w-text_w)/2:y=30:"
            f"box=1:boxcolor=black@0.5:boxborderw=10:"
            f"enable='between(t,0,60)'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", drawtext,
            "-c:v", "libx264",
            "-c:a", "copy",
            "-preset", "fast",
            "-crf", "22",
            str(output_path),
        ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 120
        )

        if success and output_path.exists():
            logger.info(f"Score display added: {output_path}")
            return output_path
        return None


# ─── Audio Processing ─────────────────────────────────────────────────────────

class AudioProcessor:
    """Handles audio ducking, mixing, and processing."""

    def __init__(self):
        self.audio_dir = AUDIO_OUTPUT_DIR
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _has_audio(path: Path) -> bool:
        """True if the file has at least one audio stream."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            return bool(r.stdout.strip())
        except Exception:
            return False

    async def mix_audio(self, video_path: Path, voiceover_path: Path,
                        music_path: Optional[Path] = None,
                        output_name: str = "mixed") -> Optional[Path]:
        """Mix voiceover with background music (with ducking).

        Robust to source video having NO audio track (the normal case for
        stock/generated backgrounds): in that case the voiceover (+ music)
        becomes the entire soundtrack.
        """
        output_path = self.audio_dir / f"{output_name}_audio.mp4"
        video_has_audio = self._has_audio(video_path)

        if music_path and music_path.exists():
            # Voiceover at full volume; music ducks under it.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(voiceover_path),
                "-i", str(music_path),
                "-filter_complex",
                (
                    f"[1:a]asplit=2[vo1][vo2];"
                    f"[vo2]alimiter=limit=1:attack=0.01:release={config.video.ducking_release}[side];"
                    f"[2:a]volume={config.video.background_music_volume}[music];"
                    f"[music][side]sidechaincompress=threshold=-20dB:ratio=10:attack=10:release=500[music_ducked];"
                    f"[vo1][music_ducked]amix=inputs=2:duration=first:dropout_transition=2[audio_out]"
                ),
                "-map", "0:v",
                "-map", "[audio_out]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", config.video.audio_bitrate,
                "-shortest",
                str(output_path),
            ]
        elif video_has_audio:
            # Duck the (real) source audio under the voiceover.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(voiceover_path),
                "-filter_complex",
                (
                    f"[0:a]volume=0.3[orig];"
                    f"[1:a]volume={config.video.voiceover_volume}[vo];"
                    f"[orig][vo]amix=inputs=2:duration=first[audio_out]"
                ),
                "-map", "0:v",
                "-map", "[audio_out]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", config.video.audio_bitrate,
                "-shortest",
                str(output_path),
            ]
        else:
            # Silent video: voiceover is the whole soundtrack.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(voiceover_path),
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", config.video.audio_bitrate,
                "-shortest",
                str(output_path),
            ]

        success, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg, cmd, 180
        )

        if success and output_path.exists():
            logger.info(f"Audio mixed: {output_path}")
            return output_path
        return None

    async def get_duration(self, audio_path: Path) -> float:
        """Get duration of an audio file in seconds."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(audio_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except Exception:
            return 0.0


# ─── Thumbnail Generator ──────────────────────────────────────────────────────

class ThumbnailGenerator:
    """Generates eye-catching thumbnails using PIL."""

    def __init__(self):
        self.output_dir = THUMBNAIL_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.font_path = self._find_font()

    def _find_font(self) -> Optional[str]:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    async def generate(self, title: str, category: str,
                       background_image: Optional[Path] = None,
                       output_name: str = "thumbnail") -> Optional[Path]:
        """Generate a thumbnail image."""
        output_path = self.output_dir / f"{output_name}.jpg"

        # Create thumbnail in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._generate_sync, title, category, background_image, output_path
        )
        return result

    def _generate_sync(self, title: str, category: str,
                       background_image: Optional[Path],
                       output_path: Path) -> Optional[Path]:
        """Synchronous thumbnail generation."""
        try:
            # Create base image
            if background_image and background_image.exists():
                img = Image.open(background_image).convert("RGB")
                img = img.resize((1280, 720), Image.LANCZOS)
            else:
                # Create gradient background
                img = Image.new("RGB", (1280, 720))
                for y in range(720):
                    r = int(30 + (y / 720) * 20)
                    g = int(80 + (y / 720) * 30)
                    b = int(150 + (y / 720) * 40)
                    for x in range(1280):
                        img.putpixel((x, y), (r, g, b))

            draw = ImageDraw.Draw(img)

            # Add semi-transparent overlay
            overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 100))
            img.paste(overlay, (0, 0), overlay)

            # Add category badge
            cat_colors = {
                "world_cup_moments": "#FFD700",
                "legendary_matches": "#FF4500",
                "iconic_goals": "#00FF7F",
                "player_stories": "#1E90FF",
                "controversial_moments": "#FF0000",
                "transfers": "#FF69B4",
                "records": "#9370DB",
                "rivalries": "#FF8C00",
            }
            cat_color = cat_colors.get(category, "#FFFFFF")
            cat_name = config.categories.get(category, {}).get("name", category)

            # Draw category badge
            if self.font_path:
                try:
                    font_small = ImageFont.truetype(self.font_path, 36)
                    font_large = ImageFont.truetype(self.font_path, 64)
                except Exception:
                    font_small = ImageFont.load_default()
                    font_large = ImageFont.load_default()
            else:
                font_small = ImageFont.load_default()
                font_large = ImageFont.load_default()

            # Category badge
            draw.rectangle([(20, 20), (250, 70)], fill=cat_color)
            draw.text((30, 25), cat_name.upper(), fill="black", font=font_small)

            # Main title
            # Word wrap title
            words = title.split()
            lines = []
            current_line = ""
            for word in words:
                test_line = f"{current_line} {word}".strip()
                bbox = draw.textbbox((0, 0), test_line, font=font_large)
                if bbox[2] - bbox[0] < 1200:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = word
            if current_line:
                lines.append(current_line)

            # Draw title lines
            y_start = 300
            for i, line in enumerate(lines[:3]):
                bbox = draw.textbbox((0, 0), line, font=font_large)
                text_w = bbox[2] - bbox[0]
                x = (1280 - text_w) // 2
                # Outline
                for dx, dy in [(-2, -2), (-2, 2), (2, -2), (2, 2)]:
                    draw.text((x + dx, y_start + i * 80 + dy), line,
                              fill="black", font=font_large)
                draw.text((x, y_start + i * 80), line,
                          fill="white", font=font_large)

            # Save
            img.save(output_path, "JPEG", quality=90)
            logger.info(f"Thumbnail generated: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Thumbnail generation error: {e}")
            return None


# ─── Video Assembler ─────────────────────────────────────────────────────────

class VideoAssembler:
    """Assembles final video from clips, overlays, and audio."""

    def __init__(self):
        self.output_dir = VIDEO_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def assemble(self, clips: List[Path], voiceover_path: Path,
                       music_path: Optional[Path] = None,
                       output_name: str = "final_video") -> Optional[Path]:
        """Assemble final video from multiple clips with voiceover and music."""
        output_path = self.output_dir / f"{output_name}.mp4"

        if not clips:
            logger.error("No clips to assemble")
            return None

        if len(clips) == 1:
            # Single clip - just mix audio
            processor = AudioProcessor()
            return await processor.mix_audio(
                clips[0], voiceover_path, music_path, output_name
            )

        # Multiple clips - concatenate first
        concat_path = self.output_dir / f"{output_name}_concat.mp4"
        concat_list = self.output_dir / f"{output_name}_list.txt"

        try:
            with open(concat_list, "w") as f:
                for clip in clips:
                    f.write(f"file '{clip.resolve()}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-preset", "fast",
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                str(concat_path),
            ]

            success, _ = await asyncio.get_event_loop().run_in_executor(
                None, _run_ffmpeg, cmd, 180
            )

            if not success or not concat_path.exists():
                logger.error("Failed to concatenate clips")
                return None

            # Mix audio
            processor = AudioProcessor()
            return await processor.mix_audio(
                concat_path, voiceover_path, music_path, output_name
            )

        except Exception as e:
            logger.error(f"Video assembly error: {e}")
            return None
        finally:
            # Clean up concat list
            if concat_list.exists():
                concat_list.unlink()

    async def add_intro_outro(self, video_path: Path,
                              intro_path: Optional[Path] = None,
                              outro_path: Optional[Path] = None,
                              output_name: str = "final") -> Optional[Path]:
        """Add intro and outro cards to the video."""
        if not intro_path and not outro_path:
            return video_path

        output_path = self.output_dir / f"{output_name}_intro_outro.mp4"

        # Build concat list
        concat_list = self.output_dir / f"{output_name}_io_list.txt"
        try:
            with open(concat_list, "w") as f:
                if intro_path and intro_path.exists():
                    f.write(f"file '{intro_path.resolve()}'\n")
                f.write(f"file '{video_path.resolve()}'\n")
                if outro_path and outro_path.exists():
                    f.write(f"file '{outro_path.resolve()}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-preset", "fast",
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                str(output_path),
            ]

            success, _ = await asyncio.get_event_loop().run_in_executor(
                None, _run_ffmpeg, cmd, 180
            )

            if success and output_path.exists():
                return output_path
            return video_path
        except Exception as e:
            logger.error(f"Intro/outro error: {e}")
            return video_path
        finally:
            if concat_list.exists():
                concat_list.unlink()


# ─── Media Pipeline ────────────────────────────────────────────────────────────

class MediaPipeline:
    """High-level media processing pipeline."""

    def __init__(self):
        self.fetcher = StockAssetFetcher()
        self.clipper = VideoClipper()
        self.overlay = OverlayEngine()
        self.audio = AudioProcessor()
        self.thumbnail = ThumbnailGenerator()
        self.assembler = VideoAssembler()
        self.work_dir = VIDEO_OUTPUT_DIR / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # --- helpers ---------------------------------------------------------
    @staticmethod
    def _search_terms(category: str, title: str) -> List[str]:
        """Build scene phrases for the visuals (generic/atmospheric, never match clips)."""
        if config.video.asset_provider == "ai_image":
            if CONTENT_MODE == "scifi":
                return [
                    "a dark server room with rows of glowing blue lights, cold atmosphere",
                    "an empty smart home at night, screens glowing softly in the dark",
                    "a neon-lit futuristic city skyline at night, rain, cinematic",
                    "a single glowing computer screen in a dim room, eerie",
                    "a humanoid robot silhouette in a dark lab, backlit",
                    "a holographic interface floating in an empty room, blue light",
                    "a self-driving car on an empty highway at night, headlights glowing",
                    "a wall of security monitors glowing in a dark control room",
                ]
            if CONTENT_MODE == "horror":
                return [
                    "a dark empty hallway lit by a single flickering bulb, long shadows",
                    "a moonlit window with a faint silhouette behind the curtain",
                    "an abandoned room with an old chair, dust and fog, dim light",
                    "a foggy street at night, lone streetlamp, no people",
                    "a staircase descending into darkness, eerie atmosphere",
                    "an old mirror reflecting an empty room, cold blue light",
                    "a child's bedroom at night, toys in shadow, unsettling stillness",
                    "a forest at night, bare trees, mist, faint distant light",
                ]
            if CONTENT_MODE == "bible":
                return [
                    "golden sunrise over distant mountains, rays of light through clouds, peaceful",
                    "a calm sea at dawn, warm light on gentle waves, serene and majestic",
                    "light breaking through dark storm clouds, hopeful, cinematic",
                    "an open green field at golden hour, soft warm light, tranquil",
                    "a single lit candle glowing in soft darkness, warm and reverent",
                    "an ancient stone path winding through the holy land at sunrise",
                    "a quiet mountaintop above the clouds bathed in golden light",
                    "sunbeams streaming through a forest canopy onto a peaceful path",
                ]
            # Cinematic, symbolic scenes — no real players (avoids bad faces / likeness).
            return [
                "a packed football stadium at night under blazing floodlights, fans roaring",
                "a lone soccer player silhouette on the pitch, dramatic backlight, fog",
                "extreme close-up of a soccer ball on dewy grass at golden hour",
                "a huge crowd of football fans celebrating, confetti and flares, energy",
                "a glowing golden trophy on a pedestal, spotlight, dark arena",
                "sweeping cinematic aerial of a floodlit football pitch under stormy skies",
                "a goal net rippling as a ball strikes it, dramatic frozen moment",
                "a stadium tunnel with bright light at the end, atmospheric haze",
            ]
        base = ["soccer stadium night", "football on grass close up",
                "stadium crowd cheering", "football pitch aerial",
                "golden trophy lights", "soccer ball slow motion"]
        cat = config.categories.get(category, {})
        kws = list(cat.get("keywords", []))[:2]
        terms = []
        for k in kws:
            terms.append(f"{k} stadium")
        terms += base
        return terms

    def _caption_chunks(self, text: str, max_words: int = 3) -> List[str]:
        """Split narration into short on-screen caption phrases."""
        words = re.findall(r"\S+", text)
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) >= max_words:
                chunks.append(" ".join(cur))
                cur = []
        if cur:
            chunks.append(" ".join(cur))
        return chunks or [text]

    def _load_word_timings(self, voiceover_path: Path):
        """Load real per-word timings written by the voiceover step, if present."""
        wp = voiceover_path.parent / (voiceover_path.stem + ".words.json")
        if not wp.exists():
            return None
        try:
            with open(wp, encoding="utf-8") as f:
                data = json.load(f)
            return [w for w in data if str(w.get("word", "")).strip()]
        except Exception:
            return None

    def _build_ass_timed(self, words: list, out: Path) -> None:
        """Write karaoke captions using REAL per-word timings, so highlights land
        exactly on each spoken word (no drift)."""
        fs = int(config.video.height * 0.042)
        marginv = int(config.video.height * 0.30)

        def t(s):
            cs = int(round(max(s, 0) * 100)); h, cs = divmod(cs, 360000)
            m, cs = divmod(cs, 6000); sec, cs = divmod(cs, 100)
            return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"

        header = (
            "[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {config.video.width}\nPlayResY: {config.video.height}\n"
            "WrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Pop,Arial,{fs},&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,"
            f"-1,0,0,0,100,100,0,0,1,7,3,2,110,110,{marginv},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )

        lines = []
        group = 3  # words per caption phrase
        for i in range(0, len(words), group):
            chunk = words[i:i + group]
            if not chunk:
                continue
            start = float(chunk[0]["start"])
            last = chunk[-1]
            end = float(last["start"]) + float(last.get("dur", 0.3))
            # cap the tail so it doesn't run into the next phrase
            nxt = words[i + group]["start"] if i + group < len(words) else None
            end = (min(end + 0.12, float(nxt)) if nxt is not None else end + 0.12)
            parts = []
            for j, w in enumerate(chunk):
                token = str(w["word"]).replace("\n", " ").strip().upper()
                if not token:
                    continue
                # highlight lasts until the next word begins (absorbs any pause)
                if j < len(chunk) - 1:
                    k_sec = float(chunk[j + 1]["start"]) - float(w["start"])
                else:
                    k_sec = float(w.get("dur", 0.3))
                k = max(int(round(k_sec * 100)), 1)
                parts.append(f"{{\\k{k}}}{token}")
            if not parts:
                continue
            cap = " ".join(parts)
            lines.append(f"Dialogue: 0,{t(start)},{t(end)},Pop,,0,0,0,,{{\\fad(80,80)}}{cap}")
        out.write_text(header + "\n".join(lines) + "\n")

    def _build_ass(self, text: str, total: float, out: Path) -> None:
        """Write styled .ass captions with word-by-word karaoke highlighting.

        Each phrase shows ~3 words; words light up one at a time (white ->
        yellow) in sync with the narration pacing for a modern Shorts feel.
        """
        chunks = self._caption_chunks(text)
        weights = [max(len(c), 1) for c in chunks]
        wsum = sum(weights)
        fs = int(config.video.height * 0.042)
        marginv = int(config.video.height * 0.30)

        def t(s):
            cs = int(round(s * 100)); h, cs = divmod(cs, 360000)
            m, cs = divmod(cs, 6000); sec, cs = divmod(cs, 100)
            return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"

        # Standard V4+ style line (includes SecondaryColour so \k works).
        # Colours are &HAABBGGRR: primary/sung = yellow, secondary/unsung = white.
        header = (
            "[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {config.video.width}\nPlayResY: {config.video.height}\n"
            "WrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Pop,Arial,{fs},&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,"
            f"-1,0,0,0,100,100,0,0,1,7,3,2,110,110,{marginv},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )
        lines, clock = [], 0.0
        for chunk, w in zip(chunks, weights):
            dur = total * (w / wsum)
            start, end = clock, clock + dur
            clock = end
            words = [w2.replace("\n", " ").strip().upper() for w2 in chunk.split() if w2.strip()]
            if not words:
                continue
            # Distribute the phrase duration across its words (centiseconds).
            cs_total = max(int(round(dur * 100)), 1)
            wl = [max(len(x), 1) for x in words]
            wlsum = sum(wl)
            parts, acc = [], 0
            for i, word in enumerate(words):
                if i == len(words) - 1:
                    k = max(cs_total - acc, 1)
                else:
                    k = max(int(round(cs_total * wl[i] / wlsum)), 1)
                    acc += k
                parts.append(f"{{\\k{k}}}{word}")
            cap = " ".join(parts)
            lines.append(f"Dialogue: 0,{t(start)},{t(end)},Pop,,0,0,0,,{{\\fad(80,80)}}{cap}")
        out.write_text(header + "\n".join(lines) + "\n")

    async def _normalize(self, src: Path, seconds: float, out: Path,
                         seg_index: int = 0) -> bool:
        w, h, fps = config.video.width, config.video.height, config.video.fps
        dur = max(seconds, 0.8)
        base = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},fps={fps}")
        if getattr(config.video, "ken_burns", True):
            frames = max(int(round(dur * fps)), 1)
            # Alternate slow zoom-in / zoom-out per segment for visual variety.
            if seg_index % 2 == 0:
                z = f"min(1+0.12*on/{frames},1.12)"        # zoom in
            else:
                z = f"max(1.12-0.12*on/{frames},1.0)"       # zoom out
            kb = (f"zoompan=z='{z}':d=1:"
                  f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}")
            vf = f"{base},{kb},setsar=1,format=yuv420p"
        else:
            vf = f"{base},setsar=1,format=yuv420p"
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src),
               "-t", f"{dur:.3f}", "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)]
        ok, _ = await asyncio.get_event_loop().run_in_executor(None, _run_ffmpeg, cmd, 120)
        return ok and out.exists()

    # --- main pipeline ---------------------------------------------------
    async def process(self, script_data: dict, voiceover_path: Path,
                      category: str, title: str,
                      music_path: Optional[Path] = None,
                      output_name: str = "video") -> Tuple[Optional[Path], Optional[Path]]:
        """Build a compliant narrated short.

        Visuals come from licensed stock / generated backgrounds sequenced to
        the voiceover length, with synced captions and the narration as audio.
        Returns (video_path, thumbnail_path).
        """
        narration = script_data.get("full_text", "") if isinstance(script_data, dict) else str(script_data)

        # 1. How long is the narration? Drives everything else.
        vo_seconds = await self.audio.get_duration(Path(voiceover_path))
        if vo_seconds <= 0:
            vo_seconds = float(config.video.target_duration_seconds)

        # 2. Decide how many background segments we need.
        seg_len = config.video.seconds_per_clip
        n_segments = max(1, min(12, int(round(vo_seconds / seg_len + 0.5))))
        per = vo_seconds / n_segments
        # Prefer story-matched scene prompts (AI-image mode); else generic scenes.
        scenes = script_data.get("scenes") if isinstance(script_data, dict) else None
        if scenes:
            terms = scenes
            logger.info("Using story-matched scene prompts for visuals")
        else:
            terms = self._search_terms(category, title)

        # 3. Fetch + normalize each segment (licensed stock or generated).
        norm_clips: List[Path] = []
        for i in range(n_segments):
            query = terms[i % len(terms)]
            raw = await self.fetcher.fetch(query, per + 0.5, f"{output_name}_seg{i:02d}")
            if not raw:
                continue
            norm = self.work_dir / f"{output_name}_norm{i:02d}.mp4"
            if await self._normalize(raw, per, norm, seg_index=i):
                norm_clips.append(norm)
        if not norm_clips:
            logger.error("No background assets could be produced")
            return None, None

        # 4. Concatenate into one silent video track.
        concat_list = self.work_dir / f"{output_name}_list.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in norm_clips))
        track = self.work_dir / f"{output_name}_track.mp4"
        ok, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg,
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(track)], 180)
        if not ok:
            logger.error("Failed to concat background track")
            return None, None

        # 5. Burn synced captions.
        ass = self.work_dir / f"{output_name}.ass"
        word_timings = self._load_word_timings(Path(voiceover_path))
        if word_timings:
            logger.info(f"Using real word timings for captions ({len(word_timings)} words)")
            self._build_ass_timed(word_timings, ass)
        else:
            self._build_ass(narration, vo_seconds, ass)
        captioned = self.work_dir / f"{output_name}_captioned.mp4"
        ass_arg = str(ass).replace("\\", "/").replace(":", "\\:")
        ok, _ = await asyncio.get_event_loop().run_in_executor(
            None, _run_ffmpeg,
            ["ffmpeg", "-y", "-i", str(track), "-vf", f"subtitles='{ass_arg}'",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-an", str(captioned)], 240)
        if not ok:
            captioned = track  # fall back to uncaptioned track

        # 6. Lay the voiceover (+ optional ducked music) over the visuals.
        final = await self.audio.mix_audio(
            captioned, Path(voiceover_path), music_path, output_name)
        if not final:
            logger.error("Audio mux failed")
            return None, None

        # 7. Thumbnail.
        thumb = await self.thumbnail.generate(title, category, output_name=output_name)

        # 8. Save a copy to output/videos/ with a human-readable filename.
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60] or output_name
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        readable = VIDEO_OUTPUT_DIR / f"{stamp}_{slug}.mp4"
        try:
            shutil.copy(str(final), str(readable))
            final = readable
        except Exception as e:
            logger.warning(f"Could not copy final video to videos dir: {e}")

        logger.info(f"Compliant video assembled: {final}")
        return final, thumb
