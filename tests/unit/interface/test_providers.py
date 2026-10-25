"""Hosted provider wiring: opt-in, fail-closed, honestly reported, secret-free."""

import logging
import urllib.request

import pytest
from support import FAKE_DSN

from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.integrations.infrastructure.gemma import GemmaAssist
from flakepilot.interface.composition_root import Settings, SettingsError, create_app
from flakepilot.interface.composition_root.providers import hosted_providers

KEYS = {
    "FLAKEPILOT_GEMINI_API_KEY": "gem-secret-AAAA1111",
    "FLAKEPILOT_SERPAPI_API_KEY": "serp-secret-BBBB2222",
    "FLAKEPILOT_BROWSERBASE_API_KEY": "bb-secret-CCCC3333",
    "FLAKEPILOT_ELEVENLABS_API_KEY": "xi-secret-DDDD4444",
    "FLAKEPILOT_TABPFN_API_KEY": "pfn-secret-EEEE5555",
}
FULL = {
    "FLAKEPILOT_DATABASE_URL": FAKE_DSN,
    **KEYS,
    "FLAKEPILOT_GEMINI_MODEL": "gemma-4-26b-a4b-it",
    "FLAKEPILOT_BROWSER_ALLOWLIST": "docs.python.org",
    "FLAKEPILOT_ELEVENLABS_VOICE_ID": "JBFqnCBsd6RMkjVDRZzb",
    "FLAKEPILOT_SENTRY_DSN": "https://0123abcdpublic@o42.ingest.example.invalid/4508",
    "FLAKEPILOT_AUTH0_DOMAIN": "tenant.us.auth0.com",
    "FLAKEPILOT_AUTH0_AUDIENCE": "https://api.flakepilot.example",
}


def test_nothing_is_built_or_claimed_without_settings() -> None:
    providers = hosted_providers(
        Settings.from_environ({"FLAKEPILOT_DATABASE_URL": FAKE_DSN})
    )
    assert isinstance(providers.assist, NullAiAssist)
    assert providers.research is None and providers.browser is None
    assert providers.speech is None and providers.prioritization_candidate is None
    assert set(providers.status().values()) == {"none"}


def test_full_settings_build_every_adapter_and_report_codes_only() -> None:
    providers = hosted_providers(Settings.from_environ(FULL))
    assert isinstance(providers.assist, GemmaAssist)
    assert providers.status() == {
        "ai_assist": "gemini_api",
        "research": "serpapi",
        "browser": "browserbase",
        "speech": "elevenlabs",
        "error_reporting": "sentry",
        "agent_token_issuer": "auth0",
        "prioritization_candidate": "tabpfn_operator_only",
    }
    rendered = repr(providers) + str(providers.status())
    assert not any(secret in rendered for secret in KEYS.values())


def test_the_local_endpoint_is_still_the_other_advisory_option() -> None:
    providers = hosted_providers(
        Settings.from_environ(
            {
                "FLAKEPILOT_DATABASE_URL": FAKE_DSN,
                "FLAKEPILOT_AI_ENDPOINT": "http://127.0.0.1:8080/v1/chat/completions",
                "FLAKEPILOT_AI_MODEL": "gemma_local",
            }
        )
    )
    assert providers.status()["ai_assist"] == "local_endpoint"


def test_an_adapter_refusal_is_a_startup_refusal_without_the_key() -> None:
    odd = "serp+key/with=symbols"  # printable, so settings accept it; SerpApi does not
    settings = Settings.from_environ(
        {"FLAKEPILOT_DATABASE_URL": FAKE_DSN, "FLAKEPILOT_SERPAPI_API_KEY": odd}
    )
    with pytest.raises(SettingsError) as error:
        hosted_providers(settings)
    assert odd not in str(error.value)
    with pytest.raises(SettingsError):
        create_app(settings)


def test_building_the_app_contacts_no_provider_and_logs_no_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("no network contact while building the app")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    caplog.set_level(logging.INFO, logger="flakepilot.providers")
    # No DSN here: the reporter's root-logger handler would outlive this test.
    environ = {k: v for k, v in FULL.items() if k != "FLAKEPILOT_SENTRY_DSN"}
    create_app(Settings.from_environ(environ))
    [line] = [
        r.getMessage() for r in caplog.records if r.name == "flakepilot.providers"
    ]
    assert "(not checked at runtime)" in line and "ai_assist=gemini_api" in line
    assert not any(secret in caplog.text for secret in KEYS.values())
