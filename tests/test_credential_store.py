from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from snapeda_plugin.credential_store import CredentialStore, SERVICE_NAME


class FakeSecretBackend:
    __module__ = "keyring.backends.SecretService"
    priority = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.password: str | None = None

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        return self.password

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        self.password = password


def test_keyring_probe_does_not_read_entries_until_explicit_use() -> None:
    backend = FakeSecretBackend()
    store = CredentialStore(backend)
    assert store.available
    assert backend.calls == []
    assert store.get_password("user@example.test") is None
    store.set_password("user@example.test", "private")
    assert backend.calls == [
        ("get", SERVICE_NAME, "user@example.test"),
        ("set", SERVICE_NAME, "user@example.test"),
    ]


def test_only_positive_priority_allowlisted_platform_backends_are_accepted() -> None:
    class FailBackend:
        __module__ = "keyring.backends.fail"
        priority = 1

    class AlternativeBackend:
        __module__ = "keyrings.alt.file"
        priority = 1

    class DisabledBackend(FakeSecretBackend):
        priority = 0

    assert not CredentialStore(FailBackend()).available
    assert not CredentialStore(AlternativeBackend()).available
    assert not CredentialStore(DisabledBackend()).available
    assert not CredentialStore().available


def test_detect_uses_keyring_backend_without_reading_credentials(
    monkeypatch: Any,
) -> None:
    backend = FakeSecretBackend()
    module = ModuleType("keyring")
    module.get_keyring = lambda: backend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    store = CredentialStore.detect()
    assert store.available
    assert backend.calls == []
