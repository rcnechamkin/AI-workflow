"""Read-mostly git access: worktrees, branches and what a worktree is in the middle of."""
import os
from pathlib import Path
import re
import subprocess

PREFIX = 'AVR'          # the issue-key prefix of the project being managed; set from workflow.json


def set_prefix(prefix):
    global PREFIX
    PREFIX = prefix.upper()


def git(cwd, *args, timeout=60):
    """(returncode, stdout). A missing git or a timeout is a failure, never an empty success."""
    try:
        p = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return 127, ''
    return p.returncode, p.stdout.strip()


def issue_of(text):
    """The issue a branch name or PR title carries (`feat/avr-236-x`, `Fix it (AVR-236)`), or None."""
    m = re.search(rf'(?:^|[^a-z0-9]){PREFIX}-(\d+)(?![0-9])', text or '', re.I)
    return f'{PREFIX}-{int(m[1])}' if m else None


def issue_id(text):
    m = re.fullmatch(rf'(?i){PREFIX}-?(\d+)', (text or '').strip())
    return f'{PREFIX}-{int(m[1])}' if m else None


def toplevel(path):
    """The worktree root containing `path` (which may not exist yet), or None outside git."""
    p = Path(path)
    while not p.is_dir():
        if p.parent == p:
            return None
        p = p.parent
    rc, out = git(p, 'rev-parse', '--show-toplevel')
    return out if rc == 0 and out else None


def common_dir(path):
    rc, out = git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return out if rc == 0 and out else None


def git_dir(path):
    rc, out = git(path, 'rev-parse', '--path-format=absolute', '--git-dir')
    return out if rc == 0 and out else None


def worktrees(repo):
    """Every worktree of `repo`: [{'path', 'head', 'branch', 'detached', 'prunable'}], or None."""
    rc, out = git(repo, 'worktree', 'list', '--porcelain')
    if rc:
        return None
    rows, cur = [], {}
    for line in out.splitlines() + ['']:
        if not line:
            if cur:
                rows.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            cur = {'path': value, 'head': None, 'branch': None, 'detached': False, 'prunable': False}
        elif key == 'HEAD':
            cur['head'] = value
        elif key == 'branch':
            cur['branch'] = value.removeprefix('refs/heads/')
        elif key in ('detached', 'prunable'):
            cur[key] = True
    return [r for r in rows if 'path' in r]


def busy(worktree):
    """Why taking this worktree over could destroy work: an operation in progress or uncommitted
    changes. None when it is clean, 'unknown state' when git cannot say."""
    gd = git_dir(worktree)
    if not gd:
        return 'unknown state (git unreadable)'
    for marker, name in (('MERGE_HEAD', 'a merge in progress'), ('rebase-merge', 'a rebase in progress'),
                         ('rebase-apply', 'a rebase in progress'), ('CHERRY_PICK_HEAD', 'a cherry-pick in progress'),
                         ('REVERT_HEAD', 'a revert in progress')):
        if os.path.exists(os.path.join(gd, marker)):
            return name
    rc, out = git(worktree, 'status', '--porcelain', '--untracked-files=no')
    if rc:
        return 'unknown state (git status failed)'
    return f'{len(out.splitlines())} uncommitted change(s)' if out else None


def drift(worktree, base='origin/main'):
    """(ahead, behind) of HEAD against `base`, or None."""
    rc, out = git(worktree, 'rev-list', '--left-right', '--count', f'{base}...HEAD')
    if rc:
        return None
    behind, ahead = out.split()
    return int(ahead), int(behind)


def rev(repo, ref):
    rc, out = git(repo, 'rev-parse', ref)
    return out if rc == 0 else None


def current_branch(worktree):
    rc, out = git(worktree, 'branch', '--show-current')
    return out if rc == 0 and out else None


def fetch_main(repo):
    return git(repo, 'fetch', 'origin', 'main', '--quiet', timeout=180)[0] == 0


def unmerged_branches(repo, issue):
    """Branches carrying `issue` that hold commits origin/main lacks: {'local': [...], 'remote': [...]}, or None."""
    rc, out = git(repo, 'for-each-ref', '--format=%(refname)', '--no-merged', 'origin/main', 'refs/heads', 'refs/remotes/origin')
    if rc:
        return None
    found = {'local': [], 'remote': []}
    for ref in out.splitlines():
        kind, name = ('local', ref.removeprefix('refs/heads/')) if ref.startswith('refs/heads/') else ('remote', ref.removeprefix('refs/remotes/origin/'))
        if issue_of(name) == issue:
            found[kind].append(name)
    return found


def branch_exists(repo, name):
    return git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}')[0] == 0 or         git(repo, 'rev-parse', '--verify', '--quiet', f'refs/remotes/origin/{name}')[0] == 0
