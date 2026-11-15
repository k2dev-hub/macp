"""web_search tool smoke via SearXNG."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443").rstrip("/")
SSL_CONTEXT = ssl._create_unverified_context() if BASE.startswith("https://") else None
ADMIN = os.getenv("MACP_ADMIN_TOKEN", "").strip()
PASS = 0
FAIL = 0


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def reset_agent(agent):
    status, rows = call("GET", f"/v1/assignments?state=all&agent={agent}", None, ADMIN)
    if status == 200:
        for row in rows:
            if row.get("state") in ("offered", "claimed", "active", "submitted"):
                call("POST", f"/v1/assignments/{row['id']}/release", None, ADMIN)


def main() -> int:
    if not ADMIN:
        print("FAIL no admin token")
        return 2
    reset_agent("worker-smoke")
    code = f"T-2{int(time.time()) % 10**6:06d}"
    status, _ = call("POST", "/v1/tasks", {
        "code": code, "title": "web search smoke", "workstream": "stream-core", "priority": "P2",
    }, ADMIN)
    check("task created", status == 201, status)
    status, dispatched = call("POST", f"/v1/tasks/{code}/dispatch", {"agent": "worker-smoke"}, ADMIN)
    check("dispatched", status == 201, f"{status} {dispatched}")
    status, claim = call("POST", f"/v1/assignments/{dispatched['assignment_id']}/claim", None, ADMIN)
    token = claim["token"]["value"]
    check("token includes web:search", "web:search" in claim["token"]["scopes"], claim["token"]["scopes"])

    def tool(args, tok=token):
        return call("POST", "/v1/tools/web_search/invoke", {
            "agent": "worker-smoke", "task": code, "attempt": claim["attempt"], "arguments": args,
        }, tok)

    status, out = tool({"query": "SearXNG", "max_results": 3})
    output = (out or {}).get("output") or {}
    results = output.get("results") or []
    check("search returns results", status == 200 and out.get("status") == "ok" and len(results) > 0, out)
    check("max_results respected", len(results) <= 3, len(results))
    check("urls are public http(s)", all(r["url"].startswith(("http://", "https://")) for r in results), results)

    status, out = tool({"query": "linux", "max_results": 100})
    output = (out or {}).get("output") or {}
    check("max_results clamped", len(output.get("results") or []) <= 10, len(output.get("results") or []))

    status, out = tool({"query": ""})
    check("empty query rejected", status == 200 and out.get("status") == "error", out)

    status, _ = call("POST", "/v1/tools/web_search/invoke", {
        "agent": "worker-smoke", "task": code, "attempt": claim["attempt"], "arguments": {"query": "x"},
    })
    check("unauthenticated search rejected", status == 401, status)
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    call("POST", f"/v1/assignments/{dispatched['assignment_id']}/release", None, ADMIN)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
