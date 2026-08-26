# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""Embed one or two traces into a standalone copy of the viewer.

Produces a single HTML file with no external dependencies, suitable for sharing,
archiving alongside a paper, or opening from a USB stick with no install.
"""
import json, pathlib, sys, argparse

HERE = pathlib.Path(__file__).resolve().parent

def bundle(traces, out, title=None):
    html = (HERE / "viewer.html").read_text(encoding="utf-8")
    payload = json.dumps([json.loads(pathlib.Path(t).read_text(encoding="utf-8")) for t in traces],
                         separators=(",", ":"))
    html = html.replace('<script id="embedded" type="application/json">null</script>',
                        f'<script id="embedded" type="application/json">{payload}</script>')
    if title:
        html = html.replace("<title>AirResilience · trace viewer</title>", f"<title>{title}</title>")
    pathlib.Path(out).write_text(html, encoding="utf-8")
    return pathlib.Path(out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("traces", nargs="+", help="one or two .trace.json files")
    ap.add_argument("-o", "--out", default="viewer_bundled.html")
    ap.add_argument("-t", "--title")
    a = ap.parse_args()
    p = bundle(a.traces[:2], a.out, a.title)
    print(f"wrote {p}  ({p.stat().st_size/1024:.0f} KB)")
