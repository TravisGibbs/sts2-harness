"""Copy the built benchmark pages (dashboard, reports, replays) into a static-site folder.

Only HTML is copied: raw JSONL recordings, stdout logs and suite metadata stay local.
Smoke suites are skipped unless --include-smoke is given. Relative links between pages
are checked after copying; the script exits non-zero if any are broken.

    python tools/export_site.py C:/path/to/site/public/some-unlisted-slug
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys

BENCH = Path(__file__).resolve().parents[1] / "bench"
LINK = re.compile(r'''(?:href|src)=["']([^"'#?]+)|"(?:report|replay)": "([^"]+)"''')


def pages(include_smoke):
    for path in BENCH.rglob("*.html"):
        rel = path.relative_to(BENCH)
        if "runs" in rel.parts:
            continue
        if not include_smoke and rel.parts[0].startswith("smoke"):
            continue
        yield rel


def broken_links(root):
    broken = []
    for page in root.rglob("*.html"):
        for a, b in LINK.findall(page.read_text(encoding="utf-8")):
            link = a or b
            if link.startswith(("http:", "https:", "data:", "mailto:", "javascript:")):
                continue
            if not link.endswith(".html"):
                continue
            if not (page.parent / link).resolve().exists():
                broken.append((page.relative_to(root), link))
    return broken


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dest", type=Path)
    ap.add_argument("--include-smoke", action="store_true")
    args = ap.parse_args()
    args.dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel in pages(args.include_smoke):
        target = args.dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BENCH / rel, target)
        count += 1
    broken = broken_links(args.dest)
    size = sum(f.stat().st_size for f in args.dest.rglob("*") if f.is_file())
    print(f"copied {count} pages to {args.dest} ({size / 1e6:.1f} MB)")
    for page, link in broken[:20]:
        print(f"broken link: {page} -> {link}")
    if broken:
        print(f"{len(broken)} broken links")
        sys.exit(1)


if __name__ == "__main__":
    main()
