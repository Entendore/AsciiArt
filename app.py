import sys
import os
import math
import random
import subprocess
import shutil
import cv2
import numpy as np
from PySide6.QtWidgets import (QApplication, QMainWindow, QPlainTextEdit, QVBoxLayout,
                               QWidget, QFileDialog, QMessageBox, QHBoxLayout,
                               QTabWidget, QPushButton, QStatusBar, QDialog,
                               QSpinBox, QDoubleSpinBox, QProgressBar, QGroupBox,
                               QFormLayout, QLabel, QLineEdit, QComboBox)
from PySide6.QtGui import QFont, QFontDatabase, QColor, QPainter, QImage, QPixmap
from PySide6.QtCore import Qt, QTimer, QThread, Signal

# ==========================================
# Constants & Palettes
# ==========================================
GRID_W, GRID_H = 80, 30

PALETTES = {
    "Standard": " .:-=+*#%@",
    "Blocks": " ░▒▓█",
    "Dots": " ·•●",
    "Detailed": " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "Binary": " 01"
}

# ==========================================
# Vectorized animation
# ==========================================

def make_plasma_indices(t, chars, px=None, py=None):
    """Compute plasma frame as a 2D int index array (one vectorized op)."""
    if px is None:
        px, py = np.meshgrid(np.arange(GRID_W, dtype=np.float32),
                             np.arange(GRID_H, dtype=np.float32))
    v = np.sin(px * (1.0 / 16.0) + t)
    v += np.sin(py * (1.0 / 8.0) + t)
    v += np.sin((px + py) * (1.0 / 16.0) + t)
    v += np.sin((px - py) * (1.0 / 16.0) + t)
    idx = ((v + 4.0) * (1.0 / 8.0) * (len(chars) - 1)).astype(np.int32)
    np.clip(idx, 0, len(chars) - 1, out=idx)
    return idx


class FireState:
    """Vectorized fire simulation."""
    def __init__(self, w=GRID_W, h=GRID_H):
        self.w = w
        self.h = h
        self.heat = np.zeros((h, w), dtype=np.float32)
        self.heat[-1] = 100.0

    def step(self, chars):
        heat = self.heat
        below = heat[1:, :]
        below_left = np.zeros_like(below)
        below_left[:, 1:] = below[:, :-1]
        below_right = np.zeros_like(below)
        below_right[:, :-1] = below[:, 1:]
        cur = heat[:-1, :]

        avg = (below + below_left + below_right + cur) * 0.25
        avg -= np.random.randint(0, 4, size=avg.shape).astype(np.float32)
        np.maximum(avg, 0.0, out=avg)

        new_heat = np.empty_like(heat)
        new_heat[:-1, :] = avg
        new_heat[-1, :] = 100.0 - np.random.randint(0, 10, size=self.w).astype(np.float32)
        self.heat = new_heat

        idx = (new_heat * (1.0 / 100.0) * (len(chars) - 1)).astype(np.int32)
        np.clip(idx, 0, len(chars) - 1, out=idx)
        return idx


def indices_to_text(idx, chars):
    """Fast 2D index array -> text via a single buffer join."""
    # Use UTF-32 so any unicode char (e.g. blocks ░▒▓█) is exactly 4 bytes
    chars_arr = np.frombuffer(chars.encode('utf-32-le'), dtype=np.uint32)
    flat = chars_arr[idx]  # (H, W) uint32
    
    h, w = flat.shape
    out = np.empty((h, w + 1), dtype=np.uint32)
    out[:, :w] = flat
    out[:, w] = ord('\n')
    return out.tobytes().decode('utf-32-le').rstrip('\n')


# ==========================================
# Glyph atlas (batch text rendering)
# ==========================================

class GlyphAtlas:
    """Pre-render each character once; assemble frames with numpy gather."""

    def __init__(self, chars, font):
        self.chars = chars

        probe = QImage(10, 10, QImage.Format_RGB32)
        p = QPainter(probe); p.setFont(font)
        fm = p.fontMetrics()
        self.char_w = max(1, fm.horizontalAdvance('M'))
        self.char_h = max(1, fm.height())
        self.ascent = fm.ascent()
        p.end()

        self.atlas = np.zeros((len(chars), self.char_h, self.char_w, 3), dtype=np.uint8)
        for i, ch in enumerate(chars):
            qi = QImage(self.char_w, self.char_h, QImage.Format_RGB32)
            qi.fill(0xff000000)
            painter = QPainter(qi)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(font)
            painter.drawText(0, self.ascent, ch)
            painter.end()

            bpl = qi.bytesPerLine()
            ptr = qi.constBits()
            ptr.setsize(qi.sizeInBytes())
            arr = np.frombuffer(ptr, dtype=np.uint8).reshape(self.char_h, bpl // 4, 4)
            self.atlas[i] = np.ascontiguousarray(arr[:, :self.char_w, :3])

    def render(self, idx_array, width, height):
        rows, cols = idx_array.shape
        text_h = rows * self.char_h
        text_w = cols * self.char_w

        glyphs = self.atlas[idx_array]
        text_img = np.ascontiguousarray(
            glyphs.transpose(0, 2, 1, 3, 4).reshape(text_h, text_w, 3)
        )

        if text_w == width and text_h == height:
            return text_img

        final = np.zeros((height, width, 3), dtype=np.uint8)
        y_off = (height - text_h) // 2
        x_off = (width - text_w) // 2
        sy = max(0, -y_off); sx = max(0, -x_off)
        dy = max(0, y_off);  dx = max(0, x_off)
        h = min(text_h - sy, height - dy)
        w = min(text_w - sx, width - dx)
        if h > 0 and w > 0:
            final[dy:dy + h, dx:dx + w] = text_img[sy:sy + h, sx:sx + w]
        return final


# ==========================================
# Video Export Thread (self-contained)
# ==========================================

class VideoExportThread(QThread):
    progress = Signal(int, str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, anim_type, chars, output_path, duration, fps, width, height, audio_path=None):
        super().__init__()
        self.output_path = output_path
        self.duration = duration
        self.fps = fps
        self.width = width
        self.height = height
        self.audio_path = audio_path
        self.is_running = True
        self.anim_type = anim_type
        self.chars = chars

    def stop(self):
        self.is_running = False

    def run(self):
        try:
            total_frames = int(self.duration * self.fps)

            font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            font.setPointSize(self._compute_font_size())
            atlas = GlyphAtlas(self.chars, font)

            px = py = None
            t = 0.0
            fire = None
            if self.anim_type == 'plasma':
                px, py = np.meshgrid(np.arange(GRID_W, dtype=np.float32),
                                     np.arange(GRID_H, dtype=np.float32))
            elif self.anim_type == 'fire':
                fire = FireState(GRID_W, GRID_H)

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            temp_path = self.output_path.replace('.mp4', '_temp.mp4')
            out = cv2.VideoWriter(temp_path, fourcc, self.fps,
                                  (self.width, self.height))
            if not out.isOpened():
                self.error.emit("Failed to initialize video writer")
                return

            plasma_dt = 0.05
            try:
                for frame_idx in range(total_frames):
                    if not self.is_running:
                        break

                    if self.anim_type == 'plasma':
                        idx_array = make_plasma_indices(t, self.chars, px, py)
                        t += plasma_dt
                    elif self.anim_type == 'fire':
                        idx_array = fire.step(self.chars)
                    else:
                        idx_array = np.zeros((GRID_H, GRID_W), dtype=np.int32)

                    out.write(atlas.render(idx_array, self.width, self.height))

                    if (frame_idx & 7) == 0 or frame_idx == total_frames - 1:
                        progress = int((frame_idx + 1) / total_frames * 100)
                        eta = (total_frames - frame_idx - 1) / self.fps
                        self.progress.emit(
                            progress,
                            f"Frame {frame_idx + 1}/{total_frames} | ETA: {eta:.1f}s"
                        )
            finally:
                out.release()

            if not self.is_running:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return

            if self.audio_path:
                self._add_audio(temp_path, self.output_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            else:
                os.rename(temp_path, self.output_path)

            self.finished.emit(self.output_path)
        except Exception as e:
            self.error.emit(f"Export failed: {str(e)}")

    def _compute_font_size(self):
        max_w = self.width / GRID_W
        max_h = self.height / GRID_H
        aspect = 0.55
        char_h = min(max_h, max_w / aspect)
        return max(6, min(72, int(char_h * 0.75)))

    def _add_audio(self, video_path, output_path):
        try:
            cmd = ['ffmpeg', '-i', video_path, '-i', self.audio_path,
                   '-c:v', 'copy', '-c:a', 'aac', '-shortest', output_path, '-y']
            subprocess.run(cmd, check=True, capture_output=True)
        except Exception:
            shutil.copy(video_path, output_path)


# ==========================================
# Export Dialog
# ==========================================

class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export to YouTube Shorts")
        self.resize(500, 450)
        self._init_ui()
        
    def _init_ui(self):
        layout = QVBoxLayout(self)

        info_group = QGroupBox("YouTube Shorts Format")
        info_layout = QFormLayout()
        info_layout.addRow(QLabel("Aspect Ratio: 9:16 (Vertical)"))
        info_layout.addRow(QLabel("Max Duration: 60 seconds"))
        info_layout.addRow(QLabel("Recommended FPS: 30"))
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)

        settings_group = QGroupBox("Export Settings")
        s = QFormLayout()
        
        self.res_combo = QComboBox()
        self.res_combo.addItems(["Custom", "SD (480p)", "HD (720p)", "Full HD (1080p)", "QHD (1440p)", "4K UHD (2160p)"])
        self.res_combo.currentIndexChanged.connect(self._on_res_changed)
        s.addRow("Resolution Preset:", self.res_combo)

        self.duration_spin = QDoubleSpinBox(); self.duration_spin.setRange(1, 60)
        self.duration_spin.setValue(15); self.duration_spin.setSuffix(" seconds")
        s.addRow("Duration:", self.duration_spin)
        
        self.fps_spin = QSpinBox(); self.fps_spin.setRange(15, 60)
        self.fps_spin.setValue(30); self.fps_spin.setSuffix(" FPS")
        s.addRow("Frame Rate:", self.fps_spin)
        
        self.width_spin = QSpinBox(); self.width_spin.setRange(256, 3840)
        self.width_spin.setValue(1080); self.width_spin.setSingleStep(2)
        s.addRow("Width:", self.width_spin)
        
        self.height_spin = QSpinBox(); self.height_spin.setRange(460, 3840)
        self.height_spin.setValue(1920); self.height_spin.setSingleStep(2)
        s.addRow("Height:", self.height_spin)

        self.palette_combo = QComboBox()
        self.palette_combo.addItems(list(PALETTES.keys()))
        s.addRow("Palette:", self.palette_combo)

        audio_layout = QHBoxLayout()
        self.audio_input = QLineEdit()
        self.audio_input.setPlaceholderText("Optional: Select audio file...")
        audio_layout.addWidget(self.audio_input)
        audio_btn = QPushButton("Browse...")
        audio_btn.clicked.connect(self._browse_audio)
        audio_layout.addWidget(audio_btn)
        s.addRow("Audio:", audio_layout)
        
        settings_group.setLayout(s)
        layout.addWidget(settings_group)

        preview_group = QGroupBox("Preview")
        pv = QVBoxLayout()
        self.preview_label = QLabel("Vertical format preview will appear here")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #333; color: white; padding: 20px;")
        pv.addWidget(self.preview_label)
        preview_group.setLayout(pv)
        layout.addWidget(preview_group)

        btns = QHBoxLayout()
        self.export_btn = QPushButton("Export Video")
        self.export_btn.clicked.connect(self.accept)
        self.export_btn.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        btns.addWidget(self.export_btn)
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self.width_spin.valueChanged.connect(self._on_spin_changed)
        self.height_spin.valueChanged.connect(self._on_spin_changed)
        self._update_preview()

    def _on_res_changed(self, idx):
        res_map = {
            0: None,
            1: (480, 854),
            2: (720, 1280),
            3: (1080, 1920),
            4: (1440, 2560),
            5: (2160, 3840)
        }
        dims = res_map.get(idx)
        if dims:
            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(dims[0])
            self.height_spin.setValue(dims[1])
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
            self._update_preview()

    def _on_spin_changed(self):
        if self.res_combo.currentIndex() != 0:
            self.res_combo.blockSignals(True)
            self.res_combo.setCurrentIndex(0)
            self.res_combo.blockSignals(False)
        self._update_preview()

    def _browse_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Audio File", "",
            "Audio Files (*.mp3 *.wav *.ogg *.m4a);;All Files (*)")
        if path:
            self.audio_input.setText(path)

    def _update_preview(self):
        w = self.width_spin.value(); h = self.height_spin.value()
        pw = 200; ph = int(pw * h / w)
        img = QImage(pw, ph, QImage.Format_RGB888); img.fill(QColor(50, 50, 50))
        painter = QPainter(img)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(img.rect(), Qt.AlignCenter, f"{w}x{h}\n9:16")
        painter.end()
        self.preview_label.setPixmap(QPixmap.fromImage(img))

    def get_settings(self):
        return {
            'duration': self.duration_spin.value(),
            'fps': self.fps_spin.value(),
            'width': self.width_spin.value(),
            'height': self.height_spin.value(),
            'audio': self.audio_input.text() or None,
            'palette': PALETTES[self.palette_combo.currentText()]
        }


# ==========================================
# Progress Dialog
# ==========================================

class ProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exporting Video...")
        self.resize(400, 150)
        self.setModal(True)
        layout = QVBoxLayout(self)
        self.status_label = QLabel("Preparing export...")
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(self.cancel_btn)

    def update_progress(self, value, status):
        self.progress_bar.setValue(value)
        self.status_label.setText(status)


# ==========================================
# Main Window
# ==========================================

def get_monospace_font(size=10):
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    return font


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ASCII Art Studio - YouTube Shorts Export")
        self.resize(1200, 900)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.setStatusBar(QStatusBar())

        self._create_menus()
        self._add_sample_tabs()

        self.export_thread = None
        self.progress_dialog = None

    def _create_menus(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        export_action = file_menu.addAction("Export to YouTube Shorts...")
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_to_youtube)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction("About", self._show_about)

    def _add_sample_tabs(self):
        self.tabs.addTab(self._create_plasma_tab(), "🌊 Plasma")
        self.tabs.addTab(self._create_fire_tab(), "🔥 Fire")

    def _create_plasma_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        tab.anim_type = 'plasma'
        tab.chars = PALETTES["Standard"]
        tab.time = 0.0
        tab._px, tab._py = np.meshgrid(np.arange(GRID_W, dtype=np.float32),
                                       np.arange(GRID_H, dtype=np.float32))

        ctrl = QHBoxLayout()
        play_btn = QPushButton("Pause")
        ctrl.addWidget(play_btn)
        
        ctrl.addWidget(QLabel("Palette:"))
        palette_combo = QComboBox()
        palette_combo.addItems(list(PALETTES.keys()))
        def on_palette_change(text):
            tab.chars = PALETTES[text]
            self._update_plasma(tab)
        palette_combo.currentTextChanged.connect(on_palette_change)
        ctrl.addWidget(palette_combo)
        
        ctrl.addStretch()
        layout.addLayout(ctrl)

        tab.text_edit = QPlainTextEdit()
        tab.text_edit.setReadOnly(True)
        tab.text_edit.setFont(get_monospace_font(12))
        layout.addWidget(tab.text_edit)

        tab.timer = QTimer(tab)
        tab.timer.timeout.connect(lambda: self._update_plasma(tab))
        tab.timer.start(33)
        play_btn.clicked.connect(lambda: self._toggle_timer(tab, play_btn))

        self._update_plasma(tab)
        return tab

    def _update_plasma(self, tab):
        tab.time += 0.05
        idx = make_plasma_indices(tab.time, tab.chars, tab._px, tab._py)
        tab.text_edit.setPlainText(indices_to_text(idx, tab.chars))

    def _create_fire_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        tab.anim_type = 'fire'
        tab.chars = PALETTES["Standard"]
        tab.fire = FireState(GRID_W, GRID_H)

        ctrl = QHBoxLayout()
        play_btn = QPushButton("Pause")
        ctrl.addWidget(play_btn)

        ctrl.addWidget(QLabel("Palette:"))
        palette_combo = QComboBox()
        palette_combo.addItems(list(PALETTES.keys()))
        def on_palette_change(text):
            tab.chars = PALETTES[text]
            self._update_fire(tab)
        palette_combo.currentTextChanged.connect(on_palette_change)
        ctrl.addWidget(palette_combo)

        ctrl.addStretch()
        layout.addLayout(ctrl)

        tab.text_edit = QPlainTextEdit()
        tab.text_edit.setReadOnly(True)
        tab.text_edit.setFont(get_monospace_font(12))
        layout.addWidget(tab.text_edit)

        tab.timer = QTimer(tab)
        tab.timer.timeout.connect(lambda: self._update_fire(tab))
        tab.timer.start(50)
        play_btn.clicked.connect(lambda: self._toggle_timer(tab, play_btn))

        self._update_fire(tab)
        return tab

    def _update_fire(self, tab):
        idx = tab.fire.step(tab.chars)
        tab.text_edit.setPlainText(indices_to_text(idx, tab.chars))

    def _toggle_timer(self, tab, btn):
        if tab.timer.isActive():
            tab.timer.stop(); btn.setText("Play")
        else:
            tab.timer.start(); btn.setText("Pause")

    def export_to_youtube(self):
        current_tab = self.tabs.currentWidget()
        if not hasattr(current_tab, 'text_edit'):
            QMessageBox.warning(self, "Error", "Current tab doesn't support export")
            return

        dialog = ExportDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        settings = dialog.get_settings()

        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Video", "", "MP4 Files (*.mp4);;All Files (*)")
        if not output_path:
            return
        if not output_path.endswith('.mp4'):
            output_path += '.mp4'

        self.progress_dialog = ProgressDialog(self)
        self.progress_dialog.show()

        self.export_thread = VideoExportThread(
            current_tab.anim_type, 
            settings['palette'], 
            output_path,
            settings['duration'], settings['fps'],
            settings['width'], settings['height'], settings['audio'])

        self.export_thread.progress.connect(self.progress_dialog.update_progress)
        self.export_thread.finished.connect(self._on_export_finished)
        self.export_thread.error.connect(self._on_export_error)
        self.progress_dialog.cancel_btn.clicked.connect(self.export_thread.stop)
        self.export_thread.start()

    def _on_export_finished(self, output_path):
        self.progress_dialog.close()
        QMessageBox.information(
            self, "Success",
            f"Video exported successfully!\n\nFile: {output_path}\n\n"
            f"You can now upload this to YouTube Shorts!")

    def _on_export_error(self, error_msg):
        self.progress_dialog.close()
        QMessageBox.critical(self, "Export Error", error_msg)

    def _show_about(self):
        QMessageBox.about(
            self, "About",
            "ASCII Art Studio\n\n"
            "Export animations to YouTube Shorts format!\n\n"
            "Features:\n"
            "- Vertical video (9:16 aspect ratio)\n"
            "- Resolutions up to 4K UHD\n"
            "- Multiple ASCII Palettes\n"
            "- Custom duration and FPS\n"
            "- Optional audio track\n"
            "- Vectorized numpy animation\n"
            "- Glyph-atlas batch rendering")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())