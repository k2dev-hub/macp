"""Per-agent Ed25519 receipt signatures.

Production split (2026-11-14):
  * the private keys live only in the `macp-signer` container, which exposes
    POST /v1/sign behind a bearer token;
  * API containers mount only `agent_pubkeys/` and verify locally.

`MACP_SIGNER_URL` selects the remote signer.  Without it the old in-process
behaviour is kept for local development, but receipt creation no longer silently
substitutes a hash for a signature (see `SigningUnavailable`).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization

PUBKEYS_DIR = Path(os.getenv("MACP_AGENT_KEYS_DIR", "/app/agent_keys"))
LEGACY_KEYS_DIR = Path(os.getenv("MACP_AGENT_PRIVATE_KEYS_DIR", PUBKEYS_DIR))


class SigningUnavailable(RuntimeError):
    """Raised when a signature is required but no signer/key can produce one."""


def _signer_url() -> str:
    return os.getenv("MACP_SIGNER_URL", "").strip().rstrip("/")


def _signer_token() -> str:
    token = os.getenv("MACP_SIGNER_TOKEN", "").strip()
    if not token:
        path = os.getenv("MACP_SIGNER_TOKEN_FILE", "")
        if path and Path(path).exists():
            token = Path(path).read_text(encoding="utf-8").strip()
    return token


def _public_path(agent: str) -> Path | None:
    for suffix in (".pub", "_ssh.pub"):
        candidate = PUBKEYS_DIR / f"{agent}{suffix}"
        if suffix == ".pub" and candidate.exists():
            return candidate
    candidate = PUBKEYS_DIR / f"{agent}.pub"
    return candidate if candidate.exists() else None


def public_key(agent: str):
    path = _public_path(agent)
    if path is None:
        return None
    try:
        return serialization.load_pem_public_key(path.read_bytes())
    except Exception:
        return None


def sign(agent: str, data: str) -> str | None:
    """Sign `data` for `agent`.

    Raises `SigningUnavailable` when a signer is configured but fails, so a
    receipt is never stored with a placeholder instead of a real signature.
    """

    url = _signer_url()
    if url:
        payload = json.dumps({"agent": agent, "data": data}).encode("utf-8")
        req = urllib.request.Request(url + "/v1/sign", data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + _signer_token())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            signature = body.get("signature")
            if not signature or not str(signature).startswith("ed25519:"):
                raise SigningUnavailable(f"signer returned no signature for {agent}")
            return signature
        except urllib.error.HTTPError as exc:
            raise SigningUnavailable(
                f"signer HTTP {exc.code} for {agent}: {exc.read().decode(errors='replace')[:200]}"
            ) from exc
        except Exception as exc:  # network, timeout, bad JSON
            raise SigningUnavailable(f"signer unreachable for {agent}: {exc}") from exc

    # Local development fallback: read the private key directly if it is mounted.
    path = LEGACY_KEYS_DIR / f"{agent}.pem"
    if not path.exists():
        raise SigningUnavailable(f"no signer configured and no private key for {agent}")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    return "ed25519:" + base64.b64encode(key.sign(data.encode("utf-8"))).decode("ascii")


def verify(agent: str, data: str, signature: str) -> bool:
    key = public_key(agent)
    if key is None or not signature or not signature.startswith("ed25519:"):
        return False
    try:
        key.verify(base64.b64decode(signature.removeprefix("ed25519:")), data.encode("utf-8"))
        return True
    except Exception:
        return False
