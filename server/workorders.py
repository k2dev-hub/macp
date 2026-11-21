"""Work Order defaults, linter and write-surface helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from typing import Any

from .canonical import canonical_json

SEED_APP = "def add(a, b):\n    return a + b\n"
SEED_APP_DIGEST = "sha256:" + hashlib.sha256(SEED_APP.encode("utf-8")).hexdigest()
DEFAULT_WRITE_SURFACE = ["solution.py", "src/**", "tests/**", "README.md"]
REQUIRED_STOP = {
    "anchor-missing",
    "write-surface-exceeded",
    "budget-80",
    "ambiguous-requirement",
    "upstream-conflict",
}
REQUIRED_FORBIDDEN = {"push", "publish", "secret.read"}
LEVELS = {"L1": 1, "L2": 2, "L3": 3}
LEVEL_NAMES = {1: "L1", 2: "L2", 3: "L3"}
# Shell targets a Work Order may allow for shell_run.  Kept in sync with
# PLATFORM_PROFILES in tool_registry.py.
KNOWN_PLATFORMS = {"sandbox", "linux", "android", "windows"}
# Platform-specific gates must run in the profile that owns that toolchain, so a
# build can never be verified in a different environment than it was produced in.
GATE_PROFILES = {
    "android-build": "android-debian",
    "android-test": "android-debian",
    "windows-build": "windows-ltsc",
    "build": "kvm-debian",
    "package": "kvm-debian",
}
LEVEL_GATES = {
    1: [],
    2: ["unit", "integration"],
    3: ["unit", "integration", "security"],
}
HIGH_RISK_PREFIXES = (
    "server/auth",
    "deploy/",
    ".github/",
    "migrations/",
    "server/db.py",
)
HIGH_RISK_SUFFIXES = (".sql", "Dockerfile", "docker-compose.yml", ".pem", ".key")
MEDIUM_RISK_PREFIXES = (
    "server/",
    "web/src/",
    "shared/",
    "infra/",
    "src/core/",
    "src/session.py",
    "tests/",
)


def surface_level(patterns: list[str] | None) -> int:
    """Classify a *declared* write surface, before any code exists.

    ``effective_level`` classifies the real diff, which is the right answer at
    receipt/merge time.  This answers the earlier question: may this caller even
    declare a write surface that reaches high-risk paths?  Globs are cut at the
    first ``*`` so ``deploy/**`` and ``deploy/*.sh`` both probe ``deploy/``.
    """

    level = 1
    for raw in patterns or []:
        probe = str(raw).strip().lstrip("./").split("*")[0]
        if not probe:
            continue
        if any(probe.startswith(prefix) for prefix in HIGH_RISK_PREFIXES) or any(
            probe.endswith(suffix) for suffix in HIGH_RISK_SUFFIXES
        ):
            return 3
        if any(probe.startswith(prefix) for prefix in MEDIUM_RISK_PREFIXES):
            level = max(level, 2)
    return level


def default_work_order(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "macp.workorder/v1",
        "task_id": task["code"],
        "attempt": 1,
        "title": task["title"],
        "workstream": task["workstream"],
        "priority": task["priority"],
        "kind": task["kind"],
        "dependencies": [],
        "write_surface": list(DEFAULT_WRITE_SURFACE),
        # Execution environment for gates/shell_run: "local" (bwrap runner) or
        # "kvm-debian" (throw-away VM on the KVM node).  Unknown values are
        # rejected at run time, never silently downgraded to local.
        "execution": {"profile": "local"},
        "read_only_refs": ["README.md", "src/app.py"],
        "anchors": [
            {
                "id": "A1",
                "ref": "src/app.py:1-2",
                "digest": SEED_APP_DIGEST,
                "note": "seed app module",
            }
        ],
        "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
        "completion_contract": {
            "entrypoints": ["solution.py"],
            "consumers": ["solution.py"],
            "observable": "solution.py compiles and exposes the requested function",
        },
        "stop_conditions": sorted(REQUIRED_STOP),
        "forbidden": ["push", "publish", "secret.read", "edit:.git/**"],
        "batch_budget": {
            "max_lines": 900,
            "max_files": 120,
            "max_minutes": 240,
            "max_tokens": 400000,
            "max_cost": 2.0,
        },
        "model_policy": {"preference": "default"},
        "report_format": "macp.receipt/v1",
    }


def lint_work_order(wo: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []

    def err(rule: str, path: str, message: str) -> None:
        errors.append({"rule": rule, "path": path, "message": message})

    if wo.get("schema") != "macp.workorder/v1":
        err("R01-schema", "/schema", "must be macp.workorder/v1")
    for key in ("task_id", "title", "workstream", "priority", "kind"):
        if not wo.get(key):
            err("R01-required", f"/{key}", "required")

    globs = wo.get("write_surface")
    if not isinstance(globs, list) or not globs:
        err("R08-write-surface", "/write_surface", "must be a non-empty array")
    else:
        for i, glob in enumerate(globs):
            ok, reason = valid_glob(glob)
            if not ok:
                err("R08-write-surface", f"/write_surface/{i}", reason or "invalid glob")

    acceptance = wo.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        err("R06-acceptance", "/acceptance", "must contain at least one gate")
    else:
        if not any(item.get("required") for item in acceptance):
            err("R06-acceptance", "/acceptance", "at least one gate must be required")
        for i, item in enumerate(acceptance):
            gate = item.get("gate")
            if not isinstance(gate, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", gate):
                err("R06-acceptance", f"/acceptance/{i}/gate", "invalid gate")

    # execution.platforms: which OS shells shell_run may target.  Omitted means
    # "only the platform of execution.profile", which is the safe default; listing
    # more is an explicit decision by the Work Order author (cross-platform checks).
    execution = wo.get("execution") or {}
    platforms = execution.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            err("R13-execution", "/execution/platforms", "must be a non-empty array")
        else:
            for i, item in enumerate(platforms):
                if item not in KNOWN_PLATFORMS:
                    err(
                        "R13-execution",
                        f"/execution/platforms/{i}",
                        "must be one of " + ", ".join(sorted(KNOWN_PLATFORMS)),
                    )
    if not isinstance(execution.get("profile", "local"), str):
        err("R13-execution", "/execution/profile", "must be a string")

    # R14: a platform-specific gate may only be accepted in the profile that can
    # actually run it.  shell_run's platform is a *shell* dimension; gates are
    # pinned to the build environment so "built here, verified there" is
    # impossible.
    profile = execution.get("profile") or "local"
    for i, item in enumerate(wo.get("acceptance") or []):
        gate = item.get("gate")
        required_profile = GATE_PROFILES.get(gate)
        if required_profile and profile != required_profile:
            err(
                "R14-gate-profile",
                f"/acceptance/{i}/gate",
                f"{gate} requires execution.profile={required_profile} (got {profile})",
            )

    contract = wo.get("completion_contract") or {}
    if not contract.get("observable"):
        err("R07-completion", "/completion_contract/observable", "required")
    if wo.get("kind") in ("slice", "fix"):
        if not contract.get("entrypoints") or not contract.get("consumers"):
            err("R07-completion", "/completion_contract", "entrypoints/consumers required")

    stops = set(wo.get("stop_conditions") or [])
    missing = sorted(REQUIRED_STOP - stops)
    if missing:
        err("R12-stop", "/stop_conditions", "missing: " + ", ".join(missing))

    forbidden = set(wo.get("forbidden") or [])
    missing = sorted(REQUIRED_FORBIDDEN - forbidden)
    if missing:
        err("R11-forbidden", "/forbidden", "missing: " + ", ".join(missing))

    budget = wo.get("batch_budget") or {}
    for key, cap in {"max_lines": 5000, "max_files": 1000, "max_minutes": 1440}.items():
        value = budget.get(key)
        if not isinstance(value, int) or value < 1 or value > cap:
            err("R10-budget", f"/batch_budget/{key}", f"must be 1..{cap}")

    if wo.get("report_format") != "macp.receipt/v1":
        err("R13-report", "/report_format", "must be macp.receipt/v1")

    for i, anchor in enumerate(wo.get("anchors") or []):
        ref = anchor.get("ref", "")
        if not re.fullmatch(r"[^:]+:[0-9]+(?:-[0-9]+)?", ref):
            err("R05-anchor", f"/anchors/{i}/ref", "must be file:line or file:start-end")
        digest = anchor.get("digest", "")
        if digest and not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            err("R05-anchor", f"/anchors/{i}/digest", "invalid digest")

    return errors


def valid_glob(glob: str) -> tuple[bool, str | None]:
    if not isinstance(glob, str) or not glob:
        return False, "glob must be a non-empty string"
    if glob.startswith("/") or "\\" in glob or ".." in glob.split("/"):
        return False, "absolute/parent paths are not allowed"
    if glob == "**" or glob.endswith("/"):
        return False, "root ** and trailing slash are not allowed"
    if glob.endswith("/**"):
        return True, None
    name = glob.rsplit("/", 1)[-1]
    if name.startswith("*.") and name.count("*") == 1:
        return True, None
    if "*" in glob or "?" in glob or "[" in glob or "{" in glob:
        return False, "only exact, dir/**, dir/*.ext are supported"
    return True, None


def path_allowed(path: str, globs: list[str]) -> bool:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or ".." in path.split("/") or ".git" in path.split("/"):
        return False
    for glob in globs or []:
        if glob.endswith("/**"):
            prefix = glob[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        elif glob.rsplit("/", 1)[-1].startswith("*."):
            ext = glob.rsplit("*.", 1)[1]
            prefix = glob.rsplit("/", 1)[0] if "/" in glob else ""
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1 :] if prefix else path
            if "/" not in rest and rest.endswith("." + ext):
                return True
        elif path == glob:
            return True
    return False


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in paths:
                paths.append(path)
    return paths


def declared_level(wo: dict[str, Any]) -> int:
    levels = [
        LEVELS.get(item.get("level", "L1"), 1)
        for item in (wo.get("acceptance") or [])
        if item.get("gate")
    ]
    profile = (wo.get("gate_profile") or {}).get("level")
    if profile in LEVELS:
        levels.append(LEVELS[profile])
    return max(levels or [1])


def classify_change(paths: list[str], diff_lines: int = 0) -> int:
    level = 1
    for raw in paths:
        path = raw.replace("\\", "/")
        if any(path.startswith(prefix) for prefix in HIGH_RISK_PREFIXES) or any(
            path.endswith(suffix) for suffix in HIGH_RISK_SUFFIXES
        ):
            level = max(level, 3)
        elif any(path.startswith(prefix) for prefix in MEDIUM_RISK_PREFIXES):
            level = max(level, 2)
    if diff_lines > 400:
        level = max(level, 3)
    elif diff_lines > 150:
        level = max(level, 2)
    return level


def effective_level(wo: dict[str, Any], paths: list[str] | None = None, diff_lines: int = 0) -> int:
    return max(declared_level(wo), classify_change(paths or [], diff_lines))


def required_gates(
    wo: dict[str, Any], paths: list[str] | None = None, diff_lines: int = 0
) -> list[str]:
    gates = [
        item["gate"]
        for item in (wo.get("acceptance") or [])
        if item.get("required") and item.get("gate")
    ]
    level = effective_level(wo, paths, diff_lines)
    for gate in LEVEL_GATES.get(level, []):
        if gate not in gates:
            gates.append(gate)
    return gates


def gate_exit(gate: dict) -> int | None:
    """Exit code of a receipt gate entry, tolerating the shapes agents emit.

    Agents write ``exit``, ``exit_code``, or a status string.  Reading only
    ``exit`` made a receipt written as {"gate": "compile", "status": "passed"}
    look like a failing gate, and the agent retried until it blocked the task.
    """

    value = gate.get("exit", gate.get("exit_code"))
    if value is None:
        status = str(gate.get("status") or "").strip().lower()
        if status in ("pass", "passed", "ok", "success", "succeeded"):
            return 0
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def body_hash(wo: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(wo)).hexdigest()
