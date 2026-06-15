"""Unit tests for run_metrics (pure — no Agent SDK)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import run_metrics  # noqa: E402


STAGES = [
    {"stage": "planner", "seconds": 330.0, "attempts": 1, "input_other": 100_000,
     "input_cache_read": 50_000, "input_cache_creation": 0, "output": 4_000, "calls": 25},
    {"stage": "executor", "seconds": 120.0, "attempts": 1, "input_other": 40_000,
     "input_cache_read": 10_000, "input_cache_creation": 0, "output": 3_000, "calls": 8},
    {"stage": "qa", "seconds": 60.0, "attempts": 2, "input_other": 20_000,
     "input_cache_read": 5_000, "input_cache_creation": 0, "output": 1_500, "calls": 5},
]


def test_summarize_totals():
    t = run_metrics.summarize(STAGES)
    assert t["input"] == 100_000 + 50_000 + 40_000 + 10_000 + 20_000 + 5_000
    assert t["output"] == 4_000 + 3_000 + 1_500
    assert t["total_tokens"] == t["input"] + t["output"]
    assert t["calls"] == 25 + 8 + 5
    assert t["seconds"] == 510.0


def test_summarize_empty():
    t = run_metrics.summarize([])
    assert t["total_tokens"] == 0 and t["calls"] == 0 and t["seconds"] == 0.0


def test_shadow_cost_uses_cache_rate():
    table = {"m": {"input": 0.15, "output": 0.60, "cache_read": 0.015}}
    t = run_metrics.summarize(STAGES)
    cost = run_metrics.shadow_cost_usd(t, "m", table)
    # uncached input billed at 0.15, cached at 0.015, output at 0.60
    expected = (160_000 / 1e6 * 0.15) + (65_000 / 1e6 * 0.015) + (8_500 / 1e6 * 0.60)
    assert cost == round(expected, 4)


def test_shadow_cost_none_when_no_price():
    t = run_metrics.summarize(STAGES)
    assert run_metrics.shadow_cost_usd(t, "unknown-model", {}) is None


def test_quota_pct(monkeypatch):
    monkeypatch.setenv("KIMI_QUOTA_TOKENS_PER_WINDOW", "1000000")
    assert run_metrics.quota_pct(130_000) == 13.0
    monkeypatch.delenv("KIMI_QUOTA_TOKENS_PER_WINDOW", raising=False)
    assert run_metrics.quota_pct(130_000) is None


def test_build_markdown_has_stages_and_total():
    t = run_metrics.summarize(STAGES)
    md = run_metrics.build_markdown(STAGES, t, "kimi-k2.7-code", None, 13.0)
    assert "`planner`" in md and "`executor`" in md and "`qa`" in md
    assert "Total" in md and "kimi-k2.7-code" in md
    assert "13.0%" in md  # quota surfaced
    assert "(×2)" in md   # qa retried


def test_load_price_table(monkeypatch):
    # Custom entry overlays built-in table
    monkeypatch.setenv("KIMI_PRICE_TABLE_JSON", '{"m": {"input": 1, "output": 2}}')
    table = run_metrics.load_price_table()
    assert table["m"]["output"] == 2
    assert "kimi-k2.7-code" in table  # built-in still present

    # Invalid JSON falls back to built-in only
    monkeypatch.setenv("KIMI_PRICE_TABLE_JSON", "not json")
    table = run_metrics.load_price_table()
    assert "kimi-k2.7-code" in table
    assert "m" not in table


def test_builtin_pricing():
    t = run_metrics.summarize(STAGES)
    table = run_metrics.load_price_table()
    cost = run_metrics.shadow_cost_usd(t, "kimi-k2.7-code", table)
    assert cost is not None
    # Verify formula: uncached * 0.95 + cache_read * 0.19 + output * 4.00 (per MTok)
    uncached = t["input_other"] + t["input_cache_creation"]
    expected = (uncached / 1e6 * 0.95) + (t["input_cache_read"] / 1e6 * 0.19) + (t["output"] / 1e6 * 4.00)
    assert cost == round(expected, 4)


def test_snapshot_handoffs(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "review-plan.json").write_text('{"issues": []}')
    dest = tmp_path / "out"
    dest.mkdir()
    run_metrics.snapshot_handoffs(str(work), str(dest), ["review-plan.json", "missing.json"])
    assert (dest / "review-plan.json").exists()
    assert not (dest / "missing.json").exists()


def test_emit_writes_summary_and_metadata(tmp_path, monkeypatch):
    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("KIMI_QUOTA_TOKENS_PER_WINDOW", "1000000")
    dest = tmp_path / "metrics"
    dest.mkdir()

    meta = run_metrics.emit(
        repo="Nyle-ai/x", pr_number=7, sha="abc123", model="kimi-k2.7-code",
        stage_metrics=STAGES, verdict="approve", num_issues=0, dest_dir=str(dest),
    )

    assert meta["repo"] == "Nyle-ai/x" and meta["pr"] == 7 and meta["verdict"] == "approve"
    assert meta["totals"]["total_tokens"] == run_metrics.summarize(STAGES)["total_tokens"]
    assert meta["quota_pct"] is not None

    written = json.loads((dest / "run-metadata.json").read_text())
    assert written["model"] == "kimi-k2.7-code"
    assert "spend by stage" in summary_file.read_text()
