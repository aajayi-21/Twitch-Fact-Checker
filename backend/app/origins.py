"""Which browser origins may talk to the local backend.

The backend listens on 127.0.0.1, so every web page the user has open can try
to reach it. ``TrustedHostMiddleware`` stops DNS rebinding (a page whose own
domain resolves to 127.0.0.1). This module stops the rest: a request or
WebSocket handshake that carries an ``Origin`` header is let through only when
it comes from

- **Doxa itself**: ``chrome-extension://<id>`` with ``<id>`` in
  ``ALLOWED_EXTENSION_IDS`` (:class:`app.config.Settings`), or
- **the backend's own pages** (the dashboard, the streamer console, the OBS
  overlay): the origin is ``http://<Host header>``, i.e. same-origin.

A request with **no** ``Origin`` header passes. Browsers always send one on a
WebSocket handshake and on every cross-origin request that can change state,
so a missing ``Origin`` means a non-browser client on this machine (the ingest
CLI, curl, the eval scripts) or a plain top-level page load.

``Origin: null`` (sandboxed iframes, ``file://`` pages, some redirects) is
refused, except on :data:`NULL_ORIGIN_PATHS`: read-only paths that an OBS
browser source loaded from a file may need.

Why a middleware and not CORS: CORS only decides whether a page may *read* a
response. It never applies to WebSocket handshakes, and a "simple" cross-site
POST still executes. Before this guard any website could open ``/ws/audio``,
end the user's live session and spend their API credit.
"""

import logging
import re
import time

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

#: The ID of the unpacked development extension. ``extension/manifest.json``
#: carries the matching public ``key``, so this ID is the same on every
#: machine. Only the public half of that key pair exists.
DEV_EXTENSION_ID = "hbgnggamecjggjhmlnpfkcpocopcbokm"

#: The default for ``ALLOWED_EXTENSION_IDS``. Add the Chrome Web Store item's
#: ID here once the item exists (docs/release-build-plan.md, Phase 9).
DEFAULT_ALLOWED_EXTENSION_IDS: tuple[str, ...] = (DEV_EXTENSION_ID,)

#: Chrome extension IDs are 32 letters from a to p.
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

#: CORS only decides which responses an allowed page may read; the guard
#: below has already refused every extension that is not on the list.
CORS_ORIGIN_REGEX = r"^chrome-extension://[a-p]{32}$"

#: Paths that accept ``Origin: null``. Only read-only paths belong here.
NULL_ORIGIN_PATHS: frozenset[str] = frozenset({"/ws/events"})

_EXTENSION_SCHEME = "chrome-extension://"
#: A refused origin is logged at most once per this many seconds.
_REFUSAL_LOG_INTERVAL_S = 60.0


def parse_extension_ids(raw: str) -> frozenset[str]:
    """Parse a comma-separated ``ALLOWED_EXTENSION_IDS`` value.

    Raises:
        ValueError: naming the first entry that is not a Chrome extension ID,
            so a typo fails at startup instead of silently locking the
            extension out.
    """
    ids: set[str] = set()
    for entry in raw.split(","):
        candidate = entry.strip().lower()
        if not candidate:
            continue
        if not EXTENSION_ID_RE.match(candidate):
            raise ValueError(
                f"ALLOWED_EXTENSION_IDS entry {entry.strip()!r} is not a Chrome "
                "extension ID (32 letters a-p, shown on chrome://extensions)"
            )
        ids.add(candidate)
    return frozenset(ids)


def origin_allowed(
    origin: str | None,
    *,
    host: str | None,
    path: str,
    extension_ids: frozenset[str],
) -> bool:
    """Whether a request with this ``Origin`` may reach the backend.

    Args:
        origin: the ``Origin`` header, or ``None`` when absent.
        host: the ``Host`` header (already restricted to localhost names by
            ``TrustedHostMiddleware``), used for the same-origin rule.
        path: the request path, for :data:`NULL_ORIGIN_PATHS`.
        extension_ids: the allowed Chrome extension IDs.
    """
    if origin is None:
        return True
    origin = origin.strip().lower()
    if origin == "null":
        return path in NULL_ORIGIN_PATHS
    if origin.startswith(_EXTENSION_SCHEME):
        return origin.removeprefix(_EXTENSION_SCHEME) in extension_ids
    if not host:
        return False
    host = host.strip().lower()
    return origin in (f"http://{host}", f"https://{host}")


def refusal_detail(origin: str | None) -> str:
    """The 403 message. For an extension it says how to allow it, because
    the likeliest cause is the person's own second copy of Doxa."""
    origin = (origin or "").strip()
    if origin.lower().startswith(_EXTENSION_SCHEME):
        extension_id = origin[len(_EXTENSION_SCHEME) :]
        return (
            f"This backend does not accept the extension {extension_id}. If it "
            "is your copy of Doxa, add that ID to ALLOWED_EXTENSION_IDS in "
            "backend/.env and restart the backend."
        )
    return f"origin {origin!r} may not use this backend"


class OriginGuardMiddleware:
    """Refuse HTTP requests (403) and WebSocket handshakes (close 1008) whose
    ``Origin`` is not allowed by :func:`origin_allowed`.

    The allowed extension IDs are read per request from
    ``app.state.settings.allowed_extension_id_set``, so they follow the
    settings the app booted with (and the test fixtures). Before settings
    exist, the defaults apply.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._last_refusal_log: dict[str, float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin_allowed(
            origin,
            host=headers.get("host"),
            path=scope["path"],
            extension_ids=self._extension_ids(scope),
        ):
            await self.app(scope, receive, send)
            return
        self._log_refusal(str(origin), scope)
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake
            # with 403, so the page never gets a socket.
            await WebSocket(scope, receive, send).close(code=1008)
            return
        response = JSONResponse({"detail": refusal_detail(origin)}, status_code=403)
        await response(scope, receive, send)

    @staticmethod
    def _extension_ids(scope: Scope) -> frozenset[str]:
        app = scope.get("app")
        settings = getattr(getattr(app, "state", None), "settings", None)
        if settings is None:
            return frozenset(DEFAULT_ALLOWED_EXTENSION_IDS)
        return settings.allowed_extension_id_set

    def _log_refusal(self, origin: str, scope: Scope) -> None:
        now = time.monotonic()
        last = self._last_refusal_log.get(origin)
        if last is not None and now - last < _REFUSAL_LOG_INTERVAL_S:
            return
        if len(self._last_refusal_log) > 256:
            self._last_refusal_log.clear()
        self._last_refusal_log[origin] = now
        logger.warning(
            "refused %s %s from origin %s (not Doxa, not this backend's own "
            "pages; add an extension ID to ALLOWED_EXTENSION_IDS if it is yours)",
            scope["type"],
            scope["path"],
            origin,
        )
