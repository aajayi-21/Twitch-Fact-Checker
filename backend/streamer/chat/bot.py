"""The channel bot: hub events in, policy-gated chat messages out.

One :class:`ChannelBot` per ``(platform, channel)``. Two long-lived loops
mirror the pipeline's task shape:

- ``_consume_loop`` — hub subscription → :func:`~streamer.chat.policy.decide`
  → format → send → record. Deliberately does NOT buffer across a disconnect:
  a verdict sent after a reconnect is about the wrong moment, so while the
  transport is down verdicts drop (and are recorded with their reason).
- ``_chat_loop`` — connect → read events (commands, notices, mod state) →
  on drop, jittered backoff → reconnect. Owns the connection; the transport
  itself never retries.

**Dry run is a separate master switch from consent.** Consent gates whether
the channel may be posted to at all; ``dry_run`` (the default) additionally
turns every would-be post into a recorded-but-unsent row. Arming a channel
does not un-dry it — the operator flips it deliberately after reading a real
stream's worth of would-have-posted messages. That reading IS the pre-launch
gate.

Design notes with safety weight:

- Every verdict produces exactly one ``chat_posts`` row (posted / queued /
  suppressed / dry_run / failed), so "why didn't it post that?" is always
  answerable from data, never from logs.
- Disputes auto-MUTE at ≥3 distinct disputers on one handle — never
  auto-retract. Auto-retraction hands chat a brigade button that forces the
  bot to publicly withdraw true statements, which is itself a clip. Silence
  is safe; retraction is a statement a human makes.
- The review queue lives in memory with a TTL: a fact-check approved six
  minutes after the claim is confusing, not helpful. Expired items are
  recorded as such.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from app.events import EventHub, SessionEvent
from app.models import Verdict
from app.sessions import SessionRegistry, channel_key

from streamer.chat import commands as cmd
from streamer.chat.consent import consent_failure
from streamer.chat.format import (
    format_correction,
    format_disclosure,
    format_retraction,
    format_verdict_post,
    verdict_handle,
)
from streamer.chat.limits import PostingLatch, SlidingWindowCap, action_for_notice
from streamer.chat.policy import (
    Decision,
    InMemoryPostHistory,
    PostContext,
    PostingPolicy,
    decide,
)
from streamer.chat.source_quality import summarize_sources
from streamer.chat.transport import (
    AuthFailed,
    ChatMessage,
    ChatTransport,
    ModStateEvent,
    NoticeEvent,
    ReconnectRequested,
)
from streamer.db import StreamerDatabase

logger = logging.getLogger(__name__)

REVIEW_TTL_S = 180.0
DISCLOSURE_EVERY_N_POSTS = 10
DISPUTE_AUTO_MUTE_THRESHOLD = 3
DISPUTE_AUTO_MUTE_S = 600.0
PROBATION_APPROVALS_NEEDED = 10
PROBATION_MAX_RETRACTIONS = 1
RECONNECT_BACKOFFS_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

# Command-reply flood control: replies must never become the spam.
REPLY_MIN_GAP_PER_USER_S = 3.0
REPLY_CAP = 10
REPLY_WINDOW_S = 300.0


@dataclass
class PendingPost:
    """One review-queue item, keyed by its chat_posts row id."""

    post_id: str
    verdict: Verdict
    event: SessionEvent
    message: str
    created_at: float  # monotonic


class ChannelBot:
    """Everything one channel's bot knows and does."""

    def __init__(
        self,
        *,
        platform: str,
        channel: str,
        transport: ChatTransport,
        db: StreamerDatabase,
        hub: EventHub,
        registry: SessionRegistry,
        allowlist: frozenset[str],
        policy: PostingPolicy | None = None,
        dry_run: bool = True,
        review_ttl_s: float = REVIEW_TTL_S,
        engagement_window_s: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.platform = platform
        self.channel = channel_key(platform, channel)[1] or channel
        self.transport = transport
        self._db = db
        self._hub = hub
        self._registry = registry
        self._allowlist = allowlist
        self.policy = policy or PostingPolicy()
        self.dry_run = dry_run
        self._review_ttl_s = review_ttl_s
        self._engagement_window_s = engagement_window_s
        self._now = now

        self.history = InMemoryPostHistory()
        self.latch = PostingLatch(now=now)
        self.muted_until: float | None = None
        self.joined_at = now()
        self.trusted = False
        self.approved_posts = 0
        self.retractions = 0
        self.disabled = False  # hard-disabled by a ban-class NOTICE
        self.review_queue: dict[str, PendingPost] = {}

        self._stop = asyncio.Event()
        # The consent row, cached per connection and refreshed by the command
        # handlers that change it (enable/disable).
        self._cached_record: Any = None
        self._posts_since_disclosure: int | None = None  # None = never posted
        self._disputers_by_handle: dict[str, set[str]] = {}
        self._reply_window = SlidingWindowCap(REPLY_CAP, REPLY_WINDOW_S, now=now)
        self._last_reply_by_user: dict[str, float] = {}
        self._chat_activity: deque[float] = deque(maxlen=500)
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def load_channel_state(self) -> None:
        """Pull persisted mode/mute/probation/config so a restart never
        re-enables what a human turned off — nor forgets what they tuned."""
        row = await self._db.fetch_chat_channel(self.platform, self.channel)
        if row is None:
            return
        self.trusted = bool(row["trusted"])
        self.approved_posts = int(row["approved_posts"])
        self.retractions = int(row["retractions"])
        # config_json first (the tuned knobs), then the mode COLUMN on top:
        # the column is the kill-switch record (!fc off writes it strictly),
        # so on any divergence the column wins.
        config_raw = row["config_json"]
        if config_raw and config_raw != "{}":
            import json

            try:
                self.policy = self.policy.with_config(json.loads(config_raw))
            except (ValueError, TypeError) as exc:
                # A bad persisted config must not brick the bot; defaults are
                # the safe fallback and the problem is logged loudly.
                logger.error(
                    "ignoring invalid persisted channel config (%s); "
                    "running on defaults",
                    exc,
                )
        mode = row["mode"]
        if mode in ("auto", "review", "off") and mode != self.policy.mode:
            self.policy = self._policy_with(mode=mode)
        muted_until = row["muted_until"]
        if muted_until == "rest":
            self.muted_until = float("inf")
        elif muted_until:
            from datetime import datetime, timezone

            deadline = datetime.strptime(muted_until, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                self.muted_until = self._now() + remaining

    async def run(self) -> None:
        """Run both loops until :meth:`stop`."""
        await self.load_channel_state()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._consume_loop(), name="bot-consume")
            group.create_task(self._chat_loop(), name="bot-chat")

    def stop(self) -> None:
        self._stop.set()
        for task in self._background:
            task.cancel()

    # ------------------------------------------------------------------ #
    # Loop 1: verdicts from the hub
    # ------------------------------------------------------------------ #

    async def _consume_loop(self) -> None:
        subscription = self._hub.subscribe(
            name=f"chat-bot:{self.channel}",
            channels={self.channel},
            types={"verdict"},
        )
        try:
            async for event in subscription:
                if self._stop.is_set():
                    return
                try:
                    await self.handle_verdict_event(event)
                except Exception:
                    logger.exception("verdict handling failed; the bot stays up")
        finally:
            subscription.close()

    async def handle_verdict_event(self, event: SessionEvent) -> None:
        """decide() → act. One chat_posts row per verdict, whatever happens."""
        verdict = Verdict.model_validate(
            {k: v for k, v in event.frame.items() if k != "type"}
        )
        decision = self._decide(verdict, event)
        if decision.action == "drop":
            await self._record(
                event, verdict, status="suppressed", reason=decision.reason
            )
            logger.info(
                "verdict %s suppressed (%s): %r",
                verdict.label,
                decision.reason,
                verdict.claim,
            )
            return
        message = format_verdict_post(
            verdict,
            template=self.policy.template,
            sources_style=self.policy.sources_style,
        )
        if message is None:
            await self._record(event, verdict, status="failed", reason="unformattable")
            return
        if decision.action == "review":
            post_id = await self._record(
                event, verdict, status="queued", reason=decision.reason, message=message
            )
            self.review_queue[post_id] = PendingPost(
                post_id=post_id,
                verdict=verdict,
                event=event,
                message=message,
                created_at=self._now(),
            )
            self._expire_stale_queue_items()
            return
        await self._post(event, verdict, message, reason=decision.reason)

    def _decide(self, verdict: Verdict, event: SessionEvent) -> Decision:
        if self.disabled:
            failure: str | None = "channel_disabled"
        else:
            failure = consent_failure(
                platform=self.platform,
                channel=self.channel,
                allowlist=self._allowlist,
                bot_is_moderator=self.transport.is_moderator,
                record=self._cached_record,
                live_session_keys=frozenset(
                    session.channel_key for session in self._registry.all()
                ),
            )
        context = PostContext(
            consent_failure=failure,
            muted_until=self.muted_until,
            joined_at=self.joined_at,
            latched=self.latch.active,
            probation_active=not self.trusted,
        )
        return decide(
            verdict=verdict,
            check_worthiness=event.check_worthiness,
            claim_age_s=event.claim_age_s,
            context=context,
            policy=self.policy,
            history=self.history,
            now=self._now(),
            self_domains=frozenset({f"{self.channel}.com", f"{self.channel}.tv"}),
        )

    async def refresh_channel_record(self) -> None:
        self._cached_record = await self._db.fetch_channel_record(
            self.platform, self.channel
        )

    async def _post(
        self,
        event: SessionEvent,
        verdict: Verdict,
        message: str,
        *,
        reason: str,
        approved_by: str | None = None,
    ) -> None:
        if self.dry_run:
            await self._record(
                event,
                verdict,
                status="dry_run",
                reason=reason,
                message=message,
                approved_by=approved_by,
            )
            logger.info("DRY RUN — would post: %s", message)
            return
        await self._maybe_send_disclosure()
        message_id = await self.transport.send(message)
        if message_id is None:
            await self._record(
                event, verdict, status="failed", reason="send_dropped", message=message
            )
            return
        self.history.record_post(verdict, now=self._now())
        if self._posts_since_disclosure is not None:
            self._posts_since_disclosure += 1
        post_id = await self._record(
            event,
            verdict,
            status="posted",
            reason=reason,
            message=message,
            message_id=message_id,
            approved_by=approved_by,
        )
        self._schedule_engagement(post_id)
        logger.info("posted to #%s: %s", self.channel, message)

    async def _maybe_send_disclosure(self) -> None:
        """Before the first real post of a session, and every N posts after:
        chat gets told a bot is speaking and that it can be wrong."""
        if self._posts_since_disclosure is None:
            self._posts_since_disclosure = 0
        elif self._posts_since_disclosure < DISCLOSURE_EVERY_N_POSTS:
            return
        disclosure = format_disclosure()
        if disclosure is not None:
            await self.transport.send(disclosure)
        self._posts_since_disclosure = 0

    async def _record(
        self,
        event: SessionEvent,
        verdict: Verdict,
        *,
        status: str,
        reason: str,
        message: str | None = None,
        message_id: str | None = None,
        approved_by: str | None = None,
        post_id: str | None = None,
    ) -> str:
        summary = summarize_sources(verdict.sources)
        from app.models import utc_now_iso

        return await self._db.record_chat_post(
            post_id=post_id,
            verdict_id=verdict.id,
            claim_id=event.claim_id,
            session_id=event.session_id,
            platform=self.platform,
            channel=self.channel,
            handle=verdict_handle(verdict),
            status=status,
            reason=reason,
            mode=self.policy.mode,
            label=verdict.label,
            topic=verdict.topic,
            check_worthiness=event.check_worthiness,
            source_count=len(verdict.sources),
            distinct_domains=summary.distinct_domains,
            best_tier=summary.best_tier,
            used_fallback=verdict.used_fallback,
            claim_age_s=event.claim_age_s,
            stream_time_s=event.stream_time_s,
            message_text=message,
            message_id=message_id,
            posted_at=utc_now_iso() if status == "posted" else None,
            approved_by_user_id=approved_by,
        )

    # ------------------------------------------------------------------ #
    # Review queue (driven by the control panel via the app's routes)
    # ------------------------------------------------------------------ #

    def _expire_stale_queue_items(self) -> None:
        cutoff = self._now() - self._review_ttl_s
        for post_id in [
            pid
            for pid, pending in self.review_queue.items()
            if pending.created_at <= cutoff
        ]:
            pending = self.review_queue.pop(post_id)
            task = asyncio.ensure_future(
                self._record(
                    pending.event,
                    pending.verdict,
                    status="expired",
                    reason="review_ttl",
                    message=pending.message,
                    post_id=post_id,
                )
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def approve(self, post_id: str, *, approved_by: str) -> bool:
        """Panel click: post a queued item now. False = unknown or expired."""
        pending = self.review_queue.pop(post_id, None)
        if pending is None:
            return False
        if self._now() - pending.created_at > self._review_ttl_s:
            await self._record(
                pending.event,
                pending.verdict,
                status="expired",
                reason="review_ttl",
                message=pending.message,
                post_id=post_id,
            )
            return False
        await self._post(
            pending.event,
            pending.verdict,
            pending.message,
            reason="approved",
            approved_by=approved_by,
        )
        await self._register_approval()
        return True

    async def skip(self, post_id: str, *, skipped_by: str) -> bool:
        pending = self.review_queue.pop(post_id, None)
        if pending is None:
            return False
        await self._record(
            pending.event,
            pending.verdict,
            status="skipped",
            reason="review_skipped",
            message=pending.message,
            post_id=post_id,
        )
        return True

    async def _register_approval(self) -> None:
        """Probation bookkeeping: graduate after enough approvals with at
        most one retraction."""
        self.approved_posts += 1
        fields: dict[str, Any] = {"approved_posts": self.approved_posts}
        if (
            not self.trusted
            and self.approved_posts >= PROBATION_APPROVALS_NEEDED
            and self.retractions <= PROBATION_MAX_RETRACTIONS
        ):
            self.trusted = True
            fields["trusted"] = 1
            logger.info(
                "channel #%s graduated probation (%d approvals, %d retractions)",
                self.channel,
                self.approved_posts,
                self.retractions,
            )
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, **fields
        )

    # ------------------------------------------------------------------ #
    # Loop 2: the chat connection
    # ------------------------------------------------------------------ #

    async def _chat_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self.transport.connect()
                self.joined_at = self._now()
                await self.refresh_channel_record()
                attempt = 0
                async for chat_event in self.transport.events():
                    if self._stop.is_set():
                        return
                    await self.handle_chat_event(chat_event)
            except AuthFailed:
                logger.error(
                    "chat auth failed for #%s — the token is dead; refresh it "
                    "or reconnect Twitch from the control panel",
                    self.channel,
                )
                attempt = len(RECONNECT_BACKOFFS_S) - 1  # back off long
            except ReconnectRequested:
                logger.info("Twitch asked for a reconnect; complying")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("chat connection lost (%s); will reconnect", exc)
            finally:
                await self.transport.close()
            if self._stop.is_set() or self.disabled:
                return
            backoff = RECONNECT_BACKOFFS_S[min(attempt, len(RECONNECT_BACKOFFS_S) - 1)]
            attempt += 1
            await self._sleep_or_stop(backoff * random.uniform(0.8, 1.2))

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def handle_chat_event(
        self, chat_event: ChatMessage | NoticeEvent | ModStateEvent
    ) -> None:
        if isinstance(chat_event, ChatMessage):
            self._chat_activity.append(self._now())
            command = cmd.parse_command(chat_event.text)
            if command is not None:
                await self.handle_command(chat_event, command)
        elif isinstance(chat_event, NoticeEvent):
            self.handle_notice(chat_event)
        # ModStateEvent needs no action here: the transport tracks
        # is_moderator, and the next consent check reads it fresh.

    def handle_notice(self, notice: NoticeEvent) -> None:
        action = action_for_notice(notice.msg_id)
        if action.kind == "latch":
            self.latch.trip(action.latch_s, action.note)
            log = logger.error if action.alert else logger.info
            log(
                "Twitch rejected a message (%s); posting latched for %.0fs",
                action.note,
                action.latch_s,
            )
        elif action.kind == "disable":
            self.disabled = True
            logger.error(
                "channel #%s HARD-DISABLED (%s) — a banned bot that keeps "
                "trying is an evasion pattern; a human must re-enable",
                self.channel,
                action.note,
            )
        elif action.kind == "drop":
            logger.info("Twitch dropped a message (%s)", action.note or notice.msg_id)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    async def handle_command(self, message: ChatMessage, command: cmd.Command) -> None:
        authorized = cmd.is_authorized(message, command.verb)
        await self._db.record_chat_command(
            platform=self.platform,
            channel=self.channel,
            user_id=message.user_id,
            role=cmd.role_of(message),
            command=command.verb,  # THE VERB ONLY — args never persist
            accepted=authorized,
        )
        if not authorized:
            # Viewers get silence (loud denial is a spam amplifier); a mod
            # attempting a broadcaster verb gets one gentle correction.
            if cmd.mod_attempted_broadcaster_verb(message, command.verb):
                await self._reply(message, cmd.reply_broadcaster_only())
            return
        handler = getattr(self, f"_cmd_{command.verb}", None)
        if handler is not None:
            await handler(message, command.args)

    async def _reply(self, message: ChatMessage, text: str | None) -> None:
        """Flood-controlled reply. Replies ride the transport budget too."""
        if text is None or self.dry_run:
            return
        last = self._last_reply_by_user.get(message.user_id)
        if last is not None and self._now() - last < REPLY_MIN_GAP_PER_USER_S:
            return
        if not self._reply_window.allows():
            return
        self._last_reply_by_user[message.user_id] = self._now()
        self._reply_window.record()
        await self.transport.send(text)

    def _policy_with(self, **changes: Any) -> PostingPolicy:
        # Single validated path: !fc, the panel, and the persisted config all
        # build policies through with_config, so no route can skip a clamp.
        return self.policy.with_config(changes)

    async def _persist_mode(self, mode: str) -> None:
        self.policy = self._policy_with(mode=mode)
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, mode=mode
        )

    async def apply_settings(self, changes: dict[str, Any]) -> None:
        """The control panel's settings write: validate, swap, persist.

        Raises :class:`PolicyConfigError` on unknown keys or clamp
        violations — the caller turns that into a 400 with the exact message,
        so the panel shows WHY a value was refused instead of clamping it
        silently (a silently-mutated safety setting is a lie in the UI).
        """
        new_policy = self.policy.with_config(changes)
        self.policy = new_policy
        fields: dict[str, Any] = {"config_json": self._config_json()}
        if "mode" in changes:
            # The mode column is the kill-switch record; keep it in step.
            fields["mode"] = new_policy.mode
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, **fields
        )

    async def set_trusted(self, trusted: bool) -> None:
        """End (or reinstate) probation from the panel. Loud on purpose."""
        self.trusted = trusted
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, trusted=int(trusted)
        )
        logger.warning(
            "probation %s for #%s via control panel",
            "ENDED" if trusted else "REINSTATED",
            self.channel,
        )

    async def retract(self, handle: str, *, by: str) -> bool:
        """Retract a posted verdict: fixed public wording + a feedback row.

        Shared by ``!fc retract`` and the panel's Retract button; the appeal
        path and the eval set are the same mechanism either way.
        """
        row = await self._db.fetch_chat_post_by_handle(
            self.platform, self.channel, handle
        )
        if row is None or row["status"] != "posted" or row["retracted_at"]:
            return False
        retraction = format_retraction(handle)
        if retraction is not None and not self.dry_run:
            await self.transport.send(retraction)
        await self._db.mark_chat_post_retracted(
            post_id=row["id"], retracted_by_user_id=by
        )
        self.retractions += 1
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, retractions=self.retractions
        )
        if row["verdict_id"]:
            await self._db.record_feedback(row["verdict_id"], "down", None, None)
        return True

    # -- viewer verbs ----------------------------------------------------- #

    async def _cmd_help(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        await self._reply(message, cmd.reply_help())

    async def _cmd_about(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        await self._reply(message, cmd.reply_about())

    async def _cmd_status(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        record = self._cached_record
        await self._reply(
            message,
            cmd.reply_status(
                self.policy,
                posts_this_hour=self.history.posts_in_window(3600.0, now=self._now()),
                muted=self.muted_until is not None and self._now() < self.muted_until,
                armed=record is not None and record.armed,
            ),
        )

    async def _cmd_why(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        handle = cmd.valid_handle(args[0]) if args else None
        if handle is None:
            return  # a non-hex handle gets NO reply — nothing to echo
        row = await self._db.fetch_chat_post_by_handle(
            self.platform, self.channel, handle
        )
        if row is None or not row["message_text"]:
            return
        # The stored text is exactly what policy approved — resending it
        # verbatim is the only reply that cannot say anything new.
        await self._reply(message, row["message_text"])

    async def _cmd_dispute(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        handle = cmd.valid_handle(args[0]) if args else None
        if handle is None:
            return
        row = await self._db.fetch_chat_post_by_handle(
            self.platform, self.channel, handle
        )
        if row is None:
            return
        disputers = self._disputers_by_handle.setdefault(handle, set())
        disputers.add(message.user_id)
        if row["verdict_id"]:
            # The dispute note goes to the db and the panel — NEVER back to
            # chat. Feedback rows are the seed of the eval set.
            await self._db.record_feedback(row["verdict_id"], "down", None, None)
        await self._reply(message, cmd.reply_dispute_noted(handle))
        if len(disputers) >= DISPUTE_AUTO_MUTE_THRESHOLD:
            # Auto-MUTE, never auto-retract: silence is safe, retraction is a
            # statement — and an auto-retract is a brigade button.
            self.muted_until = self._now() + DISPUTE_AUTO_MUTE_S
            logger.error(
                "auto-muted #%s for %.0fs: %d distinct disputers on %s — "
                "operator review needed",
                self.channel,
                DISPUTE_AUTO_MUTE_S,
                len(disputers),
                handle,
            )

    # -- moderator verbs --------------------------------------------------- #

    async def _cmd_on(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        self.muted_until = None
        await self._persist_mode("auto")
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, muted_until=None
        )
        await self._reply(message, cmd.reply_mode("auto"))

    async def _cmd_off(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        await self._persist_mode("off")
        await self._reply(message, cmd.reply_mode("off"))

    async def _cmd_review(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        await self._persist_mode("review")
        await self._reply(message, cmd.reply_mode("review"))

    async def _cmd_mute(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        duration = cmd.parse_mute_duration(args)
        if duration is None:
            return
        self.muted_until = self._now() + duration
        if duration == float("inf"):
            persisted = "rest"
        else:
            from datetime import datetime, timedelta, timezone

            persisted = (
                datetime.now(timezone.utc) + timedelta(seconds=duration)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Persisted as an ABSOLUTE deadline so a restart cannot resurrect
        # posting before the human-chosen time.
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, muted_until=persisted
        )
        await self._reply(message, cmd.reply_muted(duration))

    async def _cmd_unmute(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        self.muted_until = None
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, muted_until=None
        )
        await self._reply(message, cmd.reply_unmuted(self.policy))

    async def _cmd_cap(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        if not args or not args[0].isdigit():
            return
        try:
            self.policy = self._policy_with(posts_per_hour=int(args[0]))
        except ValueError:
            return  # clamp violation: silently ignored, never echoed
        await self._db.upsert_chat_channel(
            platform=self.platform,
            channel=self.channel,
            config_json=self._config_json(),
        )
        await self._reply(message, cmd.reply_cap(self.policy.posts_per_hour))

    async def _cmd_topics(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        from app.models import TOPICS

        if len(args) == 2 and args[1].lower() in ("on", "off"):
            slug, state = args[0].lower(), args[1].lower()
            if slug not in TOPICS:
                return
            topics = set(self.policy.topics)
            (topics.add if state == "on" else topics.discard)(slug)
            try:
                self.policy = self._policy_with(topics=frozenset(topics))
            except ValueError:
                return
            await self._db.upsert_chat_channel(
                platform=self.platform,
                channel=self.channel,
                config_json=self._config_json(),
            )
        await self._reply(message, cmd.reply_topics(self.policy))

    async def _cmd_labels(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        mapping = {
            "false": frozenset({"FALSE"}),
            "both": frozenset({"FALSE", "MISLEADING"}),
        }
        if not args or args[0].lower() not in mapping:
            return
        self.policy = self._policy_with(labels=mapping[args[0].lower()])
        await self._db.upsert_chat_channel(
            platform=self.platform,
            channel=self.channel,
            config_json=self._config_json(),
        )
        await self._reply(message, cmd.reply_labels(self.policy))

    async def _cmd_retract(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        handle = cmd.valid_handle(args[0]) if args else None
        if handle is None:
            return
        await self.retract(handle, by=message.user_id)

    # -- broadcaster verbs -------------------------------------------------- #

    async def _cmd_enable(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        from app.models import utc_now_iso

        await self._db.upsert_chat_channel(
            platform=self.platform,
            channel=self.channel,
            armed_at=utc_now_iso(),
            armed_by_user_id=message.user_id,
            room_id=message.room_id,
            consent_method="mod+command",
            disarmed_at=None,
        )
        await self.refresh_channel_record()
        await self._reply(message, cmd.reply_enabled(self.policy))

    async def _cmd_disable(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        from app.models import utc_now_iso

        await self._db.upsert_chat_channel(
            platform=self.platform,
            channel=self.channel,
            disarmed_at=utc_now_iso(),
            mode="off",
        )
        self.policy = self._policy_with(mode="off")
        await self.refresh_channel_record()
        await self._reply(message, cmd.reply_disabled())
        # Consent revoked: leave the room entirely.
        self.stop()

    async def _cmd_correct(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        handle = cmd.valid_handle(args[0]) if args else None
        label = args[1].upper() if len(args) > 1 else ""
        if handle is None or label not in cmd.CORRECTION_LABELS:
            return
        row = await self._db.fetch_chat_post_by_handle(
            self.platform, self.channel, handle
        )
        if row is None or row["status"] != "posted" or row["retracted_at"]:
            return
        correction = format_correction(handle, label)  # type: ignore[arg-type]
        if correction is not None and not self.dry_run:
            await self.transport.send(correction)
        await self._db.mark_chat_post_retracted(
            post_id=row["id"], retracted_by_user_id=message.user_id
        )
        self.retractions += 1
        await self._db.upsert_chat_channel(
            platform=self.platform,
            channel=self.channel,
            retractions=self.retractions,
        )
        if row["verdict_id"]:
            await self._db.record_feedback(row["verdict_id"], "down", label, None)

    async def _cmd_trust(self, message: ChatMessage, args: tuple[str, ...]) -> None:
        self.trusted = True
        await self._db.upsert_chat_channel(
            platform=self.platform, channel=self.channel, trusted=1
        )
        await self._reply(message, cmd.reply_trusted())

    # ------------------------------------------------------------------ #
    # Engagement measurement (counts only, never text)
    # ------------------------------------------------------------------ #

    def _schedule_engagement(self, post_id: str) -> None:
        before = self._count_activity_in(self._engagement_window_s)
        task = asyncio.ensure_future(self._measure_engagement(post_id, before))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _measure_engagement(self, post_id: str, before: int) -> None:
        await asyncio.sleep(self._engagement_window_s)
        await self._db.record_chat_engagement(
            post_id=post_id,
            window_s=int(self._engagement_window_s),
            messages_before=before,
            messages_after=self._count_activity_in(self._engagement_window_s),
            unique_chatters_after=0,  # ids not retained; counts only
        )

    def _count_activity_in(self, window_s: float) -> int:
        cutoff = self._now() - window_s
        return sum(1 for stamp in self._chat_activity if stamp > cutoff)

    def _config_json(self) -> str:
        """The FULL policy, so a restart restores every tuned knob."""
        import json

        return json.dumps(self.policy.to_config())
