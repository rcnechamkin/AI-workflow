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
STATES = {'needs-cody': 'Needs Cody', 'blocked': 'Blocked', 'ready-for-agent': 'Ready for Agent',
          'in-progress': 'In Progress', 'pr-ci': 'PR / CI', 'ready-for-playtest': 'Ready for Playtest',
          'done': 'Done', 'unknown': 'Unknown / incomplete evidence'}


def load_config(path=None, root=None):
    cfg = json.loads(Path(path or os.environ.get('AI_WORKFLOW_CONFIG') or ROOT / 'workflow.json').read_text(encoding='utf-8'))
    base = Path(root or os.environ.get('AI_WORKFLOW_ROOT') or ROOT.parent)
    for repo in cfg['repos'].values():
        repo['path'] = str(base / repo['dir'])
    cfg['root'] = str(base)
    gitio.set_prefix(cfg.get('issue_prefix', 'AVR'))
    return cfg


def ttl(cfg):
    return cfg.get('claim_ttl_hours', claims.DEFAULT_TTL_HOURS)


def owner_identity(explicit=None, env=None, agent=None, session=None):
    """(owner label, agent kind) for this session, or (None, None). Opaque labels only."""
    env = os.environ if env is None else env
    if session:
        kind = agent or 'agent'
        return f'{kind}-{re.sub(r"[^A-Za-z0-9]", "", session)[:8]}', kind
    label = explicit or env.get('AI_WORKFLOW_SESSION')
    if label:
        return label, agent or (label.split('-', 1)[0] if '-' in label else 'agent')
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
            there = os.path.isdir(wt['path'])
            trees.append({**wt, 'repo': name, 'issue': gitio.issue_of(wt['branch']), 'missing': not there,
                          'claim': claim, 'claim_state': claims.state(claim, now),
                          'busy': gitio.busy(wt['path']) if there else None,
                          'drift': gitio.drift(wt['path']) if there else None,
                          'claim_file': str(claims.claim_file(common, wt['path']))})
        out[name] = {'available': True, 'data': trees, 'common_dir': common,
                     'main': gitio.rev(repo['path'], 'origin/main')}
    return out


def trees_for(ws, issue):
    return [t for repo in ws.values() if repo['available'] for t in repo['data'] if t['issue'] == issue]


def pair(prs):
    """Group PRs into changes. The same head branch in two repositories is one paired change; open
    PRs of one issue that sit alone in different repositories are paired by the issue."""
    groups = {}
    for pr in prs:
        groups.setdefault(pr['branch'], []).append(pr)
    out = [{'branch': b, 'issue': rows[0]['issue'], 'prs': rows} for b, rows in groups.items()]
    lone = {}
    for g in out:
        if len({p['repo'] for p in g['prs']}) == 1 and g['issue'] and all(p['state'] == 'open' for p in g['prs']):
            lone.setdefault(g['issue'], []).append(g)
    for issue, same in lone.items():
        if len(same) > 1 and len({g['prs'][0]['repo'] for g in same}) == len(same):
            for g in same[1:]:
                same[0]['prs'] += g['prs']
                same[0]['branch'] += f' + {g["branch"]}'
                out.remove(g)
    for g in out:
        g['paired'] = len({p['repo'] for p in g['prs']}) > 1
    return out


def repositories(cfg, issue, trees, prs, override=None):
    """Which configured repositories the issue touches, and on what evidence."""
    if override:
        return {'value': sorted(override), 'basis': 'given on the command line'}
    declared = sources.sections(issue['description'])['Repositories'] if issue and issue.get('description') else None
    named = set(re.findall(r'[a-z][a-z0-9-]+', declared or ''))
    repos = sorted(n for n, r in cfg['repos'].items() if r['dir'] in named)
    if repos:
        return {'value': repos, 'basis': 'Repositories section of the issue'}
    seen = sorted({t['repo'] for t in trees} | {p['repo'] for p in prs})
    if seen:
        return {'value': seen, 'basis': 'existing branches and PRs (the issue does not declare Repositories)'}
    return {'value': None, 'basis': 'undetermined: the issue does not declare Repositories and no branch exists yet'}


def readiness(issue, decisions, repos, linear_data, trees, prs, github_ok, me=None, now=None, decisions_confirmed=False):
    """One state from Linear, GitHub and git, with the evidence. No state of its own:

    done, ready-for-playtest, pr-ci, needs-cody, blocked, in-progress, ready-for-agent, or
    unknown when the evidence for any of those is incomplete. `can_start` says whether an agent
    may begin (or resume) now.
    """
    reasons, missing = [], []

    def result(state, can_start=False):
        return {'state': state, 'label': STATES[state], 'can_start': can_start, 'reasons': reasons, 'missing': missing}

    if issue is None:
        missing.append('the Linear issue')
        return result('unknown')
    if not github_ok:
        missing.append('GitHub pull-request state')
    open_prs = [p for p in prs if p['state'] == 'open']
    merged = [p for p in prs if p['state'] == 'merged']
    labels = issue.get('labels') or []
    if issue['state'] in sources.TERMINAL:
        reasons.append(f'Linear state {issue["state"]}')
        if open_prs:
            reasons.append('but PR(s) still open: ' + ', '.join(f'{p["repo"]}#{p["number"]}' for p in open_prs))
        return result('done')
    if issue['state'] == 'In Review' and 'Human Validation' in labels and not open_prs:
        reasons.append('In Review with label Human Validation: real-device acceptance pending')
        return result('ready-for-playtest')
    if open_prs:
        reasons += [f'{p["repo"]}#{p["number"]} open, CI {p["ci"]}' for p in open_prs]
        return result('pr-ci')
    if issue['state'] == 'In Review':
        reasons.append('Linear state In Review' + (f'; {len(merged)} PR(s) merged, none open' if merged else '; no PR found'))
        return result('pr-ci')
    if decisions == 'unresolved' and not decisions_confirmed:
        reasons.append('Open Decisions holds an unresolved question for the owner')
        return result('needs-cody')
    if issue['state'] == 'Backlog':
        reasons.append('Linear state Backlog: not scheduled for an agent')
        return result('needs-cody')
    if issue['state'] not in ('Todo', 'In Progress'):
        missing.append(f'a meaning for Linear state {issue["state"]!r}')
        return result('unknown')

    blockers = []
    if issue['blocked_by'] is None:
        missing.append('the issue\'s dependencies (this Linear read did not include relations)')
    for dep in issue['blocked_by'] or []:
        state = issue['dependency_states'].get(dep) or (linear_data.get(dep) or {}).get('state')
        if not state:
            missing.append(f'the state of dependency {dep}')
        elif state not in sources.TERMINAL:
            blockers.append(f'{dep} ({state})')
    if blockers:
        reasons.append('blocked by ' + ', '.join(blockers))
        return result('blocked')

    foreign = [t for t in trees if t['claim_state'] in ('held', 'handoff') and t['claim']['owner'] != me
               and not (t['claim_state'] == 'handoff' and t['claim']['handoff_to'] in ('any', me))]
    abandoned = [t for t in trees if t['claim_state'] == 'stale' and t['claim']['owner'] != me and t['busy']]
    unreadable = [t for t in trees if t['claim_state'] == 'corrupt']
    active = [t for t in trees if t['busy'] or (t['drift'] and t['drift'][0]) or t['claim_state'] != 'free']
    if foreign or abandoned or unreadable:
        reasons += [f'{t["repo"]} worktree {t["path"]} is claimed by {claims.describe(t["claim"], now)}' for t in foreign]
        reasons += [f'{t["repo"]} worktree {t["path"]} has a stale claim by {t["claim"]["owner"]} over {t["busy"]}: '
                    'taking it over is an owner decision' for t in abandoned]
        reasons += [f'{t["repo"]} worktree {t["path"]} has an unreadable claim file' for t in unreadable]
        return result('in-progress')
    if issue['state'] == 'In Progress' or active:
        if issue['state'] == 'In Progress':
            reasons.append('Linear state In Progress')
        reasons += [f'{t["repo"]} worktree {t["path"]}: ' + ', '.join(filter(None, [
            t['busy'], f'{t["drift"][0]} commit(s) ahead of main' if t['drift'] and t['drift'][0] else None,
            f'claim {t["claim_state"]}' if t['claim_state'] != 'free' else None])) for t in active]
        if not active:
            reasons.append('no worktree holds the work: nothing to resume, start fresh')
        return result('in-progress', can_start=not missing and decisions != 'unresolved')

    if decisions in ('missing', 'unknown') and not decisions_confirmed:
        missing.append('an Open Decisions section saying none (the issue has no such section)' if decisions == 'missing'
                       else 'the issue description (Open Decisions could not be read)')
    if not repos['value']:
        missing.append('which repositories the issue touches (no Repositories section; pass --repo)')
    if missing:
        return result('unknown')
    reasons.append('Todo, no open decisions, no unfinished dependency, no other session on it')
    return result('ready-for-agent', can_start=True)


def issue_context(cfg, issue_id, ws, linear, prs_by_repo, pi, me=None, now=None, repos_override=None, decisions_confirmed=False):
    """Structured context for one issue. Unavailable sources stay marked unavailable."""
    ctx = {'schema': 'ai-workflow.issue-context/v1', 'issue': issue_id, 'unavailable': []}
    issue = linear['data'].get(issue_id) if linear['available'] else None
    decisions = 'unknown'
    if not linear['available']:
        ctx['unavailable'].append({'source': 'linear', 'reason': linear['reason']})
    elif issue is None:
        ctx['unavailable'].append({'source': 'linear', 'reason': f'{issue_id} is not in {linear.get("origin", "the Linear read")}'})
    if issue:
        decisions = sources.open_decisions(issue['description'])
        ctx['linear'] = {**{k: issue[k] for k in ('title', 'state', 'labels', 'url', 'project', 'milestone', 'parent', 'blocked_by')},
                         'origin': linear.get('origin'), 'open_decisions': decisions,
                         'sections': sources.sections(issue['description'])}
    else:
        ctx['linear'] = None

    trees = trees_for(ws, issue_id)
    ctx['worktrees'] = [{k: t[k] for k in ('repo', 'path', 'branch', 'head', 'busy', 'drift', 'claim_state')}
                        | {'owner': (t['claim'] or {}).get('owner')} for t in trees]
    for name, repo in ws.items():
        if not repo['available']:
            ctx['unavailable'].append({'source': f'git:{name}', 'reason': repo['reason']})

    prs, github_ok = [], True
    for name, res in prs_by_repo.items():
        if res['available']:
            prs += res['data']
        else:
            github_ok = False
            ctx['unavailable'].append({'source': f'github:{name}', 'reason': res['reason']})
    ctx['prs'] = pair(prs)
    ctx['repositories'] = repositories(cfg, issue, trees, prs, repos_override)
    ctx['main'] = {n: r.get('main') for n, r in ws.items() if r['available']}
    ctx['instructions'] = {n: 'AGENTS.md' for n in cfg['repos']}
    if pi['available']:
        ctx['deployed'] = {}
        for n in cfg['repos']:
            sha = (pi['data'].get(n) or {}).get('deployed_sha') or (pi['data'].get(n) or {}).get('checkout_sha')
            ctx['deployed'][n] = {'sha': sha, 'matches_main': bool(sha) and sha == ctx['main'].get(n)}
    else:
        ctx['deployed'] = None
        ctx['unavailable'].append({'source': 'pi', 'reason': pi['reason']})
    ctx['readiness'] = readiness(issue, decisions, ctx['repositories'], linear['data'] if linear['available'] else {},
                                 trees, prs, github_ok, me=me, now=now, decisions_confirmed=decisions_confirmed)
    return ctx


def needs_cody(cfg, ws, linear, prs_by_repo, pi, now=None):
    """The owner's cockpit: what needs a human decision now, what agents are doing, and what could
    not be checked. An unchecked source goes to `unavailable`, never to an empty queue."""
    cody, agent, unavailable = [], [], []

    def add(bucket, kind, ref, text):
        bucket.append({'kind': kind, 'ref': ref, 'text': text})

    def ref(pr):
        return f'{cfg["repos"][pr["repo"]]["slug"]}#{pr["number"]}'

    issues = linear['data'] if linear['available'] else {}
    open_prs = []
    for name, res in prs_by_repo.items():
        if res['available']:
            open_prs += [p for p in res['data'] if p['state'] == 'open']
        else:
            unavailable.append({'source': f'github:{name}', 'reason': res['reason']})
    for group in pair(open_prs):
        rows = group['prs']
        refs = ' + '.join(ref(p) for p in rows)
        title = rows[0]['title']
        problems = [(p, kind) for p in rows for kind, bad in (
            ('ci-failed', p['ci'] == 'failed'), ('conflict', p['conflicting']), ('ci-running', p['ci'] == 'running'),
            ('draft', p['draft']), ('ci-none', p['ci'] == 'none')) if bad]
        if problems:
            p, kind = problems[0]
            add(agent, kind, refs, {'ci-failed': 'CI failed', 'conflict': 'conflicts with main', 'ci-running': 'CI running',
                                    'draft': 'draft', 'ci-none': 'no CI result reported; not presented as ready'}[kind]
                + f' ({ref(p)}): "{title}"')
            continue
        behind = [f'{ref(p)} is {p["behind_main"]} behind main' for p in rows if p['behind_main']]
        unknown = [ref(p) for p in rows if p['behind_main'] is None or p['conflicting'] is None]
        order = ' Paired change: merge Party first unless the PRs say otherwise.' if group['paired'] else ''
        add(cody, 'review', refs, f'CI passed; review and merge decision: "{title}".{order}'
            + (f' Note: {"; ".join(behind)}.' if behind else '')
            + (f' Mergeability not confirmed by GitHub for {", ".join(unknown)}.' if unknown else ''))
        linked = issues.get(group['issue'])
        if linked and linked['state'] in sources.TERMINAL:
            add(cody, 'conflict', group['issue'], f'Linear says {linked["state"]} but {refs} is still open: which is right?')

    if linear['available']:
        for ident, issue in sorted(issues.items(), key=lambda kv: int(kv[0].split('-')[1])):
            if issue['state'] in sources.TERMINAL:
                continue
            decisions = sources.open_decisions(issue['description'])
            if decisions == 'unresolved':
                add(cody, 'decision', ident, f'Open Decisions unresolved: "{issue["title"]}"')
            elif issue['state'] == 'In Review' and 'Human Validation' in issue['labels'] \
                    and not any(p['issue'] == ident for p in open_prs):
                add(cody, 'playtest', ident, f'real-device validation: "{issue["title"]}"')
            elif issue['state'] == 'Todo' and decisions == 'missing':
                add(agent, 'issue-hygiene', ident, 'Todo without an Open Decisions section: not startable until the issue follows the template')
        unread = sorted((i for i, issue in issues.items() if issue['description'] is None and issue['state'] not in sources.TERMINAL),
                        key=lambda i: int(i.split('-')[1]))
        if unread:
            unavailable.append({'source': 'linear:open-decisions', 'reason': f'{len(unread)} open issue(s) were read without their '
                                'description, so unresolved Open Decisions cannot be ruled out: ' + ', '.join(unread)})
    else:
        unavailable.append({'source': 'linear', 'reason': linear['reason']})

    if pi['available']:
        for name, repo in ws.items():
            dep = pi['data'].get(name) or {}
            sha = dep.get('deployed_sha') or dep.get('checkout_sha')
            if repo['available'] and sha and repo.get('main') and sha != repo['main']:
                add(cody, 'deploy', name, f'main {repo["main"][:12]} is not deployed (the appliance has {sha[:12]}); deployment is an owner action')
        if (pi['data'].get('summary') or {}).get('state') == 'degraded':
            add(cody, 'appliance', 'pi', 'appliance degraded: ' + '; '.join(pi['data']['summary'].get('reasons', [])))
    else:
        unavailable.append({'source': 'pi', 'reason': pi['reason']})

    for name, repo in ws.items():
        if not repo['available']:
            unavailable.append({'source': f'git:{name}', 'reason': repo['reason']})
            continue
        for t in repo['data']:
            claim, where = t['claim'], Path(t['path']).name
            if t['claim_state'] == 'corrupt':
                add(cody, 'takeover', where, f'unreadable claim file {t["claim_file"]}')
            elif t['claim_state'] == 'stale' and t['busy']:
                add(cody, 'takeover', where, f'abandoned work: stale claim by {claim["owner"]} over {t["busy"]}; decide whether it is '
                    'kept before anyone takes the worktree over')
            elif t['claim_state'] == 'handoff' and claim['handoff_to'] in ('cody', 'owner'):
                add(cody, 'blocked-agent', where, f'{claim["owner"]} stopped on {claim.get("issue") or t["branch"]} and needs judgment: '
                    f'{claim.get("handoff_note") or "(no note)"}')
            elif t['claim_state'] in ('held', 'handoff'):
                add(agent, 'working', where, f'{claim["owner"]} on {claim.get("issue") or t["branch"]}'
                    + (f', offered to {claim["handoff_to"]}: {claim.get("handoff_note", "")}' if claim.get('handoff_to') else ''))
    return {'schema': 'ai-workflow.needs-cody/v1', 'needs_cody': cody, 'agent': agent, 'unavailable': unavailable}
