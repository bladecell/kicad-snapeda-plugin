from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import ipc_action
import pytest


def test_interpreter_probe_requires_curl_cffi_not_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(argv)
        command = argv[-1]
        imports_curl_cffi = "curl_cffi" in command
        imports_requests = "requests" in command
        # This interpreter has wx and requests, but not curl_cffi.
        return SimpleNamespace(
            returncode=0 if imports_requests and not imports_curl_cffi else 1
        )

    monkeypatch.setattr(ipc_action.subprocess, "run", run)
    assert not ipc_action.usable_python("/fake/python")
    assert commands[0][:2] == ["/fake/python", "-c"]
    assert "import wx, curl_cffi" == commands[0][2]

    def all_required_modules_available(
        argv: list[str], **_kwargs: object
    ) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ipc_action.subprocess, "run", all_required_modules_available)
    assert ipc_action.usable_python("/fake/python")


def test_ipc_action_starts_native_launcher_from_plugin_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(ipc_action, "usable_python", lambda _path: True)
    monkeypatch.setattr(ipc_action.shutil, "which", lambda _name: "/usr/bin/python3")
    monkeypatch.setattr(
        ipc_action.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    monkeypatch.setattr(ipc_action.sys, "executable", "/plugin/venv/bin/python")

    ipc_action.main()

    argv, kwargs = calls[0]
    assert argv[1] == str(Path(ipc_action.__file__).resolve().parent / "launch.py")
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == Path(ipc_action.__file__).resolve().parent
