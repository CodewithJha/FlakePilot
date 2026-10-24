"""Hosted TabPFN (Prior Labs) as an evaluation `Predictor`.

Follows the documented `/tabpfn/*` JSON flow at https://api.priorlabs.ai
(docs.priorlabs.ai/api/rest-quickstart): prepare a training upload, PUT the
CSV files to the returned signed URLs, fit, prepare and PUT the test upload,
then predict class probabilities. No SDK (ADR-017 decision 10).

The HTTPS exchange itself is injected: outbound HTTP lives only in
integration adapters (ADR-003), so without a transport from the composition
root nothing can be sent.

Data flow: only numeric feature rows and 0/1 training labels leave the
process — no test names, identity IDs, org or repo IDs, or repository content.
The bearer token goes only to the API origin, never to the signed storage
URLs. Every failure becomes an unavailable `Prediction` with a short reason
code; provider text is never echoed. Nothing here has run against the live
API: no key exists in this repository (owner-gated).
"""

import asyncio
import csv
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from flakepilot.test_intelligence.application.evaluation import Feature, Prediction

BASE_URL = "https://api.priorlabs.ai"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 10_000
_VERSION = re.compile(r"[A-Za-z0-9._@:+-]{1,80}")

# Blocking (method, url, headers, body, timeout seconds) -> (status, body). It
# must refuse redirects and non-https URLs, and raise OSError when unreachable.
Transport = Callable[[str, str, Mapping[str, str], bytes, float], tuple[int, bytes]]


class _Bad(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _csv(rows: Sequence[Sequence[object]], header: Sequence[str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    return out.getvalue().encode()


class TabPfnPredictor:
    """`Predictor` over the hosted API. `model_path` selects the model version
    (e.g. "v3.5_default"); the reported version is `model_path@package`."""

    def __init__(
        self,
        *,
        api_key: str,
        model_path: str = "v3.5_default",
        base_url: str = BASE_URL,
        transport: Transport,
        timeout_seconds: int = 600,
    ) -> None:
        if not api_key:
            raise ValueError("the TabPFN API key is required")
        if not base_url.startswith("https://"):
            raise ValueError("the TabPFN API is reached over https only")
        if _VERSION.fullmatch(model_path) is None:
            raise ValueError("the model path is a short version code")
        if not 1 <= int(timeout_seconds) <= 3600:
            raise ValueError("the timeout is an integer from 1 to 3600 seconds")
        self._key = api_key
        self._model_path = model_path
        self._base = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._transport = transport

    def _call(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes
    ) -> bytes:
        try:
            status, answer = self._transport(method, url, headers, body, self._timeout)
        except OSError:
            raise _Bad("provider_unreachable") from None
        if status == 401:
            raise _Bad("provider_unauthorized")
        if status == 429:
            raise _Bad("provider_rate_limited")
        if not 200 <= status < 300:
            raise _Bad("provider_http_error")
        if len(answer) > MAX_RESPONSE_BYTES:
            raise _Bad("response_too_large")
        return answer

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._call(
            "POST",
            f"{self._base}{path}",
            {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": "FlakePilot",
            },
            json.dumps(payload).encode(),
        )
        try:
            parsed = json.loads(body)  # long fits stream leading keepalive whitespace
        except ValueError:
            raise _Bad("bad_response") from None
        if not isinstance(parsed, dict):
            raise _Bad("bad_response")
        return parsed

    def _put(self, info: object, content: bytes) -> None:
        """Upload to a signed URL with only the headers the API asked for."""
        if not isinstance(info, dict):
            raise _Bad("bad_response")
        urls, headers = info.get("signed_urls"), info.get("required_headers") or {}
        if not isinstance(urls, list) or not urls or not isinstance(headers, dict):
            raise _Bad("bad_response")
        url = urls[0]
        if not isinstance(url, str) or not url.startswith("https://"):
            raise _Bad("bad_response")
        if not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise _Bad("bad_response")
        self._call("PUT", url, headers, content)

    def _run(
        self,
        train_features: Sequence[tuple[Feature, ...]],
        train_labels: Sequence[bool],
        test_features: Sequence[tuple[Feature, ...]],
    ) -> Prediction:
        header = [f"f{i}" for i in range(len(train_features[0]))]
        prepared = self._post(
            "/tabpfn/prepare_train_set_upload",
            {"x_train_info": {"format": "csv"}, "y_train_info": {"format": "csv"}},
        )
        self._put(prepared.get("x_train_info"), _csv(train_features, header))
        self._put(
            prepared.get("y_train_info"),
            _csv([[int(label)] for label in train_labels], ["label"]),
        )
        fitted = self._post(
            "/tabpfn/fit",
            {
                "train_set_upload_id": prepared.get("train_set_upload_id"),
                "task_config": {
                    "task": "classification",
                    "tabpfn_config": {
                        "model_path": self._model_path,
                        "random_state": 0,
                    },
                },
            },
        )
        fitted_id = fitted.get("fitted_train_set_id")
        if fitted.get("status") != "completed" or not isinstance(fitted_id, str):
            raise _Bad("fit_not_completed")
        test_upload = self._post(
            "/tabpfn/prepare_test_set_upload",
            {"fitted_train_set_id": fitted_id, "x_test_info": {"format": "csv"}},
        )
        self._put(test_upload.get("x_test_info"), _csv(test_features, header))
        answer = self._post(
            "/tabpfn/predict",
            {
                "test_set_upload_id": test_upload.get("test_set_upload_id"),
                "fitted_train_set_id": fitted_id,
                "task_config": {
                    "task": "classification",
                    "predict_params": {"output_type": "probas"},
                },
            },
        )
        try:
            metadata = answer["metadata"]
            classes = [str(c).lower() for c in metadata["classes"]]
            positive = next(
                i for i, c in enumerate(classes) if c in ("1", "1.0", "true")
            )
            probabilities = tuple(float(row[positive]) for row in answer["prediction"])
            version = f"{self._model_path}@{metadata['package_version']}"
        except (KeyError, IndexError, TypeError, ValueError, StopIteration):
            raise _Bad("bad_response") from None
        if _VERSION.fullmatch(version) is None:
            raise _Bad("bad_response")
        return Prediction(
            available=True, probabilities=probabilities, reason=None, model=version
        )

    async def predict(
        self,
        train_features: Sequence[tuple[Feature, ...]],
        train_labels: Sequence[bool],
        test_features: Sequence[tuple[Feature, ...]],
    ) -> Prediction:
        if not train_features or not test_features:
            return self._unavailable("no_rows")
        if max(len(train_features), len(test_features)) > MAX_ROWS:
            return self._unavailable("too_many_rows")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._run, train_features, train_labels, test_features
                ),
                timeout=self._timeout,
            )
        except _Bad as error:
            return self._unavailable(error.reason)
        except TimeoutError:
            return self._unavailable("provider_unreachable")

    def _unavailable(self, reason: str) -> Prediction:
        return Prediction(
            available=False, probabilities=None, reason=reason, model=self._model_path
        )
