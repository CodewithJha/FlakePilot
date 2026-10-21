"""Entry point: `python -m flakepilot.interface.composition_root` runs the server;
`... bootstrap-org --help` is the operator onboarding command (see `bootstrap`)."""

import os
import sys

import uvicorn

from flakepilot.interface.composition_root import Settings, SettingsError, create_app


def main() -> None:
    try:
        settings = Settings.from_environ(os.environ)
    except SettingsError as exc:
        sys.exit(f"Invalid configuration: {exc}")
    args = sys.argv[1:]
    if args[:1] == ["bootstrap-org"]:
        from flakepilot.interface.composition_root.bootstrap import run_bootstrap_org

        sys.exit(run_bootstrap_org(settings, args[1:]))
    if args:
        sys.exit(
            "Usage: python -m flakepilot.interface.composition_root [bootstrap-org]"
        )
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
