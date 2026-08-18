"""``fact-checker-ingest`` — the streamer's pre-stream one-liner.

Pump loop only: sources produce 250 ms PCM frames, the socket ships them and
prints verdicts. Respawn logic lives in the source, reconnect logic in the
socket; this file owns argument parsing, source selection, output rendering,
and the graceful Ctrl-C (stop frame → wait for the server's flush → exit 130,
matching the old ``stream_wav.py`` contract).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from app.models import TOPICS

from streamer.ingest import DEFAULT_URL
from streamer.ingest.client import IngestSocket, build_hello
from streamer.ingest.sources import (
    AudioSource,
    DeviceSource,
    Identity,
    StreamlinkSource,
    WavSource,
    derive_identity,
    list_devices,
)

STOP_FLUSH_TIMEOUT_S = 10.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fact-checker-ingest",
        description=(
            "Stream a live channel's audio (or a local device, or a WAV "
            "fixture) into the fact-checker backend."
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="",
        help="channel or URL (twitch.tv/foo, kick.com/foo, bare 'foo'), or a "
        "WAV path with --source wav",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "streamlink", "device", "wav"],
        default="auto",
        help="audio source (default: auto — .wav files play back, anything "
        "else goes through streamlink)",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"default: {DEFAULT_URL}")
    parser.add_argument(
        "--platform", choices=sorted({"twitch", "youtube", "kick", "rumble"})
    )
    parser.add_argument("--channel", help="override the derived channel name")
    parser.add_argument("--title", help="stream title for the session record")
    parser.add_argument(
        "--sensitivity", choices=["low", "medium", "high"], default="medium"
    )
    parser.add_argument(
        "--topics",
        help="comma-separated topic slugs to fact-check (default: all)",
    )
    parser.add_argument(
        "--transcripts", action="store_true", help="print live transcript lines"
    )
    parser.add_argument("--quality", default="audio_only,worst")
    parser.add_argument(
        "--streamlink-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument passed through to streamlink (repeatable), "
        "e.g. --streamlink-arg=--twitch-low-latency",
    )
    parser.add_argument("--device", help="input device name or index")
    parser.add_argument(
        "--list-devices", action="store_true", help="list audio devices and exit"
    )
    parser.add_argument("--speed", type=float, default=1.0, help="wav pacing")
    parser.add_argument(
        "--json",
        action="store_true",
        help="NDJSON: one raw server frame per line on stdout",
    )
    return parser


def resolve_source(args: argparse.Namespace) -> tuple[AudioSource, Identity]:
    choice = args.source
    if choice == "auto":
        if args.target.lower().endswith(".wav") and Path(args.target).exists():
            choice = "wav"
        elif args.target:
            choice = "streamlink"
        else:
            raise SystemExit(
                "error: give a channel/URL, or --source device, or a .wav path"
            )
    if choice == "wav":
        source: AudioSource = WavSource(Path(args.target), speed=args.speed)
        identity = Identity(None, None)
    elif choice == "device":
        source = DeviceSource(args.device)
        identity = Identity(None, None)
    else:
        source = StreamlinkSource(
            args.target, quality=args.quality, extra_args=args.streamlink_arg
        )
        identity = derive_identity(args.target)
    if args.platform:
        identity = Identity(args.platform, identity.channel)
    if args.channel:
        identity = Identity(
            identity.platform or "twitch", args.channel.strip().lstrip("#").lower()
        )
    if identity.channel is None and choice != "wav":
        print(
            "warning: no --channel — this session will be invisible on the "
            "dashboard's Channels card, and the chat bot cannot bind to it.",
            file=sys.stderr,
        )
    return source, identity


def parse_topics(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    slugs = [slug.strip().lower() for slug in raw.split(",") if slug.strip()]
    unknown = sorted(set(slugs) - set(TOPICS))
    if unknown:
        raise SystemExit(f"error: unknown topics {unknown}; valid: {', '.join(TOPICS)}")
    return slugs


LABEL_MARKS = {"TRUE": "✅", "FALSE": "❌", "MISLEADING": "⚠️ ", "UNVERIFIED": "❓"}


def make_frame_printer(args: argparse.Namespace):
    def render(frame: dict) -> None:
        if args.json:
            print(json.dumps(frame, ensure_ascii=False), flush=True)
            return
        kind = frame.get("type")
        if kind == "verdict":
            mark = LABEL_MARKS.get(frame.get("label", ""), "•")
            sources = ", ".join(
                source.get("url", "").split("/")[2].removeprefix("www.")
                for source in frame.get("sources", [])
                if source.get("url", "").startswith("http")
            )
            print(
                f"{mark} {frame.get('label'):<11} {frame.get('claim')!r}"
                + (f"  [{sources}]" if sources else ""),
                flush=True,
            )
        elif kind == "status" and frame.get("stage") == "verifying":
            print(f"…  checking    {frame.get('claim')!r}", flush=True)
        elif kind == "contradiction":
            print(
                f"🕰  contradiction: earlier {frame.get('prior_claim')!r} vs "
                f"now {frame.get('current_claim')!r}",
                flush=True,
            )
        elif kind == "transcript" and args.transcripts:
            print(f"    {frame.get('text', '')}", file=sys.stderr, flush=True)

    return render


async def run(args: argparse.Namespace) -> int:
    source, identity = resolve_source(args)
    hello = build_hello(
        identity=identity,
        stream_title=args.title,
        sensitivity=args.sensitivity,
        enabled_topics=parse_topics(args.topics),
        send_transcripts=bool(args.transcripts),
    )
    exit_code = 0
    stop_event = asyncio.Event()

    def fail(code: int, message: str) -> None:
        nonlocal exit_code
        exit_code = code
        print(f"error: {message}", file=sys.stderr)
        stop_event.set()

    socket = IngestSocket(args.url, hello, on_frame=make_frame_printer(args), fail=fail)
    print(
        f"note: source={source.name} {source.describe()} -> {args.url} "
        f"(channel={identity.channel or '-'})",
        file=sys.stderr,
    )

    loop = asyncio.get_running_loop()
    interrupts = 0

    def on_interrupt() -> None:
        nonlocal interrupts
        interrupts += 1
        if interrupts >= 2:  # second Ctrl-C: hard exit
            raise SystemExit(130)
        print(
            "note: stopping — sending the stop frame and waiting for the "
            "server to flush (Ctrl-C again to hard-exit)",
            file=sys.stderr,
        )
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, on_interrupt)
    except NotImplementedError:  # pragma: no cover - Windows Proactor loop
        pass

    reader = asyncio.ensure_future(socket.run_reader())

    async def pump() -> None:
        async for frame in source.frames():
            if stop_event.is_set():
                return
            await socket.send_pcm(frame)
        stop_event.set()  # finite source (wav) ended

    pumper = asyncio.ensure_future(pump())
    await stop_event.wait()
    pumper.cancel()
    await source.aclose()
    if exit_code == 0:
        # Graceful path: stop frame, then let the server flush its last
        # verdicts (they are paid for; the reader prints them).
        await socket.send_stop()
        try:
            await asyncio.wait_for(reader, timeout=STOP_FLUSH_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            pass
    socket.stopping = True
    reader.cancel()
    await socket.close()
    if socket.dropped_seconds > 0:
        print(
            f"note: {socket.dropped_seconds:.1f}s of audio dropped while "
            "disconnected",
            file=sys.stderr,
        )
    return exit_code if exit_code else (130 if interrupts else 0)


def main() -> None:
    args = build_parser().parse_args()
    if args.list_devices:
        print(list_devices())
        return
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:  # pragma: no cover - Windows fallback path
        print("note: interrupted", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
