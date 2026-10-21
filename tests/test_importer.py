from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from snapeda_plugin.archive import inspect_archive
from snapeda_plugin.library import import_component


def archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


def test_extracts_supported_assets_and_links_step_model() -> None:
    contents = inspect_archive(
        archive(
            {
                "sym.kicad_sym": b'(symbol "Part" (property "Footprint" "Pkg:Pad"))',
                "Pkg.kicad_mod": b'(footprint "Pkg" (version 20240108))',
                "Part.step": b"ISO-10303-21;",
                "import.htm": b"not executable",
            }
        )
    )
    assert contents.symbols[0].name == "sym.kicad_sym"
    assert contents.footprints[0].name == "Pkg.kicad_mod"
    assert contents.models[0].name == "Part.step"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "folder/../../escape"])
def test_rejects_archive_path_traversal(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        inspect_archive(archive({name: b"bad"}))


def test_rejects_duplicate_asset_names_case_insensitively() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        inspect_archive(archive({"Part.kicad_mod": b"a", "part.KICAD_MOD": b"b"}))


def test_import_creates_non_overwriting_library_and_binds_matching_model(
    tmp_path: Path,
) -> None:
    contents = inspect_archive(
        archive(
            {
                "Part.kicad_mod": b'(footprint "Part" (version 20240108))',
                "Part.step": b"ISO-10303-21;",
            }
        )
    )
    installed = import_component(contents, tmp_path, "Part / 1", scope="project").root
    footprint = (tmp_path / "SnapEDA.pretty" / "Part.kicad_mod").read_text()
    assert '(model "${KIPRJMOD}/SnapEDA.3dshapes/Part_1/Part.step"' in footprint
    assert (
        tmp_path / "SnapEDA.3dshapes" / "Part_1" / "Part.step"
    ).read_bytes() == b"ISO-10303-21;"
    with pytest.raises(FileExistsError):
        import_component(contents, tmp_path, "Part / 1", scope="project")


def test_ignores_non_kicad_archive_content() -> None:
    contents = inspect_archive(archive({"import.htm": b"<script>bad()</script>"}))
    assert contents.symbols == contents.footprints == contents.models == ()


def test_project_imports_merge_into_shared_libraries_and_keep_model_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    first = inspect_archive(
        archive(
            {
                "first.kicad_sym": b'(kicad_symbol_lib (version 20231120) (generator test) (symbol "First" (property "Footprint" "Old:FirstPkg")))',
                "FirstPkg.kicad_mod": b'(footprint "FirstPkg" (version 20240108))',
                "FirstPkg.step": b"first model",
            }
        )
    )
    second = inspect_archive(
        archive(
            {
                "second.kicad_sym": b'(kicad_symbol_lib (version 20231120) (generator test) (symbol "Second" (property "Footprint" "Old:SecondPkg")))',
                "SecondPkg.kicad_mod": b'(footprint "SecondPkg" (version 20240108))',
                "SecondPkg.step": b"second model",
            }
        )
    )

    import_component(first, project, "First Part", scope="project")
    import_component(second, project, "Second Part", scope="project")

    from snapeda_plugin.sexpr import parse

    symbol_path = project / "SnapEDA.kicad_sym"
    symbol_lib = parse(symbol_path.read_text())
    symbols = [
        node.children[1].value
        for node in symbol_lib.children
        if node.children and node.children[0].value == "symbol"
    ]
    assert symbols == ["First", "Second"]
    assert (project / "sym-lib-table").read_text().count('(name "SnapEDA")') == 1
    assert (project / "fp-lib-table").read_text().count('(name "SnapEDA")') == 1
    assert (project / "SnapEDA.pretty" / "FirstPkg.kicad_mod").is_file()
    first_fp = (project / "SnapEDA.pretty" / "FirstPkg.kicad_mod").read_text()
    second_fp = (project / "SnapEDA.pretty" / "SecondPkg.kicad_mod").read_text()
    assert "${KIPRJMOD}/SnapEDA.3dshapes/First_Part/FirstPkg.step" in first_fp
    assert "${KIPRJMOD}/SnapEDA.3dshapes/Second_Part/SecondPkg.step" in second_fp
    assert '(property "Footprint" "SnapEDA:FirstPkg")' in symbol_path.read_text()


def test_rejects_distinct_archive_paths_that_collide_when_flattened() -> None:
    with pytest.raises(ValueError, match="colliding"):
        inspect_archive(
            archive(
                {
                    "first/Part.kicad_mod": b"first",
                    "second/part.KICAD_MOD": b"second",
                }
            )
        )
