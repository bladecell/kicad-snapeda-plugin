"""Optional password persistence through a recognized operating-system keyring."""

from __future__ import annotations

import warnings
from typing import Any

SERVICE_NAME = "snapeda-kicad-plugin"
_TRUSTED_BACKEND_MODULES = {
    "keyring.backends.SecretService",
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.kwallet",
}


class CredentialStore:
    """Use only an available platform keyring; never fall back to files."""

    def __init__(self, backend: Any | None = None) -> None:
        self._backend = backend if _is_trusted_backend(backend) else None

    @classmethod
    def detect(cls) -> CredentialStore:
        try:
            import keyring

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                backend = keyring.get_keyring()
        except Exception:
            return cls()
        return cls(backend)

    @property
    def available(self) -> bool:
        return self._backend is not None

    def disable(self) -> None:
        self._backend = None

    def get_password(self, username: str) -> str | None:
        if self._backend is None:
            raise RuntimeError("No secure OS keyring is available")
        return self._backend.get_password(SERVICE_NAME, username)

    def set_password(self, username: str, password: str) -> None:
        if self._backend is None:
            raise RuntimeError("No secure OS keyring is available")
        self._backend.set_password(SERVICE_NAME, username, password)


def _is_trusted_backend(backend: Any | None) -> bool:
    if backend is None:
        return False
    try:
        module = type(backend).__module__
        priority = backend.priority
    except Exception:
        return False
    trusted = any(
        module == known or module.startswith(f"{known}.")
        for known in _TRUSTED_BACKEND_MODULES
    )
    return trusted and isinstance(priority, (int, float)) and priority > 0
