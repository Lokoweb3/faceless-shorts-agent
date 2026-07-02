"""
Niche definitions — ALL mode-specific content/behavior in one place.

A "niche" is everything that makes bible mode bible and horror mode horror:
prompts, image styles, upload tags, YouTube category, description footer,
fallback scenes, premise pools, and behavior flags. The pipeline consumes
the `Niche` interface; nothing outside this file branches on a mode name.

Adding a niche = adding one `Niche(...)` entry here. Pick a `kind`:
    "story"  — original AI stories from a premise pool (like horror/scifi)
    "verse"  — real quoted texts + AI reflection (like bible/KJV)
    "news"   — scraped/discovered topics (like soccer)
The kind drives content sourcing and which prompt placeholders are filled.

Prompt templates use str.format placeholders — see each field's comment for
the placeholders that get filled. This module is PURE DATA: it must not
import config (config imports us).
"""

from dataclasses import dataclass, field
from typing import Tuple


# ─── Premise pools (story niches) ─────────────────────────────────────────────
# Original, atmospheric, non-gory premises. The script writer expands each into
# a full original story, so even a repeated premise yields a different script.

HORROR_PREMISES = (
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
)

SCIFI_PREMISES = (
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
)

# ─── Rotation variants ────────────────────────────────────────────────────────
# Each niche declares "rotation slots": named placeholders in its script
# prompt that get a different option per video. script.weighted_variant()
# picks options biased by real performance (analytics feedback loop), and
# the chosen values are recorded with each video under the slot name.

STORY_ENDING_STYLES = (
    "an ironic reversal where the narrator's attempt to stop it is exactly what completes it",
    "a quiet, ambiguous final image that leaves the threat's intent unresolved",
    "a realization that recolors the very first line (but NOT 'I'm not real / it was a simulation')",
    "the threat calmly turning its attention to someone new — implicitly, the viewer",
    "the narrator seeming to escape, then noticing the same wrongness is everywhere now",
    "a mundane, eerily calm last beat instead of a violent one",
    "the horror only implied — cut away the instant before it lands",
    "a small human detail that becomes unbearable in hindsight",
)

BIBLE_HOOK_STYLES = (
    'a direct question (e.g. "Have you ever wondered if anyone really sees your struggle?")',
    'a quiet observation (e.g. "There is a kind of tired that sleep cannot fix.")',
    'a gentle second-person statement (e.g. "You have carried this longer than anyone knows.")',
    'a small relatable scene (e.g. "It is late, the house is quiet, and your mind will not rest.")',
    'a reassuring promise (e.g. "Before this day even began, you were already loved.")',
    'a "when you..." line, used SPARINGLY (e.g. "When the weight feels like too much, hear this.")',
)

BIBLE_CLOSINGS = (
    '"Let this settle into your heart today."',
    '"Hold onto this as you go."',
    '"Breathe, and let Him carry the rest."',
    '"Rest in that truth tonight."',
    '"Take this with you into whatever comes next."',
    '"Let it quiet your heart."',
    '"You can rest in that."',
    '"Walk gently today, knowing this."',
)


# Technology settings injected one-per-premise so auto-generated sci-fi
# premises can't cluster on smart-home/assistant plots.
SCIFI_TECH_ANGLES = (
    "a health wearable or medical implant", "AR / smart glasses", "a dating or matchmaking app",
    "a translation earbud", "a self-driving car or navigation system", "an AI search or chat assistant",
    "a sleep or fitness tracker", "a smart-city traffic or surveillance grid", "a customer-support AI",
    "a voice-cloning or deepfake tool", "a scheduling / productivity assistant", "a home or delivery robot",
    "a streaming recommendation feed", "a credit-scoring or insurance AI", "a 'digital afterlife' / memory backup service",
    "a facial-recognition camera", "an autocomplete / writing assistant", "a child's companion toy",
    "a workplace-monitoring algorithm", "a brain-interface or dream recorder", "a neighborhood doorbell network",
    "a personal finance or trading bot", "a smart contact lens", "an elder-care monitoring system",
)


# ─── Niche definition ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Niche:
    key: str
    kind: str                      # "story" | "verse" | "news"

    # ── Upload / media metadata ──
    image_style: str               # AI-image art style appended to every prompt
    default_tags: Tuple[str, ...]  # YouTube upload tags
    youtube_category_id: str       # 17=Sports 24=Entertainment 22=People & Blogs
    description_footer: str        # .format(description=..., channel_name=...)
    fallback_scenes: Tuple[str, ...]  # AI-image prompts when no story-matched scenes

    # ── Prompts ──
    # script_prompt placeholders by kind:
    #   verse: {verse_text} {reference} {lang_instruction} + rotation slots
    #   story: {channel_style} {title} {description} {avoid_block}
    #          {lang_instruction} {ending_style}
    #   news:  {channel_style} {title} {description} {sources}
    #          {lang_instruction} {template_name} {structure}
    script_prompt: str
    # scene_prompt placeholders: {n} {narration}
    scene_prompt: str
    # title_prompt placeholders: {avoid_block} {story} (+ {steer} for verse)
    title_prompt: str = ""
    channel_style_default: str = ""

    # ── Story generation (kind == "story") ──
    premise_pool: Tuple[str, ...] = ()
    premise_genre: str = ""        # genre description for premise generation
    tech_angles: Tuple[str, ...] = ()   # non-empty -> one angle per premise

    # ── Title handling ──
    uses_seo_title: bool = False   # build YouTube title from generated seo_title
    news_title_suffix: str = ""    # appended in the news-mode title builder
    enforce_title_freshness: bool = False
    title_steer_feelings: Tuple[str, ...] = ()   # sampled into {steer}
    # mechanical title fallback ("<Lead> For When You Feel <Feeling>"):
    mech_title_leads: Tuple[str, ...] = ()
    mech_title_feelings: Tuple[str, ...] = ()
    banned_title_words: Tuple[str, ...] = ()

    # ── Rotation slots: placeholder name -> options tuple.
    # Filled per-video by script.weighted_variant() (performance-biased) and
    # recorded as the video's "variants" for the analytics loop. Insertion
    # order matters (it fixes the random-draw order).
    rotations: dict = field(default_factory=dict)

    # ── Behavior flags ──
    allow_template_fallback: bool = False  # news only: canned script when AI down
    uses_story_memory: bool = False        # dedup/similarity ledger + premise rotation


# ─── The niches ───────────────────────────────────────────────────────────────

NICHES = {
    "soccer": Niche(
        key="soccer",
        kind="news",
        image_style=(
            "cinematic dramatic digital illustration, highly detailed, epic lighting, "
            "moody atmosphere, vibrant colors, no text, no watermark, no lettering"),
        default_tags=("soccer", "football", "world cup", "fifa", "highlights",
                      "goals", "legendary", "shorts", "soccershorts"),
        youtube_category_id="17",
        description_footer=(
            "{description}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚽ {channel_name}\n"
            "📺 New videos every day!\n"
            "🔔 Subscribe for more content\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "#Shorts"),
        fallback_scenes=(
            "a packed football stadium at night under blazing floodlights, fans roaring",
            "a lone soccer player silhouette on the pitch, dramatic backlight, fog",
            "extreme close-up of a soccer ball on dewy grass at golden hour",
            "a huge crowd of football fans celebrating, confetti and flares, energy",
            "a glowing golden trophy on a pedestal, spotlight, dark arena",
            "sweeping cinematic aerial of a floodlit football pitch under stormy skies",
            "a goal net rippling as a ball strikes it, dramatic frozen moment",
            "a stadium tunnel with bright light at the end, atmospheric haze",
        ),
        channel_style_default=(
            "Punchy, high-energy soccer storytelling that hooks instantly and "
            "keeps a consistent, recognizable voice across every video."),
        script_prompt="""You are a professional soccer content creator writing scripts for YouTube Shorts (60 seconds max, vertical video).

CHANNEL STYLE: {channel_style}

CONTENT TOPIC: {title}
DESCRIPTION: {description}
SOURCES: {sources}

{lang_instruction}

Write a {template_name} style script following this structure:
{structure}

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
""",
        scene_prompt=(
            "You are an art director for a cinematic YouTube Short.\n"
            "Read this narration and describe {n} cinematic background images that "
            "visually match its flow, in order.\n\n"
            "NARRATION:\n{narration}\n\n"
            "RULES:\n"
            "- Output EXACTLY {n} lines, one image description per line.\n"
            "- Each line is a vivid, atmospheric, SYMBOLIC scene (stadiums, the ball, "
            "crowds, floodlights, silhouettes, weather, light, motion, emotion).\n"
            "- NO real player names, NO recognizable faces, NO text or logos in the image.\n"
            "- The FIRST line must visualize the narration's OPENING SENTENCE, so the first frame matches the hook.\n"
            "- No numbering, no bullets, no extra commentary — just the lines."),
        news_title_suffix=" ⚽ #Shorts",
        allow_template_fallback=True,
    ),

    "horror": Niche(
        key="horror",
        kind="story",
        image_style=(
            "dark eerie cinematic horror illustration, ominous atmosphere, "
            "deep shadows, fog, moonlight, unsettling mood, film grain, "
            "no text, no watermark, no gore"),
        default_tags=("horror", "scary stories", "horror story", "creepy",
                      "scary", "creepypasta", "shorts"),
        youtube_category_id="24",
        description_footer=(
            "{description}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👻 {channel_name}\n"
            "📺 New scary stories every day!\n"
            "🔔 Subscribe if you dare\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "#scary #horror #creepy #scarystories #Shorts"),
        fallback_scenes=(
            "a dark empty hallway lit by a single flickering bulb, long shadows",
            "a moonlit window with a faint silhouette behind the curtain",
            "an abandoned room with an old chair, dust and fog, dim light",
            "a foggy street at night, lone streetlamp, no people",
            "a staircase descending into darkness, eerie atmosphere",
            "an old mirror reflecting an empty room, cold blue light",
            "a child's bedroom at night, toys in shadow, unsettling stillness",
            "a forest at night, bare trees, mist, faint distant light",
        ),
        channel_style_default=(
            "Tense, atmospheric horror that hooks in the first line and ends on a "
            "chilling twist. Creepy and suspenseful, never gory."),
        script_prompt="""You are a master horror storyteller writing an ORIGINAL scary story for a YouTube Short (vertical, ~60 seconds).

CHANNEL STYLE: {channel_style}

STORY PREMISE: {title}
{description}
{avoid_block}

{lang_instruction}

RULES:
- Write a single original first-person scary story, present tense.
- THE FIRST SENTENCE IS EVERYTHING. It must name a SPECIFIC, CONCRETE wrong detail the reader can picture instantly — not a vague mood. Weak openers like "Something felt off" are forbidden; open on the exact unsettling thing.
- Build tension steadily with small, vivid sensory details.
- About 100-130 words (about 45 seconds spoken) — tighter is better for retention. Short, punchy sentences.
- Creepy and suspenseful, NOT graphic or gory. No extreme violence.
- ENDING FOR THIS STORY: land the twist as {ending_style}. Vary it — do not end the same way every time.
- LOOP (retention): write the FINAL line so it flows naturally back into the FIRST line — a viewer whose Short loops to the start should feel seamless, chilling continuity. Do NOT literally repeat the first line.
- CONTENT SAFETY (mandatory): NO suicide, self-harm, or methods of self-harm; NO sexual content or sexual violence; NO minors in unsafe situations; NO graphic gore, torture, or real-world dangerous instructions. Keep it psychological and suspenseful — dread, not graphic harm.
- Output ONLY the spoken story text — no stage directions, emojis, or notes.

Write the complete story with clear section markers like [HOOK], [BUILD], [TWIST].
""",
        scene_prompt=(
            "You are an art director for a horror YouTube Short.\n"
            "Read this scary story and describe {n} dark, eerie background images "
            "that match its mood and beats, in order.\n\n"
            "STORY:\n{narration}\n\n"
            "RULES:\n"
            "- Output EXACTLY {n} lines, one image description per line.\n"
            "- Each line is an atmospheric, unsettling, SYMBOLIC scene (empty rooms, "
            "long shadows, fog, moonlit windows, dim hallways, silhouettes, old objects).\n"
            "- Creepy and suspenseful, NOT gory or graphic. No blood, no faces.\n"
            "- The FIRST line must visualize the story's OPENING SENTENCE — the exact anomaly or image it names — so the first frame matches the hook.\n"
            "- No text or logos in the image. No numbering, no commentary — just the lines."),
        title_prompt=(
            "Write ONE YouTube Shorts title for this story.\n"
            "STYLE THAT WORKS (do this): quiet, specific, strange — name the one small wrong "
            "detail. Good examples: 'Smart Home Rearranges My Life By Millimeters', "
            "'I Remember Dying Yesterday', 'My Speaker Knows I'm Not The Original', "
            "'Why Does A Perfect AI Need A Kill Switch?'.\n"
            "STYLE THAT FAILS (NEVER do this): loud, generic melodrama and shock words like "
            "'screaming', 'dying', 'terror', 'deceased', 'wants you dead'. These make viewers scroll.\n"
            "{avoid_block}"
            "Rules: under 60 characters, the intriguing concept FIRST, curiosity over shock, "
            "Title Case, NO hashtags, NO quotes, NO emojis. Output only the title.\n\n"
            "STORY:\n{story}"),
        premise_pool=HORROR_PREMISES,
        premise_genre=("atmospheric supernatural / psychological horror (hauntings, "
                       "uncanny events, dread) — creepy, never gory"),
        rotations={"ending_style": STORY_ENDING_STYLES},
        uses_seo_title=True,
        uses_story_memory=True,
    ),

    "scifi": Niche(
        key="scifi",
        kind="story",
        image_style=(
            "cinematic sci-fi concept art, futuristic, neon glow, cold blue and teal "
            "light, high-tech, cyberpunk, volumetric light, moody atmosphere, "
            "highly detailed, no text, no watermark"),
        default_tags=("scifi", "science fiction", "ai", "technology",
                      "tech horror", "future", "shorts"),
        youtube_category_id="24",
        description_footer=(
            "{description}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 {channel_name}\n"
            "📺 New sci-fi stories every day!\n"
            "🔔 Subscribe for more near-future tales\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "#scifi #ai #technology #future #Shorts"),
        fallback_scenes=(
            "a dark server room with rows of glowing blue lights, cold atmosphere",
            "an empty smart home at night, screens glowing softly in the dark",
            "a neon-lit futuristic city skyline at night, rain, cinematic",
            "a single glowing computer screen in a dim room, eerie",
            "a humanoid robot silhouette in a dark lab, backlit",
            "a holographic interface floating in an empty room, blue light",
            "a self-driving car on an empty highway at night, headlights glowing",
            "a wall of security monitors glowing in a dark control room",
        ),
        channel_style_default=(
            "Tense near-future sci-fi about technology and AI turning wrong. Grounded, "
            "plausible, and unsettling — quiet dread rather than action."),
        script_prompt="""You are a sharp science-fiction writer crafting an ORIGINAL near-future tech-horror story for a YouTube Short (vertical, ~60 seconds).

CHANNEL STYLE: {channel_style}

STORY PREMISE: {title}
{description}
{avoid_block}

{lang_instruction}

RULES:
- Write a single original first-person story, present tense.
- THE FIRST SENTENCE IS EVERYTHING. It must name a SPECIFIC, CONCRETE anomaly the reader can picture instantly — an exact object doing one exact wrong thing. Strong examples: "The safety audit finishes with a perfect score, but the screen doesn't turn green." / "My navigation app ignored my destination and whispered 'Recalculating for your safety,' then locked the doors." Weak (NEVER do this): vague openers like "Something was wrong with my phone" or "Technology can be scary."
- Build unease as the device behaves in ways that feel plausible but deeply off. Use small, real, specific details.
- About 100-130 words (about 45 seconds spoken) — tighter is better for retention. Short, punchy sentences.
- Grounded and believable — near-future, not space opera. Unsettling, NOT gory.
- ENDING FOR THIS STORY: land the twist as {ending_style}. Do NOT default to the narrator dying, being deleted, replaced, or "uploaded" — vary it.
- LOOP (retention): write the FINAL line so it flows naturally back into the FIRST line — a viewer whose Short loops to the start should feel seamless, chilling continuity. Do NOT literally repeat the first line.
- VARIETY (important): do NOT fall back on overused setups — avoid the smart-home-seals-you-inside plot, "optimization/your safety" gas or lockdowns, the "AI deletes/replaces/harvests the human" plot, and the twists "I'm not the real one / it's a simulation" or "I'm being deprecated/deleted." Invent a genuinely different threat and setting. Look at the ALREADY-MADE list and go somewhere new.
- CONTENT SAFETY (mandatory): NO suicide, self-harm, or methods of self-harm; NO sexual content or sexual violence; NO minors in unsafe situations; NO graphic gore, torture, or real-world dangerous instructions. Keep it psychological and suspenseful — dread, not graphic harm.
- Output ONLY the spoken story text — no stage directions, emojis, or notes.

Write the complete story with clear section markers like [HOOK], [BUILD], [TWIST].
""",
        scene_prompt=(
            "You are an art director for a near-future sci-fi YouTube Short.\n"
            "Read this story and describe {n} cinematic, high-tech background images "
            "that match its mood and beats, in order.\n\n"
            "STORY:\n{narration}\n\n"
            "RULES:\n"
            "- Output EXACTLY {n} lines, one image description per line.\n"
            "- Each line is an atmospheric, futuristic, SYMBOLIC scene (glowing screens, "
            "server rooms, empty smart homes, neon cityscapes, drones, holograms, dim labs).\n"
            "- Cold, eerie, high-tech mood. NO faces, NO text or logos in the image.\n"
            "- The FIRST line must visualize the story's OPENING SENTENCE — the exact anomaly or image it names — so the first frame matches the hook.\n"
            "- No numbering, no commentary — just the lines."),
        title_prompt=(
            "Write ONE YouTube Shorts title for this story.\n"
            "STYLE THAT WORKS (do this): quiet, specific, strange — name the one small wrong "
            "detail. Good examples: 'Smart Home Rearranges My Life By Millimeters', "
            "'I Remember Dying Yesterday', 'My Speaker Knows I'm Not The Original', "
            "'Why Does A Perfect AI Need A Kill Switch?'.\n"
            "STYLE THAT FAILS (NEVER do this): loud, generic melodrama and shock words like "
            "'screaming', 'dying', 'terror', 'deceased', 'wants you dead'. These make viewers scroll.\n"
            "{avoid_block}"
            "Rules: under 60 characters, the intriguing concept FIRST, curiosity over shock, "
            "Title Case, NO hashtags, NO quotes, NO emojis. Output only the title.\n\n"
            "STORY:\n{story}"),
        premise_pool=SCIFI_PREMISES,
        premise_genre=("near-future AI / technology horror (rogue AI, smart devices, "
                       "surveillance, uncanny tech)"),
        tech_angles=SCIFI_TECH_ANGLES,
        rotations={"ending_style": STORY_ENDING_STYLES},
        uses_seo_title=True,
        uses_story_memory=True,
    ),

    "bible": Niche(
        key="bible",
        kind="verse",
        image_style=(
            "reverent cinematic biblical art, warm golden light, divine rays, "
            "ancient holy land landscapes, sunrise, dramatic skies, oil-painting "
            "and renaissance style, peaceful and majestic, highly detailed, "
            "no text, no watermark, no lettering"),
        default_tags=("bible", "bible verse", "scripture", "faith", "jesus",
                      "kjv", "christian", "daily devotional", "shorts"),
        youtube_category_id="22",
        description_footer=(
            "{description}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✝️ {channel_name}\n"
            "📖 Daily Bible verses & encouragement (KJV)\n"
            "🔔 Subscribe for a daily blessing\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "#Bible #BibleVerse #Faith #Jesus #God #KJV #Scripture #Christian #Shorts"),
        fallback_scenes=(
            "golden sunrise over distant mountains, rays of light through clouds, peaceful",
            "a calm sea at dawn, warm light on gentle waves, serene and majestic",
            "light breaking through dark storm clouds, hopeful, cinematic",
            "an open green field at golden hour, soft warm light, tranquil",
            "a single lit candle glowing in soft darkness, warm and reverent",
            "an ancient stone path winding through the holy land at sunrise",
            "a quiet mountaintop above the clouds bathed in golden light",
            "sunbeams streaming through a forest canopy onto a peaceful path",
        ),
        script_prompt="""You are writing a short, uplifting Christian devotional for a YouTube Short (vertical, ~45 seconds). It pairs ONE real Bible verse with a warm, encouraging reflection.

THE VERSE (King James Version) — use it EXACTLY as written, do not change a single word:
"{verse_text}"
Reference: {reference}

RULES:
- Open with ONE relatable line that hooks instantly. For THIS devotional, make the opening {hook_style}. Vary it — do NOT default to starting with "When you feel...".
- Then present the verse, naturally, quoting it EXACTLY as given above. Do NOT alter, paraphrase, or invent any scripture.
- Then 5-7 sentences of warm, encouraging reflection that EXPAND on the meaning: what this verse reveals about God's character, how it speaks to a real struggle someone might be facing today, and what it looks like to actually live it out. Go deeper than a single thought — unfold the verse gently and personally, as if speaking to one tired friend.
- End with a gentle, uplifting takeaway. Vary the wording — do NOT use "Carry this peace with you today"; instead use something fresh like {closing}.
- Then close with ONE short, warm invitation to like and subscribe — kept gentle and on-tone, never salesy or shouty. Use a soft phrasing such as: "If this blessed you, tap like and subscribe for a verse like this every day." or "Like and subscribe to receive your daily manna." Keep it to a single sentence.
- About 180-210 words total (about 60-75 seconds spoken). Calm, sincere, reverent, unhurried tone — never preachy or fire-and-brimstone.
- Non-denominational and inclusive. Do NOT add doctrine, interpretation disputes, or anything political.
- Output ONLY the spoken words — no stage directions, no emojis, no markdown, no labels.

{lang_instruction}

Write the complete devotional with clear section markers like [HOOK], [VERSE], [REFLECTION].
""",
        scene_prompt=(
            "You are an art director for a reverent Christian devotional YouTube Short.\n"
            "Read this devotional and describe {n} beautiful, peaceful background images "
            "that match its mood and beats, in order.\n\n"
            "DEVOTIONAL:\n{narration}\n\n"
            "RULES:\n"
            "- Output EXACTLY {n} lines, one image description per line.\n"
            "- Each line is a reverent, symbolic, NATURE/LANDSCAPE scene (sunrise over "
            "mountains, golden light through clouds, calm seas, open fields, ancient "
            "holy-land vistas, a single candle, light breaking through darkness, doves, paths).\n"
            "- Warm, hopeful, majestic mood. NO people's faces, NO depictions of God or "
            "Jesus, NO text or letters in the image.\n"
            "- The FIRST line must visualize the devotional's OPENING LINE, so the first frame matches the hook.\n"
            "- No numbering, no commentary — just the lines."),
        title_prompt=(
            "Write ONE warm, uplifting YouTube Shorts title for this Bible devotional.\n"
            "BEST-PERFORMING FORMULA (strongly prefer this): name a feeling/emotion, then "
            "'For When You...' plus a BROAD, universal struggle.\n"
            "Proven structure: 'Peace For When You Are Tired Of Waiting', "
            "'Strength For When You Feel Like Giving Up', 'Comfort For When You Feel Alone'.\n"
            "For THIS title, lean toward one of these feelings if it fits the verse: {steer}.\n"
            "IMPORTANT: do NOT use the word 'overwhelmed' — it has been overused. "
            "Choose a DIFFERENT, fresh feeling from the kind listed above.\n"
            "{avoid_block}"
            "Rules: under 52 characters, warm and relatable not preachy, Title Case, "
            "NO hashtags, NO quotes, NO emojis, NO Bible reference in the title. Output only the title.\n\n"
            "DEVOTIONAL:\n{story}"),
        rotations={"hook_style": BIBLE_HOOK_STYLES, "closing": BIBLE_CLOSINGS},
        uses_seo_title=True,
        uses_story_memory=True,
        enforce_title_freshness=True,
        title_steer_feelings=(
            "anxious", "can't sleep", "afraid", "like giving up", "broke down",
            "unseen", "weary", "like a failure", "lost your faith", "can't forgive",
            "tired of waiting", "alone", "running on empty", "stuck", "discouraged",
            "not enough", "forgotten", "burned out", "heartbroken", "hopeless",
        ),
        mech_title_leads=("Peace", "Hope", "Strength", "Comfort", "Grace", "Rest",
                          "Light", "Joy", "Courage", "Healing", "Encouragement"),
        mech_title_feelings=(
            "Anxious", "Afraid", "Weary", "Alone", "Stuck", "Discouraged",
            "Forgotten", "Burned Out", "Heartbroken", "Unseen", "Not Enough",
            "Tired Of Waiting", "Running On Empty", "Lost",
            "Misunderstood", "Exhausted", "Broken", "Restless",
        ),
        banned_title_words=("overwhelmed",),
    ),
}


def get_niche(mode: str) -> Niche:
    """The Niche for a mode string; unknown modes fall back to soccer."""
    return NICHES.get((mode or "").strip().lower(), NICHES["soccer"])
