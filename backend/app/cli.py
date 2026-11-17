"""Console commands: ``doxa`` (the viewer backend) and ``doxa-streamer``.

Installed with uvx or pip, these are how the servers start::

    uvx --from 'git+https://github.com/<owner>/<repo>#subdirectory=backend' doxa

In a checkout, ``run.sh`` and ``run-streamer.sh`` call the same commands
through ``uv run``.
"""

import argparse
import ipaddress
import os
import sys

import uvicorn

from app.config import data_dir, resolve_env_file, speech_engine_installed

#: The backend trusts only localhost (TrustedHostMiddleware, app/origins.py):
#: listening anywhere else would expose it without making it usable.
_LOOPBACK_NAMES = frozenset({"localhost"})


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(
    app_path: str,
    *,
    prog: str,
    label: str,
    default_port: int,
    argv: list[str] | None = None,
) -> int:
    """Parse ``--host``/``--port``, say where the data lives, run uvicorn.

    Returns:
        The process exit code: 2 for a refused address, else uvicorn's.
    """
    parser = argparse.ArgumentParser(prog=prog, description=f"Start the {label}.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="loopback address to listen on"
    )
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)
    if not _is_loopback(args.host):
        print(
            f"{prog}: refusing to listen on {args.host}: this backend is for this "
            "computer only (use 127.0.0.1 or localhost).",
            file=sys.stderr,
        )
        return 2
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # The app reads its address from settings (startup banner, dashboard
    # link); make them say where it is really listening.
    os.environ["HOST"] = args.host
    os.environ["PORT"] = str(args.port)
    # Flushed: uvicorn's log goes to stderr, and buffered stdout would print
    # these after it, or not at all if the output is a file.
    print(f"Starting the {label} on http://{args.host}:{args.port}", flush=True)
    print(f"Settings and history: {folder}", flush=True)
    print(f"Settings file:        {resolve_env_file()}", flush=True)
    if not speech_engine_installed():
        print(
            "No speech engine installed: the extension must transcribe in the "
            "browser (Settings → Speech recognition → This browser). Install the "
            "stt extra to transcribe here instead.",
            flush=True,
        )
    # --no-access-log equivalent: the extension polls /healthz every few
    # seconds, which would otherwise bury the session log.
    uvicorn.run(app_path, host=args.host, port=args.port, access_log=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    """``doxa``: the viewer backend the extension talks to."""
    return serve(
        "app.main:app",
        prog="doxa",
        label="Doxa backend",
        default_port=8710,
        argv=argv,
    )


def streamer_main(argv: list[str] | None = None) -> int:
    """``doxa-streamer``: the streamer backend (chat bot, OBS overlay)."""
    return serve(
        "streamer.main:app",
        prog="doxa-streamer",
        label="Doxa streamer backend",
        default_port=8711,
        argv=argv,
    )
