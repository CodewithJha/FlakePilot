"""Research provider adapters: a deterministic recorded fixture for local truth.

The fixture stands in for a live search API with canned, validated sources so
ports, budgets, deduplication, and the untrusted-content framing are testable
without credentials (ADR-017 decision 4; goal 8). A live SerpApi-style
transport adapter enters only after owner-approved credentials, so its exact
transmitted fields and data flow can be verified first — the port contract,
not its promises, is what keeps results as data.
"""

import re
from datetime import datetime

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    ResearchProviderError,
    ResearchQuery,
    ResearchSource,
)

PROVIDER = "recorded_research"


class RecordedResearchProvider:
    """Deterministic, credential-free research: a canned corpus keyed by the
    query's words. The same query always yields the same sources in the same
    order. Every fixture entry is validated through the `ResearchSource`
    contract, so a recorded source obeys the same provenance rules a live
    provider's results would — and a malformed one fails closed with the
    same short code instead of shipping unvalidated content."""

    def __init__(self, corpus: dict[str, tuple[dict[str, str], ...]]) -> None:
        """`corpus` maps a topic word to canned source entries (`url`, `title`,
        `retrieved_at`, `excerpt`)."""
        self._corpus = corpus

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    async def search(self, query: ResearchQuery) -> tuple[ResearchSource, ...]:
        words = [word.lower() for word in re.split(r"\s+", query.query.strip()) if word]
        groups = [self._corpus.get(word, ()) for word in words if word]
        seen_urls: set[str] = set()
        sources: list[ResearchSource] = []
        # Round-robin over topic groups: pass 0 takes each topic's first entry,
        # pass 1 its second, and so on. One crowded topic cannot evict the
        # others, and the budget is the port's promise, not the corpus's.
        deepest = max((len(group) for group in groups), default=0)
        for index in range(deepest):
            if len(sources) >= query.max_sources:
                break
            for group in groups:
                if len(sources) >= query.max_sources:
                    break
                if index >= len(group):
                    continue
                entry = group[index]
                if entry["url"] in seen_urls:
                    continue  # deduplication is the port's job, not the corpus's
                seen_urls.add(entry["url"])
                try:
                    source = ResearchSource(
                        url=entry["url"],
                        title=entry["title"],
                        retrieved_at=_canned(entry["retrieved_at"]),
                        excerpt=entry["excerpt"],
                    )
                except (KeyError, ValueError, TypeError):
                    raise ResearchProviderError("bad_source") from None
                sources.append(source)
        return tuple(sources)


def _canned(value: str) -> datetime:
    return datetime.fromisoformat(value)
