"""Shared read-only reporting helpers for the operator CLI and the run view.

Identity of what these produce: exactly the domain verdict's own fields
(failure class, reason, rule version, target revision, evidence), rendered
with every user-controlled value HTML-escaped. No confidence, no extra
labels, no evidence rewording: what the rule set recorded is what is shown.
"""

import html
from typing import Any

from flakepilot.test_intelligence.application.run_view import ClassifiedCase

__all__ = ["classification_cell", "evidence_text", "fail_cascade"]


def fail_cascade(value: Any) -> str:
    return "&mdash;" if value in (None, "") else html.escape(str(value))


def evidence_text(classification: Any) -> str:
    """The verdict's own recorded facts, deterministically ordered and escaped.

    No fact is added, reworded, or dropped: every key the rule set recorded
    (counts, run IDs, environments, SHAs) is shown; empty entries are omitted.
    """
    items = []
    for key in sorted(classification.evidence):
        value = classification.evidence[key]
        if value is None or value == [] or value == "":
            continue
        rendered = (
            ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        )
        items.append(f"{html.escape(key)}={html.escape(rendered)}")
    note = f" &middot; rule {html.escape(classification.rule_version)}"
    target = classification.target_sha
    if target:
        note += f" &middot; target_sha {html.escape(target)}"
    return (f"{note} &middot; " if items else note) + " &middot; ".join(items)


def classification_cell(entry: ClassifiedCase) -> str:
    verdict = entry.classification
    if verdict is None:
        return html.escape("classification unavailable")
    head = html.escape(verdict.failure_class.value)
    gc = f" v-{html.escape(verdict.failure_class.value)}"
    if verdict.reason:
        head += f' <span class="note">({html.escape(verdict.reason.value)})</span>'
    return (
        f'<span class="verdict{gc}">{head}</span>'
        f'<br><span class="note">{evidence_text(verdict)}</span>'
    )
