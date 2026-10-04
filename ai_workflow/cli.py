"""ai-workflow: cross-project development control. One place that answers what is being worked
on, by whom, in which worktree, in what state, and what needs the owner.

    python aw.py setup [--check | --uninstall | --warn-only] [--chain]   install the commit hooks; verify the setup
    python aw.py start AVR-236 [--repo party|games|both]   readiness, worktrees, claims, context: then "Implement AVR-236"
    python aw.py issue AVR-236                             readiness and structured context, changing nothing
    python aw.py context AVR-236                           what to read first, with provenance; changing nothing
    python aw.py status                                    every worktree: branch, issue, claim, drift, unfinished work
    python aw.py claim [AVR-236] | release | handoff --to X --note "..."
    python aw.py ready [AVR-236] --tests "unit 40/40"      report READY_FOR_PR instead of opening a PR
    python aw.py queue [release AVR-236]                   the merge queue, WIP limits, merge sets, auto-merge eligibility
    python aw.py prs [AVR-237]                             PRs, CI, review and pairing across repositories
    python aw.py needs-cody                                the owner's queue; agent work; what could not be checked
    python aw.py hook pre-commit                           what the installed git hooks run
    python aw.py guard                                     optional Claude Code PreToolUse hook (hook JSON on stdin)

Options go after the command; `--json` gives every answer as data. Writes only local claim
files, git hooks (setup) and new branches/worktrees (start). Never pushes, opens or merges a PR,
enables auto-merge, changes a repository setting, deploys, or writes to Linear or GitHub. Exit codes: 0 ok, 2 usage, 3 refused, 4 a required source was
unavailable.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from . import claims, context, gitio, guard, hooks, model, queue, sources, tokens

AW = Path(__file__).resolve().parents[1] / 'aw.py'


class Sources:
    """The outside world; tests replace this."""
    prs = staticmethod(sources.prs)
    linear = staticmethod(sources.linear)
    pi_status = staticmethod(sources.pi_status)
    automerge = staticmethod(lambda repo, slug: sources.automerge(slug))
    fetch_main = staticmethod(gitio.fetch_main)


SOURCES = Sources


def emit(args, doc, text):
    print(json.dumps(doc, indent=2) if args.json else text)


def need_owner(args):
    owner, agent = model.owner_identity(args.owner, agent=getattr(args, 'agent', None), session=getattr(args, 'session', None))
    if not owner:
        print('no session identity: pass --agent NAME --session ID, or set AI_WORKFLOW_SESSION (for example codex-1a2b3c4d)',
              file=sys.stderr)
    return owner, agent


def need_issue(text):
    issue = gitio.issue_id(text)
    if not issue:
        print(f'expected an issue id like {gitio.PREFIX}-236', file=sys.stderr)
    return issue


def gather(cfg, issue, args):
    prs = {name: SOURCES.prs(name, repo['slug'], issue) for name, repo in cfg['repos'].items()}
    return SOURCES.linear(issue, snapshot=args.linear_snapshot), prs, SOURCES.pi_status(cfg.get('status_url'))


# ---- status -------------------------------------------------------------------------------------
def tree_line(t):
    claim = t['claim']
    who = {'free': '-', 'corrupt': 'UNREADABLE CLAIM'}.get(t['claim_state']) or f'{claim["owner"]} [{t["claim_state"]}]'
    drift = f'+{t["drift"][0]}/-{t["drift"][1]}' if t['drift'] else '?'
    cond = 'MISSING' if t['missing'] else (t['busy'] or 'clean')
    return f'  {Path(t["path"]).name:<34} {(t["branch"] or "(detached)"):<48} {(t["issue"] or "-"):<8} {who:<28} {drift:<9} {cond}'


def cmd_status(args, cfg):
    ws = model.workspace(cfg)
    lines, problems = [], []
    for name, repo in ws.items():
        if not repo['available']:
            lines.append(f'{name}: UNAVAILABLE ({repo["reason"]})')
            continue
        lines.append(f'{name}  origin/main {str(repo["main"])[:12]}')
        lines.append(f'  {"worktree":<34} {"branch":<48} {"issue":<8} {"claim":<28} {"vs main":<9} condition')
        lines += [tree_line(t) for t in repo['data']]
        by_issue = {}
        for t in repo['data']:
            if t['issue']:
                by_issue.setdefault(t['issue'], []).append(t)
        problems += [f'{name}: {i} has {len(ts)} worktrees ({", ".join(Path(t["path"]).name for t in ts)})'
                     for i, ts in by_issue.items() if len(ts) > 1]
        problems += [f'{name}: {Path(t["path"]).name} has uncommitted work and no claim'
                     for t in repo['data'] if t['claim_state'] == 'free' and t['busy'] and not t['missing']]
    if problems:
        lines += ['', 'Worth a look:'] + [f'  - {p}' for p in problems]
    emit(args, {'schema': 'ai-workflow.status/v1', 'repos': ws, 'attention': problems}, '\n'.join(lines))
    return 0


# ---- issue --------------------------------------------------------------------------------------
def pr_line(p):
    flags = ''.join([' CONFLICT' if p['conflicting'] else '', ' draft' if p['draft'] else '',
                     f' {p["behind_main"]}-behind-main' if p['behind_main'] else '',
                     ' review-required' if p['review_required'] else ''])
    return f'{p["repo"]}#{p["number"]} {p["state"]} ci={p["ci"]}{flags}'


def context_lines(ctx):
    lin, r = ctx['linear'], ctx['readiness']
    out = [f'{ctx["issue"]}: {lin["title"]}' if lin else f'{ctx["issue"]}: (Linear not read)',
           f'  State    {r["label"]}' + (' - an agent may start' if r['can_start'] else '')]
    out += [f'           {x}' for x in r['reasons']] + [f'           missing: {x}' for x in r['missing']]
    out += [f'           fix: {x}' for x in r['hints']]
    if lin:
        where = ' / '.join(filter(None, [lin['project'], lin['milestone'], f'parent {lin["parent"]}' if lin['parent'] else None]))
        out.append(f'  Linear   {lin["state"]}; Open Decisions: {lin["open_decisions"]}; labels: {", ".join(lin["labels"]) or "none"}'
                   + (f'; {where}' if where else ''))
    out.append(f'  Repos    {", ".join(ctx["repositories"]["value"] or []) or "?"} ({ctx["repositories"]["basis"]})')
    for t in ctx['worktrees']:
        out.append(f'  Worktree {t["repo"]}: {t["path"]} [{t["branch"]}] claim={t["owner"] or "-"} ({t["claim_state"]}) {t["busy"] or "clean"}')
    for g in ctx['prs']:
        out.append(f'  PRs      {g["branch"]}{" (paired)" if g["paired"] else ""}: ' + ', '.join(pr_line(p) for p in g['prs']))
    out.append('  Deployed ' + ('unavailable' if ctx['deployed'] is None else
                                ', '.join(f'{n} {str(d["sha"])[:12]}{"" if d["matches_main"] else " (not main)"}' for n, d in ctx['deployed'].items())))
    out += [f'  UNAVAILABLE {u["source"]}: {u["reason"]}' for u in ctx['unavailable']]
    return out


def cmd_issue(args, cfg):
    issue = need_issue(args.issue)
    if not issue:
        return 2
    linear, prs, pi = gather(cfg, issue, args)
    ctx = model.issue_context(cfg, issue, model.workspace(cfg), linear, prs, pi, me=model.owner_identity(args.owner)[0],
                              repos_override=repo_list(args, cfg))
    emit(args, ctx, '\n'.join(context_lines(ctx)))
    return 0


def manifest_for(cfg, issue, ctx, linear, args, ws=None):
    """The context manifest for an issue whose Linear record was read, else None."""
    record = linear['data'].get(issue) if linear['available'] else None
    if record is None:
        return None
    return context.build(cfg, record, ctx['repositories']['value'] or [], ws or model.workspace(cfg),
                         prs=[{k: p[k] for k in ('repo', 'number', 'state', 'ci', 'branch', 'url')} for g in ctx['prs'] for p in g['prs']],
                         max_items=getattr(args, 'max_items', None) or context.MAX_ITEMS,
                         max_tokens=getattr(args, 'max_tokens', None) or context.MAX_TOKENS)


def cmd_context(args, cfg):
    issue = need_issue(args.issue)
    if not issue:
        return 2
    linear, prs, pi = gather(cfg, issue, args)
    ws = model.workspace(cfg)
    ctx = model.issue_context(cfg, issue, ws, linear, prs, pi, me=model.owner_identity(args.owner)[0], repos_override=repo_list(args, cfg))
    manifest = manifest_for(cfg, issue, ctx, linear, args, ws)
    if manifest is None:
        why = next((u['reason'] for u in ctx['unavailable'] if u['source'] == 'linear'), 'the Linear issue was not read')
        emit(args, {'schema': context.SCHEMA, 'issue': issue, 'status': 'unavailable', 'reason': why},
             f'Context for {issue} [unavailable]: {why}')
        return 4
    manifest['readiness'] = {k: ctx['readiness'][k] for k in ('state', 'label', 'can_start', 'reasons')}
    if args.save:
        manifest['saved_to'] = str(context.save(manifest))
    out = context.render(manifest)
    out.insert(1, f'  State    {ctx["readiness"]["label"]}' + (f': {"; ".join(ctx["readiness"]["reasons"])}' if ctx['readiness']['reasons'] else ''))
    if manifest['prs']:
        out.append('  PRs      ' + ', '.join(f'{p["repo"]}#{p["number"]} {p["state"]} ci={p["ci"]}' for p in manifest['prs']))
    emit(args, manifest, '\n'.join(out))
    return 4 if manifest['status'] != 'ok' else 0


def repo_list(args, cfg):
    repo = getattr(args, 'repo', None)
    return None if not repo else (list(cfg['repos']) if repo == 'both' else [repo])


# ---- start --------------------------------------------------------------------------------------
def slug(title, words=5, limit=40):
    parts = re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).split()
    return '-'.join(parts[:words])[:limit].strip('-') or 'work'


def plan_workspace(cfg, name, issue, trees, me, args, title, labels, adopt=None):
    """What `start` would do in one repository: ('reuse'|'checkout'|'create', path, branch), or (None, why)."""
    repo = cfg['repos'][name]
    mine = [t for t in trees if t['repo'] == name]
    if len(mine) > 1:
        live = [t for t in mine if t['busy'] or (t['drift'] and t['drift'][0]) or (t['claim'] or {}).get('owner') == me]
        if len(live) != 1:
            return None, (f'{name}: {len(mine)} worktrees carry {issue} ({", ".join(Path(t["path"]).name for t in mine)}) and '
                          'it is not clear which holds the work; claim the right one directly with `claim --path`')
        mine = live
    if mine:
        return ('reuse', mine[0]['path'], mine[0]['branch']), None
    number = issue.split('-')[1]
    path = Path(cfg['root']) / f'{repo["dir"]}.wt-{gitio.PREFIX.lower()}{number}'
    suffix = 1
    while path.exists():
        suffix += 1
        path = Path(cfg['root']) / f'{repo["dir"]}.wt-{gitio.PREFIX.lower()}{number}-{suffix}'
    found = gitio.unmerged_branches(repo['path'], issue)
    if found is None:
        return None, f'{name}: could not list branches'
    names = sorted(set(found['local']) | set(found['remote']))
    if len(names) > 1:
        return None, f'{name}: several unmerged branches carry {issue} ({", ".join(names)}); finish or name one before starting again'
    if names:
        return ('checkout', str(path), names[0]), None
    kind = args.type or ('fix' if 'Bug' in labels else 'docs' if labels == ['Docs'] else 'feat')
    branch = adopt or f'{kind}/{gitio.PREFIX.lower()}-{number}-{args.desc or slug(title)}'
    base, n = branch, 1
    while gitio.branch_exists(repo['path'], branch):
        n += 1
        branch = f'{base}-{n}'
    return ('create', str(path), branch), None


def cmd_start(args, cfg):
    issue = need_issue(args.issue)
    if not issue:
        return 2
    owner, agent = need_owner(args)
    if not owner:
        return 2
    # a dry run changes nothing, not even remote-tracking refs: it plans against origin/main as last fetched
    fetched = {name: args.dry_run or SOURCES.fetch_main(repo['path']) for name, repo in cfg['repos'].items()}
    linear, prs, pi = gather(cfg, issue, args)
    ws = model.workspace(cfg)
    ctx = model.issue_context(cfg, issue, ws, linear, prs, pi, me=owner, repos_override=repo_list(args, cfg),
                              decisions_confirmed=args.decisions_confirmed)
    doc = {'schema': 'ai-workflow.start/v1', 'issue': issue, 'owner': owner, 'started': False, 'context': ctx, 'workspaces': []}

    def refuse(code, why):
        doc['refused'] = why
        emit(args, doc, '\n'.join(context_lines(ctx) + [f'  => not started: {why}']))
        return code

    ready = ctx['readiness']
    if not ready['can_start']:
        return refuse(4 if ready['state'] == 'unknown' else 3, f'{ready["label"]}: ' + '; '.join(ready['reasons'] + [f'missing {m}' for m in ready['missing']]))
    repos = ctx['repositories']['value']
    if not repos:
        return refuse(4, 'which repositories the issue touches is undetermined; pass --repo')
    stale = [n for n in repos if not fetched[n]]
    if stale:
        return refuse(4, f'could not fetch origin/main for {", ".join(stale)}; refusing to work from a stale main')
    lin = ctx['linear']
    trees = model.trees_for(ws, issue)
    plans, adopt = {}, None
    for name in repos:
        plan, why = plan_workspace(cfg, name, issue, trees, owner, args, lin['title'], lin['labels'], adopt)
        if not plan:
            return refuse(3, why)
        plans[name] = plan
        adopt = adopt or plan[2]
    if args.dry_run:
        doc['dry_run'] = True
        doc['manifest'] = manifest_for(cfg, issue, ctx, linear, args, ws)
        out = context_lines(ctx) + context.render(doc['manifest'])
        for name, (action, path, branch) in plans.items():
            doc['workspaces'].append({'repo': name, 'path': path, 'branch': branch, 'action': action, 'claim': 'not claimed (dry run)'})
            out.append(f'  Would     {name}: {action} {path} [{branch}] from origin/main {str(ctx["main"].get(name))[:12]} (as last fetched), then claim it for {owner}')
        out.append(f'  => dry run: nothing fetched, created or claimed. Task would be: Implement {issue}')
        emit(args, doc, chr(10).join(out))
        return 0
    claimed = []
    for name, (action, path, branch) in plans.items():
        repo = cfg['repos'][name]
        if action == 'create':
            rc, _ = gitio.git(repo['path'], 'worktree', 'add', '--no-track', '-b', branch, path, 'origin/main', timeout=180)
        elif action == 'checkout':
            local = gitio.git(repo['path'], 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}')[0] == 0
            rc, _ = gitio.git(repo['path'], 'worktree', 'add', path, branch, timeout=180) if local else \
                gitio.git(repo['path'], 'worktree', 'add', '--track', '-b', branch, path, f'origin/{branch}', timeout=180)
        else:
            rc = 0
        if rc:
            for file in claimed:
                claims.release(file, owner)
            return refuse(4, f'{name}: git could not create the worktree {path} on {branch}')
        file = claims.claim_file(gitio.common_dir(path), path)
        got = claims.acquire(file, path, owner, agent=agent, issue=issue, branch=branch, repo=name, note=args.note,
                             ttl_hours=model.ttl(cfg), busy=lambda path=path: gitio.busy(path))
        doc['workspaces'].append({'repo': name, 'path': path, 'branch': branch, 'action': action, 'claim': got.code,
                                  'drift': gitio.drift(path), 'instructions': str(Path(path) / 'AGENTS.md')})
        if not got.ok:
            for done in claimed:
                claims.release(done, owner)
            return refuse(3, f'{name}: {got.message}')
        if got.code != 'refreshed':
            claimed.append(file)
    doc['started'] = True
    doc['task'] = f'Implement {issue}'
    doc['manifest'] = manifest_for(cfg, issue, ctx, linear, args)          # built now, so it reads the claimed worktree
    doc['manifest_file'] = str(context.save(doc['manifest']))
    doc['commands'] = {'context': f'python {AW.as_posix()} issue {issue} --json',
                       'prs': f'python {AW.as_posix()} prs {issue}',
                       'handoff': f'python {AW.as_posix()} handoff {issue} --to any --note "<state of the work>"',
                       'release': f'python {AW.as_posix()} release {issue}'}
    out = context_lines(ctx)
    for w in doc['workspaces']:
        behind = f', {w["drift"][1]} behind main: bring origin/main in first' if w['drift'] and w['drift'][1] else ''
        out.append(f'  Workspace {w["repo"]}: {w["path"]} [{w["branch"]}] {w["action"]}, claim {w["claim"]} ({owner}){behind}')
    if len(doc['workspaces']) > 1:
        out.append('  Paired change: same branch name in both repositories; each PR links the other and names the merge order')
    out += context.render(doc['manifest'])
    out.append(f'  Saved     {doc["manifest_file"]} (disposable; rebuild with `context {issue}`)')
    out += [f'  Later     {k}: {v}' for k, v in doc['commands'].items()]
    out.append(f'  Task      {doc["task"]}')
    emit(args, doc, '\n'.join(out))
    return 0


# ---- claims -------------------------------------------------------------------------------------
def resolve_trees(args, cfg):
    """The worktrees a claim/release/handoff applies to: --path, the issue's worktrees, or cwd."""
    issue = gitio.issue_id(args.issue) if getattr(args, 'issue', None) else None
    if getattr(args, 'issue', None) and not issue:
        return None, f'expected an issue id like {gitio.PREFIX}-236'
    if issue and not args.path:
        trees = model.trees_for(model.workspace(cfg), issue)
        if not trees:
            return None, f'no worktree carries {issue} yet: `aw.py start {issue}` creates one'
        return [(t['path'], issue, t['branch'], t['repo']) for t in trees], None
    top = gitio.toplevel(args.path or os.getcwd())
    if not top:
        return None, f'{args.path or os.getcwd()} is not inside a git worktree'
    branch = gitio.current_branch(top)
    commons = {claims.norm(c): n for n, r in cfg['repos'].items() if (c := gitio.common_dir(r['path']))}
    return [(top, issue or gitio.issue_of(branch), branch, commons.get(claims.norm(gitio.common_dir(top))))], None


def cmd_claim(args, cfg):
    owner, agent = need_owner(args)
    if not owner:
        return 2
    trees, err = resolve_trees(args, cfg)
    if err:
        print(err, file=sys.stderr)
        return 2
    results, code = [], 0
    for path, issue, branch, repo in trees:
        file = claims.claim_file(gitio.common_dir(path), path)
        if args.command == 'claim':
            got = claims.acquire(file, path, owner, agent=agent, issue=issue, branch=branch, repo=repo, note=args.note,
                                 ttl_hours=model.ttl(cfg), busy=lambda path=path: gitio.busy(path), force=args.force)
        elif args.command == 'handoff':
            got = claims.handoff(file, owner, args.to, args.note)
        else:
            got = claims.release(file, owner, args.force)
        results.append({'worktree': path, **got.as_dict()})
        code = code or (0 if got.ok else 3)
    emit(args, {'owner': owner, 'results': results},
         '\n'.join(f'{r["worktree"]}: {r["code"]}' + (f' - {r["message"]}' if r['message'] else f' ({owner})') for r in results))
    return code


# ---- ready / queue ------------------------------------------------------------------------------
def cmd_ready(args, cfg):
    owner, _ = need_owner(args)
    if not owner:
        return 2
    trees, err = resolve_trees(args, cfg)
    if err:
        print(err, file=sys.stderr)
        return 2
    declared = [name for name in queue.DECLARABLE if getattr(args, name.replace('-', '_'))]
    results, code = [], 0
    for path, issue, branch, repo in trees:
        file = claims.claim_file(gitio.common_dir(path), path)
        claim, ahead = claims.read(file), (gitio.drift(path) or (None, None))[0]
        why = None
        if claims.state(claim) not in ('held', 'stale', 'handoff') or claim['owner'] != owner:
            why = f'claim this worktree first: it is {claims.describe(claim)}'
        elif gitio.busy(path):
            why = f'{gitio.busy(path)}: commit or drop it before reporting READY_FOR_PR'
        elif not ahead:
            why = 'no commits ahead of origin/main: there is nothing to open'
        if why:
            results.append({'worktree': path, 'ok': False, 'code': 'refused', 'message': why, 'ready': None})
            code = 3
            continue
        tree = {'issue': issue, 'branch': branch, 'head': gitio.rev(path, 'HEAD')}
        got = queue.record(file, owner, tree, args.tests, declared, args.set, queue.changed_files(path), cfg)
        results.append({'worktree': path, 'repo': repo, 'ok': got.ok, 'code': got.code, 'message': got.message,
                        'ready': (got.claim or {}).get('ready') if got.ok else None})
        code = code or (0 if got.ok else 3)
    out = []
    for r in results:
        e = r['ready']
        out += [f'{r["worktree"]}: {r["message"]}'] if not e else [
            f'READY_FOR_PR  {e["issue"] or "-"}  {r["repo"]}  {e["branch"]} @ {e["commit"][:12]}',
            f'  tests    {e["tests"]}',
            f'  touches  {e["files"]} file(s); {e["kind"]}' + (', docs-only' if e['docs_only'] else '')
            + (f'; {", ".join(e["classes"])}' if e['classes'] else '; no ADR, contract, deployment or Needs Cody')
            + (f'; merge set {e["set"]}' if e['set'] else ''),
            '  Do not open the PR: the orchestrator releases it (`queue release`) when there is room.']
    emit(args, {'owner': owner, 'results': results}, '\n'.join(out))
    return code


def queue_board(cfg):
    prs = {name: SOURCES.prs(name, repo['slug']) for name, repo in cfg['repos'].items()}
    settings = {name: SOURCES.automerge(name, repo['slug']) for name, repo in cfg['repos'].items()}
    return queue.board(cfg, model.workspace(cfg), prs, settings, lambda name, issue: SOURCES.prs(name, cfg['repos'][name]['slug'], issue))


def cmd_queue(args, cfg):
    doc = queue_board(cfg)
    blind = any(u['source'].startswith('github:') for u in doc['unavailable'])
    if args.action != 'release':
        emit(args, doc, '\n'.join(queue.render(doc)))
        return 4 if blind else 0
    owner, _ = need_owner(args)
    if not owner:
        return 2
    issue = gitio.issue_id(args.issue) if args.issue else None
    top = claims.norm(gitio.toplevel(args.path) or args.path) if args.path else None
    wanted = [e for e in doc['queue'] if (issue is None or e['issue'] == issue) and (top is None or claims.norm(e['worktree']) == top)
              and (issue or top) and e['state'] in ('ready', 'stale')]
    if not wanted:
        print(f'nothing is READY_FOR_PR for {args.issue or args.path or "(name an issue or --path)"}', file=sys.stderr)
        return 3
    if blind:
        emit(args, {**doc, 'released': []}, 'not released: GitHub could not be read, so the open PRs are unknown\n'
             + '\n'.join(f'  {u["source"]}: {u["reason"]}' for u in doc['unavailable']))
        return 4
    out, released, code = [], [], 0
    for e in wanted:
        if not e['can_release']:
            out.append(f'{e["issue"] or e["branch"]} {e["repo"]}: not released: ' + '; '.join(e['blocked_by']))
            code = 3
            continue
        got = claims.mark_released(e['claim_file'], e['commit'], owner)
        if not got.ok:
            out.append(f'{e["issue"] or e["branch"]} {e["repo"]}: not released: {got.message}')
            code = 3
            continue
        released.append({'issue': e['issue'], 'repo': e['repo'], 'branch': e['branch'], 'owner': e['owner']})
        out.append(f'{e["issue"] or e["branch"]} {e["repo"]}: released by {owner}: {e["owner"]} may open the PR for {e["branch"]} now')
    emit(args, {**doc, 'released': released}, '\n'.join(out))
    return code


# ---- prs / needs-cody ---------------------------------------------------------------------------
def cmd_prs(args, cfg):
    issue = gitio.issue_id(args.issue) if args.issue else None
    if args.issue and not issue:
        return 2 if not need_issue(args.issue) else 0
    rows, unavailable = [], []
    for name, repo in cfg['repos'].items():
        res = SOURCES.prs(name, repo['slug'], issue)
        if res['available']:
            rows += res['data']
        else:
            unavailable.append({'source': f'github:{name}', 'reason': res['reason']})
    groups = model.pair(rows)
    out = []
    for g in groups:
        out.append(f'{g["branch"]}{"  (paired)" if g["paired"] else ""}')
        out += [f'  {p["repo"]}#{p["number"]:<4} {p["state"]:<7} ci={p["ci"]:<8} review={p["review"] or "-":<17}'
                f'behind={"?" if p["behind_main"] is None and p["state"] == "open" else p["behind_main"] or 0:<3}'
                f'{" CONFLICT" if p["conflicting"] else ""}{" draft" if p["draft"] else ""}  {p["title"]}' for p in g['prs']]
    out += [f'UNAVAILABLE {u["source"]}: {u["reason"]}' for u in unavailable]
    emit(args, {'schema': 'ai-workflow.prs/v1', 'issue': issue, 'groups': groups, 'unavailable': unavailable},
         '\n'.join(out) or 'no PRs')
    return 4 if unavailable else 0


def cmd_needs_cody(args, cfg):
    prs = {name: SOURCES.prs(name, repo['slug']) for name, repo in cfg['repos'].items()}
    linear = SOURCES.linear(snapshot=args.linear_snapshot)
    if linear['available']:                       # an open PR may name an issue the active-issue read left out (already Done)
        for res in prs.values():
            for pr in res['data'] if res['available'] else []:
                if pr['issue'] and pr['issue'] not in linear['data']:
                    one = SOURCES.linear(pr['issue'], snapshot=args.linear_snapshot)
                    if one['available']:
                        linear['data'].update(one['data'])
    doc = model.needs_cody(cfg, model.workspace(cfg), linear, prs, SOURCES.pi_status(cfg.get('status_url')))
    out = [f'Needs Cody now ({len(doc["needs_cody"])})'] + [f'  [{i["kind"]}] {i["ref"]}: {i["text"]}' for i in doc['needs_cody']]
    out += [f'Agent work in progress ({len(doc["agent"])})'] + [f'  [{i["kind"]}] {i["ref"]}: {i["text"]}' for i in doc['agent']]
    out += [f'Could not check ({len(doc["unavailable"])})'] + [f'  {u["source"]}: {u["reason"]}' for u in doc['unavailable']]
    emit(args, doc, '\n'.join(out))
    return 0


# ---- setup / hook -------------------------------------------------------------------------------
def cmd_setup(args, cfg):
    reports, ok = [], True
    for name, repo in cfg['repos'].items():
        if not os.path.isdir(repo['path']):
            reports.append({'name': name, 'repo': repo['path'], 'ok': False, 'actions': [],
                            'problems': [f'no checkout at {repo["path"]} (expected beside this repository; or set AI_WORKFLOW_ROOT)']})
        elif args.uninstall:
            reports.append({'name': name, **hooks.uninstall(repo['path'])})
        else:
            reports.append({'name': name, **hooks.install(repo['path'], AW, chain=args.chain, check_only=args.check,
                                                          warn_only=args.warn_only)})
        ok = ok and reports[-1]['ok']
    env = []
    if not args.uninstall:
        token, where = tokens.linear_token()
        env.append({'check': 'linear', 'ok': bool(token), 'detail': where})
        rc, _, err = sources._run(['gh', 'auth', 'status'])
        env.append({'check': 'github', 'ok': rc == 0, 'detail': 'gh is logged in' if rc == 0 else 'gh is missing or not logged in (`gh auth login`)'})
        owner, _ = model.owner_identity(args.owner)
        env.append({'check': 'identity', 'ok': bool(owner), 'detail': owner or 'no session identity in this shell: set AI_WORKFLOW_SESSION before committing on issue branches'})
    out = []
    for r in reports:
        mode = f', {r["mode"]}' if r.get('mode') else ''
        out.append(f'{r["name"]}: {"ok" if r["ok"] else "PROBLEM"}{mode}  ({r["repo"]})')
        out += [f'  {a}' for a in r['actions']] + [f'  ! {p}' for p in r['problems']]
        if r['ok'] and not r['actions']:
            out.append('  nothing to change')
    out += [f'{e["check"]}: {"ok" if e["ok"] else "not available"}  ({e["detail"]})' for e in env]
    emit(args, {'schema': 'ai-workflow.setup/v1', 'ok': ok, 'repos': reports, 'environment': env}, '\n'.join(out))
    return 0 if ok else 3


def cmd_hook(args, cfg):
    code, message = hooks.check(os.getcwd(), model.owner_identity()[0])
    if code and args.warn_only:
        print(f'ai-workflow WARNING (not enforced yet; this commit will be refused once enforcement is on):' + chr(10) + f'{message}', file=sys.stderr)
        return 0
    if message:
        print(f'ai-workflow: {message}', file=sys.stderr)
    return code


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='structured output')
    common.add_argument('--owner', help='session label (default: AI_WORKFLOW_SESSION, else the Claude session)')
    common.add_argument('--linear-snapshot', metavar='FILE|-',
                        help='Linear issue(s) as JSON from a file, or - for stdin, instead of the Linear API')
    ap = argparse.ArgumentParser(prog='ai-workflow', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('status', parents=[common])
    p = sub.add_parser('issue', parents=[common])
    p.add_argument('issue')
    p.add_argument('--repo', choices=['party', 'games', 'both'], help='the repositories, when the issue does not say')
    p = sub.add_parser('context', parents=[common])
    p.add_argument('issue')
    p.add_argument('--repo', choices=['party', 'games', 'both'], help='the repositories, when the issue does not say')
    p.add_argument('--max-items', type=int, help=f'context budget in items (default {context.MAX_ITEMS})')
    p.add_argument('--max-tokens', type=int, help=f'context budget in estimated tokens (default {context.MAX_TOKENS})')
    p.add_argument('--save', action='store_true', help='also write the manifest to the state directory')
    p = sub.add_parser('start', parents=[common])
    p.add_argument('issue')
    p.add_argument('--agent', help='agent kind for the claim label, with --session (claude, codex, ...)')
    p.add_argument('--session', help='opaque session id; the claim label is <agent>-<first 8 characters>')
    p.add_argument('--repo', choices=['party', 'games', 'both'], help='the repositories, when the issue does not say')
    p.add_argument('--type', choices=['feat', 'fix', 'chore', 'docs', 'experiment'], help='branch type (default: from labels)')
    p.add_argument('--desc', help='branch description (default: from the title)')
    p.add_argument('--note', default='')
    p.add_argument('--dry-run', action='store_true', help='readiness and the plan only: fetch, create and claim nothing')
    p.add_argument('--max-items', type=int, help=f'context budget in items (default {context.MAX_ITEMS})')
    p.add_argument('--max-tokens', type=int, help=f'context budget in estimated tokens (default {context.MAX_TOKENS})')
    p.add_argument('--decisions-confirmed', action='store_true',
                   help='the OWNER states there are no open product decisions although the issue does not say so')
    for name in ('claim', 'release', 'handoff'):
        p = sub.add_parser(name, parents=[common])
        p.add_argument('issue', nargs='?')
        p.add_argument('--path')
        p.add_argument('--note', default='')
        p.add_argument('--force', action='store_true', help='take or drop a claim that is not yours: an owner decision')
        p.add_argument('--to', default='any', help='handoff: the receiving session label, `any`, or `cody` to ask the owner')
    p = sub.add_parser('ready', parents=[common])
    p.add_argument('issue', nargs='?')
    p.add_argument('--path')
    p.add_argument('--tests', required=True, help='what was run and its result, as you would report it')
    p.add_argument('--set', help='the merge set this branch belongs to, when it is not simply its issue')
    p.add_argument('--needs-cody', action='store_true', help='the change needs an owner decision or owner-only validation')
    p.add_argument('--adr', action='store_true', help='the change makes or alters an ADR decision')
    p.add_argument('--contract', action='store_true', help='the change alters a protocol or cross-repo contract')
    p.add_argument('--deployment', action='store_true', help='the change alters deployment or appliance behaviour')
    p = sub.add_parser('queue', parents=[common])
    p.add_argument('action', nargs='?', choices=['list', 'release'], default='list')
    p.add_argument('issue', nargs='?')
    p.add_argument('--path')
    p = sub.add_parser('prs', parents=[common])
    p.add_argument('issue', nargs='?')
    sub.add_parser('needs-cody', parents=[common])
    p = sub.add_parser('setup', parents=[common])
    p.add_argument('--check', action='store_true', help='report only; change nothing')
    p.add_argument('--chain', action='store_true', help='keep an existing foreign hook and run it after the claim check')
    p.add_argument('--uninstall', action='store_true', help='remove our hooks and restore any chained one')
    p.add_argument('--warn-only', action='store_true', help='install the hooks in a mode that warns instead of refusing')
    p = sub.add_parser('hook')
    p.add_argument('name', nargs='?', default='pre-commit')
    p.add_argument('--warn-only', action='store_true')
    sub.add_parser('guard')
    args = ap.parse_args(argv)
    if args.command == 'guard':
        return guard.main()
    if args.command == 'hook':
        try:
            return cmd_hook(args, model.load_config())
        except Exception as e:           # the tool failing is not a claim decision: warn, do not block
            print(f'ai-workflow: claim check failed to run ({type(e).__name__}: {e})', file=sys.stderr)
            return 1
    cfg = model.load_config()
    return {'status': cmd_status, 'issue': cmd_issue, 'context': cmd_context, 'start': cmd_start, 'claim': cmd_claim, 'release': cmd_claim,
            'handoff': cmd_claim, 'ready': cmd_ready, 'queue': cmd_queue, 'prs': cmd_prs, 'needs-cody': cmd_needs_cody, 'setup': cmd_setup}[args.command](args, cfg)
