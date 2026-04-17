"""Manga Live Translator — desktop overlay prototype (Opus 4.7).

Transparent always-on-top, click-through overlay that renders translated
Chinese text on top of whatever you're reading. Captures the screen via
mss, detects page changes via perceptual hash, translates via Claude vision.
"""

import sys
import os
import json
import time
import threading
import base64
import io
import hashlib
import datetime
from pathlib import Path
from typing import Optional, List, Dict

import mss
from PIL import Image, ImageDraw, ImageFont
from anthropic import Anthropic, APIStatusError

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QRect
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QPainterPath, QPen, QBrush,
    QFontMetrics, QGuiApplication, QPalette,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QLabel, QPushButton, QLineEdit,
    QComboBox, QPlainTextEdit, QVBoxLayout, QHBoxLayout, QFormLayout,
    QFileDialog, QSpinBox, QGroupBox, QCheckBox, QDoubleSpinBox,
)

try:
    import win32gui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"

PRICE = {
    "claude-opus-4-7":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
}

PROMPT_TMPL = """这是一页日语漫画截图，尺寸 {w}×{h} 像素，坐标原点左上角。

识别所有含日语文字的区域（对话气泡、旁白框、拟声词、招牌、标签、思考泡泡等）。每个区域输出：
1. "text_jp": 日语原文（保留换行；竖排读法 = 自上而下、右列先读；忽略 furigana 小假名）
2. "text_zh": 自然流畅的中文翻译（拟声词翻成中文拟声词，例 ドキドキ→怦怦 / バタン→砰）
3. "bbox": [x_min, y_min, x_max, y_max]，整数像素坐标，紧贴该文字框的视觉边界

**严格输出 JSON array，不要 markdown 代码块，不要任何解释文字。**
格式: [{{"text_jp":"...","text_zh":"...","bbox":[x1,y1,x2,y2]}}, ...]
没有日语文字时返回 []。
忽略 UI 元素：菜单栏、按钮、URL、时间戳、页码、page number、系统时钟、鼠标指针。
kanji + furigana 合并为同一个 bbox，不要拆开。"""


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**default_config(), **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return default_config()


def default_config() -> dict:
    return {
        "api_key": "",
        "model": "claude-opus-4-7",
        "poll_ms": 1500,
        "diff_pct": 15,
        "stable_n": 3,
        "save_dir": "",
        "save_enabled": True,
        "font_factor": 0.65,
        "monitor_index": 1,
    }


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("Config save failed:", e)


def avg_hash(img: Image.Image) -> bytes:
    small = img.convert("L").resize((16, 16), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    avg = sum(px) / 256.0
    return bytes(1 if p > avg else 0 for p in px)


def hash_diff(a: Optional[bytes], b: Optional[bytes]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for i in range(len(a)) if a[i] != b[i]) / len(a)


# ------------------------------------------------------------------ capture

class CaptureWorker(QObject):
    state_changed = pyqtSignal(str, str)
    log_message   = pyqtSignal(str, str)
    stats_updated = pyqtSignal(dict)
    bubbles_ready = pyqtSignal(list, object, dict)
    overlay_clear = pyqtSignal()
    error_pause   = pyqtSignal()
    loop_ended    = pyqtSignal()

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.running = False
        self.client: Optional[Anthropic] = None
        self._reset_state()

    def _reset_state(self) -> None:
        self.last_hash: Optional[bytes] = None
        self.stable_tick_count = 0
        self.page_epoch = 0
        self.last_rendered_epoch = -1
        self.last_translate_at = 0.0
        self.consecutive_errors = 0
        self.frame_count = 0
        self.change_count = 0
        self.trans_count = 0
        self.discard_count = 0
        self.total_in = 0
        self.total_out = 0
        self.busy = False

    def start(self) -> None:
        key = self.cfg.get("api_key", "").strip()
        if not key:
            self.log_message.emit("API key missing.", "err")
            return
        try:
            self.client = Anthropic(api_key=key)
        except Exception as e:
            self.log_message.emit(f"Client init failed: {e}", "err")
            return
        self._reset_state()
        self.running = True
        self.state_changed.emit("watching", "active")
        self.log_message.emit("Capture started.", "ok")
        self._emit_stats()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self.busy = False
        self.state_changed.emit("idle", "idle")
        self.log_message.emit("Stopped.", "info")
        self.overlay_clear.emit()

    def _loop(self) -> None:
        while self.running:
            try:
                self._tick()
            except Exception as e:
                self.log_message.emit(f"Tick error: {e}", "err")
            time.sleep(max(0.4, self.cfg["poll_ms"] / 1000.0))
        self.loop_ended.emit()

    def _tick(self) -> None:
        if not self.running:
            return
        # Grab frame
        with mss.mss() as sct:
            monitors = sct.monitors
            idx = min(max(1, self.cfg.get("monitor_index", 1)), len(monitors) - 1)
            mon = monitors[idx]
            raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        # Cap long edge at 2576 for Opus 4.7 high-res ceiling
        MAX = 2576
        w, h = img.size
        le = max(w, h)
        if le > MAX:
            s = MAX / le
            img = img.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)

        self.frame_count += 1
        self._emit_stats()

        h_now = avg_hash(img)
        threshold = max(0.01, self.cfg["diff_pct"] / 100.0)
        need_n = max(1, self.cfg["stable_n"])

        if self.last_hash is None:
            self.last_hash = h_now
            self.stable_tick_count = 1
            return

        diff = hash_diff(self.last_hash, h_now)
        if diff > threshold:
            if self.stable_tick_count >= need_n:
                self.page_epoch += 1
                self.overlay_clear.emit()
            self.stable_tick_count = 0
            self.last_hash = h_now
            return

        self.stable_tick_count += 1
        self.last_hash = h_now

        if self.busy:
            return

        if (self.stable_tick_count >= need_n
                and self.last_rendered_epoch != self.page_epoch):
            since_last = time.time() - self.last_translate_at
            if self.last_translate_at and since_last < 2.5:
                return
            self.change_count += 1
            self._emit_stats()
            self.log_message.emit(
                f"Stable {need_n} ticks → translate (epoch {self.page_epoch}).", "ok")
            self._translate(img, self.page_epoch)

    def _translate(self, img: Image.Image, my_epoch: int) -> None:
        self.busy = True
        self.state_changed.emit("translating", "busy")
        model = self.cfg["model"]
        t0 = time.time()
        backoff = 0.0
        try:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()

            resp = self.client.messages.create(
                model=model,
                max_tokens=6000,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": PROMPT_TMPL.format(w=img.width, h=img.height)},
                    ],
                }],
            )
            elapsed = time.time() - t0

            self.total_in += resp.usage.input_tokens
            self.total_out += resp.usage.output_tokens
            self._emit_stats()

            if resp.stop_reason == "max_tokens":
                self.log_message.emit("Response truncated at max_tokens.", "warn")

            text = "".join(getattr(c, "text", "") for c in resp.content).strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            first = text.find("[")
            last = text.rfind("]")
            if first >= 0 and last > first:
                text = text[first:last + 1]

            try:
                parsed = json.loads(text)
                bubbles = parsed if isinstance(parsed, list) else parsed.get("bubbles", [])
            except Exception:
                self.log_message.emit(f"Bad JSON: {text[:80]}", "err")
                self.consecutive_errors += 1
                return

            clean = self._sanitize(bubbles, img.size)
            self.trans_count += 1
            self.consecutive_errors = 0
            self._emit_stats()
            self.log_message.emit(
                f"Got {len(clean)} bubble(s) in {elapsed:.1f}s "
                f"(epoch {my_epoch}, in:{resp.usage.input_tokens} out:{resp.usage.output_tokens})",
                "ok")

            if my_epoch != self.page_epoch:
                self.discard_count += 1
                self._emit_stats()
                self.log_message.emit(
                    f"Page moved (epoch {my_epoch}→{self.page_epoch}), discarded.", "warn")
                return
            if not self.running:
                return

            self.last_rendered_epoch = my_epoch
            meta = {"model": model, "epoch": my_epoch, "image_size": img.size}
            self.bubbles_ready.emit(clean, img, meta)

        except APIStatusError as e:
            s = getattr(e, "status_code", 0) or 0
            if s in (401, 403):
                self.log_message.emit("Auth error — check API key (sk-ant-...).", "err")
                self.consecutive_errors = 99
            elif s == 429:
                self.log_message.emit("429 rate-limited, back off 20s.", "warn")
                backoff = 20.0
                self.consecutive_errors += 1
            elif s == 529:
                self.log_message.emit("529 overloaded, back off 15s.", "warn")
                backoff = 15.0
                self.consecutive_errors += 1
            elif 500 <= s < 600:
                self.log_message.emit(f"{s} server err, back off 10s.", "warn")
                backoff = 10.0
                self.consecutive_errors += 1
            else:
                self.log_message.emit(f"{s}: {str(e)[:120]}", "err")
                self.consecutive_errors += 1
        except Exception as e:
            self.log_message.emit(f"Translate failed: {e}", "err")
            self.consecutive_errors += 1
        finally:
            self.busy = False
            self.last_translate_at = time.time()
            if self.running:
                if self.consecutive_errors >= 3:
                    self.log_message.emit("3 consecutive errors → pausing.", "err")
                    self.running = False
                    self.state_changed.emit("paused (error)", "err")
                    self.error_pause.emit()
                else:
                    self.state_changed.emit("watching", "active")
                    if backoff > 0:
                        self.state_changed.emit(f"backoff {int(backoff)}s", "busy")
                        time.sleep(backoff)
                        if self.running:
                            self.state_changed.emit("watching", "active")

    def _sanitize(self, bubbles, img_size) -> List[Dict]:
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
            if not all(map(lambda v: v == v, [x1, y1, x2, y2])):  # NaN filter
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

    def _emit_stats(self) -> None:
        p = PRICE.get(self.cfg["model"], PRICE["claude-opus-4-7"])
        cost = (self.total_in * p["in"] + self.total_out * p["out"]) / 1e6
        self.stats_updated.emit({
            "frames":   self.frame_count,
            "changes":  self.change_count,
            "trans":    self.trans_count,
            "discards": self.discard_count,
            "in":       self.total_in,
            "out":      self.total_out,
            "cost":     cost,
        })


# ------------------------------------------------------------------ overlay

class BubbleLabel(QWidget):
    """Transparent widget that paints black text with a white outline stroke."""

    def __init__(self, text: str, font_pt: int, parent=None):
        super().__init__(parent)
        self.text = text
        self.font_pt = font_pt
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        font = QFont("Microsoft YaHei", self.font_pt)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        fm = QFontMetrics(font)

        rect = self.rect()
        lines = self._wrap(self.text, fm, rect.width())
        total_h = len(lines) * fm.height()
        y0 = rect.top() + max(0, (rect.height() - total_h) // 2)

        for i, line in enumerate(lines):
            path = QPainterPath()
            line_w = fm.horizontalAdvance(line)
            x = rect.left() + max(0, (rect.width() - line_w) // 2)
            y = y0 + i * fm.height() + fm.ascent()
            path.addText(float(x), float(y), font, line)
            # White outline stroke (fat)
            painter.setPen(QPen(QColor(255, 255, 255, 235), 3.5,
                                Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            # Black fill
            painter.fillPath(path, QBrush(QColor(0, 0, 0, 255)))

    def _wrap(self, text: str, fm: QFontMetrics, max_w: int) -> List[str]:
        lines: List[str] = []
        for part in text.split("\n"):
            if not part:
                lines.append("")
                continue
            cur = ""
            for ch in part:
                trial = cur + ch
                if fm.horizontalAdvance(trial) > max_w and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur = trial
            if cur:
                lines.append(cur)
        return lines or [""]


class OverlayWindow(QWidget):
    def __init__(self, geom: QRect):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(geom)
        self.bubble_widgets: List[BubbleLabel] = []

    def showEvent(self, event):
        super().showEvent(event)
        if HAS_WIN32:
            self._apply_click_through_windows()

    def _apply_click_through_windows(self) -> None:
        try:
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE,
                ex
                | win32con.WS_EX_LAYERED
                | win32con.WS_EX_TRANSPARENT
                | win32con.WS_EX_TOOLWINDOW
                | win32con.WS_EX_NOACTIVATE,
            )
        except Exception as e:
            print("win32 click-through failed:", e)

    def render_bubbles(self, bubbles: List[Dict], image_size, font_factor: float) -> None:
        self.clear()
        iw, ih = image_size
        if iw <= 0 or ih <= 0:
            return
        win_w = self.width()
        win_h = self.height()
        sx = win_w / iw
        sy = win_h / ih

        sorted_b = sorted(
            bubbles,
            key=lambda b: -((b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])),
        )

        for b in sorted_b:
            x1, y1, x2, y2 = b["bbox"]
            w = (x2 - x1) * sx
            h = (y2 - y1) * sy
            if w < 8 or h < 8:
                continue
            chars = max(1, len(b["text_zh"]))
            area = w * h
            fs_px = (area / chars) ** 0.5 * font_factor
            char_cap = 18 if chars <= 2 else 24 if chars <= 8 else 20
            fs_px = max(9, min(fs_px, char_cap))
            fs_pt = max(6, int(fs_px / 1.333))

            label = BubbleLabel(b["text_zh"], fs_pt, parent=self)
            label.setGeometry(int(x1 * sx), int(y1 * sy), int(w), int(h))
            label.show()
            self.bubble_widgets.append(label)

    def clear(self) -> None:
        for w in self.bubble_widgets:
            w.deleteLater()
        self.bubble_widgets = []


# ------------------------------------------------------------------ saver

def _find_cjk_font() -> Optional[str]:
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _wrap_text_pil(text: str, font, max_w: float, draw: ImageDraw.ImageDraw) -> List[str]:
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


def save_translated_page(
    img: Image.Image,
    bubbles: List[Dict],
    save_dir: Path,
    font_factor: float = 0.65,
) -> Optional[Path]:
    if not save_dir or not save_dir.exists():
        return None
    font_path = _find_cjk_font()
    if not font_path:
        return None

    out = img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)

    # render bubbles (same sizing rules as overlay, scaled to original px)
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
        lines = _wrap_text_pil(text, font, w, draw)
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

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Page hash — so the same manga page gets the same file name prefix
    phash = hashlib.md5(avg_hash(img)).hexdigest()[:8]
    out_path = save_dir / f"manga-{ts}-{phash}.png"
    try:
        out.save(out_path, "PNG")
        return out_path
    except Exception as e:
        print("Save failed:", e)
        return None


# ------------------------------------------------------------------ UI

class ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("Manga Live Translator — Control")
        self.resize(540, 720)

        self.worker = CaptureWorker(self.cfg)
        self.worker.state_changed.connect(self._on_state)
        self.worker.log_message.connect(self._on_log)
        self.worker.stats_updated.connect(self._on_stats)
        self.worker.bubbles_ready.connect(self._on_bubbles)
        self.worker.overlay_clear.connect(self._on_overlay_clear)
        self.worker.error_pause.connect(self._on_error_pause)

        self._build_overlay()
        self._build_ui()
        self._push_cfg_to_ui()

    def _build_overlay(self) -> None:
        screens = QGuiApplication.screens()
        # cfg.monitor_index matches mss numbering (1 = primary, 2 = second).
        # Qt screens: index 0 is primary. Map mss 1 → Qt 0, mss 2 → Qt 1, etc.
        qt_idx = max(0, self.cfg.get("monitor_index", 1) - 1)
        screen = screens[min(qt_idx, len(screens) - 1)]
        self.overlay = OverlayWindow(screen.geometry())
        self.overlay.show()

    # ---------- UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        # Credentials
        cred = QGroupBox("Anthropic")
        form_cred = QFormLayout()
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("sk-ant-...")
        form_cred.addRow("API key", self.key_input)
        self.model_combo = QComboBox()
        self.model_combo.addItems(["claude-opus-4-7", "claude-sonnet-4-6"])
        form_cred.addRow("Model", self.model_combo)
        cred.setLayout(form_cred)
        root.addWidget(cred)

        # Capture
        cap = QGroupBox("Capture")
        form_cap = QFormLayout()
        self.monitor_spin = QSpinBox()
        self.monitor_spin.setRange(1, max(1, len(QGuiApplication.screens())))
        form_cap.addRow("Monitor (1 = primary)", self.monitor_spin)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(400, 8000)
        self.poll_spin.setSingleStep(100)
        self.poll_spin.setSuffix(" ms")
        form_cap.addRow("Poll interval", self.poll_spin)
        self.diff_spin = QSpinBox()
        self.diff_spin.setRange(1, 50)
        self.diff_spin.setSuffix(" %")
        form_cap.addRow("Page-diff threshold", self.diff_spin)
        self.stable_spin = QSpinBox()
        self.stable_spin.setRange(1, 8)
        form_cap.addRow("Stable ticks to trigger", self.stable_spin)
        cap.setLayout(form_cap)
        root.addWidget(cap)

        # Overlay + save
        ov = QGroupBox("Overlay & Save")
        form_ov = QFormLayout()
        self.font_spin = QDoubleSpinBox()
        self.font_spin.setRange(0.3, 1.2)
        self.font_spin.setSingleStep(0.05)
        self.font_spin.setDecimals(2)
        form_ov.addRow("Font scale", self.font_spin)
        self.save_check = QCheckBox("Save translated pages as PNG")
        form_ov.addRow(self.save_check)
        save_row = QHBoxLayout()
        self.save_dir_input = QLineEdit()
        self.save_dir_input.setPlaceholderText("(choose a folder)")
        save_row.addWidget(self.save_dir_input, 1)
        self.save_browse_btn = QPushButton("Browse…")
        self.save_browse_btn.clicked.connect(self._pick_save_dir)
        save_row.addWidget(self.save_browse_btn)
        save_row_w = QWidget(); save_row_w.setLayout(save_row)
        form_ov.addRow("Save folder", save_row_w)
        ov.setLayout(form_ov)
        root.addWidget(ov)

        # Control buttons + state
        btns = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setStyleSheet("background:#2563eb;color:white;padding:8px 18px;border-radius:4px;")
        self.start_btn.clicked.connect(self._on_start)
        btns.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("padding:8px 18px;border-radius:4px;")
        self.stop_btn.clicked.connect(self._on_stop)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)
        self.state_label = QLabel("● idle")
        self.state_label.setStyleSheet("color:#888;font-size:13px;")
        btns.addWidget(self.state_label)
        btns_w = QWidget(); btns_w.setLayout(btns)
        root.addWidget(btns_w)

        # Stats
        stats_group = QGroupBox("Stats")
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet("font-family:Consolas,monospace;color:#ccc;font-size:11px;")
        stats_v = QVBoxLayout(); stats_v.addWidget(self.stats_label)
        stats_group.setLayout(stats_v)
        root.addWidget(stats_group)

        # Event log
        log_group = QGroupBox("Event log")
        log_v = QVBoxLayout()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background:#0f0f0f;color:#a0a0a0;font-family:Consolas,monospace;font-size:11px;")
        self.log_view.setMaximumBlockCount(400)
        log_v.addWidget(self.log_view)
        log_group.setLayout(log_v)
        root.addWidget(log_group, 1)

        self._on_stats({"frames": 0, "changes": 0, "trans": 0, "discards": 0, "in": 0, "out": 0, "cost": 0})

    def _push_cfg_to_ui(self) -> None:
        self.key_input.setText(self.cfg.get("api_key", ""))
        self.model_combo.setCurrentText(self.cfg.get("model", "claude-opus-4-7"))
        self.monitor_spin.setValue(self.cfg.get("monitor_index", 1))
        self.poll_spin.setValue(self.cfg.get("poll_ms", 1500))
        self.diff_spin.setValue(self.cfg.get("diff_pct", 15))
        self.stable_spin.setValue(self.cfg.get("stable_n", 3))
        self.font_spin.setValue(self.cfg.get("font_factor", 0.65))
        self.save_check.setChecked(bool(self.cfg.get("save_enabled", True)))
        self.save_dir_input.setText(self.cfg.get("save_dir", ""))

    def _pull_ui_to_cfg(self) -> None:
        self.cfg["api_key"] = self.key_input.text().strip()
        self.cfg["model"] = self.model_combo.currentText()
        self.cfg["monitor_index"] = self.monitor_spin.value()
        self.cfg["poll_ms"] = self.poll_spin.value()
        self.cfg["diff_pct"] = self.diff_spin.value()
        self.cfg["stable_n"] = self.stable_spin.value()
        self.cfg["font_factor"] = float(self.font_spin.value())
        self.cfg["save_enabled"] = self.save_check.isChecked()
        self.cfg["save_dir"] = self.save_dir_input.text().strip()
        save_config(self.cfg)

    # ---------- actions
    def _pick_save_dir(self) -> None:
        start = self.save_dir_input.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose save folder", start)
        if d:
            self.save_dir_input.setText(d)

    def _on_start(self) -> None:
        self._pull_ui_to_cfg()
        # Rebuild overlay if monitor changed
        prev_geom = self.overlay.geometry() if self.overlay else None
        screens = QGuiApplication.screens()
        qt_idx = max(0, self.cfg.get("monitor_index", 1) - 1)
        screen = screens[min(qt_idx, len(screens) - 1)]
        new_geom = screen.geometry()
        if prev_geom is None or new_geom != prev_geom:
            if self.overlay:
                self.overlay.close()
            self.overlay = OverlayWindow(new_geom)
            self.overlay.show()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker.start()

    def _on_stop(self) -> None:
        self.worker.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ---------- signals
    def _on_state(self, text: str, kind: str) -> None:
        color = {
            "active": "#22c55e", "busy": "#f59e0b", "err": "#ef4444",
        }.get(kind, "#888")
        self.state_label.setText(f"● {text}")
        self.state_label.setStyleSheet(f"color:{color};font-size:13px;font-weight:600;")

    def _on_log(self, msg: str, level: str) -> None:
        color = {"ok": "#4ade80", "warn": "#fbbf24", "err": "#f87171"}.get(level, "#a0a0a0")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendHtml(
            f'<span style="color:#555;">{ts}</span> '
            f'<span style="color:{color};">{msg}</span>'
        )

    def _on_stats(self, s: dict) -> None:
        self.stats_label.setText(
            f"Frames sampled:     {s['frames']}\n"
            f"Page changes:       {s['changes']}\n"
            f"Translations:       {s['trans']}\n"
            f"Discarded (stale):  {s['discards']}\n"
            f"Tokens in / out:    {s['in']:,} / {s['out']:,}\n"
            f"Cost estimate:      ${s['cost']:.4f}"
        )

    def _on_bubbles(self, bubbles: list, img: Image.Image, meta: dict) -> None:
        if not self.overlay:
            return
        self.overlay.render_bubbles(bubbles, meta["image_size"], self.cfg["font_factor"])
        # Save
        if self.cfg.get("save_enabled") and self.cfg.get("save_dir"):
            save_dir = Path(self.cfg["save_dir"])
            save_dir.mkdir(parents=True, exist_ok=True)
            threading.Thread(
                target=self._do_save, args=(img, bubbles, save_dir), daemon=True
            ).start()

    def _do_save(self, img: Image.Image, bubbles: list, save_dir: Path) -> None:
        p = save_translated_page(img, bubbles, save_dir, self.cfg.get("font_factor", 0.65))
        if p:
            self._on_log(f"Saved: {p.name}", "ok")
        else:
            self._on_log("Save failed (missing font or folder).", "warn")

    def _on_overlay_clear(self) -> None:
        if self.overlay:
            self.overlay.clear()

    def _on_error_pause(self) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ---------- lifecycle
    def closeEvent(self, event):
        self._pull_ui_to_cfg()
        self.worker.stop()
        if self.overlay:
            self.overlay.close()
        super().closeEvent(event)


# ------------------------------------------------------------------ theme

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
    win = ControlWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
