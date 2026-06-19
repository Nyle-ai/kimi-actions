"""Tests for the three-agent Reviewer tool."""

import os
import sys
from unittest.mock import Mock, patch

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _mock_file(filename, patch_text="@@ -1,2 +1,2 @@\n+a\n+b"):
    f = Mock()
    f.filename = filename
    f.patch = patch_text
    return f


class MockGitHubClient:
    """Mock GitHub client for testing."""

    def __init__(self, files=None):
        self.posted_comments = []
        self.reviews = []
        self.resolved_threads = []
        self.bot_threads = []
        self.latest_bot_review_state = None
        self._files = files if files is not None else [_mock_file("src/main.py")]

    def get_pr(self, repo, pr_number):
        return Mock(
            number=pr_number,
            title="Test PR",
            body="Test PR body",
            head=Mock(ref="feature-branch", sha="abc123def456789"),
            base=Mock(ref="main"),
            get_files=Mock(return_value=self._files),
        )

    def get_last_bot_comment(self, repo, pr_number):
        return None

    def post_comment(self, repo, pr_number, body):
        self.posted_comments.append(body)

    def _get_diff_line_map(self, repo, pr_number):
        return {f.filename: {1, 2} for f in self._files}

    def create_review_with_comments(
        self, repo, pr_number, comments, body="", event="COMMENT"
    ):
        self.reviews.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "comments": comments,
                "body": body,
                "event": event,
            }
        )

    def get_bot_review_threads(self, repo, pr_number, bot_login=None):
        return self.bot_threads

    def get_latest_bot_review_state(self, repo, pr_number, bot_login=None):
        return self.latest_bot_review_state

    def resolve_review_thread(self, thread_id):
        self.resolved_threads.append(thread_id)
        return True


@pytest.fixture
def mock_action_config():
    """Create mock action config."""
    with patch("tools.base.get_action_config") as mock:
        config = Mock()
        config.model = "kimi-k2.7-code"
        config.review_level = "normal"
        config.max_files = 50
        config.exclude_patterns = ["*.lock"]
        config.review = Mock(num_max_findings=10, extra_instructions="")
        config.enable_inline_comments = True
        config.enable_auto_resolve = True
        mock.return_value = config
        yield config


class TestReviewerBasic:
    def test_reviewer_initialization(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        assert reviewer.github == github
        assert reviewer.skill_name == "code-review"


class TestReviewerIntegration:
    def test_run_with_empty_diff(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient(files=[])
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()

        result = reviewer.run("owner/repo", 123)
        assert "No changes to review" in result

    def test_run_with_no_skill(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()
        reviewer.repo_config = None
        reviewer.get_skill = Mock(return_value=None)

        result = reviewer.run("owner/repo", 123)
        assert "Error" in result
        assert "skill not found" in result.lower()

    def test_run_success_posts_inline_and_summary(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()
        reviewer.repo_config = None
        reviewer.get_skill = Mock(
            return_value=Mock(instructions="Review", scripts={}, path=None)
        )

        qa_result = {
            "issues": [
                {
                    "path": "src/main.py",
                    "line": 2,
                    "severity": "high",
                    "category": "bug",
                    "title": "Boom",
                    "body": "Explanation",
                }
            ],
            "verdict": "comment",
            "summary": "A summary.",
        }
        with patch.object(reviewer, "clone_repo", return_value=True):
            with patch("asyncio.run", return_value=qa_result):
                result = reviewer.run("owner/repo", 123)

        # Inline review posted with one anchored comment.
        assert len(github.reviews) == 1
        assert github.reviews[0]["comments"][0]["path"] == "src/main.py"
        assert github.reviews[0]["event"] == "REQUEST_CHANGES"
        # Summary returned for the issue comment.
        assert "Pull Request Overview" in result
        assert "Boom" in result
        assert "Request changes" in result
        assert "<!-- kimi-review:sha=abc123def456 -->" in result

    def test_run_with_no_issues_clears_prior_request_changes(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        github.latest_bot_review_state = "CHANGES_REQUESTED"
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()
        reviewer.repo_config = None
        reviewer.get_skill = Mock(
            return_value=Mock(instructions="Review", scripts={}, path=None)
        )

        qa_result = {"issues": [], "verdict": "approve", "summary": "Clean."}
        with patch.object(reviewer, "clone_repo", return_value=True):
            with patch("asyncio.run", return_value=qa_result):
                result = reviewer.run("owner/repo", 123)

        assert len(github.reviews) == 1
        assert github.reviews[0]["event"] == "APPROVE"
        assert github.reviews[0]["comments"] == []
        assert "Approve" in result

    def test_run_with_no_issues_without_prior_block_posts_no_review(
        self, mock_action_config
    ):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()
        reviewer.repo_config = None
        reviewer.get_skill = Mock(
            return_value=Mock(instructions="Review", scripts={}, path=None)
        )

        qa_result = {"issues": [], "verdict": "approve", "summary": "Clean."}
        with patch.object(reviewer, "clone_repo", return_value=True):
            with patch("asyncio.run", return_value=qa_result):
                result = reviewer.run("owner/repo", 123)

        assert github.reviews == []
        assert "Approve" in result

    def test_no_new_changes_short_circuit(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        # 12-char prefix of head sha abc123def456789
        github.get_last_bot_comment = Mock(return_value={"sha": "abc123def456"})
        reviewer = Reviewer(github)
        reviewer.load_context = Mock()

        result = reviewer.run("owner/repo", 123)
        assert "No new changes since last review" in result


class TestAnchoring:
    def test_dedup_and_nearest_line(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        diff_map = {"a.py": {10, 11, 20}}
        issues = [
            {"path": "a.py", "line": 10, "title": "x", "body": "b"},
            {"path": "a.py", "line": 10, "title": "dup", "body": "b"},  # dup -> dropped
            {"path": "a.py", "line": 15, "title": "snap", "body": "b"},  # -> nearest 11
            {"path": "missing.py", "line": 1, "title": "overflow", "body": "b"},
        ]
        comments, overflow = reviewer._anchor_comments(issues, diff_map)
        anchored = sorted(c["line"] for c in comments)
        assert anchored == [10, 11]  # 15 snaps to 11 (nearest); dup dropped
        assert len(overflow) == 1
        assert overflow[0]["path"] == "missing.py"


class TestVerdict:
    def test_high_or_critical_in_substantive_category_requests_changes(
        self, mock_action_config
    ):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        assert (
            reviewer._deterministic_verdict([{"severity": "high", "category": "bug"}])
            == "request_changes"
        )
        assert (
            reviewer._deterministic_verdict(
                [{"severity": "critical", "category": "security"}]
            )
            == "request_changes"
        )

    def test_high_in_non_blocking_category_only_comments(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        # The model labels lint/type-safety findings "high"; these comment, not block.
        assert (
            reviewer._deterministic_verdict([{"severity": "high", "category": "quality"}])
            == "comment"
        )
        assert (
            reviewer._deterministic_verdict(
                [{"severity": "critical", "category": "performance"}]
            )
            == "comment"
        )

    def test_medium_low_comments_and_empty_approves(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        assert (
            reviewer._deterministic_verdict([{"severity": "medium", "category": "bug"}])
            == "comment"
        )
        assert (
            reviewer._deterministic_verdict([{"severity": "low", "category": "bug"}])
            == "comment"
        )
        assert reviewer._deterministic_verdict([]) == "approve"

    def test_non_blocking_review_event_comments_without_prior_block(
        self, mock_action_config
    ):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        reviewer._post_inline(
            "owner/repo",
            1,
            [
                {
                    "path": "src/main.py",
                    "line": 1,
                    "severity": "medium",
                    "category": "bug",
                    "title": "FYI",
                    "body": "Non-blocking.",
                }
            ],
            "comment",
        )

        assert github.reviews[0]["event"] == "COMMENT"
        assert github.reviews[0]["comments"]

    def test_non_blocking_review_event_clears_prior_request_changes(
        self, mock_action_config
    ):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        github.latest_bot_review_state = "CHANGES_REQUESTED"
        reviewer = Reviewer(github)
        reviewer._post_inline(
            "owner/repo",
            1,
            [
                {
                    "path": "src/main.py",
                    "line": 1,
                    "severity": "medium",
                    "category": "bug",
                    "title": "FYI",
                    "body": "Non-blocking.",
                }
            ],
            "comment",
        )

        assert github.reviews[0]["event"] == "APPROVE"
        assert github.reviews[0]["comments"]

    def test_request_changes_posts_even_when_inline_disabled(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        reviewer = Reviewer(github)
        reviewer.config.enable_inline_comments = False
        reviewer._post_inline(
            "owner/repo",
            1,
            [
                {
                    "path": "src/main.py",
                    "line": 1,
                    "severity": "high",
                    "category": "bug",
                    "title": "Blocking",
                    "body": "Must fix.",
                }
            ],
            "request_changes",
        )

        assert github.reviews[0]["event"] == "REQUEST_CHANGES"
        assert github.reviews[0]["comments"] == []
        assert "Blocking" in github.reviews[0]["body"]


class TestAutoResolve:
    def test_resolves_fixed_threads(self, mock_action_config):
        from tools.reviewer import Reviewer

        github = MockGitHubClient()
        github.bot_threads = [
            {"thread_id": "T1", "path": "a.py", "line": 5, "is_resolved": False},
            {"thread_id": "T2", "path": "b.py", "line": 9, "is_resolved": False},
            {"thread_id": "T3", "path": "c.py", "line": 1, "is_resolved": True},
        ]
        reviewer = Reviewer(github)
        # Current review still raises a.py:5 -> keep; b.py:9 not raised -> resolve.
        issues = [{"path": "a.py", "line": 5}]
        reviewer._auto_resolve("owner/repo", 1, issues)
        assert github.resolved_threads == ["T2"]


class TestPipeline:
    @pytest.mark.asyncio
    async def test_planner_zero_issues_short_circuits(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        reviewer.repo_config = None
        skill = Mock(instructions="rubric", path=None)

        async def fake_stage(work_dir, prompt, label="agent"):
            # Planner writes an empty issue list.
            with open(os.path.join(work_dir, "review-plan.json"), "w") as f:
                f.write('{"issues": []}')
            return ""

        with patch.object(reviewer, "run_agent_reliably", side_effect=fake_stage) as ran:
            qa = await reviewer._run_pipeline(
                str(tmp_path), skill, "title", "a -> b", "diff text"
            )
        assert qa == {"issues": [], "verdict": "approve", "summary": ""}
        # Only the planner ran; executor/QA were skipped.
        assert ran.call_count == 1

    @pytest.mark.asyncio
    async def test_ticket_context_reaches_planner(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        reviewer.repo_config = None
        skill = Mock(instructions="rubric", path=None)
        captured = {}

        async def fake_stage(work_dir, prompt, label="agent"):
            captured[label] = prompt
            with open(os.path.join(work_dir, "review-plan.json"), "w") as f:
                f.write('{"issues": []}')
            return ""

        with patch.object(reviewer, "run_agent_reliably", side_effect=fake_stage):
            await reviewer._run_pipeline(
                str(tmp_path),
                skill,
                "title",
                "a -> b",
                "diff",
                ticket_context="## Linked ticket\n- ID: ENG-1",
            )
        assert "Linked ticket" in captured["planner"]
        assert "ENG-1" in captured["planner"]

    @pytest.mark.asyncio
    async def test_coverage_and_project_rules_reach_planner(
        self, mock_action_config, tmp_path
    ):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        reviewer.repo_config = None
        skill = Mock(instructions="rubric", path=None)
        captured = {}

        async def fake_stage(work_dir, prompt, label="agent"):
            captured[label] = prompt
            with open(os.path.join(work_dir, "review-plan.json"), "w") as f:
                f.write('{"issues": []}')
            return ""

        with patch.object(reviewer, "run_agent_reliably", side_effect=fake_stage):
            await reviewer._run_pipeline(
                str(tmp_path),
                skill,
                "title",
                "a -> b",
                "diff",
                review_model={
                    "total_changed_files": 2,
                    "reviewed_count": 1,
                    "max_files": 1,
                    "files": [
                        {
                            "filename": "src/app/api/users/route.ts",
                            "included": True,
                            "risk_tags": ["api", "security"],
                        },
                        {
                            "filename": "docs/readme.md",
                            "included": False,
                            "reason": "max_files_cap",
                            "risk_tags": [],
                        },
                    ],
                    "unreviewed_files": [
                        {
                            "filename": "docs/readme.md",
                            "reason": "max_files_cap",
                            "risk_tags": [],
                        }
                    ],
                },
                project_rules=[
                    {
                        "path": ".claude/rules/api-routes.md",
                        "reason": "matched_risk_or_path",
                        "content": "Validate org scoping.",
                    }
                ],
            )
        assert "Deterministic PR coverage model" in captured["planner"]
        assert "untrusted project guidance" in captured["planner"]
        assert "docs/readme.md" in captured["planner"]
        assert "api-routes.md" in captured["planner"]
        assert "Validate org scoping" in captured["planner"]

    def test_format_ticket(self, mock_action_config):
        from tools.reviewer import Reviewer
        from ticket_context import TicketContext

        reviewer = Reviewer(MockGitHubClient())
        assert reviewer._format_ticket(None) == ""
        out = reviewer._format_ticket(
            TicketContext(id="ENG-1", title="Add x", description="do x", status="Open")
        )
        assert "Linked ticket" in out
        assert "ENG-1" in out and "Add x" in out and "do x" in out

    @pytest.mark.asyncio
    async def test_full_three_stage_handoff(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        reviewer.repo_config = None
        skill = Mock(instructions="rubric", path=None)

        outputs = {
            "planner": '{"issues": [{"path": "a.py", "line": 1, "severity": "high"}]}',
            "executor": '{"issues": [{"path": "a.py", "line": 1, "body": "x"}], "verdict": "comment"}',
            "qa": '{"issues": [{"path": "a.py", "line": 1, "body": "x"}], "verdict": "comment", "summary": "ok"}',
        }
        filenames = {
            "planner": "review-plan.json",
            "executor": "review-draft.json",
            "qa": "qa-validated-review.json",
        }

        async def fake_stage(work_dir, prompt, label="agent"):
            with open(os.path.join(work_dir, filenames[label]), "w") as f:
                f.write(outputs[label])
            return ""

        with patch.object(reviewer, "run_agent_reliably", side_effect=fake_stage) as ran:
            qa = await reviewer._run_pipeline(
                str(tmp_path), skill, "title", "a -> b", "diff text"
            )
        assert ran.call_count == 3
        assert qa["summary"] == "ok"
        assert qa["issues"][0]["path"] == "a.py"


class TestStageJson:
    def test_reads_from_disk(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        (tmp_path / "review-plan.json").write_text(
            '{"issues": [{"path": "a.py", "line": 1}]}'
        )
        out = reviewer._read_stage_json(str(tmp_path), "review-plan.json", "")
        assert out["issues"][0]["line"] == 1

    def test_falls_back_to_text(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        text = 'prose ```json\n{"issues": []}\n``` more'
        out = reviewer._read_stage_json(str(tmp_path), "missing.json", text)
        assert out == {"issues": []}

    def test_returns_empty_when_unusable(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        out = reviewer._read_stage_json(str(tmp_path), "missing.json", "no json here")
        assert out["issues"] == []
        assert out["_schema_errors"]

    def test_normalizes_legacy_schema(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        (tmp_path / "review-draft.json").write_text(
            '{"findings": [{"path": "a.py", "line": "42", "severity": "HIGH", '
            '"category": "bug"}], "verdict": "REQUEST_CHANGES"}'
        )
        out = reviewer._read_stage_json(str(tmp_path), "review-draft.json", "")
        assert out["issues"][0]["line"] == 42
        assert out["issues"][0]["severity"] == "high"
        assert out["verdict"] == "request_changes"
        assert out["_schema_warnings"]

    def test_drops_empty_issue_objects(self, mock_action_config, tmp_path):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        (tmp_path / "review-plan.json").write_text(
            '{"issues": [{}, {"path": "a.py", "severity": "medium"}]}'
        )
        out = reviewer._read_stage_json(str(tmp_path), "review-plan.json", "")
        assert len(out["issues"]) == 1
        assert out["issues"][0]["path"] == "a.py"

    @pytest.mark.asyncio
    async def test_planner_schema_errors_fail_closed(
        self, mock_action_config, tmp_path
    ):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        reviewer.repo_config = None
        skill = Mock(instructions="rubric", path=None)

        async def fake_stage(work_dir, prompt, label="agent"):
            with open(os.path.join(work_dir, "review-plan.json"), "w") as f:
                f.write('{"summary": "missing issues"}')
            return ""

        with patch.object(reviewer, "run_agent_reliably", side_effect=fake_stage):
            with pytest.raises(ValueError):
                await reviewer._run_pipeline(
                    str(tmp_path), skill, "title", "a -> b", "diff text"
                )


class TestProjectRules:
    def test_loads_top_level_and_matching_rule_files(
        self, mock_action_config, tmp_path
    ):
        from tools.reviewer import Reviewer

        (tmp_path / "CLAUDE.md").write_text(
            "Follow .claude/rules/api-routes.md for route changes."
        )
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "api-routes.md").write_text(
            "Validate route body and org scoping."
        )
        (rules_dir / "queue.md").write_text("Use idempotent queue jobs.")
        (rules_dir / "ui.md").write_text("Visual polish.")

        reviewer = Reviewer(MockGitHubClient())
        loaded = reviewer._load_project_rules(
            str(tmp_path),
            {
                "reviewed_files": ["src/app/api/users/route.ts"],
                "files": [{"risk_tags": ["api"]}],
            },
        )
        paths = {rule["path"] for rule in loaded}
        assert "CLAUDE.md" in paths
        assert ".claude/rules/api-routes.md" in paths
        assert ".claude/rules/ui.md" not in paths


class TestStageJsonTolerance:
    """Normalization tolerates benign model schema variation seen in real trajectories,
    while still failing closed on genuinely unusable output."""

    def test_unwraps_review_envelope(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        data = {
            "review": {
                "issues": [
                    {"location": {"file": "a.ts", "lines": "34-52"},
                     "severity": "medium", "category": "type-safety",
                     "title": "t", "description": "d"}
                ],
                "verdict": "APPROVE_WITH_COMMENTS",
            }
        }
        out = reviewer._normalize_stage_json(data, "review-draft.json")
        assert not out.get("_schema_errors")
        assert len(out["issues"]) == 1
        issue = out["issues"][0]
        assert issue["path"] == "a.ts"
        assert issue["line"] == 52 and issue["start_line"] == 34
        assert issue["category"] == "quality"  # type-safety -> quality
        assert issue["body"] == "d"            # description -> body

    def test_accepts_candidate_issues_container(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        data = {"version": 1, "candidateIssues": [
            {"path": "x.ts", "line": 5, "severity": "low",
             "category": "correctness", "title": "t"}]}
        out = reviewer._normalize_stage_json(data, "review-plan.json")
        assert not out.get("_schema_errors")
        assert len(out["issues"]) == 1
        assert out["issues"][0]["category"] == "bug"  # correctness -> bug

    def test_alt_location_keys_derive_path_and_line(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        data = {"issues": [
            {"file": "y.ts", "lines": [26, 29], "severity": "high",
             "category": "bug", "title": "t"},
            {"file": "z.ts", "lines": "40", "severity": "high",
             "category": "bug", "title": "t2"},
        ]}
        out = reviewer._normalize_stage_json(data, "review-plan.json")
        a, b = out["issues"]
        assert a["path"] == "y.ts" and a["line"] == 29 and a["start_line"] == 26
        assert b["path"] == "z.ts" and b["line"] == 40

    def test_preserves_needs_verification_for_executor(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        data = {"issues": [
            {"path": "a.ts", "line": 1, "severity": "high", "category": "bug",
             "title": "t", "rationale": "r", "needs_verification": True}]}
        out = reviewer._normalize_stage_json(data, "review-plan.json")
        assert out["issues"][0]["needs_verification"] is True

    def test_genuinely_unparseable_still_fails_closed(self, mock_action_config):
        from tools.reviewer import Reviewer

        reviewer = Reviewer(MockGitHubClient())
        out = reviewer._normalize_stage_json({"summary": "no issues key"}, "review-plan.json")
        assert out["issues"] == []
        assert out["_schema_errors"]
        with pytest.raises(ValueError):
            reviewer._raise_schema_errors(out, "planner")


class TestDiffByteCap:
    """The diff handed to the model is bounded per-file and in total, and the coverage
    model records what was truncated/omitted so the prompt block stays honest."""

    def test_truncates_oversized_patch_and_omits_overflow(self, mock_action_config):
        from tools.reviewer import (
            Reviewer,
            MAX_PATCH_BYTES_PER_FILE,
            MAX_DIFF_TOTAL_BYTES,
        )

        files = [_mock_file("big.py", patch_text="x" * (MAX_PATCH_BYTES_PER_FILE + 500))]
        overflow = (MAX_DIFF_TOTAL_BYTES // MAX_PATCH_BYTES_PER_FILE) + 3
        for i in range(overflow):
            files.append(_mock_file(f"f{i}.py", patch_text="y" * MAX_PATCH_BYTES_PER_FILE))

        reviewer = Reviewer(MockGitHubClient(files=files))
        reviewer.repo_config = None
        reviewer.config.max_files = 1000  # keep every file in the coverage model

        diff, model = reviewer._build_filtered_diff("o/r", 1)

        assert "big.py" in model["truncated_files"]
        assert "[patch truncated]" in diff
        assert model["diff_omitted_files"]  # ran out of total budget
        # Total stays bounded (last included file may straddle the budget).
        assert len(diff) <= MAX_DIFF_TOTAL_BYTES + MAX_PATCH_BYTES_PER_FILE + 200
        block = reviewer._review_model_block(model)
        assert "Diff size limits applied" in block
