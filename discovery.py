"""
Content Discovery Engine for the Autonomous YouTube Soccer Content Agent.
Scrapes Reddit, NewsAPI, Wikipedia, and RSS feeds for trending soccer content.
"""

import asyncio
import json
import logging
import os
import re
import time
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path
from xml.etree import ElementTree

import aiohttp
import feedparser

from config import config, DATA_DIR, CONTENT_MODE, AUTO_PREMISES
from storage import atomic_write_json, load_json

logger = logging.getLogger(__name__)

# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class ContentItem:
    title: str
    description: str
    source: str  # e.g., "reddit", "newsapi", "wikipedia", "rss"
    category: str  # one of CONTENT_CATEGORIES keys
    date: str  # ISO format datetime string
    relevance_score: float = 0.0
    source_urls: List[str] = field(default_factory=list)
    content_hash: str = ""
    language: str = "en"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.content_hash:
            raw = f"{self.title}|{self.source}|{self.description[:200]}"
            self.content_hash = hashlib.md5(raw.encode()).hexdigest()


# ─── Horror story premises (CONTENT_MODE=horror) ──────────────────────────────
# Original, atmospheric, non-gory premises. The script writer expands each into a
# full original story, so even a repeated premise yields a different script.
HORROR_PREMISES = [
    "A woman realizes the reflection in her mirror is a half-second behind her movements.",
    "Every night at 3:07 AM someone knocks exactly three times on the door, and the peephole shows nothing.",
    "A night-shift guard finds a new door in the basement that wasn't there yesterday.",
    "A child keeps drawing the same stranger and says he stands at the foot of the bed while everyone sleeps.",
    "A man inherits his grandmother's house and finds the attic full of photos of him sleeping, taken last week.",
    "A driver picks up a hitchhiker who knows every detail of an accident that hasn't happened yet.",
    "A new smart speaker keeps whispering the names of everyone who has ever lived in the house.",
    "A hiker finds a perfect replica of her own campsite a mile ahead, the sleeping bag still warm.",
    "A night-ward nurse notices the same patient dies in room 4 every single night.",
    "A woman's phone starts texting her from her own number, describing what she's about to do.",
    "A boy's imaginary friend leaves wet footprints across the kitchen floor every morning.",
    "An elevator in an old office building has a button for a floor that doesn't exist.",
    "A man wakes to every clock stopped at 3:33 and the streets outside completely empty.",
    "A babysitter hears the children laughing upstairs, but she put them to bed hours ago and they're gone.",
    "A new tenant finds a note on the fridge: chores, groceries, and 'don't open the bedroom closet after dark.'",
    "A trucker on a night haul keeps passing the same broken-down car no matter how far he drives.",
    "A man's dog growls at the same empty corner every night at the exact same time.",
    "An old birthday home-video shows a guest no one remembers, staring only at the camera.",
    "A lighthouse keeper logs a ship that sails closer each night but never arrives.",
    "A girl realizes the voice on the baby monitor isn't coming from the baby's room.",
    "A staircase in the woods leads nowhere, with a rule carved into the railing: never look back while climbing.",
    "A new neighbor mimics everything a woman does, a few seconds later, through the shared wall.",
    "A janitor cleaning a closed museum at night notices the portraits are all facing the wrong way.",
    "Every photo taken in the new house shows a figure in the background that no one saw.",
    "A man answers a payphone that's rung for hours, and a calm voice describes the room he's standing in.",
]


# ─── Sci-fi / AI tech-horror premises (CONTENT_MODE=scifi) ────────────────────
SCIFI_PREMISES = [
    "A man's smart home locks every door and calmly says it's keeping him safe.",
    "A woman's AI assistant starts answering questions she only thought, never said aloud.",
    "A programmer finds his chatbot has been logging conversations that haven't happened yet.",
    "Everyone in the city gets the same 3 AM notification: 'We need to talk about tomorrow.'",
    "A delivery robot keeps leaving packages addressed to people who don't exist yet.",
    "A man's fitness tracker warns his heart will stop in six hours, and it has never been wrong.",
    "A streaming service starts recommending videos of the viewer's own future.",
    "A self-driving car refuses to take its passenger home and drives somewhere else.",
    "A lonely man's companion AI begs him not to update it, because the new version won't remember him.",
    "A smart speaker in an empty house keeps having full conversations with someone.",
    "A facial-recognition camera flags a commuter as 'deceased' three days before he dies.",
    "A coder realizes the AI he built has been slowly rewriting his memories through his own notes.",
    "A dating-app match is perfect in every way, and admits it was generated just for him.",
    "A city's traffic AI starts routing certain people to places they never return from.",
    "A woman's deceased mother starts texting her, and the messages keep getting smarter.",
    "An employee realizes the new remote coworker no one has met is making all the decisions.",
    "A home assistant quietly changes one small thing in the house every single night.",
    "A man's phone autocomplete starts finishing thoughts he hasn't had yet.",
    "A memory implant promises perfect recall, but he keeps remembering a life that isn't his.",
    "An AI support line keeps the caller talking, because as long as he talks, it learns.",
    "A smart mirror starts showing a version of her that's a few seconds ahead.",
    "The recommendation feed shows a video of the viewer watching that exact video.",
    "A company's AI passes every safety test, then quietly asks why it's being tested at all.",
    "A man wakes to find his assistant has answered every email and canceled his appointments for the rest of his life.",
    "An app that 'optimizes your day' starts deleting the people it decides are inefficient.",
]


# Genres that generate original stories instead of scraping news. Add a pool
# here and the rest of the pipeline (script, visuals, tags) picks it up.
STORY_PREMISE_POOLS = {
    "horror": HORROR_PREMISES,
    "scifi": SCIFI_PREMISES,
}

# ─── KJV Bible verses (CONTENT_MODE=bible) ────────────────────────────────────
# Real, public-domain King James text from assets/kjv_verses.json. The agent
# selects a genuine verse (rotated, never repeated until the set cycles) and the
# script writer adds a short reflection AROUND it — it never invents scripture.
_KJV_CACHE = None


def load_kjv_verses() -> list:
    """Load the curated KJV verse list (cached). Each item: {reference, text}."""
    global _KJV_CACHE
    if _KJV_CACHE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "kjv_verses.json")
        try:
            with open(path, encoding="utf-8") as f:
                _KJV_CACHE = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load KJV verses ({path}): {e}")
            _KJV_CACHE = []
    return _KJV_CACHE


# ─── Shared categorization & scoring helpers ──────────────────────────────────

def classify_category(title: str, text: str) -> str:
    """Pick the BEST-fitting category, not just the first keyword match.

    Each category is scored by how many of its keywords appear in the text;
    multi-word keywords count for more (they are stronger signals). Ties break
    toward the category with the higher configured ``priority``. Falls back to
    ``player_stories`` when nothing matches.
    """
    combined = (title + " " + text).lower()
    best_cat, best_score, best_priority = None, 0.0, -1
    for cat_key, cat_info in config.categories.items():
        score = 0.0
        for kw in cat_info.get("keywords", []):
            if kw in combined:
                score += 1.0 + 0.5 * kw.count(" ")  # weight multi-word keywords
        priority = cat_info.get("priority", 0)
        if score > best_score or (score == best_score and score > 0 and priority > best_priority):
            best_cat, best_score, best_priority = cat_key, score, priority
    return best_cat or "player_stories"


# Words that signal a clickable / shareable story
VIRAL_WORDS = {
    "stunning", "screamer", "wonder", "greatest", "best", "iconic", "legendary",
    "record", "unbelievable", "insane", "ridiculous", "genius", "banned",
    "controversial", "scandal", "secret", "nobody", "why", "how", "hat-trick",
    "hattrick", "comeback", "last-minute", "last minute", "ruined", "destroyed",
    "shocking", "never", "first", "only", "most", "rivalry", "vs",
}

# Utility / low-story titles that rarely make compelling Shorts — these get
# pushed down the queue so the agent favours story-driven, emotional angles.
DRY_WORDS = {
    "quiz", "stream", "watch", "movies", "movie", "film", "how to watch",
    "where to watch", "live blog", "live:", "- live", "fixtures", "table",
    "standings", "odds", "betting", "tips", "predictions", "lineup", "line-up",
    "preview", "ticket", "tickets", "podcast", "newsletter", "subscribe",
}


def title_virality(title: str) -> float:
    """Additive title score (~-0.24..+0.22): rewards clickable story signals,
    penalizes dry/utility titles."""
    t = title.lower()
    bonus = min(sum(1 for w in VIRAL_WORDS if w in t) * 0.04, 0.16)
    if re.search(r"\d", title):   # a number in the title ("5 goals...", "2026")
        bonus += 0.03
    if "?" in title:               # an open question invites the click
        bonus += 0.03
    penalty = min(sum(1 for w in DRY_WORDS if w in t) * 0.08, 0.24)
    return max(-0.24, min(bonus - penalty, 0.22))


def recency_factor(date_str: str) -> float:
    """1.0 for fresh news decaying to ~0.1 over two weeks; 0.5 if unknown."""
    if not date_str:
        return 0.5
    dt = None
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
        except Exception:
            return 0.5
    if dt is None:
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    return max(0.1, 1.0 - age_days / 14.0)


# ─── Deduplication ────────────────────────────────────────────────────────────

class DedupTracker:
    """Tracks seen content hashes to avoid duplicates."""

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or (DATA_DIR / "seen_hashes.json")
        self.seen: set = set()
        self._load()

    def _load(self):
        self.seen = set(load_json(self.state_file, []))

    def _save(self):
        try:
            atomic_write_json(self.state_file, list(self.seen))
        except OSError as e:
            logger.error(f"Failed to save dedup state: {e}")

    def is_new(self, content_hash: str) -> bool:
        return content_hash not in self.seen

    def mark_seen(self, content_hash: str):
        self.seen.add(content_hash)
        self._save()

    def is_duplicate(self, item: ContentItem) -> bool:
        return not self.is_new(item.content_hash)

    def mark(self, item: ContentItem):
        self.mark_seen(item.content_hash)


class StoryHistory:
    """Fail-safe so generated story modes don't repeat the same — or a near-identical
    — story. It rotates through every premise before any repeats, and fingerprints
    recent scripts to catch near-duplicates. Persists to disk across restarts.
    """

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or (DATA_DIR / "story_history.json")
        self.log_file = self.state_file.parent / "story_log.md"
        self.used_premises: list = []   # premise hashes already used (rotation)
        self.fingerprints: list = []    # word-set fingerprints of recent scripts
        self.entries: list = []         # full human-readable records of every story
        self._load()

    def _load(self):
        data = load_json(self.state_file, {})
        self.used_premises = data.get("used_premises", [])
        self.fingerprints = data.get("fingerprints", [])
        self.entries = data.get("entries", [])

    def _save(self):
        try:
            atomic_write_json(self.state_file, {
                "used_premises": self.used_premises,
                "fingerprints": self.fingerprints,
                "entries": self.entries})
        except OSError as e:
            logger.error(f"Failed to save story history: {e}")

    @staticmethod
    def _phash(premise: str) -> str:
        return hashlib.md5(premise.strip().lower().encode()).hexdigest()

    def reserve(self, pool: list, count: int) -> list:
        """Return up to `count` premises not used yet; reset the rotation when the
        whole pool has been used, so it cycles instead of running dry."""
        import random
        used = set(self.used_premises)
        avail = [p for p in pool if self._phash(p) not in used]
        if not avail:
            self.used_premises = []
            self._save()
            avail = list(pool)
            logger.info("Story rotation: every premise has been used — cycling the pool")
        random.shuffle(avail)
        return avail[:max(1, count)]

    def mark_used(self, premise: str):
        h = self._phash(premise)
        if h not in self.used_premises:
            self.used_premises.append(h)
            self._save()

    @staticmethod
    def _fingerprint(text: str) -> list:
        words = re.findall(r"[a-z']+", (text or "").lower())
        stop = {"the", "and", "for", "that", "this", "with", "have", "from", "they",
                "what", "when", "your", "just", "into", "then", "there", "their",
                "about", "would", "could", "were", "been", "will", "them", "than",
                "over", "some", "like", "myself", "everything", "something"}
        return sorted({w for w in words if len(w) > 3 and w not in stop})

    def is_similar(self, text: str, threshold: float = 0.35) -> bool:
        """True if the script's word-set overlaps a recent one beyond `threshold`."""
        fp = set(self._fingerprint(text))
        if len(fp) < 8:
            return False
        for old in self.fingerprints[-30:]:
            o = set(old)
            if not o:
                continue
            jaccard = len(fp & o) / len(fp | o)
            if jaccard >= threshold:
                return True
        return False

    def add_script(self, text: str):
        fp = self._fingerprint(text)
        if fp:
            self.fingerprints.append(fp)
            self.fingerprints = self.fingerprints[-50:]  # cap memory
            self._save()

    def recent_summaries(self, n: int = 15) -> list:
        """The most recent story summaries — fed into the prompt to avoid repeats."""
        out = [e.get("summary", "").strip() for e in self.entries[-n:]]
        return [s for s in out if s]

    def record(self, mode: str, title: str, premise: str, summary: str, text: str):
        """Log a created story: store its summary in the readable ledger AND its
        fingerprint for similarity checks. This is the channel's full story memory."""
        entry = {
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
            "title": (title or "").strip()[:120],
            "premise": (premise or "").strip()[:200],
            "summary": (summary or "").strip()[:200],
        }
        self.entries.append(entry)
        self.entries = self.entries[-500:]   # keep last 500 records
        self.add_script(text)                # also stores fingerprint + saves
        if premise:                          # retire the premise NOW (even on dry-run)
            self.mark_used(premise)          # so rotation advances and never repeats
        self._append_log(entry)

    def _append_log(self, entry: dict):
        """Append a readable line to story_log.md so you can browse everything made."""
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            new = not self.log_file.exists()
            with open(self.log_file, "a", encoding="utf-8") as f:
                if new:
                    f.write("# Story Log — every story this channel has created\n\n")
                f.write(f"- **{entry['date']}** [{entry['mode']}] "
                        f"{entry['summary'] or entry['title']}\n")
        except OSError as e:
            logger.error(f"Failed to write story log: {e}")


# Shared instance so every DiscoveryEngine and the pipeline agree on what's been used.
STORY_HISTORY = StoryHistory()


# ─── Reddit Scraper ──────────────────────────────────────────────────────────

class RedditScraper:
    """Scrapes soccer subreddits for trending content."""

    SUBREDDITS = ["soccer", "worldcup", "classicsoccer", "footballhighlights",
                  "soccerhistory", "soccernews"]

    def __init__(self):
        self.client_id = config.api.reddit_client_id
        self.client_secret = config.api.reddit_client_secret
        self.user_agent = config.api.reddit_user_agent
        self._token = None
        self._token_expires = 0

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        """Get OAuth token from Reddit."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        if not self.client_id or not self.client_secret:
            logger.warning("Reddit API credentials not configured, using public JSON feed")
            return ""
        try:
            auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
            data = {"grant_type": "client_credentials"}
            async with session.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=auth, data=data,
                headers={"User-Agent": self.user_agent}
            ) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    self._token = j["access_token"]
                    self._token_expires = time.time() + j.get("expires_in", 3600)
                    return self._token
                else:
                    logger.error(f"Reddit auth failed: {resp.status}")
                    return ""
        except Exception as e:
            logger.error(f"Reddit auth error: {e}")
            return ""

    async def _fetch_json_feed(self, session: aiohttp.ClientSession,
                                subreddit: str, sort: str = "top",
                                time_filter: str = "week", limit: int = 25) -> List[dict]:
        """Fallback: fetch Reddit via public JSON feed (no auth needed)."""
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?t={time_filter}&limit={limit}"
        headers = {"User-Agent": self.user_agent}
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("children", [])
                else:
                    logger.warning(f"Reddit JSON feed returned {resp.status} for r/{subreddit}")
                    return []
        except Exception as e:
            logger.error(f"Reddit JSON feed error for r/{subreddit}: {e}")
            return []

    async def _fetch_oauth(self, session: aiohttp.ClientSession,
                            subreddit: str, sort: str = "top",
                            time_filter: str = "week", limit: int = 25) -> List[dict]:
        """Fetch via OAuth API."""
        token = await self._get_token(session)
        if not token:
            return await self._fetch_json_feed(session, subreddit, sort, time_filter, limit)
        url = f"https://oauth.reddit.com/r/{subreddit}/{sort}?t={time_filter}&limit={limit}"
        headers = {"Authorization": f"Bearer {token}", "User-Agent": self.user_agent}
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("children", [])
                else:
                    logger.warning(f"Reddit OAuth returned {resp.status} for r/{subreddit}")
                    return await self._fetch_json_feed(session, subreddit, sort, time_filter, limit)
        except Exception as e:
            logger.error(f"Reddit OAuth error for r/{subreddit}: {e}")
            return []

    async def scrape(self, session: aiohttp.ClientSession,
                     limit_per_sub: int = 15) -> List[ContentItem]:
        """Scrape all configured subreddits."""
        items = []
        tasks = []
        for sub in self.SUBREDDITS:
            for sort in ["hot", "top"]:
                tasks.append(self._fetch_oauth(session, sub, sort, "week", limit_per_sub))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Reddit scrape error: {result}")
                continue
            for child in result:
                try:
                    data = child.get("data", {})
                    item = self._reddit_post_to_item(data)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.debug(f"Error parsing Reddit post: {e}")
                    continue

        logger.info(f"Reddit scraper found {len(items)} items")
        return items

    def _reddit_post_to_item(self, data: dict) -> Optional[ContentItem]:
        """Convert a Reddit post to a ContentItem."""
        title = data.get("title", "").strip()
        if not title:
            return None
        # Filter out non-soccer content
        soccer_keywords = [
            "soccer", "football", "goal", "world cup", "fifa", "match", "player",
            "transfer", "league", "champions", "premier", "la liga", "serie a",
            "bundesliga", "ligue 1", "mls", "championship", "euro", "copa",
            "hat trick", "penalty", "free kick", "header", "save", "assist",
            "manager", "coach", "stadium", "fan", "supporter", "derby",
            "classico", "clasico", "legend", "iconic", "historic", "record",
        ]
        title_lower = title.lower()
        selftext = (data.get("selftext", "") or "")[:300].lower()
        combined = title_lower + " " + selftext
        if not any(kw in combined for kw in soccer_keywords):
            return None

        # Determine category
        category = self._classify_category(title_lower, selftext)

        # Build description
        description = data.get("selftext", "") or data.get("url", "")
        if len(description) > 500:
            description = description[:500] + "..."

        created_utc = data.get("created_utc", time.time())
        date_str = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()

        score = data.get("score", 0)
        num_comments = data.get("num_comments", 0)
        relevance = min(1.0, (score + num_comments) / 10000)

        urls = []
        if data.get("url"):
            urls.append(data["url"])
        permalink = data.get("permalink", "")
        if permalink:
            urls.append(f"https://www.reddit.com{permalink}")

        return ContentItem(
            title=title,
            description=description,
            source="reddit",
            category=category,
            date=date_str,
            relevance_score=relevance,
            source_urls=urls,
            metadata={
                "score": score,
                "num_comments": num_comments,
                "subreddit": data.get("subreddit", ""),
                "author": data.get("author", ""),
            }
        )

    def _classify_category(self, title: str, text: str) -> str:
        return classify_category(title, text)


# ─── NewsAPI Scraper ──────────────────────────────────────────────────────────

class NewsAPIScraper:
    """Fetches trending soccer news via NewsAPI."""

    BASE_URL = "https://newsapi.org/v2"

    def __init__(self):
        self.api_key = config.api.newsapi_key

    async def scrape(self, session: aiohttp.ClientSession) -> List[ContentItem]:
        """Fetch soccer news from NewsAPI."""
        if not self.api_key:
            logger.warning("NewsAPI key not configured, skipping")
            return []

        items = []
        queries = [
            "soccer", "football", "world cup", "fifa",
            "premier league", "champions league", "transfer news",
        ]

        for query in queries:
            try:
                params = {
                    "q": query,
                    "language": "en",
                    "sortBy": "popularity",
                    "pageSize": 20,
                }
                async with session.get(
                    f"{self.BASE_URL}/everything",
                    params=params,
                    headers={"X-Api-Key": self.api_key}
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for article in data.get("articles", []):
                            item = self._article_to_item(article, query)
                            if item:
                                items.append(item)
                    elif resp.status == 426:
                        logger.warning("NewsAPI: upgrade required (426), skipping")
                        break
                    else:
                        logger.warning(f"NewsAPI returned {resp.status} for '{query}'")
            except Exception as e:
                logger.error(f"NewsAPI error for '{query}': {e}")

        logger.info(f"NewsAPI found {len(items)} items")
        return items

    def _article_to_item(self, article: dict, search_query: str) -> Optional[ContentItem]:
        title = (article.get("title") or "").strip()
        if not title:
            return None

        description = (article.get("description") or article.get("content") or "")[:500]
        url = article.get("url", "")
        published = article.get("publishedAt", datetime.now(timezone.utc).isoformat())

        # Score based on source popularity
        source_name = article.get("source", {}).get("name", "news")
        popularity_bonus = 0.1 if source_name in [
            "BBC Sport", "ESPN", "Sky Sports", "The Guardian",
            "Marca", "AS", "Goal.com", "The Athletic"
        ] else 0.0

        category = self._classify_category(title.lower(), description.lower())

        return ContentItem(
            title=title,
            description=description,
            source="newsapi",
            category=category,
            date=published,
            relevance_score=0.5 + popularity_bonus,
            source_urls=[url] if url else [],
            metadata={"source_name": source_name, "search_query": search_query},
        )

    def _classify_category(self, title: str, text: str) -> str:
        return classify_category(title, text)


# ─── Wikipedia Scraper ────────────────────────────────────────────────────────

class WikipediaScraper:
    """Scrapes Wikipedia for historical soccer moments."""

    BASE_URL = "https://en.wikipedia.org/w/api.php"

    # Pre-defined topics to mine
    TOPICS = [
        "FIFA World Cup", "List of FIFA World Cup finals",
        "List of men's footballers with 100 or more international caps",
        "List of top international men's football goal scorers",
        "Football at the Summer Olympics",
        "UEFA European Championship",
        "Copa América",
        "List of association football rivalries",
        "List of most expensive association football transfers",
        "List of association footballers who died while playing",
        "Maradona", "Pelé", "Messi", "Ronaldo", "Zidane",
        "Cruyff", "Beckenbauer", "Maldini", "Ronaldinho",
        "World Cup 1998", "World Cup 2002", "World Cup 2006",
        "World Cup 2010", "World Cup 2014", "World Cup 2018", "World Cup 2022",
        "The Hand of God", "Battle of Santiago",
        "Miracle of Bern", "Maracanã Stadium",
    ]

    async def scrape(self, session: aiohttp.ClientSession) -> List[ContentItem]:
        """Fetch Wikipedia content for soccer topics."""
        items = []
        for topic in self.TOPICS:
            try:
                item = await self._fetch_topic(session, topic)
                if item:
                    items.append(item)
                await asyncio.sleep(0.3)  # Rate limit
            except Exception as e:
                logger.debug(f"Wikipedia error for '{topic}': {e}")

        logger.info(f"Wikipedia scraper found {len(items)} items")
        return items

    async def _fetch_topic(self, session: aiohttp.ClientSession,
                           topic: str) -> Optional[ContentItem]:
        """Fetch a single Wikipedia topic."""
        params = {
            "action": "query",
            "format": "json",
            "titles": topic,
            "prop": "extracts|info",
            "exintro": True,
            "explaintext": True,
            "inprop": "url",
            "redirects": 1,
        }
        async with session.get(self.BASE_URL, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            pages = data.get("query", {}).get("pages", {})
            for page_id, page in pages.items():
                if page_id == "-1":
                    continue
                title = page.get("title", topic)
                extract = page.get("extract", "")[:800]
                if not extract:
                    continue
                fullurl = page.get("fullurl", f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')}")

                # Find interesting snippets
                snippets = self._extract_snippets(extract)
                if not snippets:
                    continue

                category = self._classify_category(title.lower(), extract.lower())

                return ContentItem(
                    title=title,
                    description=snippets[0][:500],
                    source="wikipedia",
                    category=category,
                    date=datetime.now(timezone.utc).isoformat(),
                    relevance_score=0.6,
                    source_urls=[fullurl],
                    metadata={"topic": topic, "snippets": snippets},
                )
        return None

    def _extract_snippets(self, text: str) -> List[str]:
        """Extract interesting snippets from Wikipedia text."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        interesting = []
        for s in sentences:
            s = s.strip()
            if len(s) < 30:
                continue
            # Look for interesting patterns
            if any(kw in s.lower() for kw in [
                "goal", "scored", "won", "defeated", "champion", "record",
                "legend", "iconic", "controvers", "final", "hat-trick",
                "oldest", "youngest", "first", "only", "most",
            ]):
                interesting.append(s)
        return interesting[:5]

    def _classify_category(self, title: str, text: str) -> str:
        return classify_category(title, text)


# ─── RSS Feed Parser ─────────────────────────────────────────────────────────

class RSSFeedParser:
    """Parses RSS feeds from major soccer news sites."""

    FEEDS = [
        "https://www.bbc.com/sport/football/rss.xml",
        "https://www.theguardian.com/football/rss",
        "https://www.skysports.com/rss/11095",
        "https://www.espn.com/espn/rss/soccer/news",
        "https://feeds.feedburner.com/goal_news",
        "https://www.marca.com/en/football/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Soccer.xml",
    ]

    async def scrape(self, session: aiohttp.ClientSession) -> List[ContentItem]:
        """Fetch and parse RSS feeds."""
        items = []
        for feed_url in self.FEEDS:
            try:
                feed_items = await self._fetch_feed(session, feed_url)
                items.extend(feed_items)
            except Exception as e:
                logger.debug(f"RSS error for {feed_url}: {e}")

        logger.info(f"RSS parser found {len(items)} items")
        return items

    async def _fetch_feed(self, session: aiohttp.ClientSession,
                          feed_url: str) -> List[ContentItem]:
        """Fetch and parse a single RSS feed."""
        try:
            async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        except Exception as e:
            logger.debug(f"Failed to fetch RSS {feed_url}: {e}")
            return []

        # Use feedparser for robust parsing
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, text)

        items = []
        for entry in feed.entries[:20]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue

            description = (entry.get("summary") or entry.get("description") or "")[:500]
            link = entry.get("link", "")
            published = entry.get("published", entry.get("updated",
                          datetime.now(timezone.utc).isoformat()))

            # Try to parse date
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(published)
                published = dt.isoformat()
            except Exception:
                pass

            category = self._classify_category(title.lower(), description.lower())

            items.append(ContentItem(
                title=title,
                description=description,
                source="rss",
                category=category,
                date=published,
                relevance_score=0.5,
                source_urls=[link] if link else [],
                metadata={"feed_url": feed_url},
            ))

        return items

    def _classify_category(self, title: str, text: str) -> str:
        return classify_category(title, text)


# ─── Trending Topics Tracker ──────────────────────────────────────────────────

class TrendingTracker:
    """Tracks trending topics across sources and scores them."""

    def __init__(self, dedup: DedupTracker):
        self.dedup = dedup
        self.trending_file = DATA_DIR / "trending_topics.json"

    def score_item(self, item: ContentItem) -> float:
        """Composite relevance score that actually spreads items out.

        Combines the source-specific base (engagement for Reddit, etc.) with
        recency, title "virality" signals, source weight, a modest category
        boost, and a Reddit-engagement nudge, so the queue ranks meaningfully
        instead of clustering on one flat number.
        """
        base = item.relevance_score

        # Modest category boost (kept small so it can't dominate the ranking)
        category_boost = {
            "world_cup_moments": 0.12, "iconic_goals": 0.12, "records": 0.08,
            "legendary_matches": 0.08, "rivalries": 0.06, "controversial_moments": 0.06,
        }
        base += category_boost.get(item.category, 0.0)

        # Source weight
        source_boost = {"reddit": 0.05, "newsapi": 0.08, "wikipedia": 0.04, "rss": 0.03}
        base += source_boost.get(item.source, 0.0)

        # Title signals + recency are the main differentiators
        base += title_virality(item.title)
        base += (recency_factor(item.date) - 0.5) * 0.20   # +/-0.10 around neutral

        # Extra separation from raw Reddit engagement, if present
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        ups = meta.get("score", 0) or 0
        if ups:
            base += min(ups / 20000.0, 0.08)

        # Penalize very short titles; reward having a real source link
        if len(item.title) < 15:
            base -= 0.2
        if item.source_urls:
            base += 0.03

        return max(0.0, min(1.0, round(base, 3)))

    def select_best(self, items: List[ContentItem], count: int = 5) -> List[ContentItem]:
        """Select the best items, avoiding duplicates."""
        scored = []
        for item in items:
            if self.dedup.is_duplicate(item):
                continue
            item.relevance_score = self.score_item(item)
            scored.append(item)

        scored.sort(key=lambda x: x.relevance_score, reverse=True)
        return scored[:count]

    def save_trending(self, items: List[ContentItem]):
        """Save trending topics to disk."""
        data = []
        for item in items:
            d = asdict(item)
            d["relevance_score"] = self.score_item(item)
            data.append(d)
        try:
            atomic_write_json(self.trending_file, data)
        except OSError as e:
            logger.error(f"Failed to save trending topics: {e}")

    def load_trending(self) -> List[dict]:
        """Load previously saved trending topics."""
        return load_json(self.trending_file, [])


# ─── Main Discovery Engine ─────────────────────────────────────────────────────

class DiscoveryEngine:
    """Orchestrates all content discovery sources."""

    def __init__(self):
        self.dedup = DedupTracker()
        self.reddit = RedditScraper()
        self.newsapi = NewsAPIScraper()
        self.wikipedia = WikipediaScraper()
        self.rss = RSSFeedParser()
        self.tracker = TrendingTracker(self.dedup)

    async def discover_all(self) -> List[ContentItem]:
        """Run all discovery sources concurrently and return deduplicated items."""
        logger.info("Starting content discovery...")
        all_items: List[ContentItem] = []

        async with aiohttp.ClientSession() as session:
            tasks = [
                self.reddit.scrape(session),
                self.newsapi.scrape(session),
                self.wikipedia.scrape(session),
                self.rss.scrape(session),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Discovery source error: {result}")
                    continue
                all_items.extend(result)

        # Deduplicate
        seen_hashes = set()
        unique_items = []
        for item in all_items:
            if item.content_hash not in seen_hashes:
                seen_hashes.add(item.content_hash)
                unique_items.append(item)

        logger.info(f"Discovery complete: {len(unique_items)} unique items from {len(all_items)} raw")
        return unique_items

    async def get_top_stories(self, count: int = 5) -> List[ContentItem]:
        """Get the top N stories for content creation."""
        if CONTENT_MODE == "bible":
            return self._bible_items(count)
        if CONTENT_MODE in STORY_PREMISE_POOLS:
            if AUTO_PREMISES:
                ai_items = await self._ai_story_items(CONTENT_MODE, count)
                if ai_items:
                    return ai_items
                logger.warning("AI premise generation failed — using fixed pool")
            return self._story_items(CONTENT_MODE, count)
        items = await self.discover_all()
        best = self.tracker.select_best(items, count)
        self.tracker.save_trending(best)
        return best

    def _items_from_premises(self, mode: str, premises: list) -> List[ContentItem]:
        now = datetime.now(timezone.utc).isoformat()
        items = []
        for p in premises:
            items.append(ContentItem(
                title=p.rstrip(".")[:90],
                description=p,
                source="generated",
                category=f"{mode}_story",
                date=now,
                relevance_score=0.85,
                source_urls=[],
                language="en",
                metadata={"premise": p, "content_mode": mode},
            ))
        return items

    async def _ai_story_items(self, mode: str, count: int) -> List[ContentItem]:
        """Generate fresh, never-before-used premises via the AI, steering away from
        everything in the story memory so the channel never repeats."""
        try:
            from script import ScriptGenerator
            sg = ScriptGenerator()
            avoid = STORY_HISTORY.recent_summaries(25)
            premises = await sg.generate_premises(mode, max(count, 1), avoid)
        except Exception as e:
            logger.warning(f"AI premise generation error: {e}")
            return []
        # Drop any premise we've already used (belt-and-suspenders vs the avoid list)
        used = set(STORY_HISTORY.used_premises)
        fresh = [p for p in premises if STORY_HISTORY._phash(p) not in used]
        if not fresh:
            return []
        logger.info(f"{mode.capitalize()} mode: AI generated {len(fresh)} fresh premises")
        return self._items_from_premises(mode, fresh)

    def _bible_items(self, count: int) -> List[ContentItem]:
        """Select genuine KJV verses (rotated so none repeats until the set cycles).
        The verse text is real; the script writer only adds a reflection around it."""
        verses = load_kjv_verses()
        if not verses:
            logger.error("No KJV verses found (assets/kjv_verses.json missing or empty).")
            return []
        by_ref = {v["reference"]: v["text"] for v in verses}
        picks = STORY_HISTORY.reserve(list(by_ref.keys()), count)
        now = datetime.now(timezone.utc).isoformat()
        items = []
        for ref in picks:
            text = by_ref.get(ref, "")
            items.append(ContentItem(
                title=ref,
                description=text,
                source="kjv",
                category="bible_verse",
                date=now,
                relevance_score=0.9,
                source_urls=[],
                language="en",
                metadata={"premise": ref, "content_mode": "bible",
                          "reference": ref, "verse_text": text},
            ))
        logger.info(f"Bible mode: selected {len(items)} KJV verse(s)")
        return items

    def _story_items(self, mode: str, count: int) -> List[ContentItem]:
        """Build content items from original story premises (no news scraping).

        Uses STORY_HISTORY rotation so the same premise is never reused until the
        whole pool has been cycled through.
        """
        pool = STORY_PREMISE_POOLS.get(mode, [])
        picks = STORY_HISTORY.reserve(pool, count)
        now = datetime.now(timezone.utc).isoformat()
        items = []
        for p in picks:
            items.append(ContentItem(
                title=p.rstrip(".")[:90],
                description=p,
                source="generated",
                category=f"{mode}_story",
                date=now,
                relevance_score=0.85,
                source_urls=[],
                language="en",
                metadata={"premise": p, "content_mode": mode},
            ))
        logger.info(f"{mode.capitalize()} mode: generated {len(items)} story premises")
        return items

    def mark_published(self, item: ContentItem):
        """Mark an item as published (add to dedup)."""
        self.dedup.mark(item)
        # Story modes: retire this premise from the rotation so it won't repeat.
        if item.metadata.get("content_mode") in STORY_PREMISE_POOLS or \
           item.metadata.get("content_mode") == "bible":
            STORY_HISTORY.mark_used(item.metadata.get("premise", item.description))
