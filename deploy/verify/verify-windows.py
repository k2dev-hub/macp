#!/usr/bin/env python3
"""Windows build probe: compile a real .exe inside the Windows LTSC node.

Uses the in-box .NET Framework csc.exe (no downloads), ships the workspace with
tar, runs `cmd /c build.bat` through the static-host profile and checks that the
exe comes back with a SHA-256 and lands in the hash chain.

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
CODE = f"T-77{int(time.time()) % 10000:04d}"

failures: list[str] = []


def api(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + (token or TOKEN))
    req.add_header("X-MACP-Project", "demo")
    try:
        with urllib.request.urlopen(req, timeout=1800, context=CTX) as resp:
            raw = resp.read().decode()
            if not raw.strip():
                return resp.status, None
            return resp.status, (json.loads(raw) if raw.strip()[:1] in "{[(" else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:300]) if detail else ""))
    if not ok:
        failures.append(name)


def tool(token, name, args, agent="worker-d", attempt=1):
    st, res = api(
        "POST",
        f"/v1/tools/{name}/invoke",
        {"tool_call_id": f"w_{name}", "agent": agent, "task": CODE,
         "attempt": attempt, "arguments": args},
        token,
    )
    if st != 200:
        raise RuntimeError(f"{name}: HTTP {st} {res}")
    body = res or {}
    if body.get("status") == "error":
        raise RuntimeError(f"{name}: {body.get('error')}")
    return body.get("output") or {}


BUILD_BAT = f"""@echo off
rem probe {CODE}
if not exist dist mkdir dist
set CSC=C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe
"%CSC%" /nologo /out:dist\\hello.exe hello.cs
if errorlevel 1 exit /b 1
echo built by windows-ltsc > dist\\build-info.txt
dir dist
"""

HELLO_CS = f"""using System;

class Program {{
    static void Main() {{
        Console.WriteLine("hello from the windows-ltsc profile");
        Console.WriteLine("probe: {CODE}");
    }}
}}
"""


def free_agent(agent: str = "worker-d") -> None:
    st, rows = api("GET", f"/v1/assignments?state=all&agent={agent}")
    for row in rows or []:
        if row.get("state") in ("offered", "active", "claimed", "submitted"):
            api("POST", f"/v1/assignments/{row['id']}/release")


free_agent()
print(f"task {CODE}")
st, created = api(
    "POST",
    "/v1/tasks",
    {
        "code": CODE,
        "title": "Windows probe: csc build inside the windows-ltsc node",
        "workstream": "stream-infra",
        "kind": "infra",
        "work_order": {
            "write_surface": ["dist/**", "build.bat", "hello.cs"],
            "acceptance": [{"gate": "windows-build", "required": True, "level": "L1"}],
            "execution": {"profile": "windows-ltsc", "collect": ["dist/**"]},
        },
    },
)
check("create task with execution.profile=windows-ltsc", st in (200, 201), created)
if st not in (200, 201):
    print("FAILURES:", failures)
    sys.exit(1)

st, _ = api("POST", f"/v1/tasks/{CODE}/dispatch", {"agent": "worker-d"})
check("dispatch", st in (200, 201), st)

assignment = None
for _ in range(10):
    st, assignment = api("GET", "/v1/assignments/next?agent=worker-d")
    if assignment:
        break
    time.sleep(2)
check("offer available", bool(assignment))
aid = (assignment or {}).get("assignment_id")
st, claim = api("POST", f"/v1/assignments/{aid}/claim")
check("claim", st == 200, st)
tok = claim["token"]["value"]

tool(tok, "workspace_write_file", {"path": "build.bat", "content": BUILD_BAT})
tool(tok, "workspace_write_file", {"path": "hello.cs", "content": HELLO_CS})
print("     wrote build.bat and hello.cs")

print("     running `windows-build` gate (cmd /c build.bat in the Windows VM) ...")
t0 = time.time()
gate = tool(tok, "gate_run", {"gate": "windows-build"})
elapsed = int(time.time() - t0)

check("windows-build exit=0", gate.get("exit") == 0,
      {k: gate.get(k) for k in ("exit", "stderr", "stdout")})
check("ran under windows-ltsc", gate.get("profile") == "windows-ltsc", gate.get("profile"))
check("reports the windows domain", bool(gate.get("vm")), gate.get("vm"))
arts = gate.get("artifacts") or []
exes = [a for a in arts if str(a.get("path", "")).lower().endswith(".exe")]
check("hello.exe collected", bool(exes), [a.get("path") for a in arts])
check("exe hash present", all(a.get("sha256") and len(a["sha256"]) == 64 for a in exes), exes[:1])
print(f"     gate took {elapsed}s, artifacts: {[a.get('path') for a in arts]}")

tool(tok, "workspace_commit", {"message": f"{CODE}: windows probe"})
submit = tool(tok, "workspace_submit", {})
receipt = tool(
    tok,
    "receipt_submit",
    {
        "receipt_id": f"R-{CODE.replace('-', '')}",
        "candidate_ref": submit["candidate_ref"],
        "base_commit": submit["base_commit"],
        "gates": [{"gate": "windows-build", "exit": gate["exit"],
                   "evidence_id": gate["evidence_id"]}],
    },
)
rid = ((receipt or {}).get("body") or {}).get("receipt_id") or receipt.get("receipt_id")
check("receipt accepted", bool(rid), receipt)

st, review = api("POST", f"/v1/tasks/{CODE}/reviews",
                 {"decision": "accept", "receipt_id": rid})
check("review accept", st == 200, st)

st, merge = api("POST", "/v1/merges", {"task": CODE})
merge_id = (merge or {}).get("id") or (merge or {}).get("merge_id")
st, processed = api("POST", f"/v1/merges/{merge_id}/process")
check("merge processed", st == 200, f"{st} {processed}")

deadline = time.time() + 120
state = ""
while time.time() < deadline:
    st, task = api("GET", f"/v1/tasks/{CODE}")
    state = (task or {}).get("state", "")
    if state == "merged":
        break
    time.sleep(3)
check("task merged", state == "merged", state)

st, events = api("GET", "/v1/events?limit=200")
produced = [e for e in (events or []) if e.get("type") == "artifact.produced"]
check("artifact.produced events in the chain", len(produced) >= 1, len(produced))

st, chain = api("GET", "/v1/audit/chain?limit=5000")
check("event chain ok", chain.get("ok") is True, chain.get("broken_links"))

print()
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
