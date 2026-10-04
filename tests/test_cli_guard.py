import contextlib
import io
import json
import os
import unittest

from support import READY, RepoCase, git, issue

from avrana_workflow import claims, cli, gitio, guard, sources


class Offline:
    """No GitHub, no Linear, no Pi: the CLI must still answer and say what it could not read."""
    prs = staticmethod(lambda repo, slug, issue=None: sources.unavailable(f'gh failed for {slug} (offline)'))
    linear = staticmethod(lambda issue=None, snapshot=None: sources.linear(issue, snapshot=snapshot))
    pi_status = staticmethod(lambda url: sources.unavailable(f'{url} unreachable (offline)'))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(RepoCase):
    def setUp(self):
        super().setUp()
        self._sources, cli.SOURCES = cli.SOURCES, Offline
        self.addCleanup(setattr, cli, 'SOURCES', self._sources)

    def test_claiming_an_issue_claims_its_worktree_in_both_repositories(self):
        self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        self.add_worktree('games', 'feat/avr-900-x', 'avrana-party-games.wt-avr900')
        code, out, _ = run('claim', 'AVR-900', '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0)
        self.assertEqual([r['code'] for r in json.loads(out)['results']], ['claimed', 'claimed'])
        code, out, _ = run('claim', 'avr-900', '--owner', 'codex-bbbb')
        self.assertEqual(code, 3)
        self.assertIn('claude-aaaa', out)
        code, out, _ = run('status', '--json')
        owners = {t['path'].replace('\\', '/').rsplit('/', 1)[-1]: (t['claim'] or {}).get('owner')
                  for r in json.loads(out)['repos'].values() for t in r['data']}
        self.assertEqual(owners, {'avrana-party': None, 'avrana-party.wt-avr900': 'claude-aaaa',
                                  'avrana-party-games': None, 'avrana-party-games.wt-avr900': 'claude-aaaa'})

    def test_handoff_then_release_through_the_cli(self):
        self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        run('claim', 'AVR-900', '--owner', 'claude-aaaa')
        self.assertEqual(run('release', 'AVR-900', '--owner', 'codex-bbbb')[0], 3)
        self.assertEqual(run('handoff', 'AVR-900', '--owner', 'claude-aaaa', '--to', 'codex-bbbb', '--note', 'CI red')[0], 0)
        self.assertEqual(run('claim', 'AVR-900', '--owner', 'codex-bbbb')[0], 0)
        self.assertEqual(run('release', 'AVR-900', '--owner', 'codex-bbbb')[0], 0)
        self.assertIn('claimed', run('claim', 'AVR-900', '--owner', 'claude-cccc')[1])

    def test_no_identity_is_an_error_not_an_anonymous_claim(self):
        self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        code, _, err = run('claim', 'AVR-900')
        self.assertEqual(code, 2)
        self.assertIn('AVRANA_SESSION', err)
        os.environ['CLAUDE_CODE_SESSION_ID'] = '8cf0d847-ee34-418b'
        self.assertIn('claude-8cf0d847', run('claim', 'AVR-900')[1])

    def test_worktree_creates_a_dedicated_claimed_worktree_from_origin_main(self):
        code, out, _ = run('worktree', 'AVR-900', '--repo', 'both', '--type', 'feat', '--desc', 'registry', '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0, out)
        rows = json.loads(out)['results']
        self.assertEqual([(r['created'], r['code'], r['branch']) for r in rows], [(True, 'claimed', 'feat/avr-900-registry')] * 2)
        self.assertTrue((self.root / 'avrana-party.wt-avr900' / 'README.md').exists())
        self.assertEqual(git(self.root / 'avrana-party.wt-avr900', 'rev-parse', 'HEAD'), git(self.repo('party'), 'rev-parse', 'origin/main'))
        # a second session asking for the same issue is shown the existing worktree and refused
        code, out, _ = run('worktree', 'AVR-900', '--repo', 'party', '--owner', 'codex-bbbb', '--json')
        self.assertEqual(code, 3)
        self.assertEqual((json.loads(out)['results'][0]['created'], json.loads(out)['results'][0]['code']), (False, 'refused-held'))

    def test_issue_and_queue_report_every_unavailable_source(self):
        code, out, _ = run('issue', 'AVR-900', '--json')
        ctx = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(ctx['ready'])
        self.assertEqual({u['source'] for u in ctx['unavailable']}, {'linear', 'github:party', 'github:games', 'pi'})
        code, out, _ = run('needs-cody', '--json')
        self.assertEqual({u['source'] for u in json.loads(out)['unavailable']}, {'linear', 'github:party', 'github:games', 'pi'})
        self.assertEqual(run('prs', 'AVR-900')[0], 4)

    def test_linear_snapshot_from_a_connector(self):
        snap = self.root / 'snap.json'
        snap.write_text(json.dumps({'issues': [issue('AVR-900', description=None), issue('AVR-900', description=READY)]}), encoding='utf-8')
        ctx = json.loads(run('issue', 'AVR-900', '--linear-snapshot', str(snap), '--json')[1])
        self.assertEqual(ctx['linear']['open_decisions'], 'none')
        self.assertNotIn('linear', {u['source'] for u in ctx['unavailable']})


class GuardTests(RepoCase):
    def edit(self, session, path):
        return guard.evaluate({'session_id': session, 'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}}, self.cfg)

    def bash(self, session, command, cwd):
        return guard.evaluate({'session_id': session, 'cwd': str(cwd), 'tool_name': 'Bash', 'tool_input': {'command': command}}, self.cfg)

    def test_two_sessions_editing_one_worktree(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        first = self.edit('aaaaaaaa-1111', wt / 'docs' / 'new.md')              # the file need not exist yet
        self.assertEqual(first[0], 'note')
        self.assertEqual(claims.read(claims.claim_file(gitio.common_dir(wt), wt))['issue'], 'AVR-900')
        self.assertEqual(self.edit('aaaaaaaa-1111', wt / 'README.md'), ('ok', ''))
        decision, message = self.edit('bbbbbbbb-2222', wt / 'README.md')
        self.assertEqual(decision, 'ask')
        self.assertIn('claude-aaaaaaaa', message)

    def test_commit_and_merge_in_another_sessions_worktree_ask(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        self.edit('aaaaaaaa-1111', wt / 'README.md')
        self.assertEqual(self.bash('bbbbbbbb-2222', 'git commit -am "wip"', wt)[0], 'ask')
        self.assertEqual(self.bash('bbbbbbbb-2222', 'git merge origin/main', wt)[0], 'ask')
        self.assertEqual(self.bash('bbbbbbbb-2222', f'git -C "{wt}" add -A', self.root)[0], 'ask')
        self.assertEqual(self.bash('bbbbbbbb-2222', f'cd "{wt}" && git stash', self.root)[0], 'ask')
        self.assertEqual(self.bash('bbbbbbbb-2222', 'git status && git log -3', wt), ('ok', ''))

    def test_other_worktrees_and_other_repositories_are_untouched(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        self.edit('aaaaaaaa-1111', wt / 'README.md')
        self.assertEqual(self.edit('bbbbbbbb-2222', self.repo('games') / 'README.md')[0], 'note')
        self.assertEqual(self.edit('bbbbbbbb-2222', self.root / 'elsewhere' / 'notes.md'), ('ok', ''))
        self.assertEqual(guard.evaluate({'tool_name': 'Edit', 'tool_input': {'file_path': str(wt / 'README.md')}}, self.cfg), ('ok', ''))

    def test_hook_output_shape(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        self.edit('aaaaaaaa-1111', wt / 'README.md')
        out = io.StringIO()
        payload = {'session_id': 'bbbbbbbb-2222', 'tool_name': 'Write', 'tool_input': {'file_path': str(wt / 'a.txt')}}
        with contextlib.redirect_stdout(out):
            self.assertEqual(guard.main(io.StringIO(json.dumps(payload)), self.cfg), 0)
        self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'ask')
        with contextlib.redirect_stdout(io.StringIO()) as quiet:
            self.assertEqual(guard.main(io.StringIO('not json'), self.cfg), 0)
        self.assertEqual(quiet.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
