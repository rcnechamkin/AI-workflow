# avrana-workflow: agent entry point

Cross-repository development tooling for Avrana. Product rules live in each product repository's
`AGENTS.md`; this file covers only this repository. Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

- Derive, do not store. Linear owns desired work, GitHub owns PRs and CI, git owns worktrees, the
  Pi's `/party/api/status` owns what is deployed. The only state this repository persists is the
  local worktree claim.
- A source that cannot be read is reported as unavailable. Never return an empty success for it.
- Never add: merging, pushing, deploying, Linear or GitHub writes, a daemon, a database.
- Standard library only. Every behavior has a test in `tests/`; run
  `python -m unittest discover -s tests` before finishing.
- No credentials, machine paths or personal data in the repository or in claim files.
