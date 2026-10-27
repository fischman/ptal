#!/usr/bin/env python3
"""ptal: a single-file, portrait-oriented code review page for one git commit.

    ptal.py GIT_ROOT COMMITTISH [--then COMMITTISH]... [--replies FILE]... [--comments FILE] [-o OUT.html]

Reads the commit with git (GIT_ROOT is anything `git -C` accepts) and writes
one self-contained HTML file: commit details and the list of changed files on
top, a side-by-side before/after view of the selected file below.

The review is a sequence of snapshots: S0 is the commit's parent, S1 the
commit, and each --then adds a follow-up commit S2, S3, ...  Every file that
changed in any step is embedded at every snapshot, so the page can show the
diff between any two of them: each round, and the net change.  Comments
anchor to a snapshot, file and line, so they never go stale.

--replies FILE embeds a coding agent's answers to the reviewer's comments:
a JSON file validated against REPLY_SCHEMA below (the "Copy brief for agent"
button in the page produces the comments as JSON, the instructions, and the
schema).  Replies show up threaded under the comments they answer, with a
status and pointers (commit, file, line) into the follow-up commit.
--comments FILE seeds the page with a comments export, for use in another
browser.

Comments, reviewed marks and resolved marks live in the browser's
localStorage under a key prefix made from the commit's abbreviated hash, so
pages opened from file:// URLs (which all share one origin) do not see each
other's data, while a page regenerated for the same commit, with or without
follow-ups, keeps it.  Image files (png, jpeg, gif, webp, svg, bmp, ico,
avif) are embedded and shown before/after; Android vector drawables (XML)
are converted to SVG for the same purpose.

A merge commit is compared with its first parent; a root commit with the
empty tree.  Python 3 standard library and git only.
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon",
               ".avif": "image/avif"}
IMAGE_MAX = 10 << 20   # per side; larger images are reported by size only
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def git(root, *args, input=None):
    p = subprocess.run(["git", "-C", root, *args], input=input, capture_output=True)
    if p.returncode != 0:
        sys.exit("git %s: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
    return p.stdout


def commit_info(root, committish):
    sha = git(root, "rev-parse", "--verify", "--end-of-options", committish + "^{commit}").decode().strip()
    fields = ["%H", "%h", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%D", "%s", "%b"]
    out = git(root, "show", "-s", "--format=" + "%x00".join(fields), sha).decode("utf-8", "replace")
    v = out.split("\0", len(fields) - 1)
    parents = v[2].split()
    return {
        "sha": v[0], "short": v[1], "parents": parents,
        "author": v[3], "authorEmail": v[4], "authorDate": v[5],
        "committer": v[6], "committerEmail": v[7], "commitDate": v[8],
        "refs": v[9], "subject": v[10], "body": v[11].rstrip("\n"),
        "base": parents[0] if parents else EMPTY_TREE,
        "root": os.path.abspath(root),
    }


def raw_changes(root, base, sha):
    """Parse `git diff --raw -z`: one dict per changed path."""
    out = git(root, "diff", "--raw", "-z", "--no-abbrev", "--no-ext-diff", "--no-color",
              "-M", "-C", base, sha)
    parts = out.split(b"\0")
    files, i = [], 0
    while i < len(parts) and parts[i]:
        head = parts[i].decode("utf-8", "replace")
        assert head.startswith(":"), head
        old_mode, new_mode, old_sha, new_sha, status = head[1:].split(" ", 4)
        f = {"status": status[0], "score": status[1:] or None,
             "oldMode": old_mode, "newMode": new_mode, "oldSha": old_sha, "newSha": new_sha}
        if status[0] in "RC":
            f["oldPath"] = parts[i + 1].decode("utf-8", "replace")
            f["path"] = parts[i + 2].decode("utf-8", "replace")
            i += 3
        else:
            f["path"] = parts[i + 1].decode("utf-8", "replace")
            f["oldPath"] = f["path"]
            i += 2
        files.append(f)
    return files


def tree_of(root, sha):
    """{path: (mode, blob sha)} for every file in a tree-ish."""
    if sha == EMPTY_TREE:
        return {}
    out = git(root, "ls-tree", "-r", "-z", sha)
    tree = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, _type, blob = meta.decode().split()
        tree[path.decode("utf-8", "replace")] = (mode, blob)
    return tree


def fetch_blobs(root, shas):
    """Return {sha: bytes} for every blob in shas via one `git cat-file --batch`."""
    shas = sorted(s for s in set(shas) if s and s != ZERO_SHA)
    if not shas:
        return {}
    out = git(root, "cat-file", "--batch", input=("\n".join(shas) + "\n").encode())
    blobs, pos = {}, 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode().split()
        if len(header) == 2 and header[1] == "missing":
            sys.exit("object %s missing" % header[0])
        sha, _type, size = header[0], header[1], int(header[2])
        blobs[sha] = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


# --- Android VectorDrawable -> SVG -------------------------------------------------

ANDROID = "{http://schemas.android.com/apk/res/android}"
AAPT = "{http://schemas.android.com/aapt}"
ANDROID_COLORS = {"white": "#ffffffff", "black": "#ff000000", "transparent": "#00000000",
                  "darker_gray": "#ffaaaaaa", "holo_red_light": "#ffff4444", "holo_blue_light": "#ff33b5e5"}


def android_color(value):
    """(#rrggbb, alpha 0..1) for an Android colour literal; a resource reference
    that cannot be resolved renders as mid grey.  None for none/transparent."""
    if value is None:
        return None
    v = value.strip()
    if v.startswith("@android:color/"):
        v = ANDROID_COLORS.get(v.split("/", 1)[1], "#ff808080")
    elif v.startswith("@") or v.startswith("?"):
        v = "#ff808080"
    if not v.startswith("#"):
        return None
    h = v[1:]
    if len(h) == 3:
        h = "ff" + "".join(c * 2 for c in h)
    elif len(h) == 4:
        h = "".join(c * 2 for c in h)
    elif len(h) == 6:
        h = "ff" + h
    if len(h) != 8:
        return None
    try:
        a = int(h[:2], 16) / 255.0
    except ValueError:
        return None
    return ("#" + h[2:].lower(), a) if a > 0 else None


def luminance(rgb):
    r, g, b = (int(rgb[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def num(value, default=0.0):
    if value is None:
        return default
    v = value.strip()
    for suffix in ("dp", "dip", "px", "sp", "pt", "mm", "in"):
        if v.endswith(suffix):
            v = v[:-len(suffix)]
            break
    try:
        return float(v)
    except ValueError:
        return default


def fmt(x):
    return ("%.4f" % x).rstrip("0").rstrip(".") or "0"


def vector_to_svg(data):
    """Render an Android <vector> drawable as SVG.  Returns (svg bytes, info)
    or None if data is not a vector drawable.  info = {"dp": "WxH",
    "light": bool (fills are mostly light, so show on a dark plate)}."""
    if b"<vector" not in data[:2048]:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag != "vector":
        return None
    a = lambda e, n, d=None: e.get(ANDROID + n, d)
    vw, vh = num(a(root, "viewportWidth"), 0), num(a(root, "viewportHeight"), 0)
    w, h = num(a(root, "width"), vw), num(a(root, "height"), vh)
    if vw <= 0 or vh <= 0:
        return None
    scale = max(1.0, min(160.0 / max(w, h), 8.0))   # icons are tiny: show them enlarged
    out, defs, lums = [], [], []
    counter = [0]

    def gid(prefix):
        counter[0] += 1
        return "%s%d" % (prefix, counter[0])

    def paint(el, attr):
        """SVG paint for android:fillColor/strokeColor, which may be a literal or an
        <aapt:attr> child holding a <gradient>."""
        for child in el:
            if child.tag == AAPT + "attr" and child.get("name") == "android:" + attr:
                g = child.find("gradient")
                if g is not None:
                    return gradient(g), 1.0
        c = android_color(a(el, attr))
        if c is None:
            return "none", 1.0
        lums.append(luminance(c[0]))
        return c[0], c[1]

    def gradient(g):
        kind = a(g, "type", "linear")
        items = [(num(a(i, "offset"), 0), android_color(a(i, "color"))) for i in g.findall("item")]
        if not items:
            items = [(0, android_color(a(g, "startColor"))), (1, android_color(a(g, "endColor")))]
            if a(g, "centerColor"):
                items.insert(1, (0.5, android_color(a(g, "centerColor"))))
        stops = "".join('<stop offset="%s" stop-color="%s" stop-opacity="%s"/>' % (fmt(o), c[0] if c else "#000", fmt(c[1] if c else 0))
                        for o, c in items)
        for _, c in items:
            if c:
                lums.append(luminance(c[0]))
        i = gid("g")
        if kind == "radial":
            defs.append('<radialGradient id="%s" gradientUnits="userSpaceOnUse" cx="%s" cy="%s" r="%s">%s</radialGradient>'
                        % (i, fmt(num(a(g, "centerX"))), fmt(num(a(g, "centerY"))), fmt(num(a(g, "gradientRadius"), 1)), stops))
        else:   # linear; sweep gradients are approximated by a linear one
            defs.append('<linearGradient id="%s" gradientUnits="userSpaceOnUse" x1="%s" y1="%s" x2="%s" y2="%s">%s</linearGradient>'
                        % (i, fmt(num(a(g, "startX"))), fmt(num(a(g, "startY"))), fmt(num(a(g, "endX"))), fmt(num(a(g, "endY"))), stops))
        return "url(#%s)" % i

    def path(el):
        d = a(el, "pathData")
        if not d:
            return ""
        fill, fa = paint(el, "fillColor")
        stroke, sa = paint(el, "strokeColor")
        attrs = ['d="%s"' % esc(d), 'fill="%s"' % fill]
        fa *= num(a(el, "fillAlpha"), 1)
        if fa < 1:
            attrs.append('fill-opacity="%s"' % fmt(fa))
        if a(el, "fillType", "nonZero") == "evenOdd":
            attrs.append('fill-rule="evenodd"')
        if stroke != "none":
            sa *= num(a(el, "strokeAlpha"), 1)
            attrs.append('stroke="%s" stroke-width="%s"' % (stroke, fmt(num(a(el, "strokeWidth"), 0))))
            if sa < 1:
                attrs.append('stroke-opacity="%s"' % fmt(sa))
            attrs.append('stroke-linecap="%s" stroke-linejoin="%s" stroke-miterlimit="%s"'
                         % (a(el, "strokeLineCap", "butt"), a(el, "strokeLineJoin", "miter"), fmt(num(a(el, "strokeMiterLimit"), 4))))
        return "<path %s/>" % " ".join(attrs)

    def group(el):
        px, py = num(a(el, "pivotX")), num(a(el, "pivotY"))
        sx, sy = num(a(el, "scaleX"), 1), num(a(el, "scaleY"), 1)
        rot, tx, ty = num(a(el, "rotation")), num(a(el, "translateX")), num(a(el, "translateY"))
        t = []
        if tx or ty or px or py:
            t.append("translate(%s %s)" % (fmt(tx + px), fmt(ty + py)))
        if rot:
            t.append("rotate(%s)" % fmt(rot))
        if sx != 1 or sy != 1:
            t.append("scale(%s %s)" % (fmt(sx), fmt(sy)))
        if px or py:
            t.append("translate(%s %s)" % (fmt(-px), fmt(-py)))
        body = children(el)
        return "<g%s>%s</g>" % (' transform="%s"' % " ".join(t) if t else "", body)

    def children(el):
        """Render child elements; a <clip-path> clips everything after it."""
        parts, open_clips = [], 0
        for child in el:
            if child.tag == "clip-path":
                i = gid("c")
                defs.append('<clipPath id="%s"><path d="%s"/></clipPath>' % (i, esc(a(child, "pathData", ""))))
                parts.append('<g clip-path="url(#%s)">' % i)
                open_clips += 1
            elif child.tag == "group":
                parts.append(group(child))
            elif child.tag == "path":
                parts.append(path(child))
        return "".join(parts) + "</g>" * open_clips

    body = children(root)
    tint = android_color(a(root, "tint"))
    if tint:   # a tinted drawable is drawn in the tint colour wherever it is opaque
        i = gid("t")
        defs.append('<filter id="%s" color-interpolation-filters="sRGB"><feFlood flood-color="%s"/><feComposite in2="SourceAlpha" operator="in"/></filter>' % (i, tint[0]))
        body = '<g filter="url(#%s)">%s</g>' % (i, body)
        lums = [luminance(tint[0])]
    alpha = num(a(root, "alpha"), 1)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" width="%s" height="%s"%s>%s%s</svg>'
           % (fmt(vw), fmt(vh), fmt(w * scale), fmt(h * scale), ' opacity="%s"' % fmt(alpha) if alpha < 1 else "",
              "<defs>%s</defs>" % "".join(defs) if defs else "", body))
    light = bool(lums) and sum(lums) / len(lums) > 0.6
    return svg.encode("utf-8"), {"dp": "%s×%sdp" % (fmt(w), fmt(h)), "light": light}


def esc(text):
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def data_uri(mime, data):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def is_binary(data):
    return b"\0" in data[:8192]


def lines_of(data):
    ls = data.split(b"\n")
    if ls and ls[-1] == b"":
        ls.pop()
    return [l.decode("utf-8", "replace") for l in ls]


def hunks_between(root, old_sha, new_sha):
    """[ {os, oc, ns, nc} ] from `git diff -U0 blob blob` (1-based starts; for an
    empty range the start names the line before the insertion point, as in
    unified diff headers)."""
    out = git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", old_sha, new_sha)
    hunks = []
    for line in out.decode("utf-8", "replace").splitlines():
        m = HUNK_RE.match(line)
        if m:
            hunks.append({"os": int(m.group(1)), "oc": int(m.group(2) if m.group(2) is not None else 1),
                          "ns": int(m.group(3)), "nc": int(m.group(4) if m.group(4) is not None else 1)})
    return hunks


def whole_file_hunk(old_lines, new_lines):
    if not old_lines and not new_lines:
        return []
    return [{"os": 1 if old_lines else 0, "oc": len(old_lines), "ns": 1 if new_lines else 0, "nc": len(new_lines)}]


def assemble_files(root, snaps, trees):
    """One entry per file across the snapshots: paths[k] is the file's path at
    snapshot k (None when absent), following renames; status[k] is git's status
    letter for the step k -> k+1 when the file changed in it."""
    M = len(snaps)
    files = []

    def new_file():
        f = {"paths": [None] * M, "status": {}, "copyFrom": {}}
        files.append(f)
        return f

    for i in range(M - 1):
        for ch in raw_changes(root, snaps[i]["sha"], snaps[i + 1]["sha"]):
            st = ch["status"]
            if st in "AC":
                f = None
                if st == "A":   # re-added after an earlier deletion: the same file
                    f = next((g for g in files if g["paths"][i] is None and ch["path"] in g["paths"][:i]), None)
                if f is None:
                    f = new_file()
                if st == "C":
                    f["copyFrom"][i] = ch["oldPath"]
            else:
                f = next((g for g in files if g["paths"][i] == ch["oldPath"]), None)
                if f is None:
                    f = new_file()
                    for k in range(i + 1):   # untouched by earlier steps: the same path wherever it exists
                        if ch["oldPath"] in trees[k]:
                            f["paths"][k] = ch["oldPath"]
            if st != "D":
                f["paths"][i + 1] = ch["path"]
            f["status"][i] = st
    for f in files:   # unchanged in a step: carry the path forward
        for j in range(1, M):
            if f["paths"][j] is None and f["paths"][j - 1] is not None and (j - 1) not in f["status"] \
                    and f["paths"][j - 1] in trees[j]:
                f["paths"][j] = f["paths"][j - 1]
    for f in files:
        f["modes"] = [trees[k][p][0] if p else None for k, p in enumerate(f["paths"])]
        f["shas"] = [trees[k][p][1] if p else None for k, p in enumerate(f["paths"])]
    return files


def key_of(f):
    """Stable identity of a file across snapshots: its path at S1, else at S0,
    else the first path it has (the same key the single-commit pages used)."""
    p = f["paths"]
    return p[1] if len(p) > 1 and p[1] else p[0] if p[0] else next(x for x in p if x)


def build(root, committish, thens, replies, seed):
    info = commit_info(root, committish)
    follow = [commit_info(root, t) for t in thens]
    resolve_locations(root, replies)
    base_short = git(root, "rev-parse", "--short", info["base"]).decode().strip() if info["parents"] else "empty"
    snaps = [{"sha": info["base"], "short": base_short, "label": "base", "subject": None, "round": 0},
             {"sha": info["sha"], "short": info["short"], "label": info["short"], "subject": info["subject"], "round": 1}]
    for k, c in enumerate(follow):
        snaps.append({"sha": c["sha"], "short": c["short"], "label": c["short"], "subject": c["subject"], "round": k + 2,
                      "author": c["author"], "authorDate": c["authorDate"], "body": c["body"]})
    trees = [tree_of(root, s["sha"]) for s in snaps]
    files = assemble_files(root, snaps, trees)

    # blobs: one record per distinct content; submodule entries get a synthetic one
    wanted = {sha for f in files for sha, mode in zip(f["shas"], f["modes"]) if sha and mode != "160000"}
    raw = fetch_blobs(root, wanted)
    for f in files:
        for sha, mode in zip(f["shas"], f["modes"]):
            if sha and mode == "160000":
                raw[sha] = ("Subproject commit %s\n" % sha).encode()
    blobs = {}
    for sha, data in raw.items():
        b = {"size": len(data), "binary": is_binary(data)}
        b["lines"] = None if b["binary"] else lines_of(data)
        blobs[sha] = b

    hunks = {}
    for f in files:
        f["key"] = key_of(f)
        paths = [p for p in f["paths"] if p]
        mime = next((IMAGE_TYPES[os.path.splitext(p)[1].lower()] for p in paths if os.path.splitext(p)[1].lower() in IMAGE_TYPES), None)
        vinfo = None
        for sha, mode in zip(f["shas"], f["modes"]):
            if not sha or mode == "160000" or "img" in blobs[sha]:
                continue
            data = raw[sha]
            if mime:
                blobs[sha]["img"] = data_uri(mime, data) if len(data) <= IMAGE_MAX else None
            elif any(p.endswith(".xml") for p in paths):
                v = vector_to_svg(data)
                if v:
                    blobs[sha]["img"] = data_uri("image/svg+xml", v[0])
                    vinfo = v[1]
        if mime:
            f["image"] = mime
        elif vinfo:
            f["image"] = "image/svg+xml"
            f["imageNote"] = "vector drawable " + vinfo["dp"]
            f["plate"] = "dark" if vinfo["light"] else "light"
        # hunks for every pair of snapshots whose contents differ (text only)
        M = len(snaps)
        for i in range(M):
            for j in range(i + 1, M):
                a, b = f["shas"][i], f["shas"][j]
                if a == b:
                    continue
                key = "%s:%s" % (a or "", b or "")
                if key in hunks:
                    continue
                la = blobs[a]["lines"] if a else []
                lb = blobs[b]["lines"] if b else []
                if la is None or lb is None:
                    continue   # binary: no line diff
                if not a or not b or f["modes"][i] == "160000" or f["modes"][j] == "160000":
                    hunks[key] = whole_file_hunk(la, lb)
                else:
                    hunks[key] = hunks_between(root, a, b)
    files.sort(key=lambda f: f["key"].encode("utf-8"))   # byte order, as git lists paths
    return {"commit": info, "snaps": snaps, "files": files, "blobs": blobs, "hunks": hunks,
            "replies": replies, "seed": seed, "replySchema": REPLY_SCHEMA,
            "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}


LOCATION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["commit", "file", "line"],
    "properties": {
        "commit": {"type": "string", "description": "Full hash of the commit the file and line refer to (normally your new commit)."},
        "file": {"type": "string", "description": "Path of the file in the repository at that commit."},
        "line": {"type": "integer", "minimum": 1, "description": "1-based line number in that file at that commit."}}}

REPLY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ptal replies",
    "description": "A coding agent's answers to the comments of a ptal review brief.",
    "type": "object", "additionalProperties": False, "required": ["replies"],
    "properties": {"replies": {"type": "array", "items": {"$ref": "#/$defs/reply"}}},
    "$defs": {
        "location": LOCATION_SCHEMA,
        "reply": {
            "type": "object", "additionalProperties": False, "required": ["text"],
            "oneOf": [{"required": ["re", "status"]}, {"required": ["at"]}],
            "properties": {
                "re": {"type": "string", "description": "The id of the comment this answers."},
                "status": {"enum": ["fixed", "answered", "declined", "deferred"],
                           "description": "fixed: changed in a new commit; answered: no change needed, text explains; "
                                          "declined: not doing it, text says why; deferred: later, text says what it needs."},
                "text": {"type": "string", "description": "What you changed, or your answer."},
                "fixedAt": {"type": "array", "items": {"$ref": "#/$defs/location"},
                            "description": "Where the fix is (for status fixed); each entry is a commit, file and line."},
                "at": {"$ref": "#/$defs/location",
                       "description": "For a note of your own that answers no comment: the commit, file and line it is about."}}}}}


def check_location(where, x):
    if not isinstance(x, dict) or set(x) - set(LOCATION_SCHEMA["properties"]) or set(LOCATION_SCHEMA["required"]) - set(x):
        sys.exit("%s: a location needs exactly commit, file and line" % where)
    if not isinstance(x["commit"], str) or not isinstance(x["file"], str) or not isinstance(x["line"], int) or x["line"] < 1:
        sys.exit("%s: commit and file are strings, line a positive integer" % where)


def load_replies(paths):
    """Agent replies from one or more JSON files, each {"replies": [...]} as
    REPLY_SCHEMA describes (a bare list is accepted too).  Every item either
    answers a comment ("re": its id, with a status) or stands alone ("at": a
    location).  Checked here against the schema's constraints by hand."""
    props = REPLY_SCHEMA["$defs"]["reply"]["properties"]
    statuses = props["status"]["enum"]
    out = []
    for n, path in enumerate(paths):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        items = doc if isinstance(doc, list) else doc.get("replies")
        if not isinstance(items, list):
            sys.exit('%s: expected {"replies": [...]}' % path)
        for k, item in enumerate(items):
            where = "%s: reply %d" % (path, k + 1)
            if not isinstance(item, dict) or set(item) - set(props):
                sys.exit("%s: unknown field(s) %s" % (where, sorted(set(item) - set(props)) if isinstance(item, dict) else "(not an object)"))
            if not isinstance(item.get("text"), str):
                sys.exit("%s: needs a text" % where)
            if ("re" in item) == ("at" in item):
                sys.exit("%s: needs either re (with a status) or at, not both" % where)
            if "re" in item:
                if not isinstance(item["re"], str) or item.get("status") not in statuses:
                    sys.exit("%s: re is a comment id and status one of %s" % (where, ", ".join(statuses)))
            else:
                check_location(where + " at", item["at"])
                if "status" in item:
                    sys.exit("%s: a note (at) has no status" % where)
            if "fixedAt" in item:
                if not isinstance(item["fixedAt"], list):
                    sys.exit("%s: fixedAt is a list of locations" % where)
                for x in item["fixedAt"]:
                    check_location(where + " fixedAt", x)
            r = dict(item)
            r["id"] = "agent-%d-%d" % (n + 1, k + 1)
            r["author"] = "agent"
            r["source"] = os.path.basename(path)
            out.append(r)
    return out


def resolve_locations(root, replies):
    """Replace the commit of every location with its full hash when git knows
    it; the page matches them against the snapshots by prefix, so an unknown
    or abbreviated one still renders, just without a link."""
    cache = {}
    def full(c):
        if c not in cache:
            p = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", c + "^{commit}"], capture_output=True)
            cache[c] = p.stdout.decode().strip() if p.returncode == 0 else c
        return cache[c]
    for r in replies:
        for x in r.get("fixedAt", []) + ([r["at"]] if "at" in r else []):
            x["commit"] = full(x["commit"])


def load_seed(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, list):
        doc = {"comments": doc}
    return {"comments": doc.get("comments", []), "resolved": doc.get("resolved", {})}


def render(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    title = "%s %s" % (data["commit"]["short"], data["commit"]["subject"])
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    return TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #fff; --fg: #1b1f23; --dim: #6a737d; --line: #d9dde2; --pane: #f6f8fa;
  --accent: #0366d6; --sel: #e8f0fe; --cur: #f0b429;
  --del: #ffecec; --del2: #ffb8b8; --add: #e6ffec; --add2: #a6f3b4; --chg: #fff8e1;
  --cmt: #fffbe6; --cmtb: #e3c800; --agent: #e8f1fb; --agentb: #4a90d9; --note: #f1ebff; --noteb: #7c4dff; --btn: #f3f4f6; --danger: #c62828;
  --ok: #1a7f37; --warn: #9a6700;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #d7dde3; --dim: #8b949e; --line: #30363d; --pane: #161b22;
    --accent: #58a6ff; --sel: #1f2d3d; --cur: #e3b341;
    --del: #3a1d1f; --del2: #7a2b2f; --add: #17311f; --add2: #2b6a3a; --chg: #2b2a1c;
    --cmt: #2a2713; --cmtb: #9e8a00; --agent: #13233a; --agentb: #3b73b9; --note: #221a38; --noteb: #9b7bff; --btn: #21262d; --danger: #ff7b72;
    --ok: #3fb950; --warn: #d29922;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font: 13px/1.4 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       display: flex; flex-direction: column; overflow: hidden; }
button, select { font: inherit; color: inherit; background: var(--btn); border: 1px solid var(--line);
         border-radius: 4px; padding: 2px 8px; cursor: pointer; }
button:hover { border-color: var(--accent); }
button.danger { color: var(--danger); }
button.on { border-color: var(--accent); color: var(--accent); }
a { color: var(--accent); }
code, .mono { font-family: var(--mono); font-size: 12px; }

#top { flex: 0 0 auto; height: 38vh; overflow: auto; background: var(--pane);
       border-bottom: 1px solid var(--line); }
#splitter { flex: 0 0 7px; background: var(--line); cursor: row-resize; position: relative; }
#splitter::after { content: ""; position: absolute; left: 50%; top: 2px; width: 40px; height: 3px;
                   margin-left: -20px; border-radius: 2px; background: var(--dim); opacity: .5; }
#bottom { flex: 1 1 auto; overflow: auto; position: relative; }

#bar { display: flex; gap: 6px; align-items: center; padding: 6px 8px; position: sticky; top: 0;
       background: var(--pane); border-bottom: 1px solid var(--line); z-index: 2; flex-wrap: wrap; }
#bar .title { font-weight: 600; flex: 1 1 auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#commit { padding: 8px; }
#commit .meta { color: var(--dim); font-size: 12px; margin-bottom: 4px; overflow-wrap: anywhere; }
#commit .meta b { color: var(--fg); font-weight: 500; }
#commit .follow { margin-top: 6px; font-size: 12px; }
#commit .follow details { margin: 2px 0 2px 1.6em; color: var(--dim); }
#commit .follow summary { cursor: pointer; }
#commit .follow pre { white-space: pre-wrap; font: 12px/1.4 var(--mono); margin: 4px 0; }
.msg { margin: 6px 0; }
.mline { display: flex; align-items: flex-start; }
.mline .gut, .ln { flex: 0 0 auto; color: var(--dim); cursor: pointer; user-select: none; text-align: right; }
.mline .gut { width: 1.6em; min-height: 1.4em; align-self: stretch; margin-right: 6px; font-family: var(--mono); font-size: 12px; }
.mline:hover .gut::before, td.ln:hover::before { content: "+"; color: var(--accent); font-weight: bold; }
.mline .gut.has::before, td.ln.has::before { content: "\25CF"; color: var(--cmtb); }
.mline .txt { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); font-size: 12px; }
.mline.subject .txt { font-weight: 600; font-size: 13px; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }

#files { border-top: 1px solid var(--line); padding-bottom: 6px; }
#files .hdr { padding: 6px 8px 2px; color: var(--dim); font-size: 12px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
#files .hdr .stat { margin-left: auto; }
#files .hdr select { font-size: 12px; padding: 1px 4px; max-width: 60vw; }
#ftree { border-collapse: collapse; width: auto; max-width: 100%; }
#ftree td { padding: 3px 8px 3px 0; vertical-align: baseline; }
#ftree td.fcell { padding-left: 8px; border-left: 3px solid transparent; font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
#ftree td.stat { font-size: 11px; font-family: var(--mono); white-space: nowrap; padding-right: 12px; }
#ftree tr.file { cursor: pointer; }
#ftree tr.file:hover td, #ftree tr.dir:hover td { background: var(--sel); }
#ftree tr.file.sel td { background: var(--sel); }
#ftree tr.file.sel td.fcell { border-left-color: var(--accent); }
#ftree tr.dir { cursor: pointer; color: var(--dim); user-select: none; }
#ftree tr.dir .caret { display: inline-block; width: 1em; text-align: center; font-size: 10px; }
#ftree tr.hidden { display: none; }
#ftree input.rev { margin: 0 6px 0 0; vertical-align: -2px; accent-color: var(--accent); cursor: pointer; }
#ftree tr.reviewed td.fcell { color: var(--dim); }
#files .hdr button.small { padding: 0 6px; font-size: 11px; }
#files .progress { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding: 2px 8px 6px; font-size: 12px; color: var(--dim); }
#files .progress b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
#files .meter { display: inline-flex; gap: 6px; align-items: center; }
#files .bar { display: inline-block; width: 72px; height: 7px; border-radius: 4px; background: var(--line); overflow: hidden; }
#files .fill { display: block; height: 100%; background: var(--accent); border-radius: 4px; transition: width .15s; }
.st { font-family: var(--mono); font-size: 11px; font-weight: 700; display: inline-block; min-width: 1.3em; text-align: center;
      border-radius: 3px; padding: 0 2px; margin-right: 5px; }
.st.A { background: var(--add2); } .st.D { background: var(--del2); } .st.M { background: var(--chg); }
.st.R, .st.C, .st.T, .st.U, .st.X { background: var(--sel); }
.fcell .dir { color: var(--dim); }
.stat .a { color: #1a7f37; } .stat .d { color: #cf222e; }
@media (prefers-color-scheme: dark) { .stat .a { color: #3fb950; } .stat .d { color: #f85149; } }
.badge { font-size: 11px; font-family: system-ui, sans-serif; background: var(--cmtb); color: #000; border-radius: 8px; padding: 0 6px; margin-left: 6px; }
.badge.open { background: var(--del2); }
.images { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 8px; border-bottom: 1px solid var(--line); }
.images .img { text-align: center; font-size: 11px; color: var(--dim); min-width: 0; }
.images img { max-width: 100%; height: auto; display: block; margin: 0 auto 4px;
              background: repeating-conic-gradient(var(--line) 0 25%, var(--bg) 0 50%) 0 0 / 16px 16px; }
.images .none { padding: 24px 0; border: 1px dashed var(--line); border-radius: 4px; }
.images.dark img { background: repeating-conic-gradient(#4a4a4a 0 25%, #2a2a2a 0 50%) 0 0 / 16px 16px; }
.images .note { grid-column: 1 / -1; text-align: center; font-size: 11px; color: var(--dim); }

#filehead { position: sticky; top: 0; z-index: 2; background: var(--pane); border-bottom: 1px solid var(--line);
            padding: 4px 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 12px; }
#filehead .p { font-family: var(--mono); flex: 1 1 auto; overflow-wrap: anywhere; }
#filehead .hk { color: var(--dim); white-space: nowrap; }
#filehead .vw { color: var(--dim); white-space: nowrap; font-family: var(--mono); }
#empty { padding: 24px; color: var(--dim); text-align: center; }
#empty kbd { font-family: var(--mono); background: var(--btn); border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; }

table.diff { border-collapse: collapse; width: 100%; table-layout: fixed; font-family: var(--mono); font-size: 12px;
             tab-size: 4; }
table.diff.nowrap { width: auto; min-width: 100%; table-layout: auto; }
table.diff col.ln { width: 3.4em; }
table.diff td { padding: 0 4px; vertical-align: top; line-height: 1.45; }
td.ln { text-align: right; color: var(--dim); user-select: none; cursor: pointer; white-space: nowrap;
        border-right: 1px solid var(--line); min-width: 3.4em; }
td.ln.n { border-left: 1px solid var(--line); }
td.ln.has { color: var(--cmtb); }
td.code { white-space: pre-wrap; overflow-wrap: anywhere; }
table.diff.nowrap td.code { white-space: pre; }
td.code.o { border-right: 1px solid var(--line); }
tr.del td.o, tr.chg td.o { background: var(--del); }
tr.add td.n, tr.chg td.n { background: var(--add); }
tr.add td.o, tr.del td.n { background: var(--pane); }
tr.add td.o.code, tr.del td.n.code { background: repeating-linear-gradient(135deg, transparent 0 6px, var(--line) 6px 7px); opacity: .6; }
.x { background: var(--del2); border-radius: 2px; }
.y { background: var(--add2); border-radius: 2px; }
tr.cur td.ln.o { box-shadow: inset 3px 0 0 var(--cur); }
tr.flash td { animation: flash 1.2s; }
@keyframes flash { from { background: var(--cur); } to { } }
tr.fold td { background: var(--pane); color: var(--accent); text-align: center; cursor: pointer; padding: 2px; font-size: 11px;
             border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); user-select: none; }
tr.fold:hover td { text-decoration: underline; }
tr.cmtrow td { padding: 0; background: var(--bg); }
.thread { margin: 3px 4px; }
.cmt { padding: 5px 8px; background: var(--cmt); border: 1px solid var(--cmtb); border-left-width: 4px;
       border-radius: 4px; font-family: system-ui, sans-serif; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
.cmt.agent { background: var(--agent); border-color: var(--agentb); }
.cmt.note { background: var(--note); border-color: var(--noteb); }
#notes { margin: 6px 0; padding: 5px 8px; background: var(--note); border: 1px solid var(--noteb); border-left-width: 4px; font-size: 12px; }
#notes b { color: var(--noteb); }
#notes div { margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#notes a { cursor: pointer; color: var(--noteb); margin-right: 6px; }
.editor label { font-size: 12px; color: var(--dim); display: inline-flex; align-items: center; gap: 4px; }
.cmt.reply { margin-left: 14px; margin-top: 3px; border-left-width: 3px; }
.thread.resolved .cmt { opacity: .75; }
.cmt .tools { float: right; margin-left: 8px; }
.cmt .tools button { padding: 0 5px; font-size: 11px; }
.cmt .when { color: var(--dim); font-size: 11px; }
.cmt .who { font-weight: 600; font-size: 11px; margin-right: 6px; }
.cmt .who.agent { color: var(--agentb); }
.who.note { color: var(--noteb); }
#panel .pc.note { border-left: 3px solid var(--noteb); }
.cmt .src { font-family: var(--mono); font-size: 11px; color: var(--dim); }
.stt { font-size: 10.5px; font-weight: 700; text-transform: uppercase; border-radius: 3px; padding: 0 5px; margin-right: 6px; letter-spacing: .02em; }
.stt.fixed { background: var(--add2); color: #0b3d1b; } .stt.answered { background: var(--sel); color: var(--accent); }
.stt.declined { background: var(--del2); color: #5a1010; } .stt.deferred { background: var(--line); color: var(--fg); }
.stt.other { background: var(--chg); color: var(--fg); }
.tfoot { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 11.5px; color: var(--dim); margin: 2px 0 0 4px; }
.tfoot label { cursor: pointer; }
.tfoot input { accent-color: var(--ok); vertical-align: -2px; margin: 0 3px 0 0; }
.tfoot a { cursor: pointer; }
.tfoot .map { font-family: var(--mono); }
.editor { margin: 3px 4px; }
.editor textarea { width: 100%; min-height: 4em; font: 12.5px/1.4 system-ui, sans-serif; color: var(--fg); background: var(--bg);
                   border: 1px solid var(--accent); border-radius: 4px; padding: 4px; resize: vertical; }
.editor .row { display: flex; gap: 6px; margin-top: 3px; align-items: center; }
.editor .hint { color: var(--dim); font-size: 11px; flex: 1 1 auto; }

#panel, #help { position: fixed; inset: 0; background: var(--bg); z-index: 10; display: none; flex-direction: column; }
#panel.open, #help.open { display: flex; }
#panel .phead { display: flex; gap: 6px; align-items: center; padding: 8px; border-bottom: 1px solid var(--line); flex-wrap: wrap;
                background: var(--pane); }
#panel .phead b { flex: 1 1 auto; }
#panel .pf { display: flex; gap: 4px; padding: 6px 8px 0; align-items: center; font-size: 12px; color: var(--dim); }
#panel .pf button { padding: 0 8px; font-size: 11px; }
#panel .pbody { flex: 1 1 auto; overflow: auto; padding: 8px; }
#panel h3 { font-size: 12px; font-family: var(--mono); margin: 12px 0 4px; color: var(--dim); overflow-wrap: anywhere; }
#panel .pc { margin: 4px 0 8px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 4px; }
#panel .pc.resolved { opacity: .7; }
#panel .pc .loc { font-family: var(--mono); font-size: 11px; color: var(--dim); display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
#panel .pc .loc a { cursor: pointer; }
#panel .pc .src { font-family: var(--mono); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--dim);
                  border-left: 2px solid var(--line); padding-left: 6px; margin: 3px 0; }
#panel .pc .txt { white-space: pre-wrap; overflow-wrap: anywhere; }
#panel .pc .rp { margin: 4px 0 0 12px; padding: 3px 6px; border-left: 3px solid var(--agentb); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12.5px; }
#panel .pc .rp.reviewer { border-left-color: var(--cmtb); }
#help .hbody { padding: 16px; overflow: auto; }
#help table td { padding: 2px 10px 2px 0; }
#help kbd { font-family: var(--mono); background: var(--btn); border: 1px solid var(--line); border-radius: 3px; padding: 0 5px; }
#toast { position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%); background: var(--fg); color: var(--bg);
         padding: 6px 12px; border-radius: 4px; opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 20; }
#toast.show { opacity: .9; }
</style>
</head>
<body>
<div id="top">
  <div id="bar">
    <span class="title" id="bartitle"></span>
    <button id="btn-comments" title="All comments (C)">Comments</button>
    <button id="btn-help" title="Keys (?)">?</button>
  </div>
  <div id="commit"></div>
  <div id="files"></div>
</div>
<div id="splitter" title="Drag to resize; double-click to toggle"></div>
<div id="bottom">
  <div id="empty">Select a file above, or press <kbd>n</kbd> to start at the first change.</div>
</div>
<div id="panel">
  <div class="phead">
    <b id="ptitle">Comments</b>
    <button id="btn-brief" title="Markdown brief of the unresolved comments, with context and the reply format">Copy brief for agent</button>
    <button id="btn-copy">Copy as Markdown</button>
    <button id="btn-json">Download JSON</button>
    <button id="btn-delall" class="danger">Delete all</button>
    <button id="btn-pclose">Close</button>
  </div>
  <div class="pf" id="pfilter"></div>
  <div class="pbody" id="pbody"></div>
</div>
<div id="help">
  <div class="phead" style="display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--line);background:var(--pane)">
    <b style="flex:1 1 auto">Keys</b><button id="btn-hclose">Close</button>
  </div>
  <div class="hbody">
    <table>
      <tr><td><kbd>n</kbd> / <kbd>p</kbd></td><td>next / previous change; continues into the next / previous file</td></tr>
      <tr><td><kbd>]</kbd> / <kbd>[</kbd></td><td>next / previous file</td></tr>
      <tr><td><kbd>v</kbd></td><td>cycle the view: which two snapshots are compared (only with follow-up commits)</td></tr>
      <tr><td><kbd>c</kbd></td><td>comment on the current change (right side; left side if nothing was added)</td></tr>
      <tr><td><kbd>N</kbd></td><td>note to self on the current change: a comment the agent never sees, listed at the top until resolved</td></tr>
      <tr><td><kbd>C</kbd></td><td>show all comments, with their replies and resolved marks</td></tr>
      <tr><td><kbd>r</kbd></td><td>mark / unmark the current file as reviewed in this view (the checkbox in the file list)</td></tr>
      <tr><td><kbd>u</kbd></td><td>show only unreviewed files; <kbd>n</kbd>/<kbd>p</kbd> and <kbd>]</kbd>/<kbd>[</kbd> then skip reviewed ones</td></tr>
      <tr><td><kbd>w</kbd></td><td>toggle line wrapping</td></tr>
      <tr><td><kbd>x</kbd></td><td>expand all folded unchanged regions in this file</td></tr>
      <tr><td><kbd>t</kbd></td><td>toggle the top pane</td></tr>
      <tr><td><kbd>?</kbd></td><td>this help</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>close overlays / cancel editing</td></tr>
      <tr><td><kbd>Ctrl</kbd>+<kbd>Enter</kbd></td><td>save the comment being edited</td></tr>
    </table>
    <p>Click a line number (either side) or the gutter of a commit-message line to attach a comment. A comment belongs
       to the snapshot it was made on and shows in every view that includes that snapshot. Comments, reviewed marks and
       resolved marks are stored in this browser's localStorage under a prefix made from the commit's abbreviated hash,
       so a regenerated page for the same commit keeps them.</p>
  </div>
</div>
<div id="toast"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
'use strict';
const DATA = JSON.parse(document.getElementById('data').textContent);
const PREFIX = 'ptal:' + DATA.commit.short + ':';
const FOLD_MIN = 24, FOLD_CTX = 4, MERGE_GAP = 3;
const SN = DATA.snaps, M = SN.length, files = DATA.files, blobs = DATA.blobs;
const STATUSES = ['fixed', 'answered', 'declined', 'deferred'];

// ---- storage -----------------------------------------------------------------
// Keys: comments (reviewer comments), resolved (comment id -> true), reviewed
// (file key -> true, for the S0->S1 view; other views use reviewed:a-b), ui.
// Comment records keep the single-commit page's fields (file, side, line, src,
// text) and add snap (snapshot index; null for the commit message), author,
// re (parent comment id), so pages generated before snapshots existed still
// read them.
const store = {
  get(k, d) { try { const v = localStorage.getItem(PREFIX + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(PREFIX + k, JSON.stringify(v)); } catch (e) { toast('localStorage unavailable: ' + e.message); } },
};
function sideOf(c) { return c.file == null ? 'commit' : c.snap === 0 ? 'old' : c.snap === 1 ? 'new' : 'snap' + c.snap; }
function normalize(c) {
  const n = Object.assign({ author: 'reviewer', re: null }, c);
  if (n.file === undefined) n.file = null;
  if (n.snap === undefined || (n.snap === null && n.file != null))
    n.snap = n.file == null ? null : n.side === 'old' ? 0 : n.side === 'new' ? 1 : /^snap\d+$/.test(n.side || '') ? +n.side.slice(4) : 1;
  n.side = sideOf(n);
  return n;
}
let comments = store.get('comments', []).map(normalize);
let resolved = store.get('resolved', {});
const ui = Object.assign({ file: -1, hunk: -1, topH: null, wrap: true, topHidden: false, collapsed: {}, unreviewedOnly: false, view: [0, 1], filter: 'all' }, store.get('ui', {}));
if (!Array.isArray(ui.view) || ui.view.length !== 2 || !(ui.view[0] >= 0 && ui.view[1] < M && ui.view[0] < ui.view[1])) ui.view = [0, 1];
function reviewedKey() { const [a, b] = ui.view; return a === 0 && b === 1 ? 'reviewed' : 'reviewed:' + a + '-' + b; }
let reviewed = store.get(reviewedKey(), {});
function saveComments() { store.set('comments', comments); }
function saveResolved() { store.set('resolved', resolved); }
function saveReviewed() { store.set(reviewedKey(), reviewed); }
function saveUI() { store.set('ui', ui); }
if (DATA.seed) {   // a comments export given on the command line: add what this browser lacks
  let added = 0;
  for (const c of DATA.seed.comments || []) if (c && c.id && !comments.some(x => x.id === c.id)) { comments.push(normalize(c)); added++; }
  for (const id of Object.keys(DATA.seed.resolved || {})) if (!resolved[id]) { resolved[id] = true; added++; }
  if (added) { saveComments(); saveResolved(); }
}
window.addEventListener('storage', e => {
  if (e.key === PREFIX + 'comments') { comments = store.get('comments', []).map(normalize); rerenderAll(); }
  if (e.key === PREFIX + 'resolved') { resolved = store.get('resolved', {}); rerenderAll(); }
  if (e.key === PREFIX + reviewedKey()) { reviewed = store.get(reviewedKey(), {}); renderFiles(); }
});

// ---- helpers -----------------------------------------------------------------
const $ = s => document.querySelector(s);
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    if (k === 'class') e.className = attrs[k];
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]);
    else if (k === 'dataset') Object.assign(e.dataset, attrs[k]);
    else if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  }
  for (const kid of kids) if (kid != null) e.append(kid);
  return e;
}
let toastTimer;
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1600); }
function fmtDate(iso) { try { return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); } catch (e) { return iso; } }
function fmtSize(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(2) + ' MB'; }
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }
function snapName(k) { return 'S' + k + ' ' + SN[k].label; }
function linesAt(f, k) { const s = f.shas[k]; return s && blobs[s].lines ? blobs[s].lines : []; }
function isBinaryAt(f, k) { const s = f.shas[k]; return !!(s && blobs[s].binary); }
function hunksFor(a, b) { return a === b ? [] : (DATA.hunks[(a || '') + ':' + (b || '')] || []); }
function fileIndexByKey(key) { return files.findIndex(f => f.key === key); }

// ---- views: which two snapshots are compared -------------------------------
// rounds first (consecutive snapshots), then wider spans, the net view last
function pairs() { const out = []; for (let i = 0; i < M; i++) for (let j = i + 1; j < M; j++) out.push([i, j]); return out.sort((x, y) => (x[1] - x[0]) - (y[1] - y[0]) || x[0] - y[0]); }
function viewLabel(a, b) {
  const tag = b === a + 1 ? 'round ' + b : a === 0 && b === M - 1 ? 'net' : '';
  return snapName(a) + ' → ' + snapName(b) + (tag ? '  (' + tag + ')' : '');
}
function pathIn(f) { const [a, b] = ui.view; return f.paths[b] || f.paths[a] || f.key; }
function statusIn(f) {
  const [a, b] = ui.view;
  if (!f.shas[a] && !f.shas[b]) return 'X';
  if (!f.shas[a]) return b === a + 1 && f.copyFrom[a] ? 'C' : 'A';
  if (!f.shas[b]) return 'D';
  if (f.paths[a] !== f.paths[b]) return 'R';
  return 'M';
}
function changedIn(f) { const [a, b] = ui.view; return f.shas[a] !== f.shas[b] || f.paths[a] !== f.paths[b] || f.modes[a] !== f.modes[b]; }
function statsIn(f) {
  const [a, b] = ui.view, hs = hunksFor(f.shas[a], f.shas[b]);
  return { adds: hs.reduce((s, h) => s + h.nc, 0), dels: hs.reduce((s, h) => s + h.oc, 0), binary: isBinaryAt(f, a) || isBinaryAt(f, b) };
}
let order = [];   // file indexes shown in the current view, in list order
function computeOrder() {
  order = files.map((f, i) => i).filter(i => changedIn(files[i]));
  order.sort((x, y) => { const p = pathIn(files[x]), q = pathIn(files[y]); return p < q ? -1 : p > q ? 1 : 0; });
}
function setView(a, b) {
  if (a === ui.view[0] && b === ui.view[1]) return;
  ui.view = [a, b]; reviewed = store.get(reviewedKey(), {}); ui.hunk = -1; expanded = new Set(); rowCache.clear(); saveUI();
  computeOrder();
  if (ui.file >= 0 && !order.includes(ui.file)) ui.file = -1;
  renderFiles(); if (ui.file >= 0) renderDiff(); else { $('#bottom').replaceChildren(el('div', { id: 'empty' }, 'Select a file above, or press n to start at the first change.')); }
  toast('View: ' + viewLabel(a, b));
}
function cycleView() {
  const ps = pairs(); if (ps.length < 2) { toast('Only one view: no follow-up commits'); return; }
  const k = ps.findIndex(p => p[0] === ui.view[0] && p[1] === ui.view[1]);
  const [a, b] = ps[(k + 1) % ps.length]; setView(a, b);
}

// ---- comments: lookup ---------------------------------------------------------
// Agent replies come from the page data; reviewer comments from storage.  A root
// comment has no "re"; replies carry the id of what they answer.
// Locations in replies name a commit; it is matched against the snapshots by
// prefix (snap -1 when the commit is not part of this review), and the file
// by its path at that snapshot.
function snapOfCommit(sha) { return !sha ? -1 : SN.findIndex(s => s.sha === sha || (sha.length >= 7 && (s.sha.startsWith(sha) || sha.startsWith(s.sha)))); }
function keyAt(path, snap) { const f = files.find(f => f.paths[snap] === path); return f ? f.key : path; }
function locOf(x) { const snap = snapOfCommit(x.commit); return { commit: x.commit, snap, file: snap >= 0 ? keyAt(x.file, snap) : x.file, line: x.line }; }
const agentComments = (DATA.replies || []).map(r => {
  const n = Object.assign({}, r, { author: 'agent', re: r.re || null, status: r.status || null, text: r.text || '' });
  const parent = n.re ? comments.find(c => c.id === n.re) : null;
  if (r.at) { const L = locOf(r.at); n.at = L; n.file = L.file; n.snap = L.snap; n.line = L.line; n.orphan = L.snap < 0; }
  else if (parent) { n.file = parent.file; n.snap = parent.snap; n.line = parent.line; }
  else { n.file = null; n.snap = null; n.line = 0; n.orphan = true; }
  n.fixedAt = (r.fixedAt || []).map(locOf);
  return n;
});
function locLink(x, before) {
  const label = x.file + ':' + x.line;
  if (x.snap < 0) return el('span', { class: 'map', title: 'commit ' + x.commit + ' is not part of this review' }, x.commit.slice(0, 7) + ' ' + label + ' (not in this review)');
  return el('a', { class: 'map', onclick: () => { if (before) before(); showLine(x.file, x.snap, x.line); } }, snapName(x.snap) + ' ' + label);
}
function all() { return comments.concat(agentComments); }
function byId(id) { return all().find(c => c.id === id); }
function rootsAt(file, snap, line) { return all().filter(c => !c.re && !c.orphan && c.file === file && c.snap === snap && c.line === line); }
function repliesTo(id) { return all().filter(c => c.re === id).sort((x, y) => (x.created || '') < (y.created || '') ? -1 : 1); }
function roots() { return all().filter(c => !c.re && !c.orphan); }
function isResolved(c) { return !!resolved[c.id]; }
// A note to self is a reviewer comment the agent never sees: left out of the
// brief, listed in the top pane until resolved.
function isNote(c) { return c.kind === 'note'; }
function whoLabel(c) { return c.author === 'agent' ? 'agent' : isNote(c) ? 'note to self' : 'you'; }
function whoClass(c) { return c.author === 'agent' ? 'agent' : isNote(c) ? 'note' : 'reviewer'; }
function openNotes() { return sortedRoots().filter(c => isNote(c) && !isResolved(c)); }
function countFor(fileKey) { return roots().filter(c => c.file === fileKey).length; }
function openCountFor(fileKey) { return roots().filter(c => c.file === fileKey && !isResolved(c) && c.author === 'reviewer').length; }

// ---- rows & hunks ------------------------------------------------------------
const rowCache = new Map();
function rowsOf(fi) {
  const [a, b] = ui.view, ck = fi + ':' + a + ':' + b;
  if (rowCache.has(ck)) return rowCache.get(ck);
  const f = files[fi], rows = [], groups = [], old = linesAt(f, a), nw = linesAt(f, b);
  let o = 0, n = 0, g = null, lastEnd = -Infinity;
  for (const h of hunksFor(f.shas[a], f.shas[b])) {
    const os = h.oc ? h.os - 1 : h.os, ns = h.nc ? h.ns - 1 : h.ns;
    while (o < os && n < ns) rows.push({ t: 'eq', o: o++, n: n++ });
    if (o !== os || n !== ns) { console.warn('hunk misaligned', f.key, h, o, n); }
    const start = rows.length;
    if (!g || start - lastEnd > MERGE_GAP) { g = { start, end: start, idx: groups.length }; groups.push(g); }
    const both = Math.min(h.oc, h.nc);
    for (let i = 0; i < both; i++) rows.push({ t: 'chg', o: o++, n: n++, g: g.idx });
    for (let i = both; i < h.oc; i++) rows.push({ t: 'del', o: o++, n: null, g: g.idx });
    for (let i = both; i < h.nc; i++) rows.push({ t: 'add', o: null, n: n++, g: g.idx });
    g.end = lastEnd = rows.length;
  }
  while (o < old.length && n < nw.length) rows.push({ t: 'eq', o: o++, n: n++ });
  while (o < old.length) rows.push({ t: 'del', o: o++, n: null });
  while (n < nw.length) rows.push({ t: 'add', o: null, n: n++ });
  const r = { rows, groups, old, nw };
  rowCache.set(ck, r);
  return r;
}
function groupCount(fi) { return Math.max(1, rowsOf(fi).groups.length); }
// Where line L (1-based) of the from-snapshot of hunks hs ended up: {line} or {changed: true}.
function mapLine(hs, L) {
  let delta = 0;
  for (const h of hs) {
    if (h.oc && L >= h.os && L < h.os + h.oc) return { changed: true, h };
    if (L < (h.oc ? h.os : h.os + 1)) break;
    delta += h.nc - h.oc;
  }
  return { line: L + delta };
}

// intra-line emphasis for changed pairs: common prefix/suffix
function emphasize(a, b) {
  let p = 0; const max = Math.min(a.length, b.length);
  while (p < max && a[p] === b[p]) p++;
  let s = 0;
  while (s < max - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  if (p + s === 0 || (a.length - p - s) > a.length * 0.9 && (b.length - p - s) > b.length * 0.9) return null;
  return { p, s };
}
function codeCell(text, cls, other, mark) {
  const td = el('td', { class: 'code ' + cls });
  const e = other != null ? emphasize(text, other) : null;
  if (e && text.length - e.p - e.s > 0) {
    td.append(text.slice(0, e.p), el('span', { class: mark }, text.slice(e.p, text.length - e.s)), text.slice(text.length - e.s));
  } else td.textContent = text;
  if (!text) td.innerHTML = '&nbsp;';
  return td;
}

// ---- top pane: commit --------------------------------------------------------
function renderCommit() {
  const c = DATA.commit, box = $('#commit');
  box.replaceChildren();
  $('#bartitle').textContent = c.short + ' ' + c.subject;
  document.title = c.short + ' ' + c.subject;
  const meta = el('div', { class: 'meta' });
  meta.append(el('span', { class: 'mono', title: c.sha }, c.sha), ' ');
  if (c.refs) meta.append(el('span', null, '(' + c.refs + ')'), ' ');
  meta.append(el('br'), el('b', null, c.author), ' <' + c.authorEmail + '> ', fmtDate(c.authorDate));
  if (c.committer !== c.author || c.commitDate !== c.authorDate)
    meta.append(el('br'), 'committed by ', el('b', null, c.committer), ' ', fmtDate(c.commitDate));
  meta.append(el('br'), c.parents.length ? 'parent' + (c.parents.length > 1 ? 's' : '') + ' ' : 'root commit',
    ...c.parents.map(p => el('span', { class: 'mono', title: p }, p.slice(0, 10) + ' ')));
  if (c.parents.length > 1) meta.append(el('br'), 'merge commit: changes shown against the first parent');
  meta.append(el('br'), el('span', null, c.root + ' · generated ' + fmtDate(DATA.generated)));
  box.append(meta);
  const notes = openNotes();
  if (notes.length) {
    const nb = el('div', { id: 'notes' }, el('b', null, notes.length + ' open note' + (notes.length === 1 ? '' : 's') + ' to self'));
    for (const cm of notes) nb.append(el('div', { title: cm.text }, el('a', { onclick: () => jumpTo(cm) }, cm.file == null ? 'commit' : cm.file + ' ' + snapName(cm.snap) + ':' + cm.line), cm.text.split('\n')[0]));
    box.append(nb);
  }
  const msg = el('div', { class: 'msg' });
  const lines = [c.subject, ...(c.body ? ['', ...c.body.split('\n')] : [])];
  lines.forEach((text, i) => {
    const ln = i + 1, ts = rootsAt(null, null, ln);
    const line = el('div', { class: 'mline' + (i === 0 ? ' subject' : ''), dataset: { line: ln } },
      el('span', { class: 'gut' + (ts.length ? ' has' : ''), title: 'Comment on this line', onclick: () => openEditor(line, { file: null, snap: null, line: ln, src: text }) }),
      el('span', { class: 'txt' }, text || ' '));
    msg.append(line);
    for (const cm of ts) msg.append(threadEl(cm));
  });
  box.append(msg);
  const general = el('div', { class: 'actions' },
    el('button', { onclick: () => openEditor(general, { file: null, snap: null, line: 0, src: c.subject }) }, '+ comment on the commit'));
  box.append(general);
  for (const cm of rootsAt(null, null, 0)) box.append(threadEl(cm));
  if (M > 2) {
    const fo = el('div', { class: 'follow' }, el('b', null, 'Follow-up commits'));
    for (let k = 2; k < M; k++) {
      const s = SN[k];
      fo.append(el('details', null, el('summary', null, el('span', { class: 'mono', title: s.sha }, 'S' + k + ' ' + s.short), ' ', s.subject,
        el('span', { class: 'when' }, '  ' + (s.author || '') + ' ' + (s.authorDate ? fmtDate(s.authorDate) : ''))),
        s.body ? el('pre', null, s.body) : el('span', null, '(no body)')));
    }
    box.append(fo);
  }
}

// ---- top pane: files ---------------------------------------------------------
// The list is in byte order of the path shown, which is also the order of a
// tree walk that sorts each directory's entries by name with "/" appended to
// directory names, so list order and n/p order agree.  Single-child directory
// chains are shown as one "a/b/c" row.
function buildTree() {
  const root = { name: '', path: '', dirs: [], files: [] };
  for (const i of order) {
    let node = root;
    for (const part of pathIn(files[i]).split('/').slice(0, -1)) {
      let d = node.dirs.find(d => d.name === part);
      if (!d) { d = { name: part, path: node.path + part + '/', dirs: [], files: [] }; node.dirs.push(d); }
      node = d;
    }
    node.files.push(i);
  }
  (function compress(node) {
    node.dirs = node.dirs.map(d => { while (!d.files.length && d.dirs.length === 1) { const c = d.dirs[0]; d = { name: d.name + '/' + c.name, path: c.path, dirs: c.dirs, files: c.files }; } return d; });
    node.dirs.forEach(compress);
  })(root);
  return root;
}
function renderFiles() {
  const box = $('#files');
  box.replaceChildren();
  const [a, b] = ui.view;
  let adds = 0, dels = 0;
  for (const i of order) { const s = statsIn(files[i]); adds += s.adds; dels += s.dels; }
  const nrev = order.filter(i => isReviewed(files[i])).length;
  const hdr = el('div', { class: 'hdr' }, el('span', null, order.length + ' file' + (order.length === 1 ? '' : 's') + ' changed'));
  if (M > 2) {
    const sel = el('select', { id: 'viewsel', title: 'Which two snapshots to compare (v cycles)' });
    for (const [i, j] of pairs()) { const o = el('option', { value: i + '-' + j }, viewLabel(i, j)); if (i === a && j === b) o.selected = true; sel.append(o); }
    sel.addEventListener('change', () => { const [i, j] = sel.value.split('-').map(Number); setView(i, j); });
    hdr.append(sel);
  }
  hdr.append(el('button', { class: 'small' + (ui.unreviewedOnly ? ' on' : ''), title: 'Show only unreviewed files (u)', onclick: toggleUnreviewed }, ui.unreviewedOnly ? 'unreviewed only' : 'all files'),
    el('span', { class: 'stat' }, el('span', { class: 'a' }, '+' + adds), ' ', el('span', { class: 'd' }, '−' + dels)));
  box.append(hdr);
  box.append(progressEl(nrev));
  const tbody = el('tbody');
  const shown = i => !(ui.unreviewedOnly && isReviewed(files[i]));
  (function walk(node, depth, hidden) {
    const entries = [...node.dirs.map(d => ({ key: d.name + '/', d })), ...node.files.map(i => ({ key: pathIn(files[i]).slice(pathIn(files[i]).lastIndexOf('/') + 1), i }))];
    entries.sort((x, y) => x.key < y.key ? -1 : x.key > y.key ? 1 : 0);
    for (const { d, i } of entries) {
      if (d) {
        const collapsed = !!ui.collapsed[d.path];
        const anyShown = d.path && order.some(i => pathIn(files[i]).startsWith(d.path) && shown(i));
        tbody.append(el('tr', { class: 'dir' + (hidden || !anyShown ? ' hidden' : ''), dataset: { dir: d.path }, onclick: () => { if (collapsed) delete ui.collapsed[d.path]; else ui.collapsed[d.path] = true; saveUI(); renderFiles(); } },
          el('td', { class: 'fcell', colspan: 2, style: 'padding-left:' + (8 + depth * 14) + 'px' },
            el('span', { class: 'caret' }, collapsed ? '▶' : '▼'), d.name + '/')));
        walk(d, depth + 1, hidden || collapsed);
        continue;
      }
      const f = files[i], p = pathIn(f), base = p.slice(p.lastIndexOf('/') + 1), st = statusIn(f), stats = statsIn(f);
      const cb = el('input', { type: 'checkbox', class: 'rev', title: 'Reviewed (r toggles the current file)' });
      cb.checked = isReviewed(f);
      cb.addEventListener('click', e => { e.stopPropagation(); setReviewed(i, cb.checked); });
      const cell = el('td', { class: 'fcell', style: 'padding-left:' + (8 + depth * 14) + 'px' }, cb, el('span', { class: 'st ' + st, title: statusName(st) }, st));
      const from = st === 'R' ? f.paths[a] : st === 'C' ? f.copyFrom[a] : null;
      if (from) {
        const sameDir = from.slice(0, from.lastIndexOf('/') + 1) === p.slice(0, p.lastIndexOf('/') + 1);
        cell.append(el('span', { class: 'dir' }, (sameDir ? from.slice(from.lastIndexOf('/') + 1) : from) + ' → '));
      }
      cell.append(base);
      if (f.modes[a] && f.modes[b] && f.modes[a] !== f.modes[b]) cell.append(el('span', { class: 'dir' }, ' ' + f.modes[a] + '→' + f.modes[b]));
      const cnt = countFor(f.key), open = openCountFor(f.key);
      if (cnt) cell.append(el('span', { class: 'badge' + (open ? ' open' : ''), title: open + ' unresolved of ' + cnt }, cnt));
      const stat = el('td', { class: 'stat' });
      if (f.image && !hunksFor(f.shas[a], f.shas[b]).length) stat.append('image');
      else if (stats.binary) stat.append('binary');
      else stat.append(el('span', { class: 'a' }, '+' + stats.adds), ' ', el('span', { class: 'd' }, '−' + stats.dels));
      tbody.append(el('tr', { class: 'file' + (i === ui.file ? ' sel' : '') + (isReviewed(f) ? ' reviewed' : '') + (hidden || !shown(i) ? ' hidden' : ''),
        dataset: { fi: i }, title: p, onclick: () => openFile(i, 0) }, cell, stat));
    }
  })(buildTree(), 0, false);
  box.append(el('table', { id: 'ftree' }, tbody));
  if (ui.unreviewedOnly && nrev === order.length && order.length) box.append(el('div', { id: 'empty' }, 'Every file is marked reviewed.'));
}
function isReviewed(f) { return !!reviewed[f.key]; }
// Progress: share of files reviewed, and share of diff lines (added + deleted,
// text files only) in reviewed files, so big files weigh more.  Percentages
// round, but 100% means complete and anything short of it reads at most 99%.
function pct(n, d) { return !d ? 0 : n === d ? 100 : Math.min(99, Math.round(100 * n / d)); }
function progressEl(nrev) {
  let total = 0, done = 0;
  for (const i of order) { const s = statsIn(files[i]); if (s.binary) continue; total += s.adds + s.dels; if (isReviewed(files[i])) done += s.adds + s.dels; }
  const meter = (label, n, d, unit) => el('span', { class: 'meter', title: n + ' of ' + d + ' ' + unit },
    el('span', { class: 'bar' }, el('span', { class: 'fill', style: 'width:' + pct(n, d) + '%' })),
    el('b', null, pct(n, d) + '%'), ' of ' + label);
  return el('div', { class: 'progress', id: 'progress' }, el('span', { class: 'lbl' }, 'reviewed'), meter('files', nrev, order.length, 'files reviewed'),
    total ? meter('lines', done, total, 'diff lines in reviewed files (binary files not counted)') : null);
}
function setReviewed(fi, on) {
  const k = files[fi].key;
  if (on) reviewed[k] = true; else delete reviewed[k];
  saveReviewed(); renderFiles();
  toast((on ? 'Reviewed: ' : 'Unreviewed: ') + pathIn(files[fi]));
}
function toggleUnreviewed() {
  ui.unreviewedOnly = !ui.unreviewedOnly; saveUI(); renderFiles();
  toast(ui.unreviewedOnly ? 'Showing unreviewed files only' : 'Showing all files');
}
// where n/p and ]/[ may land: every listed file, or only the unreviewed ones while filtering
function navigable(fi) { return !(ui.unreviewedOnly && isReviewed(files[fi])); }
function nextFile(fromPos, dir) {
  for (let p = fromPos + dir; p >= 0 && p < order.length; p += dir) if (navigable(order[p])) return order[p];
  return -1;
}
function revealFile(fi) {
  for (const dir of Object.keys(ui.collapsed)) if (pathIn(files[fi]).startsWith(dir)) delete ui.collapsed[dir];
}
function statusName(s) { return { A: 'added', D: 'deleted', M: 'modified', R: 'renamed', C: 'copied', T: 'type changed', U: 'unmerged', X: 'unknown' }[s] || s; }

// ---- bottom pane: diff -------------------------------------------------------
let expanded = new Set();   // row indexes forced visible (fold expansion) for ui.file
function openFile(fi, hunk) {
  if (fi < 0 || fi >= files.length) return;
  if (fi !== ui.file) expanded = new Set();
  ui.file = fi; ui.hunk = hunk == null ? -1 : hunk; revealFile(fi); saveUI();
  renderFiles(); renderDiff();
  const sel = document.querySelector('.file.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
  if (ui.hunk >= 0) focusHunk(ui.hunk, true); else $('#bottom').scrollTop = 0;
}
function renderDiff() {
  const f = files[ui.file], bottom = $('#bottom'), [a, b] = ui.view, st = statusIn(f);
  bottom.replaceChildren();
  const { rows, groups, old, nw } = rowsOf(ui.file);
  const head = el('div', { id: 'filehead' },
    el('span', { class: 'st ' + st, title: statusName(st) }, st),
    el('span', { class: 'p' }, (st === 'R' ? f.paths[a] + ' → ' : '') + pathIn(f)),
    M > 2 ? el('span', { class: 'vw', title: viewLabel(a, b) }, 'S' + a + ' → S' + b + (changedIn(f) ? '' : ' (unchanged in this view)')) : null,
    el('span', { class: 'hk', id: 'hunkpos' }),
    el('button', { onclick: () => { ui.wrap = !ui.wrap; saveUI(); renderDiff(); }, title: 'Toggle wrapping (w)' }, ui.wrap ? 'wrap' : 'no wrap'),
    el('button', { onclick: expandAll, title: 'Expand folded regions (x)' }, 'expand'));
  bottom.append(head);
  const binary = isBinaryAt(f, a) || isBinaryAt(f, b);
  if (f.image) {
    const side = (k, label) => { const s = f.shas[k], bl = s ? blobs[s] : null; return el('div', { class: 'img' },
      bl && bl.img ? el('img', { src: bl.img, alt: label }) : el('div', { class: 'none' }, bl ? 'image too large to embed' : 'no ' + label + ' image'),
      el('span', null, bl ? label + ': ' + fmtSize(bl.size) : '')); };
    bottom.append(el('div', { class: 'images' + (f.plate === 'dark' ? ' dark' : '') }, side(a, 'before'), side(b, 'after'),
      f.imageNote ? el('div', { class: 'note' }, f.imageNote + (f.plate === 'dark' ? ' (light colours: shown on a dark plate)' : '')) : null));
    if (binary) { updateHunkPos(); return; }
  }
  if (binary) {
    const sz = k => f.shas[k] ? fmtSize(blobs[f.shas[k]].size) : 'absent';
    bottom.append(el('div', { id: 'empty' }, 'Binary file: ' + sz(a) + ' → ' + sz(b) + '.'));
    updateHunkPos(); return;
  }
  if (!rows.length) { bottom.append(el('div', { id: 'empty' }, 'Empty file.')); updateHunkPos(); return; }
  const table = el('table', { class: 'diff' + (ui.wrap ? '' : ' nowrap') },
    el('colgroup', null, el('col', { class: 'ln' }), el('col'), el('col', { class: 'ln' }), el('col')));
  const tbody = el('tbody');
  const interesting = rows.map(r => r.t !== 'eq' || hasComment(f, r));
  let i = 0;
  while (i < rows.length) {
    if (interesting[i] || expanded.has(i)) { tbody.append(rowEl(f, rows[i], i, old, nw)); i++; continue; }
    let j = i; while (j < rows.length && !interesting[j] && !expanded.has(j)) j++;
    const lead = i === 0 ? 0 : FOLD_CTX, trail = j === rows.length ? 0 : FOLD_CTX;
    if (j - i - lead - trail >= FOLD_MIN) {
      for (let k = i; k < i + lead; k++) tbody.append(rowEl(f, rows[k], k, old, nw));
      const from = i + lead, to = j - trail;
      tbody.append(el('tr', { class: 'fold', dataset: { from, to } }, el('td', { colspan: 4, onclick: () => { for (let k = from; k < to; k++) expanded.add(k); renderDiff(); } },
        '⋯ ' + (to - from) + ' unchanged lines ⋯')));
      for (let k = to; k < j; k++) tbody.append(rowEl(f, rows[k], k, old, nw));
    } else for (let k = i; k < j; k++) tbody.append(rowEl(f, rows[k], k, old, nw));
    i = j;
  }
  table.append(tbody);
  bottom.append(table);
  markCurrent(); updateHunkPos();
}
function hasComment(f, r) {
  const [a, b] = ui.view;
  return (r.o != null && rootsAt(f.key, a, r.o + 1).length) || (r.n != null && rootsAt(f.key, b, r.n + 1).length);
}
function rowEl(f, r, idx, old, nw) {
  const frag = document.createDocumentFragment(), [a, b] = ui.view;
  const tr = el('tr', { class: r.t + (r.g != null && r.g === ui.hunk ? ' cur' : ''), dataset: { row: idx, g: r.g == null ? '' : r.g } });
  const oText = r.o != null ? old[r.o] : null, nText = r.n != null ? nw[r.n] : null;
  const oc = r.o != null ? rootsAt(f.key, a, r.o + 1) : [], nc = r.n != null ? rootsAt(f.key, b, r.n + 1) : [];
  const olm = el('td', { class: 'ln o' + (oc.length ? ' has' : ''), title: r.o != null ? 'Comment on ' + snapName(a) + ' line ' + (r.o + 1) : null }, r.o != null ? String(r.o + 1) : '');
  const nln = el('td', { class: 'ln n' + (nc.length ? ' has' : ''), title: r.n != null ? 'Comment on ' + snapName(b) + ' line ' + (r.n + 1) : null }, r.n != null ? String(r.n + 1) : '');
  if (r.o != null) olm.onclick = () => openEditor(tr, { file: f.key, snap: a, line: r.o + 1, src: oText }, 'old');
  if (r.n != null) nln.onclick = () => openEditor(tr, { file: f.key, snap: b, line: r.n + 1, src: nText }, 'new');
  tr.append(olm, codeCell(oText == null ? '' : oText, 'o', r.t === 'chg' ? nText : null, 'x'),
            nln, codeCell(nText == null ? '' : nText, 'n', r.t === 'chg' ? oText : null, 'y'));
  frag.append(tr);
  for (const cm of oc) frag.append(commentRow(cm, 'old'));
  for (const cm of nc) frag.append(commentRow(cm, 'new'));
  return frag;
}
function commentRow(cm, side) {
  const tr = el('tr', { class: 'cmtrow' });
  const left = el('td', { colspan: 2 }), right = el('td', { colspan: 2 });
  (side === 'old' ? left : right).append(threadEl(cm));
  tr.append(left, right);
  return tr;
}
function expandAll() { const { rows } = rowsOf(ui.file); for (let k = 0; k < rows.length; k++) expanded.add(k); renderDiff(); }

// ---- hunk navigation ---------------------------------------------------------
function markCurrent() {
  document.querySelectorAll('tr.cur').forEach(t => t.classList.remove('cur'));
  if (ui.hunk < 0) return;
  document.querySelectorAll('tr[data-g="' + ui.hunk + '"]').forEach(t => t.classList.add('cur'));
}
function updateHunkPos() {
  const e = $('#hunkpos'); if (!e) return;
  const n = rowsOf(ui.file).groups.length;
  e.textContent = n ? (ui.hunk >= 0 ? 'change ' + (ui.hunk + 1) + '/' + n : n + ' change' + (n === 1 ? '' : 's')) : 'no line changes';
}
function focusHunk(h, center) {
  ui.hunk = h; saveUI();
  const { groups } = rowsOf(ui.file);
  if (!groups.length) { $('#bottom').scrollTop = 0; updateHunkPos(); return; }
  const g = groups[h];
  markCurrent(); updateHunkPos();
  const tr = document.querySelector('tr[data-row="' + g.start + '"]');
  if (tr) tr.scrollIntoView({ block: center ? 'center' : 'nearest' });
}
function step(dir) {
  if (ui.file < 0) { stepFile(dir); if (ui.file >= 0 && dir < 0) focusHunk(groupCount(ui.file) - 1, true); return; }
  let h = ui.hunk + dir;
  if (h < 0 || h >= groupCount(ui.file)) {
    const nf = nextFile(order.indexOf(ui.file), dir);
    if (nf < 0) { toast(dir > 0 ? 'Last change of the last file' : 'First change of the first file'); return; }
    openFile(nf, dir > 0 ? 0 : groupCount(nf) - 1);
    return;
  }
  focusHunk(h, true);
}
function stepFile(dir) {
  const pos = ui.file < 0 || !order.includes(ui.file) ? (dir > 0 ? -1 : order.length) : order.indexOf(ui.file);
  const nf = nextFile(pos, dir);
  if (nf < 0) { toast(dir > 0 ? 'Last file' : 'First file'); return; }
  openFile(nf, 0);
}
// Show a line of a snapshot: switch to a view containing it if needed, open
// the file, unfold the row and scroll to it.
function showLine(fileKey, snap, line, cmId) {
  const fi = fileIndexByKey(fileKey);
  if (fi < 0) { toast('File not in this review: ' + fileKey); return; }
  let [a, b] = ui.view;
  if (snap !== a && snap !== b) { [a, b] = snap === 0 ? [0, 1] : [snap - 1, snap]; setView(a, b); }
  if (fi !== ui.file) { ui.file = fi; ui.hunk = -1; expanded = new Set(); revealFile(fi); saveUI(); renderFiles(); }
  const { rows } = rowsOf(fi);
  const idx = rows.findIndex(r => (snap === a ? r.o : r.n) === line - 1);
  if (idx >= 0) expanded.add(idx);
  renderDiff();
  const target = (cmId && document.querySelector('#bottom .cmt[data-id="' + cmId + '"]')) || (idx >= 0 && document.querySelector('tr[data-row="' + idx + '"]'));
  if (target) { target.scrollIntoView({ block: 'center' }); if (target.tagName === 'TR') { target.classList.add('flash'); } }
  else $('#bottom').scrollTop = 0;
}

// ---- comments: editing -------------------------------------------------------
let editing = null;   // the editor element currently open
function openEditor(anchor, target, side, existing, parent, asNote) {
  closeEditor();
  const ta = el('textarea', { placeholder: existing ? '' : parent ? 'Reply…' : asNote ? 'Note to self…' : 'Comment…' });
  if (existing) ta.value = existing.text;
  const noteCb = parent || (existing && existing.re) ? null : el('input', { type: 'checkbox', class: 'note' });
  if (noteCb) noteCb.checked = existing ? isNote(existing) : !!asNote;
  const box = el('div', { class: 'editor' }, ta,
    el('div', { class: 'row' },
      el('button', { onclick: save }, existing ? 'Update' : parent ? 'Reply' : 'Save'),
      el('button', { onclick: closeEditor }, 'Cancel'),
      noteCb ? el('label', { title: 'Never sent to the agent; listed at the top of the page until resolved' }, noteCb, 'note to self') : '',
      el('span', { class: 'hint' }, 'Ctrl+Enter saves, Esc cancels')));
  ta.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); closeEditor(); }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
    e.stopPropagation();
  });
  function save() {
    const text = ta.value.trim();
    if (!text) { closeEditor(); return; }
    if (existing) { existing.text = text; existing.updated = new Date().toISOString(); if (noteCb) { if (noteCb.checked) existing.kind = 'note'; else delete existing.kind; } }
    else {
      const c = { id: uid(), author: 'reviewer', re: parent ? parent.id : null, file: target.file, snap: target.snap, line: target.line, src: target.src, text, created: new Date().toISOString() };
      if (noteCb && noteCb.checked) c.kind = 'note';
      comments.push(normalize(c));
      if (parent && parent.author === 'agent' && parent.re) { /* a reply to a reply stays on the same thread root */ }
    }
    saveComments(); editing = null; rerenderAll();
  }
  let holder;
  if (anchor.tagName === 'TR') {
    holder = el('tr', { class: 'cmtrow editing' });
    const left = el('td', { colspan: 2 }), right = el('td', { colspan: 2 });
    (side === 'old' ? left : right).append(box);
    holder.append(left, right);
    let after = anchor; while (after.nextSibling && after.nextSibling.classList.contains('cmtrow')) after = after.nextSibling;
    after.after(holder);
  } else if (anchor.classList.contains('cmt') || anchor.classList.contains('thread')) {
    holder = box;
    if (existing && anchor.classList.contains('cmt')) { anchor.replaceWith(holder); holder.restore = anchor; }
    else anchor.append(holder);
  } else {
    holder = box;
    let after = anchor; while (after.nextSibling && after.nextSibling.classList.contains('thread')) after = after.nextSibling;
    after.after(holder);
  }
  editing = holder;
  ta.focus();
}
function closeEditor() {
  if (!editing) return;
  if (editing.restore) editing.replaceWith(editing.restore); else editing.remove();
  editing = null;
}
function statusEl(s) { return s ? el('span', { class: 'stt ' + (STATUSES.includes(s) ? s : 'other') }, s) : ''; }
function commentBox(cm, isReply) {
  const box = el('div', { class: 'cmt ' + whoClass(cm) + (isReply ? ' reply' : ''), dataset: { id: cm.id } });
  if (cm.author !== 'agent') box.append(el('span', { class: 'tools' },
    el('button', { onclick: () => openEditor(box, cm, null, cm) }, 'edit'), ' ',
    el('button', { class: 'danger', onclick: () => deleteComment(cm.id) }, 'delete')));
  box.append(el('span', { class: 'who ' + whoClass(cm) }, whoLabel(cm)), statusEl(cm.status),
    el('span', { class: 'when' }, cm.created ? fmtDate(cm.updated || cm.created) + (cm.updated ? ' (edited)' : '') : (cm.source || '')), el('br'), cm.text);
  if (cm.fixedAt && cm.fixedAt.length) {
    const p = el('div', { class: 'tfoot' }, 'see: ');
    for (const x of cm.fixedAt) p.append(locLink(x), ' ');
    box.append(p);
  }
  return box;
}
// A thread: the root comment, its replies, and a footer with resolve / reply /
// where-did-this-line-go links.
function threadEl(root) {
  const t = el('div', { class: 'thread' + (isResolved(root) ? ' resolved' : ''), dataset: { root: root.id } });
  t.append(commentBox(root, false));
  for (const r of repliesTo(root.id)) t.append(commentBox(r, true));
  const foot = el('div', { class: 'tfoot' });
  if (root.author === 'reviewer') {
    const cb = el('input', { type: 'checkbox', class: 'res' }); cb.checked = isResolved(root);
    cb.addEventListener('change', () => setResolved(root.id, cb.checked));
    foot.append(el('label', null, cb, 'resolved'));
  }
  foot.append(el('a', { onclick: () => openEditor(t, { file: root.file, snap: root.snap, line: root.line, src: root.src }, null, null, root) }, 'reply'));
  if (root.file != null && M > 2 && root.snap != null) {
    const f = files[fileIndexByKey(root.file)];
    if (f) for (let k = root.snap + 1; k < M; k++) {
      if (!f.shas[k]) { foot.append(el('span', { class: 'map' }, snapName(k) + ': file absent')); continue; }
      if (f.shas[k] === f.shas[root.snap]) { foot.append(el('a', { class: 'map', title: 'unchanged', onclick: () => showLine(root.file, k, root.line) }, snapName(k) + ': same')); continue; }
      const m = mapLine(hunksFor(f.shas[root.snap], f.shas[k]), root.line);
      if (m.changed) foot.append(el('a', { class: 'map', onclick: () => showLine(root.file, root.snap, root.line, null) && 0 || showChange(root.file, root.snap, k, root.line) }, snapName(k) + ': changed here'));
      else foot.append(el('a', { class: 'map', onclick: () => showLine(root.file, k, m.line) }, snapName(k) + ': line ' + m.line));
    }
  }
  t.append(foot);
  return t;
}
// A changed line: open the view from its snapshot to k and scroll to the old line's row.
function showChange(fileKey, s, k, line) { setView(s, k); showLine(fileKey, s, line); }
function setResolved(id, on) { if (on) resolved[id] = true; else delete resolved[id]; saveResolved(); rerenderAll(); }
function deleteComment(id) {
  const ids = new Set([id]);
  for (const c of comments) if (c.re && ids.has(c.re)) ids.add(c.id);
  comments = comments.filter(c => !ids.has(c.id)); for (const x of ids) delete resolved[x];
  saveComments(); saveResolved(); rerenderAll();
}
function rerenderAll() {
  renderCommit(); renderFiles();
  if (ui.file >= 0) renderDiff();
  if ($('#panel').classList.contains('open')) renderPanel();
  updateBadge();
}
function updateBadge() {
  const n = comments.length, open = roots().filter(c => c.author === 'reviewer' && !isResolved(c)).length;
  $('#btn-comments').textContent = 'Comments' + (n ? ' (' + n + ')' : '');
  $('#btn-comments').title = open + ' unresolved (C)';
}
function commentOnCurrent(asNote) {
  if (ui.file < 0 || ui.hunk < 0) { toast('No current change'); return; }
  const { rows, groups, old, nw } = rowsOf(ui.file), [a, b] = ui.view;
  if (!groups.length) return;
  const g = groups[ui.hunk];
  let idx = -1, side = 'new';
  for (let k = g.start; k < g.end; k++) if (rows[k].n != null) { idx = k; break; }
  if (idx < 0) { idx = g.start; side = 'old'; }
  const tr = document.querySelector('tr[data-row="' + idx + '"]');
  if (!tr) return;
  const r = rows[idx], f = files[ui.file];
  openEditor(tr, side === 'new' ? { file: f.key, snap: b, line: r.n + 1, src: nw[r.n] } : { file: f.key, snap: a, line: r.o + 1, src: old[r.o] }, side, null, null, asNote);
}

// ---- all-comments panel ------------------------------------------------------
function sortedRoots() {
  const idx = new Map(files.map((f, i) => [f.key, i]));
  return roots().sort((x, y) => {
    const fx = x.file == null ? -1 : (idx.get(x.file) ?? 1e9), fy = y.file == null ? -1 : (idx.get(y.file) ?? 1e9);
    if (fx !== fy) return fx - fy;
    if (x.snap !== y.snap) return (x.snap ?? -1) - (y.snap ?? -1);
    return x.line - y.line;
  });
}
function locText(cm) { return cm.file == null ? (cm.line ? 'message line ' + cm.line : 'whole commit') : snapName(cm.snap) + ' line ' + cm.line; }
function renderPanel() {
  const body = $('#pbody'), pf = $('#pfilter'); body.replaceChildren(); pf.replaceChildren();
  const rs = sortedRoots(), nres = rs.filter(isResolved).length;
  const nnotes = rs.filter(isNote).length;
  $('#ptitle').textContent = rs.length + ' comment' + (rs.length === 1 ? '' : 's') + (nres ? ', ' + nres + ' resolved' : '') + (nnotes ? ', ' + nnotes + ' note' + (nnotes === 1 ? '' : 's') + ' to self' : '');
  pf.append('show: ');
  for (const [k, label] of [['all', 'all'], ['unresolved', 'unresolved'], ['resolved', 'resolved'], ['notes', 'notes to self']])
    pf.append(el('button', { class: ui.filter === k ? 'on' : '', dataset: { filter: k }, onclick: () => { ui.filter = k; saveUI(); renderPanel(); } }, label));
  const orphans = agentComments.filter(c => c.orphan);
  const shown = rs.filter(c => ui.filter === 'all' || (ui.filter === 'notes' ? isNote(c) : (ui.filter === 'resolved') === isResolved(c)));
  if (!shown.length) { body.append(el('div', { id: 'empty' }, rs.length ? 'Nothing ' + ui.filter + '.' : 'No comments yet.')); }
  let lastFile;
  for (const cm of shown) {
    const fname = cm.file == null ? 'Commit ' + DATA.commit.short : cm.file;
    if (fname !== lastFile) { body.append(el('h3', null, fname)); lastFile = fname; }
    const pc = el('div', { class: 'pc' + (isResolved(cm) ? ' resolved' : '') + (isNote(cm) ? ' note' : ''), dataset: { id: cm.id } });
    const loc = el('div', { class: 'loc' }, el('a', { onclick: () => jumpTo(cm) }, locText(cm)),
      el('span', { class: 'who ' + whoClass(cm) }, isNote(cm) ? 'note to self' : cm.author), statusEl(cm.status),
      el('span', { class: 'when' }, cm.created ? fmtDate(cm.updated || cm.created) : ''), el('span', { style: 'flex:1 1 auto' }));
    if (cm.author === 'reviewer') {
      const cb = el('input', { type: 'checkbox', class: 'res' }); cb.checked = isResolved(cm);
      cb.addEventListener('change', () => setResolved(cm.id, cb.checked));
      loc.append(el('label', null, cb, 'resolved'), el('button', { class: 'danger', onclick: () => deleteComment(cm.id) }, 'delete'));
    }
    pc.append(loc);
    if (cm.src) pc.append(el('div', { class: 'src' }, cm.src));
    pc.append(el('div', { class: 'txt' }, cm.text));
    for (const r of repliesTo(cm.id)) {
      const rp = el('div', { class: 'rp ' + r.author }, el('span', { class: 'who ' + r.author }, r.author), statusEl(r.status), r.text);
      for (const x of r.fixedAt || []) rp.append(' → ', locLink(x, closePanel));
      pc.append(rp);
    }
    body.append(pc);
  }
  if (orphans.length) {
    body.append(el('h3', null, 'Agent entries that could not be placed (' + orphans.length + ')'));
    for (const r of orphans) body.append(el('div', { class: 'pc' }, el('div', { class: 'loc' },
      r.re ? 're: ' + r.re + ' (no such comment in this browser)' : 'at: ' + r.at.commit.slice(0, 7) + ' ' + r.at.file + ':' + r.at.line + ' (commit not in this review)',
      statusEl(r.status)), el('div', { class: 'txt' }, r.text)));
  }
}
function jumpTo(cm) {
  closePanel();
  if (cm.file == null) {
    $('#top').scrollTop = 0;
    const box = document.querySelector('#commit .cmt[data-id="' + cm.id + '"]');
    if (box) box.scrollIntoView({ block: 'center' });
    return;
  }
  showLine(cm.file, cm.snap, cm.line, cm.id);
}
function openPanel() { renderPanel(); $('#panel').classList.add('open'); }
function closePanel() { $('#panel').classList.remove('open'); }
function ticks(s) { return s.replace(/`/g, 'ˋ'); }
function asMarkdown() {
  const c = DATA.commit, out = ['# Review of ' + c.short + ' ' + c.subject, ''];
  const rs = sortedRoots();
  section(rs.filter(cm => !isNote(cm)));
  if (rs.some(isNote)) { out.push('# Notes to self', ''); section(rs.filter(isNote)); }
  return out.join('\n');
  function section(list) {
  let lastFile;
  for (const cm of list) {
    const fname = cm.file == null ? 'Commit message' : cm.file;
    if (fname !== lastFile) { out.push('## ' + fname, ''); lastFile = fname; }
    const loc = cm.file == null ? (cm.line ? 'line ' + cm.line : 'general') : 'S' + cm.snap + ':' + cm.line;
    out.push('- **' + loc + '**' + (isResolved(cm) ? ' (resolved)' : '') + (cm.src ? ' `' + ticks(cm.src).trim() + '`' : ''));
    out.push(...cm.text.split('\n').map(l => '  ' + l));
    for (const r of repliesTo(cm.id)) out.push('  - *' + r.author + (r.status ? ' (' + r.status + ')' : '') + ':* ' + r.text.split('\n').join('\n    '));
    out.push('');
  }
  }
}
// The brief: what a coding agent needs to act on the unresolved comments and
// to write a replies file ptal can embed: instructions, the reply schema, and
// the comments as JSON.
function briefComment(cm) {
  const o = { id: cm.id, commit: SN[cm.snap == null ? 1 : cm.snap].sha };
  const ctx = (ls, line) => { const c = {}; for (let k = Math.max(0, line - 4); k < Math.min(ls.length, line + 3); k++) c[k + 1] = ls[k]; return c; };
  if (cm.file == null) {
    if (cm.line) { const c = DATA.commit; o.messageLine = cm.line; o.context = ctx([c.subject, ...(c.body ? ['', ...c.body.split('\n')] : [])], cm.line); }
  } else {
    const f = files[fileIndexByKey(cm.file)];
    o.path = f ? f.paths[cm.snap] || cm.file : cm.file; o.line = cm.line;
    if (f) o.context = ctx(linesAt(f, cm.snap), cm.line);
  }
  o.text = cm.text;
  const rs = repliesTo(cm.id).map(r => Object.assign({ author: r.author }, r.status ? { status: r.status } : {}, { text: r.text }));
  if (rs.length) o.replies = rs;
  return o;
}
function brief() {
  const c = DATA.commit, last = SN[M - 1];
  const rs = sortedRoots().filter(r => r.author === 'reviewer' && !isResolved(r) && !isNote(r));
  const out = ['# Code review of commit ' + c.short + ': ' + c.subject, '',
    'A human reviewer has read commit ' + c.sha + ' in the git repository at `' + c.root + '`' + (M > 2 ? ', together with the follow-up commit' + (M > 3 ? 's' : '') + ' listed below,' : '') + ' and left comments on specific lines. Your job is to act on each comment: change the code in a new commit, or explain why not. You answer in a JSON file, which the reviewer feeds back into the review page together with your commit, so your replies and the places you changed appear next to the comments they answer.',
    '', 'The review is a sequence of snapshots of the repository:', ''];
  SN.forEach((s, k) => out.push('- S' + k + ' = ' + s.sha + (k === 0 ? ' (parent of the reviewed commit)' : k === 1 ? ' (the reviewed commit)' : ' (follow-up ' + (k - 1) + ')') + (s.subject ? ': ' + s.subject : '')));
  out.push('', '## How to respond', '',
    '1. Verify that the currently checked-out branch is at S' + (M - 1) + ': `git rev-parse HEAD` must print `' + last.sha + '`. If it does not, stop and ask the reviewer. Make your changes as one or more new commits on that branch. Never amend, rebase or reset existing commits.',
    '2. Address every comment in the JSON at the end of this brief. Each comment has an `id`, the full hash of the `commit` it was made against, and a location: `path` and `line` in a file at that commit; or `messageLine`, a 1-based line of that commit\'s message as `git show -s --format=%B` prints it (line 1 is the subject); or neither, for the commit as a whole. `context` holds nearby lines of the file or message, keyed by line number. `text` is the reviewer\'s comment and `replies` are the earlier answers in that thread, oldest first, including yours from previous rounds.',
    '3. Write `replies.json` with one entry per comment: `re` is the comment\'s id, `status` is one of fixed, answered, declined or deferred, and `text` says what you changed or why not. When the status is fixed, add `fixedAt`: one or more locations of the fix, each a full `commit` hash (your new commit), a `file` path and a `line` in that file at that commit. An entry with `at` instead of `re` is a note of your own about a location. The file must validate against this JSON schema:', '',
    '```json', JSON.stringify(DATA.replySchema), '```', '',
    '4. Hand back the hash(es) of your new commit(s) and `replies.json`. The reviewer then runs:', '',
    '   `ptal.py ' + c.root + ' ' + c.sha + (M > 2 ? SN.slice(2).map(s => ' --then ' + s.sha).join('') : '') + ' --then <your commit> --replies replies.json`', '',
    '## Comments (' + rs.length + ' unresolved)', '',
    '```json', JSON.stringify({ comments: rs.map(briefComment) }, null, 1), '```', '');
  return out.join('\n');
}
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => toast('Copied'), () => fallback());
  else fallback();
  function fallback() {
    const ta = el('textarea', null, text); document.body.append(ta); ta.select();
    try { document.execCommand('copy'); toast('Copied'); } catch (e) { toast('Copy failed'); } ta.remove();
  }
}
function download(name, text, type) {
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

// ---- top pane sizing ---------------------------------------------------------
const top = $('#top'), splitter = $('#splitter');
function applyTop() {
  if (ui.topHidden) { top.style.height = '0px'; top.style.overflow = 'hidden'; top.style.borderBottom = 'none'; }
  else { top.style.height = ui.topH ? ui.topH + 'px' : '38vh'; top.style.overflow = ''; top.style.borderBottom = ''; }
}
splitter.addEventListener('pointerdown', e => {
  e.preventDefault(); splitter.setPointerCapture(e.pointerId);
  const startY = e.clientY, startH = top.getBoundingClientRect().height;
  const move = ev => { ui.topH = Math.max(0, Math.min(window.innerHeight - 60, startH + ev.clientY - startY)); ui.topHidden = false; applyTop(); };
  const up = () => { splitter.removeEventListener('pointermove', move); splitter.removeEventListener('pointerup', up); saveUI(); };
  splitter.addEventListener('pointermove', move); splitter.addEventListener('pointerup', up);
});
splitter.addEventListener('dblclick', () => { ui.topHidden = !ui.topHidden; saveUI(); applyTop(); });

// ---- keys --------------------------------------------------------------------
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const help = $('#help').classList.contains('open'), panel = $('#panel').classList.contains('open');
  if (e.key === 'Escape') { if (help) $('#help').classList.remove('open'); else if (panel) closePanel(); else closeEditor(); return; }
  if (help || panel) { if (e.key === '?' || e.key === 'C') { $('#help').classList.remove('open'); closePanel(); e.preventDefault(); } return; }
  switch (e.key) {
    case 'n': step(1); break;
    case 'p': step(-1); break;
    case ']': stepFile(1); break;
    case '[': stepFile(-1); break;
    case 'v': cycleView(); break;
    case 'c': commentOnCurrent(false); break;
    case 'N': commentOnCurrent(true); break;
    case 'C': openPanel(); break;
    case 'r': if (ui.file >= 0) setReviewed(ui.file, !isReviewed(files[ui.file])); else toast('No file selected'); break;
    case 'u': toggleUnreviewed(); break;
    case 'w': ui.wrap = !ui.wrap; saveUI(); if (ui.file >= 0) renderDiff(); toast(ui.wrap ? 'Wrapping lines' : 'Not wrapping lines'); break;
    case 'x': if (ui.file >= 0) expandAll(); break;
    case 't': ui.topHidden = !ui.topHidden; saveUI(); applyTop(); break;
    case '?': $('#help').classList.add('open'); break;
    default: return;
  }
  e.preventDefault();
});
$('#btn-comments').onclick = openPanel;
$('#btn-pclose').onclick = closePanel;
$('#btn-help').onclick = () => $('#help').classList.add('open');
$('#btn-hclose').onclick = () => $('#help').classList.remove('open');
$('#btn-copy').onclick = () => copyText(asMarkdown());
$('#btn-brief').onclick = () => copyText(brief());
$('#btn-json').onclick = () => download('review-' + DATA.commit.short + '-comments.json',
  JSON.stringify({ commit: DATA.commit.sha, snaps: SN.map(s => s.sha), comments: comments.slice(), resolved }, null, 2), 'application/json');
$('#btn-delall').onclick = () => {
  if (!comments.length) return;
  if (confirm('Delete all ' + comments.length + ' of your comments for this review? This cannot be undone.')) { comments = []; resolved = {}; saveComments(); saveResolved(); rerenderAll(); toast('All comments deleted'); }
};

// ---- init --------------------------------------------------------------------
applyTop();
computeOrder();
renderCommit(); renderFiles(); updateBadge();
if (ui.file >= files.length) ui.file = -1;
if (ui.file >= 0) { renderDiff(); if (ui.hunk >= 0 && ui.hunk < groupCount(ui.file)) focusHunk(ui.hunk, true); else ui.hunk = -1; }
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("root", help="git repository (passed to git -C)")
    ap.add_argument("commit", help="commit to review (any committish)")
    ap.add_argument("--then", action="append", default=[], metavar="COMMITTISH",
                    help="a follow-up commit (repeatable, in order); adds a snapshot")
    ap.add_argument("--replies", action="append", default=[], metavar="FILE",
                    help="JSON file of a coding agent's replies to the comments (repeatable)")
    ap.add_argument("--comments", metavar="FILE", help="comments export (JSON) to seed the page's storage with")
    ap.add_argument("-o", "--output", help="output file (default: review-<short sha>.html in the current directory; - for stdout)")
    a = ap.parse_args()
    data = build(a.root, a.commit, a.then, load_replies(a.replies), load_seed(a.comments))
    out = a.output or "review-%s.html" % data["commit"]["short"]
    page = render(data)
    if out == "-":
        sys.stdout.write(page)
    else:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(page)
        print("%s  (%d files, %d snapshots, %d bytes)" % (out, len(data["files"]), len(data["snaps"]), len(page.encode("utf-8"))), file=sys.stderr)


if __name__ == "__main__":
    main()
