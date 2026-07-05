"""
Script Generation Module for the Autonomous YouTube Soccer Content Agent.
Generates engaging scripts using AI models with category-specific templates.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path

import aiohttp

from config import config, DATA_DIR, STATE_DIR, SCRIPTS_OUTPUT_DIR, _get_env, CONTENT_MODE, NICHE
from niches import get_niche
from storage import atomic_write_json, load_json

# All niche-specific content (prompts, premise pools, rotation variants,
# tech angles) lives in niches.py — this module consumes the NICHE object.

# ── Analytics feedback loop: performance-weighted variant selection ──────────
# The nightly stats job (publisher.refresh_video_stats) aggregates real view
# counts per variant into state/variant_stats.json. Rotation points below use
# weighted_variant() instead of random.choice(): 20% of the time it still
# explores uniformly (so no variant ever starves), otherwise it samples
# proportionally to average views, with unproven variants (<2 measured
# videos) weighted at the global average so new options get a fair shot.

VARIANT_STATS_FILE = STATE_DIR / "variant_stats.json"
_EXPLORE_RATE = 0.2
_MIN_VIDEOS_TO_TRUST = 2


def weighted_variant(kind: str, options: list) -> str:
    """Pick a rotation variant, biased toward what has actually performed."""
    try:
        stats = load_json(VARIANT_STATS_FILE, {}).get("variants", {}).get(kind, {})
    except Exception:
        stats = {}
    if not stats or random.random() < _EXPLORE_RATE:
        return random.choice(options)
    proven = [v["avg_views"] for v in stats.values()
              if v.get("videos", 0) >= _MIN_VIDEOS_TO_TRUST]
    if not proven:
        return random.choice(options)
    prior = max(sum(proven) / len(proven), 1.0)   # fair weight for the unproven
    weights = []
    for opt in options:
        s = stats.get(opt)
        if s and s.get("videos", 0) >= _MIN_VIDEOS_TO_TRUST:
            weights.append(max(float(s.get("avg_views", 0.0)), prior * 0.05))
        else:
            weights.append(prior)
    return random.choices(options, weights=weights, k=1)[0]


logger = logging.getLogger(__name__)


# ─── Script Templates ─────────────────────────────────────────────────────────

@dataclass
class ScriptTemplate:
    """A script template for a content category."""
    name: str
    format_name: str
    structure: List[str]  # Sections of the script
    hook_template: str
    cta_template: str
    duration_seconds: int = 50
    language: str = "en"


# Template definitions
SCRIPT_TEMPLATES = {
    "did_you_know": ScriptTemplate(
        name="Did You Know?",
        format_name="did_you_know",
        structure=[
            "hook",          # Attention-grabbing opener
            "setup",         # Context/background
            "reveal",        # The surprising fact
            "explanation",   # Why it matters
            "call_to_action", # Subscribe/like
        ],
        hook_template=(
            "Did you know that {fact}? "
            "Most soccer fans have no idea about this incredible {topic} story."
        ),
        cta_template=(
            "Mind-blowing, right? "
            "Subscribe for more incredible soccer facts you never knew! ⚽"
        ),
    ),
    "what_happened": ScriptTemplate(
        name="What Happened?",
        format_name="what_happened",
        structure=[
            "hook",
            "context",
            "inciting_incident",
            "climax",
            "aftermath",
            "call_to_action",
        ],
        hook_template=(
            "What really happened during {event}? "
            "The story that shocked the soccer world."
        ),
        cta_template=(
            "That's the untold story. "
            "Like and subscribe for more soccer secrets revealed! 🔥"
        ),
    ),
    "legendary": ScriptTemplate(
        name="Legendary",
        format_name="legendary",
        structure=[
            "hook",
            "introduction",
            "rise_to_fame",
            "defining_moment",
            "legacy",
            "call_to_action",
        ],
        hook_template=(
            "This is the story of {name}. "
            "Not just a player — a legend who changed soccer forever."
        ),
        cta_template=(
            "Legends never die. "
            "Subscribe to keep their stories alive! 🙌"
        ),
    ),
    "countdown": ScriptTemplate(
        name="Countdown",
        format_name="countdown",
        structure=[
            "hook",
            "intro",
            "item_1",
            "item_2",
            "item_3",
            "item_4",
            "item_5",
            "call_to_action",
        ],
        hook_template=(
            "Top {number} {topic} that will blow your mind. "
            "Number 1 is absolutely incredible."
        ),
        cta_template=(
            "Which one surprised you most? "
            "Comment below and subscribe for more top lists! 🏆"
        ),
    ),
    "versus": ScriptTemplate(
        name="Versus",
        format_name="versus",
        structure=[
            "hook",
            "introduction_both",
            "side_a",
            "side_b",
            "head_to_head",
            "verdict",
            "call_to_action",
        ],
        hook_template=(
            "{entity_a} vs {entity_b}. "
            "The ultimate soccer rivalry — who comes out on top?"
        ),
        cta_template=(
            "Team {entity_a} or Team {entity_b}? "
            "Vote in the comments and subscribe for more vs battles! ⚔️"
        ),
    ),
}


# ─── AI Script Generator ──────────────────────────────────────────────────────

class ScriptGenerator:
    """Generates scripts using AI models (OpenAI, Anthropic, or local)."""

    def __init__(self):
        self.output_dir = SCRIPTS_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Which rotation variants (hook style, ending style, ...) the most
        # recent _build_prompt chose — recorded into script_data so the
        # analytics loop can join performance back onto them.
        self._last_variants: Dict[str, str] = {}
        # Structural title pattern chosen by the most recent generate_title
        # (empty when the niche has no patterns) — recorded with variants.
        self._last_title_pattern: str = ""

    async def generate(self, content_item: Any, category: str,
                       language: str = "en") -> Optional[Dict[str, Any]]:
        """
        Generate a complete script for a content item.
        Returns dict with: full_text, sections, hook, cta, metadata.
        """
        # JSON single-call niches: one generation returns script + title +
        # scenes + description; code-level guards verify the output.
        if NICHE.script_output == "json" and NICHE.kind == "verse":
            return await self._generate_json_verse(content_item)

        template = SCRIPT_TEMPLATES.get(
            config.categories.get(category, {}).get("script_format", "legendary"),
            SCRIPT_TEMPLATES["legendary"]
        )

        # Build prompt (sets self._last_variants to the rotation choices made)
        self._last_variants = {}
        prompt = self._build_prompt(content_item, template, language)
        variants = dict(self._last_variants)

        # Hook freshness, part 1: steer the FIRST attempt away from recent
        # opening lines (memory-using niches only).
        recent_hooks = self._load_hook_history() if NICHE.uses_story_memory else []
        if recent_hooks:
            prompt += ("\n\nRECENT OPENING LINES — your opening line must NOT "
                       "resemble any of these in imagery or wording; use a "
                       "completely different image:\n- "
                       + "\n- ".join(recent_hooks[-10:]))

        # Generate via AI
        ai_text = await self._call_ai(prompt)

        if not ai_text:
            # Story/devotional niches must NEVER publish the canned fallback:
            # it's the same weak text every run. Fail the item instead — the
            # scheduler simply retries next cycle, so a provider outage costs
            # time, not channel quality.
            if not NICHE.allow_template_fallback:
                logger.error("AI generation failed — skipping this item "
                             "(this niche never uses the template fallback). "
                             "Check your AI provider/key; the agent will retry "
                             "next cycle.")
                return None
            logger.warning("AI generation failed, using template-based fallback")
            ai_text = self._fallback_generate(content_item, template)

        # Parse into sections
        sections = self._parse_sections(ai_text, template)

        # Hook freshness, part 2: if the opening line still echoes a recent
        # one, regenerate ONCE with the offenders spelled out. If the retry
        # is also stale, keep the better attempt and log it — a repeated
        # hook is a quality wart, not a reason to skip the video.
        if recent_hooks:
            hook_line = self._extract_hook_line(sections, ai_text)
            offenders = self._similar_hooks(hook_line, recent_hooks)
            if offenders:
                logger.info(f"Hook too similar to a recent opening — regenerating "
                            f"once ({hook_line[:60]!r} ~ {offenders[0][:60]!r})")
                retry_prompt = prompt + (
                    "\n\nIMPORTANT: your previous opening was rejected for "
                    "repeating recent imagery. It must NOT resemble:\n- "
                    + "\n- ".join(offenders[:5])
                    + "\nOpen with a COMPLETELY different image or scene.")
                retry_text = await self._call_ai(retry_prompt)
                if retry_text:
                    retry_sections = self._parse_sections(retry_text, template)
                    retry_hook = self._extract_hook_line(retry_sections, retry_text)
                    if not self._similar_hooks(retry_hook, recent_hooks):
                        sections = retry_sections
                        ai_text = retry_text
                    else:
                        logger.warning("Retry hook still similar — keeping the "
                                       "original script")

        # Extract hook and CTA
        hook = sections.get("hook", template.hook_template)
        cta = sections.get("call_to_action", template.cta_template)

        # Build full text (skip empty sections so the narration has no
        # stray blank runs from unfilled template slots)
        full_text = "\n\n".join(s for s in sections.values() if s.strip())

        # Remember the accepted opening line for future freshness checks.
        if NICHE.uses_story_memory:
            self._remember_hook(self._extract_hook_line(sections, full_text))

        script_data = {
            "full_text": full_text,
            "sections": sections,
            "hook": hook,
            "cta": cta,
            "template": template.format_name,
            "category": category,
            "language": language,
            "title": content_item.title if hasattr(content_item, 'title') else "Untitled Story",
            "estimated_duration_seconds": self._estimate_duration(full_text),
            # Which rotation variants produced this script — the analytics
            # loop joins views back onto these to learn what performs.
            "variants": variants,
        }

        # Story-matched visual scene prompts (only needed for AI-image visuals)
        if getattr(config.video, "asset_provider", "") == "ai_image":
            try:
                scenes = await self.generate_scene_prompts(full_text, n=8)
                if scenes:
                    script_data["scenes"] = scenes
                    logger.info(f"Generated {len(scenes)} story-matched scene prompts")
            except Exception as e:
                logger.warning(f"Scene prompt generation failed: {e}")

        # SEO hook title (short, punchy, keyword-first) for niches that use it
        if NICHE.uses_seo_title:
            try:
                self._last_title_pattern = ""
                # Verse niches: the reference (item title) enables the
                # verse-anchored pattern and the forced-unique fallback.
                reference = (script_data.get("title", "")
                             if NICHE.kind == "verse" else "")
                # Recent titles go into the FIRST prompt too, so the model
                # varies structure up front instead of only after rejection.
                first_avoid = (self._load_title_history()[-10:]
                               if NICHE.enforce_title_freshness else None)
                seo = await self.generate_title(full_text,
                                                avoid_titles=first_avoid,
                                                reference=reference)
                # Enforce freshness against the recent-title history
                # (dup/fuzzy/cooldowns + retries + mechanical/forced fix).
                if seo and NICHE.enforce_title_freshness:
                    seo = await self._ensure_fresh_title(seo, full_text,
                                                         reference=reference)
                if seo:
                    script_data["seo_title"] = seo
                    logger.info(f"SEO title: {seo}")
                    # Record the structural pattern for the analytics loop.
                    if self._last_title_pattern:
                        variants["title_pattern"] = self._last_title_pattern
                        script_data["variants"] = variants
            except Exception as e:
                logger.warning(f"Title generation failed: {e}")

        # Save script
        await self._save_script(script_data)

        return script_data

    # ── JSON single-call generation (verse niches) ───────────────────────

    @staticmethod
    def _parse_json_block(raw: str) -> Optional[dict]:
        """Extract the first JSON object from model output, tolerating fences
        and stray commentary. None if nothing parseable."""
        s = (raw or "").strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(s[start:end + 1], strict=False)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _verse_verbatim(verse: str, script: str) -> bool:
        """True if the verse's WORDING appears in the script — compared on
        letters/digits only, so quote style and punctuation don't matter but
        a single changed/paraphrased word fails."""
        strip = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        v = strip(verse)
        return bool(v) and v in strip(script)

    async def _generate_json_verse(self, content_item: Any) -> Optional[Dict[str, Any]]:
        """Single-call generation for verse niches: the niche's JSON prompt
        returns title/hook/script/scenes/description/pinned_comment at once.

        Code-enforced guards (the prompt's SELF-CHECK is not trusted):
        - verse text verbatim in the script (retry once, else SKIP the item —
          altered scripture must never publish)
        - hook similarity vs recent openings (retry once, keep best)
        - title freshness (dup/fuzzy/cooldowns + retries + forced-unique)
        """
        reference = getattr(content_item, "title", "")
        verse_text = getattr(content_item, "description", "")
        dur = config.video.target_duration_seconds
        word_count = int(round(dur / 60 * 145))
        published = self._load_title_history()[-15:]
        published_block = ("\n".join(f"- {t}" for t in published)
                           or "- (none published yet)")
        prompt = NICHE.script_prompt.format(
            verse_reference=reference, verse_text=verse_text,
            target_duration=dur, word_count=word_count,
            published_titles=published_block)

        # Hook steering up front, as in the marker path.
        recent_hooks = self._load_hook_history() if NICHE.uses_story_memory else []
        if recent_hooks:
            prompt += ("\n\nRECENT OPENING LINES — your hook must NOT resemble "
                       "any of these in imagery or wording:\n- "
                       + "\n- ".join(recent_hooks[-10:]))

        data, note = None, ""
        for attempt in (1, 2):
            raw = await self._call_ai(prompt + note, max_tokens=1600)
            if not raw:
                continue
            parsed = self._parse_json_block(raw)
            if not parsed or not (parsed.get("script") or "").strip():
                logger.warning(f"JSON script output unparseable (attempt {attempt}/2)")
                note = ("\n\nIMPORTANT: your previous output was not valid JSON. "
                        "Output ONLY the JSON object, no fences, no commentary.")
                continue
            if not self._verse_verbatim(verse_text, parsed["script"]):
                logger.warning(f"Verse not verbatim in script (attempt {attempt}/2) "
                               f"— regenerating")
                note = ("\n\nCRITICAL: your previous output altered the verse text. "
                        "The verse must appear in the script character-for-character "
                        "exactly as provided in INPUTS.")
                continue
            data = parsed
            break
        if not data:
            logger.error("JSON generation failed (unparseable or verse altered "
                         "twice) — skipping this item; altered scripture never "
                         "publishes. Will retry next cycle.")
            return None

        script_text = data["script"].strip()
        hook = (data.get("hook") or "").strip() or \
            self._extract_hook_line({}, script_text)

        # Hook freshness backstop: one regeneration with offenders named.
        offenders = self._similar_hooks(hook, recent_hooks) if recent_hooks else []
        if offenders:
            logger.info(f"Hook too similar to a recent opening — regenerating once "
                        f"({hook[:60]!r} ~ {offenders[0][:60]!r})")
            retry_raw = await self._call_ai(
                prompt + "\n\nIMPORTANT: your previous hook repeated recent imagery. "
                "It must NOT resemble:\n- " + "\n- ".join(offenders[:5]),
                max_tokens=1600)
            retry = self._parse_json_block(retry_raw or "")
            if retry and (retry.get("script") or "").strip() \
                    and self._verse_verbatim(verse_text, retry["script"]):
                r_hook = (retry.get("hook") or "").strip()
                if r_hook and not self._similar_hooks(r_hook, recent_hooks):
                    data, script_text, hook = retry, retry["script"].strip(), r_hook
                else:
                    logger.warning("Retry hook still similar — keeping the original")

        # Word-count sanity (prompt asks ±10%; log-only, never fatal).
        n_words = len(script_text.split())
        if not (word_count * 0.7 <= n_words <= word_count * 1.5):
            logger.warning(f"Script length {n_words} words vs target {word_count} "
                           f"— outside comfort range but publishing anyway")

        variants: Dict[str, str] = {}
        theme = (data.get("emotional_theme") or "").strip()[:60]
        if theme:
            variants["emotional_theme"] = theme

        scenes = [str(s).strip()[:200] for s in (data.get("scene_prompts") or [])
                  if str(s).strip()][:8]

        script_data: Dict[str, Any] = {
            "full_text": script_text,
            "sections": {"hook": hook, "script": script_text},
            "hook": hook,
            "cta": "",
            "template": "json_verse",
            "category": f"{NICHE.key}_verse",
            "language": "en",
            "title": reference,
            "estimated_duration_seconds": self._estimate_duration(script_text),
            "variants": variants,
        }
        if scenes:
            script_data["scenes"] = scenes
        desc = (data.get("description") or "").strip()
        if desc:
            script_data["description_override"] = desc[:900]
        pinned = (data.get("pinned_comment") or "").strip()
        if pinned:
            script_data["pinned_comment"] = pinned[:1000]

        # Title: JSON title is the first candidate; the code-level freshness
        # machinery (dup/fuzzy/cooldowns, retries, verse-ref forcing) decides.
        self._last_title_pattern = ""
        title = (data.get("title") or "").strip()[:90]
        if title and NICHE.enforce_title_freshness:
            title = await self._ensure_fresh_title(title, script_text,
                                                   reference=reference)
        if title:
            script_data["seo_title"] = title
            logger.info(f"SEO title: {title}")
            if self._last_title_pattern:
                variants["title_pattern"] = self._last_title_pattern
        if theme:
            logger.info(f"Emotional theme: {theme}")

        if NICHE.uses_story_memory:
            self._remember_hook(hook)

        await self._save_script(script_data)
        return script_data

    async def generate_scene_prompts(self, narration: str, n: int = 8) -> List[str]:
        """Ask the AI for n cinematic, symbolic scene descriptions that match the
        narration's flow — used as AI-image prompts so the visuals follow the story.

        Kept atmospheric/symbolic (no real names or faces) to avoid bad likenesses.
        Returns [] on failure so the caller falls back to generic scenes.
        """
        prompt = NICHE.scene_prompt.format(n=n, narration=narration[:1200])
        out = await self._call_ai(prompt)
        if not out:
            return []
        scenes = []
        for raw in out.splitlines():
            s = raw.strip().lstrip("0123456789.-)•*# ").strip().strip('"\'')
            if s and len(s) > 8:
                scenes.append(s[:200])
        return scenes[:n]

    async def summarize_story(self, text: str) -> str:
        """One-sentence summary of a generated story, for the record/ledger and to
        feed back into future prompts so the AI avoids repeating ideas."""
        prompt = (
            "Summarize this short story in ONE concise sentence (max 20 words) that "
            "captures its core idea and twist. Output only the sentence.\n\n"
            + (text or "")[:1500]
        )
        try:
            out = await self._call_ai(prompt)
            if out:
                s = out.strip().splitlines()[0].strip().strip('"').strip()
                if s:
                    return s[:200]
        except Exception:
            pass
        # Fallback: first sentence of the story itself
        first = (text or "").strip().split(".")[0].strip()
        return (first[:160] + "…") if first else "(summary unavailable)"

    # High-signal patterns for content that must never be published.
    _UNSAFE_PATTERNS = [
        r"suicid", r"kill(?:ing)?\s+(?:my|him|her|them|your)self",
        r"take\s+(?:my|his|her|your)\s+own\s+life", r"end\s+(?:my|his|her)\s+life",
        r"self[\s-]?harm", r"cut(?:ting)?\s+(?:my|him|her|your)self",
        r"hang(?:ing)?\s+(?:my|him|her|your)self", r"overdose", r"\bnoose\b",
        r"slit(?:ting)?\s+(?:my|his|her|their|your)\s+wrist",
        r"\brape\b", r"molest", r"child\s+(?:abuse|porn|sexual)",
        r"behead", r"dismember", r"disembowel",
    ]

    @classmethod
    def is_unsafe_story(cls, text: str) -> bool:
        """True if the story contains disallowed content (suicide/self-harm, sexual
        violence, child exploitation, extreme gore). Backup to the prompt rules."""
        t = (text or "").lower()
        for pat in cls._UNSAFE_PATTERNS:
            if re.search(pat, t):
                return True
        return False

    # ── Hook freshness ───────────────────────────────────────────────────
    # The opening line is the description's first line AND the retention
    # hook — and the model loves recycling imagery ("carrying the weight of
    # the world" shipped three times). Recent hooks are remembered on disk;
    # a new hook that shares too many content words with a recent one
    # triggers one regeneration with the offenders spelled out.

    _HOOK_HISTORY_FILE = DATA_DIR / "hook_history.json"
    _HOOK_KEEP = 30
    _HOOK_OVERLAP_THRESHOLD = 0.5

    _HOOK_STOPWORDS = frozenset(
        "the and for that this with have from they what when your just into "
        "then there their about would could were been will them than over "
        "some like you are not can its was his her had who all out one very "
        "ever feel feels feeling felt remember today tonight day says said "
        "has does most more".split())

    def _load_hook_history(self) -> list:
        return load_json(self._HOOK_HISTORY_FILE, [])

    def _remember_hook(self, hook: str):
        hook = (hook or "").strip()
        if not hook:
            return
        hist = self._load_hook_history()
        hist.append(hook)
        try:
            atomic_write_json(self._HOOK_HISTORY_FILE, hist[-self._HOOK_KEEP:])
        except OSError:
            pass

    @staticmethod
    def _extract_hook_line(sections: dict, full_text: str) -> str:
        """The spoken opening line: first non-empty line of the hook section,
        else the first sentence of the script."""
        hook = (sections.get("hook") or "").strip()
        if hook:
            for line in hook.splitlines():
                if line.strip():
                    return line.strip()
        first = re.split(r"(?<=[.!?])\s+", (full_text or "").strip())
        return first[0].strip() if first else ""

    @classmethod
    def _hook_words(cls, s: str) -> set:
        return {w for w in re.findall(r"[a-z']+", (s or "").lower())
                if len(w) > 2 and w not in cls._HOOK_STOPWORDS}

    def _similar_hooks(self, hook: str, recent: list) -> list:
        """Recent hooks whose content words overlap `hook` beyond the
        threshold (ratio against the smaller set, so short lines count)."""
        hw = self._hook_words(hook)
        if len(hw) < 3:
            return []
        out = []
        for old in recent[-self._HOOK_KEEP:]:
            ow = self._hook_words(old)
            if not ow:
                continue
            if len(hw & ow) / max(1, min(len(hw), len(ow))) >= self._HOOK_OVERLAP_THRESHOLD:
                out.append(old)
        return out

    # ── Title freshness ──────────────────────────────────────────────────
    # The model sometimes ignores "don't use overwhelmed" and has no memory
    # of yesterday's titles — so freshness is enforced in CODE: recent titles
    # are remembered on disk, duplicates/banned words trigger a retry, and a
    # mechanical fallback guarantees a fresh title even if the retry repeats.
    # The banned words and mechanical-formula word lists are niche data
    # (NICHE.banned_title_words / mech_title_leads / mech_title_feelings).

    _TITLE_HISTORY_FILE = DATA_DIR / "title_history.json"

    def _load_title_history(self) -> list:
        return load_json(self._TITLE_HISTORY_FILE, [])

    def _remember_title(self, title: str):
        hist = self._load_title_history()
        hist.append(title)
        try:
            atomic_write_json(self._TITLE_HISTORY_FILE, hist[-50:])
        except OSError:
            pass

    @staticmethod
    def _norm_title(t: str) -> str:
        t = t.lower().replace("#shorts", "")
        return re.sub(r"[^a-z0-9 ]", "", t).strip()

    # How far back the fatigue windows look. A lead word ("Peace ...") may
    # not repeat within the last 4 titles; a feeling ("... Burned Out") not
    # within the last 8. Exact duplicates are rejected against the whole
    # remembered window (40).
    _LEAD_COOLDOWN = 4
    _FEELING_COOLDOWN = 8

    @classmethod
    def _title_lead(cls, t: str) -> str:
        words = cls._norm_title(t).split()
        return words[0] if words else ""

    @classmethod
    def _title_feeling(cls, t: str) -> str:
        """The emotional tail of a formula title: whatever follows
        'you feel'/'you are', else the last two words."""
        norm = cls._norm_title(t)
        for marker in (" you feel ", " you are "):
            if marker in norm:
                return norm.split(marker, 1)[1].strip()
        words = norm.split()
        return " ".join(words[-2:]) if len(words) >= 2 else norm

    # A candidate whose normalized text matches a remembered title at or
    # above this SequenceMatcher ratio is a near-duplicate even if no
    # formula rule catches it ('Peace For When You Feel Alone' vs
    # 'Grace For When You Feel Alone').
    _FUZZY_DUP_THRESHOLD = 0.82

    def _title_problem(self, title: str, recent: list) -> str:
        """Return why a title is unacceptable, or '' if it's fine.

        Checks exact duplicates, fuzzy near-duplicates, AND formula fatigue:
        the channel's titles often share the '<Lead> For When You Feel
        <Feeling>' shape, so repeating the lead or the feeling within a
        short window reads as the same video even when the full title
        differs ('Peace For When You Feel X' five posts in a row).
        """
        import difflib
        low = title.lower()
        if any(w in low for w in NICHE.banned_title_words):
            return "banned word"
        norm = self._norm_title(title)
        for old in recent:
            old_norm = self._norm_title(old)
            if norm == old_norm:
                return "duplicate of a recent title"
            ratio = difflib.SequenceMatcher(None, norm, old_norm).ratio()
            if ratio >= self._FUZZY_DUP_THRESHOLD:
                return f"too similar to recent title {old!r} (ratio {ratio:.2f})"
        lead = self._title_lead(title)
        if lead and lead in {self._title_lead(t) for t in recent[-self._LEAD_COOLDOWN:]}:
            return f"lead word {lead!r} used within the last {self._LEAD_COOLDOWN} titles"
        feeling = self._title_feeling(title)
        if feeling and feeling in {self._title_feeling(t)
                                   for t in recent[-self._FEELING_COOLDOWN:]}:
            return f"feeling {feeling!r} used within the last {self._FEELING_COOLDOWN} titles"
        return ""

    async def _ensure_fresh_title(self, title: str, story_text: str,
                                  reference: str = "") -> str:
        """Enforce title freshness in CODE, not just in the prompt.

        Checks against the recent-title history; retries up to 3 times with
        the rejected candidates spelled out; then tries the mechanical
        formula builder; finally appends the verse reference to FORCE
        uniqueness so a repeat can never ship. The accepted title is
        remembered in data/title_history.json.
        """
        recent = self._load_title_history()
        rejected: list = []

        problem = self._title_problem(title, recent)
        attempts = 0
        while problem and attempts < 3:
            attempts += 1
            logger.info(f"Title rejected ({problem}): {title!r} — regenerating "
                        f"(attempt {attempts}/3)")
            rejected.append(title)
            retry = await self.generate_title(
                story_text, avoid_titles=(recent[-15:] + rejected),
                reference=reference)
            if not retry:
                break
            title = retry
            problem = self._title_problem(title, recent)

        if problem and NICHE.mech_title_leads:
            # Mechanical pass: build a fresh formula title from the niche's
            # lead/feeling word lists (the dup/fuzzy/cooldown checks steer it
            # away from everything recent).
            used = " ".join(self._norm_title(t) for t in recent)
            feelings = [f for f in NICHE.mech_title_feelings
                        if f.lower() not in used] or list(NICHE.mech_title_feelings)
            leads = list(NICHE.mech_title_leads)
            random.shuffle(leads)
            random.shuffle(feelings)
            for lead in leads:
                for feel in feelings:
                    verb = "Are" if feel in ("Tired Of Waiting",
                                             "Running On Empty") else "Feel"
                    candidate = f"{lead} For When You {verb} {feel}"
                    if not self._title_problem(candidate, recent):
                        logger.info(f"Title fixed mechanically: {candidate}")
                        title, problem = candidate, ""
                        break
                if not problem:
                    break

        if problem and reference:
            # Final guarantee: the verse reference makes any title unique
            # (references rotate through the whole pool before repeating).
            title = f"{title} — {reference}"[:90]
            logger.info(f"Title forced unique with verse reference: {title!r} "
                        f"(was: {problem})")

        self._remember_title(title)
        return title

    async def generate_title(self, story_text: str,
                             avoid_titles: Optional[list] = None,
                             reference: str = "") -> str:
        """Generate a YouTube Shorts title from the niche's title prompt.

        Niches with title_patterns rotate the title's STRUCTURE per video
        (question / verse-anchored / promise / ...), picked by
        weighted_variant so real view counts bias which shapes get used.
        The chosen pattern is stashed on self._last_title_pattern so
        generate() can record it with the video's variants.
        """
        if not NICHE.title_prompt:
            return ""
        avoid_block = (("Do NOT reuse any of these recent titles — and differ "
                        "in STRUCTURE from all of them, not just word choice: "
                        + "; ".join(avoid_titles) + "\n")
                       if avoid_titles else "")
        # {steer}: a rotating sample of the niche's steer feelings, so titles
        # don't cluster on one emotion. Only filled when the niche defines it.
        steer = ""
        if NICHE.title_steer_feelings:
            picks = random.sample(list(NICHE.title_steer_feelings), 4)
            steer = ", ".join(f"'{f}'" for f in picks)
        # Structural pattern rotation (performance-weighted).
        pattern_block = ""
        if NICHE.title_patterns:
            name = weighted_variant("title_pattern", list(NICHE.title_patterns))
            self._last_title_pattern = name
            pattern_block = NICHE.title_patterns[name].format(
                reference=reference or "the verse reference")
        prompt = NICHE.title_prompt.format(
            steer=steer, avoid_block=avoid_block, pattern_block=pattern_block,
            story=(story_text or "")[:800])
        cap = 90 if NICHE.title_patterns else 60  # room for verse refs; <=100 with #Shorts
        try:
            out = await self._call_ai(prompt)
            if out:
                t = out.strip().splitlines()[0].strip().strip('"').strip().rstrip(".")
                if t:
                    return t[:cap]
        except Exception:
            pass
        return ""

    async def generate_premises(self, mode: str, n: int = 3, avoid: list = None) -> List[str]:
        """Generate brand-new, original story premises (infinite content), steering
        away from anything in `avoid` (recent summaries) so it never repeats."""
        niche = get_niche(mode)
        genre = niche.premise_genre or "atmospheric horror"
        avoid_block = ""
        if avoid:
            avoid_block = ("\n\nDo NOT repeat or closely echo any of these already-used "
                           "ideas:\n- " + "\n- ".join(avoid[:25]))
        # Variety engine: niches with tech_angles force each premise onto a
        # DIFFERENT technology so premises can't cluster on one setting.
        tech_block = ""
        if niche.tech_angles:
            angles = random.sample(list(niche.tech_angles),
                                   min(max(n, 1), len(niche.tech_angles)))
            tech_block = ("\nBase each premise on a DIFFERENT one of these technologies "
                          "(one each, any order):\n- " + "\n- ".join(angles))
        prompt = (
            f"Generate {n} ORIGINAL, distinct one-sentence story premises for {genre} "
            f"YouTube Shorts.\n"
            "Each premise: a single vivid sentence with an unsettling hook, third person.\n"
            "Make them clearly different from each other.\n"
            f"Output exactly {n} lines, one premise per line. No numbering, no commentary."
            f"{tech_block}{avoid_block}"
        )
        try:
            out = await self._call_ai(prompt)
            if not out:
                return []
            premises = []
            for raw in out.splitlines():
                s = raw.strip().lstrip("0123456789.-)•*# ").strip().strip('"\'')
                if s and len(s) > 15:
                    premises.append(s[:200])
            return premises[:n]
        except Exception:
            return []

    def _build_prompt(self, content_item: Any, template: ScriptTemplate,
                      language: str) -> str:
        """Build the AI prompt for script generation from the niche's
        script_prompt template. The niche's rotation slots are filled with
        performance-weighted variants and recorded on self._last_variants."""
        title = getattr(content_item, 'title', 'Untitled Story')
        description = getattr(content_item, 'description', '')
        source_urls = getattr(content_item, 'source_urls', [])

        lang_instruction = ""
        if language == "es":
            lang_instruction = "Write the script in Spanish (Latin American)."
        elif language == "pt":
            lang_instruction = "Write the script in Portuguese (Brazilian)."

        # Fill the niche's rotation slots (hook_style/closing/ending_style/...)
        # with performance-weighted picks; record them for the analytics loop.
        chosen = {kind: weighted_variant(kind, list(options))
                  for kind, options in NICHE.rotations.items()}
        self._last_variants = dict(chosen)

        if NICHE.kind == "verse":
            # Verse niches: title is the reference, description the exact text.
            return NICHE.script_prompt.format(
                verse_text=description, reference=title,
                lang_instruction=lang_instruction, **chosen)

        if NICHE.kind == "story":
            # Past stories the channel has made — fed in so the AI avoids
            # repeating ideas.
            avoid_block = ""
            try:
                from discovery import STORY_HISTORY
                recents = STORY_HISTORY.recent_summaries(15)
                if recents:
                    avoid_block = ("\n\nALREADY MADE — do NOT reuse or closely echo any of "
                                   "these story ideas:\n- " + "\n- ".join(recents))
            except Exception:
                avoid_block = ""
            channel_style = _get_env("CHANNEL_STYLE", NICHE.channel_style_default)
            return NICHE.script_prompt.format(
                channel_style=channel_style, title=title,
                description=description[:400], avoid_block=avoid_block,
                lang_instruction=lang_instruction, **chosen)

        # News niches (soccer): topic + sources + category template structure.
        channel_style = _get_env("CHANNEL_STYLE", NICHE.channel_style_default)
        return NICHE.script_prompt.format(
            channel_style=channel_style, title=title,
            description=description[:500],
            sources=", ".join(source_urls[:3]),
            lang_instruction=lang_instruction,
            template_name=template.name,
            structure=", ".join(template.structure), **chosen)

    async def _call_ai(self, prompt: str,
                       max_tokens: Optional[int] = None) -> Optional[str]:
        """Call AI model (Grok, OpenAI, Anthropic, or local).

        max_tokens raises the output budget for long structured outputs
        (e.g. single-call JSON scripts); None keeps each provider's default.
        """
        # Try Grok (xAI) first if configured
        if config.api.xai_api_key:
            result = await self._call_grok(prompt, max_tokens)
            if result:
                return result

        # Try OpenAI
        if config.api.openai_api_key:
            result = await self._call_openai(prompt, max_tokens)
            if result:
                return result

        # Try Anthropic
        if config.api.anthropic_api_key:
            result = await self._call_anthropic(prompt, max_tokens)
            if result:
                return result

        # Try local model (Ollama)
        result = await self._call_local(prompt, max_tokens)
        if result:
            return result

        return None

    async def _call_grok(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call xAI Grok via its OpenAI-compatible chat completions endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": config.api.grok_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional soccer content creator writing YouTube Shorts scripts."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": max_tokens or 600,
                    "temperature": 0.8,
                }
                headers = {
                    "Authorization": f"Bearer {config.api.xai_api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.x.ai/v1/chat/completions",
                    json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=45)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    body = (await resp.text())[:200]
                    logger.warning(f"Grok returned {resp.status}: {body}")
                    return None
        except Exception as e:
            logger.error(f"Grok error: {e}")
            return None

    async def _call_openai(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call OpenAI API."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": "You are a professional soccer content creator writing YouTube Shorts scripts."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": max_tokens or 500,
                    "temperature": 0.8,
                }
                headers = {
                    "Authorization": f"Bearer {config.api.openai_api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    else:
                        logger.warning(f"OpenAI returned {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            return None

    async def _call_anthropic(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call Anthropic API (Claude Fable 5)."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": config.api.anthropic_model,
                    "max_tokens": max_tokens or 2000,
                    # Fable 5 always thinks; keep it shallow for this short
                    # script task so reasoning doesn't consume the token budget.
                    "output_config": {"effort": "low"},
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                }
                headers = {
                    "x-api-key": config.api.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Anthropic returned {resp.status}")
                        return None
                    data = await resp.json()
                    # Fable 5 safety classifiers can decline with HTTP 200 and
                    # stop_reason "refusal" (empty/partial content). Fall through
                    # to the next provider instead of indexing into content.
                    if data.get("stop_reason") == "refusal":
                        logger.warning("Anthropic (Fable 5) refused the request")
                        return None
                    # Pick the first non-empty text block — the response may also
                    # carry empty-text thinking blocks, so content[0] isn't safe.
                    for block in data.get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            return block["text"]
                    return None
        except Exception as e:
            logger.error(f"Anthropic error: {e}")
            return None

    async def _call_local(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call an Ollama model (local instance OR Ollama Cloud).

        Works with a plain local Ollama (no auth) and with Ollama Cloud /
        any auth-protected endpoint: if OLLAMA_API_KEY is set it's sent as a
        Bearer token, and LOCAL_MODEL_ENDPOINT can point at
        https://ollama.com/api/generate.
        """
        try:
            headers = {"Content-Type": "application/json"}
            if config.api.ollama_api_key:
                headers["Authorization"] = f"Bearer {config.api.ollama_api_key}"
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": config.api.local_model_name,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,   # keep reasoning models from "thinking" into the script
                    "options": {
                        "temperature": 0.8,
                        "num_predict": max_tokens or 500,
                    }
                }
                async with session.post(
                    config.api.local_model_endpoint,
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data.get("response", "") or ""
                        # Belt-and-suspenders: drop any leaked <think>...</think>
                        text = re.sub(r"<think>.*?</think>", "", text,
                                      flags=re.DOTALL | re.IGNORECASE).strip()
                        if not text:
                            # Reasoning models (qwen3.5 etc.) can burn the whole
                            # num_predict budget "thinking" and return HTTP 200
                            # with an empty response — surface it instead of
                            # failing silently. Fix: use a non-thinking model
                            # (e.g. gemma4:31b) as LOCAL_MODEL_NAME.
                            logger.warning(
                                "Ollama returned 200 but an EMPTY response%s — "
                                "if this is a reasoning model, its thinking may "
                                "have consumed the token budget; use a "
                                "non-thinking model as LOCAL_MODEL_NAME.",
                                " (thinking present)" if data.get("thinking") else "")
                            return None
                        return text
                    else:
                        body = (await resp.text())[:200]
                        logger.warning(f"Local/Ollama model returned {resp.status}: {body}")
                        return None
        except Exception as e:
            logger.error(f"Local model error: {e}")
            return None

    def _fallback_generate(self, content_item: Any, template: ScriptTemplate) -> str:
        """Template-based fallback when AI is unavailable (news modes only).

        Story/bible modes never reach this — generate() fails the item instead,
        because a canned identical story published repeatedly is worse than a
        skipped cycle.
        """
        title = getattr(content_item, 'title', 'Untitled Story')
        description = getattr(content_item, 'description', '')

        # Extract key info from description
        sentences = re.split(r'(?<=[.!?])\s+', description)
        key_facts = [s for s in sentences if len(s) > 20][:3]

        sections = []
        for section_name in template.structure:
            if section_name == "hook":
                text = template.hook_template.format(
                    fact=key_facts[0] if key_facts else "an incredible story",
                    topic=title,
                    event=title,
                    name=title,
                    number="5",
                    entity_a=title.split(" vs ")[0] if " vs " in title else title,
                    entity_b=title.split(" vs ")[1] if " vs " in title else "their rivals",
                )
            elif section_name == "call_to_action":
                text = template.cta_template.format(
                    entity_a=title.split(" vs ")[0] if " vs " in title else title,
                    entity_b=title.split(" vs ")[1] if " vs " in title else "their rivals",
                )
            elif section_name.startswith("item_"):
                idx = int(section_name.split("_")[1])
                if key_facts and idx <= len(key_facts):
                    text = f"Number {idx}: {key_facts[idx-1]}"
                else:
                    text = f"Number {idx}: Another incredible moment in soccer history."
            elif section_name == "side_a":
                text = f"On one side, we have {title.split(' vs ')[0] if ' vs ' in title else 'the first team'}."
            elif section_name == "side_b":
                text = f"On the other side, {title.split(' vs ')[1] if ' vs ' in title else 'their opponents'}."
            else:
                text = key_facts[0] if key_facts else f"Let's talk about {title}."
                if len(key_facts) > 1:
                    text = key_facts[1]

            sections.append(f"[{section_name.upper()}]\n{text}")

        return "\n\n".join(sections)

    def _parse_sections(self, text: str, template: ScriptTemplate) -> Dict[str, str]:
        """Parse AI output into named sections."""
        sections = {}
        current_section = None
        current_text = []

        for line in text.split("\n"):
            # Check for section markers
            section_match = re.match(r'\[(\w+)\]', line.strip().upper())
            if section_match:
                if current_section and current_text:
                    sections[current_section.lower()] = "\n".join(current_text).strip()
                current_section = section_match.group(1)
                current_text = []
            elif current_section:
                current_text.append(line)

        # Don't forget the last section
        if current_section and current_text:
            sections[current_section.lower()] = "\n".join(current_text).strip()

        # Ensure all expected sections exist
        for section in template.structure:
            if section not in sections:
                sections[section] = ""

        # Fail-safe: models sometimes return a perfectly good script with no
        # [SECTION] markers at all. Previously that discarded the ENTIRE
        # generation (every section empty -> whitespace full_text -> the video
        # failed at voiceover). Keep the work: treat the whole output as one
        # section instead.
        if text.strip() and not any(v.strip() for v in sections.values()):
            first = template.structure[0] if template.structure else "hook"
            sections[first] = text.strip()

        return sections

    def _estimate_duration(self, text: str) -> int:
        """Estimate spoken duration in seconds (average 150 words/min)."""
        words = len(text.split())
        return max(15, int(words / 150 * 60))

    async def _save_script(self, script_data: dict) -> None:
        """Save script to disk."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r'[^\w\s-]', '', script_data.get("title", "script"))[:50]
        safe_title = safe_title.strip().replace(" ", "_")
        filename = f"{timestamp}_{safe_title}.json"
        path = self.output_dir / filename
        try:
            with open(path, "w") as f:
                json.dump(script_data, f, indent=2)
            logger.info(f"Script saved: {path}")
        except OSError as e:
            logger.error(f"Failed to save script: {e}")

    async def generate_hook(self, title: str, category: str) -> str:
        """Generate just the hook (first 3 seconds)."""
        template = SCRIPT_TEMPLATES.get(
            config.categories.get(category, {}).get("script_format", "legendary"),
            SCRIPT_TEMPLATES["legendary"]
        )
        prompt = (
            f"Write a powerful 3-second hook for a YouTube Short about: {title}\n"
            f"Category: {category}\n"
            f"Style: {template.name}\n"
            f"The hook must grab attention immediately. Maximum 15 words."
        )
        result = await self._call_ai(prompt)
        if result:
            return result.strip()[:100]
        return template.hook_template.format(
            fact="an incredible story", topic=title, name=title,
            number="5", entity_a=title, entity_b="their rivals"
        )

    async def generate_cta(self, category: str) -> str:
        """Generate a call-to-action."""
        template = SCRIPT_TEMPLATES.get(
            config.categories.get(category, {}).get("script_format", "legendary"),
            SCRIPT_TEMPLATES["legendary"]
        )
        return template.cta_template


