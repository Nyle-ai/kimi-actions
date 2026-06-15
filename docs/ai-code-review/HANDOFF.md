# Handoff — AI PR Code Reviewer upgrade (`Nyle-ai/kimi-actions`)

**Status:** Planned, not yet implemented · **Date:** 2026-06-14
**Repo:** `/Users/artur/Programming/Nyle/kimi-actions` — remote `git@github.com:Nyle-ai/kimi-actions.git`, branch `main`
**Proposed destination for this doc in-repo:** `docs/ai-code-review/HANDOFF.md`

> Self-contained handoff: a teammate, a fresh Claude Code session, or Ultraplan should be able to pick
> this up cold. Sections 1–8 = what to build; 9 = why (research); 10–13 = how to continue + sources.

---

## 1. TL;DR

Upgrade the existing Kimi-SDK Docker GitHub Action from a **single-pass** reviewer into a **3-agent
(Planner → Executor → QA)** PR reviewer with: ClickUp/Linear ticket context, inline `suggestion`
comments, a PR overview + mermaid diagram, auto-resolve of fixed threads, and core hardening (model
bump to **K2.7 Code**, fix dead config, "what-NOT-to-flag" noise discipline, prompt-injection defense,
retries/timeout). It runs on the user's **$40 Kimi Code subscription** — a known **ToS violation**
(subscriptions forbid non-interactive/CI use); the pay-go switch is kept to one line.

## 2. Goal & why

Today's action is functional but behind and noisy. Three inputs shaped this upgrade:
1. **Cloudflare's AI-code-review design discipline** (signal > noise).
2. **The user's manual JIRA/Claude workflow** (3-agent pipeline + ticket context + inline suggestions).
3. **Model/cost/ToS research** (current model, SDK version, endpoints, subscription terms).

## 3. Decisions locked

| Decision | Choice | Rationale |
|---|---|---|
| Pipeline depth | **Full 3-agent** (Planner→Executor→QA) | User chose max quality parity with their manual workflow |
| Add-ons | **ClickUp+Linear context, PR overview+mermaid, auto-resolve** | Selected; **semgrep/security layer excluded** |
| Inline comments + noise discipline | **In, regardless** | Core quality; `create_review_with_comments` already exists unused |
| Credential | **$40 Kimi Code subscription** | User accepts ToS risk; pay-go kept one-line fallback |
| Subscription wiring | base `https://api.kimi.com/coding/v1`, model `kimi-for-coding` | Server-maps to current K2.7 Code |
| Pay-go fallback | base `https://api.moonshot.ai/v1`, model `kimi-k2.7-code` | Compliant + cheap (~$0.05/review) |

## 4. Current state — how the action works today

Docker GitHub Action (Python 3.12), fork of `xiaoju111a/kimi-actions`. Flow:
`main.py` routes events → `pull_request` (auto review), `issue_comment` (`/review`, `/ask`, `/help`),
`pull_request_review_comment` (inline `/ask`) → `Reviewer.run()` / `Ask.run()` →
**one** Kimi Agent SDK `Session` → returns Markdown → posted as **one issue comment** with a SHA marker.

Key file map:
- `src/main.py` — event router + command parsing.
- `src/tools/base.py` — `BaseTool`: SDK session (`run_agent`), repo clone, skill resolution, env setup. **Model default duplicated here (`AGENT_MODEL`/`AGENT_BASE_URL`).**
- `src/tools/reviewer.py` — single-pass review; builds prompt from skill + embeds whole diff.
- `src/tools/ask.py` — `/ask` Q&A.
- `src/github_client.py` — PyGithub wrapper. **Has unused `create_review_with_comments`, `post_review`, `_get_diff_line_map`.** `get_pr_diff` returns the **entire** diff (no filtering).
- `src/action_config.py` — inputs/env (model default `kimi-k2.5`). **Many fields dead.**
- `src/repo_config.py` — `.kimi-config.yml` loader. **`ignore_files`/categories/`extra_instructions` dead.**
- `src/skill_loader.py` — loads `SKILL.md` + references + scripts; passes `skills_dir` to SDK.
- `src/skills/code-review/` — `SKILL.md` + `references/` (common-issues, p3, python, security).
- `action.yml`, `Dockerfile` (installs git + semgrep, unused), `entrypoint.sh`, `requirements.txt` (`kimi-agent-sdk==0.0.2`).
- Tests: 115 in `tests/`; CI = ruff + pytest (`.github/workflows/ci.yml`).

## 5. Gap analysis (must-fix)

- **Dead config:** `exclude_patterns`, `max_files`, `ignore_files`, category toggles, `.kimi-config.yml` `extra_instructions` parsed but **never applied** → unfiltered diffs (lockfiles etc.) hit the model.
- **No "what NOT to flag"** in `SKILL.md` (only buried in `references/common-issues.md`); no severity thresholds → noisy reviews.
- **No prompt-injection defense** — PR title/body/diff/`/ask` embedded raw.
- **No retry/timeout/heartbeat.**
- **Behind:** model `kimi-k2.5`/`k2.6`, SDK `0.0.2`, `.cn` endpoint; model default in 3 files.
- **Docs drift:** README sells "Smart Incremental Review" that was removed (commit `469e144`).

## 6. Target architecture

Reuse the manual workflow's **file-handoff** pattern (agents `Write` JSON to disk; runner reads
between stages). **Do NOT copy its prose-scraping regex** — that fragility only exists because
`claude-code-action` posts prose to the PR. Our in-process Python + SDK return value avoids it.

```
Reviewer.run() (orchestrator)
  ├─ build context: filtered diff + ticket context + cloned repo + guideline files
  ├─ Stage 1 PLANNER  → Session → review-plan.json        (issues; no posting)
  ├─ Stage 2 EXECUTOR → Session → review-draft.json       (verified, formatted + suggestions)
  ├─ Stage 3 QA       → Session → qa-validated-review.json (false-positives/fixed removed)
  └─ POSTER (pure Python) → inline comments + verdict + auto-resolve
PR-overview (PR open): one Session → overview.json → single overview comment + mermaid
```

## 7. Implementation plan (work items)

1. **Model/SDK/config:** bump `kimi-agent-sdk` 0.0.2→0.0.5 (verify `Session.create`); one model source of truth (delete `base.py` constants, read config); defaults to subscription endpoint/model; add inputs/secrets (`CLICKUP_TOKEN`, `CLICKUP_TEAM_ID`, `LINEAR_API_KEY`, `enable_overview`, `enable_auto_resolve`).
2. **3-agent pipeline:** refactor `tools/reviewer.py` into an orchestrator running 3 sessions, gating on the JSON files; role prompts as new skill files (`PLANNER.md`/`EXECUTOR.md`/`QA.md`); Planner reads repo `CLAUDE.md`/`AGENTS.md`/`CODE_REVIEW.md` if present; inject repo `extra_instructions` (currently dropped).
3. **Posting (Python):** wire the unused `create_review_with_comments` + `_get_diff_line_map`; port from manual workflow: changed-lines map, nearest-line anchoring, dedup by `path:line`, non-diff → one "Additional Review Comments"; add `resolve_review_thread` (GraphQL) for QA's `resolvedIssues`; verdict summary table (non-blocking).
4. **Ticket context:** new `src/ticket_context.py` — `TicketProvider` ABC + `ClickUpProvider` (`GET /api/v2/task/{id}`, `?custom_task_ids=true&team_id=`) + `LinearProvider` (GraphQL filter by team key + number); `extract_ticket_id()` scans title→branch→commits→body (`[A-Z]+-\d+`); per-repo via `.kimi-config.yml` (`ticket.provider`). Inject intent into Planner for code-vs-requirement checks.
5. **PR overview:** new `src/overview.py` — one session → `{summary, context, key_changes[], mermaid_diagram}`; post once on `opened` (dup-guard by marker); validate mermaid `sequenceDiagram`.
6. **Diff filtering:** new `src/diff_filter.py` — apply `exclude_patterns`+`ignore_files`+`max_files`; always strip lockfiles/minified/`.map`/generated, **keep DB migrations**; wire category toggles.
7. **SKILL.md:** promote "What NOT to flag" + add severity thresholds + bias-to-approval + low-findings target.
8. **Sanitization:** new `src/sanitize.py` — fence/escape untrusted PR title/body/diff/ticket/`/ask`; strip control markers + stage anchors.
9. **Reliability:** retry/backoff `[5s,10s,20s,30s]` around sessions + GitHub calls; `asyncio.wait_for` wall-clock timeout; ~30s heartbeat; workflow `concurrency` cancel-in-progress.
10. **Docs:** fix README incremental section; update model table; document ticket config, subscription ToS risk + pay-go switch.
11. **Tests:** add ticket extraction/providers (mock HTTP), diff filter, sanitize, poster dedup/anchor, pipeline gating (mock SDK); keep 115 green.

## 8. Files

**Create:** `src/ticket_context.py`, `src/overview.py`, `src/diff_filter.py`, `src/sanitize.py`,
`src/skills/code-review/{PLANNER,EXECUTOR,QA}.md`, matching `tests/`.
**Modify:** `tools/reviewer.py`, `tools/base.py`, `github_client.py`, `action.yml`, `action_config.py`,
`repo_config.py`, `skill_loader.py`, `src/skills/code-review/SKILL.md`,
`.github/workflows/test-action.yml`, `requirements.txt`, `README.md`, `.kimi-config.example.yml`.

## 9. Research findings (session 2026-06-14)

**Harness landscape** (GitHub API freshness check): purpose-built reviewers beat general agents.
| Tool | Type | Last push | Stars | Backing | Note |
|---|---|---|---|---|---|
| alibaba/open-code-review | reviewer CLI | today | 6.9k | Alibaba | best-backed; GLM/Kimi first-class; no native PR comments |
| The-PR-Agent/pr-agent | reviewer+Action | Jun 6 | 11.6k | community (ex-Qodo) | `qodo-ai/pr-agent` 301→here; company went closed |
| kodustech/kodus-ai | reviewer platform | today | 1.2k | Kodus | native comments; self-hosted server |
| anomalyco/opencode | general agent | today | 174k | SST | `sst/opencode` moved here |
| earendil-works/pi | general agent | today | 62k | A. Ronacher | "pi" = this; built-in GLM/Kimi |
→ We build on the **existing Kimi-SDK fork** rather than adopting one.

**Cost & ToS:** Kimi Code ($40) and GLM coding subscriptions **forbid non-interactive/CI use** (Kimi:
"personal interactive use only"; GLM §4.2 bans bot/automation). Pay-go is compliant + cheap: GLM-4.x
~$0.03/review, Kimi K2.7 Code ~$0.05/review. **User accepts the risk and runs on the subscription.**

**Cloudflare principles:** "what NOT to flag" is the #1 lever; specialized reviewers + coordinator
judge pass; severity rubric biased to approval (~1.2 findings/review); risk-tier by diff size; filter
diff first (keep migrations); ops: stdin-not-argv, heartbeat, break-glass, prompt-injection stripping.

**Model facts (verified today):** flagship `kimi-k2.7-code` (released 2026-06-13, thinking always-on;
$0.19 cache-hit/$0.95 miss in, $4.00 out per 1M). SDK current `0.0.5`. Subscription endpoint
`api.kimi.com/coding/v1` model `kimi-for-coding`; pay-go global `api.moonshot.ai/v1` model
`kimi-k2.7-code`. Same model ids on `.ai`/`.cn`.

**Ideas adopted from the manual JIRA/Claude workflow:** Planner→Executor→QA; ticket context
(→ ClickUp+Linear); inline `suggestion` comments + dedup + nearest-line anchoring; verdict; auto-resolve.
Keep the file-handoff; drop the comment-scraping.

## 10. Verification & smoke test

1. `pytest` (coverage) + `ruff check` — green incl. new tests.
2. **Highest-risk unknown:** confirm `kimi-agent-sdk==0.0.5` `Session.create` works against
   `https://api.kimi.com/coding/v1` + `kimi-for-coding` with the subscription key. If it rejects the
   coding endpoint, fall back to pay-go `api.moonshot.ai/v1` + `kimi-k2.7-code` (already wired).
3. Small throwaway PR in a `Nyle-ai` repo via `test-action.yml` (`workflow_dispatch`): verify overview
   posts once; ticket context resolves (ClickUp repo + Linear repo); inline `suggestion` comments anchor
   with no dupes; verdict posts; push a fix → re-review auto-resolves the thread. Start small to bound tokens.

## 11. Risks / open questions

- **ToS + rate limits:** 4+ non-interactive sessions/PR on the subscription is exactly the pattern its
  terms forbid (suspension risk) and can hit the 5-hour/weekly window. Pay-go is the safe switch.
- **SDK 0.0.2→0.0.5** may change `Session.create`; pin-verify.
- **Token cost:** 3 agents + overview ≈ 4× a single pass; rely on diff filtering + risk-tiering.
- **Open:** exact ClickUp ID scheme per repo (custom IDs vs native) and Linear team keys — confirm at impl.

## 12. How to continue

- **Secrets to set on each repo:** `KIMI_API_KEY` (subscription key from kimi.com/code console),
  `KIMI_BASE_URL=https://api.kimi.com/coding/v1`, `CLICKUP_TOKEN`+`CLICKUP_TEAM_ID` (ClickUp repo),
  `LINEAR_API_KEY` (Linear repo). `GITHUB_TOKEN` is automatic.
- **Ultraplan path:** cloud agents need the session rooted in a git repo — launch from
  `/Users/artur/Programming/Nyle/kimi-actions` (the `Maestri` cwd is not a repo, which is why the first
  attempt failed). Then point Ultraplan at this doc.
- **Local path:** approve and implement directly against the repo from here.

## 13. Source links

**Design sources**
- Cloudflare AI code review — https://blog.cloudflare.com/ai-code-review/
- Kodus — https://kodus.io/ · https://github.com/kodustech/kodus-ai
- Alibaba open-code-review — https://alibaba.github.io/open-code-review/ · https://github.com/alibaba/open-code-review
- OCR + Claude tutorial — https://vinayakpandey-7997.medium.com/automated-code-review-with-open-code-review-and-claude-f594e07b77ef

**Harness alternatives evaluated**
- The-PR-Agent (ex-Qodo) — https://github.com/The-PR-Agent/pr-agent
- opencode — https://github.com/anomalyco/opencode
- pi — https://github.com/earendil-works/pi · https://pi.dev

**Kimi model / SDK / endpoints**
- K2.7 Code — https://www.kimi.com/resources/kimi-k2-7-code · pricing https://platform.moonshot.ai/docs/pricing/chat-k27-code
- kimi-agent-sdk — https://github.com/MoonshotAI/kimi-agent-sdk · https://pypi.org/project/kimi-agent-sdk/
- Kimi CLI — https://github.com/MoonshotAI/kimi-cli · agent/Claude-Code support https://platform.moonshot.ai/docs/guide/agent-support
- Kimi Code subscription — https://kimi.com/code (endpoint `https://api.kimi.com/coding/`)
- Pay-go platform — https://platform.moonshot.ai/docs (endpoint `https://api.moonshot.ai/v1`)

**Compliant pay-go alternative (GLM)**
- GLM Coding Plan — https://z.ai/subscribe · Claude-Code setup https://docs.z.ai/devpack/tool/claude (Anthropic base `https://api.z.ai/api/anthropic`, model `glm-4.7`)

**Ticket APIs (for impl)**
- ClickUp API v2 — https://clickup.com/api (`GET https://api.clickup.com/api/v2/task/{task_id}`)
- Linear GraphQL — https://developers.linear.app/docs/graphql/working-with-the-graphql-api (`https://api.linear.app/graphql`)

**Fork origin**
- https://github.com/xiaoju111a/kimi-actions
