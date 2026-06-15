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
        # Summary returned for the issue comment.
        assert "Pull Request Overview" in result
        assert "Boom" in result
        assert "<!-- kimi-review:sha=abc123def456 -->" in result

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
        (tmp_path / "review-plan.json").write_text('{"issues": [{"line": 1}]}')
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
        assert out == {}
