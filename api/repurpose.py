# -*- coding: utf-8 -*-
"""
Content Repurposer — Vercel Serverless Function (Python, standard library only).

POST /api/repurpose   {"url": "https://youtu.be/...", "notes": "...", "lang": "auto|en|ru|ua"}

1. Video data (each source is optional, nothing here is fatal):
   * YouTube Data API v3 (YOUTUBE_API_KEY): title, channel, full description, tags,
     chapters (timestamps parsed from the description), language;
   * oEmbed (no key) as a fallback for title + channel;
   * public subtitles via the InnerTube player API / watch page. YouTube often blocks
     these requests from cloud IPs (Vercel) — then we simply continue with the
     title, description, chapters and the user's `notes`.
   If YouTube refuses and SUBS_PROXY_URL is set, the subtitles are requested from our
   backup server: GET {SUBS_PROXY_URL}?v={video_id} with header X-Token: {SUBS_PROXY_TOKEN}
   (timeout 25 s); a non-empty JSON "text" is used as the full transcript.
   Only `notes` without a link also works.
   When there are no subtitles, Gemini WATCHES the public video itself (YouTube URL as
   fileData, low media resolution, first VIDEO_WATCH_LIMIT seconds) — Google fetches the
   video on its side, so the cloud-IP block does not matter. If that fails, the pack is
   written from the text sources only.
2. Gemini (shared carousel in api/_gemini.py) writes the content pack as strict JSON:
   telegram_post, seo_article {h1, meta_description, intro, sections[3-4], conclusion},
   social_posts[5] (insight, steps, mistake, tip, question). Everything is validated here.

GET /api/repurpose?probe=<video url>  -> video data + subtitle status only (no Gemini),
useful to check whether subtitles are reachable from the server.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gemini import (  # noqa: E402
    BUSY_HELP,
    KEY_HELP,
    QUOTA_HELP,
    REQUEST,
    GeminiFail,
    call_gemini,
    extract_json,
    start_request,
)

YT_KEY = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
YT_BASE = "https://www.googleapis.com/youtube/v3/"
# Our backup subtitle server (used only when YouTube refuses the direct request):
# GET {SUBS_PROXY_URL}?v={video_id}  with header  X-Token: {SUBS_PROXY_TOKEN}  ->  {"text": "..."}
SUBS_PROXY_URL = (os.environ.get("SUBS_PROXY_URL") or "").strip()
SUBS_PROXY_TOKEN = (os.environ.get("SUBS_PROXY_TOKEN") or "").strip()
SUBS_PROXY_TIMEOUT = 25
UA_HEADER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

REQUEST_BUDGET = 140    # whole request; vercel.json maxDuration for this function: 150
GEMINI_BUDGET = 62      # text-only Gemini call
VIDEO_BUDGET = 80       # Gemini watching the video (all attempts)
VIDEO_TRY_TIMEOUT = 60  # one model watching the video
VIDEO_WATCH_LIMIT = 1200  # seconds of video Gemini watches (longer videos are clipped)
VIDEO_FPS = 0.2         # frames per second sampled from the video (speech is what matters)
FETCH_TIMEOUT = 7       # each YouTube request
TRANSCRIPT_LIMIT = 14000
DESCRIPTION_LIMIT = 5000
NOTES_LIMIT = 20000

LANG_NAMES = {"en": "English", "ru": "Russian", "ua": "Ukrainian"}
ISO_NAMES = {
    "de": "German", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese",
    "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian", "kk": "Kazakh", "be": "Belarusian",
    "nl": "Dutch", "cs": "Czech", "ro": "Romanian", "hi": "Hindi", "id": "Indonesian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic",
}
ANGLES = ["insight", "steps", "mistake", "tip", "question"]
CONCLUSION_H2 = {"en": "Conclusion", "ru": "Вывод", "ua": "Висновок"}

TG_MIN, TG_MAX = 800, 1300
ART_MIN, ART_MAX = 2500, 3500
POST_MAX = 280

CYRILLIC = re.compile(u"[\u0400-\u04FF]")
RU_ONLY = re.compile(u"[ыэъёЫЭЪЁ]")
UA_ONLY = re.compile(u"[іїєґІЇЄҐ]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def lang_ok(text, lang):
    if lang == "en":
        return not CYRILLIC.search(text or "")
    if lang == "ua":
        return not RU_ONLY.search(text or "")
    if lang == "ru":
        return not UA_ONLY.search(text or "")
    return True


def detect_lang(text):
    """en / ru / ua from the script of a text ('' when unclear)."""
    text = text or ""
    cyr = len(CYRILLIC.findall(text))
    lat = len(re.findall(r"[A-Za-z]", text))
    if cyr > lat * 0.5 and cyr >= 20:
        ua, ru = len(UA_ONLY.findall(text)), len(RU_ONLY.findall(text))
        if ua > ru:
            return "ua"
        return "ru"
    if lat >= 20:
        return "en"
    return ""


def iso_to_lang(code):
    c = (code or "").lower().split("-")[0]
    if c == "uk":
        return "ua"
    return c


def extract_video_id(url):
    if not url:
        return None
    m = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url.strip()):
        return url.strip()
    return None


def http_get(url, timeout=FETCH_TIMEOUT, headers=None):
    h = {"User-Agent": UA_HEADER, "Accept-Language": "en-US,en;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def http_post_json(url, payload, timeout=FETCH_TIMEOUT, headers=None):
    h = {"User-Agent": UA_HEADER, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def parse_chapters(description):
    """Timestamps like '0:00 Intro' / '01:02:03 - Topic' from the description."""
    chapters = []
    for line in (description or "").splitlines():
        m = re.match(r"^\s*[\(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\)\]]?\s*[-–—:|.]?\s*(.{2,120})$", line)
        if m:
            title = m.group(2).strip(" -–—|")
            if title:
                chapters.append({"time": m.group(1), "title": title})
    return chapters if len(chapters) >= 2 else []


# ---------------------------------------------------------------------------
# Video metadata
# ---------------------------------------------------------------------------


def fetch_meta_api(video_id):
    params = urllib.parse.urlencode({"part": "snippet,contentDetails", "id": video_id, "key": YT_KEY})
    data = json.loads(http_get(YT_BASE + "videos?" + params, timeout=10))
    items = data.get("items") or []
    if not items:
        return None
    sn = items[0].get("snippet") or {}
    cd = items[0].get("contentDetails") or {}
    return {
        "duration": iso_duration(cd.get("duration") or ""),
        "live": (sn.get("liveBroadcastContent") or "none") != "none",
        "title": sn.get("title") or "",
        "channel": sn.get("channelTitle") or "",
        "description": sn.get("description") or "",
        "tags": (sn.get("tags") or [])[:20],
        "language": iso_to_lang(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or ""),
    }


def iso_duration(value):
    """'PT1H2M3S' -> 3723 (0 when unknown)."""
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def fetch_meta_oembed(video_id):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        "https://www.youtube.com/watch?v=" + video_id, safe="")
    data = json.loads(http_get(url))
    return {"title": data.get("title") or "", "channel": data.get("author_name") or "",
            "description": "", "tags": [], "language": "", "duration": 0, "live": False}


# ---------------------------------------------------------------------------
# Subtitles (best effort — YouTube may block cloud IPs)
# ---------------------------------------------------------------------------

INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
INNERTUBE_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38",
                    "androidSdkVersion": 34, "hl": "en", "gl": "US"}


def _tracks_from_player(player):
    if not isinstance(player, dict):
        return [], ""
    status = ((player.get("playabilityStatus") or {}).get("status") or "")
    tracks = (((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
              .get("captionTracks") or [])
    return tracks, status


def caption_tracks(video_id):
    """Return (tracks, how) or raise RuntimeError with a short reason."""
    reason = ""
    try:
        player = http_post_json(INNERTUBE_URL, {"context": {"client": INNERTUBE_CLIENT}, "videoId": video_id},
                                headers={"X-Youtube-Client-Name": "3",
                                         "X-Youtube-Client-Version": INNERTUBE_CLIENT["clientVersion"]})
        tracks, status = _tracks_from_player(player)
        if tracks:
            return tracks, "innertube"
        reason = "player status %s, no caption tracks" % (status or "?")
    except Exception as e:  # blocked / network
        reason = "innertube: %s" % str(e)[:80]
    try:
        page = http_get("https://www.youtube.com/watch?v=%s&hl=en" % video_id)
        if "consent.youtube.com" in page[:3000] or 'class="g-recaptcha"' in page:
            raise RuntimeError("YouTube asked for a captcha/consent")
        m = re.search(r'"captionTracks":(\[.*?\])', page)
        if m:
            tracks = json.loads(m.group(1))
            if tracks:
                return tracks, "watch_page"
        if "LOGIN_REQUIRED" in page or "confirm you" in page:
            raise RuntimeError("YouTube asked to sign in (bot check)")
        reason += "; watch page: no caption tracks"
    except Exception as e:
        reason += "; watch page: %s" % str(e)[:80]
    raise RuntimeError(reason.strip("; "))


def pick_track(tracks, want):
    def lang_of(t):
        return iso_to_lang(t.get("languageCode") or "")
    manual = [t for t in tracks if t.get("kind") != "asr"]
    for pool in (manual, tracks):
        for t in pool:
            if want and lang_of(t) == want:
                return t
    return (manual or tracks)[0]


def parse_caption_xml(xml):
    parts = re.findall(r"<(?:text|p)\b[^>]*>(.*?)</(?:text|p)>", xml, re.S)
    out = []
    for p in parts:
        p = re.sub(r"<[^>]+>", "", p)          # srv3 <s> word tags
        p = html.unescape(html.unescape(p)).replace("\n", " ").strip()
        if p and not (p.startswith("[") and p.endswith("]")):
            out.append(p)
    text = re.sub(r"\s+", " ", " ".join(out)).strip()
    return text


def fetch_transcript(video_id, want_lang=""):
    tracks, how = caption_tracks(video_id)
    track = pick_track(tracks, want_lang)
    base = (track.get("baseUrl") or "").replace("&fmt=srv3", "")
    if not base:
        raise RuntimeError("caption track without URL")
    xml = http_get(base)
    text = parse_caption_xml(xml)
    if len(text) < 80:
        raise RuntimeError("subtitles are empty (YouTube may require a session token)")
    return {
        "text": text[:TRANSCRIPT_LIMIT],
        "language": iso_to_lang(track.get("languageCode") or ""),
        "auto": track.get("kind") == "asr",
        "via": how,
        "truncated": len(text) > TRANSCRIPT_LIMIT,
    }


def fetch_transcript_proxy(video_id):
    """Backup subtitle server. Returns a transcript dict or raises RuntimeError."""
    sep = "&" if "?" in SUBS_PROXY_URL else "?"
    url = SUBS_PROXY_URL + sep + urllib.parse.urlencode({"v": video_id})
    headers = {"Accept": "application/json"}
    if SUBS_PROXY_TOKEN:
        headers["X-Token"] = SUBS_PROXY_TOKEN
    started = time.time()
    try:
        raw = http_get(url, timeout=SUBS_PROXY_TIMEOUT, headers=headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            body = ""
        raise RuntimeError("HTTP %d %s" % (e.code, body.strip()))
    except Exception as e:  # timeout / DNS / TLS / connection refused
        raise RuntimeError("%s after %.1fs: %s" % (type(e).__name__, time.time() - started, str(e)[:100]))
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("answer is not JSON: %r" % raw[:80])
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        keys = ", ".join(list(data)[:8]) if isinstance(data, dict) else type(data).__name__
        raise RuntimeError("empty 'text' (got: %s)" % keys)
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "text": text[:TRANSCRIPT_LIMIT],
        "language": iso_to_lang(str(data.get("lang") or data.get("language") or "")),
        "auto": bool(data.get("auto")) or data.get("kind") == "asr",
        "via": "proxy",
        "truncated": len(text) > TRANSCRIPT_LIMIT,
    }


# ---------------------------------------------------------------------------
# Collect all sources
# ---------------------------------------------------------------------------


def collect(url, notes):
    """Gather everything we can about the video. Never raises for YouTube problems."""
    out = {"video": None, "meta": None, "transcript": None, "chapters": [], "warnings": [],
           "sources": {"api": False, "oembed": False, "description": False, "chapters": 0,
                       "subtitles": "none", "notes": bool(notes)}}
    vid = extract_video_id(url) if url else None
    if url and not vid:
        out["error"] = "bad_url"
        return out
    if not vid:
        return out

    meta = None
    if YT_KEY:
        try:
            meta = fetch_meta_api(vid)
            if meta is None:
                out["error"] = "not_found"
                return out
            out["sources"]["api"] = True
        except urllib.error.HTTPError as e:
            out["warnings"].append("youtube_api_%s" % e.code)
        except Exception:
            out["warnings"].append("youtube_api_failed")
    else:
        out["warnings"].append("no_youtube_key")
    if meta is None:
        try:
            meta = fetch_meta_oembed(vid)
            out["sources"]["oembed"] = True
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                out["error"] = "not_found"
                return out
            meta = None
        except Exception:
            meta = None
    if meta is None:
        meta = {"title": "", "channel": "", "description": "", "tags": [], "language": "",
                "duration": 0, "live": False}
        out["warnings"].append("metadata_unavailable")

    out["meta"] = meta
    out["chapters"] = parse_chapters(meta["description"])
    out["sources"]["description"] = len(meta["description"].strip()) > 40
    out["sources"]["chapters"] = len(out["chapters"])
    out["video"] = {
        "id": vid,
        "url": "https://www.youtube.com/watch?v=" + vid,
        "title": meta["title"],
        "channel": meta["channel"],
        "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
    }

    # subtitles: best effort, never fatal
    if REQUEST["deadline"] is None or REQUEST["deadline"] - time.time() > GEMINI_BUDGET + 6:
        try:
            out["transcript"] = fetch_transcript(vid, meta.get("language") or "")
            out["sources"]["subtitles"] = "auto" if out["transcript"]["auto"] else "manual"
        except Exception as e:
            reason = str(e)[:200]
            if SUBS_PROXY_URL:  # YouTube refused us -> ask our backup subtitle server
                try:
                    out["transcript"] = fetch_transcript_proxy(vid)
                except Exception as pe:
                    reason += "; proxy: %s" % str(pe)[:120]
            if out["transcript"]:
                out["sources"]["subtitles"] = "auto" if out["transcript"]["auto"] else "manual"
                out["sources"]["subtitles_via"] = "proxy"
            else:
                out["sources"]["subtitles"] = "blocked"
                out["subtitles_reason"] = reason
                out["warnings"].append("subtitles_unavailable")
    return out


def resolve_lang(requested, src, notes):
    if requested in LANG_NAMES:
        return requested
    meta = src.get("meta") or {}
    tr = src.get("transcript") or {}
    for cand in (meta.get("language"), tr.get("language")):
        if cand:
            return cand
    sample = " ".join([meta.get("title", ""), meta.get("description", "")[:1500], notes[:3000],
                       tr.get("text", "")[:1500]])
    return detect_lang(sample) or "en"


def lang_name(code):
    return LANG_NAMES.get(code) or ISO_NAMES.get(code) or "the language of the source"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def build_prompt(src, notes, lang, watch=False):
    meta = src.get("meta") or {}
    tr = src.get("transcript") or {}
    ln = lang_name(lang)
    purity = {
        "en": "Write everything in English only, with no Cyrillic letters (translate titles if needed).",
        "ru": "Write everything in Russian only; never use the Ukrainian letters і ї є ґ.",
        "ua": "Write everything in Ukrainian only; never use the Russian letters ы э ъ ё "
              "(translate Russian titles and quotes into Ukrainian).",
    }.get(lang, "Write everything in %s." % ln)
    parts = []
    if meta.get("title"):
        parts.append("VIDEO TITLE: " + meta["title"])
    if meta.get("channel"):
        parts.append("CHANNEL: " + meta["channel"])
    if src.get("chapters"):
        parts.append("CHAPTERS:\n" + "\n".join("%s %s" % (c["time"], c["title"]) for c in src["chapters"][:30]))
    if meta.get("description"):
        parts.append("DESCRIPTION:\n" + meta["description"][:DESCRIPTION_LIMIT])
    if meta.get("tags"):
        parts.append("TAGS: " + ", ".join(meta["tags"]))
    if tr.get("text"):
        parts.append("TRANSCRIPT (%s subtitles):\n%s" % ("auto" if tr.get("auto") else "manual", tr["text"]))
    if notes:
        parts.append("AUTHOR NOTES / TRANSCRIPT / KEY POINTS:\n" + notes[:NOTES_LIMIT])
    source = "\n\n".join(parts)
    watch_note = ""
    if watch:
        dur = int(meta.get("duration") or 0)
        watch_note = (
            "THE VIDEO ITSELF IS ATTACHED. Watch and listen to it: what is said and shown in the video is "
            "the PRIMARY source — use its real explanations, steps, tips and examples. The title, "
            "description and notes below are only context.\n")
        if dur > VIDEO_WATCH_LIMIT:
            watch_note += ("Only the first %d minutes of the %d-minute video are attached; use the chapters "
                           "and description for the rest.\n" % (VIDEO_WATCH_LIMIT // 60, dur // 60))
    return watch_note + (
        "You are an expert content marketer. Repurpose the YouTube video described below into a "
        "ready-to-publish content pack in %(ln)s.\n"
        "Use ONLY facts, steps and ideas present in the source. If the source is thin, stay general "
        "and practical — never invent numbers, quotes, names or results that are not in the source.\n"
        "Reply with ONLY valid minified JSON (no markdown fences, no commentary), exactly this shape:\n"
        '{"telegram_post":"","seo_article":{"h1":"","meta_description":"","intro":"",'
        '"sections":[{"h2":"","body":""}],"conclusion_h2":"","conclusion":""},'
        '"social_posts":[{"angle":"insight","text":""},{"angle":"steps","text":""},'
        '{"angle":"mistake","text":""},{"angle":"tip","text":""},{"angle":"question","text":""}]}\n\n'
        "Requirements:\n"
        "- telegram_post: %(tmin)d-%(tmax)d characters, plain text for a Telegram channel (NO markdown "
        "asterisks or #headers): a hooky first line with one emoji, an empty line, 3-5 bullet lines "
        "starting with an emoji or • that give the main value of the video, a short takeaway, and a "
        "call to action to watch the video (write [link] where the link goes). Use line breaks (\\n).\n"
        "- seo_article: a complete blog article of %(amin)d-%(amax)d characters in total. h1 with the "
        "main keyword; meta_description 140-160 characters; intro 1 paragraph; EXACTLY 3 or 4 sections, "
        "each with a descriptive h2 and a body of 2 developed paragraphs separated by \\n\\n; "
        "conclusion_h2 (e.g. 'Conclusion' translated) and a conclusion paragraph. No markdown inside fields.\n"
        "- social_posts: EXACTLY 5 posts for X (Twitter) / Threads / LinkedIn, each at most %(pmax)d "
        "characters, in this order of angles: 1 insight (provocative insight), 2 steps (short numbered "
        "step list with line breaks), 3 mistake (breakdown of a common mistake), 4 tip (one short "
        "actionable tip), 5 question (a question to the audience). Max 2 hashtags per post.\n"
        "- %(purity)s\n\n"
        "SOURCE:\n%(source)s\n"
    ) % {"ln": ln, "tmin": TG_MIN, "tmax": TG_MAX, "amin": ART_MIN, "amax": ART_MAX,
         "pmax": POST_MAX, "purity": purity, "source": source}


def _clean_text(s, limit):
    s = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    s = s.replace("**", "").replace("__", "")
    return s[:limit].strip()


def _trim_post(text):
    """Keep a social post within POST_MAX characters, cutting at a sentence/line end."""
    if len(text) <= POST_MAX:
        return text
    cut = text[:POST_MAX]
    best = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if best >= POST_MAX * 0.6:
        return cut[:best + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(" ,;:—–-") + "…"


def _trim_telegram(text):
    if len(text) <= TG_MAX + 200:
        return text
    paras = text.split("\n")
    out = ""
    for p in paras:
        if len(out) + len(p) + 1 > TG_MAX + 150:
            break
        out += (p + "\n")
    return out.strip() or text[:TG_MAX]


def article_markdown(a):
    md = ["# " + a["h1"], "", a["intro"], ""]
    for s in a["sections"]:
        md += ["## " + s["h2"], "", s["body"], ""]
    md += ["## " + a["conclusion_h2"], "", a["conclusion"]]
    return "\n".join(md).strip()


def article_html(a):
    e = html.escape
    out = ["<h1>%s</h1>" % e(a["h1"]), "<p>%s</p>" % e(a["intro"])]
    for s in a["sections"]:
        out.append("<h2>%s</h2>" % e(s["h2"]))
        out += ["<p>%s</p>" % e(p) for p in s["body"].split("\n\n") if p.strip()]
    out.append("<h2>%s</h2>" % e(a["conclusion_h2"]))
    out.append("<p>%s</p>" % e(a["conclusion"]))
    return "\n".join(out)


def normalize(data, lang):
    """Validate Gemini JSON. Returns (pack, problems[])."""
    problems = []
    if not isinstance(data, dict):
        return None, ["not an object"]
    tg = _clean_text(data.get("telegram_post"), 4000)
    tg = _trim_telegram(tg)
    if len(tg) < 500:
        problems.append("telegram_post too short (%d chars, need %d-%d)" % (len(tg), TG_MIN, TG_MAX))

    a = data.get("seo_article") if isinstance(data.get("seo_article"), dict) else {}
    secs = []
    for s in (a.get("sections") or [])[:4]:
        if isinstance(s, dict) and str(s.get("h2") or "").strip() and str(s.get("body") or "").strip():
            secs.append({"h2": _clean_text(s.get("h2"), 140).lstrip("# "), "body": _clean_text(s.get("body"), 2500)})
    article = {
        "h1": _clean_text(a.get("h1"), 160).lstrip("# "),
        "meta_description": _clean_text(a.get("meta_description"), 400),
        "intro": _clean_text(a.get("intro"), 1500),
        "sections": secs,
        "conclusion_h2": _clean_text(a.get("conclusion_h2"), 80).lstrip("# ") or CONCLUSION_H2.get(lang, "Conclusion"),
        "conclusion": _clean_text(a.get("conclusion"), 1500),
    }
    md = article["meta_description"]
    if len(md) > 160:
        cut = md[:158]
        md = (cut[:cut.rfind(" ")] if cut.rfind(" ") > 100 else cut).rstrip(" ,;:") + "…"
        article["meta_description"] = md
    if not article["h1"] or not article["intro"] or len(secs) < 3 or not article["conclusion"]:
        problems.append("seo_article incomplete (%d sections)" % len(secs))

    art_len = len(article_markdown(article))
    if art_len < 1500:
        problems.append("seo_article too short (%d chars, need %d-%d)" % (art_len, ART_MIN, ART_MAX))

    posts, by_angle = [], {}
    raw_posts = data.get("social_posts") if isinstance(data.get("social_posts"), list) else []
    for i, p in enumerate(raw_posts):
        if isinstance(p, str):
            p = {"text": p}
        if not isinstance(p, dict):
            continue
        text = _trim_post(_clean_text(p.get("text"), 1200))
        if len(text) < 20:
            continue
        angle = str(p.get("angle") or "").strip().lower()
        if angle not in ANGLES or angle in by_angle:
            angle = next((x for x in ANGLES if x not in by_angle), None)
        if angle is None:
            break
        by_angle[angle] = {"angle": angle, "text": text}
    posts = [by_angle[x] for x in ANGLES if x in by_angle]
    if len(posts) < 5:
        problems.append("only %d social posts" % len(posts))

    everything = " ".join([tg, article_markdown(article), article["meta_description"]] + [p["text"] for p in posts])
    if not lang_ok(everything, lang):
        problems.append("language purity (%s)" % lang)

    article["markdown"] = article_markdown(article)
    article["html"] = article_html(article)
    pack = {
        "telegram_post": tg,
        "seo_article": article,
        "social_posts": posts,
        "stats": {
            "telegram_chars": len(tg),
            "article_chars": len(article["markdown"]),
            "post_chars": [len(p["text"]) for p in posts],
        },
    }
    return pack, problems


def gemini_error(e):
    if e.message == "NO_KEYS":
        return {
            "success": False,
            "error": "GEMINI_API_KEY is not configured, so the content pack cannot be written.",
            "setup": {"title": "Add a free Gemini API key", "steps": [
                "Create a key at https://aistudio.google.com/apikey (new keys start with 'AQ.').",
                "In Vercel: Project Settings -> Environment Variables, add GEMINI_API_KEY "
                "(optionally GEMINI_API_KEY_2 / _3 from other Google Cloud projects).",
                "Redeploy the project."]},
        }
    setup = {"quota": QUOTA_HELP, "transient": BUSY_HELP, "key": KEY_HELP}.get(e.kind, BUSY_HELP)
    return {"success": False, "error": "Gemini could not write the content pack: " + e.message[:400],
            "setup": setup, "retry": e.kind in ("quota", "transient", "bad_response", "model", "")}


def generate(url, notes, lang_req):
    start_request(REQUEST_BUDGET)
    url = (url or "").strip()
    notes = (notes or "").strip()[:NOTES_LIMIT]
    if not url and len(notes) < 40:
        return {"success": False, "error": "Paste a YouTube video link, or at least a few sentences "
                "of text / transcript / key points (40+ characters)."}

    src = collect(url, notes)
    if src.get("error") == "bad_url" and len(notes) < 40:
        return {"success": False, "error": "This does not look like a YouTube video link.",
                "setup": {"title": "Supported links", "steps": [
                    "https://www.youtube.com/watch?v=VIDEO_ID",
                    "https://youtu.be/VIDEO_ID",
                    "https://www.youtube.com/shorts/VIDEO_ID",
                    "Or leave the link empty and paste your text, transcript or key points."]}}
    if src.get("error") == "not_found" and len(notes) < 40:
        return {"success": False, "error": "Video not found: it may be private, deleted or the ID is wrong."}
    if src.get("error") in ("bad_url", "not_found"):
        src["warnings"].append("video_ignored")
        src["video"] = None

    meta = src.get("meta") or {}
    substance = len(meta.get("description", "")) + len((src.get("transcript") or {}).get("text", "")) + len(notes)
    lang = resolve_lang(lang_req, src, notes)
    pack, problems, watched = None, [], False

    # 1) no subtitles -> let Gemini watch the video itself
    if src["video"] and not src.get("transcript") and not meta.get("live"):
        src["sources"]["video_ai"] = "failed"
        try:
            text = call_gemini(build_prompt(src, notes, lang, watch=True), budget=min(VIDEO_BUDGET, time_left() - 30),
                               media=video_media(src), try_timeout=VIDEO_TRY_TIMEOUT, hedge_after=30, max_parallel=2)
            pack, problems = normalize(extract_json(text), lang)
            if pack is not None:
                watched = True
                src["sources"]["video_ai"] = "watched"
                src["warnings"] = [w for w in src["warnings"] if w != "subtitles_unavailable"]
        except GeminiFail as e:
            src["video_ai_reason"] = str(e)[:300]
            if e.message == "NO_KEYS" or e.kind == "key":
                return gemini_error(e)
        except (ValueError, TypeError):
            src["video_ai_reason"] = "invalid JSON"

    if not watched:
        if src["video"] and not meta.get("title") and substance < 40:
            return {"success": False, "error": "Could not read this video from the server (YouTube blocked the "
                    "request). Paste the transcript or key points into the text field and try again."}
        if substance < 150:
            src["warnings"].append("thin_source")
        # 2) text-only pack (description, chapters, subtitles, notes)
        if time_left() < 20:
            return {"success": False, "error": "Gemini took too long to answer. Please press Generate again.",
                    "setup": BUSY_HELP, "retry": True}
        prompt = build_prompt(src, notes, lang)
        try:
            pack, problems = normalize(extract_json(
                call_gemini(prompt, budget=min(GEMINI_BUDGET, time_left() - 6))), lang)
        except GeminiFail as e:
            return gemini_error(e)
        except (ValueError, TypeError):
            pack, problems = None, ["invalid JSON"]

    # one corrective retry if the answer did not pass validation and time allows
    left = time_left()
    if problems and left > 30 and watched and pack is not None:
        # fix the draft written from the video with a cheap text-only call (no second video pass)
        draft = {"telegram_post": pack["telegram_post"],
                 "seo_article": {k: pack["seo_article"][k] for k in
                                 ("h1", "meta_description", "intro", "sections", "conclusion_h2", "conclusion")},
                 "social_posts": pack["social_posts"]}
        fix = ("\n\nDRAFT WRITTEN AFTER WATCHING THE VIDEO (keep its facts and content):\n%s\n\n"
               "THE DRAFT WAS REJECTED: %s. Return the complete corrected JSON and follow every requirement "
               "exactly.\n" % (json.dumps(draft, ensure_ascii=False), "; ".join(problems)))
        try:
            pack2, problems2 = normalize(extract_json(
                call_gemini(build_prompt(src, notes, lang) + fix, budget=left - 8)), lang)
            if pack2 is not None and len(problems2) < len(problems):
                pack, problems = pack2, problems2
        except (GeminiFail, ValueError, TypeError):
            pass
    elif problems and left > 30 and not watched:
        fix = ("\n\nYOUR PREVIOUS ANSWER WAS REJECTED: %s. Return the complete JSON again and follow "
               "every requirement exactly.\n" % "; ".join(problems))
        try:
            pack2, problems2 = normalize(extract_json(call_gemini(prompt + fix, budget=left - 8)), lang)
            if pack2 is not None and (pack is None or len(problems2) < len(problems)):
                pack, problems = pack2, problems2
        except (GeminiFail, ValueError, TypeError):
            pass
    if pack is None:
        return {"success": False, "error": "Gemini returned an invalid answer. Please try again.",
                "setup": BUSY_HELP, "retry": True}

    if problems:
        src["warnings"].append("quality: " + "; ".join(problems))
    if src["video"]:
        pack["telegram_post"] = pack["telegram_post"].replace("[link]", src["video"]["url"])
        pack["stats"]["telegram_chars"] = len(pack["telegram_post"])
    out = {
        "success": True,
        "lang": lang,
        "video": src["video"],
        "sources": src["sources"],
        "warnings": src["warnings"],
    }
    out.update(pack)
    return out


def time_left():
    return (REQUEST["deadline"] or (time.time() + REQUEST_BUDGET)) - time.time()


def video_media(src):
    """Extra Gemini parts: the public YouTube video itself (fetched by Google, not by us)."""
    vm = {"fps": VIDEO_FPS}
    dur = int((src.get("meta") or {}).get("duration") or 0)
    if dur > VIDEO_WATCH_LIMIT:
        vm["endOffset"] = "%ds" % VIDEO_WATCH_LIMIT
    return {"parts": [{"fileData": {"fileUri": src["video"]["url"]}, "videoMetadata": vm}],
            "generation": {"mediaResolution": "MEDIA_RESOLUTION_LOW"}}


def probe(url):
    start_request(REQUEST_BUDGET)  # same budget as a real run, so the subtitle step is really tried
    src = collect(url, "")
    return {
        "status": "ok",
        "youtube_key": bool(YT_KEY),
        "subs_proxy": bool(SUBS_PROXY_URL),
        "video": src.get("video"),
        "error": src.get("error"),
        "sources": src["sources"],
        "chapters": src["chapters"][:10],
        "subtitles_reason": src.get("subtitles_reason"),
        "transcript_chars": len((src.get("transcript") or {}).get("text", "")),
        "warnings": src["warnings"],
    }


def parse_lang(value):
    v = str(value or "auto").strip().lower()
    if v in ("uk", "ua", "uk-ua"):
        return "ua"
    if v in ("en", "ru"):
        return v
    return "auto"


# ---------------------------------------------------------------------------
# Vercel Python handler
# ---------------------------------------------------------------------------


def _send_json(h, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "Content-Type")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class handler(BaseHTTPRequestHandler):  # noqa: N801 - required name for Vercel
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("probe"):
            try:
                _send_json(self, probe(q["probe"][0]))
            except Exception as e:
                _send_json(self, {"status": "error", "error": str(e)[:300]})
            return
        _send_json(self, {"status": "ok", "service": "Content Repurposer", "youtube_key": bool(YT_KEY),
                                    "subs_proxy": bool(SUBS_PROXY_URL)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            _send_json(self, generate(str(data.get("url") or "")[:500], str(data.get("notes") or ""),
                                      parse_lang(data.get("lang"))))
        except Exception as e:
            _send_json(self, {"success": False, "error": "Server error: %s" % e})
