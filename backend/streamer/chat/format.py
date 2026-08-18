"""Building the literal strings the bot says in public.

**The safety-critical module.** Read :func:`sanitize_for_chat` before changing
anything here.

The bot is required to be a moderator in the channel (that is how consent is
proven — see :mod:`streamer.chat.consent`), and Twitch executes a moderator's
``/ban``, ``/timeout``, ``/clear`` as COMMANDS rather than text. Verdict text
is derived from an LLM explanation, which is derived from web search results,
which are attacker-influenceable. A single leading ``/`` would therefore mass-
ban a chat. That is why every outbound message is required to begin with a
known emoji, as an assertion rather than a convention: if we ever construct a
message that does not, that is a bug and the message must not be sent.

Being a moderator also means the bot's messages **bypass AutoMod**, so nothing
the streamer's own filters would have caught gets caught. Hence the outbound
content filter here rather than a hope that the model behaves.

The other rule worth stating up front:

    **Truth-bearing content is never truncated. It either fits, or the post is
    dropped.**

Claims are never cut (an over-long claim fails the predicate before reaching
the formatter). Explanations are trimmed only at sentence boundaries. URLs are
atomic — one that does not fit degrades to its bare domain, never to a prefix.
This removes the entire class of "truncation inverted the meaning" bugs
(``"X is not the largest economy"`` -> ``"X is not the largest"``) without
needing negation-scope analysis. Under-posting is the correct failure
direction: the hourly cap means most verdicts are discarded anyway.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Literal

from app.models import Label, Verdict

from streamer.chat.source_quality import registrable_domain

logger = logging.getLogger(__name__)

# Twitch's hard limit is 500; we build to 480 so a downstream tweak (a longer
# handle, a wider emoji) cannot silently cross it.
MAX_CHAT_CHARS = 500
SAFE_CHAT_CHARS = 480

MAX_CLAIM_CHARS = 200
MIN_CLAIM_CHARS = 25
MIN_EXPLANATION_CHARS = 40

LABEL_EMOJI: dict[Label, str] = {
    "FALSE": "❌",
    "MISLEADING": "⚠️",
    "TRUE": "✅",
    "UNVERIFIED": "❓",
}

# Every outbound message must start with one of these. `/`, `.` and `!` are
# the characters Twitch treats as command prefixes.
ALLOWED_LEADING = ("❌", "⚠️", "✅", "❓", "↩️", "🤖", "🕰️")

TemplateName = Literal["standard", "compact", "verbose"]

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w{3,25}")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d[\s().-]?){9,15}\d")
_FIRST_SECOND_PERSON_RE = re.compile(
    r"\b(i|i'm|i've|i'd|i'll|me|my|mine|you|you're|you've|your|yours|we|we're|"
    r"our|ours|us)\b",
    re.IGNORECASE,
)
# Attribution verbs near the start of a claim: the gate is supposed to rewrite
# into self-contained third person, so a surviving "X said" means the claim is
# about reported speech and the bot would be adjudicating someone not present.
_REPORTED_SPEECH_RE = re.compile(
    r"\b(said|says|claimed|claims|tweeted|posted|told|wrote|according to)\b",
    re.IGNORECASE,
)

# Deliberately small and blunt. This is a backstop against an LLM echoing
# something from a search result, not a content-moderation system.
_UNSAFE_TERMS: frozenset[str] = frozenset(
    {
        "kys",
        "kill yourself",
    }
)


def visible_length(text: str) -> int:
    """Length in UTF-16 code units — the strictest reading of "500 characters".

    ``🤖`` (U+1F916) is one Python character but two UTF-16 units, and Twitch
    counts the latter. Measuring with ``len()`` would let an emoji-heavy
    message sail past a check and be truncated server-side mid-word.
    """
    return len(text.encode("utf-16-le")) // 2


def sanitize_for_chat(text: str) -> str | None:
    """Make one line safe to send, or return ``None`` to drop it.

    Applied to EVERY outbound message without exception, command replies
    included. The steps, in order and each for a reason:

    1. NFC-normalize, so lookalike decompositions collapse before matching.
    2. Delete every Unicode ``Cc``/``Cf`` character. This covers ``\\r`` and
       ``\\n`` (which would split one PRIVMSG into a second IRC command),
       zero-width joiners, and the bidi overrides U+202A–U+202E / U+2066–U+2069
       (which can render a message visually reversed or spoofed).
    3. Collapse whitespace runs.
    4. Require a known leading emoji — the command-prefix guard.
    5. Reject PII and @mentions: a Twitch @mention pings a real person.
    6. Enforce both length budgets.
    """
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        char for char in text if unicodedata.category(char) not in {"Cc", "Cf"}
    )
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if not text.startswith(ALLOWED_LEADING):
        # A bug, not bad input: every template starts with an emoji. Refuse
        # rather than lstrip, because "we built a message we did not intend"
        # is exactly the state in which sending is most dangerous.
        logger.error("refusing to send a message with no leading marker: %r", text)
        return None
    lowered = text.lower()
    if any(term in lowered for term in _UNSAFE_TERMS):
        logger.warning("refusing to send a message matching the unsafe-term list")
        return None
    if _MENTION_RE.search(text) or _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        logger.warning("refusing to send a message containing a mention or PII")
        return None
    if visible_length(text) > SAFE_CHAT_CHARS:
        return None
    if len(text.encode("utf-8")) > MAX_CHAT_CHARS:
        return None
    return text


def claim_is_postable(claim_text: str) -> tuple[bool, str]:
    """Shape checks on the claim. Returns ``(ok, reason)``.

    These are not style preferences. The gate rewrites every claim into "ONE
    self-contained declarative sentence" (``prompts.py``), so the text posted
    is a PARAPHRASE, not the streamer's words — quoting a paraphrase back at
    2,000 viewers as if it were a quotation is the defamation-shaped failure.
    A surviving first- or second-person pronoun means that rewrite did not
    happen, and also means the claim is personal ("I've never been to Japan"),
    which is unverifiable by search and the worst possible thing to adjudicate
    in public.
    """
    text = claim_text.strip()
    if len(text) < MIN_CLAIM_CHARS:
        return False, "claim_too_short"
    if len(text) > MAX_CLAIM_CHARS:
        return False, "claim_too_long"
    if "\n" in claim_text or "\r" in claim_text or '"' in text:
        return False, "malformed_claim"
    if not text.endswith((".", "?", "!")):
        return False, "malformed_claim"
    if _FIRST_SECOND_PERSON_RE.search(text):
        return False, "first_person_claim"
    head = " ".join(text.split()[:6])
    if _REPORTED_SPEECH_RE.search(head):
        return False, "reported_speech"
    return True, "ok"


def explanation_is_postable(explanation: str) -> tuple[bool, str]:
    """An explanation must be a receipt, not an assertion."""
    text = explanation.strip()
    if len(text) < MIN_EXPLANATION_CHARS:
        return False, "explanation_too_short"
    if _URL_RE.search(text):
        # Sources are rendered separately and deliberately; a URL inside prose
        # bypasses the domain-vs-link policy.
        return False, "explanation_has_url"
    return True, "ok"


def truncate_to_sentences(text: str, budget: int) -> str | None:
    """Whole leading sentences that fit in ``budget``, or ``None``.

    Never cuts mid-sentence, so a trailing clause can never be dropped in a
    way that flips the meaning of what remains.
    """
    text = text.strip()
    if visible_length(text) <= budget:
        return text
    kept: list[str] = []
    for sentence in _SENTENCE_END_RE.split(text):
        candidate = " ".join([*kept, sentence]).strip()
        if visible_length(candidate) > budget:
            break
        kept.append(sentence)
    if not kept:
        return None
    return " ".join(kept).strip()


def source_label(
    verdict: Verdict, *, style: Literal["domain", "url"], budget: int, limit: int
) -> str:
    """Render citations as domain chips (default) or whole URLs.

    ``domain`` is the default because a non-moderator bot's links are
    frequently filtered by channel link permissions, and because a bare domain
    is what a viewer actually reads.
    """
    domains: list[str] = []
    for source in verdict.sources:
        domain = registrable_domain(source.url)
        if domain and domain not in domains:
            domains.append(domain)
    if not domains:
        return ""
    if style == "url" and verdict.sources:
        url = verdict.sources[0].url
        # Atomic: a URL that does not fit becomes its domain, never a prefix.
        if visible_length(url) <= budget:
            return f"[{url}]"
    shown = domains[:limit]
    extra = len(domains) - len(shown)
    rendered = ", ".join(shown) + (f" +{extra}" if extra > 0 else "")
    while shown and visible_length(f"[{rendered}]") > budget:
        shown.pop()
        extra = len(domains) - len(shown)
        rendered = ", ".join(shown) + (f" +{extra}" if extra > 0 else "")
    return f"[{rendered}]" if shown else ""


def verdict_handle(verdict: Verdict, *, width: int = 4) -> str:
    """Short public id for ``!fc why`` / ``!fc dispute``.

    A prefix of ``Verdict.id`` (uuid4 hex), which is already the stable handle
    across the wire, the ``verdicts`` table, and ``POST /feedback``.
    """
    return verdict.id[:width]


def format_verdict_post(
    verdict: Verdict,
    *,
    template: TemplateName = "standard",
    sources_style: Literal["domain", "url"] = "domain",
    handle_width: int = 4,
) -> str | None:
    """The literal chat message, or ``None`` if it cannot be built safely.

    Shape: ``{emoji} {LABEL} · "{claim}" — {explanation} {sources} · 🤖 !fc why {id}``
    """
    emoji = LABEL_EMOJI.get(verdict.label, "❓")
    handle = verdict_handle(verdict, width=handle_width)
    claim = verdict.claim.strip()
    head = f'{emoji} {verdict.label} · "{claim}"'
    tail = f" · 🤖 !fc why {handle}"

    source_limit = 3 if template == "verbose" else 2
    sources = source_label(
        verdict,
        style=sources_style,
        budget=max(0, SAFE_CHAT_CHARS - visible_length(head + tail) - 4),
        limit=source_limit,
    )

    if template == "compact":
        body = ""
    else:
        remaining = SAFE_CHAT_CHARS - visible_length(
            f"{head} — {tail} {sources}".rstrip()
        )
        if remaining < MIN_EXPLANATION_CHARS:
            return None
        explanation = truncate_to_sentences(verdict.explanation, remaining)
        if explanation is None:
            return None
        body = f" — {explanation}"

    parts = [head + body]
    if sources:
        parts.append(sources)
    message = " ".join(parts) + tail
    return sanitize_for_chat(message)


def format_retraction(handle: str) -> str | None:
    """Withdraw a post. Fixed wording — never echoes an operator's reason."""
    return sanitize_for_chat(
        f"↩️ RETRACTED · Check {handle} posted earlier was wrong and has been "
        "withdrawn. Sorry for the noise. · 🤖"
    )


def format_correction(handle: str, corrected: Label) -> str | None:
    return sanitize_for_chat(
        f"↩️ CORRECTION · Check {handle} should have read {corrected}. The "
        f"earlier message is withdrawn. · 🤖 !fc why {handle}"
    )


def format_disclosure() -> str | None:
    """Posted on join and periodically: what this is, and that it can be wrong."""
    return sanitize_for_chat(
        "🤖 Fact-check bot, running at the broadcaster's request. It checks "
        "claims, not people, and only posts when reputable sources disagree. "
        "It can be wrong — !fc about"
    )


def format_about() -> str | None:
    return sanitize_for_chat(
        "🤖 Automated fact-checker: I transcribe the stream, pick out checkable "
        "claims, search the web, and post only FALSE/MISLEADING results backed "
        "by 2+ reputable sources. I get things wrong — !fc dispute <id> flags one."
    )
