"""JUnit report upload (PRODUCT-SCOPE Workflow B; ADR-013).

`POST /ingest/junit` with `Authorization: Bearer fpi_…`, `Content-Type: application/xml`
(or `text/xml`), the X-FlakePilot-* metadata headers, and the report as the body. The
token decides the org and repo. The body is read in chunks and abandoned as soon as it
exceeds the limit; a declared Content-Length over the limit is refused without reading.

201: accepted and processed. 200: duplicate of an earlier upload (`duplicate: true`).
Errors: 401, 409 `idempotency_conflict`, 413 `payload_too_large`, 415
`unsupported_media_type`, 422 `validation_error` (a metadata header, named) or
`invalid_report` (with a reason code), 503 `ingestion_not_configured`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from flakepilot.identity.application.iam import CiUploadPrincipal
from flakepilot.interface.http.auth import ci_upload_principal, request_origin
from flakepilot.test_intelligence.application.ingestion import (
    MAX_UPLOAD_BYTES,
    METADATA_HEADERS,
    Ingestion,
    IngestionNotConfigured,
)

router = APIRouter(tags=["ingestion"])


async def _body_within_limit(request: Request) -> bytes | None:
    """The body, or None as soon as it is known to exceed MAX_UPLOAD_BYTES."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/ingest/junit", status_code=201, response_model=None)
async def ingest_junit(
    request: Request,
    principal: Annotated[CiUploadPrincipal, Depends(ci_upload_principal)],
) -> JSONResponse:
    ingestion: Ingestion | None = getattr(request.app.state, "ingestion", None)
    if ingestion is None:
        raise IngestionNotConfigured
    result = await ingestion.ingest(
        principal,
        headers={name: request.headers.getlist(name) for name in METADATA_HEADERS},
        content_type=request.headers.get("content-type"),
        content_encoding=request.headers.get("content-encoding"),
        body=await _body_within_limit(request),
        origin=request_origin(),
    )
    summary = result.summary
    return JSONResponse(
        {
            "upload_id": result.upload_id,
            "run_id": result.run_id,
            "state": "processed",
            "duplicate": result.duplicate,
            "tests": summary.tests,
            "attempts": summary.attempts,
            "outcomes": {
                "passed": summary.passed,
                "failed": summary.failed,
                "errored": summary.errored,
                "skipped": summary.skipped,
            },
        },
        200 if result.duplicate else 201,
    )
