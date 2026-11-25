import hmac
import ipaddress
from pathlib import Path
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB_ROOT = Path(__file__).resolve().parent / "web"
COOKIE_NAME = "webull_local_session"


class LocalUI:
    """Automatic loopback-only browser access, without sending .env keys to JS."""

    def __init__(self):
        self.session = secrets.token_urlsafe(48)
        self.csrf = secrets.token_urlsafe(32)

    @staticmethod
    def local_request(request):
        try:
            peer_is_local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            return False
        hostname = (request.url.hostname or "").lower()
        if not peer_is_local or hostname not in {"localhost", "127.0.0.1", "::1"}:
            return False
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin and origin != expected:
            return False
        return request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}

    def authorize(self, request):
        token = request.cookies.get(COOKIE_NAME, "")
        csrf = request.headers.get("X-CSRF-Token", "")
        return (
            self.local_request(request)
            and hmac.compare_digest(token.encode(), self.session.encode())
            and hmac.compare_digest(csrf.encode(), self.csrf.encode())
        )

    def mount(self, app):
        @app.get("/", include_in_schema=False)
        def dashboard(request: Request):
            if not self.local_request(request):
                raise HTTPException(403, "Dashboard is available on localhost only.")
            return FileResponse(WEB_ROOT / "index.html", media_type="text/html")

        @app.get("/api-console", include_in_schema=False)
        def api_console(request: Request):
            if not self.local_request(request):
                raise HTTPException(403, "API console is available on localhost only.")
            return FileResponse(WEB_ROOT / "console.html", media_type="text/html")

        @app.get("/ui/session", include_in_schema=False)
        def session(request: Request):
            if not self.local_request(request) or request.headers.get("X-Local-UI") != "1":
                raise HTTPException(403, "Open the local dashboard to initialize its session.")
            response = JSONResponse({"csrf_token": self.csrf, "configuration_source": ".env"})
            response.set_cookie(COOKIE_NAME, self.session, httponly=True, samesite="strict",
                                secure=False, max_age=8 * 60 * 60, path="/")
            return response

        # Only public UI assets are mounted; the project folder/.env are never served.
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="dashboard-assets")
