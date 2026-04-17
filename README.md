# Manga Live Translator

Personal tool for translating Japanese manga pages (日语 → 中文) with overlay bubbles.

Built as a testbed for **Claude Opus 4.7** vision capabilities. Ended up discovering **Gemma 4 26B MoE (local via Ollama)** is equally good on manga OCR + bbox localization, for free.

## Two modes

| File | Mode | Use case |
|---|---|---|
| `batch.py` | **Batch** — point at a folder of pages, get translated PNGs | Main workflow. Handles 100-200 pages unattended |
| `app.py` | **Live overlay** — transparent click-through window over your screen | Experimental. Useful if you read from a live app |

`batch.py` is the recommended path.

## Quick start (Windows)

Double-click `run-batch.bat` (first run builds venv + installs deps, ~1 min).

Config:
- **Backend**: `Ollama (local Gemma 4)` (default, free) or `Claude API` (paid, faster parallel)
- **Ollama model**: default `gemma4:26b-a4b-it-q4_K_M` (pre-pull: `ollama pull gemma4:26b-a4b-it-q4_K_M`)
- **Claude API key**: only needed if you pick Claude backend
- **Input folder**: where your `.jpg`/`.png`/`.webp` manga pages live
- **Output folder**: where translated PNGs get saved

Click **Start**. Skip-existing is on by default so re-runs only translate failed/new pages.

## What it does

1. For each image in the input folder, sends to vision model (Gemma 4 or Claude)
2. Model returns `[{text_jp, text_zh, bbox}, ...]`
3. Composites black-text-with-white-stroke overlays onto original image at the returned bboxes
4. Saves full-resolution PNG to output folder

## Why two backends

| | Ollama Gemma 4 26B MoE | Claude Sonnet 4.6 / Opus 4.7 |
|---|---|---|
| Cost | $0 | ~$0.015-0.035 / page |
| Speed (per page) | ~25s serial on RTX 5090 | ~11s × 3 parallel |
| 200 pages wall time | ~70 min | ~12 min |
| Offline | ✓ | ✗ |
| bbox vertical accuracy | No bias | Sonnet clusters upper half |
| Context | 256K | 200K (Opus 4.7 1M) |

Local Gemma is the default because it's free, offline, and actually has better bbox y-coordinate fidelity (Gemma 4 outputs normalized 0-1000 coords; the code rescales to pixels). The Claude path stays as a backup.

## Files

```
app.py              Live overlay (PyQt6 transparent window, mss screen capture)
batch.py            Batch translator (main entry)
requirements.txt    Python deps
run.bat             Launch app.py (overlay mode)
run-batch.bat       Launch batch.py (recommended)
index.html          First prototype (browser-based, share-screen + overlay). Kept for history
start.bat           Launch the HTML prototype via local http server
config.json         (gitignored) app.py state
batch_config.json   (gitignored) batch.py state
venv/               (gitignored) Python environment
```

## Dependencies

```
PyQt6              UI
mss                Fast screen capture (app.py only)
anthropic          Claude SDK
Pillow             Image processing + rendering
pywin32            Windows click-through for overlay (app.py only)
json-repair        LLM JSON output fallback repair
```

## Caveats

- Windows-only (click-through overlay uses Win32, screenshot via mss is cross-platform but untested elsewhere)
- Gemma 4 `keep_alive` is set to 0 after each batch — model auto-unloads from VRAM when the run finishes
- Failed pages don't produce output files → `Skip existing` on next run auto-retries them
- Prompt engineering is tuned for vertical Japanese manga; horizontal text works but less tested

## Evolution

1. Browser-based prototype with share-screen + overlay (`index.html`) — color issues under HDR, awkward workflow
2. Desktop PyQt overlay with transparent click-through (`app.py`) — better, but live tracking feels busy
3. Batch tool (`batch.py`) — process whole chapters offline, read later
4. Added local Gemma 4 backend — free + better bbox accuracy

Personal project. Not maintained for others.
