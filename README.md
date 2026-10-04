# avrana-workflow

Cross-repository development control for [Avrana Party](https://github.com/rcnechamkin/avrana-party)
and [Avrana Party Games](https://github.com/rcnechamkin/avrana-party-games): who is working where,
what state an issue and its PRs are in, and what actually needs the owner. Python standard library
only. Design and boundary: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

It expects to sit beside the two checkouts (or set `AVRANA_ROOT`):

```
Projects/
  avrana-party/  avrana-party-games/  avrana-workflow/
```

## Commands

```sh
python aw.py status                                  # every worktree: branch, issue, claim, drift, unfinished work
python aw.py issue AVR-236 [--json]                  # structured context and blockers for one issue
python aw.py worktree AVR-236 --repo party --type feat --desc native-registry   # dedicated worktree from origin/main, claimed
python aw.py claim [AVR-236] [--path P]              # claim this worktree, or every worktree of the issue
python aw.py handoff [AVR-236] --to codex-1a2b --note "tests red in test_x"
python aw.py release [AVR-236]
python aw.py prs [AVR-237]                           # PRs, CI and pairing in both repositories
python aw.py needs-cody                              # the owner's queue, agent work, and what could not be checked
```

Put options after the command. `--json` gives the structured form of every answer. Exit codes:
0 ok, 2 usage, 3 refused (someone else holds the claim), 4 a required source was unavailable.

It writes only local claim files and, for `worktree`, a new branch and worktree. It never pushes,
merges, deploys, or writes to Linear or GitHub.

## Identity

Claims need a session label. Claude Code sessions get `claude-<8 chars>` automatically. Anything
else sets one: `AVRANA_SESSION=codex-1a2b` (or `--owner`). Labels are opaque; do not put names or
secrets in them.

## Linear

With `LINEAR_API_KEY` in the environment the CLI reads Linear directly (never store the key in a
repository). Without it, an agent that has a Linear connector writes what it read to a JSON file
and passes `--linear-snapshot file.json` (one issue, a list, or `{"issues": [...]}` in the
connector's own shape). With neither, Linear is reported unavailable and no issue is called ready.

## Claude Code hook (optional, owner installs)

To have Claude sessions claim on first edit and stop before editing another session's worktree,
add to the user-level `~/.claude/settings.json`:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
  "hooks": [{"type": "command", "command": "python C:/Users/<you>/Projects/avrana-workflow/aw.py guard", "timeout": 15}]}]}}
```

It does nothing outside the configured repositories and never blocks on its own failure.

## Tests

```sh
python -m unittest discover -s tests
```

The tests build temporary Party and Games checkouts with real git; GitHub, Linear and the Pi are
faked or pointed at a closed port.
