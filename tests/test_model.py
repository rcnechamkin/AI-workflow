import json
import unittest

from support import READY, UNDECIDED, RepoCase, issue, pr

from avrana_workflow import claims, gitio, model, sources

NO_LINEAR = sources.unavailable('no LINEAR_API_KEY and no --linear-snapshot')
NO_PI = sources.unavailable('http://127.0.0.1:9/status unreachable (URLError)')


def linear_of(*raw):
    return {**sources.ok({i['id']: i for i in map(sources.normalize_issue, raw)}), 'origin': 'test'}


def gh(rows_by_slug, fail=()):
    def run(cmd, timeout=40):
        slug = cmd[cmd.index('--repo') + 1]
        if slug in fail:
            return 1, '', 'error connecting to api.github.com'
        return 0, json.dumps(rows_by_slug.get(slug, [])), ''
    return run


class SourceTests(unittest.TestCase):
    def test_ci_state(self):
        self.assertEqual(sources.ci_state([]), 'none')
        self.assertEqual(sources.ci_state(None), 'none')
        self.assertEqual(sources.ci_state([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'status': 'COMPLETED', 'conclusion': 'SKIPPED'}]), 'passed')
        self.assertEqual(sources.ci_state([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'status': 'IN_PROGRESS', 'conclusion': ''}]), 'running')
        self.assertEqual(sources.ci_state([{'status': 'IN_PROGRESS'}, {'status': 'COMPLETED', 'conclusion': 'FAILURE'}]), 'failed')
        self.assertEqual(sources.ci_state([{'state': 'PENDING'}]), 'running')          # a commit status, not a check run
        self.assertEqual(sources.ci_state([{'status': 'COMPLETED', 'conclusion': None}]), 'running')   # unknown is never passed

    def test_gh_failure_is_unavailable_not_empty(self):
        res = sources.prs('party', 'o/avrana-party', run=gh({}, fail={'o/avrana-party'}))
        self.assertFalse(res['available'])
        self.assertIn('api.github.com', res['reason'])
        self.assertFalse(sources.prs('party', 'o/x', run=lambda cmd, timeout=40: (127, '', 'FileNotFoundError'))['available'])
        self.assertFalse(sources.prs('party', 'o/x', run=lambda cmd, timeout=40: (0, 'not json', ''))['available'])

    def test_issue_search_keeps_only_prs_naming_the_issue(self):
        rows = [pr('party', 1, 'feat/avr-23-x'), pr('party', 2, 'feat/avr-237-y'), pr('party', 3, 'docs/z', title='Follow-up (AVR-23)')]
        res = sources.prs('party', 'o/avrana-party', 'AVR-23', run=gh({'o/avrana-party': rows}))
        self.assertEqual([p['number'] for p in res['data']], [1, 3])

    def test_merged_pr_state(self):
        row = sources.normalize_pr('party', pr('party', 9, 'feat/avr-1-x', state='MERGED', merged='2026-10-03T00:00:00Z'))
        self.assertEqual(row['state'], 'merged')

    def test_linear_without_key_or_snapshot_is_unavailable(self):
        res = sources.linear('AVR-1', snapshot=None, key=None)
        self.assertFalse(res['available'])
        self.assertFalse(sources.linear('AVR-1', snapshot='does-not-exist.json')['available'])

    def test_linear_api_failure_is_unavailable(self):
        def boom(key, query, variables):
            raise OSError('network down')
        self.assertFalse(sources.linear('AVR-1', key='k', graphql=boom)['available'])

    def test_linear_api_shape(self):
        def api(key, query, variables):
            return {'issue': {'identifier': 'AVR-5', 'title': 't', 'url': 'u', 'description': READY,
                              'state': {'name': 'Todo', 'type': 'unstarted'}, 'labels': {'nodes': [{'name': 'Core'}]},
                              'inverseRelations': {'nodes': [{'type': 'blocks', 'issue': {'identifier': 'AVR-4'}},
                                                             {'type': 'related', 'issue': {'identifier': 'AVR-3'}}]}}}
        got = sources.linear('AVR-5', key='k', graphql=api)['data']['AVR-5']
        self.assertEqual((got['state'], got['labels'], got['blocked_by']), ('Todo', ['Core'], ['AVR-4']))

    def test_open_decisions(self):
        self.assertEqual(sources.open_decisions(None), 'unknown')
        self.assertEqual(sources.open_decisions('## Outcome\nx'), 'missing')
        self.assertEqual(sources.open_decisions(READY), 'none')
        self.assertEqual(sources.open_decisions(UNDECIDED), 'unresolved')

    def test_pi_unreachable_is_unavailable(self):
        self.assertFalse(sources.pi_status('http://127.0.0.1:9/status', timeout=2)['available'])


class ModelTests(RepoCase):
    def context(self, ident, linear, party_prs=(), games_prs=(), pi=NO_PI, fail=(), me=None):
        run = gh({'o/avrana-party': list(party_prs), 'o/avrana-party-games': list(games_prs)}, fail=fail)
        prs = {n: sources.prs(n, r['slug'], ident, run=run) for n, r in self.cfg['repos'].items()}
        return model.issue_context(self.cfg, ident, model.workspace(self.cfg), linear, prs, pi, me=me)

    def queue(self, linear, party_prs=(), games_prs=(), pi=NO_PI, fail=()):
        run = gh({'o/avrana-party': list(party_prs), 'o/avrana-party-games': list(games_prs)}, fail=fail)
        prs = {n: sources.prs(n, r['slug'], run=run) for n, r in self.cfg['repos'].items()}
        return model.needs_cody(self.cfg, model.workspace(self.cfg), linear, prs, pi)

    def test_ready_issue_with_no_work_started(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=READY)))
        self.assertTrue(ctx['ready'])
        self.assertEqual(ctx['linear']['lifecycle'], 'ready-for-agent')
        self.assertEqual(ctx['repositories'], {'value': ['games', 'party'], 'basis': 'Repositories section of the issue'})
        self.assertEqual(ctx['worktrees'], [])
        self.assertIsNone(ctx['deployed'])
        self.assertIn('pi', [u['source'] for u in ctx['unavailable']])

    def test_unresolved_open_decisions_block_the_issue_and_reach_cody(self):
        linear = linear_of(issue('AVR-900', description=UNDECIDED))
        ctx = self.context('AVR-900', linear)
        self.assertFalse(ctx['ready'])
        self.assertEqual(ctx['linear']['lifecycle'], 'needs-cody')
        self.assertTrue(any('Open Decisions' in b for b in ctx['blockers']))
        self.assertEqual([(i['kind'], i['ref']) for i in self.queue(linear)['needs_cody']], [('decision', 'AVR-900')])

    def test_missing_linear_is_unavailable_and_never_ready(self):
        ctx = self.context('AVR-900', NO_LINEAR)
        self.assertFalse(ctx['ready'])
        self.assertIsNone(ctx['linear'])
        self.assertIn('linear', [u['source'] for u in ctx['unavailable']])
        queue = self.queue(NO_LINEAR)
        self.assertEqual(queue['needs_cody'], [])
        self.assertEqual({u['source'] for u in queue['unavailable']}, {'linear', 'pi'})

    def test_missing_github_is_unavailable_not_no_prs(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=READY)), fail={'o/avrana-party-games'})
        self.assertIn('github:games', [u['source'] for u in ctx['unavailable']])
        queue = self.queue(NO_LINEAR, party_prs=[pr('party', 1, 'feat/avr-1-x')], fail={'o/avrana-party-games'})
        self.assertIn('github:games', [u['source'] for u in queue['unavailable']])
        self.assertEqual([i['ref'] for i in queue['needs_cody']], ['o/avrana-party#1'])

    def test_issue_without_template_sections_is_not_called_ready_for_agent(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description='## Outcome\njust this')))
        self.assertEqual(ctx['linear']['lifecycle'], 'todo-unverified')
        self.assertEqual(ctx['repositories']['value'], None)
        self.assertTrue(any('no Open Decisions section' in n for n in ctx['notes']))

    def test_unfinished_dependency_blocks(self):
        linear = linear_of(issue('AVR-900', description=READY, blocked_by=['AVR-800', 'AVR-801', 'AVR-802']),
                           issue('AVR-800', state='Done'), issue('AVR-801', state='Todo'))
        ctx = self.context('AVR-900', linear)
        self.assertEqual([b for b in ctx['blockers']], ['blocked by AVR-801 (Todo)'])
        self.assertTrue(any('AVR-802' in n and 'unavailable' in n for n in ctx['notes']))

    def test_paired_repository_work(self):
        self.add_worktree('party', 'feat/avr-900-envelope', 'avrana-party.wt-avr900')
        self.add_worktree('games', 'feat/avr-900-envelope', 'avrana-party-games.wt-avr900')
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', state='In Review', description=READY)),
                           party_prs=[pr('party', 54, 'feat/avr-900-envelope')],
                           games_prs=[pr('games', 29, 'feat/avr-900-envelope', checks=[('IN_PROGRESS', '')])])
        self.assertEqual(sorted(t['repo'] for t in ctx['worktrees']), ['games', 'party'])
        self.assertEqual(len(ctx['prs']), 1)
        self.assertTrue(ctx['prs'][0]['paired'])
        self.assertEqual({p['repo']: p['ci'] for p in ctx['prs'][0]['prs']}, {'party': 'passed', 'games': 'running'})
        self.assertEqual(ctx['linear']['lifecycle'], 'pr-ci')

    def test_worktree_claimed_by_another_session_blocks(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        claims.acquire(claims.claim_file(gitio.common_dir(wt), wt), wt, 'claude-aaaa', issue='AVR-900')
        linear = linear_of(issue('AVR-900', description=READY))
        self.assertTrue(any('claimed by claude-aaaa' in b for b in self.context('AVR-900', linear, me='codex-b')['blockers']))
        self.assertTrue(self.context('AVR-900', linear, me='claude-aaaa')['ready'])

    def test_ci_failure_is_agent_work_and_ci_pass_is_codys_review(self):
        queue = self.queue(NO_LINEAR, party_prs=[
            pr('party', 1, 'fix/avr-1-a', checks=[('COMPLETED', 'FAILURE')]),
            pr('party', 2, 'fix/avr-2-b'),
            pr('party', 3, 'fix/avr-3-c', checks=[('IN_PROGRESS', '')]),
            pr('party', 4, 'fix/avr-4-d', checks=[]),
            pr('party', 5, 'fix/avr-5-e', draft=True),
            pr('party', 6, 'fix/avr-6-f', mergeable='CONFLICTING')])
        self.assertEqual([(i['kind'], i['ref']) for i in queue['needs_cody']], [('review', 'o/avrana-party#2')])
        self.assertEqual({i['ref'].split('#')[1]: i['kind'] for i in queue['agent']},
                         {'1': 'ci-failed', '3': 'ci-running', '4': 'ci-none', '5': 'draft', '6': 'conflict'})

    def test_merged_pr_is_not_in_the_queue_and_shows_as_merged(self):
        merged = pr('party', 7, 'feat/avr-900-x', state='MERGED', merged='2026-10-03T00:00:00Z')
        self.assertEqual(self.queue(NO_LINEAR, party_prs=[merged])['needs_cody'], [])
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', state='Done', description=READY)), party_prs=[merged])
        self.assertEqual(ctx['prs'][0]['prs'][0]['state'], 'merged')
        self.assertEqual(ctx['linear']['lifecycle'], 'done')

    def test_playtest_reaches_cody(self):
        linear = linear_of(issue('AVR-212', state='In Review', labels=['Human Validation'], description=READY),
                           issue('AVR-213', state='In Review', description=READY))
        self.assertEqual([(i['kind'], i['ref']) for i in self.queue(linear)['needs_cody']], [('playtest', 'AVR-212')])

    def test_issue_read_without_description_is_not_assumed_decided(self):
        queue = self.queue(linear_of(issue('AVR-7', description=None), issue('AVR-8', state='Done', description=None)))
        self.assertEqual(queue['needs_cody'], [])
        reason = next(u['reason'] for u in queue['unavailable'] if u['source'] == 'linear:open-decisions')
        self.assertIn('AVR-7', reason)
        self.assertNotIn('AVR-8', reason)

    def test_pi_unavailable_never_reports_deployed_or_asks_for_a_deploy(self):
        queue = self.queue(NO_LINEAR)
        self.assertIn('pi', [u['source'] for u in queue['unavailable']])
        self.assertFalse([i for i in queue['needs_cody'] if i['kind'] == 'deploy'])

    def test_pi_behind_main_is_a_deploy_decision(self):
        main = gitio.rev(self.repo('party'), 'origin/main')
        games_main = gitio.rev(self.repo('games'), 'origin/main')
        pi = sources.ok({'party': {'deployed_sha': '0' * 40}, 'games': {'deployed_sha': games_main}, 'summary': {'state': 'ok'}})
        queue = self.queue(NO_LINEAR, pi=pi)
        self.assertEqual([(i['kind'], i['ref']) for i in queue['needs_cody']], [('deploy', 'party')])
        ctx = self.context('AVR-900', NO_LINEAR, pi=pi)
        self.assertEqual(ctx['deployed'], {'party': {'sha': '0' * 40, 'matches_main': False}, 'games': {'sha': games_main, 'matches_main': True}})
        self.assertNotEqual(main, '0' * 40)

    def test_stale_claim_over_unfinished_work_reaches_cody(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        old = claims.utcnow().replace(year=2020)
        claims.acquire(claims.claim_file(gitio.common_dir(wt), wt), wt, 'claude-gone', now=old)
        self.assertEqual(self.queue(NO_LINEAR)['needs_cody'], [])          # stale but clean: any agent may recover it
        (wt / 'README.md').write_text('unfinished\n', encoding='utf-8')
        self.assertEqual([i['kind'] for i in self.queue(NO_LINEAR)['needs_cody']], ['takeover'])

    def test_missing_checkout_is_unavailable(self):
        cfg = {**self.cfg, 'repos': {**self.cfg['repos'], 'games': {'slug': 'o/g', 'dir': 'nope', 'path': str(self.root / 'nope')}}}
        ws = model.workspace(cfg)
        self.assertFalse(ws['games']['available'])
        self.assertTrue(ws['party']['available'])


if __name__ == '__main__':
    unittest.main()
