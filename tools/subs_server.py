# -*- coding: utf-8 -*-
"""
Backup subtitle server for Content Repurposer (runs on YOUR VPS, e.g. Oracle Cloud).

    GET /subs?v=VIDEO_ID        header  X-Token: <SUBS_TOKEN>
    -> {"ok": true,  "video_id": "...", "text": "full transcript", "lang": "ru", "source": "manual|auto"}
    -> {"ok": false, "video_id": "...", "text": "", "error": "why"}

Only subtitles are read — the video itself is never downloaded, so the yt-dlp error
"Requested format is not available" (SABR / JS-challenge problems) does not matter.

Install & run (Python 3.10+, but 3.11+ is recommended by yt-dlp):
    pip install -U "yt-dlp[default]"
    export SUBS_TOKEN="your-secret-token"      # the same value as SUBS_PROXY_TOKEN in Vercel
    export PORT=8090
    python3 subs_server.py

Optional:
    YTDLP_COOKIES=/home/ubuntu/cookies.txt     # Netscape cookies.txt if YouTube asks to sign in
    SUBS_LANGS=ru,uk,en                        # preferred languages after the original one

Vercel: SUBS_PROXY_URL = http://<server-ip>:8090/subs   SUBS_PROXY_TOKEN = <SUBS_TOKEN>

Quick test on the server:
    curl -H "X-Token: $SUBS_TOKEN" "http://127.0.0.1:8090/subs?v=dQw4w9WgXcQ"

Standard library only (+ yt-dlp).
"""

import html
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import yt_dlp

TOKEN = os.environ.get("SUBS_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8090"))
COOKIES = os.environ.get("YTDLP_COOKIES", "").strip()
PREFERRED = [x.strip() for x in os.environ.get("SUBS_LANGS", "ru,uk,en").split(",") if x.strip()]
TEXT_LIMIT = 200000
CACHE_MAX = 300
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_cache = {}
_cache_lock = threading.Lock()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# subtitle formats -> plain text
# ---------------------------------------------------------------------------

def parse_json3(raw):
    data = json.loads(raw)
    parts = []
    for ev in data.get("events") or []:
        t = "".join(seg.get("utf8") or "" for seg in ev.get("segs") or [])
        if t.strip():
            parts.append(t)
    return " ".join(" ".join(parts).split())


def parse_vtt(raw):
    out, last = [], None
    for line in raw.splitlines():
        line = line.strip()
        if (not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE"))
                or "-->" in line or line.isdigit()):
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line and line != last:   # auto captions repeat every line twice
            out.append(line)
            last = line
    return " ".join(" ".join(out).split())


# ---------------------------------------------------------------------------
# choose the best track
# ---------------------------------------------------------------------------

def pick_track(info):
    """-> (lang, source, [formats]) or None. Original language first, then PREFERRED."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if v and k != "live_chat"}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
    orig = (info.get("language") or "").split("-")[0]

    def first(d, keys):
        for k in keys:
            if k and k in d:
                return k
        return None

    if manual:
        k = first(manual, [orig] + PREFERRED) or next(iter(manual))
        return k, "manual", manual[k]
    if auto:
        orig_keys = [k for k in auto if k.endswith("-orig")]          # untranslated speech recognition
        k = first(auto, orig_keys + [orig] + PREFERRED) or next(iter(auto))
        return k, "auto", auto[k]
    return None


def fetch_text(ydl, formats):
    by_ext = {f.get("ext"): f for f in formats if f.get("url")}
    for ext, parser in (("json3", parse_json3), ("vtt", parse_vtt)):
        f = by_ext.get(ext)
        if not f:
            continue
        raw = ydl.urlopen(f["url"]).read().decode("utf-8", "replace")
        text = parser(raw)
        if text:
            return text
    raise RuntimeError("subtitle track has no json3/vtt text (formats: %s)" % ", ".join(sorted(by_ext)))


def get_subtitles(video_id):
    opts = {
        "skip_download": True,
        "ignore_no_formats_error": True,   # never fail because the video formats are missing
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
    }
    if COOKIES:
        opts["cookiefile"] = COOKIES
    with yt_dlp.YoutubeDL(opts) as ydl:
        # process=False: no format selection at all -> no "Requested format is not available"
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False, process=False)
        track = pick_track(info or {})
        if not track:
            raise RuntimeError("this video has no subtitles or automatic captions")
        lang, source, formats = track
        text = fetch_text(ydl, formats)
    return {"text": text[:TEXT_LIMIT], "lang": lang, "source": source}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SubsServer/1.0"

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):   # quieter default log
        log(self.address_string(), fmt % args)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/health":
            self._json(200, {"ok": True})
            return
        if u.path.rstrip("/") != "/subs":
            self._json(404, {"error": "not_found"})
            return
        if TOKEN and self.headers.get("X-Token", "") != TOKEN:
            self._json(401, {"ok": False, "error": "bad X-Token"})
            return
        vid = (parse_qs(u.query).get("v") or [""])[0].strip()
        if not VIDEO_ID.match(vid):
            self._json(400, {"ok": False, "video_id": vid, "text": "", "error": "bad video id"})
            return
        with _cache_lock:
            hit = _cache.get(vid)
        if hit:
            self._json(200, dict(hit, ok=True, video_id=vid, cached=True))
            return
        t0 = time.time()
        try:
            res = get_subtitles(vid)
        except Exception as e:  # yt-dlp DownloadError / network / parsing
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:300]
            log("FAIL", vid, "%.1fs" % (time.time() - t0), msg)
            self._json(200, {"ok": False, "video_id": vid, "text": "", "error": msg})
            return
        log("OK", vid, res["lang"], res["source"], len(res["text"]), "chars", "%.1fs" % (time.time() - t0))
        with _cache_lock:
            if len(_cache) >= CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[vid] = res
        self._json(200, dict(res, ok=True, video_id=vid))


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not TOKEN:
        log("WARNING: SUBS_TOKEN is not set, the server accepts requests without a token")
    log("yt-dlp", yt_dlp.version.__version__, "| listening on 0.0.0.0:%d /subs" % PORT)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
