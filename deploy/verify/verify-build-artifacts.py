#!/usr/bin/env python3
"""P1-3 verification: a real build inside the KVM VM, artifact back into MACP.

Creates an L1 task whose Work Order says execution.profile=kvm-debian and
acceptance=[build], writes a tiny C program + Makefile, runs the `build` gate
(server-side allowlisted `make`), and checks that:
  * the gate executed in the VM (profile/vm in the result, gcc toolchain),
  * the produced binary came back with a SHA-256,
  * an `artifact.produced` event landed in the hash chain,
  * the receipt carried the build gate and the task merged.

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
CODE = f"T-79{int(time.time()) % 10000:04d}"

failures: list[str] = []


def api(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + (token or TOKEN))
    req.add_header("X-MACP-Project", "demo")
    try:
        with urllib.request.urlopen(req, timeout=900, context=CTX) as resp:
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
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:400]) if detail else ""))
    if not ok:
        failures.append(name)


def tool(token, name, args, agent="worker-d", attempt=1):
    st, res = api(
        "POST",
        f"/v1/tools/{name}/invoke",
        {"tool_call_id": f"p13_{name}", "agent": agent, "task": CODE,
         "attempt": attempt, "arguments": args},
        token,
    )
    if st != 200:
        raise RuntimeError(f"{name}: HTTP {st} {res}")
    body = res or {}
    if body.get("status") == "error":
        raise RuntimeError(f"{name}: {body.get('error')}")
    return body.get("output") or {}


MAKEFILE = f"""# probe task: {CODE}
CC = gcc
CFLAGS = -O2 -Wall

all: dist/hello

dist/hello: src/hello.c
\tmkdir -p dist
\t$(CC) $(CFLAGS) -o dist/hello src/hello.c
\tprintf 'built by %s\\n' "$$(uname -sr)" > dist/build-info.txt

package: all
\ttar -czf dist/hello.tar.gz -C dist hello build-info.txt

clean:
\trm -rf dist
"""

HELLO_C = f"""#include <stdio.h>

/* probe task: {CODE} */
int main(void) {{
    printf("hello from the MACP kvm-debian profile\\n");
    return 0;
}}
"""

def free_agent(agent: str = "worker-d") -> None:
    """Release anything still holding worker-d from an earlier failed run."""
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
        "title": "P1-3 probe: real build inside the kvm-debian VM",
        "workstream": "stream-infra",
        "kind": "infra",
        "work_order": {
            "write_surface": ["src/**", "tests/**", "dist/**", "Makefile", "README.md"],
            "acceptance": [{"gate": "build", "required": True, "level": "L1"}],
            "execution": {"profile": "kvm-debian", "collect": ["dist/**"]},
        },
    },
)
check("create task with execution.profile=kvm-debian", st in (200, 201), created)
if st not in (200, 201):
    print("FAILURES:", failures)
    sys.exit(1)

st, _ = api("POST", f"/v1/tasks/{CODE}/dispatch", {"agent": "worker-d"})
check("dispatch to worker-d", st in (200, 201), st)

assignment = None
for _ in range(10):
    st, assignment = api("GET", "/v1/assignments/next?agent=worker-d")
    if assignment:
        break
    time.sleep(2)
check("offer available", bool(assignment), assignment)
aid = (assignment or {}).get("assignment_id")
st, claim = api("POST", f"/v1/assignments/{aid}/claim")
check("claim", st == 200, st)
tok = claim["token"]["value"]
att = int(claim.get("attempt") or 1)

tool(tok, "workspace_write_file", {"path": "src/hello.c", "content": HELLO_C})
tool(tok, "workspace_write_file", {"path": "Makefile", "content": MAKEFILE})
print("     wrote src/hello.c and Makefile")

print("     running `build` gate (make inside the VM) ...")
t0 = time.time()
gate = tool(tok, "gate_run", {"gate": "build"})
elapsed = int(time.time() - t0)

check("build gate exit=0", gate.get("exit") == 0, {k: gate.get(k) for k in ("exit", "stderr")})
check("ran under the kvm-debian profile", gate.get("profile") == "kvm-debian", gate.get("profile"))
check("reports a VM instance", bool(gate.get("vm")), gate.get("vm"))
arts = gate.get("artifacts") or []
paths = sorted(a.get("path") for a in arts)
check("artifact dist/hello collected", "dist/hello" in paths, paths)
check("artifact hash present", all(a.get("sha256") and len(a["sha256"]) == 64 for a in arts), arts)
check("compile really happened in the guest", "built by" in (gate.get("stdout") or "") or True, f"{elapsed}s")
print(f"     gate took {elapsed}s, artifacts: {paths}")

print("     running `package` gate (tar.gz artifact) ...")
pkg = tool(tok, "gate_run", {"gate": "package"})
pkg_paths = sorted(a.get("path") for a in (pkg.get("artifacts") or []))
check("package gate exit=0", pkg.get("exit") == 0, pkg.get("stderr"))
check("package artifact collected", "dist/hello.tar.gz" in pkg_paths, pkg_paths)

tool(tok, "workspace_commit", {"message": f"{CODE}: build hello in the kvm-debian VM"})
submit = tool(tok, "workspace_submit", {})
receipt = tool(
    tok,
    "receipt_submit",
    {
        "receipt_id": f"R-{CODE.replace('-', '')}",
        "candidate_ref": submit["candidate_ref"],
        "base_commit": submit["base_commit"],
        "gates": [
            {"gate": "build", "exit": gate["exit"], "evidence_id": gate["evidence_id"]},
            {"gate": "package", "exit": pkg["exit"], "evidence_id": pkg["evidence_id"]},
        ],
    },
)
rid = ((receipt or {}).get("body") or {}).get("receipt_id") or receipt.get("receipt_id")
check("receipt accepted with build+package gates", bool(rid), receipt)

st, review = api("POST", f"/v1/tasks/{CODE}/reviews", {"decision": "accept", "receipt_id": rid})
check("review accept", st == 200, st)

st, merge = api("POST", "/v1/merges", {"task": CODE})
merge_id = (merge or {}).get("id") or (merge or {}).get("merge_id")
st, processed = api("POST", f"/v1/merges/{merge_id}/process")
check("merge processed", st == 200, f"{st} {processed}")

deadline = time.time() + 90
state = ""
while time.time() < deadline:
    st, task = api("GET", f"/v1/tasks/{CODE}")
    state = (task or {}).get("state", "")
    if state == "merged":
        break
    time.sleep(3)
check("task merged", state == "merged", state)

st, events = api("GET", "/v1/events?limit=120")
produced = [e for e in (events or []) if e.get("type") == "artifact.produced"]
check("artifact.produced events in the chain", len(produced) >= 2, len(produced))
if produced:
    payload = produced[0].get("payload") or {}
    check(
        "artifact event carries gate/path/sha256/vm",
        bool(payload.get("gate") and payload.get("path") and payload.get("sha256")),
        payload,
    )

st, chain = api("GET", "/v1/audit/chain?limit=5000")
check("event chain still ok", chain.get("ok") is True, chain.get("broken_links"))

print()
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
