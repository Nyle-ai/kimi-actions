"""Ticket context — resolve the ClickUp/Linear ticket linked to a PR.

The reviewer otherwise judges a diff in isolation. When a repo opts in via
``.kimi-config.yml`` (``ticket.provider``) and the matching secret is configured, this module
finds the ticket id referenced by the PR, fetches the ticket's intent (title/description/status)
and hands it to the Planner so it can check the code against the requirement.

Everything here is **best-effort**: any extraction/network/parse failure returns ``None`` and the
review proceeds without ticket context. Uses stdlib ``urllib`` only (no new dependency).
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Matches Linear/ClickUp-style identifiers like ENG-123. Case-insensitive because Linear
# auto-generated branch names are lowercase (e.g. "artur/eng-123-fix").
TICKET_ID_RE = re.compile(r"\b([A-Za-z]{2,}-\d+)\b")

_MAX_DESCRIPTION = 1500


@dataclass
class TicketContext:
    """The intent of a linked ticket, as fed to the reviewer."""

    id: str
    title: str = ""
    description: str = ""
    status: str = ""
    url: str = ""


def extract_ticket_id(
    title: str,
    branch: str,
    commit_messages: List[str],
    body: str,
) -> Optional[str]:
    """Find a ticket id, scanning title → branch → commits → body. Returns it upper-cased."""
    for source in (title, branch, "\n".join(commit_messages or []), body):
        if not source:
            continue
        match = TICKET_ID_RE.search(source)
        if match:
            return match.group(1).upper()
    return None


def _http_json(
    url: str, headers: dict, data: Optional[bytes] = None
) -> Optional[dict]:
    """GET (or POST when ``data`` is given) a JSON endpoint. Returns parsed dict or None."""
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        logger.warning(f"Ticket request to {url} failed: {e}")
        return None


class TicketProvider(ABC):
    """Fetches a ticket's context by id."""

    @abstractmethod
    def fetch(self, ticket_id: str) -> Optional[TicketContext]:
        ...


class ClickUpProvider(TicketProvider):
    """ClickUp REST v2. Resolves custom task ids (e.g. DEV-123) via ``custom_task_ids``."""

    def __init__(self, token: str, team_id: str) -> None:
        self.token = token
        self.team_id = team_id

    def fetch(self, ticket_id: str) -> Optional[TicketContext]:
        params = urllib.parse.urlencode(
            {"custom_task_ids": "true", "team_id": self.team_id}
        )
        url = f"https://api.clickup.com/api/v2/task/{ticket_id}?{params}"
        data = _http_json(url, {"Authorization": self.token})
        if not data or "id" not in data:
            return None
        status = (data.get("status") or {}).get("status", "")
        description = data.get("description") or data.get("text_content") or ""
        return TicketContext(
            id=ticket_id,
            title=data.get("name", ""),
            description=description[:_MAX_DESCRIPTION],
            status=status,
            url=data.get("url", ""),
        )


class LinearProvider(TicketProvider):
    """Linear GraphQL. Looks up an issue by team key + number (e.g. ENG-123)."""

    _QUERY = (
        "query($key:String!,$num:Float!){"
        "issues(filter:{team:{key:{eq:$key}},number:{eq:$num}}){"
        "nodes{identifier title description url state{name}}}}"
    )

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, ticket_id: str) -> Optional[TicketContext]:
        try:
            key, num = ticket_id.split("-", 1)
            number = float(int(num))
        except (ValueError, IndexError):
            return None

        payload = json.dumps(
            {"query": self._QUERY, "variables": {"key": key, "num": number}}
        ).encode("utf-8")
        data = _http_json(
            "https://api.linear.app/graphql",
            {"Authorization": self.api_key, "Content-Type": "application/json"},
            data=payload,
        )
        try:
            nodes = data["data"]["issues"]["nodes"]
        except (KeyError, TypeError):
            return None
        if not nodes:
            return None

        node = nodes[0]
        return TicketContext(
            id=node.get("identifier", ticket_id),
            title=node.get("title", ""),
            description=(node.get("description") or "")[:_MAX_DESCRIPTION],
            status=(node.get("state") or {}).get("name", ""),
            url=node.get("url", ""),
        )


def get_provider(repo_config, config) -> Optional[TicketProvider]:
    """Build the configured provider, or None when not opted-in / missing secrets."""
    provider = getattr(repo_config, "ticket_provider", "") if repo_config else ""
    if provider == "clickup" and config.clickup_token and config.clickup_team_id:
        return ClickUpProvider(config.clickup_token, config.clickup_team_id)
    if provider == "linear" and config.linear_api_key:
        return LinearProvider(config.linear_api_key)
    if provider:
        logger.info(f"Ticket provider '{provider}' configured but secrets are missing")
    return None


def resolve_ticket_context(pr, repo_config, config) -> Optional[TicketContext]:
    """Resolve the ticket linked to ``pr``. Best-effort; returns None on any failure."""
    provider = get_provider(repo_config, config)
    if not provider:
        return None

    try:
        commit_messages = [c.commit.message for c in pr.get_commits()]
    except Exception as e:  # noqa: BLE001 - best effort
        logger.warning(f"Could not read PR commits for ticket extraction: {e}")
        commit_messages = []

    ticket_id = extract_ticket_id(
        pr.title or "",
        getattr(pr.head, "ref", "") or "",
        commit_messages,
        pr.body or "",
    )
    if not ticket_id:
        return None

    try:
        ticket = provider.fetch(ticket_id)
    except Exception as e:  # noqa: BLE001 - best effort
        logger.warning(f"Ticket fetch failed for {ticket_id}: {e}")
        return None

    if ticket:
        logger.info(f"Resolved ticket context for {ticket_id}")
    return ticket
