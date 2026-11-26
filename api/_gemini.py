"""Shared Gemini client for NicheRadar AI API handlers (stdlib only).

Files whose name starts with "_" are NOT exposed by Vercel as endpoints,
so this module is only importable by the handlers in api/.

Carousel rules
--------------
* Models are tried from a fixed ring (GEMINI_MODELS below, or env
  GEMINI_MODELS="a,b,c" to override).
* On HTTP 503 / 429 / 5xx / timeout we do NOT retry the same model:
  we switch instantly to the NEXT model in the ring.
* Keys (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3) rotate too:
  every time the ring of models wraps around, the next key is used, so all
  model x key combinations are covered exactly once per call.
* One request waits at most GEMINI_TRY_TIMEOUT = 25 seconds.
* The model + key that answered last is tried FIRST on the next request.
* 404 (unknown model) skips that model; 401/403 (bad key) skips that key.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
    "gemini-pro-latest",
]

GEMINI_MODELS = [
    m.strip().replace("models/", "")
    for m in (os.environ.get("GEMINI_MODELS") or ",".join(DEFAULT_MODELS)).split(",")
    if m.strip()
]

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

GEMINI_TRY_TIMEOUT = 25    # seconds to wait for ONE model before switching
GEMINI_CALL_BUDGET = 55    # seconds for all attempts of one call_gemini()
REQUEST_BUDGET_SEC = 110   # whole API request (vercel.json maxDuration: 120)

# State of a warm serverless instance.
STATE = {
    "model_i": 0,       # model that answered last -> tried first next time
    "key_i": 0,         # key that answered last
    "last_ok": None,    # "model-name" of the last success (diagnostics)
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


def gemini_request(model, key, prompt, timeout=GEMINI_TRY_TIMEOUT, json_mode=True, thinking=True):
    """One generateContent call. Raises GeminiFail with a classified kind."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 1,
            "maxOutputTokens": 8192,
        },
    }
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


def _try_once(model, key, prompt, timeout):
    """One model+key attempt; adapts the request format, never repeats on 503/429."""
    for _ in range(3):
        try:
            return gemini_request(
                model, key, prompt, timeout=timeout,
                json_mode=model not in _NO_JSON_MODE,
                thinking=model not in _NO_THINKING_CFG,
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


def attempt_order(n_models=None, n_keys=None, start_model=None, start_key=None):
    """All (model_index, key_index) pairs in carousel order.

    The model changes on EVERY step; the key changes each time the model ring
    wraps around. Starts from the pair that answered last.
    """
    m = len(GEMINI_MODELS) if n_models is None else n_models
    k = len(GEMINI_KEYS) if n_keys is None else n_keys
    sm = STATE["model_i"] % m if start_model is None else start_model
    sk = STATE["key_i"] % k if start_key is None else start_key
    return [((sm + i) % m, (sk + i // m) % k) for i in range(m * k)]


def call_gemini(prompt):
    """Send a prompt through the model/key carousel. Returns the response text."""
    if not GEMINI_KEYS:
        raise GeminiFail(False, "NO_KEYS")
    if not GEMINI_MODELS:
        raise GeminiFail(False, "No Gemini models configured (GEMINI_MODELS is empty).")

    started = time.time()
    end = started + GEMINI_CALL_BUDGET
    if REQUEST["deadline"]:
        end = min(end, REQUEST["deadline"] - 4)

    dead_models, dead_keys = set(), set()
    errors = []  # (model, key_index, GeminiFail)

    for model_i, key_i in attempt_order():
        model = GEMINI_MODELS[model_i]
        if model in dead_models or key_i in dead_keys:
            continue
        remaining = end - time.time()
        if remaining < 6:  # not enough time for a meaningful attempt
            break
        try:
            text = _try_once(model, GEMINI_KEYS[key_i], prompt,
                             timeout=min(GEMINI_TRY_TIMEOUT, remaining - 1))
        except GeminiFail as e:
            errors.append((model, key_i, e))
            if e.kind == "model":
                dead_models.add(model)
            elif e.kind == "key":
                dead_keys.add(key_i)
            elif not e.retryable:
                raise
            # 503 / 429 / timeout: switch to the next model immediately.
            if len(dead_keys) == len(GEMINI_KEYS) or len(dead_models) == len(GEMINI_MODELS):
                break
            continue
        STATE.update(model_i=model_i, key_i=key_i, last_ok=model)
        return text

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
    return "%s Attempts: %d (%s). Google says: %s" % (head, len(errors), tried, sample[:240])


def diagnostics():
    """Safe info for GET /api/analyze?models=1 (never exposes keys)."""
    return {
        "models": GEMINI_MODELS,
        "keys_configured": len(GEMINI_KEYS),
        "next_model": GEMINI_MODELS[STATE["model_i"] % len(GEMINI_MODELS)] if GEMINI_MODELS else None,
        "last_ok": STATE["last_ok"],
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
