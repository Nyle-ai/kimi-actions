"""Diff filtering for code review.

Drops files that add noise to a review (lockfiles, minified/generated assets) before
the diff ever reaches the model. Applies, in order:

1. Always-keep rules (DB migrations) — these win over every strip rule.
2. Always-strip rules (lockfiles, minified, source maps, generated bundles).
3. User ``exclude_patterns`` (action input) and repo ``ignore_files`` (.kimi-config.yml).
4. A ``max_files`` cap.

Previously these config fields were parsed but never applied, so unfiltered diffs hit
the model. This module wires them in.
"""

import fnmatch
import logging
import os
from typing import Iterable, List, Sequence

logger = logging.getLogger(__name__)

# Patterns that are ALWAYS stripped regardless of config — pure noise for a reviewer.
ALWAYS_STRIP: List[str] = [
    "*.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "*.snap",
    "*.svg",
    "*.ico",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
]

# Patterns that are ALWAYS kept even if they would otherwise match a strip/exclude rule.
# DB migrations carry real risk (data loss, locks) and must always be reviewed.
ALWAYS_KEEP: List[str] = [
    "*/migrations/*",
    "migrations/*",
    "*/migrate/*",
    "*.sql",
]


def _matches_any(filename: str, patterns: Iterable[str]) -> bool:
    """True if ``filename`` matches any glob pattern (tested on full path and basename)."""
    base = os.path.basename(filename)
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def should_include(
    filename: str,
    exclude_patterns: Sequence[str] = (),
    ignore_files: Sequence[str] = (),
) -> bool:
    """Decide whether a changed file should be part of the reviewed diff."""
    if _matches_any(filename, ALWAYS_KEEP):
        return True
    if _matches_any(filename, ALWAYS_STRIP):
        return False
    if _matches_any(filename, exclude_patterns):
        return False
    if _matches_any(filename, ignore_files):
        return False
    return True


def filter_files(
    files: Iterable,
    exclude_patterns: Sequence[str] = (),
    ignore_files: Sequence[str] = (),
    max_files: int = 50,
) -> List:
    """Filter PR file objects (must expose ``.filename``) and cap the count.

    Returns the kept file objects, in their original order, truncated to ``max_files``.
    """
    kept = [
        f
        for f in files
        if should_include(f.filename, exclude_patterns, ignore_files)
    ]
    if len(kept) > max_files:
        logger.info(
            "Diff filter: capping %d files to max_files=%d", len(kept), max_files
        )
        kept = kept[:max_files]
    return kept
