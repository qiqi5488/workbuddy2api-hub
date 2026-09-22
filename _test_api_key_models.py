"""A key restricted to a set of models must only discover and call those.

The panel lets an operator tick the models a key may use. That has to hold on
both surfaces, otherwise the restriction is cosmetic:

  * `/v1/models` is what populates a client's model picker, so a restricted key
    that still sees the whole catalog offers models every call would reject;
  * a chat / Responses request naming a model outside the list must be refused
    locally, or a client can bypass the picker with a hand-written model id and
    spend the account's quota on a model the operator meant to withhold.

An empty list stays unrestricted: that is what every key written before the
field existed reads back as, so an upgrade cannot lock anyone out.

The settings half needs no network. The HTTP half starts a real server on a
spare port with a throwaway store (no accounts, so allowed requests stop at the
"no usable account" 503 - which is itself proof the model gate let them
through).

    python _test_api_key_models.py
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
print("[1] a key's model list is normalized on read and on write")

d = tempfile.mkdtemp(prefix="wb-keymodels-")
stored = S.set_api_keys(d, [
    {"id": "k1", "name": "restricted", "key": "key-restricted",
     "models": ["GLM-5.3", "glm-5.3", " deepseek-v4.1-flash ", ""]},
    {"id": "k2", "name": "open", "key": "key-open"},
])
check("ids are kept", [e["id"] for e in stored] == ["k1", "k2"], stored)
check("model ids are lowercased and trimmed",
      stored[0]["models"] == ["glm-5.3", "deepseek-v4.1-flash"], stored[0]["models"])
check("duplicates collapse", len(stored[0]["models"]) == 2, stored[0]["models"])
check("a row without the field reads back unrestricted", stored[1]["models"] == [],
      stored[1]["models"])

print()
print("[2] key_allows_model treats an empty list as unrestricted")

restricted, open_key = stored[0], stored[1]
check("a listed model is allowed", S.key_allows_model(restricted, "glm-5.3"))
check("case does not matter for the request model",
      S.key_allows_model(restricted, "GLM-5.3"))
check("an unlisted model is refused", not S.key_allows_model(restricted, "gpt-6-astra"))
check("a partial name is not a match", not S.key_allows_model(restricted, "glm-5"))
check("an empty list allows everything", S.key_allows_model(open_key, "gpt-6-astra"))
check("a missing list allows everything", S.key_allows_model({}, "gpt-6-astra"))
check("a missing entry allows everything", S.key_allows_model(None, "gpt-6-astra"))

print()
print("[3] the restriction survives a reload, and a legacy file stays open")

reloaded = S.api_keys(d)
check("models round-trip through settings.json",
      reloaded[0]["models"] == ["glm-5.3", "deepseek-v4.1-flash"], reloaded[0]["models"])

d_legacy = tempfile.mkdtemp(prefix="wb-keymodels-legacy-")
S.save(d_legacy, {"api_keys": [{"id": "old", "name": "old", "key": "key-old",
                                "realm": "", "enabled": True}]})
legacy = S.api_keys(d_legacy)
check("a key stored before the field existed loads", len(legacy) == 1, legacy)
check("and is treated as unrestricted", legacy[0]["models"] == [], legacy[0])

d_single = tempfile.mkdtemp(prefix="wb-keymodels-single-")
S.save(d_single, {"api_key": "key-single", "api_key_set": True})
single = S.api_keys(d_single)
check("the single-key legacy shape is also unrestricted",
      single and single[0]["models"] == [], single)


# ------------------------------------------------------------ live server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(port, path, method="GET", body=None, key=None, panel_token=None):
    """Return (status, parsed-json-or-{}); HTTPError is returned, not raised."""
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
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        return status, json.loads(raw or "{}")
    except Exception:
        return status, {"raw": raw}


port = free_port()
work = tempfile.mkdtemp(prefix="wbkeymodels_")
store = os.path.join(work, "accounts")
os.makedirs(store)
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [
        {"id": "k1", "name": "restricted", "key": "KEYRESTRICTED",
         "models": ["deepseek-v4.1-flash"], "enabled": True},
        {"id": "k2", "name": "open", "key": "KEYOPEN", "enabled": True},
    ]}, fh)

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
            status, _ = request(port, "/health")
            if status == 200:
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
    print("[4] /v1/models only advertises the models the key may use")

    status, open_list = request(port, "/v1/models", key="KEYOPEN")
    open_ids = [m.get("id") for m in open_list.get("data") or []]
    check("the unrestricted key gets a 200", status == 200, status)
    check("and a full catalog", len(open_ids) > 1, open_ids)
    check("including a model the other key may not use", "gpt-6-astra" in open_ids,
          open_ids)

    status, restricted_list = request(port, "/v1/models", key="KEYRESTRICTED")
    restricted_ids = [m.get("id") for m in restricted_list.get("data") or []]
    check("the restricted key gets a 200", status == 200, status)
    check("and exactly its own model",
          restricted_ids == ["deepseek-v4.1-flash"], restricted_ids)
    check("the entry keeps its full shape",
          restricted_list["data"][0].get("object") == "model"
          and "capabilities" in restricted_list["data"][0],
          restricted_list["data"][0])

    print()
    print("[5] a call to an unlisted model is refused before the upstream")

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYRESTRICTED",
        body={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}]})
    message = ((err.get("error") or {}).get("message") or "")
    check("chat completions -> 400", status == 400, (status, err))
    check("the message names the model", "gpt-6-astra" in message, message)
    check("the message names the key", "restricted" in message, message)
    check("and says what is allowed", "deepseek-v4.1-flash" in message, message)

    status, err = request(
        port, "/v1/responses", method="POST", key="KEYRESTRICTED",
        body={"model": "gpt-6-astra", "input": "hi"})
    message = ((err.get("error") or {}).get("message") or "")
    check("responses -> 400", status == 400, (status, err))
    check("responses names the restriction", "gpt-6-astra" in message, message)

    print()
    print("[6] an allowed model reaches the upstream path (and only that stops it)")

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYRESTRICTED",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    check("an allowed model is not rejected by the model gate",
          status == 503 and b"no usable account".decode() in json.dumps(err),
          (status, err))

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYRESTRICTED",
        body={"model": "DeepSeek-V4.1-Flash",
              "messages": [{"role": "user", "content": "hi"}]})
    check("the match is case-insensitive", status == 503, (status, err))

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYOPEN",
        body={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}]})
    check("an unrestricted key still reaches the upstream path", status == 503,
          (status, err))

    print()
    print("[7] the panel round-trips a model list, and an old client cannot widen it")

    status, login = request(port, "/panel/login", method="POST",
                            body={"password": "admin"})
    token = login.get("token") or ""
    check("panel login succeeded", status == 200 and bool(token), (status, login))

    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"api_keys": [
                                {"id": "k1", "name": "restricted", "key": "",
                                 "models": ["GLM-5.3", "kimi-k3"]},
                                {"id": "k2", "name": "open", "key": "", "models": []},
                            ]})
    check("the save succeeded", status == 200, (status, saved))

    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the stored list is reported back",
          by_id.get("k1", {}).get("models") == ["glm-5.3", "kimi-k3"], by_id.get("k1"))
    check("a cleared list reports as unrestricted",
          by_id.get("k2", {}).get("models") == [], by_id.get("k2"))
    check("masked keys are still masked, not blanked",
          all(k.get("masked") for k in view.get("api_keys") or []), view.get("api_keys"))

    status, models = request(port, "/v1/models", key="KEYRESTRICTED")
    ids = [m.get("id") for m in models.get("data") or []]
    check("the edited list governs /v1/models", ids == ["glm-5.3", "kimi-k3"], ids)

    # An older panel build posts rows without a `models` field at all. That must
    # mean "leave the stored restriction alone", never "clear it".
    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"api_keys": [
                                {"id": "k1", "name": "restricted", "key": ""},
                            ]})
    check("a save without the models field succeeded", status == 200, (status, saved))
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the restriction was not widened by an old client",
          by_id.get("k1", {}).get("models") == ["glm-5.3", "kimi-k3"], by_id.get("k1"))

    status, err = request(port, "/settings/save", method="POST", panel_token=token,
                          body={"api_keys": [
                              {"id": "k1", "name": "restricted", "key": "",
                               "models": "glm-5.3"},
                          ]})
    check("a non-list models value is rejected", status == 400, (status, err))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d_legacy, ignore_errors=True)
    shutil.rmtree(d_single, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
