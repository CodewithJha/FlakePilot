"""Entry point: `python -m flakepilot.interface.composition_root` runs the server;
`... bootstrap-org --help` onboards an org; `... register-repo | add-member | grant |
revoke | show-access --help` administer members, bindings and grants (operator trust);
`... list-runs --help` lists a repository's stored runs (read-only);
`... verify-github-app --help` runs the read-only staging check of the GitHub App
and the code-host access verifier;
`... worker` runs the leased job worker;
`... issue-ci-upload-token --help` issues a CI token; `... ci-run-report --help`
renders a stored run."""

import asyncio
import os
import sys

import uvicorn

from flakepilot.interface.composition_root import Settings, SettingsError, create_app

_USAGE = (
    "Usage: python -m flakepilot.interface.composition_root "
    "[bootstrap-org | register-repo | add-member | grant | revoke | show-access | "
    "list-runs | verify-github-app | issue-ci-upload-token | ci-run-report | worker]"
)


def main() -> None:
    try:
        settings = Settings.from_environ(os.environ)
    except SettingsError as exc:
        sys.exit(f"Invalid configuration: {exc}")
    args = sys.argv[1:]
    if args[:1] == ["bootstrap-org"]:
        from flakepilot.interface.composition_root.bootstrap import run_bootstrap_org

        sys.exit(run_bootstrap_org(settings, args[1:]))
    if args[:1] and args[0] in (
        "register-repo",
        "add-member",
        "grant",
        "revoke",
        "show-access",
    ):
        from flakepilot.interface.composition_root.administration import (
            run_administration,
        )

        sys.exit(run_administration(settings, args))
    if args[:1] == ["verify-github-app"]:
        from flakepilot.interface.composition_root.staging_verification import (
            run_staging_verification,
        )

        sys.exit(run_staging_verification(settings, args[1:]))
    if args[:1] == ["issue-ci-upload-token"]:
        from flakepilot.interface.composition_root.operator import (
            run_issue_ci_upload_token,
        )

        sys.exit(run_issue_ci_upload_token(settings, args[1:]))
    if args[:1] == ["list-runs"]:
        from flakepilot.interface.composition_root.operator import run_list_runs

        sys.exit(run_list_runs(settings, args[1:]))
    if args[:1] == ["ci-run-report"]:
        from flakepilot.interface.composition_root.operator import run_ci_run_report

        sys.exit(run_ci_run_report(settings, args[1:]))
    if args == ["worker"]:
        from flakepilot.interface.composition_root.worker import run_worker

        sys.exit(asyncio.run(run_worker(settings)))
    if args:
        sys.exit(_USAGE)
    # uvicorn's own logging and access log are off: the app logs JSON lines without query strings.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
