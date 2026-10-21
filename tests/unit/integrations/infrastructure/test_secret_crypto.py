from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.integrations.application.ports import SecretScope
from flakepilot.integrations.infrastructure.secret_store import (
    KEY_VERSION,
    EncryptedSecretStore,
    SecretStoreError,
    decrypt,
    encrypt,
)

KEY = bytes(range(32))
VALUE = "plaintext-SENTINEL-55"
ORG_A = SecretScope("org-a")


def test_round_trip_hides_the_plaintext_and_uses_fresh_nonces() -> None:
    sealed = encrypt(KEY, ORG_A, "name", VALUE)
    assert VALUE.encode() not in sealed
    assert decrypt(KEY, KEY_VERSION, ORG_A, "name", sealed) == VALUE
    assert encrypt(KEY, ORG_A, "name", VALUE)[:12] != sealed[:12]


def flip(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 1]) + data[index + 1 :]


@pytest.mark.parametrize(
    "tamper",
    [
        {"key": bytes(32)},
        {"scope": SecretScope("org-b")},
        {"scope": SecretScope("org-a", "repo-1")},
        {"scope": SecretScope()},
        {"name": "other"},
        {"key_version": KEY_VERSION + 1},
        {"flip": 0},  # nonce
        {"flip": 20},  # ciphertext
        {"flip": -1},  # tag
    ],
)
def test_any_mismatch_or_tampering_fails_closed(tamper: dict[str, object]) -> None:
    sealed = encrypt(KEY, ORG_A, "name", VALUE)
    if isinstance(tamper.get("flip"), int):
        index = tamper["flip"]
        assert isinstance(index, int)
        sealed = flip(sealed, index % len(sealed))
    key = tamper.get("key", KEY)
    scope = tamper.get("scope", ORG_A)
    name = tamper.get("name", "name")
    key_version = tamper.get("key_version", KEY_VERSION)
    assert isinstance(key, bytes) and isinstance(scope, SecretScope)
    assert isinstance(name, str) and isinstance(key_version, int)
    with pytest.raises(SecretStoreError) as excinfo:
        decrypt(key, key_version, scope, name, sealed)
    assert VALUE not in str(excinfo.value)


def test_keys_must_be_32_bytes_and_repo_scopes_need_an_org() -> None:
    def no_transaction() -> AbstractAsyncContextManager[AsyncSession]:
        raise AssertionError("not used")

    with pytest.raises(ValueError):
        EncryptedSecretStore(no_transaction, b"short")
    with pytest.raises(ValueError):
        SecretScope(repo_id="repo-1")
