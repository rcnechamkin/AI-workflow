# AI-workflow: agent entry point

Cross-project development tooling. Product rules live in each product repository's `AGENTS.md`;
this file covers only this repository. Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

- Derive, do not store. Linear owns desired work, GitHub owns PRs and CI, git owns worktrees, the
  appliance's status endpoint owns what is deployed. The only state this repository persists is
  the local worktree claim.
- A source that cannot be read is reported as unavailable. Never return an empty success, a
  "ready" or a "passed" for it.
- Never add: merging, pushing, deploying, Linear or GitHub writes, a daemon, a database, agent
  launching.
- Standard library only. Every behavior has a test in `tests/`; run
  `python -m unittest discover -s tests` before finishing.
- No credentials, tokens, machine paths or personal data in the repository, in output, or in
  claim files.
- Changes land by pull request; the owner merges.
