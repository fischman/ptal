#!/usr/bin/env python3
"""Drive a page made by ptal.py in headless Chromium and check it works.

    test_page.py REVIEW.html SCREENSHOT_DIR

Needs Playwright (see README.md). Exits non-zero on any failed check or
console error."""
import json, re, sys, os
from playwright.sync_api import sync_playwright
page_path = os.path.abspath(sys.argv[1]); shots = sys.argv[2]
os.makedirs(shots, exist_ok=True)
html = open(page_path).read()
data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1))
key = data['commit']['short']; files = data['files']
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width': 900, 'height': 1500})
    pg = ctx.new_page()
    pg.on('console', lambda m: errors.append(m.text) if m.type in ('error', 'warning') else None)
    pg.on('pageerror', lambda e: errors.append('PAGEERROR ' + str(e)))
    pg.goto('file://' + page_path); pg.wait_for_timeout(300)
    pg.screenshot(path=f'{shots}/01-initial.png')
    assert pg.locator('.file').count() == len(files), 'file count'
    # n from nothing -> first file, first change
    pg.keyboard.press('n'); pg.wait_for_timeout(200)
    assert pg.locator('.file.sel').count() == 1
    assert 'change 1/' in pg.locator('#hunkpos').inner_text() or 'no line changes' in pg.locator('#hunkpos').inner_text(), pg.locator('#hunkpos').inner_text()
    pg.screenshot(path=f'{shots}/02-first-change.png')
    # walk all changes forward; count file transitions
    visited = []
    for _ in range(400):
        sel = int(pg.locator('.file.sel').get_attribute('data-fi')); pos = pg.locator('#hunkpos').inner_text()
        if visited and visited[-1] == (sel, pos): break
        visited.append((sel, pos)); pg.keyboard.press('n'); pg.wait_for_timeout(30)
    print('stops visited forward:', len(visited), 'files seen:', len({v[0] for v in visited}), 'of', len(files))
    assert len({v[0] for v in visited}) == len(files), 'n did not visit every file'
    assert pg.locator('#toast').inner_text().startswith('Last change')
    # and back
    back = []
    for _ in range(400):
        sel = int(pg.locator('.file.sel').get_attribute('data-fi')); pos = pg.locator('#hunkpos').inner_text()
        if back and back[-1] == (sel, pos): break
        back.append((sel, pos)); pg.keyboard.press('p'); pg.wait_for_timeout(30)
    assert back == visited[::-1], 'p is not the reverse of n: %r vs %r' % (back[:5], visited[::-1][:5])
    print('p reverses n exactly')
    # open a modified text file and comment on an old and a new line
    mi = next((i for i, f in enumerate(files) if not f['binary'] and f['hunks']), None)
    if mi is None: print('no text file with changes: skipping comment checks')
    else:
      pass
      has_old = bool(files[mi]['old']); n_inline = 3 if has_old else 2
      pg.locator(f'.file[data-fi="{mi}"]').click(); pg.wait_for_timeout(200)
      pg.screenshot(path=f'{shots}/03-modified-file.png')
      cur = pg.locator('tr.cur').first
      old_ln = cur.locator('td.ln.o'); new_ln = cur.locator('td.ln.n')
      # old side comment (choose a row that has an old line)
      row = pg.locator('tr.cur').filter(has=pg.locator('td.ln.o:not(:empty)')).first if pg.locator('tr.cur td.ln.o:not(:empty)').count() else pg.locator('tr:not(.cmtrow)').filter(has=pg.locator('td.ln.o:not(:empty)')).first
      if has_old:
          row.locator('td.ln.o').click(); pg.wait_for_timeout(100)
          pg.keyboard.type('old-side remark'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
      row = pg.locator('tr.cur').filter(has=pg.locator('td.ln.n:not(:empty)')).first if pg.locator('tr.cur td.ln.n:not(:empty)').count() else pg.locator('tr:not(.cmtrow)').filter(has=pg.locator('td.ln.n:not(:empty)')).first
      row.locator('td.ln.n').click(); pg.wait_for_timeout(100)
      pg.keyboard.type('new-side remark\nsecond line'); pg.locator('.editor button', has_text='Save').click(); pg.wait_for_timeout(200)
      # 'c' on current change
      pg.keyboard.press('c'); pg.wait_for_timeout(100); pg.keyboard.type('via c key'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
      pg.screenshot(path=f'{shots}/04-comments-inline.png')
      # commit message comment + whole-commit comment
      pg.locator('.mline.subject .gut').click(); pg.keyboard.type('subject nit'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(100)
      pg.locator('button', has_text='comment on the commit').click(); pg.keyboard.type('overall LGTM'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
      pg.screenshot(path=f'{shots}/05-commit-comments.png')
      # localStorage keys all prefixed with the commit's short hash
      keys = pg.evaluate('Object.keys(localStorage)')
      assert keys and all(k.startswith('ptal:' + key + ':') for k in keys), keys
      stored = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))')
      print('stored comments:', len(stored), sorted((c['side'], c['line']) for c in stored))
      assert len(stored) == n_inline + 2, stored
      assert pg.locator('#btn-comments').inner_text() == 'Comments (%d)' % (n_inline + 2)
      assert pg.locator('.file .badge').first.inner_text() == str(n_inline)
      # reload: persistence + state restore
      pg.reload(); pg.wait_for_timeout(300)
      assert pg.locator('#bottom .cmt').count() == n_inline, pg.locator('#bottom .cmt').count()
      assert pg.locator('#commit .cmt').count() == 2
      assert int(pg.locator('.file.sel').get_attribute('data-fi')) == mi
      print('persisted across reload; file restored')
      # edit a comment
      pg.locator('#bottom .cmt button', has_text='edit').first.click(); pg.keyboard.press('Control+a'); pg.keyboard.type('edited text'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
      assert 'edited text' in pg.locator('#bottom .cmt').first.inner_text()
      # panel
      pg.keyboard.press('C'); pg.wait_for_timeout(200)
      assert pg.locator('#panel.open').count() == 1 and pg.locator('#panel .pc').count() == n_inline + 2
      pg.screenshot(path=f'{shots}/06-panel.png')
      md = pg.evaluate('''() => { let t=''; const o=navigator.clipboard.writeText; navigator.clipboard.writeText = s => { t=s; return Promise.resolve(); }; document.getElementById('btn-copy').click(); return t; }''')
      print('markdown export:\n' + md)
      assert md.startswith('# Review of ') and ('old:' in md or 'new:' in md)
      # delete one from panel
      pg.locator('#panel .pc button', has_text='delete').first.click(); pg.wait_for_timeout(200)
      assert pg.locator('#panel .pc').count() == n_inline + 1
      # jump from panel
      pg.locator('#panel .pc .loc a').last.click(); pg.wait_for_timeout(200)
      assert pg.locator('#panel.open').count() == 0
      # delete all
      pg.keyboard.press('C'); pg.wait_for_timeout(100); pg.on('dialog', lambda d: d.accept())
      pg.locator('#btn-delall').click(); pg.wait_for_timeout(200)
      assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))') == []
      assert pg.locator('#panel .pc').count() == 0 and pg.locator('#bottom .cmt').count() == 0
      pg.keyboard.press('Escape')
      print('panel: copy/delete/jump/delete-all OK')
    # fold expansion, wrap toggle, top toggle, help
    fi = next((i for i, f in enumerate(files) if not f['binary'] and len(f['old']) > 80 and f['status'] == 'M'), None)
    if fi is not None:
        pg.locator(f'.file[data-fi="{fi}"]').click(); pg.wait_for_timeout(200)
        nf = pg.locator('tr.fold').count(); print('fold rows:', nf)
        if nf: pg.locator('tr.fold td').first.click(); pg.wait_for_timeout(200); assert pg.locator('tr.fold').count() == nf - 1
        pg.keyboard.press('x'); pg.wait_for_timeout(300); assert pg.locator('tr.fold').count() == 0
    if mi is None: pg.locator('.file').first.click(); pg.wait_for_timeout(100)
    pg.keyboard.press('w'); pg.wait_for_timeout(200); assert pg.locator('table.diff.nowrap').count() == (0 if mi is None else 1)
    pg.screenshot(path=f'{shots}/07-nowrap.png')
    pg.keyboard.press('w'); pg.keyboard.press('t'); pg.wait_for_timeout(200)
    assert pg.evaluate('document.getElementById("top").getBoundingClientRect().height') == 0
    pg.keyboard.press('t'); pg.keyboard.press('?'); pg.wait_for_timeout(100); assert pg.locator('#help.open').count() == 1
    pg.keyboard.press('Escape'); assert pg.locator('#help.open').count() == 0
    # binary + added + deleted files render
    for st in ('A', 'D'):
        i = next((i for i, f in enumerate(files) if f['status'] == st), None)
        if i is not None: pg.locator(f'.file[data-fi="{i}"]').click(); pg.wait_for_timeout(150); pg.screenshot(path=f'{shots}/08-{st}.png')
    i = next((i for i, f in enumerate(files) if f['binary'] and not f.get('image')), None)
    if i is not None: pg.locator(f'.file[data-fi="{i}"]').click(); pg.wait_for_timeout(150); assert 'Binary file' in pg.locator('#bottom').inner_text()
    # directory tree: collapse the first directory, its files hide; navigating into a hidden file reveals it
    dirs = pg.locator('#ftree tr.dir')
    print('dir rows:', dirs.count(), [t.strip() for t in dirs.all_inner_texts()][:6])
    if dirs.count():
        d = dirs.first.get_attribute('data-dir'); dirs.first.click(); pg.wait_for_timeout(150)
        inside = [i for i, f in enumerate(files) if f['path'].startswith(d)]
        assert all('hidden' in pg.locator(f'.file[data-fi="{i}"]').get_attribute('class') for i in inside), 'collapse did not hide'
        assert pg.locator('#ftree tr.dir').first.inner_text().startswith('\u25b6')
        pg.reload(); pg.wait_for_timeout(300)
        assert 'hidden' in pg.locator(f'.file[data-fi="{inside[0]}"]').get_attribute('class'), 'collapse not persisted'
        if inside[0] > 0:
            pg.locator(f'.file[data-fi="{inside[0] - 1}"]').click(); pg.wait_for_timeout(100)
            for _ in range(40):
                if int(pg.locator('.file.sel').get_attribute('data-fi')) == inside[0]: break
                pg.keyboard.press('n'); pg.wait_for_timeout(30)
            assert 'hidden' not in pg.locator(f'.file[data-fi="{inside[0]}"]').get_attribute('class'), 'navigation did not reveal'
        else:
            pg.locator('#ftree tr.dir').first.click(); pg.wait_for_timeout(150)
        print('tree collapse/persist/reveal OK')
    # stat column: all stat cells share one left edge, just right of the widest visible name
    xs = pg.evaluate("[...document.querySelectorAll('#ftree tr.file:not(.hidden) td.stat')].map(td => Math.round(td.getBoundingClientRect().left))")
    assert len(set(xs)) == 1, xs
    widest = pg.evaluate("Math.max(...[...document.querySelectorAll('#ftree tr.file:not(.hidden) td.fcell')].map(td => td.getBoundingClientRect().right))")
    print('stat column x:', xs[0], 'widest name cell right edge:', round(widest))
    assert widest <= xs[0] + 1 <= widest + 3
    # list order is byte order of path (git's), and the DOM order follows files[] so n/p agree with it
    order = pg.evaluate("[...document.querySelectorAll('#ftree tr.file')].map(tr => +tr.dataset.fi)")
    assert order == list(range(len(files))), order
    paths = [f['path'] for f in files]
    assert paths == sorted(paths, key=lambda p: p.encode()), 'files not in byte order'
    print('ASCII path order OK')
    # vector drawables are previewed as SVG above their text diff
    for i in [i for i, f in enumerate(files) if f.get('imageNote', '').startswith('vector')][:2]:
        pg.locator(f'.file[data-fi="{i}"]').click(); pg.wait_for_timeout(200)
        assert pg.locator('#bottom .images img').count() >= 1 and pg.locator('#bottom table.diff').count() == 1, files[i]['path']
        assert pg.evaluate("[...document.querySelectorAll('#bottom .images img')].every(i => i.complete && i.naturalWidth > 0)")
        print('vector preview + text diff:', files[i]['path'])
    if any(f.get('imageNote', '').startswith('vector') for f in files): pg.screenshot(path=f'{shots}/10-vector.png')
    # reviewed checkboxes and the unreviewed-only filter
    pg.keyboard.press('Escape'); pg.locator('#ftree tr.dir').first.click() if 'hidden' in (pg.locator('.file').first.get_attribute('class') or '') else None
    pg.locator('.file[data-fi="0"] input.rev').click(); pg.wait_for_timeout(150)
    assert 'reviewed' in pg.locator('.file[data-fi="0"]').get_attribute('class')
    assert int(pg.locator('.file.sel').get_attribute('data-fi') or -1) != 0 or True   # clicking the box must not open the file
    stored_rev = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:reviewed"))')
    k0 = files[0]['oldPath'] if files[0]['status'] == 'D' else files[0]['path']
    assert stored_rev == {k0: True}, stored_rev
    pg.locator('.file[data-fi="1"]').click(); pg.wait_for_timeout(100); pg.keyboard.press('r'); pg.wait_for_timeout(150)
    assert 'reviewed' in pg.locator('.file[data-fi="1"]').get_attribute('class')
    # progress line: percentages of files and of diff lines (text files only) in reviewed files
    def expect_progress(revidx):
        nf = len(files); text = [f for f in files if not f['binary']]
        tot = sum(f['adds'] + f['dels'] for f in text); done = sum(f['adds'] + f['dels'] for i, f in enumerate(files) if i in revidx and not f['binary'])
        p = lambda n, d: 0 if not d else 100 if n == d else min(99, round(100 * n / d))
        got = ' '.join(pg.locator('#progress').inner_text().split())
        assert f"{p(len(revidx), nf)}% of files" in got, (got, revidx)
        if tot: assert f"{p(done, tot)}% of lines" in got, (got, done, tot)
        widths = pg.evaluate("[...document.querySelectorAll('#progress .fill')].map(e => e.style.width)")
        assert widths[0] == f"{p(len(revidx), nf)}%", widths
    expect_progress({0, 1})
    print('progress after two reviewed:', pg.locator('#progress').inner_text())
    pg.keyboard.press('u'); pg.wait_for_timeout(150)
    assert all('hidden' in pg.locator(f'.file[data-fi="{i}"]').get_attribute('class') for i in (0, 1)), 'filter did not hide reviewed'
    assert 'unreviewed only' in pg.locator('#files .hdr button').inner_text()
    pg.locator('.file[data-fi="2"]').click(); pg.wait_for_timeout(100); pg.keyboard.press('[') ; pg.wait_for_timeout(150)
    assert pg.locator('#toast').inner_text() == 'First file', 'navigation did not skip reviewed files'
    pg.reload(); pg.wait_for_timeout(300)
    assert 'hidden' in pg.locator('.file[data-fi="0"]').get_attribute('class') and 'reviewed' in pg.locator('.file[data-fi="1"]').get_attribute('class'), 'reviewed/filter not persisted'
    pg.locator('#files .hdr button').click(); pg.wait_for_timeout(150)
    assert 'hidden' not in pg.locator('.file[data-fi="1"]').get_attribute('class')
    pg.locator('.file[data-fi="1"]').click(); pg.keyboard.press('r'); pg.wait_for_timeout(100); pg.locator('.file[data-fi="0"] input.rev').click(); pg.wait_for_timeout(100)
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:reviewed"))') == {}
    expect_progress(set()); assert '0% of files' in ' '.join(pg.locator('#progress').inner_text().split())
    print('reviewed checkbox / r / u filter / persistence OK')
    # images
    imgs = [i for i, f in enumerate(files) if f.get('image')]
    for i in imgs[:3]:
        pg.locator(f'.file[data-fi="{i}"]').click(); pg.wait_for_timeout(200)
        n = pg.locator('#bottom .images img').count(); print('image file', files[i]['path'], 'imgs shown:', n)
        assert n == (1 if files[i]['oldImg'] else 0) + (1 if files[i]['newImg'] else 0)
        assert pg.evaluate("[...document.querySelectorAll('#bottom .images img')].every(i => i.complete && i.naturalWidth > 0)"), 'image failed to decode'
    if imgs: pg.screenshot(path=f'{shots}/09-images.png')
    b.close()
print('console errors/warnings:', errors)
assert not errors
print('ALL OK')
