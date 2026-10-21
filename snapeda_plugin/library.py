"""Install assets and register KiCad symbol and footprint libraries."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import fcntl
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterator

from .archive import ArchiveContents
from .sexpr import (
    append_library,
    library_names,
    merge_symbol_libraries,
    field_value,
    parse,
    quote,
    rewrite_footprint_properties,
    validate_symbol_inheritance,
)


@dataclass(frozen=True, slots=True)
class ImportedLibraries:
    root: Path
    footprint_path: Path | None
    footprint_nickname: str | None
    symbol_paths: tuple[Path, ...]
    symbol_nicknames: tuple[str, ...]

    @property
    def symbol_path(self) -> Path | None:
        return self.symbol_paths[0] if self.symbol_paths else None


def safe_name(value: str) -> str:
    """Create a portable library nickname from a part number."""
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:64]
    if not result:
        raise ValueError("Part has no usable library name")
    return result


def resolve_model_bindings(
    contents: ArchiveContents, choices: dict[str, str] | None = None
) -> dict[str, str]:
    """Match STEP files to footprints by basename or unique one-to-one pairing."""
    bindings, remaining_footprints, remaining_models = model_binding_options(contents)
    if choices:
        for footprint_name, model_name in choices.items():
            if footprint_name not in remaining_footprints:
                raise ValueError("Invalid explicit 3D model selection")
            remaining_footprints.remove(footprint_name)
            if model_name:
                if model_name not in remaining_models:
                    raise ValueError("Invalid explicit 3D model selection")
                bindings[footprint_name] = model_name
                remaining_models.remove(model_name)
    if remaining_footprints and remaining_models:
        if len(remaining_footprints) == len(remaining_models) == 1:
            bindings[remaining_footprints[0]] = remaining_models[0]
        else:
            raise ValueError(
                "3D model association is ambiguous; choose a model for each footprint"
            )
    return bindings


def model_binding_options(
    contents: ArchiveContents,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return exact matches and assets needing an explicit association."""
    bindings: dict[str, str] = {}
    for footprint in contents.footprints:
        matches = [
            model
            for model in contents.models
            if Path(model.name).stem.casefold() == Path(footprint.name).stem.casefold()
        ]
        if len(matches) == 1:
            bindings[footprint.name] = matches[0].name
    used_models = set(bindings.values())
    remaining_footprints = [
        item.name for item in contents.footprints if item.name not in bindings
    ]
    remaining_models = [
        item.name for item in contents.models if item.name not in used_models
    ]
    if len(remaining_footprints) == len(remaining_models) == 1:
        bindings[remaining_footprints.pop()] = remaining_models.pop()
    return bindings, remaining_footprints, remaining_models


def _table_text(path: Path, table_type: str) -> str:
    if not path.exists():
        return f"({table_type}\n  (version 7)\n)\n"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Library table is not a regular file: {path}")
    text = path.read_text(encoding="utf-8")
    root = parse(text)
    if not root.children or root.children[0].value != table_type:
        raise ValueError(f"Unexpected library table format in {path}")
    return text


def _table_text_at(directory_fd: int, name: str, table_type: str) -> str:
    data = _read_at(directory_fd, name)
    if data is None:
        return f"({table_type}\n  (version 7)\n)\n"
    text = data.decode("utf-8")
    root = parse(text)
    if not root.children or root.children[0].value != table_type:
        raise ValueError(f"Unexpected library table format in {name}")
    return text


def _entry(nickname: str, uri: str, description: str) -> str:
    return (
        f'(lib (name {quote(nickname)}) (type "KiCad") (uri {quote(uri)}) '
        f'(options "") (descr {quote(description)}))'
    )


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _table_directory_lock(directory: Path) -> Iterator[int]:
    """Serialize this add-on's table transactions across threads and processes."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _read_at(directory_fd: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd
        )
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"SnapEDA asset is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _identity_at(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"SnapEDA asset is not a regular file: {name}")
    return metadata.st_dev, metadata.st_ino


def _write_at(
    directory_fd: int,
    name: str,
    data: bytes,
    *,
    replace: bool,
    expected: bytes | None = None,
    on_publish: Callable[[tuple[int, int]], None] | None = None,
) -> tuple[int, int]:
    """Stage a file through a pinned directory; new names use no-replace link."""
    temporary_name = f".snapeda-{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    failure: BaseException | None = None
    identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        if replace:
            if expected is None or _read_at(directory_fd, name) != expected:
                raise RuntimeError(f"SnapEDA asset changed during import: {name}")
            os.replace(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        else:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        if on_publish is not None and identity is not None:
            on_publish(identity)
        os.fsync(directory_fd)
    except BaseException as exc:
        failure = exc
    cleanup_errors: list[BaseException] = []
    try:
        os.close(descriptor)
    except BaseException as exc:
        cleanup_errors.append(exc)
    try:
        os.unlink(temporary_name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except BaseException as exc:
        cleanup_errors.append(exc)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except BaseException as retry_error:
            cleanup_errors.append(retry_error)
    if failure is not None and cleanup_errors:
        raise BaseExceptionGroup(
            "Asset write failed and temporary cleanup was incomplete",
            [failure, *cleanup_errors],
        ) from failure
    if failure is not None:
        raise failure
    if cleanup_errors:
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        raise BaseExceptionGroup("Temporary cleanup failed", cleanup_errors)
    assert identity is not None
    return identity


def _ensure_child_directory(parent_fd: int, name: str) -> tuple[int, bool]:
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    return descriptor, created


def _commit_tables(
    table_dir: Path,
    entries: list[tuple[Path, str, str]],
    *,
    acquire_lock: bool = True,
    directory_fd: int | None = None,
) -> None:
    """Merge library entries under a directory lock and preserve concurrent edits."""
    with _table_directory_lock(table_dir) if acquire_lock else nullcontext():
        originals: dict[Path, bytes | None] = {}
        staged_text: dict[Path, str] = {}
        names_by_path: dict[Path, set[str]] = {}
        read_table = (
            (lambda path: _read_at(directory_fd, path.name))
            if directory_fd is not None
            else _read_optional
        )
        for table_path, table_type, entry in entries:
            if table_path not in staged_text:
                original = read_table(table_path)
                originals[table_path] = original
                current = (
                    _table_text_at(directory_fd, table_path.name, table_type)
                    if directory_fd is not None
                    else _table_text(table_path, table_type)
                )
                staged_text[table_path] = current
                names_by_path[table_path] = library_names(current)
            nickname = parse(entry).children[1].children[1].value
            names = names_by_path[table_path]
            if nickname and nickname.casefold() in {name.casefold() for name in names}:
                raise ValueError(f"Library nickname already registered: {nickname}")
            staged_text[table_path] = append_library(staged_text[table_path], entry)
            if nickname:
                names.add(nickname)

        staged = {path: text.encode("utf-8") for path, text in staged_text.items()}
        for path, original in originals.items():
            if read_table(path) != original:
                raise RuntimeError(f"Library table changed during import: {path}")

        attempted: list[Path] = []
        staged_identities: dict[Path, tuple[int, int]] = {}
        try:
            for path, data in staged.items():
                if read_table(path) != originals[path]:
                    raise RuntimeError(f"Library table changed during import: {path}")
                attempted.append(path)
                if directory_fd is None:
                    _atomic_write(path, data)
                else:
                    _write_at(
                        directory_fd,
                        path.name,
                        data,
                        replace=originals[path] is not None,
                        expected=originals[path],
                    )
                    identity = _identity_at(directory_fd, path.name)
                    if identity is not None:
                        staged_identities[path] = identity
        except BaseException as failure:
            rollback_errors: list[BaseException] = []
            for path in reversed(attempted):
                try:
                    if read_table(path) != staged[path]:
                        continue
                    if (
                        directory_fd is not None
                        and path in staged_identities
                        and _identity_at(directory_fd, path.name)
                        != staged_identities[path]
                    ):
                        continue
                    original = originals[path]
                    if original is None:
                        if directory_fd is None:
                            path.unlink(missing_ok=True)
                        else:
                            os.unlink(path.name, dir_fd=directory_fd)
                    else:
                        if directory_fd is None:
                            _atomic_write(path, original)
                        else:
                            _write_at(
                                directory_fd,
                                path.name,
                                original,
                                replace=True,
                                expected=staged[path],
                            )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise BaseExceptionGroup(
                    "Library table update failed and rollback was incomplete",
                    [failure, *rollback_errors],
                ) from failure
            raise


def import_component(
    contents: ArchiveContents,
    library_parent: Path,
    part_name: str,
    *,
    scope: str,
    table_dir: Path | None = None,
    model_choices: dict[str, str] | None = None,
) -> ImportedLibraries:
    """Import files into shared project libraries or per-part global libraries.

    Project imports merge symbols and reject asset collisions; global imports
    preserve the historical per-part layout. Failed imports roll back staged
    assets and table changes when the transaction still owns their contents.
    """
    if scope not in {"project", "global"}:
        raise ValueError("scope must be 'project' or 'global'")
    if not (contents.symbols or contents.footprints or contents.models):
        raise ValueError("The archive contained no supported KiCad assets")
    if scope == "global" and table_dir is None:
        raise ValueError(
            "Global registration requires the KiCad configuration directory"
        )
    _validate_assets(contents)
    if scope == "project":
        return _import_project_shared(
            contents, library_parent, part_name, model_choices
        )
    root = library_parent / safe_name(part_name)
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    assert table_dir is not None
    footprint_path: Path | None = None
    footprint_nickname: str | None = None
    symbol_paths: list[Path] = []
    symbol_nicknames: list[str] = []
    try:
        table_dir.mkdir(parents=True, exist_ok=True)
        fp_table = table_dir / "fp-lib-table" if contents.footprints else None
        sym_table = table_dir / "sym-lib-table" if contents.symbols else None
        bindings = resolve_model_bindings(contents, model_choices)
        model_dir = root / "3dmodels"
        if contents.models:
            model_dir.mkdir()
            for model in contents.models:
                (model_dir / model.name).write_bytes(model.data)
        if contents.footprints:
            footprint_nickname = f"SnapEDA_{safe_name(part_name)}"
            footprint_path = root / f"{footprint_nickname}.pretty"
            footprint_path.mkdir()
            for footprint in contents.footprints:
                target = footprint_path / footprint.name
                text = footprint.data.decode("utf-8")
                binding = bindings.get(footprint.name)
                if binding:
                    tree = parse(text)
                    if any(
                        node.value is None
                        and node.children
                        and node.children[0].value == "model"
                        for node in tree.children
                    ):
                        raise ValueError(
                            f"Footprint already contains a 3D model: {footprint.name}"
                        )
                    model_file = model_dir / binding
                    uri = (
                        str(model_file.resolve())
                        if scope == "global"
                        else "${KIPRJMOD}/"
                        + model_file.relative_to(library_parent).as_posix()
                    )
                    text = (
                        text[: tree.end - 1]
                        + f"\n  (model {quote(uri)} (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n"
                        + text[tree.end - 1 :]
                    )
                target.write_text(text, encoding="utf-8")
        if contents.symbols:
            for index, symbol in enumerate(contents.symbols, start=1):
                nickname = (
                    f"SnapEDA_{safe_name(part_name)}"
                    if index == 1
                    else f"SnapEDA_{safe_name(part_name)}_{index}"
                )
                symbol_path = root / f"{nickname}.kicad_sym"
                symbol_path.write_bytes(symbol.data)
                symbol_paths.append(symbol_path)
                symbol_nicknames.append(nickname)

        entries: list[tuple[Path, str, str]] = []
        if fp_table and footprint_path and footprint_nickname:
            uri = (
                str(footprint_path.resolve())
                if scope == "global"
                else "${KIPRJMOD}/"
                + footprint_path.relative_to(library_parent).as_posix()
            )
            entries.append(
                (
                    fp_table,
                    "fp_lib_table",
                    _entry(footprint_nickname, uri, f"SnapEDA {part_name}"),
                )
            )
        if sym_table:
            for nickname, symbol_path in zip(
                symbol_nicknames, symbol_paths, strict=True
            ):
                uri = (
                    str(symbol_path.resolve())
                    if scope == "global"
                    else "${KIPRJMOD}/"
                    + symbol_path.relative_to(library_parent).as_posix()
                )
                entries.append(
                    (
                        sym_table,
                        "sym_lib_table",
                        _entry(nickname, uri, f"SnapEDA {part_name}"),
                    )
                )
        _commit_tables(table_dir, entries)
        return ImportedLibraries(
            root,
            footprint_path,
            footprint_nickname,
            tuple(symbol_paths),
            tuple(symbol_nicknames),
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _import_project_shared(
    contents: ArchiveContents,
    project: Path,
    part_name: str,
    model_choices: dict[str, str] | None,
) -> ImportedLibraries:
    """Merge project imports into the SnapEDA project libraries transactionally."""
    project.mkdir(parents=True, exist_ok=True)
    with _table_directory_lock(project) as project_fd:
        return _import_project_shared_locked(
            contents, project, part_name, model_choices, project_fd
        )


def _import_project_shared_locked(
    contents: ArchiveContents,
    project: Path,
    part_name: str,
    model_choices: dict[str, str] | None,
    project_fd: int,
) -> ImportedLibraries:
    part = safe_name(part_name)
    symbol_path = project / "SnapEDA.kicad_sym"
    footprint_path = project / "SnapEDA.pretty"
    model_root = project / "SnapEDA.3dshapes"
    sym_table = project / "sym-lib-table" if contents.symbols else None
    fp_table = project / "fp-lib-table" if contents.footprints else None
    entries: list[tuple[Path, str, str]] = []
    if sym_table:
        entries.append(
            (
                sym_table,
                "sym_lib_table",
                _entry(
                    "SnapEDA",
                    "${KIPRJMOD}/SnapEDA.kicad_sym",
                    "SnapEDA project symbols",
                ),
            )
        )
    if fp_table:
        entries.append(
            (
                fp_table,
                "fp_lib_table",
                _entry(
                    "SnapEDA",
                    "${KIPRJMOD}/SnapEDA.pretty",
                    "SnapEDA project footprints",
                ),
            )
        )

    bindings = resolve_model_bindings(contents, model_choices)
    incoming_symbols = ""
    seen_symbols = "(kicad_symbol_lib (version 20231120))"
    symbol_names_to_footprints: dict[str, str] = {}
    for footprint in contents.footprints:
        name = Path(footprint.name).stem
        symbol_names_to_footprints[name] = name
    if contents.symbols:
        for asset in contents.symbols:
            text = asset.data.decode("utf-8")
            seen_symbols = merge_symbol_libraries(seen_symbols, text)
            incoming_symbols = (
                text
                if not incoming_symbols
                else merge_symbol_libraries(incoming_symbols, text)
            )
        incoming_symbols = rewrite_footprint_properties(
            incoming_symbols, symbol_names_to_footprints
        )

    staged_assets: dict[Path, bytes] = {}
    if contents.models:
        for model in contents.models:
            staged_assets[model_root / part / model.name] = model.data
    symbol_original: bytes | None = None
    if contents.symbols:
        symbol_original = _read_at(project_fd, symbol_path.name)
        if symbol_original is None:
            validate_symbol_inheritance(incoming_symbols)
            combined_symbols = incoming_symbols
        else:
            combined_symbols = merge_symbol_libraries(
                symbol_original.decode("utf-8"), incoming_symbols
            )
            validate_symbol_inheritance(combined_symbols)
        staged_assets[symbol_path] = combined_symbols.encode("utf-8")

    footprint_assets: dict[Path, bytes] = {}
    for asset in contents.footprints:
        target = footprint_path / asset.name
        text = asset.data.decode("utf-8")
        tree = parse(text)
        if len(tree.children) < 2 or tree.children[1].value != Path(asset.name).stem:
            raise ValueError(
                f"Footprint internal name does not match filename: {asset.name}"
            )
        binding = bindings.get(asset.name)
        if binding:
            if any(
                node.value is None
                and node.children
                and node.children[0].value == "model"
                for node in tree.children
            ):
                raise ValueError(f"Footprint already contains a 3D model: {asset.name}")
            model_file = model_root / part / binding
            model_uri = "${KIPRJMOD}/" + model_file.relative_to(project).as_posix()
            text = (
                text[: tree.end - 1]
                + f"\n  (model {quote(model_uri)} (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n"
                + text[tree.end - 1 :]
            )
            source = next(item for item in contents.models if item.name == binding)
            staged_assets[model_file] = source.data
        footprint_assets[target] = text.encode("utf-8")
    staged_assets.update(footprint_assets)

    changed: dict[Path, tuple[bytes | None, bytes, tuple[int, int] | None]] = {}
    created_dirs: list[tuple[int, str]] = []
    directory_fds: dict[Path, int] = {}
    try:
        try:
            filtered_entries: list[tuple[Path, str, str]] = []
            table_snapshots: dict[Path, bytes | None] = {}
            for table_path, table_type, entry in entries:
                table_snapshots[table_path] = _read_at(project_fd, table_path.name)
                current = _table_text_at(project_fd, table_path.name, table_type)
                registered = next(
                    (
                        node
                        for node in parse(current).children
                        if node.value is None
                        and node.children
                        and node.children[0].value == "lib"
                        and field_value(node, "name") == "SnapEDA"
                    ),
                    None,
                )
                if registered is not None:
                    if field_value(registered, "uri") != field_value(
                        parse(entry), "uri"
                    ):
                        raise ValueError("Library nickname already registered: SnapEDA")
                    continue
                filtered_entries.append((table_path, table_type, entry))
            entries = filtered_entries
            if contents.footprints:
                directory_fds[footprint_path], created = _ensure_child_directory(
                    project_fd, footprint_path.name
                )
                if created:
                    created_dirs.append((project_fd, footprint_path.name))
            if contents.models:
                directory_fds[model_root], created = _ensure_child_directory(
                    project_fd, model_root.name
                )
                if created:
                    created_dirs.append((project_fd, model_root.name))
                directory_fds[model_root / part], created = _ensure_child_directory(
                    directory_fds[model_root], part
                )
                if created:
                    created_dirs.append((directory_fds[model_root], part))
            for path, data in staged_assets.items():
                parent_fd = (
                    project_fd if path == symbol_path else directory_fds[path.parent]
                )
                previous = _read_at(parent_fd, path.name)
                if path == symbol_path:
                    if previous != symbol_original:
                        raise RuntimeError(
                            f"SnapEDA asset changed during import: {path.name}"
                        )
                elif previous is not None or any(
                    item.casefold() == path.name.casefold()
                    for item in os.listdir(parent_fd)
                ):
                    raise FileExistsError(
                        f"{'Footprint' if path.parent == footprint_path else '3D model'} already exists in SnapEDA library: {path.name}"
                    )
                changed[path] = (previous, data, None)
                _write_at(
                    parent_fd,
                    path.name,
                    data,
                    replace=path == symbol_path and previous is not None,
                    expected=previous,
                    on_publish=lambda identity, asset_path=path, before=previous, staged=data: changed.__setitem__(
                        asset_path, (before, staged, identity)
                    ),
                )
            if any(
                _read_at(project_fd, path.name) != snapshot
                for path, snapshot in table_snapshots.items()
            ):
                raise RuntimeError("Library table changed during import")
            _commit_tables(
                project, entries, acquire_lock=False, directory_fd=project_fd
            )
        except BaseException as failure:
            rollback_errors: list[BaseException] = []
            for path, (original, staged, identity) in reversed(tuple(changed.items())):
                if identity is None:
                    continue
                parent_fd = (
                    project_fd if path == symbol_path else directory_fds[path.parent]
                )
                try:
                    if _read_at(parent_fd, path.name) != staged:
                        continue
                    if _identity_at(parent_fd, path.name) != identity:
                        continue
                    if original is None:
                        os.unlink(path.name, dir_fd=parent_fd)
                    else:
                        _write_at(
                            parent_fd,
                            path.name,
                            original,
                            replace=True,
                            expected=staged,
                        )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            for parent_fd, name in reversed(created_dirs):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
            if rollback_errors:
                raise BaseExceptionGroup(
                    "Project asset update failed and rollback was incomplete",
                    [failure, *rollback_errors],
                ) from failure
            raise
    finally:
        for descriptor in reversed(tuple(directory_fds.values())):
            os.close(descriptor)

    return ImportedLibraries(
        project,
        footprint_path if contents.footprints else None,
        "SnapEDA" if contents.footprints else None,
        (symbol_path,) if contents.symbols else (),
        ("SnapEDA",) if contents.symbols else (),
    )


def _validate_assets(contents: ArchiveContents) -> None:
    for group in (contents.symbols, contents.footprints, contents.models):
        names: set[str] = set()
        for asset in group:
            if (
                not asset.name
                or asset.name in {".", ".."}
                or "/" in asset.name
                or "\\" in asset.name
                or "\x00" in asset.name
            ):
                raise ValueError(f"Invalid KiCad asset filename: {asset.name!r}")
            key = asset.name.casefold()
            if key in names:
                raise ValueError(f"Duplicate KiCad asset filename: {asset.name}")
            names.add(key)
    for asset in contents.symbols:
        try:
            tree = parse(asset.data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid symbol library file: {asset.name}") from exc
        if not tree.children or tree.children[0].value != "kicad_symbol_lib":
            raise ValueError(f"Invalid symbol library file: {asset.name}")
    for asset in contents.footprints:
        try:
            tree = parse(asset.data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid footprint library file: {asset.name}") from exc
        if not tree.children or tree.children[0].value != "footprint":
            raise ValueError(f"Invalid footprint library file: {asset.name}")
