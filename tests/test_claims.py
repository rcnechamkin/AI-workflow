from datetime import timedelta
from pathlib import Path
import threading
import unittest

from support import RepoCase, git

from ai_workflow import claims, gitio


class ClaimTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.wt = self.add_worktree('party', 'feat/avr-900-thing', 'avrana-party.wt-avr900')
        self.file = claims.claim_file(gitio.common_dir(self.wt), self.wt)
        self.now = claims.utcnow()

    def take(self, owner, **kw):
        kw.setdefault('busy', lambda: gitio.busy(self.wt))
        return claims.acquire(self.file, self.wt, owner, issue='AVR-900', **kw)

    def test_second_session_is_refused_and_told_who_owns_it(self):
        self.assertEqual(self.take('claude-aaaa1111').code, 'claimed')
        second = self.take('codex-bbbb2222')
        self.assertFalse(second.ok)
        self.assertEqual(second.code, 'refused-held')
        self.assertIn('claude-aaaa1111', second.message)
        self.assertEqual(claims.read(self.file)['owner'], 'claude-aaaa1111')

    def test_simultaneous_claims_have_exactly_one_winner(self):
        results = []
        go = threading.Barrier(8)

        def attempt(i):
            go.wait()
            results.append(self.take(f'agent-{i}'))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual(claims.read(self.file)['owner'], next(r.claim['owner'] for r in results if r.ok))

    def test_owner_reclaiming_refreshes(self):
        self.take('claude-a', now=self.now)
        again = self.take('claude-a', now=self.now + timedelta(hours=1))
        self.assertEqual(again.code, 'refreshed')
        self.assertEqual(again.claim['claimed_at'], claims.stamp(self.now))

    def test_stale_claim_on_a_clean_worktree_is_recovered(self):
        self.take('claude-a', now=self.now)
        later = self.now + timedelta(hours=7)
        self.assertEqual(claims.state(claims.read(self.file), later), 'stale')
        got = self.take('codex-b', now=later)
        self.assertEqual((got.ok, got.code), (True, 'taken-stale'))
        self.assertEqual(got.claim['previous_owner'], 'claude-a')

    def test_stale_claim_over_uncommitted_work_needs_an_owner_decision(self):
        self.take('claude-a', now=self.now)
        (self.wt / 'README.md').write_text('half-finished\n', encoding='utf-8')
        later = self.now + timedelta(hours=7)
        got = self.take('codex-b', now=later)
        self.assertEqual((got.ok, got.code), (False, 'refused-stale-busy'))
        self.assertIn('uncommitted', got.message)
        forced = self.take('codex-b', now=later, force=True)
        self.assertEqual((forced.code, forced.claim['previous_owner'], forced.claim['forced']), ('forced', 'claude-a', True))

    def test_stale_claim_during_a_merge_is_not_taken(self):
        self.take('claude-a', now=self.now)
        (Path(gitio.git_dir(self.wt)) / 'MERGE_HEAD').write_text('0' * 40 + '\n', encoding='utf-8')
        got = self.take('codex-b', now=self.now + timedelta(hours=7))
        self.assertEqual(got.code, 'refused-stale-busy')
        self.assertIn('merge in progress', got.message)

    def test_heartbeat_keeps_a_claim_alive(self):
        self.take('claude-a', now=self.now)
        self.assertTrue(claims.heartbeat(self.file, 'claude-a', now=self.now + timedelta(hours=5)))
        self.assertEqual(claims.state(claims.read(self.file), self.now + timedelta(hours=10)), 'held')
        self.assertFalse(claims.heartbeat(self.file, 'codex-b', now=self.now))

    def test_explicit_handoff_to_a_named_session(self):
        self.take('claude-a')
        self.assertFalse(claims.handoff(self.file, 'codex-b').ok)
        self.assertTrue(claims.handoff(self.file, 'claude-a', to='codex-b', note='tests red in test_x').ok)
        self.assertEqual(claims.state(claims.read(self.file)), 'handoff')
        self.assertEqual(self.take('claude-c').code, 'refused-held')
        got = self.take('codex-b')
        self.assertEqual((got.code, got.claim['previous_owner'], got.claim['note']),
                         ('taken-handoff', 'claude-a', 'tests red in test_x'))
        self.assertNotIn('handoff_to', got.claim)

    def test_handoff_to_any(self):
        self.take('claude-a')
        claims.handoff(self.file, 'claude-a')
        self.assertEqual(self.take('codex-z').code, 'taken-handoff')

    def test_only_the_owner_releases(self):
        self.take('claude-a')
        self.assertEqual(claims.release(self.file, 'codex-b').code, 'refused-not-owner')
        self.assertEqual(claims.release(self.file, 'claude-a').code, 'released')
        self.assertIsNone(claims.read(self.file))
        self.assertEqual(claims.release(self.file, 'claude-a').code, 'not-claimed')
        self.assertEqual(self.take('codex-b').code, 'claimed')

    def test_unreadable_claim_is_never_treated_as_free(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text('<<<<<<< HEAD\n', encoding='utf-8')
        self.assertEqual(claims.state(claims.read(self.file)), 'corrupt')
        self.assertEqual(self.take('claude-a').code, 'refused-corrupt')
        self.assertEqual(self.take('claude-a', force=True).code, 'forced')

    def test_claims_are_per_worktree_local_and_uncommitted(self):
        other = self.add_worktree('party', 'fix/avr-901-other', 'avrana-party.wt-avr901')
        other_file = claims.claim_file(gitio.common_dir(other), other)
        self.assertNotEqual(other_file, self.file)
        self.assertEqual(other_file.parent, self.file.parent)          # one directory per repository
        self.take('claude-a')
        self.assertTrue(claims.acquire(other_file, other, 'codex-b').ok)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')      # nothing to commit, in any worktree
        self.assertEqual(git(self.repo('party'), 'status', '--porcelain'), '')
        self.assertEqual(set(claims.read(self.file)), {'schema', 'worktree', 'repo', 'branch', 'issue', 'owner', 'agent',
                                                       'claimed_at', 'heartbeat_at', 'ttl_hours', 'note'})


if __name__ == '__main__':
    unittest.main()
