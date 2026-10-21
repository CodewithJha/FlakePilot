"""Encrypted secret store (ADR-009 default; ADR-005 decision 6): the INT secret adapter's
own `int_secrets` schema. Only this adapter reads or writes it.

AES-256-GCM with a random 96-bit nonce per write. The key is held outside the database
(FLAKEPILOT_SECRET_KEY), never given to execution workers. Associated data binds each
ciphertext to its scope (org, repo), name and key version, so a ciphertext moved to another
org, repo or name fails to decrypt. `key_version` allows key rotation later.

`resolve` is for other INT adapters at the point of use; the SecretStore port never
returns values to domain or application code. Errors never contain values.
"""

import json
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import (
    CheckConstraint,
    Column,
    ColumnElement,
    DateTime,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.integrations.application.ports import SecretScope

# Same convention in every context's metadata (tests/architecture checks they match).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
KEY_VERSION = 1
NONCE_BYTES = 12

metadata = MetaData(schema="int_secrets", naming_convention=NAMING_CONVENTION)

secrets_table = Table(
    "secrets",
    metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("org_id", Uuid(as_uuid=False)),
    Column("repo_id", Uuid(as_uuid=False)),
    Column("name", Text, nullable=False),
    Column("key_version", SmallInteger, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("org_id", "repo_id", "name", postgresql_nulls_not_distinct=True),
    CheckConstraint("repo_id IS NULL OR org_id IS NOT NULL", name="repo_needs_org"),
    CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
)


class SecretStoreError(Exception):
    """A secret is missing or failed its integrity check. Never carries a value."""


def _associated_data(key_version: int, scope: SecretScope, name: str) -> bytes:
    fields = ["flakepilot-secret", key_version, scope.org_id, scope.repo_id, name]
    return json.dumps(fields).encode()


def encrypt(key: bytes, scope: SecretScope, name: str, value: str) -> bytes:
    """Returns nonce + ciphertext (with tag) for `KEY_VERSION`."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    data = _associated_data(KEY_VERSION, scope, name)
    return nonce + AESGCM(key).encrypt(nonce, value.encode(), data)


def decrypt(
    key: bytes, key_version: int, scope: SecretScope, name: str, sealed: bytes
) -> str:
    nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    data = _associated_data(key_version, scope, name)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, data).decode()
    except InvalidTag:
        raise SecretStoreError("secret failed its integrity check") from None


Transaction = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class EncryptedSecretStore:
    def __init__(self, transaction: Transaction, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("the secret key must be 32 bytes")
        self._transaction = transaction
        self._key = key

    async def put(self, scope: SecretScope, name: str, value: str) -> str:
        """Create or replace; returns the secret's reference (its ID)."""
        sealed = encrypt(self._key, scope, name, value)
        new_secret = insert(secrets_table).values(
            id=str(uuid.uuid4()),
            org_id=scope.org_id,
            repo_id=scope.repo_id,
            name=name,
            key_version=KEY_VERSION,
            nonce=sealed[:NONCE_BYTES],
            ciphertext=sealed[NONCE_BYTES:],
            created_at=func.now(),
            updated_at=func.now(),
        )
        statement = new_secret.on_conflict_do_update(
            index_elements=["org_id", "repo_id", "name"],
            set_={
                "key_version": new_secret.excluded.key_version,
                "nonce": new_secret.excluded.nonce,
                "ciphertext": new_secret.excluded.ciphertext,
                "updated_at": func.now(),
            },
        ).returning(secrets_table.c.id)
        async with self._transaction() as session:
            reference: str = (await session.execute(statement)).scalar_one()
        return reference

    async def resolve(self, scope: SecretScope, name: str) -> str:
        """For INT adapters only, at the point of use."""
        async with self._transaction() as session:
            found = await session.execute(
                select(
                    secrets_table.c.key_version,
                    secrets_table.c.nonce,
                    secrets_table.c.ciphertext,
                ).where(*_matches(scope, name))
            )
            row = found.first()
        if row is None:
            raise SecretStoreError("secret not found")
        if row.key_version != KEY_VERSION:
            raise SecretStoreError("secret uses an unknown key version")
        return decrypt(
            self._key, row.key_version, scope, name, row.nonce + row.ciphertext
        )

    async def delete(self, scope: SecretScope, name: str) -> None:
        async with self._transaction() as session:
            await session.execute(delete(secrets_table).where(*_matches(scope, name)))


def _matches(scope: SecretScope, name: str) -> list[ColumnElement[bool]]:
    return [
        secrets_table.c.org_id.is_not_distinct_from(scope.org_id),
        secrets_table.c.repo_id.is_not_distinct_from(scope.repo_id),
        secrets_table.c.name == name,
    ]
