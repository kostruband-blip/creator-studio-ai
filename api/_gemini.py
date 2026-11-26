"""Shared Gemini client for NicheRadar AI API handlers (stdlib only).

Files whose name starts with "_" are NOT exposed by Vercel as endpoints,
so this module is only importable by the handlers in api/.

Carousel rules
--------------
* Model list comes from Gemini itself: GET /v1beta/models (ListModels) is
  called first, and only TEXT models are kept (support generateContent; no
  embedding / image / TTS / audio / live / video models). Models that Google
  retires simply disappear from that list. The list is cached for 15 minutes.
* Order: available models from PREFERRED_MODELS first (in that order), then
  the other discovered text models (fast flash first, pro / gemma last).
  If ListModels fails, PREFERRED_MODELS is used as a fallback.
  Env GEMINI_MODELS="a,b,c" skips discovery and uses exactly that list.
* On HTTP 503 / 429 / 5xx / timeout we do NOT retry the same model:
  we switch instantly to the NEXT model in the ring.
* Keys (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3) rotate too:
  every time the ring of models wraps around, the next key is used, so all
  model x key combinations are covered at most once per call.
* One request waits at most GEMINI_TRY_TIMEOUT = 25 seconds; if a model has
  not answered after HEDGE_AFTER_SEC, the next one starts in parallel
  (max MAX_PARALLEL) and the first successful answer wins.
* The model + key that answered last is tried FIRST on the next request.
* 404 (model gone) removes the model from the list; 401/403 skips that key.
"""

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Priority order only: used first IF Gemini lists them as available,
# and as a fallback list when ListModels itself fails.
PREFERRED_MODELS = [
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
    "gemini-pro-latest",
]

# Optional hard override (skips discovery).
ENV_MODELS = [
    m.strip().replace("models/", "")
    for m in (os.environ.get("GEMINI_MODELS") or "").split(",")
    if m.strip()
]

# Words that mark non-text models.
NON_TEXT_MARKERS = (
    "embed", "image", "imagen", "banana", "tts", "audio", "speech", "live",
    "veo", "video", "lyria", "music", "aqa", "robotics", "computer-use",
    "transcribe", "vision", "native",
)

MODEL_LIST_TTL_SEC = 15 * 60  # re-ask Gemini for the model list every 15 min
LIST_TIMEOUT_SEC = 8          # ListModels request timeout

GEMINI_KEYS = [
    k.strip()
    for k in (
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GEMINI_API_KEY_2"),
        os.environ.get("GEMINI_API_KEY_3"),
    )
    if k and k.strip()
]

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

GEMINI_TRY_TIMEOUT = 25    # seconds to wait for ONE model before giving up on it
GEMINI_CALL_BUDGET = 55    # seconds for all attempts of one call_gemini()
HEDGE_AFTER_SEC = 6        # no answer yet -> also start the next model in parallel
MAX_PARALLEL = 3           # at most this many models in flight at once
MIN_ATTEMPT_SEC = 6        # do not start a model with less time than this left
REQUEST_BUDGET_SEC = 110   # whole API request (vercel.json maxDuration: 120)

# State of a warm serverless instance.
STATE = {
    "model": None,      # model that answered last -> tried first next time
    "key_i": 0,         # key that answered last
}
MODELS = {
    "list": [],         # current ring of text models
    "fetched_at": 0.0,
    "source": "",       # "gemini" (ListModels) | "env" | "fallback"
    "discovered": 0,    # how many models ListModels returned in total
}
REQUEST = {"deadline": None}  # set per request via start_request()

_NO_JSON_MODE = set()     # models that reject responseMimeType=application/json
_NO_THINKING_CFG = set()  # models that reject our thinkingConfig


def start_request(budget=REQUEST_BUDGET_SEC):
    """Call at the start of every API request so Gemini stays inside Vercel's limit."""
    REQUEST["deadline"] = time.time() + budget


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GeminiFail(Exception):
    """A Gemini call failed.

    kind: "quota" (429) | "transient" (5xx / timeout / network) | "model" (404)
          | "key" (401/403) | "json_mode" | "thinking_cfg" | "bad_response" | ""
    """

    def __init__(self, retryable, message, code=0, kind=""):
        self.retryable = retryable
        self.message = message
        self.code = code
        self.kind = kind
        super().__init__(message)


class GeminiStepError(RuntimeError):
    """RuntimeError that also carries setup/help steps for the UI."""

    def __init__(self, message, setup=None):
        super().__init__(message)
        self.setup = setup


QUOTA_HELP = {
    "title": "Gemini quota exceeded",
    "steps": [
        "Free Gemini limits are counted per Google Cloud project and per model; "
        "daily limits reset at midnight Pacific time.",
        "Check your live limits: https://aistudio.google.com/rate-limit",
        "Wait a minute and try again, or try again after the daily reset.",
        "Add GEMINI_API_KEY_2 / GEMINI_API_KEY_3 created in DIFFERENT Google "
        "Cloud projects (keys from the same project share one quota), then redeploy.",
    ],
}

BUSY_HELP = {
    "title": "Gemini is overloaded right now",
    "steps": [
        "Google's Gemini servers are busy (HTTP 503 / timeouts). This is temporary "
        "and not a problem with your keys or settings.",
        "Wait 30-60 seconds and press Analyze again.",
        "Optional: add GEMINI_API_KEY_2 from another Google Cloud project in Vercel "
        "for more capacity, then redeploy.",
    ],
}

KEY_HELP = {
    "title": "Gemini API key problem",
    "steps": [
        "Create a key at https://aistudio.google.com/apikey (new keys start with 'AQ.').",
        "Use 'Copy key' in the key details - the list shows a shortened, masked value.",
        "In Vercel set GEMINI_API_KEY to that value and redeploy.",
    ],
}


def step_error(step, e):
    """Turn a GeminiFail into a GeminiStepError with UI help steps."""
    setup = {"quota": QUOTA_HELP, "transient": BUSY_HELP, "key": KEY_HELP}.get(e.kind)
    return GeminiStepError("Gemini %s failed: %s" % (step, e.message), setup)


# ---------------------------------------------------------------------------
# Single request
# ---------------------------------------------------------------------------


def _google_error_message(detail):
    try:
        err = json.loads(detail).get("error") or {}
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or "")[:300]
        return str(err)[:300]
    except Exception:
        return (detail or "")[:300]


def _thinking_config(model):
    """Ask for less 'thinking' so answers come back faster (None = model default)."""
    low = model.lower()
    if low.startswith("gemma") or "pro" in low:
        return None
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", low)
    if not m:
        return None  # aliases like gemini-flash-latest
    major = int(m.group(1))
    if major >= 3:
        return {"thinkingLevel": "low"}
    if major == 2 and m.group(2) == "5":
        return {"thinkingBudget": 0}
    return None


def gemini_request(model, key, prompt, timeout=GEMINI_TRY_TIMEOUT, json_mode=True, thinking=True, media=None):
    """One generateContent call. Raises GeminiFail with a classified kind.

    media (optional): {"parts": [...extra parts before the prompt, e.g. a YouTube fileData...],
                       "generation": {...extra generationConfig, e.g. mediaResolution...}}
    """
    parts = list((media or {}).get("parts") or []) + [{"text": prompt}]
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 1,
            "maxOutputTokens": 8192,
        },
    }
    if media and media.get("generation"):
        body["generationConfig"].update(media["generation"])
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    tcfg = _thinking_config(model) if thinking else None
    if tcfg:
        body["generationConfig"]["thinkingConfig"] = tcfg

    req = urllib.request.Request(
        GEMINI_BASE + model + ":generateContent",
        data=json.dumps(body).encode("utf-8"),
        # Works with both the new "AQ." auth keys and legacy "AIza" keys.
        headers={"Content-Type": "application/json", "x-goog-api-key": key.strip()},
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
        if e.code == 400 and tcfg and "thinking" in low:
            raise GeminiFail(True, text, e.code, "thinking_cfg")
        if e.code == 400 and json_mode and ("json mode" in low or "mime" in low):
            raise GeminiFail(True, text, e.code, "json_mode")
        if e.code == 429:
            raise GeminiFail(True, text, e.code, "quota")
        if e.code >= 500:
            raise GeminiFail(True, text, e.code, "transient")
        if e.code == 404 or (e.code == 400 and "model" in low):
            raise GeminiFail(True, text, e.code, "model")
        if e.code in (401, 403) or (e.code == 400 and "api key" in low):
            raise GeminiFail(True, text, e.code, "key")
        raise GeminiFail(False, text, e.code, "")
    except Exception as e:  # network error / socket timeout
        reason = str(e)[:150]
        if "timed out" in reason.lower():
            reason = "%s did not answer within %ds (timeout)" % (model, int(timeout))
        raise GeminiFail(True, reason, 0, "transient")

    try:
        parts = json.loads(raw)["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise ValueError("empty text")
        return text
    except Exception as e:
        raise GeminiFail(True, "Unexpected Gemini response: %s" % str(e)[:200], 0, "bad_response")


def _try_once(model, key, prompt, timeout, media=None):
    """One model+key attempt; adapts the request format, never repeats on 503/429."""
    for _ in range(3):
        try:
            return gemini_request(
                model, key, prompt, timeout=timeout,
                json_mode=model not in _NO_JSON_MODE,
                thinking=model not in _NO_THINKING_CFG,
                media=media,
            )
        except GeminiFail as e:
            # Only format problems are re-sent to the same model.
            if e.kind == "thinking_cfg":
                _NO_THINKING_CFG.add(model)
                continue
            if e.kind == "json_mode":
                _NO_JSON_MODE.add(model)
                continue
            raise
    raise GeminiFail(True, "%s rejected the request format" % model, 400, "model")


# ---------------------------------------------------------------------------
# Carousel
# ---------------------------------------------------------------------------


def is_text_model(m):
    """True for text-generation models from a ListModels entry."""
    name = (m.get("name") or "").replace("models/", "")
    if "generateContent" not in (m.get("supportedGenerationMethods") or []):
        return False
    if not (name.startswith("gemini") or name.startswith("gemma")):
        return False
    low = name.lower()
    return not any(marker in low for marker in NON_TEXT_MARKERS)


def _rank(name):
    """Order for models outside PREFERRED_MODELS: fast flash first, pro/gemma last."""
    low = name.lower()
    m = re.search(r"(\d+(?:\.\d+)?)", low)
    version = float(m.group(1)) if m else 0.0
    unstable = ("preview" in low or "exp" in low
                or re.search(r"-\d{3}$|-\d{2}-\d{2}", low) is not None)
    if low.startswith("gemma"):
        group = 6
    elif "flash" in low and "lite" not in low:
        group = 2 if unstable else 1
    elif "flash" in low:
        group = 3
    elif "pro" in low:
        group = 5
    else:
        group = 4
    return (group, -version, low)


def order_models(names):
    """Preferred models (that exist) first, in preferred order; then the rest."""
    available = set(names)
    head = [m for m in PREFERRED_MODELS if m in available]
    tail = sorted((m for m in available if m not in head), key=_rank)
    return head + tail


def list_text_models(key, timeout=LIST_TIMEOUT_SEC):
    """Ask Gemini which models this key can use; keep only text models."""
    found, total, page_token = [], 0, ""
    for _ in range(10):  # pagination guard
        url = GEMINI_BASE.rstrip("/") + "?pageSize=1000"
        if page_token:
            url += "&pageToken=" + urllib.parse.quote(page_token)
        req = urllib.request.Request(url, headers={"x-goog-api-key": key.strip()})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("models") or []:
            total += 1
            if is_text_model(m):
                name = m["name"].replace("models/", "")
                if name not in found:
                    found.append(name)
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return order_models(found), total


def get_models(force=False):
    """Current ring of text models (ListModels result, cached)."""
    if ENV_MODELS:
        if MODELS["source"] != "env":
            MODELS.update(list=list(ENV_MODELS), source="env", fetched_at=time.time())
        return MODELS["list"]
    fresh = time.time() - MODELS["fetched_at"] < MODEL_LIST_TTL_SEC
    if MODELS["list"] and fresh and not force:
        return MODELS["list"]
    for key in GEMINI_KEYS:
        try:
            models, total = list_text_models(key)
        except Exception:
            continue  # try the next key
        if models:
            MODELS.update(list=models, source="gemini", fetched_at=time.time(), discovered=total)
            return models
    # ListModels failed for every key: keep the last good list, else fallback.
    if not MODELS["list"] or MODELS["source"] != "gemini":
        MODELS.update(list=list(PREFERRED_MODELS), source="fallback", fetched_at=time.time())
    return MODELS["list"]


def _drop_model(model):
    """Model is gone (404): remove it from the ring until the next refresh."""
    if model in MODELS["list"] and len(MODELS["list"]) > 1:
        MODELS["list"] = [m for m in MODELS["list"] if m != model]


def attempt_order(models, n_keys=None):
    """All (model, key_index) pairs in carousel order.

    The model changes on EVERY step; the key changes each time the model ring
    wraps around. Starts from the model + key that answered last.
    """
    m = len(models)
    k = len(GEMINI_KEYS) if n_keys is None else n_keys
    sm = models.index(STATE["model"]) if STATE["model"] in models else 0
    sk = STATE["key_i"] % k
    return [(models[(sm + i) % m], (sk + i // m) % k) for i in range(m * k)]


def call_gemini(prompt, budget=None, media=None, try_timeout=None, hedge_after=None, max_parallel=None):
    """Send a prompt through the model/key carousel. Returns the response text.

    Hedged carousel:
    * error (503 / 429 / timeout / 5xx) -> the next model starts immediately;
    * no answer after HEDGE_AFTER_SEC   -> the next model ALSO starts, in
      parallel (max MAX_PARALLEL in flight), so one slow overloaded model
      cannot eat the whole time budget;
    * the first successful answer wins; each model waits <= GEMINI_TRY_TIMEOUT.

    media / try_timeout / hedge_after / max_parallel: used for multimodal calls
    (Gemini watching a YouTube video needs more than 25 s per model, and fewer
    parallel copies because every copy processes the whole video).
    """
    try_timeout = try_timeout or GEMINI_TRY_TIMEOUT
    hedge_after = hedge_after or HEDGE_AFTER_SEC
    max_parallel = max_parallel or MAX_PARALLEL
    if not GEMINI_KEYS:
        raise GeminiFail(False, "NO_KEYS")

    started = time.time()
    end = started + (budget or GEMINI_CALL_BUDGET)
    if REQUEST["deadline"]:
        end = min(end, REQUEST["deadline"] - 4)

    models = get_models()
    if media:  # only Gemini models accept video input (not Gemma)
        models = [m for m in models if m.lower().startswith("gemini")] or models
    if not models:
        raise GeminiFail(False, "Gemini returned no text models for these keys.", 0, "model")

    pending = list(attempt_order(models))
    results = queue.Queue()
    dead_models, dead_keys = set(), set()
    errors = []      # (model, key_index, GeminiFail)
    in_flight = {}   # attempt id -> (model, key_index)
    next_id = [0]

    def worker(aid, model, key_i, timeout):
        try:
            results.put((aid, model, key_i, _try_once(model, GEMINI_KEYS[key_i], prompt, timeout, media), None))
        except GeminiFail as e:
            results.put((aid, model, key_i, None, e))
        except Exception as e:  # never let a thread die silently
            results.put((aid, model, key_i, None, GeminiFail(True, str(e)[:200], 0, "transient")))

    def launch():
        """Start the next useful (model, key) pair. False if none can start."""
        while pending:
            model, key_i = pending.pop(0)
            if model in dead_models or key_i in dead_keys:
                continue
            busy = {m for m, _ in in_flight.values()}
            if model in busy:  # same model already running with another key
                pending.append((model, key_i))
                if all(m in busy or m in dead_models or k in dead_keys for m, k in pending):
                    return False
                continue
            remaining = end - time.time()
            if remaining < MIN_ATTEMPT_SEC:
                pending.clear()
                return False
            aid = next_id[0]
            next_id[0] += 1
            in_flight[aid] = (model, key_i)
            t = threading.Thread(target=worker, args=(aid, model, key_i, min(try_timeout, remaining - 1)))
            t.daemon = True
            t.start()
            return True
        return False

    launch()
    while in_flight:
        wait = min(hedge_after, max(0.05, end - time.time()))
        try:
            aid, model, key_i, text, err = results.get(timeout=wait)
        except queue.Empty:
            if time.time() >= end:
                break
            if len(in_flight) < max_parallel:
                launch()  # slow answer: hedge with the next model
            continue
        in_flight.pop(aid, None)
        if err is None:
            STATE.update(model=model, key_i=key_i)
            return text
        errors.append((model, key_i, err))
        if err.kind == "model":
            dead_models.add(model)
            if err.code == 404:
                _drop_model(model)
        elif err.kind == "key":
            dead_keys.add(key_i)
        elif not err.retryable:
            raise err
        if len(dead_keys) == len(GEMINI_KEYS) or len(dead_models) == len(models):
            break
        # 503 / 429 / timeout: switch to the next model immediately.
        launch()
        if not in_flight and pending:
            launch()

    if not errors and in_flight:
        errors.append((in_flight[next(iter(in_flight))][0], 0,
                       GeminiFail(True, "no answer within the time budget (timeout)", 0, "transient")))
    raise GeminiFail(False, _summarize(errors), 0, _dominant_kind(errors))


def _dominant_kind(errors):
    kinds = [e.kind for _, _, e in errors]
    if not kinds:
        return "transient"
    for k in ("key", "quota", "transient", "bad_response"):
        if k in kinds and all(x in (k, "model") for x in kinds):
            return k
    if "quota" in kinds:
        return "quota"
    if "transient" in kinds:
        return "transient"
    return kinds[-1]


def _summarize(errors):
    if not errors:
        return "No Gemini attempt could be made (time budget exhausted)."
    kind = _dominant_kind(errors)
    head = {
        "quota": "All Gemini models returned 429 (quota / rate limit exceeded).",
        "key": "Gemini rejected the API key (401/403). Check GEMINI_API_KEY.",
        "transient": "Gemini is temporarily unavailable (5xx / timeout).",
        "model": "None of the configured Gemini models is available for this key.",
    }.get(kind, "Gemini request failed.")
    sample = next((e.message for _, _, e in errors if e.kind == kind), errors[-1][2].message)
    tried = " -> ".join(m for m, _, _ in errors[:8]) + (" ..." if len(errors) > 8 else "")
    return "%s Attempts: %d (%s). Models in carousel: %d (%s). Google says: %s" % (
        head, len(errors), tried, len(MODELS["list"]), MODELS["source"] or "?", sample[:220])


def diagnostics(refresh=True):
    """Safe info for GET /api/analyze?models=1 (never exposes keys)."""
    models = get_models(force=refresh) if GEMINI_KEYS else []
    nxt = STATE["model"] if STATE["model"] in models else (models[0] if models else None)
    return {
        "source": MODELS["source"] or "none",
        "listed_by_gemini_total": MODELS["discovered"],
        "text_models": models,
        "next_model": nxt,
        "last_ok": STATE["model"],
        "keys_configured": len(GEMINI_KEYS),
        "try_timeout_sec": GEMINI_TRY_TIMEOUT,
    }


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------


def extract_json(text):
    """Robustly pull a JSON object out of a model response."""
    if not text:
        raise ValueError("empty model response")
    text = text.strip()
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
