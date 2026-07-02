# Faceless Shorts Agent

An automated faceless YouTube **Shorts** generator. It writes a short script, narrates it,
generates matching visuals, assembles a vertical video, and uploads it to **your own**
YouTube channel — on a schedule, hands-off.

It runs **entirely on your own machine** with **your own API keys**. Nothing is sent to
anyone else; your keys live only in your local `.env` file.

Content modes: **bible** (KJV verse devotionals), **scifi** / **horror** (original AI
stories), **soccer** (news-driven). Switch with one setting.

---

## ⚠️ Read this first (honest expectations)

- This is a **personal, self-hosted tool**, not a hosted service. You install it and run it yourself.
- **You bring your own keys** and pay for your own usage (AI model, voice, etc.). Costs are small but not always zero.
- **YouTube upload limits apply to your own Google account.** A new channel can only upload a few videos/day until it builds trust, and the API has a per-project daily quota (~6 uploads/day on the default free quota). This is a Google limit, not the tool's.
- **Growth takes time.** Posting more videos does not mean more views — quality and consistency matter far more than volume. Most channels take months.
- You are responsible for following **YouTube's Terms** and its rules on automated/AI content.

---

## What you need

1. **Python 3.10+** and **ffmpeg** installed.
2. A **Google account** + a YouTube channel you own.
3. An **AI script key** (Ollama Cloud — free tier — or OpenAI/Anthropic/Grok).
4. *(Optional)* an **ElevenLabs** key for premium narration (otherwise free edge-tts is used).
   You can sign up here: <https://try.elevenlabs.io/aeq5w6vy3emf> *(affiliate link — using it supports this project at no extra cost to you)*.

---

## Install

```bash
# clone your copy
git clone <your-repo-url>
cd <repo-folder>

# install ffmpeg
#   Ubuntu/WSL:  sudo apt update && sudo apt install -y ffmpeg
#   Mac:         brew install ffmpeg

# install python deps
pip install -r requirements.txt

# create your config from the template
cp .env.example .env
```

Now open `.env` and fill in your keys (see next section).

---

## Connect your YouTube channel (one-time OAuth)

To let the tool upload to **your** channel, you create your own Google Cloud OAuth
credentials. This is the fiddliest step, but you only do it once — and there's a
**guided wizard** that does the hard parts for you:

```bash
python3 setup_oauth.py        # or: ./run.sh --auth
```

The wizard:
1. walks you through the four Google Cloud console steps with direct links
   (create project → enable **YouTube Data API v3** → consent screen set to
   **In production** → create a **Desktop app** OAuth client),
2. opens your browser for Google sign-in (no code copy/pasting, no OAuth playground),
3. **writes the credentials into `.env` for you**, and
4. verifies the token by printing your channel name.

Two things it will remind you about, because they matter:

- **Publish the consent screen to "In production."** If you leave it in *Testing*,
  Google expires the refresh token **every 7 days** and uploads silently stop.
  A "this app isn't verified" notice during sign-in is normal for a personal app —
  click *Advanced → continue*; it's your own app.
- Verify your channel at <https://youtube.com/verify> (phone) — it raises upload limits.

<details>
<summary>Manual alternative (OAuth playground) — if you prefer not to use the wizard</summary>

1. Create a project at <https://console.cloud.google.com>, enable **YouTube Data API v3**,
   publish the consent screen to **Production**.
2. **Credentials → Create credentials → OAuth client ID → Web application.**
   Under *Authorized redirect URIs* add exactly: `https://developers.google.com/oauthplayground`
3. At <https://developers.google.com/oauthplayground>: gear icon → tick **Use your own
   OAuth credentials** → paste Client ID + Secret → scope
   `https://www.googleapis.com/auth/youtube.upload` → **Authorize** →
   **Exchange authorization code for tokens** → copy the **Refresh token** (`1//...`).
4. Put `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` into `.env`.

</details>

---

## Configure `.env`

Open `.env` and set at least:

```
CONTENT_MODE=bible            # bible | scifi | horror | soccer
CHANNEL_NAME=Your Channel
OLLAMA_API_KEY=...            # from https://ollama.com/settings/keys
LOCAL_MODEL_ENDPOINT=https://ollama.com/api/generate
LOCAL_MODEL_NAME=gemma4:31b   # a model available on your provider
ASSET_PROVIDER=ai_image
UPLOAD_PRIVACY=private        # keep private until you've reviewed output
MAX_DAILY_UPLOADS=2
CHECK_INTERVAL_MINUTES=720
# optional premium voice:
# ELEVENLABS_API_KEY=...
# ELEVENLABS_VOICE_ID=...
```

Every option is documented inline in `.env.example`.

---

## Run

```bash
# 1) safest first: build ONE video without uploading
python3 agent.py --once --dry-run

# 2) build + upload ONE (uses UPLOAD_PRIVACY; keep it private first)
python3 agent.py --once

# 3) autopilot (keeps running on your schedule)
nohup python3 agent.py > agent.log 2>&1 &
tail -f agent.log          # watch it;  pkill -f agent.py  to stop
```

Review your first videos (set to **private**) before switching `UPLOAD_PRIVACY=public`.

---

## Optional: Channel Control dashboard

`channel_dashboard.py` is a local web panel to manage one or more channels (start/stop,
generate one now, edit each channel's `.env`, watch logs). Run:

```bash
python3 channel_dashboard.py
```

It prints (and opens) a URL containing a **one-time access token** — use that exact
URL. Every dashboard in this project requires its token and rejects non-localhost
requests, so other devices, other local users, and malicious web pages can't reach
the endpoints that read/write your keys. If you restart a dashboard, grab the fresh
URL from the terminal.

---

## Safety & good-citizen notes

- Keep `.env` **out of git** — it holds your keys. The included `.gitignore` already excludes it. Never paste keys into screenshots or chats.
- If you expose a key by accident, **revoke and regenerate it** immediately.
- Don't flood a new channel; respect YouTube's automated-content rules.
- Bible mode quotes real public-domain KJV scripture and does not alter it.

---

## Troubleshooting

These are the real problems you're most likely to hit, and the fix for each.

### Setup / OAuth

**"Access blocked: this app isn't verified" when authorizing.**
Normal for a personal app. Click **Advanced → Go to (your app) → Continue**. It's your own app; the warning just means Google hasn't audited it.

**The refresh token field in the OAuth Playground is empty.**
You skipped a step. Open the gear icon, tick **Use your own OAuth credentials**, paste your Client ID + Secret, *then* Authorize and Exchange. If you authorize before doing that, you get no usable refresh token.

**Login works for a week, then uploads start failing with auth errors.**
Your OAuth app is still in **Testing**, so the refresh token expired after 7 days. In the Google Auth Platform → **Audience**, click **Publish app** so the status reads **In production**. Then re-run `python3 setup_oauth.py` to get a fresh token (the agent's log also tells you exactly this when it detects the `invalid_grant` error).

**`redirect_uri_mismatch`.**
The redirect URI on your OAuth client must be *exactly* `https://developers.google.com/oauthplayground` — no trailing slash, no `www`. Fix it in Credentials → your client, then retry.

**`access_denied` during authorization.**
Your Google address isn't a **test user** on the consent screen, or the app isn't published. Add yourself under Audience → Test users (or publish to Production) and retry.

### Uploads

**`uploadLimitExceeded` / "Uploads paused until ..." in the log.**
YouTube caps how many videos a **new/unverified** channel can upload per day. When the agent hits any quota/limit error it now **pauses itself until the quota resets** (midnight Pacific) instead of wasting work — finished videos wait in `state/pending_uploads.json` and upload automatically once the pause lifts. Verify the channel at <https://youtube.com/verify> (phone) and lower `MAX_DAILY_UPLOADS` (try 2–3). This is a Google limit, not a bug.

**Quota exceeded / 403 after ~6 uploads in a day.**
The YouTube Data API gives ~10,000 units/day per Google Cloud project, and each upload costs ~1,600 units — so ~6 uploads/day max on the free quota. The agent pauses and retries automatically (see above). To do more per day, request more quota from Google (a formal process).

**A video was built but not uploaded.**
Look in `state/pending_uploads.json` — assembled videos that couldn't upload (quota, auth outage, network) are queued there and retried once per cycle, up to 5 attempts. Nothing is regenerated or thrown away.

**Thumbnails.**
Custom thumbnail upload is **off by default** (`UPLOAD_THUMBNAILS=false`): Shorts don't display custom thumbnails, the call costs API quota, and unverified channels get 400 errors from it. If you want them (e.g. for long-form), verify the channel, keep the image under 2MB, and set `UPLOAD_THUMBNAILS=true`.

**Video uploaded but it's public when you wanted it hidden.**
Set `UPLOAD_PRIVACY=private` in `.env` (or `unlisted`) and **restart the agent**. Always keep new channels on `private` until you've reviewed a few.

### Visuals

**Lots of `429 Too Many Requests` on images.**
The free Pollinations image service is rate-limiting you — usually from generating too many videos at once (multiple channels, high `MAX_DAILY_UPLOADS`). The agent now defends itself three ways: it spaces image requests out (`AI_IMAGE_MIN_INTERVAL`, default 4s), it uses fewer images per video (`SECONDS_PER_CLIP`, default 8s per segment), and when a request still fails it **reuses a previously generated on-theme image** from `data/image_cache/` instead of a plain gradient — so quality holds up even under rate-limiting. If it persists: lower your upload rate, add a `POLLINATIONS_API_KEY`, or switch `ASSET_PROVIDER` to `pexels` (free key).

### Script / AI model

**Script step fails or returns nothing.**
Check `LOCAL_MODEL_NAME` is a model your provider actually serves. On Ollama Cloud, use the exact cloud model name (e.g. `gemma4:31b`) — a model that only exists locally, or a wrong size/suffix, will fail. Verify your `OLLAMA_API_KEY` is set and valid.

**Voice is the wrong/robotic one.**
ElevenLabs is only used when **both** `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` are set. If either is missing, it falls back to free edge-tts. Set both (and pick a voice ID from your ElevenLabs library) for premium narration.

### Settings won't take effect

**You changed `.env` but the agent ignores it.**
Three usual causes: (1) the setting line is **commented out** (starts with `#`) — uncomment it; (2) there are **duplicate** lines for the same key — keep one; (3) you didn't **restart** the agent/scheduler after saving. Settings are read at startup, so stop and start it (`pkill -f agent.py`, then run again).

**"Daily upload limit reached" right after starting.**
The agent counts today's uploads from `state/publication_history.json` using the **UTC** date (which rolls over earlier than your local midnight). Test runs count too. Wait for the UTC reset, raise `MAX_DAILY_UPLOADS`, or use the dashboard's **Reset today** button (it backs up first and never touches your topic memory).

### Content quality (bible mode)

**A warm title doesn't match its verse** (e.g. an uplifting title over a verse about judgment).
Some verses don't suit an encouraging devotional. Remove the offending reference from `assets/kjv_verses.json`. The pool is curated, but if one slips through, delete that entry and the video for it.

### Dashboard

**The dashboard looks unchanged after I updated the file.**
You're running an old copy or seeing a cached page. Make sure you replaced the actual file you run (watch for `name (1).py` duplicates in Downloads), **restart** it (`pkill -f channel_dashboard.py` then run again), and **hard-refresh** the browser (Ctrl+Shift+R) or use a private window.

**`BrokenPipeError` spam in the dashboard terminal.**
Harmless — it happens when the browser closes a log request early. The current version silences it.

### General workflow gotchas

- After downloading a new build, actually **copy the files into your project folder** and **restart** the process — forgetting one of these causes most "it didn't change" confusion.
- If you keep a separate verse/data file (like `assets/kjv_verses.json`), remember to copy **that too** — copying only `*.py` won't update it.
- **Never** paste an API key into a screenshot or chat. If you do, **revoke and regenerate it** immediately (e.g. Ollama keys at <https://ollama.com/settings/keys>).

---

## Recommended tools

- **ElevenLabs** — natural, premium AI narration (what this tool uses when configured).
  Sign up: <https://try.elevenlabs.io/aeq5w6vy3emf>
  *(This is an affiliate link. If you sign up through it, the maintainer may earn a
  referral commission at no extra cost to you. You're free to sign up directly at
  elevenlabs.io instead — the tool works the same either way.)*

---

## License

Add your own license (e.g. MIT) before publishing. You are responsible for your channel,
your content, and your compliance with all third-party terms.
