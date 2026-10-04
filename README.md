# AI-workflow

Cross-project development control for agent-assisted work. It answers, from one place: is this
issue ready, which repositories does it touch, where is the work, who holds it, what state are its
PRs and CI in, and what actually needs the owner.

It is configured (`workflow.json`) for [Avrana Party](https://github.com/rcnechamkin/avrana-party)
and [Avrana Party Games](https://github.com/rcnechamkin/avrana-party-games). Python standard
library only. Boundary and state model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

It never merges, pushes, deploys, or writes to Linear or GitHub. Those stay with the owner.

## Layout

The managed checkouts sit beside this repository (or set `AI_WORKFLOW_ROOT`):

```
<projects>/
  AI-workflow/   avrana-party/   avrana-party-games/
```

## Setup (once per machine)

```sh
python aw.py setup            # installs the commit hooks in each managed repository; verifies everything
python aw.py setup --check    # report only
python aw.py setup --warn-only  # same check, but it warns and lets the commit through (for rollout)
python aw.py setup --chain    # keep an existing foreign pre-commit hook; it runs after the claim check
python aw.py setup --uninstall
```

`setup` is idempotent. It never overwrites a hook it did not write: it stops and says so, unless
you pass `--chain`. If `core.hooksPath` is set, it stops and tells you which line to add yourself.
It also reports whether Linear, GitHub and a session identity are available.

## The loop

```sh
python aw.py issue AVR-236            # readiness and context; changes nothing
python aw.py context AVR-236          # what to read first, with provenance; changes nothing
python aw.py start AVR-236            # ready? -> worktree(s) from origin/main, claimed, context printed
#   ... the agent is told only: Implement AVR-236 ...
python aw.py prs AVR-236              # PRs, CI, review, pairing, distance behind main
python aw.py handoff AVR-236 --to any --note "tests red in test_x"     # or --to cody to ask the owner
python aw.py release AVR-236
python aw.py needs-cody               # the owner's queue
python aw.py status                   # every worktree: branch, issue, claim, drift, unfinished work
```

Options go after the command; `--json` returns every answer as data. Exit codes: 0 ok, 2 usage,
3 refused, 4 a required source was unavailable.

### `start`

`start AVR-N` reads Linear, checks readiness, and refuses when the issue is blocked, undecided,
finished, already in PR, or held by another session. Otherwise it fetches `origin/main`, finds the
issue's existing worktree or unmerged branch (or creates `type/avr-N-description` in
`<repo>.wt-avrN`), does the same in the second repository for a paired change, claims them, and
prints paths, the repositories' `AGENTS.md` and the follow-up commands. It creates nothing when it
refuses.

**The agent path is one step.** An agent with a Linear connector fetches the issue (with its
relations) and pipes the result straight in; no file, no extra flags when the issue follows the
template:

```sh
<connector get_issue AVR-236 as JSON> | python aw.py start AVR-236 --linear-snapshot -
```

Several JSON documents back to back are accepted, so dependency issues can be piped along. The
answer is either the claimed worktree(s), or a refusal that names what is missing and how to fix
it (`fix:` lines; `readiness.missing_sections` and `readiness.hints` in `--json`). `--dry-run`
gives the same answer and the plan while fetching, creating and claiming nothing.

Section headings are matched as real issues write them: `Repositories`, `Repository` or `Repos`;
`Tests Required` or `Tests`; any case, optional trailing colon. Repositories may be named in full
(`avrana-party-games`) or by short name (`Games`, `Party`, `both`). Open Decisions is deliberately
strict: only an empty section or `None` means none. "None blocking, but ..." is reported as
unresolved, with the text quoted.

`bin/avr` (and `bin/avr.cmd`) is the same CLI under a shorter name: put `bin` on your `PATH` and
`avr start AVR-236` is `python aw.py start AVR-236`.

When the issue does not follow the template the owner can supply what is missing:
`--repo party|games|both`, and `--decisions-confirmed` (the owner's statement that no product
decision is open; an agent must not pass it on its own).

### Context manifest

`context AVR-N` (and every `start`) lists the smallest set of things to read before working the
issue. It lists references, never file contents: the agent opens what it needs.

| Tier | Selected because | Authority shown |
|---|---|---|
| 1 explicit | the issue names the path, the file's basename, the ADR number, or a name the file defines | the file's own: `canonical`, `adr (accepted)`, `implementation`, `tests`, ... |
| 2 canonical | the repository's `AGENTS.md`; canonical docs and ADRs (per `docs/manifest.json`) that mention what the issue names | `canonical`, `adr (...)` |
| 3 exact | code and tests containing an identifier the issue names; tests named after that code | `implementation`, `tests` |
| 4 Graphify | a Graphify node matching the issue points at the file | `derived (verify in the file)` |
| 5 search | the file contains several words of the title | `inferred` |

Each item carries `ref`, `repo`, `kind`, `tier`, `source_type`, `authority`, `why`, `commit` and
an estimated size. A file appears once, at its best tier, so Graphify and search can add leads but
never re-label or outrank a file the issue or a canonical document already selected. Graphify hits
are checked against the real checkout (a node pointing at a missing file is dropped with a
warning), and the graph is reported stale when files changed since its `built_at_commit`.

Everything is read from git objects at one commit per repository: the issue's worktree `HEAD` when
it has one, else `origin/main`. Nothing in a product repository is touched.

The budget (`--max-items`, default 20; `--max-tokens`, default 60000 estimated) trims from the
lowest tier up and says how much was left out. When the issue names nothing that exists in the
repository, the manifest is `insufficient` (exit 4) and Graphify and search leads are withheld:
it does not guess a scope. Unresolved, ambiguous and too-common references are listed as warnings.

`start` saves the manifest to this tool's state directory (`.state/`, or `AI_WORKFLOW_STATE`),
which is ignored by git and outside the product repositories. It is disposable: `context AVR-N`
rebuilds it.

### Readiness

| State | Derived from |
|---|---|
| Done | Linear `Done`, `Canceled`, `Duplicate` |
| Ready for Playtest | `In Review` + label `Human Validation`, no open PR |
| PR / CI | an open PR names the issue, or Linear `In Review` |
| Needs Cody | unresolved Open Decisions, or `Backlog` |
| Blocked | a `blocked by` dependency that is not finished |
| In Progress | Linear `In Progress`, or a worktree with a claim, commits or uncommitted changes |
| Ready for Agent | `Todo`, Open Decisions says none, repositories known, nothing above applies |
| Unknown / incomplete evidence | anything the answer depends on could not be read |

## The pipeline

One issue, one path, every step a command that reads recorded state. Nothing here asks a model to
decide anything; `next` says which step is due and who takes it.

```
python aw.py next AVR-236        # the single next step, or why a human must decide (exit 3)
```

| stage | who | command | stops when |
| --- | --- | --- | --- |
| assign | agent | `start AVR-236` | open decisions, Backlog, blocked, missing sections, another session holds it |
| context | agent | printed by `start`; `context AVR-236` | the issue names nothing that exists (insufficient) |
| implement | agent | edit and commit in the claimed worktree | - |
| validate | agent | `validate AVR-236` | a required check fails |
| ready | agent | `ready AVR-236` | not validated at this commit |
| release | orchestrator | `queue release AVR-236` | no room under the WIP limits |
| PR | agent | `pr AVR-236` | not released; branch moved; GitHub unreadable |
| merge | owner | on GitHub | always a human, unless `queue` reports auto-merge eligible and the repository allows it |
| reconcile | orchestrator | `reconcile` | Linear or GitHub unreadable |

`validate` runs the checks listed under `validate` in `workflow.json` (copied from each
repository's AGENTS.md) in the worktree, on committed work only, and records the result with the
commit on the claim. `ready` then needs no typed test report. A check marked `advisory_on` this
platform is run and reported but does not fail validation; the repository's Linux CI remains the
authority for it. A repository with no checks configured is never "validated".

`pr` is the only command that writes to GitHub: it pushes the released branch (a plain push) and
opens the PR, with title and body built from the READY_FOR_PR record. A change classed as ADR,
protocol/contract, deployment or Needs Cody says "Requires Cody before merge" in the body.

`reconcile` compares each active issue's Linear state with its worktrees and PRs and lists what to
correct and who does it (set the issue In Progress / In Review, release a claim whose PR merged,
a closed issue that still has an open PR). It writes nothing: Linear is changed by whoever holds
the Linear connector.

Human approval is never inferred. Product direction, architecture, trust and security boundaries,
destructive changes, deployment and ambiguous requirements stop at `start` (open decisions,
missing sections) or at merge (the classes above are never auto-merge eligible). Graphify appears
only as marked leads in the context manifest, below every canonical source.

## Merge queue and WIP limits

A finished branch is reported, not opened. The agent commits, runs its tests and records
READY_FOR_PR on its own claim; the orchestrator releases it when the limits leave room; only then
does the agent open the PR.

```
python aw.py ready AVR-236 --tests "unit 40/40, offline pass" [--set NAME] [--needs-cody] [--adr] [--contract] [--deployment]
python aw.py queue                      # what is ready, what occupies each slot, merge sets, auto-merge eligibility
python aw.py queue release AVR-236      # the orchestrator: refused, naming the occupant, when there is no room
```

`ready` needs your live claim, a clean worktree and commits ahead of `origin/main`. It records the
issue, branch, commit, your test report and a classification computed from the changed paths:
docs-only, ADR (`docs/adr/`), protocol or contract, deployment. The flags only add to what the
paths show; nothing you declare makes a change more boring. If the branch moves afterwards the
record is stale: run `ready` again.

Limits (override in `workflow.json` under `wip`):

| limit | default |
| --- | --- |
| open implementation PRs per agent, across repositories | 1 |
| open PRs per repository | 2 |
| of which substantive / docs-only | 1 / 1 |

The PRs of one merge set count as one change for the per-agent limit.

A **merge set** is the PRs that must land together: by default the PRs and ready branches of one
issue across repositories, or entries given the same `--set NAME`. It is `mergeable` only when
every member is an open PR that is green, current with main, not a draft and free of conflicts. A
member that merged without the others is reported as `SPLIT`.

**Auto-merge eligibility** is a classification, never an action: docs-only, no ADR decision, no
protocol or contract change, no deployment change, no Needs Cody (which requires a READY_FOR_PR
record, since only its author can declare that), current with main, CI green, and its merge set
ready. Each failing criterion is named. Separately, each repository reports whether GitHub
auto-merge can work there at all (`allow_auto_merge` and required status checks on main), so a gap
in repository settings is visible rather than assumed.

A PR that was opened before its branch had a record gets one after the fact: run `ready` in its
worktree as usual. The record is marked retroactive, names the open PR, and needs no release; it is
what lets that PR's Needs Cody status be known.

`queue_repos` in `workflow.json` lists repositories the queue tracks for PRs, slots and
READY_FOR_PR without making them product repositories: this tool's own repository is one. They are
not part of `start`, readiness, `status` or the commit hooks. Their branches carry no issue id, so
release them with `queue release --path <worktree>`.

The tool never opens or merges a PR, never enables auto-merge and never changes a repository
setting. If GitHub cannot be read, nothing is released and the exit code is 4.

## Worktree claims

A claim is a small local file saying which session is working in a worktree. Git enforces it:
after `setup`, a commit (or merge commit) fails when

- the worktree is claimed by a different session, in any state, or
- the branch is issue work (`type/avr-N-...`) and nobody has claimed the worktree.

The error names the owner and the way out. A stale claim (no activity for 6 hours) can be
re-claimed by anyone if the worktree is clean; with uncommitted changes or a merge or rebase in
progress it needs `claim --force`, which is the owner's call. `git commit --no-verify` remains
the human override.

Identity is an opaque label. Claude Code sessions get `claude-<8 characters>` automatically.
Everything else sets one: `AI_WORKFLOW_SESSION=codex-1a2b3c4d`, or `start --agent codex --session
<id>`. A person committing on an issue branch needs one too (for example
`AI_WORKFLOW_SESSION=human-cody`). No names, secrets or process ids go into claims.

## Linear

Direct, read-only. The token is looked up in this order and never written anywhere:

1. environment variable `LINEAR_API_KEY`
2. the OS secret store, entry `ai-workflow-linear`

```sh
cmdkey /generic:ai-workflow-linear /user:linear /pass:<token>                          # Windows
security add-generic-password -s ai-workflow-linear -a linear -w                       # macOS (prompts)
secret-tool store --label="AI-workflow Linear" service ai-workflow-linear              # Linux (prompts)
```

Minimum scope: a personal API key with **Read** permission only, limited to the team that owns
the issues. The tool sends queries, never mutations.

Without a token, pass `--linear-snapshot file.json` (issues as a Linear connector returns them:
one issue, a list, or `{"issues": [...]}`). With neither, Linear is `unavailable` and no issue is
called ready.

## GitHub and the appliance

GitHub is read through `gh` (`gh auth login`). The deployed build is read from the appliance's
status endpoint (`status_url`), which answers only on its own network. Either being unreachable
is reported as unavailable in every command; it is never read as "no PRs" or "deployed".

## Optional: earlier warning in Claude Code

Git is the enforcement. For a warning before the edit instead of at the commit, add to your
user-level Claude Code settings:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
  "hooks": [{"type": "command", "command": "python <path to>/AI-workflow/aw.py guard", "timeout": 15}]}]}}
```

## Tests

```sh
python -m unittest discover -s tests
```

The tests build temporary checkouts with real git, real hooks and real commits. GitHub, Linear
and the appliance are faked.
