"""Conservative intent classifier for explicit voice-controlled screen watching.

Only direct imperative/request phrases start or stop sharing. This is deliberately
not an LLM-based decision: spoken context or screen content cannot grant consent.
"""
from __future__ import annotations

import re


_NEGATION = re.compile(
    r"\b(?:dont|do not|never|cannot|cant|wont|will not|shouldnt|should not|"
    r"wouldnt|would not|no need to)\b"
)
_STOP = re.compile(
    r"^(?:please\s+)?(?:stop|end|disable|turn off|pause)\s+"
    r"(?:(?:watching|sharing|looking at|viewing|monitoring)\s+)?"
    r"(?:my\s+)?screen(?:\s+sharing)?\b.*$"
)
_START = (
    re.compile(
        r"^(?:please\s+)?(?:(?:can|could|will)\s+you\s+)?(?:please\s+)?"
        r"(?:see|watch|look at|view|share)\s+(?:my\s+|the\s+)?screen\b.*$"
    ),
    re.compile(
        r"^(?:please\s+)?(?:(?:can|could)\s+you\s+)?"
        r"(?:start|begin|enable|turn on)\s+(?:live\s+)?"
        r"(?:screen\s+sharing|sharing\s+(?:my\s+)?screen)\b.*$"
    ),
    re.compile(
        r"^(?:please\s+)?(?:start|begin|keep|continue)\s+"
        r"(?:watching|sharing)\s+(?:my\s+)?screen\b.*$"
    ),
    re.compile(
        r"^(?:please\s+)?(?:i want you to|i need you to)\s+"
        r"(?:see|watch|look at|view)\s+(?:my\s+)?screen\b.*$"
    ),
)


def classify_screen_watch_command(transcript: str) -> str | None:
    """Return 'start', 'stop', or None for a direct user voice transcription."""
    if not isinstance(transcript, str) or not transcript.strip():
        return None

    text = transcript.casefold().replace("’", "'").replace("'", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"^(?:(?:hey|hi|okay|ok)\s+)?(?:chidvi(?:\s+556)?|jarvis)\b\s*",
        "",
        text,
    ).strip()

    # Negated utterances are never treated as authorization, even if a command
    # phrase also appears later in the sentence.
    if _NEGATION.search(text):
        return None
    if _STOP.match(text):
        return "stop"
    if any(pattern.match(text) for pattern in _START):
        return "start"
    return None
