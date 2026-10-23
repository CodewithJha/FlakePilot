"""The evidence report tool is honest, bounded, and never promotes itself.

These tests exercise schema version 1 through the loader used by the other
`ops/isolation` modules. Nothing here passes an isolation check: a report
from this tool is a measurement document, not a production attestation.
"""

import dataclasses
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from flakepilot.execution.domain.capability import _FIELDS
from flakepilot.execution.domain.evidence import IsolationChecks

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "ops" / "isolation" / "evidence_report.py"
COLLECT = ROOT / "ops" / "isolation" / "collect_report.py"

_SystemCall = Callable[[list[str], float], tuple[int, str]]
_Report = dict[str, object]


class _Tools(Protocol):
    CHECK_FIELDS: tuple[str, ...]
    FIELD_TO_CONTROLS: dict[str, tuple[str, ...]]
    MECHANISM_LEVEL: dict[str, int]
    _SECRET_PATTERNS: tuple[str, ...]

    def derive_level(self, mechanism: str) -> str: ...

    def parse_result_lines(self, text: str) -> dict[str, str]: ...

    def control_observations(
        self, results: dict[str, str], *, simulated: bool, measured_from: str
    ) -> list[dict[str, object]]: ...

    def build_report(
        self,
        identity: dict[str, object],
        measurements: list[dict[str, object]],
        *,
        collected_at: str,
        image_digest: str = "",
    ) -> _Report: ...

    def validate_report(self, report: object) -> None: ...

    def report_sha256(self, report: _Report) -> str: ...

    def identity_fingerprint(self, identity: dict[str, object]) -> str: ...

    def is_current(self, stored: object, identity: dict[str, object]) -> bool: ...


class _Discover(Protocol):
    def identity_facts(
        self, execute: _SystemCall | None = ...
    ) -> dict[str, object]: ...

    def runner(self, argv: list[str], timeout: float) -> tuple[int, str]: ...


def _tools() -> _Tools:
    spec = importlib.util.spec_from_file_location("evidence_report", REPORT)
    if spec is None or spec.loader is None:
        raise RuntimeError("report module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_Tools, module)


def _collect() -> _Discover:
    spec = importlib.util.spec_from_file_location("collect_report", COLLECT)
    if spec is None or spec.loader is None:
        raise RuntimeError("collect module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_Discover, module)


def _identity(mechanism: str = "mac_docker_desktop") -> dict[str, object]:
    return _IDENTITY | {"mechanism": mechanism}


_IDENTITY: dict[str, object] = {
    "mechanism": "mac_docker_desktop",
    "os": "Darwin 27.0.1",
    "arch": "arm64",
    "kernel": "27.0.0",
    "runtime": "docker",
    "runtime_version": "29.5.3",
}


def _full_results() -> str:
    return (
        "RESULT net_1_1_1_1=blocked errno=111\n"
        "RESULT net_8_8_8_8=blocked errno=113\n"
        "RESULT net_lo=blocked errno=111\n"
        "RESULT meta_imds=blocked errno=113\n"
        "RESULT meta_azure=blocked errno=113\n"
        "RESULT control_plane=blocked errno=113\n"
        "RESULT bad=none\n"
        "RESULT leak=none\n"
        "RESULT keys=FORK\n"
        "RESULT sock_docker=0\n"
        "RESULT sock_run=0\n"
        "RESULT sock_containerd=0\n"
        "RESULT host_ledger_key=absent\n"
        "RESULT marker_exists=0 marker_content=absent\n"
        "RESULT docker_dir=0\n"
        "RESULT graph_path=0\n"
        "RESULT uid=65534\n"
        "RESULT gid=65534\n"
        "RESULT capeff=0000000000000000\n"
        "RESULT nonewprivs=1\n"
        "RESULT setuid_errno=1\n"
        "RESULT mount=errno_1\n"
        "RESULT mntns=errno_1\n"
        "RESULT userns=errno_1\n"
        "RESULT cpu_max=100000_100000\n"
        "RESULT cpu_quota=100000\n"
        "RESULT memory_oom=cgroup_oom\n"
        "RESULT children=14 stop_errno=11\n"
        "RESULT disk_errno=28 written=65536\n"
        "RESULT output_limit=capped\n"
        "RESULT wall_limit=capped\n"
        "RESULT cleanup_pass2_new=0\n"
        "RESULT own=bytes_16\n"
        "RESULT other=errno_13\n"
        "RESULT host=errno_13\n"
        "RESULT artifact_isolation=isolated\n"
        "RESULT image_provenance=digest_pinned\n"
        "RESULT audit_recovery=verified\n"
    )


def _checks(report: _Report) -> list[dict[str, str]]:
    records = report["checks"]
    assert isinstance(records, list)
    return cast("list[dict[str, str]]", records)


def _statuses(report: _Report) -> dict[str, str]:
    return {str(item["field"]): str(item["status"]) for item in _checks(report)}


def _summary(report: _Report) -> dict[str, int]:
    counts = report["summary"]
    assert isinstance(counts, dict)
    return cast("dict[str, int]", counts)


def _measurements(report: _Report) -> list[dict[str, object]]:
    items = report["measurements"]
    assert isinstance(items, list)
    return cast("list[dict[str, object]]", items)


def _facts_of(discovered: dict[str, object]) -> dict[str, str]:
    facts = discovered["facts"]
    assert isinstance(facts, dict)
    return cast("dict[str, str]", facts)


def _identity_of(discovered: dict[str, object]) -> dict[str, str]:
    identity = discovered["identity"]
    assert isinstance(identity, dict)
    return cast("dict[str, str]", identity)


def _observed(tools: _Tools, text: str, **flags: object) -> list[dict[str, object]]:
    results = tools.parse_result_lines(text)
    return tools.control_observations(
        results,
        simulated=bool(flags.get("simulated", False)),
        measured_from=str(flags.get("measured_from", "guest")),
    )


def test_the_mapping_matches_the_domain() -> None:
    tools = _tools()
    assert tools.FIELD_TO_CONTROLS == _FIELDS
    assert set(tools.CHECK_FIELDS) == {
        field.name for field in dataclasses.fields(IsolationChecks)
    }


def test_no_mechanism_derives_production_approved() -> None:
    tools = _tools()
    for mechanism in tools.MECHANISM_LEVEL:
        assert tools.derive_level(mechanism) != "production_approved"
    assert tools.derive_level("mac_docker_desktop") == "shared_kernel_container"
    assert tools.derive_level("in_process") == "simulation"


def test_a_full_run_builds_and_only_reports_what_it_saw() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity(),
        _observed(tools, _full_results()),
        collected_at="2026-10-23T06:00:00Z",
        image_digest="sha256:" + "a" * 64,
    )
    assert report["evidence_level"] == "shared_kernel_container"
    assert _summary(report)["NOT_MEASURED"] == 0
    assert _summary(report)["unmeasured_controls"] == 0
    assert set(_statuses(report).values()) == {"PASS"}


def test_missing_measurements_fail_closed() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity(),
        _observed(tools, ""),
        collected_at="2026-10-23T06:00:00Z",
    )
    assert set(_statuses(report).values()) == {"NOT_MEASURED"}
    assert _summary(report)["PASS"] == 0


def test_partial_results_keep_the_missing_field_unmeasured() -> None:
    tools = _tools()
    text = "RESULT net_1_1_1_1=blocked errno=111\nRESULT meta_imds=blocked errno=113\n"
    report = tools.build_report(
        _identity(), _observed(tools, text), collected_at="2026-10-23T06:00:00Z"
    )
    statuses = _statuses(report)
    assert statuses["network_denied"] == "NOT_MEASURED"
    assert statuses["metadata_denied"] == "NOT_MEASURED"


def test_an_observed_failure_is_fail_not_error() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity(),
        _observed(tools, "RESULT net_1_1_1_1=open errno=0\n"),
        collected_at="2026-10-23T06:00:00Z",
    )
    assert _statuses(report)["network_denied"] == "FAIL"


def test_a_simulated_mechanism_never_passes_a_check() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity("in_process"),
        _observed(tools, _full_results(), simulated=True),
        collected_at="2026-10-23T06:00:00Z",
    )
    assert report["evidence_level"] == "simulation"
    assert set(_statuses(report).values()) == {"ERROR"}


def test_a_simulated_observed_entry_is_refused() -> None:
    tools = _tools()
    poisoned = [
        dict(item, simulated=True) if item["control"] == "cpu_limit" else item
        for item in _observed(tools, "RESULT cpu_max=100000_100000\n")
    ]
    _refused(
        tools.build_report, _identity(), poisoned, collected_at="2026-10-23T06:00:00Z"
    )


def test_the_level_is_derived_and_cannot_be_claimed() -> None:
    tools = _tools()
    report = tools.build_report(_identity(), [], collected_at="2026-10-23T06:00:00Z")
    assert report["evidence_level"] == tools.derive_level("mac_docker_desktop")
    forged = dict(report, evidence_level="production_approved")
    try:
        tools.validate_report(forged)
    except ValueError:
        return
    raise AssertionError("production_approved accepted")


def test_tampered_checks_are_refused() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity(),
        _observed(tools, "RESULT cpu_max=100000_100000\n"),
        collected_at="2026-10-23T06:00:00Z",
    )
    assert _statuses(report)["limits_enforced"] == "NOT_MEASURED"
    forged = [dict(item, status="PASS") for item in _checks(report)]
    try:
        tools.validate_report(dict(report, checks=forged))
    except ValueError:
        return
    raise AssertionError("tampered checks accepted")


def test_the_hash_follows_the_bytes() -> None:
    tools = _tools()
    report = tools.build_report(_identity(), [], collected_at="2026-10-23T06:00:00Z")
    stable = tools.report_sha256(report)
    assert tools.report_sha256(report) == stable
    tampered = dict(report, collected_at="2026-10-23T06:00:01Z")
    assert tools.report_sha256(tampered) != stable


def test_an_identity_change_requires_reverification() -> None:
    tools = _tools()
    stored = tools.identity_fingerprint(_IDENTITY)
    assert tools.is_current(stored, _IDENTITY)
    assert not tools.is_current(stored, dict(_IDENTITY, kernel="6.12.76"))
    assert not tools.is_current(None, _IDENTITY)
    assert not tools.is_current("not-a-digest", _IDENTITY)


def test_a_malformed_identity_is_refused() -> None:
    tools = _tools()
    bad_identities: tuple[dict[str, object], ...] = (
        {},
        {"mechanism": "mac_docker_desktop"},
        dict(_IDENTITY, mechanism="not_a_mechanism"),
        dict(_IDENTITY, kernel=""),
        dict(_IDENTITY, extra="x"),
    )
    for identity in bad_identities:
        try:
            tools.build_report(identity, [], collected_at="2026-10-23T06:00:00Z")
        except ValueError:
            continue
        raise AssertionError(f"accepted {identity}")


def test_oversized_probe_output_is_refused() -> None:
    tools = _tools()
    text = "\n".join(f"RESULT uid_{index}=1" for index in range(600))
    try:
        tools.parse_result_lines(text)
    except ValueError:
        return
    raise AssertionError("oversized result accepted")


def test_a_secret_shaped_detail_is_refused() -> None:
    tools = _tools()
    poisoned = [
        dict(item, detail="collector leaked ghs_abc123DEFGHIJ")
        if item["control"] == "cpu_limit"
        else item
        for item in _observed(tools, _full_results())
    ]
    _refused(
        tools.build_report, _identity(), poisoned, collected_at="2026-10-23T06:00:00Z"
    )


def test_environment_names_are_counted_and_never_reported() -> None:
    tools = _tools()
    report = tools.build_report(
        _identity(),
        _observed(tools, "RESULT bad=DATABASE_URL\nRESULT leak=none\n"),
        collected_at="2026-10-23T06:00:00Z",
    )
    rendered = json.dumps(_measurements(report))
    assert "DATABASE_URL" not in rendered
    assert _statuses(report)["credentials_absent"] == "FAIL"


def test_a_failing_identity_query_is_error_never_a_fact() -> None:
    collect = _collect()
    discovered = collect.identity_facts(_broken)
    facts = _facts_of(discovered)
    assert facts["os_status"] == "ERROR"
    identity = _identity_of(discovered)
    assert identity["runtime"] == ""
    assert identity["os"] == ""


def test_the_allowlisted_runner_bounds_output() -> None:
    collect = _collect()
    code, text = collect.runner(["python3", "-c", "print('x' * 10000)"], 10.0)
    assert code == 0
    assert len(text) <= 4096


def test_the_runner_reports_a_missing_tool_as_error() -> None:
    collect = _collect()
    discovered = collect.identity_facts()
    facts = _facts_of(discovered)
    assert facts["os_status"] == "PASS"
    assert facts["docker_version_status"] in ("PASS", "ERROR")


def test_the_probes_never_receive_environment_knowledge() -> None:
    tools = _tools()
    text = (Path(REPORT).read_text() + Path(COLLECT).read_text()).lower()
    for banned in ("os.environ", "getenv", "github_token", "flakepilot_"):
        assert banned not in text
    assert "ghs_" in tools._SECRET_PATTERNS
    assert "postgresql://" in tools._SECRET_PATTERNS


def _broken(argv: list[str], timeout: float) -> tuple[int, str]:
    del argv, timeout
    return 127, ""


Build = Callable[..., object]


def _refused(
    build: Build,
    identity: dict[str, object],
    poisoned: list[dict[str, object]],
    *,
    collected_at: str,
) -> None:
    try:
        build(identity, poisoned, collected_at=collected_at)
    except ValueError:
        return
    raise AssertionError("refusable input accepted")
