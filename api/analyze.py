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

# Carousel of models and keys. We rotate both to dodge 429/503 errors.
GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-pro-latest",
]

# Rotating cursor so every cold start does not hammer the same key/model.
_GEMINI_CURSOR = {"i": 0}

YT_BASE = "https://www.googleapis.com/youtube/v3/"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"


class GeminiFail(Exception):
    """Raised when a Gemini call fails."""

    def __init__(self, retryable, message):
        self.retryable = retryable
        self.message = message
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


def _gemini_request(model, key, prompt):
    url = GEMINI_BASE + model + ":generateContent?key=" + key
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 1,
            "maxOutputTokens": 2048,
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 503):
            raise GeminiFail(True, "HTTP %d" % e.code)
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        raise GeminiFail(False, "HTTP %d: %s" % (e.code, detail[:300]))
    except Exception as e:  # network / timeout
        raise GeminiFail(True, str(e))

    try:
        j = json.loads(raw)
        return j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        raise GeminiFail(False, "Unexpected Gemini response: %s" % str(e)[:200])


def call_gemini(prompt):
    """Try every (key, model) combination, rotating on retryable errors."""
    if not GEMINI_KEYS:
        raise GeminiFail(False, "NO_KEYS")

    combos = [(m, k) for m in range(len(GEMINI_MODELS)) for k in range(len(GEMINI_KEYS))]
    start = _GEMINI_CURSOR["i"] % len(combos)
    last_err = None

    for off in range(len(combos)):
        idx = (start + off) % len(combos)
        _GEMINI_CURSOR["i"] = (idx + 1) % len(combos)
        model_i, key_i = combos[idx]
        model = GEMINI_MODELS[model_i]
        key = GEMINI_KEYS[key_i]
        try:
            return _gemini_request(model, key, prompt)
        except GeminiFail as e:
            last_err = e
            if e.retryable:
                continue
            # Fatal: do not burn the remaining combinations.
            raise

    if last_err is not None:
        raise last_err
    raise GeminiFail(True, "All Gemini attempts failed")


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
        raise RuntimeError("Gemini niche analysis failed: %s" % e.message)
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
        raise RuntimeError("Gemini idea generation failed: %s" % e.message)
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
        return {"success": False, "error": str(e)}
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


class handler:  # noqa: N801 - required name for Vercel
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
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
