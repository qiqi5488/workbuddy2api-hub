"""An API key with a token cap must stop working once it has spent the cap.

The panel lets an operator set a cumulative total-token limit per key. That cap
is the key's whole life, not a rolling window, so the spent count has to be
persisted somewhere a restart cannot reset it. Two things have to hold:

  * once `used >= limit`, a new Chat Completions / Responses request is refused
    locally with a 403 that names the key, the used total and the limit, before
    the upstream is touched - so a spent key burns nothing more;
  * a request that succeeds attributes its total_tokens to the key that made
    it, and the counter survives a process restart.

An absent or zero limit means "unlimited", which is what every key written
before the field existed reads back as, so an upgrade cannot lock anyone out.

The settings and counter halves need no network. The HTTP half starts a real
server on a spare port with a throwaway store, pre-seeding the persisted
counter to exercise the enforcement boundary without needing an upstream
account.

    python _test_api_key_tokens.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

sys.path.insert(0, HERE)
import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------- settings
print("[1] token_limit is normalized and defaults to unlimited")

check("an absent limit reads back as 0", S._clean_key_token_limit(None) == 0)
check("an empty string reads back as 0", S._clean_key_token_limit("") == 0)
check("an int survives", S._clean_key_token_limit(500000) == 500000)
check("a numeric string survives", S._clean_key_token_limit("500000") == 500000)
check("a float string truncates", S._clean_key_token_limit("500000.9") == 500000)
check("a negative value degrades to 0 (unlimited)", S._clean_key_token_limit(-5) == 0)
check("an unparseable value degrades to 0 rather than crash",
      S._clean_key_token_limit("lots") == 0)

d = tempfile.mkdtemp(prefix="wb-keytok-")
stored = S.set_api_keys(d, [
    {"id": "k1", "name": "限流", "key": "key-capped", "token_limit": 123456},
    {"id": "k2", "name": "不限", "key": "key-open"},
])
check("the cap is stored", stored[0]["token_limit"] == 123456, stored[0])
check("a row without the field is unlimited", stored[1]["token_limit"] == 0, stored[1])
check("and round-trips through settings.json",
      [e["token_limit"] for e in S.api_keys(d)] == [123456, 0], S.api_keys(d))

d_legacy = tempfile.mkdtemp(prefix="wb-keytok-legacy-")
S.save(d_legacy, {"api_keys": [{"id": "old", "name": "old", "key": "key-old"}]})
check("a key stored before the field existed is unlimited",
      S.api_keys(d_legacy)[0]["token_limit"] == 0, S.api_keys(d_legacy)[0])


# ---------------------------------------------------------------- counter
print()
print("[2] the per-key counter accumulates, persists and resets")

import wb_proxy as P

work = tempfile.mkdtemp(prefix="wb-keytok-counter-")
P.ACCOUNTS_DIR = work
P._key_tokens.clear()
P.load_key_tokens()
check("a fresh store starts at zero", P.key_token_usage("k1") == 0)

P.key_token_add("k1", 150)
P.key_token_add("k1", 350)
check("additions accumulate", P.key_token_usage("k1") == 500, P.key_token_usage("k1"))
check("other keys are independent", P.key_token_usage("k2") == 0)

# Reload from disk, as a restart would.
P.load_key_tokens()
check("the total survives a reload", P.key_token_usage("k1") == 500, P.key_token_usage("k1"))

P.key_token_add("k1", 0)
check("adding zero does not move the total", P.key_token_usage("k1") == 500)
P.key_token_add("", 100)
check("a missing key id is a no-op", P.key_token_usage("k1") == 500)

P.key_token_reset("k1")
check("reset zeroes the key", P.key_token_usage("k1") == 0)
P.load_key_tokens()
check("and the reset survives a reload", P.key_token_usage("k1") == 0)
P.key_token_add("k1", 50)
P.key_token_add("k2", 75)
P.key_token_reset("k1")
check("resetting one key leaves the others", P.key_token_usage("k2") == 75)
shutil.rmtree(work, ignore_errors=True)

print()
print("[2b] record_usage attributes total_tokens to the requesting key")

work2 = tempfile.mkdtemp(prefix="wb-keytok-attrib-")
P.ACCOUNTS_DIR = work2
P.USAGE_DIR = work2
P.USAGE_LOG = os.path.join(work2, "usage.jsonl")
P._key_tokens.clear()
P.load_key_tokens()

fake = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 0}}
P.record_usage("glm-5.3", fake, key_id="k1")
check("the completed request is attributed", P.key_token_usage("k1") == 150,
      P.key_token_usage("k1"))

with open(P.USAGE_LOG, encoding="utf-8") as fh:
    rows = [json.loads(line) for line in fh if line.strip()]
check("the key id is written to the usage row", rows and rows[-1].get("key_id") == "k1",
      rows[-1] if rows else None)

# A request with no usage block must not move the counter.
P.record_usage("glm-5.3", None, key_id="k1")
check("a request without usage does not inflate the counter",
      P.key_token_usage("k1") == 150, P.key_token_usage("k1"))

# A request with no key id must not be attributed (and must not crash).
P.record_usage("glm-5.3", fake, key_id=None)
check("a keyless request leaves the counter alone", P.key_token_usage("k1") == 150)

# A failed request that carried partial usage still spent tokens.
P.record_error("glm-5.3", 502, "stream aborted", usage={"total_tokens": 40},
               key_id="k1")
check("a failed request's partial usage is still counted",
      P.key_token_usage("k1") == 190, P.key_token_usage("k1"))
shutil.rmtree(work2, ignore_errors=True)


# ------------------------------------------------------------ live server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(port, path, method="GET", body=None, key=None, panel_token=None):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if panel_token:
        headers["X-Panel-Token"] = panel_token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")


port = free_port()
work = tempfile.mkdtemp(prefix="wbkeytok_")
store = os.path.join(work, "accounts")
os.makedirs(store)
# key_usage.json seeds the counter so the boundary is testable without a real
# upstream: CAPPED has spent 600 of a 500 limit (already over); BELOW has spent
# 100 of 500 (still under).
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [
        {"id": "capped", "name": "限流Key", "key": "KEYCAPPED",
         "token_limit": 500, "enabled": True},
        {"id": "below", "name": "未满Key", "key": "KEYBELOW",
         "token_limit": 500, "enabled": True},
        {"id": "open", "name": "不限Key", "key": "KEYOPEN", "enabled": True},
    ]}, fh)
with open(os.path.join(store, "key_tokens.json"), "w", encoding="utf-8") as fh:
    json.dump({"capped": 600, "below": 100}, fh)

proc = subprocess.Popen(
    [PY, "wb_proxy.py", "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store],
    cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")),
)


def wait_ready():
    for _ in range(40):
        time.sleep(0.5)
        try:
            if request(port, "/health")[0] == 200:
                return True
        except Exception:
            if proc.poll() is not None:
                return False
    return False


try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)

    print()
    print("[3] a key at its cap is refused before the upstream")

    status, models = request(port, "/v1/models", key="KEYCAPPED")
    check("listing models still works (it costs nothing)", status == 200, status)

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYCAPPED",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    message = ((err.get("error") or {}).get("message") or "")
    check("chat completions -> 403", status == 403, (status, err))
    check("the message names the quota", "额度已用尽" in message, message)
    check("it names the key", "限流Key" in message, message)
    check("it names the used and limit", ("600" in message and "500" in message), message)

    status, err = request(
        port, "/v1/responses", method="POST", key="KEYCAPPED",
        body={"model": "deepseek-v4.1-flash", "input": "hi"})
    message = ((err.get("error") or {}).get("message") or "")
    check("responses -> 403", status == 403, (status, err))
    check("and says the same thing", "额度已用尽" in message, message)

    print()
    print("[4] a key under its cap, and an unlimited key, are unaffected")

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYBELOW",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    check("a key under its cap reaches the upstream path (503: no accounts)",
          status == 503, (status, err))

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYOPEN",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    check("an unlimited key is untouched by the gate", status == 503, (status, err))

    print()
    print("[5] the panel reports, edits and resets the cap")

    status, login = request(port, "/panel/login", method="POST", body={"password": "admin"})
    token = login.get("token") or ""
    check("panel login succeeded", status == 200 and bool(token), (status, login))

    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the cap is reported", by_id.get("capped", {}).get("token_limit") == 500,
          by_id.get("capped"))
    check("the spent count is reported", by_id.get("capped", {}).get("token_used") == 600,
          by_id.get("capped"))
    check("an unlimited key reports no cap", by_id.get("open", {}).get("token_limit") == 0,
          by_id.get("open"))

    # Raising the cap brings the key back without touching the spent count.
    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"api_keys": [
                                {"id": "capped", "name": "限流Key", "key": "",
                                 "token_limit": 1000},
                            ]})
    check("raising the cap saved", status == 200, (status, saved))
    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYCAPPED",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    check("the key is usable again under the higher cap", status == 503, (status, err))

    # The spent count must have survived the save (the reset endpoint is the
    # only thing allowed to zero it).
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("editing the cap did not reset the spent count",
          by_id.get("capped", {}).get("token_used") == 600, by_id.get("capped"))

    # Reset zeroes the count, which frees the key under the original cap.
    status, reset = request(port, "/settings/reset-token-usage", method="POST",
                            panel_token=token, body={"id": "capped"})
    check("the reset succeeded", status == 200 and reset.get("token_used") == 0,
          (status, reset))
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the spent count is zero after reset",
          by_id.get("capped", {}).get("token_used") == 0, by_id.get("capped"))
    check("a reset needs a panel session",
          request(port, "/settings/reset-token-usage", method="POST",
                  body={"id": "capped"})[0] == 401)

    print()
    print("[6] save validation rejects a malformed cap, and an old client cannot erase it")

    status, err = request(port, "/settings/save", method="POST", panel_token=token,
                          body={"api_keys": [
                              {"id": "capped", "name": "限流Key", "key": "",
                               "token_limit": "many"},
                          ]})
    check("a non-numeric cap is rejected", status == 400, (status, err))

    status, err = request(port, "/settings/save", method="POST", panel_token=token,
                          body={"api_keys": [
                              {"id": "capped", "name": "限流Key", "key": "",
                               "token_limit": -1},
                          ]})
    check("a negative cap is rejected", status == 400, (status, err))

    # An older panel build posts rows without the field; the stored cap must
    # survive rather than being widened to unlimited.
    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"api_keys": [
                                {"id": "capped", "name": "限流Key", "key": "",
                                 "realm": "", "models": [], "expires_at": 0,
                                 "enabled": True},
                            ]})
    check("a save without the field succeeded", status == 200, (status, saved))
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the stored cap survived an old client's save",
          by_id.get("capped", {}).get("token_limit") == 1000, by_id.get("capped"))

    status, err = request(port, "/settings/reset-token-usage", method="POST",
                          panel_token=token, body={})
    check("a reset without an id is rejected", status == 400, (status, err))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d_legacy, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
