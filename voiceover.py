"""
Text-to-Speech Narration Module for the Autonomous YouTube Soccer Content Agent.
Uses edge-tts for high-quality free TTS (rate/volume prosody control).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from config import config, AUDIO_OUTPUT_DIR, _get_env

logger = logging.getLogger(__name__)

# ─── Voice Configuration ──────────────────────────────────────────────────────

VOICE_CONFIG = {
    "en": {
        "male_deep": "en-US-GuyNeural",
        "male_energetic": "en-GB-RyanNeural",
        "male_authoritative": "en-US-DavisNeural",
        "female_energetic": "en-GB-SoniaNeural",
        "female_warm": "en-US-JennyNeural",
        "female_british": "en-GB-LibbyNeural",
        "default": "en-GB-RyanNeural",
    },
    "es": {
        "male": "es-MX-JorgeNeural",
        "female": "es-MX-DaliaNeural",
        "default": "es-MX-JorgeNeural",
    },
    "pt": {
        "male": "pt-BR-AntonioNeural",
        "female": "pt-BR-FranciscaNeural",
        "default": "pt-BR-AntonioNeural",
    },
}


# ─── SSML Builder ──────────────────────────────────────────────────────────────

# NOTE: edge-tts removed custom SSML support in v5.0.0 (Microsoft blocks any
# SSML it can't generate itself). Prosody is controlled via --rate / --volume
# flags instead. The old SSMLBuilder was dead code and has been removed.


# ─── Edge TTS Engine ──────────────────────────────────────────────────────────

class EdgeTTS:
    """Text-to-Speech using edge-tts (free, high quality)."""

    def __init__(self):
        self.output_dir = AUDIO_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._check_available()

    def _check_available(self) -> bool:
        """Check if the edge-tts package is importable."""
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            logger.warning("edge-tts not installed. Install with: pip install edge-tts")
            return False

    @staticmethod
    def _chars_to_words(alignment: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert ElevenLabs character-level alignment into per-word timings
        (same shape media.py expects: {word, start, dur})."""
        chars = alignment.get("characters", [])
        starts = alignment.get("character_start_times_seconds", [])
        ends = alignment.get("character_end_times_seconds", [])
        words, cur, cur_start, prev_end = [], "", None, 0.0
        for ch, st, en in zip(chars, starts, ends):
            if ch.isspace():
                if cur:
                    words.append({"word": cur, "start": cur_start,
                                  "dur": max(prev_end - cur_start, 0.05)})
                    cur, cur_start = "", None
            else:
                if not cur:
                    cur_start = st
                cur += ch
                prev_end = en
        if cur:
            words.append({"word": cur, "start": cur_start,
                          "dur": max(prev_end - cur_start, 0.05)})
        return words

    async def _synthesize_elevenlabs(self, text: str, api_key: str, voice_id: str,
                                     output_path: Path) -> Optional[Path]:
        """Generate narration with ElevenLabs, with character-level timestamps so
        captions stay perfectly in sync. Returns the audio path, or None on failure."""
        import aiohttp
        import base64

        model = _get_env("ELEVENLABS_MODEL", "eleven_multilingual_v2")
        try:
            stability = float(_get_env("ELEVENLABS_STABILITY", "0.5"))
            similarity = float(_get_env("ELEVENLABS_SIMILARITY", "0.75"))
        except ValueError:
            stability, similarity = 0.5, 0.75

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": model,
            "voice_settings": {"stability": stability, "similarity_boost": similarity},
        }
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        logger.error(f"ElevenLabs HTTP {resp.status}: {body}")
                        return None
                    data = await resp.json()

            audio_b64 = data.get("audio_base64")
            if not audio_b64:
                logger.error("ElevenLabs returned no audio")
                return None
            with open(output_path, "wb") as f:
                f.write(base64.b64decode(audio_b64))

            alignment = data.get("alignment") or data.get("normalized_alignment")
            if alignment:
                words = self._chars_to_words(alignment)
                if words:
                    wp = output_path.parent / (output_path.stem + ".words.json")
                    try:
                        with open(wp, "w", encoding="utf-8") as wf:
                            json.dump(words, wf)
                    except OSError:
                        pass

            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"Voiceover generated (ElevenLabs, {model}): {output_path}")
                return output_path
            return None
        except Exception as e:
            logger.error(f"ElevenLabs error: {e}")
            return None

    async def synthesize(self, text: str, voice: str = "en-GB-RyanNeural",
                         rate: str = "+10%", volume: str = "+0%",
                         output_name: str = "voiceover") -> Optional[Path]:
        """
        Synthesize speech from text using the edge-tts Python module.

        Uses the library API directly (not the `edge-tts` command-line program),
        so it works regardless of whether the console script is on PATH. Prosody
        is set via the supported rate / volume params (custom SSML is no longer
        supported by edge-tts). Returns the path to the audio file.
        """
        output_path = self.output_dir / f"{output_name}.mp3"

        # ── ElevenLabs (premium): used when an API key + voice ID are configured.
        # Falls back to edge-tts if it fails or isn't set.
        el_key = _get_env("ELEVENLABS_API_KEY", "")
        el_voice = _get_env("ELEVENLABS_VOICE_ID", "")
        if el_key:
            if not el_voice:
                logger.warning("ELEVENLABS_API_KEY set but ELEVENLABS_VOICE_ID is missing "
                               "— using edge-tts. Add a voice ID to use ElevenLabs.")
            else:
                result = await self._synthesize_elevenlabs(text, el_key, el_voice, output_path)
                if result:
                    return result
                logger.warning("ElevenLabs synthesis failed — falling back to edge-tts")

        try:
            import edge_tts
        except ImportError:
            logger.error("edge-tts not installed. Run: pip install edge-tts")
            return None
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
            # Stream so we can capture real per-word timings (WordBoundary events)
            # and write exact, in-sync captions instead of estimating.
            words = []
            with open(output_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        # offset / duration are in 100-nanosecond units
                        words.append({
                            "word": chunk.get("text", ""),
                            "start": chunk.get("offset", 0) / 1e7,
                            "dur": chunk.get("duration", 0) / 1e7,
                        })
            if output_path.exists() and output_path.stat().st_size > 0:
                if words:
                    words_path = output_path.parent / (output_path.stem + ".words.json")
                    try:
                        with open(words_path, "w", encoding="utf-8") as wf:
                            json.dump(words, wf)
                    except OSError:
                        pass
                logger.info(f"Voiceover generated: {output_path}")
                return output_path
            logger.error("edge-tts produced no audio (empty file)")
            return None
        except Exception as e:
            logger.error(f"edge-tts error: {e}")
            return None

    async def synthesize_hook(self, text: str, voice: str = "en-GB-RyanNeural",
                               output_name: str = "hook") -> Optional[Path]:
        """Synthesize just the hook with extra energy (faster, louder)."""
        return await self.synthesize(
            text, voice=voice, rate="+18%", volume="+15%", output_name=output_name
        )

    def get_voice_for_language(self, language: str, gender: str = "default") -> str:
        """Get the appropriate voice for a language."""
        lang_config = VOICE_CONFIG.get(language, VOICE_CONFIG["en"])
        return lang_config.get(gender, lang_config["default"])


# ─── Background Music Manager ─────────────────────────────────────────────────

class BackgroundMusic:
    """Manages royalty-free background music tracks."""

    def __init__(self):
        self.music_dir = AUDIO_OUTPUT_DIR / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default_tracks()

    def _ensure_default_tracks(self):
        """Create placeholder for music tracks. Users should add their own."""
        readme = self.music_dir / "README.txt"
        if not readme.exists():
            with open(readme, "w") as f:
                f.write(
                    "Add royalty-free background music tracks to this directory.\n"
                    "Supported formats: mp3, wav, m4a, ogg\n\n"
                    "Recommended sources:\n"
                    "- YouTube Audio Library\n"
                    "- Uppbeat (https://uppbeat.io)\n"
                    "- Pixabay Music (https://pixabay.com/music)\n"
                    "- Free Music Archive (https://freemusicarchive.org)\n\n"
                    "Track naming: track_01.mp3, track_02.mp3, etc.\n"
                )

    def get_random_track(self) -> Optional[Path]:
        """Get a random background music track."""
        tracks = list(self.music_dir.glob("*.*"))
        tracks = [t for t in tracks if t.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")]
        if not tracks:
            return None
        import random
        return random.choice(tracks)

    def get_track_by_mood(self, mood: str = "epic") -> Optional[Path]:
        """Get a track matching a mood (epic, dramatic, emotional, upbeat)."""
        tracks = list(self.music_dir.glob(f"*{mood}*.*"))
        tracks += list(self.music_dir.glob(f"*{mood}*.mp3"))
        if not tracks:
            return self.get_random_track()
        return tracks[0]


# ─── Voiceover Pipeline ────────────────────────────────────────────────────────

class VoiceoverPipeline:
    """High-level voiceover generation pipeline."""

    def __init__(self):
        self.tts = EdgeTTS()
        self.music = BackgroundMusic()

    async def generate(self, script_data: dict,
                       language: str = "en",
                       output_name: str = "voiceover") -> Optional[Path]:
        """
        Generate complete voiceover with hook, body, and optional music.
        Returns path to the final voiceover audio file.
        """
        full_text = script_data.get("full_text", "")
        hook = script_data.get("hook", "")
        sections = script_data.get("sections", {})

        if not full_text:
            logger.error("No script text to synthesize")
            return None

        # Voice + delivery are configurable from .env:
        #   TTS_VOICE  (e.g. en-US-GuyNeural)   TTS_RATE (e.g. -8%)   TTS_VOLUME (e.g. +0%)
        voice = _get_env("TTS_VOICE", "") or self.tts.get_voice_for_language(language)
        rate = _get_env("TTS_RATE", "+10%")
        volume = _get_env("TTS_VOLUME", "+0%")

        # Synthesize full voiceover
        voiceover_path = await self.tts.synthesize(
            full_text, voice=voice, rate=rate, volume=volume, output_name=output_name
        )

        if not voiceover_path:
            logger.error("Failed to generate voiceover")
            return None

        return voiceover_path

    async def generate_with_music(self, script_data: dict,
                                   language: str = "en",
                                   output_name: str = "voiceover_mixed") -> Optional[Path]:
        """Generate voiceover and mix with background music."""
        voiceover = await self.generate(script_data, language, output_name)
        if not voiceover:
            return None

        # Get background music
        music_track = self.music.get_random_track()
        if not music_track:
            logger.info("No background music available, using voiceover only")
            return voiceover

        # Mix voiceover with music using ffmpeg
        output_path = AUDIO_OUTPUT_DIR / f"{output_name}_with_music.mp3"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(voiceover),
            "-i", str(music_track),
            "-filter_complex",
            (
                f"[1:a]volume={config.video.background_music_volume}[music];"
                f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[out]"
            ),
            "-map", "[out]",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(output_path),
        ]

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            )
            if result.returncode == 0 and output_path.exists():
                logger.info(f"Voiceover with music: {output_path}")
                return output_path
            return voiceover
        except Exception as e:
            logger.error(f"Music mixing error: {e}")
            return voiceover
