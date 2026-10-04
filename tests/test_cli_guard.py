import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from support import (GAMES_ONLY, PARTY_ONLY, READY, ROOT, UNDECIDED, RepoCase, git, issue, pr, run)

from ai_workflow import claims, gitio, guard


class ClaimCliTests(RepoCase):
    def test_claiming_an_issue_claims_its_worktree_in_both_repositories(self):
        self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        self.add_worktree('games', 'feat/avr-900-x', 'avrana-party-games.wt-avr900')
        code, out, _ = run('claim', 'AVR-900', '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0)
        self.assertEqual([r['code'] for r in json.loads(out)['results']], ['claimed', 'claimed'])
        code, out, _ = run('claim', 'avr-900', '--owner', 'codex-bbbb')
        self.assertEqual(code, 3)
        self.assertIn('claude-aaaa', out)
        owners = {t['path'].replace('\\', '/').rsplit('/', 1)[-1]: (t['claim'] or {}).get('owner')
                  for r in json.loads(run('status', '--json')[1])['repos'].values() for t in r['data']}
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
        self.assertIn('AI_WORKFLOW_SESSION', err)
        os.environ['CLAUDE_CODE_SESSION_ID'] = '8cf0d847-ee34-418b'
        self.assertIn('claude-8cf0d847', run('claim', 'AVR-900')[1])

    def test_separate_processes_racing_for_one_worktree_have_one_winner(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        procs = [subprocess.Popen([sys.executable, str(ROOT / 'aw.py'), 'claim', '--path', str(wt), '--owner', f'agent-{i}'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
        codes = [p.wait() for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(sorted(codes), [0, 3, 3, 3, 3, 3])
        self.assertEqual(claims.state(claims.read(claims.claim_file(gitio.common_dir(wt), wt))), 'held')


class StartTests(RepoCase):
    def start(self, *extra, ident='AVR-900', owner='claude-aaaa'):
        code, out, err = run('start', ident, '--owner', owner, '--json', *extra)
        return code, json.loads(out) if out.strip() else err

    def test_single_repository_issue_end_to_end(self):
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY, title='Build the generic native-game registry, routing & provisioning')])
        code, doc = self.start()
        self.assertEqual(code, 0, doc)
        self.assertEqual((doc['started'], doc['task']), (True, 'Implement AVR-900'))
        [w] = doc['workspaces']
        self.assertEqual((w['repo'], w['branch'], w['action'], w['claim']),
                         ('party', 'feat/avr-900-build-the-generic-native-game', 'create', 'claimed'))
        wt = self.root / 'avrana-party.wt-avr900'
        self.assertEqual(git(wt, 'rev-parse', 'HEAD'), git(self.repo('party'), 'rev-parse', 'origin/main'))
        self.assertEqual(claims.read(claims.claim_file(gitio.common_dir(wt), wt))['owner'], 'claude-aaaa')
        self.assertFalse((self.root / 'avrana-party-games.wt-avr900').exists())
        self.assertEqual(doc['context']['linear']['sections']['Acceptance Criteria'], '- y')
        self.assertEqual(git(wt, 'config', '--get', 'branch.feat/avr-900-build-the-generic-native-game.merge') if False else '', '')

    def test_paired_issue_gets_the_same_branch_in_both_repositories(self):
        self.world(issues=[issue('AVR-900', description=READY, labels=['Bug'])])
        code, doc = self.start('--desc', 'envelope')
        self.assertEqual(code, 0, doc)
        self.assertEqual([(w['repo'], w['branch'], w['claim']) for w in doc['workspaces']],
                         [('games', 'fix/avr-900-envelope', 'claimed'), ('party', 'fix/avr-900-envelope', 'claimed')])

    def test_start_with_agent_and_session(self):
        self.world(issues=[issue('AVR-900', description=GAMES_ONLY)])
        code, out, _ = run('start', 'AVR-900', '--agent', 'codex', '--session', '1a2b3c4d-9999-ffff', '--json')
        self.assertEqual((code, json.loads(out)['owner']), (0, 'codex-1a2b3c4d'))

    def test_existing_worktree_is_found_not_recreated_and_the_owner_resumes(self):
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY)])
        self.assertEqual(self.start()[0], 0)
        code, doc = self.start()
        self.assertEqual((code, doc['workspaces'][0]['action'], doc['workspaces'][0]['claim']), (0, 'reuse', 'refreshed'))
        self.assertEqual(len(gitio.worktrees(self.repo('party'))), 2)

    def test_a_second_session_is_refused_and_nothing_is_created(self):
        self.world(issues=[issue('AVR-900', description=READY)])
        self.assertEqual(self.start()[0], 0)
        code, doc = self.start(owner='codex-bbbb')
        self.assertEqual((code, doc['started']), (3, False))
        self.assertIn('claimed by claude-aaaa', doc['refused'])

    def test_existing_unmerged_remote_branch_is_checked_out_not_duplicated(self):
        other = self.add_worktree('party', 'feat/avr-900-earlier', 'tmp-wt')
        (other / 'work.md').write_text('earlier\n', encoding='utf-8')
        git(other, 'add', 'work.md')
        git(other, 'commit', '-q', '-m', 'earlier work')
        git(other, 'push', '-q', 'origin', 'feat/avr-900-earlier')
        git(self.repo('party'), 'worktree', 'remove', str(other))
        git(self.repo('party'), 'branch', '-D', 'feat/avr-900-earlier')
        self.world(issues=[issue('AVR-900', state='In Progress', description=PARTY_ONLY)])
        code, doc = self.start()
        self.assertEqual(code, 0, doc)
        self.assertEqual((doc['workspaces'][0]['action'], doc['workspaces'][0]['branch']), ('checkout', 'feat/avr-900-earlier'))
        self.assertTrue((self.root / 'avrana-party.wt-avr900' / 'work.md').exists())

    def test_existing_open_pr_means_the_issue_is_not_started_again(self):
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY)], party=[pr('party', 12, 'feat/avr-900-x')])
        code, doc = self.start()
        self.assertEqual((code, doc['context']['readiness']['state']), (3, 'pr-ci'))
        self.assertEqual(len(gitio.worktrees(self.repo('party'))), 1)

    def test_refusals_create_nothing(self):
        cases = [([issue('AVR-900', description=UNDECIDED)], 3, 'needs-cody'),
                 ([issue('AVR-900', description=READY, blocked_by=['AVR-1']), issue('AVR-1', state='In Progress')], 3, 'blocked'),
                 ([issue('AVR-900', state='Done', description=READY)], 3, 'done'),
                 ([issue('AVR-900', state='Backlog', description=READY)], 3, 'needs-cody'),
                 ([issue('AVR-900', description='## Outcome\nno template')], 4, 'unknown'),
                 (None, 4, 'unknown')]
        for issues, want, state in cases:
            self.world(issues=issues)
            code, doc = self.start()
            self.assertEqual((code, doc['context']['readiness']['state'], doc['started']), (want, state, False), issues)
        self.assertEqual(len(gitio.worktrees(self.repo('party'))) + len(gitio.worktrees(self.repo('games'))), 2)

    def test_dry_run_plans_but_fetches_creates_and_claims_nothing(self):
        self.world(issues=[issue('AVR-900', description=READY)], fetch=False)          # a dry run never needs the fetch
        code, doc = self.start('--dry-run')
        self.assertEqual((code, doc['started'], doc['dry_run']), (0, False, True))
        self.assertEqual([(w['repo'], w['action'], w['branch']) for w in doc['workspaces']],
                         [('games', 'create', 'feat/avr-900-avr-900-title'), ('party', 'create', 'feat/avr-900-avr-900-title')])
        for name in ('party', 'games'):
            self.assertEqual(len(gitio.worktrees(self.repo(name))), 1)
            self.assertEqual(git(self.repo(name), 'branch', '--list', '*avr-900*'), '')
            self.assertFalse((self.repo(name) / '.git' / 'ai-workflow').exists())
        self.world(issues=[issue('AVR-900', description=UNDECIDED)])
        code, doc = self.start('--dry-run')
        self.assertEqual((code, doc['started']), (3, False))

    def test_owner_supplied_repo_and_decisions_unblock_an_untemplated_issue(self):
        self.world(issues=[issue('AVR-900', description='## Outcome\nno template')])
        code, doc = self.start('--repo', 'games', '--decisions-confirmed')
        self.assertEqual((code, [w['repo'] for w in doc['workspaces']]), (0, ['games']))

    def test_unavailable_github_or_stale_main_refuses(self):
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY)], fail={'o/avrana-party'})
        self.assertEqual(self.start()[0], 4)
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY)], fetch=False)
        code, doc = self.start()
        self.assertEqual(code, 4)
        self.assertIn('stale main', doc['refused'])
        self.assertEqual(len(gitio.worktrees(self.repo('party'))), 1)

    def test_issue_command_changes_nothing(self):
        self.world(issues=[issue('AVR-900', description=READY)])
        code, out, _ = run('issue', 'AVR-900')
        self.assertEqual(code, 0)
        self.assertIn('Ready for Agent - an agent may start', out)
        self.assertEqual(len(gitio.worktrees(self.repo('party'))), 1)

    def test_everything_unavailable_is_said_so(self):
        ctx = json.loads(run('issue', 'AVR-900', '--json')[1])
        self.assertEqual({u['source'] for u in ctx['unavailable']}, {'linear', 'pi'})
        self.world(fail={'o/avrana-party', 'o/avrana-party-games'})
        ctx = json.loads(run('issue', 'AVR-900', '--json')[1])
        self.assertEqual({u['source'] for u in ctx['unavailable']}, {'linear', 'github:party', 'github:games', 'pi'})
        self.assertEqual({u['source'] for u in json.loads(run('needs-cody', '--json')[1])['unavailable']},
                         {'linear', 'github:party', 'github:games', 'pi'})
        self.assertEqual(run('prs', 'AVR-900')[0], 4)

    def test_linear_snapshot_fallback(self):
        snap = self.root / 'snap.json'
        snap.write_text(json.dumps({'issues': [issue('AVR-900', description=None), issue('AVR-900', description=READY)]}), encoding='utf-8')
        ctx = json.loads(run('issue', 'AVR-900', '--linear-snapshot', str(snap), '--json')[1])
        self.assertEqual((ctx['linear']['open_decisions'], ctx['readiness']['state']), ('none', 'ready-for-agent'))

    def test_needs_cody_text_has_the_three_sections(self):
        self.world(issues=[issue('AVR-10', description=UNDECIDED)], party=[pr('party', 3, 'fix/avr-3-c', checks=[('IN_PROGRESS', '')])])
        out = run('needs-cody')[1]
        self.assertIn('Needs Cody now (1)', out)
        self.assertIn('Agent work in progress (1)', out)
        self.assertIn('Could not check (1)', out)


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
