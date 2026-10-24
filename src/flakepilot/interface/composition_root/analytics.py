"""Operator analytics commands (operator trust, read-only, JSON on stdout).

- `provider-status`: which hosted providers this environment would use, by
  provider code; no database, no network, no key printed.
- `reliability-trend --org-id --repo-id [--days N] [--bucket day|week]`: CI
  failure and flaky counts per bucket and environment for the last N days
  against the N days before (`Reliability.trend`).
- `evaluate-prioritization --org-id --repo-id --train-cutoff ISO
  --horizon-days N [--k K] [--with-tabpfn]`: the leakage-safe evaluation of
  failure prediction against the shipped baseline. TabPFN is contacted only
  with `--with-tabpfn` and `FLAKEPILOT_TABPFN_API_KEY` both present; it then
  receives numeric feature rows and 0/1 labels, nothing else. Without it the
  candidate is reported `blocked: not_configured`, never filled in.

Failures print the cause class only, like the other operator commands.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from flakepilot.interface.composition_root import Settings, ti_unit_of_work
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)
from flakepilot.interface.composition_root.providers import hosted_providers
from flakepilot.interface.composition_root.settings import SettingsError
from flakepilot.test_intelligence.application.evaluation import Evaluation
from flakepilot.test_intelligence.application.reliability import Reliability


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _uuid(value: str) -> str:
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a canonical UUID")


def _days(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 366:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 366")
    return int(value)


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must carry a UTC offset")
    return parsed


def _pair(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--org-id", required=True, type=_uuid, help="org ID (UUID)")
    parser.add_argument("--repo-id", required=True, type=_uuid, help="repo ID (UUID)")


def run_provider_status(settings: Settings, argv: Sequence[str]) -> int:
    argparse.ArgumentParser(
        prog="provider-status",
        description=(
            "Hosted providers this environment would use "
            "(configured; not checked at runtime)."
        ),
    ).parse_args(argv)
    try:
        status = hosted_providers(settings).status()
    except SettingsError as exc:
        print(f"provider-status: {exc}", file=sys.stderr)
        return 1
    _print_json(status)
    return 0


def run_reliability_trend(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="reliability-trend",
        description="CI failure and flaky trends against the previous window.",
    )
    _pair(parser)
    parser.add_argument("--days", type=_days, default=7, help="window length")
    parser.add_argument("--bucket", choices=("day", "week"), default="day")
    args = parser.parse_args(argv)

    async def trend() -> Any:
        engine = create_engine(settings.database_url.reveal())
        try:
            return await Reliability(ti_unit_of_work(engine)).trend(
                org_id=args.org_id,
                repo_id=args.repo_id,
                end=datetime.now(UTC),
                window=timedelta(days=args.days),
                bucket=args.bucket,
            )
        finally:
            await dispose_engine(engine)

    try:
        report = asyncio.run(trend())
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        print(f"reliability-trend failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_json(asdict(report))
    return 0


def run_evaluate_prioritization(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="evaluate-prioritization",
        description=(
            "Leakage-safe evaluation of failure prediction against the shipped "
            "prioritization baseline."
        ),
    )
    _pair(parser)
    parser.add_argument("--train-cutoff", required=True, type=_instant)
    parser.add_argument("--horizon-days", required=True, type=_days)
    parser.add_argument("--k", type=_days, default=10, help="precision@k cut")
    parser.add_argument(
        "--with-tabpfn",
        action="store_true",
        help="send numeric feature rows and labels to TabPFN (needs the key)",
    )
    args = parser.parse_args(argv)
    try:
        candidate = hosted_providers(settings).prioritization_candidate
    except SettingsError as exc:
        print(f"evaluate-prioritization: {exc}", file=sys.stderr)
        return 1
    if args.with_tabpfn and candidate is None:
        print(
            "evaluate-prioritization: --with-tabpfn needs FLAKEPILOT_TABPFN_API_KEY",
            file=sys.stderr,
        )
        return 1

    async def evaluate() -> Any:
        engine = create_engine(settings.database_url.reveal())
        try:
            return await Evaluation(ti_unit_of_work(engine)).run(
                org_id=args.org_id,
                repo_id=args.repo_id,
                train_cutoff=args.train_cutoff,
                horizon=timedelta(days=args.horizon_days),
                k=args.k,
                predictor=candidate if args.with_tabpfn else None,
            )
        finally:
            await dispose_engine(engine)

    try:
        report = asyncio.run(evaluate())
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        print(f"evaluate-prioritization failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_json(asdict(report))
    return 0
