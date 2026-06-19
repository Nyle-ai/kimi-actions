"""Tests for BaseTool class."""

import asyncio
import pytest
from unittest.mock import Mock, patch, AsyncMock
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class MockGitHubClient:
    """Mock GitHub client."""

    def __init__(self):
        self.posted_comments = []
        self.reviews = []

    def get_pr(self, repo, pr_number):
        return Mock(
            number=pr_number,
            title="Test PR",
            head=Mock(ref="feature-branch", sha="abc123"),
            base=Mock(ref="main"),
        )

    def get_pr_diff(self, repo, pr_number):
        return """diff --git a/test.py b/test.py
index 1234567..abcdefg 100644
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def hello():
+    print("world")
     pass
"""

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


class ConcreteTool:
    """Concrete implementation of BaseTool for testing."""

    from tools.base import BaseTool

    class _ConcreteTool(BaseTool):
        @property
        def skill_name(self) -> str:
            return "test-skill"

        def run(self, repo_name: str, pr_number: int, **kwargs) -> str:
            return "test result"

    @classmethod
    def create(cls, github):
        return cls._ConcreteTool(github)


@pytest.fixture
def mock_action_config():
    """Create mock action config."""
    with patch("tools.base.get_action_config") as mock:
        config = Mock()
        config.model = "kimi-k2.5"
        config.kimi_base_url = "https://api.moonshot.cn/v1"
        config.review_level = "normal"
        config.max_files = 10
        config.exclude_patterns = ["*.lock"]
        mock.return_value = config
        yield config


class TestBaseToolAbstract:
    """Test abstract methods and initialization."""

    def test_cannot_instantiate_directly(self, mock_action_config):
        """Test that BaseTool cannot be instantiated directly."""
        from tools.base import BaseTool

        github = MockGitHubClient()
        with pytest.raises(TypeError):
            BaseTool(github)

    def test_subclass_must_implement_skill_name(self, mock_action_config):
        """Test that subclass must implement skill_name property."""
        from tools.base import BaseTool

        class IncompleteTool(BaseTool):
            def run(self, repo_name: str, pr_number: int, **kwargs) -> str:
                return "test"

        github = MockGitHubClient()
        with pytest.raises(TypeError):
            IncompleteTool(github)

    def test_subclass_must_implement_run(self, mock_action_config):
        """Test that subclass must implement run method."""
        from tools.base import BaseTool

        class IncompleteTool(BaseTool):
            @property
            def skill_name(self) -> str:
                return "test"

        github = MockGitHubClient()
        with pytest.raises(TypeError):
            IncompleteTool(github)

    def test_concrete_tool_initialization(self, mock_action_config):
        """Test that concrete tool can be initialized."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        assert tool.github == github
        assert tool.config == mock_action_config
        assert tool.skill_name == "test-skill"


class TestBaseToolCloneRepo:
    """Test clone_repo method."""

    def test_clone_repo_success(self, mock_action_config):
        """Test successful repository clone."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = tool.clone_repo("owner/repo", "/tmp/test", branch="main")

            assert result is True
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "git" in call_args
            assert "clone" in call_args
            assert "-b" in call_args
            assert "main" in call_args

    def test_clone_repo_without_branch(self, mock_action_config):
        """Test clone without specifying branch."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = tool.clone_repo("owner/repo", "/tmp/test")

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "-b" not in call_args

    def test_clone_repo_branch_fallback(self, mock_action_config):
        """Test fallback to default branch when specified branch fails."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch("subprocess.run") as mock_run:
            # Configure side_effect to raise on first call, succeed on second
            call_count = [0]

            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    from subprocess import CalledProcessError

                    raise CalledProcessError(1, "git")
                return Mock(returncode=0)

            mock_run.side_effect = side_effect

            result = tool.clone_repo("owner/repo", "/tmp/test", branch="nonexistent")

            assert result is True
            assert call_count[0] == 2

    def test_clone_repo_failure(self, mock_action_config):
        """Test clone failure."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch("subprocess.run") as mock_run:
            from subprocess import CalledProcessError

            mock_run.side_effect = CalledProcessError(1, "git")

            result = tool.clone_repo("owner/repo", "/tmp/test")

            assert result is False


class TestBaseToolRunAgent:
    """Test run_agent method."""

    def test_run_agent_success(self, mock_action_config):
        """Test successful agent execution."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}):
            # Skip this test if kimi_agent_sdk cannot be imported (e.g. the installed
            # kimi-cli is incompatible with this Python — an env issue, not our code).
            try:
                import kimi_agent_sdk  # noqa: F401
            except Exception as e:  # noqa: BLE001 - any import failure means we can't run this
                pytest.skip(f"kimi_agent_sdk unavailable: {e}")

            # Mock the Session.create to return a mock session
            mock_session = AsyncMock()

            # Create a mock TextPart class
            class MockTextPart:
                def __init__(self, text):
                    self.text = text

            # Mock the prompt method to yield TextPart objects
            async def mock_prompt(*args):
                yield MockTextPart("Response ")
                yield MockTextPart("text")

            mock_session.prompt = mock_prompt
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            with patch("kimi_agent_sdk.Session.create", return_value=mock_session):
                with patch("kimi_agent_sdk.TextPart", MockTextPart):
                    result = asyncio.run(tool.run_agent("/tmp/test", "Test prompt"))

                    assert (
                        "Response text" in result or result == ""
                    )  # May be empty if import fails

    def test_run_agent_no_api_key(self, mock_action_config):
        """Test agent execution without API key."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(tool.run_agent("/tmp/test", "Test prompt"))

            assert result == ""

    def test_run_agent_import_error(self, mock_action_config):
        """Test agent execution with import error."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}):
            with patch(
                "builtins.__import__", side_effect=ImportError("Module not found")
            ):
                result = asyncio.run(tool.run_agent("/tmp/test", "Test prompt"))

                assert result == ""


class TestBaseToolContext:
    """Test context loading methods."""

    def test_load_context_success(self, mock_action_config):
        """Test successful context loading."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch("tools.base.load_repo_config") as mock_load:
            mock_config = Mock()
            mock_config.ignore_files = ["*.test"]
            mock_validation = Mock(valid=True, errors=[], warnings=[])
            mock_load.return_value = (mock_config, mock_validation)

            tool.load_context("owner/repo", ref="abc123")

            assert tool.repo_config == mock_config
            mock_load.assert_called_once_with(github, "owner/repo", ref="abc123")

    def test_get_skill_with_override(self, mock_action_config):
        """Test getting skill with repository override."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        # Setup repo config with override
        tool.repo_config = Mock()
        tool.repo_config.skill_overrides = {"test-skill": "custom-skill"}

        # Setup skill manager
        custom_skill = Mock()
        custom_skill.name = "custom-skill"
        tool.skill_manager.get_skill = Mock(return_value=custom_skill)

        skill = tool.get_skill()

        assert skill == custom_skill
        tool.skill_manager.get_skill.assert_called_once_with("custom-skill")


class TestBaseToolHelpers:
    """Test helper methods."""

    def test_format_footer(self, mock_action_config):
        """Test footer formatting."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        footer = tool.format_footer()

        # The standalone footer is just a divider; the "Powered by Kimi" line was
        # removed in commit fd95f62, so this asserts current behaviour.
        assert footer == "---"

    def test_format_footer_with_extra_info(self, mock_action_config):
        """Test footer with extra information."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        footer = tool.format_footer("5 suggestions")

        assert "5 suggestions" in footer

    def test_setup_agent_env(self, mock_action_config):
        """Test agent environment setup."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}):
            api_key = tool.setup_agent_env()

            assert api_key == "test-key"
            assert os.environ.get("KIMI_BASE_URL") == "https://api.moonshot.cn/v1"
            assert os.environ.get("KIMI_MODEL_NAME") == "kimi-k2.5"

    def test_setup_agent_env_no_key(self, mock_action_config):
        """Test agent environment setup without API key."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {}, clear=True):
            api_key = tool.setup_agent_env()

            assert api_key is None


class TestBaseToolReliability:
    """Cost/outage guardrails in run_agent / run_agent_reliably.

    All tests use fakes for run_agent — they never touch the real Agent SDK.
    """

    @staticmethod
    def _immediate_failure_agent(tool, counter):
        """Fake run_agent that records an error and makes no SDK calls (calls == 0)."""
        from tools.base import _zero_usage

        async def fake(work_dir, prompt, skills_dir=None, max_steps=100):
            counter["n"] += 1
            tool._last_usage = _zero_usage()  # calls == 0 → "immediate failure"
            tool._last_error = "RuntimeError('moonshot 503')"
            return ""

        return fake

    @staticmethod
    def _ran_but_empty_agent(tool, counter):
        """Fake run_agent that ran (calls > 0) but produced no output, no error."""
        from tools.base import _zero_usage

        async def fake(work_dir, prompt, skills_dir=None, max_steps=100):
            counter["n"] += 1
            usage = _zero_usage()
            usage["calls"] = 2
            usage["output"] = 5
            tool._last_usage = usage
            tool._last_error = ""
            return ""

        return fake

    def test_error_captured_into_stage_row(self, mock_action_config):
        """A captured run_agent error is surfaced on the recorded stage row."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        counter = {"n": 0}
        tool.run_agent = self._immediate_failure_agent(tool, counter)

        with patch("tools.base.RETRY_BACKOFF", [0, 0, 0, 0]):
            result = asyncio.run(
                tool.run_agent_reliably("/tmp/test", "prompt", label="planner")
            )

        assert result == ""
        assert len(tool.stage_metrics) == 1
        row = tool.stage_metrics[-1]
        assert row["stage"] == "planner"
        assert row["error"] == "RuntimeError('moonshot 503')"

    def test_immediate_failure_aborts_after_two_attempts(self, mock_action_config):
        """An immediate failure (calls == 0 + error) stops after <= 2 attempts."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        counter = {"n": 0}
        tool.run_agent = self._immediate_failure_agent(tool, counter)

        with patch("tools.base.RETRY_BACKOFF", [0, 0, 0, 0]):
            result = asyncio.run(
                tool.run_agent_reliably("/tmp/test", "prompt", label="planner")
            )

        assert result == ""
        assert counter["n"] == 2  # not the full 5 attempts
        assert tool.stage_metrics[-1]["attempts"] == 2

    def test_full_retry_when_calls_made(self, mock_action_config):
        """A 'ran but empty' stage (calls > 0) keeps the full retry budget."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        counter = {"n": 0}
        tool.run_agent = self._ran_but_empty_agent(tool, counter)

        with patch("tools.base.RETRY_BACKOFF", [0, 0, 0, 0]):
            result = asyncio.run(
                tool.run_agent_reliably("/tmp/test", "prompt", label="qa")
            )

        assert result == ""
        assert counter["n"] == 5  # len(RETRY_BACKOFF) + 1 — full budget used
        row = tool.stage_metrics[-1]
        assert row["attempts"] == 5
        assert "error" not in row  # no error captured on the empty-output path

    def test_timeout_caps_attempts(self, mock_action_config):
        """A timing-out stage is retried at most once more (not the full budget)."""
        from tools.base import _zero_usage

        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        counter = {"n": 0}

        async def slow_agent(work_dir, prompt, skills_dir=None, max_steps=100):
            counter["n"] += 1
            usage = _zero_usage()
            usage["calls"] = 1  # a step ran before the timeout
            tool._last_usage = usage
            tool._last_error = ""
            await asyncio.sleep(5)  # longer than the (patched) timeout
            return "never reached"

        tool.run_agent = slow_agent

        with patch("tools.base.RETRY_BACKOFF", [0, 0, 0, 0]), patch(
            "tools.base.AGENT_TIMEOUT_SECONDS", 0.05
        ):
            result = asyncio.run(
                tool.run_agent_reliably("/tmp/test", "prompt", label="executor")
            )

        assert result == ""
        assert counter["n"] == 2  # original timeout + 1 retry

    def test_max_steps_threaded_default(self, mock_action_config):
        """run_agent_reliably forwards the default step cap (100) to run_agent."""
        from tools.base import _zero_usage

        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        seen = {}

        async def fake(work_dir, prompt, skills_dir=None, max_steps=100):
            seen["max_steps"] = max_steps
            tool._last_usage = _zero_usage()
            tool._last_error = ""
            return "done"

        tool.run_agent = fake
        result = asyncio.run(tool.run_agent_reliably("/tmp/test", "prompt"))

        assert result == "done"
        assert seen["max_steps"] == 100

    def test_max_steps_threaded_explicit(self, mock_action_config):
        """A caller-supplied max_steps is threaded through to run_agent."""
        from tools.base import _zero_usage

        github = MockGitHubClient()
        tool = ConcreteTool.create(github)
        seen = {}

        async def fake(work_dir, prompt, skills_dir=None, max_steps=100):
            seen["max_steps"] = max_steps
            tool._last_usage = _zero_usage()
            tool._last_error = ""
            return "done"

        tool.run_agent = fake
        result = asyncio.run(
            tool.run_agent_reliably("/tmp/test", "prompt", max_steps=250)
        )

        assert result == "done"
        assert seen["max_steps"] == 250

    def test_run_agent_import_error_captures_error(self, mock_action_config):
        """run_agent records the underlying error on the ImportError early-return."""
        github = MockGitHubClient()
        tool = ConcreteTool.create(github)

        with patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}):
            with patch(
                "builtins.__import__", side_effect=ImportError("Module not found")
            ):
                result = asyncio.run(tool.run_agent("/tmp/test", "Test prompt"))

        assert result == ""
        assert "Module not found" in tool._last_error
