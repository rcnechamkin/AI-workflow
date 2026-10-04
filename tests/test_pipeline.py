"""The pipeline's remaining deterministic steps: validate, pr, next and reconcile. One issue goes
start -> implement -> validate -> ready -> (release) -> pr -> merge -> reconcile, and `next` names
the single next step, or stops for a human."""
import json
from pathlib import Path

from support import PARTY_ONLY, UNDECIDED, RepoCase, git, issue, pr, run

VALIDATE = {'party': [{'name': 'unit', 'run': 'run-unit'}, {'name': 'browser', 'run': 'run-browser', 'advisory_on': ['*']}]}


class PipelineCase(RepoCase):
    def setUp(self):
        super().setUp()
        path = self.root / 'workflow.json'
        cfg = json.loads(path.read_text(encoding='utf-8'))
        cfg['validate'] = VALIDATE
        path.write_text(json.dumps(cfg), encoding='utf-8')
        self.opened, self.ran = [], []

    def world(self, checks=None, **kw):
        super().world(checks={'run-unit': 0, 'run-browser': 0, **(checks or {})}, opened=self.opened, ran=self.ran, **kw)

    def work(self, branch='feat/avr-900-x', files=('a.py',), owner='claude-aaaa', repo='party', dirname='wt900'):
        path = self.add_worktree(repo, branch, dirname)
        for rel in files:
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text('x\n', encoding='utf-8')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'Make the thing work')
        self.assertEqual(run('claim', '--path', str(path), '--owner', owner)[0], 0)
        return path

    def step(self, *extra, owner='claude-aaaa'):
        code, out, _ = run('next', 'AVR-900', '--owner', owner, '--json', *extra)
        doc = json.loads(out)
        return code, doc, [(s['stage'], s['who']) for s in doc['steps']]


class ValidateTests(PipelineCase):
    def test_validate_runs_the_configured_checks_and_records_the_result_at_the_commit(self):
        path = self.work()
        self.world()
        code, out, _ = run('validate', '--path', str(path), '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0, out)
        v = json.loads(out)['results'][0]['validated']
        self.assertEqual((v['ok'], v['commit'], [c['name'] for c in v['checks']]), (True, git(path, 'rev-parse', 'HEAD'), ['unit', 'browser']))
        self.assertEqual([c for c, _ in self.ran], ['run-unit', 'run-browser'])
        self.assertTrue(all(Path(cwd).resolve() == path.resolve() for _, cwd in self.ran))

    def test_a_failing_required_check_fails_validation_and_an_advisory_one_does_not(self):
        path = self.work()
        self.world(checks={'run-browser': 1})
        code, out, _ = run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(code, 0, out)
        self.assertIn('advisory', out)
        self.world(checks={'run-unit': 1})
        code, out, _ = run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(code, 3)
        self.assertIn('FAIL', out)

    def test_validate_refuses_uncommitted_work_and_a_repository_with_no_checks_is_not_a_pass(self):
        path = self.work()
        self.world()
        (path / 'a.py').write_text('dirty\n', encoding='utf-8')
        self.assertEqual(run('validate', '--path', str(path), '--owner', 'claude-aaaa')[0], 3)
        games = self.work(repo='games', dirname='g900')
        code, out, _ = run('validate', '--path', str(games), '--owner', 'claude-aaaa')
        self.assertEqual(code, 4)
        self.assertIn('no validation is configured', out)

    def test_a_commit_made_while_the_checks_run_is_not_validated(self):
        from ai_workflow import model, pipeline
        path = self.work()
        before = git(path, 'rev-parse', 'HEAD')

        def run_and_commit(command, cwd, timeout=None):
            if not (path / 'late.py').exists():
                (path / 'late.py').write_text('late\n', encoding='utf-8')
                git(path, 'add', '-A')
                git(path, 'commit', '-q', '-m', 'late')
            return 0, ''

        cfg = model.load_config(self.root / 'workflow.json', self.root)
        validated, _ = pipeline.validate(cfg, 'party', str(path), run_and_commit)
        self.assertEqual((validated['commit'], validated['ok']), (before, False))
        self.assertTrue(validated['moved'])

    def test_ready_uses_the_validation_instead_of_a_typed_report_and_refuses_without_one(self):
        path = self.work()
        self.world()
        code, out, _ = run('ready', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(code, 3)
        self.assertIn('validate', out)
        run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        code, out, _ = run('ready', '--path', str(path), '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0, out)
        self.assertIn('2/2', json.loads(out)['results'][0]['ready']['tests'])
        self.world(checks={'run-unit': 1})
        (path / 'b.py').write_text('y\n', encoding='utf-8')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'more')
        run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(run('ready', '--path', str(path), '--owner', 'claude-aaaa')[0], 3)      # failed validation is not ready


class PrTests(PipelineCase):
    def released(self, **kw):
        path = self.work(**kw)
        self.world(issues=[{**issue('AVR-900', state='In Progress', description=PARTY_ONLY), 'title': 'Route native games'}])
        run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        run('ready', '--path', str(path), '--owner', 'claude-aaaa')
        return path

    def test_pr_is_refused_until_the_orchestrator_releases_it(self):
        path = self.released()
        code, out, _ = run('pr', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(code, 3)
        self.assertIn('not released', out)
        self.assertEqual(self.opened, [])

    def test_pr_pushes_the_branch_and_opens_the_pr_from_the_record(self):
        path = self.released()
        run('queue', 'release', 'AVR-900', '--owner', 'gru')
        code, out, _ = run('pr', '--path', str(path), '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0, out)
        (call,) = self.opened
        self.assertEqual((call['slug'], call['branch'], call['title']), ('o/avrana-party', 'feat/avr-900-x', 'Route native games (AVR-900)'))
        for needle in ('AVR-900', '2/2', 'implementation', 'released by gru'):
            self.assertIn(needle, call['body'])
        self.assertEqual(git(self.repo('party'), 'rev-parse', 'origin/feat/avr-900-x'), git(path, 'rev-parse', 'HEAD'))
        self.assertEqual(json.loads(out)['results'][0]['url'], 'https://example.invalid/o/avrana-party/pull/1')

    def test_a_change_that_needs_the_owner_says_so_in_the_pr(self):
        path = self.work(files=('ops/deploy.sh',))
        self.world()
        run('validate', '--path', str(path), '--owner', 'claude-aaaa')
        run('ready', '--path', str(path), '--owner', 'claude-aaaa', '--needs-cody')
        run('queue', 'release', 'AVR-900', '--owner', 'gru')
        run('pr', '--path', str(path), '--owner', 'claude-aaaa')
        body = self.opened[0]['body']
        self.assertIn('Requires Cody before merge', body)
        self.assertIn('deployment', body)

    def test_dry_run_and_an_existing_pr_open_nothing(self):
        path = self.released()
        run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(run('pr', '--path', str(path), '--owner', 'claude-aaaa', '--dry-run')[0], 0)
        self.assertEqual(self.opened, [])
        self.world(party=[pr('avrana-party', 70, 'feat/avr-900-x', files=['a.py'])])
        code, out, _ = run('pr', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(code, 0)
        self.assertIn('party#70', out)
        self.assertEqual(self.opened, [])

    def test_pr_is_not_opened_when_the_branch_moved_or_github_is_unreadable(self):
        path = self.released()
        run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.world(fail=('o/avrana-party',))
        self.assertEqual(run('pr', '--path', str(path), '--owner', 'claude-aaaa')[0], 4)
        self.world()
        (path / 'b.py').write_text('y\n', encoding='utf-8')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'more')
        self.assertEqual(run('pr', '--path', str(path), '--owner', 'claude-aaaa')[0], 3)
        self.assertEqual(self.opened, [])


class NextTests(PipelineCase):
    READY = [issue('AVR-900', description=PARTY_ONLY)]

    def test_each_stage_names_one_next_step(self):
        self.world(issues=self.READY)
        self.assertEqual(self.step()[2], [('start', 'agent')])
        path = self.add_worktree('party', 'feat/avr-900-x', 'wt900')
        run('claim', '--path', str(path), '--owner', 'claude-aaaa')
        self.assertEqual(self.step()[2], [('implement', 'agent')])
        (path / 'a.py').write_text('x\n', encoding='utf-8')
        self.assertEqual(self.step()[2], [('implement', 'agent')])                # uncommitted work
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'work')
        code, doc, steps = self.step()
        self.assertEqual(steps, [('validate', 'agent')])
        self.assertIn('validate AVR-900', doc['steps'][0]['next'])
        run('validate', 'AVR-900', '--owner', 'claude-aaaa')
        self.assertEqual(self.step()[2], [('ready', 'agent')])
        run('ready', 'AVR-900', '--owner', 'claude-aaaa')
        self.assertEqual(self.step()[2], [('release', 'orchestrator')])
        run('queue', 'release', 'AVR-900', '--owner', 'gru')
        self.assertEqual(self.step()[2], [('pr', 'agent')])
        self.world(issues=self.READY, party=[pr('avrana-party', 70, 'feat/avr-900-x', checks=(('IN_PROGRESS', ''),))])
        self.assertEqual(self.step()[2], [('ci', 'none')])
        self.world(issues=self.READY, party=[pr('avrana-party', 70, 'feat/avr-900-x', checks=(('COMPLETED', 'FAILURE'),))])
        self.assertEqual(self.step()[2], [('fix-ci', 'agent')])
        self.world(issues=self.READY, party=[pr('avrana-party', 70, 'feat/avr-900-x')], behind={'feat/avr-900-x': 2})
        self.assertEqual(self.step()[2], [('update', 'agent')])
        self.world(issues=self.READY, party=[pr('avrana-party', 70, 'feat/avr-900-x')])
        self.assertEqual(self.step()[2], [('merge', 'cody')])
        self.world(issues=self.READY, party=[pr('avrana-party', 70, 'feat/avr-900-x', state='MERGED', merged='2026-10-04T00:00:00Z')])
        code, doc, steps = self.step()
        self.assertEqual(steps, [('finish', 'agent')])
        self.assertIn('release', doc['steps'][0]['next'])

    def test_a_failed_validation_sends_the_agent_back_to_fix_it(self):
        path = self.work()
        self.world(issues=self.READY, checks={'run-unit': 1})
        run('validate', 'AVR-900', '--owner', 'claude-aaaa')
        self.assertEqual(self.step()[2], [('fix', 'agent')])

    def test_anything_that_needs_a_human_stops_with_the_reason(self):
        self.world(issues=[issue('AVR-900', description=UNDECIDED)])
        code, doc, steps = self.step()
        self.assertEqual((code, steps), (3, [('stop', 'cody')]))
        self.assertIn('Open Decisions', doc['steps'][0]['why'])
        self.world(issues=[issue('AVR-900', description=PARTY_ONLY, blocked_by=['AVR-1']), issue('AVR-1', state='In Progress')])
        self.assertEqual(self.step()[2], [('stop', 'cody')])
        self.world(issues=[issue('AVR-900', description='## Outcome\nNicer.\n')])   # ambiguous: sections missing
        code, doc, steps = self.step()
        self.assertEqual((code, steps), (3, [('stop', 'cody')]))

    def test_another_sessions_issue_and_an_unreadable_source_stop_too(self):
        self.work(owner='codex-bbbb')
        self.world(issues=self.READY)
        code, doc, steps = self.step()
        self.assertEqual((code, steps), (3, [('stop', 'orchestrator')]))
        self.assertIn('codex-bbbb', doc['steps'][0]['why'])
        self.world(issues=None)
        self.assertEqual(self.step()[0], 4)

    def test_a_finished_issue_has_nothing_left(self):
        self.world(issues=[issue('AVR-900', state='Done', description=PARTY_ONLY)])
        self.assertEqual(self.step()[2], [('done', 'none')])


class ReconcileTests(PipelineCase):
    def actions(self, **kw):
        self.world(**kw)
        code, out, _ = run('reconcile', '--json')
        doc = json.loads(out)
        return code, [(a['issue'], a['kind'], a['who']) for a in doc['actions']], doc

    def test_merged_work_that_linear_still_calls_in_progress(self):
        merged = pr('avrana-party', 70, 'feat/avr-900-x', state='MERGED', merged='2026-10-04T00:00:00Z')
        path = self.add_worktree('party', 'feat/avr-900-x', 'wt900')
        run('claim', '--path', str(path), '--owner', 'claude-aaaa')
        code, acts, doc = self.actions(issues=[issue('AVR-900', state='In Progress')], party=[merged])
        self.assertEqual(code, 0)
        self.assertIn(('AVR-900', 'linear-status', 'orchestrator'), acts)
        self.assertIn(('AVR-900', 'release-claim', 'agent'), acts)
        self.assertTrue(any('In Review or Done' in a['text'] for a in doc['actions']))

    def test_open_pr_or_claimed_work_on_an_issue_linear_has_not_started(self):
        self.work()
        code, acts, _ = self.actions(issues=[issue('AVR-900', state='Todo')])
        self.assertEqual(acts, [('AVR-900', 'linear-status', 'orchestrator')])
        code, acts, doc = self.actions(issues=[issue('AVR-900', state='Todo')], party=[pr('avrana-party', 70, 'feat/avr-900-x')])
        self.assertTrue(any('In Review' in a['text'] for a in doc['actions']))

    def test_a_closed_issue_with_live_work_goes_to_the_owner(self):
        self.work()
        code, acts, _ = self.actions(issues=[issue('AVR-900', state='Done')], party=[pr('avrana-party', 70, 'feat/avr-900-x')])
        self.assertIn(('AVR-900', 'closed-with-open-pr', 'cody'), acts)
        self.assertIn(('AVR-900', 'release-claim', 'agent'), acts)

    def test_consistent_state_needs_nothing(self):
        self.work()
        code, acts, _ = self.actions(issues=[issue('AVR-900', state='In Progress')])
        self.assertEqual((code, acts), (0, []))

    def test_unreadable_sources_are_exit_4_and_an_unknown_issue_is_listed_not_guessed(self):
        self.work()
        code, acts, doc = self.actions(issues=None)
        self.assertEqual(code, 4)
        code, acts, doc = self.actions(issues=[issue('AVR-1', state='Todo')])
        self.assertEqual((code, acts), (4, []))
        self.assertTrue(any('AVR-900' in u['reason'] for u in doc['unavailable']))


if __name__ == '__main__':
    import unittest
    unittest.main()
