#!/usr/bin/env python3
"""Check a range page (ptal.py ROOT A..B): builds a throwaway repository in
which one file is changed and changed back inside the range, checks the
page's data with git alone, then drives the page in headless Chromium
(every commit's message, comments on a message line of one commit, the
brief).  Needs Playwright (see README.md).

    test_range.py SCREENSHOT_DIR
"""
import json, os, re, subprocess, sys, tempfile
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
shots = sys.argv[1]; os.makedirs(shots, exist_ok=True)
tmp = tempfile.mkdtemp(); repo = os.path.join(tmp, 'r')
def git(*a): return subprocess.run(['git', '-C', repo, *a], check=True, capture_output=True, text=True).stdout.strip()
os.makedirs(repo); git('init', '-q'); git('config', 'user.name', 'T'); git('config', 'user.email', 't@example.com')
def commit(files, *msg):
    for name, text in files.items():
        with open(os.path.join(repo, name), 'w') as fh: fh.write(text)
    git('add', '.'); git('commit', '-q', *sum((['-m', m] for m in msg), []))
commit({'a.txt': 'a\n', 'b.txt': 'b\n'}, 'base')
commit({'a.txt': 'a changed\n'}, 'Change a')
commit({'b.txt': 'b changed\n'}, 'Change b', 'Second paragraph.')
commit({'a.txt': 'a\n'}, 'Change a back')
base, tip = git('rev-parse', 'HEAD~3'), git('rev-parse', 'HEAD')

def page(spec):
    out = os.path.join(tmp, 'page.html')
    subprocess.run([sys.executable, os.path.join(HERE, 'ptal.py'), repo, spec, '-o', out], check=True, capture_output=True)
    return out, json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', open(out).read(), re.S).group(1))

# data: the range is one change; a.txt, changed and changed back, is not in it
for spec in ('HEAD~3..HEAD', 'HEAD~3..', base + '...' + tip):
    _, d = page(spec); c = d['commit']
    assert [x['sha'] for x in d['snaps']] == [base, tip], spec
    assert [f['key'] for f in d['files']] == ['b.txt'], (spec, [f['key'] for f in d['files']])
    assert [m['subject'] for m in c['range']['commits']] == ['Change a', 'Change b', 'Change a back'], spec
    assert c['range']['spec'] == base + '..' + tip and c['short'] == base[:7] + '..' + tip[:7] and c['subject'] == '3 commits', c
r = subprocess.run([sys.executable, os.path.join(HERE, 'ptal.py'), repo, 'HEAD..HEAD', '-o', os.devnull], capture_output=True, text=True)
assert r.returncode != 0 and 'no commits in the range' in r.stderr, r.stderr
_, d = page('HEAD~2..HEAD~1'); assert d['commit']['subject'] == 'Change b' and len(d['commit']['range']['commits']) == 1
print('range data OK')

path, data = page('HEAD~3..HEAD'); key = data['commit']['short']; second = data['commit']['range']['commits'][1]['sha']
errors = []
def txt(loc): return ' '.join(loc.inner_text().split())
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_context(viewport={'width': 900, 'height': 1400}).new_page()
    pg.on('console', lambda m: errors.append(m.text) if m.type in ('error', 'warning') else None)
    pg.on('pageerror', lambda e: errors.append('PAGEERROR ' + str(e)))
    pg.goto('file://' + path); pg.wait_for_timeout(300)
    msgs = pg.locator('#commit .msg'); assert msgs.count() == 3, msgs.count()
    assert 'range of 3 commits' in txt(pg.locator('#commit .meta')), txt(pg.locator('#commit .meta'))
    assert [txt(m.locator('.mline.subject')) for m in msgs.all()] == ['Change a', 'Change b', 'Change a back']
    assert pg.locator('#ftree tr.file').count() == 1
    # comment on line 3 of the second commit's message: only that commit's line carries it
    m2 = pg.locator(f'#commit .msg[data-msg="{second}"]')
    m2.locator('.mline[data-line="3"] .gut').click(); pg.keyboard.type('reword this'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
    assert m2.locator('.cmt').count() == 1 and pg.locator('#commit .cmt').count() == 1
    assert pg.locator('#commit .mline .gut.has').count() == 1
    pg.locator('button', has_text='comment on the whole range').click(); pg.keyboard.type('overall fine'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
    stored = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))')
    assert sorted((c.get('msg', ''), c['line']) for c in stored) == [('', 0), (second, 3)], stored
    pg.reload(); pg.wait_for_timeout(300)
    assert pg.locator(f'#commit .msg[data-msg="{second}"] .cmt').count() == 1, 'comment lost on reload'
    pg.screenshot(path=f'{shots}/01-range.png')
    # panel names the commit; brief points the message comment at that commit
    pg.locator('#btn-comments').click(); pg.wait_for_timeout(200)
    assert second[:7] + ' message line 3' in txt(pg.locator('#panel')) and 'whole range' in txt(pg.locator('#panel')), txt(pg.locator('#panel'))
    brief = pg.evaluate('''() => { let t=''; navigator.clipboard.writeText = s => { t=s; return Promise.resolve(); }; document.getElementById('btn-brief').click(); return t; }''')
    assert 'S0 = ' + base + ' (start of the reviewed range)' in brief and 'S1 = ' + tip + ' (end of the reviewed range)' in brief, brief
    assert 'ptal.py ' + repo + ' ' + base + '..' + tip + ' --then <your commit>' in brief, brief
    cs = {c['text']: c for c in json.loads(re.findall(r'```json\n(.*?)\n```', brief, re.S)[-1])['comments']}
    assert cs['reword this']['commit'] == second and cs['reword this']['messageLine'] == 3 and cs['reword this']['context']['3'] == 'Second paragraph.', cs
    assert cs['overall fine']['commit'] == tip and 'messageLine' not in cs['overall fine'], cs
    print('page OK')
    b.close()
print('console errors/warnings:', errors)
assert not errors
print('ALL OK')
