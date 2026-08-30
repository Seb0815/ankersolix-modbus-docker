"""Browser hardening for a passwordless trusted-LAN UI."""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CSRF_COOKIE = "solix_csrf"
CSRF_HEADER = "x-solix-csrf"


class BrowserSecurityMiddleware(BaseHTTPMiddleware):
    """Issue a CSRF cookie and reject cross-origin write requests."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            rejection = self._validate_write(request)
            if rejection is not None:
                return rejection

        response = await call_next(request)
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(
                CSRF_COOKIE,
                secrets.token_urlsafe(32),
                httponly=False,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @staticmethod
    def _validate_write(request: Request) -> JSONResponse | None:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"detail": "JSON erforderlich"}, status_code=415)
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get(CSRF_HEADER)
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            return JSONResponse({"detail": "Ungültiges CSRF-Token"}, status_code=403)
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin != expected_origin:
            return JSONResponse({"detail": "Ungültiger Ursprung"}, status_code=403)
        return None
