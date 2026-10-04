import io
import json
import sys
import unittest
import urllib.error

from support import READY, UNDECIDED

from ai_workflow import sources, tokens

NODE = {'identifier': 'AVR-5', 'title': 'Registry', 'url': 'https://linear.example/AVR-5', 'description': UNDECIDED,
        'state': {'name': 'Todo', 'type': 'unstarted'}, 'labels': {'nodes': [{'name': 'Core'}, {'name': 'Bug'}]},
        'project': {'name': 'Avrana Party'}, 'projectMilestone': {'name': 'M6'}, 'parent': {'identifier': 'AVR-1'},
        'inverseRelations': {'nodes': [{'type': 'blocks', 'issue': {'identifier': 'AVR-4', 'state': {'name': 'In Progress', 'type': 'started'}}},
                                       {'type': 'related', 'issue': {'identifier': 'AVR-3', 'state': {'name': 'Done', 'type': 'completed'}}}]}}


def api(reply):
    calls = []

    def graphql(token, query, variables):
        calls.append((token, query, variables))
        if isinstance(reply, Exception):
            raise reply
        return reply(variables) if callable(reply) else reply
    graphql.calls = calls
    return graphql


class LinearTests(unittest.TestCase):
    def read(self, reply, issue='AVR-5', **kw):
        return sources.linear(issue, token='t', graphql=api(reply), **kw)

    def test_one_issue_with_everything_the_readiness_engine_needs(self):
        got = self.read({'data': {'issue': NODE}})
        self.assertTrue(got['available'])
        issue = got['data']['AVR-5']
        self.assertEqual((issue['title'], issue['state'], issue['labels'], issue['project'], issue['milestone'], issue['parent']),
                         ('Registry', 'Todo', ['Core', 'Bug'], 'Avrana Party', 'M6', 'AVR-1'))
        self.assertEqual((issue['blocked_by'], issue['dependency_states']), (['AVR-4'], {'AVR-4': 'In Progress'}))
        self.assertEqual(sources.open_decisions(issue['description']), 'unresolved')
        secs = sources.sections(issue['description'])
        self.assertEqual((secs['Outcome'], secs['Acceptance Criteria'], secs['Tests Required']), ('x', '- y', 'z'))
        self.assertIn('avrana-party', secs['Repositories'])

    def test_issue_not_found_is_an_empty_read_not_an_error(self):
        got = self.read({'data': {'issue': None}})
        self.assertEqual((got['available'], got['data']), (True, {}))

    def test_active_issues_are_paged(self):
        pages = {None: {'nodes': [NODE], 'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}},
                 'c1': {'nodes': [{**NODE, 'identifier': 'AVR-6', 'description': READY}], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        graphql = api(lambda variables: {'data': {'issues': pages[variables['after']]}})
        got = sources.linear(token='t', graphql=graphql)
        self.assertEqual(sorted(got['data']), ['AVR-5', 'AVR-6'])
        self.assertEqual(len(graphql.calls), 2)
        self.assertIn('"unstarted", "started"', graphql.calls[0][1])

    def test_the_query_is_read_only(self):
        graphql = api({'data': {'issue': NODE}})
        sources.linear('AVR-5', token='t', graphql=graphql)
        self.assertNotIn('mutation', graphql.calls[0][1])

    def test_malformed_responses_are_unavailable(self):
        for reply in ({}, {'data': None}, {'data': []}, {'data': {'issue': {'title': 'no identifier'}}},
                      {'data': {'issue': {**NODE, 'inverseRelations': {'nodes': [{'type': 'blocks'}]}}}},
                      {'data': {'issue': 'a string'}}, {'errors': [{'message': 'Authentication required'}], 'data': None}):
            got = self.read(reply)
            self.assertFalse(got['available'], reply)
        self.assertIn('Authentication required', self.read({'errors': [{'message': 'Authentication required'}]})['reason'])
        self.assertFalse(sources.linear(token='t', graphql=api({'data': {'issues': {'nodes': [NODE]}}}))['available'])

    def test_network_and_auth_failures_are_unavailable(self):
        self.assertIn('unreachable', self.read(urllib.error.URLError('down'))['reason'])
        self.assertIn('unreachable', self.read(TimeoutError())['reason'])
        denied = self.read(urllib.error.HTTPError('u', 401, 'Unauthorized', {}, io.BytesIO(b'')))
        self.assertIn('token rejected', denied['reason'])
        self.assertIn('malformed', self.read(ValueError('not json'))['reason'])

    def test_no_token_is_unavailable_and_says_where_it_looked(self):
        got = sources.linear('AVR-5', graphql=api({'data': {'issue': NODE}}), find_token=lambda: tokens.linear_token(env={}, store=lambda n, p: None))
        self.assertFalse(got['available'])
        self.assertIn('LINEAR_API_KEY', got['reason'])
        self.assertIn('ai-workflow-linear', got['reason'])

    def test_snapshot_fallback(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'snap.json'
            path.write_text(json.dumps({'issues': [{'id': 'AVR-9', 'title': 't', 'status': 'Todo', 'labels': ['Core']},
                                                   {'id': 'AVR-9', 'description': READY, 'relations': {'blockedBy': []}, 'project': 'P'}]}), encoding='utf-8')
            got = sources.linear('AVR-9', snapshot=str(path))
            self.assertEqual((got['data']['AVR-9']['state'], got['data']['AVR-9']['blocked_by'], got['data']['AVR-9']['project']), ('Todo', [], 'P'))
            self.assertNotIn(d, got['origin'])                      # the origin names the file, not the machine path
            path.write_text('{"issues": [{"title": "no id"}]}', encoding='utf-8')
            self.assertFalse(sources.linear('AVR-9', snapshot=str(path))['available'])
        self.assertFalse(sources.linear('AVR-9', snapshot='does-not-exist.json')['available'])


class TokenTests(unittest.TestCase):
    def test_environment_wins_over_the_store(self):
        token, where = tokens.linear_token(env={'LINEAR_API_KEY': ' lin_env '}, store=lambda n, p: 'lin_store')
        self.assertEqual((token, where), ('lin_env', 'environment LINEAR_API_KEY'))

    def test_os_secret_store(self):
        seen = []
        token, where = tokens.linear_token(env={}, platform='linux', store=lambda n, p: seen.append((n, p)) or 'lin_store\n')
        self.assertEqual((token, seen), ('lin_store', [('ai-workflow-linear', 'linux')]))
        self.assertNotIn('lin_store', where)

    def test_missing_or_broken_store_is_no_token(self):
        self.assertEqual(tokens.linear_token(env={}, store=lambda n, p: None)[0], None)
        self.assertEqual(tokens.linear_token(env={}, store=lambda n, p: '  ')[0], None)

        def broken(name, platform):
            raise OSError('keychain locked')
        token, why = tokens.linear_token(env={}, store=broken)
        self.assertEqual(token, None)
        self.assertIn('could not be read', why)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Credential Manager')
    def test_windows_credential_manager_reports_a_missing_entry(self):
        self.assertIsNone(tokens._store('ai-workflow-test-entry-that-does-not-exist', 'win32'))


if __name__ == '__main__':
    unittest.main()
