"""The merge queue: READY_FOR_PR records, WIP limits, merge sets and auto-merge eligibility.

A finished branch is reported READY_FOR_PR (recorded on its worktree's claim) instead of being
opened as a PR. The orchestrator lists the queue and releases an entry when the limits leave room;
only then does the agent open the PR. Everything here is classification and reporting from the
claims, the worktrees and GitHub's own answers: nothing opens, merges or configures anything.

Limits (workflow.json `wip`, with these defaults): one open implementation PR per agent, two open
PRs per repository, of which one substantive and one docs/governance. The PRs of one merge set
count as one change for the per-agent limit.

A merge set is the PRs that must land together: by default the PRs and ready branches of one issue
across repositories, or the entries given the same `--set NAME`. It is mergeable only when every
member is an open PR that is green, current with main and free of conflicts; one member merged
without the others is a SPLIT.
"""
from fnmatch import fnmatch
import re

from . import claims, gitio

SCHEMA = 'ai-workflow.queue/v1'
LIMITS = {'prs_per_agent': 1, 'prs_per_repo': 2, 'substantive_per_repo': 1, 'docs_per_repo': 1}
PATTERNS = {'contract': ['contracts/*', 'protocol.py', '*/protocol.py', 'party_protocol.py', '*/party_protocol.py',
                         'provider/avrana-contract.json'],
            'deployment': ['deploy/*', 'ops/*', '*.service', '*.socket', '*.timer', '*.nginx', '.github/workflows/*']}
DECLARABLE = ('needs-cody', 'adr', 'contract', 'deployment')
SLOT = {'implementation': 'substantive', 'docs': 'docs'}


def classify(files, declared=(), patterns=None):
    """What a change is, from the paths it touches plus what its author declared. Declared classes
    only ever add: nothing an agent says makes a change more boring than its paths show."""
    patterns = patterns or PATTERNS
    classes = set(declared)
    if files is None:
        return {'kind': 'implementation', 'docs_only': False, 'classes': sorted(classes | {'files-unknown'})}
    docs_only = bool(files) and all(f.endswith('.md') or f.startswith('docs/') for f in files)
    if any(re.search(r'(^|/)docs/adr/', f) for f in files):
        classes.add('adr')
    for name, globs in patterns.items():
        if any(fnmatch(f, g) for f in files for g in globs):
            classes.add(name)
    return {'kind': 'docs' if docs_only and not classes else 'implementation', 'docs_only': docs_only, 'classes': sorted(classes)}


def limits(cfg):
    return {**LIMITS, **cfg.get('wip', {})}


def ref(pr):
    return f'{pr["repo"]}#{pr["number"]}'


def entries(ws):
    """Every READY_FOR_PR record in the workspace, with the state of its worktree."""
    out = []
    for name, repo in ws.items():
        for t in repo['data'] if repo['available'] else []:
            ready = (t['claim'] or {}).get('ready') if t['claim_state'] != 'corrupt' else None
            if not ready:
                continue
            out.append({**ready, 'repo': name, 'worktree': t['path'], 'owner': t['claim']['owner'], 'head': t['head'],
                        'ahead': (t['drift'] or (None, None))[0], 'claim_file': t['claim_file'],
                        'key': ready.get('set') or ready.get('issue') or f'{name}:{ready["branch"]}'})
    return out


def _problems(pr):
    out = []
    if pr['draft']:
        out.append(f'{ref(pr)} is a draft')
    if pr['ci'] != 'passed':
        out.append(f'{ref(pr)} ci={pr["ci"]}')
    if pr['behind_main'] is None:
        out.append(f'{ref(pr)}: whether it is behind main is unknown')
    elif pr['behind_main']:
        out.append(f'{ref(pr)} is {pr["behind_main"]} behind main')
    if pr['conflicting']:
        out.append(f'{ref(pr)} conflicts with main')
    return out


def board(cfg, ws, prs, settings, history):
    """The whole picture. `prs` is {repo: open-PR source result}; `settings` is {repo: auto-merge
    source result}; `history(repo, issue)` returns every PR of an issue in a repository."""
    lim = limits(cfg)
    doc = {'schema': SCHEMA, 'limits': lim, 'repos': {}, 'queue': [], 'sets': [], 'unavailable': []}
    ready = entries(ws)
    by_branch = {(e['repo'], e['branch']): e for e in ready}
    owners = {(n, t['branch']): t['claim']['owner'] for n, r in ws.items() if r['available'] for t in r['data']
              if t['branch'] and t['claim'] and t['claim_state'] != 'corrupt'}
    open_prs = []
    for name in cfg['repos']:
        res = prs[name]
        if not res['available']:
            doc['unavailable'].append({'source': f'github:{name}', 'reason': res['reason']})
        rows = [p for p in res['data'] if p['state'] == 'open'] if res['available'] else None
        for p in rows or []:
            entry = by_branch.get((name, p['branch']))
            c = classify(p.get('files'), (entry or {}).get('declared', ()), cfg.get('classes'))
            p.update(kind=c['kind'], docs_only=c['docs_only'], classes=c['classes'], owner=owners.get((name, p['branch'])),
                     ready=bool(entry), key=(entry or {}).get('key') or p['issue'])
            open_prs.append(p)
        got = settings[name]
        if got['available']:
            gaps = [text for flag, text in ((got['data']['allow_auto_merge'], 'allow_auto_merge off'),
                                            (got['data']['required_checks'], 'no required checks')) if not flag]
            auto = {'available': not gaps, 'why': 'auto-merge not available on this repo: ' + '; '.join(gaps) if gaps else ''}
        else:
            auto = {'available': None, 'why': f'whether auto-merge is available is unknown: {got["reason"]}'}
            doc['unavailable'].append({'source': f'github-settings:{name}', 'reason': got['reason']})
        doc['repos'][name] = {'available': rows is not None, 'open': rows or [], 'auto_merge': auto}

    doc['sets'] = _sets(cfg, ws, ready, open_prs, history, doc['unavailable'])
    held = {s['name']: s for s in doc['sets']}
    for p in open_prs:
        mine = held.get(p['key'])
        problems = _problems(p)
        p['mergeable_now'] = not problems and (mine is None or mine['verdict'] == 'mergeable')
        entry = by_branch.get((p['repo'], p['branch']))
        why = ([] if p['docs_only'] else ['not docs-only' + (' (changed files unknown)' if 'files-unknown' in p['classes'] else '')])
        why += [text for cls, text in (('adr', 'touches an ADR (ADR decision)'), ('contract', 'protocol/contract change'),
                                       ('deployment', 'deployment change'), ('needs-cody', 'Needs Cody')) if cls in p['classes']]
        if not entry:
            why.append('no READY_FOR_PR record: Needs Cody was never declared')
        if p['behind_main'] is None:
            why.append('behind main unknown')
        elif p['behind_main']:
            why.append(f'{p["behind_main"]} behind main')
        if p['ci'] != 'passed':
            why.append(f'CI {p["ci"]}' + (': no checks ran' if p['ci'] == 'none' else ''))
        why += [text for flag, text in ((p['draft'], 'draft'), (p['conflicting'], 'conflicts with main')) if flag]
        if mine and mine['verdict'] != 'mergeable':
            why.append(f'merge set {mine["name"]} is not ready ({mine["verdict"]})')
        p['auto_merge'] = {'eligible': not why, 'reasons': why}

    for e in ready:
        pr = next((p for p in open_prs if (p['repo'], p['branch']) == (e['repo'], e['branch'])), None)
        state = 'pr-open' if pr else 'landed' if e['ahead'] == 0 else 'stale' if e['head'] != e['commit'] else \
            'released' if e.get('released') else 'ready'
        blocked = []
        if state == 'stale':
            blocked = ['the branch moved since READY_FOR_PR was recorded: run `ready` again']
        elif state == 'ready':
            blocked = check_release(e, doc, open_prs, lim)
        doc['queue'].append({**{k: e[k] for k in ('issue', 'repo', 'branch', 'commit', 'tests', 'kind', 'classes', 'owner', 'worktree',
                                                  'key', 'at')}, 'set': e.get('set'), 'released': e.get('released'), 'state': state,
                             'can_release': state == 'ready' and not blocked, 'blocked_by': blocked, 'claim_file': e['claim_file']})
    return doc


def check_release(entry, doc, open_prs, lim):
    """Why this entry may not become a PR yet; an empty list means there is room."""
    name = entry['repo']
    if not doc['repos'][name]['available'] or any(not r['available'] for r in doc['repos'].values()):
        return ['GitHub could not be read, so the open PRs are unknown']
    here = [p for p in open_prs if p['repo'] == name]
    out = []
    if len(here) >= lim['prs_per_repo']:
        out.append(f'{name} already has {len(here)} open PRs ({", ".join(map(ref, here))}); the limit is {lim["prs_per_repo"]}')
    slot = SLOT[entry['kind']]
    same = [p for p in here if p['kind'] == entry['kind']]
    if len(same) >= lim[f'{slot}_per_repo']:
        out.append(f'the {slot} slot in {name} is taken by {", ".join(map(ref, same))}')
    if entry['kind'] == 'implementation':
        theirs = [p for p in open_prs if p['owner'] == entry['owner'] and p['kind'] == 'implementation' and p['key'] != entry['key']]
        if len(theirs) >= lim['prs_per_agent']:
            out.append(f'{entry["owner"]} already has an open implementation PR: {", ".join(map(ref, theirs))} '
                       f'(limit {lim["prs_per_agent"]} per agent)')
    return out


def _sets(cfg, ws, ready, open_prs, history, unavailable):
    members = {}                                             # key -> repo -> member, best source wins
    rank = {'open': 0, 'no-pr-ready': 1, 'merged': 2, 'no-pr': 3}

    def put(key, repo, member):
        slot = members.setdefault(key, {})
        if repo not in slot or rank[member['how']] < rank[slot[repo]['how']]:
            slot[repo] = member

    named = {e['set'] for e in ready if e.get('set')}
    issues = {}                                              # key -> the issues whose PR history belongs to it
    for e in ready:
        put(e['key'], e['repo'], {'how': 'no-pr-ready', 'ref': f'{e["repo"]}:{e["branch"]}', 'state': 'ready, no PR', 'problems':
                                  [f'{e["repo"]}: {e["branch"]} is READY_FOR_PR but has no PR yet']})
        if e.get('issue'):
            issues.setdefault(e['key'], set()).add(e['issue'])
    for p in open_prs:
        if p['key']:
            put(p['key'], p['repo'], {'how': 'open', 'ref': ref(p), 'state': 'open', 'problems': _problems(p)})
            if p['issue']:
                issues.setdefault(p['key'], set()).add(p['issue'])
    for key, wanted in issues.items():
        for name in cfg['repos']:
            for issue in sorted(wanted):
                res = history(name, issue)
                if not res['available']:
                    if not any(u['source'] == f'github:{name}' for u in unavailable):
                        unavailable.append({'source': f'github:{name}', 'reason': res['reason']})
                    continue
                for p in sorted(res['data'], key=lambda p: -p['number']):
                    if p['state'] == 'merged':
                        put(key, name, {'how': 'merged', 'ref': ref(p), 'state': 'merged', 'problems': []})
                        break
            for t in ws[name]['data'] if ws[name]['available'] else []:
                if t['issue'] in wanted and t['branch'] and (t['drift'] or (0, 0))[0] > 0:
                    put(key, name, {'how': 'no-pr', 'ref': f'{name}:{t["branch"]}', 'state': 'no PR', 'problems':
                                    [f'{name}: {t["branch"]} has commits but no PR yet and is not READY_FOR_PR']})
    out = []
    for key, slot in sorted(members.items()):
        if len(slot) < 2 and key not in named:
            continue
        rows = [slot[r] for r in sorted(slot)]
        merged = [m for m in rows if m['how'] == 'merged']
        reasons = [p for m in rows for p in m['problems']]
        if len(merged) == len(rows):
            continue                                         # landed together: nothing to report
        if merged:
            verdict = 'SPLIT'
            reasons.insert(0, f'{", ".join(m["ref"] for m in merged)} merged without '
                              f'{", ".join(m["ref"] for m in rows if m["how"] != "merged")}')
        else:
            verdict = 'waiting' if reasons else 'mergeable'
        out.append({'name': key, 'verdict': verdict, 'reasons': reasons,
                    'members': [{'repo': r, 'ref': slot[r]['ref'], 'state': slot[r]['state']} for r in sorted(slot)]})
    return out


def record(file, owner, tree, tests, declared, set_name, files, cfg, now=None):
    """Write READY_FOR_PR onto our own claim; returns the Outcome."""
    c = classify(files, declared, cfg.get('classes'))
    ready = {'issue': tree['issue'], 'branch': tree['branch'], 'commit': tree['head'], 'tests': tests, 'kind': c['kind'],
             'docs_only': c['docs_only'], 'classes': c['classes'], 'declared': sorted(declared), 'files': len(files or []),
             'set': set_name, 'at': claims.stamp(now or claims.utcnow())}
    return claims.annotate(file, owner, ready=ready)


def render(doc):
    out = [f'Queue: READY_FOR_PR ({len(doc["queue"])})']
    for e in doc['queue']:
        verdict = {'ready': 'can be released' if e['can_release'] else 'blocked: ' + '; '.join(e['blocked_by']),
                   'stale': 'stale: ' + '; '.join(e['blocked_by']), 'released': f'released by {(e["released"] or {}).get("by")}: open the PR',
                   'pr-open': 'PR open', 'landed': 'landed on main'}[e['state']]
        out.append(f'  {e["issue"] or "-":<8} {e["repo"]:<6} {e["branch"]:<44} {e["owner"]:<16} {e["kind"]:<14} {verdict}')
    out.append('Open PRs')
    lim = doc['limits']
    for name, repo in doc['repos'].items():
        if not repo['available']:
            out.append(f'  {name}: UNAVAILABLE')
            continue
        def slot_text(slot):
            held = [ref(p) for p in repo['open'] if SLOT[p['kind']] == slot]
            over = f' (OVER the limit of {lim[slot + "_per_repo"]})' if len(held) > lim[f'{slot}_per_repo'] else ''
            return f'{slot}: {", ".join(held) or "free"}{over}'

        slots = ', '.join(map(slot_text, ('substantive', 'docs')))
        out.append(f'  {name}  {len(repo["open"])}/{lim["prs_per_repo"]}  {slots}')
        for p in repo['open']:
            am = p['auto_merge']
            out.append(f'    {ref(p):<10} ci={p["ci"]:<8} {p["kind"]:<14} {p["owner"] or "owner unknown":<16} '
                       f'{"mergeable now" if p["mergeable_now"] else "not mergeable yet"}; '
                       f'auto-merge: {"eligible" if am["eligible"] else "no (" + "; ".join(am["reasons"]) + ")"}')
        if repo['auto_merge']['why']:
            out.append(f'    {repo["auto_merge"]["why"]}')
    if doc['sets']:
        out.append('Merge sets')
        for s in doc['sets']:
            out.append(f'  {s["name"]}  {s["verdict"]}: ' + ', '.join(f'{m["ref"]} {m["state"]}' for m in s['members']))
            out += [f'    {"!! " if s["verdict"] == "SPLIT" else ""}{r}' for r in s['reasons']]
    if doc['unavailable']:
        out += [f'Could not check ({len(doc["unavailable"])})'] + [f'  {u["source"]}: {u["reason"]}' for u in doc['unavailable']]
    return out


def changed_files(worktree, base='origin/main'):
    rc, out = gitio.git(worktree, 'diff', '--name-only', f'{base}...HEAD')
    return out.splitlines() if rc == 0 else None
