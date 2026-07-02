"""
YouTube Publisher Module for the Autonomous YouTube Soccer Content Agent.
Handles upload, scheduling, analytics, and comment moderation via YouTube Data API v3.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from email.utils import parsedate_to_datetime

import aiohttp

from config import config, STATE_DIR, CONTENT_MODE, _get_env
from storage import atomic_write_json, load_json

logger = logging.getLogger(__name__)


# ─── Upload Quota Circuit Breaker ─────────────────────────────────────────────

class QuotaGuard:
    """Persistent circuit breaker for YouTube quota errors.

    When an upload fails with a quota/limit reason, uploads are paused until
    the quota window resets (midnight Pacific). The scheduler checks this
    BEFORE generating content, so no AI/TTS/image work is wasted on videos
    that can't be published — and retries don't burn more quota.
    """

    COOLDOWN_FILE = STATE_DIR / "upload_cooldown.json"
    QUOTA_REASONS = {
        "quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
        "userRateLimitExceeded", "rateLimitExceeded",
    }

    @classmethod
    def paused_until(cls) -> Optional[datetime]:
        """UTC datetime uploads are paused until, or None if not paused."""
        data = load_json(cls.COOLDOWN_FILE, {})
        try:
            until = datetime.fromisoformat(data.get("until", ""))
        except (TypeError, ValueError):
            return None
        if datetime.now(timezone.utc) < until:
            return until
        return None

    @classmethod
    def trip(cls, reason: str) -> None:
        """Pause uploads until the next YouTube quota reset (midnight Pacific)."""
        try:
            from zoneinfo import ZoneInfo
            pacific = ZoneInfo("America/Los_Angeles")
        except Exception:  # no tzdata available: fixed PST approximation
            pacific = timezone(timedelta(hours=-8))
        now_pt = datetime.now(pacific)
        # 00:05 the next day — a small buffer past the reset.
        reset = (now_pt + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0)
        until = reset.astimezone(timezone.utc)
        try:
            atomic_write_json(cls.COOLDOWN_FILE, {
                "reason": reason,
                "tripped_at": datetime.now(timezone.utc).isoformat(),
                "until": until.isoformat(),
            })
        except OSError as e:
            logger.error(f"Could not persist quota cooldown: {e}")
        logger.error(
            f"YouTube quota exhausted ({reason}). Uploads paused until "
            f"{until.isoformat()} (quota resets at midnight Pacific). "
            f"Finished videos wait in the pending-upload queue.")

    @classmethod
    def classify(cls, body: str) -> Optional[str]:
        """Return the quota reason if an API error body is quota-related."""
        try:
            err = json.loads(body).get("error", {})
        except (ValueError, AttributeError):
            return None
        if not isinstance(err, dict):
            return None
        reasons = set()
        for e in (err.get("errors") or []):
            if isinstance(e, dict) and e.get("reason"):
                reasons.add(e["reason"])
        for d in (err.get("details") or []):
            if isinstance(d, dict) and d.get("reason"):
                reasons.add(d["reason"])
        hit = reasons & cls.QUOTA_REASONS
        return next(iter(hit)) if hit else None


# ─── OAuth Token Manager ──────────────────────────────────────────────────────

class YouTubeOAuth:
    """Manages OAuth2 tokens for YouTube Data API v3."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    TOKEN_FILE = STATE_DIR / "youtube_token.json"

    def __init__(self):
        self.client_id = config.api.youtube_client_id
        self.client_secret = config.api.youtube_client_secret
        self.refresh_token = config.api.youtube_refresh_token
        self.access_token = None
        self.token_expires = 0
        # True once the refresh token is known-dead (invalid_grant) — a fatal
        # state that needs re-authorization, unlike a transient network error.
        self.auth_dead = False
        self._load_token()

    def _write_health(self, state: str, detail: str = ""):
        """Persist channel auth health so the Channel Control dashboard can
        surface it (state/channel_health.json)."""
        try:
            atomic_write_json(STATE_DIR / "channel_health.json",
                              {"auth": state, "detail": detail, "at": time.time()})
        except OSError:
            pass

    async def verify(self) -> bool:
        """Pre-flight credential check. Cheap: one token refresh, no upload.

        Returns True if we can obtain an access token; records the result in
        channel_health.json either way. On invalid_grant, auth_dead is set so
        callers can distinguish 'fix your token' from 'retry later'.
        """
        self.auth_dead = False
        token = await self.get_access_token()
        if token:
            self._write_health("ok")
            return True
        return False

    def _load_token(self):
        """Load cached token from disk."""
        data = load_json(self.TOKEN_FILE, {})
        self.access_token = data.get("access_token")
        self.token_expires = data.get("expires_at", 0)

    def _save_token(self, access_token: str, expires_in: int):
        """Save token to disk."""
        data = {
            "access_token": access_token,
            "expires_at": time.time() + expires_in - 60,  # 1 min buffer
        }
        try:
            atomic_write_json(self.TOKEN_FILE, data)
            os.chmod(self.TOKEN_FILE, 0o600)  # owner-only (no-op on DrvFs)
        except OSError as e:
            logger.error(f"Failed to save token: {e}")

    async def get_access_token(self) -> Optional[str]:
        """Get a valid access token, refreshing if needed."""
        if self.access_token and time.time() < self.token_expires:
            return self.access_token

        if not self.refresh_token:
            logger.error("No YouTube refresh token configured")
            return None

        try:
            async with aiohttp.ClientSession() as session:
                data = {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                }
                async with session.post(self.TOKEN_URL, data=data) as resp:
                    if resp.status == 200:
                        j = await resp.json()
                        self.access_token = j["access_token"]
                        expires_in = j.get("expires_in", 3600)
                        self._save_token(self.access_token, expires_in)
                        return self.access_token
                    else:
                        error_text = await resp.text()
                        if "invalid_grant" in error_text:
                            self.auth_dead = True
                            self._write_health(
                                "auth_dead",
                                "Refresh token expired or revoked. Run "
                                "python3 setup_oauth.py to re-authorize. If this "
                                "keeps happening, publish your OAuth app to "
                                "Production (Testing-mode tokens expire after 7 days).")
                            logger.error(
                                "YouTube refresh token is expired or revoked "
                                "(invalid_grant). Most common cause: the OAuth "
                                "consent screen is still in 'Testing' mode, where "
                                "Google expires refresh tokens after 7 days.\n"
                                "  Fix: 1) run  python3 setup_oauth.py  to re-authorize\n"
                                "       2) set the app to 'In production' in the Google "
                                "Cloud console so tokens stop expiring.")
                        else:
                            logger.error(f"Token refresh failed: {resp.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return None


# ─── YouTube Uploader ─────────────────────────────────────────────────────────

class YouTubeUploader:
    """Uploads videos to YouTube using the Data API v3."""

    UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
    API_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self):
        self.oauth = YouTubeOAuth()
        self.upload_history_file = STATE_DIR / "upload_history.json"
        self.upload_history = self._load_history()

    def _load_history(self) -> List[dict]:
        """Load upload history from disk."""
        return load_json(self.upload_history_file, [])

    def _save_history(self):
        """Save upload history to disk."""
        try:
            atomic_write_json(self.upload_history_file, self.upload_history[-100:])
        except OSError as e:
            logger.error(f"Failed to save upload history: {e}")

    async def upload(self, video_path: Path, title: str, description: str,
                     tags: List[str], category_id: str = "17",
                     privacy_status: str = "public",
                     thumbnail_path: Optional[Path] = None,
                     publish_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Upload a video to YouTube.
        
        Args:
            video_path: Path to the video file
            title: Video title
            description: Video description
            tags: List of tags
            category_id: YouTube category ID (17 = Sports)
            privacy_status: public, unlisted, or private
            thumbnail_path: Optional thumbnail image
            publish_at: ISO datetime for scheduled publishing
        
        Returns:
            Dict with video metadata on success, None on failure
        """
        # Dry run check
        if config.scheduler.dry_run:
            logger.info(f"DRY RUN: Would upload '{title}' to YouTube")
            return {
                "id": "dry_run_video_id",
                "title": title,
                "status": "dry_run",
            }

        # Circuit breaker: don't burn more quota while paused.
        paused = QuotaGuard.paused_until()
        if paused:
            logger.warning(f"Upload skipped: quota-paused until {paused.isoformat()}")
            return None

        token = await self.oauth.get_access_token()
        if not token:
            logger.error("Cannot upload: no valid YouTube token")
            return None

        # 1. Upload video metadata
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        if publish_at:
            body["status"]["publishAt"] = publish_at

        # Upload in chunks
        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Start resumable upload
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/*",
                }

                upload_url = f"{self.UPLOAD_URL}?uploadType=resumable&part=snippet,status"
                async with session.post(
                    upload_url, json=body, headers=headers
                ) as resp:
                    if resp.status not in (200, 201):
                        error_text = await resp.text()
                        reason = QuotaGuard.classify(error_text)
                        if reason:
                            QuotaGuard.trip(reason)  # logs + persists cooldown
                        else:
                            logger.error(f"Upload initiation failed: {resp.status} - {error_text}")
                        return None

                    # Get upload URL from Location header
                    upload_location = resp.headers.get("Location", "")
                    if not upload_location:
                        logger.error("No upload location in response")
                        return None

                # Step 2: Upload the actual video file
                video_size = video_path.stat().st_size
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/*",
                    "Content-Length": str(video_size),
                }

                with open(video_path, "rb") as f:
                    video_data = f.read()

                async with session.put(
                    upload_location, data=video_data, headers=headers
                ) as resp:
                    if resp.status not in (200, 201):
                        error_text = await resp.text()
                        reason = QuotaGuard.classify(error_text)
                        if reason:
                            QuotaGuard.trip(reason)  # logs + persists cooldown
                        else:
                            logger.error(f"Video upload failed: {resp.status} - {error_text}")
                        return None

                    result = await resp.json()
                    video_id = result.get("id", "")

                if not video_id:
                    logger.error("No video ID in upload response")
                    return None

                logger.info(f"Video uploaded successfully! ID: {video_id}")

                # Step 3: Thumbnail — OFF by default. Shorts don't display
                # custom thumbnails in the Shorts feed, the call costs quota,
                # and unverified channels get 4xx errors from it.
                # Set UPLOAD_THUMBNAILS=true to enable (e.g. for long-form).
                if thumbnail_path and thumbnail_path.exists():
                    if _get_env("UPLOAD_THUMBNAILS", "false").strip().lower() in ("1", "true", "yes", "on"):
                        await self._upload_thumbnail(session, token, video_id, thumbnail_path)
                    else:
                        logger.info("Thumbnail not uploaded (UPLOAD_THUMBNAILS=false; "
                                    "Shorts don't show custom thumbnails)")

                # Record in history
                record = {
                    "video_id": video_id,
                    "title": title,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "privacy_status": privacy_status,
                    "publish_at": publish_at,
                }
                self.upload_history.append(record)
                self._save_history()

                return result

        except Exception as e:
            logger.error(f"Upload error: {e}")
            return None

    async def _upload_thumbnail(self, session: aiohttp.ClientSession,
                                 token: str, video_id: str,
                                 thumbnail_path: Path) -> bool:
        """Upload a thumbnail for a video."""
        url = f"{self.API_URL}/thumbnails/set?videoId={video_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg",
        }

        try:
            with open(thumbnail_path, "rb") as f:
                data = f.read()
            async with session.post(url, data=data, headers=headers) as resp:
                if resp.status == 200:
                    logger.info(f"Thumbnail uploaded for video {video_id}")
                    return True
                else:
                    logger.warning(f"Thumbnail upload failed: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Thumbnail upload error: {e}")
            return False

    async def update_video(self, video_id: str, title: Optional[str] = None,
                            description: Optional[str] = None,
                            tags: Optional[List[str]] = None) -> bool:
        """Update video metadata."""
        token = await self.oauth.get_access_token()
        if not token:
            return False

        body = {"id": video_id, "snippet": {}}
        if title:
            body["snippet"]["title"] = title[:100]
        if description:
            body["snippet"]["description"] = description[:5000]
        if tags:
            body["snippet"]["tags"] = tags[:500]

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.API_URL}/videos?part=snippet"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status == 200:
                        logger.info(f"Video {video_id} updated")
                        return True
                    else:
                        logger.error(f"Update failed: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"Update error: {e}")
            return False


# ─── Analytics Tracker ─────────────────────────────────────────────────────────

class AnalyticsTracker:
    """Tracks video performance analytics."""

    def __init__(self):
        self.analytics_file = STATE_DIR / "analytics.json"
        self.data = self._load()

    def _load(self) -> dict:
        return load_json(self.analytics_file, {})

    def _save(self):
        try:
            atomic_write_json(self.analytics_file, self.data)
        except OSError as e:
            logger.error(f"Failed to save analytics: {e}")

    def record_upload(self, video_id: str, title: str, category: str,
                      variants: Optional[Dict[str, str]] = None):
        """Record a new upload (with the script's rotation variants, so the
        stats refresher can learn which hook/ending/closing styles perform)."""
        self.data[video_id] = {
            "title": title,
            "category": category,
            "variants": variants or {},
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "views": 0,
            "likes": 0,
            "comments": 0,
            "estimated_retention": 0.0,
        }
        self._save()

    def update_stats(self, video_id: str, views: int = 0, likes: int = 0,
                     comments: int = 0, retention: float = 0.0):
        """Update analytics for a video."""
        if video_id in self.data:
            self.data[video_id]["views"] = views
            self.data[video_id]["likes"] = likes
            self.data[video_id]["comments"] = comments
            self.data[video_id]["estimated_retention"] = retention
            self.data[video_id]["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

    # ── Analytics feedback loop ──────────────────────────────────────────

    VARIANT_STATS_FILE = STATE_DIR / "variant_stats.json"

    async def refresh_video_stats(self, oauth: "YouTubeOAuth",
                                  max_videos: int = 50) -> int:
        """Pull views/likes/comments for the most recent uploads.

        Cheap by design: one videos.list call per 50 ids costs 1 quota unit
        (an upload costs ~1600). Skips dry-run ids. Returns videos updated.
        """
        ids = [vid for vid, d in sorted(
                   self.data.items(),
                   key=lambda kv: kv[1].get("uploaded_at", ""), reverse=True)
               if vid and not vid.startswith("dry_run")][:max_videos]
        if not ids:
            return 0
        token = await oauth.get_access_token()
        if not token:
            logger.warning("Stats refresh skipped: no YouTube token")
            return 0
        updated = 0
        try:
            async with aiohttp.ClientSession() as session:
                for i in range(0, len(ids), 50):   # API max 50 ids per call
                    batch = ids[i:i + 50]
                    async with session.get(
                        "https://www.googleapis.com/youtube/v3/videos",
                        params={"part": "statistics", "id": ",".join(batch)},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            body = (await resp.text())[:200]
                            logger.warning(f"Stats fetch failed: {resp.status} {body}")
                            break
                        for item in (await resp.json()).get("items", []):
                            vid = item.get("id")
                            st = item.get("statistics", {})
                            if vid in self.data:
                                d = self.data[vid]
                                d["views"] = int(st.get("viewCount", 0) or 0)
                                d["likes"] = int(st.get("likeCount", 0) or 0)
                                d["comments"] = int(st.get("commentCount", 0) or 0)
                                d["last_updated"] = datetime.now(timezone.utc).isoformat()
                                updated += 1
        except Exception as e:
            logger.error(f"Stats refresh error: {e}")
        if updated:
            self._save()
            self.rebuild_variant_stats()
            logger.info(f"Refreshed stats for {updated} video(s)")
        return updated

    def rebuild_variant_stats(self) -> dict:
        """Aggregate per-variant performance from analytics.json into
        state/variant_stats.json — read by script.weighted_variant() to bias
        rotation toward what actually gets watched."""
        agg: Dict[str, Dict[str, dict]] = {}
        for vid, d in self.data.items():
            if vid.startswith("dry_run"):
                continue
            views = int(d.get("views", 0) or 0)
            likes = int(d.get("likes", 0) or 0)
            for kind, value in (d.get("variants") or {}).items():
                slot = agg.setdefault(kind, {}).setdefault(
                    value, {"videos": 0, "total_views": 0, "total_likes": 0})
                slot["videos"] += 1
                slot["total_views"] += views
                slot["total_likes"] += likes
        for kind in agg:
            for value, s in agg[kind].items():
                n = max(s["videos"], 1)
                s["avg_views"] = round(s["total_views"] / n, 2)
                s["avg_likes"] = round(s["total_likes"] / n, 2)
        out = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "variants": agg}
        try:
            atomic_write_json(self.VARIANT_STATS_FILE, out)
        except OSError as e:
            logger.error(f"Failed to save variant stats: {e}")
        return out

    def get_best_performers(self, top_n: int = 5) -> List[dict]:
        """Get top performing videos by views."""
        videos = sorted(
            self.data.values(),
            key=lambda x: x.get("views", 0),
            reverse=True
        )
        return videos[:top_n]

    def get_category_performance(self) -> Dict[str, Dict[str, float]]:
        """Get performance metrics grouped by category."""
        categories = {}
        for vid_id, stats in self.data.items():
            cat = stats.get("category", "unknown")
            if cat not in categories:
                categories[cat] = {"total_views": 0, "total_videos": 0, "total_likes": 0}
            categories[cat]["total_views"] += stats.get("views", 0)
            categories[cat]["total_videos"] += 1
            categories[cat]["total_likes"] += stats.get("likes", 0)

        # Calculate averages
        for cat in categories:
            n = categories[cat]["total_videos"]
            if n > 0:
                categories[cat]["avg_views"] = categories[cat]["total_views"] / n
                categories[cat]["avg_likes"] = categories[cat]["total_likes"] / n

        return categories


# ─── Comment Moderator ────────────────────────────────────────────────────────

class CommentModerator:
    """Auto-replies to comments on uploaded videos."""

    API_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self):
        self.oauth = YouTubeOAuth()
        self.reply_templates = config.upload.comment_replies

    async def get_comments(self, video_id: str, max_results: int = 20) -> List[dict]:
        """Get comments for a video."""
        token = await self.oauth.get_access_token()
        if not token:
            return []

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.API_URL}/commentThreads"
                params = {
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": max_results,
                    "order": "time",
                }
                headers = {"Authorization": f"Bearer {token}"}
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("items", [])
                    else:
                        logger.warning(f"Get comments failed: {resp.status}")
                        return []
        except Exception as e:
            logger.error(f"Get comments error: {e}")
            return []

    def classify_comment(self, text: str) -> str:
        """Classify a comment as positive, negative, or question."""
        text_lower = text.lower()

        positive_words = ["great", "awesome", "amazing", "love", "best", "nice",
                          "cool", "fantastic", "wow", "incredible", "thanks",
                          "good", "super", "perfect", "excellent", "fire"]
        negative_words = ["bad", "terrible", "awful", "worst", "hate", "boring",
                          "stupid", "trash", "garbage", "waste", "fake"]
        question_words = ["what", "how", "why", "when", "where", "who", "which",
                          "?", "please"]

        if any(w in text_lower for w in question_words):
            return "question"
        if any(w in text_lower for w in positive_words):
            return "positive"
        if any(w in text_lower for w in negative_words):
            return "negative"
        return "default"

    def generate_reply(self, comment_text: str) -> str:
        """Generate an appropriate reply to a comment."""
        classification = self.classify_comment(comment_text)
        templates = self.reply_templates.get(classification, self.reply_templates["default"])
        import random
        return random.choice(templates)

    async def reply_to_comment(self, comment_id: str, reply_text: str) -> bool:
        """Reply to a specific comment."""
        token = await self.oauth.get_access_token()
        if not token:
            return False

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.API_URL}/comments?part=snippet"
                body = {
                    "snippet": {
                        "parentId": comment_id,
                        "textOriginal": reply_text,
                    }
                }
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status == 200:
                        logger.info(f"Replied to comment {comment_id}")
                        return True
                    else:
                        logger.warning(f"Reply failed: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"Reply error: {e}")
            return False

    async def moderate_video(self, video_id: str, max_replies: int = 5) -> int:
        """Auto-reply to recent comments on a video."""
        comments = await self.get_comments(video_id)
        replied = 0

        for comment in comments[:max_replies]:
            try:
                snippet = comment["snippet"]["topLevelComment"]["snippet"]
                comment_id = comment["snippet"]["topLevelComment"]["id"]
                comment_text = snippet.get("textOriginal", "")

                # Don't reply to our own comments
                author = snippet.get("authorChannelId", {}).get("value", "")
                if author == "channel_owner":
                    continue

                reply = self.generate_reply(comment_text)
                success = await self.reply_to_comment(comment_id, reply)
                if success:
                    replied += 1
                await asyncio.sleep(1)  # Rate limit
            except Exception as e:
                logger.debug(f"Comment moderation error: {e}")
                continue

        logger.info(f"Moderated {replied} comments on video {video_id}")
        return replied


# ─── Publisher Pipeline ────────────────────────────────────────────────────────

class PublisherPipeline:
    """High-level publishing pipeline."""

    def __init__(self):
        self.uploader = YouTubeUploader()
        self.analytics = AnalyticsTracker()
        self.moderator = CommentModerator()

    def upload_paused_until(self) -> Optional[datetime]:
        """UTC datetime uploads are quota-paused until, or None."""
        return QuotaGuard.paused_until()

    async def verify_credentials(self) -> bool:
        """Cheap pre-flight: can we get an access token? (No upload happens.)
        Also records auth health for the Channel Control dashboard."""
        return await self.uploader.oauth.verify()

    @property
    def auth_dead(self) -> bool:
        """True when the refresh token is known-dead (needs re-authorization)."""
        return getattr(self.uploader.oauth, "auth_dead", False)

    async def publish(self, video_path: Path, thumbnail_path: Optional[Path],
                      title: str, description: str, tags: List[str],
                      category: str,
                      privacy_status: str = "private",
                      schedule_hours_from_now: Optional[int] = None,
                      variants: Optional[Dict[str, str]] = None) -> Optional[str]:
        """
        Full publishing pipeline.
        Returns video_id on success, None on failure.
        """
        # Build description with links (mode-aware)
        if CONTENT_MODE == "scifi":
            full_description = (
                f"{description}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 {config.upload.channel_name}\n"
                f"📺 New sci-fi stories every day!\n"
                f"🔔 Subscribe for more near-future tales\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"#scifi #ai #technology #future #Shorts"
            )
        elif CONTENT_MODE == "horror":
            full_description = (
                f"{description}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👻 {config.upload.channel_name}\n"
                f"📺 New scary stories every day!\n"
                f"🔔 Subscribe if you dare\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"#scary #horror #creepy #scarystories #Shorts"
            )
        elif CONTENT_MODE == "bible":
            full_description = (
                f"{description}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✝️ {config.upload.channel_name}\n"
                f"📖 Daily Bible verses & encouragement (KJV)\n"
                f"🔔 Subscribe for a daily blessing\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"#Bible #BibleVerse #Faith #Jesus #God #KJV #Scripture #Christian #Shorts"
            )
        else:
            full_description = (
                f"{description}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {config.upload.channel_name}\n"
                f"📺 New videos every day!\n"
                f"🔔 Subscribe for more content\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"#Shorts"
            )

        # Build tags
        all_tags = list(set(config.upload.default_tags + tags))
        all_tags.append(config.upload.shorts_tag)

        # Schedule if requested
        publish_at = None
        if schedule_hours_from_now:
            scheduled_time = datetime.now(timezone.utc) + timedelta(
                hours=schedule_hours_from_now
            )
            publish_at = scheduled_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Upload (category is mode-aware: Sports/Entertainment/People & Blogs)
        result = await self.uploader.upload(
            video_path=video_path,
            title=title,
            description=full_description,
            tags=all_tags,
            category_id=config.upload.youtube_category_id,
            thumbnail_path=thumbnail_path,
            privacy_status=privacy_status,
            publish_at=publish_at,
        )

        if result and result.get("id"):
            video_id = result["id"]
            self.analytics.record_upload(video_id, title, category, variants)
            logger.info(f"Published: {title} (ID: {video_id})")
            return video_id

        return None

    async def refresh_stats(self) -> int:
        """Pull fresh view counts and rebuild per-variant performance.
        Called daily by the scheduler; also usable standalone."""
        return await self.analytics.refresh_video_stats(self.uploader.oauth)

    async def get_daily_stats(self) -> Dict[str, Any]:
        """Get daily publishing statistics."""
        history = self.uploader.upload_history
        today = datetime.now(timezone.utc).date()

        today_uploads = [
            h for h in history
            if h.get("uploaded_at", "").startswith(today.isoformat())
        ]

        return {
            "total_uploads": len(history),
            "today_uploads": len(today_uploads),
            "best_performers": self.analytics.get_best_performers(),
            "category_performance": self.analytics.get_category_performance(),
        }
