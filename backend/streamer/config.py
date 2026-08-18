"""Streamer-product settings: the shared Settings plus the bot's surface.

Subclasses :class:`app.config.Settings`, so every shared knob (LLM providers,
Whisper, pipeline tuning) works identically and the same ``.env`` file drives
both products — with the streamer's OWN defaults where the two must differ:
its own port (8711) and its own database file (``streamer.db``), so the two
apps run side by side without touching each other's state.

Token hygiene: ``twitch_bot_token``/``twitch_bot_refresh`` are secrets. They
are never returned by any endpoint (status carries an ``…abcd`` hint only),
never logged (the IRC transport redacts ``PASS``), and written to ``.env``
exclusively through ``app.setup.upsert_env_values`` (atomic, 0600).
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_STREAMER_DB = _BACKEND_DIR / "streamer.db"


class StreamerSettings(Settings):
    """Everything the viewer backend has, plus the streamer product."""

    # Side-by-side defaults: own port, own database.
    port: int = 8711
    db_path: str = str(_DEFAULT_STREAMER_DB)

    # ---- Twitch chat bot ------------------------------------------------ #
    # The bot ACCOUNT's user token (scopes: chat:read chat:edit). A leading
    # "oauth:" is tolerated everywhere and stripped where needed.
    twitch_bot_token: str = ""
    # Rotating refresh token (Device Code Flow only; one-time-use, so it is
    # re-persisted on every refresh).
    twitch_bot_refresh: str = ""
    # Cached from /oauth2/validate so status needs no live probe.
    twitch_bot_login: str = ""
    twitch_bot_expires_at: str = ""
    # The channel to watch and post into (the broadcaster's login).
    twitch_channel: str = ""
    # Public client id for the Device Code Flow. No default: the operator
    # registers their own app (client ids are public, but Twitch forbids
    # sharing one across applications). Empty = paste-token setup only.
    twitch_client_id: str = ""
    # Operator allowlist (consent proof #1). CSV; empty = just twitch_channel.
    twitch_chat_channels: str = ""

    # ---- Posting master switches ---------------------------------------- #
    # Dry run records every would-be post (exact message text) and sends
    # NOTHING. Deliberately defaults ON: the operator flips it after reading
    # a real stream's worth of would-have-posted messages.
    chat_dry_run: bool = True

    # ---- /ws/events ------------------------------------------------------ #
    # Optional shared token for the events socket (?token=...), for when the
    # backend is ever exposed beyond localhost. NEVER the Twitch token —
    # tokens in URLs land in logs and OBS scene configs.
    events_token: str = ""

    @property
    def twitch_bot_token_value(self) -> str:
        return self.twitch_bot_token.removeprefix("oauth:").strip()

    @property
    def chat_bot_configured(self) -> bool:
        """A real token, a known bot login, and a target channel."""
        return bool(
            self.twitch_bot_token_value
            and not self._is_placeholder_key(self.twitch_bot_token)
            and self.twitch_bot_login.strip()
            and self.twitch_channel.strip()
        )

    @property
    def chat_allowlist(self) -> frozenset[str]:
        """Consent proof #1. Defaults to exactly the configured channel."""
        listed = {
            name.strip().lstrip("#").lower()
            for name in self.twitch_chat_channels.split(",")
            if name.strip()
        }
        channel = self.twitch_channel.strip().lstrip("#").lower()
        if channel:
            listed.add(channel)
        return frozenset(listed)
