"""The derived state model: what is being worked on, where, by whom, and what needs the owner.

Nothing here is stored. Every function is a pure reading of git worktrees, claims and the
source documents handed to it, so the same inputs always give the same answer.
"""
import json
import os
from pathlib import Path
import re

from . import claims, gitio, sources

ROOT = Path(__file__).resolve().parents[1]


def load_config(path=None, root=None):
    cfg = json.loads(Path(path or os.environ.get('AVRANA_WORKFLOW_CONFIG') or ROOT / 'workflow.json').read_text(encoding='utf-8'))
    base = Path(root or os.environ.get('AVRANA_ROOT') or ROOT.parent)
    for repo in cfg['repos'].values():
        repo['path'] = str(base / repo['dir'])
    cfg['root'] = str(base)
    return cfg


def owner_identity(explicit=None, env=None):
    """(owner label, agent kind) for this session, or (None, None). Opaque labels only."""
    env = os.environ if env is None else env
    if explicit:
        return explicit, explicit.split('-', 1)[0] if '-' in explicit else 'agent'
    if env.get('AVRANA_SESSION'):
        label = env['AVRANA_SESSION']
        return label, label.split('-', 1)[0] if '-' in label else 'agent'
    if env.get('CLAUDE_CODE_SESSION_ID'):
        return f'claude-{env["CLAUDE_CODE_SESSION_ID"][:8]}', 'claude'
    return None, None


def workspace(cfg, now=None):
    """Every worktree of every configured repository with its claim and condition."""
    out = {}
    for name, repo in cfg['repos'].items():
        rows = gitio.worktrees(repo['path'])
        if rows is None:
            out[name] = {'available': False, 'reason': f'no git checkout at {repo["path"]}'}
            continue
        common = gitio.common_dir(repo['path'])
        trees = []
        for wt in rows:
            claim = claims.read(claims.claim_file(common, wt['path']))
            trees.append({**wt, 'repo': name, 'issue': gitio.issue_of(wt['branch']),
                          'missing': not os.path.isdir(wt['path']),
                          'claim': claim, 'claim_state': claims.state(claim, now),
                          'busy': gitio.busy(wt['path']) if os.path.isdir(wt['path']) else None,
                          'drift': gitio.drift(wt['path']) if os.path.isdir(wt['path']) else None,
                          'claim_file': str(claims.claim_file(common, wt['path']))})
        out[name] = {'available': True, 'data': trees, 'common_dir': common,
                     'main': gitio.rev(repo['path'], 'origin/main')}
    return out


def trees_for(ws, issue):
    return [t for repo in ws.values() if repo['available'] for t in repo['data'] if t['issue'] == issue]


def lifecycle(issue, decisions):
    """The loop state of docs/WORKFLOW.md (Party) from Linear's state, labels and Open Decisions."""
    state, labels = issue.get('state'), issue.get('labels') or []
    if state in ('Done', 'Canceled', 'Duplicate'):
        return 'done'
    if state == 'In Review':
        return 'ready-for-playtest' if 'Human Validation' in labels else 'pr-ci'
    if state == 'In Progress':
        return 'in-progress'
    if state == 'Todo':
        return {'none': 'ready-for-agent', 'unresolved': 'needs-cody'}.get(decisions, 'todo-unverified')
    return 'needs-cody' if state == 'Backlog' else 'unknown'


def pair(prs):
    """Group PRs by head branch: the same branch name in both repositories is a paired change."""
    groups = {}
    for pr in prs:
        groups.setdefault(pr['branch'], []).append(pr)
    return [{'branch': b, 'paired': len({p['repo'] for p in rows}) > 1, 'prs': rows} for b, rows in groups.items()]


def issue_context(cfg, issue_id, ws, linear, prs_by_repo, pi, me=None, now=None):
    """Structured context for one issue. Unavailable sources stay marked unavailable."""
    ctx = {'schema': 'avrana.issue-context/v1', 'issue': issue_id, 'unavailable': [], 'blockers': [], 'notes': []}
    issue = linear['data'].get(issue_id) if linear['available'] else None
    if not linear['available']:
        ctx['unavailable'].append({'source': 'linear', 'reason': linear['reason']})
        ctx['linear'] = None
        ctx['blockers'].append('Linear issue not read: scope, acceptance and Open Decisions are unknown')
    elif issue is None:
        ctx['linear'] = None
        ctx['unavailable'].append({'source': 'linear', 'reason': f'{issue_id} is not in {linear.get("origin", "the Linear read")}'})
        ctx['blockers'].append(f'{issue_id} not found in Linear read')
    else:
        secs = sources.sections(issue['description'])
        decisions = sources.open_decisions(issue['description'])
        ctx['linear'] = {**{k: issue[k] for k in ('title', 'state', 'labels', 'url', 'blocked_by')},
                         'origin': linear.get('origin'), 'sections': secs, 'open_decisions': decisions,
                         'lifecycle': lifecycle(issue, decisions)}
        if decisions == 'unresolved':
            ctx['blockers'].append('Open Decisions holds an unresolved question: stop and ask Cody')
        elif decisions == 'missing':
            ctx['notes'].append('the issue has no Open Decisions section, so "no open decisions" is unconfirmed')
        missing = [s for s in ('Repositories', 'Tests Required', 'Acceptance Criteria') if secs[s] is None]
        if missing and issue['description'] is not None:
            ctx['notes'].append(f'issue template sections missing: {", ".join(missing)}')
        if issue['state'] == 'Backlog':
            ctx['blockers'].append('issue is in Backlog: not scheduled for an agent')
        if issue['blocked_by'] is None:
            ctx['notes'].append('dependencies were not part of this Linear read')
        for dep in issue['blocked_by'] or []:
            other = linear['data'].get(dep)
            if other is None or not other.get('state'):
                ctx['notes'].append(f'blocked by {dep}, whose state is unavailable in this read')
            elif other['state'] not in ('Done', 'Canceled', 'Duplicate'):
                ctx['blockers'].append(f'blocked by {dep} ({other["state"]})')

    # where the work lives
    trees = trees_for(ws, issue_id)
    ctx['worktrees'] = [{k: t[k] for k in ('repo', 'path', 'branch', 'head', 'busy', 'drift', 'claim_state')}
                        | {'owner': (t['claim'] or {}).get('owner')} for t in trees]
    for t in trees:
        if t['claim_state'] in ('held', 'handoff') and (t['claim'] or {}).get('owner') != me:
            ctx['blockers'].append(f'{t["repo"]} worktree {t["path"]} is claimed by {claims.describe(t["claim"], now)}')
        if t['claim_state'] == 'stale':
            ctx['notes'].append(f'{t["repo"]} worktree {t["path"]} has a stale claim by {t["claim"]["owner"]}')
    for name, repo in ws.items():
        if not repo['available']:
            ctx['unavailable'].append({'source': f'git:{name}', 'reason': repo['reason']})

    prs = []
    for name, res in prs_by_repo.items():
        if res['available']:
            prs += res['data']
        else:
            ctx['unavailable'].append({'source': f'github:{name}', 'reason': res['reason']})
    ctx['prs'] = pair(prs)

    declared = (ctx['linear'] or {}).get('sections', {}).get('Repositories') if ctx['linear'] else None
    named = set(re.findall(r'[a-z][a-z0-9-]+', declared or ''))
    repos = sorted(n for n, r in cfg['repos'].items() if r['dir'] in named)
    if repos:
        ctx['repositories'] = {'value': repos, 'basis': 'Repositories section of the issue'}
    else:
        seen = sorted({t['repo'] for t in trees} | {p['repo'] for p in prs})
        ctx['repositories'] = {'value': seen or None,
                               'basis': 'existing branches and PRs' if seen else 'undetermined: the issue does not declare Repositories and no branch exists yet'}

    ctx['main'] = {n: r.get('main') for n, r in ws.items() if r['available']}
    ctx['instructions'] = {n: str(Path(r['path']) / 'AGENTS.md') for n, r in cfg['repos'].items()}
    if pi['available']:
        ctx['deployed'] = {n: {'sha': (pi['data'].get(n) or {}).get('deployed_sha') or (pi['data'].get(n) or {}).get('checkout_sha'),
                               'matches_main': ((pi['data'].get(n) or {}).get('deployed_sha') or (pi['data'].get(n) or {}).get('checkout_sha')) == ctx['main'].get(n)}
                           for n in cfg['repos']}
    else:
        ctx['deployed'] = None
        ctx['unavailable'].append({'source': 'pi', 'reason': pi['reason']})
    ctx['ready'] = not ctx['blockers']
    return ctx


def needs_cody(cfg, ws, linear, prs_by_repo, pi, now=None):
    """Split what is open into what needs the owner, what an agent should do, and what could
    not be checked. An unchecked source contributes to `unavailable`, never to an empty queue."""
    cody, agent, unavailable = [], [], []

    def add(bucket, kind, ref, text):
        bucket.append({'kind': kind, 'ref': ref, 'text': text})

    for name, res in prs_by_repo.items():
        if not res['available']:
            unavailable.append({'source': f'github:{name}', 'reason': res['reason']})
            continue
        slug = cfg['repos'][name]['slug']
        for pr in res['data']:
            if pr['state'] != 'open':
                continue
            ref = f'{slug}#{pr["number"]}'
            if pr['ci'] == 'failed':
                add(agent, 'ci-failed', ref, f'CI failed on "{pr["title"]}"')
            elif pr['conflicting']:
                add(agent, 'conflict', ref, f'"{pr["title"]}" conflicts with main')
            elif pr['ci'] == 'running':
                add(agent, 'ci-running', ref, f'CI still running on "{pr["title"]}"')
            elif pr['draft']:
                add(agent, 'draft', ref, f'draft: "{pr["title"]}"')
            elif pr['ci'] == 'none':
                add(agent, 'ci-none', ref, f'no CI result reported for "{pr["title"]}"; not presented as ready')
            else:
                add(cody, 'review', ref, f'CI passed; review and merge decision: "{pr["title"]}" ({pr["url"]})')

    if linear['available']:
        for ident, issue in sorted(linear['data'].items(), key=lambda kv: int(kv[0].split('-')[1])):
            decisions = sources.open_decisions(issue['description'])
            phase = lifecycle(issue, decisions)
            if decisions == 'unresolved' and phase != 'done':
                add(cody, 'decision', ident, f'Open Decisions unresolved: "{issue["title"]}"')
            elif phase == 'ready-for-playtest':
                add(cody, 'playtest', ident, f'real-device validation: "{issue["title"]}"')
            elif phase == 'todo-unverified' and decisions == 'missing':
                add(agent, 'issue-hygiene', ident, 'Todo without an Open Decisions section; rewrite to the template before dispatch')
        unread = [i for i, issue in linear['data'].items() if issue['description'] is None
                  and lifecycle(issue, 'unknown') != 'done']
        if unread:
            unavailable.append({'source': 'linear:open-decisions', 'reason': f'{len(unread)} open issue(s) were read without '
                                'their description, so unresolved Open Decisions cannot be ruled out: ' + ', '.join(sorted(unread, key=lambda i: int(i.split('-')[1])))})
    else:
        unavailable.append({'source': 'linear', 'reason': linear['reason']})

    if pi['available']:
        for name, repo in ws.items():
            dep = pi['data'].get(name) or {}
            sha = dep.get('deployed_sha') or dep.get('checkout_sha')
            if repo['available'] and sha and repo.get('main') and sha != repo['main']:
                add(cody, 'deploy', name, f'main {repo["main"][:12]} is not deployed (Pi has {sha[:12]}); deployment is an owner action')
        if (pi['data'].get('summary') or {}).get('state') == 'degraded':
            add(cody, 'appliance', 'pi', 'appliance degraded: ' + '; '.join(pi['data']['summary'].get('reasons', [])))
    else:
        unavailable.append({'source': 'pi', 'reason': pi['reason']})

    for name, repo in ws.items():
        if not repo['available']:
            unavailable.append({'source': f'git:{name}', 'reason': repo['reason']})
            continue
        for t in repo['data']:
            if t['claim_state'] == 'stale' and t['busy']:
                add(cody, 'takeover', t['path'], f'stale claim by {t["claim"]["owner"]} on a worktree with {t["busy"]}; '
                    'decide whether that work is kept before anyone takes it over')
            elif t['claim_state'] == 'corrupt':
                add(cody, 'takeover', t['path'], f'unreadable claim file {t["claim_file"]}')
    return {'schema': 'avrana.needs-cody/v1', 'needs_cody': cody, 'agent': agent, 'unavailable': unavailable}
