"""The STREAMER product: fact-checks in the broadcaster's own chat and stream.

Deliberately a separate package, a separate app, and a separate port from the
viewer tool in :mod:`app`, so the two can be run side by side and compared.

| | viewer (``app.main:app``) | streamer (``streamer.main:app``) |
|---|---|---|
| launcher | ``backend/run.sh`` | ``backend/run-streamer.sh`` |
| port | 8710 | 8711 |
| database | ``fact_checker.db`` | ``streamer.db`` |
| audio in | browser extension (tabCapture) | ingest CLI (streamlink / device) |
| verdicts out | one viewer's private overlay | Twitch chat + OBS overlay + panel |
| audience | the person running it | the whole channel |

The streamer app is a SUPERSET: it mounts the shared routers (``ws``,
``setup``, ``stats``, ``feedback``, ``debug``) alongside its own, so it is
self-contained. Everything it borrows from :mod:`app` — the pipeline, the
transcriber, the fact checker, the database, the wire models — is imported,
never forked. The one seam it needs was added to the shared core:
:class:`app.events.EventHub`.

**Why the split is real and not cosmetic.** The viewer tool shows a verdict to
one person who chose to see it. This tool makes an automated pipeline speak
publicly, in a named person's chat, in front of their audience. That difference
is the whole reason `docs/improvement-report.md` §3.1 could reject a public
leaderboard while endorsing this: a consenting broadcaster is in the loop.
Everything under :mod:`streamer.chat` exists to keep that consent real and to
make the bot's public statements defensible.
"""

STREAMER_VERSION = "0.1.0"
