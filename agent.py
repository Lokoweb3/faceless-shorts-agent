"""
Main Orchestrator for the Autonomous YouTube Soccer Content Agent.
Manages the daily workflow: discover → select → download → script → voiceover → edit → thumbnail → upload.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

from config import config, BASE_DIR, STATE_DIR, LOG_DIR, CONTENT_MODE, NICHE, _get_env
from storage import atomic_write_json, load_json, InstanceLock
from discovery import DiscoveryEngine, ContentItem, STORY_HISTORY, STORY_PREMISE_POOLS
from script import ScriptGenerator
from voiceover import VoiceoverPipeline
from media import MediaPipeline
from publisher import PublisherPipeline

logger = logging.getLogger(__name__)


# ─── State Manager ────────────────────────────────────────────────────────────

class StateManager:
    """Manages persistent state for the agent (queue, history, calendar)."""

    QUEUE_FILE = STATE_DIR / "content_queue.json"
    HISTORY_FILE = STATE_DIR / "publication_history.json"
    CALENDAR_FILE = STATE_DIR / "content_calendar.json"
    PENDING_FILE = STATE_DIR / "pending_uploads.json"

    def __init__(self):
        self.queue: List[Dict[str, Any]] = self._load(self.QUEUE_FILE, [])
        self.history: List[Dict[str, Any]] = self._load(self.HISTORY_FILE, [])
        self.calendar: Dict[str, Any] = self._load(self.CALENDAR_FILE, {})
        # Videos that were fully assembled but couldn't be uploaded yet
        # (quota exhausted, auth outage, network) — retried each cycle.
        self.pending: List[Dict[str, Any]] = self._load(self.PENDING_FILE, [])

    def _load(self, path: Path, default: Any) -> Any:
        # Atomic-write + loud-corruption handling lives in storage.py.
        return load_json(path, default)

    def _save(self, path: Path, data: Any):
        try:
            atomic_write_json(path, data)
        except OSError as e:
            logger.error(f"Failed to save {path}: {e}")

    def add_to_queue(self, item: Dict[str, Any]):
        """Add a content item to the processing queue."""
        item["added_at"] = datetime.now(timezone.utc).isoformat()
        item["status"] = "queued"
        self.queue.append(item)
        self._save(self.QUEUE_FILE, self.queue)
        logger.info(f"Added to queue: {item.get('title', 'unknown')}")

    def get_next_from_queue(self) -> Optional[Dict[str, Any]]:
        """Get the next item from the queue that hasn't been processed."""
        for item in self.queue:
            if item.get("status") == "queued":
                return item
        return None

    def mark_processing(self, item: Dict[str, Any]):
        """Mark an item as being processed."""
        item["status"] = "processing"
        item["processing_started_at"] = datetime.now(timezone.utc).isoformat()
        self._save(self.QUEUE_FILE, self.queue)

    def mark_completed(self, item: Dict[str, Any], result: Dict[str, Any]):
        """Mark an item as completed and move to history."""
        item["status"] = "completed"
        item["completed_at"] = datetime.now(timezone.utc).isoformat()
        item["result"] = result
        self.history.append(item)
        self.queue = [q for q in self.queue if q.get("content_hash") != item.get("content_hash")]
        self._save(self.QUEUE_FILE, self.queue)
        self._save(self.HISTORY_FILE, self.history)

    def mark_failed(self, item: Dict[str, Any], error: str):
        """Mark an item as failed."""
        item["status"] = "failed"
        item["failed_at"] = datetime.now(timezone.utc).isoformat()
        item["error"] = error
        self._save(self.QUEUE_FILE, self.queue)

    def mark_pending_upload(self, item: Dict[str, Any]):
        """Mark an item's video as assembled but awaiting upload."""
        item["status"] = "pending_upload"
        item["pending_since"] = datetime.now(timezone.utc).isoformat()
        self._save(self.QUEUE_FILE, self.queue)

    # ── Pending-upload queue (assembled videos awaiting quota/auth) ──────

    def add_pending_upload(self, entry: Dict[str, Any]):
        """Park a finished video for upload retry — never throw away work."""
        entry["queued_at"] = datetime.now(timezone.utc).isoformat()
        entry.setdefault("attempts", 0)
        self.pending.append(entry)
        self._save(self.PENDING_FILE, self.pending)
        logger.info(f"Queued for later upload: {entry.get('title', 'unknown')}")

    def remove_pending_upload(self, entry: Dict[str, Any]):
        self.pending = [p for p in self.pending if p is not entry]
        self._save(self.PENDING_FILE, self.pending)

    def save_pending(self):
        self._save(self.PENDING_FILE, self.pending)

    def get_daily_count(self) -> int:
        """Get number of publications today."""
        today = datetime.now(timezone.utc).date()
        return sum(
            1 for h in self.history
            if h.get("completed_at", "").startswith(today.isoformat())
        )

    def can_publish_today(self) -> bool:
        """Check if we can publish more videos today."""
        return self.get_daily_count() < config.scheduler.max_daily_uploads

    def add_to_calendar(self, date: str, item: Dict[str, Any]):
        """Add an item to the content calendar."""
        if date not in self.calendar:
            self.calendar[date] = []
        self.calendar[date].append(item)
        self._save(self.CALENDAR_FILE, self.calendar)

    def get_calendar(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get calendar entries for a date."""
        if date:
            return self.calendar.get(date, [])
        return self.calendar


# ─── Content Pipeline ─────────────────────────────────────────────────────────

class ContentPipeline:
    """Processes a single content item through the full pipeline."""

    def __init__(self, state: StateManager):
        self.state = state
        self.discovery = DiscoveryEngine()
        self.script_gen = ScriptGenerator()
        self.voiceover = VoiceoverPipeline()
        self.media = MediaPipeline()
        self.publisher = PublisherPipeline()

    async def run_full_pipeline(self, item: ContentItem) -> Optional[Dict[str, Any]]:
        """
        Run the full content pipeline for a single item.
        Returns result dict on success, None on failure.
        """
        item_dict = {
            "title": item.title,
            "description": item.description,
            "source": item.source,
            "category": item.category,
            "content_hash": item.content_hash,
            "source_urls": item.source_urls,
            "relevance_score": item.relevance_score,
        }

        logger.info(f"=== Processing: {item.title} ===")

        # 1. Generate script
        logger.info("Step 1/5: Generating script...")
        script_data = await self.script_gen.generate(item, item.category)
        if not script_data:
            logger.error("Script generation failed")
            self.state.mark_failed(item_dict, "Script generation failed")
            return None
        logger.info(f"Script generated: {len(script_data.get('full_text', ''))} chars")

        # Fail-safe (story modes): if this script is too similar to a recent one,
        # regenerate once. Then summarize it and record it in the channel's story
        # memory (ledger + fingerprint) so future stories don't repeat it.
        if NICHE.uses_story_memory:
            full_text = script_data.get("full_text", "")
            # Content-safety gate: regenerate once if unsafe; SKIP entirely (no upload)
            # if it's still unsafe. This is what makes unattended public posting safe.
            if self.script_gen.is_unsafe_story(full_text):
                logger.warning("Content-safety filter tripped — regenerating once...")
                retry = await self.script_gen.generate(item, item.category)
                if retry and not self.script_gen.is_unsafe_story(retry.get("full_text", "")):
                    script_data = retry
                    full_text = retry.get("full_text", "")
                else:
                    logger.error("Story still unsafe after regeneration — skipping (no upload).")
                    return None
            if STORY_HISTORY.is_similar(full_text):
                logger.info("Story too similar to a recent one — regenerating once...")
                retry = await self.script_gen.generate(item, item.category)
                retry_text = (retry or {}).get("full_text", "")
                # Only accept the retry if it actually fixes the problem AND
                # passes the safety gate (the retry skipped the earlier check).
                if retry_text and not STORY_HISTORY.is_similar(retry_text) \
                        and not self.script_gen.is_unsafe_story(retry_text):
                    script_data = retry
                    full_text = retry_text
                elif retry_text:
                    logger.info("Retry was still similar/unsafe — keeping the original script")
            summary = await self.script_gen.summarize_story(full_text)
            STORY_HISTORY.record(
                mode=CONTENT_MODE,
                title=item.title,
                premise=item.metadata.get("premise", item.description),
                summary=summary,
                text=full_text,
            )
            logger.info(f"Story recorded: {summary}")

        # 2. Generate voiceover (named per-item so runs never clobber each
        #    other's audio/word-timing files)
        logger.info("Step 2/5: Generating voiceover...")
        voiceover_path = await self.voiceover.generate(
            script_data, output_name=f"vo_{item.content_hash[:20]}")
        if not voiceover_path:
            logger.error("Voiceover generation failed")
            self.state.mark_failed(item_dict, "Voiceover generation failed")
            return None
        logger.info(f"Voiceover: {voiceover_path}")

        # 3. Process media (licensed/generated visuals + captions + voiceover)
        logger.info("Step 3/5: Processing media...")
        music_path = self.voiceover.music.get_random_track()
        if music_path:
            logger.info(f"Background music: {music_path.name}")
        else:
            logger.info("No background music found (add tracks to output/audio/music/)")
        video_path, thumbnail_path = await self.media.process(
            script_data=script_data,
            voiceover_path=voiceover_path,
            category=item.category,
            title=item.title,
            music_path=music_path,
            output_name=item.content_hash[:20],
        )
        if not video_path:
            logger.error("Media processing failed")
            self.state.mark_failed(item_dict, "Media processing failed")
            return None
        logger.info(f"Video: {video_path}")
        logger.info(f"Thumbnail: {thumbnail_path}")

        # 4. Publish to YouTube
        logger.info("Step 4/5: Publishing to YouTube...")
        yt_title = self._build_youtube_title(item, script_data)
        yt_description = self._build_description(item, script_data)
        yt_tags = self._build_tags(item, script_data)
        privacy = _get_env("UPLOAD_PRIVACY", "private")
        variants = script_data.get("variants") or {}
        video_id = await self.publisher.publish(
            video_path=video_path,
            thumbnail_path=thumbnail_path,
            title=yt_title,
            description=yt_description,
            tags=yt_tags,
            category=item.category,
            privacy_status=privacy,
            variants=variants,
        )
        if not video_id:
            # The video is fully assembled on disk — never throw that work
            # away. Park it in the pending-upload queue; the scheduler retries
            # it each cycle (once quota resets / auth is fixed).
            paused = self.publisher.upload_paused_until()
            why = f"quota-paused until {paused.isoformat()}" if paused else "publish failed"
            logger.error(f"Publishing failed ({why}) — video queued for retry: {video_path}")
            self.state.add_pending_upload({
                "video_path": str(video_path),
                "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
                "title": yt_title,
                "description": yt_description,
                "tags": yt_tags,
                "category": item.category,
                "privacy_status": privacy,
                "variants": variants,
                "item": item_dict,
            })
            self.state.mark_pending_upload(item_dict)
            return None
        logger.info(f"Published! Video ID: {video_id}")

        # 5. Mark as published in discovery
        logger.info("Step 5/5: Finalizing...")
        self.discovery.mark_published(item)

        result = {
            "video_id": video_id,
            "title": item.title,
            "category": item.category,
            "script_length": len(script_data.get("full_text", "")),
            "video_path": str(video_path),
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
        }

        self.state.mark_completed(item_dict, result)
        logger.info(f"=== Completed: {item.title} (ID: {video_id}) ===")
        return result

    async def process_pending_uploads(self, limit: int = 1) -> int:
        """Retry queued uploads for videos that were assembled but couldn't
        publish (quota exhausted, auth outage, network). Returns the number
        published. Stops early if uploads become quota-paused."""
        published = 0
        for entry in list(self.state.pending):
            if published >= limit:
                break
            if self.publisher.upload_paused_until():
                break
            video_path = Path(entry.get("video_path", ""))
            if not video_path.exists():
                logger.warning(f"Pending upload dropped (file missing): {video_path}")
                self.state.remove_pending_upload(entry)
                continue
            thumb = entry.get("thumbnail_path")
            logger.info(f"Retrying pending upload: {entry.get('title')}")
            video_id = await self.publisher.publish(
                video_path=video_path,
                thumbnail_path=Path(thumb) if thumb else None,
                title=entry.get("title", ""),
                description=entry.get("description", ""),
                tags=entry.get("tags", []),
                category=entry.get("category", ""),
                privacy_status=entry.get("privacy_status", "private"),
                variants=entry.get("variants") or {},
            )
            if video_id:
                item = entry.get("item") or {}
                self.state.remove_pending_upload(entry)
                self.state.mark_completed(item, {
                    "video_id": video_id,
                    "title": item.get("title") or entry.get("title"),
                    "category": entry.get("category"),
                    "video_path": str(video_path),
                    "thumbnail_path": thumb,
                })
                logger.info(f"Pending upload published: {video_id}")
                published += 1
            else:
                entry["attempts"] = entry.get("attempts", 0) + 1
                if entry["attempts"] >= 5:
                    logger.error(f"Dropping pending upload after {entry['attempts']} failed "
                                 f"attempts: {entry.get('title')} ({video_path})")
                    self.state.remove_pending_upload(entry)
                else:
                    self.state.save_pending()
                break  # don't hammer the API with the rest this cycle
        return published

    def _build_description(self, item: ContentItem, script_data: dict) -> str:
        """Build a clean, complete description — never a mid-word cut.

        Bible mode: lead with the hook + the full verse (always complete sentences).
        Other modes: use the full story text if short, else the hook.
        """
        sections = script_data.get("sections", {}) or {}
        hook = (sections.get("hook") or script_data.get("hook") or "").strip()
        full_text = (script_data.get("full_text") or item.description or "").strip()

        if NICHE.kind == "verse":
            verse = (sections.get("verse") or "").strip()
            parts = [p for p in (hook, verse) if p]
            desc = "\n\n".join(parts) if parts else full_text
        else:
            # Use the whole script if it's reasonably short, else just the hook.
            desc = full_text if len(full_text) <= 700 else (hook or full_text[:300])

        # Final safety: if we must trim, cut on a sentence boundary, not mid-word.
        if len(desc) > 900:
            cut = desc[:900]
            for end in (". ", "! ", "? ", "\n"):
                idx = cut.rfind(end)
                if idx > 400:
                    cut = cut[:idx + 1]
                    break
            desc = cut.strip()
        return desc

    def _build_youtube_title(self, item: ContentItem, script_data: dict) -> str:
        """Build an optimized YouTube title."""
        # Niches with generated SEO titles use them directly.
        if NICHE.uses_seo_title:
            base = script_data.get("seo_title", "").strip() or item.title
            suffix = " #Shorts"
            if len(base) + len(suffix) <= 100 and "#shorts" not in base.lower():
                base = f"{base}{suffix}"
            return base[:100]

        # News-niche behavior: hook prefix + the niche's title suffix.
        hook = script_data.get("hook", "")
        title = item.title
        if hook and len(hook) < 50:
            title = f"{hook} | {title}"
        if len(title) < 80:
            title = f"{title}{NICHE.news_title_suffix}"
        return title[:100]

    def _build_tags(self, item: ContentItem, script_data: dict) -> List[str]:
        """Build tags from content."""
        tags = [item.title]
        tags.append(item.category.replace("_", " "))
        tags.append(item.source)

        # Add category-specific tags
        cat_info = config.categories.get(item.category, {})
        tags.extend(cat_info.get("keywords", []))

        return tags


# ─── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Manages the autonomous scheduling of content creation and publishing."""

    def __init__(self, state: StateManager, pipeline: ContentPipeline):
        self.state = state
        self.pipeline = pipeline
        self.discovery = DiscoveryEngine()
        self.running = False
        self._shutdown_event = asyncio.Event()

    async def start(self):
        """Start the scheduler loop."""
        self.running = True
        logger.info("Scheduler started")

        # Handle shutdown signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        while self.running and not self._shutdown_event.is_set():
            try:
                await self._run_cycle()
            except Exception as e:
                logger.error(f"Scheduler cycle error: {e}", exc_info=True)

            # Wait for next check interval
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=config.scheduler.check_interval_minutes * 60
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

        logger.info("Scheduler stopped")

    def _handle_shutdown(self):
        """Handle graceful shutdown."""
        logger.info("Shutdown signal received")
        self.running = False
        self._shutdown_event.set()

    async def stop(self):
        """Stop the scheduler."""
        self.running = False
        self._shutdown_event.set()

    STATS_STAMP_FILE = STATE_DIR / "stats_refreshed.json"

    async def _maybe_refresh_stats(self):
        """Once per UTC day, pull real view counts and rebuild the variant
        performance table that biases script rotation (analytics feedback
        loop). Costs ~1 quota unit — negligible next to a 1600-unit upload."""
        today = datetime.now(timezone.utc).date().isoformat()
        from storage import load_json, atomic_write_json
        stamp = load_json(self.STATS_STAMP_FILE, {})
        if stamp.get("date") == today:
            return
        try:
            updated = await self.pipeline.publisher.refresh_stats()
            atomic_write_json(self.STATS_STAMP_FILE,
                              {"date": today, "videos_updated": updated})
        except Exception as e:
            logger.warning(f"Daily stats refresh failed (will retry next cycle): {e}")

    async def _run_cycle(self):
        """Run a single scheduler cycle."""
        logger.info("=== Starting scheduler cycle ===")

        # Analytics feedback loop: refresh view counts once a day.
        if not config.scheduler.dry_run:
            await self._maybe_refresh_stats()

        # 0. Circuit breaker: if uploads are quota-paused, don't burn AI /
        #    image / TTS work generating videos that can't be published yet.
        paused = self.pipeline.publisher.upload_paused_until()
        if paused:
            logger.info(f"Uploads quota-paused until {paused.isoformat()} — skipping cycle")
            return

        # 0b. Auth pre-flight: verify we can get an upload token BEFORE
        #     spending work on generation. Records health for the dashboard.
        #     (Dry runs don't need auth.)
        if not config.scheduler.dry_run:
            if not await self.pipeline.publisher.verify_credentials():
                if self.pipeline.publisher.auth_dead:
                    logger.error("HALTED: YouTube refresh token is dead (invalid_grant). "
                                 "Run  python3 setup_oauth.py  to re-authorize; the "
                                 "agent will resume automatically.")
                else:
                    logger.error("YouTube auth unavailable (network or credentials "
                                 "issue) — skipping generation this cycle; will "
                                 "retry next cycle.")
                return

        # 1. Check if we can publish today
        if not self.state.can_publish_today():
            logger.info(f"Daily upload limit reached ({config.scheduler.max_daily_uploads})")
            return

        # 1b. Retry any videos that were assembled earlier but couldn't
        #     upload. One publish per cycle — same pacing as a fresh item.
        if await self.pipeline.process_pending_uploads(limit=1):
            return
        if self.pipeline.publisher.upload_paused_until():
            logger.info("Uploads became quota-paused during retry — skipping generation")
            return

        # 2. Check queue for pending items
        next_item = self.state.get_next_from_queue()
        if next_item:
            logger.info(f"Processing queued item: {next_item.get('title')}")
            # Reconstruct ContentItem from dict
            item = ContentItem(
                title=next_item.get("title", ""),
                description=next_item.get("description", ""),
                source=next_item.get("source", "queue"),
                category=next_item.get("category", "player_stories"),
                date=next_item.get("added_at", datetime.now(timezone.utc).isoformat()),
                relevance_score=next_item.get("relevance_score", 0.5),
                source_urls=next_item.get("source_urls", []),
                content_hash=next_item.get("content_hash", ""),
            )
            self.state.mark_processing(next_item)
            await self.pipeline.run_full_pipeline(item)
            return

        # 3. Discover new content
        logger.info("Discovering new content...")
        items = await self.discovery.get_top_stories(count=5)

        if not items:
            logger.info("No new content discovered")
            return

        # 4. Add to queue
        for item in items:
            item_dict = {
                "title": item.title,
                "description": item.description,
                "source": item.source,
                "category": item.category,
                "content_hash": item.content_hash,
                "source_urls": item.source_urls,
                "relevance_score": item.relevance_score,
            }
            self.state.add_to_queue(item_dict)

        # 5. Process the best item immediately
        best_item = items[0]
        logger.info(f"Processing best item: {best_item.title}")
        # mark the actual queued entry (not a throwaway dict) as processing
        queued = next(
            (q for q in self.state.queue
             if q.get("content_hash") == best_item.content_hash),
            None,
        )
        if queued:
            self.state.mark_processing(queued)
        await self.pipeline.run_full_pipeline(best_item)

        logger.info("=== Scheduler cycle complete ===")


# ─── Main Agent ───────────────────────────────────────────────────────────────

class SoccerContentAgent:
    """Main autonomous agent that orchestrates everything."""

    def __init__(self):
        self.state = StateManager()
        self.pipeline = ContentPipeline(self.state)
        self.scheduler = Scheduler(self.state, self.pipeline)
        self.discovery = DiscoveryEngine()
        self.start_time = datetime.now(timezone.utc)

    async def run_once(self):
        """Run a single content creation cycle (for testing or manual use)."""
        logger.info("=== Running single content cycle ===")

        # Same pre-flight as the scheduler: don't generate what can't upload.
        if not config.scheduler.dry_run:
            paused = self.pipeline.publisher.upload_paused_until()
            if paused:
                logger.warning(f"Uploads are quota-paused until {paused.isoformat()} — "
                               "not generating. Finished videos will retry automatically.")
                return None
            if not await self.pipeline.publisher.verify_credentials():
                logger.error("YouTube auth unavailable — fix credentials first "
                             "(python3 setup_oauth.py).")
                return None
            # Prefer draining an already-assembled video over generating anew.
            if await self.pipeline.process_pending_uploads(limit=1):
                logger.info("Published a queued video from the pending-upload retry queue.")
                return {"status": "published_pending_upload"}

        # Discover content
        items = await self.discovery.get_top_stories(count=3)
        if not items:
            logger.info("No content discovered")
            return

        # Process the best item
        best = items[0]
        result = await self.pipeline.run_full_pipeline(best)

        if result:
            logger.info(f"Successfully created: {result.get('video_id')}")
        else:
            logger.error("Content creation failed")

        return result

    async def run_forever(self):
        """Run the agent autonomously 24/7."""
        logger.info("=" * 60)
        logger.info("Soccer Content Agent starting...")
        logger.info(f"Start time: {self.start_time.isoformat()}")
        logger.info(f"Dry run mode: {config.scheduler.dry_run}")
        logger.info(f"Max daily uploads: {config.scheduler.max_daily_uploads}")
        logger.info(f"Check interval: {config.scheduler.check_interval_minutes} minutes")
        logger.info("=" * 60)

        await self.scheduler.start()

    async def status(self) -> Dict[str, Any]:
        """Get current agent status."""
        uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        return {
            "status": "running" if self.scheduler.running else "stopped",
            "uptime_seconds": uptime,
            "uptime_formatted": str(timedelta(seconds=int(uptime))),
            "queue_size": len(self.state.queue),
            "total_published": len(self.state.history),
            "published_today": self.state.get_daily_count(),
            "can_publish_today": self.state.can_publish_today(),
            "dry_run": config.scheduler.dry_run,
        }

    async def queue_info(self) -> List[Dict[str, Any]]:
        """Get info about queued items."""
        return [
            {
                "title": item.get("title"),
                "category": item.get("category"),
                "status": item.get("status"),
                "added_at": item.get("added_at"),
                "score": item.get("relevance_score"),
            }
            for item in self.state.queue
        ]

    async def history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get publication history."""
        return [
            {
                "title": h.get("title"),
                "category": h.get("category"),
                "completed_at": h.get("completed_at"),
                "result": h.get("result"),
            }
            for h in self.state.history[-limit:]
        ]


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Autonomous YouTube Soccer Content Agent"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single content creation cycle and exit"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run in dry-run mode (no actual uploads)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show agent status and exit"
    )
    parser.add_argument(
        "--queue", action="store_true",
        help="Show queued items and exit"
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show publication history and exit"
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Run discovery only and show results"
    )
    parser.add_argument(
        "--refresh-stats", action="store_true",
        help="Pull fresh YouTube view counts, rebuild variant performance, and exit"
    )

    args = parser.parse_args()

    # Configure logging
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )
    # Safety net: no API key/token ever reaches a log line (users paste logs
    # into GitHub issues).
    from storage import RedactingFilter
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())

    # Set dry run mode
    if args.dry_run:
        config.scheduler.dry_run = True
        logger.info("DRY RUN MODE - No content will be published")

    agent = SoccerContentAgent()

    if args.status:
        status = await agent.status()
        print(json.dumps(status, indent=2))
        return

    if args.queue:
        queue = await agent.queue_info()
        print(json.dumps(queue, indent=2))
        return

    if args.history:
        history = await agent.history()
        print(json.dumps(history, indent=2))
        return

    if args.refresh_stats:
        updated = await agent.pipeline.publisher.refresh_stats()
        table = agent.pipeline.publisher.analytics.rebuild_variant_stats()
        print(f"Updated {updated} video(s). Variant performance:")
        print(json.dumps(table.get("variants", {}), indent=2))
        return

    if args.discover:
        discovery = DiscoveryEngine()
        items = await discovery.get_top_stories(count=10)
        for i, item in enumerate(items, 1):
            print(f"{i}. [{item.relevance_score:.2f}] {item.title}")
            print(f"   Source: {item.source} | Category: {item.category}")
            print(f"   URLs: {', '.join(item.source_urls[:2])}")
            print()
        return

    # Single-instance lock: stops two agents (or the dashboard + a CLI run)
    # from processing/publishing concurrently. Read-only commands above skip it.
    lock = InstanceLock(STATE_DIR)
    if not lock.acquire():
        print("Another agent instance is already running (state/agent.lock). Exiting.")
        sys.exit(1)
    try:
        if args.once:
            await agent.run_once()
        else:
            await agent.run_forever()
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
