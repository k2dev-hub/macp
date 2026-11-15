"""Isolated command runner for MACP gates and shell_run.

Runs in its own container with no external network, read-only root and
dropped capabilities.  Commands are confined to the workspace volume; when
bubblewrap is usable, each command also gets its own namespaces and a
read-only root with only the task worktree writable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

WORKSPACE = Path(os.getenv("MACP_WORKSPACE_ROOT", "/app/workspace")).resolve()
TOKEN_FILE = os.getenv("MACP_RUNNER_TOKEN_FILE", "/run/secrets/runner_token")
USE_BWRAP = os.getenv("MACP_RUNNER_BWRAP", "1") == "1"
# /proc/version, /proc/cpuinfo and /proc/meminfo in a container describe the HOST
# kernel and hardware, which is both a fingerprint and misleading (meminfo reports
# host RAM, not the cgroup limit).  We generate sandbox-accurate replacements here
# and bind them over the real files inside bwrap.
PROC_DIR = Path(os.getenv("MACP_RUNNER_PROC_DIR", "/tmp/macp-proc"))
app = FastAPI(title="MACP Runner", version="0.1.0")


def _cgroup_limits() -> tuple[int, int]:
    """(memory_bytes, cpu_count) as the container actually sees them."""

    memory = 0
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw not in ("max", ""):
                memory = int(raw)
                break
        except (OSError, ValueError):
            continue
    if memory <= 0 or memory > 1 << 46:
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            memory = 0

    quota, period = 0, 0
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if raw and raw[0] != "max":
            quota, period = int(raw[0]), int(raw[1])
    except (OSError, ValueError, IndexError):
        pass
    if quota and period:
        cpus = max(1, round(quota / period))
    else:
        cpus = os.cpu_count() or 1
    return memory, cpus


def ensure_sandbox_proc() -> None:
    try:
        memory, cpus = _cgroup_limits()
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        (PROC_DIR / "version").write_text(
            "Linux version 6.12.0-macp-sandbox (macp@platform) (gcc 14.2.0) "
            "#1 SMP PREEMPT_DYNAMIC MACP sandbox\n",
            encoding="utf-8",
        )
        cpu_lines = []
        for i in range(cpus):
            cpu_lines.append(
                f"processor\t: {i}\n"
                "vendor_id\t: MACP\n"
                "model name\t: MACP sandbox vCPU\n"
            )
        (PROC_DIR / "cpuinfo").write_text("\n".join(cpu_lines), encoding="utf-8")
        (PROC_DIR / "meminfo").write_text(
            f"MemTotal:       {memory // 1024} kB\n"
            "MemFree:        %d kB\n" % (memory // 4096),
            encoding="utf-8",
        )
    except OSError:
        # Never fail a job because the sandbox view could not be written.
        pass


# Materialise the sandbox /proc view once at import (cheap, idempotent).
ensure_sandbox_proc()


class RunRequest(BaseModel):
    argv: list[str]
    cwd: str = "."
    timeout: int = 30


def token() -> str:
    return Path(TOKEN_FILE).read_text(encoding="utf-8").strip()


def resolve_cwd(value: str) -> Path:
    target = (WORKSPACE / value).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise HTTPException(400, "cwd escapes workspace")
    return target


def sandboxed(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    if USE_BWRAP and shutil.which("bwrap"):
        command = [
            "bwrap",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        # Overlay the sandbox view of the host-derived procfs files.  This has to
        # happen AFTER --proc /proc (so the bind wins) but BEFORE --tmpfs /tmp,
        # because the sources live in /tmp (the only writable path in the
        # read-only runner image).
        for name in ("version", "cpuinfo", "meminfo"):
            candidate = PROC_DIR / name
            if candidate.is_file():
                command += ["--ro-bind", str(candidate), f"/proc/{name}"]
        command += [
            "--tmpfs",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "--",
            *argv,
        ]
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0 and "bwrap:" in (proc.stderr or ""):
            raise HTTPException(500, f"bubblewrap failed: {proc.stderr[:1000]}")
        return proc
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "bwrap": bool(shutil.which("bwrap")), "workspace": str(WORKSPACE)}


@app.post("/run")
async def run(
    body: RunRequest,
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "runner token required")
    import secrets

    if not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), token()
    ):
        raise HTTPException(401, "invalid runner token")
    if not body.argv:
        raise HTTPException(422, "argv is required")
    cwd = resolve_cwd(body.cwd)
    if not cwd.exists():
        raise HTTPException(400, "cwd does not exist")
    try:
        proc = sandboxed(body.argv, cwd, max(1, min(body.timeout, 300)))
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": "", "stderr": "timeout"}
    return {
        "exit": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }
