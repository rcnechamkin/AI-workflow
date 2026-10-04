"""The context manifest: explicit references first, canonical sources next, exact matches, then
Graphify (verified, never authoritative), then ordinary search; within a budget; with provenance."""
import json
import os
from pathlib import Path
import unittest

from support import RepoCase, git, issue, run

from ai_workflow import context, model

ISSUE = '''## Outcome

In missions 10 and 13 the captain decides. See ADR 0015.

## Change

Only for `captain_one`, in `games/expo/engine.py`.

## Tests Required

* Update `test_only_the_captain_may_offer`.

## Documents

`ACTIONS.md` crew decisions.

## Repositories

avrana-party-games

## Open Decisions

None
'''

GAMES = {
    'AGENTS.md': '# Games agents\n',
    'games/expo/engine.py': 'MODE = "captain_one"\n\ndef allocate(mode):\n    return mode == "captain_one"\n',
    'games/expo/docs/ACTIONS.md': '# Actions\n\nCrew decisions for captain_one.\n',
    'games/expo/docs/MISSION_MODEL.md': '# Mission model\n\nAllocation modes: captain_one.\n',
    'games/expo/client.js': '// decision panel for captain decides in missions\nconst mode = "captain_one";\n',
    'games/bluff/engine.py': 'def bluff():\n    return 1\n',
    'tests/test_expo_engine.py': 'def test_only_the_captain_may_offer():\n    assert True\n\ndef test_other():\n    assert "captain_one"\n',
    'tests/test_expo_rules.py': 'def test_rules():\n    assert True\n',
    'tests/test_bluff.py': 'def test_bluff():\n    assert True\n',
    'docs/manifest.json': json.dumps({'documents': [
        {'path': 'AGENTS.md', 'class': 'canonical', 'status': 'current'},
        {'path': 'games/expo/docs/ACTIONS.md', 'class': 'canonical', 'status': 'current'},
        {'path': 'games/expo/docs/MISSION_MODEL.md', 'class': 'canonical', 'status': 'current'},
        {'path': 'docs/notes/old-captain.md', 'class': 'historical', 'status': 'historical'}]}),
    'docs/notes/old-captain.md': '# Old\n\ncaptain_one used to work differently.\n',
}
PARTY = {
    'AGENTS.md': '# Party agents\n',
    'docs/adr/0015-game-result-envelope.md': '# ADR 0015\n\nStatus: **accepted**\n',
    'docs/adr/0016-service-identities.md': '# ADR 0016\n\nStatus: **proposed**\n',
    'docs/manifest.json': json.dumps({'documents': [
        {'path': 'docs/adr/0015-game-result-envelope.md', 'class': 'decision', 'status': 'current'},
        {'path': 'docs/adr/0016-service-identities.md', 'class': 'decision', 'status': 'current'}]}),
    'avrana/party/core.py': 'def captain():\n    return 1\n',
}


def graph(built, nodes):
    return json.dumps({'built_at_commit': built, 'nodes': [
        {'id': f'n{i}', 'label': label, 'norm_label': label.lower(), 'source_file': path, 'source_location': 'L1', 'description': desc}
        for i, (label, path, desc) in enumerate(nodes)], 'links': []})


class ContextCase(RepoCase):
    def seed(self, name, files, message='seed'):
        repo = self.repo(name)
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', message)
        git(repo, 'push', '-q', 'origin', 'main')
        return git(repo, 'rev-parse', 'HEAD')

    def setUp(self):
        super().setUp()
        os.environ['AI_WORKFLOW_STATE'] = str(self.root / 'state')
        self.games_sha = self.seed('games', GAMES)
        self.party_sha = self.seed('party', PARTY)

    def manifest(self, description=ISSUE, title='EXPO: in missions 10 and 13 the captain decides', repos=('games',), **kw):
        raw = issue('AVR-900', description=description)
        raw['title'] = title
        from ai_workflow import sources
        return context.build(self.cfg, sources.normalize_issue(raw), list(repos), model.workspace(self.cfg), **kw)

    @staticmethod
    def refs(m, **want):
        return [i['ref'] for i in m['items'] if all(i[k] == v for k, v in want.items())]

    @staticmethod
    def item(m, ref):
        return next(i for i in m['items'] if i['ref'] == ref)


class RetrievalOrderTests(ContextCase):
    def test_explicit_references_outrank_inferred_matches(self):
        m = self.manifest()
        self.assertEqual(m['status'], 'ok')
        order = [i['ref'] for i in m['items']]
        explicit = self.refs(m, tier=1)
        self.assertEqual(set(explicit), {'games/expo/engine.py', 'games/expo/docs/ACTIONS.md', 'tests/test_expo_engine.py',
                                         'docs/adr/0015-game-result-envelope.md'})
        self.assertEqual(order[:len(explicit)], explicit)                      # tier 1 first, in full
        self.assertEqual([i['tier'] for i in m['items']], sorted(i['tier'] for i in m['items']))
        engine = self.item(m, 'games/expo/engine.py')
        self.assertEqual((engine['tier'], engine['source_type'], engine['authority'], engine['kind']),
                         (1, 'issue-reference', 'implementation', 'implementation'))
        self.assertIn('`games/expo/engine.py`', engine['why'])
        # the same file would also match the identifier and the search: it appears once, at its best tier
        self.assertEqual(order.count('games/expo/engine.py'), 1)
        test = self.item(m, 'tests/test_expo_engine.py')
        self.assertEqual((test['tier'], test['kind'], test['authority']), (1, 'test', 'tests'))
        self.assertIn('defines `test_only_the_captain_may_offer`', test['why'])

    def test_every_item_carries_provenance(self):
        m = self.manifest()
        for i in m['items']:
            self.assertEqual(set(i), {'ref', 'repo', 'kind', 'tier', 'source_type', 'authority', 'why', 'commit', 'tokens'}, i)
            self.assertTrue(i['why'])
            self.assertEqual(i['commit'], {'games': self.games_sha, 'party': self.party_sha}[i['repo']])
        self.assertEqual(m['repos']['games'], {'commit': self.games_sha, 'rev': 'origin/main', 'worktree': None})

    def test_governing_sources_adr_and_canonical_docs(self):
        m = self.manifest()
        adr = self.item(m, 'docs/adr/0015-game-result-envelope.md')              # an ADR lives in Party even for a Games issue
        self.assertEqual((adr['repo'], adr['tier'], adr['kind'], adr['authority'], adr['source_type']),
                         ('party', 1, 'governing', 'adr (accepted)', 'issue-reference'))
        actions = self.item(m, 'games/expo/docs/ACTIONS.md')
        self.assertEqual((actions['kind'], actions['authority']), ('governing', 'canonical'))
        self.assertIn('basename', actions['why'])
        model_doc = self.item(m, 'games/expo/docs/MISSION_MODEL.md')            # not named, but canonical and mentions captain_one
        self.assertEqual((model_doc['tier'], model_doc['source_type'], model_doc['authority']), (2, 'canonical-doc', 'canonical'))
        agents = self.item(m, 'AGENTS.md')
        self.assertEqual((agents['tier'], agents['kind']), (2, 'governing'))
        self.assertNotIn('docs/notes/old-captain.md', [i['ref'] for i in m['items']])     # historical is never governing
        self.assertNotIn('docs/adr/0016-service-identities.md', [i['ref'] for i in m['items']])

    def test_exact_matches_come_after_canonical_and_before_graphify_and_search(self):
        m = self.manifest()
        client = self.item(m, 'games/expo/client.js')
        self.assertEqual((client['tier'], client['source_type']), (3, 'code-match'))
        self.assertIn('`captain_one`', client['why'])
        rules = self.item(m, 'tests/test_expo_rules.py')
        self.assertEqual((rules['tier'], rules['kind'], rules['source_type']), (3, 'test', 'test-match'))
        self.assertIn('games/expo', rules['why'])
        self.assertNotIn('tests/test_bluff.py', [i['ref'] for i in m['items']])
        self.assertNotIn('games/bluff/engine.py', [i['ref'] for i in m['items']])

    def test_multi_repository_issue(self):
        m = self.manifest(description=ISSUE.replace('avrana-party-games', 'avrana-party, avrana-party-games') + '\nAlso `avrana/party/core.py`.',
                          repos=('games', 'party'))
        self.assertEqual(sorted(m['repos']), ['games', 'party'])
        core = self.item(m, 'avrana/party/core.py')
        self.assertEqual((core['repo'], core['tier'], core['commit']), ('party', 1, self.party_sha))
        self.assertEqual({(i['repo'], i['ref']) for i in m['items'] if i['ref'] == 'AGENTS.md'}, {('games', 'AGENTS.md'), ('party', 'AGENTS.md')})

    def test_issue_worktree_is_inspected_instead_of_main_when_it_exists(self):
        wt = self.add_worktree('games', 'feat/avr-900-x', 'avrana-party-games.wt-avr900')
        (wt / 'games' / 'expo' / 'offer.py').write_text('X = "captain_one"\n', encoding='utf-8')
        git(wt, 'add', '-A')
        git(wt, 'commit', '-q', '-m', 'wip')
        m = self.manifest()
        self.assertEqual((m['repos']['games']['rev'], m['repos']['games']['commit']), ('worktree HEAD', git(wt, 'rev-parse', 'HEAD')))
        self.assertIn('games/expo/offer.py', self.refs(m, tier=3))


class GraphifyTests(ContextCase):
    def test_canonical_sources_outrank_graphify_and_hits_are_verified_against_the_checkout(self):
        self.games_sha = self.seed('games', {'context/graphify/semantic-graph.json': graph(self.games_sha, [
            ('Captain decides allocation (captain_one)', 'games/expo/docs/MISSION_MODEL.md', 'allocation modes'),
            ('captain_one consent panel', 'games/expo/panel.js', 'a file that does not exist'),
            ('Captain decision history', 'party-docs/docs/runbooks/x.md', 'captain_one in another corpus'),
            ('captain_one helper', 'games/bluff/engine.py', 'graph claims a relation'),
            ('Unrelated node', 'tests/test_bluff.py', 'nothing')])}, 'graph')
        m = self.manifest()
        doc = self.item(m, 'games/expo/docs/MISSION_MODEL.md')
        self.assertEqual((doc['tier'], doc['source_type']), (2, 'canonical-doc'))          # found by the canonical tier; Graphify does not demote or re-label it
        hit = self.item(m, 'games/bluff/engine.py')
        self.assertEqual((hit['tier'], hit['source_type'], hit['authority']), (4, 'graphify', 'derived (verify in the file)'))
        self.assertIn('captain_one helper', hit['why'])
        refs = [i['ref'] for i in m['items']]
        self.assertNotIn('games/expo/panel.js', refs)
        self.assertNotIn('party-docs/docs/runbooks/x.md', refs)
        self.assertNotIn('tests/test_bluff.py', refs)
        self.assertTrue(any('games/expo/panel.js' in w and 'does not exist' in w for w in m['warnings']))
        self.assertLess(max(i['tier'] for i in m['items'] if i['source_type'] != 'graphify' and i['tier'] < 4), 4)

    def test_stale_graphify_produces_a_warning(self):
        built = self.games_sha
        self.seed('games', {'context/graphify/semantic-graph.json': graph(built, [('captain_one helper', 'games/bluff/engine.py', '')])}, 'graph')
        fresh = self.manifest()
        self.assertFalse(any('stale' in w.lower() for w in fresh['warnings']), fresh['warnings'])     # only the graph file itself changed
        self.seed('games', {'games/bluff/engine.py': 'def bluff():\n    return 2\n', 'games/expo/engine.py': GAMES['games/expo/engine.py'] + '# changed\n'}, 'code moves on')
        stale = self.manifest()
        self.assertTrue(any('Graphify' in w and 'stale' in w and built[:12] in w for w in stale['warnings']), stale['warnings'])
        self.assertIn('changed since the graph was built', self.item(stale, 'games/bluff/engine.py')['why'])

    def test_graph_built_at_an_unknown_commit_cannot_be_called_fresh(self):
        self.seed('games', {'context/graphify/semantic-graph.json': graph('0' * 40, [('captain_one helper', 'games/bluff/engine.py', '')])}, 'graph')
        m = self.manifest()
        self.assertTrue(any('cannot be determined' in w for w in m['warnings']), m['warnings'])

    def test_unreadable_graph_is_a_warning_not_a_crash(self):
        self.seed('games', {'context/graphify/semantic-graph.json': '{not json'}, 'graph')
        m = self.manifest()
        self.assertEqual(m['status'], 'ok')
        self.assertTrue(any('Graphify' in w and 'unreadable' in w for w in m['warnings']))


class BudgetAndFailureTests(ContextCase):
    def test_context_budget_trims_the_lowest_tiers_first(self):
        full = self.manifest()
        small = self.manifest(max_items=4)
        self.assertEqual(len(small['items']), 4)
        self.assertEqual([i['ref'] for i in small['items']], [i['ref'] for i in full['items']][:4])
        self.assertEqual(small['budget']['dropped'], len(full['items']) - 4)
        self.assertTrue(all(d['tier'] >= max(i['tier'] for i in small['items']) for d in small['budget']['dropped_items']))
        self.assertTrue(any('budget' in w for w in small['warnings']))
        tight = self.manifest(max_tokens=20)
        self.assertLessEqual(tight['budget']['tokens'], 20 + max(i['tokens'] for i in full['items']))
        self.assertLess(len(tight['items']), len(full['items']))
        self.assertEqual(tight['items'][0]['tier'], 1)                           # the best item always survives

    def test_no_file_contents_are_embedded(self):
        text = json.dumps(self.manifest())
        self.assertNotIn('def allocate', text)
        self.assertLess(len(text), 12000)

    def test_missing_context_fails_clearly_instead_of_guessing(self):
        m = self.manifest(description='## Outcome\n\nMake the captain flow nicer in missions.\n\n## Repositories\n\navrana-party-games\n\n## Open Decisions\n\nNone\n')
        self.assertEqual(m['status'], 'insufficient')
        self.assertEqual([i['ref'] for i in m['items']], ['AGENTS.md'])           # no search or Graphify guesses presented as scope
        self.assertTrue(any('names no file' in w for w in m['warnings']), m['warnings'])

    def test_unresolved_and_ambiguous_references_are_warnings(self):
        self.seed('games', {'games/bluff/docs/ACTIONS.md': '# Bluff actions\n'}, 'second ACTIONS')
        m = self.manifest(description=ISSUE + '\nSee `games/expo/missing.py` and `no_such_symbol_anywhere`.\n')
        self.assertTrue(any('`ACTIONS.md`' in w and 'ambiguous' in w for w in m['warnings']), m['warnings'])
        self.assertTrue(any('`games/expo/missing.py`' in w and 'not found' in w for w in m['warnings']))
        self.assertTrue(any('`no_such_symbol_anywhere`' in w and 'not found' in w for w in m['warnings']))
        self.assertEqual({i['ref'] for i in m['items'] if i['ref'].endswith('ACTIONS.md')}, {'games/expo/docs/ACTIONS.md', 'games/bluff/docs/ACTIONS.md'})

    def test_a_word_too_common_to_locate_anything_is_not_used_as_an_identifier(self):
        self.seed('games', {f'games/expo/part{n}.py': 'location = 1\n' for n in range(context.COMMON + 2)}, 'common word')
        m = self.manifest(description=ISSUE + '\nNo hand-written nginx `location` block, no `games/<slug>/` special case.\n')
        self.assertTrue(any('`location`' in w and 'too common' in w for w in m['warnings']), m['warnings'])
        self.assertFalse(any('part' in i['ref'] for i in m['items']))
        self.assertFalse(any('<slug>' in w for w in m['warnings']))               # a placeholder path is not a reference

    def test_one_large_file_does_not_starve_the_entry_point(self):
        self.seed('games', {'games/expo/engine.py': GAMES['games/expo/engine.py'] + '# pad\n' * 60000}, 'large engine')
        m = self.manifest()
        refs = [i['ref'] for i in m['items']]
        self.assertIn('AGENTS.md', refs)
        self.assertIn('tests/test_expo_rules.py', refs)
        engine = self.item(m, 'games/expo/engine.py')
        self.assertGreater(engine['tokens'], context.ITEM_COST_CAP)               # the true size is still reported
        self.assertLessEqual(m['budget']['tokens'], context.MAX_TOKENS)

    def test_undetermined_repositories_build_nothing(self):
        m = self.manifest(repos=())
        self.assertEqual((m['status'], m['items']), ('insufficient', []))
        self.assertTrue(any('repositories' in w for w in m['warnings']))


class CliTests(ContextCase):
    def test_context_command_is_read_only_and_concise(self):
        self.world(issues=[{**issue('AVR-900', description=ISSUE), 'title': 'EXPO: the captain decides'}])
        before = git(self.repo('games'), 'status', '--porcelain')
        code, out, _ = run('context', 'AVR-900')
        self.assertEqual(code, 0, out)
        for heading in ('Governing', 'Implementation', 'Tests'):
            self.assertIn(heading, out)
        self.assertIn('games/expo/engine.py', out)
        self.assertIn('named in the issue', out)
        self.assertLess(len(out.splitlines()), 60)
        self.assertEqual(git(self.repo('games'), 'status', '--porcelain'), before)
        self.assertFalse((self.root / 'state').exists())                          # nothing written unless asked

    def test_insufficient_context_exits_4(self):
        self.world(issues=[issue('AVR-900', description='## Outcome\n\nNicer.\n\n## Repositories\n\navrana-party-games\n\n## Open Decisions\n\nNone\n')])
        code, out, _ = run('context', 'AVR-900')
        self.assertEqual(code, 4)
        self.assertIn('insufficient', out)
        self.world(issues=None)
        self.assertEqual(run('context', 'AVR-900')[0], 4)                           # no Linear, no manifest

    def test_start_attaches_the_manifest_and_saves_it_outside_the_product_repos(self):
        self.world(issues=[{**issue('AVR-900', description=ISSUE), 'title': 'EXPO: the captain decides'}])
        code, out, _ = run('start', 'AVR-900', '--owner', 'claude-aaaa', '--json')
        self.assertEqual(code, 0, out)
        doc = json.loads(out)
        self.assertEqual(doc['manifest']['status'], 'ok')
        self.assertEqual(doc['manifest']['repos']['games']['rev'], 'worktree HEAD')
        saved = Path(doc['manifest_file'])
        self.assertEqual(saved, self.root / 'state' / 'context' / 'AVR-900.json')
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['issue'], 'AVR-900')
        for name in ('party', 'games'):
            self.assertEqual(git(self.repo(name), 'status', '--porcelain'), '')
        self.assertEqual(git(self.root / 'avrana-party-games.wt-avr900', 'status', '--porcelain'), '')
        dry = json.loads(run('start', 'AVR-900', '--owner', 'claude-aaaa', '--json', '--dry-run')[1])
        self.assertIn('manifest', dry)
        self.assertNotIn('manifest_file', dry)


if __name__ == '__main__':
    unittest.main()
