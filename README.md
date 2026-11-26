# NicheRadar AI

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

### Monetization funnel
After the analysis the **niche card**, the **first 2 competitors**, and the **first 3 video
ideas** are shown for free. The remaining **3 competitors** and **7 ideas** are shown as
blurred, locked cards with a single call-to-action button that opens the full report in the
**Telegram bot** (`https://t.me`).

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
  — a catalog of 60+ tools for creators.

---

## Project structure

```
.
├── index.html          # Static frontend (dark theme, EN/RU/UA, paywall)
├── api/
│   └── analyze.py      # Vercel Serverless Function (stdlib only)
├── vercel.json         # Function config + rewrites + headers
├── requirements.txt    # Empty — standard library only
└── README.md
```
