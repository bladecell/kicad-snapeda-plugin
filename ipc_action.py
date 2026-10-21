"""KiCad IPC action that starts the native search window."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def usable_python(executable: str) -> bool:
    """Check that an interpreter has wxPython and the active HTTP client."""
    probe = subprocess.run(
        [executable, "-c", "import wx, curl_cffi"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )
    return probe.returncode == 0


def main() -> None:
    root = Path(__file__).resolve().parent
    configured = os.environ.get("SNAPEDA_KICAD_PYTHON")
    candidates = [
        configured,
        sys.executable,
        shutil.which("python3"),
        "/usr/bin/python3",
    ]
    interpreter = next(
        (item for item in dict.fromkeys(candidates) if item and usable_python(item)),
        None,
    )
    if interpreter is None:
        raise RuntimeError(
            "No Python interpreter with wxPython and curl_cffi was found. Set SNAPEDA_KICAD_PYTHON."
        )
    subprocess.Popen(
        [interpreter, str(root / "launch.py")],
        cwd=root,
        start_new_session=True,
        close_fds=True,
    )


if __name__ == "__main__":
    main()
