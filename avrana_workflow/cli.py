"""avrana-workflow: one place that answers what is being worked on, by whom, in which worktree,
in what state, and what needs the owner.

    python aw.py status                     worktrees, claims and drift in every repository
    python aw.py issue AVR-236              structured context for one issue
    python aw.py worktree AVR-236 --repo party --type feat --desc native-registry
    python aw.py claim [AVR-236]            claim this worktree (or the issue's worktrees)
    python aw.py release | handoff --to X   give a worktree back, or to another session
    python aw.py prs [AVR-237]              PRs, CI and pairing across repositories
    python aw.py needs-cody                 the owner's queue, and what an agent should do instead
    python aw.py guard                      Claude Code PreToolUse hook (reads the hook JSON on stdin)

Read-only except for local claim files and `worktree`, which creates a branch and worktree from
origin/main. It never pushes, merges, deploys or writes to Linear or GitHub. Exit codes: 0 ok,
2 usage, 3 refused (claimed by someone else), 4 a required source was unavailable.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from . import claims, gitio, guard, model, sources


class Sources:
    """The outside world; tests replace this."""
    prs = staticmethod(sources.prs)
    linear = staticmethod(sources.linear)
    pi_status = staticmethod(sources.pi_status)


SOURCES = Sources


def emit(args, doc, text):
    print(json.dumps(doc, indent=2) if args.json else text)


def need_owner(args):
    owner, agent = model.owner_identity(args.owner)
    if not owner:
        print('no session identity: set AVRANA_SESSION (for example codex-<short id>) or pass --owner', file=sys.stderr)
    return owner, agent


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
    emit(args, {'schema': 'avrana.status/v1', 'repos': ws, 'attention': problems}, '\n'.join(lines))
    return 0


def gather(cfg, issue, args):
    prs = {name: SOURCES.prs(name, repo['slug'], issue) for name, repo in cfg['repos'].items()}
    return SOURCES.linear(issue, snapshot=args.linear_snapshot), prs, SOURCES.pi_status(cfg['status_url'])


def cmd_issue(args, cfg):
    issue = gitio.issue_id(args.issue)
    if not issue:
        print('expected an issue id like AVR-236', file=sys.stderr)
        return 2
    linear, prs, pi = gather(cfg, issue, args)
    ctx = model.issue_context(cfg, issue, model.workspace(cfg), linear, prs, pi, me=model.owner_identity(args.owner)[0])
    lin = ctx['linear']
    out = [f'{issue}: {lin["title"]}' if lin else f'{issue}: (Linear not read)']
    if lin:
        out.append(f'  Linear   {lin["state"]} -> {lin["lifecycle"]}; Open Decisions: {lin["open_decisions"]}; labels: {", ".join(lin["labels"]) or "none"}')
    out.append(f'  Repos    {", ".join(ctx["repositories"]["value"] or []) or "?"} ({ctx["repositories"]["basis"]})')
    for t in ctx['worktrees']:
        out.append(f'  Worktree {t["repo"]}: {t["path"]} [{t["branch"]}] claim={t["owner"] or "-"} ({t["claim_state"]}) {t["busy"] or "clean"}')
    if not ctx['worktrees']:
        out.append('  Worktree none yet: `aw.py worktree ' + issue + ' --repo <name> --type <type> --desc <slug>`')
    for g in ctx['prs']:
        out.append(f'  PRs      {g["branch"]}{" (paired)" if g["paired"] else ""}: '
                   + ', '.join(f'{p["repo"]}#{p["number"]} {p["state"]} ci={p["ci"]}' for p in g['prs']))
    out.append('  Deployed ' + ('unavailable' if ctx['deployed'] is None else
                                ', '.join(f'{n} {str(d["sha"])[:12]}{"" if d["matches_main"] else " (not main)"}' for n, d in ctx['deployed'].items())))
    out += [f'  BLOCKER  {b}' for b in ctx['blockers']] + [f'  note     {n}' for n in ctx['notes']]
    out += [f'  UNAVAILABLE {u["source"]}: {u["reason"]}' for u in ctx['unavailable']]
    out.append(f'  => {"ready for an agent" if ctx["ready"] else "not ready: see blockers"}')
    emit(args, ctx, '\n'.join(out))
    return 0


def resolve_trees(args, cfg):
    """The worktrees a claim/release/handoff applies to: --path, the issue's worktrees, or cwd."""
    issue = gitio.issue_id(args.issue) if getattr(args, 'issue', None) else None
    if getattr(args, 'issue', None) and not issue:
        return None, 'expected an issue id like AVR-236'
    if issue and not args.path:
        trees = model.trees_for(model.workspace(cfg), issue)
        if not trees:
            return None, f'no worktree carries {issue} yet: create one with `aw.py worktree {issue} ...`'
        return [(t['path'], issue, t['branch'], t['repo']) for t in trees], None
    top = gitio.toplevel(args.path or os.getcwd())
    if not top:
        return None, f'{args.path or os.getcwd()} is not inside a git worktree'
    rc, branch = gitio.git(top, 'branch', '--show-current')
    commons = {claims.norm(c): n for n, r in cfg['repos'].items() if (c := gitio.common_dir(r['path']))}
    return [(top, issue or gitio.issue_of(branch), branch or None, commons.get(claims.norm(gitio.common_dir(top))))], None


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
        got = claims.acquire(claims.claim_file(gitio.common_dir(path), path), path, owner, agent=agent, issue=issue,
                             branch=branch, repo=repo, note=args.note, ttl_hours=cfg.get('claim_ttl_hours', claims.DEFAULT_TTL_HOURS),
                             busy=lambda path=path: gitio.busy(path), force=args.force)
        results.append({'worktree': path, **got.as_dict()})
        code = code or (0 if got.ok else 3)
    emit(args, {'owner': owner, 'results': results},
         '\n'.join(f'{r["worktree"]}: {r["code"]}' + (f' - {r["message"]}' if r['message'] else f' ({owner})') for r in results))
    return code


def cmd_release(args, cfg):
    owner, _ = need_owner(args)
    if not owner:
        return 2
    trees, err = resolve_trees(args, cfg)
    if err:
        print(err, file=sys.stderr)
        return 2
    results, code = [], 0
    for path, *_ in trees:
        file = claims.claim_file(gitio.common_dir(path), path)
        got = claims.handoff(file, owner, args.to, args.note) if args.command == 'handoff' else claims.release(file, owner, args.force)
        results.append({'worktree': path, **got.as_dict()})
        code = code or (0 if got.ok else 3)
    emit(args, {'owner': owner, 'results': results},
         '\n'.join(f'{r["worktree"]}: {r["code"]}' + (f' - {r["message"]}' if r['message'] else '') for r in results))
    return code


def cmd_worktree(args, cfg):
    issue = gitio.issue_id(args.issue)
    if not issue:
        print('expected an issue id like AVR-236', file=sys.stderr)
        return 2
    owner, agent = need_owner(args)
    if not owner:
        return 2
    names = list(cfg['repos']) if args.repo == 'both' else [args.repo]
    ws = model.workspace(cfg)
    results, code = [], 0
    for name in names:
        repo = cfg['repos'][name]
        if not ws[name]['available']:
            results.append({'repo': name, 'ok': False, 'code': 'unavailable', 'message': ws[name]['reason']})
            code = 4
            continue
        existing = [t for t in ws[name]['data'] if t['issue'] == issue]
        if existing:
            path, branch = existing[0]['path'], existing[0]['branch']
            created = False
        else:
            if not args.desc:
                print('creating a worktree needs --desc (and --type): the branch is type/avr-N-desc', file=sys.stderr)
                return 2
            number = issue.split('-')[1]
            branch = f'{args.type}/avr-{number}-{args.desc}'
            path = str(Path(cfg['root']) / f'{repo["dir"]}.wt-avr{number}')
            if gitio.git(repo['path'], 'fetch', 'origin', 'main', '--quiet', timeout=120)[0]:
                results.append({'repo': name, 'ok': False, 'code': 'unavailable', 'message': 'could not fetch origin/main; refusing to branch from a stale main'})
                code = 4
                continue
            rc, out = gitio.git(repo['path'], 'worktree', 'add', '-b', branch, path, 'origin/main', timeout=180)
            if rc:
                results.append({'repo': name, 'ok': False, 'code': 'git-failed', 'message': f'git worktree add failed for {path}'})
                code = code or 4
                continue
            gitio.git(path, 'branch', '--unset-upstream')
            created = True
        got = claims.acquire(claims.claim_file(gitio.common_dir(path), path), path, owner, agent=agent, issue=issue,
                             branch=branch, repo=name, note=args.note, ttl_hours=cfg.get('claim_ttl_hours', claims.DEFAULT_TTL_HOURS),
                             busy=lambda path=path: gitio.busy(path))
        results.append({'repo': name, 'worktree': path, 'branch': branch, 'created': created, **got.as_dict()})
        code = code or (0 if got.ok else 3)
    emit(args, {'issue': issue, 'owner': owner, 'results': results},
         '\n'.join(f'{r["repo"]}: {r.get("worktree", "-")} [{r.get("branch", "-")}] {"created, " if r.get("created") else ""}{r["code"]}'
                   + (f' - {r["message"]}' if r['message'] else '') for r in results))
    return code


def cmd_prs(args, cfg):
    issue = gitio.issue_id(args.issue) if args.issue else None
    if args.issue and not issue:
        print('expected an issue id like AVR-237', file=sys.stderr)
        return 2
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
                f'{" CONFLICT" if p["conflicting"] else ""}{" draft" if p["draft"] else ""}  {p["title"]}' for p in g['prs']]
    out += [f'UNAVAILABLE {u["source"]}: {u["reason"]}' for u in unavailable]
    emit(args, {'schema': 'avrana.prs/v1', 'issue': issue, 'groups': groups, 'unavailable': unavailable},
         '\n'.join(out) or ('no PRs' if not unavailable else ''))
    return 4 if unavailable else 0


def cmd_needs_cody(args, cfg):
    prs = {name: SOURCES.prs(name, repo['slug']) for name, repo in cfg['repos'].items()}
    doc = model.needs_cody(cfg, model.workspace(cfg), SOURCES.linear(snapshot=args.linear_snapshot), prs, SOURCES.pi_status(cfg['status_url']))
    out = [f'Needs Cody ({len(doc["needs_cody"])})'] + [f'  [{i["kind"]}] {i["ref"]}: {i["text"]}' for i in doc['needs_cody']] or []
    out += [f'Agent work, not Cody ({len(doc["agent"])})'] + [f'  [{i["kind"]}] {i["ref"]}: {i["text"]}' for i in doc['agent']]
    out += [f'Could not check ({len(doc["unavailable"])}) - this queue is incomplete for these sources'] if doc['unavailable'] else []
    out += [f'  {u["source"]}: {u["reason"]}' for u in doc['unavailable']]
    emit(args, doc, '\n'.join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='structured output')
    common.add_argument('--owner', help='session label (default: AVRANA_SESSION, else the Claude session)')
    common.add_argument('--linear-snapshot', help='JSON file of Linear issue(s) read through a connector')
    ap = argparse.ArgumentParser(prog='avrana-workflow', description=__doc__.split('\n')[0], parents=[common])
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('status', parents=[common])
    p = sub.add_parser('issue', parents=[common])
    p.add_argument('issue')
    p = sub.add_parser('claim', parents=[common])
    p.add_argument('issue', nargs='?')
    p.add_argument('--path')
    p.add_argument('--note', default='')
    p.add_argument('--force', action='store_true', help='take over a live or busy-stale claim: an owner decision')
    for name in ('release', 'handoff'):
        p = sub.add_parser(name, parents=[common])
        p.add_argument('issue', nargs='?')
        p.add_argument('--path')
        p.add_argument('--note', default='')
        p.add_argument('--force', action='store_true')
        p.add_argument('--to', default='any', help='handoff: the receiving session label, or any')
    p = sub.add_parser('worktree', parents=[common])
    p.add_argument('issue')
    p.add_argument('--repo', choices=['party', 'games', 'both'], required=True)
    p.add_argument('--type', choices=['feat', 'fix', 'chore', 'docs', 'experiment'], default='feat')
    p.add_argument('--desc')
    p.add_argument('--note', default='')
    p = sub.add_parser('prs', parents=[common])
    p.add_argument('issue', nargs='?')
    sub.add_parser('needs-cody', parents=[common])
    sub.add_parser('guard')
    args = ap.parse_args(argv)
    if args.command == 'guard':
        return guard.main()
    cfg = model.load_config()
    return {'status': cmd_status, 'issue': cmd_issue, 'claim': cmd_claim, 'release': cmd_release, 'handoff': cmd_release,
            'worktree': cmd_worktree, 'prs': cmd_prs, 'needs-cody': cmd_needs_cody}[args.command](args, cfg)
