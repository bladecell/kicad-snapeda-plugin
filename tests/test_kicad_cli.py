from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from snapeda_plugin.archive import ArchiveContents, Asset
from snapeda_plugin.library import import_component

KICAD_SYMBOL = b"""(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")
  (symbol "Sample" (pin_names (offset 0)) (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Sample" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (symbol "Sample_0_1" (rectangle (start -2.54 -2.54) (end 2.54 2.54)
      (stroke (width 0) (type default)) (fill (type background))))))
"""
KICAD_FOOTPRINT = b"""(footprint "PKG" (version 20240108) (generator "pcbnew") (layer "F.Cu") (attr smd)
  (fp_rect (start -1 -1) (end 1 1) (stroke (width 0.1) (type default)) (fill none) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))
"""


@pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="KiCad CLI is unavailable"
)
def test_imported_files_export_with_kicad_cli(tmp_path: Path) -> None:
    contents = ArchiveContents(
        (Asset("sample.kicad_sym", KICAD_SYMBOL),),
        (Asset("PKG.kicad_mod", KICAD_FOOTPRINT),),
        (Asset("SI-52008-F.step", b"ISO-10303-21;"),),
    )
    project = tmp_path / "project"
    project.mkdir()
    imported = import_component(
        contents,
        project,
        "SI-52008-F",
        scope="project",
    )
    fp_out = tmp_path / "fp-svg"
    fp_out.mkdir()
    subprocess.run(
        [
            "kicad-cli",
            "fp",
            "export",
            "svg",
            str(imported.footprint_path),
            "-o",
            str(fp_out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sym_out = tmp_path / "sym-svg"
    sym_out.mkdir()
    subprocess.run(
        [
            "kicad-cli",
            "sym",
            "export",
            "svg",
            str(imported.symbol_path),
            "-o",
            str(sym_out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (fp_out / "PKG.svg").is_file()
    assert (sym_out / "Sample_unit1.svg").is_file()
