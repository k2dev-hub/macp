#!/usr/bin/env python3
"""macp-kvm-node: one-shot VM job runner for the MACP platform.

Runs on a libvirt host (see [kvm] in config/macp.toml).  MACP posts a workspace tarball plus an
argv; the node boots a throw-away overlay of the golden image, runs the command
inside the guest over SSH, returns stdout/stderr and the requested artifacts,
then destroys the domain and the overlay.

    POST /v1/run   {"profile":"kvm-debian","job_id":"...","workspace_tar_b64":"...",
                    "argv":["python3","-m","unittest","discover","-s","tests"],
                    "cwd":".","timeout":300,"collect":["dist/**"]}
    GET  /health

Security posture: bearer token, argv allowlist, workspace size limit, one job at
a time (a single 15GiB host cannot run two Windows/Android class VMs), no host
directories mounted into the guest, no libvirt socket, keys never enter the VM.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NODE_TOKEN_FILE = os.getenv("MACP_KVM_NODE_TOKEN_FILE", "/etc/macp-kvm/node_token")
IMAGE_DIR = Path(os.getenv("MACP_KVM_IMAGE_DIR", "/var/lib/libvirt/images"))
GOLDEN_IMAGE = Path(
    os.getenv("MACP_KVM_GOLDEN_IMAGE", str(IMAGE_DIR / "macp/base/debian-build.qcow2"))
)
SSH_KEY = os.getenv("MACP_KVM_SSH_KEY", "/etc/macp-kvm/node_key")
SSH_USER = os.getenv("MACP_KVM_SSH_USER", "macp")
LIBVIRT_NET = os.getenv("MACP_KVM_NETWORK", "default")
MEMORY_MB = int(os.getenv("MACP_KVM_MEMORY_MB", "2048"))
VCPUS = int(os.getenv("MACP_KVM_VCPUS", "2"))
BOOT_TIMEOUT = int(os.getenv("MACP_KVM_BOOT_TIMEOUT", "180"))
MAX_WORKSPACE = int(os.getenv("MACP_KVM_MAX_WORKSPACE_BYTES", str(50 * 1024 * 1024)))
LOCK_FILE = Path("/var/lock/macp-kvm-node.lock")
LOG_DIR = Path("/var/log/macp-kvm-node")

# Only interpreters and build tools a gate needs.  Anything else is refused so a
# MACP job cannot use the node as a general-purpose shell.
ALLOWED_PROGRAMS = {
    "python3", "python", "sh", "bash", "gcc", "g++", "make", "git", "tar",
    "cat", "ls", "true", "env", "pytest", "node", "npm", "gradle", "gradlew",
    "javac", "java", "adb", "aapt2", "d8", "zipalign", "apksigner", "cmake",
    "ninja", "dotnet", "msbuild", "cmd", "cmd.exe", "powershell", "powershell.exe",
    "cl", "nmake", "build.bat",
}

# Each profile pins the golden image and its resource budget.  A job may only
# name a profile from this table (fail closed on anything else).
PROFILES: dict[str, dict] = {
    "kvm-debian": {
        "image": GOLDEN_IMAGE,
        "memory": MEMORY_MB,
        "vcpus": VCPUS,
        "disk": "6G",
    },
    "android-debian": {
        "image": Path(
            os.getenv(
                "MACP_KVM_ANDROID_IMAGE",
                str(IMAGE_DIR / "macp/base/android-build.qcow2"),
            )
        ),
        "memory": int(os.getenv("MACP_KVM_ANDROID_MEMORY_MB", "6144")),
        "vcpus": int(os.getenv("MACP_KVM_ANDROID_VCPUS", "4")),
        "disk": "16G",
        # Jobs arrive over a non-interactive ssh session, which does not source
        # /etc/profile.d/android.sh, so the toolchain env is set explicitly.
        "env": {
            "ANDROID_HOME": "/opt/android-sdk",
            "ANDROID_SDK_ROOT": "/opt/android-sdk",
            "GRADLE_USER_HOME": "/home/macp/.gradle",
            "PATH": "/opt/gradle/gradle-8.9/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/usr/local/bin:/usr/bin:/bin",
        },
    },
    "windows-ltsc": {
        # Long-lived VM, not an overlay: the Windows build node stays up and is
        # driven over SSH through cmd.exe.
        "kind": "static-host",
        "name": "windows-ltsc",
        "domain": os.getenv("MACP_KVM_WINDOWS_DOMAIN", "macp-win-build"),
        "user": os.getenv("MACP_KVM_WINDOWS_USER", "Administrator"),
        "workdir": os.getenv("MACP_KVM_WINDOWS_WORKDIR", r"C:\macp\work"),
        "memory": 6144,
        "vcpus": 4,
        "disk": "40G",
    },
}


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / "node.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def token() -> str:
    value = os.getenv("MACP_KVM_NODE_TOKEN", "").strip()
    if not value and Path(NODE_TOKEN_FILE).exists():
        value = Path(NODE_TOKEN_FILE).read_text(encoding="utf-8").strip()
    return value


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def domain_name(job_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id)[:48]
    return f"macp-job-{safe}-{uuid.uuid4().hex[:6]}"


def guest_ip(mac: str) -> str | None:
    """Find the guest's IPv4 from virsh's lease table.

    Rows look like:
      2026-11-15 00:54:26  52:54:00:5d:89:5a  ipv4  192.168.122.55/24  macp-builder  ...
    Column positions shift with the expiry timestamp, so match with a regex.
    """

    proc = run(["virsh", "net-dhcp-leases", LIBVIRT_NET], timeout=20)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        match = re.search(
            r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s+ipv4\s+(\d+\.\d+\.\d+\.\d+)/", line, re.I
        )
        if not match:
            continue
        if not mac or match.group(1).lower() == mac.lower():
            return match.group(2)
    return None


def ssh(ip: str, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return run(
        [
            "ssh", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=10",
            f"{SSH_USER}@{ip}", command,
        ],
        timeout=timeout,
    )


def wait_ssh(ip: str, deadline: float) -> bool:
    while time.time() < deadline:
        proc = ssh(ip, "true", timeout=15)
        if proc.returncode == 0:
            return True
        time.sleep(3)
    return False


def cleanup(name: str, overlay: Path) -> None:
    run(["virsh", "destroy", name], timeout=60)
    run(["virsh", "undefine", name, "--remove-all-storage"], timeout=60)
    try:
        overlay.unlink(missing_ok=True)
    except OSError:
        pass


def ssh_bytes(ip: str, command: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Like ssh(), but keeps stdout as bytes.

    Artifact transfer is a gzip stream; decoding it as text raises
    "'utf-8' codec can't decode byte 0x8b" (0x8b is the gzip magic).
    """

    return subprocess.run(
        [
            "ssh", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=10",
            f"{SSH_USER}@{ip}", command,
        ],
        capture_output=True,
        timeout=timeout,
    )


def collect_artifacts(ip: str, patterns: list[str]) -> list[dict]:
    if not patterns:
        return []
    safe = [p for p in patterns if not p.startswith("/") and ".." not in p]
    if not safe:
        return []
    # Turn each pattern into a find(1) -path expression.  GNU find matches '/'
    # with '*', so "dist/**" -> "./dist/*" covers every depth, and
    # "**/build/outputs/apk/**" -> "./*/build/outputs/apk/*" finds the APK
    # wherever the module lives.
    exprs = []
    for p in safe:
        pat = p.replace("**", "*").rstrip("/")
        if not pat:
            continue
        if not pat.startswith("."):
            pat = "./" + pat.lstrip("./")
        exprs.append(pat)
    if not exprs:
        return []
    find_clause = " -o ".join(
        "-path " + shlex.quote(e) for e in exprs
    )
    script = (
        "cd /work && find . -type f \\( " + find_clause + " \\) -print "
        "> /tmp/macp-artifacts.lst 2>/dev/null; "
        "test -s /tmp/macp-artifacts.lst && tar -czf - -T /tmp/macp-artifacts.lst"
    )
    proc = ssh_bytes(ip, script, timeout=180)
    if proc.returncode != 0:
        log(f"collect: no artifacts matched {exprs}")
        return []
    data = proc.stdout
    artifacts = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read()
                artifacts.append(
                    {
                        "path": (member.name[2:] if member.name.startswith("./") else member.name),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                        "content_b64": base64.b64encode(payload).decode("ascii"),
                    }
                )
    except tarfile.TarError:
        return []
    return artifacts


def run_static(
    spec: dict,
    body: dict,
    job_id: str,
    archive: bytes,
    argv: list,
    cwd: str,
    timeout: int,
) -> tuple[int, dict]:
    """Run a job on an already-running host (the Windows build node).

    The workspace is shipped with tar into ``workdir`` (Windows 10 has bsdtar),
    the command is executed through cmd.exe, and ``collect`` comes back the same
    way.  Everything is serialised by the same node lock as the ephemeral path.
    """

    domain = spec.get("domain")
    user = spec.get("user", "Administrator")
    workdir = spec.get("workdir", r"C:\macp\work")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 429, {"error": "node_busy", "detail": "one VM job at a time"}

        started = time.time()
        ip = static_host_ip(domain)
        if not ip:
            return 504, {"error": "static_host_unreachable", "domain": domain}

        def ssh_run(command: str, stdin: bytes | None = None, tmo: int = 300) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    "ssh", "-i", SSH_KEY,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "LogLevel=ERROR",
                    "-o", "ConnectTimeout=10",
                    f"{user}@{ip}", command,
                ],
                input=stdin,
                capture_output=True,
                timeout=tmo,
            )

        # Fresh workspace every job.
        ssh_run(f'cmd /c "if exist {workdir} rmdir /s /q {workdir}" && cmd /c "mkdir {workdir}"', tmo=120)
        up = ssh_run(f'tar -xf - -C {workdir}', stdin=archive, tmo=180)
        if up.returncode != 0:
            return 500, {
                "error": "workspace_upload_failed",
                "detail": up.stderr.decode(errors="replace")[-400:],
            }

        joined = " ".join(shlex.quote(str(a)) for a in argv)
        if os.path.basename(str(argv[0])).lower() in ("cmd", "cmd.exe", "powershell", "powershell.exe"):
            # Already a shell invocation; wrapping it in another cmd /c would
            # nest the quoting.  The ssh session runs through cmd.exe anyway.
            cmd = "cd /d " + workdir + "\\" + cwd + " && " + joined
        else:
            cmd = 'cmd /c "cd /d ' + workdir + "\\" + cwd + " && " + joined + '"'
        begin = time.time()
        proc = ssh_run(cmd, tmo=timeout + 60)
        duration = int((time.time() - begin) * 1000)

        artifacts: list[dict] = []
        patterns = [p for p in (body.get("collect") or []) if not str(p).startswith("/") and ".." not in str(p)]
        if patterns:
            # Windows has no find(1); collect the static prefix of each pattern.
            bases = sorted({
                str(p).replace("**", "*").split("*")[0].rstrip("/\\")
                for p in patterns
            } - {""})
            if bases:
                got = ssh_run(
                    'cmd /c "cd /d ' + workdir + " && tar -czf - " + " ".join(bases) + '"',
                    tmo=180,
                )
                if got.returncode == 0:
                    artifacts = parse_artifact_tar(got.stdout)

        return 200, {
            "exit": proc.returncode,
            "stdout": proc.stdout.decode(errors="replace")[-20000:],
            "stderr": proc.stderr.decode(errors="replace")[-20000:],
            "artifacts": [{k: v for k, v in a.items() if k != "content_b64"} for a in artifacts],
            "artifact_payloads": {a["path"]: a["content_b64"] for a in artifacts},
            "vm": domain,
            "profile": spec.get("name", "windows"),
            "ip": ip,
            "duration_ms": duration,
            "total_ms": int((time.time() - started) * 1000),
        }


def static_host_ip(domain: str | None) -> str | None:
    """Resolve the DHCP address of a long-lived domain by its NIC MAC."""

    if not domain:
        return None
    mac = ""
    info = run(["virsh", "domiflist", domain], timeout=30)
    for line in info.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and re.match(r"^([0-9a-f]{2}:){5}", parts[4]):
            mac = parts[4]
            break
    if not mac:
        return None
    return guest_ip(mac)


def parse_artifact_tar(data: bytes) -> list[dict]:
    artifacts: list[dict] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read()
                artifacts.append(
                    {
                        "path": (member.name[2:] if member.name.startswith("./") else member.name),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                        "content_b64": base64.b64encode(payload).decode("ascii"),
                    }
                )
    except tarfile.TarError:
        return []
    return artifacts


def execute(body: dict) -> tuple[int, dict]:
    profile = body.get("profile") or "kvm-debian"
    spec = PROFILES.get(profile)
    if spec is None:
        return 422, {
            "error": "unsupported_profile",
            "profile": profile,
            "supported": sorted(PROFILES),
        }
    image = Path(spec["image"]) if spec.get("image") else None

    argv = body.get("argv") or []
    if not isinstance(argv, list) or not argv:
        return 422, {"error": "argv_required"}
    program = os.path.basename(str(argv[0]))
    if program not in ALLOWED_PROGRAMS:
        return 422, {"error": "argv_not_allowed", "program": program, "allowed": sorted(ALLOWED_PROGRAMS)}

    raw = body.get("workspace_tar_b64") or ""
    try:
        archive = base64.b64decode(raw, validate=True)
    except Exception:
        return 422, {"error": "workspace_not_base64"}
    if len(archive) > MAX_WORKSPACE:
        return 413, {"error": "workspace_too_large", "bytes": len(archive), "limit": MAX_WORKSPACE}
    if not archive:
        return 422, {"error": "workspace_required"}

    timeout = int(body.get("timeout") or 300)
    if not 30 <= timeout <= 1800:
        return 422, {"error": "timeout_out_of_range", "timeout": timeout}
    cwd = str(body.get("cwd") or ".")
    if cwd.startswith("/") or ".." in cwd:
        return 422, {"error": "cwd_must_be_relative"}

    job_id = str(body.get("job_id") or uuid.uuid4())

    # A static-host profile drives a long-lived VM (the Windows build node) over
    # SSH: no overlay, no virt-install, no teardown.
    if spec.get("kind") == "static-host":
        return run_static(spec, body, job_id, archive, argv, cwd, timeout)

    if not image.exists():
        return 503, {"error": "golden_image_missing", "path": str(image), "profile": profile}

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 429, {"error": "node_busy", "detail": "one VM job at a time"}

        name = domain_name(job_id)
        overlay = IMAGE_DIR / f"{name}.qcow2"
        started = time.time()
        log(f"job={job_id} domain={name} argv={argv} cwd={cwd} timeout={timeout}")
        try:
            proc = run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(image), str(overlay), str(spec["disk"])], timeout=60)
            if proc.returncode != 0:
                return 500, {"error": "overlay_failed", "detail": proc.stderr[-400:]}
            run(["chown", "libvirt-qemu:libvirt-qemu", str(overlay)], timeout=30)

            proc = run(
                [
                    "virt-install", "--name", name,
                    "--memory", str(spec["memory"]), "--vcpus", str(spec["vcpus"]),
                    "--disk", f"path={overlay},bus=virtio",
                    "--import", "--os-variant", "debian12",
                    "--network", f"network={LIBVIRT_NET}",
                    "--graphics", "none", "--console", "pty,target_type=serial",
                    "--noautoconsole",
                ],
                timeout=180,
            )
            if proc.returncode != 0:
                return 500, {"error": "vm_start_failed", "detail": (proc.stderr or proc.stdout)[-400:]}

            mac = ""
            for _ in range(10):
                info = run(["virsh", "domiflist", name], timeout=30)
                for line in info.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and re.match(r"^([0-9a-f]{2}:){5}", parts[4]):
                        mac = parts[4]
                        break
                if mac:
                    break
                time.sleep(2)

            deadline = time.time() + BOOT_TIMEOUT
            ip = None
            while time.time() < deadline and not ip:
                ip = guest_ip(mac) if mac else None
                if not ip:
                    time.sleep(3)
            if not ip:
                return 504, {"error": "guest_no_dhcp_lease", "mac": mac}
            if not wait_ssh(ip, deadline):
                return 504, {"error": "guest_ssh_unreachable", "ip": ip}

            up = subprocess.run(
                ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                 f"{SSH_USER}@{ip}", "sudo mkdir -p /work && sudo chown $USER:$USER /work && tar -xzf - -C /work"],
                input=archive, capture_output=True, timeout=180,
            )
            if up.returncode != 0:
                return 500, {"error": "workspace_upload_failed", "detail": up.stderr.decode(errors="replace")[-400:]}

            # Non-interactive ssh does not read /etc/profile.d, so the profile's
            # environment (ANDROID_HOME, GRADLE_USER_HOME, PATH) is injected here.
            env_prefix = " ".join(
                f"{k}={shlex.quote(str(v))}" for k, v in (spec.get("env") or {}).items()
            )
            cmd = (
                "cd /work/" + cwd + " && "
                + (env_prefix + " " if env_prefix else "")
                + " ".join(shlex.quote(str(a)) for a in argv)
            )
            begin = time.time()
            proc = ssh(ip, cmd, timeout=timeout + 30)
            duration = int((time.time() - begin) * 1000)

            artifacts = collect_artifacts(ip, list(body.get("collect") or []))
            return 200, {
                "exit": proc.returncode,
                "stdout": proc.stdout[-20000:],
                "stderr": proc.stderr[-20000:],
                "artifacts": [
                    {k: v for k, v in a.items() if k != "content_b64"} for a in artifacts
                ],
                "artifact_payloads": {a["path"]: a["content_b64"] for a in artifacts},
                "vm": name,
                "profile": profile,
                "ip": ip,
                "duration_ms": duration,
                "total_ms": int((time.time() - started) * 1000),
            }
        finally:
            cleanup(name, overlay)
            log(f"job={job_id} cleaned up domain={name}")


class Handler(BaseHTTPRequestHandler):
    server_version = "macp-kvm-node/1"

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = token()
        header = self.headers.get("Authorization", "")
        if not expected:
            return False
        return header == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._reply(200, {
                "status": "ok",
                "profile": "kvm-debian",
                "golden_image": str(GOLDEN_IMAGE),
                "golden_image_present": GOLDEN_IMAGE.exists(),
                "busy": LOCK_FILE.exists() and self._busy(),
            })
            return
        self._reply(404, {"error": "not_found"})

    @staticmethod
    def _busy() -> bool:
        try:
            with open(LOCK_FILE, "w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            return False
        except OSError:
            return True

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/run":
            self._reply(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._reply(400, {"error": "invalid_json"})
            return
        try:
            status, payload = execute(body)
        except Exception as exc:  # never leak a stack trace to the caller
            log(f"job failed: {exc!r}")
            status, payload = 500, {"error": "internal_error", "detail": str(exc)[:300]}
        self._reply(status, payload)

    def log_message(self, fmt: str, *args) -> None:  # silence default logging
        return


def main() -> None:
    port = int(os.getenv("MACP_KVM_NODE_PORT", "9300"))
    bind = os.getenv("MACP_KVM_NODE_BIND", "0.0.0.0")
    log(f"macp-kvm-node listening on {bind}:{port} (golden={GOLDEN_IMAGE})")
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
