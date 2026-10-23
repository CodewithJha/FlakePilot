"""Collect an isolation probe report or a host identity, and no more.

Two subcommands, both safe on a control-plane laptop.

`identity` discovers the running host with one allowlisted query per fact:
`uname -s`, `uname -m`, `uname -r`, `sw_vers -productVersion` on macOS, and
`docker --version` and `podman --version` for the container runtime. It
writes the identity JSON that `report` reads back. A query that fails, is
absent, or exceeds its timeout is an `ERROR` fact, never a pass, and leaves
the identity incomplete. `report` refuses an incomplete identity.

`report` validates an identity file, reads one probe section's `RESULT`
lines from the existing probes, derives every control observation with the
rules in `evidence_report`, and writes the schema-version-1 JSON. It never
executes project code, reads environment variables, accepts a free-form
command, or passes anything but the fixed allowlisted arguments to a query.
Results that a probe did not produce leave their checks `NOT_MEASURED` and
are never a pass.

The `--mechanism` label selects the evidence level through the module table.
There is no level argument and no flag that raises the level.
`production_approved` is not constructible here; production attestation
requires independent human review of the whole bundle, not this tool.
"""

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

if __file__ and str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from evidence_report import (
    MECHANISM_LEVEL,
    build_report,
    control_observations,
    parse_result_lines,
    report_sha256,
)

__all__ = ["discovered_identity", "identity_facts", "runner"]

_MAX_OUTPUT = 4096
_TIMEOUT = 5.0
_IDENTITY_FIELDS = ("os", "arch", "kernel", "runtime", "runtime_version")
_SYSTEM_QUERIES: tuple[tuple[list[str], str], ...] = (
    (["uname", "-s"], "os"),
    (["uname", "-m"], "arch"),
    (["uname", "-r"], "kernel"),
)

MACOS_QUERIES: tuple[tuple[list[str], str], ...] = (
    (["sw_vers", "-productVersion"], "macos_version"),
)
RUNTIME_QUERIES: tuple[tuple[list[str], str], ...] = (
    (["docker", "--version"], "docker_version"),
    (["podman", "--version"], "podman_version"),
)


def runner(argv: list[str], timeout: float) -> tuple[int, str]:
    """One bounded system query from the fixed allowlist. Never passes a
    caller-supplied string; the argv values are module constants or a fake
    supplied by the tests."""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    text = completed.stdout.decode("utf-8", errors="replace")[:_MAX_OUTPUT]
    return completed.returncode, text


def identity_facts(execute: object = None) -> dict[str, object]:
    """Observed host facts. Every entry is an observation, never a claim.

    `facts` maps a fact name to a bounded value or, when the query failed,
    to an `ERROR` marker with the exit code. The values are the only inputs
    the identity carries; no environment variable is read.
    """
    command = runner if execute is None else execute
    facts: dict[str, str] = {}
    system = _SYSTEM_QUERIES
    if sys.platform in ("darwin", "linux"):
        system = system + MACOS_QUERIES
    for argv, name in system + RUNTIME_QUERIES:
        code, text = command(argv, _TIMEOUT)  # type: ignore[operator]
        lines = text.splitlines()
        facts[name] = lines[0][:120] if code == 0 and lines else ""
        facts[f"{name}_status"] = "PASS" if code == 0 and lines else "ERROR"

    os_value: str = facts.get("os", "")
    arch: str = facts.get("arch", "")
    kernel: str = facts.get("kernel", "")
    macos = facts.get("macos_version", "")
    docker = facts.get("docker_version", "")
    podman = facts.get("podman_version", "")
    identity = dict.fromkeys(_IDENTITY_FIELDS, "")
    if os_value:
        identity["os"] = f"{os_value} {macos}" if macos else os_value
    identity["arch"] = arch
    identity["kernel"] = kernel
    if docker:
        identity["runtime"] = "docker"
        parts = docker.split()
        identity["runtime_version"] = parts[2] if len(parts) > 2 else ""
    elif podman:
        identity["runtime"] = "podman"
        parts = podman.split()
        identity["runtime_version"] = parts[2] if len(parts) > 2 else ""
    facts["runtime_name"] = identity["runtime"]
    facts["runtime_version"] = identity["runtime_version"]
    return {"facts": facts, "identity": identity}


def discovered_identity(
    mechanism: str, execute: object = None
) -> tuple[dict[str, str], dict[str, str]]:
    """The identity for a mechanism, observed where it can be observed.

    The identity is incomplete when a required fact could not be observed on
    this host. Incomplete means `report` will refuse; it never means a best
    guess. Returns the identity dict and the per-query fact reporting.
    """
    discovery = identity_facts(execute)
    here = {name: discovery["identity"][name] for name in _IDENTITY_FIELDS}
    complete = all(here[name] for name in _IDENTITY_FIELDS)
    if not complete:
        raise SystemExit(_missing_text(here, discovery["facts"]))
    return {**here, "mechanism": mechanism}, dict(discovery["facts"])


def _missing_text(
    here: dict[str, str], facts: dict[str, str]
) -> str:  # pragma: no cover - exercised by the SystemExit path in tests
    missing = [name for name in _IDENTITY_FIELDS if not here[name]]
    statuses = json.dumps({k: facts[k] for k in sorted(facts) if k.endswith("_status")})
    return f"identity incomplete: missing {missing}: {statuses}"


def _now() -> str:
    moment = datetime.datetime.now(datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: str) -> str:
    """Bounded read. Stdin is accepted; a file that is too large is refused."""
    if path == "-":
        raw = sys.stdin.buffer.read()
        if len(raw) > 131072:
            raise SystemExit("RESULT input too large")
        return raw.decode("utf-8", errors="replace")
    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise SystemExit("RESULT file too large")
    return raw.decode("utf-8", errors="replace")


def build(identity_path: str, results_path: str, image_digest: str) -> str:
    """Load the identity and one probe section; emit the report JSON."""
    identity = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    if not isinstance(identity, dict) or identity.get("mechanism") not in (
        MECHANISM_LEVEL
    ):
        raise SystemExit("identity must name a known mechanism")
    results = parse_result_lines(_read_text(results_path))
    simulated = MECHANISM_LEVEL[str(identity["mechanism"])] == 0
    measurements = control_observations(
        results, simulated=simulated, measured_from="guest"
    )
    report = build_report(
        identity,
        measurements,
        collected_at=_now(),
        image_digest=image_digest,
    )
    report["report_sha256"] = report_sha256(report)
    return json.dumps(report, indent=1, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="collect_report",
        description="Schema-versioned isolation evidence report or host identity.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("identity", help="query the running host")
    discovery.add_argument(
        "--mechanism", required=True, choices=sorted(MECHANISM_LEVEL)
    )
    emit = commands.add_parser("report", help="build the evidence report")
    emit.add_argument("--identity", required=True, help="path to identity JSON")
    emit.add_argument("--results", required=True, help="RESULT text, - for stdin")
    emit.add_argument("--image-digest", default="")
    emit.add_argument("--out", default="")
    args = parser.parse_args()
    if args.command == "identity":
        payload, _ = discovered_identity(args.mechanism)
        sys.stdout.write(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        return
    text = build(
        args.identity,
        args.results,
        args.image_digest,
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        return
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
