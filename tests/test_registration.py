from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from snapeda_plugin.archive import ArchiveContents, Asset
from snapeda_plugin.library import import_component, resolve_model_bindings
from snapeda_plugin.sexpr import library_names


def test_appends_project_libraries_without_replacing_existing_entries(
    tmp_path: Path,
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Existing") (type "KiCad") (uri "old.pretty") (options "") (descr "")))\n'
    )
    contents = ArchiveContents(
        (
            Asset(
                "part.kicad_sym", b'(kicad_symbol_lib (version 20231120) (symbol "Q"))'
            ),
        ),
        (Asset("PKG.kicad_mod", b'(footprint "PKG" (version 20240108))'),),
        (),
    )
    imported = import_component(contents, project, "Q-1", scope="project")
    table = (project / "fp-lib-table").read_text()
    assert '"Existing"' in table
    assert imported.footprint_nickname in library_names(table)
    assert (project / "sym-lib-table").is_file()
    assert imported.footprint_path.is_dir()
    assert imported.symbol_path.is_file()


def test_all_symbol_assets_are_registered_and_table_updates_accumulate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    contents = ArchiveContents(
        (
            Asset(
                "one.kicad_sym", b'(kicad_symbol_lib (version 20231120) (symbol "One"))'
            ),
            Asset(
                "two.kicad_sym", b'(kicad_symbol_lib (version 20231120) (symbol "Two"))'
            ),
        ),
        (),
        (),
    )
    imported = import_component(contents, project, "Q-1", scope="project")
    table = (project / "sym-lib-table").read_text()
    assert len(imported.symbol_paths) == 1
    assert imported.symbol_nicknames == ("SnapEDA",)
    assert imported.symbol_nicknames[0] in library_names(table)


def test_concurrent_imports_do_not_lose_library_table_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    first_writer = threading.Event()
    second_writer = threading.Event()
    release_first = threading.Event()
    writer_lock = threading.Lock()
    first_thread: int | None = None
    real_write = library._write_at

    def pause_first_writer(
        directory_fd: int, name: str, data: bytes, **kwargs: object
    ) -> None:
        nonlocal first_thread
        current_thread = threading.get_ident()
        with writer_lock:
            if first_thread is None:
                first_thread = current_thread
            is_first = current_thread == first_thread
        if is_first:
            first_writer.set()
            assert release_first.wait(timeout=3)
        else:
            second_writer.set()
        real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", pause_first_writer)
    contents = ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            import_component, contents, project, "Part-A", scope="project"
        )
        assert first_writer.wait(timeout=2)
        second = pool.submit(
            import_component,
            ArchiveContents(
                (), (Asset("PKG-B.kicad_mod", b'(footprint "PKG-B")'),), ()
            ),
            project,
            "Part-B",
            scope="project",
        )
        second_writer.wait(timeout=0.2)
        release_first.set()
        first.result(timeout=3)
        second.result(timeout=3)

    names = library_names((project / "fp-lib-table").read_text())
    assert names == {"SnapEDA"}


def test_import_rolls_back_when_library_nickname_conflicts(tmp_path: Path) -> None:
    project = tmp_path / "board"
    project.mkdir()
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "SnapEDA") (type "KiCad") (uri "other.pretty") (options "") (descr "")))\n'
    )
    contents = ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ())
    with pytest.raises(ValueError, match="already registered"):
        import_component(contents, project, "Q-1", scope="project")
    assert not (project / "SnapEDA.pretty").exists()


def test_shared_project_import_rejects_duplicate_symbol_and_footprint_names(
    tmp_path: Path,
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    import_component(
        ArchiveContents(
            (
                Asset(
                    "first.kicad_sym",
                    b'(kicad_symbol_lib (version 20231120) (symbol "Part"))',
                ),
            ),
            (Asset("PKG.kicad_mod", b'(footprint "PKG")'),),
            (),
        ),
        project,
        "first",
        scope="project",
    )
    previous_symbols = (project / "SnapEDA.kicad_sym").read_bytes()
    with pytest.raises(ValueError, match="Symbol already exists"):
        import_component(
            ArchiveContents(
                (
                    Asset(
                        "second.kicad_sym",
                        b'(kicad_symbol_lib (version 20231120) (symbol "Part"))',
                    ),
                ),
                (),
                (),
            ),
            project,
            "second",
            scope="project",
        )
    with pytest.raises(FileExistsError, match="Footprint already exists"):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "third",
            scope="project",
        )
    assert (project / "SnapEDA.kicad_sym").read_bytes() == previous_symbols
    assert len(list((project / "SnapEDA.pretty").iterdir())) == 1


def test_symbol_merge_rejects_duplicates_in_first_and_later_files() -> None:
    from snapeda_plugin.sexpr import merge_symbol_libraries

    duplicate = '(kicad_symbol_lib (version 20231120) (symbol "Part") (symbol "pART"))'
    with pytest.raises(ValueError, match="already exists"):
        merge_symbol_libraries("(kicad_symbol_lib (version 20231120))", duplicate)


def test_single_incoming_symbol_library_is_checked_for_casefold_duplicates(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        import_component(
            ArchiveContents(
                (
                    Asset(
                        "duplicate.kicad_sym",
                        b'(kicad_symbol_lib (version 20231120) (symbol "Part") (symbol "pART"))',
                    ),
                ),
                (),
                (),
            ),
            project,
            "part",
            scope="project",
        )
    assert not (project / "SnapEDA.kicad_sym").exists()


def test_concurrent_symbol_imports_merge_under_project_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    first_read = threading.Event()
    second_read = threading.Event()
    release_first = threading.Event()
    real_read = library._read_at
    read_count = 0
    count_lock = threading.Lock()

    def pause_first_read(directory_fd: int, name: str) -> bytes | None:
        nonlocal read_count
        if name == "SnapEDA.kicad_sym":
            with count_lock:
                read_count += 1
                current = read_count
            if current == 1:
                first_read.set()
                assert release_first.wait(timeout=3)
            else:
                second_read.set()
        return real_read(directory_fd, name)

    monkeypatch.setattr(library, "_read_at", pause_first_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            import_component,
            ArchiveContents(
                (
                    Asset(
                        "a.kicad_sym",
                        b'(kicad_symbol_lib (version 20231120) (symbol "A"))',
                    ),
                ),
                (),
                (),
            ),
            project,
            "A",
            scope="project",
        )
        assert first_read.wait(timeout=2)
        second = pool.submit(
            import_component,
            ArchiveContents(
                (
                    Asset(
                        "b.kicad_sym",
                        b'(kicad_symbol_lib (version 20231120) (symbol "B"))',
                    ),
                ),
                (),
                (),
            ),
            project,
            "B",
            scope="project",
        )
        assert not second_read.wait(timeout=0.2)
        release_first.set()
        first.result(timeout=3)
        second.result(timeout=3)
    text = (project / "SnapEDA.kicad_sym").read_text()
    assert '(symbol "A")' in text
    assert '(symbol "B")' in text


def test_project_import_rejects_symlinked_shared_library_directory(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "SnapEDA.pretty").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "part",
            scope="project",
        )
    assert list(outside.iterdir()) == []


def test_atomic_new_asset_creation_does_not_replace_racing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    external = b'(footprint "PKG")'
    external_identity: tuple[int, int] | None = None
    real_write = library._write_at

    def race_new_file(
        directory_fd: int,
        name: str,
        data: bytes,
        **kwargs: object,
    ) -> tuple[int, int]:
        nonlocal external_identity
        if name == "PKG.kicad_mod":
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(external)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            external_identity = metadata.st_dev, metadata.st_ino
            return real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]
        return real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", race_new_file)
    with pytest.raises(FileExistsError):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "part",
            scope="project",
        )
    collided_asset = project / "SnapEDA.pretty" / "PKG.kicad_mod"
    assert collided_asset.read_bytes() == external
    metadata = collided_asset.stat(follow_symlinks=False)
    assert (metadata.st_dev, metadata.st_ino) == external_identity


def test_symbol_inheritance_and_nested_footprint_properties_survive_merge() -> None:
    from snapeda_plugin.sexpr import (
        merge_symbol_libraries,
        rewrite_footprint_properties,
        validate_symbol_inheritance,
    )

    library = (
        "(kicad_symbol_lib (version 20231120) (generator test) "
        '(symbol "Base" (property "Footprint" "Old:Pkg")) '
        '(symbol "Derived" (extends "SnapEDA:Base") '
        '(symbol "Derived_0_1" (property "Footprint" "Old:Pkg"))))'
    )
    rewritten = rewrite_footprint_properties(library, {"Pkg": "Pkg"})
    validate_symbol_inheritance(rewritten)
    assert rewritten.count('"SnapEDA:Pkg"') == 2
    with pytest.raises(ValueError, match="Missing KiCad symbol base"):
        validate_symbol_inheritance(
            "(kicad_symbol_lib (version 20231120) "
            '(symbol "Child" (extends "SnapEDA:Missing")))'
        )
    with pytest.raises(ValueError, match="Cyclic"):
        validate_symbol_inheritance(
            "(kicad_symbol_lib (version 20231120) "
            '(symbol "A" (extends "SnapEDA:B")) '
            '(symbol "B" (extends "SnapEDA:A")))'
        )
    preserved = merge_symbol_libraries(
        "(kicad_symbol_lib (version 20231120) (generator test) " '(symbol "Base"))',
        "(kicad_symbol_lib (version 20231120) "
        '(symbol "Child" (extends "SnapEDA:Base")))',
    )
    validate_symbol_inheritance(preserved)
    assert '(symbol "Child" (extends "SnapEDA:Base"))' in preserved


def test_project_rollback_preserves_external_asset_edit_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    external = b"external replacement"
    footprint = project / "SnapEDA.pretty" / "PKG.kicad_mod"
    real_write = library._write_at

    def fail_after_external_edit(
        directory_fd: int, name: str, data: bytes, **kwargs: object
    ) -> None:
        if name == "fp-lib-table":
            footprint.write_bytes(external)
            raise PermissionError("simulated table failure")
        real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", fail_after_external_edit)
    with pytest.raises(PermissionError):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "part",
            scope="project",
        )
    assert footprint.read_bytes() == external
    assert not (project / "fp-lib-table").exists()


def test_project_rollback_aggregates_failure_and_attempts_remaining_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    real_write = library._write_at
    real_unlink = os.unlink

    def fail_table(
        directory_fd: int, name: str, data: bytes, **kwargs: object
    ) -> tuple[int, int]:
        if name == "fp-lib-table":
            raise PermissionError("injected table failure")
        return real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    def fail_model_unlink(name: str, *args: object, **kwargs: object) -> None:
        if name == "Part.step":
            raise PermissionError("injected model rollback failure")
        real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", fail_table)
    monkeypatch.setattr(library.os, "unlink", fail_model_unlink)
    with pytest.raises(BaseExceptionGroup, match="rollback was incomplete"):
        import_component(
            ArchiveContents(
                (),
                (Asset("Part.kicad_mod", b'(footprint "Part")'),),
                (Asset("Part.step", b"model"),),
            ),
            project,
            "part",
            scope="project",
        )
    assert not (project / "SnapEDA.pretty" / "Part.kicad_mod").exists()
    assert (
        project / "SnapEDA.3dshapes" / "part" / "Part.step"
    ).read_bytes() == b"model"


def test_post_link_directory_fsync_failure_rolls_back_asset_and_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    real_fsync = os.fsync
    injected = False

    def fail_directory_fsync(fd: int) -> None:
        nonlocal injected
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not injected:
            injected = True
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(library.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync"):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "part",
            scope="project",
        )
    assert injected
    assert not (project / "SnapEDA.pretty" / "PKG.kicad_mod").exists()
    assert not (project / "fp-lib-table").exists()
    assert not list(project.rglob(".snapeda-*"))


def test_post_replace_directory_fsync_failure_restores_combined_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    import_component(
        ArchiveContents(
            (
                Asset(
                    "first.kicad_sym",
                    b'(kicad_symbol_lib (version 20231120) (symbol "First"))',
                ),
            ),
            (),
            (),
        ),
        project,
        "first",
        scope="project",
    )
    symbol = project / "SnapEDA.kicad_sym"
    original = symbol.read_bytes()
    table_before = (project / "sym-lib-table").read_bytes()
    real_fsync = os.fsync
    injected = False

    def fail_directory_fsync(fd: int) -> None:
        nonlocal injected
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not injected:
            injected = True
            raise OSError("injected replacement fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(library.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="replacement fsync"):
        import_component(
            ArchiveContents(
                (
                    Asset(
                        "second.kicad_sym",
                        b'(kicad_symbol_lib (version 20231120) (symbol "Second"))',
                    ),
                ),
                (),
                (),
            ),
            project,
            "second",
            scope="project",
        )
    assert injected
    assert symbol.read_bytes() == original
    assert (project / "sym-lib-table").read_bytes() == table_before
    assert not list(project.glob(".snapeda-*"))


def test_post_link_temporary_unlink_failure_retries_then_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    real_unlink = os.unlink
    injected = False

    def fail_temp_cleanup(name: str, *args: object, **kwargs: object) -> None:
        nonlocal injected
        if name.startswith(".snapeda-") and not injected:
            injected = True
            raise OSError("injected temporary cleanup failure")
        real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library.os, "unlink", fail_temp_cleanup)
    with pytest.raises(OSError, match="temporary cleanup"):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "part",
            scope="project",
        )
    assert injected
    assert not (project / "SnapEDA.pretty" / "PKG.kicad_mod").exists()
    assert not (project / "fp-lib-table").exists()
    assert not list(project.rglob(".snapeda-*"))


def test_global_registration_uses_config_tables_and_absolute_assets(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "global-libraries"
    config_root = tmp_path / "kicad-config"
    imported = import_component(
        ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
        library_root,
        "Q-1",
        scope="global",
        table_dir=config_root,
    )
    assert str(imported.footprint_path) in (config_root / "fp-lib-table").read_text()
    assert (
        str(imported.footprint_path / "PKG.kicad_mod")
        not in (config_root / "fp-lib-table").read_text()
    )


def test_matching_single_footprint_and_step_asset_binds_despite_different_names() -> (
    None
):
    contents = ArchiveContents(
        (),
        (Asset("BEL_SI-52008-F.kicad_mod", b'(footprint "BEL_SI-52008-F")'),),
        (Asset("SI-52008-F.step", b"ISO-10303-21;"),),
    )
    assert resolve_model_bindings(contents) == {
        "BEL_SI-52008-F.kicad_mod": "SI-52008-F.step"
    }


def test_ambiguous_models_require_an_explicit_choice() -> None:
    contents = ArchiveContents(
        (),
        (
            Asset("A.kicad_mod", b'(footprint "A")'),
            Asset("B.kicad_mod", b'(footprint "B")'),
        ),
        (Asset("X.step", b"x"), Asset("Y.step", b"y")),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_model_bindings(contents)


def test_explicit_no_model_choice_is_respected_in_ambiguous_package() -> None:
    contents = ArchiveContents(
        (),
        (
            Asset("A.kicad_mod", b'(footprint "A")'),
            Asset("B.kicad_mod", b'(footprint "B")'),
        ),
        (Asset("X.step", b"x"), Asset("Y.step", b"y")),
    )
    assert resolve_model_bindings(
        contents, {"A.kicad_mod": "", "B.kicad_mod": "Y.step"}
    ) == {"B.kicad_mod": "Y.step"}


def test_ipc_plugin_manifest_matches_kicad_schema_shape() -> None:
    manifest = json.loads((Path(__file__).parents[1] / "plugin.json").read_text())
    assert manifest["runtime"]["type"] == "python"
    assert manifest["actions"][0]["scopes"] == ["pcb", "schematic"]
    assert manifest["actions"][0]["icons-light"] == ["icon_light.png"]
    assert manifest["actions"][0]["icons-dark"] == ["icon_dark.png"]
    assert manifest["actions"][0]["entrypoint"] == "ipc_action.py"


def test_project_model_path_uses_kiprojmod_for_downloaded_name_mismatch(
    tmp_path: Path,
) -> None:
    contents = ArchiveContents(
        (),
        (Asset("BEL_SI-52008-F.kicad_mod", b'(footprint "BEL_SI-52008-F")'),),
        (Asset("SI-52008-F.step", b"ISO-10303-21;"),),
    )
    imported = import_component(contents, tmp_path, "SI-52008-F", scope="project")
    footprint_text = (imported.footprint_path / "BEL_SI-52008-F.kicad_mod").read_text()
    assert "${KIPRJMOD}/SnapEDA.3dshapes/SI-52008-F/SI-52008-F.step" in footprint_text


def test_failed_second_table_write_restores_first_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    fp_table = project / "fp-lib-table"
    sym_table = project / "sym-lib-table"
    original_fp = b"(fp_lib_table (version 7))\n"
    original_sym = b"(sym_lib_table (version 7))\n"
    fp_table.write_bytes(original_fp)
    sym_table.write_bytes(original_sym)
    real_write = library._write_at

    def fail_second_table(
        directory_fd: int, name: str, data: bytes, **kwargs: object
    ) -> None:
        if name == "fp-lib-table" and b"SnapEDA" in data:
            raise PermissionError("test write failure")
        real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", fail_second_table)
    contents = ArchiveContents(
        (
            Asset(
                "part.kicad_sym",
                b'(kicad_symbol_lib (version 20231120) (symbol "Part"))',
            ),
        ),
        (Asset("PKG.kicad_mod", b'(footprint "PKG")'),),
        (),
    )
    with pytest.raises(PermissionError):
        import_component(contents, project, "Q-1", scope="project")
    assert fp_table.read_bytes() == original_fp
    assert sym_table.read_bytes() == original_sym
    assert not (project / "Q-1").exists()


def test_optimistic_check_preserves_external_table_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    fp_table = project / "fp-lib-table"
    original = b"(fp_lib_table (version 7))\n"
    external = b'(fp_lib_table (version 7) (lib (name "External") (type "KiCad") (uri "x.pretty") (options "") (descr "")))\n'
    fp_table.write_bytes(original)
    read_table = library._table_text_at
    changed = False

    def external_edit(directory_fd: int, name: str, table_type: str) -> str:
        nonlocal changed
        text = read_table(directory_fd, name, table_type)
        if name == "fp-lib-table" and not changed:
            fp_table.write_bytes(external)
            changed = True
        return text

    monkeypatch.setattr(library, "_table_text_at", external_edit)
    with pytest.raises(RuntimeError, match="changed during import"):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b'(footprint "PKG")'),), ()),
            project,
            "Q-1",
            scope="project",
        )
    assert fp_table.read_bytes() == external
    assert not (project / "Q-1").exists()


def test_rollback_does_not_replace_a_newer_external_table_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snapeda_plugin.library as library

    project = tmp_path / "project"
    project.mkdir()
    fp_table = project / "fp-lib-table"
    sym_table = project / "sym-lib-table"
    original_fp = b"(fp_lib_table (version 7))\n"
    original_sym = b"(sym_lib_table (version 7))\n"
    fp_table.write_bytes(original_fp)
    sym_table.write_bytes(original_sym)
    external = b'(fp_lib_table (version 7) (lib (name "External") (type "KiCad") (uri "x.pretty") (options "") (descr "")))\n'
    real_write = library._write_at
    changed = False

    def race_during_commit(
        directory_fd: int, name: str, data: bytes, **kwargs: object
    ) -> None:
        nonlocal changed
        if name == "fp-lib-table" and b"SnapEDA" in data:
            real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]
            fp_table.write_bytes(external)
            changed = True
            raise PermissionError("simulated second-table failure")
        real_write(directory_fd, name, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library, "_write_at", race_during_commit)
    contents = ArchiveContents(
        (
            Asset(
                "part.kicad_sym",
                b'(kicad_symbol_lib (version 20231120) (symbol "Part"))',
            ),
        ),
        (Asset("PKG.kicad_mod", b'(footprint "PKG")'),),
        (),
    )
    with pytest.raises(PermissionError):
        import_component(contents, project, "Q-1", scope="project")
    assert changed
    assert fp_table.read_bytes() == external
    assert sym_table.read_bytes() == original_sym
    assert not (project / "SnapEDA.pretty").exists()
    assert not (project / "SnapEDA.kicad_sym").exists()


def test_malformed_footprint_is_rejected_before_registration(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="footprint library file"):
        import_component(
            ArchiveContents((), (Asset("PKG.kicad_mod", b"not a footprint"),), ()),
            project,
            "Q-1",
            scope="project",
        )
    assert not (project / "SnapEDA.pretty").exists()
