"""Operator command: create an org and its first org admin (onboarding bootstrap).

    python -m flakepilot.interface.composition_root bootstrap-org \\
        --name "Acme" --admin-github-id 12345 [--admin-login octocat]

Reads FLAKEPILOT_DATABASE_URL through Settings (never printed). Prints only the new org
ID; on failure exits 1 with a message that names the problem, never a value or a
database detail. Run migrations first. Not audited yet (AUD is deferred).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from flakepilot.identity.application.iam import (
    Iam,
    InvalidBootstrapInput,
    OrgAlreadyExists,
)
from flakepilot.integrations.infrastructure.github import ISSUER as GITHUB_ISSUER
from flakepilot.interface.composition_root import Settings, iam_unit_of_work
from flakepilot.interface.composition_root.database import create_engine, dispose_engine


def run_bootstrap_org(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m flakepilot.interface.composition_root bootstrap-org",
        description="Create an org whose first org admin is the given GitHub user.",
    )
    parser.add_argument("--name", required=True, help="org name, 1-100 characters")
    parser.add_argument(
        "--admin-github-id", required=True, help="numeric GitHub user ID"
    )
    parser.add_argument("--admin-login", help="GitHub login (refreshed at sign-in)")
    args = parser.parse_args(argv)
    try:
        org_id = asyncio.run(
            _bootstrap(settings, args.name, args.admin_github_id, args.admin_login)
        )
    except (InvalidBootstrapInput, OrgAlreadyExists) as exc:
        print(f"bootstrap-org: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver errors may carry connection details
        print(f"bootstrap-org failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(org_id)
    return 0


async def _bootstrap(
    settings: Settings, name: str, github_id: str, login: str | None
) -> str:
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        return await iam.bootstrap_org(
            name=name,
            admin_issuer=GITHUB_ISSUER,
            admin_subject=github_id,
            admin_login=login,
        )
    finally:
        await dispose_engine(engine)
