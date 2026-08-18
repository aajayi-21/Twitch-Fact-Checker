# Streamer chat bot — pre-launch checklist

Nothing points at a real live channel until every box is checked. Ordered so
each item is verifiable by someone other than the person who configured it.

The reasoning behind the gates lives in `business-analysis.md` §6 ("a wrong
public verdict is a product-killing clip") and `streamer-stage2-plan.md`;
this file is just the boxes.

## Code invariants (verified by a green suite; the named test is the proof)

- [ ] `cd backend && uv run pytest -m "not slow"` — fully green.
- [ ] No outbound message can begin with `/`, `.`, or `!`, or contain CR/LF
      (`test_chat_format.py::TestCommandInjectionGuard`) — the bot is a
      moderator, so a leading `/` would EXECUTE as a moderation command.
- [ ] No user text is ever echoed into an outbound message
      (`test_chat_bot.py::…::test_dispute_note_is_never_echoed`,
      `test_chat_commands.py::TestHandleValidation`).
- [ ] Each consent proof failing individually blocks posting
      (`test_chat_consent.py`, `test_chat_bot.py::TestConsentGates`).
- [ ] UNVERIFIED / fallback-parsed / zero-source verdicts can never post and
      config cannot change that (`test_chat_policy.py::TestClamps`).
- [ ] The PASS line never appears un-redacted in logs
      (`test_chat_transport.py::TestHandshake::test_pass_is_redacted…`).

## The bot account

- [ ] A dedicated account with an obviously-bot name (`…factcheck` /
      `…factbot`) — never a human-looking name.
- [ ] Profile bio says what it is and that it can be wrong.
- [ ] Verified email AND phone (channels with those requirements silently
      reject messages otherwise — the `msg_verified_email` /
      `msg_requires_verified_phone_number` NOTICEs).
- [ ] Token scopes are exactly `chat:read chat:edit` — **no API-level
      moderation scopes.** The bot holds mod STATUS in-channel (rate tier,
      link permissions, consent proof); it must not hold moderation POWER.
- [ ] `TWITCH_CHAT_CHANNELS` lists only the pilot channel(s).

## Consent (per channel, before the first join)

- [ ] Written consent from the broadcaster (message/email), kept outside the
      database: what the bot posts, that it can be wrong, that decisions are
      logged, how to stop it.
- [ ] The broadcaster personally granted mod (`/mod <bot>`) — the panel's
      "Mod the bot" step is green.
- [ ] The broadcaster personally typed `!fc enable`
      (`chat_channels.armed_at` / `armed_by_user_id` populated).
- [ ] The broadcaster AND at least one mod have each run `!fc mute` and
      `!fc off` and seen the confirmation.
- [ ] The streamer has been told, explicitly: **mute before reaction
      content, clips, guests, and ad reads** — the pipeline cannot tell
      third-party audio from the streamer's own voice.

## Dry run (mandatory — no exceptions)

- [ ] `CHAT_DRY_RUN=true` for at least one FULL real stream.
- [ ] A human read EVERY row in the panel's Recent decisions feed —
      especially every `dry_run` row (the exact would-have-posted message).
      Target: zero messages the reviewer would be embarrassed by. Any
      embarrassment → tighten policy, repeat the dry run.
- [ ] The drop-reason histogram (`GET /stats/chat`) has been read. A large
      `hourly_cap` share means the content bar is too loose — not that the
      cap is too low.
- [ ] Probation is intact: the channel is NOT `trusted` and NOT in `auto`
      unless the broadcaster explicitly ran `!fc trust`.

## Operations

- [ ] The kill switch is reachable in under 30 seconds without SSH: the
      panel's MUTE button (as an OBS dock) AND `!fc off` from the
      broadcaster's phone.
- [ ] Someone is watching the panel during the first live stream, full
      session.
- [ ] The wrong-verdict budget is agreed IN WRITING before launch (the
      business analysis's kill criterion: ≤1 disputed-wrong public verdict
      per 10 streams) — including who decides and what pauses the bot.
- [ ] `backend/streamer.db` is backed up — `chat_channels` holds the consent
      records.
- [ ] Rollback rehearsed once: `!fc off`, and separately
      `CHAT_DRY_RUN=true` + restart.

## The first live stream

- [ ] Review mode regardless of settings (probation enforces this for new
      channels); the operator approves/skips by hand all session.
- [ ] Post-stream: read every posted message together with the streamer
      within 24 h; disagreements become `!fc wrong <id>` retractions (which
      feed the eval set automatically).
