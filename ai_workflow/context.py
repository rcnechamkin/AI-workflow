"""The context manifest: the smallest set of authoritative things to read before working an issue.

Linear issue in, a ranked list of references out. Never file contents: the agent opens what it
needs. Every item says where it came from, how much authority it carries and why it was picked.

Retrieval order (the `tier` of an item; a file appears once, at its best tier):

    1  explicit      a path, basename, ADR number or defined name written in the issue
    2  canonical     the repository entry point, and canonical docs/ADRs that mention what the issue names
    3  exact         code and tests that contain an identifier the issue names, and tests named after that code
    4  graphify      files a Graphify node points at: a lead, verified to exist, never an authority
    5  search        files matching several title words: inferred, last, and only if anything above was found

Everything is read from git objects at one commit per repository (the issue's worktree HEAD when
it has one, else origin/main), so the manifest does not depend on what any checkout has open and
changes nothing. The manifest itself is disposable: `save` writes it under this tool's state
directory, never into a product repository.
"""
import json
import os
from pathlib import Path
import re

from . import gitio, model

SCHEMA = 'ai-workflow.context-manifest/v1'
MAX_ITEMS, MAX_TOKENS = 20, 60000
ITEM_COST_CAP = 6000        # a large file is read selectively: it costs the budget at most this much
COMMON = 20                 # an identifier found in more files than this locates nothing
GRAPH = 'context/graphify/semantic-graph.json'
DERIVED_DIRS = ('context/graphify/', 'graphify-public/', 'graphify-out/', '.graphify-context/')
GOVERNING_CLASSES = {'canonical', 'decision'}
NEVER_INFERRED = {'historical', 'evidence', 'archived', 'research'}
GENERIC = {'src', 'lib', 'games', 'tests', 'test', 'web', 'core', 'docs', 'tools', 'ops', 'avrana', 'engine', 'index', 'main',
           'server', 'utils', 'client', 'common', 'party'}
STOP = {'about', 'after', 'again', 'before', 'being', 'between', 'could', 'every', 'first', 'their', 'there', 'these', 'those',
        'through', 'under', 'until', 'where', 'which', 'while', 'within', 'without', 'should', 'would', 'other', 'only'}
CODE = ('.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.sh', '.html', '.css', '.go', '.rs')
KIND_ORDER = {'governing': 0, 'implementation': 1, 'test': 2}


class Checkout:
    """One repository at one commit, read through git objects only."""

    def __init__(self, name, path, rev, label, worktree=None):
        self.name, self.path, self.rev, self.label, self.worktree = name, path, rev, label, worktree
        self.commit = gitio.rev(path, rev)
        rc, out = gitio.git(path, 'ls-tree', '-r', '-l', rev)
        self.sizes = {}
        for line in out.splitlines() if rc == 0 else []:
            meta, _, file = line.partition('\t')
            parts = meta.split()
            if len(parts) == 4 and parts[1] == 'blob' and parts[3].isdigit():
                self.sizes[file] = int(parts[3])
        self._classes = None

    def read(self, file):
        rc, out = gitio.git(self.path, 'show', f'{self.rev}:{file}')
        return out if rc == 0 else None

    def grep(self, pattern, regex=False, ignore_case=False, paths=()):
        args = ['grep', '-l', '-I'] + (['-E'] if regex else ['-F', '-w']) + (['-i'] if ignore_case else [])
        rc, out = gitio.git(self.path, *args, '-e', pattern, self.rev, '--', *paths)
        prefix = f'{self.rev}:'
        return [line.removeprefix(prefix) for line in out.splitlines()] if rc == 0 else []

    def classes(self):
        """{path: class} from the repository's docs manifest (docs/manifest.json), when it has one."""
        if self._classes is None:
            self._classes = {}
            try:
                docs = json.loads(self.read('docs/manifest.json') or '{}').get('documents', [])
                rows = docs.values() if isinstance(docs, dict) else docs
                self._classes = {d['path']: d.get('class') for d in rows if isinstance(d, dict) and d.get('path')}
            except (ValueError, AttributeError, TypeError):
                pass
        return self._classes


def kind_of(file):
    base = file.rsplit('/', 1)[-1]
    if file.endswith('.md'):
        return 'governing'
    if file.startswith('tests/') or '/tests/' in file or base.startswith('test_') or '.spec.' in base or '.test.' in base:
        return 'test'
    return 'implementation'


def authority_of(co, file):
    """How much weight a file carries, from the repository's own classification."""
    cls = co.classes().get(file)
    if re.search(r'(^|/)docs/adr/\d{4}-', file) or cls == 'decision':
        m = re.search(r'^Status:\W*(\w+)', co.read(file) or '', re.M)
        return f'adr ({m[1].lower()})' if m else 'adr (status not stated)'
    if kind_of(file) == 'test':
        return 'tests'
    if not file.endswith('.md'):
        return 'implementation'
    if cls == 'canonical':
        return 'canonical'
    if cls in NEVER_INFERRED:
        return f'{cls} (evidence, not instruction)'
    return cls or 'unclassified document'


def references(text):
    """(paths, identifiers, adr numbers) written in the issue, in order of appearance."""
    text = re.sub(r'https?://\S+', ' ', text or '')
    paths, names = [], []
    for token in re.findall(r'`([^`\n]+)`', text):
        token = token.strip().strip('.,;:')
        if not token or ' ' in token or token[0] == '-' or any(c in token for c in '<>*{}=$'):
            continue
        if '/' in token or re.search(r'\.[A-Za-z0-9]{1,5}$', token):
            paths.append(token.lstrip('./'))
        elif re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.\-]{3,}', token):
            names.append(token)
    bare = re.sub(r'`[^`\n]+`', ' ', text)
    paths += [p for p in re.findall(r'(?<![\w/.-])((?:[\w.-]+/)+[\w-]+\.[A-Za-z0-9]{1,5})\b', bare)]
    adrs = re.findall(r'\bADR[ -]?(\d{4})\b', text)
    unique = lambda rows: list(dict.fromkeys(rows))                                  # noqa: E731
    return unique(paths), unique(names), unique(adrs)


def checkouts(cfg, repos, ws, issue_id):
    """The commit to inspect in every configured repository; `repos` are the issue's targets."""
    out = {}
    for name, repo in cfg['repos'].items():
        if not ws.get(name, {}).get('available'):
            continue
        tree = next((t for t in ws[name]['data'] if t['issue'] == issue_id and not t['missing']), None) if name in repos else None
        out[name] = Checkout(name, tree['path'], 'HEAD', 'worktree HEAD', tree['path']) if tree else \
            Checkout(name, repo['path'], 'origin/main', 'origin/main')
    return out


def build(cfg, issue, repos, ws, prs=(), max_items=MAX_ITEMS, max_tokens=MAX_TOKENS):
    """The manifest for a normalised Linear issue and the repositories it touches."""
    manifest = {'schema': SCHEMA, 'issue': issue['id'], 'title': issue['title'], 'status': 'ok', 'repos': {}, 'items': [],
                'prs': list(prs), 'warnings': [], 'budget': {'max_items': max_items, 'max_tokens': max_tokens, 'tokens': 0,
                                                              'dropped': 0, 'dropped_items': []}}
    warn = manifest['warnings'].append
    if not repos:
        manifest['status'] = 'insufficient'
        warn('the repositories this issue touches are undetermined, so no context was assembled (nothing is guessed)')
        return manifest
    every = checkouts(cfg, repos, ws, issue['id'])
    targets = {n: c for n, c in every.items() if n in repos and c.commit}
    for name in repos:
        if name not in targets:
            warn(f'{name}: no readable checkout; nothing from that repository is in this manifest')
    manifest['repos'] = {n: {'commit': c.commit, 'rev': c.label, 'worktree': c.worktree} for n, c in targets.items()}
    found = {}                                               # (repo, file) -> item, kept at its best tier

    def add(co, file, tier, source_type, why, score=0):
        key = (co.name, file)
        if key in found and (found[key]['tier'], -found[key]['_score']) <= (tier, -score):
            return
        found[key] = {'ref': file, 'repo': co.name, 'kind': kind_of(file), 'tier': tier, 'source_type': source_type,
                      'authority': authority_of(co, file), 'why': why, 'commit': co.commit,
                      'tokens': co.sizes.get(file, 0) // 4, '_score': score}

    paths, names, adrs = references(f'{issue["title"]}\n{issue.get("description") or ""}')

    # tier 1: what the issue names
    for token in paths:
        hits = [(co, token) for co in targets.values() if token in co.sizes]
        how = f'named in the issue (`{token}`)'
        if not hits:
            hits = [(co, f) for co in targets.values() for f in co.sizes if f.endswith('/' + token)]
            how = f'named in the issue by basename (`{token}`)'
            if len(hits) > 1:
                warn(f'`{token}` is ambiguous: {len(hits)} files have that name ({", ".join(f for _, f in hits[:5])}); all are listed')
        if not hits:
            warn(f'`{token}` is named in the issue but not found at {", ".join(f"{c.name} {c.label}" for c in targets.values())}')
        for co, file in hits[:5]:
            add(co, file, 1, 'issue-reference', how, score=3)
    for number in adrs:
        hits = [(co, f) for co in every.values() if co.commit for f in co.sizes if re.search(rf'(^|/)docs/adr/{number}-', f)]
        if not hits:
            warn(f'ADR {number} is named in the issue but no docs/adr/{number}-*.md exists in the configured repositories')
        for co, file in hits:
            add(co, file, 1, 'issue-reference', f'named in the issue (ADR {number})', score=4)
    live = []                                                # identifiers that exist somewhere: used by tiers 2 to 4
    for name in names:
        defined = [(co, f) for co in targets.values()
                   for f in co.grep(rf'(def|class|function|const|let|var|fn)\s+{re.escape(name)}\b', regex=True)]
        mentioned = [(co, f) for co in targets.values() for f in co.grep(name)]
        if not defined and not mentioned:
            warn(f'`{name}` is named in the issue but not found in {", ".join(targets)}')
            continue
        if len(mentioned) > COMMON:
            warn(f'`{name}` is named in the issue but is too common to locate anything ({len(mentioned)} files contain it); ignored')
            continue
        live.append((name, mentioned))
        for co, file in defined[:3]:
            add(co, file, 1, 'issue-reference', f'named in the issue: defines `{name}`', score=2)

    # tier 2: the entry point, and governing documents that mention what the issue names
    for co in targets.values():
        if 'AGENTS.md' in co.sizes:
            add(co, 'AGENTS.md', 2, 'canonical-doc', 'repository entry point: the rules for any change here', score=9)
    for name, mentioned in live:
        for co, file in mentioned:
            if file.endswith('.md') and (co.classes().get(file) in GOVERNING_CLASSES or re.search(r'(^|/)docs/adr/\d{4}-', file)):
                prior = found.get((co.name, file), {}).get('_score', 0) if found.get((co.name, file), {}).get('tier') == 2 else 0
                add(co, file, 2, 'canonical-doc', f'canonical document that mentions `{name}`', score=prior + 1)

    # tier 3: exact code and test matches
    anchors = [(co, f) for (repo, f), item in found.items() if item['tier'] == 1 and item['kind'] == 'implementation'
               for co in [targets.get(repo)] if co]
    near = {(co.name, f.rsplit('/', 1)[0]) for co, f in anchors}
    for name, mentioned in live:
        rows = [(co, f) for co, f in mentioned if not f.endswith('.md') and not f.startswith(DERIVED_DIRS)]
        rows.sort(key=lambda r: ((r[0].name, r[1].rsplit('/', 1)[0]) not in near, r[1]))
        for co, file in rows[:5]:
            add(co, file, 3, 'test-match' if kind_of(file) == 'test' else 'code-match', f'contains `{name}`, which the issue names',
                score=2 if (co.name, file.rsplit('/', 1)[0]) in near else 1)
    for co, source in anchors:
        folder = source.rsplit('/', 1)[0]
        words = [w for w in (folder.rsplit('/', 1)[-1], source.rsplit('/', 1)[-1].split('.')[0]) if w.lower() not in GENERIC and len(w) > 2]
        for file in co.sizes:
            if kind_of(file) == 'test' and any(w.lower() in re.split(r'[^a-z0-9]+', file.rsplit('/', 1)[-1].lower()) for w in words):
                add(co, file, 3, 'test-match', f'test file named after {folder} (where `{source}` lives)', score=0)

    if not any(i['tier'] in (1, 3) for i in found.values()):
        manifest['status'] = 'insufficient'
        warn('the issue names no file, test, ADR or identifier that exists in the repository, so the scope cannot be located '
             'without guessing: find the code yourself, or add the references to the issue. Graphify and search leads are '
             'withheld because nothing authoritative anchors them.')
    else:
        _graphify(targets, names, issue['title'], add, warn, found)
        _search(targets, issue['title'], add, found)

    ordered = sorted(found.values(), key=lambda i: (i['tier'], -i['_score'], KIND_ORDER[i['kind']], i['repo'], i['ref']))
    budget = manifest['budget']
    for item in ordered:
        score = item.pop('_score')
        cost = min(item['tokens'], ITEM_COST_CAP)
        over = len(manifest['items']) >= max_items or (manifest['items'] and budget['tokens'] + cost > max_tokens)
        if over or budget['dropped']:                        # once trimming starts, everything after is lower priority
            budget['dropped'] += 1
            if len(budget['dropped_items']) < 15:
                budget['dropped_items'].append({'ref': item['ref'], 'repo': item['repo'], 'tier': item['tier']})
            continue
        manifest['items'].append(item)
        budget['tokens'] += cost
    if budget['dropped']:
        warn(f'context budget reached ({max_items} items, ~{max_tokens} tokens): {budget["dropped"]} lower-priority item(s) left out')
    return manifest


def _graphify(targets, names, title, add, warn, found):
    words = keywords(title)
    for co in targets.values():
        if GRAPH not in co.sizes:
            continue
        try:
            graph = json.loads(co.read(GRAPH))
            nodes, built = graph['nodes'], graph.get('built_at_commit')
        except (ValueError, TypeError, KeyError):
            warn(f'{co.name}: the Graphify graph is unreadable; no Graphify leads')
            continue
        changed = None
        if built:
            rc, out = gitio.git(co.path, 'diff', '--name-only', built, co.rev)
            if rc == 0:
                changed = {f for f in out.splitlines() if not f.startswith(DERIVED_DIRS)}
        if changed is None:
            warn(f'{co.name}: whether the Graphify graph is stale cannot be determined (built at '
                 f'{str(built)[:12] or "an unrecorded commit"}, which this checkout does not have); treat its leads with care')
        elif changed:
            warn(f'{co.name}: the Graphify graph is stale: built at {built[:12]}, {len(changed)} file(s) changed since '
                 f'({co.label} {co.commit[:12]}). Its hits are leads only.')
        hits, missing = 0, []
        for node in nodes:
            text = ' '.join(str(node.get(k) or '') for k in ('label', 'norm_label', 'description')).lower()
            file = node.get('source_file') or ''
            named = [n for n in names if n.lower() in text]
            if not named and sum(w in text for w in words) < 2:
                continue
            if file not in co.sizes:                         # verified against the real checkout, or it is not a lead
                missing.append(file)
                continue
            if file.startswith(DERIVED_DIRS) or (co.name, file) in found or hits >= 5:
                continue
            if co.classes().get(file) in NEVER_INFERRED:
                continue
            hits += 1
            add(co, file, 4, 'graphify', f'Graphify node "{node.get("label")}" points here (derived; the file exists at this commit'
                + ('; it has changed since the graph was built' if changed and file in changed else '') + ')')
            found[(co.name, file)]['authority'] = 'derived (verify in the file)'
        for file in list(dict.fromkeys(missing))[:5]:
            warn(f'{co.name}: Graphify points at {file}, which does not exist at {co.label}; ignored')


def keywords(title):
    return [w for w in dict.fromkeys(re.findall(r'[a-z][a-z0-9_]{4,}', (title or '').lower())) if w not in STOP][:8]


def _search(targets, title, add, found):
    words = keywords(title)
    for co in targets.values():
        counts = {}
        for word in words:
            for file in co.grep(word, ignore_case=True):
                if file.endswith(CODE) and not file.startswith(DERIVED_DIRS):          # code only: never data files
                    counts.setdefault(file, []).append(word)
        ranked = sorted(((f, ws) for f, ws in counts.items() if len(ws) >= 2 and (co.name, f) not in found),
                        key=lambda r: (-len(r[1]), r[0]))
        for file, matched in ranked[:5]:
            add(co, file, 5, 'search', f'ordinary search: contains the title words {", ".join(matched)} (inferred, not confirmed)',
                score=len(matched))
            found[(co.name, file)]['authority'] = 'inferred'


def state_dir():
    return Path(os.environ.get('AI_WORKFLOW_STATE') or model.ROOT / '.state')


def save(manifest):
    """Write the manifest under this tool's state directory (disposable; never a product repository)."""
    path = state_dir() / 'context' / f'{manifest["issue"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return path


def render(manifest):
    """The concise text form: what to read first, and why."""
    repos = ', '.join(f'{n} @ {str(r["commit"])[:12]} ({r["rev"]})' for n, r in manifest['repos'].items()) or 'no repository'
    out = [f'Context for {manifest["issue"]} [{manifest["status"]}]: {repos}']
    many = len(manifest['repos']) > 1

    def line(i):
        where = f'{i["repo"]}:' if many or i['repo'] not in manifest['repos'] else ''
        big = f' (~{i["tokens"] // 1000}k tokens: read selectively)' if i['tokens'] > ITEM_COST_CAP else ''
        return f'    {where}{i["ref"]}  [{i["authority"]}]  {i["why"]}{big}'

    sections = (('Governing', lambda i: i['tier'] <= 3 and i['kind'] == 'governing'),
                ('Implementation', lambda i: i['tier'] <= 3 and i['kind'] == 'implementation'),
                ('Tests', lambda i: i['tier'] <= 3 and i['kind'] == 'test'),
                ('Leads from Graphify (derived, verify)', lambda i: i['tier'] == 4),
                ('Leads from ordinary search (inferred)', lambda i: i['tier'] == 5))
    for title, wanted in sections:
        rows = [i for i in manifest['items'] if wanted(i)]
        if rows:
            out += [f'  {title}'] + [line(i) for i in rows]
    if manifest['warnings']:
        out += ['  Warnings'] + [f'    {w}' for w in manifest['warnings']]
    b = manifest['budget']
    out.append(f'  Budget   {len(manifest["items"])}/{b["max_items"]} items, ~{b["tokens"]}/{b["max_tokens"]} tokens'
               + (f'; {b["dropped"]} left out' if b['dropped'] else ''))
    return out
