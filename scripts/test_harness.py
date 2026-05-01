"""Identification-stage iteration harness.

Runs the same core pipeline as batch.py:process_one minus GUI/threading,
on a fixed fixture set (Sean's failure pages 302-305). Each run writes
PNG output + JSON metadata into a timestamped subdirectory of
`test_outputs/`, so you can A/B compare configs visually.

Usage:
    python scripts/test_harness.py                     # run all fixtures
    python scripts/test_harness.py 302 304             # only specific pages
    python scripts/test_harness.py --label tile-3x2    # tag this run
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# These imports work without instantiating QApplication
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
import batch as B  # noqa: E402

FIXTURES_DIR = ROOT / "test_outputs" / "fixtures"
OUT_ROOT = ROOT / "test_outputs"


def run_one(img_path: Path, out_dir: Path, cfg: dict, font_path: str) -> Dict:
    t0 = time.time()
    img = Image.open(img_path).convert("RGB")
    img = B.cap_long_edge(img, 2576)
    iw, ih = img.size

    t_call = time.time()
    try:
        tile_mode = cfg.get("tile_mode", "off")
        if tile_mode == "off":
            bubbles, in_tok, out_tok = B.call_ollama(img, cfg, cfg["max_retries"])
        else:
            bubbles, in_tok, out_tok = B.call_ollama_tiled(
                img, cfg, cfg["max_retries"], tile_mode=tile_mode)
    except Exception as e:
        dt = time.time() - t0
        meta = {
            "page": img_path.stem,
            "size": [iw, ih],
            "error": str(e),
            "elapsed_s": round(dt, 2),
        }
        (out_dir / f"{img_path.stem}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta
    call_dt = time.time() - t_call

    t_render = time.time()
    rendered = B.render_bubbles_on_image(img, bubbles, font_path, cfg["font_factor"])
    render_dt = time.time() - t_render

    out_png = out_dir / f"{img_path.stem}_after.png"
    rendered.save(out_png, "PNG")

    dt = time.time() - t0
    meta = {
        "page": img_path.stem,
        "size": [iw, ih],
        "bubble_count": len(bubbles),
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "call_s": round(call_dt, 2),
        "render_s": round(render_dt, 2),
        "elapsed_s": round(dt, 2),
        "bubbles": bubbles,
    }
    (out_dir / f"{img_path.stem}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="*", help="Page numbers (e.g. 302 304); default all")
    ap.add_argument("--label", default="run", help="Subdir label under test_outputs/")
    ap.add_argument("--model", default="gemma4:26b-a4b-it-q4_K_M")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--tile", default="off",
                    choices=["off", "auto", "split2", "grid2x2", "spread4",
                             "split2+full", "grid2x2+full"])
    args = ap.parse_args()

    if not FIXTURES_DIR.exists():
        print(f"Fixtures missing: {FIXTURES_DIR}")
        return 2

    fixtures = sorted(FIXTURES_DIR.glob("*.png"))
    if args.pages:
        wanted = set(args.pages)
        fixtures = [p for p in fixtures if p.stem in wanted]
    if not fixtures:
        print("No fixtures matched.")
        return 2

    ts = time.strftime("%H%M%S")
    out_dir = OUT_ROOT / f"{ts}_{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = {
        "ollama_url": "http://localhost:11434",
        "ollama_model": args.model,
        "max_retries": args.retries,
        "font_factor": 0.65,
        "tile_mode": args.tile,
    }

    font_path = B.find_cjk_font()
    if not font_path:
        print("No CJK font found.")
        return 2

    print(f"Output: {out_dir}")
    print(f"Model:  {args.model}")
    print(f"Pages:  {[p.stem for p in fixtures]}")
    print()

    summary = []
    for p in fixtures:
        print(f"=== {p.stem} ===")
        meta = run_one(p, out_dir, cfg, font_path)
        if "error" in meta:
            print(f"  ERROR: {meta['error']}  ({meta['elapsed_s']}s)")
        else:
            print(f"  bubbles={meta['bubble_count']}  "
                  f"call={meta['call_s']}s  render={meta['render_s']}s  "
                  f"total={meta['elapsed_s']}s")
        summary.append(meta)

    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"Done. See {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
