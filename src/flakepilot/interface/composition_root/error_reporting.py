"""Opt-in error reporting to Sentry (sponsor goal 7), standard library only.

Off unless FLAKEPILOT_SENTRY_DSN is set. The vendor SDK is not used: it requires
certifi (MPL-2.0), which the runtime licence gate refuses (ADR-010 TQ-2), and it
captures far more than the boundary allows (SENTRY-OBSERVABILITY-BOUNDARY).

Events are built from an allow-list, never filtered from a full capture. An event
carries exactly: an event id, the timestamp, the level, the logger name, the log
call's message template (never its arguments), the exception class and its code
locations (file name, function, line; never the exception text, source lines or
local variables), and tags from `request_id` plus `TAG_FIELDS`. Request data,
headers, cookies, query strings, bodies, breadcrumbs, users, host names,
environment variables and argv are never collected. Kept free text that looks
like a credential is replaced by `[redacted]`. The HTTP transport lives in
integrations (`SentryEnvelopeTransport`).

Delivery is best-effort: a bounded queue and one daemon thread. A full queue or a
failed send drops the event silently; logging that failure could recurse.
"""

import contextlib
import logging
import queue
import re
import threading
import traceback
import uuid
from collections.abc import Callable
from importlib.metadata import version
from pathlib import PurePath
from types import TracebackType
from typing import Any

from flakepilot.integrations.infrastructure.sentry import (
    Post,
    SentryEnvelopeTransport,
    urllib_post,
)
from flakepilot.interface.composition_root.settings import Settings
from flakepilot.interface.http.request_context import request_id_var

HANDLER_NAME = "flakepilot-sentry"
#: Log-record attributes (`extra=`) that may become tags. IDs and templates only.
TAG_FIELDS = ("route", "org_id", "repo_id", "investigation_id", "stage")
QUEUE_SIZE = 100
DRAIN_SECONDS = 2.0
_MAX_FRAMES = 50
_MAX_TEXT = 200
_TAG_VALUE = re.compile(r"[A-Za-z0-9._:/{}-]{1,200}")
_CREDENTIAL = re.compile(
    r"(?i)token|key|secret|passw|authoriz|bearer|cookie|session|dsn|fpi_|gh[opsu]_|sk-"
)


def _text(value: str) -> str:
    return "[redacted]" if _CREDENTIAL.search(value) else value[:_MAX_TEXT]


def _tags(record: logging.LogRecord) -> dict[str, str]:
    candidates = {"request_id": request_id_var.get()}
    candidates.update({name: getattr(record, name, None) for name in TAG_FIELDS})
    return {
        name: value
        for name, value in candidates.items()
        if isinstance(value, str)
        and _TAG_VALUE.fullmatch(value)
        and not _CREDENTIAL.search(value)
    }


def _exception(kind: type[BaseException], tb: TracebackType | None) -> dict[str, Any]:
    stack = traceback.StackSummary.extract(
        traceback.walk_tb(tb), limit=_MAX_FRAMES, lookup_lines=False
    )
    frames = [
        {
            "filename": PurePath(frame.filename).name,
            "function": frame.name,
            "lineno": frame.lineno,
            "in_app": "flakepilot" in PurePath(frame.filename).parts,
        }
        for frame in stack
    ]
    return {
        "type": kind.__qualname__,
        "module": kind.__module__,
        "stacktrace": {"frames": frames},
    }


def build_event(record: logging.LogRecord) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "timestamp": record.created,
        "platform": "python",
        "level": "fatal" if record.levelno >= logging.CRITICAL else "error",
        "logger": _text(record.name),
        "release": f"flakepilot@{version('flakepilot')}",
        "tags": _tags(record),
    }
    if isinstance(record.msg, str):
        event["logentry"] = {"message": _text(record.msg)}
    if record.exc_info and record.exc_info[0] is not None:
        event["exception"] = {
            "values": [_exception(record.exc_info[0], record.exc_info[2])]
        }
    return event


class SentryHandler(logging.Handler):
    """ERROR and above, as allow-listed events, handed to `send` off-thread."""

    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        super().__init__(logging.ERROR)
        self.set_name(HANDLER_NAME)
        self._send = send
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(QUEUE_SIZE)
        self._thread = threading.Thread(
            target=self._send_all, name=HANDLER_NAME, daemon=True
        )
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        # Never handleError: it prints the record's arguments to stderr.
        with contextlib.suppress(Exception):
            self._queue.put_nowait(build_event(record))

    def _send_all(self) -> None:
        while (event := self._queue.get()) is not None:
            with contextlib.suppress(Exception):
                self._send(event)

    def close(self) -> None:
        """Drain what is queued, bounded; logging.shutdown calls this at exit."""
        with contextlib.suppress(queue.Full):
            self._queue.put(None, timeout=DRAIN_SECONDS)
        self._thread.join(DRAIN_SECONDS)
        super().close()


def configure_error_reporting(settings: Settings, post: Post = urllib_post) -> None:
    """Install the reporter on the root logger when a DSN is set; a previous one is
    removed either way, so an unset DSN always means nothing is sent."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(handler)
        handler.close()
    if settings.sentry_dsn is not None:
        transport = SentryEnvelopeTransport(settings.sentry_dsn.reveal(), post)
        root.addHandler(SentryHandler(transport))
