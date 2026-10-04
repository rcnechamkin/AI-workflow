"""Claude Code PreToolUse hook: surface a worktree collision before the edit, not after.

    echo '{"session_id": "...", "cwd": "...", "tool_name": "Edit", "tool_input": {...}}' | python aw.py guard

A convenience layer over the same claim files every agent uses (`aw.py claim`); Codex and humans
claim explicitly. Before an edit or a state-changing git command inside a configured repository:

- unclaimed worktree, or one handed to this session: claim it for this session;
- our own claim: refresh its heartbeat;
- a stale claim on a clean worktree: take it and say so;
- another session's live claim, or a stale claim over unfinished work: ask the person at the
  keyboard (an unattended session cannot approve it).

Always exits 0 and never blocks outside the configured repositories or on its own failure.
"""
import json
import re
import sys

from . import claims, gitio, model

EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
GIT_WRITE = re.compile(r'\bgit\b((?:\s+-[cC]\s+\S+)*)\s+(commit|merge|rebase|cherry-pick|revert|reset|checkout|switch|restore|stash|pull|am|apply|add|rm|mv)\b')


def targets(payload):
    """Paths whose worktree this tool call is about to change."""
    tool, inp = payload.get('tool_name', ''), payload.get('tool_input') or {}
    if tool in EDIT_TOOLS:
        path = inp.get('file_path') or inp.get('notebook_path')
        return [path] if path else []
    if tool != 'Bash':
        return []
    command, cwd = inp.get('command') or '', payload.get('cwd')
    cd = re.match(r'\s*cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*(&&|;)', command)
    if cd:
        cwd = cd[2] or cd[3] or cd[4]
    found = []
    for m in GIT_WRITE.finditer(command):
        dash_c = re.search(r'-C\s+("([^"]+)"|\'([^\']+)\'|(\S+))', m[1] or '')
        found.append((dash_c[2] or dash_c[3] or dash_c[4]) if dash_c else cwd)
    return [p for p in found if p]


def _msys(path):
    m = re.match(r'^/([a-zA-Z])/(.*)$', path)        # Git Bash spelling of a Windows drive path
    return f'{m[1]}:/{m[2]}' if m and sys.platform == 'win32' else path


def evaluate(payload, cfg, now=None):
    """('ok' | 'note' | 'ask', message)."""
    session = payload.get('session_id')
    if not session:
        return 'ok', ''
    owner = f'claude-{session[:8]}'
    commons = {claims.norm(c): name for name, r in cfg['repos'].items() if (c := gitio.common_dir(r['path']))}
    notes = []
    for path in dict.fromkeys(targets(payload)):
        top = gitio.toplevel(_msys(path))
        common = gitio.common_dir(top) if top else None
        if not common or claims.norm(common) not in commons:
            continue
        file = claims.claim_file(common, top)
        current = claims.read(file)
        st = claims.state(current, now)
        if st in ('held', 'stale') and current['owner'] == owner:
            claims.heartbeat(file, owner, now)
            continue
        rc, branch = gitio.git(top, 'branch', '--show-current')
        got = claims.acquire(file, top, owner, agent='claude', issue=gitio.issue_of(branch), branch=branch or None,
                             repo=commons[claims.norm(common)], ttl_hours=cfg.get('claim_ttl_hours', claims.DEFAULT_TTL_HOURS),
                             now=now, busy=lambda top=top: gitio.busy(top))
        if not got.ok:
            return 'ask', (f'worktree {top} is {got.message} Two sessions in one worktree is how conflict markers got '
                           'committed; approve only if you are deliberately taking this worktree over.')
        if got.code != 'claimed':
            notes.append(f'{top}: claim {got.code} (was {got.claim.get("previous_owner")})')
        else:
            notes.append(f'{top}: claimed for this session ({owner}); release with `aw.py release` when done')
    return ('note', 'Avrana worktree claims: ' + '; '.join(notes)) if notes else ('ok', '')


def main(stdin=None, cfg=None):
    try:
        payload = json.load(stdin or sys.stdin)
        decision, message = evaluate(payload, cfg or model.load_config())
    except Exception:          # a guard that crashes must not stop work; the claim CLI still exists
        return 0
    if decision == 'ask':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'ask',
                                                 'permissionDecisionReason': f'Avrana worktree guard: {message}'}}))
    elif decision == 'note':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': message}}))
    return 0
