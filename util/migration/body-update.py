#!/usr/bin/env python3
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

LIMIT = 65536
REPO = 'thunderbird/platform-infrastructure'
PARENT = '1719'
BOX = re.compile(r'^(\s*[-*] \[)([ xX])(\] )(.*)$')
PASS = re.compile(r'^\s*(?:[-*]\s+)?PASS(?::|\t| )\s*(.+?)\s*$')
FIELD = re.compile(r'\t| \| ')
HEADING = re.compile(r'^#{1,2}\s')
ENTRY = re.compile(r'^[-*]\s+Entry gate\b')
BLOCK = re.compile(r'^```placeholders[ \t]*\r?\n(.*?)^```', re.M | re.S)
PR_URL = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/\d+')
EPILOG = """\
--tick reads the evidence file of a GATE (#1719, ## Working rules, Evidence).
  Gate line: 'GATE <gate>: PASS' (or FAIL, STOPPED). The boxes of '## GATE <gate>'
  or '## Block <gate>' are ticked only when that line reads PASS.
  Box line: 'PASS<tab or space or colon><box text>[<tab or " | "><value>]'.
  A box is ticked when its text, whitespace collapsed, equals a PASS line text.
  Scope: the GATE or Block section, every 'Staged when' list that names the
  gate (a bare 'Staged when:' in an issue that holds the GATE), and the
  'Entry gate' lists of the parent issue (default #1719).
--close reads the one comment of the PR that holds ```placeholders blocks:
  'issue <number or URL>' selects the issue; '- <placeholder line>' is the
  exact line, stripped; each following '+ <line>' is a replacement line
  (none deletes it). A placeholder that says '[hand] lines above' also
  deletes the '# [hand]' lines directly above it.
Fact, investigation and exit-gate boxes of #1690 and #1694 have no mode:
  the control session ticks each by hand once the result comment is posted:
  gh issue view <n> -R thunderbird/platform-infrastructure --json body --jq .body > body.md,
  change that one box to '- [x]' and append the result comment URL,
  gh issue edit <n> -R thunderbird/platform-infrastructure --body-file body.md.
Every mode refuses a body above 65,536 characters. A failed or empty gh read
exits 1; a usage error exits 2.
"""


class Fail(Exception):
    pass


def norm(s):
    return ' '.join(s.split())


def gh(*args):
    try:
        p = subprocess.run(['gh', *args], capture_output=True, text=True)
    except OSError as e:
        raise Fail(f'gh {args[0]} {args[1]}: {e}')
    if p.returncode:
        raise Fail(f'gh {" ".join(args[:3])} failed: {p.stderr.strip()[:300]}')
    return p.stdout


def issue_ref(s, repo):
    m = re.fullmatch(r'https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)', s.strip())
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r'#?(\d+)', s.strip())
    if m:
        return repo, m.group(1)
    raise Fail(f'not an issue number or URL: {s}')


def load(out, key):
    try:
        data = json.loads(out)
    except ValueError:
        raise Fail(f'gh read of {key}: output is not JSON')
    return data.get(key) if isinstance(data, dict) else None


def label(ref):
    return f'{ref[0]}#{ref[1]}'


def read_body(ref):
    body = load(gh('issue', 'view', ref[1], '-R', ref[0], '--json', 'body'), 'body')
    if not isinstance(body, str) or not body.strip():
        raise Fail(f'gh issue view {label(ref)}: empty body')
    if len(body) > LIMIT:
        raise Fail(f'{label(ref)} body is {len(body)} characters, above {LIMIT}')
    return body


def read_evidence(path, gate):
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise Fail(f'evidence {path}: {e}')
    if not text.strip():
        raise Fail(f'evidence {path} is empty')
    status = re.compile(
        r'^\s*(?:#+\s*)?GATE\s+' + re.escape(gate) + r'\s*[:-]?\s*(PASS|FAIL|STOPPED)\b'
    )
    passed, states = [], []
    for line in text.splitlines():
        m = status.match(line)
        if m:
            states.append(m.group(1))
            continue
        m = PASS.match(line)
        if m:
            passed.append(norm(FIELD.split(m.group(1))[0]))
    return passed, bool(states) and all(s == 'PASS' for s in states)


def boxes(lines, gate, entry):
    head = re.compile(r'^##\s+(?:GATE|Block)\s+' + re.escape(gate) + r'(?=[:\s]|$)')
    tok = re.compile(r'(?<![\w-])' + re.escape(gate) + r'(?![\w-])')
    has_gate = any(head.match(x.rstrip('\r')) for x in lines)
    fence = section = staged = entries = False
    for i, raw in enumerate(lines):
        line = raw.rstrip('\r')
        if line.lstrip().startswith('```'):
            fence = not fence
            staged = entries = False
            continue
        if fence:
            continue
        if HEADING.match(line):
            section, staged, entries = bool(head.match(line)), False, False
            continue
        if line.startswith('Staged when'):
            staged = bool(tok.search(line)) or (norm(line) == 'Staged when:' and has_gate)
            entries = False
            continue
        if entry and ENTRY.match(line):
            entries, staged = True, False
            continue
        m = BOX.match(line)
        if m:
            if section:
                yield i, 'gate', m
            elif staged:
                yield i, 'staged', m
            elif entries and line[0].isspace():
                yield i, 'entry', m
            continue
        if not line.strip() or not line[0].isspace():
            staged = entries = False


def tick_body(body, gate, passed, gate_pass, entry, used, held):
    lines = body.split('\n')
    for i, kind, m in boxes(lines, gate, entry):
        text = norm(m.group(4))
        if text not in passed:
            continue
        if kind == 'gate' and not gate_pass:
            held.add(text)
            continue
        used.add(text)
        if m.group(2) == ' ':
            cr = '\r' if lines[i].endswith('\r') else ''
            lines[i] = m.group(1) + 'x' + m.group(3) + m.group(4) + cr
    return '\n'.join(lines)


def tick(a):
    refs = [issue_ref(x, a.repo) for x in a.issue or []]
    parent = issue_ref(a.parent, a.repo) if a.parent not in ('', 'none') else None
    if parent and parent not in refs:
        refs.append(parent)
    if not refs:
        raise Fail('no issue to edit: give --issue or --parent')
    passed, gate_pass = read_evidence(a.evidence, a.tick)
    if not passed:
        raise Fail(f'evidence {a.evidence} reports no PASS line')
    used, held, out = set(), set(), []
    for ref in refs:
        body = read_body(ref)
        new = tick_body(body, a.tick, set(passed), gate_pass, ref == parent, used, held)
        out.append((ref, body, new))
    for text in sorted(held):
        print(f'not ticked, GATE {a.tick} is not reported PASS: {text}', file=sys.stderr)
    left = [t for t in dict.fromkeys(passed) if t not in used and t not in held]
    for text in left:
        print(f'PASS line matched no box in scope: {text}', file=sys.stderr)
    if not any(b != n for _, b, n in out) and (left or held):
        raise Fail('no box ticked')
    return out


def parse_blocks(text, repo):
    entries, cur = [], None
    for block in BLOCK.findall(text):
        for raw in block.splitlines():
            line = raw.rstrip('\r')
            if not line.strip():
                continue
            m = re.fullmatch(r'issue\s+(\S+)\s*', line)
            if m:
                cur = issue_ref(m.group(1), repo)
            elif line.startswith('- '):
                if cur is None:
                    raise Fail(f'placeholder before any issue line: {line}')
                if 'PLACEHOLDER:' not in line:
                    raise Fail(f'not a PLACEHOLDER line: {line}')
                entries.append((cur, line[2:].strip(), []))
            elif line.startswith('+ ') or line == '+':
                if not entries:
                    raise Fail(f'replacement before any placeholder: {line}')
                entries[-1][2].append(line[2:])
            else:
                raise Fail(f'unreadable line in placeholders block: {line}')
    return entries


def close_body(body, ref, items):
    lines = body.split('\n')
    for old, new in items:
        hits = [i for i, x in enumerate(lines) if x.rstrip('\r').strip() == old]
        if len(hits) != 1:
            raise Fail(f'{label(ref)}: {len(hits)} lines equal placeholder: {old}')
        i = hits[0]
        cr = '\r' if lines[i].endswith('\r') else ''
        indent = re.match(r'[ \t]*', lines[i]).group(0)
        start = i
        if '[hand] lines above' in old:
            while start > 0 and '# [hand]' in lines[start - 1]:
                start -= 1
            if start == i:
                raise Fail(f'{label(ref)}: no [hand] line above placeholder: {old}')
        lines[start : i + 1] = [indent + x + cr for x in new]
    return '\n'.join(lines)


def close(a):
    if not PR_URL.fullmatch(a.close):
        raise Fail(f'not a pull request URL: {a.close}')
    out = gh('pr', 'view', a.close, '--json', 'author,comments')
    comments = load(out, 'comments')
    if not isinstance(comments, list) or not all(isinstance(c, dict) for c in comments):
        raise Fail(f'gh pr view {a.close}: unreadable output')
    if not comments:
        raise Fail(f'{a.close} has no comment')
    owner = (load(out, 'author') or {}).get('login')
    if not owner:
        raise Fail(f'gh pr view {a.close}: no PR author')
    carriers = [
        c.get('body') or ''
        for c in comments
        if (c.get('author') or {}).get('login') == owner and BLOCK.search(c.get('body') or '')
    ]
    if len(carriers) != 1:
        raise Fail(
            f'{a.close}: {len(carriers)} comments by the PR author {owner} carry a placeholders'
            ' block, want 1'
        )
    entries = parse_blocks(carriers[0], a.repo)
    if not entries:
        raise Fail(f'{a.close}: the placeholders block lists no line')
    for ref, _, _ in entries:
        if ref[0] != a.repo:
            raise Fail(f'{a.close}: {label(ref)} is outside {a.repo}')
    by_issue = {}
    for ref, old, new in entries:
        by_issue.setdefault(ref, []).append((old, new))
    out = []
    for ref, items in by_issue.items():
        body = read_body(ref)
        out.append((ref, body, close_body(body, ref, items)))
    return out


def diff(ref, old, new):
    a = [x + '\n' for x in old.split('\n')]
    b = [x + '\n' for x in new.split('\n')]
    return ''.join(difflib.unified_diff(a, b, f'a/{label(ref)}', f'b/{label(ref)}'))


def write(ref, body):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'body.md')
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(body)
        gh('issue', 'edit', ref[1], '-R', ref[0], '--body-file', p)


def parse(argv):
    p = argparse.ArgumentParser(
        prog='body-update.py',
        description='Tick GATE boxes or close PLACEHOLDER lines in phase issue bodies.',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--tick', metavar='GATE')
    g.add_argument('--close', metavar='PR_URL')
    p.add_argument('--evidence', metavar='FILE')
    p.add_argument('--issue', action='append', metavar='N_OR_URL')
    p.add_argument('--parent', default=PARENT, metavar='N_OR_URL')
    p.add_argument('--repo', default=REPO)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    if a.tick is not None and not a.evidence:
        p.error('--tick needs --evidence')
    if a.close is not None and (a.evidence or a.issue):
        p.error('--close takes no --evidence or --issue')
    return a


def main(argv=None):
    a = parse(argv)
    try:
        out = tick(a) if a.tick is not None else close(a)
        for ref, old, new in out:
            if len(new) > LIMIT:
                raise Fail(f'{label(ref)} would be {len(new)} characters, above {LIMIT}')
        for ref, old, new in out:
            if old == new:
                print(f'no change: {label(ref)}')
                continue
            sys.stdout.write(diff(ref, old, new))
            print(f'body size: {len(new)} characters ({label(ref)}, limit {LIMIT})')
        if not a.dry_run:
            for ref, old, new in out:
                if old != new:
                    write(ref, new)
                    print(f'written: {label(ref)}')
    except Fail as e:
        print(f'body-update.py: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
