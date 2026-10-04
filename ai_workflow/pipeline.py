"""The deterministic steps between "issue assigned" and "issue reconciled".

    start -> implement -> validate -> ready -> release (orchestrator) -> pr -> merge (owner) -> reconcile

`start`, `context`, `ready` and `queue release` live elsewhere; this module adds what was left to
agent judgement: running the repository's own checks (`validate`), opening the PR from the
recorded facts (`pr`), naming the single next step (`next`) and comparing Linear with what git and
GitHub show (`reconcile`). No step reasons about anything: each reads recorded state and either
acts, names the next command, or stops for a human with the reason.

Validation commands are configuration (`validate` in workflow.json), copied from each
repository's AGENTS.md. A check listed as advisory on this platform is run and reported but does
not fail validation: the repository's Linux CI stays the authority for it.
"""
import sys
import time

from . import claims, gitio, sources

TAIL = 15


def checks_for(cfg, repo):
    return (cfg.get('validate') or {}).get(repo) or []


def validate(cfg, repo, worktree, run, platform=sys.platform):
    """Run the configured checks in `worktree`; the record to store on the claim, and the output
    tails of whatever failed. None when the repository has no checks: that is not a pass."""
    wanted = checks_for(cfg, repo)
    if not wanted:
        return None, []
    rows, tails = [], []
    for check in wanted:
        began = time.monotonic()
        rc, out = run(check['run'], worktree, check.get('timeout'))
        advisory = any(p in ('*', platform) for p in check.get('advisory_on', ()))
        rows.append({'name': check['name'], 'run': check['run'], 'rc': rc, 'ok': rc == 0, 'advisory': advisory,
                     'seconds': round(time.monotonic() - began, 1)})
        if rc:
            tails.append((check['name'], (out or '').strip().splitlines()[-TAIL:]))
    required = [r for r in rows if not r['advisory']]
    return {'commit': gitio.rev(worktree, 'HEAD'), 'ok': all(r['ok'] for r in required), 'checks': rows,
            'at': claims.stamp(claims.utcnow())}, tails


def summary(validated):
    """The test report `ready` records when validation stands in for a typed one."""
    rows = validated['checks']
    failed = [r['name'] for r in rows if not r['ok']]
    return (f'validate: {sum(r["ok"] for r in rows)}/{len(rows)} checks passed at {validated["commit"][:12]} '
            f'({", ".join(r["name"] for r in rows)})' + (f'; advisory failed: {", ".join(failed)}' if failed else ''))


def pr_text(entry, title, releaser):
    """Title and body of the PR, from the READY_FOR_PR record alone."""
    issue = entry.get('issue')
    body = [f'Issue: {issue}' if issue else 'No tracked issue.', '', '## Tests', '', entry['tests'], '', '## Classification', '',
            f'{entry["kind"]}' + (', docs-only' if entry.get('docs_only') else '') + f'; {entry["files"]} file(s) changed.']
    if entry['classes']:
        body += ['', f'**Requires Cody before merge:** {", ".join(entry["classes"])}.']
    else:
        body += ['', 'No ADR decision, protocol/contract change, deployment change or Needs Cody was detected or declared.']
    if entry.get('set'):
        body += ['', f'Merge set: {entry["set"]} (lands together with its other members).']
    body += ['', f'Opened by ai-workflow from the READY_FOR_PR record at {entry["commit"][:12]}; released by {releaser}.']
    return (f'{title} ({issue})' if issue and issue not in title else title), '\n'.join(body) + '\n'


def tree_step(cfg, issue, tree, prs):
    """(stage, who, next, why) for one worktree this session holds."""
    claim, repo, branch = tree['claim'], tree['repo'], tree['branch']
    mine = sorted((p for p in prs if p['repo'] == repo and p['branch'] == branch), key=lambda p: -p['number'])
    open_pr = next((p for p in mine if p['state'] == 'open'), None)
    merged = next((p for p in mine if p['state'] == 'merged'), None)
    ref = lambda p: f'{p["repo"]}#{p["number"]}'                                     # noqa: E731
    if open_pr:
        if open_pr['ci'] == 'failed':
            return 'fix-ci', 'agent', f'fix the failing checks on {ref(open_pr)}, then `validate {issue}` and push', 'CI failed'
        if open_pr['conflicting'] or open_pr['behind_main']:
            return 'update', 'agent', f'merge origin/main into {branch} (no rebase), `validate {issue}`, push', \
                f'{ref(open_pr)} ' + ('conflicts with main' if open_pr['conflicting'] else f'is {open_pr["behind_main"]} behind main')
        if open_pr['ci'] != 'passed':
            return 'ci', 'none', f'wait: checks are {open_pr["ci"]} on {ref(open_pr)}', ''
        return 'merge', 'cody', f'wait: {ref(open_pr)} is green and current; merging is the owner\'s (or `queue` says auto-merge eligible)', ''
    if merged:
        return 'finish', 'agent', f'`release {issue}` (its PR {ref(merged)} is merged), then `reconcile`', ''
    if tree['busy']:
        return 'implement', 'agent', f'finish and commit the work in {tree["path"]}', tree['busy']
    if not (tree['drift'] and tree['drift'][0]):
        return 'implement', 'agent', f'implement {issue} in {tree["path"]} (`context {issue}` lists what to read first) and commit', \
            'no commits ahead of main'
    validated, ready = claim.get('validated'), claim.get('ready')
    if ready and ready.get('commit') == tree['head']:
        if ready.get('released'):
            return 'pr', 'agent', f'`pr {issue}`', f'released by {ready["released"].get("by")}'
        return 'release', 'orchestrator', f'`queue release {issue}` when `queue` shows room', 'READY_FOR_PR is recorded'
    if not checks_for(cfg, repo):
        return 'ready', 'agent', f'`ready {issue} --tests "<what you ran and its result>"`', f'no validation is configured for {repo}'
    if not validated or validated.get('commit') != tree['head']:
        return 'validate', 'agent', f'`validate {issue}`', 'this commit has not been validated'
    if not validated['ok']:
        failed = ', '.join(c['name'] for c in validated['checks'] if not c['ok'] and not c['advisory'])
        return 'fix', 'agent', f'fix the failing checks ({failed}), commit, `validate {issue}`', 'validation failed'
    return 'ready', 'agent', f'`ready {issue}`', 'validated'


def next_steps(cfg, issue, ctx, trees, prs, me):
    """The single next step per worktree, or one step that starts or stops the issue."""
    r = ctx['readiness']
    mine = [t for t in trees if t['claim_state'] in ('held', 'stale') and t['claim']['owner'] == me and not t['missing']]
    step = lambda stage, who, nxt, why, repo=None: {'stage': stage, 'who': who, 'next': nxt, 'why': why, 'repo': repo}  # noqa: E731
    if r['state'] == 'done' and not mine:
        return [step('done', 'none', 'nothing to do', '; '.join(r['reasons']))]
    if mine:
        return [step(*tree_step(cfg, issue, t, prs), repo=t['repo']) for t in mine]
    foreign = [line for line in r['reasons'] if 'is claimed by' in line or 'stale claim' in line or 'unreadable claim' in line]
    if foreign:
        return [step('stop', 'orchestrator', 'another session holds this issue: ask for a handoff, or pick other work', '; '.join(foreign))]
    if r['can_start']:
        return [step('start', 'agent', f'`start {issue}`', '; '.join(r['reasons']))]
    why = r['reasons'] + [f'missing: {m}' for m in r['missing']]
    return [step('stop', 'cody', 'a human decides: ' + (r['hints'][0] if r['hints'] else r['label']), '; '.join(why) or r['label'])]


def reconcile(ws, linear, open_by_repo, history):
    """What Linear says against what git and GitHub show. Read-only: the actions name who applies
    them. `history(issue)` returns every PR of the issue across repositories, or None."""
    actions, unavailable = [], []
    trees = [t for repo in ws.values() if repo['available'] for t in repo['data'] if t['issue'] and not t['missing']]
    live = [t for t in trees if t['claim_state'] in ('held', 'handoff', 'stale') or (t['drift'] and t['drift'][0])]
    issues = sorted({t['issue'] for t in live if t['claim_state'] != 'free'} | {p['issue'] for p in open_by_repo if p['issue']},
                    key=lambda i: int(i.rsplit('-', 1)[-1]))
    for issue in issues:
        record = linear(issue)
        prs = history(issue)
        if record is None or prs is None:
            unavailable.append({'source': 'linear' if record is None else 'github',
                                'reason': f'{issue} could not be read, so it is not reconciled'})
            continue
        state = record['state']
        mine = [t for t in live if t['issue'] == issue]
        open_prs = [p for p in prs if p['state'] == 'open']
        merged = [p for p in prs if p['state'] == 'merged']
        ref = lambda p: f'{p["repo"]}#{p["number"]}'                                 # noqa: E731

        def add(kind, who, text):
            actions.append({'issue': issue, 'kind': kind, 'who': who, 'text': text})

        unmerged = [t for t in mine if t['drift'] and t['drift'][0]
                    and not any(p['repo'] == t['repo'] and p['branch'] == t['branch'] for p in merged)]
        if state in sources.TERMINAL:
            if open_prs:
                add('closed-with-open-pr', 'cody', f'Linear says {state} but {", ".join(map(ref, open_prs))} is open: '
                    'close the PR or reopen the issue')
        elif open_prs:
            if state in ('Backlog', 'Todo', 'In Progress'):
                add('linear-status', 'orchestrator', f'{", ".join(map(ref, open_prs))} is open: set {issue} to In Review (it is {state})')
        elif merged and not unmerged:
            if state != 'In Review':
                add('linear-status', 'orchestrator', f'{", ".join(map(ref, merged))} merged and no work is left on a branch: set {issue} '
                    f'to In Review or Done per its acceptance criteria (it is {state})')
        elif unmerged and state in ('Backlog', 'Todo'):
            add('linear-status', 'orchestrator', f'work is under way in {", ".join(t["repo"] for t in unmerged)}: set {issue} to '
                f'In Progress (it is {state})')
        for t in mine:
            if t['claim_state'] == 'free':
                continue
            landed = any(p['repo'] == t['repo'] and p['branch'] == t['branch'] for p in merged) and t not in unmerged
            if state in sources.TERMINAL or landed:
                add('release-claim', 'agent', f'{t["claim"]["owner"]}: release the claim on {t["path"]} '
                    f'({"its PR is merged" if landed else f"Linear says {state}"})')
    return actions, unavailable
