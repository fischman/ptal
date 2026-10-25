#!/usr/bin/env python3
"""ptal: a single-file, portrait-oriented code review page for one git commit.

    ptal.py GIT_ROOT COMMITTISH [-o OUT.html]

Reads the commit with git (GIT_ROOT is anything `git -C` accepts) and writes
one self-contained HTML file: commit details and the list of changed files on
top, a side-by-side before/after view of the selected file below.  Comments
made in the page live in the browser's localStorage under a key prefix that
contains the commit's abbreviated hash, so pages opened from file:// URLs
(which all share one origin) do not see each other's comments, while a page
regenerated for the same commit keeps them.  Image files (png, jpeg, gif,
webp, svg, bmp, ico, avif) are embedded and shown before/after; Android
vector drawables (XML) are converted to SVG for the same purpose.  Each file has
a "reviewed" checkbox, kept in the same storage.

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


def fetch_blobs(root, shas):
    """Return {sha: bytes} for every blob in shas via one `git cat-file --batch`."""
    shas = sorted(s for s in set(shas) if s != ZERO_SHA)
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


def build(root, committish):
    info = commit_info(root, committish)
    changes = raw_changes(root, info["base"], info["sha"])
    blobs = fetch_blobs(root, [s for f in changes for s in (f["oldSha"], f["newSha"])
                               if f["oldMode"] != "160000" and f["newMode"] != "160000"])
    files = []
    for f in changes:
        entry = {k: f[k] for k in ("path", "oldPath", "status", "oldMode", "newMode", "score")}

        def side(sha, mode):
            if sha == ZERO_SHA:
                return b""
            if mode == "160000":
                return ("Subproject commit %s\n" % sha).encode()
            return blobs[sha]
        old, new = side(f["oldSha"], f["oldMode"]), side(f["newSha"], f["newMode"])
        mime = IMAGE_TYPES.get(os.path.splitext(f["path"])[1].lower()) or IMAGE_TYPES.get(os.path.splitext(f["oldPath"])[1].lower())
        if mime and f["oldMode"] != "160000" and f["newMode"] != "160000":
            entry["image"] = mime
            entry["oldImg"] = data_uri(mime, old) if old and len(old) <= IMAGE_MAX else None
            entry["newImg"] = data_uri(mime, new) if new and len(new) <= IMAGE_MAX else None
        elif f["path"].endswith(".xml") or f["oldPath"].endswith(".xml"):
            vo, vn = vector_to_svg(old), vector_to_svg(new)
            if vo or vn:
                entry["image"] = "image/svg+xml"
                entry["oldImg"] = data_uri("image/svg+xml", vo[0]) if vo else None
                entry["newImg"] = data_uri("image/svg+xml", vn[0]) if vn else None
                vinfo = (vn or vo)[1]
                entry["imageNote"] = "vector drawable " + vinfo["dp"]
                entry["plate"] = "dark" if vinfo["light"] else "light"
        entry["binary"] = is_binary(old) or is_binary(new)
        if entry["binary"]:
            entry.update(old=[], new=[], hunks=[])
        else:
            ol, nl = lines_of(old), lines_of(new)
            entry.update(old=ol, new=nl)
            if f["oldSha"] == f["newSha"]:
                entry["hunks"] = []
            elif f["oldSha"] == ZERO_SHA or f["newSha"] == ZERO_SHA or \
                    f["oldMode"] == "160000" or f["newMode"] == "160000":
                entry["hunks"] = [{"os": 1 if ol else 0, "oc": len(ol), "ns": 1 if nl else 0, "nc": len(nl)}] \
                    if (ol or nl) else []
            else:
                entry["hunks"] = hunks_between(root, f["oldSha"], f["newSha"])
        entry["adds"] = sum(h["nc"] for h in entry["hunks"])
        entry["dels"] = sum(h["oc"] for h in entry["hunks"])
        entry["oldSize"], entry["newSize"] = len(old), len(new)
        files.append(entry)
    files.sort(key=lambda e: e["path"].encode("utf-8"))   # byte order, as git lists paths
    return {"commit": info, "files": files,
            "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}


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
  --cmt: #fffbe6; --cmtb: #e3c800; --btn: #f3f4f6; --danger: #c62828;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #d7dde3; --dim: #8b949e; --line: #30363d; --pane: #161b22;
    --accent: #58a6ff; --sel: #1f2d3d; --cur: #e3b341;
    --del: #3a1d1f; --del2: #7a2b2f; --add: #17311f; --add2: #2b6a3a; --chg: #2b2a1c;
    --cmt: #2a2713; --cmtb: #9e8a00; --btn: #21262d; --danger: #ff7b72;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font: 13px/1.4 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       display: flex; flex-direction: column; overflow: hidden; }
button { font: inherit; color: inherit; background: var(--btn); border: 1px solid var(--line);
         border-radius: 4px; padding: 2px 8px; cursor: pointer; }
button:hover { border-color: var(--accent); }
button.danger { color: var(--danger); }
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
.msg { margin: 6px 0; }
.mline { display: flex; align-items: flex-start; }
.mline .gut, .ln { flex: 0 0 auto; color: var(--dim); cursor: pointer; user-select: none; text-align: right; }
.mline .gut { width: 1.6em; min-height: 1.4em; align-self: stretch; margin-right: 6px; font-family: var(--mono); font-size: 12px; }
.mline:hover .gut::before, td.ln:hover::before { content: "+"; color: var(--accent); font-weight: bold; }
.mline .gut.has::before, td.ln.has::before { content: "\25CF"; color: var(--cmtb); }
.mline .txt { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); font-size: 12px; }
.mline.subject .txt { font-weight: 600; font-size: 13px; }
.mline.sel { background: var(--sel); }
.actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }

#files { border-top: 1px solid var(--line); padding-bottom: 6px; }
#files .hdr { padding: 6px 8px 2px; color: var(--dim); font-size: 12px; display: flex; align-items: center; }
#files .hdr .stat { margin-left: auto; }
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
#files .hdr button.small { padding: 0 6px; font-size: 11px; margin: 0 8px; }
#files .progress { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding: 2px 8px 6px; font-size: 12px; color: var(--dim); }
#files .progress .lbl { color: var(--dim); }
#files .progress b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
#files .meter { display: inline-flex; gap: 6px; align-items: center; }
#files .bar { display: inline-block; width: 72px; height: 7px; border-radius: 4px; background: var(--line); overflow: hidden; }
#files .fill { display: block; height: 100%; background: var(--accent); border-radius: 4px; transition: width .15s; }
#files .hdr button.on { border-color: var(--accent); color: var(--accent); }
.st { font-family: var(--mono); font-size: 11px; font-weight: 700; display: inline-block; min-width: 1.3em; text-align: center;
      border-radius: 3px; padding: 0 2px; margin-right: 5px; }
.st.A { background: var(--add2); } .st.D { background: var(--del2); } .st.M { background: var(--chg); }
.st.R, .st.C, .st.T, .st.U, .st.X { background: var(--sel); }
.fcell .dir { color: var(--dim); }
.stat .a { color: #1a7f37; } .stat .d { color: #cf222e; }
@media (prefers-color-scheme: dark) { .stat .a { color: #3fb950; } .stat .d { color: #f85149; } }
.badge { font-size: 11px; font-family: system-ui, sans-serif; background: var(--cmtb); color: #000; border-radius: 8px; padding: 0 6px; margin-left: 6px; }
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
tr.fold td { background: var(--pane); color: var(--accent); text-align: center; cursor: pointer; padding: 2px; font-size: 11px;
             border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); user-select: none; }
tr.fold:hover td { text-decoration: underline; }
tr.cmtrow td { padding: 0; background: var(--bg); }
.cmt { margin: 3px 4px; padding: 5px 8px; background: var(--cmt); border: 1px solid var(--cmtb); border-left-width: 4px;
       border-radius: 4px; font-family: system-ui, sans-serif; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
.cmt .tools { float: right; margin-left: 8px; }
.cmt .tools button { padding: 0 5px; font-size: 11px; }
.cmt .when { color: var(--dim); font-size: 11px; }
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
#panel .pbody { flex: 1 1 auto; overflow: auto; padding: 8px; }
#panel h3 { font-size: 12px; font-family: var(--mono); margin: 12px 0 4px; color: var(--dim); overflow-wrap: anywhere; }
#panel .pc { margin: 4px 0 8px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 4px; }
#panel .pc .loc { font-family: var(--mono); font-size: 11px; color: var(--dim); display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
#panel .pc .loc a { cursor: pointer; }
#panel .pc .src { font-family: var(--mono); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--dim);
                  border-left: 2px solid var(--line); padding-left: 6px; margin: 3px 0; }
#panel .pc .txt { white-space: pre-wrap; overflow-wrap: anywhere; }
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
    <button id="btn-copy">Copy as Markdown</button>
    <button id="btn-json">Download JSON</button>
    <button id="btn-delall" class="danger">Delete all</button>
    <button id="btn-pclose">Close</button>
  </div>
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
      <tr><td><kbd>c</kbd></td><td>comment on the current change (after side; before side if nothing was added)</td></tr>
      <tr><td><kbd>C</kbd></td><td>show all comments</td></tr>
      <tr><td><kbd>r</kbd></td><td>mark / unmark the current file as reviewed (the checkbox in the file list)</td></tr>
      <tr><td><kbd>u</kbd></td><td>show only unreviewed files; <kbd>n</kbd>/<kbd>p</kbd> and <kbd>]</kbd>/<kbd>[</kbd> then skip reviewed ones</td></tr>
      <tr><td><kbd>w</kbd></td><td>toggle line wrapping</td></tr>
      <tr><td><kbd>x</kbd></td><td>expand all folded unchanged regions in this file</td></tr>
      <tr><td><kbd>t</kbd></td><td>toggle the top pane</td></tr>
      <tr><td><kbd>?</kbd></td><td>this help</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>close overlays / cancel editing</td></tr>
      <tr><td><kbd>Ctrl</kbd>+<kbd>Enter</kbd></td><td>save the comment being edited</td></tr>
    </table>
    <p>Click a line number (either side) or the gutter of a commit-message line to attach a comment.
       Comments are stored in this browser's localStorage under a prefix made from the commit's abbreviated hash,
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

// ---- storage -----------------------------------------------------------------
const store = {
  get(k, d) { try { const v = localStorage.getItem(PREFIX + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(PREFIX + k, JSON.stringify(v)); } catch (e) { toast('localStorage unavailable: ' + e.message); } },
};
let comments = store.get('comments', []);
let reviewed = store.get('reviewed', {});   // fileKey -> true
const ui = Object.assign({ file: -1, hunk: -1, topH: null, wrap: true, topHidden: false, collapsed: {}, unreviewedOnly: false }, store.get('ui', {}));
function saveComments() { store.set('comments', comments); }
function saveReviewed() { store.set('reviewed', reviewed); }
function saveUI() { store.set('ui', ui); }
window.addEventListener('storage', e => {
  if (e.key === PREFIX + 'comments') { comments = store.get('comments', []); renderCommit(); renderFiles(); if (ui.file >= 0) renderDiff(); }
  if (e.key === PREFIX + 'reviewed') { reviewed = store.get('reviewed', {}); renderFiles(); }
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
function fileKey(f) { return f.status === 'D' ? f.oldPath : f.path; }
function commentsFor(fileKeyOrNull, side, line) {
  return comments.filter(c => c.file === fileKeyOrNull && c.side === side && c.line === line);
}
function countFor(fileKeyOrNull) { return comments.filter(c => c.file === fileKeyOrNull).length; }

// ---- rows & hunks ------------------------------------------------------------
const files = DATA.files;
const rowCache = new Map();
function rowsOf(fi) {
  if (rowCache.has(fi)) return rowCache.get(fi);
  const f = files[fi], rows = [], groups = [];
  let o = 0, n = 0, g = null, lastEnd = -Infinity;
  for (const h of f.hunks) {
    const os = h.oc ? h.os - 1 : h.os, ns = h.nc ? h.ns - 1 : h.ns;
    while (o < os && n < ns) rows.push({ t: 'eq', o: o++, n: n++ });
    if (o !== os || n !== ns) { console.warn('hunk misaligned', f.path, h, o, n); }
    const start = rows.length;
    if (!g || start - lastEnd > MERGE_GAP) { g = { start, end: start, idx: groups.length }; groups.push(g); }
    const both = Math.min(h.oc, h.nc);
    for (let i = 0; i < both; i++) rows.push({ t: 'chg', o: o++, n: n++, g: g.idx });
    for (let i = both; i < h.oc; i++) rows.push({ t: 'del', o: o++, n: null, g: g.idx });
    for (let i = both; i < h.nc; i++) rows.push({ t: 'add', o: null, n: n++, g: g.idx });
    g.end = lastEnd = rows.length;
  }
  while (o < f.old.length && n < f.new.length) rows.push({ t: 'eq', o: o++, n: n++ });
  while (o < f.old.length) rows.push({ t: 'del', o: o++, n: null });
  while (n < f.new.length) rows.push({ t: 'add', o: null, n: n++ });
  const r = { rows, groups };
  rowCache.set(fi, r);
  return r;
}
function groupCount(fi) { return Math.max(1, rowsOf(fi).groups.length); }

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
  meta.append(el('br'), el('span', null, c.root + ' \u00b7 generated ' + fmtDate(DATA.generated)));
  box.append(meta);
  const msg = el('div', { class: 'msg' });
  const lines = [c.subject, ...(c.body ? ['', ...c.body.split('\n')] : [])];
  lines.forEach((text, i) => {
    const ln = i + 1, has = commentsFor(null, 'commit', ln).length > 0;
    const line = el('div', { class: 'mline' + (i === 0 ? ' subject' : ''), dataset: { line: ln } },
      el('span', { class: 'gut' + (has ? ' has' : ''), title: 'Comment on this line', onclick: () => openEditor(line, { file: null, side: 'commit', line: ln, src: text }) }),
      el('span', { class: 'txt' }, text || '\u00a0'));
    msg.append(line);
    for (const cm of commentsFor(null, 'commit', ln)) msg.append(commentBox(cm));
  });
  box.append(msg);
  const general = el('div', { class: 'actions' },
    el('button', { onclick: () => openEditor(general, { file: null, side: 'commit', line: 0, src: c.subject }) }, '+ comment on the commit'));
  box.append(general);
  for (const cm of commentsFor(null, 'commit', 0)) box.append(commentBox(cm));
}

// ---- top pane: files ---------------------------------------------------------
// files[] is in byte order of path, which is also the order of a tree walk that
// sorts each directory's entries by name with "/" appended to directory names;
// the walk below does that, so list order and n/p order agree.  Single-child
// directory chains are shown as one "a/b/c" row.
const TREE = (() => {
  const root = { name: '', path: '', dirs: [], files: [] };
  files.forEach((f, i) => {
    let node = root;
    for (const part of f.path.split('/').slice(0, -1)) {
      let d = node.dirs.find(d => d.name === part);
      if (!d) { d = { name: part, path: node.path + part + '/', dirs: [], files: [] }; node.dirs.push(d); }
      node = d;
    }
    node.files.push(i);
  });
  (function compress(node) {
    node.dirs = node.dirs.map(d => { while (!d.files.length && d.dirs.length === 1) { const c = d.dirs[0]; d = { name: d.name + '/' + c.name, path: c.path, dirs: c.dirs, files: c.files }; } return d; });
    node.dirs.forEach(compress);
  })(root);
  return root;
})();
function renderFiles() {
  const box = $('#files');
  box.replaceChildren();
  const adds = files.reduce((s, f) => s + f.adds, 0), dels = files.reduce((s, f) => s + f.dels, 0);
  const nrev = files.filter(isReviewed).length;
  box.append(el('div', { class: 'hdr' },
    el('span', null, files.length + ' file' + (files.length === 1 ? '' : 's') + ' changed'),
    el('button', { class: 'small' + (ui.unreviewedOnly ? ' on' : ''), title: 'Show only unreviewed files (u)', onclick: toggleUnreviewed }, ui.unreviewedOnly ? 'unreviewed only' : 'all files'),
    el('span', { class: 'stat' }, el('span', { class: 'a' }, '+' + adds), ' ', el('span', { class: 'd' }, '−' + dels))));
  box.append(progressEl(nrev));
  const tbody = el('tbody');
  const shown = i => !(ui.unreviewedOnly && isReviewed(files[i]));
  (function walk(node, depth, hidden) {
    const entries = [...node.dirs.map(d => ({ key: d.name + '/', d })), ...node.files.map(i => ({ key: files[i].path.slice(files[i].path.lastIndexOf('/') + 1), i }))];
    entries.sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    for (const { d, i } of entries) {
     if (d) {
      const collapsed = !!ui.collapsed[d.path];
      const anyShown = files.some((f, i) => f.path.startsWith(d.path) && shown(i));
      tbody.append(el('tr', { class: 'dir' + (hidden || !anyShown ? ' hidden' : ''), dataset: { dir: d.path }, onclick: () => { if (collapsed) delete ui.collapsed[d.path]; else ui.collapsed[d.path] = true; saveUI(); renderFiles(); } },
        el('td', { class: 'fcell', colspan: 2, style: 'padding-left:' + (8 + depth * 14) + 'px' },
          el('span', { class: 'caret' }, collapsed ? '▶' : '▼'), d.name + '/')));
      walk(d, depth + 1, hidden || collapsed);
      continue;
     }
      const f = files[i], base = f.path.slice(f.path.lastIndexOf('/') + 1);
      const cb = el('input', { type: 'checkbox', class: 'rev', title: 'Reviewed (r toggles the current file)' });
      cb.checked = isReviewed(f);
      cb.addEventListener('click', e => { e.stopPropagation(); setReviewed(i, cb.checked); });
      const cell = el('td', { class: 'fcell', style: 'padding-left:' + (8 + depth * 14) + 'px' }, cb, el('span', { class: 'st ' + f.status, title: statusName(f.status) }, f.status));
      if (f.status === 'R' || f.status === 'C') {
        const sameDir = f.oldPath.slice(0, f.oldPath.lastIndexOf('/') + 1) === f.path.slice(0, f.path.lastIndexOf('/') + 1);
        cell.append(el('span', { class: 'dir' }, (sameDir ? f.oldPath.slice(f.oldPath.lastIndexOf('/') + 1) : f.oldPath) + ' → '));
      }
      cell.append(base);
      if (f.oldMode !== f.newMode && f.status !== 'A' && f.status !== 'D') cell.append(el('span', { class: 'dir' }, ' ' + f.oldMode + '→' + f.newMode));
      const cnt = countFor(fileKey(f));
      if (cnt) cell.append(el('span', { class: 'badge' }, cnt));
      const stat = el('td', { class: 'stat' });
      if (f.image && !f.hunks.length) stat.append('image');
      else if (f.binary) stat.append('binary');
      else stat.append(el('span', { class: 'a' }, '+' + f.adds), ' ', el('span', { class: 'd' }, '−' + f.dels));
      tbody.append(el('tr', { class: 'file' + (i === ui.file ? ' sel' : '') + (isReviewed(f) ? ' reviewed' : '') + (hidden || !shown(i) ? ' hidden' : ''),
        dataset: { fi: i }, title: f.path, onclick: () => openFile(i, 0) }, cell, stat));
    }
  })(TREE, 0, false);
  box.append(el('table', { id: 'ftree' }, tbody));
  if (ui.unreviewedOnly && nrev === files.length) box.append(el('div', { id: 'empty' }, 'Every file is marked reviewed.'));
}
function isReviewed(f) { return !!reviewed[fileKey(f)]; }
// Progress: share of files reviewed, and share of diff lines (added + deleted,
// text files only) in reviewed files, so big files weigh more.  Percentages
// round, but 100% means complete and anything short of it reads at most 99%.
function pct(n, d) { return !d ? 0 : n === d ? 100 : Math.min(99, Math.round(100 * n / d)); }
function progressEl(nrev) {
  const text = files.filter(f => !f.binary);
  const weight = f => f.adds + f.dels;
  const total = text.reduce((s, f) => s + weight(f), 0), done = text.filter(isReviewed).reduce((s, f) => s + weight(f), 0);
  const meter = (label, n, d, unit) => el('span', { class: 'meter', title: n + ' of ' + d + ' ' + unit },
    el('span', { class: 'bar' }, el('span', { class: 'fill', style: 'width:' + pct(n, d) + '%' })),
    el('b', null, pct(n, d) + '%'), ' of ' + label);
  return el('div', { class: 'progress', id: 'progress' }, el('span', { class: 'lbl' }, 'reviewed'), meter('files', nrev, files.length, 'files reviewed'),
    total ? meter('lines', done, total, 'diff lines in reviewed files (binary files not counted)') : null);
}
function setReviewed(fi, on) {
  const k = fileKey(files[fi]);
  if (on) reviewed[k] = true; else delete reviewed[k];
  saveReviewed(); renderFiles();
  toast((on ? 'Reviewed: ' : 'Unreviewed: ') + files[fi].path);
}
function toggleUnreviewed() {
  ui.unreviewedOnly = !ui.unreviewedOnly; saveUI(); renderFiles();
  toast(ui.unreviewedOnly ? 'Showing unreviewed files only' : 'Showing all files');
}
// where n/p and ]/[ may land: every file, or only the unreviewed ones while filtering
function navigable(fi) { return !(ui.unreviewedOnly && isReviewed(files[fi])); }
function nextFile(from, dir) {
  for (let i = from + dir; i >= 0 && i < files.length; i += dir) if (navigable(i)) return i;
  return -1;
}
function revealFile(fi) {
  // expand every collapsed directory above files[fi]
  for (const dir of Object.keys(ui.collapsed)) if (files[fi].path.startsWith(dir)) delete ui.collapsed[dir];
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
  const f = files[ui.file], bottom = $('#bottom');
  bottom.replaceChildren();
  const { rows, groups } = rowsOf(ui.file);
  const head = el('div', { id: 'filehead' },
    el('span', { class: 'st ' + f.status, title: statusName(f.status) }, f.status),
    el('span', { class: 'p' }, (f.status === 'R' || f.status === 'C' ? f.oldPath + ' \u2192 ' : '') + f.path),
    el('span', { class: 'hk', id: 'hunkpos' }),
    el('button', { onclick: () => { ui.wrap = !ui.wrap; saveUI(); renderDiff(); }, title: 'Toggle wrapping (w)' }, ui.wrap ? 'wrap' : 'no wrap'),
    el('button', { onclick: expandAll, title: 'Expand folded regions (x)' }, 'expand'));
  bottom.append(head);
  if (f.image) {
    const side = (uri, size, label) => el('div', { class: 'img' },
      uri ? el('img', { src: uri, alt: label }) : el('div', { class: 'none' }, size ? 'image too large to embed' : 'no ' + label + ' image'),
      el('span', null, size ? label + ': ' + fmtSize(size) : ''));
    bottom.append(el('div', { class: 'images' + (f.plate === 'dark' ? ' dark' : '') }, side(f.oldImg, f.oldSize, 'before'), side(f.newImg, f.newSize, 'after'),
      f.imageNote ? el('div', { class: 'note' }, f.imageNote + (f.plate === 'dark' ? ' (light colours: shown on a dark plate)' : '')) : null));
    if (f.binary) { updateHunkPos(); return; }
  }
  if (f.binary) {
    bottom.append(el('div', { id: 'empty' }, 'Binary file: ' + fmtSize(f.oldSize) + ' \u2192 ' + fmtSize(f.newSize) + '.'));
    updateHunkPos(); return;
  }
  if (!rows.length) { bottom.append(el('div', { id: 'empty' }, 'Empty file.')); updateHunkPos(); return; }
  const key = fileKey(f);
  const table = el('table', { class: 'diff' + (ui.wrap ? '' : ' nowrap') },
    el('colgroup', null, el('col', { class: 'ln' }), el('col'), el('col', { class: 'ln' }), el('col')));
  const tbody = el('tbody');
  const interesting = rows.map(r => r.t !== 'eq' || hasComment(key, r));
  // fold runs of uninteresting rows, keeping FOLD_CTX next to interesting ones
  let i = 0;
  while (i < rows.length) {
    if (interesting[i] || expanded.has(i)) { tbody.append(rowEl(f, key, rows[i], i)); i++; continue; }
    let j = i; while (j < rows.length && !interesting[j] && !expanded.has(j)) j++;
    const lead = i === 0 ? 0 : FOLD_CTX, trail = j === rows.length ? 0 : FOLD_CTX;
    if (j - i - lead - trail >= FOLD_MIN) {
      for (let k = i; k < i + lead; k++) tbody.append(rowEl(f, key, rows[k], k));
      const from = i + lead, to = j - trail;
      tbody.append(el('tr', { class: 'fold', dataset: { from, to } }, el('td', { colspan: 4, onclick: () => { for (let k = from; k < to; k++) expanded.add(k); renderDiff(); } },
        '\u22ef ' + (to - from) + ' unchanged lines \u22ef')));
      for (let k = to; k < j; k++) tbody.append(rowEl(f, key, rows[k], k));
    } else for (let k = i; k < j; k++) tbody.append(rowEl(f, key, rows[k], k));
    i = j;
  }
  table.append(tbody);
  bottom.append(table);
  markCurrent(); updateHunkPos();
}
function hasComment(key, r) {
  return (r.o != null && commentsFor(key, 'old', r.o + 1).length) || (r.n != null && commentsFor(key, 'new', r.n + 1).length);
}
function rowEl(f, key, r, idx) {
  const frag = document.createDocumentFragment();
  const tr = el('tr', { class: r.t + (r.g != null && r.g === ui.hunk ? ' cur' : ''), dataset: { row: idx, g: r.g == null ? '' : r.g } });
  const oText = r.o != null ? f.old[r.o] : null, nText = r.n != null ? f.new[r.n] : null;
  const oc = r.o != null ? commentsFor(key, 'old', r.o + 1) : [], nc = r.n != null ? commentsFor(key, 'new', r.n + 1) : [];
  const olm = el('td', { class: 'ln o' + (oc.length ? ' has' : ''), title: r.o != null ? 'Comment on old line ' + (r.o + 1) : null }, r.o != null ? String(r.o + 1) : '');
  const nln = el('td', { class: 'ln n' + (nc.length ? ' has' : ''), title: r.n != null ? 'Comment on new line ' + (r.n + 1) : null }, r.n != null ? String(r.n + 1) : '');
  if (r.o != null) olm.onclick = () => openEditor(tr, { file: key, side: 'old', line: r.o + 1, src: oText }, 'old');
  if (r.n != null) nln.onclick = () => openEditor(tr, { file: key, side: 'new', line: r.n + 1, src: nText }, 'new');
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
  (side === 'old' ? left : right).append(commentBox(cm));
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
  // make sure the hunk rows are not inside a fold (they never are: changed rows are interesting)
  markCurrent(); updateHunkPos();
  const tr = document.querySelector('tr[data-row="' + g.start + '"]');
  if (tr) tr.scrollIntoView({ block: center ? 'center' : 'nearest' });
}
function step(dir) {
  if (ui.file < 0) { stepFile(dir); if (ui.file >= 0 && dir < 0) focusHunk(groupCount(ui.file) - 1, true); return; }
  let h = ui.hunk + dir;
  if (h < 0 || h >= groupCount(ui.file)) {
    const nf = nextFile(ui.file, dir);
    if (nf < 0) { toast(dir > 0 ? 'Last change of the last file' : 'First change of the first file'); return; }
    openFile(nf, dir > 0 ? 0 : groupCount(nf) - 1);
    return;
  }
  focusHunk(h, true);
}
function stepFile(dir) {
  const nf = nextFile(ui.file < 0 ? (dir > 0 ? -1 : files.length) : ui.file, dir);
  if (nf < 0) { toast(dir > 0 ? 'Last file' : 'First file'); return; }
  openFile(nf, 0);
}

// ---- comments ----------------------------------------------------------------
let editing = null;   // the editor element currently open
function openEditor(anchor, target, side, existing) {
  closeEditor();
  const ta = el('textarea', { placeholder: existing ? '' : 'Comment\u2026' });
  if (existing) ta.value = existing.text;
  const box = el('div', { class: 'editor' }, ta,
    el('div', { class: 'row' },
      el('button', { onclick: save }, existing ? 'Update' : 'Save'),
      el('button', { onclick: closeEditor }, 'Cancel'),
      el('span', { class: 'hint' }, 'Ctrl+Enter saves, Esc cancels')));
  ta.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); closeEditor(); }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
    e.stopPropagation();
  });
  function save() {
    const text = ta.value.trim();
    if (!text) { closeEditor(); return; }
    if (existing) { existing.text = text; existing.updated = new Date().toISOString(); }
    else comments.push({ id: uid(), file: target.file, side: target.side, line: target.line, src: target.src, text, created: new Date().toISOString() });
    saveComments(); editing = null; rerenderAll();
  }
  let holder;
  if (anchor.tagName === 'TR') {
    holder = el('tr', { class: 'cmtrow editing' });
    const left = el('td', { colspan: 2 }), right = el('td', { colspan: 2 });
    (side === 'old' ? left : right).append(box);
    holder.append(left, right);
    // place after the row and after any existing comment rows that belong to it
    let after = anchor; while (after.nextSibling && after.nextSibling.classList.contains('cmtrow')) after = after.nextSibling;
    after.after(holder);
  } else {
    holder = box;
    let after = anchor; while (after.nextSibling && after.nextSibling.classList.contains('cmt')) after = after.nextSibling;
    if (existing && anchor.classList.contains('cmt')) { anchor.replaceWith(holder); holder.restore = anchor; }
    else after.after(holder);
  }
  editing = holder;
  ta.focus();
}
function closeEditor() {
  if (!editing) return;
  if (editing.restore) editing.replaceWith(editing.restore); else editing.remove();
  editing = null;
}
function commentBox(cm) {
  const box = el('div', { class: 'cmt', dataset: { id: cm.id } });
  box.append(el('span', { class: 'tools' },
    el('button', { onclick: () => openEditor(box, cm, cm.side, cm) }, 'edit'), ' ',
    el('button', { class: 'danger', onclick: () => deleteComment(cm.id) }, 'delete')));
  box.append(el('span', { class: 'when' }, fmtDate(cm.updated || cm.created) + (cm.updated ? ' (edited)' : '')), el('br'), cm.text);
  return box;
}
function deleteComment(id) {
  comments = comments.filter(c => c.id !== id); saveComments(); rerenderAll();
}
function rerenderAll() {
  renderCommit(); renderFiles();
  if (ui.file >= 0) renderDiff();
  if ($('#panel').classList.contains('open')) renderPanel();
  updateBadge();
}
function updateBadge() { $('#btn-comments').textContent = 'Comments' + (comments.length ? ' (' + comments.length + ')' : ''); }
function commentOnCurrent() {
  if (ui.file < 0 || ui.hunk < 0) { toast('No current change'); return; }
  const { rows, groups } = rowsOf(ui.file);
  if (!groups.length) return;
  const g = groups[ui.hunk];
  let idx = -1, side = 'new';
  for (let k = g.start; k < g.end; k++) if (rows[k].n != null) { idx = k; break; }
  if (idx < 0) { idx = g.start; side = 'old'; }
  const tr = document.querySelector('tr[data-row="' + idx + '"]');
  if (!tr) return;
  const r = rows[idx], f = files[ui.file], key = fileKey(f);
  openEditor(tr, side === 'new' ? { file: key, side: 'new', line: r.n + 1, src: f.new[r.n] } : { file: key, side: 'old', line: r.o + 1, src: f.old[r.o] }, side);
}

// ---- all-comments panel ------------------------------------------------------
function sortedComments() {
  const order = new Map(files.map((f, i) => [fileKey(f), i]));
  return comments.slice().sort((a, b) => {
    const fa = a.file == null ? -1 : (order.get(a.file) ?? 1e9), fb = b.file == null ? -1 : (order.get(b.file) ?? 1e9);
    if (fa !== fb) return fa - fb;
    if (a.side !== b.side) return a.side === 'old' ? -1 : 1;
    return a.line - b.line;
  });
}
function renderPanel() {
  const body = $('#pbody'); body.replaceChildren();
  $('#ptitle').textContent = comments.length + ' comment' + (comments.length === 1 ? '' : 's');
  if (!comments.length) { body.append(el('div', { id: 'empty' }, 'No comments yet.')); return; }
  let lastFile;
  for (const cm of sortedComments()) {
    const fname = cm.file == null ? 'Commit ' + DATA.commit.short : cm.file;
    if (fname !== lastFile) { body.append(el('h3', null, fname)); lastFile = fname; }
    const loc = cm.file == null ? (cm.line ? 'message line ' + cm.line : 'whole commit') : cm.side + ' line ' + cm.line;
    body.append(el('div', { class: 'pc' },
      el('div', { class: 'loc' }, el('a', { onclick: () => jumpTo(cm) }, loc), el('span', { class: 'when' }, fmtDate(cm.updated || cm.created)),
        el('span', { style: 'flex:1 1 auto' }), el('button', { class: 'danger', onclick: () => deleteComment(cm.id) }, 'delete')),
      cm.src != null && cm.src !== '' ? el('div', { class: 'src' }, cm.src) : null,
      el('div', { class: 'txt' }, cm.text)));
  }
}
function jumpTo(cm) {
  closePanel();
  if (cm.file == null) {
    $('#top').scrollTop = 0;
    const box = document.querySelector('.cmt[data-id="' + cm.id + '"]');
    if (box) box.scrollIntoView({ block: 'center' });
    return;
  }
  const fi = files.findIndex(f => fileKey(f) === cm.file);
  if (fi < 0) { toast('File not in this commit: ' + cm.file); return; }
  if (fi !== ui.file) { ui.file = fi; ui.hunk = -1; expanded = new Set(); saveUI(); renderFiles(); renderDiff(); }
  const box = document.querySelector('#bottom .cmt[data-id="' + cm.id + '"]');
  if (box) box.scrollIntoView({ block: 'center' });
}
function openPanel() { renderPanel(); $('#panel').classList.add('open'); }
function closePanel() { $('#panel').classList.remove('open'); }
function asMarkdown() {
  const c = DATA.commit, out = ['# Review of ' + c.short + ' ' + c.subject, ''];
  let lastFile;
  for (const cm of sortedComments()) {
    const fname = cm.file == null ? 'Commit message' : cm.file;
    if (fname !== lastFile) { out.push('## ' + fname, ''); lastFile = fname; }
    const loc = cm.file == null ? (cm.line ? 'line ' + cm.line : 'general') : cm.side + ':' + cm.line;
    out.push('- **' + loc + '**' + (cm.src ? ' `' + cm.src.replace(/`/g, '\u02cb').trim() + '`' : ''));
    out.push(...cm.text.split('\n').map(l => '  ' + l), '');
  }
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
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const help = $('#help').classList.contains('open'), panel = $('#panel').classList.contains('open');
  if (e.key === 'Escape') { if (help) $('#help').classList.remove('open'); else if (panel) closePanel(); else closeEditor(); return; }
  if (help || panel) { if (e.key === '?' || e.key === 'C') { $('#help').classList.remove('open'); closePanel(); e.preventDefault(); } return; }
  switch (e.key) {
    case 'n': step(1); break;
    case 'p': step(-1); break;
    case ']': stepFile(1); break;
    case '[': stepFile(-1); break;
    case 'c': commentOnCurrent(); break;
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
$('#btn-json').onclick = () => download('review-' + DATA.commit.short + '-comments.json',
  JSON.stringify({ commit: DATA.commit.sha, comments: sortedComments() }, null, 2), 'application/json');
$('#btn-delall').onclick = () => {
  if (!comments.length) return;
  if (confirm('Delete all ' + comments.length + ' comments for this review? This cannot be undone.')) { comments = []; saveComments(); rerenderAll(); toast('All comments deleted'); }
};

// ---- init --------------------------------------------------------------------
applyTop();
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
    ap.add_argument("-o", "--output", help="output file (default: review-<short sha>.html in the current directory; - for stdout)")
    a = ap.parse_args()
    data = build(a.root, a.commit)
    out = a.output or "review-%s.html" % data["commit"]["short"]
    page = render(data)
    if out == "-":
        sys.stdout.write(page)
    else:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(page)
        print("%s  (%d files, %d bytes)" % (out, len(data["files"]), len(page.encode("utf-8"))), file=sys.stderr)


if __name__ == "__main__":
    main()
