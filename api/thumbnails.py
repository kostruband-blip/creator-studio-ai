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

The thumbnails themselves are rendered in the browser on <canvas>, so text
on the image (incl. Cyrillic and Ukrainian і ї є ґ) is always crisp.
"""

import json
import os
import random
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gemini import (  # noqa: E402
    KEY_HELP,
    GeminiFail,
    call_gemini,
    diagnostics as gemini_diagnostics,
    extract_json,
    start_request,
)

REQUEST_BUDGET = 52   # whole request; vercel.json maxDuration for this function: 60
GEMINI_BUDGET = 45    # seconds for the Gemini carousel

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


def clean_image_prompt(value, topic):
    s = _clean(value, 380)
    if not s or CYRILLIC.search(s):
        # Gemini did not give an English prompt -> generic but on-topic scene
        s = "Dramatic eye-catching scene that illustrates the video topic: " + _clean(topic, 120)
    s = s.rstrip(" .,")
    return s + ". " + IMAGE_SUFFIX


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
        "accent_color": PALETTE[i % len(PALETTE)],
        "image_prompt": clean_image_prompt(scene, topic),
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


def build_prompt(topic, niche, lang):
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
        '{"concepts":[{"angle_name":"","overlay_text":"","accent_color":"#RRGGBB",'
        '"image_prompt":"","ctr_tip":""}],'
        '"titles":[{"title":"","ctr_score":90,"trigger":""}]}\n\n'
        "Rules:\n"
        "- concepts: EXACTLY 3, each a clearly different psychological angle for an A/B test "
        "(e.g. Curiosity, Before vs After, Shock / Urgent breakdown, Contrarian, Result reveal).\n"
        "- angle_name: short name of the angle in %(l)s (1-4 words).\n"
        "- overlay_text: the punchy text printed ON the thumbnail, STRICTLY 2-4 words in %(l)s, "
        "no punctuation at the end, no emoji, no hashtags. It must not repeat the title.\n"
        "- accent_color: one bright high-contrast HEX colour for the text plate, "
        "e.g. #FACC15, #EF4444, #22C55E, #F97316, #06B6D4. Use a different colour per concept.\n"
        "- image_prompt: a detailed prompt IN ENGLISH for an AI image generator that creates a clean "
        "background photo/scene WITHOUT any text: YouTube thumbnail style, studio lighting, high "
        "contrast, one clear subject placed on the RIGHT third, empty uncluttered space on the LEFT "
        "for the headline, no small details. Never ask for text, letters, logos or captions.\n"
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
    }


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
        concepts.append({
            "angle_name": angle or fb["angle_name"],
            "overlay_text": overlay,
            "accent_color": color,
            "image_prompt": clean_image_prompt(c.get("image_prompt"), topic),
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
    try:
        raw = call_gemini(build_prompt(topic, niche, lang), budget=GEMINI_BUDGET)
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
        if "models" in query:
            out = {"status": "ok"}
            out.update(gemini_diagnostics())
            _send_json(self, out)
            return
        _send_json(self, {"status": "ok", "service": "Thumbnail & Title Lab"})

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
