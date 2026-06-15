"""Prompt-injection defense for untrusted PR content.

PR titles, bodies, diffs and ``/ask`` questions are attacker-controlled. They are embedded
in agent prompts, so before they go in we:

- strip control markers we use internally (so a PR can't forge a "review already done" marker
  or inject a pipeline stage anchor),
- strip zero-width / invisible characters used to smuggle instructions,
- fence the content and neutralize fence-breakouts so it is unambiguously *data, not instructions*.
"""

import re

# Our own internal markers / stage anchors. A PR author must not be able to inject these.
_CONTROL_PATTERNS = [
    re.compile(r"<!--\s*kimi-review.*?-->", re.IGNORECASE | re.DOTALL),
    re.compile(r"\[\[\s*STAGE\s*:.*?\]\]", re.IGNORECASE | re.DOTALL),
]

# Zero-width and other invisible characters used to hide injected instructions:
# ZWSP, ZWNJ, ZWJ, LRM, RLM, bidi embeddings/overrides, word joiner, BOM.
_INVISIBLE = re.compile(
    "[​‌‍‎‏‪-‮⁠﻿]"
)


def strip_control_markers(text: str) -> str:
    """Remove internal markers, stage anchors and invisible characters from untrusted text."""
    if not text:
        return ""
    for pat in _CONTROL_PATTERNS:
        text = pat.sub("", text)
    text = _INVISIBLE.sub("", text)
    return text


def fence(text: str, lang: str = "") -> str:
    """Wrap untrusted text in a code fence, neutralizing any fence-breakout attempt.

    Backtick runs inside the content are collapsed so they cannot close our fence early.
    """
    cleaned = strip_control_markers(text)
    # Collapse any run of >=3 backticks so it can't terminate the fence.
    cleaned = re.sub(r"`{3,}", "``", cleaned)
    return f"```{lang}\n{cleaned}\n```"


def sanitize_untrusted(text: str) -> str:
    """Strip control markers from a short untrusted string (e.g. a PR title) without fencing."""
    return strip_control_markers(text).strip()
