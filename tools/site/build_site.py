"""Build the trimmed benchmark site (one benchmark page + an annex of every run) and publish it.

    python tools/site/build_site.py                    # build into site_build/sts2
    python tools/site/build_site.py --skip-videos      # pages, replays and raw files only
    python tools/site/build_site.py --publish C:/path/to/site/public/sts2

    python tools/site/build_site.py --renders D:/renders   # game footage from there (default: renders/)

Steps: manifest -> step-by-step replays (annex/replays) -> gzipped raw recordings (annex/raw)
-> game footage of verified renders (annex/game) -> fight replays zipped per run (annex/fights)
-> diagnosis write-up (annex/open-model-diagnosis.html) -> pages (page.py) -> videos (video.py)
-> link check. --publish mirrors the build into the destination, deleting files the build no
longer has, so the published folder holds exactly this site.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(HERE))
import choices  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import replay  # noqa: E402

DIAGNOSIS_DOC = "docs/open-model-diagnosis.md"
DIAGNOSIS_BRANCH = "open-model-diagnosis"
LINK = re.compile(r'''(?:href|src|poster)=["']([^"'#?]+)''')


def fresh(out: Path, src: Path) -> bool:
    return out.exists() and out.stat().st_mtime >= src.stat().st_mtime


def build_replays(data: dict, out: Path) -> int:
    n = 0
    for r in data["runs"]:
        src, dst = Path(r["path"]), out / r["replay"]
        if not fresh(dst, src):
            replay.build(src, dst)
            n += 1
    return n


def build_raw(data: dict, out: Path) -> int:
    n = 0
    for r in data["runs"]:
        src, dst = Path(r["path"]), out / r["raw"]
        if fresh(dst, src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # mtime=0 and no file name in the header, so the same recording gives the same bytes.
        with src.open("rb") as f, open(dst, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
            shutil.copyfileobj(f, gz)
        n += 1
    return n


def same_copy(dst: Path, src: Path) -> bool:
    if not dst.exists():
        return False
    d, s = dst.stat(), src.stat()
    return d.st_size == s.st_size and abs(d.st_mtime - s.st_mtime) < 1  # copy2 carries the mtime over


def build_game(data: dict, out: Path) -> int:
    """Copy each verified render's game.mp4 / poster.jpg into annex/game/<id>.mp4 / .jpg."""
    n = 0
    for r in data["runs"]:
        for key, name in (("game_video", "game.mp4"), ("game_poster", "poster.jpg")):
            if key not in r:
                continue
            src, dst = Path(r["game_dir"]) / name, out / r[key]
            if same_copy(dst, src):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # keeps the mtime, so the next build sees it as current
            n += 1
    return n


FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def fights_zip_bytes(rid: str, folder: Path) -> bytes:
    """A zip of a run's floor_NN.mcr files, the same bytes for the same files: sorted names, fixed
    timestamps and attributes, all under a folder named after the run."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in manifest_mod.mcr_files(folder):
            info = zipfile.ZipInfo(f"{rid}/{f.name}", date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            z.writestr(info, f.read_bytes(), compresslevel=9)
    return buf.getvalue()


def build_fights(data: dict, out: Path) -> int:
    n = 0
    for r in data["runs"]:
        if "fights_zip" not in r:
            continue
        dst = out / r["fights_zip"]
        blob = fights_zip_bytes(r["id"], Path(r["fights_dir"]))
        if dst.exists() and dst.read_bytes() == blob:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        n += 1
    return n


def prune(data: dict, out: Path) -> None:
    """Drop per-run files for runs the manifest no longer has (e.g. a model moved to a newer harness),
    and game footage / fight zips a run no longer has (e.g. its render was redone and diverged)."""
    ids = {r["id"] for r in data["runs"]}
    for sub, suffixes in (("annex/replays", (".html",)), ("annex/raw", (".jsonl.gz",)), ("annex/videos", (".mp4", ".jpg"))):
        folder = out / sub
        for p in folder.glob("*") if folder.is_dir() else []:
            stem = next((p.name[: -len(s)] for s in suffixes if p.name.endswith(s)), None)
            if stem is not None and stem not in ids:
                p.unlink()
    keep = {(out / r[k]).resolve() for r in data["runs"] for k in ("game_video", "game_poster", "fights_zip") if k in r}
    for sub in ("annex/game", "annex/fights"):
        folder = out / sub
        for p in folder.glob("*") if folder.is_dir() else []:
            if p.is_file() and p.resolve() not in keep:
                p.unlink()
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def missing_files(data: dict, out: Path) -> list[str]:
    """Manifest files the pages link to but the build does not have (the generated videos aside)."""
    keys = ("replay", "raw", "game_video", "game_poster", "fights_zip")
    return [r[k] for r in data["runs"] for k in keys if k in r and not (out / r[k]).exists()]


def diagnosis_markdown() -> str | None:
    doc = ROOT / DIAGNOSIS_DOC
    if doc.exists():
        return doc.read_text(encoding="utf-8")
    shown = subprocess.run(["git", "show", f"{DIAGNOSIS_BRANCH}:{DIAGNOSIS_DOC}"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8")
    return shown.stdout if shown.returncode == 0 else None


def build_diagnosis(out: Path) -> bool:
    md = diagnosis_markdown()
    if md is None:
        return False
    import markdown
    body = markdown.markdown(md, extensions=["tables", "fenced_code"])
    body = body.replace("<table>", '<div class="wrap"><table>').replace("</table>", "</table></div>")
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Why open models did worse</title>
<style>
  :root {{ --bg: #fbfaf7; --ink: #111; --muted: #77736b; --line: #d8d3c9; --link: #00c; --code: #f1eee8; color-scheme: light dark; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #121212; --ink: #eee; --muted: #8d8a84; --line: #3a3835; --link: #8ab4ff; --code: #1d1c1a; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 2rem auto; padding: 0 16px; max-width: 52em; font-family: "Times New Roman", Times, serif;
         font-size: 18px; line-height: 1.5; color: var(--ink); background: var(--bg); }}
  a {{ color: var(--link); }}
  h1 {{ font-size: 1.5em; font-weight: normal; }} h2 {{ font-size: 1.2em; margin-top: 2em; }} h3 {{ font-size: 1.05em; }}
  .muted {{ color: var(--muted); }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; font-size: 0.85em; margin: 0.8em 0; }}
  th, td {{ border: 1px solid var(--line); padding: 0.25em 0.5em; text-align: left; vertical-align: top; }}
  code {{ font-size: 0.85em; }}
  pre {{ overflow-x: auto; background: var(--code); padding: 0.6em; }}
</style>
</head>
<body>
<p class="muted"><a href="../">Benchmark</a> · <a href="./">Annex</a></p>
{body}
</body>
</html>
"""
    dst = out / "annex" / "open-model-diagnosis.html"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(page, encoding="utf-8")
    return True


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def broken_links(root: Path) -> list[tuple[Path, str]]:
    broken = []
    for page in root.rglob("*.html"):
        if "replays" in page.parts:
            continue  # replay pages are self-contained
        for link in LINK.findall(page.read_text(encoding="utf-8")):
            if link.startswith(("http:", "https:", "data:", "mailto:", "javascript:")) or not link.strip():
                continue
            target = (page.parent / link).resolve()
            if not target.is_relative_to(root.resolve()):
                continue  # leaves the site (e.g. back to the workspace index)
            if link.endswith("/"):
                target = target / "index.html"
            if not target.exists():
                broken.append((page.relative_to(root), link))
    return broken


def publish(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    want = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    removed = 0
    for p in sorted(dest.rglob("*"), reverse=True):
        rel = p.relative_to(dest)
        if p.is_file() and rel not in want:
            p.unlink()
            removed += 1
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    copied = 0
    for rel in sorted(want):
        s, d = src / rel, dest / rel
        if d.exists() and d.stat().st_size == s.stat().st_size and d.read_bytes() == s.read_bytes():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        copied += 1
    print(f"published to {dest}: {copied} files written, {removed} removed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "site_build" / "sts2")
    ap.add_argument("--skip-videos", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--publish", type=Path, default=None, help="mirror the build into this folder")
    ap.add_argument("--renders", type=Path, default=None,
                    help="game renders folder (default: $STS2_RENDERS, else <repo>/renders)")
    args = ap.parse_args()

    out: Path = args.out
    manifest_path = out.parent / "manifest.json"
    data = manifest_mod.build(args.renders)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"manifest: {len(data['models'])} models, {len(data['runs'])} runs")
    rc = data["renders"]
    if any(rc.values()):
        print(f"renders: {rc['verified']} verified (published), {rc['diverged']} diverged, {rc['failed']} failed")

    prune(data, out)
    print(f"replays: {build_replays(data, out)} built")
    print(f"raw: {build_raw(data, out)} written")
    if any("game_video" in r or "fights_zip" in r for r in data["runs"]):
        print(f"game footage: {build_game(data, out)} files copied")
        print(f"fights: {build_fights(data, out)} zips written")
    (out / "annex" / "open-model-diagnosis.html").unlink(missing_ok=True)  # long write-up retired; doc stays on its branch
    picks = choices.aggregate(data)  # per-recording cache; page.py's Choices section reads the same cache
    (out.parent / "choices.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
    print(f"choices: {len(picks['models'])} models, {picks['runs']} runs ({picks['parsed']} re-read)")
    run("tools/site/page.py", "--manifest", str(manifest_path), "--out", str(out))
    if not args.skip_videos:
        run("tools/site/video.py", "runs", "--manifest", str(manifest_path), "--out", str(out), "--workers", str(args.workers))
        if data.get("game_grid"):  # real footage of the chosen models' runs, once all of it is filmed
            run("tools/site/game_grid.py", "--manifest", str(manifest_path), "--out", str(out))
        else:
            stamp = out.parent / "rollup.grid.json"  # left by game_grid.py: rollup.mp4 is footage, so redo it
            force = ["--force"] if stamp.exists() else []
            stamp.unlink(missing_ok=True)
            run("tools/site/video.py", "rollup", "--manifest", str(manifest_path), "--out", str(out), *force)

    broken = broken_links(out) + [(Path("manifest.json"), f) for f in missing_files(data, out)]
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"site: {out} ({size / 1e6:.1f} MB)")
    for page, link in broken[:20]:
        print(f"broken link: {page} -> {link}")
    if broken:
        print(f"{len(broken)} broken links")
        if args.publish:
            print("not publishing")
        return 1
    if args.publish:
        publish(out, args.publish)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
