"""Per-stage spend / trajectory metrics for a review run.

The three-agent pipeline (Planner -> Executor -> QA) records a per-stage usage row
in ``BaseTool.stage_metrics`` (tokens, wall-time, attempts). This module turns those
rows into:

- a markdown table written to ``$GITHUB_STEP_SUMMARY`` (renders on the run page),
- a ``run-metadata.json`` trajectory record persisted to the metrics dir,
- a one-line operator log.

Token spend is the first-class metric: on the flat-rate Kimi *subscription* endpoint
there is no per-call dollar charge, so tokens (summed per step = actually-billed input)
are the real proxy for quota consumption. A "shadow $" (what the run *would* cost on the
metered API) is only shown when a price table is configured — we never invent prices.

Pure functions here are stdlib-only and unit-testable without the Agent SDK.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from sanitize import redact_obj, redact_secrets

logger = logging.getLogger(__name__)

# Token fields carried by kosong's TokenUsage (cache-aware).
TOKEN_FIELDS = ("input_other", "input_cache_read", "input_cache_creation", "output")

# Source: https://platform.kimi.ai/docs/pricing/chat-k27-code
# "input" = cache-miss rate, "cache_read" = cache-hit rate, "output" = output rate ($/MTok)
BUILTIN_PRICE_TABLE: Dict[str, Dict[str, float]] = {
    "kimi-k2.7-code": {"input": 0.95, "output": 4.00, "cache_read": 0.19},
    "kimi-k2.7-code-highspeed": {"input": 1.90, "output": 8.00, "cache_read": 0.38},
}


def _input(row: Dict[str, Any]) -> int:
    return (
        int(row.get("input_other", 0))
        + int(row.get("input_cache_read", 0))
        + int(row.get("input_cache_creation", 0))
    )


def _total(row: Dict[str, Any]) -> int:
    return _input(row) + int(row.get("output", 0))


def summarize(stage_metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-stage rows into totals."""
    totals = {f: 0 for f in TOKEN_FIELDS}
    totals["seconds"] = 0.0
    totals["calls"] = 0
    for row in stage_metrics:
        for f in TOKEN_FIELDS:
            totals[f] += int(row.get(f, 0))
        totals["seconds"] += float(row.get("seconds", 0.0))
        totals["calls"] += int(row.get("calls", 0))
    totals["input"] = (
        totals["input_other"] + totals["input_cache_read"] + totals["input_cache_creation"]
    )
    totals["total_tokens"] = totals["input"] + totals["output"]
    return totals


def load_price_table() -> Dict[str, Dict[str, float]]:
    """Per-model $/MTok price table for the shadow cost.

    Starts from ``BUILTIN_PRICE_TABLE`` (kimi-k2.7-code rates baked in).
    ``KIMI_PRICE_TABLE_JSON`` overlays / overrides entries — useful for other
    models or updated rates without a code change.
    """
    table = dict(BUILTIN_PRICE_TABLE)
    raw = os.environ.get("KIMI_PRICE_TABLE_JSON", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                table.update(overrides)
            else:
                logger.warning("KIMI_PRICE_TABLE_JSON is not a JSON object; ignoring")
        except (ValueError, TypeError):
            logger.warning("KIMI_PRICE_TABLE_JSON is not valid JSON; ignoring")
    return table


def shadow_cost_usd(
    totals: Dict[str, Any], model: str, price_table: Dict[str, Dict[str, float]]
) -> Optional[float]:
    """Reference $ cost at metered-API rates, or None when no price is configured."""
    price = price_table.get(model)
    if not price:
        return None
    cache_read_rate = float(price.get("cache_read", price.get("input", 0.0)))
    uncached_in = totals["input_other"] + totals["input_cache_creation"]
    return round(
        uncached_in / 1e6 * float(price.get("input", 0.0))
        + totals["input_cache_read"] / 1e6 * cache_read_rate
        + totals["output"] / 1e6 * float(price.get("output", 0.0)),
        4,
    )


def quota_pct(total_tokens: int) -> Optional[float]:
    """Estimated % of the rolling quota window this run consumed.

    Needs ``KIMI_QUOTA_TOKENS_PER_WINDOW`` (tokens that exhaust the window, e.g. the 5h
    subscription cap). Without it we can't translate tokens to quota %, so return None.
    """
    raw = os.environ.get("KIMI_QUOTA_TOKENS_PER_WINDOW", "").strip()
    if not raw:
        return None
    try:
        window = float(raw)
    except ValueError:
        return None
    if window <= 0:
        return None
    return round(total_tokens / window * 100, 2)


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def build_markdown(
    stage_metrics: List[Dict[str, Any]],
    totals: Dict[str, Any],
    model: str,
    shadow_usd: Optional[float],
    quota: Optional[float],
) -> str:
    """Render the per-stage spend table for the GitHub Step Summary."""
    has_cost = shadow_usd is not None
    header = ["Stage", "Tokens (in / out)", "Cache hit", "Calls", "Time"]
    if has_cost:
        header.append("Shadow $")
    rows = []
    for r in stage_metrics:
        cache = r.get("input_cache_read", 0)
        in_tok, out_tok = _input(r), int(r.get("output", 0))
        line = [
            f"`{r.get('stage', '?')}`"
            + ("" if int(r.get("attempts", 1)) <= 1 else f" (×{r.get('attempts')})"),
            f"{_fmt_int(in_tok)} / {_fmt_int(out_tok)}",
            _fmt_int(int(cache)),
            str(int(r.get("calls", 0))),
            f"{float(r.get('seconds', 0.0)):.0f}s",
        ]
        if has_cost:
            line.append("")
        rows.append("| " + " | ".join(line) + " |")

    total_line = [
        "**Total**",
        f"**{_fmt_int(totals['input'])} / {_fmt_int(totals['output'])}**",
        _fmt_int(totals["input_cache_read"]),
        str(totals["calls"]),
        f"**{totals['seconds']:.0f}s**",
    ]
    if has_cost:
        total_line.append(f"**${shadow_usd:.4f}**")
    rows.append("| " + " | ".join(total_line) + " |")

    out = [
        "## 🌗 AI Code Review — spend by stage",
        "",
        f"Model: `{model}` · total **{_fmt_int(totals['total_tokens'])} tokens**"
        + (f" · ~**{quota:.1f}%** of quota window" if quota is not None else "")
        + (f" · shadow **${shadow_usd:.4f}**" if shadow_usd is not None else ""),
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
        *rows,
    ]
    if quota is None:
        out += [
            "",
            "<sub>Subscription endpoint: no per-call charge — tokens are the quota proxy. "
            "Set `KIMI_QUOTA_TOKENS_PER_WINDOW` to show quota %. "
            "Override prices via `KIMI_PRICE_TABLE_JSON`.</sub>",
        ]
    return "\n".join(out)


def build_metadata(
    *,
    repo: str,
    pr_number: int,
    sha: str,
    model: str,
    stage_metrics: List[Dict[str, Any]],
    totals: Dict[str, Any],
    verdict: str,
    num_issues: int,
    shadow_usd: Optional[float],
    quota: Optional[float],
    review_model: Optional[Dict[str, Any]] = None,
    project_rules: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The persisted trajectory record (run-metadata.json)."""
    metadata = {
        "repo": repo,
        "pr": pr_number,
        "sha": sha,
        "model": model,
        "verdict": verdict,
        "num_issues": num_issues,
        "stages": stage_metrics,
        "totals": totals,
        "shadow_cost_usd": shadow_usd,
        "quota_pct": quota,
    }
    if review_model is not None:
        metadata["review_model"] = review_model
    if project_rules is not None:
        metadata["project_rules"] = project_rules
    return metadata


def write_step_summary(markdown: str) -> None:
    """Append markdown to the GitHub Step Summary, if running in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")
    except OSError as e:
        logger.warning(f"Could not write step summary: {e}")


def workspace_root() -> str:
    """Root that maps to the host workspace the upload-artifact step reads.

    This action runs as a Docker container, where the host workspace is
    bind-mounted at ``/github/workspace`` (``--workdir /github/workspace``).
    ``$GITHUB_WORKSPACE`` inside the container is forwarded as the *host* path
    (e.g. ``/home/runner/work/<repo>/<repo>``), which is NOT mounted — writing
    there lands on the container's ephemeral FS and is lost on ``--rm``, so a
    later upload step finds nothing. Prefer the mount point when it exists.
    """
    if os.path.isdir("/github/workspace"):
        return "/github/workspace"
    return os.environ.get("GITHUB_WORKSPACE", ".")


def metrics_dir() -> str:
    """Persistent dir (in the workspace, so the upload-artifact step can grab it)."""
    base = os.environ.get("KIMI_METRICS_DIR") or os.path.join(
        workspace_root(), ".kimi-review"
    )
    os.makedirs(base, exist_ok=True)
    return base


def snapshot_handoffs(work_dir: str, dest_dir: str, files: List[str]) -> None:
    """Copy the per-stage handoff JSONs out of the temp work dir before it's cleaned up."""
    for name in files:
        src = os.path.join(work_dir, name)
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8") as fr, open(
                os.path.join(dest_dir, name), "w", encoding="utf-8"
            ) as fw:
                fw.write(redact_secrets(fr.read()))
        except OSError as e:
            logger.warning(f"Could not snapshot {name}: {e}")


def write_metadata(metadata: Dict[str, Any], dest_dir: str) -> None:
    try:
        with open(os.path.join(dest_dir, "run-metadata.json"), "w", encoding="utf-8") as f:
            json.dump(redact_obj(metadata), f, indent=2)
    except OSError as e:
        logger.warning(f"Could not write run-metadata.json: {e}")


def emit(
    *,
    repo: str,
    pr_number: int,
    sha: str,
    model: str,
    stage_metrics: List[Dict[str, Any]],
    verdict: str,
    num_issues: int,
    dest_dir: Optional[str] = None,
    review_model: Optional[Dict[str, Any]] = None,
    project_rules: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build + persist the run summary. Returns the metadata dict (also for tests)."""
    totals = summarize(stage_metrics)
    shadow = shadow_cost_usd(totals, model, load_price_table())
    quota = quota_pct(totals["total_tokens"])

    markdown = build_markdown(stage_metrics, totals, model, shadow, quota)
    write_step_summary(markdown)

    metadata = build_metadata(
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        model=model,
        stage_metrics=stage_metrics,
        totals=totals,
        verdict=verdict,
        num_issues=num_issues,
        shadow_usd=shadow,
        quota=quota,
        review_model=review_model,
        project_rules=project_rules,
    )
    write_metadata(metadata, dest_dir or metrics_dir())

    logger.info(
        "review spend — %s tokens (in %s / out %s) across %d calls, %.0fs%s",
        _fmt_int(totals["total_tokens"]),
        _fmt_int(totals["input"]),
        _fmt_int(totals["output"]),
        totals["calls"],
        totals["seconds"],
        f", ~{quota:.1f}% quota" if quota is not None else "",
    )
    return metadata
