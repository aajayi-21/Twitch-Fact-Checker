"""Terminal logging: a readable, colored, session-aware format.

This backend is watched in a terminal while a stream is running, so the log
IS the primary UI for everything the extension does not show. Three problems
with plain ``basicConfig`` this module fixes:

1. **It silently does nothing when a handler already exists** (pytest, or an
   embedding process), so ``LOG_LEVEL`` gets ignored in exactly the
   environments where people are debugging. :func:`configure_logging` uses
   ``force=True``.
2. **uvicorn owns its own loggers and formatter**, so the terminal shows two
   competing formats. They are re-pointed at this one.
3. **Nothing tied a line to a session.** A ``session_id`` contextvar is
   stamped onto every record by a filter, so gate/verify/db lines emitted
   deep inside shared, process-wide objects still say which session caused
   them — without threading an id through every call.

Style rules the rest of the codebase already follows and this preserves:
lowercase messages, no trailing period, lazy ``%s`` args, ``%r`` for user
text. Nothing here ever formats a ``Settings`` object (it holds API keys).
"""

import contextvars
import logging
import os
import sys
from typing import Any

#: Set per session by the pipeline; read by the log filter. Contextvars do
#: NOT propagate into ThreadPoolExecutor jobs, so STT/db worker threads log
#: without an id — which is honest, they are process-wide workers.
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default=""
)

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

_LEVEL_COLORS = {
    logging.DEBUG: "\033[38;5;244m",  # grey
    logging.INFO: "\033[38;5;39m",  # blue
    logging.WARNING: "\033[38;5;214m",  # amber
    logging.ERROR: "\033[38;5;203m",  # red
    logging.CRITICAL: "\033[1;38;5;199m",  # magenta, bold
}

_LEVEL_LABELS = {
    logging.DEBUG: "DBG",
    logging.INFO: "INF",
    logging.WARNING: "WRN",
    logging.ERROR: "ERR",
    logging.CRITICAL: "CRT",
}

#: Logger-name prefix stripped in the terminal — every module is "app.*".
_NAME_PREFIX = "app."


class SessionIdFilter(logging.Filter):
    """Stamp the current session id onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = session_id_var.get("")
        return True


class TerminalFormatter(logging.Formatter):
    """``HH:MM:SS LVL module [session] message`` with optional color.

    Color is decided once, at construction, from whether the stream is a TTY
    (plus the conventional ``NO_COLOR`` opt-out), so piping the server into a
    file or a log collector yields clean text.
    """

    def __init__(self, use_color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        level_label = _LEVEL_LABELS.get(record.levelno, record.levelname[:3])
        name = record.name
        if name.startswith(_NAME_PREFIX):
            name = name[len(_NAME_PREFIX) :]
        elif name.startswith("uvicorn"):
            name = "uvicorn"
        session = getattr(record, "session_id", "") or ""
        # Short, stable correlation tag — the full uuid is in the database.
        session_tag = f" {session[:8]}" if session else ""
        message = record.getMessage()
        timestamp = self.formatTime(record, self.datefmt)

        if self._use_color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            head = (
                f"{DIM}{timestamp}{RESET} "
                f"{color}{level_label}{RESET} "
                f"{DIM}{name}{session_tag}{RESET}"
            )
            if record.levelno >= logging.ERROR:
                message = f"{color}{message}{RESET}"
        else:
            head = f"{timestamp} {level_label} {name}{session_tag}"

        line = f"{head}  {message}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


def supports_color(stream: Any = None) -> bool:
    """Whether to emit ANSI codes: a TTY, and NO_COLOR not set."""
    stream = stream or sys.stderr
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def configure_logging(level: str) -> None:
    """Install the terminal handler on the root and uvicorn's loggers.

    ``force=True`` matters: ``basicConfig`` is a no-op when the root logger
    already has a handler, which is precisely the case under pytest and when
    uvicorn has already configured itself.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(TerminalFormatter(supports_color(sys.stderr)))
    handler.addFilter(SessionIdFilter())
    logging.basicConfig(
        level=level.upper(), handlers=[handler], force=True
    )
    # uvicorn installs its own handlers/formatter before lifespan runs;
    # clearing them makes it inherit ours instead of double-printing in a
    # second format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # httpx logs one INFO line per request; at DEBUG the Hugging Face and
    # OpenAI clients are louder still. Keep third parties at WARNING unless
    # the user explicitly asked for DEBUG.
    if level.upper() != "DEBUG":
        for noisy in ("httpx", "httpcore", "urllib3", "filelock", "transformers"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(lines: list[tuple[str, str]], title: str) -> str:
    """A boxed key/value block for the one-time startup summary.

    Startup facts are worth a scannable block: which provider serves which
    stage, which speech engine and device, where the database is. Everything
    after this is one line per event.
    """
    use_color = supports_color(sys.stderr)
    width = max((len(key) for key, _ in lines), default=0)
    rendered = [f"{BOLD}{title}{RESET}" if use_color else title]
    for key, value in lines:
        label = f"{key:<{width}}"
        if use_color:
            rendered.append(f"  {DIM}{label}{RESET}  {value}")
        else:
            rendered.append(f"  {label}  {value}")
    return "\n".join(rendered)
