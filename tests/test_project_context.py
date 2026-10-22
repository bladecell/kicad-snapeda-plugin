from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest

from snapeda_plugin.project_context import (
    active_project_root,
    project_root_from_documents,
)


def document(path: str) -> SimpleNamespace:
    return SimpleNamespace(project=SimpleNamespace(path=path))


def test_project_root_accepts_project_file_or_directory(tmp_path: Path) -> None:
    project = tmp_path / "board"
    project.mkdir()
    (project / "board.kicad_pro").touch()

    assert project_root_from_documents([document(str(project))]) == project
    assert (
        project_root_from_documents([document(str(project / "board.kicad_pro"))])
        == project
    )


def test_project_root_refuses_ambiguous_or_invalid_context(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "first.kicad_pro").touch()
    (second / "second.kicad_pro").touch()

    assert (
        project_root_from_documents([document(str(first)), document(str(second))])
        is None
    )
    assert project_root_from_documents([document(str(tmp_path / "missing"))]) is None
    assert project_root_from_documents([]) is None


def test_active_project_reads_only_pcb_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    (project / "board.kicad_pro").touch()
    calls: list[int] = []

    class FakeKiCad:
        def __init__(self, *, timeout_ms: int) -> None:
            assert timeout_ms == 1000

        def get_open_documents(self, doc_type: int) -> list[SimpleNamespace]:
            calls.append(doc_type)
            return [document(str(project))]

    kipy = ModuleType("kipy")
    kipy.KiCad = FakeKiCad  # type: ignore[attr-defined]
    proto = ModuleType("kipy.proto")
    common = ModuleType("kipy.proto.common")
    types = ModuleType("kipy.proto.common.types")
    types.DocumentType = SimpleNamespace(DOCTYPE_PCB=3)  # type: ignore[attr-defined]
    for name, module in (
        ("kipy", kipy),
        ("kipy.proto", proto),
        ("kipy.proto.common", common),
        ("kipy.proto.common.types", types),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    assert active_project_root() == project
    assert calls == [3]
