"""Tests for main.py event handlers."""

import pytest
from unittest.mock import Mock, patch
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import (
    handle_review_comment_event,
    handle_pr_event,
    parse_command,
    DRAFT_SKIP_MARKER,
)
from action_config import ActionConfig


class TestParseCommand:
    """Tests for command parsing."""

    def test_parse_command_at_start(self):
        """Test parsing command at the start of comment."""
        command, args = parse_command("/ask What does this do?")
        assert command == "ask"
        assert args == "What does this do?"

    def test_parse_command_after_quotes(self):
        """Test parsing command after quoted lines."""
        body = """> some quoted text
> more quoted text

/ask What does this do?"""
        command, args = parse_command(body)
        assert command == "ask"
        assert args == "What does this do?"

    def test_parse_command_no_args(self):
        """Test parsing command without arguments."""
        command, args = parse_command("/review")
        assert command == "review"
        assert args == ""

    def test_parse_command_not_found(self):
        """Test when no command is found."""
        command, args = parse_command("Just a regular comment")
        assert command is None
        assert args is None


class TestHandleReviewCommentEvent:
    """Tests for pull_request_review_comment event handler."""

    @pytest.fixture
    def mock_config(self):
        """Create mock action config."""
        config = Mock(spec=ActionConfig)
        config.github_token = "test_token"
        config.kimi_api_key = "test_key"
        config.kimi_base_url = "https://api.moonshot.cn/v1"
        return config

    @pytest.fixture
    def review_comment_event(self):
        """Create a mock pull_request_review_comment event."""
        return {
            "action": "created",
            "comment": {
                "id": 123,
                "body": "/ask What does this function do?",
                "path": "src/auth.py",
                "line": 10,
                "original_line": 10,
                "diff_hunk": """@@ -5,10 +5,15 @@ import jwt
 
 def login(username, password):
     # TODO: validate credentials
+    user_id = username  # Bug: should lookup user
+    token = jwt.encode({"user_id": user_id}, "secret")
+    return token""",
            },
            "pull_request": {"number": 42},
            "repository": {"full_name": "owner/repo"},
        }

    def test_handle_ask_in_inline_comment(self, mock_config, review_comment_event):
        """Test /ask command in inline comment."""
        with patch("main.GitHubClient") as MockGitHub, patch("main.Ask") as MockAsk:
            # Setup mocks
            mock_github = Mock()
            MockGitHub.return_value = mock_github

            mock_ask_tool = Mock()
            mock_ask_tool.run.return_value = "This function authenticates users."
            MockAsk.return_value = mock_ask_tool

            # Call handler
            handle_review_comment_event(review_comment_event, mock_config)

            # Verify Ask tool was called with context
            MockAsk.assert_called_once_with(mock_github)
            assert mock_ask_tool.run.called
            call_args = mock_ask_tool.run.call_args

            # Check that question includes file context
            question = call_args.kwargs.get("question", "")
            assert "src/auth.py" in question
            assert "line 10" in question
            assert "inline" in call_args.kwargs
            assert call_args.kwargs["inline"] is True

            # Verify reply was posted
            mock_github.reply_to_review_comment.assert_called_once()
            reply_args = mock_github.reply_to_review_comment.call_args
            assert reply_args[0][0] == "owner/repo"
            assert reply_args[0][1] == 42
            assert reply_args[0][2] == 123
            assert "authenticates users" in reply_args[0][3]

    def test_handle_non_created_action(self, mock_config, review_comment_event):
        """Test that non-created actions are ignored."""
        review_comment_event["action"] = "edited"

        with patch("main.GitHubClient") as MockGitHub:
            handle_review_comment_event(review_comment_event, mock_config)

            # Should not initialize GitHub client
            MockGitHub.assert_not_called()

    def test_handle_no_command(self, mock_config, review_comment_event):
        """Test that comments without commands are ignored."""
        review_comment_event["comment"]["body"] = "Just a regular comment"

        with patch("main.GitHubClient") as MockGitHub:
            handle_review_comment_event(review_comment_event, mock_config)

            # Should not initialize GitHub client
            MockGitHub.assert_not_called()

    def test_handle_ask_without_question(self, mock_config, review_comment_event):
        """Test /ask without a question."""
        review_comment_event["comment"]["body"] = "/ask"

        with patch("main.GitHubClient") as MockGitHub:
            mock_github = Mock()
            MockGitHub.return_value = mock_github

            handle_review_comment_event(review_comment_event, mock_config)

            # Should post error message
            mock_github.reply_to_review_comment.assert_called_once()
            reply_args = mock_github.reply_to_review_comment.call_args
            assert "Please provide a question" in reply_args[0][3]

    def test_handle_other_command(self, mock_config, review_comment_event):
        """Test other commands in inline comments."""
        review_comment_event["comment"]["body"] = "/review"

        with patch("main.GitHubClient") as MockGitHub:
            mock_github = Mock()
            MockGitHub.return_value = mock_github

            handle_review_comment_event(review_comment_event, mock_config)

            # Should suggest using main PR comment area
            mock_github.reply_to_review_comment.assert_called_once()
            reply_args = mock_github.reply_to_review_comment.call_args
            assert "main PR comment area" in reply_args[0][3]

    def test_handle_missing_pr_info(self, mock_config, review_comment_event):
        """Test handling when PR info is missing."""
        del review_comment_event["pull_request"]

        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.logger") as mock_logger,
        ):
            handle_review_comment_event(review_comment_event, mock_config)

            # Should log error
            assert mock_logger.error.called
            MockGitHub.assert_not_called()

    def test_handle_github_client_error(self, mock_config, review_comment_event):
        """Test handling GitHub client initialization error."""
        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.logger") as mock_logger,
        ):
            MockGitHub.side_effect = Exception("API error")

            handle_review_comment_event(review_comment_event, mock_config)

            # Should log error
            assert mock_logger.error.called

    def test_handle_ask_execution_error(self, mock_config, review_comment_event):
        """Test handling Ask tool execution error."""
        with patch("main.GitHubClient") as MockGitHub, patch("main.Ask") as MockAsk:
            mock_github = Mock()
            MockGitHub.return_value = mock_github

            mock_ask_tool = Mock()
            mock_ask_tool.run.side_effect = Exception("Agent error")
            MockAsk.return_value = mock_ask_tool

            handle_review_comment_event(review_comment_event, mock_config)

            # Should post error message
            mock_github.reply_to_review_comment.assert_called_once()
            reply_args = mock_github.reply_to_review_comment.call_args
            assert "Error" in reply_args[0][3]

    def test_handle_reply_failure_fallback(self, mock_config, review_comment_event):
        """Test fallback to regular comment when reply fails."""
        with patch("main.GitHubClient") as MockGitHub, patch("main.Ask") as MockAsk:
            mock_github = Mock()
            mock_github.reply_to_review_comment.side_effect = Exception("Reply failed")
            MockGitHub.return_value = mock_github

            mock_ask_tool = Mock()
            mock_ask_tool.run.return_value = "Answer"
            MockAsk.return_value = mock_ask_tool

            handle_review_comment_event(review_comment_event, mock_config)

            # Should fallback to regular comment
            mock_github.post_comment.assert_called_once()
            comment_args = mock_github.post_comment.call_args
            assert "Answer" in comment_args[0][2]


class TestHandlePrEvent:
    """Tests for pull_request event handler — draft auto-review gating."""

    @pytest.fixture
    def mock_config(self):
        config = Mock(spec=ActionConfig)
        config.github_token = "test_token"
        config.kimi_api_key = "test_key"
        config.kimi_base_url = "https://api.moonshot.cn/v1"
        return config

    @pytest.fixture
    def draft_pr_event(self):
        """A draft PR just opened (the first sent version)."""
        return {
            "action": "opened",
            "pull_request": {"number": 42, "draft": True},
            "repository": {"full_name": "owner/repo"},
        }

    def test_draft_first_version_is_reviewed(self, mock_config, draft_pr_event):
        """First version of a draft (no prior review) gets reviewed."""
        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.Reviewer") as MockReviewer,
            patch("main.get_input", return_value="true"),
        ):
            mock_github = Mock()
            mock_github.get_last_bot_comment.return_value = None  # never reviewed
            MockGitHub.return_value = mock_github

            mock_reviewer = Mock()
            mock_reviewer.run.return_value = "SUMMARY <!-- kimi-review:sha=abc -->"
            MockReviewer.return_value = mock_reviewer

            handle_pr_event(draft_pr_event, mock_config)

            mock_reviewer.run.assert_called_once_with("owner/repo", 42)
            mock_github.post_comment.assert_called_once()
            assert "SUMMARY" in mock_github.post_comment.call_args[0][2]

    def test_draft_resync_skips_and_posts_dedup_notice(
        self, mock_config, draft_pr_event
    ):
        """Later pushes to a draft skip review and refresh a single skip notice."""
        draft_pr_event["action"] = "synchronize"
        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.Reviewer") as MockReviewer,
            patch("main.get_input", return_value="true"),
        ):
            mock_github = Mock()
            # A prior review already exists on this PR.
            mock_github.get_last_bot_comment.return_value = {
                "sha": "abc123",
                "comment_id": 7,
            }
            MockGitHub.return_value = mock_github

            mock_reviewer = Mock()
            MockReviewer.return_value = mock_reviewer

            handle_pr_event(draft_pr_event, mock_config)

            # No review runs on a draft resync.
            mock_reviewer.run.assert_not_called()
            # Older skip notice(s) are deleted before posting a fresh one.
            mock_github.delete_issue_comments_with_marker.assert_called_once_with(
                "owner/repo", 42, DRAFT_SKIP_MARKER
            )
            mock_github.post_comment.assert_called_once()
            posted = mock_github.post_comment.call_args[0][2]
            assert DRAFT_SKIP_MARKER in posted
            assert "/review" in posted
            assert "Ready for review" in posted
            # The skip notice must NOT masquerade as a review (no sha marker), or it
            # would be mistaken for the original review and break dedup/detection.
            assert "kimi-review:sha=" not in posted

    def test_ready_for_review_runs_review_and_clears_notice(
        self, mock_config, draft_pr_event
    ):
        """Draft -> ready transition reviews and removes the stale skip notice."""
        draft_pr_event["action"] = "ready_for_review"
        draft_pr_event["pull_request"]["draft"] = False
        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.Reviewer") as MockReviewer,
            patch("main.get_input", return_value="true"),
        ):
            mock_github = Mock()
            mock_github.get_last_bot_comment.return_value = {
                "sha": "abc123",
                "comment_id": 7,
            }
            MockGitHub.return_value = mock_github

            mock_reviewer = Mock()
            mock_reviewer.run.return_value = "SUMMARY"
            MockReviewer.return_value = mock_reviewer

            handle_pr_event(draft_pr_event, mock_config)

            mock_reviewer.run.assert_called_once_with("owner/repo", 42)
            mock_github.delete_issue_comments_with_marker.assert_called_once_with(
                "owner/repo", 42, DRAFT_SKIP_MARKER
            )

    def test_auto_review_disabled_does_nothing(self, mock_config, draft_pr_event):
        """With auto_review off, the pull_request path takes no action."""
        with (
            patch("main.GitHubClient") as MockGitHub,
            patch("main.Reviewer") as MockReviewer,
            patch("main.get_input", return_value="false"),
        ):
            mock_github = Mock()
            MockGitHub.return_value = mock_github
            mock_reviewer = Mock()
            MockReviewer.return_value = mock_reviewer

            handle_pr_event(draft_pr_event, mock_config)

            mock_reviewer.run.assert_not_called()
            mock_github.post_comment.assert_not_called()
            mock_github.delete_issue_comments_with_marker.assert_not_called()
