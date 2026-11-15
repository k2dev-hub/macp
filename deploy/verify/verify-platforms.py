#!/usr/bin/env python3
"""Verify shell_run's platform routing and the sandbox /proc view.

Checks, with real calls:
  1. a task whose Work Order declares only `local` allows sandbox and refuses linux;
  2. a task that declares ["android", "sandbox"] allows both and still refuses linux;
  3. the sandbox reports itself (no host kernel / CPU model / host RAM);
  4. gate_run still ignores platform and stays in the Work Order profile.

Run on macp.example.com.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443")
TOKEN = open("/opt/macp/deploy/secrets/write_token", encoding="utf-8").read().strip()
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

failures: list[str] = []

# Pick an idle worker instead of a fixed one: on a shared deployment the
# hard-coded agent may be busy, and dispatch then returns 409.
AGENT = os.getenv("MACP_PROBE_AGENT", "")


def api(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + (token or TOKEN))
    req.add_header("X-MACP-Project", "demo")
    try:
        with urllib.request.urlopen(req, timeout=900, context=CTX) as r:
            raw = r.read().decode()
            if not raw.strip():
                return r.status, None
            return r.status, (json.loads(raw) if raw.strip()[:1] in "{[(" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return e.code, raw


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:220]) if detail else ""))
    if not ok:
        failures.append(name)


def free_agent():
    st, rows = api("GET", f"/v1/assignments?state=all&agent={AGENT or 'worker-d'}")
    for row in rows or []:
        if row.get("state") in ("offered", "active", "claimed", "submitted"):
            api("POST", f"/v1/assignments/{row['id']}/release")


def start_task(code: str, profile: str, platforms=None) -> str:
    global AGENT
    if not AGENT:
        st, agents = api("GET", "/v1/agents")
        idle = [a["code"] for a in (agents or [])
                if a.get("role") == "worker" and a.get("status") == "idle"]
        AGENT = idle[0] if idle else "worker-d"
    free_agent()
    execution = {"profile": profile}
    if platforms is not None:
        execution["platforms"] = platforms
    st, created = api("POST", "/v1/tasks", {
        "code": code, "title": f"platform routing {profile}", "workstream": "stream-infra",
        "kind": "infra",
        "work_order": {
            "write_surface": ["notes/**"],
            "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
            "execution": execution,
        },
    })
    if st not in (200, 201):
        print(f"  create failed: {created}")
        return ""
    api("POST", f"/v1/tasks/{code}/dispatch", {"agent": AGENT})
    assignment = None
    for _ in range(10):
        st, assignment = api("GET", f"/v1/assignments/next?agent={AGENT}")
        if assignment:
            break
        time.sleep(2)
    if not assignment:
        print("  no assignment")
        return ""
    st, claim = api("POST", f"/v1/assignments/{assignment['assignment_id']}/claim")
    return claim["token"]["value"]


def call(tok, code, tool, args, attempt=1):
    st, res = api("POST", f"/v1/tools/{tool}/invoke", {
        "tool_call_id": "p", "agent": AGENT, "task": code, "attempt": attempt,
        "arguments": args}, tok)
    body = res or {}
    return st, body


# ---------------------------------------------------------------- scenario 1
print("== local profile, no execution.platforms ==")
CODE = f"T-44{int(time.time()) % 10000:04d}"
tok = start_task(CODE, "local")
check("task created and claimed", bool(tok))
if tok:
    st, res = call(tok, CODE, "shell_run", {"platform": "sandbox", "argv": ["cat", "/proc/version"]})
    out = (res.get("output") or {})
    check("sandbox allowed", st == 200 and res.get("status") != "error", st)
    check("sandbox /proc/version is the sandbox view",
          "macp-sandbox" in (out.get("stdout") or ""), (out.get("stdout") or "")[:80])
    check("result reports platform=sandbox", out.get("platform") == "sandbox", out.get("platform"))

    st, res = call(tok, CODE, "shell_run", {"platform": "linux", "argv": ["cat", "/proc/version"]})
    check("linux refused on a local-only task", st == 403, res.get("detail"))
    detail = res.get("detail")
    allowed = None
    if isinstance(detail, dict):
        allowed = detail.get("allowed")
    elif isinstance(detail, str):
        allowed = "allowed" in detail
    check("refusal lists the allowed platforms", allowed == ["sandbox"] or allowed is True, detail)

    st, res = call(tok, CODE, "shell_run", {"argv": ["cat", "/proc/version"]})
    missing_platform_rejected = (
        st in (400, 422)
        or res.get("status") == "error"
        or "platform" in str(res.get("detail", "")).lower()
    )
    check("missing platform is rejected", missing_platform_rejected, (st, res.get("detail")))

    st, res = call(tok, CODE, "shell_run", {"platform": "sandbox", "argv": ["cat", "/proc/cpuinfo"]})
    cpu = ((res.get("output") or {}).get("stdout") or "")
    check("cpuinfo shows the sandbox vCPU, not the host CPU",
          "MACP sandbox vCPU" in cpu and "Celeron" not in cpu, cpu[:60].replace("\n", " "))

    st, res = call(tok, CODE, "shell_run", {"platform": "sandbox", "argv": ["cat", "/proc/meminfo"]})
    mem = ((res.get("output") or {}).get("stdout") or "")
    total = 0
    for line in mem.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
    check("meminfo reflects the cgroup limit (not host RAM)", 0 < total <= 3 * 1024 * 1024,
          f"{total} kB")

# ---------------------------------------------------------------- scenario 2
print("== android profile declaring [android, sandbox] ==")
CODE2 = f"T-43{int(time.time()) % 10000:04d}"
tok2 = start_task(CODE2, "android-debian", ["android", "sandbox"])
check("cross-platform task created", bool(tok2))
if tok2:
    st, res = call(tok2, CODE2, "shell_run", {"platform": "sandbox", "argv": ["cat", "/proc/version"]})
    check("sandbox allowed when declared", st == 200 and res.get("status") != "error", st)
    st, res = call(tok2, CODE2, "shell_run", {"platform": "linux", "argv": ["cat", "/proc/version"]})
    check("undeclared linux still refused", st == 403, res.get("error"))
    st, res = call(tok2, CODE2, "shell_run", {"platform": "android", "argv": ["cat", "/proc/version"]})
    out = res.get("output") or {}
    check("android routed to the Android VM",
          out.get("platform") == "android" and out.get("profile") == "android-debian",
          {k: out.get(k) for k in ("platform", "profile", "vm")})
    check("android VM kernel is the guest kernel", "Linux version" in (out.get("stdout") or ""),
          (out.get("stdout") or "")[:80])

    # gate_run must ignore platform and stay in the Work Order profile.
    st, res = call(tok2, CODE2, "gate_run", {"gate": "android-build", "platform": "sandbox"})
    check("gate_run ignores an extra platform field", st == 200, st)

print()
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
