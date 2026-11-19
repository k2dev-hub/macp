"""Anchor provenance fingerprints on a Hyperledger Fabric channel.

The MACP hash chain stays the local source of truth.  Fabric adds what a local
chain cannot: several organisations keep the same fingerprint, so no single
operator can rewrite the history unnoticed.

The bridge is a small HTTP service next to the peer (deploy/fabric/bridge.py).
This module only talks to it, so the API image needs no Fabric SDK, no gRPC and
no MSP material — an unreachable bridge degrades to "not anchored" instead of
breaking the build record.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _base_url() -> str:
    return os.getenv("MACP_FABRIC_URL", "").strip().rstrip("/")


def _token() -> str:
    token = os.getenv("MACP_FABRIC_TOKEN", "").strip()
    if token:
        return token
    path = os.getenv("MACP_FABRIC_TOKEN_FILE", "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def enabled() -> bool:
    return bool(_base_url())


def anchor(record: dict[str, Any], timeout: float = 20.0) -> dict[str, Any] | None:
    """Submit one record's fingerprint.

    Returns the bridge answer, or a failure envelope.  Never raises: a chain
    outage must not lose the build record.
    """

    url = _base_url()
    if not url:
        return None

    payload = {
        "build_id": record.get("build_id"),
        "fingerprint": record.get("fingerprint"),
        "artifact_sha256": record.get("sha256") or record.get("artifact_sha256"),
        "task": record.get("task"),
        "builder": record.get("builder"),
        "project": record.get("project"),
        "created_at": record.get("created_at"),
    }
    request = urllib.request.Request(
        url + "/anchor",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    token = _token()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            "status": "anchored" if body.get("tx_id") else "failed",
            "tx_id": body.get("tx_id"),
            "block": body.get("block_number"),
            "error": None if body.get("tx_id") else body.get("error"),
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return {"status": "failed", "tx_id": None, "block": None, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # network, timeout, bad json
        return {"status": "failed", "tx_id": None, "block": None, "error": str(exc)[:300]}


def query(build_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """Read a fingerprint back from the chain (independent of our database)."""

    url = _base_url()
    if not url:
        return None
    request = urllib.request.Request(url + f"/anchor/{build_id}")
    token = _token()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"found": False}
        return {"found": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"found": False, "error": str(exc)[:200]}


def ledger(limit: int = 20, timeout: float = 20.0) -> dict[str, Any] | None:
    """Newest anchors on the chain, for the ledger view."""

    url = _base_url()
    if not url:
        return None
    request = urllib.request.Request(f"{url}/ledger?limit={max(1, min(200, limit))}")
    token = _token()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def stats(timeout: float = 15.0) -> dict[str, Any] | None:
    """Totals and time span of the ledger."""

    url = _base_url()
    if not url:
        return None
    request = urllib.request.Request(url + "/stats")
    token = _token()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"error": str(exc)[:200]}
