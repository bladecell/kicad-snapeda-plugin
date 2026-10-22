"""Resolve the single project exposed by KiCad's read-only IPC document list."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from typing import Any


def project_root_from_documents(documents: Sequence[Any]) -> Path | None:
    """Return a validated project root only when document context is unambiguous."""
    roots: set[Path] = set()
    for document in documents:
        project = getattr(document, "project", None)
        raw_path = getattr(project, "path", "")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        if candidate.suffix == ".kicad_pro":
            candidate = candidate.parent
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            return None
        if not root.is_dir() or not any(root.glob("*.kicad_pro")):
            return None
        roots.add(root)
    if len(roots) != 1:
        return None
    return next(iter(roots))


def active_project_root() -> Path | None:
    """Read available PCB project context without changing KiCad documents."""
    try:
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType

        kicad = KiCad(timeout_ms=1000)
        documents = kicad.get_open_documents(DocumentType.DOCTYPE_PCB)
        return project_root_from_documents(documents)
    except Exception:
        return None
