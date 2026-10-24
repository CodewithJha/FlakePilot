"""The spoken investigation brief (ADR-017; goal 14).

`compose_brief` turns a finished `InvestigationReport` into a short text:
verdict counts, supporting evidence counts, uncertainty, deterministic next
steps, and a link to the full report. It is built from counts and enum
values only — no test names, log lines, model text, page excerpts, or
anything else a repository or provider wrote — so the text that may leave
the process for a speech provider carries no repository content or secrets.

Audio is never automatic: nothing in the investigation calls `speak_brief`;
a caller that already holds the report asks for it explicitly.
"""

from collections import Counter
from urllib.parse import urlsplit

from flakepilot.diagnosis.application.investigation import (
    InvestigationReport,
    StageStatus,
)
from flakepilot.integrations.application.ports import (
    SpeechAudio,
    SpeechProvider,
    SpeechProviderError,
    SpeechRequest,
)

MAX_BRIEF_BYTES = 1_200
MAX_LINK_BYTES = 200

# The deterministic classifier's verdicts, in the order a reader should act.
_VERDICTS = (
    "deterministic_regression",
    "infrastructure_failure",
    "flaky",
    "inconclusive",
)


def _plain_link(url: str) -> str:
    """A report link that is safe to speak and to send: http(s), no
    credentials, no query or fragment (where tokens tend to ride)."""
    if not 1 <= len(url.encode()) <= MAX_LINK_BYTES or any(
        char <= " " or char in "\\\x7f" for char in url
    ):
        raise ValueError("the report link is a plain URL of at most 200 bytes")
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("the report link is http(s) without credentials or query")
    return url


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def compose_brief(report: InvestigationReport, *, report_url: str) -> str:
    link = _plain_link(report_url)
    cases = report.run.cases
    verdicts = Counter(
        case.classification.failure_class.value
        for case in cases
        if case.classification is not None
    )
    unclassified = len(cases) - sum(verdicts.values())
    failing = sum(
        1
        for case in cases
        if any(attempt in ("failed", "errored") for attempt in case.case.attempts)
    )

    named = [
        f"{verdicts[name]} {name.replace('_', ' ')}"
        for name in _VERDICTS
        if verdicts[name]
    ]
    verdict = ", ".join(named) if named else "no failure verdicts"
    if unclassified:
        verdict += f"; {_count(unclassified, 'test')} without a verdict"

    linked = report.assist.linked if report.assist else 0
    evidence = (
        f"{_count(len(cases), 'test')} in the run, {failing} with failing attempts, "
        f"{_count(len(report.sources), 'research source')}, "
        f"{_count(linked, 'model claim')} linked to evidence"
    )

    skipped = sum(1 for stage in report.stages if stage.status is StageStatus.SKIPPED)
    failed = sum(1 for stage in report.stages if stage.status is StageStatus.FAILED)
    uncertainty = (
        f"{verdicts['inconclusive']} inconclusive; {skipped} of "
        f"{len(report.stages)} stages skipped and {failed} failed"
    )
    if report.assist and report.assist.unverified:
        uncertainty += f"; {_count(report.assist.unverified, 'model claim')} unverified"

    steps = []
    if verdicts["deterministic_regression"]:
        steps.append("Start with the deterministic regressions.")
    if verdicts["infrastructure_failure"]:
        steps.append("Check the CI infrastructure behind the infrastructure failures.")
    if verdicts["flaky"]:
        steps.append("Stabilize or quarantine the flaky tests.")
    if verdicts["inconclusive"] or unclassified:
        steps.append("Collect more runs to settle the open verdicts.")
    if report.prioritization and report.prioritization.ranked:
        steps.append(
            "History ranks "
            f"{_count(len(report.prioritization.ranked), 'test')} for attention."
        )
    if not steps:
        steps.append("No action is needed for this run.")

    brief = (
        "FlakePilot investigation brief. "
        f"Verdict: {verdict}. "
        f"Evidence: {evidence}. "
        f"Uncertainty: {uncertainty}. Model claims are advisory. "
        f"Next steps: {' '.join(steps)} "
        f"Full report: {link}"
    )
    if len(brief.encode()) > MAX_BRIEF_BYTES:
        raise ValueError("the brief exceeds its length cap")
    return brief


class SpokenBriefUnavailable(Exception):
    """The speech provider refused or could not be reached. `reason` is its
    short code; provider text never appears."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def speak_brief(
    report: InvestigationReport,
    *,
    speech: SpeechProvider,
    report_url: str,
    voice: str = "default",
) -> SpeechAudio:
    """Explicitly requested audio for one report. Provider failures become
    `SpokenBriefUnavailable`; text-only operation needs none of this."""
    request = SpeechRequest(compose_brief(report, report_url=report_url), voice=voice)
    try:
        return await speech.speak(request)
    except SpeechProviderError as error:
        raise SpokenBriefUnavailable(error.reason) from None
