"""Tests for ticket context resolution (ClickUp / Linear)."""

import io
import json
import os
import sys
from unittest.mock import Mock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ticket_context as tc  # noqa: E402


def _fake_response(payload):
    """A context-manager object mimicking urllib's urlopen return."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class TestExtractTicketId:
    def test_title_takes_precedence(self):
        assert (
            tc.extract_ticket_id("Fix ENG-12 login", "branch", ["GH-9"], "ENG-99")
            == "ENG-12"
        )

    def test_falls_through_to_branch_then_commits_then_body(self):
        assert tc.extract_ticket_id("", "artur/eng-123-fix", [], "") == "ENG-123"
        assert tc.extract_ticket_id("", "", ["chore: PD-7"], "") == "PD-7"
        assert tc.extract_ticket_id("", "", [], "closes ABC-1") == "ABC-1"

    def test_uppercases_result(self):
        assert tc.extract_ticket_id("dev-5 done", "", [], "") == "DEV-5"

    def test_returns_none_when_absent(self):
        assert tc.extract_ticket_id("no ticket here", "main", ["wip"], "body") is None


class TestClickUpProvider:
    def test_fetch_maps_fields_and_url(self):
        provider = tc.ClickUpProvider("pk_token", "team42")
        payload = {
            "id": "abc",
            "name": "Add login",
            "description": "Users can log in",
            "status": {"status": "in progress"},
            "url": "https://app.clickup.com/t/abc",
        }
        with patch(
            "ticket_context.urllib.request.urlopen",
            return_value=_fake_response(payload),
        ) as urlopen:
            ctx = provider.fetch("DEV-1")

        assert ctx.title == "Add login"
        assert ctx.status == "in progress"
        assert ctx.description == "Users can log in"
        # Request used custom task ids + team id.
        called_url = urlopen.call_args[0][0].full_url
        assert "custom_task_ids=true" in called_url
        assert "team_id=team42" in called_url
        assert "/task/DEV-1" in called_url

    def test_fetch_returns_none_on_empty(self):
        provider = tc.ClickUpProvider("pk_token", "team42")
        with patch(
            "ticket_context.urllib.request.urlopen", return_value=_fake_response({})
        ):
            assert provider.fetch("DEV-1") is None


class TestLinearProvider:
    def test_fetch_parses_first_node(self):
        provider = tc.LinearProvider("lin_key")
        payload = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "identifier": "ENG-123",
                            "title": "Build it",
                            "description": "Spec text",
                            "url": "https://linear.app/x/ENG-123",
                            "state": {"name": "In Progress"},
                        }
                    ]
                }
            }
        }
        with patch(
            "ticket_context.urllib.request.urlopen",
            return_value=_fake_response(payload),
        ):
            ctx = provider.fetch("ENG-123")

        assert ctx.id == "ENG-123"
        assert ctx.title == "Build it"
        assert ctx.status == "In Progress"

    def test_fetch_returns_none_when_no_nodes(self):
        provider = tc.LinearProvider("lin_key")
        payload = {"data": {"issues": {"nodes": []}}}
        with patch(
            "ticket_context.urllib.request.urlopen",
            return_value=_fake_response(payload),
        ):
            assert provider.fetch("ENG-123") is None


class TestGetProvider:
    def _config(self, **kw):
        cfg = Mock()
        cfg.clickup_token = kw.get("clickup_token", "")
        cfg.clickup_team_id = kw.get("clickup_team_id", "")
        cfg.linear_api_key = kw.get("linear_api_key", "")
        return cfg

    def test_clickup_selected_when_configured(self):
        rc = Mock(ticket_provider="clickup")
        cfg = self._config(clickup_token="t", clickup_team_id="42")
        assert isinstance(tc.get_provider(rc, cfg), tc.ClickUpProvider)

    def test_linear_selected_when_configured(self):
        rc = Mock(ticket_provider="linear")
        cfg = self._config(linear_api_key="k")
        assert isinstance(tc.get_provider(rc, cfg), tc.LinearProvider)

    def test_none_when_secrets_missing(self):
        rc = Mock(ticket_provider="clickup")
        assert tc.get_provider(rc, self._config()) is None

    def test_none_when_disabled(self):
        rc = Mock(ticket_provider="")
        cfg = self._config(linear_api_key="k")
        assert tc.get_provider(rc, cfg) is None


class TestResolveTicketContext:
    def _pr(self):
        pr = Mock()
        pr.title = "Implement ENG-5"
        pr.body = ""
        pr.head = Mock(ref="eng-5-impl")
        pr.get_commits = Mock(return_value=[])
        return pr

    def test_returns_none_when_no_provider(self):
        rc = Mock(ticket_provider="")
        cfg = Mock(clickup_token="", clickup_team_id="", linear_api_key="")
        assert tc.resolve_ticket_context(self._pr(), rc, cfg) is None

    def test_returns_ticket_when_wired(self):
        rc = Mock(ticket_provider="linear")
        cfg = Mock(linear_api_key="k")
        ticket = tc.TicketContext(id="ENG-5", title="Do it")
        with patch.object(tc.LinearProvider, "fetch", return_value=ticket):
            out = tc.resolve_ticket_context(self._pr(), rc, cfg)
        assert out.id == "ENG-5"

    def test_returns_none_on_fetch_error(self):
        rc = Mock(ticket_provider="linear")
        cfg = Mock(linear_api_key="k")
        with patch.object(tc.LinearProvider, "fetch", side_effect=RuntimeError("boom")):
            assert tc.resolve_ticket_context(self._pr(), rc, cfg) is None
