# -*- coding: utf-8 -*-
"""
Thumbnail & Title Lab — Vercel Serverless Function (Python, standard library only).

POST /api/thumbnails   {"topic": "...", "niche": "...", "lang": "en|ru|ua"}

Asks Gemini (shared carousel in api/_gemini.py) for:
  * concepts — exactly 3 thumbnail concepts for an A/B test
      {angle_name, overlay_text (2-4 words), accent_color (#RRGGBB),
       image_prompt (English, no text in the picture), ctr_tip}
  * titles   — exactly 6 clickable titles
      {title (45-80 chars), ctr_score (82-97), trigger}

Every field is validated and normalised here, so the browser always gets
exactly 3 concepts and 6 titles. When Gemini is overloaded (429 / 5xx /
timeout) a template result is returned with "ai": "fallback" so the tool
still works. A missing or rejected Gemini key returns a clear JSON error
with setup steps (never HTTP 500).

A YouTube link in `topic` (youtube.com/watch?v=, youtu.be/, /shorts/, /live/) is
resolved to the real video: title, channel, description excerpt and tags come from
the YouTube Data API (YOUTUBE_API_KEY) or oEmbed, and the concepts/titles are made
for THAT video instead of for the URL text. Any other words next to the link are
kept as author notes. The response then also contains "video".

The thumbnails themselves are rendered in the browser on <canvas>, so text
on the image (incl. Cyrillic and Ukrainian і ї є ґ) is always crisp. Every concept
also carries bg_prompt (<= 12 English words, for the free image service),
highlight (the accent word of overlay_text) and badge (1-2 word sticker).

POST /api/thumbnails   {"bg": "<image prompt>", "seed": 123}
  Optional AI background through Hugging Face when HF_TOKEN is set in Vercel
  (free token: huggingface.co/settings/tokens). Returns
  {"success": true, "image": "data:image/...;base64,...", "model": "..."}
  or {"success": false, "error": "...", "code": 401|402|503|...}. Without HF_TOKEN the
  browser uses the free Pollinations service instead.
"""

import base64

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gemini import (  # noqa: E402
    KEY_HELP,
    REQUEST,
    GeminiFail,
    call_gemini,
    diagnostics as gemini_diagnostics,
    extract_json,
    start_request,
)

REQUEST_BUDGET = 54   # whole request; vercel.json maxDuration for this function: 60
GEMINI_BUDGET = 45    # seconds for the Gemini carousel
YT_KEY = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
HF_TOKEN = (os.environ.get("HF_TOKEN") or "").strip()
HF_ROUTER = "https://router.huggingface.co/"
HF_HUB_API = "https://huggingface.co/api/models/"
HF_MODELS = [m.strip() for m in (os.environ.get("HF_MODEL") or "").split(",") if m.strip()] + [
    "black-forest-labs/FLUX.1-schnell",
    "black-forest-labs/FLUX.1-dev",
]
# image providers we know how to call through the HF router, in order of preference
HF_PROVIDERS = ["hf-inference", "together", "nscale", "fal-ai", "replicate"]
HF_BUDGET = 52        # seconds for one background (function maxDuration 60)
BG_W, BG_H = 1280, 720
SHORT_PROMPT_WORDS = 12
YT_TIMEOUT = 6
YT_URL = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s#]*?&)?v=|shorts/|live/|embed/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})[^\s]*", re.I)

LANG_NAMES = {"en": "English", "ru": "Russian", "ua": "Ukrainian"}

N_CONCEPTS = 3
N_TITLES = 6
TITLE_MIN, TITLE_MAX = 45, 80
TITLE_HARD_MIN = 25          # shorter Gemini titles are replaced by templates
SCORE_MIN, SCORE_MAX = 82, 97

PALETTE = ["#FACC15", "#EF4444", "#22C55E", "#F97316", "#06B6D4", "#EC4899"]

DEFAULT_TRIGGERS = ["Curiosity Gap", "Loss Aversion", "Contrarian", "How-To",
                    "Social Proof", "Urgency"]

IMAGE_SUFFIX = (
    "YouTube thumbnail background, cinematic studio lighting, high contrast, "
    "vivid saturated colors, sharp focus, main subject on the right third, "
    "clean uncluttered dark negative space on the left for a headline, "
    "no small details, no text, no letters, no words, no logos, no watermark"
)

CYRILLIC = re.compile(u"[\u0400-\u04FF]")
RU_ONLY = re.compile(u"[ыэъёЫЭЪЁ]")      # must never appear in Ukrainian
UA_ONLY = re.compile(u"[іїєґІЇЄҐ]")      # must never appear in Russian
TRAILING_PUNCT = re.compile(u"[\\s.,!?;:…\\-–—\"'«»“”„()\\[\\]]+$")
LEADING_PUNCT = re.compile(u"^[\\s.,!?;:…\\-–—\"'«»“”„()\\[\\]]+")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
STOPWORDS = set("""a an the of on in at to for with and or by from into onto over under is are be that which
this these those its it his her their very some any as while background photo image picture scene
how what why who i me my you your we our do does did can will""".split())


# ---------------------------------------------------------------------------
# Language purity
# ---------------------------------------------------------------------------


def lang_ok(text, lang):
    """True if `text` respects the script rules of `lang`."""
    if lang == "en":
        return not CYRILLIC.search(text or "")
    if lang == "ua":
        return not RU_ONLY.search(text or "")
    if lang == "ru":
        return not UA_ONLY.search(text or "")
    return True


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _clean(s, limit):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    # drop emoji / non-BMP symbols — canvas fonts may not have them
    s = "".join(ch for ch in s if ord(ch) <= 0xFFFF)
    return s[:limit].strip()


def clean_overlay(text):
    """2-4 words, no punctuation at the end (or start). Returns '' if unusable."""
    s = _clean(text, 60)
    s = s.replace('"', "").replace("«", "").replace("»", "").replace("“", "").replace("”", "")
    s = LEADING_PUNCT.sub("", TRAILING_PUNCT.sub("", s))
    words = s.split()
    if not 2 <= len(words) <= 4:
        return ""   # cutting a longer phrase would break its meaning -> template instead
    return TRAILING_PUNCT.sub("", " ".join(words))


def _luminance(hex_color):
    c = [int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def clean_color(value, i):
    v = str(value or "").strip()
    if re.match(r"^#[0-9A-Fa-f]{3}$", v):
        v = "#" + "".join(ch * 2 for ch in v[1:])
    if not HEX.match(v):
        return PALETTE[i % len(PALETTE)]
    if _luminance(v) < 0.12:   # too dark to work as a bright accent
        return PALETTE[i % len(PALETTE)]
    return v.upper()


def _scene(value, topic):
    s = _clean(value, 380)
    if not s or CYRILLIC.search(s):
        # Gemini did not give an English prompt -> generic but on-topic scene
        s = "Dramatic eye-catching scene that illustrates the video topic: " + _clean(topic, 120)
    return s.rstrip(" .,")


def clean_image_prompt(value, topic):
    return _scene(value, topic) + ". " + IMAGE_SUFFIX


def short_prompt(value, scene):
    """<= 12 English words for the free image service (long prompts get the 'limit' card more often)."""
    tail = ["cinematic", "thumbnail", "no", "text"]
    src = _clean(value, 200)
    if not src or CYRILLIC.search(src):
        src = scene
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", src)
             if w.lower() not in STOPWORDS and w.lower() not in ("text", "letters", "logo", "logos", "watermark")]
    if len(words) < 2:
        words = ["dramatic", "studio", "scene"]
    words = words[:SHORT_PROMPT_WORDS - len(tail)]
    return " ".join(words + tail)


def clean_highlight(value, overlay):
    """One word of overlay_text that gets the accent colour; '' -> the browser picks one."""
    w = TRAILING_PUNCT.sub("", LEADING_PUNCT.sub("", _clean(value, 30)))
    if not w or " " in w:
        return ""
    words = [TRAILING_PUNCT.sub("", x).lower() for x in overlay.split()]
    return w.upper() if w.lower() in words else ""


def clean_badge(value, lang, i):
    s = _clean(value, 24).replace('"', "")
    s = LEADING_PUNCT.sub("", TRAILING_PUNCT.sub("", s))
    if not s or len(s) > 16 or len(s.split()) > 2 or not lang_ok(s, lang):
        return FALLBACK_BADGES[lang][i % N_CONCEPTS]
    return s.upper()


def trim_title(s):
    s = _clean(s, 200).strip('"«»“”')
    if len(s) <= TITLE_MAX:
        return s
    cut = s[:TITLE_MAX + 1]
    sp = cut.rfind(" ")
    s = cut[:sp] if sp >= TITLE_MIN else s[:TITLE_MAX]
    return s.rstrip(" ,;:—–-")


def clamp_score(v, fallback):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = fallback
    return max(SCORE_MIN, min(SCORE_MAX, n))


# ---------------------------------------------------------------------------
# Template fallback (Gemini busy) — grammatical in all 3 languages
# ---------------------------------------------------------------------------

FALLBACK_CONCEPTS = {
    "en": [
        ("Curiosity", "NOBODY KNEW THIS",
         "A hidden detail makes viewers want to close the gap, so they click to find out."),
        ("Before vs After", "BEFORE VS AFTER",
         "A visible transformation promises a clear payoff in one glance."),
        ("Shock / Urgent", "DON'T DO THIS",
         "A warning triggers loss aversion: viewers click so they don't repeat the mistake."),
    ],
    "ru": [
        ("Интрига", "НИКТО ЭТОГО НЕ ЗНАЛ",
         "Недосказанность создаёт пробел в знаниях, и зритель кликает, чтобы его закрыть."),
        ("До и после", "ДО И ПОСЛЕ",
         "Наглядная трансформация с первого взгляда обещает понятный результат."),
        ("Шок / Срочный разбор", "НЕ ДЕЛАЙ ТАК",
         "Предупреждение включает страх потери: зритель кликает, чтобы не повторить ошибку."),
    ],
    "ua": [
        ("Інтрига", "НІХТО ЦЬОГО НЕ ЗНАВ",
         "Недомовленість створює прогалину в знаннях, і глядач клікає, щоб її закрити."),
        ("До і після", "ДО І ПІСЛЯ",
         "Наочна трансформація з першого погляду обіцяє зрозумілий результат."),
        ("Шок / Терміновий розбір", "НЕ РОБИ ТАК",
         "Попередження вмикає страх втрати: глядач клікає, щоб не повторити помилку."),
    ],
}

FALLBACK_BADGES = {
    "en": ["MUST SEE", "BEFORE / AFTER", "WARNING"],
    "ru": ["СМОТРИ", "ДО / ПОСЛЕ", "ВНИМАНИЕ"],
    "ua": ["ДИВИСЬ", "ДО / ПІСЛЯ", "УВАГА"],
}

FALLBACK_SHORT = [   # topic-free scenes when the topic is not in English
    "surprised person looking at glowing mysterious object",
    "dramatic before and after transformation split scene",
    "shocked person under red warning light",
]

FALLBACK_SCENES = [
    "Close-up of a surprised person looking at a mysterious glowing object related to {t}",
    "Split-screen style transformation scene, dull and broken on one side, bright and perfect on the other, about {t}",
    "Dramatic warning scene with red rim light and a shocked expression, about {t}",
]

FALLBACK_TITLES = {
    "en": [
        ("{t}: What Actually Happens After 30 Days", "Curiosity Gap"),
        ("{t} — The Mistake 9 Out of 10 People Make", "Loss Aversion"),
        ("{t}: Everything You Were Told Is Wrong", "Contrarian"),
        ("{t}: A Step-by-Step Guide Even for Beginners", "How-To"),
        ("{t} — The Hidden Detail Pros Never Mention", "Curiosity Gap"),
        ("{t}: I Wasted Months So You Don't Have To", "Loss Aversion"),
    ],
    "ru": [
        ("{t}: что на самом деле происходит через 30 дней", "Curiosity Gap"),
        ("{t} — ошибка, которую делают 9 из 10 человек", "Loss Aversion"),
        ("{t}: всё, что вам говорили, — неправда", "Contrarian"),
        ("{t}: пошаговая инструкция даже для новичка", "How-To"),
        ("{t} — скрытая деталь, о которой молчат профи", "Curiosity Gap"),
        ("{t}: я потратил месяцы, чтобы вы не теряли время", "Loss Aversion"),
    ],
    "ua": [
        ("{t}: що насправді відбувається через 30 днів", "Curiosity Gap"),
        ("{t} — помилка, яку роблять 9 з 10 людей", "Loss Aversion"),
        ("{t}: усе, що вам казали, — неправда", "Contrarian"),
        ("{t}: покрокова інструкція навіть для новачка", "How-To"),
        ("{t} — прихована деталь, про яку мовчать профі", "Curiosity Gap"),
        ("{t}: я витратив місяці, щоб ви не гаяли час", "Loss Aversion"),
    ],
}

FALLBACK_SCORES = [94, 92, 90, 88, 86, 84]


# words that must not end a shortened topic ("... machine for:")
_DANGLING = set("""
a an the for to of in on at by with and or vs from about into my your our is are
за на в во у к ко с со из по для и или о об от до при про без над под что как мой моя
з із зі та й і або від про через що як мій моя біля
""".split())


def _short_topic(topic, limit=36):
    t = _clean(topic, 200).rstrip(" .!?,;:")
    if len(t) > limit:
        cut = t[:limit + 1]
        sp = cut.rfind(" ")
        t = cut[:sp] if sp > 10 else t[:limit]
        words = t.split()
        while len(words) > 2 and (words[-1].lower() in _DANGLING or len(words[-1]) == 1):
            words.pop()
        t = " ".join(words)
    t = t.rstrip(" ,;:—–-")
    first = t.split(" ", 1)[0]
    if any(ch.isupper() for ch in first[1:]):   # keep brand casing: iPhone, eBay
        return t
    return t[:1].upper() + t[1:]


def fallback_concept(i, lang, topic):
    angle, overlay, tip = FALLBACK_CONCEPTS[lang][i % N_CONCEPTS]
    scene = FALLBACK_SCENES[i % N_CONCEPTS].format(t=_clean(topic, 120))
    return {
        "angle_name": angle,
        "overlay_text": overlay,
        "highlight": "",
        "badge": FALLBACK_BADGES[lang][i % N_CONCEPTS],
        "accent_color": PALETTE[i % len(PALETTE)],
        "image_prompt": clean_image_prompt(scene, topic),
        "bg_prompt": short_prompt("" if CYRILLIC.search(topic) else
                                  " ".join(_clean(topic, 120).split()[:6]) + " " + FALLBACK_SHORT[i % N_CONCEPTS],
                                  FALLBACK_SHORT[i % N_CONCEPTS]),
        "ctr_tip": tip,
        "source": "fallback",
    }


def fallback_title(i, lang, topic):
    tpl, trigger = FALLBACK_TITLES[lang][i % N_TITLES]
    return {
        "title": trim_title(tpl.format(t=_short_topic(topic))),
        "ctr_score": FALLBACK_SCORES[i % N_TITLES],
        "trigger": trigger,
        "source": "fallback",
    }


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# YouTube link in the topic -> real video title / description
# ---------------------------------------------------------------------------


def _http_json(url, timeout=YT_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CreatorStudioAI"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_video(video_id):
    """{'title','channel','description','tags'} or None (not found / unreadable)."""
    if YT_KEY:
        try:
            data = _http_json("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
                {"part": "snippet", "id": video_id, "key": YT_KEY}))
            items = data.get("items") or []
            if not items:
                return None
            sn = items[0].get("snippet") or {}
            return {"title": sn.get("title") or "", "channel": sn.get("channelTitle") or "",
                    "description": sn.get("description") or "", "tags": (sn.get("tags") or [])[:12]}
        except Exception:
            pass  # quota / network -> oEmbed
    try:
        data = _http_json("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
            "https://www.youtube.com/watch?v=" + video_id, safe=""), timeout=5)
        return {"title": data.get("title") or "", "channel": data.get("author_name") or "",
                "description": "", "tags": []}
    except Exception:
        return None


def split_topic(topic):
    """'video https://youtu.be/ID how to ...' -> ('ID', 'video how to ...'); no link -> (None, topic)."""
    m = YT_URL.search(topic or "")
    if not m:
        return None, topic
    rest = re.sub(r"\s+", " ", (topic[:m.start()] + " " + topic[m.end():])).strip(" -—:|,")
    return m.group(1), rest


def build_prompt(topic, niche, lang, video=None, notes=""):
    lname = LANG_NAMES[lang]
    purity = {
        "en": "Write overlay_text, angle_name, ctr_tip and titles in English only — no Cyrillic letters.",
        "ru": "Write overlay_text, angle_name, ctr_tip and titles in Russian only — never use the "
              "Ukrainian letters і ї є ґ.",
        "ua": "Write overlay_text, angle_name, ctr_tip and titles in Ukrainian only — never use the "
              "Russian letters ы э ъ ё; use proper Ukrainian words.",
    }[lang]
    return (
        "You are a top YouTube packaging strategist (thumbnails + titles) who maximises CTR.\n"
        "Reply with ONLY valid minified JSON, no markdown, no commentary, exactly this shape:\n"
        '{"concepts":[{"angle_name":"","overlay_text":"","highlight":"","badge":"","accent_color":"#RRGGBB",'
        '"image_prompt":"","bg_prompt":"","ctr_tip":""}],'
        '"titles":[{"title":"","ctr_score":90,"trigger":""}]}\n\n'
        "Rules:\n"
        "- concepts: EXACTLY 3, each a clearly different psychological angle for an A/B test "
        "(e.g. Curiosity, Before vs After, Shock / Urgent breakdown, Contrarian, Result reveal).\n"
        "- angle_name: short name of the angle in %(l)s (1-4 words).\n"
        "- overlay_text: the punchy text printed ON the thumbnail, STRICTLY 2-4 words in %(l)s, "
        "no punctuation at the end, no emoji, no hashtags. It must not repeat the title.\n"
        "- highlight: the ONE most powerful word of overlay_text (a number, a result or an emotional "
        "word), copied exactly from overlay_text. It is painted in the accent colour.\n"
        "- badge: a sticker text in %(l)s, 1-2 words, max 14 characters, e.g. a result, a time or a "
        "price ('5 MINUTES', '100%% WORKS'). No emoji.\n"
        "- accent_color: one bright high-contrast HEX colour for the text plate, "
        "e.g. #FACC15, #EF4444, #22C55E, #F97316, #06B6D4. Use a different colour per concept.\n"
        "- image_prompt: a detailed prompt IN ENGLISH for an AI image generator that creates a clean "
        "background photo/scene WITHOUT any text: YouTube thumbnail style, studio lighting, high "
        "contrast, one clear subject placed on the RIGHT third, empty uncluttered space on the LEFT "
        "for the headline, no small details. Never ask for text, letters, logos or captions.\n"
        "- bg_prompt: the same scene IN ENGLISH in 6-9 words only (subject + action + mood), no text.\n"
        "- ctr_tip: ONE sentence in %(l)s explaining why viewers click this visual approach.\n"
        "- titles: EXACTLY 6 different clickable video titles in %(l)s, each %(mn)d-%(mx)d characters, "
        "specific to the topic, no clickbait cliches (no 'you won't believe', 'shocking', 'insane'), "
        "no ALL CAPS, no emoji, no hashtags.\n"
        "- ctr_score: integer %(smin)d-%(smax)d, your honest CTR potential estimate (vary them).\n"
        "- trigger: the psychological trigger in English, e.g. Curiosity Gap, Contrarian, How-To, "
        "Loss Aversion, Social Proof, Urgency, Specific Result, Story.\n"
        "- %(purity)s\n\n"
        "Video topic / draft title: %(topic)s\n"
        "Channel niche: %(niche)s\n"
    ) % {
        "l": lname, "mn": TITLE_MIN, "mx": TITLE_MAX, "smin": SCORE_MIN, "smax": SCORE_MAX,
        "purity": purity, "topic": topic, "niche": niche or "(not specified — infer from the topic)",
    } + _video_context(video, notes)


def _video_context(video, notes):
    if not video:
        return ""
    out = ("\nThis is an EXISTING YouTube video. Make the thumbnails and titles about exactly what this "
           "video shows (same subject, same objects in image_prompt), new packaging for it:\n"
           "Current title: %s\nChannel: %s\n" % (video.get("title", ""), video.get("channel", "")))
    desc = re.sub(r"https?://\S+", "", video.get("description") or "")
    desc = re.sub(r"\s+", " ", desc).strip()[:700]
    if desc:
        out += "Description (excerpt): %s\n" % desc
    if video.get("tags"):
        out += "Tags: %s\n" % ", ".join(video["tags"])
    if notes:
        out += "Author notes: %s\n" % notes
    return out


def normalize(data, topic, lang):
    """Validate Gemini JSON -> (concepts[3], titles[6], n_replaced)."""
    replaced = 0
    raw_concepts = data.get("concepts") if isinstance(data, dict) else None
    raw_titles = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(raw_concepts, list):
        raw_concepts = []
    if not isinstance(raw_titles, list):
        raw_titles = []

    concepts, used_colors = [], set()
    for c in raw_concepts:
        if len(concepts) >= N_CONCEPTS:
            break
        if not isinstance(c, dict):
            continue
        i = len(concepts)
        overlay = clean_overlay(c.get("overlay_text"))
        angle = _clean(c.get("angle_name"), 40)
        tip = _clean(c.get("ctr_tip"), 240)
        if not overlay or not lang_ok(overlay + " " + angle + " " + tip, lang):
            continue
        color = clean_color(c.get("accent_color"), i)
        if color in used_colors:
            color = next(p for p in PALETTE if p not in used_colors)
        used_colors.add(color)
        fb = fallback_concept(i, lang, topic)
        scene = _scene(c.get("image_prompt"), topic)
        concepts.append({
            "angle_name": angle or fb["angle_name"],
            "overlay_text": overlay,
            "highlight": clean_highlight(c.get("highlight"), overlay),
            "badge": clean_badge(c.get("badge"), lang, i),
            "accent_color": color,
            "image_prompt": scene + ". " + IMAGE_SUFFIX,
            "bg_prompt": short_prompt(c.get("bg_prompt"), scene),
            "ctr_tip": tip or fb["ctr_tip"],
            "source": "gemini",
        })
    used_overlays = {c["overlay_text"].lower() for c in concepts}
    templates = [j for j in range(N_CONCEPTS)
                 if FALLBACK_CONCEPTS[lang][j][1].lower() not in used_overlays]
    while len(concepts) < N_CONCEPTS:
        fb = fallback_concept(templates.pop(0) if templates else len(concepts), lang, topic)
        if fb["accent_color"] in used_colors:
            fb["accent_color"] = next(p for p in PALETTE if p not in used_colors)
        used_colors.add(fb["accent_color"])
        concepts.append(fb)
        replaced += 1

    titles, seen = [], set()
    for t in raw_titles:
        if len(titles) >= N_TITLES:
            break
        if isinstance(t, str):
            t = {"title": t}
        if not isinstance(t, dict):
            continue
        title = trim_title(t.get("title"))
        key = title.lower()
        if len(title) < TITLE_HARD_MIN or key in seen or not lang_ok(title, lang):
            continue
        seen.add(key)
        trigger = _clean(t.get("trigger"), 32) or DEFAULT_TRIGGERS[len(titles) % len(DEFAULT_TRIGGERS)]
        if CYRILLIC.search(trigger):
            trigger = DEFAULT_TRIGGERS[len(titles) % len(DEFAULT_TRIGGERS)]
        titles.append({
            "title": title,
            "ctr_score": clamp_score(t.get("ctr_score"), FALLBACK_SCORES[len(titles)]),
            "trigger": trigger,
            "source": "gemini",
        })
    i = 0
    while len(titles) < N_TITLES:
        fb = fallback_title(i, lang, topic)
        i += 1
        if fb["title"].lower() in seen:
            continue
        seen.add(fb["title"].lower())
        titles.append(fb)
        replaced += 1

    titles.sort(key=lambda x: -x["ctr_score"])
    return concepts, titles, replaced


def fallback_payload(topic, niche, lang):
    concepts, titles, _ = normalize({}, topic, lang)
    return {
        "success": True,
        "topic": topic,
        "niche": niche,
        "lang": lang,
        "concepts": concepts,
        "titles": titles,
        "ai": "fallback",
        "seed": random.randint(1, 10 ** 6),
    }


def generate(topic, niche, lang):
    start_request(REQUEST_BUDGET)
    video, notes = None, ""
    vid, rest = split_topic(topic)
    if vid:
        meta = fetch_video(vid)
        if meta and meta.get("title"):
            video = {"id": vid, "url": "https://www.youtube.com/watch?v=" + vid, "title": meta["title"],
                     "channel": meta["channel"], "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
                     "description": meta["description"], "tags": meta["tags"]}
            notes = rest
            topic = _clean(meta["title"] + (" — " + rest if len(rest) >= 3 else ""), 200)
        elif len(rest) >= 3:
            topic = rest  # video unreadable, but the user also described it in words
        else:
            return {"success": False,
                    "error": "Could not read this YouTube video (private, deleted or a wrong link). "
                             "Describe the video topic in words instead.",
                    "setup": {"title": "What to enter", "steps": [
                        "A public video link: https://youtu.be/VIDEO_ID or https://www.youtube.com/watch?v=VIDEO_ID",
                        "Or the topic / draft title in words, e.g. 'How to balance a car wheel at home'"]}}
    out = _generate(topic, niche, lang, video, notes)
    if out.get("success"):
        out["hf"] = bool(HF_TOKEN)
    if video and out.get("success"):
        out["video"] = {k: video[k] for k in ("id", "url", "title", "channel", "thumbnail")}
    return out


def _generate(topic, niche, lang, video=None, notes=""):
    left = (REQUEST["deadline"] or (time.time() + REQUEST_BUDGET)) - time.time()
    try:
        raw = call_gemini(build_prompt(topic, niche, lang, video, notes), budget=max(10, min(GEMINI_BUDGET, left - 4)))
    except GeminiFail as e:
        if e.message == "NO_KEYS":
            return {
                "success": False,
                "error": "GEMINI_API_KEY is not configured, so titles and thumbnail "
                         "concepts cannot be generated.",
                "setup": {
                    "title": "Add a free Gemini API key",
                    "steps": [
                        "Create a key at https://aistudio.google.com/apikey (new keys start with 'AQ.').",
                        "In Vercel: Project Settings -> Environment Variables, add GEMINI_API_KEY "
                        "(optionally GEMINI_API_KEY_2 / GEMINI_API_KEY_3 from other projects).",
                        "Redeploy the project.",
                    ],
                },
            }
        if e.kind == "key":
            return {"success": False, "error": "Gemini rejected the API key: " + e.message,
                    "setup": KEY_HELP}
        # quota / transient / model / bad_response -> templates, tool keeps working
        out = fallback_payload(topic, niche, lang)
        out["ai_error"] = e.kind or "error"
        return out

    try:
        data = extract_json(raw)
    except Exception:
        out = fallback_payload(topic, niche, lang)
        out["ai_error"] = "bad_response"
        return out

    concepts, titles, replaced = normalize(data, topic, lang)
    total = N_CONCEPTS + N_TITLES
    ai = "gemini" if replaced == 0 else ("fallback" if replaced >= total else "mixed")
    return {
        "success": True,
        "topic": topic,
        "niche": niche,
        "lang": lang,
        "concepts": concepts,
        "titles": titles,
        "ai": ai,
        "seed": random.randint(1, 10 ** 6),
    }


# ---------------------------------------------------------------------------
# Optional AI background through Hugging Face (HF_TOKEN)
# ---------------------------------------------------------------------------

HF_HELP = {"title": "Free AI backgrounds via Hugging Face (optional)", "steps": [
    "Create a free account at huggingface.co and open Settings -> Access Tokens.",
    "Create a token (type: Read is enough, or Fine-grained with 'Make calls to Inference Providers').",
    "Vercel -> Project -> Settings -> Environment Variables: add HF_TOKEN = hf_...",
    "Redeploy. Without HF_TOKEN the free Pollinations service is used automatically."]}


def _hf_error(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    msg = body
    try:
        j = json.loads(body)
        msg = j.get("error") if isinstance(j, dict) else body
        if isinstance(msg, dict):
            msg = msg.get("message") or json.dumps(msg)
    except ValueError:
        pass
    return re.sub(r"\s+", " ", str(msg or ""))[:160]


_HF_MAPPING = {}


def _hf_request(url, payload=None, timeout=30, extra=None, raw=False):
    h = {"Authorization": "Bearer " + HF_TOKEN, "User-Agent": "CreatorStudioAI/1.0"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    h.update(extra or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                                 headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read()
    if raw:
        return ctype, data
    return json.loads(data.decode("utf-8", "replace"))


def hf_providers(model, timeout=8):
    """Live text-to-image providers of `model` on the HF router: [(provider, provider_model_id)]."""
    if model in _HF_MAPPING:
        return _HF_MAPPING[model]
    info = _hf_request(HF_HUB_API + model + "?expand=inferenceProviderMapping", timeout=timeout)
    mapping = info.get("inferenceProviderMapping") if isinstance(info, dict) else None
    items = []
    if isinstance(mapping, dict):
        items = [dict(v, provider=k) for k, v in mapping.items() if isinstance(v, dict)]
    elif isinstance(mapping, list):
        items = [v for v in mapping if isinstance(v, dict)]
    live = {}
    for v in items:
        if v.get("status", "live") == "live" and v.get("task", "text-to-image") == "text-to-image" \
                and v.get("provider") in HF_PROVIDERS and v.get("providerId"):
            live[v["provider"]] = v["providerId"]
    out = [(p, live[p]) for p in HF_PROVIDERS if p in live]
    _HF_MAPPING[model] = out
    return out


def _download(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "CreatorStudioAI/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower(), resp.read()


def hf_generate(provider, pid, model, prompt, seed, size, timeout):
    """One call through the HF router -> (content_type, bytes). Raises on any error."""
    w, h = size
    fast = "schnell" in model.lower()
    if provider == "hf-inference":
        params = {"width": w, "height": h, "seed": seed}
        if fast:
            params["num_inference_steps"] = 4
        return _hf_request(HF_ROUTER + "hf-inference/models/" + pid, {"inputs": prompt, "parameters": params},
                           timeout, {"Accept": "image/jpeg"}, raw=True)
    if provider == "together":
        payload = {"prompt": prompt, "response_format": "base64", "model": pid, "width": w, "height": h, "seed": seed}
        if fast:
            payload["steps"] = 4
        d = _hf_request(HF_ROUTER + "together/v1/images/generations", payload, timeout)
        return "image/jpeg", base64.b64decode(d["data"][0]["b64_json"])
    if provider == "nscale":
        d = _hf_request(HF_ROUTER + "nscale/v1/images/generations",
                        {"response_format": "b64_json", "prompt": prompt, "model": pid, "size": "%dx%d" % (w, h)}, timeout)
        return "image/png", base64.b64decode(d["data"][0]["b64_json"])
    if provider == "fal-ai":
        payload = {"prompt": prompt, "image_size": {"width": w, "height": h}, "seed": seed}
        if fast:
            payload["num_inference_steps"] = 4
        d = _hf_request(HF_ROUTER + "fal-ai/" + pid, payload, timeout)
        return _download(d["images"][0]["url"], timeout)
    if provider == "replicate":
        route = "replicate/v1/predictions" if ":" in pid else "replicate/v1/models/%s/predictions" % pid
        payload = {"input": {"prompt": prompt, "width": w, "height": h, "seed": seed, "aspect_ratio": "16:9"}}
        if ":" in pid:
            payload["version"] = pid.split(":", 1)[1]
        d = _hf_request(HF_ROUTER + route, payload, timeout, {"Prefer": "wait"})
        outp = d.get("output")
        if not outp:
            raise RuntimeError("replicate: no output yet (%s)" % d.get("status"))
        return _download(outp if isinstance(outp, str) else outp[0], timeout)
    raise RuntimeError("unsupported provider " + provider)


def hf_background(prompt, seed, size=(BG_W, BG_H)):
    """-> {"success": True, "image": data-URL, "model", "provider"} or {"success": False, "error", "code", "tried"}."""
    if not HF_TOKEN:
        return {"success": False, "code": 0, "error": "HF_TOKEN is not set", "setup": HF_HELP}
    prompt = _clean(prompt, 600) or "dramatic cinematic studio scene, no text"
    seed = int(seed) % 2147483647
    deadline = time.time() + HF_BUDGET
    tried, last_code = [], 503
    for model in HF_MODELS:
        if deadline - time.time() < 6:
            break
        try:
            providers = hf_providers(model)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"success": False, "code": e.code, "setup": HF_HELP, "tried": tried,
                        "error": "HF_TOKEN was rejected by Hugging Face (HTTP %d)" % e.code}
            tried.append("%s: mapping HTTP %d" % (model, e.code))
            continue
        except Exception as e:
            tried.append("%s: mapping %s" % (model, type(e).__name__))
            continue
        if not providers:
            tried.append("%s: no live image provider" % model)
            continue
        for provider, pid in providers:
            left = deadline - time.time()
            if left < 6:
                break
            try:
                ctype, data = hf_generate(provider, pid, model, prompt, seed, size, min(left, 45))
                if len(data) < 2000:
                    raise RuntimeError("empty image")
                if not ctype.startswith("image/"):
                    ctype = "image/jpeg"
                return {"success": True, "model": model, "provider": provider, "tried": tried,
                        "image": "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode("ascii"))}
            except urllib.error.HTTPError as e:
                last_code = e.code
                msg = _hf_error(e)
                tried.append("%s@%s: HTTP %d %s" % (model, provider, e.code, msg[:90]))
                if e.code in (401, 403) and "provider" not in msg.lower():
                    return {"success": False, "code": e.code, "setup": HF_HELP, "tried": tried,
                            "error": "HF_TOKEN was rejected by Hugging Face (HTTP %d): %s" % (e.code, msg[:120])}
                if e.code == 402:
                    return {"success": False, "code": 402, "tried": tried,
                            "error": "Hugging Face free monthly credits are used up (HTTP 402)"}
            except Exception as e:  # timeout / network / unexpected JSON
                last_code = 504
                tried.append("%s@%s: %s %s" % (model, provider, type(e).__name__, str(e)[:80]))
    return {"success": False, "code": last_code, "tried": tried,
            "error": tried[-1] if tried else "no model answered"}


def parse_lang(value):
    v = str(value or "en").strip().lower()
    if v in ("uk", "ua", "uk-ua"):
        return "ua"
    if v.startswith("ru"):
        return "ru"
    return "en"


# ---------------------------------------------------------------------------
# Vercel Python handler
# ---------------------------------------------------------------------------


def _send_json(handler_obj, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler_obj.send_response(status)
    handler_obj.send_header("Content-Type", "application/json; charset=utf-8")
    handler_obj.send_header("Access-Control-Allow-Origin", "*")
    handler_obj.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler_obj.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler_obj.send_header("Cache-Control", "no-store")
    handler_obj.send_header("Content-Length", str(len(body)))
    handler_obj.end_headers()
    handler_obj.wfile.write(body)


class handler(BaseHTTPRequestHandler):  # noqa: N801 - required name for Vercel
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "hfcheck" in query:   # one small test picture (512x288) -> status only, no image
            t0 = time.time()
            r = hf_background("red sports car in a neon garage, cinematic", 7, size=(512, 288))
            out = {"status": "ok", "hf_token": bool(HF_TOKEN), "success": r.get("success"),
                   "model": r.get("model"), "provider": r.get("provider"), "tried": r.get("tried"),
                   "error": r.get("error"), "code": r.get("code"),
                   "image_bytes": len(r.get("image") or "") * 3 // 4, "elapsed": round(time.time() - t0, 1)}
            _send_json(self, out)
            return
        if "models" in query:
            out = {"status": "ok"}
            out.update(gemini_diagnostics())
            _send_json(self, out)
            return
        _send_json(self, {"status": "ok", "service": "Thumbnail & Title Lab", "hf": bool(HF_TOKEN)})

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
            if data.get("bg"):
                try:
                    seed = int(data.get("seed") or random.randint(1, 10 ** 9))
                except (TypeError, ValueError):
                    seed = random.randint(1, 10 ** 9)
                _send_json(self, hf_background(data.get("bg"), seed))
                return
            topic = _clean(data.get("topic"), 200)
            niche = _clean(data.get("niche"), 120)
            lang = parse_lang(data.get("lang"))
            if len(topic) < 3:
                _send_json(self, {"success": False,
                                  "error": "Missing 'topic': describe the video or paste a draft title."})
                return
            _send_json(self, generate(topic, niche, lang))
        except Exception as e:
            _send_json(self, {"success": False, "error": "Server error: %s" % e})
