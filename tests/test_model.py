import unittest

from support import GAMES_ONLY, NO_LINEAR, NO_PI, PARTY_ONLY, READY, UNDECIDED, RepoCase, gh, issue, linear_of, pr

from ai_workflow import claims, gitio, model, sources


class SourceTests(unittest.TestCase):
    def test_ci_state(self):
        done = lambda c: {'status': 'COMPLETED', 'conclusion': c}                      # noqa: E731
        self.assertEqual(sources.ci_state([]), 'none')
        self.assertEqual(sources.ci_state(None), 'none')
        self.assertEqual(sources.ci_state([done('SUCCESS'), done('SKIPPED')]), 'passed')
        self.assertEqual(sources.ci_state([done('SUCCESS'), {'status': 'IN_PROGRESS', 'conclusion': ''}]), 'running')
        self.assertEqual(sources.ci_state([{'status': 'QUEUED'}]), 'running')
        self.assertEqual(sources.ci_state([{'status': 'IN_PROGRESS'}, done('FAILURE')]), 'failed')
        self.assertEqual(sources.ci_state([{'state': 'PENDING'}]), 'running')          # a commit status, not a check run
        self.assertEqual(sources.ci_state([done(None)]), 'running')                    # unknown is never a pass

    def test_gh_failure_is_unavailable_not_empty(self):
        res = sources.prs('party', 'o/avrana-party', run=gh({}, fail={'o/avrana-party'}))
        self.assertFalse(res['available'])
        self.assertIn('api.github.com', res['reason'])
        self.assertFalse(sources.prs('party', 'o/x', run=lambda cmd, timeout=40: (127, '', 'FileNotFoundError'))['available'])
        self.assertFalse(sources.prs('party', 'o/x', run=lambda cmd, timeout=40: (0, 'not json', ''))['available'])
        self.assertFalse(sources.prs('party', 'o/x', run=lambda cmd, timeout=40: (0, '[{"title": "no number"}]', ''))['available'])

    def test_issue_search_keeps_only_prs_naming_the_issue(self):
        rows = [pr('party', 1, 'feat/avr-23-x'), pr('party', 2, 'feat/avr-237-y'), pr('party', 3, 'docs/z', title='Follow-up (AVR-23)')]
        res = sources.prs('party', 'o/avrana-party', 'AVR-23', run=gh({'o/avrana-party': rows}))
        self.assertEqual([p['number'] for p in res['data']], [1, 3])
        self.assertEqual([p['issue'] for p in res['data']], ['AVR-23', 'AVR-23'])

    def test_pr_facts(self):
        rows = [pr('party', 1, 'feat/avr-1-x', review='REVIEW_REQUIRED'), pr('party', 2, 'feat/avr-2-y', mergeable='UNKNOWN'),
                pr('party', 3, 'feat/avr-3-z', state='MERGED', merged='2026-10-03T00:00:00Z'), pr('party', 4, 'feat/avr-4-q')]
        got = {p['number']: p for p in sources.prs('party', 'o/avrana-party', run=gh(
            {'o/avrana-party': rows}, behind={'feat/avr-1-x': 4, 'feat/avr-4-q': None}))['data']}
        self.assertEqual((got[1]['behind_main'], got[1]['review_required'], got[1]['conflicting']), (4, True, False))
        self.assertIsNone(got[2]['conflicting'])                 # GitHub has not computed it: unknown, not "fine"
        self.assertEqual((got[3]['state'], got[3]['behind_main']), ('merged', None))
        self.assertIsNone(got[4]['behind_main'])                 # compare failed: unknown, not zero

    def test_open_decisions(self):
        self.assertEqual(sources.open_decisions(None), 'unknown')
        self.assertEqual(sources.open_decisions('## Outcome\nx'), 'missing')
        self.assertEqual(sources.open_decisions(READY), 'none')
        self.assertEqual(sources.open_decisions(UNDECIDED), 'unresolved')

    def test_pi_unreachable_is_unavailable(self):
        self.assertFalse(sources.pi_status('http://127.0.0.1:9/status', timeout=2)['available'])
        self.assertFalse(sources.pi_status(None)['available'])


class ReadinessTests(RepoCase):
    def context(self, ident, linear, party=(), games=(), pi=NO_PI, fail=(), me=None, **kw):
        run = gh({'o/avrana-party': list(party), 'o/avrana-party-games': list(games)}, fail=fail)
        prs = {n: sources.prs(n, r['slug'], ident, run=run) for n, r in self.cfg['repos'].items()}
        return model.issue_context(self.cfg, ident, model.workspace(self.cfg), linear, prs, pi, me=me, **kw)

    def state(self, *a, **kw):
        r = self.context(*a, **kw)['readiness']
        return r['state'], r['can_start']

    def test_ready_single_repo_issue(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=PARTY_ONLY, project='Avrana Party',
                                                      projectMilestone={'name': 'M6'}, parentId='AVR-224')))
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('ready-for-agent', True))
        self.assertEqual(ctx['repositories'], {'value': ['party'], 'basis': 'Repositories section of the issue'})
        self.assertEqual((ctx['linear']['project'], ctx['linear']['milestone'], ctx['linear']['parent']), ('Avrana Party', 'M6', 'AVR-224'))
        self.assertEqual(ctx['linear']['sections']['Tests Required'], 'z')
        self.assertIsNone(ctx['deployed'])
        self.assertIn('pi', [u['source'] for u in ctx['unavailable']])

    def test_repository_detection(self):
        self.assertEqual(self.context('AVR-1', linear_of(issue('AVR-1', description=READY)))['repositories']['value'], ['games', 'party'])
        self.assertEqual(self.context('AVR-1', linear_of(issue('AVR-1', description=GAMES_ONLY)))['repositories']['value'], ['games'])
        self.assertEqual(self.context('AVR-1', linear_of(issue('AVR-1', description=READY)), repos_override=['party'])['repositories']['value'], ['party'])

    def test_unresolved_open_decisions_need_cody(self):
        self.assertEqual(self.state('AVR-900', linear_of(issue('AVR-900', description=UNDECIDED))), ('needs-cody', False))

    def test_backlog_needs_cody(self):
        self.assertEqual(self.state('AVR-900', linear_of(issue('AVR-900', state='Backlog', description=READY))), ('needs-cody', False))

    def test_missing_linear_is_unknown_never_ready(self):
        ctx = self.context('AVR-900', NO_LINEAR)
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('unknown', False))
        self.assertIsNone(ctx['linear'])
        self.assertIn('linear', [u['source'] for u in ctx['unavailable']])
        self.assertEqual(self.state('AVR-901', linear_of(issue('AVR-900', description=READY))), ('unknown', False))   # not in the read

    def test_missing_github_is_unknown_not_no_prs(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=READY)), fail={'o/avrana-party-games'})
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('unknown', False))
        self.assertIn('GitHub pull-request state', ctx['readiness']['missing'])
        self.assertIn('github:games', [u['source'] for u in ctx['unavailable']])

    def test_issue_without_template_sections_is_unknown(self):
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description='## Outcome\njust this')))
        self.assertEqual(ctx['readiness']['state'], 'unknown')
        self.assertEqual(len(ctx['readiness']['missing']), 2)             # Open Decisions and Repositories
        confirmed = self.context('AVR-900', linear_of(issue('AVR-900', description='## Outcome\njust this')),
                                 repos_override=['party'], decisions_confirmed=True)
        self.assertEqual(confirmed['readiness']['state'], 'ready-for-agent')

    def test_blocked_by_an_unfinished_dependency(self):
        linear = linear_of(issue('AVR-900', description=READY, blocked_by=['AVR-800', 'AVR-801']),
                           issue('AVR-800', state='Done'), issue('AVR-801', state='Todo'))
        ctx = self.context('AVR-900', linear)
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['reasons']), ('blocked', ['blocked by AVR-801 (Todo)']))

    def test_dependency_of_unknown_state_is_incomplete_evidence(self):
        linear = linear_of(issue('AVR-900', description=READY, blocked_by=['AVR-802']))
        ctx = self.context('AVR-900', linear)
        self.assertEqual(ctx['readiness']['state'], 'unknown')
        self.assertIn('the state of dependency AVR-802', ctx['readiness']['missing'])
        no_relations = dict(issue('AVR-900', description=READY))
        del no_relations['relations']
        self.assertEqual(self.state('AVR-900', linear_of(no_relations)), ('unknown', False))

    def test_done_canceled_duplicate(self):
        for state in ('Done', 'Canceled', 'Duplicate'):
            self.assertEqual(self.state('AVR-900', linear_of(issue('AVR-900', state=state, description=READY))), ('done', False))

    def test_open_pr_means_pr_ci_whatever_linear_says(self):
        linear = linear_of(issue('AVR-900', state='Todo', description=READY))
        self.assertEqual(self.state('AVR-900', linear, party=[pr('party', 5, 'feat/avr-900-x', checks=[('IN_PROGRESS', '')])]), ('pr-ci', False))
        self.assertEqual(self.state('AVR-900', linear_of(issue('AVR-900', state='In Review', description=READY)),
                                    party=[pr('party', 5, 'feat/avr-900-x', state='MERGED', merged='x')]), ('pr-ci', False))

    def test_ready_for_playtest(self):
        linear = linear_of(issue('AVR-900', state='In Review', labels=['Human Validation'], description=READY))
        self.assertEqual(self.state('AVR-900', linear), ('ready-for-playtest', False))

    def test_paired_repository_work(self):
        self.add_worktree('party', 'feat/avr-900-envelope', 'avrana-party.wt-avr900')
        self.add_worktree('games', 'feat/avr-900-envelope', 'avrana-party-games.wt-avr900')
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', state='In Review', description=READY)),
                           party=[pr('party', 54, 'feat/avr-900-envelope')],
                           games=[pr('games', 29, 'feat/avr-900-envelope', checks=[('IN_PROGRESS', '')])])
        self.assertEqual(sorted(t['repo'] for t in ctx['worktrees']), ['games', 'party'])
        self.assertEqual(len(ctx['prs']), 1)
        self.assertTrue(ctx['prs'][0]['paired'])
        self.assertEqual({p['repo']: p['ci'] for p in ctx['prs'][0]['prs']}, {'party': 'passed', 'games': 'running'})

    def test_prs_pair_by_issue_when_branch_names_differ(self):
        groups = model.pair([sources.normalize_pr('party', pr('party', 1, 'feat/avr-900-core')),
                             sources.normalize_pr('games', pr('games', 2, 'feat/avr-900-provider')),
                             sources.normalize_pr('party', pr('party', 3, 'fix/avr-901-x'))])
        self.assertEqual([(g['paired'], len(g['prs'])) for g in groups], [(True, 2), (False, 1)])

    def test_another_sessions_claim_means_in_progress_and_no_start(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        claims.acquire(claims.claim_file(gitio.common_dir(wt), wt), wt, 'claude-aaaa', issue='AVR-900')
        linear = linear_of(issue('AVR-900', description=PARTY_ONLY))
        ctx = self.context('AVR-900', linear, me='codex-b')
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('in-progress', False))
        self.assertIn('claimed by claude-aaaa', ctx['readiness']['reasons'][0])
        self.assertEqual(self.state('AVR-900', linear, me=None), ('in-progress', False))        # no identity is not the owner
        self.assertEqual(self.state('AVR-900', linear, me='claude-aaaa'), ('in-progress', True))  # the owner resumes

    def test_refusal_decided_by_linear_state_still_names_the_session_holding_the_worktree(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        claims.acquire(claims.claim_file(gitio.common_dir(wt), wt), wt, 'claude-aaaa', issue='AVR-900')
        for state, want in (('Backlog', 'needs-cody'), ('Done', 'done'), ('In Review', 'pr-ci')):
            r = self.context('AVR-900', linear_of(issue('AVR-900', state=state, description=PARTY_ONLY)), me='codex-b')['readiness']
            self.assertEqual(r['state'], want)
            self.assertTrue(any('claimed by claude-aaaa' in x for x in r['reasons']), (state, r['reasons']))
            mine = self.context('AVR-900', linear_of(issue('AVR-900', state=state, description=PARTY_ONLY)), me='claude-aaaa')['readiness']
            self.assertFalse(any('claimed by' in x for x in mine['reasons']), state)
        undecided = self.context('AVR-900', linear_of(issue('AVR-900', description=UNDECIDED)), me='codex-b')['readiness']
        self.assertEqual(undecided['state'], 'needs-cody')
        self.assertTrue(any('claimed by claude-aaaa' in x for x in undecided['reasons']))
        held = self.context('AVR-900', linear_of(issue('AVR-900', description=PARTY_ONLY)), me='codex-b')['readiness']
        self.assertEqual(sum('claimed by claude-aaaa' in x for x in held['reasons']), 1)      # said once, not twice

    def test_unfinished_work_in_an_existing_worktree(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        (wt / 'README.md').write_text('half\n', encoding='utf-8')
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=PARTY_ONLY)), me='codex-b')
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('in-progress', True))
        self.assertIn('uncommitted', ctx['readiness']['reasons'][0])

    def test_abandoned_dirty_worktree_is_not_resumable_by_an_agent(self):
        wt = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        claims.acquire(claims.claim_file(gitio.common_dir(wt), wt), wt, 'claude-gone', now=claims.utcnow().replace(year=2020))
        (wt / 'README.md').write_text('half\n', encoding='utf-8')
        ctx = self.context('AVR-900', linear_of(issue('AVR-900', description=PARTY_ONLY)), me='codex-b')
        self.assertEqual((ctx['readiness']['state'], ctx['readiness']['can_start']), ('in-progress', False))
        self.assertIn('owner decision', ctx['readiness']['reasons'][0])

    def test_pi_states(self):
        games_main = gitio.rev(self.repo('games'), 'origin/main')
        pi = sources.ok({'party': {'deployed_sha': '0' * 40}, 'games': {'deployed_sha': games_main}, 'summary': {'state': 'ok'}})
        ctx = self.context('AVR-900', NO_LINEAR, pi=pi)
        self.assertEqual(ctx['deployed'], {'party': {'sha': '0' * 40, 'matches_main': False}, 'games': {'sha': games_main, 'matches_main': True}})
        self.assertIsNone(self.context('AVR-900', NO_LINEAR)['deployed'])

    def test_missing_checkout_is_unavailable(self):
        cfg = {**self.cfg, 'repos': {**self.cfg['repos'], 'games': {'slug': 'o/g', 'dir': 'nope', 'path': str(self.root / 'nope')}}}
        ws = model.workspace(cfg)
        self.assertFalse(ws['games']['available'])
        self.assertTrue(ws['party']['available'])


class NeedsCodyTests(RepoCase):
    def queue(self, linear, party=(), games=(), pi=NO_PI, fail=(), behind=None):
        run = gh({'o/avrana-party': list(party), 'o/avrana-party-games': list(games)}, fail=fail, behind=behind)
        prs = {n: sources.prs(n, r['slug'], run=run) for n, r in self.cfg['repos'].items()}
        return model.needs_cody(self.cfg, model.workspace(self.cfg), linear, prs, pi)

    def kinds(self, queue, bucket='needs_cody'):
        return [(i['kind'], i['ref']) for i in queue[bucket]]

    def test_routine_pr_states_are_agent_work_and_only_green_prs_reach_cody(self):
        queue = self.queue(NO_LINEAR, party=[
            pr('party', 1, 'fix/avr-1-a', checks=[('COMPLETED', 'FAILURE')]), pr('party', 2, 'fix/avr-2-b'),
            pr('party', 3, 'fix/avr-3-c', checks=[('IN_PROGRESS', '')]), pr('party', 4, 'fix/avr-4-d', checks=[]),
            pr('party', 5, 'fix/avr-5-e', draft=True), pr('party', 6, 'fix/avr-6-f', mergeable='CONFLICTING')], behind={'fix/avr-2-b': 3})
        self.assertEqual(self.kinds(queue), [('review', 'o/avrana-party#2')])
        self.assertIn('3 behind main', queue['needs_cody'][0]['text'])
        self.assertEqual({i['ref'].split('#')[1]: i['kind'] for i in queue['agent']},
                         {'1': 'ci-failed', '3': 'ci-running', '4': 'ci-none', '5': 'draft', '6': 'conflict'})

    def test_paired_prs_are_one_merge_decision_and_wait_for_both(self):
        both = self.queue(NO_LINEAR, party=[pr('party', 54, 'feat/avr-900-x')], games=[pr('games', 29, 'feat/avr-900-x')])
        self.assertEqual(self.kinds(both), [('review', 'o/avrana-party#54 + o/avrana-party-games#29')])
        self.assertIn('merge Party first', both['needs_cody'][0]['text'])
        half = self.queue(NO_LINEAR, party=[pr('party', 54, 'feat/avr-900-x')],
                          games=[pr('games', 29, 'feat/avr-900-x', checks=[('COMPLETED', 'FAILURE')])])
        self.assertEqual(self.kinds(half), [])
        self.assertEqual(self.kinds(half, 'agent'), [('ci-failed', 'o/avrana-party#54 + o/avrana-party-games#29')])

    def test_merged_and_closed_prs_are_not_in_the_queue(self):
        queue = self.queue(NO_LINEAR, party=[pr('party', 7, 'feat/avr-900-x', state='MERGED', merged='x'), pr('party', 8, 'feat/avr-901-x', state='CLOSED')])
        self.assertEqual((queue['needs_cody'], queue['agent']), ([], []))

    def test_decisions_playtests_and_conflicting_sources(self):
        linear = linear_of(issue('AVR-10', description=UNDECIDED), issue('AVR-212', state='In Review', labels=['Human Validation'], description=READY),
                           issue('AVR-213', state='In Review', description=READY), issue('AVR-300', state='Done', description=READY),
                           issue('AVR-14', description='## Outcome\nx'), issue('AVR-15', state='Done', description=UNDECIDED))
        queue = self.queue(linear, party=[pr('party', 9, 'feat/avr-300-x')])
        self.assertEqual(self.kinds(queue), [('review', 'o/avrana-party#9'), ('conflict', 'AVR-300'), ('decision', 'AVR-10'), ('playtest', 'AVR-212')])
        self.assertEqual(self.kinds(queue, 'agent'), [('issue-hygiene', 'AVR-14')])

    def test_unavailable_sources_are_listed_and_never_an_empty_queue(self):
        queue = self.queue(NO_LINEAR, party=[pr('party', 1, 'feat/avr-1-x')], fail={'o/avrana-party-games'})
        self.assertEqual({u['source'] for u in queue['unavailable']}, {'linear', 'pi', 'github:games'})
        self.assertEqual(self.kinds(queue), [('review', 'o/avrana-party#1')])
        self.assertFalse([i for i in queue['needs_cody'] if i['kind'] == 'deploy'])

    def test_issue_read_without_description_is_not_assumed_decided(self):
        queue = self.queue(linear_of(issue('AVR-7', description=None), issue('AVR-8', state='Done', description=None)))
        self.assertEqual(queue['needs_cody'], [])
        reason = next(u['reason'] for u in queue['unavailable'] if u['source'] == 'linear:open-decisions')
        self.assertIn('AVR-7', reason)
        self.assertNotIn('AVR-8', reason)

    def test_deploy_ready_and_degraded_appliance(self):
        games_main = gitio.rev(self.repo('games'), 'origin/main')
        pi = sources.ok({'party': {'deployed_sha': '0' * 40}, 'games': {'deployed_sha': games_main},
                         'summary': {'state': 'degraded', 'reasons': ['certificate expiring']}})
        self.assertEqual(self.kinds(self.queue(NO_LINEAR, pi=pi)), [('deploy', 'party'), ('appliance', 'pi')])

    def test_worktree_claims_in_the_cockpit(self):
        a = self.add_worktree('party', 'feat/avr-900-x', 'avrana-party.wt-avr900')
        b = self.add_worktree('party', 'feat/avr-901-x', 'avrana-party.wt-avr901')
        c = self.add_worktree('games', 'feat/avr-902-x', 'avrana-party-games.wt-avr902')
        d = self.add_worktree('games', 'feat/avr-903-x', 'avrana-party-games.wt-avr903')
        file = lambda wt: claims.claim_file(gitio.common_dir(wt), wt)               # noqa: E731
        claims.acquire(file(a), a, 'claude-work', issue='AVR-900')
        claims.acquire(file(b), b, 'claude-gone', issue='AVR-901', now=claims.utcnow().replace(year=2020))
        claims.acquire(file(c), c, 'codex-stuck', issue='AVR-902')
        claims.handoff(file(c), 'codex-stuck', to='cody', note='two ADRs disagree about the cookie scope')
        claims.acquire(file(d), d, 'claude-old', issue='AVR-903', now=claims.utcnow().replace(year=2020))
        (d / 'README.md').write_text('unfinished\n', encoding='utf-8')
        queue = self.queue(NO_LINEAR)
        self.assertEqual(self.kinds(queue), [('blocked-agent', 'avrana-party-games.wt-avr902'), ('takeover', 'avrana-party-games.wt-avr903')])
        self.assertIn('two ADRs disagree', queue['needs_cody'][0]['text'])
        self.assertEqual(self.kinds(queue, 'agent'), [('working', 'avrana-party.wt-avr900')])     # the stale clean claim is nobody's problem


if __name__ == '__main__':
    unittest.main()
