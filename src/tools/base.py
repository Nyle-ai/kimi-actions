"""Base tool class for Kimi Actions.

Provides common functionality for all tools:
- Skill loading and management
- Agent SDK interaction
- Repository cloning
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from action_config import get_action_config, DEFAULT_MODEL, DEFAULT_BASE_URL
from github_client import GitHubClient
from skill_loader import SkillManager, Skill
from repo_config import load_repo_config, RepoConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Reliability tuning (overridable via env for tests / large repos).
AGENT_TIMEOUT_SECONDS = int(os.environ.get("KIMI_AGENT_TIMEOUT", "900"))
RETRY_BACKOFF = [5, 10, 20, 30]
HEARTBEAT_INTERVAL = 30

# Default per-turn agent step cap (mirrors the historical hardcoded value so the
# default behaviour is unchanged; callers can scale it via run_agent(max_steps=...)).
DEFAULT_MAX_STEPS_PER_TURN = 100

# An immediate failure (the SDK never made a call AND an error was captured — e.g.
# auth/outage) won't heal in a few seconds, so don't burn the whole retry budget on it.
MAX_IMMEDIATE_FAILURE_ATTEMPTS = 2
# A timeout has already spent ~AGENT_TIMEOUT_SECONDS of wall-clock; allow at most this
# many further attempts rather than retrying to the full RETRY_BACKOFF count.
MAX_RETRIES_AFTER_TIMEOUT = 1

# Token usage fields carried by kosong's TokenUsage (cache-aware).
_USAGE_FIELDS = ("input_other", "input_cache_read", "input_cache_creation", "output")


def _zero_usage() -> Dict[str, int]:
    return {f: 0 for f in _USAGE_FIELDS} | {"calls": 0}


def with_retry(
    func: Callable[[], T],
    *,
    label: str = "github",
    backoff=RETRY_BACKOFF,
) -> T:
    """Run a synchronous call with retry/backoff, re-raising the last error if all attempts fail."""
    last_exc: Optional[Exception] = None
    for attempt in range(len(backoff) + 1):
        try:
            return func()
        except Exception as e:  # noqa: BLE001 - we re-raise below
            last_exc = e
            if attempt < len(backoff):
                delay = backoff[attempt]
                logger.warning(f"{label} call failed ({e}); retrying in {delay}s")
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


class BaseTool(ABC):
    """Abstract base class for all tools.

    Subclasses must implement:
    - skill_name: The default skill to use
    - run(): The main execution logic
    """

    def __init__(self, github: GitHubClient):
        self.github = github
        self.config = get_action_config()

        # Skill management
        self.skill_manager = SkillManager()
        self.repo_config: Optional[RepoConfig] = None

        # Per-stage spend instrumentation: run_agent accumulates the most recent
        # call's token usage into _last_usage; run_agent_reliably rolls each stage
        # (incl. retries) up into stage_metrics for the run summary.
        self.stage_metrics: List[Dict[str, Any]] = []
        self._last_usage: Dict[str, int] = _zero_usage()
        # Most recent run_agent failure (repr of the exception or a short reason).
        # Empty string means the last run_agent call did not record an error.
        self._last_error: str = ""

    @property
    @abstractmethod
    def skill_name(self) -> str:
        """Default skill name for this tool."""
        pass

    @abstractmethod
    def run(self, repo_name: str, pr_number: int, **kwargs) -> str:
        """Execute the tool's main logic."""
        pass

    def load_context(self, repo_name: str, ref: str = None) -> None:
        """Load repository config and custom skills."""
        self.repo_config, validation = load_repo_config(self.github, repo_name, ref=ref)

        if not validation.valid:
            logger.error(f"Config validation failed: {validation.errors}")
        if validation.warnings:
            logger.warning(f"Config warnings: {validation.warnings}")

        self.skill_manager.load_from_repo(self.github, repo_name, ref=ref)

    def get_skill(self) -> Optional[Skill]:
        """Get the skill for this tool, respecting overrides."""
        skill_to_use = self.skill_name

        if self.repo_config and self.repo_config.skill_overrides:
            override = self.repo_config.skill_overrides.get(self.skill_name)
            if override:
                logger.info(f"Using skill override: {self.skill_name} -> {override}")
                skill_to_use = override

        skill = self.skill_manager.get_skill(skill_to_use)
        if not skill:
            logger.warning(f"Skill not found: {skill_to_use}")

        return skill

    def format_footer(self, extra_info: str = "") -> str:
        """Generate standard footer for tool output."""
        footer = "---"
        if extra_info:
            footer += f"\n<sub>{extra_info}</sub>"
        return footer

    @property
    def agent_model(self) -> str:
        """Resolve the model name from config (workflow input) with fallback."""
        return getattr(self.config, "model", None) or DEFAULT_MODEL

    def setup_agent_env(self) -> Optional[str]:
        """Setup environment variables for Agent SDK.

        Returns:
            API key if available, None otherwise.
        """
        api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("INPUT_KIMI_API_KEY")
        if not api_key:
            return None

        # Get base URL from config or environment
        base_url = (
            self.config.kimi_base_url
            or os.environ.get("KIMI_BASE_URL")
            or DEFAULT_BASE_URL
        )

        os.environ["KIMI_API_KEY"] = api_key
        os.environ["KIMI_BASE_URL"] = base_url
        os.environ["KIMI_MODEL_NAME"] = self.agent_model
        return api_key

    def clone_repo(
        self, repo_name: str, work_dir: str, branch: str = None, sha: str = None
    ) -> bool:
        """Clone repository with fallback logic.

        Args:
            repo_name: Repository name (owner/repo)
            work_dir: Directory to clone into
            branch: Branch name (optional)
            sha: Commit SHA to check out (used as fallback when branch is deleted/missing)

        Returns:
            True if clone succeeded, False otherwise
        """
        token = os.environ.get("INPUT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            clone_url = f"https://x-access-token:{token}@github.com/{repo_name}.git"
        else:
            clone_url = f"https://github.com/{repo_name}.git"

        try:
            if branch:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "-b", branch, clone_url, work_dir],
                    check=True,
                    capture_output=True,
                )
                logger.info(f"Successfully cloned {repo_name} (branch: {branch})")
                return True

            subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, work_dir],
                check=True,
                capture_output=True,
            )
            logger.info(f"Successfully cloned {repo_name}")
            return True
        except subprocess.CalledProcessError as e:
            if branch and sha:
                # Branch was deleted (e.g. merged PR) — fetch by SHA from default branch
                logger.warning(
                    f"Failed to clone branch {branch} (deleted?); falling back to SHA {sha[:12]}"
                )
                try:
                    subprocess.run(
                        ["git", "clone", "--no-tags", clone_url, work_dir],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", work_dir, "fetch", "--depth", "1", "origin", sha],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", work_dir, "checkout", sha],
                        check=True,
                        capture_output=True,
                    )
                    logger.info(f"Successfully checked out {repo_name} @ {sha[:12]}")
                    return True
                except subprocess.CalledProcessError:
                    logger.error(f"Failed to clone {repo_name} by SHA: {e}")
                    return False
            elif branch:
                # No SHA — fall back to default branch as before
                logger.warning(
                    f"Failed to clone branch {branch}, trying default branch"
                )
                try:
                    subprocess.run(
                        ["git", "clone", "--depth", "1", clone_url, work_dir],
                        check=True,
                        capture_output=True,
                    )
                    logger.info(f"Successfully cloned {repo_name} (default branch)")
                    return True
                except subprocess.CalledProcessError:
                    logger.error(f"Failed to clone {repo_name}: {e}")
                    return False

            logger.error(f"Failed to clone {repo_name}: {e}")
            return False

    def get_skills_dir(self) -> Optional[Path]:
        """Get skills directory from current skill.

        Returns:
            Path to skills directory if skill has scripts, None otherwise
        """
        skill = self.get_skill()
        if skill and skill.skill_dir:
            return Path(skill.skill_dir)
        return None

    async def run_agent(
        self,
        work_dir: str,
        prompt: str,
        skills_dir: Optional[str] = None,
        max_steps: int = DEFAULT_MAX_STEPS_PER_TURN,
    ) -> str:
        """Run agent with standard configuration.

        Args:
            work_dir: Working directory for agent
            prompt: Prompt to send to agent
            skills_dir: Optional path to skills directory. If None, auto-detects from current skill.
            max_steps: Per-turn step cap passed to the SDK session (defaults to the
                historical value so behaviour is unchanged; callers can scale it).

        Returns:
            Agent response text
        """
        self._last_usage = _zero_usage()
        self._last_error = ""
        try:
            from kimi_agent_sdk import Session, ApprovalRequest, TextPart, StatusUpdate
            from kaos.path import KaosPath
        except Exception as e:  # noqa: BLE001 - a missing or import-incompatible SDK must degrade gracefully
            logger.error(f"kimi-agent-sdk unavailable: {e}")
            self._last_error = repr(e)
            return ""

        api_key = self.setup_agent_env()
        if not api_key:
            logger.error("KIMI_API_KEY not found")
            self._last_error = "KIMI_API_KEY not found"
            return ""

        # Auto-detect skills_dir from current skill if not provided
        if skills_dir is None:
            skills_path = self.get_skills_dir()
        else:
            skills_path = Path(skills_dir) if skills_dir else None

        # Convert to KaosPath for Agent SDK
        work_dir_kaos = KaosPath(work_dir) if work_dir else KaosPath.cwd()
        skills_dir_kaos = KaosPath(str(skills_path)) if skills_path else None

        text_parts = []
        try:
            async with await Session.create(
                work_dir=work_dir_kaos,
                model=self.agent_model,
                yolo=True,
                max_steps_per_turn=max_steps,
                skills_dir=skills_dir_kaos,
            ) as session:
                async for msg in session.prompt(prompt):
                    if isinstance(msg, TextPart):
                        text_parts.append(msg.text)
                    elif isinstance(msg, ApprovalRequest):
                        msg.resolve("approve")
                    elif isinstance(msg, StatusUpdate):
                        self._accumulate_usage(getattr(msg, "token_usage", None))

            response = "".join(text_parts)
            logger.info(
                f"Agent completed successfully, response length: {len(response)}"
            )
            if skills_path:
                logger.info(f"Agent used skills from: {skills_path}")
            return response
        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            self._last_error = repr(e)
            return ""

    def _accumulate_usage(self, token_usage: Any) -> None:
        """Add one step's TokenUsage (from a StatusUpdate wire message) to _last_usage."""
        if token_usage is None:
            return
        for field in _USAGE_FIELDS:
            self._last_usage[field] += int(getattr(token_usage, field, 0) or 0)
        self._last_usage["calls"] += 1

    def _record_stage(
        self, label: str, start: float, usage: Dict[str, int], attempts: int
    ) -> None:
        """Append a per-stage spend row for the run summary."""
        row: Dict[str, Any] = {
            "stage": label,
            "seconds": round(time.monotonic() - start, 1),
            "attempts": attempts,
        }
        row.update(usage)
        # Surface the last failure so run-metadata stages carry it (previously the
        # underlying error was swallowed and never reached the trajectory record).
        if self._last_error:
            row["error"] = self._last_error
        self.stage_metrics.append(row)

    async def _heartbeat(self, label: str) -> None:
        """Emit a periodic log line so long agent runs don't look hung."""
        elapsed = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            elapsed += HEARTBEAT_INTERVAL
            logger.info(f"{label} still running ({elapsed}s elapsed)")

    async def run_agent_reliably(
        self,
        work_dir: str,
        prompt: str,
        skills_dir: Optional[str] = None,
        label: str = "agent",
        max_steps: int = DEFAULT_MAX_STEPS_PER_TURN,
    ) -> str:
        """Run an agent stage with a wall-clock timeout, heartbeat and retry/backoff.

        Retries when the stage times out or returns no output. Returns "" if every attempt
        fails so the caller can decide how to degrade.

        Two failure modes short-circuit the full retry budget:
        - Immediate failure (no SDK calls + a captured error, e.g. auth/outage): capped at
          ``MAX_IMMEDIATE_FAILURE_ATTEMPTS`` since such errors won't heal in seconds.
        - Timeout: each timeout already burned ~``AGENT_TIMEOUT_SECONDS`` of wall-clock, so
          only ``MAX_RETRIES_AFTER_TIMEOUT`` further attempts are allowed.

        Args:
            max_steps: Per-turn step cap forwarded to ``run_agent`` (default unchanged).
        """
        stage_start = time.monotonic()
        stage_usage = _zero_usage()
        attempts_used = 0
        # Remaining attempts permitted once a timeout has occurred (None until the first).
        post_timeout_budget: Optional[int] = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            attempts_used += 1
            timed_out = False
            hb = asyncio.create_task(self._heartbeat(label))
            try:
                result = await asyncio.wait_for(
                    self.run_agent(work_dir, prompt, skills_dir, max_steps),
                    timeout=AGENT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(f"{label} timed out after {AGENT_TIMEOUT_SECONDS}s")
                result = ""
                timed_out = True
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb

            # Count this attempt's tokens — retries are real spend.
            for field in stage_usage:
                stage_usage[field] += self._last_usage.get(field, 0)

            if result.strip():
                self._record_stage(label, stage_start, stage_usage, attempts_used)
                return result

            # F3: an immediate failure (the SDK never made a call and an error was
            # captured) won't recover in a few seconds — stop after a couple of tries.
            immediate_failure = (
                self._last_usage.get("calls", 0) == 0 and bool(self._last_error)
            )
            if immediate_failure and attempts_used >= MAX_IMMEDIATE_FAILURE_ATTEMPTS:
                logger.error(
                    f"{label} failed immediately ({self._last_error}); "
                    f"aborting after {attempts_used} attempt(s)"
                )
                break

            # #3: a timeout has already spent the full wall-clock budget; allow only a
            # limited number of further attempts instead of retrying to the full count.
            if timed_out and post_timeout_budget is None:
                post_timeout_budget = MAX_RETRIES_AFTER_TIMEOUT
            if post_timeout_budget is not None:
                if post_timeout_budget <= 0:
                    logger.error(
                        f"{label} timed out repeatedly; "
                        f"aborting after {attempts_used} attempt(s)"
                    )
                    break
                post_timeout_budget -= 1

            if attempt < len(RETRY_BACKOFF):
                delay = RETRY_BACKOFF[attempt]
                logger.warning(f"{label} produced no output; retrying in {delay}s")
                await asyncio.sleep(delay)

        self._record_stage(label, stage_start, stage_usage, attempts_used)
        logger.error(f"{label} failed after {attempts_used} attempt(s)")
        return ""
