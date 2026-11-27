# -*- coding: utf-8 -*-
"""
Local preview server for NicheRadar AI.

Serves index.html (NicheRadar), /thumbnails (Thumbnail & Title Lab),
/repurpose (Content Repurposer) and MOCK /api/analyze, /api/thumbnails and
/api/repurpose endpoints, so the full UI can be previewed without
YouTube/Gemini API keys. Real endpoints: api/analyze.py, api/thumbnails.py,
api/repurpose.py.

Run:  python3 preview_server.py   (listens on 0.0.0.0:3000)
"""
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "api"))
import thumbnails as thumbs_api  # noqa: E402  (template builder, no network)
import repurpose as rep_api  # noqa: E402  (normalize() builds markdown/html, no network)

PAGES = {"/": "index.html", "/index.html": "index.html",
         "/thumbnails": "thumbnails.html", "/thumbnails/": "thumbnails.html",
         "/thumbnails.html": "thumbnails.html",
         "/repurpose": "repurpose.html", "/repurpose/": "repurpose.html",
         "/repurpose.html": "repurpose.html"}
for _pre in ("ru", "uk"):  # localized copies built by tools/build_i18n_pages.py
    PAGES.update({"/%s" % _pre: "%s/index.html" % _pre, "/%s/" % _pre: "%s/index.html" % _pre})
    for _pg in ("thumbnails", "repurpose"):
        PAGES.update({"/%s/%s" % (_pre, _pg): "%s/%s.html" % (_pre, _pg),
                      "/%s/%s/" % (_pre, _pg): "%s/%s.html" % (_pre, _pg)})
ICONS = {
    "/favicon.ico": "image/x-icon",
    "/favicon.svg": "image/svg+xml",
    "/favicon-32.png": "image/png",
    "/apple-touch-icon.png": "image/png",
}


def svg_avatar(text, hue=265):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="hsl(%d,80%%,62%%)"/>'
        '<stop offset="1" stop-color="hsl(%d,80%%,52%%)"/></linearGradient></defs>'
        '<rect width="96" height="96" rx="20" fill="url(#g)"/>'
        '<text x="50%%" y="50%%" dy=".35em" text-anchor="middle" '
        'font-family="Arial" font-size="38" fill="#fff" font-weight="700">%s</text>'
        "</svg>"
    ) % (hue, (hue + 40) % 360, text[:2].upper())
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


def mock_analyze():
    av = [svg_avatar("TW", 265), svg_avatar("GT", 200), svg_avatar("MR", 150),
          svg_avatar("TL", 30), svg_avatar("DV", 320), svg_avatar("PB", 95)]
    channel = {
        "id": "UCtechwave00000000000000",
        "title": "TechWave",
        "description": "Honest smartphone and laptop reviews, tips and tutorials.",
        "avatar": av[0],
        "subscribers": 128000,
        "views": 5400000,
        "videos": [],
    }
    niche = "Tech reviews & tutorials"
    competitors = [
        {"title": "GadgetGuru", "url": "https://www.youtube.com/@GadgetGuru", "avatar": av[1],
         "subscribers": 920000, "views": 41000000,
         "bestVideo": {"title": "I tested the cheapest phone of 2025", "url": "https://www.youtube.com/watch?v=aaaaaa", "views": 3200000}},
        {"title": "MarquesScope", "url": "https://www.youtube.com/@MarquesScope", "avatar": av[2],
         "subscribers": 640000, "views": 28000000,
         "bestVideo": {"title": "The phone that changed everything", "url": "https://www.youtube.com/watch?v=bbbbbb", "views": 5100000}},
        {"title": "TechLens", "url": "https://www.youtube.com/@TechLens", "avatar": av[3],
         "subscribers": 410000, "views": 15000000,
         "bestVideo": {"title": "5 settings you must change now", "url": "https://www.youtube.com/watch?v=cccccc", "views": 2200000}},
        {"title": "DevBytes", "url": "https://www.youtube.com/@DevBytes", "avatar": av[4],
         "subscribers": 230000, "views": 9000000,
         "bestVideo": {"title": "I built a PC for $300", "url": "https://www.youtube.com/watch?v=dddddd", "views": 1400000}},
        {"title": "PixelBench", "url": "https://www.youtube.com/@PixelBench", "avatar": av[5],
         "subscribers": 175000, "views": 6200000,
         "bestVideo": {"title": "Camera shootout: flagships vs budget", "url": "https://www.youtube.com/watch?v=eeeeee", "views": 980000}},
    ]
    ideas = [
        {"title": "I Used the Cheapest Smartphone for 30 Days", "topic": "Budget challenge format that always overperforms in tech."},
        {"title": "5 Hidden Settings That Double Your Battery Life", "topic": "Practical tips list — high save rate, very shareable."},
        {"title": "This Phone Silently Beat the Flagships", "topic": "Underdog review angle that sparks debate in comments."},
        {"title": "I Tested Viral TikTok Tech Hacks — Most Are Lies", "topic": "Myth-busting format with strong click-through."},
        {"title": "The $50 Gadget That Replaced My $500 Tool", "topic": "Affordable alternative story, great for newcomers."},
        {"title": "Why Your Laptop Is Slow (and the 2-Minute Fix)", "topic": "Troubleshooting tutorial with evergreen search demand."},
        {"title": "I Compared 3 Budget Phones So You Don't Have To", "topic": "Comparison roundup that converts viewers to subscribers."},
        {"title": "The Camera Test Nobody Talks About", "topic": "Low-light / real-world footage angle competitors missed."},
        {"title": "I Asked Fans to Break My Phone — Here's What Happened", "topic": "Community-driven durability stunt for high retention."},
        {"title": "The Upgrade That Actually Matters in 2025", "topic": "Forward-looking buying-guide hook for the new cycle."},
    ]
    return {
        "success": True,
        "channel": channel,
        "niche": niche,
        "competitors": competitors,
        "ideas": ideas,
        "language": "en",
    }


def mock_repurpose(url, notes, lang):
    """Canned English content pack run through the real normalize()."""
    vid = rep_api.extract_video_id(url) if url else None
    body = ("Most creators treat a finished video as the end of the job. In reality it is raw material: "
            "one 15-minute talk already contains a dozen ideas, examples and quotes that can live on other platforms. "
            "The trick is to keep the core message and change only the format.\n\n"
            "Start with the single sentence your viewer should remember, then add the proof: a number, a story or a short demo. "
            "Everything else is supporting detail that you can cut without losing value.")
    raw = {
        "telegram_post": ("One video can feed your channels for a whole week.\n\n"
                          "Here is what we took from this episode:\n"
                          "• The main idea fits into one sentence, and that sentence becomes your hook.\n"
                          "• Every strong point needs proof: a number, a story or a quick demo.\n"
                          "• Cut the warm-up. Viewers decide in the first 5 seconds.\n"
                          "• Repeat the key message at the end so it actually sticks.\n\n"
                          "Takeaway: you do not need more ideas, you need to squeeze more out of the ones you already filmed. "
                          "Pick your last video, write down its single core sentence and build three posts around it today.\n\n"
                          "Watch the full video here: [link]\n"
                          "Which of your videos deserves a second life? Tell us in the comments."),
        "seo_article": {
            "h1": "How to Repurpose One YouTube Video Into a Week of Content",
            "meta_description": "Learn how to turn one YouTube video into a Telegram post, an SEO article and five short social posts without writing from scratch.",
            "intro": body,
            "sections": [{"h2": "Find the Core Message First", "body": body},
                         {"h2": "Back Every Point With Proof", "body": body},
                         {"h2": "Adapt the Format, Not the Idea", "body": body}],
            "conclusion_h2": "Conclusion",
            "conclusion": "Repurposing is not copy-paste. It is choosing the strongest idea from your video and presenting it in the format each platform rewards. Start with your latest upload today.",
        },
        "social_posts": [
            {"angle": "insight", "text": "Your best content idea is not new. It is buried in a video you already published. Dig it out before you film anything else."},
            {"angle": "steps", "text": "Repurpose a video in 3 steps:\n1. Write its core idea in one sentence\n2. Pick one proof: number, story or demo\n3. Rewrite it for each platform"},
            {"angle": "mistake", "text": "Biggest repurposing mistake: pasting the same text everywhere. Same idea, different format. Otherwise every platform will ignore you."},
            {"angle": "tip", "text": "Quick tip: the first 5 seconds of your video are usually your best hook for a text post too. Steal from yourself."},
            {"angle": "question", "text": "Be honest: how many of your old videos could become posts this week? 1, 5 or all of them?"},
        ],
    }
    pack, problems = rep_api.normalize(raw, "en")
    video = None
    if vid:
        u = "https://www.youtube.com/watch?v=" + vid
        video = {"id": vid, "url": u, "title": "How to Turn One Video Into a Week of Content (Preview)",
                 "channel": "Creator Studio Demo", "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid}
        pack["telegram_post"] = pack["telegram_post"].replace("[link]", u)
    out = {"success": True, "lang": "en", "video": video,
           "sources": {"api": bool(video), "oembed": False, "description": bool(video), "chapters": 4 if video else 0,
                       "subtitles": "blocked" if video else "none", "notes": bool(notes),
                       "video_ai": "watched" if video else None},
           "warnings": ["quality: " + "; ".join(problems)] if problems else []}
    out.update(pack)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in PAGES:
            try:
                with open(os.path.join(ROOT, PAGES[path]), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path in ICONS:
            with open(os.path.join(ROOT, path.lstrip("/")), "rb") as f:
                self._send(200, f.read(), ICONS[path])
        elif path.rstrip("/") in ("/api/repurpose", "/api/repurpose.py"):
            self._send(200, json.dumps({"status": "ok", "service": "Content Repurposer (preview mock)",
                                        "usage": "POST {url, notes, lang: auto|en|ru|ua}"}))
        else:
            self._send(200, json.dumps({"status": "ok", "service": "NicheRadar AI (preview)"}))

    def do_POST(self):
        if self.path.rstrip("/") in ("/api/analyze", "/api/analyze.py"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            url = (data.get("url") or "").strip()
            if not url:
                self._send(200, json.dumps({"success": False, "error": "Missing 'url'."}))
                return
            # simulate 4-6s of backend work so the stepper is visible
            time.sleep(3.6)
            self._send(200, json.dumps(mock_analyze(), ensure_ascii=False))
        elif self.path.rstrip("/") in ("/api/thumbnails", "/api/thumbnails.py"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            if data.get("bg"):  # optional Hugging Face background: real call only if HF_TOKEN is set
                self._send(200, json.dumps(thumbs_api.ai_background(data["bg"], data.get("seed") or 1)))
                return
            topic = (data.get("topic") or "").strip()
            if len(topic) < 3:
                self._send(200, json.dumps({"success": False, "error": "Missing 'topic'."}))
                return
            time.sleep(2.5)
            out = thumbs_api.fallback_payload(topic, (data.get("niche") or "").strip(),
                                              thumbs_api.parse_lang(data.get("lang")))
            out["ai"] = "gemini"  # preview: pretend the templates came from Gemini
            out["hf"] = bool(thumbs_api.HF_TOKEN)
            out["bg_api"] = thumbs_api.bg_api()
            vid, _rest = thumbs_api.split_topic(topic)
            if vid:  # preview: pretend the YouTube link was resolved
                out["video"] = {"id": vid, "url": "https://www.youtube.com/watch?v=" + vid,
                                "title": "How to balance a car wheel at home (preview)", "channel": "Demo channel",
                                "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid}
            self._send(200, json.dumps(out, ensure_ascii=False))
        elif self.path.rstrip("/") in ("/api/repurpose", "/api/repurpose.py"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            url = (data.get("url") or "").strip()
            notes = (data.get("notes") or "").strip()
            if not url and len(notes) < 40:
                self._send(200, json.dumps({"success": False, "error": "Paste a YouTube link or at least a few sentences of text."}))
                return
            time.sleep(3)
            self._send(200, json.dumps(mock_repurpose(url, notes, data.get("lang")), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print("NicheRadar AI preview → http://0.0.0.0:%d" % port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
