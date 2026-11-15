"""Requirement document ingestion: md/txt/pdf/docx/doc."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path

WORKSPACE = Path(os.getenv("MACP_WORKSPACE_ROOT", "/app/workspace"))
MAX_BYTES = int(os.getenv("MACP_DOC_MAX_BYTES", str(10 * 1024 * 1024)))
MAX_CHARS = int(os.getenv("MACP_DOC_MAX_CHARS", "2000000"))
ALLOWED = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}


def safe_name(name: str) -> str:
    base = Path(name or "document").name
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)
    return base[:200] or "document"


def storage_dir(project_code: str, document_id: uuid.UUID) -> Path:
    return WORKSPACE / "projects" / project_code / "documents" / str(document_id)


def _text_file(path: Path) -> tuple[str, list[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding), []
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), ["text decoded with replacement characters"]


def _pdf(path: Path) -> tuple[str, list[str]]:
    from pypdf import PdfReader

    warnings: list[str] = []
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted")
    pages = []
    for index, page in enumerate(reader.pages[:300], start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            warnings.append(f"page {index} text extraction failed")
    if len(reader.pages) > 300:
        warnings.append("only first 300 PDF pages extracted")
    text = "\n\n".join(pages).strip()
    if not text:
        warnings.append("PDF contains no extractable text (possibly scanned)")
    return text, warnings


def _docx(path: Path) -> tuple[str, list[str]]:
    from docx import Document

    document = Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip(), []


def _doc(path: Path) -> tuple[str, list[str]]:
    warnings: list[str] = []
    for args in (
        ["antiword", "-m", "UTF-8.txt", str(path)],
        ["antiword", str(path)],
    ):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=30, check=False)
        except FileNotFoundError as exc:
            raise ValueError("antiword is not installed; convert .doc to .docx or PDF") from exc
        if proc.returncode == 0 and proc.stdout.strip():
            for encoding in ("utf-8", "latin-1"):
                try:
                    return proc.stdout.decode(encoding), warnings
                except UnicodeDecodeError:
                    continue
        warnings.append("antiword conversion failed")
    raise ValueError("could not extract legacy .doc; convert to .docx or PDF")


def extract(path: Path, suffix: str) -> tuple[str, list[str]]:
    suffix = suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        return _text_file(path)
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".docx":
        return _docx(path)
    if suffix == ".doc":
        return _doc(path)
    raise ValueError(f"unsupported document type: {suffix}")


def ingest(project_code: str, filename: str, data: bytes) -> dict:
    name = safe_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("supported types: md, markdown, txt, pdf, docx, doc")
    if len(data) == 0:
        raise ValueError("empty file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"file exceeds {MAX_BYTES} bytes")
    document_id = uuid.uuid4()
    directory = storage_dir(project_code, document_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    os.chmod(path, 0o600)
    text, warnings = extract(path, suffix)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        warnings.append(f"text truncated to {MAX_CHARS} chars")
    return {
        "id": document_id,
        "filename": name,
        "media_type": ALLOWED[suffix],
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage_path": str(path),
        "text_content": text,
        "char_count": len(text),
        "warnings": warnings,
        "suffix": suffix,
    }
