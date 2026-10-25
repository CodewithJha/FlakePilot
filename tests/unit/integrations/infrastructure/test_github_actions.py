"""GitHub Actions history: bounded API reads, one checked redirect, hostile zips."""

import asyncio
import io
import urllib.request
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from actions_support import STORAGE, ActionsApi, FakeRun, FakeTokens, junit, zip_of

from flakepilot.integrations.application.ports import CodeHostError, ReportRejected
from flakepilot.integrations.infrastructure import github_actions
from flakepilot.integrations.infrastructure.github_actions import (
    GitHubActionsHistory,
    RunArtifact,
    extract_reports,
    storage_url_ok,
)

DAY = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
REPORT = junit(("tests.a", "test_one", "passed"))


def _history(api: ActionsApi) -> GitHubActionsHistory:
    return GitHubActionsHistory(FakeTokens(), api)


def test_completed_runs_are_parsed_and_their_url_is_built_not_copied() -> None:
    api = ActionsApi("acme", "web", [FakeRun(7, DAY, {}), FakeRun(8, DAY, {})])
    runs = asyncio.run(_history(api).completed_runs(owner="acme", name="web", limit=2))
    assert [r.id for r in runs] == [7, 8]
    assert runs[0].completed_at == DAY
    assert runs[0].url == "https://github.com/acme/web/actions/runs/7/attempts/1"
    sent = api.requests[0]
    assert sent.get_header("Authorization") == "Bearer ghs_" + "t" * 20
    assert "status=completed" in sent.full_url and "per_page=2" in sent.full_url


def test_runs_in_an_unexpected_shape_are_left_out() -> None:
    def answer(
        request: urllib.request.Request, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        del request, limit
        return (
            200,
            {},
            (
                b'{"workflow_runs": [{"id": 1, "status": "in_progress"},'
                b' {"id": 2, "status": "completed", "run_attempt": 1,'
                b' "head_sha": "not-a-sha", "updated_at": "2026-10-01T00:00:00Z",'
                b' "run_started_at": "2026-10-01T00:00:00Z"}]}'
            ),
        )

    history = GitHubActionsHistory(FakeTokens(), answer)
    assert asyncio.run(history.completed_runs(owner="a", name="b", limit=5)) == ()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"limit": 0}, "invalid_request"),
        ({"limit": 101}, "invalid_request"),
        ({"limit": 1, "workflow": "../ci.yml"}, "invalid_request"),
        ({"limit": 1, "branch": "a..b"}, "invalid_request"),
    ],
)
def test_bad_listing_arguments_never_reach_the_network(
    kwargs: dict[str, object], reason: str
) -> None:
    api = ActionsApi("acme", "web", [])
    with pytest.raises(CodeHostError) as raised:
        asyncio.run(_history(api).completed_runs(owner="acme", name="web", **kwargs))  # type: ignore[arg-type]
    assert raised.value.reason == reason
    assert api.requests == []


def test_a_refused_listing_is_a_short_code() -> None:
    def refused(
        request: urllib.request.Request, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        del request, limit
        return 403, {}, b'{"message": "Resource not accessible by integration"}'

    history = GitHubActionsHistory(FakeTokens(), refused)
    with pytest.raises(CodeHostError) as raised:
        asyncio.run(history.completed_runs(owner="a", name="b", limit=1))
    assert raised.value.reason == "http_403"


def test_download_follows_one_redirect_without_the_bearer() -> None:
    archive = zip_of({"report.xml": REPORT})
    api = ActionsApi("acme", "web", [FakeRun(7, DAY, {"junit": archive})])
    history = _history(api)
    (artifact,) = asyncio.run(history.artifacts(owner="acme", name="web", run_id=7))
    assert artifact.name == "junit" and artifact.size == len(archive)
    body = asyncio.run(history.download(owner="acme", name="web", artifact=artifact))
    assert body == archive
    api_call, storage_call = api.requests[-2:]
    assert api_call.full_url.endswith(f"/actions/artifacts/{artifact.id}/zip")
    assert api_call.get_header("Authorization") is not None
    assert storage_call.full_url.startswith(STORAGE)
    assert storage_call.get_header("Authorization") is None


@pytest.mark.parametrize(
    "location",
    [
        "http://productionresultssa1.blob.core.windows.net/x",
        "https://evil.example/x.zip",
        "https://user@productionresultssa1.blob.core.windows.net/x",
        "https://blob.core.windows.net.evil.example/x",
        "",
    ],
)
def test_a_redirect_off_github_storage_is_refused(location: str) -> None:
    calls: list[str] = []

    def answer(
        request: urllib.request.Request, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        del limit
        calls.append(request.full_url)
        return 302, {"location": location}, b""

    history = GitHubActionsHistory(FakeTokens(), answer)
    with pytest.raises(CodeHostError) as raised:
        asyncio.run(
            history.download(
                owner="a", name="b", artifact=RunArtifact(5, "junit", 10, False)
            )
        )
    assert raised.value.reason == "rejected_redirect"
    assert len(calls) == 1
    assert not storage_url_ok(location)


def test_expired_and_oversized_artifacts_are_not_downloaded() -> None:
    api = ActionsApi("acme", "web", [])
    history = _history(api)
    for artifact, reason in (
        (RunArtifact(1, "junit", 10, True), "artifact_expired"),
        (
            RunArtifact(2, "junit", github_actions.MAX_ARCHIVE_BYTES + 1, False),
            "artifact_too_large",
        ),
    ):
        with pytest.raises(CodeHostError) as raised:
            asyncio.run(history.download(owner="acme", name="web", artifact=artifact))
        assert raised.value.reason == reason
    assert api.requests == []


def test_reports_are_the_xml_entries_in_name_order() -> None:
    archive = zip_of(
        {
            "b/TEST-b.xml": b"<b/>",
            "a/TEST-a.XML": b"<a/>",
            "coverage.txt": b"ignored",
            "dir/": b"",
        }
    )
    assert extract_reports(archive) == (b"<a/>", b"<b/>")


@pytest.mark.parametrize(
    "name",
    ["../escape.xml", "a/../../escape.xml", "/etc/passwd.xml", "C:/x.xml", "a\\b.xml"],
)
def test_a_name_that_could_traverse_refuses_the_whole_archive(name: str) -> None:
    archive = zip_of({"ok.xml": REPORT, name: REPORT})
    with pytest.raises(ReportRejected) as raised:
        extract_reports(archive)
    assert raised.value.reason == "unsafe_entry_name"


def test_a_zip_bomb_is_stopped_by_the_read_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_actions, "MAX_REPORT_BYTES", 1024)
    bomb = zip_of({"bomb.xml": b"0" * (4 * 1024 * 1024)})  # deflates to a few KiB
    assert len(bomb) < 64 * 1024
    with pytest.raises(ReportRejected) as raised:
        extract_reports(bomb)
    assert raised.value.reason == "too_large"


def test_an_understated_entry_size_is_still_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_actions, "MAX_REPORT_BYTES", 1024)
    archive = bytearray(zip_of({"lie.xml": b"x" * 4096}))
    # Rewrite the central directory's uncompressed size (offset 24 in the
    # header) to claim 10 bytes; zipfile trusts it, our read cap does not.
    central = archive.rfind(b"PK\x01\x02")
    archive[central + 24 : central + 28] = (10).to_bytes(4, "little")
    with pytest.raises(ReportRejected) as raised:
        extract_reports(bytes(archive))
    assert raised.value.reason in ("too_large", "malformed_archive")


def test_total_bytes_entries_encryption_and_codecs_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_actions, "MAX_TOTAL_REPORT_BYTES", 2 * len(REPORT))
    many = zip_of({f"r{i}.xml": REPORT for i in range(3)})
    with pytest.raises(ReportRejected) as raised:
        extract_reports(many)
    assert raised.value.reason == "archive_too_large"

    monkeypatch.setattr(github_actions, "MAX_ENTRIES", 2)
    with pytest.raises(ReportRejected) as raised:
        extract_reports(many)
    assert raised.value.reason == "too_many_entries"

    # zipfile clears flag bits when writing: set "encrypted" in both headers.
    encrypted = bytearray(zip_of({"secret.xml": REPORT}))
    encrypted[6] |= 0x1  # local file header, general purpose flags
    central = encrypted.rfind(b"PK\x01\x02")
    encrypted[central + 8] |= 0x1
    with pytest.raises(ReportRejected) as raised:
        extract_reports(bytes(encrypted))
    assert raised.value.reason == "encrypted_entry"

    bzipped = io.BytesIO()
    with zipfile.ZipFile(bzipped, "w", zipfile.ZIP_BZIP2) as bundle:
        bundle.writestr("r.xml", REPORT)
    with pytest.raises(ReportRejected) as raised:
        extract_reports(bzipped.getvalue())
    assert raised.value.reason == "unsupported_compression"


def test_garbage_and_oversized_archives_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ReportRejected) as raised:
        extract_reports(b"PK\x03\x04 not really a zip")
    assert raised.value.reason == "malformed_archive"
    monkeypatch.setattr(github_actions, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ReportRejected) as raised:
        extract_reports(zip_of({"r.xml": REPORT}))
    assert raised.value.reason == "archive_too_large"
