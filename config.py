"""
Configuration module for the Autonomous YouTube Soccer Content Agent.
All settings, API keys, channel config, and content categories.
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# ─── Base Paths ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
VIDEO_OUTPUT_DIR = OUTPUT_DIR / "videos"
THUMBNAIL_OUTPUT_DIR = OUTPUT_DIR / "thumbnails"
AUDIO_OUTPUT_DIR = OUTPUT_DIR / "audio"
SCRIPTS_OUTPUT_DIR = OUTPUT_DIR / "scripts"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"

for d in [DATA_DIR, VIDEO_OUTPUT_DIR, THUMBNAIL_OUTPUT_DIR,
          AUDIO_OUTPUT_DIR, SCRIPTS_OUTPUT_DIR, LOG_DIR, STATE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── API Keys (loaded from environment / .env) ────────────────────────────────

def _get_env(key: str, default: str = "") -> str:
    """Get a config value. Precedence: real env var > .env file > default.

    Previously the .env file was only consulted when the value was empty,
    so keys with a non-empty default (e.g. LOCAL_MODEL_ENDPOINT) silently
    ignored the .env. Now the .env is always honored.
    """
    # 1. A real (non-empty) environment variable always wins.
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    # 2. Otherwise look in the .env file.
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith(key + "="):
                        val = line.split("=", 1)[1]
                        # Drop an inline comment that follows whitespace
                        # (e.g. "flux   # or: turbo") so it isn't read as value.
                        for sep in (" #", "\t#"):
                            if sep in val:
                                val = val.split(sep, 1)[0]
                        return val.strip().strip("\"'")
        except Exception:
            pass
    # 3. Fall back to the default.
    return default


# Content domain: "soccer" (news-driven) or "horror"/"scifi" (original stories).
CONTENT_MODE = _get_env("CONTENT_MODE", "soccer").strip().lower()

# The active niche: ALL mode-specific prompts/styles/tags/behavior live in
# niches.py — the rest of the codebase consumes this object instead of
# branching on CONTENT_MODE.
from niches import get_niche, NICHES  # noqa: E402 (needs _get_env above)
NICHE = get_niche(CONTENT_MODE)

# When true, story modes generate brand-new AI premises every run (infinite,
# never-repeating) instead of drawing from the built-in fixed pool.
AUTO_PREMISES = _get_env("AUTO_PREMISES", "false").strip().lower() in ("1", "true", "yes", "on")


def _default_image_style() -> str:
    """Default AI-image art style, from the active niche."""
    return NICHE.image_style


def _default_tags() -> list:
    """Default upload tags, from the active niche.

    These previously were soccer tags regardless of mode, so bible/horror
    videos were uploaded tagged "fifa, world cup..." — which tells YouTube's
    classifier exactly the wrong thing about the video.
    """
    return list(NICHE.default_tags)


def _default_category_id() -> str:
    """YouTube categoryId from the active niche (17=Sports, 24=Entertainment,
    22=People & Blogs). Previously every mode uploaded as Sports."""
    return NICHE.youtube_category_id


# ─── API Configuration ────────────────────────────────────────────────────────

@dataclass
class APIConfig:
    youtube_api_key: str = field(default_factory=lambda: _get_env("YOUTUBE_API_KEY"))
    youtube_client_id: str = field(default_factory=lambda: _get_env("YOUTUBE_CLIENT_ID"))
    youtube_client_secret: str = field(default_factory=lambda: _get_env("YOUTUBE_CLIENT_SECRET"))
    youtube_refresh_token: str = field(default_factory=lambda: _get_env("YOUTUBE_REFRESH_TOKEN"))
    newsapi_key: str = field(default_factory=lambda: _get_env("NEWSAPI_KEY"))
    pexels_api_key: str = field(default_factory=lambda: _get_env("PEXELS_API_KEY"))
    reddit_client_id: str = field(default_factory=lambda: _get_env("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _get_env("REDDIT_CLIENT_SECRET"))
    reddit_user_agent: str = field(default_factory=lambda: _get_env("REDDIT_USER_AGENT", "youtube-soccer-agent/1.0"))
    openai_api_key: str = field(default_factory=lambda: _get_env("OPENAI_API_KEY"))
    anthropic_api_key: str = field(default_factory=lambda: _get_env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _get_env("ANTHROPIC_MODEL", "claude-fable-5"))
    # xAI Grok (OpenAI-compatible API). Accepts XAI_API_KEY or GROK_API_KEY.
    xai_api_key: str = field(default_factory=lambda: _get_env("XAI_API_KEY") or _get_env("GROK_API_KEY"))
    grok_model: str = field(default_factory=lambda: _get_env("GROK_MODEL", "grok-4.3"))
    # Local model endpoint (Ollama, etc.)
    local_model_endpoint: str = field(default_factory=lambda: _get_env("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/api/generate"))
    local_model_name: str = field(default_factory=lambda: _get_env("LOCAL_MODEL_NAME", "llama3"))
    # Ollama Cloud (or any auth-protected Ollama endpoint). If set, sent as a
    # Bearer token. Leave empty for a plain local Ollama instance.
    ollama_api_key: str = field(default_factory=lambda: _get_env("OLLAMA_API_KEY"))
    # Pollinations (AI-image visuals). Free without a key; optional key improves reliability.
    pollinations_api_key: str = field(default_factory=lambda: _get_env("POLLINATIONS_API_KEY"))


# ─── Content Categories ──────────────────────────────────────────────────────

CONTENT_CATEGORIES = {
    "world_cup_moments": {
        "name": "World Cup Moments",
        "description": "Iconic moments from World Cup history",
        "keywords": ["world cup", "fifa world cup", "world cup moment", "world cup history"],
        "script_format": "legendary",
        "priority": 10,
    },
    "legendary_matches": {
        "name": "Legendary Matches",
        "description": "Classic matches that defined football history",
        "keywords": ["legendary match", "classic match", "greatest game", "historic match"],
        "script_format": "legendary",
        "priority": 9,
    },
    "iconic_goals": {
        "name": "Iconic Goals",
        "description": "The most memorable goals ever scored",
        "keywords": ["iconic goal", "greatest goal", "best goal", "wonder goal", "stunning goal"],
        "script_format": "did_you_know",
        "priority": 8,
    },
    "player_stories": {
        "name": "Player Stories",
        "description": "Fascinating stories about legendary players",
        "keywords": ["player story", "legend", "football legend", "soccer star", "career"],
        "script_format": "legendary",
        "priority": 7,
    },
    "controversial_moments": {
        "name": "Controversial Moments",
        "description": "The most controversial moments in football",
        "keywords": ["controversy", "scandal", "controversial", "drama"],
        "script_format": "what_happened",
        "priority": 6,
    },
    "transfers": {
        "name": "Transfers",
        "description": "Biggest transfers and transfer sagas",
        "keywords": ["transfer", "signing", "moved to", "joined", "transfer fee"],
        "script_format": "did_you_know",
        "priority": 5,
    },
    "records": {
        "name": "Records",
        "description": "Football records and statistics",
        "keywords": ["record", "most goals", "most appearances", "hat-trick", "milestone"],
        "script_format": "countdown",
        "priority": 8,
    },
    "rivalries": {
        "name": "Rivalries",
        "description": "The greatest rivalries in football",
        "keywords": ["rivalry", "derby", "vs", "clasico", "el clasico"],
        "script_format": "versus",
        "priority": 7,
    },
}

# ─── Video Settings ───────────────────────────────────────────────────────────

@dataclass
class VideoSettings:
    # Vertical 9:16 format for Shorts/Reels/TikTok
    width: int = 1080
    height: int = 1920
    fps: int = 30
    max_duration_seconds: int = 60
    min_duration_seconds: int = 30
    target_duration_seconds: int = 50
    codec: str = "libx264"
    audio_codec: str = "aac"
    bitrate: str = "8M"
    audio_bitrate: str = "192k"
    preset: str = "medium"
    # Overlay defaults
    font_size_title: int = 48
    font_size_body: int = 36
    font_size_lower_third: int = 28
    font_color: str = "white"
    font_outline_color: str = "black"
    font_outline_width: int = 2
    # Ken Burns
    ken_burns_zoom: float = 1.15
    ken_burns_duration: float = 4.0
    # Speed ramping
    slow_motion_speed: float = 0.5
    fast_forward_speed: float = 1.5
    # Audio
    background_music_volume: float = 0.15
    voiceover_volume: float = 1.0
    ducking_release: float = 0.5  # seconds for audio ducking release
    # ─── Asset sourcing (COMPLIANCE) ──────────────────────────────────
    # The agent never downloads/reuploads broadcast footage. Visuals come
    # from licensed stock (Pexels), AI-generated images, or are generated.
    # ASSET_PROVIDER: pexels | ai_image | generated
    asset_provider: str = field(default_factory=lambda: _get_env("ASSET_PROVIDER", "pexels"))
    # AI "animated story" images (Pollinations, free, no key by default).
    ai_image_model: str = field(default_factory=lambda: _get_env("AI_IMAGE_MODEL", "flux"))
    ai_image_style: str = field(default_factory=lambda: _get_env(
        "AI_IMAGE_STYLE", _default_image_style()))
    require_licensed_assets: bool = True
    add_attribution_to_description: bool = True
    # Length of each background segment. Longer segments = fewer image
    # requests per video (~40% fewer at 8s vs 5s), which is the cheapest
    # way to stay under Pollinations rate limits; Ken Burns carries the
    # extra seconds visually. Override with SECONDS_PER_CLIP in .env.
    seconds_per_clip: float = field(default_factory=lambda: float(
        _get_env("SECONDS_PER_CLIP", "8") or 8))
    ken_burns: bool = True                # slow zoom motion on each segment


# ─── Upload / Schedule Settings ───────────────────────────────────────────────

@dataclass
class UploadSettings:
    channel_name: str = field(default_factory=lambda: _get_env("CHANNEL_NAME", "AI Nightmares"))
    channel_description: str = (
        "The best soccer content on YouTube! "
        "World Cup moments, legendary matches, iconic goals, and player stories. "
        "New videos every day."
    )
    default_tags: List[str] = field(default_factory=_default_tags)
    # YouTube category for uploads (overridable via YOUTUBE_CATEGORY_ID).
    youtube_category_id: str = field(default_factory=lambda: (
        _get_env("YOUTUBE_CATEGORY_ID", "") or _default_category_id()))
    shorts_tag: str = "#Shorts"
    upload_frequency_per_day: int = 2
    optimal_post_times_utc: List[str] = field(default_factory=lambda: [
        "12:00", "16:00", "20:00"
    ])
    language: str = "en"
    secondary_languages: List[str] = field(default_factory=lambda: ["es", "pt"])
    # Comment auto-reply templates
    comment_replies: dict = field(default_factory=lambda: {
        "positive": [
            "Thanks for watching! 🙌",
            "Glad you enjoyed it! More coming soon.",
            "Appreciate the support! ⚽",
        ],
        "question": [
            "Great question! We'll cover that in an upcoming video.",
            "Stay tuned — we have a video on that coming soon!",
        ],
        "negative": [
            "Thanks for the feedback — we're always improving!",
            "Appreciate you sharing your thoughts.",
        ],
        "default": [
            "Thanks for watching! Subscribe for more soccer content ⚽",
        ],
    })


# ─── Scheduler Settings ───────────────────────────────────────────────────────

@dataclass
class SchedulerSettings:
    check_interval_minutes: int = field(
        default_factory=lambda: int(_get_env("CHECK_INTERVAL_MINUTES", "60") or 60))
    max_daily_uploads: int = field(
        default_factory=lambda: int(_get_env("MAX_DAILY_UPLOADS", "2") or 2))
    content_queue_size: int = 10
    retry_max_attempts: int = 3
    retry_delay_seconds: int = 300
    dry_run: bool = False  # Set to True to test without publishing


# ─── Master Config ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    api: APIConfig = field(default_factory=APIConfig)
    video: VideoSettings = field(default_factory=VideoSettings)
    upload: UploadSettings = field(default_factory=UploadSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    categories: dict = field(default_factory=lambda: CONTENT_CATEGORIES)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        return cls(
            api=APIConfig(**d.get("api", {})),
            video=VideoSettings(**d.get("video", {})),
            upload=UploadSettings(**d.get("upload", {})),
            scheduler=SchedulerSettings(**d.get("scheduler", {})),
            categories=d.get("categories", CONTENT_CATEGORIES),
        )

    def save(self, path: Optional[Path] = None) -> None:
        path = path or (BASE_DIR / "config_saved.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or (BASE_DIR / "config_saved.json")
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        return cls()


# ─── Singleton ────────────────────────────────────────────────────────────────

config = Config()
