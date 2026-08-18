"""Audio sources: streamlink+ffmpeg, a local device, or a WAV fixture.

One contract: an :class:`AudioSource` yields Int16LE mono 16 kHz frames of
``FRAME_SAMPLES`` samples. **Respawn lives inside the source; reconnect lives
inside the socket client; the CLI is a dumb pump between them** — neither
layer knows about the other's failures.

Security rule with a test on it: the streamlink pipeline is built with
``os.pipe()`` + ``create_subprocess_exec`` — NEVER a shell. A channel name is
user input; it is validated against Twitch's login alphabet before it enters
argv, and no string this module handles ever passes through ``/bin/sh``.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import re
import shutil
import sys
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from streamer.ingest import FRAME_BYTES, FRAME_SAMPLES, FRAME_SECONDS, SAMPLE_RATE

logger = logging.getLogger(__name__)

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")

PLATFORM_BY_HOSTNAME = {
    "twitch.tv": "twitch",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "kick.com": "kick",
    "rumble.com": "rumble",
}

_BINARY_HINTS = {
    "ffmpeg": (
        "  Debian/Ubuntu  sudo apt install ffmpeg\n"
        "  Fedora         sudo dnf install ffmpeg\n"
        "  macOS          brew install ffmpeg\n"
        "  Windows        winget install Gyan.FFmpeg"
    ),
    "streamlink": (
        "  Easiest        cd backend && uv sync --extra ingest\n"
        "  Or             pipx install streamlink  /  brew install streamlink"
    ),
}


class SourceError(SystemExit):
    """Fatal source problem with a human message (exits 1)."""

    def __init__(self, message: str) -> None:
        super().__init__(f"error: {message}")


def require_binaries(*names: str) -> None:
    """Fail fast, before anything expensive, with per-OS install hints."""
    for name in names:
        if shutil.which(name) is None:
            hint = _BINARY_HINTS.get(name, "")
            raise SourceError(f"`{name}` was not found on your PATH.\n{hint}")


@dataclass(frozen=True, slots=True)
class Identity:
    """What the hello frame carries so the session shows up on /dashboard."""

    platform: str | None
    channel: str | None


def derive_identity(target: str) -> Identity:
    """Bare ``foo`` -> twitch/foo; URLs -> hostname map + first path segment.

    YouTube/Rumble paths carry video ids, not channel names, so those yield
    platform-only identity (mirrors the extension's derivation).
    """
    text = target.strip()
    if not text:
        return Identity(None, None)
    if "/" not in text and CHANNEL_RE.match(text):
        return Identity("twitch", text.lower())
    candidate = text if "//" in text else f"https://{text}"
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(candidate)
    except ValueError:
        return Identity(None, None)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = PLATFORM_BY_HOSTNAME.get(host)
    if platform is None:
        return Identity(None, None)
    if platform in ("twitch", "kick"):
        segments = [part for part in parsed.path.split("/") if part]
        if segments and CHANNEL_RE.match(segments[0]):
            return Identity(platform, segments[0].lower())
    return Identity(platform, None)


class AudioSource(abc.ABC):
    """Yields ``FRAME_SAMPLES``-sample Int16LE frames until permanently done.

    A live source loops internally across subprocess respawns; only a finite
    source (the WAV fixture) ever ends its iterator.
    """

    name: str = "source"

    @abc.abstractmethod
    def describe(self) -> str: ...

    @abc.abstractmethod
    def frames(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None:  # pragma: no cover - overridden as needed
        return None


# --------------------------------------------------------------------------- #
# WAV fixture (the old scripts/stream_wav.py path, moved verbatim)
# --------------------------------------------------------------------------- #


def load_pcm_16k_mono(wav_path: Path) -> np.ndarray:
    """Load a WAV as int16 mono 16 kHz samples, converting when necessary."""
    with wave.open(str(wav_path), "rb") as reader:
        sample_width = reader.getsampwidth()
        if sample_width != 2:
            raise SourceError(
                f"{wav_path} is {sample_width * 8}-bit; only 16-bit PCM WAV is "
                "supported (regenerate with scripts/make_fixture_wav.py)"
            )
        channels = reader.getnchannels()
        source_rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        print(f"note: downmixing {channels} channels to mono", file=sys.stderr)
        samples = samples.reshape(-1, channels).mean(axis=1)
    if source_rate != SAMPLE_RATE:
        print(
            f"note: resampling {source_rate} Hz -> {SAMPLE_RATE} Hz",
            file=sys.stderr,
        )
        duration_s = len(samples) / source_rate
        target_count = int(round(duration_s * SAMPLE_RATE))
        source_times = np.arange(len(samples), dtype=np.float64) / source_rate
        target_times = np.arange(target_count, dtype=np.float64) / SAMPLE_RATE
        samples = np.interp(target_times, source_times, samples)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")


class WavSource(AudioSource):
    """Finite fixture playback, paced at ``speed``× real time."""

    name = "wav"

    def __init__(self, path: Path, *, speed: float = 1.0) -> None:
        if speed <= 0:
            raise SourceError("--speed must be > 0")
        self._path = path
        self._speed = speed
        self._samples = load_pcm_16k_mono(path)

    def describe(self) -> str:
        duration_s = len(self._samples) / SAMPLE_RATE
        return f"{self._path} ({duration_s:.1f}s) at {self._speed}x"

    async def frames(self) -> AsyncIterator[bytes]:
        started = time.monotonic()
        sent = 0
        for offset in range(0, len(self._samples), FRAME_SAMPLES):
            yield self._samples[offset : offset + FRAME_SAMPLES].tobytes()
            sent += 1
            target = started + sent * (FRAME_SECONDS / self._speed)
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# streamlink | ffmpeg
# --------------------------------------------------------------------------- #


def streamlink_argv(target: str, quality: str, extra: list[str]) -> list[str]:
    """The exact argv (golden-tested: proves no shell is ever involved).

    ``--retry-streams 30 --retry-max 0`` moves the "streamer is offline" wait
    INTO streamlink, which turns any streamlink exit back into a genuine
    error and deletes an entire retry layer here.
    """
    return [
        "streamlink",
        "--stdout",
        "--twitch-disable-ads",
        "--retry-streams",
        "30",
        "--retry-max",
        "0",
        *extra,
        target,
        quality,
    ]


def ffmpeg_argv() -> list[str]:
    # -nostdin is mandatory: without it ffmpeg eats the terminal's stdin and
    # breaks Ctrl-C for the whole process group.
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]


class StreamlinkSource(AudioSource):
    """Live platform audio: streamlink → (kernel pipe) → ffmpeg → PCM.

    ``audio_only,worst`` because audio_only is a transcode that source-only
    (often non-partner) streams lack. Respawns forever with 0.5→15 s backoff
    — the user WANTS it to survive an ad break, a raid, and a 3 a.m. router
    reboot; every retry prints loudly so a wedged state is never silent.
    """

    name = "streamlink"

    def __init__(
        self,
        target: str,
        *,
        quality: str = "audio_only,worst",
        extra_args: list[str] | None = None,
    ) -> None:
        identity = derive_identity(target)
        if identity.platform == "twitch" and identity.channel is None:
            raise SourceError(f"cannot parse a Twitch channel from {target!r}")
        require_binaries("streamlink", "ffmpeg")
        self._target = (
            target if "//" in target or "." in target else (f"twitch.tv/{target}")
        )
        self._quality = quality
        self._extra = extra_args or []
        self._procs: list[asyncio.subprocess.Process] = []
        self._stderr_ring: deque[str] = deque(maxlen=20)
        self._closing = False

    def describe(self) -> str:
        return f"{self._target} [{self._quality}]"

    async def frames(self) -> AsyncIterator[bytes]:
        backoff_s = 0.5
        while not self._closing:
            started = time.monotonic()
            try:
                ffmpeg = await self._spawn()
            except OSError as exc:
                raise SourceError(f"could not start the capture pipeline: {exc}")
            try:
                while True:
                    try:
                        chunk = await ffmpeg.stdout.readexactly(FRAME_BYTES)  # type: ignore[union-attr]
                    except asyncio.IncompleteReadError as exc:
                        if exc.partial:
                            yield exc.partial.ljust(FRAME_BYTES, b"\x00")
                        break
                    yield chunk
                    backoff_s = 0.5  # healthy stream: reset the backoff
            finally:
                await self._terminate()
            if self._closing:
                return
            uptime = time.monotonic() - started
            tail = "\n".join(f"    {line}" for line in self._stderr_ring)
            print(
                f"error: capture pipeline exited after {uptime:.1f}s\n"
                f"  last output:\n{tail or '    (none)'}\n"
                f"  retrying in {backoff_s:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 15.0)

    async def _spawn(self) -> asyncio.subprocess.Process:
        # Kernel-to-kernel pipe; both parent fds MUST close or ffmpeg never
        # sees EOF when streamlink dies and the source hangs forever.
        read_fd, write_fd = os.pipe()
        try:
            streamlink = await asyncio.create_subprocess_exec(
                *streamlink_argv(self._target, self._quality, self._extra),
                stdout=write_fd,
                stderr=asyncio.subprocess.PIPE,
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_argv(),
                stdin=read_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self._procs = [streamlink, ffmpeg]
        for process, tag in ((streamlink, "streamlink"), (ffmpeg, "ffmpeg")):
            asyncio.ensure_future(self._drain_stderr(process, tag))
        return ffmpeg

    async def _drain_stderr(
        self, process: asyncio.subprocess.Process, tag: str
    ) -> None:
        if process.stderr is None:
            return
        async for line in process.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr_ring.append(f"[{tag}] {text}")
                print(f"[{tag}] {text}", file=sys.stderr)

    async def _terminate(self) -> None:
        for process in self._procs:
            if process.returncode is None:
                process.terminate()
        for process in self._procs:
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
        self._procs = []

    async def aclose(self) -> None:
        self._closing = True
        await self._terminate()


# --------------------------------------------------------------------------- #
# Local audio device (zero broadcast delay)
# --------------------------------------------------------------------------- #


class DeviceSource(AudioSource):
    """Capture a local input/loopback device via sounddevice (optional extra).

    The best-quality path when run on the streamer's own machine: no
    broadcast delay, no HLS buffering, pre-encode audio. The PortAudio
    callback runs on a foreign thread, so frames cross into asyncio via
    ``call_soon_threadsafe`` with an explicit ``bytes()`` copy (PortAudio
    reuses its buffer). The queue is bounded and DROPS on overflow: a live
    stream cannot be paused, and buffering through a stall only produces
    stale fact-checks.
    """

    name = "device"

    def __init__(self, device: str | int | None = None) -> None:
        try:
            import sounddevice  # noqa: F401 - availability probe
        except OSError as exc:  # PortAudio missing
            raise SourceError(
                "sounddevice is installed but PortAudio is not:\n"
                f"  Linux    sudo apt install libportaudio2\n  ({exc})"
            )
        except ImportError:
            raise SourceError(
                "--source device needs the `sounddevice` package:\n"
                "  cd backend && uv sync --extra device\n"
                "  Linux also needs PortAudio: sudo apt install libportaudio2"
            )
        self._device = device
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)  # ~2 s
        self._dropped = 0
        self._stream = None

    def describe(self) -> str:
        return f"device {self._device if self._device is not None else '(default)'}"

    async def frames(self) -> AsyncIterator[bytes]:
        import sounddevice

        loop = asyncio.get_running_loop()

        def on_audio(indata, frame_count, time_info, status) -> None:
            payload = bytes(indata)  # copy: PortAudio reuses the buffer

            def offer() -> None:
                if self._queue.full():
                    self._queue.get_nowait()
                    self._dropped += 1
                self._queue.put_nowait(payload)

            loop.call_soon_threadsafe(offer)

        self._stream = sounddevice.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self._device,
            callback=on_audio,
        )
        with self._stream:
            while True:
                yield await self._queue.get()

    async def aclose(self) -> None:
        if self._stream is not None:
            self._stream.abort()


def list_devices() -> str:
    try:
        import sounddevice
    except Exception:
        return "sounddevice is not installed — cd backend && uv sync --extra device"
    lines = [str(sounddevice.query_devices())]
    lines.append(
        "\nFor capturing the STREAM's own audio, pick a loopback/monitor "
        "device:\n  Linux    a PulseAudio '*.monitor' source\n"
        "  Windows  WASAPI loopback or 'Stereo Mix'\n"
        "  macOS    BlackHole or Loopback"
    )
    return "\n".join(lines)
