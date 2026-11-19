"""The benchmark page's grid video, made of real game footage.

    python tools/site/game_grid.py [--manifest site_build/manifest.json] [--out site_build/sts2] [--force]

Writes rollup.mp4 + rollup.jpg (1920x1080, 4x4 tiles of 480x270): the models in the manifest's
"game_grid" (manifest.py GRID_MODELS), two runs each, from the footage tools/render filmed in the
real game. Every tile shares one clock, so a longer run keeps playing after a shorter one has ended
(a tile that ends shows how: WIN, died on floor N, or stopped). The floor badge comes from the
render's chapter marks. Does nothing when the manifest has no game_grid (footage incomplete):
video.py's decision-by-decision rollup is used then.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import video as V  # noqa: E402  (fonts, colours, encoder)

W, H = 1920, 1080
COLS, ROWS = 4, 4
TW, TH = W // COLS, H // ROWS
FPS = 24
RUN_SECONDS = 90         # the longest run's footage is fitted into this
HOLD_END = 3 * FPS
FLASH = 18               # frames of coloured border when a run ends
DEAD = (214, 120, 120)


class Tile:
    def __init__(self, run: dict, model: dict, k: float):
        self.run, self.model = run, model
        g = run["game_render"]
        self.chapters = sorted(g.get("chapters") or [], key=lambda c: c["t"])
        src = Path(run["game_dir"]) / "game.mp4"
        self.k = k
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", str(src), "-an", "-vf",
             f"setpts=PTS/{k:.5f},fps={FPS},scale={TW}:{TH}:flags=bicubic",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, creationflags=V.BELOW_NORMAL)
        self.last: Image.Image | None = None
        self.ended_at: int | None = None
        self.frames = 0

    def next(self, fi: int) -> Image.Image:
        if self.ended_at is None:
            raw = self.proc.stdout.read(TW * TH * 3)
            if len(raw) == TW * TH * 3:
                self.last = Image.frombytes("RGB", (TW, TH), raw)
                self.frames += 1
            else:
                self.ended_at = fi
                self.proc.wait()
        return self.last or Image.new("RGB", (TW, TH), V.BG)

    def floor_at(self, fi: int) -> int | None:
        t = fi / FPS * self.k  # seconds into the source footage
        floor = None
        for c in self.chapters:
            if c["t"] <= t + 0.05:
                floor = c["floor"]
        return floor


def label_img(model: dict, run: dict) -> Image.Image:
    name, sub = model["name"], f"{model['maker']} · {run['seed']}"
    f1, f2 = V.font(V.SANS_SB, 21), V.font(V.SANS, 15)
    w = int(max(V.text_w(name, f1), V.text_w(sub, f2))) + 24
    im = Image.new("RGBA", (w, 58), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    V.rrect(d, (0, 0, w - 1, 57), 6, fill=(12, 13, 17, 200))
    d.text((12, 25), name, font=f1, fill=V.TEXT, anchor="ls")
    d.text((12, 46), sub, font=f2, fill=V.MUTED, anchor="ls")
    return im


def badge_img(text: str, col) -> Image.Image:
    f = V.font(V.SANS_B, 17)
    w = int(V.text_w(text, f)) + 22
    im = Image.new("RGBA", (w, 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    V.rrect(d, (0, 0, w - 1, 29), 6, fill=(12, 13, 17, 200))
    d.text((w / 2, 21), text, font=f, fill=col, anchor="ms")
    return im


def end_img(run: dict) -> tuple[Image.Image, tuple]:
    o, floor = run["outcome"], run.get("final_floor") or 0
    if o == "victory":
        big, small, col = "WIN", "beat the Act 3 boss", V.GOLD
    elif o == "defeat":
        big, small, col = f"✝ {floor}", f"died on floor {floor}", DEAD
    else:
        big, small, col = "STOPPED", f"on floor {floor} ({o.replace('_', ' ')})", (190, 194, 204)
    im = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fb = V.font(V.SYM if big.startswith("✝") else V.SANS_B, 64)
    d.text((TW / 2, TH / 2 + 18), big, font=fb, fill=col, anchor="ms")
    d.text((TW / 2, TH / 2 + 50), small, font=V.font(V.SANS, 19), fill=V.TEXT, anchor="ms")
    return im, col


def paste_rgba(dst: Image.Image, im: Image.Image, xy) -> None:
    dst.paste(im, xy, im)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=str(V.ROOT / "site_build" / "manifest.json"))
    ap.add_argument("--out", default=str(V.ROOT / "site_build" / "sts2"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    grid = man.get("game_grid")
    if not grid:
        print("game grid: footage incomplete, nothing to do")
        return 2
    by_id = {r["id"]: r for r in man["runs"]}
    models = {m["model"]: m for m in man["models"]}
    cells = [(models[row["model"]], by_id[rid]) for row in grid["rows"] for rid in row["runs"]][: COLS * ROWS]
    out = Path(args.out)
    vid, jpg = out / "rollup.mp4", out / "rollup.jpg"
    sources = [Path(r["game_dir"]) / "game.mp4" for _, r in cells]
    stamp = out.parent / "rollup.grid.json"  # what the current rollup.mp4 was made from (not published)
    key = json.dumps([[str(p), p.stat().st_mtime] for p in sources])
    if not args.force and vid.exists() and jpg.exists() and stamp.exists() and stamp.read_text() == key:
        print(f"{vid} is up to date")
        return 0

    V.lower_priority()
    t0 = time.time()
    longest = max(r["game_render"]["duration_s"] for _, r in cells)
    k = longest / RUN_SECONDS
    tiles = [Tile(r, m, k) for m, r in cells]
    labels = [label_img(m, r) for m, r in cells]
    ends = [end_img(r) for _, r in cells]
    badges: dict = {}
    run_frames = int(RUN_SECONDS * FPS) + FPS  # a little slack for rounding
    enc = V.Encoder(W, H, FPS, vid, crf=31, gop=FPS * 4, threads=4)
    poster_at, poster = int(RUN_SECONDS * FPS * 0.55), None
    fi, done_at = 0, None
    try:
        while True:
            frame = Image.new("RGB", (W, H), V.BG)
            for i, tile in enumerate(tiles):
                x, y = (i % COLS) * TW, (i // COLS) * TH
                im = tile.next(fi).copy()
                if tile.ended_at is not None:
                    im = Image.blend(im, Image.new("RGB", (TW, TH), V.BG), 0.6)
                    end, col = ends[i]
                    paste_rgba(im, end, (0, 0))
                    age = fi - tile.ended_at
                    d = ImageDraw.Draw(im)
                    if age < FLASH:
                        d.rectangle((1, 1, TW - 2, TH - 2), outline=V.mix(V.BG, col, 1 - age / FLASH), width=4)
                    elif tile.run["outcome"] == "victory":
                        d.rectangle((1, 1, TW - 2, TH - 2), outline=V.mix(V.BG, col, 0.6), width=2)
                else:
                    floor = tile.floor_at(fi)
                    if floor:
                        b = badges.get(floor) or badges.setdefault(floor, badge_img(f"FLOOR {floor}", V.TEXT))
                        paste_rgba(im, b, (TW - b.width - 10, 10))
                paste_rgba(im, labels[i], (10, 10))
                ImageDraw.Draw(im).rectangle((0, 0, TW - 1, TH - 1), outline=V.BG, width=2)
                frame.paste(im, (x, y))
            enc.write(frame)
            if fi == poster_at:
                poster = frame
            if done_at is None and all(t.ended_at is not None for t in tiles):
                done_at = fi
            fi += 1
            if done_at is not None and fi >= done_at + HOLD_END:
                break
            if fi > run_frames + HOLD_END + 10 * FPS:
                raise RuntimeError("footage ran longer than expected")
    except BaseException:
        enc.abort()
        for t in tiles:
            t.proc.kill()
        raise
    enc.close()
    V.save_poster(poster or frame, jpg, 960)
    stamp.write_text(key)
    print(f"{vid}: game grid, {len(tiles)} tiles at {k * tiles[0].run['game_render'].get('speed', 3):.1f}x game speed, "
          f"{fi} frames ({fi / FPS:.0f}s), {vid.stat().st_size / 1e6:.1f} MB, rendered in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
