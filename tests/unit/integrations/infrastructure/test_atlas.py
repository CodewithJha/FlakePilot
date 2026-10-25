"""Atlas adapter (goal 4): TLS-only URIs, bounded client, upsert semantics, short codes."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from flakepilot.integrations.infrastructure import atlas
from flakepilot.integrations.infrastructure.atlas import (
    AtlasReportSink,
    ReportSinkError,
    database_ok,
    uri_ok,
)

URI = "mongodb+srv://usr_x1:pw_Q7z@cluster0.example.mongodb.net/?retryWrites=true"


@pytest.mark.parametrize(
    "uri",
    [
        URI,
        "mongodb://usr:pw@host-1.example.net:27017,host-2.example.net:27017/?tls=true",
    ],
)
def test_tls_uris_are_accepted(uri: str) -> None:
    assert uri_ok(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "https://cluster0.example.mongodb.net",
        "mongodb+srv://",
        URI + "&tls=false",
        URI + "&ssl=false",
        URI + "&tlsInsecure=true",
        URI + "&tlsAllowInvalidCertificates=true",
        URI + "&tlsAllowInvalidHostnames=true",
        URI + " spaced",
        "mongodb+srv://" + "a" * 2100,
    ],
)
def test_uris_that_weaken_tls_or_are_malformed_are_refused(uri: str) -> None:
    assert not uri_ok(uri)


def test_database_names_are_short_codes() -> None:
    assert database_ok("flakepilot_reports")
    assert not database_ok("bad name")
    assert not database_ok("a" * 64)


class FakeCollection:
    def __init__(self, error: Exception | None = None) -> None:
        self.keys: set[str] = set()
        self.calls: list[tuple[Any, Any, bool]] = []
        self.error = error

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool
    ) -> SimpleNamespace:
        self.calls.append((query, update, upsert))
        if self.error is not None:
            raise self.error
        if query["_id"] in self.keys:
            return SimpleNamespace(upserted_id=None)
        self.keys.add(query["_id"])
        return SimpleNamespace(upserted_id=query["_id"])


def _sink(collection: FakeCollection) -> AtlasReportSink:
    sink = AtlasReportSink(uri=URI, database="flakepilot")
    sink._collection = collection  # type: ignore[assignment]
    return sink


def test_the_client_is_lazy_tls_and_bounded() -> None:
    sink = AtlasReportSink(uri=URI, database="flakepilot")
    options = sink._client.options
    assert options.server_selection_timeout == atlas.SERVER_SELECTION_MS / 1000
    assert options.pool_options.connect_timeout == atlas.CONNECT_MS / 1000
    assert options.pool_options.socket_timeout == atlas.SOCKET_MS / 1000
    assert options._options["tls"] is True
    asyncio.run(sink.close())


def test_upsert_writes_once_with_set_on_insert() -> None:
    collection = FakeCollection()
    sink = _sink(collection)
    assert asyncio.run(sink.upsert("k1", {"run_id": "r"})) is True
    assert asyncio.run(sink.upsert("k1", {"run_id": "r"})) is False
    query, update, upsert = collection.calls[0]
    assert query == {"_id": "k1"}
    assert update == {"$setOnInsert": {"run_id": "r"}}
    assert upsert is True


def test_a_concurrent_insert_of_the_same_key_is_unchanged() -> None:
    sink = _sink(FakeCollection(DuplicateKeyError("E11000 host.example.net")))
    assert asyncio.run(sink.upsert("k1", {})) is False


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ServerSelectionTimeoutError("cluster0.example.net:27017 timed out"),
            "atlas_unreachable",
        ),
        (OperationFailure("bad auth host.example.net", code=18), "atlas_unauthorized"),
        (OperationFailure("not allowed", code=8000), "atlas_unauthorized"),
        (OperationFailure("other", code=2), "atlas_refused"),
        (PyMongoError("host.example.net"), "atlas_error"),
    ],
)
def test_driver_errors_become_short_codes_without_text(
    error: Exception, reason: str
) -> None:
    with pytest.raises(ReportSinkError) as raised:
        asyncio.run(_sink(FakeCollection(error)).upsert("k1", {}))
    assert raised.value.reason == reason
    assert str(raised.value) == reason
    assert raised.value.__cause__ is None


def test_a_misconfigured_sink_is_refused_before_any_client() -> None:
    with pytest.raises(ReportSinkError, match="atlas_misconfigured"):
        AtlasReportSink(uri=URI + "&tls=false", database="flakepilot")
