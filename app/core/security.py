import hmac
import secrets

from fastapi import HTTPException, Request
from starlette.formparsers import MultiPartException
from starlette.responses import PlainTextResponse


def csrf_signing_key(key: bytes) -> str:
    return hmac.new(key, b"shelfshuttle-csrf", "sha256").hexdigest()


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def protect(request: Request, form=None) -> None:
    sent = request.headers.get("x-csrf-token") or (form.get("_csrf") if form else None)
    expected = request.session.get("csrf")
    if (
        not isinstance(sent, str)
        or not isinstance(expected, str)
        or not hmac.compare_digest(sent.encode(), expected.encode())
    ):
        raise HTTPException(403, "The request token expired. Reload the page and try again.")


class RequestPolicy:
    def __init__(self, app, upload_limit: int):
        self.app = app
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                if too_large:
                    message["status"] = 413
                headers = message.setdefault("headers", [])
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                        ),
                    ]
                )
                if not scope["path"].startswith("/static/"):
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        if scope["path"] in {"/add", "/api/jobs", "/api/epub/inspect"}:
            limit = self.upload_limit + 64 * 1024
        else:
            limit = 32 * 1024
        too_large = False
        received = 0

        async def bounded_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    # The multipart parser closes any partially allocated temporary files.
                    raise MultiPartException("The request body exceeds the upload limit.")
            return message

        lengths = [value for name, value in scope["headers"] if name == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError
                too_large = int(lengths[0]) > limit
            except ValueError:
                await PlainTextResponse("Invalid Content-Length.", 400)(scope, receive, secure_send)
                return
            if too_large:
                await PlainTextResponse("The request body exceeds the upload limit.", 413)(
                    scope,
                    receive,
                    secure_send,
                )
                return
        try:
            await self.app(scope, bounded_receive, secure_send)
        except MultiPartException:
            if not too_large:
                raise
            await PlainTextResponse("The request body exceeds the upload limit.", 413)(
                scope,
                receive,
                secure_send,
            )
