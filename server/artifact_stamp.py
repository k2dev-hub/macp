"""Stamp a build: the files that travel next to an artifact.

A provenance record is useless if nobody can find it from the artifact.  For
each recorded build we produce a small stamp package:

    provenance.json   the record, its fingerprint and the verification URL
    verify-qr.svg     the same URL as a vector QR (print at any size)
    verify-qr.png     a raster QR for a label printer or an asset folder
    README.txt        one paragraph: what this is and how to check it

The artifact itself is never modified — its hash is what the record covers, and
rewriting it would invalidate the very thing we signed.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any


def verify_url(base_url: str, build_id: str) -> str:
    return base_url.rstrip("/") + f"/verify/{build_id}"


def _qr(target: str, scale: int = 6):
    import segno

    return segno.make(target, error="m"), scale


def qr_svg(target: str) -> str:
    code, scale = _qr(target)
    return code.svg_inline(scale=scale, border=2)


def qr_png(target: str) -> bytes:
    code, scale = _qr(target)
    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=scale, border=2)
    return buffer.getvalue()


def _readme(record: dict[str, Any], url: str) -> str:
    return (
        "构建溯源凭据 / Build provenance stamp\n"
        "=====================================\n\n"
        f"构建编号 Build id : {record.get('build_id')}\n"
        f"产物指纹 Artifact : {record.get('artifact_sha256')}\n"
        f"源码版本 Source   : {record.get('source_commit')}\n"
        f"构建时间 Built at : {record.get('created_at_iso') or record.get('created_at')}\n"
        f"构建人 Builder    : {record.get('builder')}\n\n"
        f"验证 Verification : {url}\n"
        "扫描 verify-qr.png 或访问上面的地址即可核对；\n"
        "记录以哈希链存证，产物或字段被改动时验证会失败。\n"
    )


def build_stamp(record: dict[str, Any], base_url: str) -> dict[str, bytes]:
    """Return the files of one stamp package, keyed by file name."""

    url = verify_url(base_url, record["build_id"])
    payload = {
        "build_id": record.get("build_id"),
        "fingerprint": record.get("fingerprint"),
        "artifact": {
            "name": record.get("artifact_name"),
            "sha256": record.get("artifact_sha256"),
            "size": record.get("artifact_size"),
        },
        "source": {
            "commit": record.get("source_commit"),
            "ref": record.get("source_ref"),
        },
        "build": {
            "gate": record.get("gate"),
            "platform": record.get("platform"),
            "profile": record.get("profile"),
            "command": record.get("build_command"),
            "environment": record.get("build_env"),
            "builder": record.get("builder"),
            "created_at": record.get("created_at_iso") or record.get("created_at"),
        },
        "dependencies": {
            "count": record.get("dependency_count"),
            "items": record.get("dependencies"),
        },
        "verify_url": url,
        "stamped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return {
        "provenance.json": json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        "verify-qr.svg": qr_svg(url).encode("utf-8"),
        "verify-qr.png": qr_png(url),
        "README.txt": _readme(record, url).encode("utf-8"),
    }


def build_zip(record: dict[str, Any], base_url: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in build_stamp(record, base_url).items():
            archive.writestr(name, data)
    return buffer.getvalue()
