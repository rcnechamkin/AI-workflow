"""Temporary Party + Games checkouts with a real origin, and fakes for the outside world."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_workflow import cli, gitio, model, sources  # noqa: E402

IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
SESSION_VARS = ('AI_WORKFLOW_SESSION', 'CLAUDE_CODE_SESSION_ID', 'AI_WORKFLOW_LINEAR_SNAPSHOT', 'LINEAR_API_KEY')


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
            'issue_prefix': 'AVR',
            'repos': {'party': {'slug': 'o/avrana-party', 'dir': 'avrana-party'},
                      'games': {'slug': 'o/avrana-party-games', 'dir': 'avrana-party-games'}},
            'status_url': 'http://127.0.0.1:9/status', 'claim_ttl_hours': 6}), encoding='utf-8')
        self.cfg = model.load_config(cfg_path, self.root)
        self._env = dict(os.environ)
        os.environ.update(IDENTITY, AI_WORKFLOW_CONFIG=str(cfg_path), AI_WORKFLOW_ROOT=str(self.root))
        for k in SESSION_VARS:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)
        self._sources = cli.SOURCES
        self.addCleanup(setattr, cli, 'SOURCES', self._sources)
        cli.SOURCES = world()

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def repo(self, name):
        return self.root / {'party': 'avrana-party', 'games': 'avrana-party-games'}[name]

    def add_worktree(self, name, branch, dirname):
        path = self.root / dirname
        git(self.repo(name), 'worktree', 'add', '-q', '--no-track', '-b', branch, str(path), 'origin/main')
        return path

    def world(self, **kw):
        cli.SOURCES = world(**kw)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def pr(repo, number, branch, state='OPEN', checks=(('COMPLETED', 'SUCCESS'),), draft=False, merged=None, title=None,
       mergeable='MERGEABLE', review=''):
    return {'number': number, 'title': title or f'{branch} change', 'state': state, 'isDraft': draft, 'headRefName': branch,
            'baseRefName': 'main', 'url': f'https://example.invalid/{repo}/pull/{number}', 'mergedAt': merged,
            'reviewDecision': review, 'mergeable': mergeable, 'statusCheckRollup': [{'status': s, 'conclusion': c} for s, c in checks]}


def issue(ident, state='Todo', description='', labels=(), blocked_by=(), **more):
    return {'id': ident, 'title': f'{ident} title', 'status': state, 'labels': list(labels), 'description': description,
            'relations': {'blockedBy': [{'id': b} for b in blocked_by]}, **more}


def gh(rows_by_slug, fail=(), behind=None):
    """A fake `gh`: `pr list` per repository, and `api .../compare/base...branch` for behind_by."""
    def runner(cmd, timeout=40):
        if cmd[:2] == ['gh', 'api']:
            m = re.match(r'repos/(.+)/compare/(.+)\.\.\.(.+)$', cmd[2])
            count = (behind or {}).get(m[3], 0)
            return (1, '', 'HTTP 404') if count is None or m[1] in fail else (0, f'{count}\n', '')
        slug = cmd[cmd.index('--repo') + 1]
        if slug in fail:
            return 1, '', 'error connecting to api.github.com'
        return 0, json.dumps(rows_by_slug.get(slug, [])), ''
    return runner


def linear_of(*raw):
    return {**sources.ok({i['id']: i for i in map(sources.normalize_issue, raw)}), 'origin': 'test'}


NO_LINEAR = sources.unavailable('no LINEAR_API_KEY in the environment and no ai-workflow-linear entry in the OS secret store')
NO_PI = sources.unavailable('http://127.0.0.1:9/status unreachable (URLError)')


def world(issues=None, party=(), games=(), pi=NO_PI, fail=(), behind=None, fetch=True):
    """The outside world for the CLI: Linear issues (None = unavailable), PR rows, Pi, fetch."""
    runner = gh({'o/avrana-party': list(party), 'o/avrana-party-games': list(games)}, fail=fail, behind=behind)

    class World:
        prs = staticmethod(lambda repo, slug, issue=None: sources.prs(repo, slug, issue, run=runner))
        pi_status = staticmethod(lambda url: pi)
        fetch_main = staticmethod(gitio.fetch_main if fetch else (lambda repo: False))

        @staticmethod
        def linear(issue=None, snapshot=None):
            if snapshot:
                return sources.linear(issue, snapshot=snapshot)
            if issues is None:
                return NO_LINEAR
            return linear_of(*issues)
    return World


READY = ('## Outcome\nx\n\n## Acceptance Criteria\n- y\n\n## Repositories\navrana-party, avrana-party-games\n\n'
         '## Tests Required\nz\n\n## Open Decisions\nNone\n')
PARTY_ONLY = READY.replace('avrana-party, avrana-party-games', '`avrana-party`')
GAMES_ONLY = READY.replace('avrana-party, avrana-party-games', 'avrana-party-games only')
UNDECIDED = READY.replace('## Open Decisions\nNone', '## Open Decisions\nShould the lobby show spectators? (Cody)')
