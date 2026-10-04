"""The agent path in one step: the issue as the Linear connector returned it, piped to `start`."""
import contextlib
import io
import json
import sys
import unittest

from support import PARTY_ONLY, READY, ROOT, RepoCase, issue, run

from ai_workflow import cli, gitio, sources

CONNECTOR = {'id': 'AVR-900', 'title': 'EXPO: the captain decides', 'status': 'Todo', 'statusType': 'unstarted', 'labels': [],
             'project': 'The Team II', 'url': 'https://linear.example/AVR-900',
             'description': '## Outcome\n\nx\n\n## Tests Required\n\n* y\n\n## Repositories\n\navrana-party-games\n\n## Open Decisions\n\nNone\n',
             'relations': {'blocks': [], 'blockedBy': [], 'relatedTo': [{'id': 'AVR-240', 'title': 't'}], 'duplicateOf': None}}


def piped(text, *argv):
    """Run the CLI with `text` on stdin, against the real Linear reader (no API token in tests)."""
    old_in, old_sources = sys.stdin, cli.SOURCES

    class Piped(old_sources):
        linear = staticmethod(lambda issue=None, snapshot=None: sources.linear(issue, snapshot=snapshot, find_token=lambda: (None, 'no token')))
    sys.stdin, cli.SOURCES = io.StringIO(text), Piped
    try:
        return run(*argv)
    finally:
        sys.stdin, cli.SOURCES = old_in, old_sources


class SectionTests(unittest.TestCase):
    def test_repos_and_repositories_headings_are_the_same_section(self):
        for heading in ('## Repositories', '## Repos', '## Repository', '### Repos:', '## repos'):
            self.assertEqual(sources.sections(f'## Outcome\nx\n\n{heading}\navrana-party\n')['Repositories'], 'avrana-party', heading)
        self.assertIsNone(sources.sections('## Reposition\navrana-party\n')['Repositories'])

    def test_tests_heading_alias_and_trailing_colon(self):
        self.assertEqual(sources.sections('## Tests\nz\n')['Tests Required'], 'z')
        self.assertEqual(sources.sections('## Open Decisions:\nNone\n')['Open Decisions'], 'None')

    def test_open_decisions_stays_conservative(self):
        self.assertEqual(sources.open_decisions('## Open Decisions\nNone.\n'), 'none')
        self.assertEqual(sources.open_decisions('## Open Decisions\n\n'), 'none')
        text = '## Open Decisions\nNone blocking. For the owner in review: accept ADR 0016 as written, or amend.\n'
        self.assertEqual(sources.open_decisions(text), 'unresolved')

    def test_snapshot_text_forms(self):
        one = json.dumps(CONNECTOR)
        dep = json.dumps({'id': 'AVR-240', 'status': 'Done', 'title': 'dep'})
        self.assertEqual(sorted(sources.parse_snapshot(one)), ['AVR-900'])
        self.assertEqual(sorted(sources.parse_snapshot(one + '\n' + dep)), ['AVR-240', 'AVR-900'])       # connector results back to back
        self.assertEqual(sorted(sources.parse_snapshot(f'[{one}, {dep}]')), ['AVR-240', 'AVR-900'])
        self.assertEqual(sorted(sources.parse_snapshot('{"issues": [' + one + ']}')), ['AVR-900'])
        for bad in ('', '   ', 'not json', one + ' trailing garbage', '{"title": "no id"}', '42'):
            with self.assertRaises(ValueError, msg=bad):
                sources.parse_snapshot(bad)


class OneStepStartTests(RepoCase):
    def test_connector_issue_on_stdin_starts_with_no_other_flags(self):
        code, out, err = piped(json.dumps(CONNECTOR), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-', '--json')
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual([(w['repo'], w['action'], w['claim']) for w in doc['workspaces']], [('games', 'create', 'claimed')])
        self.assertEqual(doc['context']['linear']['origin'], 'snapshot on stdin')

    def test_dry_run_parity_on_stdin(self):
        code, out, _ = piped(json.dumps(CONNECTOR), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-', '--dry-run', '--json')
        doc = json.loads(out)
        self.assertEqual((code, doc['dry_run'], doc['started']), (0, True, False))
        self.assertEqual(len(gitio.worktrees(self.repo('games'))), 1)

    def test_repos_spelling_from_the_issue_template_is_understood(self):
        raw = {**CONNECTOR, 'description': CONNECTOR['description'].replace('## Repositories', '## Repos')}
        code, out, _ = piped(json.dumps(raw), 'issue', 'AVR-900', '--linear-snapshot', '-', '--json')
        ctx = json.loads(out)
        self.assertEqual((ctx['repositories']['value'], ctx['readiness']['state']), (['games'], 'ready-for-agent'))

    def test_repository_names_by_short_name(self):
        for text, want in (('Games', ['games']), ('Party and Games (paired)', ['games', 'party']), ('both', ['games', 'party']),
                           ('`avrana-party`', ['party']), ('avrana-party-games only', ['games']), ('the web client', None)):
            self.world(issues=[issue('AVR-900', description=PARTY_ONLY.replace('`avrana-party`', text))])
            ctx = json.loads(run('issue', 'AVR-900', '--json')[1])
            self.assertEqual(ctx['repositories']['value'], want, text)

    def test_refusal_names_the_exact_missing_sections_and_how_to_fix_them(self):
        raw = {**CONNECTOR, 'description': '## Problem\n\nx\n\n## Acceptance criteria\n\n- [ ] y\n'}
        code, out, _ = piped(json.dumps(raw), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-', '--json')
        doc = json.loads(out)
        self.assertEqual((code, doc['started']), (4, False))
        self.assertEqual(doc['context']['readiness']['missing_sections'], ['Open Decisions', 'Repositories'])
        text = piped(json.dumps(raw), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-')[1]
        self.assertIn('## Open Decisions', text)
        self.assertIn('## Repositories', text)
        self.assertIn('--repo', text)
        self.assertEqual(len(gitio.worktrees(self.repo('games'))), 1)

    def test_none_blocking_refusal_quotes_the_text_and_says_why(self):
        raw = {**CONNECTOR, 'description': CONNECTOR['description'].replace(
            '## Open Decisions\n\nNone', '## Open Decisions\n\nNone blocking. For the owner in review: accept ADR 0016 as written, or amend.')}
        code, out, _ = piped(json.dumps(raw), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-')
        self.assertEqual(code, 3)
        self.assertIn('Needs Cody', out)
        self.assertIn('"None blocking. For the owner in review: accept ADR 0016 as written, or amend."', out)
        self.assertIn('only an empty section or "None" counts', out)

    def test_dependency_missing_from_the_snapshot_is_named(self):
        raw = {**CONNECTOR, 'relations': {'blockedBy': [{'id': 'AVR-801', 'title': 'dep'}]}}
        code, out, _ = piped(json.dumps(raw), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-')
        self.assertEqual(code, 4)
        self.assertIn('AVR-801', out)
        self.assertIn('add AVR-801 to the snapshot', out)
        dep = {'id': 'AVR-801', 'status': 'Done', 'title': 'dep'}
        code, out, _ = piped(json.dumps(raw) + json.dumps(dep), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-', '--dry-run')
        self.assertEqual(code, 0, out)

    def test_read_without_relations_says_what_to_fetch(self):
        raw = {k: v for k, v in CONNECTOR.items() if k != 'relations'}
        code, out, _ = piped(json.dumps(raw), 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-')
        self.assertEqual(code, 4)
        self.assertIn('with its relations', out)

    def test_bad_or_wrong_stdin_fails_clearly(self):
        for text, needle in (('', 'unreadable'), ('not json', 'unreadable'), (json.dumps({**CONNECTOR, 'id': 'AVR-901'}), 'AVR-900 is not in snapshot on stdin')):
            code, out, _ = piped(text, 'start', 'AVR-900', '--owner', 'claude-aaaa', '--linear-snapshot', '-')
            self.assertEqual(code, 4, text)
            self.assertIn(needle, out)
        self.assertEqual(len(gitio.worktrees(self.repo('games'))), 1)

    def test_launchers_wrap_the_one_cli(self):
        sh = (ROOT / 'bin' / 'avr').read_text(encoding='utf-8')
        cmd = (ROOT / 'bin' / 'avr.cmd').read_text(encoding='utf-8')
        self.assertIn('aw.py', sh)
        self.assertIn('aw.py', cmd)
        self.assertNotIn('Users', sh + cmd)                       # no machine paths
        self.assertFalse(any(p.name not in ('avr', 'avr.cmd') for p in (ROOT / 'bin').iterdir()))


if __name__ == '__main__':
    unittest.main()
