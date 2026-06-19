"""Prompt-injection defense and secret redaction for untrusted PR content.

PR titles, bodies, diffs and ``/ask`` questions are attacker-controlled. They are embedded
in agent prompts, so before they go in we:

- strip control markers we use internally (so a PR can't forge a "review already done" marker
  or inject a pipeline stage anchor),
- strip zero-width / invisible characters used to smuggle instructions,
- fence the content and neutralize fence-breakouts so it is unambiguously *data, not instructions*.
"""

import re
import os
from typing import Any

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

_SECRET_ENV_NAMES = (
    "KIMI_API_KEY",
    "INPUT_KIMI_API_KEY",
    "GITHUB_TOKEN",
    "INPUT_GITHUB_TOKEN",
    "CLICKUP_TOKEN",
    "INPUT_CLICKUP_TOKEN",
    "LINEAR_API_KEY",
    "INPUT_LINEAR_API_KEY",
)

_GENERIC_SECRET_PATTERNS = [
    (
        re.compile(r"https://x-access-token:[^@\s]+@github\.com", re.IGNORECASE),
        "https://x-access-token:[REDACTED]@github.com",
    ),
    # Database credentials embedded in a connection URL. Mask only the password,
    # preserving scheme/user/host so the connection target stays diagnosable.
    # ``mongodb+srv`` is listed before ``mongodb`` so the longer scheme wins.
    (
        re.compile(
            r"\b(postgresql|postgres|mysql|mongodb\+srv|mongodb|redis|amqp)://"
            r"([^:/@\s]+):([^@\s]+)@",
            re.IGNORECASE,
        ),
        lambda m: f"{m.group(1)}://{m.group(2)}:[REDACTED]@",
    ),
    # Generic HTTP(S) basic-auth credentials in a URL: mask the whole userinfo.
    # Ordered AFTER the GitHub clone-url pattern above so the already-redacted
    # ``x-access-token:[REDACTED]@github.com`` is left untouched (the negative
    # lookahead skips any password we have already replaced).
    (
        re.compile(
            r"(https?)://([^:/@\s]+):(?!\[REDACTED\])([^@\s]+)@",
            re.IGNORECASE,
        ),
        lambda m: f"{m.group(1)}://[REDACTED]@",
    ),
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        "[REDACTED:GITHUB_TOKEN]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "[REDACTED:API_KEY]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED:AWS_ACCESS_KEY]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[REDACTED:JWT]",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        "[REDACTED:SLACK_TOKEN]",
    ),
    (
        re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
        "[REDACTED:GOOGLE_API_KEY]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL | re.IGNORECASE,
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b"
            r"(\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=-]{20,})([\"']?)"
        ),
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(4)}",
    ),
]


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


def _secret_env_values() -> list[tuple[str, str]]:
    """Return exact secret values from the current action environment."""
    values = []
    seen = set()
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name, "")
        if len(value) < 8 or value in seen:
            continue
        seen.add(value)
        values.append((name, value))
    return values


def redact_secrets(text: str) -> str:
    """Redact credentials from text before persistence or GitHub posting.

    Exact known environment secret values are scrubbed first, followed by high-confidence
    token patterns. The patterns intentionally require long opaque values so normal code
    snippets and placeholders are not mangled.
    """
    if text is None:
        return ""
    out = str(text)
    for name, value in _secret_env_values():
        out = out.replace(value, f"[REDACTED:{name}]")
    for pattern, repl in _GENERIC_SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside a JSON-like object."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(v) for v in value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value
