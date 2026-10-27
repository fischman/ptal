# Please Take a Look: a tool for making a per-commit single-file .html code-review page.

## **CAVEAT**

This was almost entirely written using LLMs. I (a human) have reviewed
it for non-maliciousness, and it works well, but the code/prose is meh.

## Overview
`ptal.py` turns one git commit into one self-contained HTML file for reviewing
The name is the reviewer's "please take a look": this makes the page you look at.
in a desktop browser on a portrait screen. Python 3 standard library and git
only; no network access from the page.

    ./ptal.py GIT_ROOT COMMITTISH [--then COMMITTISH]... [--replies FILE]... [--comments FILE] [-o OUT.html]

`GIT_ROOT` is anything `git -C` accepts. The default output is
`review-<short sha>.html` in the current directory; `-o -` writes to stdout.
A merge commit is compared with its first parent, a root commit with the
empty tree. Each `--then` adds a follow-up commit as another snapshot (see
"Rounds" below). Renames and copies are detected (`-M -C`). Image files (png,
jpeg, gif, webp, svg, bmp, ico, avif) are embedded as data URIs, up to 10 MB
a side. Android vector drawables (`<vector>` XML: paths, groups with
transforms, clip paths, gradients, tint) are converted to SVG and embedded
the same way.

## The page

- **Top pane**: commit hash, author/committer, parents, refs, the message, and
  the changed files as a directory tree in byte order of path, as git lists
  them (single-child directory chains shown as one `a/b/c/` row), each file with status, a comment-count badge and +/−
  counts in a column just right of the widest name, and a "reviewed"
  checkbox. Click a directory to collapse or expand it; click a file to open
  it. Navigating into a file inside a collapsed directory expands it. The
  "all files / unreviewed only" button (or `u`) hides reviewed files, and
  `r` toggles the current file's checkbox. A progress line above the tree
  shows the percentage of files reviewed and the percentage of diff lines
  (added plus deleted, text files only, so large files weigh more) that sit
  in reviewed files; hover for the raw counts. Drag the bar below the pane to resize it;
  double-click it (or press `t`) to hide/show it.
- **Bottom pane**: before (left) and after (right) of the selected file, row
  aligned, with changed words emphasised. Image files show the before and
  after images side by side; SVGs and vector drawables get the text diff
  below the images, and light-coloured drawables sit on a dark plate. Long unchanged stretches fold into a
  `⋯ N unchanged lines ⋯` row; click it or press `x` to expand. `w` toggles
  wrapping (unwrapped lines scroll horizontally).
- **Keys**: `n` / `p` next / previous change, continuing into the next /
  previous file at the ends; `]` / `[` next / previous file; `c` comment on
  the current change; `C` all comments; `r` reviewed on/off; `u` unreviewed
  files only (navigation then skips reviewed files); `?` key help; `Esc`
  closes things.
  Changes closer than 4 unchanged lines count as one stop.
- **Comments**: click a line number on either side, or the gutter of a
  commit-message line, or "+ comment on the commit". `Ctrl+Enter` saves.
  Comments show inline under their line (on their side) and can be edited or
  deleted there. The **Comments** button (or `C`) lists them all, in file and
  line order, with jump links, per-comment delete, delete-all, "Copy as
  Markdown" and "Download JSON".

## Example output

A couple of example review pages, from the [AlarmingNotifications](https://github.com/fischman/AlarmingNotifications)
repo: [1be143d](https://htmlpreview.github.io/?https://github.com/fischman/ptal/blob/main/examples/review-1be143d.html), [eda9419](https://htmlpreview.github.io/?https://github.com/fischman/ptal/blob/main/examples/review-eda9419.html).

<details>
<summary>Serving details</summary>
These are served through https://htmlpreview.github.io/ just to get
around GitHub's serving restrictions; in practice it is expected that
review .html are used via local `file://` URLs.
</details>

## Rounds: getting comments addressed

The review is a sequence of snapshots: S0 is the parent, S1 the reviewed
commit, and every `--then COMMIT` adds S2, S3, ... Every file that changed
in any step is embedded at every snapshot, so the page can show the diff
between any two of them: the selector above the file list (or `v`) offers
each round, the net change, and every other pair. A comment belongs to the
snapshot, file and line it was made on, so it never goes stale; it shows in
every view that includes its snapshot, with "where did this line go" links
to each later snapshot (same line, moved line, or "changed here").

The loop:

1. Review S1, leave comments, open the comments panel and press **Copy
   brief for agent**. The brief is Markdown: the snapshots, instructions,
   the JSON schema of the reply file, and every unresolved comment as JSON
   (id, the full hash of the commit it was made against, `path` and `line`
   or `messageLine`, a few lines of context keyed by line number, the text,
   and earlier replies in the thread).
2. Give the brief to a coding agent. It checks that the current branch is
   at the newest snapshot, makes one or more new commits on it (never
   amending or rebasing) and writes `replies.json`:

       {"replies": [
         {"re": "<comment id>", "status": "fixed", "text": "what changed",
          "fixedAt": [{"commit": "<full hash>", "file": "path", "line": 12}]},
         {"re": "<comment id>", "status": "answered" | "declined" | "deferred", "text": "..."},
         {"at": {"commit": "<full hash>", "file": "path", "line": 7}, "text": "an unprompted note"}
       ]}

   Every location names a commit, so its file and line are unambiguous;
   `ptal.py` checks the file against the schema (`REPLY_SCHEMA`) and
   resolves the commits to full hashes. A location in a commit that is not
   one of the snapshots renders as text without a link; a reply to an
   unknown id, or a note in such a commit, is listed separately in the
   panel. The agent's own notes are threads like yours: they count as
   unresolved until you tick them. Reply ids are `agent-<file name>-<n>`,
   so keep each round's file name once resolved marks refer to it.
3. Regenerate with `--then <new commit> --replies replies.json`. Replies
   appear threaded under the comments they answer, with a status badge and
   jump links into the follow-up commit. Tick **resolved** on each comment
   you accept; the panel filters all / unresolved / resolved, and the next
   brief contains only unresolved threads, with their history. Reply to a
   thread from the page to carry a rebuttal into the next round.
4. Repeat with more `--then` commits and reply files as needed. Replies
   are embedded in the page, not stored by the browser, so every
   regeneration takes every reply file so far: `--then A --replies a.json
   --then B --replies b.json`. The brief's command already lists them.

A **note to self** is a comment the agent never sees: tick the box in
the comment editor, or press `N` instead of `c`. Notes are left out of the
brief, listed under their own heading in the Markdown copy, and shown in a
box at the top of the commit pane until resolved, so they survive any number
of regenerations with further `--then` and `--replies`. The panel has a
"notes to self" filter. Editing a note and unticking the box turns it into
an ordinary comment for the next brief.

`--comments FILE` seeds the page with a comments export (the panel's
**Download JSON**) for use in another browser; records already present are
kept. Reviewed marks are per view, so files touched in a round come back
unreviewed for that round.

## Storage

Everything lives in the browser's `localStorage`, under keys
`ptal:<short hash>:comments`, `ptal:<short hash>:resolved`,
`ptal:<short hash>:reviewed` (the S0 to S1 view; other views use
`reviewed:<a>-<b>`) and `ptal:<short hash>:ui`, where the short hash
is the commit's abbreviated hash as `git show --format=%h` prints it. Files
opened from `file://` URLs all share one origin, so the prefix keeps reviews
of different commits apart, while a page regenerated for the same commit
(after a change to `ptal.py`, say) keeps its comments and UI state.

## Testing

`test_rounds.py` drives a three-snapshot page made with `--then`,
`--replies` and `--comments` (seeded and legacy-format comments, threaded
replies, line mapping, view switching, resolve marks, the brief).
`test_page.py` drives a generated page in headless Chromium (keys, comments,
persistence across reload, the panel, folds, wrap, the directory tree, the
stat column, images, binary/added/deleted files) and asserts no console
errors. `example-AlarmingNotifications-d54b8b9.html` is a generated page. It needs Playwright, which is not a
dependency of `ptal.py` itself:

    uv venv pw && uv pip install --python pw/bin/python playwright
    PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers pw/bin/python -m playwright install --with-deps chromium
    ./ptal.py /work/AlarmingNotifications HEAD -o /tmp/r.html
    PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers pw/bin/python test_page.py /tmp/r.html /tmp/shots
