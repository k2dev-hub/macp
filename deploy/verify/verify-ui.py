#!/usr/bin/env python3
"""Check the six management endpoints the console depends on.

Run on the deployment host after deploying the new api image:

    python3 deploy/verify/verify-ui.py
    MACP_BASE=https://10.126.126.11:9443 python3 deploy/verify/verify-ui.py

Each check asserts the shape the console pages actually read, not just HTTP 200:
an empty list or a missing key would leave a page blank, which is the failure
mode that matters here.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443")
TOKEN_FILE = os.getenv("MACP_TOKEN_FILE", "deploy/secrets/write_token")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

FAILURES: list[str] = []


def call(path: str, token: str, method: str = "GET", body: dict | None = None):
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=payload, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("X-MACP-Project", os.getenv("MACP_PROJECT", "demo"))
    if payload:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30, context=CTX) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:200]
    except Exception as exc:  # network, timeout
        return 0, str(exc)[:200]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ((" | " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    if not os.path.exists(TOKEN_FILE):
        print(f"token file missing: {TOKEN_FILE}")
        return 1
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()

    status, body = call("/v1/ui/projects", token)
    rows = body if isinstance(body, list) else []
    check("项目列表可读", status == 200 and bool(rows),
          f"{status} 项目 {len(rows)} 个")
    check("项目带任务与 Agent 计数", bool(rows) and "tasks" in rows[0] and "agents" in rows[0])

    status, body = call("/v1/ui/artifacts?limit=10", token)
    data = body if isinstance(body, dict) else {}
    check("产物接口可读", status == 200 and "evidence" in data and "builds" in data, str(status))
    check("构建记录非空（本机做过构建）", bool(data.get("builds")),
          f"{len(data.get('builds') or [])} 条")
    evidence = data.get("evidence") or []
    downloadable = [item for item in evidence if item.get("downloadable")]
    check("证据列表带可下载标记", all("downloadable" in item for item in evidence),
          f"可下载 {len(downloadable)}/{len(evidence)}")

    status, body = call("/v1/ui/agents", token)
    data = body if isinstance(body, dict) else {}
    agents = data.get("agents") or []
    check("Agent 列表可读", status == 200 and bool(agents), f"{len(agents)} 个")
    check("Agent 带模型策略字段", all("model_policy" in a for a in agents))

    status, body = call("/v1/ui/workload", token)
    data = body if isinstance(body, dict) else {}
    check("分工数据可读", status == 200 and "agents" in data and "by_state" in data, str(status))
    check("分工带在办任务", "in_flight" in data)

    status, body = call("/v1/ui/chain", token)
    data = body if isinstance(body, dict) else {}
    fabric = data.get("fabric") or {}
    check("上链概览可读", status == 200 and "totals" in data and "fabric" in data, str(status))
    check("上链带区块高度或总数", (fabric.get("block_height") is not None)
          or (fabric.get("total") is not None), f"height={fabric.get('block_height')}")

    # 写路径：改名同一个名字，应该是幂等的成功
    if rows:
        code = rows[0]["code"]
        name = rows[0]["name"]
        status, body = call(f"/v1/ui/projects/{code}", token, "PATCH", {"name": name})
        check("项目改名可写", status == 200, str(status))

    # 写路径：把某个 agent 的能力原样写回，验证 PATCH 通道
    if agents:
        agent = agents[0]
        status, body = call(
            f"/v1/ui/agents/{agent['code']}", token, "PATCH",
            {"capabilities": agent.get("capabilities") or []},
        )
        check("Agent 配置可写", status == 200, str(status))

    print()
    if FAILURES:
        print("FAILURES:", FAILURES)
        return 1
    print("console management endpoints all reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
