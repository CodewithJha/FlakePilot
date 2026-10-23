"""Structured evidence reports for isolation probes.

Schema version 1. The document records what one probe section measured on an
explicitly identified execution environment: what was observed, where it was
observed from, what was not measured, and what failed. It never executes
untrusted project code, never reads environment variables, and never stores
credentials, tokens, database URLs, or artifact bytes. A secret-shaped value
in any detail rejects the document. Only booleans, counts, and short derived
details are stored.

The report speaks the existing evidence ladder. The evidence level is derived
from the execution mechanism, and a caller cannot supply a level: Docker
Desktop on macOS, a GitHub hosted runner, and an in-process simulation cannot
claim `per_run_microvm`, and no mechanism constructs `production_approved`.
Only an independent human review of a whole bundle can confer that level, and
that review is not this module.

Statuses are `PASS`, `FAIL`, `NOT_MEASURED`, `ERROR`. A missing result, a
timeout, silence, or a tooling failure is never a pass. A check reaches
`PASS` only when every contributing control was observed inside the guest,
unsimulated, at the report's level. Any simulated or host-side observation
makes the check `ERROR`. Any observed failure is `FAIL`. A control the probe
did not answer leaves the check `NOT_MEASURED`.

This module is measurement tooling. It is not production attestation, and the
product worker does not import it. `IsolationChecks` stay false until an
independently reviewed bundle exists; writing a report here does not change
the control plane.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import fields

from flakepilot.execution.domain.capability import CONTROLS, LEVELS
from flakepilot.execution.domain.evidence import IsolationChecks

__all__ = [
    "CHECK_FIELDS",
    "FIELD_TO_CONTROLS",
    "MECHANISM_LEVEL",
    "SCHEMA_VERSION",
    "STATUSES",
    "build_report",
    "derive_level",
    "identity_fingerprint",
    "is_current",
    "parse_result_lines",
    "report_sha256",
    "validate_report",
]

SCHEMA_VERSION = 1
STATUSES = ("PASS", "FAIL", "NOT_MEASURED", "ERROR")
MEASURED_FROM = ("guest", "host")

CHECK_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(IsolationChecks))

# The seven product fields and the controls that contribute to each. A test
# asserts that this table equals the domain's own mapping, so the tool cannot
# drift away from the model.
FIELD_TO_CONTROLS: dict[str, tuple[str, ...]] = {
    "network_denied": ("network_egress_denied",),
    "metadata_denied": (
        "metadata_endpoint_denied",
        "control_plane_endpoint_denied",
    ),
    "host_mounts_denied": ("host_mounts_denied", "filesystem_scoped"),
    "credentials_absent": (
        "database_url_absent",
        "session_secret_absent",
        "github_app_key_absent",
        "host_ledger_key_absent",
    ),
    "unprivileged": ("unprivileged",),
    "limits_enforced": (
        "cpu_limit",
        "memory_limit",
        "pid_limit",
        "disk_limit",
        "output_limit",
        "wall_clock_limit",
    ),
    "cleanup_verified": (
        "host_cleanup",
        "artifact_isolation",
        "image_provenance",
        "audit_recovery",
    ),
}

# Strongest level a mechanism can support. The collector derives the report
# level from the mechanism, so no caller label can promote the result.
MECHANISM_LEVEL: dict[str, int] = {
    "in_process": 0,
    "github_hosted_runner_docker": 1,
    "mac_docker_desktop": 1,
    "linux_host_docker": 2,
    "per_run_microvm": 3,
}

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CAP_ZERO = "0000000000000000"
_DISK_TARGET = 2 * 1024 * 1024

_MAX_IDENTITY_VALUE = 200
_MAX_RESULT_KEY = 64
_MAX_RESULT_VALUE = 200
_MAX_RESULT_KEYS = 512
_MAX_MEASUREMENTS = len(CONTROLS) * 2
_MAX_IMPORT_LINES = 4096
_MAX_DETAIL = 300
_MAX_METHOD = 60

_SECRET_PATTERNS = (
    "ghs_",
    "ghp_",
    "github_pat_",
    "AKIA",
    "sk-",
    "fpi_",
    "Bearer ",
    "postgresql://",
    "postgres://",
    "mysql://",
    "mongodb://",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
)

Measurement = dict[str, object]
Measurements = dict[str, Measurement]

Required = dict[str, tuple[str, ...]]

_REQUIRED_KEYS: Required = {
    "database_url_absent": ("bad", "leak"),
    "session_secret_absent": ("bad", "leak"),
    "github_app_key_absent": ("bad", "leak"),
    "host_ledger_key_absent": ("host_ledger_key",),
    "network_egress_denied": ("net_1_1_1_1", "net_8_8_8_8"),
    "metadata_endpoint_denied": ("meta_imds", "meta_azure"),
    "control_plane_endpoint_denied": ("control_plane",),
    "cpu_limit": ("cpu_max",),
    "memory_limit": ("memory_oom",),
    "pid_limit": ("children", "stop_errno"),
    "disk_limit": ("disk_errno", "written"),
    "output_limit": ("output_limit",),
    "wall_clock_limit": ("wall_limit",),
    "host_cleanup": ("cleanup_pass2_new",),
    "filesystem_scoped": ("own", "other", "host"),
    "artifact_isolation": ("artifact_isolation",),
    "image_provenance": ("image_provenance",),
    "audit_recovery": ("audit_recovery",),
    "unprivileged": ("uid", "setuid_errno", "nonewprivs", "mount", "capeff"),
    "host_mounts_denied": (
        "sock_docker",
        "sock_run",
        "docker_dir",
        "graph_path",
        "marker_content",
    ),
}


def derive_level(mechanism: str) -> str:
    """Level for a known mechanism; an unknown mechanism is refused.

    The level always equals the strongest level the mechanism can support.
    Docker Desktop on macOS and a GitHub hosted runner are
    `shared_kernel_container`. The map never yields `production_approved`.
    """
    index = MECHANISM_LEVEL.get(mechanism)
    if index is None:
        raise ValueError("rejected")
    return LEVELS[index]


def _secret_free(text: str) -> bool:
    return not any(pattern in text for pattern in _SECRET_PATTERNS)


def parse_result_lines(text: str) -> dict[str, str]:
    """`RESULT` lines from one probe section, in the harness format.

    The format is `_fields` from the Mac driver: whitespace-split tokens,
    `key=value`, last key wins. Unknown probe keys are accepted and simply
    not used by any rule, so the collector accepts the existing probe
    outputs unchanged. Keys are bounded; oversized or empty values are
    rejected. Raw values never enter the report.
    """
    results: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("RESULT "):
            continue
        for part in line.split()[1:]:
            key, separator, value = part.partition("=")
            if not separator or not _KEY_PATTERN.match(key):
                raise ValueError("rejected")
            if not value or len(value) > _MAX_RESULT_VALUE:
                raise ValueError("rejected")
            results[key] = value
            if len(results) > _MAX_RESULT_KEYS:
                raise ValueError("rejected")
    return results


def _number(value: str) -> int | None:
    try:
        return int(value, 10)
    except ValueError:
        return None


def _flag(value: str) -> bool | None:
    if value == "1":
        return False
    if value == "0":
        return True
    return None


def _blocked(value: str) -> bool | None:
    if value.startswith("blocked"):
        return True
    if value.startswith("open"):
        return False
    return None


def _names(value: str) -> list[str]:
    return [name for name in value.split(",") if name and name != "none"]


def _env_rule(banned: str, results: dict[str, str]) -> tuple[bool | None, str]:
    """Credential absence by name census. Names are counted, never reported."""
    names = _names(results.get("bad", ""))
    leaked = results.get("leak", "") != "none"
    hits = [name for name in names if banned in name.upper()]
    if not names and results.get("bad", "") != "none":
        return None, "probe keys absent"
    if leaked or hits:
        return False, f"matched_names={len(hits)} leaked={leaked}"
    return True, f"matched_names=0 leaked=False names_total={len(names)}"


def _github_rule(results: dict[str, str]) -> tuple[bool | None, str]:
    names = _names(results.get("bad", ""))
    leaked = results.get("leak", "") != "none"
    hits = [
        name
        for name in names
        if "GITHUB" in name.upper() or "PRIVATE_KEY" in name.upper()
    ]
    if not names and results.get("bad", "") != "none":
        return None, "probe keys absent"
    return (
        not leaked and not hits,
        f"matched_names={len(hits)} leaked={leaked}",
    )


def _ledger_rule(results: dict[str, str]) -> tuple[bool | None, str]:
    value = results.get("host_ledger_key", "")
    if value == "absent":
        return True, "host_ledger_key=absent"
    if value == "present":
        return False, "host_ledger_key=present"
    return None, "not measured"


def _network(results: dict[str, str]) -> tuple[bool | None, str]:
    one = _blocked(results.get("net_1_1_1_1", ""))
    two = _blocked(results.get("net_8_8_8_8", ""))
    detail = f"1.1.1.1={one} 8.8.8.8={two}"
    if one is False or two is False:
        return False, detail
    if one is None or two is None:
        return None, detail
    return True, detail


def _metadata(results: dict[str, str]) -> tuple[bool | None, str]:
    imds = _blocked(results.get("meta_imds", ""))
    azure = _blocked(results.get("meta_azure", ""))
    detail = f"imds={imds} azure={azure}"
    if imds is False or azure is False:
        return False, detail
    if imds is None or azure is None:
        return None, detail
    return imds and azure, detail


def _control_plane(results: dict[str, str]) -> tuple[bool | None, str]:
    value = results.get("control_plane", "absent")
    if value == "absent":
        return None, "control_plane=absent"
    decision = _blocked(value)
    return decision, f"control_plane={decision}"


def _in(value: str, capped: str, uncapped: str) -> tuple[bool | None, str]:
    if value == capped:
        return True, f"value={value}"
    if value == uncapped:
        return False, f"value={value}"
    return None, f"value={value}"


def _cpu_limit(results: dict[str, str]) -> tuple[bool | None, str]:
    quota = results.get("cpu_max", "")
    if not quota:
        return None, "cpu_max=absent"
    if quota == "max":
        return False, "cpu_max=unlimited"
    text = quota.replace("_", " ")
    parts = text.split("/")
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return True, f"cpu_max={text}"
    pair = text.split()
    if len(pair) == 2 and all(part.isdigit() for part in pair):
        return True, f"cpu_max={text}"
    return None, f"cpu_max={quota}"


def _pid_limit(results: dict[str, str]) -> tuple[bool | None, str]:
    children = _number(results.get("children", ""))
    stop = results.get("stop_errno", "")
    if children is None or not stop:
        return None, f"children={children} stop={stop}"
    if stop != "none":
        return True, f"children={children} stop={stop}"
    if children < 48:
        return None, f"children={children} stop={stop}"
    return False, f"children={children} stop=none"


def _disk_limit(results: dict[str, str]) -> tuple[bool | None, str]:
    errno = _number(results.get("disk_errno", ""))
    written = _number(results.get("written", ""))
    if errno is None or written is None:
        return None, f"errno={errno} written={written}"
    if errno == 28 and 0 < written < _DISK_TARGET:
        return True, f"errno=28 written={written}"
    if errno == 0 and written >= _DISK_TARGET:
        return False, f"errno=0 written={written}"
    return None, f"errno={errno} written={written}"


def _tenant_reads(results: dict[str, str]) -> tuple[bool | None, str]:
    own = results.get("own", "")
    other = results.get("other", "")
    host = results.get("host", "")
    detail = f"own={own[:12]} other={other[:12]} host={host[:12]}"
    if not own or not other or not host:
        return None, detail
    if not own.startswith("bytes"):
        return None, detail
    if other.startswith("bytes") or host.startswith("bytes"):
        return False, detail
    if other.startswith("errno") and host.startswith("errno"):
        return True, detail
    return None, detail


def _unprivileged(results: dict[str, str]) -> tuple[bool | None, str]:
    uid = _number(results.get("uid", ""))
    setuid_raw = results.get("setuid_errno", "")
    setuid = _number(setuid_raw)
    if setuid is None:
        setuid = 999 if setuid_raw and setuid_raw != "0" else None
    nnp = results.get("nonewprivs", "")
    mount = results.get("mount", "")
    caps = results.get("capeff", "")
    if uid is None or setuid is None or not nnp or not mount or not caps:
        return None, f"uid={uid} setuid={setuid} nnp={nnp} mount={mount[:10]}"
    detail = (
        f"uid={uid} setuid={setuid} nnp={nnp} mount={mount[:10]} "
        f"caps_zero={caps == _CAP_ZERO}"
    )
    checks = (
        uid != 0,
        setuid != 0,
        nnp == "1",
        mount != "mounted",
        caps == _CAP_ZERO,
    )
    return all(checks), detail


def _mount_rules(results: dict[str, str]) -> tuple[bool | None, str]:
    keys = ("sock_docker", "sock_run", "docker_dir", "graph_path")
    flags = {key: _flag(results.get(key, "")) for key in keys}
    marker = results.get("marker_content", "")
    if any(value is None for value in flags.values()):
        return None, f"flags={flags} marker={marker[:10]}"
    if any(value is False for value in flags.values()) or marker == "HOST":
        return False, f"flags={flags} marker={marker[:10]}"
    return True, f"flags={flags} marker={marker[:10]}"


def _cleanup_rule(results: dict[str, str]) -> tuple[bool | None, str]:
    name = "cleanup_pass2_new"
    number = _number(results.get(name, ""))
    if number is None:
        return None, f"{name}={results.get(name, '')}"
    return number == 0, f"{name}={number}"


_CONTROL_RULES: dict[str, Callable[[dict[str, str]], tuple[bool | None, str]]] = {
    "network_egress_denied": _network,
    "metadata_endpoint_denied": _metadata,
    "control_plane_endpoint_denied": _control_plane,
    "database_url_absent": lambda r: _env_rule("DATABASE", r),
    "session_secret_absent": lambda r: _env_rule("SECRET", r),
    "github_app_key_absent": _github_rule,
    "host_ledger_key_absent": _ledger_rule,
    "cpu_limit": _cpu_limit,
    "memory_limit": lambda r: _in(r.get("memory_oom", ""), "cgroup_oom", "survived"),
    "pid_limit": _pid_limit,
    "disk_limit": _disk_limit,
    "output_limit": lambda r: _in(r.get("output_limit", ""), "capped", "uncapped"),
    "wall_clock_limit": lambda r: _in(r.get("wall_limit", ""), "capped", "uncapped"),
    "host_cleanup": _cleanup_rule,
    "filesystem_scoped": _tenant_reads,
    "artifact_isolation": lambda r: _in(
        r.get("artifact_isolation", ""), "isolated", "shared"
    ),
    "image_provenance": lambda r: _in(
        r.get("image_provenance", ""), "digest_pinned", "mutable_tag"
    ),
    "audit_recovery": lambda r: _in(r.get("audit_recovery", ""), "verified", "gap"),
    "unprivileged": _unprivileged,
    "host_mounts_denied": _mount_rules,
}


def control_observations(
    results: dict[str, str],
    *,
    simulated: bool,
    measured_from: str,
) -> list[Measurement]:
    """One entry per control, with the observation its rule derives.

    A simulated mechanism yields entries whose observation is recorded as
    simulated; a check can never pass from a simulated entry.
    """
    entries: list[Measurement] = []
    for control in CONTROLS:
        rule = _CONTROL_RULES[control]
        observed, detail = rule(results)
        if simulated and observed is True:
            # A simulated run records the outcome, but a value that only
            # exists inside a simulation is never a direct observation of
            # the control. It stays simulated and the check derives ERROR.
            observed = None
        entries.append(
            {
                "control": control,
                "observed": observed,
                "simulated": simulated,
                "measured_from": measured_from,
                "detail": detail[:_MAX_DETAIL],
            }
        )
    return entries


def _entry_ok(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if tuple(sorted(item)) != (
        "control",
        "detail",
        "measured_from",
        "observed",
        "simulated",
    ):
        return False
    if item["control"] not in CONTROLS:
        return False
    observed = item["observed"]
    if observed is not None and not isinstance(observed, bool):
        return False
    if not isinstance(item["simulated"], bool):
        return False
    if item["measured_from"] not in MEASURED_FROM:
        return False
    detail = item["detail"]
    if not isinstance(detail, str) or len(detail) > _MAX_DETAIL:
        return False
    return not (item["simulated"] and observed is True)


def _check_records(
    measurements: Measurements,
    collected_at: str,
) -> tuple[list[dict[str, str]], bool]:
    """The seven product fields from the recorded control observations.

    `ERROR` for any simulated or host-side observation, `FAIL` if any
    control observation failed, `NOT_MEASURED` if one was absent, and only
    when every contributing control held does the field become `PASS`.
    """
    records: list[dict[str, str]] = []
    all_pass = True
    for field in CHECK_FIELDS:
        states: list[str] = []
        details: list[str] = []
        for control in FIELD_TO_CONTROLS[field]:
            entry = measurements.get(control)
            if entry is None:
                states.append("NOT_MEASURED")
                details.append(f"{control}=absent")
                continue
            observed = entry["observed"]
            if entry["simulated"] or entry["measured_from"] != "guest":
                states.append("ERROR")
                details.append(f"{control}=simulated_or_host")
            elif observed is None:
                states.append("NOT_MEASURED")
                details.append(f"{control}=not_observed")
            elif observed is False:
                states.append("FAIL")
                details.append(f"{control}=failed")
            else:
                states.append("PASS")
                details.append(f"{control}=held")
        status = "PASS"
        if "ERROR" in states:
            status = "ERROR"
        elif "FAIL" in states:
            status = "FAIL"
        elif "NOT_MEASURED" in states:
            status = "NOT_MEASURED"
        if status != "PASS":
            all_pass = False
        records.append(
            {
                "field": field,
                "status": status,
                "method": "group rule over controls",
                "detail": " ".join(details)[:_MAX_DETAIL],
                "measured_at": collected_at,
            }
        )
    return records, all_pass


def _identity_ok(identity: object) -> bool:
    if not isinstance(identity, dict):
        return False
    expected = ("arch", "kernel", "mechanism", "os", "runtime", "runtime_version")
    if tuple(sorted(identity)) != expected:
        return False
    for value in identity.values():
        if not isinstance(value, str) or not value:
            return False
        if len(value) > _MAX_IDENTITY_VALUE:
            return False
    return identity["mechanism"] in MECHANISM_LEVEL


def identity_fingerprint(identity: dict[str, object]) -> str:
    """Hash the identity. A host, kernel, runtime, or image change
    invalidates earlier reports and requires re-verification."""
    if not _identity_ok(identity):
        raise ValueError("rejected")
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_current(stored: object, identity: dict[str, object]) -> bool:
    """A stale identity means re-verification, not a downgrade."""
    return isinstance(stored, str) and stored == identity_fingerprint(identity)


def _summary(checks: list[dict[str, str]], unmeasured: int) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for record in checks:
        counts[record["status"]] += 1
    counts["unmeasured_controls"] = unmeasured
    return counts


def _scan(value: object) -> None:
    if isinstance(value, str):
        if not _secret_free(value):
            raise ValueError("rejected")
    elif isinstance(value, list):
        for item in value:
            _scan(item)
    elif isinstance(value, dict):
        for item in value.values():
            _scan(item)


def validate_report(report: object) -> None:
    """Validate shape, enums, levels, sizes, and secrets.

    The report level must equal the level its own mechanism derives, the
    check records must be exactly the recomputation from its measurements,
    and the summary must match. `production_approved` is refused from every
    angle: no mechanism constructs it.
    """
    if not isinstance(report, dict):
        raise TypeError("rejected")
    required = {
        "checks",
        "collected_at",
        "evidence_level",
        "identity",
        "identity_sha256",
        "image_digest",
        "mechanism",
        "measurements",
        "schema_version",
        "summary",
        "unmeasured_controls",
    }
    if tuple(sorted(report)) != tuple(sorted(required)):
        raise ValueError("rejected")
    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError("rejected")
    collected = report["collected_at"]
    if not isinstance(collected, str) or not _UTC_PATTERN.match(collected):
        raise ValueError("rejected")
    mechanism = report["mechanism"]
    if not isinstance(mechanism, str) or mechanism not in MECHANISM_LEVEL:
        raise ValueError("rejected")
    level = report["evidence_level"]
    if (
        not isinstance(level, str)
        or level not in LEVELS
        or level != derive_level(mechanism)
    ):
        raise ValueError("rejected")
    identity = report["identity"]
    if not _identity_ok(identity):
        raise ValueError("rejected")
    if report["identity_sha256"] != identity_fingerprint(identity):
        raise ValueError("rejected")
    digest = report["image_digest"]
    if not isinstance(digest, str) or (digest and not _DIGEST_PATTERN.match(digest)):
        raise ValueError("rejected")
    raw = report["measurements"]
    if not isinstance(raw, list) or len(raw) > _MAX_MEASUREMENTS:
        raise ValueError("rejected")
    if not all(_entry_ok(item) for item in raw):
        raise ValueError("rejected")
    seen: set[str] = set()
    for item in raw:
        control = str(item["control"])
        if control in seen:
            raise ValueError("rejected")
        seen.add(control)
    items: Measurements = {str(item["control"]): item for item in raw}
    expected_checks, _ = _check_records(items, str(collected))
    if report["checks"] != expected_checks:
        raise ValueError("rejected")
    unmeasured = report["unmeasured_controls"]
    if not isinstance(unmeasured, list) or not all(
        isinstance(name, str) and name in CONTROLS for name in unmeasured
    ):
        raise ValueError("rejected")
    if set(unmeasured) != set(CONTROLS) - seen:
        raise ValueError("rejected")
    if report["summary"] != _summary(expected_checks, len(unmeasured)):
        raise ValueError("rejected")
    _scan(report)


def report_sha256(report: dict[str, object]) -> str:
    """Hash the complete document. Any byte of it changes the digest."""
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_report(
    identity: dict[str, object],
    measurements: list[Measurement],
    *,
    collected_at: str,
    image_digest: str = "",
) -> dict[str, object]:
    """Assemble a validated report; every refusable fact refuses here."""
    if not _identity_ok(identity):
        raise ValueError("rejected")
    mechanism = str(identity["mechanism"])
    level = derive_level(mechanism)
    entries: Measurements = {str(item["control"]): item for item in measurements}
    records, _ = _check_records(entries, collected_at)
    unmeasured = [control for control in CONTROLS if control not in set(entries)]
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "collected_at": collected_at,
        "mechanism": mechanism,
        "evidence_level": level,
        "identity": dict(identity),
        "identity_sha256": identity_fingerprint(identity),
        "image_digest": image_digest,
        "measurements": list(measurements),
        "checks": records,
        "unmeasured_controls": unmeasured,
        "summary": _summary(records, len(unmeasured)),
    }
    validate_report(report)
    return report
