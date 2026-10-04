# Avrana development control: boundary, state model and first slice

Status: proposal with a working first slice, 2026-10-03. Local repository only; not yet on GitHub.

## Principle

Avrana repositories expose safe, machine-readable primitives. This layer consumes them. The
product does not become a session-management application, and this layer never becomes a second
source of truth.

## Ownership boundary

| Repository | Owns |
|---|---|
| `avrana-party` | Platform code and its repo-local engineering rules: AGENTS, tests, CI, the Party ↔ Games contract, deploy tooling, `/party/api/status`, smoke checks, repo hooks |
| `avrana-party-games` | Game-provider code and its repo-local rules |
| `avrana-workflow` (this) | Development automation that spans repositories: worktree ownership, cross-repo status, issue context, PR/CI observation, the owner's queue |

A separate repository is warranted because this code has no home in either product repository:
it reads both, it must work when either is on any branch, and its release cadence and audience
(agents and the owner's workstation) differ from the appliance's. It is not warranted for
anything that only one repository needs.

## What stays where (audit of the AVR-230 tooling)

| Tool | Verdict | Reason |
|---|---|---|
| `tools/contract_check.py`, `repo-check.*`, `ops/deploy.sh`, `/party/api/status`, smoke checks, historical-edit gate | stays repo-local | product and repository specific; CI depends on it in place |
| `tools/claude_gate.py` + `.claude/settings.json` | stays repo-local | encodes that repository's safety rules (contract files, generated files, Pi commands) |
| `tools/graphify_context.py`, Graphify workflows | stays repo-local | derived navigation of one repository; never orchestration state |
| `tools/reconcile.py` + weekly workflow | reusable, wrap rather than move | already cross-repo, but it runs in Party's CI with Party's checks; this layer can read its JSON report later |
| `tools/avr_context.py` | superseded by `aw.py issue` once this repo is adopted | same job, but prose-only, Party-rooted, no worktree or claim knowledge; keep until AGENTS points here |

Nothing was moved. Moving `reconcile.py` or `avr_context.py` today would only add import paths.

## State model

Derived on every call, never stored:

| Question | Source |
|---|---|
| Desired work, state, dependencies, Open Decisions | Linear (API key, or a snapshot file an agent wrote from its Linear connector) |
| PRs, CI, review, pairing, conflicts | GitHub through `gh` |
| Worktrees, branches, drift, unfinished work | `git worktree list`, `git status` |
| What is deployed | the Pi's `/party/api/status` |

A source that cannot be read is reported as unavailable in every command. It never counts as
"nothing to do", "no PRs" or "deployed".

The only persisted state is the worktree claim.

### Worktree claims (`avrana.claim/v1`)

One JSON file per worktree at `<repo>/.git/avrana/claims/<worktree>-<hash>.json`, in the
repository's git common directory: shared by every worktree of that repository, never committed,
safe to delete.

```json
{
  "schema": "avrana.claim/v1",
  "worktree": "c:/users/.../avrana-party.wt-avr236",
  "repo": "party",
  "branch": "feat/avr-236-native-registry",
  "issue": "AVR-236",
  "owner": "claude-8cf0d847",
  "agent": "claude",
  "claimed_at": "2026-10-04T00:10:00Z",
  "heartbeat_at": "2026-10-04T00:24:40Z",
  "ttl_hours": 6,
  "note": ""
}
```

`owner` is an opaque session label: `AVRANA_SESSION` when set (Codex, a human), else
`claude-<first 8 of the Claude session id>`. No secrets, no names, no process ids.

| State | Meaning | Who may claim |
|---|---|---|
| free | no file | anyone |
| held | heartbeat younger than the TTL | the owner only |
| handoff | the owner offered it (`handoff --to X` or `any`) | the named session, or anyone |
| stale | heartbeat older than the TTL | anyone, if the worktree is clean; with uncommitted changes or a merge/rebase in progress only with `--force`, which is an owner decision |
| corrupt | unreadable file | nobody without `--force` |

Mutations take a short lock file, so two simultaneous claims have exactly one winner.

### Enforcement layers

1. `aw.py claim` / `worktree` / `release` / `handoff`: agent-neutral, works for Codex, Claude and humans.
2. `aw.py guard`: a Claude Code PreToolUse hook that claims on first edit and turns an edit or a
   state-changing git command in another session's worktree into a question to the person at the
   keyboard. Not installed by this repository; see README.
3. Not built yet: a git `pre-commit` check that refuses a commit in a worktree claimed by another
   session. That is the layer that would have stopped the conflict-marker commit for any agent.

### Lifecycle mapping

Linear stays authoritative; this is Party's `docs/WORKFLOW.md` table as code.

| Loop state | Derived from |
|---|---|
| needs-cody | `Backlog`, or `Todo` with unresolved Open Decisions |
| ready-for-agent | `Todo` with an Open Decisions section that says none |
| todo-unverified | `Todo` whose description has no Open Decisions section (or was not read) |
| in-progress | `In Progress` |
| pr-ci | `In Review` |
| ready-for-playtest | `In Review` + label `Human Validation` |
| done | `Done`, `Canceled`, `Duplicate` |

### The owner's queue

`needs-cody` lists only: unresolved Open Decisions, PRs whose CI passed (review and merge),
real-device validation, main ahead of the deployed build, and stale claims over unfinished work.
CI failures, running CI, drafts, conflicts and issue-template gaps are listed separately as agent
work. Sources it could not check are listed last.

## Deliberately not built

No database, daemon, dashboard, MCP server, autonomous merge or deploy, Linear writes, or dispatch.
