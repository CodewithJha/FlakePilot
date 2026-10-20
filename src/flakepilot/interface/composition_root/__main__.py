"""Server entry point: `python -m flakepilot.interface.composition_root`."""

import os
import sys

import uvicorn

from flakepilot.interface.composition_root import Settings, SettingsError, create_app


def main() -> None:
    try:
        settings = Settings.from_environ(os.environ)
    except SettingsError as exc:
        sys.exit(f"Invalid configuration: {exc}")
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
