"""MongoDB Atlas as a write-only copy of finished run reports (goal 4; ADR-017).

PostgreSQL stays the system of record (ADR-005). This adapter only upserts
documents into one fixed collection, keyed by `_id`, with `$setOnInsert`: a
document that already exists is never modified, so a re-export is a no-op.
Nothing in FlakePilot reads a document back to decide anything.

Built only by the `export-reports-atlas` operator command, never by the API
process or the worker; the driver is imported only when the adapter is built,
so loading settings never loads it. TLS is always on (a URI that turns it off
or weakens certificate checks is refused at settings time and here), and
server selection, connect and socket waits are bounded. Failures are
`ReportSinkError` short codes; driver messages can carry host names, so they
are never kept.
"""

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from flakepilot.integrations.application.ports import ReportSinkError

if TYPE_CHECKING:
    from pymongo.errors import PyMongoError

__all__ = ["AtlasReportSink", "ReportSinkError", "database_ok", "uri_ok"]

COLLECTION = "run_reports"
SERVER_SELECTION_MS = 10_000
CONNECT_MS = 10_000
SOCKET_MS = 20_000
MAX_URI_CHARS = 2_048
_DATABASE = re.compile(r"[A-Za-z0-9_-]{1,63}")
_WEAKENING = frozenset(
    {
        "tlsinsecure",
        "tlsallowinvalidcertificates",
        "tlsallowinvalidhostnames",
        "tlsdisableocspendpointcheck",
    }
)
_AUTH_CODES = frozenset({13, 18, 8000})  # Unauthorized, AuthenticationFailed, Atlas


def uri_ok(uri: str) -> bool:
    """A `mongodb+srv://` or `mongodb://` URI with a host that leaves TLS on."""
    if not 1 <= len(uri) <= MAX_URI_CHARS or not uri.isprintable() or " " in uri:
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme not in ("mongodb+srv", "mongodb") or not parts.netloc:
        return False
    host = parts.netloc.rpartition("@")[2]
    if not host or host.startswith(":"):
        return False
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        option = name.lower()
        if option in _WEAKENING:
            return False
        if option in ("tls", "ssl") and value.lower() != "true":
            return False
    return True


def database_ok(name: str) -> bool:
    return _DATABASE.fullmatch(name) is not None


def _reason(error: "PyMongoError") -> str:
    from pymongo.errors import (
        ConfigurationError,
        NetworkTimeout,
        OperationFailure,
        ServerSelectionTimeoutError,
    )

    if isinstance(error, ServerSelectionTimeoutError):
        return "atlas_unreachable"
    if isinstance(error, NetworkTimeout):
        return "atlas_timeout"
    if isinstance(error, ConfigurationError):
        return "atlas_misconfigured"
    if isinstance(error, OperationFailure):
        return "atlas_unauthorized" if error.code in _AUTH_CODES else "atlas_refused"
    return "atlas_error"


class AtlasReportSink:
    """One client per operator command; `close()` it when the batch ends."""

    def __init__(self, *, uri: str, database: str, collection: str = COLLECTION):
        from pymongo import AsyncMongoClient
        from pymongo.errors import PyMongoError

        if not uri_ok(uri) or not database_ok(database):
            raise ReportSinkError("atlas_misconfigured")
        try:
            self._client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
                uri,
                tls=True,
                serverSelectionTimeoutMS=SERVER_SELECTION_MS,
                connectTimeoutMS=CONNECT_MS,
                socketTimeoutMS=SOCKET_MS,
                appname="flakepilot-export",
                connect=False,
            )
        except PyMongoError as error:
            raise ReportSinkError(_reason(error)) from None
        self._collection = self._client[database][collection]

    async def upsert(self, key: str, document: Mapping[str, object]) -> bool:
        from pymongo.errors import DuplicateKeyError, PyMongoError

        try:
            result = await self._collection.update_one(
                {"_id": key},
                {"$setOnInsert": dict(document)},
                upsert=True,
            )
        except DuplicateKeyError:
            return False  # a concurrent export inserted the same key first
        except PyMongoError as error:
            raise ReportSinkError(_reason(error)) from None
        return result.upserted_id is not None

    async def close(self) -> None:
        await self._client.close()
