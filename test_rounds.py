#!/usr/bin/env python3
"""Drive a multi-snapshot page (made with --then/--replies/--comments) in
headless Chromium: seeded comments, threaded agent replies, line mapping,
view switching, resolve marks, the brief, and storage records in the
single-commit format.  Needs Playwright (see README.md).

    test_rounds.py REVIEW.html SCREENSHOT_DIR
"""
import json
import os, re, sys
from playwright.sync_api import sync_playwright
page_path = os.path.abspath(sys.argv[1]); shots = sys.argv[2]; os.makedirs(shots, exist_ok=True)
data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', open(page_path).read(), re.S).group(1))
key = data['commit']['short']; M = len(data['snaps']); assert M == 3
errors = []
def txt(loc): return ' '.join(loc.inner_text().split())
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={'width': 900, 'height': 1400}); pg = ctx.new_page()
    pg.on('console', lambda m: errors.append(m.text) if m.type in ('error', 'warning') and 'blob:x' not in m.text else None)
    pg.on('pageerror', lambda e: errors.append('PAGEERROR ' + str(e)))
    # legacy records written by the single-commit page, present before the page loads
    pg.goto('file://' + page_path); pg.evaluate(f'''localStorage.setItem("ptal:{key}:comments", JSON.stringify([
        {{id: "L1", file: "a.txt", side: "old", line: 1, src: "line one", text: "legacy old-side", created: "2026-10-25T00:00:00Z"}},
        {{id: "L2", file: "a.txt", side: "new", line: 6, src: "line six", text: "legacy new-side", created: "2026-10-25T00:00:01Z"}},
        {{id: "L3", file: null, side: "commit", line: 0, src: "review me", text: "legacy whole-commit", created: "2026-10-25T00:00:02Z"}}]))''')
    pg.reload(); pg.wait_for_timeout(300)
    # seed merged in: 5 seeded + 3 legacy = 8 reviewer comments
    assert txt(pg.locator('#btn-comments')) == 'Comments (10)', txt(pg.locator('#btn-comments'))
    stored = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))')
    assert {c['id'] for c in stored} == {'c1', 'c2', 'c2r', 'c3', 'c4', 'c5', 'n1', 'L1', 'L2', 'L3'}
    by = {c['id']: c for c in stored}
    assert by['L1']['snap'] == 0 and by['L1']['side'] == 'old' and by['L2']['snap'] == 1 and by['L2']['side'] == 'new' and by['L3']['snap'] is None and by['L3']['side'] == 'commit', by
    assert by['c2']['snap'] == 0 and by['c2']['side'] == 'old', by['c2']
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:resolved"))') == {'c5': True}
    print('legacy + seeded storage normalised; side field kept for the old page')
    # view selector: three pairs, default round 1
    opts = pg.locator('#viewsel option').all_inner_texts(); print('views:', opts)
    assert len(opts) == 3 and 'round 1' in opts[0] and 'round 2' in opts[1] and 'net' in opts[2]
    # round 1 file list: a, b, c, img (e.txt not yet)
    names = [e.get_attribute('title') for e in pg.locator('#ftree tr.file:not(.hidden)').all()]
    assert names == ['a.txt', 'b.txt', 'c.txt', 'img.png'], names
    # open a.txt: threads with agent replies, status badges, mapping links
    pg.locator('#ftree tr.file', has_text='a.txt').click(); pg.wait_for_timeout(200)
    th = pg.locator('#bottom .thread[data-root="c1"]'); assert th.count() == 1
    assert 'agent' in txt(th) and 'FIXED' in th.inner_text().upper() and 'Lowercased it.' in txt(th)
    assert 'S2 209bf0d: changed here' in txt(th), txt(th)          # line TWO was edited in S2
    th2 = pg.locator('#bottom .thread[data-root="c2"]'); assert 'S1 c362365: line 4' in txt(th2) and 'S2 209bf0d: line 4' in txt(th2), txt(th2)
    # replies in time order: the reviewer's earlier follow-up (seeded 2026-10-26) before the agent's answer (file written later)
    rt = th2.locator('.cmt.reply').all_inner_texts(); assert len(rt) == 2 and 'Also think about line five.' in rt[0] and "Added 'line four and a half'." in rt[1], rt
    assert 'replies.json' in th2.locator('.cmt.agent .when').inner_text()
    assert th2.locator('.cmt.agent a.map', has_text='a.txt:5').count() == 1
    assert th2.locator('.cmt.agent span.map', has_text='0123456 a.txt:1 (not in this review)').count() == 1, txt(th2)   # unknown commit: text, no link
    thL1 = pg.locator('#bottom .thread[data-root="L1"]'); assert thL1.count() == 1 and 'legacy old-side' in txt(thL1)
    assert 'legacy new-side' in txt(pg.locator('#bottom .thread[data-root="L2"]'))
    assert 'legacy whole-commit' in txt(pg.locator('#commit .thread[data-root="L3"]'))
    assert 'DECLINED' in pg.locator('#commit .thread[data-root="c4"]').inner_text().upper()
    pg.screenshot(path=f'{shots}/01-round1-threads.png')
    print('threads, replies, statuses and mapping links OK')
    # fixedAt link jumps into S2: view becomes S1->S2, right-side line 5 flashes
    th2.locator('.cmt.agent a.map', has_text='a.txt:5').click(); pg.wait_for_timeout(300)
    assert pg.locator('#viewsel').input_value() == '1-2'
    assert pg.locator('tr.flash td.ln.n', has_text='5').count() == 1
    assert 'line four and a half' in txt(pg.locator('tr.flash'))
    print('fixedAt jump OK')
    # round 2 file list: a (M), c->d (R), e (A); b and img unchanged so absent; e.txt carries the agent's own note
    names = [t.strip() for t in pg.locator('#ftree tr.file:not(.hidden) td.fcell').all_inner_texts()]
    assert any('a.txt' in n for n in names) and any('c.txt → d.txt' in n for n in names) and any('e.txt' in n for n in names) and not any('b.txt' in n for n in names), names
    pg.locator('#ftree tr.file', has_text='e.txt').click(); pg.wait_for_timeout(200)
    assert 'Added e.txt with a helper' in txt(pg.locator('#bottom .thread'))
    # reviewed marks are per view
    pg.keyboard.press('r'); pg.wait_for_timeout(100)
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:reviewed:1-2"))') == {'e.txt': True}
    assert pg.evaluate(f'localStorage.getItem("ptal:{key}:reviewed")') is None
    # net view via v: S0 -> S2; c2 (S0) shows on the left, agent note (S2) on the right; c1 (S1) not shown
    pg.keyboard.press('v'); pg.wait_for_timeout(200); assert pg.locator('#viewsel').input_value() == '0-2'
    pg.locator('#ftree tr.file', has_text='a.txt').click(); pg.wait_for_timeout(200)
    assert pg.locator('#bottom .thread[data-root="c2"]').count() == 1 and pg.locator('#bottom .thread[data-root="c1"]').count() == 0
    row = pg.locator('#bottom tr.cmtrow', has=pg.locator('.thread[data-root="c2"]')); assert 'This needs a line after it' in txt(row.locator('td').first) and txt(row.locator('td').last) == ''
    names = [t.strip() for t in pg.locator('#ftree tr.file:not(.hidden) td.fcell').all_inner_texts()]
    assert any('d.txt' in n for n in names) and not any('c.txt' in n for n in names) and any('b.txt' in n for n in names), names   # net: d.txt is simply added, b changed in round 1
    pg.screenshot(path=f'{shots}/02-net-view.png')
    # in the net view S1 threads are not rendered: the header says where they are, and the link leads there
    pg.locator('#ftree tr.file', has_text='a.txt').click(); pg.wait_for_timeout(200)
    assert pg.locator('#bottom .thread[data-root="c1"]').count() == 0
    ew = pg.locator('#elsewhere'); assert ew.count() == 1 and '3 open threads at S1, not shown in this view:' in txt(ew), txt(ew)   # c1, n1, L2
    assert [a.inner_text() for a in ew.locator('a').all()] == ['show S0 → S1', 'show S1 → S2'], txt(ew)
    assert pg.locator('#ftree tr.file', has_text='a.txt').locator('.badge.open').get_attribute('title') == '5 unresolved threads (2 at S0, 3 at S1)'
    ew.locator('a', has_text='show S1 → S2').click(); pg.wait_for_timeout(300)
    assert pg.locator('#viewsel').input_value() == '1-2' and pg.locator('#bottom .thread[data-root="c1"]').count() == 1
    assert '2 open threads at S0, not shown in this view:' in txt(pg.locator('#elsewhere')), txt(pg.locator('#elsewhere'))   # c2, L1 are at S0
    pg.locator('#viewsel').select_option('0-2'); pg.wait_for_timeout(200)
    print('net view OK')
    # panel: filters and resolve
    pg.keyboard.press('C'); pg.wait_for_timeout(200)
    assert pg.locator('#panel .pc[data-id]').count() == 9, pg.locator('#panel .pc[data-id]').count()    # opens on unresolved: 10 roots (9 reviewer incl. the note, 1 agent note) minus resolved c5
    assert pg.locator('#pfilter button').all_inner_texts() == ['all (10)', 'unresolved (9)', 'resolved (1)', 'notes to self (1)'], pg.locator('#pfilter button').all_inner_texts()
    pg.locator('#pfilter button[data-filter="all"]').click(); pg.wait_for_timeout(100)
    assert pg.locator('#panel .pc[data-id]').count() == 10
    assert 'Agent entries that could not be placed (2)' in txt(pg.locator('#pbody')), txt(pg.locator('#pbody'))[-300:]
    assert 're: nonexistent (no such comment in this browser)' in txt(pg.locator('#pbody')) and 'at: 0123456 z.txt:1 (commit not in this review)' in txt(pg.locator('#pbody'))
    pg.locator('#pfilter button[data-filter="resolved"]').click(); pg.wait_for_timeout(100)
    assert [e.get_attribute('data-id') for e in pg.locator('#panel .pc[data-id]').all()] == ['c5']
    pg.locator('#pfilter button[data-filter="unresolved"]').click(); pg.wait_for_timeout(100)
    assert pg.locator('#panel .pc[data-id="c1"]').count() == 1
    pg.locator('#panel .pc[data-id="c1"] input.res').click(); pg.wait_for_timeout(150)
    assert pg.locator('#panel .pc[data-id="c1"]').count() == 0      # filtered out once resolved
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:resolved"))') == {'c5': True, 'c1': True}
    arow = pg.locator('#ftree tr.file', has_text='a.txt')      # a.txt: c1 resolved (grey), c2 + n1 + L1 + L2 open (red); b.txt: c5 resolved only
    assert [b.inner_text() for b in arow.locator('.badge').all()] == ['4', '1'] and arow.locator('.badge.open').count() == 1 and arow.locator('.badge.done').get_attribute('title') == '1 resolved (1 at S1)'
    brow = pg.locator('#ftree tr.file', has_text='b.txt'); assert brow.locator('.badge.open').count() == 0 and brow.locator('.badge.done').inner_text() == '1'
    pg.locator('#viewsel').select_option('1-2'); pg.wait_for_timeout(200)
    erow = pg.locator('#ftree tr.file', has_text='e.txt')      # only the agent's own note: unresolved until ticked, like any thread
    assert [(b.get_attribute('class'), b.inner_text()) for b in erow.locator('.badge').all()] == [('badge open', '1')], erow.inner_text()
    pg.keyboard.press('Escape'); erow.click(); pg.wait_for_timeout(200)                  # panel closed for the click
    ath = pg.locator('#bottom .thread[data-root="agent-replies.json-6"]'); assert ath.count() == 1 and 'helper' in txt(ath), ath.count()
    ath.locator('input.res').click(); pg.wait_for_timeout(200)
    assert [(b.get_attribute('class'), b.inner_text()) for b in erow.locator('.badge').all()] == [('badge done', '1')]
    assert pg.locator('#bottom .thread.collapsed[data-root="agent-replies.json-6"]').count() == 1
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:resolved"))')['agent-replies.json-6'] is True
    pg.locator('#viewsel').select_option('0-1'); pg.keyboard.press('C'); pg.wait_for_timeout(200)
    pg.locator('#pfilter button[data-filter="all"]').click(); pg.wait_for_timeout(100)
    pg.screenshot(path=f'{shots}/03-panel.png')
    # brief: unresolved reviewer comments only, with ids, snapshot hashes, context and existing replies
    brief = pg.evaluate('''() => { let t=''; navigator.clipboard.writeText = s => { t=s; return Promise.resolve(); }; document.getElementById('btn-brief').click(); return t; }''')
    assert 'git rev-parse HEAD` must print `' + data['snaps'][2]['sha'] in brief and '--then ' + data['snaps'][2]['sha'] + ' --replies replies.json --then <your commit> --replies replies-c362365-3.json' in brief and 'Write `replies-c362365-3.json`' in brief and 'amend' in brief
    blocks = re.findall(r'```json\n(.*?)\n```', brief, re.S); assert len(blocks) == 2, len(blocks)
    schema, cj = json.loads(blocks[0]), json.loads(blocks[1])
    assert schema == data['replySchema'] and schema['$defs']['reply']['properties']['status']['enum'] == ['fixed', 'answered', 'declined', 'deferred']
    cs = {c['id']: c for c in cj['comments']}
    assert list(cs) == ['L3', 'c4', 'L1', 'c2', 'L2', 'c3'], list(cs)               # unresolved reviewer roots only (c1 resolved above, c5 seeded resolved), in panel order
    assert cs['c2'] == {'id': 'c2', 'commit': data['snaps'][0]['sha'], 'path': 'a.txt', 'line': 4, 'context': {'1': 'line one', '2': 'line two', '3': 'line three', '4': 'line four', '5': 'line five'},
                        'text': 'This needs a line after it.', 'replies': [{'author': 'reviewer', 'text': 'Also think about line five.'}, {'author': 'agent', 'status': 'fixed', 'text': "Added 'line four and a half'."}]}, cs['c2']
    assert cs['c3']['context'] == {'1': 'new file'}, cs['c3']
    assert cs['c4']['commit'] == data['snaps'][1]['sha'] and cs['c4']['messageLine'] == 1 and cs['c4']['context'] == {'1': 'review me', '2': '', '3': 'Body of the reviewed commit.'} and 'path' not in cs['c4'], cs['c4']
    assert cs['c3']['path'] == 'c.txt' and cs['c3']['commit'] == data['snaps'][1]['sha']
    assert 'n1' not in brief and 'remind me' not in brief                            # notes to self never reach the agent
    print('brief OK:', len(brief), 'chars')
    # notes to self: top-pane alert, panel filter and label, creation with N, resolving clears the alert
    assert '1 open note to self' in txt(pg.locator('#notes')) and 'remind me: check line three' in txt(pg.locator('#notes')) and 'second line' not in txt(pg.locator('#notes')), txt(pg.locator('#notes'))
    pg.locator('#pfilter button[data-filter="notes"]').click(); pg.wait_for_timeout(100)      # the panel is still open from above
    assert [e.get_attribute('data-id') for e in pg.locator('#panel .pc[data-id]').all()] == ['n1'] and pg.locator('#panel .pc.note .who.note', has_text='note to self').count() == 1
    pg.locator('#pfilter button[data-filter="all"]').click(); pg.keyboard.press('Escape'); pg.wait_for_timeout(100)
    pg.locator('#viewsel').select_option('0-1'); pg.locator('#ftree tr.file', has_text='a.txt').click(); pg.wait_for_timeout(200)   # the note is on S1, absent from the net view
    assert pg.locator('#bottom .thread[data-root="n1"] .cmt.note .who', has_text='note to self').count() == 1
    pg.keyboard.press('n'); pg.wait_for_timeout(100); pg.keyboard.press('N'); pg.wait_for_timeout(100)
    assert pg.locator('.editor input.note').is_checked()
    pg.keyboard.type('typed note'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
    recs = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))')
    typed = [c for c in recs if c['text'] == 'typed note']; assert len(typed) == 1 and typed[0]['kind'] == 'note' and typed[0]['snap'] == 1, typed
    assert '2 open notes to self' in txt(pg.locator('#notes'))
    pg.locator('#notes a').first.click(); pg.wait_for_timeout(300)                       # the alert's link jumps to the note's thread
    assert pg.locator('tr.flash .thread[data-root="n1"], tr.flash + tr .thread[data-root="n1"]').count() == 1 or pg.locator('#bottom .thread[data-root="n1"]').count() == 1
    pg.locator('#bottom .thread[data-root="n1"] input.res').click(); pg.wait_for_timeout(200)
    assert '1 open note to self' in txt(pg.locator('#notes')) and 'typed note' in txt(pg.locator('#notes'))
    # a resolved thread collapses to one line; click expands it, "collapse" folds it again, unticking reopens it
    col = pg.locator('#bottom .thread.collapsed[data-root="n1"]'); assert col.count() == 1 and 'note to self: remind me: check line three' in txt(col) and 'second line' not in txt(col), txt(col)
    col.locator('.sum').click(); pg.wait_for_timeout(100)
    full = pg.locator('#bottom .thread[data-root="n1"]:not(.collapsed)'); assert full.count() == 1 and 'second line of the note' in txt(full)
    full.locator('.tfoot a', has_text='collapse').click(); pg.wait_for_timeout(100)
    assert pg.locator('#bottom .thread.collapsed[data-root="n1"]').count() == 1
    pg.locator('#bottom .thread.collapsed[data-root="n1"] input.res').click(); pg.wait_for_timeout(200)
    assert pg.locator('#bottom .thread.collapsed[data-root="n1"]').count() == 0 and '2 open notes to self' in txt(pg.locator('#notes'))
    pg.locator('#bottom .thread[data-root="n1"] input.res').click(); pg.wait_for_timeout(200)       # resolve again for the checks below
    assert pg.locator('#bottom .thread.collapsed[data-root="c1"]').count() == 1 and '1 reply' in txt(pg.locator('#bottom .thread.collapsed[data-root="c1"]'))
    print('collapsed resolved threads OK')
    pg.screenshot(path=f'{shots}/03b-notes.png')
    print('notes to self OK')
    pg.keyboard.press('C'); pg.wait_for_timeout(200)                                        # the panel back open for the steps below
    # reply to a thread from the page, then jump from the panel to it
    pg.locator('#panel .pc[data-id="c3"] .loc a').first.click(); pg.wait_for_timeout(300)
    assert pg.locator('#panel.open').count() == 0 and pg.locator('#viewsel').input_value() in ('0-1', '1-2')
    th3 = pg.locator('#bottom .thread[data-root="c3"]'); assert th3.count() == 1
    th3.locator('.tfoot a', has_text='reply').click(); pg.keyboard.type('Thanks, confirmed.'); pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(200)
    th3 = pg.locator('#bottom .thread[data-root="c3"]'); assert th3.locator('.cmt.reply').count() == 2 and 'Thanks, confirmed.' in txt(th3)
    stored = pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:comments"))')
    rep = [c for c in stored if c.get('re') == 'c3']; assert len(rep) == 1 and rep[0]['side'] == 'new' and rep[0]['snap'] == 1, rep
    print('reply thread OK')
    assert pg.evaluate(f'JSON.parse(localStorage.getItem("ptal:{key}:resolved"))') == {'c5': True, 'c1': True, 'agent-replies.json-6': True, 'n1': True}
    b.close()
print('console errors/warnings:', errors)
assert not errors
print('ALL OK')
