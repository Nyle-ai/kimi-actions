"""Diff filtering and deterministic PR coverage modeling for code review.

Drops files that add noise to a review (lockfiles, minified/generated assets) before
the diff ever reaches the model. Applies, in order:

1. Always-keep rules (DB migrations) — these win over every strip rule.
2. Always-strip rules (lockfiles, minified, source maps, generated bundles).
3. User ``exclude_patterns`` (action input) and repo ``ignore_files`` (.kimi-config.yml).
4. A ``max_files`` cap.

Previously these config fields were parsed but never applied, so unfiltered diffs hit
the model. This module wires them in.
"""

import logging
import os
import fnmatch
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Sequence, Tuple

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

HIGH_RISK_PATH_PATTERNS: List[Tuple[str, str]] = [
    ("security", "auth"),
    ("security", "login"),
    ("security", "token"),
    ("security", "secret"),
    ("security", "permission"),
    ("security", "rls"),
    ("security", "policy"),
    ("database", "supabase"),
    ("database", "database"),
    ("database", "db"),
    ("database", "prisma"),
    ("database", "migration"),
    ("database", "cdc"),
    ("api", "api"),
    ("api", "route"),
    ("api", "controller"),
    ("queue", "queue"),
    ("queue", "worker"),
    ("queue", "job"),
    ("queue", "enqueue"),
    ("queue", "batch"),
    ("concurrency", "lock"),
    ("concurrency", "merge"),
    ("concurrency", "async"),
    ("financial", "rate"),
    ("financial", "budget"),
    ("financial", "price"),
    ("financial", "payment"),
    ("financial", "total"),
]

PATCH_RISK_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("security", re.compile(r"(?i)(secret|api[_-]?key|token|password|service[_-]?role)")),
    ("database", re.compile(r"(?i)(create\s+policy|alter\s+table|rls|jsonb|trigger)")),
    ("queue", re.compile(r"(?i)(enqueue|worker|job|queue|concurr|race|lock)")),
    ("financial", re.compile(r"(?i)(amount|rate|total|price|payment|invoice)")),
    ("validation", re.compile(r"(?i)(zod|schema|validate|parse|request\.json)")),
]

LOW_RISK_PATH_PARTS = {
    "docs",
    "documentation",
    "readme",
    "test",
    "tests",
    "__tests__",
    "fixtures",
    "stories",
}


@dataclass
class FileDecision:
    """Coverage decision for one changed file."""

    filename: str
    original_index: int
    included: bool
    reason: str
    risk_tags: List[str]
    priority: int
    status: str = ""
    additions: int = 0
    deletions: int = 0
    changes: int = 0


def _matches_any(filename: str, patterns: Iterable[str]) -> bool:
    """True if ``filename`` matches any glob pattern (tested on full path and basename)."""
    base = os.path.basename(filename)
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def _path_parts(filename: str) -> List[str]:
    return [p for p in re.split(r"[^A-Za-z0-9]+", filename.lower()) if p]


def risk_tags_for_file(file_obj: Any) -> List[str]:
    """Assign deterministic risk tags from path and patch content."""
    filename = getattr(file_obj, "filename", str(file_obj))
    lowered = filename.lower()
    tags = set()

    if _matches_any(filename, ALWAYS_KEEP):
        tags.add("migration")
        tags.add("database")

    for tag, needle in HIGH_RISK_PATH_PATTERNS:
        if needle in lowered:
            tags.add(tag)

    patch = getattr(file_obj, "patch", "") or ""
    for tag, pattern in PATCH_RISK_PATTERNS:
        if pattern.search(patch):
            tags.add(tag)

    return sorted(tags)


def _decision_reason(
    filename: str,
    exclude_patterns: Sequence[str],
    ignore_files: Sequence[str],
) -> str:
    if _matches_any(filename, ALWAYS_KEEP):
        return "always_keep"
    if _matches_any(filename, ALWAYS_STRIP):
        return "always_strip"
    if _matches_any(filename, exclude_patterns):
        return "exclude_pattern"
    if _matches_any(filename, ignore_files):
        return "repo_ignore"
    return "included"


def _priority(file_obj: Any, reason: str, risk_tags: Sequence[str]) -> int:
    filename = getattr(file_obj, "filename", str(file_obj))
    parts = set(_path_parts(filename))

    if reason == "always_keep":
        return 100
    if "security" in risk_tags:
        return 90
    if {"database", "api", "queue", "concurrency", "financial", "validation"} & set(
        risk_tags
    ):
        return 80
    if parts & LOW_RISK_PATH_PARTS or filename.lower().endswith((".md", ".mdx")):
        return 20
    return 50


def _file_decision(file_obj: Any, index: int, reason: str, included: bool) -> FileDecision:
    tags = risk_tags_for_file(file_obj)

    def int_attr(name: str) -> int:
        value = getattr(file_obj, name, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    status = getattr(file_obj, "status", "") or ""
    if not isinstance(status, str):
        status = ""

    return FileDecision(
        filename=getattr(file_obj, "filename", str(file_obj)),
        original_index=index,
        included=included,
        reason=reason,
        risk_tags=tags,
        priority=_priority(file_obj, reason, tags),
        status=status,
        additions=int_attr("additions"),
        deletions=int_attr("deletions"),
        changes=int_attr("changes"),
    )


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


def build_review_model(
    files: Iterable,
    exclude_patterns: Sequence[str] = (),
    ignore_files: Sequence[str] = (),
    max_files: int = 50,
) -> Tuple[List, dict]:
    """Build a deterministic coverage model and return selected files.

    ``ALWAYS_KEEP`` files are retained even if that means exceeding ``max_files``.
    The remaining budget is assigned by risk priority, with original PR order used as a
    stable tie-breaker. The returned model records every changed file and why it was or
    was not reviewed.
    """
    all_files = list(files)
    initial: List[Tuple[Any, FileDecision]] = []
    excluded: List[FileDecision] = []

    for index, file_obj in enumerate(all_files):
        filename = getattr(file_obj, "filename", str(file_obj))
        reason = _decision_reason(filename, exclude_patterns, ignore_files)
        include_candidate = reason in {"always_keep", "included"}
        decision = _file_decision(file_obj, index, reason, include_candidate)
        if include_candidate:
            initial.append((file_obj, decision))
        else:
            excluded.append(decision)

    must_keep = [(f, d) for f, d in initial if d.reason == "always_keep"]
    optional = [(f, d) for f, d in initial if d.reason != "always_keep"]

    optional_budget = max(max_files - len(must_keep), 0) if max_files >= 0 else 0
    optional_ranked = sorted(
        optional, key=lambda item: (-item[1].priority, item[1].original_index)
    )
    selected_pairs = must_keep + optional_ranked[:optional_budget]

    capped = []
    for _, decision in optional_ranked[optional_budget:]:
        decision.included = False
        decision.reason = "max_files_cap"
        capped.append(decision)

    selected_pairs = sorted(selected_pairs, key=lambda item: item[1].original_index)
    selected_files = [f for f, _ in selected_pairs]
    selected_decisions = [d for _, d in selected_pairs]

    decisions = sorted(
        selected_decisions + excluded + capped,
        key=lambda decision: decision.original_index,
    )

    if capped:
        logger.info(
            "Diff filter: capped %d candidate file(s) after prioritized max_files=%d",
            len(capped),
            max_files,
        )

    model = {
        "total_changed_files": len(all_files),
        "reviewed_count": len(selected_files),
        "max_files": max_files,
        "reviewed_files": [d.filename for d in selected_decisions],
        "unreviewed_files": [
            asdict(d) for d in decisions if not d.included
        ],
        "files": [asdict(d) for d in decisions],
    }
    return selected_files, model


def filter_files(
    files: Iterable,
    exclude_patterns: Sequence[str] = (),
    ignore_files: Sequence[str] = (),
    max_files: int = 50,
) -> List:
    """Filter PR file objects and cap the count using prioritized coverage."""
    kept, _ = build_review_model(files, exclude_patterns, ignore_files, max_files)
    return kept
