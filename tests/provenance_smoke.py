#!/usr/bin/env python3
"""Provenance logic checks that need no database.

Run: python3 tests/provenance_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.provenance import (  # noqa: E402
    collect_dependencies,
    fingerprint_of,
    payload_of,
    verify_record,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' | ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def base_row(**overrides) -> dict:
    row = {
        "build_id": "B-20261116-ABC123",
        "project_code": "demo",
        "task_code": "T-1",
        "attempt": 1,
        "gate": "build",
        "platform": "linux",
        "profile": "kvm-debian",
        "vm": "macp-job-1",
        "artifact_name": "hello",
        "artifact_sha256": "a" * 64,
        "artifact_size": 1234,
        "artifact_path": ".evidence/artifacts/build/hello",
        "source_commit": "deadbeef",
        "source_ref": "HEAD",
        "build_command": ["make"],
        "build_env": {"profile": "kvm-debian", "executor": "virtual-machine"},
        "dependencies": [{"name": "requests", "version": "2.32.0", "source": "requirements.txt"}],
        "dependency_count": 1,
        "builder": "worker-e",
        "fingerprint": "",
        "signature": None,
        "created_at_iso": "2026-11-16T04:00:00+00:00",
    }
    row.update(overrides)
    row["fingerprint"] = fingerprint_of(payload_of(row))
    return row


def main() -> int:
    row = base_row()
    check("fingerprint is a sha256", len(row["fingerprint"]) == 64)

    same = fingerprint_of(payload_of(base_row()))
    check("fingerprint is stable", same == row["fingerprint"])

    tampered = base_row(artifact_sha256="b" * 64)
    check("changed artifact changes the fingerprint", tampered["fingerprint"] != row["fingerprint"])

    tampered_cmd = base_row(build_command=["make", "all"])
    check("changed command changes the fingerprint", tampered_cmd["fingerprint"] != row["fingerprint"])

    # 篡改检测：行内容与指纹不一致时，验证必须失败
    broken = base_row()
    broken["build_command"] = ["make", "evil"]
    result = verify_record(broken)
    check("tampered row fails verification", result["verified"] is False)
    check(
        "fingerprint check names the failure",
        any(c["check"] == "fingerprint" and not c["ok"] for c in result["checks"]),
    )

    # 产物被替换时，验证必须失败
    tmp = Path("/tmp/macp-prov-test")
    tmp.mkdir(parents=True, exist_ok=True)
    artifact = tmp / ".evidence" / "artifacts" / "build"
    artifact.mkdir(parents=True, exist_ok=True)
    payload = b"original artifact"
    (artifact / "hello").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    good = base_row(artifact_sha256=digest)
    ok_result = verify_record(good, artifact_root=tmp)
    check(
        "artifact check passes for the original bytes",
        any(c["check"] == "artifact" and c["ok"] for c in ok_result["checks"]),
    )
    (artifact / "hello").write_bytes(b"replaced artifact")
    bad_result = verify_record(good, artifact_root=tmp)
    check(
        "replaced artifact fails the artifact check",
        any(c["check"] == "artifact" and not c["ok"] for c in bad_result["checks"]),
    )

    # 依赖采集：从工作区文件里读，不联网
    work = Path("/tmp/macp-prov-deps")
    work.mkdir(parents=True, exist_ok=True)
    (work / "requirements.txt").write_text("requests==2.32.0\nflask>=3.0\n", encoding="utf-8")
    (work / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.2.0"}, "devDependencies": {"vite": "^5.0.0"}}),
        encoding="utf-8",
    )
    deps, count = collect_dependencies(work)
    names = {d["name"] for d in deps}
    check("dependencies include python packages", {"requests", "flask"} <= names, str(sorted(names)))
    check("dependencies include node packages", {"react", "vite"} <= names)
    check("dependency count matches the list", count == len(deps))

    print()
    if FAILURES:
        print("FAILURES:", FAILURES)
        return 1
    print("all provenance checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
