# Kimi Code Review Action

🌗 AI-powered code review using [Kimi](https://kimi.moonshot.cn/) (Moonshot AI)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                              GitHub                                 │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  PR Events  │  PR Comments  │  Inline Comments               │   │
│  │             │  /review      │  /ask                          │   │
│  │             │  /ask         │                                │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────┬───────────────────────────────┘
                                      │
                                      ▼
┌────────────────────────────────────────────────────────────────────┐
│                    GitHub Actions (Docker)                         │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                      main.py                                  │ │
│  │  Event Router: PR events → /review, /ask commands             │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│                              ▼                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                    Tools Layer                                │ │
│  │                                                               │ │
│  │  ┌──────────┐              ┌──────────┐                       │ │
│  │  │ Reviewer │              │   Ask    │                       │ │
│  │  │ /review  │              │  /ask    │                       │ │
│  │  └────┬─────┘              └────┬─────┘                       │ │
│  │       └──────────┬──────────────┘                             │ │
│  │                  ▼                                            │ │
│  │         ┌────────────────┐                                    │ │
│  │         │    BaseTool    │                                    │ │
│  │         │  • clone_repo  │                                    │ │
│  │         │  • run_agent   │                                    │ │
│  │         │  • get_skill   │                                    │ │
│  │         └────────────────┘                                    │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│                              ▼                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                   SkillManager                                │ │
│  │  Load SKILL.md and set skills_dir for Agent SDK               │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│         ┌────────────────────┴────────────────────┐                │
│         ▼                                         ▼                │
│  ┌──────────────────┐                   ┌──────────────────┐       │
│  │  Kimi Agent SDK  │                   │   GitHub API     │       │
│  │ (kimi-k2.7-code) │                   │     (REST)       │       │
│  │                  │                   │                  │       │
│  │ • Auto token mgmt│                   │ • Get PR diff    │       │
│  │ • Script exec    │                   │ • Post comments  │       │
│  │ • Context mgmt   │                   │ • Get PR info    │       │
│  │ • Markdown output│                   │                  │       │
│  └──────────────────┘                   └──────────────────┘       │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### `/review` pipeline

`Reviewer.run()` filters and sanitizes the diff, clones the repo, then runs three Agent SDK
sessions that hand off via JSON files on disk:

```
filter + sanitize diff → clone
  → Planner   → review-plan.json           (candidate issues)
  → Executor  → review-draft.json          (verified + suggestion fixes)
  → QA        → qa-validated-review.json    (false-positives/noise removed)
  → POSTER (pure Python): inline comments + verdict summary + auto-resolve fixed threads
```

Each stage has a timeout, heartbeat and retry/backoff; an empty Planner result short-circuits to
an approval.

## Features

- 🔍 `/review` - **Three-agent review** (Planner → Executor → QA) for high signal, low noise
- 💬 `/ask` - Interactive Q&A about the PR or specific code
- 💡 **Inline suggestions** - Findings posted on the exact line with one-click `suggestion` fixes
- ♻️ **Auto-resolve** - Threads for fixed issues resolve themselves on the next review
- 🧹 **Diff filtering** - Lockfiles, minified bundles and generated assets are dropped (migrations kept)
- 🛡️ **Prompt-injection defense** - Untrusted PR content is sanitized and fenced before it reaches the model
- 🧠 **Agent Skills** - Modular capability extension with custom review rules
- ⚙️ Configurable review strictness and category toggles

## Quick Start

### 1. Get Kimi API Key

1. Visit [Moonshot AI Platform](https://platform.moonshot.cn/)
2. Register/Login
3. Go to "API Key Management"
4. Click "Create API Key"
5. Copy the generated API Key

### 2. Configure GitHub Secrets

1. Go to your GitHub repository
2. Click `Settings` → `Secrets and variables` → `Actions`
3. Click `New repository secret`
4. Add `KIMI_API_KEY` with the API Key from step 1
5. (Optional) Add `KIMI_BASE_URL` if using a custom API endpoint (defaults to `https://api.moonshot.ai/v1`)

### 3. Create Workflow File

```yaml
# .github/workflows/kimi-review.yml
name: Kimi Code Review

on:
  pull_request:
    # `ready_for_review` is NOT in the default set — without it, marking a draft PR
    # "Ready for review" does not trigger a run. Keep it so draft→ready auto-reviews.
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write        # add the 👀 ack reaction to /review and /ask comments (see note below)

jobs:
  kimi-review:
    runs-on: ubuntu-latest
    if: |
      github.event_name == 'pull_request' ||
      (github.event_name == 'issue_comment' &&
       github.event.issue.pull_request &&
       startsWith(github.event.comment.body, '/')) ||
      (github.event_name == 'pull_request_review_comment' &&
       startsWith(github.event.comment.body, '/'))
    steps:
      - name: Get PR ref (for comments)
        id: get-pr
        if: github.event_name == 'issue_comment' || github.event_name == 'pull_request_review_comment'
        uses: actions/github-script@v9
        with:
          script: |
            const prNumber = context.issue?.number || context.payload.pull_request?.number;
            const pr = await github.rest.pulls.get({
              owner: context.repo.owner,
              repo: context.repo.repo,
              pull_number: prNumber
            });
            core.setOutput('ref', pr.data.head.ref);
            core.setOutput('sha', pr.data.head.sha);

      - uses: actions/checkout@v6
        with:
          ref: ${{ (github.event_name == 'issue_comment' || github.event_name == 'pull_request_review_comment') && steps.get-pr.outputs.ref || github.head_ref }}

      - uses: xiaoju111a/kimi-actions@main
        with:
          kimi_api_key: ${{ secrets.KIMI_API_KEY }}
          kimi_base_url: ${{ secrets.KIMI_BASE_URL }}  # Optional
          github_token: ${{ secrets.GITHUB_TOKEN }}
          auto_review: 'false'  # Use /review command instead
```

> **Why `issues: write`?** The action adds a 👀 reaction to the triggering `/review` or `/ask`
> comment to acknowledge it. On a pull request, conversation comments are *issue comments* in
> GitHub's REST API, so adding a reaction to them requires `issues: write` — `pull-requests: write`
> alone is not enough. Without it the review still runs, but the reaction call fails with
> `403: Resource not accessible by integration`.

### Auto-review & draft PRs

The example above is **command-driven** (`auto_review: 'false'`) — reviews run only when you
comment `/review`. To review automatically on every push, set `auto_review: 'true'`.

With auto-review on, **draft PRs are handled specially to avoid burning tokens on
work-in-progress**:

- The **first version** of a draft is reviewed.
- Further pushes while it stays a draft are **skipped** — the bot leaves a single status
  comment explaining the pause (refreshed, not duplicated, on each push).
- The pause does not block you: comment **`/review`** to review the current changes even on a
  draft.
- Marking the PR **Ready for review** automatically reviews it again — but only if your `on:`
  block lists `ready_for_review` in `pull_request.types` (it is **not** in GitHub's default
  set). The example above already includes it.

## Commands

### PR Commands

Use these commands in PR comments:

| Command | Description | Usage Location |
|---------|-------------|----------------|
| `/review` | Comprehensive code review of all PR changes | PR comment area |
| `/ask <question>` | Q&A about the PR or specific code | PR comment area **or** Files changed tab (inline) |
| `/help` | Show help message | PR comment area |

**💡 Using `/ask` for code-specific questions:**
- **In PR comment area**: Ask general questions about the entire PR
- **In Files changed tab**: Click the **+** button next to a line of code, then use `/ask <question>` to ask about that specific code

**🔄 Avoiding Duplicate Reviews:**
- The bot tracks the last reviewed commit SHA
- If you run `/review` again without new commits, it will show "✅ No new changes since last review"
- This prevents wasting tokens on unchanged code

## Observability (spend & trajectory)

Every `/review` records **per-stage spend** (Planner / Executor / QA) and emits it three ways:

- A **Step Summary** table on the Actions run page — tokens in/out, cache hits, calls, wall-time.
- A **`run-metadata.json`** trajectory record + the per-stage handoff JSONs, written to
  `.kimi-review/` in the workspace. Add an upload step to keep them for later analysis:

  ```yaml
        - name: Upload Kimi review trajectory
          if: always()
          uses: actions/upload-artifact@v7
          with:
            name: kimi-review-${{ github.event.pull_request.number || github.event.issue.number }}
            path: .kimi-review/
            if-no-files-found: ignore
            retention-days: 30
  ```

- A one-line operator log: `review spend — N tokens (in X / out Y) across K calls, Ts`.

On the flat-rate Kimi **subscription** endpoint there is no per-call charge, so **tokens are the
quota proxy** — a single review pass can be a meaningful slice of the rolling quota window. Optional
env knobs:

| Env var | Effect |
|---------|--------|
| `KIMI_QUOTA_TOKENS_PER_WINDOW` | Tokens that exhaust your quota window → shows `~N% of quota` per run |
| `KIMI_PRICE_TABLE_JSON` | e.g. `{"kimi-k2.7-code": {"input": 0.15, "output": 0.6, "cache_read": 0.015}}` → adds a reference "shadow $" (metered-API rate, **not** an invoice) |
| `KIMI_LOG_HTTPX=1` | Re-enable the per-request `httpx` log lines (silenced by default) |

## Configuration

### Action Inputs

```yaml
- uses: xiaoju111a/kimi-actions@main
  with:
    # Required
    kimi_api_key: ${{ secrets.KIMI_API_KEY }}
    github_token: ${{ secrets.GITHUB_TOKEN }}
    
    # Optional
    kimi_base_url: ${{ secrets.KIMI_BASE_URL }}  # API endpoint (default: https://api.moonshot.ai/v1)
    language: 'en-US'               # Response language: zh-CN, en-US
    model: 'kimi-k2.7-code'         # Kimi model (default: kimi-k2.7-code)
    review_level: 'normal'          # Review strictness: strict, normal, gentle
    max_files: '50'                 # Max files to review
    exclude_patterns: '*.lock,*.min.js'  # Extra file patterns to exclude from the diff
    auto_review: 'false'            # Auto review on PR open (default: false, use /review command instead)
    enable_inline_comments: 'true'  # Post findings as inline review comments with suggestions
    enable_auto_resolve: 'true'     # Auto-resolve fixed threads on re-review
```

### API endpoint & model

The action defaults to the **compliant pay-go** endpoint `https://api.moonshot.ai/v1` with model
`kimi-k2.7-code` (~$0.05/review). To run on the **Kimi Code subscription** instead, set per-repo
secrets and pass them through:

```yaml
    kimi_base_url: 'https://api.kimi.com/coding/v1'
    model: 'kimi-for-coding'
```

> ⚠️ The Kimi Code subscription is for personal interactive use; non-interactive/CI use may violate its
> Terms of Service. The pay-go default above avoids this — switch only if you accept the risk.

### Repository Config (.kimi-config.yml)

Create `.kimi-config.yml` in your repo root to customize behavior:

```yaml
# Category toggles
categories:
  bug: true
  performance: true
  security: true

# Replace built-in skills with custom ones
skill_overrides:
  code-review: my-company-review

# Ignore files
ignore_files:
  - "*.test.ts"
  - "**/__mocks__/**"

# Extra instructions
extra_instructions: |
  Focus on security issues.

# Ticket context (optional) — see "Ticket context" below
ticket:
  provider: linear   # or "clickup"
```

### Ticket context (ClickUp / Linear)

When enabled, the reviewer resolves the ticket referenced by the PR (scanning the title, branch,
commits, then body for an id like `ENG-123`) and feeds the ticket's **intent** to the Planner so it
can flag code-vs-requirement gaps. It is **opt-in per repo** and **best-effort** — a failed lookup
never blocks a review.

1. Set `ticket.provider` to `clickup` or `linear` in `.kimi-config.yml`.
2. Add the matching secret(s) to your workflow:

```yaml
- uses: xiaoju111a/kimi-actions@main
  with:
    kimi_api_key: ${{ secrets.KIMI_API_KEY }}
    github_token: ${{ secrets.GITHUB_TOKEN }}
    # ClickUp (resolves custom task ids; team id is required)
    clickup_token: ${{ secrets.CLICKUP_TOKEN }}
    clickup_team_id: ${{ secrets.CLICKUP_TEAM_ID }}
    # Linear (looks up issues by team key + number)
    linear_api_key: ${{ secrets.LINEAR_API_KEY }}
```

### Custom Skills (Claude Skills Standard)

Create `.kimi/skills/` directory in your repo, each skill is a folder:

```
.kimi/skills/
├── react-review/
│   ├── SKILL.md           # Required: core instructions
│   ├── scripts/           # Optional: executable scripts
│   │   └── check_hooks.py
│   └── references/        # Optional: reference documents
│       └── hooks-rules.md
└── company-rules/
    └── SKILL.md
```

SKILL.md format:

```markdown
---
name: react-review
description: React code review expert
triggers:
  - react
  - jsx
  - hooks
---

# React Review Focus

## Hooks Rules
- Hooks can only be called at the top level of function components
- Cannot call Hooks inside conditionals

## Performance
- Check if useMemo/useCallback is needed
```

Skills are automatically triggered based on PR code content.

## Models

| Model | Endpoint | Notes |
|-------|----------|-------|
| `kimi-k2.7-code` | `https://api.moonshot.ai/v1` | **Default** (pay-go), thinking always-on |
| `kimi-for-coding` | `https://api.kimi.com/coding/v1` | Kimi Code subscription (server-maps to K2.7 Code; see ToS note above) |

All commands use the **Kimi Agent SDK** with `kimi-k2.7-code` by default.

The Agent SDK automatically handles large PRs with its large context window.

## Review Categories

| Category | Description | Examples |
|----------|-------------|----------|
| **Bug** | Code defects | Unhandled exceptions, null pointers, logic errors |
| **Security** | Security vulnerabilities | SQL injection, XSS, auth flaws |
| **Performance** | Performance issues | O(n²) algorithms, N+1 queries |

## Project Structure

```
kimi-actions/
├── action.yml                  # GitHub Action definition
├── Dockerfile                  # Docker container config
├── requirements.txt            # Python dependencies
├── tests/                      # Unit tests (115 tests)
└── src/
    ├── main.py                 # Entry point, event routing
    ├── action_config.py        # Action config (env vars)
    ├── repo_config.py          # Repo config (.kimi-config.yml)
    ├── github_client.py        # GitHub API client
    ├── skill_loader.py         # Skill loading/management
    ├── tools/                  # Command implementations (Agent SDK)
    │   ├── base.py             # Base class (common functionality)
    │   ├── reviewer.py         # /review - Code review
    │   └── ask.py              # /ask - Q&A
    └── skills/                 # Built-in Skills
        ├── code-review/
        │   ├── SKILL.md        # Review instructions
        │   └── references/     # Reference documents
        └── ask/
            └── SKILL.md
```

### Key Components

| Component | Purpose | Notes |
|-----------|---------|-------|
| **skill_loader.py** | Manage skills | Load SKILL.md, set skills_dir for Agent SDK |
| **base.py** | Common tool functionality | Repo cloning, Agent SDK execution |
| **Agent SDK** | LLM execution | Automatic token management, script execution, context handling, direct Markdown output |

## FAQ

### Q: How to get Kimi API Key?

Visit [Moonshot AI Platform](https://platform.moonshot.cn/), register and create an API Key in the management page. New users get free credits.

### Q: Does it support private repositories?

Yes. Just ensure `GITHUB_TOKEN` has permission to read repository contents.

### Q: What if PR is too large?

The **Kimi Agent SDK** automatically handles large PRs:
- **256K token context window**: Can handle very large PRs
- **Automatic context management**: SDK intelligently manages what to include
- **Smart file filtering**: Excludes binary files, lock files, minified files

No manual chunking needed - the Agent SDK handles everything automatically.

### Q: What is Agent SDK and why use it?

**Kimi Agent SDK** is an intelligent agent framework that:
- **Automatic token management**: No need to manually count tokens or manage context
- **Dynamic script execution**: Automatically calls skill scripts when needed
- **Built-in tools**: Provides file operations (read/write) and bash execution
- **Context optimization**: Intelligently manages conversation context

This allows the action to focus on **what to review** (skills, rules) rather than **how to execute** (token counting, script running).

### Q: How do skills work with Agent SDK?

Skills define **what the agent should do**:
1. **SKILL.md** contains instructions for the agent
2. **scripts/** contains executable tools (Python scripts)
3. Agent SDK automatically calls scripts when needed based on instructions

Example flow:
```
1. Load skill: code-review
2. Pass skills_dir to Agent SDK
3. Agent reads SKILL.md instructions
4. Agent automatically calls scripts/check_security.py when analyzing code
5. Agent generates review based on script output + instructions
```

### Q: How to customize review rules?

Create `.kimi-config.yml` in your repo root, or add custom Skills in `.kimi/skills/` directory. See Configuration section above.

### Q: How to use a custom API endpoint?

If you're using a proxy or custom Kimi API endpoint, add `KIMI_BASE_URL` to your repository secrets:

1. Go to `Settings` → `Secrets and variables` → `Actions`
2. Click `New repository secret`
3. Add `KIMI_BASE_URL` with your custom endpoint (e.g., `https://your-proxy.example.com/v1`)

Then use it in your workflow:

```yaml
- uses: xiaoju111a/kimi-actions@main
  with:
    kimi_api_key: ${{ secrets.KIMI_API_KEY }}
    kimi_base_url: ${{ secrets.KIMI_BASE_URL }}  # Custom endpoint from secrets
    github_token: ${{ secrets.GITHUB_TOKEN }}
```

**Note:** If `KIMI_BASE_URL` is not set, it defaults to `https://api.moonshot.ai/v1`.

This is useful for:
- Using a corporate proxy
- Testing with a local development server
- Using alternative API gateways
- Keeping endpoint URLs private

## Roadmap

- **PR overview** — a one-shot summary comment with a mermaid diagram on PR open.

## Acknowledgments

- [Moonshot AI](https://www.moonshot.cn/) - Kimi LLM
- [Kimi Agent SDK](https://github.com/MoonshotAI/kimi-agent-sdk) - Agent framework
- [pr-agent](https://github.com/qodo-ai/pr-agent) - Architecture reference
- [kimi-cli](https://github.com/MoonshotAI/kimi-cli) - Kimi CLI tool
- [kodus-ai](https://github.com/kodustech/kodus-ai) - AI code review reference

## License

MIT
