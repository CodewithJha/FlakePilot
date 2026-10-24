"""Hosted providers from settings (ADR-017): opt-in, fail-closed, honestly reported.

Constructing an adapter contacts nothing; a provider is built only when its
settings are complete, and anything unset stays absent (or `NullAiAssist`), so
an unconfigured process makes no hosted call at all. `status()` reports what
this process would use, by provider code, never a key. "Configured" is not
"working": the process never probes a provider.

The worker never calls this: it holds no provider credential.
"""

import logging
from dataclasses import dataclass

from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.integrations.infrastructure.browserbase import BrowserbaseEvidence
from flakepilot.integrations.infrastructure.elevenlabs import ElevenLabsSpeech
from flakepilot.integrations.infrastructure.gemma import GemmaAssist
from flakepilot.integrations.infrastructure.hosted_http import call
from flakepilot.integrations.infrastructure.model_endpoint import LocalEndpointAssist
from flakepilot.integrations.infrastructure.serpapi import SerpApiResearch
from flakepilot.interface.composition_root.settings import Settings, SettingsError
from flakepilot.test_intelligence.infrastructure.tabpfn import TabPfnPredictor

_logger = logging.getLogger("flakepilot.providers")


@dataclass(frozen=True)
class HostedProviders:
    assist: GemmaAssist | LocalEndpointAssist | NullAiAssist
    research: SerpApiResearch | None
    browser: BrowserbaseEvidence | None
    speech: ElevenLabsSpeech | None
    # Used only by the `evaluate-prioritization` operator command.
    prioritization_candidate: TabPfnPredictor | None
    error_reporting: bool
    # Parsed, but no route verifies agent tokens yet.
    agent_token_issuer: bool

    def status(self) -> dict[str, str]:
        def code(provider: object | None, name: str) -> str:
            return name if provider is not None else "none"

        return {
            "ai_assist": self.assist.describe().provider,
            "research": code(self.research, "serpapi"),
            "browser": code(self.browser, "browserbase"),
            "speech": code(self.speech, "elevenlabs"),
            "error_reporting": "sentry" if self.error_reporting else "none",
            "agent_token_issuer": (
                "auth0_not_wired" if self.agent_token_issuer else "none"
            ),
            "prioritization_candidate": code(
                self.prioritization_candidate, "tabpfn_operator_only"
            ),
        }


def hosted_providers(settings: Settings) -> HostedProviders:
    """Adapters re-check their own inputs; a refusal there is a startup refusal
    too, and their messages carry no key."""
    try:
        return _build(settings)
    except ValueError as error:
        raise SettingsError(f"hosted provider settings refused: {error}") from None


def _build(settings: Settings) -> HostedProviders:
    assist: GemmaAssist | LocalEndpointAssist | NullAiAssist = NullAiAssist()
    if settings.gemini_api_key is not None and settings.gemini_model is not None:
        assist = GemmaAssist(
            api_key=settings.gemini_api_key.reveal(), model_id=settings.gemini_model
        )
    elif settings.ai_endpoint is not None:
        assist = LocalEndpointAssist(
            endpoint=settings.ai_endpoint,
            model=settings.ai_model or "unspecified",
            api_key=(
                settings.ai_api_key.reveal()
                if settings.ai_api_key is not None
                else None
            ),
        )
    return HostedProviders(
        assist=assist,
        research=(
            SerpApiResearch(api_key=settings.serpapi_api_key.reveal())
            if settings.serpapi_api_key is not None
            else None
        ),
        browser=(
            BrowserbaseEvidence(
                api_key=settings.browserbase_api_key.reveal(),
                allowed_hosts=settings.browser_allowlist,
            )
            if settings.browserbase_api_key is not None
            else None
        ),
        speech=(
            ElevenLabsSpeech(
                api_key=settings.elevenlabs_api_key.reveal(),
                voices={"default": settings.elevenlabs_voice_id},
                model=settings.elevenlabs_model,
            )
            if settings.elevenlabs_api_key is not None
            and settings.elevenlabs_voice_id is not None
            else None
        ),
        prioritization_candidate=(
            TabPfnPredictor(api_key=settings.tabpfn_api_key.reveal(), transport=call)
            if settings.tabpfn_api_key is not None
            else None
        ),
        error_reporting=settings.sentry_dsn is not None,
        agent_token_issuer=settings.auth0_domain is not None,
    )


def log_status(providers: HostedProviders) -> None:
    _logger.info(
        "hosted providers configured (not checked at runtime): %s",
        " ".join(f"{name}={value}" for name, value in providers.status().items()),
    )
