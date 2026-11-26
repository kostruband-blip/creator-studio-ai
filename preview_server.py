# -*- coding: utf-8 -*-
"""
Local preview server for NicheRadar AI.

Serves index.html and a MOCK /api/analyze endpoint so the full UI
(niche card, free competitors/ideas + blurred paywall) can be previewed
without YouTube/Gemini API keys. The real endpoint is api/analyze.py on Vercel.

Run:  python3 preview_server.py   (listens on 0.0.0.0:3000)
"""
import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
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
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path in ICONS:
            with open(os.path.join(ROOT, self.path.lstrip("/")), "rb") as f:
                self._send(200, f.read(), ICONS[self.path])
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
