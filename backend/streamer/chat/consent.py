"""Consent and channel-binding enforcement — the reason this bot is allowed.

`docs/improvement-report.md` §3.1 rejected the public leaderboard because it
was "a named accusation against a real person … with no consenting
broadcaster in the loop", and endorsed the chat bot *because* one is. This
module is where that loop stops being a promise and becomes code.

The bot's OAuth token proves only that the bot account is the bot account —
nothing about whether the broadcaster wants it in channel X. Authorization
therefore rests on FOUR independent proofs, every one of which some human
other than the operator controls or Twitch itself asserts:

1. **Operator intent** — the channel is in the operator's allowlist. Stops
   typos and stray joins; the weakest proof, listed first because it is the
   only one the operator controls alone.
2. **Broadcaster grant, Twitch-asserted** — the bot holds moderator status
   in the room (our own ``USERSTATE`` carries ``mod=1``). Only the
   broadcaster, or someone they trusted with mod powers, can make that true,
   and it arrives in a tag we cannot forge. This is the proof that stops an
   operator pointing the bot at a channel they do not control.
3. **In-band consent, recorded** — the broadcaster themselves (user-id ==
   room-id, never a display name) typed ``!fc enable``, and the row with
   ``armed_at`` / ``armed_by_user_id`` is the auditable record. Stops a
   well-meaning mod arming it without the broadcaster's knowledge.
4. **Channel binding** — a LIVE ingest session exists whose
   ``(platform, channel)`` equals the chat channel. Makes cross-channel
   weaponization (ingest person A, post into person B's chat) structurally
   impossible, and stops posting the moment the stream's session ends.

Checked on **every post**, not once at join: a mid-stream demod, an
``!fc disable``, or the ingest session dying must stop the very next
message. All failures are fail-closed and carry distinct slugs so the panel
can say exactly which proof is missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.sessions import ChannelKey, channel_key


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    """The persisted consent/state row for one (platform, channel).

    A missing row means "unarmed and off" — the fail-closed default.
    ``muted_until``/timestamps are ISO strings in the db; the *decision*
    layer receives resolved values via PostContext, so this record carries
    only what the proofs need.
    """

    armed: bool  # armed_at set and disarmed_at not set after it
    armed_by_user_id: str | None
    room_id: str | None


def consent_failure(
    *,
    platform: str,
    channel: str,
    allowlist: frozenset[str],
    bot_is_moderator: bool,
    record: ChannelRecord | None,
    live_session_keys: frozenset[ChannelKey],
) -> str | None:
    """``None`` when every proof holds; else the FIRST failing proof's slug.

    Order matches the numbering in the module docstring, so the surfaced
    reason always names the earliest broken link in the chain.
    """
    normalized = channel_key(platform, channel)
    normalized_channel = normalized[1]
    if normalized_channel is None:
        return "channel_unbound"

    # 1. Operator intent.
    if normalized_channel not in {
        name.strip().lstrip("#").lower() for name in allowlist
    }:
        return "channel_not_allowlisted"

    # 2. Broadcaster grant, asserted by Twitch in our own USERSTATE.
    if not bot_is_moderator:
        return "not_moderator"

    # 3. Recorded in-band consent from the broadcaster.
    if record is None or not record.armed:
        return "not_armed"

    # 4. A live ingest session bound to THIS channel.
    if normalized not in live_session_keys:
        return "channel_unbound"

    return None
