"""Worktree claims: a local, uncommitted lease saying which agent session is working where.

One JSON file per worktree in the owning repository's git common directory
(`<repo>/.git/ai-workflow/claims/`), so every worktree of that repository sees the same claims and
nothing is ever committed. A claim holds no secrets and no personal data: an opaque session
label, the agent kind, the branch, the AVR issue and timestamps. Deleting the directory loses
nothing that cannot be re-claimed.

A claim is `held` while its heartbeat is younger than its TTL, `stale` after that, `handoff`
when its owner offered it to someone else, and `corrupt` when the file cannot be read. Staleness
is judged by time, never by process id: sessions outlive and are outlived by processes.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

SCHEMA = 'ai-workflow.claim/v1'
DEFAULT_TTL_HOURS = 6
HEARTBEAT_MIN_SECONDS = 300


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def parse(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def norm(path):
    return os.path.normcase(os.path.realpath(str(path))).replace('\\', '/')


def claim_file(common_dir, worktree):
    n = norm(worktree)
    name = re.sub(r'[^A-Za-z0-9._-]', '_', n.rsplit('/', 1)[-1]) or 'worktree'
    return Path(common_dir) / 'ai-workflow' / 'claims' / f'{name}-{hashlib.sha1(n.encode()).hexdigest()[:10]}.json'


def read(path):
    """The claim document, None when there is none, or {'corrupt': True}."""
    try:
        doc = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {'corrupt': True}
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or not doc.get('owner') or not doc.get('heartbeat_at'):
        return {'corrupt': True}
    return doc


def state(claim, now=None):
    if claim is None:
        return 'free'
    if claim.get('corrupt'):
        return 'corrupt'
    if claim.get('handoff_to'):
        return 'handoff'
    try:
        age = ((now or utcnow()) - parse(claim['heartbeat_at'])).total_seconds()
    except ValueError:
        return 'corrupt'
    return 'stale' if age > float(claim.get('ttl_hours', DEFAULT_TTL_HOURS)) * 3600 else 'held'


@contextmanager
def _lock(path, timeout=5.0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:   # a crashed writer; the lock guards milliseconds
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f'claim lock busy: {lock}')
            time.sleep(0.02)
        except PermissionError:      # Windows: the holder is unlinking it right now
            if time.monotonic() > deadline:
                raise TimeoutError(f'claim lock busy: {lock}')
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _write(path, doc):
    tmp = Path(path).with_suffix(f'.tmp{os.getpid()}')
    tmp.write_text(json.dumps(doc, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


class Outcome:
    def __init__(self, ok, code, claim=None, message=''):
        self.ok, self.code, self.claim, self.message = ok, code, claim, message

    def as_dict(self):
        return {'ok': self.ok, 'code': self.code, 'message': self.message, 'claim': self.claim}


def describe(claim, now=None):
    if claim is None:
        return 'unclaimed'
    if claim.get('corrupt'):
        return 'an unreadable claim file'
    issue = f' for {claim["issue"]}' if claim.get('issue') else ''
    offer = f', offered to {claim["handoff_to"]}' if claim.get('handoff_to') else ''
    return f'{claim["owner"]} ({claim.get("agent", "agent")}){issue}, {state(claim, now)}{offer}, last active {claim["heartbeat_at"]}'


def acquire(path, worktree, owner, *, agent='agent', issue=None, branch=None, repo=None, note='',
            ttl_hours=DEFAULT_TTL_HOURS, now=None, busy=None, force=False):
    """Claim `worktree` for `owner`.

    Succeeds when the worktree is free, already ours, handed to us, or stale and not busy.
    `busy` is a callable returning a reason (uncommitted changes, a merge in progress) or None;
    a stale claim on a busy worktree may hold another session's unfinished work, so taking it
    needs `force`, which is an owner decision. A live claim by someone else is refused without
    `force`.
    """
    now = now or utcnow()
    with _lock(path):
        current = read(path)
        st = state(current, now)
        doc = {'schema': SCHEMA, 'worktree': norm(worktree), 'repo': repo, 'branch': branch, 'issue': issue,
               'owner': owner, 'agent': agent, 'claimed_at': stamp(now), 'heartbeat_at': stamp(now),
               'ttl_hours': ttl_hours, 'note': note}
        if st == 'free':
            code = 'claimed'
        elif st != 'corrupt' and current['owner'] == owner:      # ours; claiming again also withdraws a handoff offer
            doc['claimed_at'] = current.get('claimed_at', doc['claimed_at'])
            doc['issue'] = issue or current.get('issue')
            doc['note'] = note or current.get('note', '')
            if current.get('ready'):                         # a READY_FOR_PR record outlives a refresh
                doc['ready'] = current['ready']
            code = 'refreshed'
        elif st == 'handoff' and current['handoff_to'] in (owner, 'any'):
            doc['previous_owner'] = current['owner']
            doc['issue'] = issue or current.get('issue')
            doc['note'] = note or current.get('handoff_note', '')
            code = 'taken-handoff'
        elif force:
            doc['previous_owner'] = None if st == 'corrupt' else current['owner']
            doc['forced'] = True
            code = 'forced'
        elif st == 'stale':
            reason = busy() if busy else None
            if reason:
                return Outcome(False, 'refused-stale-busy', current,
                               f'stale claim by {describe(current, now)}, but the worktree has {reason}: '
                               'that may be unfinished work. Taking it over is an owner decision (--force).')
            doc['previous_owner'] = current['owner']
            code = 'taken-stale'
        elif st == 'corrupt':
            return Outcome(False, 'refused-corrupt', current, f'unreadable claim file {path}; inspect it, then --force')
        else:
            return Outcome(False, 'refused-held', current,
                           f'claimed by {describe(current, now)}. Work in a dedicated worktree, or ask for a handoff.')
        _write(path, doc)
        return Outcome(True, code, doc)


def heartbeat(path, owner, now=None, min_seconds=HEARTBEAT_MIN_SECONDS):
    """Refresh our own claim; cheap to call often (writes at most every `min_seconds`)."""
    now = now or utcnow()
    current = read(path)
    if state(current, now) not in ('held', 'stale') or current['owner'] != owner:
        return False
    if (now - parse(current['heartbeat_at'])).total_seconds() < min_seconds:
        return True
    with _lock(path):
        current = read(path)
        if state(current, now) not in ('held', 'stale') or current['owner'] != owner:
            return False
        current['heartbeat_at'] = stamp(now)
        _write(path, current)
    return True


def release(path, owner, force=False):
    with _lock(path):
        current = read(path)
        if current is None:
            return Outcome(True, 'not-claimed')
        if not force and (current.get('corrupt') or current['owner'] != owner):
            return Outcome(False, 'refused-not-owner', current, f'claimed by {describe(current)}; only its owner releases it (or --force)')
        Path(path).unlink()
        return Outcome(True, 'released', current)


def handoff(path, owner, to='any', note='', now=None):
    """Offer our claim to another session (`to` is its owner label, or 'any')."""
    now = now or utcnow()
    with _lock(path):
        current = read(path)
        if current is None or current.get('corrupt') or current['owner'] != owner:
            return Outcome(False, 'refused-not-owner', current, f'not yours to hand off: {describe(current, now)}')
        current.update(handoff_to=to, handoff_note=note, handoff_at=stamp(now))
        _write(path, current)
        return Outcome(True, 'handoff', current)


def annotate(path, owner, **fields):
    """Set extra fields on our own live claim (for example the READY_FOR_PR record)."""
    with _lock(path):
        current = read(path)
        if current is None or current.get('corrupt') or current['owner'] != owner:
            return Outcome(False, 'refused-not-owner', current, f'claim this worktree first: it is {describe(current)}')
        current.update(fields)
        _write(path, current)
        return Outcome(True, 'recorded', current)


def mark_released(path, commit, by, now=None):
    """The orchestrator's release of a READY_FOR_PR record. Not an ownership change: the claim's
    owner still opens the PR. Refused when the record is gone or is for another commit."""
    with _lock(path):
        current = read(path)
        ready = None if current is None or current.get('corrupt') else current.get('ready')
        if not ready or ready.get('commit') != commit:
            return Outcome(False, 'refused-changed', current, 'the READY_FOR_PR record changed; list the queue again')
        ready['released'] = {'by': by, 'at': stamp(now or utcnow())}
        _write(path, current)
        return Outcome(True, 'released', current)
