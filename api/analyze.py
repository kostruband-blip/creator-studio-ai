# -*- coding: utf-8 -*-
"""
NicheRadar AI — Vercel Serverless Function (Python, standard library only).

Flow:
  Step 1  Resolve the channel (from a channel or video URL) and read its
          10 latest videos via the YouTube Data API v3.
  Step 2  Ask Gemini to define the precise niche and 4 search queries
          (in the channel's language).
  Step 3  Search popular videos for those queries, exclude the user's
          channel, rank candidate channels by subscribers + views, keep Top 5.
  Step 4  For each of the 5 competitors inspect their latest 12 videos,
          find outlier ("viral") videos, then ask Gemini for 10 clickable
          video ideas/topics for the user's channel.

Only the Python standard library is used:
  urllib.request, urllib.error, urllib.parse, json, re, os, time, sys
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration / environment
# ---------------------------------------------------------------------------

YT_KEY = (os.environ.get("YOUTUBE_API_KEY") or "").strip()

GEMINI_KEYS = [
    k.strip()
    for k in (
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GEMINI_API_KEY_2"),
        os.environ.get("GEMINI_API_KEY_3"),
    )
    if k and k.strip()
]

# ---------------------------------------------------------------------------
# Gemini model carousel
#
# 1. Ask Gemini which models this key can use (GET /v1beta/models, ListModels).
# 2. Keep only text models (support generateContent; no embedding / image /
#    TTS / audio / live / video models).
# 3. Walk the list as a ring. The model that answered last time is tried first
#    on the next request; on failure we move to the next model around the ring.
#
# Override with env GEMINI_MODELS="model-a,model-b" to skip discovery.
# ---------------------------------------------------------------------------

# Used only if ListModels itself fails (network / bad key).
_FALLBACK_MODELS = [
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
]

_ENV_MODELS = [
    m.strip().replace("models/", "")
    for m in (os.environ.get("GEMINI_MODELS") or "").split(",")
    if m.strip()
]

# Words that mark non-text models (embeddings, images, speech, video, ...).
_NON_TEXT_MARKERS = (
    "embed", "image", "imagen", "banana", "tts", "audio", "speech", "live",
    "veo", "video", "lyria", "music", "aqa", "robotics", "computer-use",
    "transcribe", "vision", "native",
)

MODEL_LIST_TTL_SEC = 15 * 60   # re-discover models every 15 minutes
GEMINI_BUDGET_SEC = 30         # time for all Gemini attempts of one call

# Shared state of a warm serverless instance.
_MODEL_STATE = {
    "models": [],       # discovered text models, in ring order
    "fetched_at": 0.0,
    "source": "",       # "api" | "env" | "fallback"
    "current": None,    # model that answered last -> tried first next time
}
_MODEL_COOLDOWN = {}    # model -> unix time until which we skip it
_NO_JSON_MODE = set()   # models that reject responseMimeType=application/json

YT_BASE = "https://www.googleapis.com/youtube/v3/"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"


class GeminiFail(Exception):
    """Raised when a Gemini call fails."""

    def __init__(self, retryable, message, code=0, kind=""):
        self.retryable = retryable
        self.message = message
        self.code = code
        # kind: "quota" | "model" | "key" | "transient" | "bad_response" | ""
        self.kind = kind
        super().__init__(message)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def fmt_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def yt_get(endpoint, params, retries=3):
    """Call the YouTube Data API v3 and return parsed JSON."""
    params = dict(params)
    params["key"] = YT_KEY
    url = YT_BASE + endpoint + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NicheRadar/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 503):
                time.sleep(1 + attempt)
                continue
            # Surface a readable message for other HTTP errors.
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                body = ""
            raise RuntimeError("YouTube API error %s: %s" % (e.code, body[:300]))
        except Exception as e:  # network / timeout
            last = e
            time.sleep(1 + attempt)
    if last is not None:
        raise RuntimeError("YouTube API request failed: %s" % last)
    raise RuntimeError("YouTube API request failed for unknown reason")


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def extract_video_id(url):
    if not url:
        return None
    m = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def extract_channel_id(url):
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{18,})", url or "")
    return m.group(1) if m else None


def extract_handle(url):
    m = re.search(r"(?:^|[/@])(@[A-Za-z0-9._-]{2,})", url or "")
    return m.group(1) if m else None


def extract_custom(url):
    m = re.search(r"/c/([A-Za-z0-9._-]+)", url or "")
    return m.group(1) if m else None


def extract_username(url):
    m = re.search(r"/user/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def resolve_channel_id(url):
    """Return a channelId from any supported YouTube URL."""
    # 1) Direct video link -> channelId
    vid = extract_video_id(url)
    if vid:
        data = yt_get("videos", {"part": "snippet", "id": vid})
        items = data.get("items") or []
        if items:
            cid = items[0].get("snippet", {}).get("channelId")
            if cid:
                return cid
        raise ValueError("Could not read the video. It may be private or unavailable.")

    # 2) /channel/UC... direct id
    cid = extract_channel_id(url)
    if cid:
        return cid

    # 3) Handle (@handle) or custom URL (/c/name)
    handle = extract_handle(url) or extract_custom(url)
    if handle:
        cid = resolve_via_handle(handle)
        if cid:
            return cid

    # 4) Legacy /user/NAME
    username = extract_username(url)
    if username:
        data = yt_get("channels", {"part": "snippet", "forUsername": username})
        items = data.get("items") or []
        if items:
            return items[0]["id"]
        # fall back to treating the legacy name as a handle
        cid = resolve_via_handle("@" + username)
        if cid:
            return cid

    raise ValueError(
        "Could not recognise this YouTube link. Use a channel URL "
        "(youtube.com/@handle, /channel/UC...) or any video URL."
    )


def resolve_via_handle(handle):
    if not handle:
        return None
    attempts = [handle, handle.lstrip("@"), "@" + handle.lstrip("@")]
    for h in attempts:
        try:
            data = yt_get("channels", {"part": "snippet", "forHandle": h})
            items = data.get("items") or []
            if items:
                return items[0]["id"]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Channel / video data
# ---------------------------------------------------------------------------


def get_channel(channel_id):
    data = yt_get(
        "channels",
        {
            "part": "snippet,statistics,contentDetails",
            "id": channel_id,
        },
    )
    items = data.get("items") or []
    if not items:
        raise ValueError("Channel not found (it may be deleted or hidden).")
    item = items[0]
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    thumbs = sn.get("thumbnails", {})
    avatar = (
        thumbs.get("high", {}).get("url")
        or thumbs.get("medium", {}).get("url")
        or thumbs.get("default", {}).get("url")
    )
    return {
        "id": channel_id,
        "title": sn.get("title", ""),
        "description": sn.get("description", ""),
        "avatar": avatar,
        "subscribers": fmt_int(st.get("subscriberCount")),
        "views": fmt_int(st.get("viewCount")),
        "language": sn.get("defaultLanguage", "") or sn.get("country", ""),
        "uploads": item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", ""),
    }


def get_videos_from_playlist(playlist_id, max_results=12):
    if not playlist_id:
        return []
    data = yt_get(
        "playlistItems",
        {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": max_results,
        },
    )
    ids = [
        it.get("contentDetails", {}).get("videoId")
        for it in data.get("items", [])
        if it.get("contentDetails", {}).get("videoId")
    ]
    if not ids:
        return []
    vdata = yt_get("videos", {"part": "snippet,statistics", "id": ",".join(ids)})
    out = []
    for v in vdata.get("items", []):
        sn = v.get("snippet", {})
        st = v.get("statistics", {})
        thumbs = sn.get("thumbnails", {})
        out.append(
            {
                "id": v["id"],
                "title": sn.get("title", ""),
                "url": "https://www.youtube.com/watch?v=" + v["id"],
                "thumbnail": thumbs.get("medium", {}).get("url")
                or thumbs.get("default", {}).get("url"),
                "views": fmt_int(st.get("viewCount")),
                "likes": fmt_int(st.get("likeCount")),
                "comments": fmt_int(st.get("commentCount")),
                "published": sn.get("publishedAt", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _google_error_message(detail):
    """Pull the human-readable message out of a Google error body."""
    try:
        j = json.loads(detail)
        err = j.get("error") or {}
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or "")[:300]
        return str(err)[:300]
    except Exception:
        return (detail or "")[:300]


def _gemini_request(model, key, prompt, timeout=30, json_mode=True):
    url = GEMINI_BASE + model + ":generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 1,
            # Thinking models spend output tokens on reasoning; leave headroom
            # so the JSON answer is not truncated.
            "maxOutputTokens": 8192,
        },
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        # Works with both the new "AQ." auth keys and legacy "AIza" keys.
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key.strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        msg = _google_error_message(detail)
        text = "HTTP %d%s" % (e.code, (": " + msg) if msg else "")
        low = msg.lower()
        if e.code == 400 and json_mode and ("json mode" in low or "mime" in low):
            raise GeminiFail(True, text, e.code, "json_mode")
        if e.code == 429:
            raise GeminiFail(True, text, e.code, "quota")
        if e.code in (500, 502, 503, 504):
            raise GeminiFail(True, text, e.code, "transient")
        if e.code == 404 or (e.code == 400 and "model" in msg.lower()):
            raise GeminiFail(True, text, e.code, "model")
        if e.code in (401, 403) or (e.code == 400 and "api key" in msg.lower()):
            raise GeminiFail(True, text, e.code, "key")
        raise GeminiFail(False, text, e.code, "")
    except Exception as e:  # network / timeout
        raise GeminiFail(True, str(e)[:200], 0, "transient")

    try:
        j = json.loads(raw)
        parts = j["candidates"][0]["content"]["parts"]
        # Skip "thought" parts; join the visible text.
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise ValueError("empty text")
        return text
    except Exception as e:
        raise GeminiFail(True, "Unexpected Gemini response: %s" % str(e)[:200], 0, "bad_response")


def _is_text_model(m):
    name = (m.get("name") or "").replace("models/", "")
    methods = m.get("supportedGenerationMethods") or []
    if "generateContent" not in methods:
        return False
    if not (name.startswith("gemini") or name.startswith("gemma")):
        return False
    low = name.lower()
    return not any(marker in low for marker in _NON_TEXT_MARKERS)


def _model_rank(name):
    """Sort key: fast free-tier text models first, heavy / paid ones last."""
    low = name.lower()
    m = re.search(r"(\d+(?:\.\d+)?)", low)
    version = float(m.group(1)) if m else 0.0
    # previews, experiments and dated snapshots (e.g. -001, -05-20) go later
    unstable = ("preview" in low or "exp" in low
                or re.search(r"-\d{3}$|-\d{2}-\d{2}", low) is not None)
    if low.startswith("gemma"):
        group = 6
    elif low == "gemini-flash-latest":
        group = 0
    elif "flash" in low and "lite" not in low:
        group = 2 if unstable else 1
    elif "flash" in low and "lite" in low:
        group = 3
    elif "pro" in low:
        group = 5
    else:
        group = 4
    return (group, -version, low)


def list_text_models(key, timeout=8):
    """ListModels for one key, filtered to text-generation models."""
    found, page_token = [], ""
    for _ in range(5):  # pagination guard
        url = GEMINI_BASE.rstrip("/") + "?pageSize=1000"
        if page_token:
            url += "&pageToken=" + urllib.parse.quote(page_token)
        req = urllib.request.Request(url, headers={"x-goog-api-key": key.strip()})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("models") or []:
            if _is_text_model(m):
                name = m["name"].replace("models/", "")
                if name not in found:
                    found.append(name)
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return sorted(found, key=_model_rank)


def get_text_models(force=False):
    """Discovered text models (cached per warm instance)."""
    st = _MODEL_STATE
    if _ENV_MODELS:
        if st["source"] != "env":
            st.update(models=list(_ENV_MODELS), source="env", fetched_at=time.time())
        return st["models"]
    fresh = time.time() - st["fetched_at"] < MODEL_LIST_TTL_SEC
    if st["models"] and fresh and not force:
        return st["models"]
    for key in GEMINI_KEYS:
        try:
            models = list_text_models(key)
        except Exception:
            continue
        if models:
            st.update(models=models, source="api", fetched_at=time.time())
            if st["current"] not in models:
                st["current"] = None
            return models
    # Discovery failed: keep the previous list if any, else the static one.
    if not st["models"]:
        st.update(models=list(_FALLBACK_MODELS), source="fallback", fetched_at=time.time())
    return st["models"]


def _ring_order(models):
    """Models in ring order, starting from the last model that answered."""
    cur = _MODEL_STATE["current"]
    start = models.index(cur) if cur in models else 0
    order = [models[(start + i) % len(models)] for i in range(len(models))]
    now = time.time()
    ready = [m for m in order if _MODEL_COOLDOWN.get(m, 0) <= now]
    return ready or order  # if everything is cooling down, try anyway


def _cooldown_for(e):
    """How long to skip a model after this error (seconds)."""
    msg = (e.message or "").lower()
    if e.kind == "model":
        return MODEL_LIST_TTL_SEC          # not available for this key
    if e.kind == "quota":
        if "limit: 0" in msg or "limit:0" in msg:
            return MODEL_LIST_TTL_SEC      # no free quota for this model at all
        if "perday" in msg or "per day" in msg or "daily" in msg:
            return MODEL_LIST_TTL_SEC      # daily quota used up
        return 30                          # per-minute limit
    if e.kind == "transient":
        return 15
    return 0


def call_gemini(prompt):
    """Send a prompt through the model carousel.

    Ring of discovered text models; for each model every key is tried
    (keys from different Google Cloud projects have separate quotas).
    The model that succeeds becomes the first one for the next request.
    """
    if not GEMINI_KEYS:
        raise GeminiFail(False, "NO_KEYS")

    started = time.time()
    models = get_text_models()
    dead_keys = set()
    errors = []  # (model, key_index, GeminiFail)

    for model in _ring_order(models):
        for key_i, key in enumerate(GEMINI_KEYS):
            if key_i in dead_keys:
                continue
            remaining = GEMINI_BUDGET_SEC - (time.time() - started)
            if remaining < 3:
                raise GeminiFail(False, _summarize_gemini_errors(errors), 0, _dominant_kind(errors))
            try:
                text = _gemini_request(
                    model, key, prompt,
                    timeout=min(30, remaining),
                    json_mode=model not in _NO_JSON_MODE,
                )
            except GeminiFail as e:
                if e.kind == "json_mode":
                    # e.g. Gemma: retry the same model without JSON mode.
                    _NO_JSON_MODE.add(model)
                    try:
                        text = _gemini_request(model, key, prompt, timeout=min(30, remaining), json_mode=False)
                    except GeminiFail as e2:
                        e = e2
                    else:
                        _MODEL_STATE["current"] = model
                        _MODEL_COOLDOWN.pop(model, None)
                        return text
                errors.append((model, key_i, e))
                if e.kind == "key":
                    dead_keys.add(key_i)
                    continue
                if e.kind == "model":
                    break  # model missing: skip remaining keys for it
                if not e.retryable:
                    raise
                continue
            _MODEL_STATE["current"] = model
            _MODEL_COOLDOWN.pop(model, None)
            return text
        # every key failed for this model -> cool it down, go to next in ring
        model_errors = [e for m, _, e in errors if m == model]
        if model_errors:
            cd = min(_cooldown_for(e) for e in model_errors) if model_errors else 0
            if cd:
                _MODEL_COOLDOWN[model] = time.time() + cd
        if len(dead_keys) == len(GEMINI_KEYS):
            break

    raise GeminiFail(False, _summarize_gemini_errors(errors), 0, _dominant_kind(errors))


def _dominant_kind(errors):
    kinds = [e.kind for _, _, e in errors]
    for k in ("key", "quota", "transient", "model", "bad_response"):
        if kinds and all(x in (k, "model") for x in kinds) and k in kinds:
            return k
    return "quota" if "quota" in kinds else (kinds[-1] if kinds else "")


def _summarize_gemini_errors(errors):
    if not errors:
        return "No Gemini attempt could be made (time budget exhausted)."
    kind = _dominant_kind(errors)
    tried = len(errors)
    sample = next((e.message for _, _, e in errors if e.kind == kind), errors[-1][2].message)
    if kind == "quota":
        head = "All Gemini models returned 429 (quota / rate limit exceeded)."
    elif kind == "key":
        head = "Gemini rejected the API key (401/403). Check GEMINI_API_KEY."
    elif kind == "transient":
        head = "Gemini is temporarily unavailable (5xx / timeout)."
    elif kind == "model":
        head = "None of the configured Gemini models is available for this key."
    else:
        head = "Gemini request failed."
    return "%s Attempts: %d. Google says: %s" % (head, tried, sample[:260])


GEMINI_QUOTA_HELP = {
    "title": "Gemini quota exceeded",
    "steps": [
        "Free Gemini limits are counted per Google Cloud project and per model; "
        "daily limits reset at midnight Pacific time.",
        "Check your live limits: https://aistudio.google.com/rate-limit",
        "Wait a minute and try again, or try again after the daily reset.",
        "Add GEMINI_API_KEY_2 / GEMINI_API_KEY_3 created in DIFFERENT Google "
        "Cloud projects (keys from the same project share one quota), then redeploy.",
        "Or enable billing for the project in AI Studio to get higher limits.",
    ],
}

GEMINI_KEY_HELP = {
    "title": "Gemini API key problem",
    "steps": [
        "Create a key at https://aistudio.google.com/apikey (new keys start with 'AQ.').",
        "Use 'Copy key' in the key details - the list shows a shortened, masked value.",
        "In Vercel set GEMINI_API_KEY to that value and redeploy.",
    ],
}


class GeminiStepError(RuntimeError):
    """RuntimeError that also carries setup/help steps for the UI."""

    def __init__(self, message, setup=None):
        super().__init__(message)
        self.setup = setup


def _gemini_step_error(step, e):
    setup = None
    if e.kind == "quota":
        setup = GEMINI_QUOTA_HELP
    elif e.kind == "key":
        setup = GEMINI_KEY_HELP
    return GeminiStepError("Gemini %s failed: %s" % (step, e.message), setup)


def extract_json(text):
    """Robustly pull a JSON object out of a model response."""
    if not text:
        raise ValueError("empty model response")
    text = text.strip()
    # strip ```json ... ``` fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON object found in model response")


# ---------------------------------------------------------------------------
# Step 2 — niche + search queries
# ---------------------------------------------------------------------------


def determine_niche(channel, videos):
    titles = [v["title"] for v in videos if v.get("title")]
    prompt = (
        "You are a YouTube growth strategist. Analyse the channel below and "
        "reply with ONLY valid minified JSON (no markdown, no commentary).\n"
        'Return exactly this shape:\n'
        '{"niche": "<precise niche in 3-7 words, in the channel language>", '
        '"queries": ["<search query 1>", "<search query 2>", '
        '"<search query 3>", "<search query 4>"]}\n'
        "The 4 queries must be YouTube search phrases (in the channel "
        "language) that surface competing channels. Do not change the "
        "channel language.\n\n"
        "Channel title: " + (channel.get("title") or "") + "\n"
        "Channel description: " + (channel.get("description") or "")[:800] + "\n"
        "Recent video titles:\n"
        + "\n".join("- " + t for t in titles)
        + "\n"
    )
    try:
        raw = call_gemini(prompt)
        data = extract_json(raw)
    except GeminiFail as e:
        if e.message == "NO_KEYS":
            raise RuntimeError(
                "GEMINI_API_KEY (and optionally GEMINI_API_KEY_2/_3) is not "
                "configured in Vercel. Add it in Project Settings -> "
                "Environment Variables, then redeploy."
            )
        raise _gemini_step_error("niche analysis", e)
    except Exception as e:
        raise RuntimeError("Gemini niche analysis failed: %s" % e)

    niche = str(data.get("niche") or "").strip() or (channel.get("title") or "Creator")
    queries = data.get("queries") or []
    if not isinstance(queries, list):
        queries = []
    queries = [str(q).strip() for q in queries if str(q).strip()][:4]
    while len(queries) < 4:
        queries.append(channel.get("title") or "YouTube")
    return niche, queries


# ---------------------------------------------------------------------------
# Step 3 — competitors
# ---------------------------------------------------------------------------


def find_candidates(queries, user_channel_id, per_query=12):
    seen = {}
    for q in queries:
        try:
            data = yt_get(
                "search",
                {
                    "part": "snippet",
                    "q": q,
                    "type": "video",
                    "order": "viewCount",
                    "maxResults": per_query,
                },
            )
        except Exception:
            continue
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            cid = sn.get("channelId")
            if not cid or cid == user_channel_id:
                continue
            if cid not in seen:
                thumbs = sn.get("thumbnails", {})
                seen[cid] = {
                    "channelId": cid,
                    "title": sn.get("channelTitle", ""),
                    "avatar": thumbs.get("high", {}).get("url")
                    or thumbs.get("default", {}).get("url"),
                }
    return list(seen.values())


def enrich_channels(channels):
    ids = [c["channelId"] for c in channels if c.get("channelId")]
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        try:
            data = yt_get(
                "channels",
                {
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(chunk),
                },
            )
        except Exception:
            continue
        for it in data.get("items", []):
            cid = it["id"]
            sn = it.get("snippet", {})
            st = it.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            out[cid] = {
                "channelId": cid,
                "title": sn.get("title", ""),
                "avatar": thumbs.get("high", {}).get("url")
                or thumbs.get("default", {}).get("url"),
                "subscribers": fmt_int(st.get("subscriberCount")),
                "views": fmt_int(st.get("viewCount")),
                "uploads": it.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", ""),
            }
    result = []
    for c in channels:
        e = out.get(c["channelId"])
        if e:
            e["avatar"] = e.get("avatar") or c.get("avatar")
            result.append(e)
    # Strongest competitors: rank by subscribers, tie-break by views.
    result.sort(key=lambda c: (c.get("subscribers", 0), c.get("views", 0)), reverse=True)
    return result[:5]


# ---------------------------------------------------------------------------
# Step 4 — outliers + ideas
# ---------------------------------------------------------------------------


def analyze_competitor(comp):
    videos = get_videos_from_playlist(comp.get("uploads"), 12)
    if not videos:
        comp["bestVideo"] = None
        comp["outliers"] = []
        return comp
    avg = sum(v["views"] for v in videos) / len(videos)
    threshold = max(avg * 2.0, 1)
    outliers = [v for v in videos if v["views"] >= threshold]
    outliers.sort(key=lambda v: v["views"], reverse=True)
    best = max(videos, key=lambda v: v["views"])
    comp["bestVideo"] = {
        "title": best["title"],
        "url": best["url"],
        "views": best["views"],
    }
    comp["outliers"] = outliers
    return comp


def generate_ideas(niche, lang_hint, competitors):
    lines = []
    for c in competitors:
        for o in (c.get("outliers") or [])[:3]:
            lines.append(
                "- [%s] %s — %s views"
                % (c.get("title", ""), o.get("title", ""), format(o.get("views", 0)))
            )
    if not lines:
        for c in competitors:
            bv = c.get("bestVideo")
            if bv:
                lines.append(
                    "- [%s] %s — %s views"
                    % (c.get("title", ""), bv.get("title", ""), format(bv.get("views", 0)))
                )
    outlier_text = "\n".join(lines) or "(no strong outliers found)"

    prompt = (
        "You are a YouTube scriptwriter. Reply with ONLY valid minified JSON "
        "(no markdown, no commentary).\n"
        'Return exactly: {"ideas": [{"title": "<clickable video title>", '
        '"topic": "<one-sentence angle or description>"}]}\n'
        "Write ALL text in this language: " + (lang_hint or "the channel language") + ".\n"
        "Make the 10 titles punchy and clickable, and the topics concrete. "
        "Base them on what already works for similar channels.\n\n"
        "Channel niche: " + niche + "\n\n"
        "Viral videos from competing channels (use as inspiration):\n"
        + outlier_text
        + "\n"
    )
    try:
        raw = call_gemini(prompt)
        data = extract_json(raw)
    except GeminiFail as e:
        if e.message == "NO_KEYS":
            raise RuntimeError(
                "GEMINI_API_KEY is not configured in Vercel. Add it in "
                "Project Settings -> Environment Variables, then redeploy."
            )
        raise _gemini_step_error("idea generation", e)
    except Exception as e:
        raise RuntimeError("Gemini idea generation failed: %s" % e)

    ideas = data.get("ideas") or []
    if not isinstance(ideas, list):
        ideas = []
    normalized = []
    for it in ideas:
        if isinstance(it, str):
            normalized.append({"title": it, "topic": ""})
        elif isinstance(it, dict):
            title = (
                it.get("title") or it.get("headline") or it.get("name") or ""
            )
            topic = (
                it.get("topic") or it.get("description") or it.get("idea") or ""
            )
            normalized.append({"title": str(title), "topic": str(topic)})
    normalized = [x for x in normalized if x["title"].strip()]
    return normalized[:10]


def format(n):
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 1_000_000_000:
        return ("%.1fB" % (n / 1_000_000_000)).rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1_000_000)).rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return ("%.1fK" % (n / 1_000)).rstrip("0").rstrip(".") + "K"
    return str(n)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _looks_like_oauth_value(value):
    """Detect common mix-ups pasted instead of an API key.

    Does not require the 'AIza' prefix, so a future key format is not blocked.
    """
    v = (value or "").strip()
    if v.startswith("ya29."):
        return "an OAuth access token (ya29...)"
    if v.startswith("gAWV") or v.startswith("gASV"):
        return "saved OAuth credentials (base64 pickle of google.oauth2.credentials)"
    if v.startswith("{") or '"token"' in v or '"refresh_token"' in v:
        return "a JSON credentials blob"
    if v.startswith("GOCSPX-") or v.endswith(".apps.googleusercontent.com"):
        return "an OAuth client secret / client ID"
    if any(c.isspace() for c in v):
        return "a value with spaces or line breaks"
    return None


def analyze(url):
    if not YT_KEY:
        return {
            "success": False,
            "error": "YOUTUBE_API_KEY environment variable is not set.",
            "setup": {
                "title": "Enable the free YouTube Data API v3",
                "steps": [
                    "Open Google Cloud Console and create a project.",
                    "Enable 'YouTube Data API v3' for that project.",
                    "Create an API key under APIs & Services -> Credentials.",
                    "In Vercel: Project Settings -> Environment Variables, add "
                    "YOUTUBE_API_KEY and at least one GEMINI_API_KEY.",
                    "Redeploy the project.",
                ],
            },
        }

    bad_kind = _looks_like_oauth_value(YT_KEY)
    if bad_kind:
        return {
            "success": False,
            "error": "YOUTUBE_API_KEY contains %s, not an API key. "
            "Reading public YouTube data needs a plain API key "
            "(usually starts with 'AIza', 39 characters)." % bad_kind,
            "setup": {
                "title": "Create a YouTube Data API v3 key",
                "steps": [
                    "Google Cloud Console -> APIs & Services -> Credentials.",
                    "Create credentials -> API key (NOT 'OAuth client ID').",
                    "In Vercel, replace YOUTUBE_API_KEY with that key and redeploy.",
                ],
            },
        }

    try:
        channel_id = resolve_channel_id(url)
        channel = get_channel(channel_id)
        videos = get_videos_from_playlist(channel.get("uploads"), 10)

        niche, queries = determine_niche(channel, videos)

        candidates = find_candidates(queries, channel_id, 12)
        competitors = enrich_channels(candidates)
        if not competitors:
            raise RuntimeError(
                "No competitor channels found for this niche. Try a different "
                "channel or check the search queries."
            )
        competitors = [analyze_competitor(c) for c in competitors]

        lang_hint = channel.get("language") or "the same language as the channel content"
        ideas = generate_ideas(niche, lang_hint, competitors)

        return {
            "success": True,
            "channel": {
                "id": channel["id"],
                "title": channel["title"],
                "description": channel["description"],
                "avatar": channel["avatar"],
                "subscribers": channel["subscribers"],
                "views": channel["views"],
                "videos": videos,
            },
            "niche": niche,
            "competitors": [
                {
                    "title": c.get("title", ""),
                    "url": "https://www.youtube.com/channel/" + c.get("channelId", ""),
                    "avatar": c.get("avatar"),
                    "subscribers": c.get("subscribers", 0),
                    "views": c.get("views", 0),
                    "bestVideo": c.get("bestVideo"),
                }
                for c in competitors
            ],
            "ideas": ideas,
            "language": channel.get("language") or "",
        }
    except RuntimeError as e:
        out = {"success": False, "error": str(e)}
        if getattr(e, "setup", None):
            out["setup"] = e.setup
        return out
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {
            "success": False,
            "error": "Unexpected error: %s" % e,
        }


# ---------------------------------------------------------------------------
# Vercel Python handler (classic @vercel/python runtime)
# ---------------------------------------------------------------------------


from http.server import BaseHTTPRequestHandler  # noqa: E402


def _send_json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):  # noqa: N801 - required name for Vercel
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        if "models" in urllib.parse.parse_qs(query):
            # Diagnostics: which text models the carousel will use.
            # Never exposes the keys themselves.
            models = get_text_models(force=True) if GEMINI_KEYS else []
            now = time.time()
            _send_json(self, {
                "status": "ok",
                "source": _MODEL_STATE["source"] or "none",
                "current": _MODEL_STATE["current"],
                "keys_configured": len(GEMINI_KEYS),
                "text_models": models,
                "cooling_down": sorted(m for m, t in _MODEL_COOLDOWN.items() if t > now),
            })
            return
        _send_json(self, {"status": "ok", "service": "NicheRadar AI"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            url = (data.get("url") or "").strip()
            if not url:
                _send_json(
                    self,
                    {"success": False, "error": "Missing 'url' in request body."},
                )
                return
            result = analyze(url)
            _send_json(self, result)
        except Exception as e:
            _send_json(self, {"success": False, "error": "Server error: %s" % e})
