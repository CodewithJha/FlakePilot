"""One real Sentry event through the allow-list reporter (goal 7). Owner-run only.

Skipped unless the DSN is in the environment, so the default suite and CI send
nothing. A skip is not verification. Running it sends exactly one synthetic
event (fake ids, a synthetic exception, the template
"flakepilot live verification") to the DSN's project:

    FLAKEPILOT_LIVE_SENTRY_DSN=... uv run pytest tests/live -k sentry \\
        --junitxml=/tmp/sentry.xml   # the accepted event id is a suite property

The event is built and enveloped by the shipped `SentryHandler` and
`SentryEnvelopeTransport`. Only the final POST is the test's own, because the
shipped `urllib_post` discards the answer: it opens the same request the same
way (verified TLS, no redirects, the shipped timeout) and keeps the status and
Sentry's `id`.
"""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from flakepilot.integrations.infrastructure import sentry
from flakepilot.interface.composition_root.error_reporting import SentryHandler

DSN = os.environ.get("FLAKEPILOT_LIVE_SENTRY_DSN", "")


class _Recorder:
    def __init__(self) -> None:
        self.answers: list[tuple[int, str, str]] = []

    def __call__(self, request: urllib.request.Request) -> None:
        assert isinstance(request.data, bytes)
        sent_id = json.loads(request.data.split(b"\n", 1)[0])["event_id"]
        opener = urllib.request.build_opener(
            sentry._NoRedirects,
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with opener.open(request, timeout=sentry.TIMEOUT) as response:
                accepted = json.loads(response.read(1024)).get("id", "")
                self.answers.append((response.status, sent_id, accepted))
        except urllib.error.HTTPError as error:
            self.answers.append((error.code, sent_id, ""))


@pytest.mark.skipif(not DSN, reason="FLAKEPILOT_LIVE_SENTRY_DSN is not set")
def test_live_sentry_accepts_one_allow_listed_event(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    recorder = _Recorder()
    handler = SentryHandler(sentry.SentryEnvelopeTransport(DSN, recorder))
    logger = logging.getLogger("flakepilot.live_verification")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("synthetic failure for live verification")
        except RuntimeError:
            logger.exception(
                "flakepilot live verification",
                extra={
                    "route": "live-verification",
                    "stage": "live_verification",
                    "org_id": "00000000-0000-4000-8000-000000000001",
                },
            )
    finally:
        logger.removeHandler(handler)
        handler.close()  # drains the queue, bounded
    deadline = time.monotonic() + sentry.TIMEOUT + 1
    while not recorder.answers and time.monotonic() < deadline:
        time.sleep(0.1)  # the drain gives up before the transport's own timeout
    ((status, sent_id, accepted),) = recorder.answers
    record_testsuite_property("sentry_event_id", accepted)
    assert status == 200
    assert accepted == sent_id and len(accepted) == 32
