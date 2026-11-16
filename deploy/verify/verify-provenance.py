#!/usr/bin/env python3
"""End-to-end check of build provenance.

Runs a real Linux build gate, then verifies what the record says:

1. the gate returns a build id and the record is queryable;
2. the record carries a fingerprint, a signature and the dependency list;
3. the public /verify page renders the record and says it is intact;
4. the QR endpoint renders;
5. replacing the artifact file flips the verdict to failed.

Run on the deployment host:  python3 deploy/verify/verify-provenance.py
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443")
TOKEN_FILE = os.getenv("MACP_TOKEN_FILE", "deploy/secrets/write_token")
TAMPER_CONTAINER = os.getenv("MACP_TAMPER_CONTAINER", "macp-api")
TAMPER_ROOT = os.getenv("MACP_TAMPER_ROOT", "/app/workspace/tasks")

FAILURES: list[str] = []
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' | ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def api(method: str, path: str, body=None, token: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=600, context=CTX) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def text(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:300]


def authed_text(url: str, token: str) -> tuple[int, str]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=60, context=CTX) as response:
            return response.status, response.read().decode("latin-1")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("latin-1")[:200]


def docker_exec(argv: list[str]) -> str:
    result = subprocess.run(
        ["docker", "exec", TAMPER_CONTAINER, *argv], check=False, capture_output=True, text=True
    )
    return (result.stdout or "").strip()


def main() -> int:
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    # Task codes are digits only (tasks_code_check).
    code = f"T-{int(time.time()) % 100000000}"

    status, body = api(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "provenance probe",
            "workstream": "stream-infra",
            "kind": "infra",
            "work_order": {
                "write_surface": ["src/**", "notes/**", "Makefile"],
                "acceptance": [{"gate": "build", "required": True, "level": "L1"}],
                "execution": {
                    "profile": "kvm-debian",
                    "platforms": ["linux", "sandbox"],
                    "collect": ["dist/**"],
                },
            },
        },
        token,
    )
    if status >= 400:
        check("task created", False, f"{status} {body}")
        return 1
    check("task created", True, code)

    status, agents = api("GET", "/v1/agents", token=token)
    idle = [a["code"] for a in (agents or []) if a.get("role") == "worker" and a.get("status") == "idle"]
    if not idle:
        check("idle worker available", False)
        return 1
    agent = idle[0]

    api("POST", f"/v1/tasks/{code}/dispatch", {"agent": agent}, token)
    status, offer = api("GET", f"/v1/assignments/next?agent={agent}", token=token)
    assignment = (offer or {}).get("assignment_id")
    status, claim = api("POST", f"/v1/assignments/{assignment}/claim", token=token)
    agent_token = ((claim or {}).get("token") or {}).get("value", "")
    if not agent_token:
        check("claimed", False, str(status))
        return 1
    check("claimed", True, agent)

    # 一个最小的 C 构建：make 产出 dist/hello
    makefile = "hello:\n\tmkdir -p dist\n\tprintf 'hello from macp\\n' > dist/hello\n"
    for path, content in (
        ("Makefile", makefile),
        ("src/main.c", "int main(void) { return 0; }\n"),
    ):
        status, _ = api(
            "POST",
            "/v1/tools/workspace_write_file/invoke",
            {
                "tool_call_id": f"w-{path}",
                "agent": agent,
                "task": code,
                "attempt": 1,
                "arguments": {"path": path, "content": content},
            },
            agent_token,
        )
        if status >= 400:
            check(f"wrote {path}", False, str(status))

    status, gate = api(
        "POST",
        "/v1/tools/gate_run/invoke",
        {
            "tool_call_id": "g-build",
            "agent": agent,
            "task": code,
            "attempt": 1,
            "arguments": {"gate": "build"},
        },
        agent_token,
    )
    body = gate if isinstance(gate, dict) else {}
    output = body.get("output") or {}
    check("build gate ran", body.get("status") == "ok", f"exit={output.get('exit')}")
    records = output.get("provenance") or []
    if not records:
        check("gate produced a provenance record", False, json.dumps(body)[:200])
        return 1
    build_id = records[0]["build_id"]
    check("gate produced a provenance record", True, build_id)

    status, detail = api("GET", f"/v1/provenance/{build_id}", token=token)
    if not isinstance(detail, dict):
        check("record queryable", False, f"{status} {str(detail)[:120]}")
        return 1
    record = detail.get("record") or {}
    verification = (detail or {}).get("verification") or {}
    check("record queryable", status == 200, str(status))
    check("fingerprint present", len(record.get("fingerprint") or "") == 64)
    check("record is signed", bool(record.get("signature")), (record.get("signature") or "")[:24])
    check("artifact hash present", len(record.get("artifact_sha256") or "") == 64)
    check("source commit recorded", bool(record.get("source_commit")), (record.get("source_commit") or "")[:12])
    check("build command recorded", record.get("build_command") == ["make"], str(record.get("build_command")))
    check("environment recorded", bool((record.get("build_env") or {}).get("profile")))
    check("verification passes", verification.get("verified") is True,
          str([c for c in verification.get("checks", []) if not c["ok"]]))

    # 标签包：记录 + 二维码 + 说明
    status, stamp = authed_text(f"{BASE}/v1/provenance/{build_id}/stamp.zip", token)
    check("stamp package downloads", status == 200 and len(stamp) > 500, str(status))
    check("gate archived the stamp files", bool(records[0].get("stamp_dir")), str(records[0].get("stamp_dir")))

    # 联盟链锚定（配置启用时）
    fabric_tx = record.get("fabric_tx_id")
    if os.getenv("MACP_EXPECT_FABRIC", "0") == "1":
        check("anchored on fabric", bool(fabric_tx), str(record.get("fabric_status")))
        if fabric_tx:
            check("fabric tx id looks like a transaction", len(fabric_tx) == 64, fabric_tx[:16])
            # 上链后紧接着查询，可能早于账本可见；重试几次
            status, chain = 0, None
            for _ in range(5):
                status, chain = api("GET", f"/v1/provenance/{build_id}/chain-anchor", token=token)
                if isinstance(chain, dict) and (chain.get("record") or {}).get("fingerprint"):
                    break
                time.sleep(2)
            remote = ((chain or {}).get("record") or {}) if isinstance(chain, dict) else {}
            check("chain returns the same fingerprint",
                  status == 200 and remote.get("fingerprint") == record.get("fingerprint"),
                  f"{status} local={str(record.get('fingerprint'))[:12]} chain={str(remote.get('fingerprint'))[:12]}")

    status, page = text(f"{BASE}/verify/{build_id}")
    check("public page renders", status == 200 and build_id in page, str(status))
    check("public page says intact", "未被篡改" in page)
    check("public page shows the artifact hash", (record.get("artifact_sha256") or "x")[:16] in page)

    status, qr = text(f"{BASE}/verify/{build_id}/qr.svg")
    check("qr renders", status == 200 and "<svg" in qr, str(status))

    # 篡改：替换产物文件后，验证必须失败
    artifact_path = record.get("artifact_path") or ""
    # The workspace keeps attempts as <code>/<n>; older layouts used attempt-<n>.
    stored = ""
    for attempt_dir in ("1", "attempt-1"):
        candidate = f"{TAMPER_ROOT}/{code}/{attempt_dir}/{artifact_path}"
        probe = docker_exec(["sh", "-c", f"test -f '{candidate}' && echo yes || true"])
        if probe.strip() == "yes":
            stored = candidate
            break
    if not stored:
        check("artifact file located for tampering", False, artifact_path)
        return 1
    original = docker_exec(["sh", "-c", f"base64 -w0 '{stored}'"])
    docker_exec(["sh", "-c", f"printf 'tampered' >> '{stored}'"])
    status, after = api("GET", f"/v1/provenance/{build_id}", token=token)
    after_verification = (after or {}).get("verification") or {}
    check("tampering is detected", after_verification.get("verified") is False)
    check(
        "the artifact check reports the replacement",
        any(c["check"] == "artifact" and not c["ok"] for c in after_verification.get("checks", [])),
    )
    status, page_after = text(f"{BASE}/verify/{build_id}")
    check("public page flips to failed", "验证失败" in page_after)

    # 还原产物，记录回到"完整"状态，便于反复运行
    if original:
        docker_exec(["sh", "-c", f"printf '%s' '{original}' | base64 -d > '{stored}'"])
        status, restored = api("GET", f"/v1/provenance/{build_id}", token=token)
        check(
            "record verifies again after restore",
            ((restored or {}).get("verification") or {}).get("verified") is True,
        )

    print()
    if FAILURES:
        print("FAILURES:", FAILURES)
        return 1
    print(f"all provenance checks passed (build {build_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
