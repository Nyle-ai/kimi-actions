"""Main entry point for AI Code Review Action."""

import json
import logging
import os
import re
import sys

from action_config import ActionConfig
from github_client import GitHubClient
from tools import Reviewer, Ask

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
# Silence per-request "HTTP Request: POST .../chat/completions 200 OK" noise from the
# Kimi client — one line per LLM call drowns the useful logs. Per-stage token/latency
# spend is captured structurally instead (see run_metrics). Override with KIMI_LOG_HTTPX=1.
if os.environ.get("KIMI_LOG_HTTPX", "") != "1":
    logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Hidden marker on the draft-skip status notice so we can find and replace it (keeping a
# single, fresh notice). Distinct from the review summary's ``kimi-review:sha=...`` marker,
# so deleting skip notices never removes the original review.
DRAFT_SKIP_MARKER = "<!-- kimi-review:draft-skip -->"
IN_PROGRESS_MARKER = "<!-- kimi-review:in-progress -->"


def _draft_skip_notice() -> str:
    """Status comment posted when auto-review is paused on a draft PR."""
    return (
        "🌗 **Draft PR — auto-review paused.**\n\n"
        "I reviewed the first version of this draft and won't auto-review further pushes "
        "while it stays in draft (this saves tokens on work-in-progress).\n\n"
        "- 💬 Comment `/review` to review the current changes now — this works even on a "
        "draft.\n"
        "- ✅ Mark the PR **Ready for review** and I'll automatically review it again.\n\n"
        f"{DRAFT_SKIP_MARKER}"
    )


def get_input(name: str, default: str = None) -> str:
    """Get action input from environment."""
    env_name = f"INPUT_{name.upper().replace('-', '_')}"
    return os.environ.get(env_name, default)


def parse_command(comment_body: str) -> tuple:
    """Parse command from comment body.

    Supports commands at the start of the body or after quoted content (> lines).

    Returns:
        Tuple of (command, args) or (None, None) if no command found.
    """
    # First try: command at the very start
    pattern = r"^/(\w+)(?:\s+(.*))?$"
    match = re.match(pattern, comment_body.strip(), re.DOTALL)
    if match:
        command = match.group(1).lower()
        args = match.group(2).strip() if match.group(2) else ""
        return command, args

    # Second try: command after quoted lines (for inline comment replies)
    # Remove all lines starting with > (quotes)
    lines = comment_body.strip().split("\n")
    non_quote_lines = [line for line in lines if not line.strip().startswith(">")]
    cleaned_body = "\n".join(non_quote_lines).strip()

    if cleaned_body:
        match = re.match(pattern, cleaned_body, re.DOTALL)
        if match:
            command = match.group(1).lower()
            args = match.group(2).strip() if match.group(2) else ""
            return command, args

    return None, None


def handle_pr_event(event: dict, config: ActionConfig):
    """Handle pull_request event (auto review on PR open/sync)."""
    pr_number = event.get("pull_request", {}).get("number")
    repo_name = event.get("repository", {}).get("full_name")
    action = event.get("action")

    if not pr_number or not repo_name:
        logger.error("Invalid pull request event")
        return

    logger.info(f"PR #{pr_number} in {repo_name} - action: {action}")

    # Initialize clients
    try:
        github = GitHubClient(config.github_token)
    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        return

    # Auto actions on PR open/sync
    auto_review = get_input("auto_review", "true").lower() == "true"
    is_draft = bool(event.get("pull_request", {}).get("draft", False))

    try:
        if auto_review:
            # On a draft PR we review only the first version (when no review exists yet),
            # then pause until it's marked Ready for review or someone runs `/review`.
            # (Skip the lookup entirely for non-draft PRs — the common path.)
            if is_draft and github.get_last_bot_comment(repo_name, pr_number):
                logger.info("Draft PR already reviewed once; pausing auto-review.")
                # Keep a single, fresh skip notice: drop the stale one, post a new one.
                github.delete_issue_comments_with_marker(
                    repo_name, pr_number, DRAFT_SKIP_MARKER
                )
                github.post_comment(repo_name, pr_number, _draft_skip_notice())
            else:
                logger.info("Running auto review...")
                reviewer = Reviewer(github)
                result = reviewer.run(repo_name, pr_number)
                if result:
                    github.post_comment(repo_name, pr_number, result)
                # A real review ran (first draft version, resync, or draft->ready):
                # clear any stale skip notice so it doesn't linger.
                github.delete_issue_comments_with_marker(
                    repo_name, pr_number, DRAFT_SKIP_MARKER
                )

        logger.info("Done!")
    except Exception as e:
        logger.error(f"Error processing PR: {e}")
        try:
            github.post_comment(
                repo_name, pr_number, f"❌ Error processing PR: {str(e)}"
            )
        except Exception:
            pass


def handle_review_comment_event(event: dict, config: ActionConfig):
    """Handle pull_request_review_comment event (inline comment command trigger)."""
    action = event.get("action")
    if action != "created":
        return

    comment = event.get("comment", {})
    comment_body = comment.get("body", "")

    # Parse command
    command, args = parse_command(comment_body)
    if not command:
        return

    pr = event.get("pull_request", {})
    pr_number = pr.get("number")
    repo_name = event.get("repository", {}).get("full_name")

    if not pr_number or not repo_name:
        logger.error(f"Missing PR info: pr_number={pr_number}, repo_name={repo_name}")
        return

    # Get context from inline comment
    file_path = comment.get("path", "")
    comment_line = comment.get("line") or comment.get("original_line", 0)
    diff_hunk = comment.get("diff_hunk", "")

    logger.info(f"Inline command: /{command} {args}")
    logger.info(f"PR #{pr_number} in {repo_name}, file: {file_path}:{comment_line}")

    # Initialize clients
    try:
        github = GitHubClient(config.github_token)
    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        return

    # Handle command with context
    result = None
    try:
        if command == "ask":
            if not args:
                result = "❌ Please provide a question"
            else:
                ask = Ask(github)
                # Extract only the last few lines of diff_hunk (the relevant code)
                hunk_lines = diff_hunk.strip().split("\n")
                # Take last 5 lines or less, skip the @@ header
                relevant_lines = [
                    line for line in hunk_lines if not line.startswith("@@")
                ][-5:]
                code_context = "\n".join(relevant_lines)

                # Pass code context to Kimi but don't show in output (GitHub UI already shows it)
                context_question = f"Regarding `{file_path}` line {comment_line}:\n```diff\n{code_context}\n```\n\n{args}"
                result = ask.run(
                    repo_name, pr_number, question=context_question, inline=True
                )
        else:
            # For other commands, just run normally
            result = (
                f"ℹ️ Command `/{command}` is better used in the main PR comment area."
            )

    except Exception as e:
        logger.error(f"Error handling inline command /{command}: {e}")
        result = f"❌ Error: {str(e)}"

    # Reply to the original inline comment
    if result:
        try:
            comment_id = comment.get("id")
            github.reply_to_review_comment(repo_name, pr_number, comment_id, result)
        except Exception as e:
            logger.error(f"Failed to reply to comment: {e}")
            # Fallback to regular comment
            github.post_comment(
                repo_name, pr_number, f"> /{command} {args}\n\n{result}"
            )


def handle_comment_event(event: dict, config: ActionConfig):
    """Handle issue_comment event (command trigger)."""
    action = event.get("action")
    if action not in ["created", "edited"]:
        return

    comment = event.get("comment", {})
    comment_body = comment.get("body", "")

    # Check if this is a PR comment
    issue = event.get("issue", {})
    if "pull_request" not in issue:
        logger.info("Not a PR comment, skipping")
        return

    # Parse command
    command, args = parse_command(comment_body)
    if not command:
        return

    pr_number = issue.get("number")
    repo_name = event.get("repository", {}).get("full_name")

    logger.info(f"Command: /{command} {args}")
    logger.info(f"PR #{pr_number} in {repo_name}")

    # Initialize clients
    try:
        github = GitHubClient(config.github_token)
    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        return

    # Add reaction to show we're processing
    github.add_reaction(repo_name, pr_number, comment.get("id"), "eyes")

    # Handle commands
    result = None

    try:
        if command == "review":
            github.post_comment(
                repo_name,
                pr_number,
                f"> /review\n\n🔍 **Review in progress** — Planner → Executor → QA running. I'll post results here when done.\n\n{IN_PROGRESS_MARKER}",
            )
            reviewer = Reviewer(github)
            result = reviewer.run(
                repo_name, pr_number, command_quote="/review"
            )
            github.delete_issue_comments_with_marker(
                repo_name, pr_number, IN_PROGRESS_MARKER
            )
            if result:
                github.post_comment(repo_name, pr_number, result)
                result = None  # Prevent double posting below

        elif command == "ask":
            if not args:
                result = "❌ Please provide a question, e.g.: `/ask What does this function do?`"
            else:
                # Check if this is a reply to a review comment (inline comment thread)
                comment_id = comment.get("id")
                review_context = None

                try:
                    review_context = github.get_review_comment_context(
                        repo_name, pr_number, comment_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to get review comment context: {e}")
                    review_context = None

                if review_context:
                    # This is a reply in a review comment thread - we have code context!
                    file_path = review_context.get("path", "")
                    line = review_context.get("line", 0)
                    diff_hunk = review_context.get("diff_hunk", "")

                    # Extract relevant code lines from diff_hunk
                    hunk_lines = diff_hunk.strip().split("\n")
                    relevant_lines = [
                        line for line in hunk_lines if not line.startswith("@@")
                    ][-5:]
                    code_context = "\n".join(relevant_lines)

                    # Add code context to the question
                    context_question = f"Regarding `{file_path}` line {line}:\n```diff\n{code_context}\n```\n\n{args}"

                    ask = Ask(github)
                    result = ask.run(
                        repo_name, pr_number, question=context_question, inline=True
                    )
                else:
                    # Regular PR comment without code context
                    ask = Ask(github)
                    result = ask.run(repo_name, pr_number, question=args, inline=False)

                    # Add a note if this seems to be in a conversation thread
                    # (heuristic: if the comment body contains quoted text)
                    if ">" in comment_body:
                        result += "\n\n💡 **Tip**: For code-specific questions, use `/ask` directly in the **Files changed** tab by clicking the **+** button next to the line of code."

        elif command == "help":
            result = get_help_message()

        else:
            result = f"❌ Unknown command: `/{command}`\n\nUse `/help` to see available commands."

    except Exception as e:
        logger.error(f"Error handling command /{command}: {e}")
        result = f"❌ Error executing command: {str(e)}"

    # Post result with command quote
    if result:
        try:
            # Quote the original command
            original_command = f"/{command}"
            if args:
                original_command += f" {args}"
            quoted_result = f"> {original_command}\n\n{result}"
            github.post_comment(repo_name, pr_number, quoted_result)
        except Exception as e:
            logger.error(f"Failed to post result: {e}")

    logger.info("Done!")


def get_help_message() -> str:
    """Get help message with available commands."""
    return """## 🌗 AI Code Review Help

### Available Commands

| Command | Description |
|---------|-------------|
| `/review` | Three-agent code review (Planner → Executor → QA) with inline suggestions |
| `/ask <question>` | Q&A about the PR or specific code |
| `/help` | Show this help message |

### Examples

```bash
# Review the PR
/review

# Ask a general question
/ask What does this PR do?

# Ask about specific code (in Files changed tab)
# Click the + button next to a line, then:
/ask Why is this approach used here?
```

### How `/review` works

The reviewer runs three focused passes and posts findings as **inline review comments**
(with one-click `suggestion` fixes) plus a verdict summary:
- **Planner** drafts candidate issues from the filtered diff.
- **Executor** verifies each against the code and writes the fix.
- **QA** drops false positives and noise, keeping only high-confidence findings.

Re-running `/review` on a fixed commit auto-resolves threads that no longer apply, and
skips work entirely when nothing changed since the last review.

---
<sub>Powered by [Kimi](https://kimi.com/) with Agent SDK</sub>
"""


def main():
    # Configure logging level from env
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

    logger.info("Kimi Actions starting...")

    # Load config
    config = ActionConfig.from_env()

    # Validate required inputs
    if not config.kimi_api_key:
        logger.error("KIMI_API_KEY is required")
        sys.exit(1)
    if not config.github_token:
        logger.error("GITHUB_TOKEN is required")
        sys.exit(1)

    # Load GitHub event
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        logger.error("GITHUB_EVENT_PATH not set")
        sys.exit(1)

    with open(event_path, "r") as f:
        event = json.load(f)

    event_name = os.environ.get("GITHUB_EVENT_NAME")
    logger.info(f"Event: {event_name}")

    # Route to appropriate handler
    if event_name in ["pull_request", "pull_request_target"]:
        handle_pr_event(event, config)
    elif event_name == "issue_comment":
        # issue_comment fires for both PR and Issue comments
        handle_comment_event(event, config)
    elif event_name == "pull_request_review_comment":
        handle_review_comment_event(event, config)
    else:
        logger.warning(f"Unsupported event: {event_name}")
        sys.exit(0)


if __name__ == "__main__":
    main()
