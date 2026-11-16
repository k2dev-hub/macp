"""Build provenance: the record a provenance page can verify.

A gate run that produces artifacts leaves a `build_provenance` row: what was
built, from which commit, with which command, environment and dependencies, by
whom, and when.  The row carries a fingerprint over a canonical payload and an
Ed25519 signature over that fingerprint, and an event joins the audit chain.

Verification recomputes three things and reports each one:

* the fingerprint, from the stored fields (a changed field breaks it);
* the signature, against the builder's public key;
* the artifact hash, re-read from disk (a replaced file breaks it).
"""

from __future__ import annotations

import json
import os
import platform
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_hex


def _sign(agent: str, data: str) -> str | None:
    """Sign through the signer service when it is available.

    Imported lazily so the record/fingerprint logic can be tested without the
    cryptography package present (the API image always ships it).
    """

    try:
        from .signing import SigningUnavailable, sign
    except Exception:
        return None
    try:
        return sign(agent, data)
    except SigningUnavailable:
        return None
    except Exception:
        return None


def _verify_signature(agent: str, data: str, signature: str) -> bool:
    try:
        from .signing import verify as verify_signature
    except Exception:
        return False
    try:
        return verify_signature(agent, data, signature)
    except Exception:
        return False

MAX_DEPENDENCIES = 400


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_build_id() -> str:
    """Human-quotable id: B-20261116-1A2B3C.

    Short enough to print on a slide or a sticker, random enough not to collide
    with a neighbouring build in the same second.
    """

    stamp = _utcnow().strftime("%Y%m%d")
    return f"B-{stamp}-{uuid.uuid4().hex[:6].upper()}"


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #


def _env_scalar(value: Any, limit: int = 120) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def collect_environment(profile: str | None, vm: str | None) -> dict[str, Any]:
    """Environment of the process that ran the build.

    For VM profiles the container is only the launcher, so the record says so
    explicitly instead of pretending the host kernel is the build kernel.
    """

    env: dict[str, Any] = {
        "runner_os": _env_scalar(platform.system()),
        "runner_kernel": _env_scalar(platform.release()),
        "runner_arch": _env_scalar(platform.machine()),
        "runner_python": _env_scalar(platform.python_version()),
        "profile": profile or "",
    }
    if vm:
        env["executor"] = "virtual-machine"
        env["vm"] = _env_scalar(vm, 160)
    elif (profile or "").startswith("kvm"):
        env["executor"] = "virtual-machine"
    elif profile in ("sandbox", "local"):
        env["executor"] = "sandbox"
    else:
        env["executor"] = _env_scalar(profile or "unknown")
    image = os.getenv("MACP_BUILD", "").strip()
    if image:
        env["image"] = _env_scalar(image)
    return env


_DEP_PATTERNS = (
    ("requirements.txt", re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(?:==|>=|~=)\s*([^\s;#]+)", re.M)),
    ("go.mod", re.compile(r"^\s*require\s+(\S+)\s+(v\S+)", re.M)),
    ("build.gradle", re.compile(r"""\b(?:implementation|api|compileOnly|runtimeOnly)\s*['"]([^:'"]+):([^:'"]+)(?::([^'"]+))?['"]""")),
)
_SKIP_DIRS = {".git", "node_modules", "build", "dist", ".gradle", ".evidence", "__pycache__"}


def collect_dependencies(base: Path | None) -> tuple[list[dict[str, str]], int]:
    """Best-effort SBOM from the files the build actually consumes.

    This is deliberately a file scan rather than a package-manager query: it
    works offline, inside every execution profile, and records what the task
    declared rather than what happened to be installed on the runner.
    """

    if base is None or not base.exists():
        return [], 0
    found: dict[tuple[str, str], dict[str, str]] = {}

    def add(name: str, version: str, source: str) -> None:
        key = (name.strip().lower(), (version or "").strip())
        if not key[0]:
            return
        found.setdefault(key, {"name": key[0], "version": key[1], "source": source})

    for path in sorted(base.rglob("*")):
        if len(found) >= MAX_DEPENDENCIES:
            break
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        name = path.name
        if name == "package.json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for section in ("dependencies", "devDependencies"):
                for dep, version in (data.get(section) or {}).items():
                    add(dep, str(version), "package.json")
            continue
        if name == "AndroidManifest.xml":
            continue
        for pattern_name, pattern in _DEP_PATTERNS:
            if name != pattern_name and not (pattern_name == "build.gradle" and name.startswith("build.gradle")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in pattern.finditer(text):
                groups = [g for g in match.groups() if g]
                if pattern_name == "build.gradle" and len(groups) >= 2:
                    add(f"{groups[0]}:{groups[1]}", groups[2] if len(groups) > 2 and groups[2] else "", "build.gradle")
                elif len(groups) >= 2:
                    add(groups[0], groups[1], pattern_name)
            if pattern_name != "build.gradle":
                break

    deps = sorted(found.values(), key=lambda item: (item["name"], item["version"]))[:MAX_DEPENDENCIES]
    return deps, len(deps)


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #


def _as_json(value, default):
    """asyncpg returns jsonb as text; decode so verification sees structures."""

    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _as_list(value) -> list:
    value = _as_json(value, [])
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    value = _as_json(value, {})
    return value if isinstance(value, dict) else {}


def payload_of(row: dict[str, Any]) -> dict[str, Any]:
    """The part of a record that is signed and re-checked.

    Volatile bookkeeping (ids, the event pointer) is excluded so a record can be
    re-verified without the database telling it what to think.
    """

    return {
        "build_id": row["build_id"],
        "project": row.get("project_code", ""),
        "task": row["task_code"],
        "attempt": int(row.get("attempt") or 1),
        "gate": row["gate"],
        "platform": row.get("platform") or "",
        "profile": row.get("profile") or "",
        "vm": row.get("vm") or "",
        "artifact": {
            "name": row.get("artifact_name") or "",
            "sha256": row.get("artifact_sha256") or "",
            "size": int(row.get("artifact_size") or 0),
        },
        "source": {
            "commit": row.get("source_commit") or "",
            "ref": row.get("source_ref") or "",
        },
        "command": _as_list(row.get("build_command")),
        "environment": _as_dict(row.get("build_env")),
        "dependencies": {
            "count": int(row.get("dependency_count") or 0),
            "items": _as_list(row.get("dependencies")),
        },
        "builder": row.get("builder") or "",
        "created_at": row["created_at_iso"],
    }


def fingerprint_of(payload: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(payload))


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


async def record_build(
    conn,
    *,
    project,
    task_row,
    agent: str,
    gate: str,
    result: dict[str, Any],
    artifacts: Iterable[dict[str, Any]],
    base: Path | None,
    source_commit: str | None,
    source_ref: str | None,
    command: list[str] | None,
    evidence_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """Record one provenance row per produced artifact.

    Returns the summary the tool call reports back, so an agent can quote the
    build id without another round trip.
    """

    artifacts = [a for a in artifacts if a.get("sha256")]
    if not artifacts:
        return []

    dependencies, dependency_count = collect_dependencies(base)
    environment = collect_environment(result.get("profile"), result.get("vm"))
    created = _utcnow()
    recorded: list[dict[str, Any]] = []

    for artifact in artifacts:
        build_id = new_build_id()
        artifact_name = Path(str(artifact.get("path") or "artifact")).name or "artifact"
        row = {
            "build_id": build_id,
            "project_code": project.code,
            "task_code": task_row["code"],
            "attempt": int(task_row.get("attempt") or 1),
            "gate": gate,
            "platform": gate.replace("-build", "") if gate.endswith("-build") else gate,
            "profile": result.get("profile") or "",
            "vm": result.get("vm") or "",
            "artifact_name": artifact_name,
            "artifact_sha256": str(artifact.get("sha256")),
            "artifact_size": int(artifact.get("size") or 0),
            "source_commit": source_commit or "",
            "source_ref": source_ref or "",
            "build_command": command or [],
            "build_env": environment,
            "dependencies": dependencies,
            "dependency_count": dependency_count,
            "builder": agent,
            "created_at_iso": created.isoformat(timespec="seconds"),
        }
        payload = payload_of(row)
        fingerprint = fingerprint_of(payload)
        signature = _sign(agent, fingerprint)

        stored = await conn.fetchrow(
            """
            insert into build_provenance
              (project_id, build_id, task_id, task_code, attempt, gate, platform,
               profile, vm, artifact_name, artifact_sha256, artifact_size,
               artifact_path, source_commit, source_ref, build_command, build_env,
               dependencies, dependency_count, builder, fingerprint, signature,
               evidence_id, created_at)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                    $16::jsonb,$17::jsonb,$18::jsonb,$19,$20,$21,$22,$23,$24)
            on conflict (project_id, build_id) do update
              set fingerprint = excluded.fingerprint
            returning id
            """,
            project.id,
            build_id,
            task_row["id"],
            task_row["code"],
            int(task_row.get("attempt") or 1),
            gate,
            row["platform"],
            row["profile"],
            row["vm"],
            artifact_name,
            row["artifact_sha256"],
            row["artifact_size"],
            str(artifact.get("stored") or artifact.get("path") or ""),
            row["source_commit"],
            row["source_ref"],
            json.dumps(row["build_command"]),
            json.dumps(row["build_env"]),
            json.dumps(row["dependencies"]),
            dependency_count,
            agent,
            fingerprint,
            signature,
            evidence_id,
            created,
        )
        recorded.append(
            {
                "build_id": build_id,
                "row_id": str(stored["id"]),
                "project": project.code,
                "task": task_row["code"],
                "builder": agent,
                "created_at": row["created_at_iso"],
                "artifact": artifact_name,
                "sha256": row["artifact_sha256"],
                "size": row["artifact_size"],
                "fingerprint": fingerprint,
                "signed": bool(signature),
                "dependencies": dependency_count,
            }
        )

    return recorded


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_record(row: dict[str, Any], *, artifact_root: Path | None = None) -> dict[str, Any]:
    """Recompute what can be recomputed and say exactly what failed."""

    payload = payload_of(row)
    expected = fingerprint_of(payload)
    checks: list[dict[str, Any]] = []

    fingerprint_ok = expected == row.get("fingerprint")
    checks.append(
        {
            "check": "fingerprint",
            "ok": fingerprint_ok,
            "detail": "record matches its fingerprint" if fingerprint_ok else "record changed after recording",
        }
    )

    signature = row.get("signature")
    if signature:
        signature_ok = _verify_signature(row.get("builder") or "", row["fingerprint"], signature)
        checks.append(
            {
                "check": "signature",
                "ok": signature_ok,
                "detail": "signed by the builder key" if signature_ok else "signature does not verify",
            }
        )
    else:
        checks.append({"check": "signature", "ok": False, "detail": "record is unsigned"})

    artifact_path = row.get("artifact_path")
    actual = None
    if artifact_root is not None and artifact_path:
        candidate = artifact_root / artifact_path
        try:
            resolved = candidate.resolve()
            root = artifact_root.resolve()
            if str(resolved).startswith(str(root)):
                actual = _hash_file(resolved)
        except OSError:
            actual = None
    artifact_ok = bool(actual) and actual == row.get("artifact_sha256")
    checks.append(
        {
            "check": "artifact",
            "ok": artifact_ok,
            "detail": (
                "artifact hash matches the recorded one"
                if artifact_ok
                else ("artifact unavailable for hashing" if actual is None else "artifact file was replaced")
            ),
            "observed_sha256": actual,
        }
    )

    return {
        "verified": all(item["ok"] for item in checks),
        "checks": checks,
        "fingerprint": row.get("fingerprint"),
        "recomputed_fingerprint": expected,
    }
