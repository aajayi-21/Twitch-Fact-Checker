"""Tests for Twitch credential validation and the device code flow.

All network via httpx.MockTransport — the suite never touches id.twitch.tv.
"""

import httpx
import pytest

from app.setup import ProviderKeyRejected, ProviderUnreachable

from streamer.twitch_auth import (
    poll_device_token,
    refresh_token,
    start_device_flow,
    token_hint,
    validate_token,
)

VALID_PAYLOAD = {
    "client_id": "abc",
    "login": "factbot",
    "scopes": ["chat:read", "chat:edit"],
    "user_id": "999",
    "expires_in": 12000,
}


def transport_returning(status: int, payload: dict) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


class TestValidateToken:
    async def test_good_token_returns_identity(self) -> None:
        info = await validate_token(
            "sometoken", transport=transport_returning(200, VALID_PAYLOAD)
        )
        assert info.login == "factbot"
        assert info.user_id == "999"
        assert info.expires_in_s == 12000

    async def test_oauth_prefix_is_stripped_before_the_probe(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            return httpx.Response(200, json=VALID_PAYLOAD)

        await validate_token("oauth:sometoken", transport=httpx.MockTransport(handler))
        assert seen == ["OAuth sometoken"]

    async def test_401_is_rejected_with_a_human_message(self) -> None:
        with pytest.raises(ProviderKeyRejected, match="expired or revoked"):
            await validate_token(
                "dead", transport=transport_returning(401, {"message": "invalid"})
            )

    async def test_app_token_is_rejected_by_name(self) -> None:
        """App tokens validate fine but cannot chat — the error must say so
        instead of letting the bot fail mysteriously later."""
        payload = dict(VALID_PAYLOAD, login="")
        with pytest.raises(ProviderKeyRejected, match="APP access token"):
            await validate_token("x", transport=transport_returning(200, payload))

    async def test_missing_chat_edit_scope_is_named_in_the_error(self) -> None:
        """The single biggest would-be support ticket: a token that connects
        but silently cannot post."""
        payload = dict(VALID_PAYLOAD, scopes=["chat:read"])
        with pytest.raises(ProviderKeyRejected, match="chat:edit"):
            await validate_token("x", transport=transport_returning(200, payload))

    async def test_network_failure_is_unreachable_not_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        with pytest.raises(ProviderUnreachable):
            await validate_token("x", transport=httpx.MockTransport(handler))

    async def test_empty_token_is_rejected_before_any_io(self) -> None:
        with pytest.raises(ProviderKeyRejected, match="empty"):
            await validate_token("oauth:")


class TestDeviceFlow:
    async def test_start_returns_the_user_code(self) -> None:
        grant = await start_device_flow(
            "client123",
            transport=transport_returning(
                200,
                {
                    "device_code": "dev123",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "interval": 5,
                    "expires_in": 1800,
                },
            ),
        )
        assert grant.user_code == "ABCD-EFGH"
        assert grant.interval_s == 5

    async def test_poll_pending_returns_none(self) -> None:
        pair = await poll_device_token(
            "client123",
            "dev123",
            transport=transport_returning(
                400, {"message": "authorization_pending", "status": 400}
            ),
        )
        assert pair is None

    async def test_poll_success_returns_the_pair(self) -> None:
        pair = await poll_device_token(
            "client123",
            "dev123",
            transport=transport_returning(
                200, {"access_token": "acc", "refresh_token": "ref"}
            ),
        )
        assert pair == ("acc", "ref")

    async def test_poll_denial_raises(self) -> None:
        with pytest.raises(ProviderKeyRejected):
            await poll_device_token(
                "client123",
                "dev123",
                transport=transport_returning(
                    400, {"message": "access_denied", "status": 400}
                ),
            )


class TestRefresh:
    async def test_refresh_returns_the_rotated_pair(self) -> None:
        seen_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_bodies.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200, json={"access_token": "new-acc", "refresh_token": "new-ref"}
            )

        pair = await refresh_token(
            "client123", "old-ref", transport=httpx.MockTransport(handler)
        )
        assert pair == ("new-acc", "new-ref")
        # Public client: NO client_secret in the refresh request.
        assert "client_secret" not in seen_bodies[0]
        assert seen_bodies[0]["grant_type"] == "refresh_token"

    async def test_refresh_failure_points_at_the_panel(self) -> None:
        with pytest.raises(ProviderKeyRejected, match="control panel"):
            await refresh_token(
                "client123",
                "dead-ref",
                transport=transport_returning(400, {"message": "invalid"}),
            )


class TestTokenHint:
    def test_shows_only_the_last_four(self) -> None:
        assert token_hint("oauth:supersecrettoken9876") == "…9876"

    def test_short_tokens_show_nothing(self) -> None:
        assert token_hint("oauth:x") is None
        assert token_hint("") is None
