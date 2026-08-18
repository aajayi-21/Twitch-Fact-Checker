"""``!fc`` command grammar, authorization, and reply templates. All pure.

Three rules with teeth:

- **Authorization keys on numeric ids, never names.** ``is_broadcaster`` was
  derived from ``user-id == room-id`` at the transport; display names accept
  homoglyphs and logins can be re-registered after a rename. VIP and
  subscriber badges confer nothing.
- **Unknown commands and unauthorized viewers get SILENCE.** A bot that
  replies "you can't do that" to every viewer is a spam engine any viewer can
  trigger at will. (Moderators attempting a broadcaster-only verb are the one
  exception — that is a genuine mistake worth one gentle correction.)
- **No user-supplied text is ever interpolated into an outbound message.**
  Replies are built from templates plus values that passed a strict
  validator: handles must match ``^[0-9a-f]{4,6}$``, topics must be in the
  fixed tuple, durations/caps are parsed integers re-rendered from the
  parse. Without this, ``!fc dispute 3f2a /ban everyone`` gets echoed by a
  moderator bot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.models import TOPICS

from streamer.chat.policy import PostingPolicy
from streamer.chat.transport import ChatMessage

Role = Literal["viewer", "moderator", "broadcaster"]

# Verb -> minimum role. Anything not listed here does not exist.
COMMAND_ROLES: dict[str, Role] = {
    # anyone
    "help": "viewer",
    "about": "viewer",
    "status": "viewer",
    "why": "viewer",
    "dispute": "viewer",
    # moderators (includes the broadcaster)
    "on": "moderator",
    "off": "moderator",
    "review": "moderator",
    "mute": "moderator",
    "unmute": "moderator",
    "cap": "moderator",
    "topics": "moderator",
    "labels": "moderator",
    "retract": "moderator",
    # broadcaster only — consent and trust are theirs alone
    "enable": "broadcaster",
    "disable": "broadcaster",
    "correct": "broadcaster",
    "trust": "broadcaster",
}

MAX_ARGS = 8
MAX_ARG_CHARS = 64

HANDLE_RE = re.compile(r"^[0-9a-f]{4,6}$")
_DURATION_RE = re.compile(r"^(\d{1,3})m$|^(\d)h$")

DEFAULT_MUTE_S = 900.0  # 15 minutes
MAX_MUTE_S = 4 * 3600.0

CORRECTION_LABELS = frozenset({"TRUE", "MISLEADING", "UNVERIFIED"})


@dataclass(frozen=True, slots=True)
class Command:
    verb: str
    args: tuple[str, ...]


def parse_command(text: str, *, prefix: str = "!fc") -> Command | None:
    """Parse one chat line into a command, or ``None`` (= say nothing).

    Case-insensitive verb; ``!fc`` alone is ``status``. ``!fcx`` is NOT a
    command (the prefix must be a whole word). Oversized arg lists and
    oversized args are rejected outright rather than truncated.
    """
    stripped = text.strip()
    if stripped.lower() != prefix and not stripped.lower().startswith(prefix + " "):
        return None
    remainder = stripped[len(prefix) :].strip()
    if not remainder:
        return Command(verb="status", args=())
    parts = remainder.split()
    verb = parts[0].lower()
    args = tuple(parts[1:])
    if verb not in COMMAND_ROLES:
        return None
    if len(args) > MAX_ARGS or any(len(arg) > MAX_ARG_CHARS for arg in args):
        return None
    return Command(verb=verb, args=args)


def role_of(message: ChatMessage) -> Role:
    if message.is_broadcaster:
        return "broadcaster"
    if message.is_moderator:
        return "moderator"
    return "viewer"


_ROLE_ORDER: dict[Role, int] = {"viewer": 0, "moderator": 1, "broadcaster": 2}


def is_authorized(message: ChatMessage, verb: str) -> bool:
    required = COMMAND_ROLES.get(verb)
    if required is None:
        return False
    return _ROLE_ORDER[role_of(message)] >= _ROLE_ORDER[required]


def mod_attempted_broadcaster_verb(message: ChatMessage, verb: str) -> bool:
    """The one denial worth answering: a real moderator making an honest
    mistake with a broadcaster-only verb."""
    return COMMAND_ROLES.get(verb) == "broadcaster" and role_of(message) == "moderator"


def parse_mute_duration(args: tuple[str, ...]) -> float | None:
    """``()`` -> 15 min; ``("30m",)``/``("2h",)`` -> seconds; ``("rest",)``
    -> +inf (until the session ends); junk -> ``None`` (invalid)."""
    if not args:
        return DEFAULT_MUTE_S
    if args[0].lower() == "rest":
        return float("inf")
    match = _DURATION_RE.match(args[0].lower())
    if not match:
        return None
    minutes, hours = match.groups()
    seconds = int(minutes) * 60.0 if minutes else int(hours) * 3600.0
    if not 60.0 <= seconds <= MAX_MUTE_S:
        return None
    return seconds


def valid_handle(candidate: str) -> str | None:
    """A handle is only ever echoed after matching ``^[0-9a-f]{4,6}$`` —
    the validator that makes replies injection-proof."""
    candidate = candidate.lower()
    return candidate if HANDLE_RE.match(candidate) else None


# --------------------------------------------------------------------------- #
# Reply templates — the ONLY strings a command may put in chat.
# --------------------------------------------------------------------------- #


def reply_help() -> str:
    return (
        "🤖 !fc status · !fc why <id> · !fc dispute <id> · mods: !fc on/off/"
        "review/mute/unmute/cap/topics/labels/retract · broadcaster: !fc "
        "enable/disable/correct/trust · !fc about"
    )


def reply_about() -> str:
    return (
        "🤖 Automated fact-checker: I transcribe the stream, pick out "
        "checkable claims, search the web, and post only well-sourced "
        "FALSE/MISLEADING results, max a few per hour. I get things wrong — "
        "!fc dispute <id> flags one for the streamer to review."
    )


def reply_status(
    policy: PostingPolicy, *, posts_this_hour: int, muted: bool, armed: bool
) -> str:
    labels = ",".join(sorted(policy.labels))
    state = "muted" if muted else ("armed" if armed else "not armed")
    return (
        f"🤖 mode={policy.mode} · {posts_this_hour}/{policy.posts_per_hour} "
        f"posts this hour · labels={labels} · {state}"
    )


def reply_enabled(policy: PostingPolicy) -> str:
    return (
        "🤖 Enabled by the broadcaster. Posting "
        f"{','.join(sorted(policy.labels))} only, max {policy.posts_per_hour}"
        f"/hour, {policy.min_gap_s:.0f}s apart. New channels start in review "
        "mode. !fc off stops it instantly; !fc about explains how it works."
    )


def reply_disabled() -> str:
    return "🤖 Disabled by the broadcaster. Leaving chat — !fc enable re-arms it."


def reply_mode(mode: str) -> str:
    if mode == "off":
        return "🤖 Chat posting is OFF and stays off until a mod runs !fc on."
    if mode == "review":
        return (
            "🤖 Review mode: verdicts queue for a human to approve. Nothing "
            "reaches chat automatically."
        )
    return "🤖 Auto mode: well-sourced FALSE/MISLEADING verdicts post automatically."


def reply_muted(duration_s: float) -> str:
    if duration_s == float("inf"):
        return (
            "🤖 Muted for the rest of the session. Overlay and logging "
            "continue; nothing goes to chat. !fc unmute to resume."
        )
    minutes = max(1, round(duration_s / 60.0))
    return (
        f"🤖 Muted for {minutes}m. Overlay and logging continue; nothing goes "
        "to chat. !fc unmute to resume."
    )


def reply_unmuted(policy: PostingPolicy) -> str:
    return (
        f"🤖 Unmuted. Back to {policy.mode} — max {policy.posts_per_hour} posts/hour."
    )


def reply_cap(posts_per_hour: int) -> str:
    return f"🤖 Cap set to {posts_per_hour} posts/hour."


def reply_topics(policy: PostingPolicy) -> str:
    enabled = len(policy.topics)
    return f"🤖 Checking {enabled} of {len(TOPICS)} topics for chat posting."


def reply_labels(policy: PostingPolicy) -> str:
    return f"🤖 Posting labels: {','.join(sorted(policy.labels))}."


def reply_trusted() -> str:
    return "🤖 Probation ended by the broadcaster — auto mode is now available."


def reply_dispute_noted(handle: str) -> str:
    return f"🤖 Noted — {handle} flagged for the streamer to review. Thanks."


def reply_broadcaster_only() -> str:
    return "🤖 !fc enable / disable / correct / trust are broadcaster-only."
