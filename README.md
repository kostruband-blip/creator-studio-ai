# Creator Studio AI — NicheRadar AI + Thumbnail & Title Lab + Content Repurposer

Three free tools for YouTube creators on one site, with a shared header and a 3-tool switcher:

| Tool | URL | What it does |
| --- | --- | --- |
| **NicheRadar AI** | `/` | Niche, Top 5 competitors and 10 viral video ideas for any channel |
| **Thumbnail & Title Lab** | `/thumbnails` | 3 ready 16:9 thumbnails (PNG 1280×720) + 6 A/B titles with CTR score |
| **Content Repurposer** | `/repurpose` | One YouTube video → Telegram post + SEO blog article + 5 short posts for X / Threads |

All tools use the shared Gemini client `api/_gemini.py` (key carousel, hedged fast timeouts)
and the same UI language switch (EN default / RU / UA, remembered between pages).
The tools link to each other: every NicheRadar idea has a **Create Thumbnail →** button
(`/thumbnails?topic=...` pre-fills the topic), and the Repurposer links to the Lab with the
video title and to KoShorts AI Studio / the Telegram bot for cutting the same video into Shorts.

**NicheRadar AI** is a free tool that tells any YouTube creator their exact niche,
reveals their **Top 5 competitors**, and generates **10 clickable viral video ideas** —
all in the language of their channel.

Paste a channel link (`youtube.com/@handle`, `/channel/UC...`) or any video link, click
**Analyze Channel & Find Top 5 Competitors**, and within ~4–6 seconds the report appears
right on the page.

- **Frontend:** a single static `index.html` (dark theme, fully responsive, EN / RU / UA).
- **Backend:** a Vercel Serverless Function `api/analyze.py` written **only with the Python
  standard library** (`urllib`, `json`, `re`, `os`, …). No heavy dependencies.
- **AI:** Google Gemini via the shared client **`api/_gemini.py`** (files starting with `_` are not exposed as endpoints):
  - the model list comes **from Gemini itself** (`GET /v1beta/models`, cached 15 min): only **text** models are kept
    (embedding / image / TTS / audio / live / video models are dropped), so retired models disappear automatically;
  - order: available ones from `gemini-flash-latest` → `gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-3-flash-preview` → `gemini-pro-latest` first,
    then the other discovered text models; if the list request fails, that preferred list is used (env `GEMINI_MODELS` = hard override);
  - on **503 / 429 / timeout** the same model is **not** retried — the **next model** is used immediately;
  - keys `GEMINI_API_KEY` → `_2` → `_3` rotate each time the model ring wraps, so every model × key pair is tried once;
  - one request waits at most **25 s**; if a model has not answered after **6 s**, the next model also starts **in parallel**
    (max 3 at once) and the first successful answer wins; the model + key that answered last is tried **first** next time;
  - if Gemini is overloaded (503 / 429) for the whole budget, the report is still returned, built from **YouTube data only**:
    niche from title keywords, ideas = competitors' viral videos (`"ai": {"niche": "fallback", "ideas": "fallback"}`),
    and the page shows a notice. Key errors are still reported so they can be fixed;
  - `404` (model retired) removes it from the ring, `401/403` skips that key.
  Carousel state: **`/api/analyze?models=1`** (keys are never shown).

---

## How it works

1. **Resolve the channel** — from a channel or video URL via the YouTube Data API v3
   (channel ID, title, description, avatar, subscribers, and the 10 latest videos).
2. **Define the niche** — Gemini returns a precise niche (3–7 words) and 4 search queries
   in the channel's language.
3. **Find Top 5 competitors** — search popular videos for those queries, exclude the user's
   channel, read candidate channel stats, and rank the 5 strongest by subscribers + views.
4. **Find viral outliers** — inspect the latest 12 videos of each competitor, detect videos
   that massively outperformed the channel average, and ask Gemini for 10 concrete, clickable
   titles/topics for the user's channel.

### Results page
After the analysis the full report is shown **open, with no paywall**: the niche card, all
**5 competitors** and all **10 viral video ideas** (clickable, on desktop and mobile).
Below the report an info block (EN / RU / UA) points to
[KoShorts AI Studio](https://koshorts.vercel.app) and
[CreatorDirectory](https://creator-directory-chi.vercel.app). The header and footer also link
the Telegram bot [@remtex_shorts_ai_bot](https://t.me/remtex_shorts_ai_bot).

---

## 🚀 Launch in 2 steps

### Step 1 — Enable the free YouTube Data API v3 (Google Cloud Console)

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a new project
   (or pick an existing one).
2. Go to **APIs & Services → Library**, search for **YouTube Data API v3**, and click
   **Enable**.
3. Go to **APIs & Services → Credentials → Create credentials → API key**.
4. (Recommended) Click the key and restrict it to the *YouTube Data API v3* to avoid abuse.
5. Copy the API key — you will need it in Step 2.

> The free tier includes 10,000 quota units/day, which is plenty for many analyses
> (one full report uses roughly ~20 units).

### Step 2 — Add environment variables in Vercel

1. Deploy this repository to Vercel (import the Git repo, or run `vercel`).
2. Open **Project Settings → Environment Variables** and add:

| Name                 | Required | Description                                                        |
| -------------------- | -------- | ------------------------------------------------------------------ |
| `YOUTUBE_API_KEY`    | ✅ yes   | API key from Step 1. Without it the function returns a setup error. |
| `GEMINI_API_KEY`     | ✅ yes   | Primary Gemini API key (Google AI Studio).                         |
| `GEMINI_API_KEY_2`   | optional | Second Gemini key (used in the rotation pool).                      |
| `GEMINI_API_KEY_3`   | optional | Third Gemini key (used in the rotation pool).                       |
| `GEMINI_MODELS`      | optional | Comma-separated model list; skips asking Gemini for available models. |
| `SUBS_PROXY_URL`     | optional | Content Repurposer: backup subtitle server, called as `GET {SUBS_PROXY_URL}?v={video_id}` when YouTube refuses the direct request. |
| `SUBS_PROXY_TOKEN`   | optional | Sent to the backup subtitle server as the `X-Token` header.        |
| `SUBS_PROXY_TIMEOUT` | optional | Seconds to wait for the backup subtitle server (default 50, 10–70). |
| `CF_ACCOUNT_ID`      | optional | Thumbnail Lab: Cloudflare account ID for **Workers AI** (free 10,000 neurons/day ≈ 95–170 backgrounds). Backgrounds are drawn by FLUX.2 klein 4B at 1280×720 (FLUX.1 schnell as backup) — tried first. |
| `CF_API_TOKEN`       | optional | Cloudflare API token (template "Workers AI"). Check: `/api/thumbnails?bgcheck=1`. |
| `HF_TOKEN`           | optional | Thumbnail Lab: free Hugging Face token (`hf_…`, huggingface.co → Settings → Access Tokens). When set, AI backgrounds 1280×720 are drawn through Hugging Face Inference Providers (FLUX.1-schnell, then FLUX.1-dev; the live provider — hf-inference / Together / Nscale / fal / Replicate — is looked up on the Hub) instead of the free Pollinations queue. Check: `/api/thumbnails?hfcheck=1`. |
| `HF_MODEL`           | optional | Comma-separated Hugging Face model ids tried before the defaults.  |

> **Gemini 429 (quota exceeded)?** Free limits are counted **per Google Cloud project and per model** (daily limits reset at midnight Pacific). Extra keys only help if they come from **different projects**. Check your live limits at https://aistudio.google.com/rate-limit or enable billing for higher limits.

> **Key formats (2026):**
> - `YOUTUBE_API_KEY` — Google Cloud → APIs & Services → Credentials → *Create credentials → API key*. Starts with `AIza…`.
> - `GEMINI_API_KEY` — Google AI Studio → *Create API key*. New keys start with `AQ.…` (legacy `AIza…` Gemini keys are being phased out). Both formats work: the key is sent via the `x-goog-api-key` header.
> - ⚠️ A value like `{"token":"ya29.…"}` is a short-lived **OAuth access token**, not an API key — it will not work here.

3. Redeploy the project so the new variables are picked up.

That's it — visit the deployed site, paste a YouTube link, and analyze.

> If `YOUTUBE_API_KEY` is missing, the API responds with a clear JSON error **and setup
> instructions** instead of crashing with a 500.

---

## Thumbnail & Title Lab (`/thumbnails`)

1. Enter the video topic, a draft title **or a YouTube link** (or click one of 3 examples). A link
   is resolved on the server (YouTube Data API / oEmbed: title, channel, description, tags), so the
   concepts and titles are made for that exact video. Optionally add a niche,
   pick the language of the titles / thumbnail text (EN / RU / UA) and optionally upload your
   photo or selfie (it never leaves the device).
2. `api/thumbnails.py` asks Gemini (via `api/_gemini.py`) for strictly validated JSON:
   **3 thumbnail concepts** (`angle_name`, `overlay_text` 2–4 words, `accent_color` HEX,
   English `image_prompt` for a text-free background, `ctr_tip`) and **6 titles**
   (`title` 45–80 chars, `ctr_score` 82–97, `trigger`). Every field is re-checked on the
   server (word count, punctuation, HEX, score range, length, language purity: no Cyrillic in
   EN, no ы/э/ъ/ё in UA, no і/ї/є/ґ in RU); anything that fails is replaced by a template.
   If Gemini is overloaded the tool still answers with templates (`"ai": "fallback"`).
3. The browser renders each thumbnail on an HTML5 canvas at 1280×720 with three different
   compositions for the A/B/C test:
   - **A** — big headline on the left + tilted sticker badge on top;
   - **B** — accent word on a tilted (−2°) ribbon plate + glowing accent frame around the picture;
   - **C** — big centered headline at the bottom with a neon glow + badge pill above it.

   Every card has a directional shade (`rgba(0,0,0,0.88)` at the text edge → transparent towards
   the subject), white words with a dense shadow and ONE accent word (Gemini's `highlight`, a number,
   or any word the user wraps in `*asterisks*`). An uploaded photo sits in the right third with a soft
   fade at the bottom, a light outline and an accent glow. Text and sticker are editable per card.
4. Backgrounds: if Cloudflare Workers AI (`CF_ACCOUNT_ID` + `CF_API_TOKEN`) or `HF_TOKEN` is set, the
   browser asks `POST /api/thumbnails {"bg": prompt, "seed": n}` (3 in parallel) and gets a ready data-URL:
   Cloudflare FLUX.2 klein 1280×720 → FLUX.1 schnell → Hugging Face (`router.huggingface.co/<provider>`).
   Otherwise (or if HF fails) the free `https://image.pollinations.ai/prompt/...` endpoint is used with
   a short prompt (`bg_prompt`, ≤ 12 words), one picture per ≥ 47 s with a countdown, the image is
   zoomed 1.08× from the top (the bottom strip with the free-tier mark is cut off), and "Free limit
   reached" cards are rejected by size and pixel check. Until a picture arrives (or if every try
   fails) a studio background with a radial spotlight is drawn. Fonts: Montserrat 900, so Cyrillic and
   і ї є ґ are always perfect. Each card has **Download PNG (1280×720)** and **New background**. The 6
   titles are shown as A/B cards with CTR bar, trigger and **Copy**.

`POST /api/thumbnails`

```json
{ "topic": "I fixed a dead washing machine for $5", "niche": "DIY repair", "lang": "en" }
```

Response: `{"success": true, "concepts": [3 × {...}], "titles": [6 × {...}], "ai": "gemini|mixed|fallback", "lang": "en"}`.
Missing `GEMINI_API_KEY` → `{"success": false, "error": "...", "setup": {"title": "...", "steps": [...]}}`.

---

## Content Repurposer (`/repurpose`)

1. Paste a YouTube video link (or click one of 3 examples). Optionally open
   **"Or paste text / transcript / key points"** — with a link it adds detail, without a link
   the pack is written from this text only. Pick the output language (Auto / EN / RU / UA).
2. `api/repurpose.py` collects the source material, best effort and never failing hard:
   title, channel, description, tags and **chapters** (timecodes parsed from the description)
   via the YouTube Data API (`YOUTUBE_API_KEY`, oEmbed fallback for the title), plus
   **subtitles** (InnerTube player → watch page caption tracks). YouTube often blocks
   subtitles for datacenter IPs (`LOGIN_REQUIRED`). Then, if `SUBS_PROXY_URL` is set, the
   backup subtitle server is asked: `GET {SUBS_PROXY_URL}?v={video_id}` with header
   `X-Token: {SUBS_PROXY_TOKEN}` (timeout `SUBS_PROXY_TIMEOUT`, 50 s by default); a non-empty JSON `text` is used as the full
   transcript (`sources.subtitles` = `manual`/`auto`, `sources.subtitles_via` = `"proxy"`).
   If there is still no transcript, **Gemini watches the public video
   itself**: the YouTube URL is sent as `fileData` (low media resolution, 0.2 fps, first
   20 minutes; Google fetches the video on its side, so the IP block does not matter;
   free tier: up to 8 hours of YouTube video per day) and `sources.video_ai` is `"watched"`.
   Only if that also fails is the pack built from description + chapters + your text, with
   the warning `subtitles_unavailable` (shown in the UI).
3. Gemini (via `api/_gemini.py`) returns strict JSON, re-validated on the server:
   - `telegram_post` — 800–1300 chars: hook, 3–5 bullets, takeaway, CTA (the video link is inserted);
   - `seo_article` — H1, meta description (≤160), intro, 3–4 H2 sections, conclusion,
     2500–3500 chars; the server also builds ready `markdown` and `html`;
   - `social_posts` — 5 posts ≤280 chars: insight, steps, mistake, tip, question.
   Language purity is enforced (no Cyrillic in EN, no ы/э/ъ/ё in UA, no і/ї/є/ґ in RU);
   if validation fails, one corrective Gemini retry is made.
4. Every block has its own **Copy** button (article: Markdown or HTML); everything is free
   and open, no sign-up.

`POST /api/repurpose`

```json
{ "url": "https://www.youtube.com/watch?v=UF8uR6Z6KLc", "notes": "optional text / key points", "lang": "auto" }
```

Response: `{"success": true, "lang": "en", "video": {id,url,title,channel,thumbnail}|null,
"sources": {api, oembed, description, chapters, subtitles: "manual|auto|blocked|none", subtitles_via?: "proxy", notes, video_ai?: "watched|failed"},
"warnings": [...], "telegram_post": "...", "seo_article": {h1, meta_description, intro, sections:[{h2,body}],
conclusion_h2, conclusion, markdown, html}, "social_posts": [5 × {angle, text}], "stats": {...}}`.
`GET /api/repurpose?probe=<youtube url>` is a diagnostic (metadata + subtitle availability, no Gemini call).
Missing `GEMINI_API_KEY` → JSON error with setup steps (never a 500).


### Backup subtitle server (`tools/subs_server.py`)

YouTube refuses subtitle requests from Vercel IPs. Run `tools/subs_server.py` on your own VPS
(stdlib + `yt-dlp`): `GET /subs?v=VIDEO_ID` with `X-Token` → `{"ok", "video_id", "text", "lang", "source"}`.
It reads only the subtitle track (`skip_download`, `process=False`), so "Requested format is not
available" never breaks it. Then set `SUBS_PROXY_URL=http://<ip>:8090/subs` and `SUBS_PROXY_TOKEN` in
Vercel. Check from Vercel: `/api/repurpose?proxycheck=1&v=VIDEO_ID`.

---

## API

`POST /api/analyze`

```json
{ "url": "https://www.youtube.com/@MrBeast" }
```

Response (success):

```json
{
  "success": true,
  "channel": { "id": "...", "title": "...", "avatar": "...", "subscribers": 123, "views": 456, "videos": [...] },
  "niche": "Extreme charity challenges",
  "competitors": [
    { "title": "...", "url": "...", "avatar": "...", "subscribers": 0, "views": 0, "bestVideo": { "title": "...", "url": "...", "views": 0 } }
  ],
  "ideas": [ { "title": "...", "topic": "..." } ]
}
```

Response (missing key):

```json
{
  "success": false,
  "error": "YOUTUBE_API_KEY environment variable is not set.",
  "setup": { "title": "...", "steps": ["...", "..."] }
}
```

---

## Local testing (without Vercel)

You can run the function logic locally with Python 3.11+:

```bash
export YOUTUBE_API_KEY="your_key"
export GEMINI_API_KEY="your_gemini_key"
python3 -c "import api.analyze as a; print(a.analyze('https://www.youtube.com/@MrBeast'))"
```

---

## Ecosystem

- **KoShorts AI Studio** — [https://koshorts.vercel.app](https://koshorts.vercel.app) — cut
  YouTube Shorts from any link.
- **CreatorDirectory** — [https://creator-directory-chi.vercel.app](https://creator-directory-chi.vercel.app)
  — a catalog of 65+ tools for creators.
- **Telegram bot** — [@remtex_shorts_ai_bot](https://t.me/remtex_shorts_ai_bot).

---

## Project structure

```
.
├── index.html          # NicheRadar AI (dark theme, EN/RU/UA, open report)
├── thumbnails.html     # Thumbnail & Title Lab (canvas thumbnails, A/B titles), served at /thumbnails
├── repurpose.html      # Content Repurposer (Telegram post, SEO article, 5 posts), served at /repurpose
├── api/
│   ├── _gemini.py      # Shared Gemini client (key carousel, hedging) — not an endpoint
│   ├── analyze.py      # POST /api/analyze    (stdlib only)
│   ├── thumbnails.py   # POST /api/thumbnails (stdlib only)
│   └── repurpose.py    # POST /api/repurpose  (stdlib only)
├── favicon.*, apple-touch-icon.png
├── preview_server.py   # Local preview with mock APIs (python3 preview_server.py)
├── vercel.json         # Function config + /thumbnails, /repurpose rewrites + headers
├── requirements.txt    # Empty — standard library only
└── README.md
```
