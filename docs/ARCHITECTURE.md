# Architecture: boundary and state model

## Principle

Product repositories expose safe, machine-readable primitives. This repository consumes them. A
product must not turn into a session-management application, and this layer must not become a
second source of truth.

## Ownership boundary

| Repository | Owns |
|---|---|
| **AI-workflow** | Cross-project development orchestration: Linear issue context, GitHub PR/CI observation, agent and worktree ownership, task readiness, cross-repository coordination, the owner's queue, workspace creation, handoff and release, and (later) dispatch |
| **avrana-party** | Party product and platform code, its tests and CI, the Party ↔ Games contract, deployment, `/party/api/status`, repo-local governance (`AGENTS.md`, documentation checks, the historical-edit gate), repo-local Graphify and Claude gate tooling |
| **avrana-party-games** | The same for the game providers |

Rules of the boundary:

- Nothing that only one repository needs lives here.
- Working product tooling is not moved for neatness. When this layer needs something a product
  tool already computes, it calls or reads that tool's output.
- The product repositories know one thing about this layer: issue work is obtained and claimed
  through it before editing (one paragraph in each `AGENTS.md`).

### Existing product tooling

| Tool | Where it stays | Relationship |
|---|---|---|
| `tools/contract_check.py`, `repo-check.*`, `ops/deploy.sh`, `/party/api/status`, smoke checks, cross-repo CI | product | consumed: the status endpoint is read; CI results arrive through GitHub |
| `tools/claude_gate.py`, `.claude/settings.json` | product | independent: encodes that repository's safety rules |
| Graphify tooling | product | independent: derived navigation, never orchestration state |
| `tools/reconcile.py` (weekly drift report) | product | wrap later: its JSON report is a candidate input for `needs-cody` |
| `tools/avr_context.py` | product, until its AGENTS reference is replaced | superseded by `aw.py issue` |

## State model

Derived on every call, never stored:

| Question | Source |
|---|---|
| Desired work, state, labels, project, milestone, parent, dependencies, Open Decisions | Linear (read-only API, or a snapshot file) |
| PRs, CI, review, pairing, conflicts, distance behind main | GitHub through `gh` |
| Worktrees, branches, drift, unfinished work | git |
| What is deployed | the appliance's status endpoint |

A source that cannot be read is `unavailable` in every command and every JSON document. It is
never read as "nothing to do", "no PRs", "passed" or "deployed". Readiness that depends on an
unread source is `unknown`.

There is no task database and no second state machine: the readiness state is a function of the
three sources at the moment of asking (see the table in the README).

### The only persisted state: worktree claims (`ai-workflow.claim/v1`)

One JSON file per worktree at `<repo>/.git/ai-workflow/claims/<worktree>-<hash>.json`, in the
repository's git common directory. It is therefore shared by every worktree of that repository,
never committed, and safe to delete.

```json
{
  "schema": "ai-workflow.claim/v1",
  "worktree": "<normalised path of the worktree>",
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

| State | Meaning | Who may claim |
|---|---|---|
| free | no file | anyone |
| held | heartbeat younger than the TTL | the owner only |
| handoff | the owner offered it (`handoff --to X`, `any`, or `cody`) | the named session, or anyone; the owner may take it back |
| stale | heartbeat older than the TTL | anyone if the worktree is clean; with uncommitted changes or a merge/rebase/cherry-pick in progress only with `--force` (an owner decision) |
| corrupt | unreadable file | nobody without `--force` |

Staleness is time since the last heartbeat (a claim, a commit by the owner, or an edit seen by
the optional Claude guard), never a process id. Mutations take a short lock file, so
simultaneous claims have exactly one winner, across threads and across processes.

### Enforcement

1. **Git hooks (the backstop, agent-neutral).** `setup` installs `pre-commit` and
   `pre-merge-commit` in each managed repository's shared hooks directory. They run
   `aw.py hook`, which refuses the commit when the worktree is claimed by another session, or is
   unclaimed issue work. Only a claim decision blocks; a failure of the tool itself warns and
   lets the commit through. `--no-verify` is the human override.
2. **`start` and `claim`.** `start` will not create or hand out a worktree another session
   holds.
3. **Claude guard (optional, earlier).** A PreToolUse hook that claims on first edit and turns an
   edit in another session's worktree into a question. Nothing depends on it.

Known limits: hooks are local to a clone, so a fresh clone needs `setup`; an agent that commits
with `--no-verify` or edits without committing is not stopped by git (the guard and `status`
surface the latter).

### The owner's queue

`needs-cody` has three parts.

- **Needs Cody now:** unresolved Open Decisions; PRs whose CI passed (one item per paired
  change, with the merge order); real-device validation; main ahead of the deployed build; a
  degraded appliance; Linear and GitHub disagreeing about an issue; an agent that stopped and
  asked (`handoff --to cody`); abandoned work (a stale claim over uncommitted changes).
- **Agent work in progress:** live claims, CI running or failed, drafts, conflicts, issues that
  do not follow the template.
- **Could not check:** every source that was unavailable.

## Deliberately not built

Automatic agent launching or dispatch, autonomous merges or deployments, a dashboard, a daemon, a
database, an appliance MCP server, analytics, Linear or GitHub writes.
