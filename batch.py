"""Manga Batch Translator — translate all images in a folder.

Point it at an input folder of manga pages, pick an output folder,
hit Start. Runs N images in parallel, saves translated PNGs to the
output folder. Reads the same venv / same deps as app.py.
"""

import sys
import os
import json
import time
import threading
import base64
import io
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageDraw, ImageFont
from anthropic import Anthropic, APIStatusError

try:
    from json_repair import repair_json as _repair_json
    HAS_JSON_REPAIR = True
except ImportError:
    HAS_JSON_REPAIR = False

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QComboBox, QPlainTextEdit, QVBoxLayout, QHBoxLayout, QFormLayout,
    QFileDialog, QSpinBox, QDoubleSpinBox, QCheckBox, QGroupBox, QProgressBar,
)


APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "batch_config.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

PRICE = {
    "claude-opus-4-7":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
}

PROMPT_TMPL = """这是一页日语漫画，尺寸 {w}×{h} 像素，坐标原点左上角 (0,0)，右下角 ({w},{h})。

识别所有含日语文字的区域（对话气泡、旁白框、拟声词、招牌、标签、思考泡泡等）。每个区域输出：
1. "text_jp": 日语原文（保留换行；竖排读法 = 自上而下、右列先读；忽略 furigana 小假名）
2. "text_zh": 自然流畅的中文翻译（拟声词翻成中文拟声词，例 ドキドキ→怦怦 / バタン→砰）
3. "bbox": [x_min, y_min, x_max, y_max]，整数像素坐标，紧贴该文字框的视觉边界

**⚠ 垂直位置准确性（关键）**
漫画页面高度 {h} 像素，请用以下垂直分区做参考：
- 顶部 1/3 区域：y 在 0 到 {h1} 之间
- 中部 1/3 区域：y 在 {h1} 到 {h2} 之间
- 底部 1/3 区域：y 在 {h2} 到 {h} 之间

常见偏差：将下半页的文字 bbox 错误地写在上半页（y 值偏小）。请严格避免。
- 如果文字视觉上在页面下半 (y > {h_half})，则 y1 和 y2 **必须都 > {h_half}**
- 多 panel 漫画中，上格 / 中格 / 下格里的文字 bbox **不要都挤到页面顶部**
- 输出前逐个核对：每个 bbox 的 y 范围是否与该文字在画面中的实际垂直位置吻合

**严格输出 JSON array，不要 markdown 代码块，不要任何解释文字。**
格式: [{{"text_jp":"...","text_zh":"...","bbox":[x1,y1,x2,y2]}}, ...]
没有日语文字时返回 []。
忽略 UI 元素：菜单栏、按钮、URL、时间戳、页码、page number。
kanji + furigana 合并为同一个 bbox，不要拆开。"""


# ------------------------------------------------------------------ config

def default_config() -> dict:
    return {
        "backend": "ollama",              # "ollama" | "claude"
        "api_key": "",
        "model": "claude-opus-4-7",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "gemma4:26b-a4b-it-q4_K_M",
        "input_dir": "",
        "output_dir": "",
        "font_factor": 0.65,
        "concurrency": 3,
        "skip_existing": True,
        "recursive": False,
        "output_format": "PNG",
        "max_retries": 3,
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**default_config(), **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return default_config()


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("Config save failed:", e)


# ------------------------------------------------------------------ helpers

def find_cjk_font() -> Optional[str]:
    for p in [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]:
        if Path(p).exists():
            return p
    return None


def wrap_text(text: str, font, max_w: float, draw: ImageDraw.ImageDraw) -> List[str]:
    lines: List[str] = []
    for part in text.split("\n"):
        if not part:
            lines.append("")
            continue
        cur = ""
        for ch in part:
            trial = cur + ch
            if draw.textlength(trial, font=font) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines or [""]


def cap_long_edge(img: Image.Image, max_edge: int = 2576) -> Image.Image:
    w, h = img.size
    le = max(w, h)
    if le <= max_edge:
        return img
    s = max_edge / le
    return img.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)


def sanitize_bubbles(bubbles, img_size) -> List[Dict]:
    clean = []
    iw, ih = img_size
    img_area = max(1, iw * ih)
    for b in bubbles:
        if not isinstance(b, dict):
            continue
        bbox = b.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = [float(v) for v in bbox]
        except Exception:
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        if x1 < 0 or y1 < 0:
            continue
        if x2 > iw * 1.05 or y2 > ih * 1.05:
            continue
        x1 = max(0.0, x1); y1 = max(0.0, y1)
        x2 = min(float(iw), x2); y2 = min(float(ih), y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        if ((x2 - x1) * (y2 - y1)) / img_area > 0.7:
            continue
        txt = b.get("text_zh", "")
        if not isinstance(txt, str) or not txt.strip():
            continue
        clean.append({
            "text_jp": str(b.get("text_jp", "")),
            "text_zh": txt.strip(),
            "bbox": [int(x1), int(y1), int(x2), int(y2)],
        })
    return clean


def render_bubbles_on_image(
    img: Image.Image,
    bubbles: List[Dict],
    font_path: str,
    font_factor: float = 0.65,
) -> Image.Image:
    out = img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)
    sorted_b = sorted(
        bubbles,
        key=lambda b: -((b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])),
    )
    for b in sorted_b:
        x1, y1, x2, y2 = b["bbox"]
        w = x2 - x1
        h = y2 - y1
        text = b["text_zh"]
        chars = max(1, len(text))
        area = w * h
        fs = (area / chars) ** 0.5 * font_factor
        char_cap = 26 if chars <= 2 else 34 if chars <= 8 else 28
        fs = int(max(12, min(fs, char_cap)))
        try:
            font = ImageFont.truetype(font_path, fs)
        except Exception:
            continue
        lines = wrap_text(text, font, w, draw)
        line_h = int(fs * 1.25)
        total_h = len(lines) * line_h
        y_start = y1 + max(0, (h - total_h) // 2)
        stroke = max(2, fs // 8)
        for i, line in enumerate(lines):
            lw = draw.textlength(line, font=font)
            cx = x1 + max(0, (w - int(lw)) // 2)
            cy = y_start + i * line_h
            draw.text(
                (cx, cy), line, font=font,
                fill="black",
                stroke_width=stroke,
                stroke_fill="white",
            )
    return out


OLLAMA_PROMPT_TMPL = """日语漫画一页，尺寸 {w}×{h} 像素。

识别每个含日语文字的区域（对话气泡、旁白框、拟声词、招牌、标签等），输出 JSON array。每项：
- "text_jp": 日语原文（竖排阅读顺序 = 自上而下、右列先读；忽略 furigana 小假名；kanji+furigana 合并一项）
- "text_zh": 自然流畅的中文翻译（拟声词翻成中文拟声词，例 ドキドキ→怦怦 / バタン→砰）
- "box_2d": [x1, y1, x2, y2]，坐标**归一化到 0-1000**（图像左上=0,0，右下=1000,1000），紧贴文字框边界

**只**输出 JSON array（以 [ 开头 ] 结尾），不要 markdown 代码块，不要任何解释文字。
没有日语文字时输出 []。忽略 UI 元素：菜单、按钮、URL、时间戳、页码。"""


def call_ollama(
    img: Image.Image,
    cfg: dict,
    max_retries: int,
) -> Tuple[List[Dict], int, int]:
    """Local Gemma 4 (or similar) via Ollama. Returns (bubbles, input_tokens, output_tokens).
    Gemma 4 outputs bbox normalized to 0-1000; we rescale to image pixels before sanitize."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    iw, ih = img.size
    base_prompt = OLLAMA_PROMPT_TMPL.format(w=iw, h=ih)

    url = cfg["ollama_url"].rstrip("/") + "/api/generate"
    model = cfg["ollama_model"]
    correction = (
        "\n\n⚠️ 上次输出无法被解析为合法 JSON array。严格要求：只输出 JSON array（[ 开头 ] 结尾），"
        "不要 markdown 代码块，不要解释，不要前后多余文字。没有日语文字输出 []。"
    )

    last_err: Optional[Exception] = None
    last_bad: Optional[str] = None

    for attempt in range(max_retries + 1):
        raw_text = ""
        try:
            prompt = base_prompt
            if last_bad and attempt > 0:
                prompt = base_prompt + correction + f"\n上次输出前200字: {last_bad[:200]!r}"

            body = {
                "model": model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 6000},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=420) as resp:
                data = json.load(resp)

            raw_text = (data.get("response") or "").strip()
            if not raw_text:
                done_reason = data.get("done_reason", "?")
                eval_count = data.get("eval_count", 0)
                prompt_eval = data.get("prompt_eval_count", 0)
                raise ValueError(
                    f"empty response (done_reason={done_reason}, "
                    f"eval_count={eval_count}, prompt_eval={prompt_eval})"
                )
            text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

            first = text.find("[")
            last = text.rfind("]")
            if first < 0 or last <= first:
                raise ValueError(f"non-JSON response (head): {raw_text[:80]!r}")
            text = text[first:last + 1]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                if HAS_JSON_REPAIR:
                    parsed = json.loads(_repair_json(text))
                else:
                    raise

            bubbles = parsed if isinstance(parsed, list) else parsed.get("bubbles", [])

            # Rescale 0-1000 normalized → image pixels; also normalize 'box_2d' → 'bbox'
            for b in bubbles:
                if not isinstance(b, dict):
                    continue
                bbox = b.get("bbox") or b.get("box_2d")
                if isinstance(bbox, list) and len(bbox) == 4:
                    try:
                        x1, y1, x2, y2 = [float(v) for v in bbox]
                        b["bbox"] = [
                            x1 * iw / 1000.0, y1 * ih / 1000.0,
                            x2 * iw / 1000.0, y2 * ih / 1000.0,
                        ]
                    except Exception:
                        pass

            in_tok = int(data.get("prompt_eval_count") or 0)
            out_tok = int(data.get("eval_count") or 0)
            return sanitize_bubbles(bubbles, img.size), in_tok, out_tok

        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            last_bad = raw_text
            if attempt < max_retries:
                time.sleep(2.0)
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            # Network / ollama down / timeout — don't corrupt last_bad
            if attempt < max_retries:
                time.sleep(3.0)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2.0)
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


def _parse_retry_after(e: APIStatusError) -> float:
    """Extract retry-after seconds from a 429/529 response, if present."""
    try:
        resp = getattr(e, "response", None)
        if resp is None:
            return 0.0
        headers = getattr(resp, "headers", {}) or {}
        # httpx Headers supports case-insensitive get
        val = ""
        for k in ("retry-after", "Retry-After", "anthropic-ratelimit-requests-reset"):
            try:
                v = headers.get(k)
            except Exception:
                v = None
            if v:
                val = str(v)
                break
        return float(val) if val else 0.0
    except Exception:
        return 0.0


def call_claude(
    client: Anthropic,
    img: Image.Image,
    model: str,
    max_retries: int,
) -> Tuple[List[Dict], int, int]:
    """Returns (bubbles, input_tokens, output_tokens). Raises on hard failure."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    w, h = img.width, img.height
    prompt = PROMPT_TMPL.format(
        w=w, h=h,
        h1=h // 3, h2=(2 * h) // 3, h_half=h // 2,
    )
    base_content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": prompt},
    ]
    correction_text = (
        "你上次的输出无法被解析为合法 JSON array。"
        "严格要求：**只**输出 JSON array（以 [ 开头，以 ] 结尾），不要任何其他文字、不要 markdown 代码块、不要解释。"
        "如果这一页没有日语文字，直接输出 [] 即可。"
        "每个 object 之间必须用逗号分隔。字符串中的双引号必须用 \\\" 转义。"
        "现在严格按格式重新输出一遍。"
    )

    last_err: Optional[Exception] = None
    last_bad_response: Optional[str] = None

    for attempt in range(max_retries + 1):
        raw_text: str = ""
        try:
            if last_bad_response and attempt > 0:
                snippet = last_bad_response[:400] or "[empty output]"
                messages = [
                    {"role": "user", "content": base_content},
                    {"role": "assistant", "content": snippet},
                    {"role": "user", "content": correction_text},
                ]
            else:
                messages = [{"role": "user", "content": base_content}]

            resp = client.messages.create(
                model=model,
                max_tokens=6000,
                messages=messages,
            )
            raw_text = "".join(getattr(c, "text", "") for c in resp.content).strip()
            text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

            first = text.find("[")
            last = text.rfind("]")
            if first < 0 or last <= first:
                # Model replied with prose / empty / refusal — no JSON array at all.
                raise ValueError(f"non-JSON response (head): {raw_text[:80]!r}")

            text = text[first:last + 1]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Format-level malformation — commas, quotes, trailing commas.
                if HAS_JSON_REPAIR:
                    parsed = json.loads(_repair_json(text))
                else:
                    raise

            bubbles = parsed if isinstance(parsed, list) else parsed.get("bubbles", [])
            return (
                sanitize_bubbles(bubbles, img.size),
                resp.usage.input_tokens,
                resp.usage.output_tokens,
            )

        except APIStatusError as e:
            last_err = e
            s = getattr(e, "status_code", 0) or 0
            retry_after = _parse_retry_after(e)
            if s == 429:
                wait = max(retry_after, 30.0)
            elif s == 529:
                wait = max(retry_after, 20.0)
            elif 500 <= s < 600:
                wait = max(retry_after, 10.0)
            else:
                raise  # 400/401/403 etc — not retryable
            if attempt < max_retries:
                time.sleep(min(wait, 120.0))
                continue
            raise

        except (json.JSONDecodeError, ValueError) as e:
            # Either the model returned prose / empty (ValueError) or the JSON
            # was so broken that even json_repair couldn't save it. Retry with
            # the model's bad output attached as a correction signal.
            last_err = e
            last_bad_response = raw_text
            if attempt < max_retries:
                time.sleep(3.0)
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ worker

class BatchWorker(QObject):
    progress = pyqtSignal(int, int, int, int)  # done, skipped, failed, total
    log_message = pyqtSignal(str, str)
    stats_updated = pyqtSignal(dict)
    finished = pyqtSignal()
    state_changed = pyqtSignal(str, str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.stop_requested = False
        self._lock = threading.Lock()
        self._start_time = 0.0
        self._done = 0
        self._skipped = 0
        self._failed = 0
        self._total = 0
        self._total_in = 0
        self._total_out = 0
        self._durations: List[float] = []

    def start(self, files: List[Path], out_dir: Path) -> None:
        self.stop_requested = False
        self._start_time = time.time()
        self._done = 0
        self._skipped = 0
        self._failed = 0
        self._total = len(files)
        self._total_in = 0
        self._total_out = 0
        self._durations = []
        self._emit_all()
        self.state_changed.emit("running", "active")
        threading.Thread(target=self._run, args=(files, out_dir), daemon=True).start()

    def stop(self) -> None:
        self.stop_requested = True
        self.state_changed.emit("stopping…", "busy")
        self.log_message.emit("Stop requested, finishing current tasks…", "warn")

    def _run(self, files: List[Path], out_dir: Path) -> None:
        backend = self.cfg.get("backend", "ollama")
        client: Optional[Anthropic] = None

        if backend == "ollama":
            # Ping ollama + verify model is installed
            try:
                url = self.cfg["ollama_url"].rstrip("/") + "/api/tags"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    tags = json.load(resp)
                installed = [m.get("name", "") for m in tags.get("models", [])]
                if self.cfg["ollama_model"] not in installed:
                    avail = ", ".join(installed[:5]) or "(none)"
                    self.log_message.emit(
                        f"Model '{self.cfg['ollama_model']}' not installed in ollama. "
                        f"Run: ollama pull {self.cfg['ollama_model']}. Available: {avail}",
                        "err")
                    self.finished.emit()
                    return
                self.log_message.emit(
                    f"Ollama reachable, model {self.cfg['ollama_model']} ready.", "ok")
            except Exception as e:
                self.log_message.emit(
                    f"Cannot reach ollama at {self.cfg['ollama_url']}: {e}. "
                    f"Start ollama via `ollama serve` and retry.", "err")
                self.finished.emit()
                return
        else:
            if not self.cfg.get("api_key", "").strip():
                self.log_message.emit("API key missing for Claude backend.", "err")
                self.finished.emit()
                return
            try:
                client = Anthropic(api_key=self.cfg["api_key"])
            except Exception as e:
                self.log_message.emit(f"Claude client init failed: {e}", "err")
                self.finished.emit()
                return

        font_path = find_cjk_font()
        if not font_path:
            self.log_message.emit("No CJK font found (tried msyh, simhei, simsun). Aborting.", "err")
            self.finished.emit()
            return

        model = self.cfg["model"]
        ff = self.cfg["font_factor"]
        skip_existing = self.cfg["skip_existing"]
        out_format = self.cfg.get("output_format", "PNG").upper()
        ext = ".png" if out_format == "PNG" else ".jpg"
        retries = self.cfg.get("max_retries", 3)
        # Ollama runs on one GPU; let the user's concurrency through but be aware
        # that ollama serializes internally unless OLLAMA_NUM_PARALLEL is tuned.
        concurrency = max(1, int(self.cfg.get("concurrency", 3)))
        if backend == "ollama" and concurrency > 1:
            self.log_message.emit(
                f"Ollama backend: capping concurrency {concurrency}→1 "
                f"(single GPU + 17GB model serializes anyway; parallel requests "
                f"cause timeouts and empty responses).",
                "warn")
            concurrency = 1
        max_passes = max(1, int(self.cfg.get("auto_retry_passes", 3)))

        def process_one(path: Path) -> str:
            if self.stop_requested:
                return "cancelled"
            out_path = out_dir / (path.stem + ext)
            if skip_existing and out_path.exists():
                return "skipped"

            t0 = time.time()
            try:
                img = Image.open(path).convert("RGB")
            except Exception as e:
                return f"err: open failed ({e})"

            img = cap_long_edge(img, 2576)

            try:
                if backend == "ollama":
                    bubbles, in_tok, out_tok = call_ollama(img, self.cfg, retries)
                else:
                    bubbles, in_tok, out_tok = call_claude(client, img, model, retries)
            except APIStatusError as e:
                s = getattr(e, "status_code", 0) or 0
                return f"err: HTTP {s}"
            except Exception as e:
                return f"err: {e}"

            with self._lock:
                self._total_in += in_tok
                self._total_out += out_tok

            rendered = render_bubbles_on_image(img, bubbles, font_path, ff)
            try:
                if out_format == "PNG":
                    rendered.save(out_path, "PNG")
                else:
                    rendered.save(out_path, "JPEG", quality=95)
            except Exception as e:
                return f"err: save failed ({e})"

            dt = time.time() - t0
            with self._lock:
                self._durations.append(dt)
            return f"{len(bubbles)} bubbles ({dt:.1f}s)"

        self.log_message.emit(
            f"Starting batch: {self._total} files, {concurrency} parallel, "
            f"model={model}, auto-retry passes={max_passes}",
            "ok"
        )

        remaining = list(files)

        for pass_num in range(1, max_passes + 1):
            if not remaining or self.stop_requested:
                break

            if pass_num > 1:
                # These files were counted as failed in the previous pass —
                # un-count them so the cumulative _failed reflects only the
                # files that fail the FINAL pass.
                with self._lock:
                    self._failed = max(0, self._failed - len(remaining))
                self.log_message.emit(
                    f"Auto-retry pass {pass_num}/{max_passes}: "
                    f"{len(remaining)} files still pending",
                    "warn")
                self.progress.emit(self._done, self._skipped, self._failed, self._total)

            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                future_map = {ex.submit(process_one, f): f for f in remaining}
                try:
                    for fut in as_completed(future_map):
                        path = future_map[fut]
                        try:
                            result = fut.result()
                        except Exception as e:
                            result = f"err: {e}"

                        with self._lock:
                            if result == "skipped":
                                self._skipped += 1
                                lvl = "info"
                            elif result.startswith("err:") or result == "cancelled":
                                self._failed += 1
                                lvl = "err"
                            else:
                                self._done += 1
                                lvl = "ok"
                            counts = (self._done, self._skipped, self._failed, self._total)

                        idx = counts[0] + counts[1] + counts[2]
                        tag = f"[{idx}/{self._total}]" if pass_num == 1 else f"[retry {pass_num}]"
                        self.log_message.emit(f"{tag} {path.name}: {result}", lvl)
                        self.progress.emit(*counts)
                        self._emit_stats()

                        if self.stop_requested:
                            for f_obj in future_map:
                                f_obj.cancel()
                            break
                except KeyboardInterrupt:
                    self.stop_requested = True

            # Re-collect files that still have no output → next-pass candidates
            remaining = [
                f for f in remaining
                if not (out_dir / (f.stem + ext)).exists()
            ]

        if remaining and not self.stop_requested:
            self.log_message.emit(
                f"{len(remaining)} files failed after {max_passes} passes: "
                f"{', '.join(p.name for p in remaining[:5])}"
                f"{' …' if len(remaining) > 5 else ''}",
                "err")

        elapsed = time.time() - self._start_time
        self.log_message.emit(
            f"Batch done. Done={self._done} Skipped={self._skipped} Failed={self._failed} "
            f"Elapsed={elapsed:.1f}s",
            "ok"
        )

        # Unload ollama model from VRAM so GPU is free for other apps
        if backend == "ollama":
            try:
                body = json.dumps({
                    "model": self.cfg["ollama_model"],
                    "keep_alive": 0,
                }).encode("utf-8")
                req = urllib.request.Request(
                    self.cfg["ollama_url"].rstrip("/") + "/api/generate",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                self.log_message.emit("Unloaded ollama model from VRAM.", "info")
            except Exception as e:
                self.log_message.emit(f"Could not unload ollama model: {e}", "warn")

        self.state_changed.emit("idle", "idle")
        self.finished.emit()

    def _emit_all(self) -> None:
        self.progress.emit(self._done, self._skipped, self._failed, self._total)
        self._emit_stats()

    def _emit_stats(self) -> None:
        backend = self.cfg.get("backend", "ollama")
        if backend == "ollama":
            cost = 0.0  # local inference, no $
        else:
            model = self.cfg.get("model", "claude-opus-4-7")
            p = PRICE.get(model, PRICE["claude-opus-4-7"])
            cost = (self._total_in * p["in"] + self._total_out * p["out"]) / 1e6
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        avg = (sum(self._durations) / len(self._durations)) if self._durations else 0.0
        remaining = self._total - self._done - self._skipped - self._failed
        # ETA: based on parallelism, ETA = remaining * avg / concurrency
        conc = max(1, self.cfg.get("concurrency", 3))
        eta = remaining * avg / conc if avg > 0 else 0.0
        self.stats_updated.emit({
            "in": self._total_in,
            "out": self._total_out,
            "cost": cost,
            "elapsed": elapsed,
            "avg": avg,
            "eta": eta,
        })


# ------------------------------------------------------------------ UI

def fmt_time(secs: float) -> str:
    if secs < 1:
        return "0s"
    if secs < 60:
        return f"{secs:.0f}s"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class BatchWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("Manga Batch Translator")
        self.resize(640, 780)

        self.worker = BatchWorker(self.cfg)
        self.worker.progress.connect(self._on_progress)
        self.worker.log_message.connect(self._on_log)
        self.worker.stats_updated.connect(self._on_stats)
        self.worker.finished.connect(self._on_finished)
        self.worker.state_changed.connect(self._on_state)

        self._build_ui()
        self._push_cfg_to_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        # Backend
        g1 = QGroupBox("Backend")
        f1 = QFormLayout()
        self.backend_combo = QComboBox()
        self.backend_combo.addItems([
            "Ollama (local Gemma 4) — free, offline, ~25s/page on RTX 5090",
            "Claude API — ~11s/page × 3 parallel, paid",
        ])
        f1.addRow("Backend", self.backend_combo)

        self.ollama_url_input = QLineEdit()
        self.ollama_url_input.setPlaceholderText("http://localhost:11434")
        f1.addRow("Ollama URL", self.ollama_url_input)
        self.ollama_model_input = QLineEdit()
        self.ollama_model_input.setPlaceholderText("gemma4:26b-a4b-it-q4_K_M")
        f1.addRow("Ollama model", self.ollama_model_input)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("sk-ant-... (only for Claude backend)")
        f1.addRow("Claude API key", self.key_input)
        self.model_combo = QComboBox()
        self.model_combo.addItems(["claude-opus-4-7", "claude-sonnet-4-6"])
        f1.addRow("Claude model", self.model_combo)

        self.backend_combo.currentIndexChanged.connect(self._update_backend_visibility)
        g1.setLayout(f1)
        root.addWidget(g1)

        # Folders
        g2 = QGroupBox("Folders")
        f2 = QFormLayout()
        in_row = QHBoxLayout()
        self.in_input = QLineEdit()
        self.in_input.setPlaceholderText("(folder containing manga page images)")
        in_row.addWidget(self.in_input, 1)
        in_btn = QPushButton("Browse…")
        in_btn.clicked.connect(lambda: self._pick_dir(self.in_input, "Input folder"))
        in_row.addWidget(in_btn)
        in_w = QWidget(); in_w.setLayout(in_row)
        f2.addRow("Input", in_w)

        out_row = QHBoxLayout()
        self.out_input = QLineEdit()
        self.out_input.setPlaceholderText("(where translated PNGs go)")
        out_row.addWidget(self.out_input, 1)
        out_btn = QPushButton("Browse…")
        out_btn.clicked.connect(lambda: self._pick_dir(self.out_input, "Output folder"))
        out_row.addWidget(out_btn)
        out_w = QWidget(); out_w.setLayout(out_row)
        f2.addRow("Output", out_w)

        self.recurse_check = QCheckBox("Include subfolders")
        f2.addRow(self.recurse_check)
        self.skip_check = QCheckBox("Skip files already translated (resume)")
        f2.addRow(self.skip_check)
        g2.setLayout(f2)
        root.addWidget(g2)

        # Options
        g3 = QGroupBox("Options")
        f3 = QFormLayout()
        self.font_spin = QDoubleSpinBox()
        self.font_spin.setRange(0.3, 1.2); self.font_spin.setSingleStep(0.05); self.font_spin.setDecimals(2)
        f3.addRow("Font scale", self.font_spin)
        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 8)
        f3.addRow("Concurrent workers (3 is safe; 5+ may hit 429)", self.conc_spin)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        f3.addRow("Retries on transient error", self.retry_spin)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["PNG", "JPEG"])
        f3.addRow("Output format (PNG lossless, JPEG smaller)", self.format_combo)
        g3.setLayout(f3)
        root.addWidget(g3)

        # Controls
        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setStyleSheet("background:#2563eb;color:white;padding:8px 18px;border-radius:4px;")
        self.start_btn.clicked.connect(self._on_start)
        ctrl.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("padding:8px 18px;border-radius:4px;")
        self.stop_btn.clicked.connect(self._on_stop)
        ctrl.addWidget(self.stop_btn)
        ctrl.addStretch(1)
        self.state_label = QLabel("● idle")
        self.state_label.setStyleSheet("color:#888;font-size:13px;")
        ctrl.addWidget(self.state_label)
        ctrl_w = QWidget(); ctrl_w.setLayout(ctrl)
        root.addWidget(ctrl_w)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v / %m (%p%)")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)
        root.addWidget(self.progress_bar)

        self.progress_label = QLabel("—")
        self.progress_label.setStyleSheet("color:#888;font-size:11px;font-family:Consolas,monospace;")
        root.addWidget(self.progress_label)

        # Stats
        g4 = QGroupBox("Stats")
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet("font-family:Consolas,monospace;color:#ccc;font-size:11px;")
        v4 = QVBoxLayout(); v4.addWidget(self.stats_label)
        g4.setLayout(v4)
        root.addWidget(g4)

        # Log
        g5 = QGroupBox("Event log")
        v5 = QVBoxLayout()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background:#0f0f0f;color:#a0a0a0;font-family:Consolas,monospace;font-size:11px;")
        self.log_view.setMaximumBlockCount(500)
        v5.addWidget(self.log_view)
        g5.setLayout(v5)
        root.addWidget(g5, 1)

        self._on_stats({"in": 0, "out": 0, "cost": 0, "elapsed": 0, "avg": 0, "eta": 0})
        self._on_progress(0, 0, 0, 0)

    def _push_cfg_to_ui(self) -> None:
        self.backend_combo.setCurrentIndex(0 if self.cfg.get("backend", "ollama") == "ollama" else 1)
        self.ollama_url_input.setText(self.cfg.get("ollama_url", "http://localhost:11434"))
        self.ollama_model_input.setText(self.cfg.get("ollama_model", "gemma4:26b-a4b-it-q4_K_M"))
        self.key_input.setText(self.cfg.get("api_key", ""))
        self.model_combo.setCurrentText(self.cfg.get("model", "claude-opus-4-7"))
        self.in_input.setText(self.cfg.get("input_dir", ""))
        self.out_input.setText(self.cfg.get("output_dir", ""))
        self.recurse_check.setChecked(bool(self.cfg.get("recursive", False)))
        self.skip_check.setChecked(bool(self.cfg.get("skip_existing", True)))
        self.font_spin.setValue(self.cfg.get("font_factor", 0.65))
        self.conc_spin.setValue(self.cfg.get("concurrency", 3))
        self.retry_spin.setValue(self.cfg.get("max_retries", 3))
        self.format_combo.setCurrentText(self.cfg.get("output_format", "PNG"))
        self._update_backend_visibility()

    def _pull_ui_to_cfg(self) -> None:
        self.cfg["backend"] = "ollama" if self.backend_combo.currentIndex() == 0 else "claude"
        self.cfg["ollama_url"] = self.ollama_url_input.text().strip() or "http://localhost:11434"
        self.cfg["ollama_model"] = self.ollama_model_input.text().strip() or "gemma4:26b-a4b-it-q4_K_M"
        self.cfg["api_key"] = self.key_input.text().strip()
        self.cfg["model"] = self.model_combo.currentText()
        self.cfg["input_dir"] = self.in_input.text().strip()
        self.cfg["output_dir"] = self.out_input.text().strip()
        self.cfg["recursive"] = self.recurse_check.isChecked()
        self.cfg["skip_existing"] = self.skip_check.isChecked()
        self.cfg["font_factor"] = float(self.font_spin.value())
        self.cfg["concurrency"] = self.conc_spin.value()
        self.cfg["max_retries"] = self.retry_spin.value()
        self.cfg["output_format"] = self.format_combo.currentText()
        save_config(self.cfg)

    def _update_backend_visibility(self) -> None:
        is_ollama = self.backend_combo.currentIndex() == 0
        self.ollama_url_input.setEnabled(is_ollama)
        self.ollama_model_input.setEnabled(is_ollama)
        self.key_input.setEnabled(not is_ollama)
        self.model_combo.setEnabled(not is_ollama)

    def _pick_dir(self, line_edit: QLineEdit, title: str) -> None:
        start = line_edit.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, title, start)
        if d:
            line_edit.setText(d)

    def _collect_files(self, in_dir: Path, recursive: bool) -> List[Path]:
        if recursive:
            paths = [p for p in in_dir.rglob("*") if p.is_file()]
        else:
            paths = [p for p in in_dir.iterdir() if p.is_file()]
        paths = [p for p in paths if p.suffix.lower() in IMAGE_EXTS]
        paths.sort(key=lambda p: p.name.lower())
        return paths

    def _on_start(self) -> None:
        self._pull_ui_to_cfg()

        if not self.cfg["api_key"]:
            self._on_log("API key missing.", "err"); return
        in_dir = Path(self.cfg["input_dir"])
        out_dir = Path(self.cfg["output_dir"])
        if not in_dir.is_dir():
            self._on_log(f"Input folder not found: {in_dir}", "err"); return
        if not self.cfg["output_dir"]:
            self._on_log("Choose an output folder.", "err"); return
        if in_dir.resolve() == out_dir.resolve():
            self._on_log("Input and output folders must be different.", "err"); return

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._on_log(f"Cannot create output folder: {e}", "err"); return

        files = self._collect_files(in_dir, self.cfg["recursive"])
        if not files:
            self._on_log(f"No images found in {in_dir}", "warn"); return

        self.log_view.clear()
        self._on_log(f"Found {len(files)} image(s). Starting…", "info")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setMaximum(len(files))
        self.progress_bar.setValue(0)
        self.worker.start(files, out_dir)

    def _on_stop(self) -> None:
        self.worker.stop()
        self.stop_btn.setEnabled(False)

    # ---------- signals
    def _on_state(self, text: str, kind: str) -> None:
        color = {"active": "#22c55e", "busy": "#f59e0b", "err": "#ef4444"}.get(kind, "#888")
        self.state_label.setText(f"● {text}")
        self.state_label.setStyleSheet(f"color:{color};font-size:13px;font-weight:600;")

    def _on_log(self, msg: str, level: str) -> None:
        color = {"ok": "#4ade80", "warn": "#fbbf24", "err": "#f87171"}.get(level, "#a0a0a0")
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendHtml(
            f'<span style="color:#555;">{ts}</span> '
            f'<span style="color:{color};">{msg}</span>'
        )

    def _on_progress(self, done: int, skipped: int, failed: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done + skipped + failed)
        else:
            self.progress_bar.setMaximum(1)
            self.progress_bar.setValue(0)
        self.progress_label.setText(
            f"Done {done}  ·  Skipped {skipped}  ·  Failed {failed}  ·  Total {total}"
        )

    def _on_stats(self, s: dict) -> None:
        self.stats_label.setText(
            f"Tokens in / out:    {s['in']:,} / {s['out']:,}\n"
            f"Cost estimate:      ${s['cost']:.4f}\n"
            f"Elapsed:            {fmt_time(s['elapsed'])}\n"
            f"Avg per image:      {s['avg']:.1f}s\n"
            f"ETA:                {fmt_time(s['eta']) if s['eta'] > 0 else '—'}"
        )

    def _on_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event):
        self._pull_ui_to_cfg()
        self.worker.stop()
        super().closeEvent(event)


# ------------------------------------------------------------------ main

def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(18, 18, 18))
    p.setColor(QPalette.ColorRole.WindowText, QColor(232, 232, 232))
    p.setColor(QPalette.ColorRole.Base, QColor(28, 28, 28))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(24, 24, 24))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(232, 232, 232))
    p.setColor(QPalette.ColorRole.Text, QColor(232, 232, 232))
    p.setColor(QPalette.ColorRole.Button, QColor(36, 36, 36))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(232, 232, 232))
    p.setColor(QPalette.ColorRole.Highlight, QColor(37, 99, 235))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(p)


def main() -> int:
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    win = BatchWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
