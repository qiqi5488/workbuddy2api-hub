"""An API key past its deadline must stop working, and say why.

The panel can give a key a validity window. Two things have to hold once it
closes:

  * the key is refused - on /v1/models and on both chat endpoints - without
    anything having to run in the background, so the deadline is compared
    against the clock on every request;
  * the reply says the key reached its usage time. Answering a generic
    "invalid api key" for a key that is in fact correct sends the operator
    hunting for a typo and hides the real reason a client stopped working.

An absent or zero deadline means "永久有效", which is what every key written
before the field existed reads back as, so an upgrade cannot lock anyone out.
An unparseable deadline is treated as expired rather than as "never": a
hand-edited file with a typo must fail closed, not grant permanent validity.

The settings half needs no network. The HTTP half starts a real server on a
spare port with a throwaway store.

    python _test_api_key_expiry.py
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
NOW = 1_800_000_000  # a fixed clock, so the cases cannot drift


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------- settings
print("[1] key_is_expired compares the deadline against the clock")

check("a past deadline is expired",
      S.key_is_expired({"expires_at": NOW - 1}, now=NOW))
check("the exact deadline counts as expired (no off-by-one second)",
      S.key_is_expired({"expires_at": NOW}, now=NOW))
check("a future deadline is not",
      not S.key_is_expired({"expires_at": NOW + 1}, now=NOW))
check("zero means never expires", not S.key_is_expired({"expires_at": 0}, now=NOW))
check("a missing field means never expires", not S.key_is_expired({}, now=NOW))
check("a missing entry means never expires", not S.key_is_expired(None, now=NOW))
check("an unparseable deadline fails closed",
      S.key_is_expired({"expires_at": "not-a-time"}, now=NOW))
check("a numeric string is accepted",
      S.key_is_expired({"expires_at": str(NOW - 5)}, now=NOW))

print()
print("[2] normalization keeps the field usable and never widens a key")

check("a missing value stores as never", S._clean_key_expiry(None) == 0)
check("an empty string stores as never", S._clean_key_expiry("") == 0)
check("an int survives", S._clean_key_expiry(1893456000) == 1893456000)
check("a float string is truncated to an int", S._clean_key_expiry("1893456000.9") == 1893456000)
check("an unparseable value fails closed, not to 'never'",
      S._clean_key_expiry("tomorrow") < 0)
check("that stored value then reads as expired",
      S.key_is_expired({"expires_at": S._clean_key_expiry("tomorrow")}, now=NOW))

print()
print("[3] a deadline round-trips, and old keys stay永久有效")

d = tempfile.mkdtemp(prefix="wb-keyexp-")
stored = S.set_api_keys(d, [
    {"id": "k1", "name": "临时", "key": "key-temp", "expires_at": NOW + 3600},
    {"id": "k2", "name": "永久", "key": "key-forever"},
])
check("the deadline is stored", stored[0]["expires_at"] == NOW + 3600, stored[0])
check("a row without the field stores as never", stored[1]["expires_at"] == 0, stored[1])
check("and round-trips through settings.json",
      [e["expires_at"] for e in S.api_keys(d)] == [NOW + 3600, 0], S.api_keys(d))

d_legacy = tempfile.mkdtemp(prefix="wb-keyexp-legacy-")
S.save(d_legacy, {"api_keys": [{"id": "old", "name": "old", "key": "key-old"}]})
check("a key stored before the field existed never expires",
      S.api_keys(d_legacy)[0]["expires_at"] == 0, S.api_keys(d_legacy)[0])

print()
print("[4] a matched-but-expired key is reported, not silently mismatched")

d2 = tempfile.mkdtemp(prefix="wb-keyexp-match-")
S.set_api_keys(d2, [
    {"id": "k1", "name": "到期Key", "key": "KEYEXPIRED", "expires_at": 1},
    {"id": "k2", "name": "有效Key", "key": "KEYLIVE", "expires_at": int(time.time()) + 3600},
    {"id": "k3", "name": "永久Key", "key": "KEYFOREVER", "expires_at": 0},
])
matched = S.match_api_key(d2, "KEYEXPIRED")
check("the expired key still matches (so the caller can explain)",
      matched is not None and matched.get("id") == "k1", matched)
check("and is flagged expired", matched.get("expired") is True, matched)
check("a live key matches unflagged",
      S.match_api_key(d2, "KEYLIVE").get("expired") is False)
check("a never-expiring key matches unflagged",
      S.match_api_key(d2, "KEYFOREVER").get("expired") is False)
check("an unknown key still matches nothing", S.match_api_key(d2, "NOPE") is None)
check("a disabled key still matches nothing",
      S.match_api_key(d2, "KEYLIVE") is not None
      and S.set_api_keys(d2, [{"id": "k2", "name": "有效Key", "key": "KEYLIVE",
                               "expires_at": int(time.time()) + 3600,
                               "enabled": False}])
      and S.match_api_key(d2, "KEYLIVE") is None)
check("the launcher key never expires",
      S.match_api_key(d2, "LAUNCH", extra_keys=("LAUNCH",)).get("expired") is False)

print()
print("[5] the panel-visible text names the deadline")

check("a deadline formats for humans",
      S.key_expiry_text({"expires_at": NOW}) != "", S.key_expiry_text({"expires_at": NOW}))
check("no deadline formats to empty (rendered as 永久有效)",
      S.key_expiry_text({"expires_at": 0}) == "")
check("an unparseable deadline still says something",
      S.key_expiry_text({"expires_at": "junk"}) == "未知",
      S.key_expiry_text({"expires_at": "junk"}))


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
work = tempfile.mkdtemp(prefix="wbkeyexp_")
store = os.path.join(work, "accounts")
os.makedirs(store)
past = int(time.time()) - 3600
future = int(time.time()) + 3600
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"api_keys": [
        {"id": "k1", "name": "已到期Key", "key": "KEYEXPIRED",
         "expires_at": past, "enabled": True},
        {"id": "k2", "name": "有效Key", "key": "KEYLIVE",
         "expires_at": future, "enabled": True},
        {"id": "k3", "name": "永久Key", "key": "KEYFOREVER", "expires_at": 0, "enabled": True},
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
    print("[6] an expired key is refused everywhere, with the reason")

    status, err = request(port, "/v1/models", key="KEYEXPIRED")
    message = ((err.get("error") or {}).get("message") or "")
    check("/v1/models -> 403", status == 403, (status, err))
    check("the reply says the usage time was reached", "已到达使用时间" in message, message)
    check("it names the key", "已到期Key" in message, message)
    check("it shows the deadline", "有效期至" in message, message)
    check("it is not reported as an invalid key", "invalid api key" not in message, message)

    status, err = request(
        port, "/v1/chat/completions", method="POST", key="KEYEXPIRED",
        body={"model": "deepseek-v4.1-flash",
              "messages": [{"role": "user", "content": "hi"}]})
    message = ((err.get("error") or {}).get("message") or "")
    check("chat completions -> 403", status == 403, (status, err))
    check("and says the same thing", "已到达使用时间" in message, message)

    status, err = request(
        port, "/v1/responses", method="POST", key="KEYEXPIRED",
        body={"model": "deepseek-v4.1-flash", "input": "hi"})
    message = ((err.get("error") or {}).get("message") or "")
    check("responses -> 403", status == 403, (status, err))
    check("and says the same thing", "已到达使用时间" in message, message)

    print()
    print("[7] live and never-expiring keys are unaffected")

    status, models = request(port, "/v1/models", key="KEYLIVE")
    check("a key with a future deadline still lists models", status == 200, (status, models))
    check("and is not filtered to nothing (unrestricted models)",
          len(models.get("data") or []) > 1, len(models.get("data") or []))

    status, _ = request(
        port, "/v1/chat/completions", method="POST", key="KEYLIVE",
        body={"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]})
    check("a live key reaches the upstream path (503: no accounts)",
          status == 503, status)

    status, models = request(port, "/v1/models", key="KEYFOREVER")
    check("a key with no deadline still works", status == 200, status)

    status, err = request(port, "/v1/models", key="NOTAKEY")
    message = ((err.get("error") or {}).get("message") or "")
    check("an unknown key still gets the plain invalid-key reply",
          status == 401 and "invalid api key" in message, (status, message))

    print()
    print("[8] the panel shows and edits the deadline")

    status, login = request(port, "/panel/login", method="POST", body={"password": "admin"})
    token = login.get("token") or ""
    check("panel login succeeded", status == 200 and bool(token), (status, login))

    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the deadline is reported", by_id.get("k1", {}).get("expires_at") == past,
          by_id.get("k1"))
    check("the expired flag is reported", by_id.get("k1", {}).get("expired") is True,
          by_id.get("k1"))
    check("a live key reports not expired", by_id.get("k2", {}).get("expired") is False,
          by_id.get("k2"))
    check("a permanent key reports no deadline", by_id.get("k3", {}).get("expires_at") == 0,
          by_id.get("k3"))

    # The panel always posts the whole list, so these saves do too: /settings/save
    # replaces the stored set rather than merging into it.
    def current_rows():
        _, view = request(port, "/settings", panel_token=token)
        out = []
        for k in view.get("api_keys") or []:
            out.append({
                "id": k.get("id"),
                "name": k.get("name"),
                "key": "",  # blank = keep the stored value
                "realm": k.get("realm") or "",
                "models": k.get("models") or [],
                "expires_at": k.get("expires_at") or 0,
                "enabled": k.get("enabled") is not False,
            })
        return out

    def save_rows(rows):
        return request(port, "/settings/save", method="POST", panel_token=token,
                       body={"api_keys": rows})

    def with_expiry(rows, key_id, value, drop=False):
        """Copy `rows`, setting (or removing) one row's expiry field."""
        out = []
        for row in rows:
            row = dict(row)
            if row["id"] == key_id:
                if drop:
                    row.pop("expires_at", None)
                else:
                    row["expires_at"] = value
            out.append(row)
        return out

    # Extending the deadline must bring the key back without touching its value.
    status, saved = save_rows(with_expiry(current_rows(), "k1", future))
    check("extending the deadline saved", status == 200, (status, saved))
    status, models = request(port, "/v1/models", key="KEYEXPIRED")
    check("the renewed key works again", status == 200, (status, models))
    check("and its stored value was not blanked",
          S.match_api_key(store, "KEYEXPIRED") is not None)

    # Clearing it must make the key permanent again.
    status, saved = save_rows(with_expiry(current_rows(), "k1", 0))
    check("clearing the deadline saved", status == 200, (status, saved))
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the key is permanent again",
          by_id.get("k1", {}).get("expires_at") == 0
          and by_id.get("k1", {}).get("expired") is False, by_id.get("k1"))

    # An older panel build posts the whole list but does not know the field;
    # that must not strip a deadline that is already stored.
    status, saved = save_rows(with_expiry(current_rows(), "k2", None, drop=True))
    check("a save without the expiry field succeeded", status == 200, (status, saved))
    status, view = request(port, "/settings", panel_token=token)
    by_id = {k.get("id"): k for k in view.get("api_keys") or []}
    check("the stored deadline was not stripped by an old client",
          by_id.get("k2", {}).get("expires_at") == future, by_id.get("k2"))

    status, err = save_rows(with_expiry(current_rows(), "k2", "tomorrow"))
    check("a non-numeric deadline is rejected", status == 400, (status, err))

    status, err = save_rows(with_expiry(current_rows(), "k2", -5))
    check("a negative deadline is rejected", status == 400, (status, err))

    print()
    print("[9] the panel session itself is never locked out by an expiry")

    status, view = request(port, "/settings", panel_token=token)
    check("the admin can still read settings after a key expired", status == 200, status)

    print()
    print("[10] with key checking switched off, an expired key is not singled out")

    # Section [8] left k1 permanent, so give it a past deadline again first.
    status, saved = save_rows(with_expiry(current_rows(), "k1", past))
    check("k1 is expired again", status == 200, status)
    status, err = request(port, "/v1/models", key="KEYEXPIRED")
    check("and is refused while checking is on", status == 403, status)

    # auth_disabled means no credential is needed at all: an anonymous request
    # already succeeds, so refusing one that happens to carry an expired key
    # would be inconsistent and would look like a bug to the operator.
    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"auth_disabled": True})
    check("key checking was switched off", status == 200 and saved.get("auth_disabled") is True,
          (status, saved))
    status, _ = request(port, "/v1/models", key="KEYEXPIRED")
    check("an expired key is let through like an anonymous request", status == 200, status)
    status, _ = request(port, "/v1/models")
    check("and an anonymous request behaves the same", status == 200, status)

    status, saved = request(port, "/settings/save", method="POST", panel_token=token,
                            body={"auth_disabled": False})
    check("key checking was switched back on", status == 200, (status, saved))
    status, err = request(port, "/v1/models", key="KEYEXPIRED")
    message = ((err.get("error") or {}).get("message") or "")
    check("the expired key is refused again, with the reason",
          status == 403 and "已到达使用时间" in message, (status, message))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    for path in (work, d, d_legacy, d2):
        shutil.rmtree(path, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
