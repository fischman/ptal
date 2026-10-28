#!/usr/bin/env python3
"""assemble_files: one entry per file across snapshots, with renames and
re-adds followed and steps that leave a file alone not splitting it.  Builds a
throwaway repository; Python 3 standard library and git only."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptal

def main():
    with tempfile.TemporaryDirectory() as d:
        def sh(*a, **kw): return subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, **kw).stdout.decode().strip()
        def w(name, text): open(os.path.join(d, name), "w").write(text)
        def commit(msg): sh("add", "-A"); sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg); return sh("rev-parse", "HEAD")
        sh("init", "-q")
        w("a.txt", "one\ntwo\n"); w("b.txt", "b\n" * 30); w("gone.txt", "bye\n"); base = commit("base")
        w("a.txt", "one\ntwo\nthree\n"); w("gone.txt", "bye bye\n"); s1 = commit("review me")
        w("a.txt", "one\n2\nthree\n"); os.rename(os.path.join(d, "b.txt"), os.path.join(d, "c.txt")); os.remove(os.path.join(d, "gone.txt")); s2 = commit("round 2")
        w("c.txt", "b\n" * 30 + "more\n"); s3 = commit("round 3: a.txt untouched")
        w("a.txt", "one\n2\n3\n"); w("gone.txt", "back\n"); s4 = commit("round 4: a.txt again, gone.txt re-added")
        data = ptal.build(d, s1, [s2, s3, s4], [], None)
        files = {f["key"]: f for f in data["files"]}
        assert len(data["files"]) == len(files) == 3, [f["paths"] for f in data["files"]]
        a = files["a.txt"]
        assert a["paths"] == ["a.txt"] * 5 and sorted(a["status"]) == [0, 1, 3], a
        b = files["b.txt"]
        assert b["paths"] == ["b.txt", "b.txt", "c.txt", "c.txt", "c.txt"] and b["status"] == {1: "R", 2: "M"}, b
        g = files["gone.txt"]
        assert g["paths"] == ["gone.txt", "gone.txt", None, None, "gone.txt"] and g["status"] == {0: "M", 1: "D", 3: "A"}, g
        assert all(f["shas"][k] for f in data["files"] for k, p in enumerate(f["paths"]) if p)
    print("assemble_files OK")

if __name__ == "__main__":
    main()
