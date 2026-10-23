"""Entry point: `python -m flakepilot.interface.composition_root` runs the server;
`... bootstrap-org --help` onboards an org; `... worker` runs the leased job worker;
`... issue-ci-upload-token --help` issues a CI token; `... ci-run-report --help`
renders a stored run."""

import asyncio
import os
import sys

import uvicorn

from flakepilot.interface.composition_root import Settings, SettingsError, create_app

_USAGE = (
    "Usage: python -m flakepilot.interface.composition_root "
    "[bootstrap-org | worker | issue-ci-upload-token | ci-run-report]"
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
    if args[:1] == ["issue-ci-upload-token"]:
        from flakepilot.interface.composition_root.operator import (
            run_issue_ci_upload_token,
        )

        sys.exit(run_issue_ci_upload_token(settings, args[1:]))
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
