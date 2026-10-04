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

from . import gitio, tokens

PR_FIELDS = 'number,title,state,isDraft,headRefName,baseRefName,url,mergedAt,reviewDecision,statusCheckRollup,mergeable'
FAILED = {'FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}
PASSED = {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
SECTIONS = ('Outcome', 'Acceptance Criteria', 'Out of Scope', 'Repositories', 'Tests Required', 'Dependencies', 'Open Decisions')
NOTHING = {'none', 'none.', '-', 'n/a', 'na', 'nothing', '(none)', '_none_', 'none at this time.'}
TERMINAL = ('Done', 'Canceled', 'Duplicate')


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
            'draft': bool(row.get('isDraft')), 'branch': row.get('headRefName'), 'base': row.get('baseRefName'),
            'url': row.get('url'), 'issue': gitio.issue_of(row.get('headRefName')) or gitio.issue_of(row.get('title')),
            'ci': ci_state(row.get('statusCheckRollup')), 'review': row.get('reviewDecision') or None,
            'review_required': row.get('reviewDecision') == 'REVIEW_REQUIRED',
            'conflicting': {'CONFLICTING': True, 'MERGEABLE': False}.get(row.get('mergeable')),   # None: GitHub has not said
            'behind_main': None}


def behind(slug, base, branch, run=_run):
    """Commits on `base` that `branch` lacks, or None when GitHub would not say."""
    rc, out, _ = run(['gh', 'api', f'repos/{slug}/compare/{base}...{branch}', '--jq', '.behind_by'])
    try:
        return int(out.strip()) if rc == 0 else None
    except ValueError:
        return None


def prs(repo, slug, issue=None, run=_run):
    """PRs of `slug`: every state for one issue, otherwise the open ones. Open PRs also carry how
    far they are behind their base."""
    cmd = ['gh', 'pr', 'list', '--repo', slug, '--limit', '50', '--json', PR_FIELDS]
    cmd += ['--state', 'all', '--search', issue] if issue else ['--state', 'open']
    rc, out, err = run(cmd)
    if rc:
        return unavailable(f'gh failed for {slug} ({((err or "").strip().splitlines() or ["gh unavailable"])[-1]})')
    try:
        rows = [normalize_pr(repo, r) for r in json.loads(out or '[]')]
    except (ValueError, KeyError, TypeError, AttributeError):
        return unavailable(f'gh returned unreadable output for {slug}')
    if issue:
        rows = [r for r in rows if r['issue'] == issue or gitio.issue_of(r['title']) == issue]
    for row in rows:
        if row['state'] == 'open' and row['branch']:
            row['behind_main'] = behind(slug, row['base'] or 'main', row['branch'], run)
    return ok(rows)


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


def _name(value):
    return value.get('name') if isinstance(value, dict) else value


def normalize_issue(raw):
    """One shape from the three we meet: the Linear connector's get_issue/list_issues and GraphQL."""
    state = raw.get('state') if isinstance(raw.get('state'), dict) else {}
    labels = raw.get('labels') or []
    if isinstance(labels, dict):
        labels = [n.get('name') for n in labels.get('nodes', [])]
    relations = raw.get('relations') or {}
    dependency_states = {}
    if isinstance(relations, dict) and 'blockedBy' in relations:
        blocked_by = [r['id'] for r in relations['blockedBy']]
    elif 'inverseRelations' in raw:
        blocked_by = []
        for node in raw['inverseRelations'].get('nodes', []):
            if node.get('type') == 'blocks':
                blocked_by.append(node['issue']['identifier'])
                if (node['issue'].get('state') or {}).get('name'):
                    dependency_states[node['issue']['identifier']] = node['issue']['state']['name']
    else:
        blocked_by = None                       # this read did not include relations
    parent = raw.get('parent')
    ident = raw.get('identifier') or raw.get('id')
    if not isinstance(ident, str) or not gitio.issue_id(ident):
        raise ValueError('issue without a recognisable identifier')
    return {'id': gitio.issue_id(ident), 'title': raw.get('title', ''),
            'state': state.get('name') or raw.get('status'), 'state_type': state.get('type') or raw.get('statusType'),
            'labels': [l for l in labels if l], 'description': raw.get('description'), 'url': raw.get('url'),
            'project': _name(raw.get('project')), 'milestone': _name(raw.get('projectMilestone')),
            'parent': parent.get('identifier') if isinstance(parent, dict) else raw.get('parentId'),
            'blocked_by': blocked_by, 'dependency_states': dependency_states}


def load_snapshot(path):
    """Issues from a JSON file an agent wrote from its Linear connector: one issue, a list, or
    {'issues': [...]}. Later entries for the same issue fill in fields earlier ones lacked."""
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = doc.get('issues', [doc]) if isinstance(doc, dict) else doc
    merged = {}
    for raw in rows:
        issue = normalize_issue(raw)
        old = merged.get(issue['id'], {})
        merged[issue['id']] = {k: (old.get(k) if v in (None, '') or (k in ('labels', 'dependency_states') and not v) else v)
                               for k, v in issue.items()}
        merged[issue['id']]['labels'] = merged[issue['id']]['labels'] or []
        merged[issue['id']]['dependency_states'] = merged[issue['id']]['dependency_states'] or {}
    return merged


ISSUE_FIELDS = '''identifier title url description state { name type } labels { nodes { name } }
  project { name } projectMilestone { name } parent { identifier }
  inverseRelations { nodes { type issue { identifier state { name type } } } }'''


def _graphql(key, query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    req = urllib.request.Request('https://api.linear.app/graphql', data=body,
                                 headers={'Content-Type': 'application/json', 'Authorization': key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def _why(reply):
    try:
        return f': {reply["errors"][0]["message"]}'[:160]
    except (KeyError, IndexError, TypeError):
        return ''


def linear(issue=None, snapshot=None, token=None, graphql=_graphql, find_token=tokens.linear_token):
    """{id: issue}: one issue when `issue` is given, else every unstarted/started issue.

    Reads the API with a token from the environment or the OS secret store; an explicit snapshot
    file (offline, or an agent's connector read) wins. Any failure, including a response that is
    not shaped like Linear's, is `unavailable`: never an empty or partial success.
    """
    snapshot = snapshot or os.environ.get('AI_WORKFLOW_LINEAR_SNAPSHOT')
    if snapshot:
        try:
            return {**ok(load_snapshot(snapshot)), 'origin': f'snapshot {Path(snapshot).name}'}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            return unavailable(f'Linear snapshot {Path(snapshot).name} unreadable ({type(e).__name__})')
    where = 'given token'
    if not token:
        token, where = find_token()
        if not token:
            return unavailable(f'{where}; or pass --linear-snapshot')
    try:
        if issue:
            reply = graphql(token, f'query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}', {'id': issue})
            if reply.get('errors') or not isinstance(reply.get('data'), dict):
                return unavailable('Linear refused the query or answered with errors' + _why(reply))
            rows = [reply['data']['issue']] if reply['data'].get('issue') else []
        else:
            rows, after = [], None
            while True:
                reply = graphql(token, f'''query($after: String) {{ issues(first: 50, after: $after,
                    filter: {{state: {{type: {{in: ["unstarted", "started"]}}}}}}) {{
                    nodes {{ {ISSUE_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }}''', {'after': after})
                if reply.get('errors') or not isinstance(reply.get('data'), dict):
                    return unavailable('Linear refused the query or answered with errors' + _why(reply))
                page = reply['data']['issues']
                rows += page['nodes']
                if not page['pageInfo']['hasNextPage']:
                    break
                after = page['pageInfo']['endCursor']
        issues = {i['id']: i for i in map(normalize_issue, rows)}
    except urllib.error.HTTPError as e:
        return unavailable(f'Linear answered HTTP {e.code}' + (' (token rejected)' if e.code in (401, 403) else ''))
    except (urllib.error.URLError, OSError) as e:
        return unavailable(f'Linear unreachable ({type(e).__name__})')
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return unavailable(f'Linear answered with a malformed document ({type(e).__name__})')
    return {**ok(issues), 'origin': f'Linear API ({where})'}


# ---- Pi -----------------------------------------------------------------------------------------
def pi_status(url, timeout=5):
    if not url:
        return unavailable('no status_url configured')
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return unavailable(f'{url} unreachable ({type(e).__name__}); the appliance answers only on its own LAN')
    if not isinstance(doc, dict):
        return unavailable(f'{url} returned an unexpected document')
    return ok(doc)
