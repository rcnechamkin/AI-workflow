"""Temporary Party + Games checkouts with a real origin, for tests that need real git."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avrana_workflow import model  # noqa: E402

IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENTITY}).stdout.strip()


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class RepoCase(unittest.TestCase):
    """self.root holds avrana-party and avrana-party-games, each cloned from a bare origin."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='aw-test-'))
        self.addCleanup(shutil.rmtree, self.root, onerror=_force_remove)
        for name in ('avrana-party', 'avrana-party-games'):
            seed = self.root / f'seed-{name}'
            seed.mkdir()
            git(seed, 'init', '-q', '-b', 'main')
            (seed / 'README.md').write_text(f'{name}\n', encoding='utf-8')
            git(seed, 'add', '.')
            git(seed, 'commit', '-q', '-m', 'init')
            git(self.root, 'clone', '-q', '--bare', str(seed), f'origin-{name}.git')
            git(self.root, 'clone', '-q', str(self.root / f'origin-{name}.git'), name)
        cfg_path = self.root / 'workflow.json'
        cfg_path.write_text(json.dumps({
            'repos': {'party': {'slug': 'o/avrana-party', 'dir': 'avrana-party'},
                      'games': {'slug': 'o/avrana-party-games', 'dir': 'avrana-party-games'}},
            'status_url': 'http://127.0.0.1:9/status', 'claim_ttl_hours': 6}), encoding='utf-8')
        self.cfg = model.load_config(cfg_path, self.root)
        self._env = dict(os.environ)
        os.environ.update(IDENTITY, AVRANA_WORKFLOW_CONFIG=str(cfg_path), AVRANA_ROOT=str(self.root))
        for k in ('AVRANA_SESSION', 'CLAUDE_CODE_SESSION_ID', 'AVRANA_LINEAR_SNAPSHOT', 'LINEAR_API_KEY'):
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def repo(self, name):
        return self.root / {'party': 'avrana-party', 'games': 'avrana-party-games'}[name]

    def add_worktree(self, name, branch, dirname):
        path = self.root / dirname
        git(self.repo(name), 'worktree', 'add', '-q', '-b', branch, str(path), 'origin/main')
        return path


def pr(repo, number, branch, state='OPEN', checks=(('COMPLETED', 'SUCCESS'),), draft=False, merged=None, title=None,
       mergeable='MERGEABLE'):
    return {'number': number, 'title': title or f'{branch} change', 'state': state, 'isDraft': draft, 'headRefName': branch,
            'url': f'https://example.invalid/{repo}/pull/{number}', 'mergedAt': merged, 'reviewDecision': '',
            'mergeable': mergeable, 'statusCheckRollup': [{'status': s, 'conclusion': c} for s, c in checks]}


def issue(ident, state='Todo', description='', labels=(), blocked_by=()):
    return {'id': ident, 'title': f'{ident} title', 'status': state, 'labels': list(labels), 'description': description,
            'relations': {'blockedBy': [{'id': b} for b in blocked_by]}}


READY = ('## Outcome\nx\n\n## Acceptance Criteria\n- y\n\n## Repositories\navrana-party, avrana-party-games\n\n'
         '## Tests Required\nz\n\n## Open Decisions\nNone\n')
UNDECIDED = READY.replace('## Open Decisions\nNone', '## Open Decisions\nShould the lobby show spectators? (Cody)')
