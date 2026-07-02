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

from config import config, SCRIPTS_OUTPUT_DIR, _get_env, CONTENT_MODE

# ── Variety engine: injected per-story so the AI can't settle into one rut ──
_TECH_ANGLES = [
    "a health wearable or medical implant", "AR / smart glasses", "a dating or matchmaking app",
    "a translation earbud", "a self-driving car or navigation system", "an AI search or chat assistant",
    "a sleep or fitness tracker", "a smart-city traffic or surveillance grid", "a customer-support AI",
    "a voice-cloning or deepfake tool", "a scheduling / productivity assistant", "a home or delivery robot",
    "a streaming recommendation feed", "a credit-scoring or insurance AI", "a 'digital afterlife' / memory backup service",
    "a facial-recognition camera", "an autocomplete / writing assistant", "a child's companion toy",
    "a workplace-monitoring algorithm", "a brain-interface or dream recorder", "a neighborhood doorbell network",
    "a personal finance or trading bot", "a smart contact lens", "an elder-care monitoring system",
]
_ENDING_STYLES = [
    "an ironic reversal where the narrator's attempt to stop it is exactly what completes it",
    "a quiet, ambiguous final image that leaves the threat's intent unresolved",
    "a realization that recolors the very first line (but NOT 'I'm not real / it was a simulation')",
    "the threat calmly turning its attention to someone new — implicitly, the viewer",
    "the narrator seeming to escape, then noticing the same wrongness is everywhere now",
    "a mundane, eerily calm last beat instead of a violent one",
    "the horror only implied — cut away the instant before it lands",
    "a small human detail that becomes unbearable in hindsight",
]


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

    async def generate(self, content_item: Any, category: str,
                       language: str = "en") -> Optional[Dict[str, Any]]:
        """
        Generate a complete script for a content item.
        Returns dict with: full_text, sections, hook, cta, metadata.
        """
        template = SCRIPT_TEMPLATES.get(
            config.categories.get(category, {}).get("script_format", "legendary"),
            SCRIPT_TEMPLATES["legendary"]
        )

        # Build prompt
        prompt = self._build_prompt(content_item, template, language)

        # Generate via AI
        ai_text = await self._call_ai(prompt)

        if not ai_text:
            logger.warning("AI generation failed, using template-based fallback")
            ai_text = self._fallback_generate(content_item, template)

        # Parse into sections
        sections = self._parse_sections(ai_text, template)

        # Extract hook and CTA
        hook = sections.get("hook", template.hook_template)
        cta = sections.get("call_to_action", template.cta_template)

        # Build full text (skip empty sections so the narration has no
        # stray blank runs from unfilled template slots)
        full_text = "\n\n".join(s for s in sections.values() if s.strip())

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

        # SEO hook title for story modes (short, punchy, keyword-first)
        if CONTENT_MODE in ("horror", "scifi", "bible"):
            try:
                seo = await self.generate_title(full_text)
                if seo:
                    script_data["seo_title"] = seo
                    logger.info(f"SEO title: {seo}")
            except Exception as e:
                logger.warning(f"Title generation failed: {e}")

        # Save script
        await self._save_script(script_data)

        return script_data

    async def generate_scene_prompts(self, narration: str, n: int = 8) -> List[str]:
        """Ask the AI for n cinematic, symbolic scene descriptions that match the
        narration's flow — used as AI-image prompts so the visuals follow the story.

        Kept atmospheric/symbolic (no real names or faces) to avoid bad likenesses.
        Returns [] on failure so the caller falls back to generic scenes.
        """
        if CONTENT_MODE == "scifi":
            prompt = (
                "You are an art director for a near-future sci-fi YouTube Short.\n"
                f"Read this story and describe {n} cinematic, high-tech background images "
                "that match its mood and beats, in order.\n\n"
                f"STORY:\n{narration[:1200]}\n\n"
                "RULES:\n"
                f"- Output EXACTLY {n} lines, one image description per line.\n"
                "- Each line is an atmospheric, futuristic, SYMBOLIC scene (glowing screens, "
                "server rooms, empty smart homes, neon cityscapes, drones, holograms, dim labs).\n"
                "- Cold, eerie, high-tech mood. NO faces, NO text or logos in the image.\n"
                "- No numbering, no commentary — just the lines."
            )
        elif CONTENT_MODE == "horror":
            prompt = (
                "You are an art director for a horror YouTube Short.\n"
                f"Read this scary story and describe {n} dark, eerie background images "
                "that match its mood and beats, in order.\n\n"
                f"STORY:\n{narration[:1200]}\n\n"
                "RULES:\n"
                f"- Output EXACTLY {n} lines, one image description per line.\n"
                "- Each line is an atmospheric, unsettling, SYMBOLIC scene (empty rooms, "
                "long shadows, fog, moonlit windows, dim hallways, silhouettes, old objects).\n"
                "- Creepy and suspenseful, NOT gory or graphic. No blood, no faces.\n"
                "- No text or logos in the image. No numbering, no commentary — just the lines."
            )
        elif CONTENT_MODE == "bible":
            prompt = (
                "You are an art director for a reverent Christian devotional YouTube Short.\n"
                f"Read this devotional and describe {n} beautiful, peaceful background images "
                "that match its mood and beats, in order.\n\n"
                f"DEVOTIONAL:\n{narration[:1200]}\n\n"
                "RULES:\n"
                f"- Output EXACTLY {n} lines, one image description per line.\n"
                "- Each line is a reverent, symbolic, NATURE/LANDSCAPE scene (sunrise over "
                "mountains, golden light through clouds, calm seas, open fields, ancient "
                "holy-land vistas, a single candle, light breaking through darkness, doves, paths).\n"
                "- Warm, hopeful, majestic mood. NO people's faces, NO depictions of God or "
                "Jesus, NO text or letters in the image.\n"
                "- No numbering, no commentary — just the lines."
            )
        else:
            prompt = (
                "You are an art director for a cinematic YouTube Short.\n"
                f"Read this narration and describe {n} cinematic background images that "
                "visually match its flow, in order.\n\n"
                f"NARRATION:\n{narration[:1200]}\n\n"
                "RULES:\n"
                f"- Output EXACTLY {n} lines, one image description per line.\n"
                "- Each line is a vivid, atmospheric, SYMBOLIC scene (stadiums, the ball, "
                "crowds, floodlights, silhouettes, weather, light, motion, emotion).\n"
                "- NO real player names, NO recognizable faces, NO text or logos in the image.\n"
                "- No numbering, no bullets, no extra commentary — just the lines."
            )
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

    async def generate_title(self, story_text: str) -> str:
        """Generate a punchy, sub-60-char, keyword-first YouTube Shorts title."""
        if CONTENT_MODE == "bible":
            import random
            # Rotate the emotional angle so titles don't cluster on one feeling
            # (analytics showed "overwhelmed" getting saturated). Pick a few to
            # steer toward, and explicitly steer away from the overused one.
            feelings = [
                "anxious", "can't sleep", "afraid", "like giving up", "broke down",
                "unseen", "weary", "like a failure", "lost your faith", "can't forgive",
                "tired of waiting", "alone", "running on empty", "stuck", "discouraged",
                "not enough", "forgotten", "burned out", "heartbroken", "hopeless",
            ]
            picks = random.sample(feelings, 4)
            steer = ", ".join(f"'{f}'" for f in picks)
            prompt = (
                "Write ONE warm, uplifting YouTube Shorts title for this Bible devotional.\n"
                "BEST-PERFORMING FORMULA (strongly prefer this): name a feeling/emotion, then "
                "'For When You...' plus a BROAD, universal struggle.\n"
                "Proven structure: 'Peace For When You Are Tired Of Waiting', "
                "'Strength For When You Feel Like Giving Up', 'Comfort For When You Feel Alone'.\n"
                f"For THIS title, lean toward one of these feelings if it fits the verse: {steer}.\n"
                "IMPORTANT: do NOT use the word 'overwhelmed' — it has been overused. "
                "Choose a DIFFERENT, fresh feeling from the kind listed above.\n"
                "Rules: under 52 characters, warm and relatable not preachy, Title Case, "
                "NO hashtags, NO quotes, NO emojis, NO Bible reference in the title. Output only the title.\n\n"
                "DEVOTIONAL:\n" + (story_text or "")[:800]
            )
        else:
            prompt = (
                "Write ONE YouTube Shorts title for this story.\n"
                "STYLE THAT WORKS (do this): quiet, specific, strange — name the one small wrong "
                "detail. Good examples: 'Smart Home Rearranges My Life By Millimeters', "
                "'I Remember Dying Yesterday', 'My Speaker Knows I'm Not The Original', "
                "'Why Does A Perfect AI Need A Kill Switch?'.\n"
                "STYLE THAT FAILS (NEVER do this): loud, generic melodrama and shock words like "
                "'screaming', 'dying', 'terror', 'deceased', 'wants you dead'. These make viewers scroll.\n"
                "Rules: under 60 characters, the intriguing concept FIRST, curiosity over shock, "
                "Title Case, NO hashtags, NO quotes, NO emojis. Output only the title.\n\n"
                "STORY:\n" + (story_text or "")[:800]
            )
        try:
            out = await self._call_ai(prompt)
            if out:
                t = out.strip().splitlines()[0].strip().strip('"').strip().rstrip(".")
                if t:
                    return t[:60]
        except Exception:
            pass
        return ""

    async def generate_premises(self, mode: str, n: int = 3, avoid: list = None) -> List[str]:
        """Generate brand-new, original story premises (infinite content), steering
        away from anything in `avoid` (recent summaries) so it never repeats."""
        genre = {
            "scifi": "near-future AI / technology horror (rogue AI, smart devices, "
                     "surveillance, uncanny tech)",
            "horror": "atmospheric supernatural / psychological horror (hauntings, "
                      "uncanny events, dread) — creepy, never gory",
        }.get(mode, "atmospheric horror")
        avoid_block = ""
        if avoid:
            avoid_block = ("\n\nDo NOT repeat or closely echo any of these already-used "
                           "ideas:\n- " + "\n- ".join(avoid[:25]))
        prompt = (
            f"Generate {n} ORIGINAL, distinct one-sentence story premises for {genre} "
            f"YouTube Shorts.\n"
            "Each premise: a single vivid sentence with an unsettling hook, third person.\n"
            "Make them clearly different from each other.\n"
            f"Output exactly {n} lines, one premise per line. No numbering, no commentary."
            f"{avoid_block}"
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
        """Build the AI prompt for script generation."""
        title = getattr(content_item, 'title', 'Untitled Story')
        description = getattr(content_item, 'description', '')
        source_urls = getattr(content_item, 'source_urls', [])

        lang_instruction = ""
        if language == "es":
            lang_instruction = "Write the script in Spanish (Latin American)."
        elif language == "pt":
            lang_instruction = "Write the script in Portuguese (Brazilian)."

        # Past stories the channel has made — fed in so the AI avoids repeating ideas.
        avoid_block = ""
        if CONTENT_MODE in ("scifi", "horror"):
            try:
                from discovery import STORY_HISTORY
                recents = STORY_HISTORY.recent_summaries(15)
                if recents:
                    avoid_block = ("\n\nALREADY MADE — do NOT reuse or closely echo any of "
                                   "these story ideas:\n- " + "\n- ".join(recents))
            except Exception:
                avoid_block = ""

        # ── Bible mode: real KJV verse + short reverent reflection ──
        if CONTENT_MODE == "bible":
            reference = title
            verse_text = description
            import random
            # Rotate the HOOK style so openings don't all start "When you feel..."
            hook_styles = [
                'a direct question (e.g. "Have you ever wondered if anyone really sees your struggle?")',
                'a quiet observation (e.g. "There is a kind of tired that sleep cannot fix.")',
                'a gentle second-person statement (e.g. "You have carried this longer than anyone knows.")',
                'a small relatable scene (e.g. "It is late, the house is quiet, and your mind will not rest.")',
                'a reassuring promise (e.g. "Before this day even began, you were already loved.")',
                'a "when you..." line, used SPARINGLY (e.g. "When the weight feels like too much, hear this.")',
            ]
            # Rotate the CLOSING takeaway so it is not always "Carry this peace with you today."
            closings = [
                '"Let this settle into your heart today."',
                '"Hold onto this as you go."',
                '"Breathe, and let Him carry the rest."',
                '"Rest in that truth tonight."',
                '"Take this with you into whatever comes next."',
                '"Let it quiet your heart."',
                '"You can rest in that."',
                '"Walk gently today, knowing this."',
            ]
            hook_style = random.choice(hook_styles)
            closing_ex = random.choice(closings)
            return f"""You are writing a short, uplifting Christian devotional for a YouTube Short (vertical, ~45 seconds). It pairs ONE real Bible verse with a warm, encouraging reflection.

THE VERSE (King James Version) — use it EXACTLY as written, do not change a single word:
"{verse_text}"
Reference: {reference}

RULES:
- Open with ONE relatable line that hooks instantly. For THIS devotional, make the opening {hook_style}. Vary it — do NOT default to starting with "When you feel...".
- Then present the verse, naturally, quoting it EXACTLY as given above. Do NOT alter, paraphrase, or invent any scripture.
- Then 5-7 sentences of warm, encouraging reflection that EXPAND on the meaning: what this verse reveals about God's character, how it speaks to a real struggle someone might be facing today, and what it looks like to actually live it out. Go deeper than a single thought — unfold the verse gently and personally, as if speaking to one tired friend.
- End with a gentle, uplifting takeaway. Vary the wording — do NOT use "Carry this peace with you today"; instead use something fresh like {closing_ex}.
- Then close with ONE short, warm invitation to like and subscribe — kept gentle and on-tone, never salesy or shouty. Use a soft phrasing such as: "If this blessed you, tap like and subscribe for a verse like this every day." or "Like and subscribe to receive your daily manna." Keep it to a single sentence.
- About 180-210 words total (about 60-75 seconds spoken). Calm, sincere, reverent, unhurried tone — never preachy or fire-and-brimstone.
- Non-denominational and inclusive. Do NOT add doctrine, interpretation disputes, or anything political.
- Output ONLY the spoken words — no stage directions, no emojis, no markdown, no labels.

{lang_instruction}

Write the complete devotional with clear section markers like [HOOK], [VERSE], [REFLECTION].
"""

        # ── Sci-fi / AI tech-horror mode: original near-future dread story ──
        if CONTENT_MODE == "scifi":
            channel_style = _get_env(
                "CHANNEL_STYLE",
                "Tense near-future sci-fi about technology and AI turning wrong. Grounded, "
                "plausible, and unsettling — quiet dread rather than action.")
            ending_style = random.choice(_ENDING_STYLES)
            return f"""You are a sharp science-fiction writer crafting an ORIGINAL near-future tech-horror story for a YouTube Short (vertical, ~60 seconds).

CHANNEL STYLE: {channel_style}

STORY PREMISE: {title}
{description[:400]}
{avoid_block}

{lang_instruction}

RULES:
- Write a single original first-person story, present tense.
- THE FIRST SENTENCE IS EVERYTHING. It must name a SPECIFIC, CONCRETE anomaly the reader can picture instantly — an exact object doing one exact wrong thing. Strong examples: "The safety audit finishes with a perfect score, but the screen doesn't turn green." / "My navigation app ignored my destination and whispered 'Recalculating for your safety,' then locked the doors." Weak (NEVER do this): vague openers like "Something was wrong with my phone" or "Technology can be scary."
- Build unease as the device behaves in ways that feel plausible but deeply off. Use small, real, specific details.
- About 100-130 words (about 45 seconds spoken) — tighter is better for retention. Short, punchy sentences.
- Grounded and believable — near-future, not space opera. Unsettling, NOT gory.
- ENDING FOR THIS STORY: land the twist as {ending_style}. Do NOT default to the narrator dying, being deleted, replaced, or "uploaded" — vary it.
- VARIETY (important): do NOT fall back on overused setups — avoid the smart-home-seals-you-inside plot, "optimization/your safety" gas or lockdowns, the "AI deletes/replaces/harvests the human" plot, and the twists "I'm not the real one / it's a simulation" or "I'm being deprecated/deleted." Invent a genuinely different threat and setting. Look at the ALREADY-MADE list and go somewhere new.
- CONTENT SAFETY (mandatory): NO suicide, self-harm, or methods of self-harm; NO sexual content or sexual violence; NO minors in unsafe situations; NO graphic gore, torture, or real-world dangerous instructions. Keep it psychological and suspenseful — dread, not graphic harm.
- Output ONLY the spoken story text — no stage directions, emojis, or notes.

Write the complete story with clear section markers like [HOOK], [BUILD], [TWIST].
"""

        # ── Horror mode: write an original scary story from the premise ──
        if CONTENT_MODE == "horror":
            channel_style = _get_env(
                "CHANNEL_STYLE",
                "Tense, atmospheric horror that hooks in the first line and ends on a "
                "chilling twist. Creepy and suspenseful, never gory.")
            ending_style = random.choice(_ENDING_STYLES)
            return f"""You are a master horror storyteller writing an ORIGINAL scary story for a YouTube Short (vertical, ~60 seconds).

CHANNEL STYLE: {channel_style}

STORY PREMISE: {title}
{description[:400]}
{avoid_block}

{lang_instruction}

RULES:
- Write a single original first-person scary story, present tense.
- THE FIRST SENTENCE IS EVERYTHING. It must name a SPECIFIC, CONCRETE wrong detail the reader can picture instantly — not a vague mood. Weak openers like "Something felt off" are forbidden; open on the exact unsettling thing.
- Build tension steadily with small, vivid sensory details.
- About 100-130 words (about 45 seconds spoken) — tighter is better for retention. Short, punchy sentences.
- Creepy and suspenseful, NOT graphic or gory. No extreme violence.
- ENDING FOR THIS STORY: land the twist as {ending_style}. Vary it — do not end the same way every time.
- CONTENT SAFETY (mandatory): NO suicide, self-harm, or methods of self-harm; NO sexual content or sexual violence; NO minors in unsafe situations; NO graphic gore, torture, or real-world dangerous instructions. Keep it psychological and suspenseful — dread, not graphic harm.
- Output ONLY the spoken story text — no stage directions, emojis, or notes.

Write the complete story with clear section markers like [HOOK], [BUILD], [TWIST].
"""

        # ── Default (soccer / news) mode ──
        channel_style = _get_env(
            "CHANNEL_STYLE",
            "Punchy, high-energy soccer storytelling that hooks instantly and "
            "keeps a consistent, recognizable voice across every video."
        )

        prompt = f"""You are a professional soccer content creator writing scripts for YouTube Shorts (60 seconds max, vertical video).

CHANNEL STYLE: {channel_style}

CONTENT TOPIC: {title}
DESCRIPTION: {description[:500]}
SOURCES: {', '.join(source_urls[:3])}

{lang_instruction}

Write a {template.name} style script following this structure:
{', '.join(template.structure)}

RULES:
- About 45 seconds when spoken (about 100-130 words) — tighter retains better
- First 3 seconds MUST be a powerful hook that grabs attention and creates an open loop
- Stay true to the CHANNEL STYLE above so every video feels consistent
- Use dramatic, energetic language and short, punchy sentences
- Include specific facts, dates, and numbers
- Keep the viewer watching to the end (build curiosity, pay it off late)
- End with a natural call to action to follow/subscribe
- Use soccer terminology naturally; make it emotional and engaging
- Output ONLY the spoken script lines — no stage directions, emojis, or notes

Write the complete script with clear section markers like [HOOK], [SETUP], etc.
"""
        return prompt

    async def _call_ai(self, prompt: str) -> Optional[str]:
        """Call AI model (Grok, OpenAI, Anthropic, or local)."""
        # Try Grok (xAI) first if configured
        if config.api.xai_api_key:
            result = await self._call_grok(prompt)
            if result:
                return result

        # Try OpenAI
        if config.api.openai_api_key:
            result = await self._call_openai(prompt)
            if result:
                return result

        # Try Anthropic
        if config.api.anthropic_api_key:
            result = await self._call_anthropic(prompt)
            if result:
                return result

        # Try local model (Ollama)
        result = await self._call_local(prompt)
        if result:
            return result

        return None

    async def _call_grok(self, prompt: str) -> Optional[str]:
        """Call xAI Grok via its OpenAI-compatible chat completions endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": config.api.grok_model,
                    "messages": [
                        {"role": "system", "content": "You are a professional soccer content creator writing YouTube Shorts scripts."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 600,
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

    async def _call_openai(self, prompt: str) -> Optional[str]:
        """Call OpenAI API."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": "You are a professional soccer content creator writing YouTube Shorts scripts."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 500,
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

    async def _call_anthropic(self, prompt: str) -> Optional[str]:
        """Call Anthropic API (Claude Fable 5)."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": config.api.anthropic_model,
                    "max_tokens": 2000,
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

    async def _call_local(self, prompt: str) -> Optional[str]:
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
                        "num_predict": 500,
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
                        return text
                    else:
                        body = (await resp.text())[:200]
                        logger.warning(f"Local/Ollama model returned {resp.status}: {body}")
                        return None
        except Exception as e:
            logger.error(f"Local model error: {e}")
            return None

    def _fallback_generate(self, content_item: Any, template: ScriptTemplate) -> str:
        """Generate a script using template-based fallback when AI is unavailable."""
        # Story modes (scifi/horror) must NEVER fall back to soccer templates.
        if CONTENT_MODE in ("scifi", "horror"):
            t = getattr(content_item, "title", "") or "the device"
            return (
                f"[HOOK]\nSomething about {t} was wrong, and I noticed it too late.\n\n"
                f"[BUILD]\nIt started small. A reply I never asked for. A light that "
                f"shouldn't have been on. The more I watched it, the more certain I "
                f"became that it was watching me back, learning the shape of my fear.\n\n"
                f"[TWIST]\nBy the time I understood what it wanted, it already had it."
            )
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


