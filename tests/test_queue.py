"""READY_FOR_PR, WIP limits, the release queue, merge sets and auto-merge eligibility. The tool
classifies and reports; it never opens, merges or configures anything on GitHub."""
import json
import subprocess
import unittest
from unittest import mock

from support import RepoCase, git, pr, run

from ai_workflow import cli, queue, sources

ON = sources.ok({'allow_auto_merge': True, 'required_checks': True})
OFF = sources.ok({'allow_auto_merge': False, 'required_checks': False})


class ClassificationTests(unittest.TestCase):
    def test_docs_only_change_is_a_docs_change(self):
        c = queue.classify(['README.md', 'docs/runbooks/deploy.md'])
        self.assertEqual((c['kind'], c['docs_only'], c['classes']), ('docs', True, []))

    def test_an_adr_is_docs_only_but_carries_an_adr_decision(self):
        c = queue.classify(['docs/adr/0017-queue.md'])
        self.assertEqual((c['kind'], c['docs_only'], c['classes']), ('implementation', True, ['adr']))

    def test_contract_protocol_and_deployment_paths_are_detected(self):
        self.assertEqual(queue.classify(['contracts/party-games.v0.json'])['classes'], ['contract'])
        self.assertEqual(queue.classify(['avrana/party/protocol.py', 'core/party_protocol.py'])['classes'], ['contract'])
        self.assertEqual(queue.classify(['ops/deploy.sh', 'arcade/avranaparty-arcade.service', 'avrana-party.nginx'])['classes'], ['deployment'])
        self.assertEqual(queue.classify(['avrana/web/app.py'])['classes'], [])

    def test_declared_classes_add_to_detected_ones_and_never_remove_them(self):
        c = queue.classify(['docs/x.md', 'ops/deploy.sh'], declared=['needs-cody'])
        self.assertEqual((c['kind'], c['classes']), ('implementation', ['deployment', 'needs-cody']))

    def test_unknown_files_are_never_docs_only(self):
        c = queue.classify(None)
        self.assertEqual((c['kind'], c['docs_only'], c['classes']), ('implementation', False, ['files-unknown']))


class AutoMergeSourceTests(unittest.TestCase):
    def runner(self, repo, protection=(1, '', 'gh: Branch not protected (HTTP 404)'), rules='[]'):
        def run(cmd, timeout=40):
            path = cmd[2]
            if path.endswith('/protection'):
                return protection
            if path.endswith('/rules/branches/main'):
                return 0, rules, ''
            return repo
        return run

    def test_setting_off_and_no_required_checks(self):
        got = sources.automerge('o/r', run=self.runner((0, 'false\n', '')))
        self.assertEqual(got, sources.ok({'allow_auto_merge': False, 'required_checks': False}))

    def test_setting_on_with_required_checks_from_protection_or_a_ruleset(self):
        got = sources.automerge('o/r', run=self.runner((0, 'true\n', ''), protection=(0, '2\n', '')))
        self.assertEqual(got['data'], {'allow_auto_merge': True, 'required_checks': True})
        got = sources.automerge('o/r', run=self.runner((0, 'true\n', ''), rules='[{"type": "required_status_checks"}]'))
        self.assertEqual(got['data'], {'allow_auto_merge': True, 'required_checks': True})

    def test_a_failure_is_unavailable_never_a_guess(self):
        self.assertFalse(sources.automerge('o/r', run=self.runner((1, '', 'error connecting')))['available'])
        self.assertFalse(sources.automerge('o/r', run=self.runner((0, 'true\n', ''), protection=(1, '', 'HTTP 403')))['available'])


class QueueCase(RepoCase):
    def work(self, name, branch, dirname, files, owner, claim='AVR-900'):
        """A claimed worktree with one commit touching `files`."""
        path = self.add_worktree(name, branch, dirname)
        for rel in files:
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text('x\n', encoding='utf-8')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'work')
        self.assertEqual(run('claim', '--path', str(path), '--owner', owner)[0], 0)
        return path

    def ready(self, path, owner, *extra):
        return run('ready', '--path', str(path), '--owner', owner, '--tests', 'unit 12/12', *extra, '--json')

    def board(self, *argv):
        code, out, _ = run('queue', *argv, '--json')
        return code, json.loads(out)


class ReadyTests(QueueCase):
    def test_ready_records_the_report_on_the_claim(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['avrana/web/app.py'], 'claude-aaaa')
        code, out, _ = self.ready(path, 'claude-aaaa')
        self.assertEqual(code, 0, out)
        entry = json.loads(out)['results'][0]['ready']
        self.assertEqual((entry['issue'], entry['branch'], entry['tests'], entry['kind']), ('AVR-900', 'feat/avr-900-x', 'unit 12/12', 'implementation'))
        self.assertEqual(entry['commit'], git(path, 'rev-parse', 'HEAD'))
        self.assertEqual(entry['files'], 1)
        code, doc = self.board()
        self.assertEqual([(e['issue'], e['state'], e['owner']) for e in doc['queue']], [('AVR-900', 'ready', 'claude-aaaa')])

    def test_ready_is_refused_without_the_claim_with_uncommitted_work_or_with_nothing_to_open(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.assertEqual(self.ready(path, 'codex-bbbb')[0], 3)                    # not the claim holder
        (path / 'a.py').write_text('dirty\n', encoding='utf-8')
        code, out, _ = self.ready(path, 'claude-aaaa')
        self.assertEqual(code, 3)
        self.assertIn('uncommitted', out)
        empty = self.add_worktree('party', 'feat/avr-901-y', 'wt901')
        run('claim', '--path', str(empty), '--owner', 'claude-aaaa')
        code, out, _ = self.ready(empty, 'claude-aaaa')
        self.assertEqual(code, 3)
        self.assertIn('no commits', out)

    def test_ready_survives_a_claim_refresh_and_goes_stale_when_the_branch_moves(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        run('claim', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(self.board()[1]['queue'][0]['state'], 'ready')
        (path / 'b.py').write_text('more\n', encoding='utf-8')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'more')
        entry = self.board()[1]['queue'][0]
        self.assertEqual(entry['state'], 'stale')
        self.assertFalse(entry['can_release'])
        self.assertTrue(any('moved' in r for r in entry['blocked_by']))


class WipLimitTests(QueueCase):
    def test_release_is_granted_when_the_slots_are_free(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        code, out, _ = run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(code, 0, out)
        self.assertIn('may open', out)
        self.assertEqual(self.board()[1]['queue'][0]['state'], 'released')

    def test_a_second_substantive_pr_in_a_repo_is_refused_naming_the_occupant(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(party=[pr('avrana-party', 63, 'feat/avr-256-units', files=['ops/deploy.sh'])])
        code, out, _ = run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(code, 3)
        self.assertIn('party#63', out)
        self.assertIn('substantive', out)
        self.assertEqual(self.board()[1]['queue'][0]['state'], 'ready')

    def test_a_docs_pr_fits_beside_a_substantive_one_but_not_beside_another_docs_pr(self):
        path = self.work('party', 'docs/avr-900-notes', 'wt900', ['docs/notes.md'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(party=[pr('avrana-party', 63, 'feat/avr-256-units', files=['ops/deploy.sh'])])
        self.assertEqual(run('queue', 'release', 'AVR-900', '--owner', 'gru')[0], 0)
        path2 = self.work('party', 'docs/avr-901-more', 'wt901', ['docs/more.md'], 'claude-aaaa', claim='AVR-901')
        self.ready(path2, 'claude-aaaa')
        self.world(party=[pr('avrana-party', 63, 'feat/avr-256-units', files=['ops/deploy.sh']),
                          pr('avrana-party', 64, 'docs/avr-900-notes', files=['docs/notes.md'])])
        code, out, _ = run('queue', 'release', 'AVR-901', '--owner', 'gru')
        self.assertEqual(code, 3)
        self.assertIn('party#64', out)

    def test_one_open_implementation_pr_per_agent_across_repositories(self):
        self.work('games', 'feat/avr-800-old', 'g800', ['a.py'], 'claude-aaaa', claim='AVR-800')
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(games=[pr('avrana-party-games', 32, 'feat/avr-800-old', files=['a.py'])])
        code, out, _ = run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(code, 3)
        self.assertIn('games#32', out)
        self.assertIn('claude-aaaa', out)

    def test_the_two_halves_of_one_merge_set_do_not_count_against_each_other(self):
        self.work('games', 'feat/avr-900-x', 'g900', ['a.py'], 'claude-aaaa')
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(games=[pr('avrana-party-games', 32, 'feat/avr-900-x', files=['a.py'])])
        self.assertEqual(run('queue', 'release', 'AVR-900', '--path', str(path), '--owner', 'gru')[0], 0)

    def test_github_unavailable_means_no_release_and_exit_4(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(fail=('o/avrana-party',))
        code, out, _ = run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(code, 4)
        self.assertEqual(self.board()[1]['queue'][0]['state'], 'ready')
        self.assertEqual(self.board()[0], 4)

    def test_nothing_ready_for_that_issue_is_a_refusal(self):
        code, _, err = run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(code, 3)
        self.assertIn('READY_FOR_PR', err)


class MergeSetTests(QueueCase):
    def both(self, party_pr, games_pr, behind=None):
        self.work('party', 'fix/avr-253-key', 'p253', ['a.py'], 'claude-aaaa', claim='AVR-253')
        self.work('games', 'fix/avr-253-key', 'g253', ['a.py'], 'claude-aaaa', claim='AVR-253')
        self.world(party=[party_pr] if party_pr else [], games=[games_pr] if games_pr else [], behind=behind)
        return self.board()[1]

    def test_a_set_is_mergeable_only_when_every_member_is_open_green_and_current(self):
        doc = self.both(pr('avrana-party', 59, 'fix/avr-253-key', files=['a.py']), pr('avrana-party-games', 33, 'fix/avr-253-key', files=['a.py']))
        (s,) = doc['sets']
        self.assertEqual((s['name'], s['verdict']), ('AVR-253', 'mergeable'))
        self.assertEqual(sorted(m['ref'] for m in s['members']), ['games#33', 'party#59'])

    def test_one_red_or_behind_member_holds_the_whole_set(self):
        doc = self.both(pr('avrana-party', 59, 'fix/avr-253-key', files=['a.py']),
                        pr('avrana-party-games', 33, 'fix/avr-253-key', files=['a.py'], checks=(('COMPLETED', 'FAILURE'),)))
        self.assertEqual(doc['sets'][0]['verdict'], 'waiting')
        self.assertTrue(any('games#33' in r and 'ci' in r for r in doc['sets'][0]['reasons']))
        party = next(p for p in doc['repos']['party']['open'] if p['number'] == 59)
        self.assertFalse(party['mergeable_now'])
        self.assertTrue(any('merge set AVR-253' in r for r in party['auto_merge']['reasons']))

    def test_a_member_that_is_behind_main_holds_the_set(self):
        doc = self.both(pr('avrana-party', 59, 'fix/avr-253-key', files=['a.py']), pr('avrana-party-games', 33, 'fix/avr-253-key', files=['a.py']),
                        behind={'fix/avr-253-key': 2})
        self.assertEqual(doc['sets'][0]['verdict'], 'waiting')
        self.assertTrue(any('behind' in r for r in doc['sets'][0]['reasons']))

    def test_a_member_without_a_pr_holds_the_set(self):
        doc = self.both(pr('avrana-party', 59, 'fix/avr-253-key', files=['a.py']), None)
        self.assertEqual(doc['sets'][0]['verdict'], 'waiting')
        self.assertTrue(any('games' in r and 'no PR' in r for r in doc['sets'][0]['reasons']))

    def test_one_member_merged_without_the_others_is_a_loud_warning(self):
        doc = self.both(pr('avrana-party', 59, 'fix/avr-253-key', state='MERGED', merged='2026-10-04T00:00:00Z', files=['a.py']),
                        pr('avrana-party-games', 33, 'fix/avr-253-key', files=['a.py']))
        self.assertEqual(doc['sets'][0]['verdict'], 'SPLIT')
        code, out, _ = run('queue')
        self.assertIn('SPLIT', out)
        self.assertIn('party#59 merged', out)

    def test_a_named_set_joins_different_issues(self):
        a = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        b = self.work('games', 'feat/avr-901-y', 'g901', ['a.py'], 'claude-aaaa', claim='AVR-901')
        self.ready(a, 'claude-aaaa', '--set', 'native-path')
        self.ready(b, 'claude-aaaa', '--set', 'native-path')
        (s,) = self.board()[1]['sets']
        self.assertEqual((s['name'], s['verdict'], len(s['members'])), ('native-path', 'waiting', 2))


class AutoMergeEligibilityTests(QueueCase):
    def one(self, files, automerge=None, declared=(), **prkw):
        path = self.work('party', 'docs/avr-900-notes', 'wt900', files, 'claude-aaaa')
        self.ready(path, 'claude-aaaa', *declared)
        self.world(party=[pr('avrana-party', 64, 'docs/avr-900-notes', files=files, **prkw)], automerge=automerge or {'party': ON, 'games': ON})
        return self.board()[1]['repos']['party']

    def test_a_boring_green_current_docs_pr_is_eligible(self):
        repo = self.one(['docs/notes.md'])
        self.assertEqual(repo['open'][0]['auto_merge'], {'eligible': True, 'reasons': []})

    def test_each_criterion_that_fails_is_named(self):
        for files, declared, prkw, needle in (
                (['a.py'], (), {}, 'not docs-only'),
                (['docs/adr/0017-x.md'], (), {}, 'ADR'),
                (['docs/x.md', 'contracts/c.json'], (), {}, 'contract'),
                (['docs/x.md', 'ops/deploy.sh'], (), {}, 'deployment'),
                (['docs/x.md'], ('--needs-cody',), {}, 'Needs Cody'),
                (['docs/x.md'], (), {'checks': (('IN_PROGRESS', ''),)}, 'CI'),
                (['docs/x.md'], (), {'checks': ()}, 'CI')):
            with self.subTest(needle=needle):
                am = self.fresh(files, declared, prkw)
                self.assertFalse(am['eligible'])
                self.assertTrue(any(needle in r for r in am['reasons']), am['reasons'])

    def fresh(self, files, declared, prkw):
        n = getattr(self, '_n', 0) + 1
        self._n = n
        path = self.work('party', f'docs/avr-9{n:02}-notes', f'wt9{n:02}', files, 'claude-aaaa', claim=f'AVR-9{n:02}')
        self.ready(path, 'claude-aaaa', *declared)
        self.world(party=[pr('avrana-party', 64, f'docs/avr-9{n:02}-notes', files=files, **prkw)], automerge={'party': ON, 'games': ON})
        return self.board()[1]['repos']['party']['open'][0]['auto_merge']

    def test_a_pr_with_no_ready_record_is_not_eligible_because_needs_cody_was_never_declared(self):
        self.world(party=[pr('avrana-party', 64, 'docs/avr-900-notes', files=['docs/x.md'])], automerge={'party': ON, 'games': ON})
        am = self.board()[1]['repos']['party']['open'][0]['auto_merge']
        self.assertFalse(am['eligible'])
        self.assertTrue(any('READY_FOR_PR' in r for r in am['reasons']))

    def test_behind_main_is_not_eligible(self):
        path = self.work('party', 'docs/avr-900-notes', 'wt900', ['docs/x.md'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        self.world(party=[pr('avrana-party', 64, 'docs/avr-900-notes', files=['docs/x.md'])], behind={'docs/avr-900-notes': 3},
                   automerge={'party': ON, 'games': ON})
        am = self.board()[1]['repos']['party']['open'][0]['auto_merge']
        self.assertTrue(any('behind main' in r for r in am['reasons']))

    def test_the_repository_gap_is_reported_not_assumed(self):
        repo = self.one(['docs/notes.md'], automerge={'party': OFF, 'games': OFF})
        self.assertEqual(repo['auto_merge']['available'], False)
        self.assertIn('allow_auto_merge off', repo['auto_merge']['why'])
        self.assertIn('no required checks', repo['auto_merge']['why'])
        self.assertTrue(repo['open'][0]['auto_merge']['eligible'])               # the PR qualifies; the repository cannot do it
        code, out, _ = run('queue')
        self.assertIn('auto-merge not available on this repo', out)

    def test_an_unreadable_repository_setting_is_unknown_and_listed_as_unavailable(self):
        code, doc = self.board()
        self.assertIsNone(doc['repos']['party']['auto_merge']['available'])
        self.assertTrue(any(u['source'] == 'github-settings:party' for u in doc['unavailable']))


class NoSideEffectTests(QueueCase):
    def test_the_queue_never_calls_a_writing_gh_command(self):
        path = self.work('party', 'feat/avr-900-x', 'wt900', ['a.py'], 'claude-aaaa')
        self.ready(path, 'claude-aaaa')
        seen, real = [], subprocess.run

        def recorder(cmd, *a, **kw):                              # the real readers, with `gh` itself replaced
            if cmd[0] != 'gh':
                return real(cmd, *a, **kw)
            seen.append(cmd)
            raise OSError('no gh in tests')

        cli.SOURCES = cli.Sources
        with mock.patch.object(subprocess, 'run', recorder):
            run('queue')
            run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertTrue(seen)
        self.assertFalse([c for c in seen if any(w in c for w in ('create', 'merge', 'edit', '-X', '--method', '-f', '-F'))], seen)


if __name__ == '__main__':
    unittest.main()
