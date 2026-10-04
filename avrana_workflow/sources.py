"""The systems of record, read only: GitHub (through `gh`), Linear and the Pi status endpoint.

Every reader returns {'available': True, 'data': ...} or {'available': False, 'reason': ...}.
Nothing here returns an empty success for a source it could not reach.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

PR_FIELDS = 'number,title,state,isDraft,headRefName,url,mergedAt,reviewDecision,statusCheckRollup,mergeable'
FAILED = {'FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}
PASSED = {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
SECTIONS = ('Outcome', 'Acceptance Criteria', 'Out of Scope', 'Repositories', 'Tests Required', 'Dependencies', 'Open Decisions')
NOTHING = {'none', 'none.', '-', 'n/a', 'na', 'nothing', '(none)', '_none_', 'none at this time.'}


def ok(data):
    return {'available': True, 'data': data}


def unavailable(reason):
    return {'available': False, 'reason': reason}


def _run(cmd, timeout=40):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, '', type(e).__name__
    return p.returncode, p.stdout, p.stderr


# ---- GitHub -------------------------------------------------------------------------------------
def ci_state(rollup):
    """'failed' | 'running' | 'passed' | 'none' from gh's statusCheckRollup."""
    if not rollup:
        return 'none'
    states = []
    for check in rollup:
        status = (check.get('status') or '').upper()
        states.append(status if status not in ('', 'COMPLETED') else (check.get('conclusion') or check.get('state') or '').upper())
    if any(s in FAILED for s in states):
        return 'failed'
    return 'passed' if all(s in PASSED for s in states) else 'running'      # anything unrecognised is not a pass


def normalize_pr(repo, row):
    state = 'merged' if row.get('mergedAt') or row.get('state') == 'MERGED' else (row.get('state') or '').lower()
    return {'repo': repo, 'number': row['number'], 'title': row.get('title', ''), 'state': state,
            'draft': bool(row.get('isDraft')), 'branch': row.get('headRefName'), 'url': row.get('url'),
            'ci': ci_state(row.get('statusCheckRollup')), 'review': row.get('reviewDecision') or None,
            'conflicting': row.get('mergeable') == 'CONFLICTING'}


def prs(repo, slug, issue=None, run=_run):
    """PRs of `slug`: every state for one issue, otherwise the open ones."""
    cmd = ['gh', 'pr', 'list', '--repo', slug, '--limit', '50', '--json', PR_FIELDS]
    cmd += ['--state', 'all', '--search', issue] if issue else ['--state', 'open']
    rc, out, err = run(cmd)
    if rc:
        return unavailable(f'gh failed for {slug} ({((err or "").strip().splitlines() or ["gh unavailable"])[-1]})')
    try:
        rows = json.loads(out or '[]')
    except ValueError:
        return unavailable(f'gh returned unreadable output for {slug}')
    if issue:
        rx = re.compile(rf'\b{re.escape(issue)}\b', re.I)
        rows = [r for r in rows if rx.search(f'{r.get("title", "")} {r.get("headRefName", "")}')]
    return ok([normalize_pr(repo, r) for r in rows])


# ---- Linear -------------------------------------------------------------------------------------
def sections(description):
    """The issue-template sections; a missing section is None, never an empty string."""
    found = {}
    for name in SECTIONS:
        m = re.search(rf'^#{{2,3}}\s*{re.escape(name)}\s*$\n(.*?)(?=^#{{2,3}}\s|\Z)', description or '', re.M | re.S | re.I)
        found[name] = m[1].strip() if m else None
    return found


def open_decisions(description):
    """'unknown' (no description read), 'missing' (no section), 'none', or 'unresolved'."""
    if description is None:
        return 'unknown'
    text = sections(description)['Open Decisions']
    if text is None:
        return 'missing'
    return 'none' if text.strip().lower() in NOTHING or not text.strip() else 'unresolved'


def normalize_issue(raw):
    """One shape from the three we meet: the Linear MCP's get_issue/list_issues and GraphQL."""
    state = raw.get('state') if isinstance(raw.get('state'), dict) else {}
    labels = raw.get('labels') or []
    if isinstance(labels, dict):
        labels = [n.get('name') for n in labels.get('nodes', [])]
    relations = raw.get('relations') or {}
    if 'blockedBy' in relations:
        blocked_by = [r['id'] for r in relations['blockedBy']]
    elif 'inverseRelations' in raw:
        blocked_by = [n['issue']['identifier'] for n in raw['inverseRelations'].get('nodes', []) if n.get('type') == 'blocks']
    else:
        blocked_by = None                       # this read did not include relations
    return {'id': raw.get('identifier') or raw.get('id'), 'title': raw.get('title', ''),
            'state': state.get('name') or raw.get('status'), 'state_type': state.get('type') or raw.get('statusType'),
            'labels': [l for l in labels if l], 'description': raw.get('description'), 'url': raw.get('url'),
            'blocked_by': blocked_by}


def load_snapshot(path):
    """Issues from a JSON file an agent wrote from its Linear connector: one issue, a list, or
    {'issues': [...]}. Later entries for the same issue fill in fields earlier ones lacked."""
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = doc.get('issues', [doc]) if isinstance(doc, dict) else doc
    merged = {}
    for raw in rows:
        issue = normalize_issue(raw)
        old = merged.get(issue['id'], {})
        merged[issue['id']] = {k: (v if v not in (None, [], '') else old.get(k)) for k, v in issue.items()}
        merged[issue['id']]['labels'] = merged[issue['id']]['labels'] or []
    return merged


ISSUE_FIELDS = '''identifier title url description state { name type } labels { nodes { name } }
  inverseRelations { nodes { type issue { identifier } } }'''


def _graphql(key, query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    req = urllib.request.Request('https://api.linear.app/graphql', data=body,
                                 headers={'Content-Type': 'application/json', 'Authorization': key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode('utf-8'))
    if data.get('errors'):
        raise ValueError('Linear returned errors')
    return data['data']


def linear(issue=None, snapshot=None, key=None, graphql=_graphql):
    """{id: issue}: one issue when `issue` is given, else every unstarted/started issue.
    A snapshot file wins over the API; with neither, Linear is unavailable."""
    snapshot = snapshot or os.environ.get('AVRANA_LINEAR_SNAPSHOT')
    if snapshot:
        try:
            return {**ok(load_snapshot(snapshot)), 'origin': f'snapshot {snapshot}'}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            return unavailable(f'Linear snapshot {snapshot} unreadable ({type(e).__name__})')
    key = key or os.environ.get('LINEAR_API_KEY')
    if not key:
        return unavailable('no LINEAR_API_KEY and no --linear-snapshot; read the issue through the Linear connector '
                           'and pass it as a snapshot')
    try:
        if issue:
            raw = graphql(key, f'query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}', {'id': issue})['issue']
            rows = [raw] if raw else []
        else:
            rows, after = [], None
            while True:
                page = graphql(key, f'''query($after: String) {{ issues(first: 100, after: $after,
                    filter: {{state: {{type: {{in: ["unstarted", "started"]}}}}}}) {{
                    nodes {{ {ISSUE_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }}''', {'after': after})['issues']
                rows += page['nodes']
                if not page['pageInfo']['hasNextPage']:
                    break
                after = page['pageInfo']['endCursor']
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        return unavailable(f'Linear unreachable or refused ({type(e).__name__})')
    return {**ok({i['id']: i for i in map(normalize_issue, rows)}), 'origin': 'Linear API'}


# ---- Pi -----------------------------------------------------------------------------------------
def pi_status(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return unavailable(f'{url} unreachable ({type(e).__name__}); the Pi answers only on the Party LAN')
    if not isinstance(doc, dict):
        return unavailable(f'{url} returned an unexpected document')
    return ok(doc)
