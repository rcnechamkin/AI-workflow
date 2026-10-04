"""Git is the backstop: a commit in a worktree claimed by another session fails, for any agent.

`setup` installs two small hooks (`pre-commit`, and `pre-merge-commit` for merges that need no
conflict resolution) into each configured repository's shared hooks directory. They call
`aw.py hook`, which reads the same claim files as every other command. Nothing here depends on a
vendor's agent hooks.

Rules at commit time, in a repository the hooks are installed in:

- the worktree is claimed by this session: allowed (and the claim's heartbeat is refreshed);
- claimed by anyone else, in any state: refused, naming the owner and what to do;
- unclaimed on an issue branch (`type/avr-N-...`): refused until claimed;
- unclaimed on any other branch: allowed.

`git commit --no-verify` remains the human override. A failure of the tool itself (missing
interpreter, unreadable config) warns and lets the commit through: only a claim decision blocks.
"""
import os
from pathlib import Path
import sys

from . import claims, gitio

MARKER = '# ai-workflow managed hook'
HOOKS = ('pre-commit', 'pre-merge-commit')
CHAINED = '.before-ai-workflow'
REFUSED = 3


def script(aw, python):
    return f'''#!/bin/sh
{MARKER} v1: refuses a commit in a worktree claimed by another session.
# Installed by `aw.py setup`; remove with `aw.py setup --uninstall`. Do not edit: it is rewritten.
AW="{aw}"
PY="{python}"
if [ -f "$AW" ]; then
  "$PY" "$AW" hook "$(basename "$0")"
  rc=$?
  if [ "$rc" -eq {REFUSED} ]; then exit 1; fi
  if [ "$rc" -ne 0 ]; then echo "ai-workflow: claim check could not run (exit $rc); commit not blocked" >&2; fi
else
  echo "ai-workflow: $AW is missing; claim check skipped" >&2
fi
previous="$0{CHAINED}"
if [ -f "$previous" ]; then exec "$previous" "$@"; fi
exit 0
'''


def hooks_dir(repo):
    rc, custom = gitio.git(repo, 'config', '--get', 'core.hooksPath')
    if rc == 0 and custom:
        return None, custom
    rc, out = gitio.git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks')
    return (out, None) if rc == 0 and out else (None, None)


def _ours(path):
    try:
        return MARKER in path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False


def install(repo, aw, python=None, chain=False, check_only=False):
    """Install or verify the hooks in one repository. Returns {'ok', 'actions', 'problems'}.

    An existing hook that is not ours is never overwritten: it is a problem to report, unless
    `chain` is given, in which case it is renamed beside ours and still runs after the claim check.
    """
    report = {'repo': str(repo), 'ok': True, 'actions': [], 'problems': []}

    def problem(text):
        report['ok'] = False
        report['problems'].append(text)

    common = gitio.common_dir(repo)
    if not common:
        problem(f'{repo} is not a git checkout')
        return report
    directory, custom = hooks_dir(repo)
    if custom:
        problem(f'core.hooksPath is set to {custom}: hooks there belong to another tool. Add a line running '
                f'`python {aw} hook pre-commit` to its pre-commit hook yourself, or unset core.hooksPath.')
        return report
    if not directory:
        problem('git would not say where its hooks directory is')
        return report
    claim_dir = Path(common) / 'ai-workflow' / 'claims'
    if not claim_dir.is_dir():
        if check_only:
            problem(f'claims directory {claim_dir} does not exist')
        else:
            claim_dir.mkdir(parents=True, exist_ok=True)
            report['actions'].append(f'created {claim_dir}')
    probe = claim_dir / '.write-test'
    if claim_dir.is_dir():
        try:
            probe.write_text('', encoding='utf-8')
            probe.unlink()
        except OSError:
            problem(f'claims directory {claim_dir} is not writable')
    text = script(Path(aw).as_posix(), Path(python or sys.executable).as_posix())
    for name in HOOKS:
        path = Path(directory) / name
        if path.exists() and not _ours(path):
            if not chain:
                problem(f'{path} already exists and is not ours: left untouched. Re-run with --chain to keep it '
                        f'(it will run after the claim check), or remove it yourself.')
                continue
            if check_only:
                problem(f'{path} is not ours (setup --chain would keep it and add the claim check)')
                continue
            kept = path.with_name(name + CHAINED)
            if kept.exists():
                problem(f'{kept} already exists; not overwriting it. Resolve the two hooks by hand.')
                continue
            path.rename(kept)
            report['actions'].append(f'kept the existing {name} as {kept.name}; it runs after the claim check')
        if path.exists() and path.read_text(encoding='utf-8', errors='replace') == text:
            continue
        if check_only:
            problem(f'{path} is {"out of date" if path.exists() else "not installed"}')
            continue
        Path(directory).mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(text, encoding='utf-8', newline='\n')
        os.chmod(path, 0o755)
        report['actions'].append(f'{"updated" if existed else "installed"} {path}')
    return report


def uninstall(repo):
    """Remove our hooks and restore any hook `--chain` set aside. Claims are left alone."""
    report = {'repo': str(repo), 'ok': True, 'actions': [], 'problems': []}
    directory, custom = hooks_dir(repo)
    if not directory:
        report['ok'] = False
        report['problems'].append(f'core.hooksPath is {custom}: nothing of ours is installed there' if custom
                                  else f'{repo} is not a git checkout')
        return report
    for name in HOOKS:
        path = Path(directory) / name
        if path.exists() and _ours(path):
            path.unlink()
            report['actions'].append(f'removed {path}')
            kept = path.with_name(name + CHAINED)
            if kept.exists():
                kept.rename(path)
                report['actions'].append(f'restored the previous {name}')
        elif path.exists():
            report['actions'].append(f'{path} is not ours: left untouched')
    return report


def check(cwd, owner, now=None):
    """(exit code, message) for a commit about to happen in `cwd`."""
    top = gitio.toplevel(cwd)
    common = gitio.common_dir(top) if top else None
    if not common:
        return 0, ''
    file = claims.claim_file(common, top)
    claim = claims.read(file)
    st = claims.state(claim, now)
    branch = gitio.current_branch(top)
    issue = gitio.issue_of(branch)
    me = owner or 'no session identity (set AI_WORKFLOW_SESSION)'
    if st == 'free':
        if not issue:
            return 0, ''
        return REFUSED, (f'commit refused: {issue} work must be claimed before committing, and this worktree is unclaimed.\n'
                         f'  you are: {me}\n  claim it: python aw.py claim   (from this worktree)')
    if st == 'corrupt':
        return REFUSED, f'commit refused: the claim file {file} is unreadable. Inspect it; `aw.py claim --force` replaces it.'
    if claim['owner'] == owner and st in ('held', 'stale'):
        claims.heartbeat(file, owner, now, min_seconds=0)
        return 0, ''
    lines = [f'commit refused: this worktree is claimed by {claims.describe(claim, now)}.', f'  you are: {me}']
    if claim['owner'] == owner:
        lines.append(f'  you offered it to {claim["handoff_to"]}: run `python aw.py claim` to take it back before committing.')
    elif st == 'handoff' and claim['handoff_to'] in ('any', owner):
        lines.append('  it is offered to you: run `python aw.py claim` to accept the handoff, then commit.')
    elif st == 'stale':
        lines.append('  the claim is stale, but there are changes here that may be its owner\'s unfinished work.\n'
                     '  Taking it over (`python aw.py claim --force`) is an owner decision, not an agent\'s.')
    else:
        lines.append('  Do not commit here. Use your own worktree (`python aw.py start <issue>`), or ask the owner for a handoff.')
    return REFUSED, '\n'.join(lines)
