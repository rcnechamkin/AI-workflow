import os
from pathlib import Path
import subprocess
import sys
import unittest

from support import IDENTITY, ROOT, RepoCase, git, run

from ai_workflow import claims, gitio, hooks

AW = ROOT / 'aw.py'


def commit(worktree, session=None, message='change', args=()):
    """A real `git commit` with the hooks live. (returncode, stderr)."""
    env = {**os.environ, **IDENTITY}
    env.pop('AI_WORKFLOW_SESSION', None)
    if session:
        env['AI_WORKFLOW_SESSION'] = session
    p = subprocess.run(['git', '-C', str(worktree), 'commit', '-q', '-m', message, *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stderr


class CommitEnforcementTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(hooks.install(self.repo('party'), AW, python=sys.executable)['ok'])
        self.wt = self.add_worktree('party', 'feat/avr-900-thing', 'avrana-party.wt-avr900')
        self.file = claims.claim_file(gitio.common_dir(self.wt), self.wt)

    def change(self, wt=None, text='edit\n'):
        wt = wt or self.wt
        with open(wt / 'README.md', 'a', encoding='utf-8') as f:
            f.write(text)
        git(wt, 'add', 'README.md')

    def take(self, owner, **kw):
        return claims.acquire(self.file, self.wt, owner, issue='AVR-900', **kw)

    def log(self, wt=None):
        return git(wt or self.wt, 'log', '--oneline').splitlines()

    def test_issue_work_must_be_claimed_before_committing(self):
        self.change()
        code, err = commit(self.wt, 'codex-bbbb')
        self.assertNotEqual(code, 0)
        self.assertIn('AVR-900 work must be claimed', err)
        self.assertIn('aw.py claim', err)
        self.assertEqual(len(self.log()), 1)

    def test_the_owner_commits_and_the_commit_refreshes_the_claim(self):
        old = claims.utcnow().replace(year=2020)
        self.take('claude-aaaa', now=old)
        self.change()
        code, err = commit(self.wt, 'claude-aaaa')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.log()), 2)
        self.assertEqual(claims.state(claims.read(self.file)), 'held')

    def test_commit_in_another_sessions_worktree_fails_and_names_the_owner(self):
        self.take('claude-aaaa')
        self.change()
        code, err = commit(self.wt, 'codex-bbbb')
        self.assertNotEqual(code, 0)
        self.assertIn('claimed by claude-aaaa', err)
        self.assertIn('you are: codex-bbbb', err)
        self.assertEqual(len(self.log()), 1)
        code, err = commit(self.wt, None)                                    # no identity is not the owner either
        self.assertNotEqual(code, 0)
        self.assertIn('no session identity', err)

    def test_stale_claim_does_not_let_a_stranger_commit_over_it(self):
        self.take('claude-aaaa', now=claims.utcnow().replace(year=2020))
        self.change()
        code, err = commit(self.wt, 'codex-bbbb')
        self.assertNotEqual(code, 0)
        self.assertIn('stale', err)
        self.assertIn('owner decision', err)
        self.assertEqual(claims.read(self.file)['owner'], 'claude-aaaa')

    def test_handoff_must_be_accepted_before_committing(self):
        self.take('claude-aaaa')
        claims.handoff(self.file, 'claude-aaaa', to='codex-bbbb', note='half done')
        self.change()
        code, err = commit(self.wt, 'codex-bbbb')
        self.assertNotEqual(code, 0)
        self.assertIn('accept the handoff', err)
        code, err = commit(self.wt, 'claude-aaaa')
        self.assertNotEqual(code, 0)
        self.assertIn('take it back', err)
        self.assertTrue(self.take('codex-bbbb').ok)
        self.assertEqual(commit(self.wt, 'codex-bbbb')[0], 0)

    def test_release_frees_the_worktree_for_the_next_session(self):
        self.take('claude-aaaa')
        claims.release(self.file, 'claude-aaaa')
        self.change()
        self.assertNotEqual(commit(self.wt, 'codex-bbbb')[0], 0)              # unclaimed issue work
        self.take('codex-bbbb')
        self.assertEqual(commit(self.wt, 'codex-bbbb')[0], 0)

    def test_unreadable_claim_blocks(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text('garbage', encoding='utf-8')
        self.change()
        code, err = commit(self.wt, 'claude-aaaa')
        self.assertNotEqual(code, 0)
        self.assertIn('unreadable', err)

    def test_branches_without_an_issue_commit_freely_unless_someone_claimed_them(self):
        main = self.repo('party')
        self.change(main)
        self.assertEqual(commit(main, None)[0], 0)
        claims.acquire(claims.claim_file(gitio.common_dir(main), main), main, 'claude-aaaa')
        self.change(main)
        code, err = commit(main, 'codex-bbbb')
        self.assertNotEqual(code, 0)
        self.assertIn('claimed by claude-aaaa', err)

    def test_merge_commits_are_checked_too(self):
        other = self.add_worktree('party', 'docs/side', 'avrana-party.wt-side')
        (other / 'side.md').write_text('side\n', encoding='utf-8')
        git(other, 'add', 'side.md')
        self.assertEqual(commit(other, None)[0], 0)
        self.take('claude-aaaa')
        self.change()
        self.assertEqual(commit(self.wt, 'claude-aaaa')[0], 0)
        env = {**os.environ, **IDENTITY, 'AI_WORKFLOW_SESSION': 'codex-bbbb'}
        p = subprocess.run(['git', '-C', str(self.wt), 'merge', '--no-ff', '-m', 'merge side', 'docs/side'], capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('claimed by claude-aaaa', p.stderr)
        self.assertFalse((self.wt / 'side.md').exists() and 'merge side' in '\n'.join(self.log()))

    def test_no_verify_is_the_human_override(self):
        self.take('claude-aaaa')
        self.change()
        self.assertEqual(commit(self.wt, 'codex-bbbb', args=['--no-verify'])[0], 0)

    def test_a_broken_tool_warns_but_does_not_block(self):
        hook = Path(gitio.common_dir(self.wt)) / 'hooks' / 'pre-commit'
        hook.write_text(hooks.script((self.root / 'gone' / 'aw.py').as_posix(), Path(sys.executable).as_posix()), encoding='utf-8', newline='\n')
        self.change()
        code, err = commit(self.wt, 'codex-bbbb')
        self.assertEqual(code, 0)
        self.assertIn('claim check skipped', err)


class SetupTests(RepoCase):
    def hook(self, name='pre-commit', repo='party'):
        return Path(gitio.common_dir(self.repo(repo))) / 'hooks' / name

    def test_setup_installs_in_every_repository_and_is_idempotent(self):
        code, out, _ = run('setup', '--json')
        self.assertEqual(code, 0, out)
        for repo in ('party', 'games'):
            for name in hooks.HOOKS:
                self.assertIn(hooks.MARKER, self.hook(name, repo).read_text(encoding='utf-8'))
            self.assertTrue((Path(gitio.common_dir(self.repo(repo))) / 'ai-workflow' / 'claims').is_dir())
        before = {n: self.hook(n).read_bytes() for n in hooks.HOOKS}
        code, out, _ = run('setup')
        self.assertEqual(code, 0)
        self.assertEqual(out.count('nothing to change'), 2)
        self.assertEqual(before, {n: self.hook(n).read_bytes() for n in hooks.HOOKS})
        self.assertEqual(run('setup', '--check')[0], 0)
        self.assertEqual(git(self.repo('party'), 'status', '--porcelain'), '')         # nothing tracked changes

    def test_check_reports_without_changing_anything(self):
        code, out, _ = run('setup', '--check')
        self.assertEqual(code, 3)
        self.assertIn('not installed', out)
        self.assertFalse(self.hook().exists())

    def test_an_existing_foreign_hook_is_never_overwritten_silently(self):
        self.hook().parent.mkdir(parents=True, exist_ok=True)
        self.hook().write_text('#!/bin/sh\necho mine >&2\nexit 0\n', encoding='utf-8', newline='\n')
        self.hook('pre-push').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        code, out, _ = run('setup')
        self.assertEqual(code, 3)
        self.assertIn('already exists and is not ours', out)
        self.assertIn('--chain', out)
        self.assertEqual(self.hook().read_text(encoding='utf-8'), '#!/bin/sh\necho mine >&2\nexit 0\n')
        self.assertIn(hooks.MARKER, self.hook(repo='games').read_text(encoding='utf-8'))    # the other repository still got set up

    def test_chain_keeps_the_existing_hook_running_and_uninstall_restores_it(self):
        self.hook().parent.mkdir(parents=True, exist_ok=True)
        foreign = '#!/bin/sh\necho "foreign hook ran" >&2\nexit 0\n'
        self.hook().write_text(foreign, encoding='utf-8', newline='\n')
        os.chmod(self.hook(), 0o755)
        self.hook('pre-push').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        self.assertEqual(run('setup', '--chain')[0], 0)
        self.assertEqual(run('setup', '--chain')[0], 0)                                # idempotent with a chained hook
        main = self.repo('party')
        (main / 'README.md').write_text('x\n', encoding='utf-8')
        git(main, 'add', 'README.md')
        code, err = commit(main, None)
        self.assertEqual(code, 0, err)
        self.assertIn('foreign hook ran', err)
        self.assertEqual(run('setup', '--uninstall')[0], 0)
        self.assertEqual(self.hook().read_text(encoding='utf-8'), foreign)
        self.assertFalse(self.hook('pre-merge-commit').exists())
        self.assertTrue(self.hook('pre-push').exists())                               # never ours, never touched
        self.assertEqual(run('setup', '--uninstall')[0], 0)                            # idempotent

    def test_warn_only_mode_warns_then_enforcing_mode_refuses(self):
        wt = self.add_worktree('party', 'feat/avr-900-thing', 'avrana-party.wt-avr900')
        self.assertEqual(run('setup', '--warn-only')[0], 0)
        code, out, _ = run('setup', '--check')
        self.assertEqual(code, 0)
        self.assertIn('warn-only', out)
        (wt / 'README.md').write_text('x' + chr(10), encoding='utf-8')
        git(wt, 'add', 'README.md')
        code, err = commit(wt, 'codex-bbbb')
        self.assertEqual(code, 0)
        self.assertIn('WARNING (not enforced yet', err)
        self.assertIn('must be claimed', err)
        self.assertIn('enforcing', run('setup')[1])
        (wt / 'README.md').write_text('y' + chr(10), encoding='utf-8')
        git(wt, 'add', 'README.md')
        self.assertNotEqual(commit(wt, 'codex-bbbb')[0], 0)

    def test_core_hookspath_stops_setup_with_a_clear_message(self):
        git(self.repo('party'), 'config', 'core.hooksPath', '.husky')
        code, out, _ = run('setup')
        self.assertEqual(code, 3)
        self.assertIn('core.hooksPath is set to .husky', out)
        self.assertFalse((self.repo('party') / '.husky').exists())

    def test_missing_repository_is_reported(self):
        os.environ['AI_WORKFLOW_ROOT'] = str(self.root / 'nowhere')
        code, out, _ = run('setup')
        self.assertEqual(code, 3)
        self.assertEqual(out.count('no checkout at'), 2)

    def test_setup_reports_linear_and_identity_without_printing_the_token(self):
        os.environ['LINEAR_API_KEY'] = 'placeholder-secretvalue'
        code, out, _ = run('setup')
        self.assertIn('linear: ok', out)
        self.assertNotIn('secretvalue', out)
        self.assertIn('identity: not available', out)


if __name__ == '__main__':
    unittest.main()
