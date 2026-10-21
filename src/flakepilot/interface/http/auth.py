"""Sign-in with GitHub, sessions, CSRF and request principals (ADR-011).

- Session cookie: an opaque random ID (IAM stores only its SHA-256), HttpOnly, SameSite=Lax,
  Path=/. Secure with the `__Host-` prefix (no subdomain can set or rescope it) unless
  FLAKEPILOT_ALLOW_INSECURE_COOKIES=true for local plain-HTTP development.
- Sign-in binds `state` and the PKCE verifier to the browser in a short-lived HttpOnly
  cookie, cleared on the callback whatever the outcome.
- CSRF: a cookie-authenticated unsafe request must carry an Origin (or else Referer)
  equal to FLAKEPILOT_PUBLIC_ORIGIN.
- The org in a URL is only a lookup key: access comes from IAM's records for the verified
  session user. After sign-in the browser always goes to "/" (no caller-chosen redirect).
"""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.identity.application.iam import (
    SESSION_LIFETIME,
    AuthNotConfigured,
    AuthProviderUnavailable,
    CiUploadPrincipal,
    Iam,
    SessionPrincipal,
    SignInFailed,
)
from flakepilot.interface.http.errors import (
    CrossSiteRequest,
    Unauthenticated,
    access_error_body,
)
from flakepilot.interface.http.request_context import (
    UNAUTHENTICATED,
    error_body,
    request_id_var,
)
from flakepilot.shared_kernel.events import Channel, Origin

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SIGN_IN_SECONDS = 600


@dataclass(frozen=True)
class AuthConfig:
    public_origin: str
    secure_cookies: bool

    @property
    def session_cookie(self) -> str:
        return ("__Host-" if self.secure_cookies else "") + "flakepilot_session"

    @property
    def sign_in_cookie(self) -> str:
        return ("__Host-" if self.secure_cookies else "") + "flakepilot_sign_in"


router = APIRouter(prefix="/auth", tags=["auth"])


def _config(request: Request) -> AuthConfig:
    config: AuthConfig | None = request.app.state.auth_config
    if config is None:
        raise AuthNotConfigured
    return config


def _iam(request: Request) -> Iam:
    iam: Iam | None = getattr(request.app.state, "iam", None)
    if iam is None:
        raise AuthNotConfigured
    return iam


def _set_cookie(
    response: Response, config: AuthConfig, name: str, value: str, max_age: int
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _clear_cookie(response: Response, config: AuthConfig, name: str) -> None:
    response.delete_cookie(
        name, path="/", secure=config.secure_cookies, httponly=True, samesite="lax"
    )


def request_origin() -> Origin:
    """Audit origin: the HTTP channel and this request's ID (set by the middleware)."""
    request_id = request_id_var.get()
    if request_id is None:
        raise RuntimeError("no request ID outside RequestContextMiddleware")
    return Origin(Channel.HTTP, request_id)


def _same_origin(request: Request, origin: str) -> bool:
    sent = request.headers.get("origin")
    if sent is None:
        referer = urlsplit(request.headers.get("referer", ""))
        sent = f"{referer.scheme}://{referer.netloc}"
    return sent.lower() == origin


async def session_principal(request: Request) -> SessionPrincipal:
    config = _config(request)
    session_id = request.cookies.get(config.session_cookie)
    if not session_id:
        raise Unauthenticated
    if request.method not in SAFE_METHODS and not _same_origin(
        request, config.public_origin
    ):
        raise CrossSiteRequest
    principal = await _iam(request).authenticate(session_id)
    if principal is None:
        raise Unauthenticated
    return principal


async def org_access(
    org_id: UUID,
    request: Request,
    principal: Annotated[SessionPrincipal, Depends(session_principal)],
) -> OrgAccess:
    return await _iam(request).org_access(principal, str(org_id))


async def ci_upload_principal(request: Request) -> CiUploadPrincipal:
    """`Authorization: Bearer fpi_…`: may only upload to the token's repo."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated
    principal = await _iam(request).verify_ci_upload_token(token.strip())
    if principal is None:
        raise Unauthenticated
    return principal


@router.get("/github/login")
async def github_login(request: Request) -> RedirectResponse:
    config = _config(request)
    start = _iam(request).begin_sign_in()
    response = RedirectResponse(start.url, status_code=302)
    value = f"{start.state}.{start.code_verifier}"
    _set_cookie(response, config, config.sign_in_cookie, value, SIGN_IN_SECONDS)
    return response


@router.get("/github/callback", response_model=None)
async def github_callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=512)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    config = _config(request)
    bound = request.cookies.get(config.sign_in_cookie, "")
    expected_state, _, verifier = bound.partition(".")
    response: Response
    try:
        session_id = await _iam(request).complete_sign_in(
            expected_state=expected_state,
            received_state=state,
            code=code,
            code_verifier=verifier,
            error=error,
            previous_session=request.cookies.get(config.session_cookie),
            origin=request_origin(),
        )
    except SignInFailed:
        body = error_body(UNAUTHENTICATED, "Sign-in failed", request_id_var.get())
        response = JSONResponse(body, 401)
    except AuthProviderUnavailable as exc:
        status, body = access_error_body(exc)
        response = JSONResponse(body, status)
    else:
        response = RedirectResponse("/", status_code=303)
        lifetime = int(SESSION_LIFETIME.total_seconds())
        _set_cookie(response, config, config.session_cookie, session_id, lifetime)
    _clear_cookie(response, config, config.sign_in_cookie)
    return response


@router.post("/logout", status_code=204, response_model=None)
async def logout(
    request: Request,
    principal: Annotated[SessionPrincipal, Depends(session_principal)],
) -> Response:
    config = _config(request)
    await _iam(request).sign_out(
        request.cookies[config.session_cookie], origin=request_origin()
    )
    response = Response(status_code=204)
    _clear_cookie(response, config, config.session_cookie)
    return response
