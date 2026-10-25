# Please Take a Look: a tool for making a per-commit single-file .html code-review page.

## **CAVEAT**

This was almost entirely written using LLMs. I (a human) have reviewed
it for non-maliciousness, and it works well, but the code/prose is meh.

## Overview
`ptal.py` turns one git commit into one self-contained HTML file for reviewing
The name is the reviewer's "please take a look": this makes the page you look at.
in a desktop browser on a portrait screen. Python 3 standard library and git
only; no network access from the page.

    ./ptal.py GIT_ROOT COMMITTISH [-o OUT.html]

`GIT_ROOT` is anything `git -C` accepts. The default output is
`review-<short sha>.html` in the current directory; `-o -` writes to stdout.
A merge commit is compared with its first parent, a root commit with the
empty tree. Renames and copies are detected (`-M -C`). Image files (png,
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

## Storage

Everything lives in the browser's `localStorage`, under keys
`ptal:<short hash>:comments`, `ptal:<short hash>:reviewed` and
`ptal:<short hash>:ui`, where the short hash
is the commit's abbreviated hash as `git show --format=%h` prints it. Files
opened from `file://` URLs all share one origin, so the prefix keeps reviews
of different commits apart, while a page regenerated for the same commit
(after a change to `ptal.py`, say) keeps its comments and UI state.

## Testing

`test_page.py` drives a generated page in headless Chromium (keys, comments,
persistence across reload, the panel, folds, wrap, the directory tree, the
stat column, images, binary/added/deleted files) and asserts no console
errors. `example-AlarmingNotifications-d54b8b9.html` is a generated page. It needs Playwright, which is not a
dependency of `ptal.py` itself:

    uv venv pw && uv pip install --python pw/bin/python playwright
    PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers pw/bin/python -m playwright install --with-deps chromium
    ./ptal.py /work/AlarmingNotifications HEAD -o /tmp/r.html
    PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers pw/bin/python test_page.py /tmp/r.html /tmp/shots
