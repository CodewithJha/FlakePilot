"""Hosted TabPFN adapter over an injected transport. No network, no key.

Verifies the documented upload -> fit -> predict sequence, that the bearer
token never reaches the signed storage URLs, that only numeric rows leave the
process, and that every failure is a short reason code.
"""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from flakepilot.test_intelligence.infrastructure.tabpfn import (
    MAX_RESPONSE_BYTES,
    TabPfnPredictor,
)

API = "https://api.priorlabs.ai"
TRAIN = [(1.0, None), (0.0, 2.5)]
LABELS = [True, False]
TEST = [(3.0, 1.0), (0.0, None)]


def upload(name: str) -> dict[str, Any]:
    return {
        "signed_urls": [f"https://storage.example/{name}"],
        "required_headers": {"Content-Type": "text/csv"},
    }


def happy() -> dict[str, tuple[int, bytes]]:
    return {
        "/tabpfn/prepare_train_set_upload": (
            200,
            json.dumps(
                {
                    "train_set_upload_id": "u1",
                    "x_train_info": upload("x"),
                    "y_train_info": upload("y"),
                }
            ).encode(),
        ),
        "/tabpfn/fit": (
            200,
            b"  \n"
            + json.dumps({"fitted_train_set_id": "f1", "status": "completed"}).encode(),
        ),
        "/tabpfn/prepare_test_set_upload": (
            200,
            json.dumps(
                {"test_set_upload_id": "t1", "x_test_info": upload("t")}
            ).encode(),
        ),
        "/tabpfn/predict": (
            200,
            json.dumps(
                {
                    "prediction": [[0.2, 0.8], [0.9, 0.1]],
                    "metadata": {"classes": [0, 1], "package_version": "3.5.1"},
                }
            ).encode(),
        ),
    }


class Sent:
    def __init__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes
    ) -> None:
        self.method, self.url, self.headers, self.body = method, url, headers, body


class Transport:
    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.requests: list[Sent] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        assert timeout > 0
        self.requests.append(Sent(method, url, headers, body))
        if url.startswith(API):
            return self.routes[url.removeprefix(API)]
        return 200, b""  # a signed-URL upload


def predict(transport: Transport) -> Any:
    predictor = TabPfnPredictor(api_key="test-key", transport=transport)
    return asyncio.run(predictor.predict(TRAIN, LABELS, TEST))


def test_the_documented_flow_yields_positive_class_probabilities() -> None:
    transport = Transport(happy())
    answer = predict(transport)
    assert answer.available and answer.probabilities == (0.8, 0.1)
    assert answer.model == "v3.5_default@3.5.1"
    assert [(r.method, r.url) for r in transport.requests] == [
        ("POST", f"{API}/tabpfn/prepare_train_set_upload"),
        ("PUT", "https://storage.example/x"),
        ("PUT", "https://storage.example/y"),
        ("POST", f"{API}/tabpfn/fit"),
        ("POST", f"{API}/tabpfn/prepare_test_set_upload"),
        ("PUT", "https://storage.example/t"),
        ("POST", f"{API}/tabpfn/predict"),
    ]
    fit = json.loads(transport.requests[3].body)
    assert fit["train_set_upload_id"] == "u1"
    assert fit["task_config"]["tabpfn_config"] == {
        "model_path": "v3.5_default",
        "random_state": 0,
    }


def test_the_token_goes_to_the_api_only_and_uploads_are_numeric_csv() -> None:
    transport = Transport(happy())
    predict(transport)
    for request in transport.requests:
        on_api = request.url.startswith(API)
        assert ("Authorization" in request.headers) is on_api
    puts = [r.body for r in transport.requests if r.method == "PUT"]
    assert puts == [
        b"f0,f1\n1.0,\n0.0,2.5\n",
        b"label\n1\n0\n",
        b"f0,f1\n3.0,1.0\n0.0,\n",
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "provider_unauthorized"),
        (429, "provider_rate_limited"),
        (500, "provider_http_error"),
    ],
)
def test_http_failures_are_reason_codes(status: int, reason: str) -> None:
    routes = happy()
    routes["/tabpfn/prepare_train_set_upload"] = (status, b"provider text")
    answer = predict(Transport(routes))
    assert (answer.available, answer.reason, answer.probabilities) == (
        False,
        reason,
        None,
    )


def test_a_non_https_signed_url_is_refused_before_any_upload() -> None:
    routes = happy()
    body = json.loads(routes["/tabpfn/prepare_train_set_upload"][1])
    body["x_train_info"]["signed_urls"] = ["http://storage.example/x"]
    routes["/tabpfn/prepare_train_set_upload"] = (200, json.dumps(body).encode())
    transport = Transport(routes)
    assert predict(transport).reason == "bad_response"
    assert all(r.method == "POST" for r in transport.requests)


def test_an_incomplete_fit_is_not_a_prediction() -> None:
    routes = happy()
    routes["/tabpfn/fit"] = (200, b'{"fitted_train_set_id": "f1", "status": "pending"}')
    assert predict(Transport(routes)).reason == "fit_not_completed"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"prediction": [[0.2, 0.8]], "metadata": {"classes": [0, 2], "package_version": "1"}}',
        b'{"prediction": [[0.2]], "metadata": {"classes": [0, 1], "package_version": "1"}}',
        b'{"prediction": [[0.2, 0.8]], "metadata": {"classes": [0, 1], "package_version": "a b"}}',
    ],
)
def test_malformed_predictions_are_bad_responses(body: bytes) -> None:
    routes = happy()
    routes["/tabpfn/predict"] = (200, body)
    assert predict(Transport(routes)).reason == "bad_response"


def test_an_unreachable_provider_is_unavailable() -> None:
    def down(*args: object) -> tuple[int, bytes]:
        raise OSError("connection refused")

    predictor = TabPfnPredictor(api_key="k", transport=down)
    answer = asyncio.run(predictor.predict(TRAIN, LABELS, TEST))
    assert answer.reason == "provider_unreachable"


def test_no_contact_without_rows() -> None:
    transport = Transport(happy())
    predictor = TabPfnPredictor(api_key="k", transport=transport)
    assert asyncio.run(predictor.predict([], [], TEST)).reason == "no_rows"
    assert transport.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": ""},
        {"api_key": "k", "base_url": "http://api.priorlabs.ai"},
        {"api_key": "k", "model_path": "v3 5"},
        {"api_key": "k", "timeout_seconds": 0},
    ],
)
def test_construction_refuses_unsafe_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TabPfnPredictor(transport=Transport(happy()), **kwargs)


def test_an_oversized_answer_is_refused() -> None:
    routes = happy()
    routes["/tabpfn/predict"] = (200, b" " * (MAX_RESPONSE_BYTES + 1))
    assert predict(Transport(routes)).reason == "response_too_large"
