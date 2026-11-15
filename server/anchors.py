"""Resolve work-order anchors against a real base commit."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout).strip())
    return proc.stdout


def _read_lines(repo: Path, base_commit: str, path: str) -> list[str]:
    return _git(repo, "show", f"{base_commit}:{path}").splitlines()


def resolve(base_commit: str, anchors: list[dict], repo: Path = Path("/app/workspace/repo.git")) -> list[dict]:
    """Return anchors with ``resolved_digest`` and ``resolved_base``.

    Digests are SHA-256 of the selected lines joined by ``\\n``.  A declared
    digest that does not match the base commit raises ``ValueError``.
    """

    resolved = []
    for anchor in anchors or []:
        ref = anchor.get("ref") or ""
        match = re.fullmatch(r"([^:]+):([0-9]+)(?:-([0-9]+))?", ref)
        if not match:
            raise ValueError(f"invalid anchor ref: {ref}")
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
        if start < 1 or end < start:
            raise ValueError(f"invalid anchor range: {ref}")
        lines = _read_lines(repo, base_commit, path)
        if end > len(lines):
            raise ValueError(f"anchor {ref} exceeds {path} ({len(lines)} lines)")
        digest = "sha256:" + hashlib.sha256(
            "\n".join(lines[start - 1 : end]).encode("utf-8")
        ).hexdigest()
        declared = anchor.get("digest")
        if declared and declared != digest:
            raise ValueError(
                f"anchor {ref} digest mismatch: declared {declared}, base {digest}"
            )
        resolved.append(
            {
                **anchor,
                "digest": digest,
                "resolved_digest": digest,
                "resolved_base": base_commit,
            }
        )
    return resolved
