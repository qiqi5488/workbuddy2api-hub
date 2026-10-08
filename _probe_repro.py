"""Reproduce the recorded-vs-actual effort mismatch in the deployed build.

Two builds matter:

  v1.6.10  build_upstream_body() reads both spellings, but open_upstream()
           reports the effort with upstream_body.get("reasoning_effort") - the
           snake key only. A camelCase request keeps "reasoningEffort", so the
           usage row falls back to the model default instead of the real value.

  v1.6.11+ one shared reader (client_effort_of / upstream_effort_of) is used for
           both the body and the usage row.

This runs the real local code for the "after" column, and applies the v1.6.10
reporting expression to the same built body for the "before" column. No network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="wb-effort-repro-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P  # noqa: E402

MODEL = "deepseek-v4.1-flash"


def msg():
    return [{"role": "user", "content": "hi"}]


CASES = [
    ("DSH camelCase  reasoningEffort=max", {"reasoningEffort": "max"}),
    ("snake          reasoning_effort=max", {"reasoning_effort": "max"}),
    ("DSH camelCase  reasoningEffort=high", {"reasoningEffort": "high"}),
    ("nothing sent (model default)", {}),
]

print("model=%s  catalog defaultEffort=%r  supported=%r"
      % (MODEL, P.model_default_effort(MODEL),
         P.model_reasoning_meta(MODEL).get("supportedEfforts")))
print("")
print("%-36s %-12s %-12s %s" % ("client request", "v1.6.10 row", "v1.6.11+ row", "upstream body key"))
print("-" * 92)

for label, extra in CASES:
    payload = {"model": MODEL, "messages": msg()}
    payload.update(extra)
    body = P.build_upstream_body(payload)

    # What the OLD build recorded: the snake key on the outbound body only.
    old_row = body.get("reasoning_effort")
    if not old_row:
        old_row = P.model_fixed_effort(MODEL) or P.model_default_effort(MODEL)
    # What the NEW build records: one reader over both spellings.
    new_row = P.upstream_effort_of(body, MODEL)

    sent = {k: v for k, v in body.items()
            if k in ("reasoning_effort", "reasoningEffort", "thinking")}
    flag = "  <== MISMATCH" if old_row != new_row else ""
    print("%-36s %-12s %-12s %s%s" % (label, old_row, new_row, sent, flag))
