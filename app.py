import sys
import math
import random
import time
import cv2
import numpy as np
from PySide6.QtWidgets import (QApplication, QMainWindow, QPlainTextEdit, QVBoxLayout,
                               QWidget, QSlider, QLabel, QComboBox, QCheckBox,
                               QFileDialog, QMessageBox, QHBoxLayout, QTabWidget,
                               QPushButton, QStatusBar, QLineEdit, QShortcut,
                               QDialog, QSpinBox, QDoubleSpinBox, QProgressBar,
                               QGroupBox, QFormLayout, QTextEdit)
from PySide6.QtGui import QFont, QFontDatabase, QKeySequence, QColor, QPainter, QImage, QPixmap
from PySide6.QtCore import Qt, QTimer, QPoint, QThread, Signal
from PIL import Image, ImageDraw, ImageFont

# ==========================================
# Video Export Thread
# ==========================================
class VideoExportThread(QThread):
    progress = Signal(int, str)  # percentage, status message
    finished = Signal(str)  # output file path
    error = Signal(str)
    
    def __init__(self, tab, output_path, duration, fps, width, height, audio_path=None):
        super().__init__()
        self.tab = tab
        self.output_path = output_path
        self.duration = duration
        self.fps = fps
        self.width = width
        self.height = height
        self.audio_path = audio_path
        self.is_running = True
        
    def stop(self):
        self.is_running = False
        
    def run(self):
        try:
            total_frames = int(self.duration * self.fps)
            
            # Initialize video writer
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            temp_path = self.output_path.replace('.mp4', '_temp.mp4')
            out = cv2.VideoWriter(temp_path, fourcc, self.fps, (self.width, self.height))
            
            if not out.isOpened():
                self.error.emit("Failed to initialize video writer")
                return
            
            # Capture frames
            for frame_idx in range(total_frames):
                if not self.is_running:
                    out.release()
                    return
                
                # Update animation
                self.tab.update_frame()
                QApplication.processEvents()
                
                # Render to image
                frame_img = self.render_frame()
                
                # Convert to OpenCV format
                frame_cv = cv2.cvtColor(np.array(frame_img), cv2.COLOR_RGB2BGR)
                
                # Write frame
                out.write(frame_cv)
                
                # Update progress
                progress = int((frame_idx + 1) / total_frames * 100)
                eta = (total_frames - frame_idx - 1) / self.fps
                self.progress.emit(progress, f"Frame {frame_idx + 1}/{total_frames} | ETA: {eta:.1f}s")
            
            out.release()
            
            # Add audio if provided
            if self.audio_path:
                self.add_audio(temp_path, self.output_path)
                import os
                os.remove(temp_path)
            else:
                import os
                os.rename(temp_path, self.output_path)
            
            self.finished.emit(self.output_path)
            
        except Exception as e:
            self.error.emit(f"Export failed: {str(e)}")
    
    def render_frame(self):
        """Render current animation frame to image"""
        # Get ASCII text
        text = self.tab.text_edit.toPlainText()
        lines = text.split('\n')
        
        # Create image
        img = Image.new('RGB', (self.width, self.height), color=(0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Try to load monospace font
        try:
            # Try common system fonts
            font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",  # Linux
                "C:\\Windows\\Fonts\\consola.ttf",  # Windows
                "/System/Library/Fonts/Menlo.ttc",  # macOS
                "C:\\Windows\\Fonts\\cour.ttf",  # Windows Courier
            ]
            
            font = None
            for fp in font_paths:
                try:
                    font = ImageFont.truetype(fp, 14)
                    break
                except:
                    continue
            
            if not font:
                font = ImageFont.load_default()
        except:
            font = ImageFont.load_default()
        
        # Calculate character dimensions
        char_width = 8
        char_height = 16
        
        # Center text in frame
        text_width = max(len(line) for line in lines) * char_width
        text_height = len(lines) * char_height
        
        x_offset = (self.width - text_width) // 2
        y_offset = (self.height - text_height) // 2
        
        # Check if color mode is enabled
        if hasattr(self.tab, 'color_mode') and self.tab.color_mode:
            # Render with colors from HTML
            self.render_colored_text(draw, lines, x_offset, y_offset, font, char_width, char_height)
        else:
            # Render in white
            for i, line in enumerate(lines):
                y = y_offset + i * char_height
                draw.text((x_offset, y), line, fill=(255, 255, 255), font=font)
        
        return img
    
    def render_colored_text(self, draw, lines, x_offset, y_offset, font, char_width, char_height):
        """Render text with colors extracted from HTML"""
        # Get HTML content
        html = self.tab.text_edit.toHtml()
        
        # Simple color extraction (this is a simplified version)
        # In production, you'd parse the HTML properly
        for i, line in enumerate(lines):
            y = y_offset + i * char_height
            for j, char in enumerate(line):
                x = x_offset + j * char_width
                # Default to white, could be enhanced with proper HTML parsing
                draw.text((x, y), char, fill=(255, 255, 255), font=font)
    
    def add_audio(self, video_path, output_path):
        """Add audio track to video using ffmpeg"""
        try:
            import subprocess
            cmd = [
                'ffmpeg', '-i', video_path,
                '-i', self.audio_path,
                '-c:v', 'copy', '-c:a', 'aac',
                '-shortest', output_path,
                '-y'
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        except Exception as e:
            # If ffmpeg fails, just copy video without audio
            import shutil
            shutil.copy(video_path, output_path)

# ==========================================
# Export Dialog
# ==========================================
class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export to YouTube Shorts")
        self.resize(500, 400)
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Format info
        info_group = QGroupBox("YouTube Shorts Format")
        info_layout = QFormLayout()
        info_layout.addRow(QLabel("Aspect Ratio: 9:16 (Vertical)"))
        info_layout.addRow(QLabel("Resolution: 1080x1920 pixels"))
        info_layout.addRow(QLabel("Max Duration: 60 seconds"))
        info_layout.addRow(QLabel("Recommended FPS: 30"))
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        # Settings
        settings_group = QGroupBox("Export Settings")
        settings_layout = QFormLayout()
        
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(1, 60)
        self.duration_spin.setValue(15)
        self.duration_spin.setSuffix(" seconds")
        settings_layout.addRow("Duration:", self.duration_spin)
        
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(15, 60)
        self.fps_spin.setValue(30)
        self.fps_spin.setSuffix(" FPS")
        settings_layout.addRow("Frame Rate:", self.fps_spin)
        
        self.width_spin = QSpinBox()
        self.width_spin.setRange(540, 1080)
        self.width_spin.setValue(1080)
        self.width_spin.setSingleStep(10)
        settings_layout.addRow("Width:", self.width_spin)
        
        self.height_spin = QSpinBox()
        self.height_spin.setRange(960, 1920)
        self.height_spin.setValue(1920)
        self.height_spin.setSingleStep(10)
        settings_layout.addRow("Height:", self.height_spin)
        
        # Audio file
        audio_layout = QHBoxLayout()
        self.audio_input = QLineEdit()
        self.audio_input.setPlaceholderText("Optional: Select audio file...")
        audio_layout.addWidget(self.audio_input)
        
        audio_btn = QPushButton("Browse...")
        audio_btn.clicked.connect(self.browse_audio)
        audio_layout.addWidget(audio_btn)
        
        settings_layout.addRow("Audio:", audio_layout)
        
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        
        # Preview
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout()
        self.preview_label = QLabel("Vertical format preview will appear here")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #333; color: white; padding: 20px;")
        preview_layout.addWidget(self.preview_label)
        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.export_btn = QPushButton("Export Video")
        self.export_btn.clicked.connect(self.accept)
        self.export_btn.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        button_layout.addWidget(self.export_btn)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)
        
        # Connect signals for preview update
        self.width_spin.valueChanged.connect(self.update_preview)
        self.height_spin.valueChanged.connect(self.update_preview)
        self.update_preview()
        
    def browse_audio(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Audio File", "",
            "Audio Files (*.mp3 *.wav *.ogg *.m4a);;All Files (*)"
        )
        if file_path:
            self.audio_input.setText(file_path)
    
    def update_preview(self):
        """Update preview to show vertical format"""
        w = self.width_spin.value()
        h = self.height_spin.value()
        
        # Create preview image
        preview_w = 200
        preview_h = int(preview_w * h / w)
        
        img = QImage(preview_w, preview_h, QImage.Format_RGB888)
        img.fill(QColor(50, 50, 50))
        
        painter = QPainter(img)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(img.rect(), Qt.AlignCenter, f"{w}x{h}\n9:16")
        painter.end()
        
        pixmap = QPixmap.fromImage(img)
        self.preview_label.setPixmap(pixmap)
    
    def get_settings(self):
        return {
            'duration': self.duration_spin.value(),
            'fps': self.fps_spin.value(),
            'width': self.width_spin.value(),
            'height': self.height_spin.value(),
            'audio': self.audio_input.text() if self.audio_input.text() else None
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
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        self.status_label = QLabel("Preparing export...")
        layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(self.cancel_btn)
    
    def update_progress(self, value, status):
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

# ==========================================
# Main Window with Export Feature
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ASCII Art Studio - YouTube Shorts Export")
        self.resize(1200, 900)
        
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # Create menus
        self.create_menus()
        
        # Add sample tabs (simplified for demo)
        self.add_sample_tabs()
        
        self.export_thread = None
        self.progress_dialog = None
        
    def create_menus(self):
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        export_action = file_menu.addAction("Export to YouTube Shorts...")
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_to_youtube)
        
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction("About", self.show_about)
    
    def add_sample_tabs(self):
        """Add sample animation tabs for testing"""
        # Add a simple plasma animation tab
        plasma_tab = self.create_plasma_tab()
        self.tabs.addTab(plasma_tab, "🌊 Plasma")
        
        # Add a fire animation tab
        fire_tab = self.create_fire_tab()
        self.tabs.addTab(fire_tab, "🔥 Fire")
    
    def create_plasma_tab(self):
        """Create a simple plasma animation tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        tab.time = 0.0
        tab.chars = " .:-=+*#%@"
        tab.color_mode = False
        
        # Controls
        control_layout = QHBoxLayout()
        play_btn = QPushButton("Pause")
        control_layout.addWidget(play_btn)
        control_layout.addStretch()
        layout.addLayout(control_layout)
        
        # Text display
        tab.text_edit = QPlainTextEdit()
        tab.text_edit.setReadOnly(True)
        tab.text_edit.setFont(get_monospace_font(12))
        layout.addWidget(tab.text_edit)
        
        # Timer
        tab.timer = QTimer(tab)
        tab.timer.timeout.connect(lambda: self.update_plasma(tab))
        tab.timer.start(33)
        
        play_btn.clicked.connect(lambda: self.toggle_timer(tab, play_btn))
        
        return tab
    
    def update_plasma(self, tab):
        """Update plasma animation"""
        tab.time += 0.05
        w, h = 80, 30
        
        out = []
        for y in range(h):
            row = []
            for x in range(w):
                v = math.sin(x / 16.0 + tab.time)
                v += math.sin(y / 8.0 + tab.time)
                v += math.sin((x + y) / 16.0 + tab.time)
                v += math.sin((x - y) / 16.0 + tab.time)
                
                idx = int((v + 4) / 8 * (len(tab.chars) - 1))
                idx = max(0, min(len(tab.chars) - 1, idx))
                row.append(tab.chars[idx])
            out.append("".join(row))
        
        tab.text_edit.setPlainText("\n".join(out))
    
    def create_fire_tab(self):
        """Create a simple fire animation tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        tab.chars = " .:-=+*#%@"
        tab.heat = []
        tab.color_mode = False
        
        # Controls
        control_layout = QHBoxLayout()
        play_btn = QPushButton("Pause")
        control_layout.addWidget(play_btn)
        control_layout.addStretch()
        layout.addLayout(control_layout)
        
        # Text display
        tab.text_edit = QPlainTextEdit()
        tab.text_edit.setReadOnly(True)
        tab.text_edit.setFont(get_monospace_font(12))
        layout.addWidget(tab.text_edit)
        
        # Timer
        tab.timer = QTimer(tab)
        tab.timer.timeout.connect(lambda: self.update_fire(tab))
        tab.timer.start(50)
        
        play_btn.clicked.connect(lambda: self.toggle_timer(tab, play_btn))
        
        return tab
    
    def update_fire(self, tab):
        """Update fire animation"""
        w, h = 80, 30
        
        if not tab.heat or len(tab.heat) != w * h:
            tab.heat = [0] * (w * h)
            for x in range(w):
                tab.heat[(h - 1) * w + x] = 100
        
        new_heat = [0] * (w * h)
        for y in range(h - 1):
            for x in range(w):
                idx = y * w + x
                val = tab.heat[idx + w]
                if x > 0:
                    val += tab.heat[idx + w - 1]
                if x < w - 1:
                    val += tab.heat[idx + w + 1]
                val += tab.heat[idx]
                val //= 4
                val -= random.randint(0, 3)
                new_heat[idx] = max(0, val)
        
        tab.heat = new_heat
        
        out = []
        for y in range(h):
            row = []
            for x in range(w):
                val = tab.heat[y * w + x]
                idx = int(val / 100 * (len(tab.chars) - 1))
                row.append(tab.chars[idx])
            out.append("".join(row))
        
        tab.text_edit.setPlainText("\n".join(out))
    
    def toggle_timer(self, tab, btn):
        """Toggle animation timer"""
        if tab.timer.isActive():
            tab.timer.stop()
            btn.setText("Play")
        else:
            tab.timer.start()
            btn.setText("Pause")
    
    def export_to_youtube(self):
        """Export current animation to YouTube Shorts format"""
        current_tab = self.tabs.currentWidget()
        
        if not hasattr(current_tab, 'text_edit'):
            QMessageBox.warning(self, "Error", "Current tab doesn't support export")
            return
        
        # Show export dialog
        dialog = ExportDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        
        settings = dialog.get_settings()
        
        # Ask for output file
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Video", "",
            "MP4 Files (*.mp4);;All Files (*)"
        )
        
        if not output_path:
            return
        
        if not output_path.endswith('.mp4'):
            output_path += '.mp4'
        
        # Show progress dialog
        self.progress_dialog = ProgressDialog(self)
        self.progress_dialog.show()
        
        # Start export thread
        self.export_thread = VideoExportThread(
            current_tab,
            output_path,
            settings['duration'],
            settings['fps'],
            settings['width'],
            settings['height'],
            settings['audio']
        )
        
        self.export_thread.progress.connect(self.progress_dialog.update_progress)
        self.export_thread.finished.connect(self.on_export_finished)
        self.export_thread.error.connect(self.on_export_error)
        
        self.progress_dialog.cancel_btn.clicked.connect(self.export_thread.stop)
        
        self.export_thread.start()
    
    def on_export_finished(self, output_path):
        """Handle export completion"""
        self.progress_dialog.close()
        QMessageBox.information(
            self, "Success",
            f"Video exported successfully!\n\n"
            f"File: {output_path}\n\n"
            f"You can now upload this to YouTube Shorts!"
        )
    
    def on_export_error(self, error_msg):
        """Handle export error"""
        self.progress_dialog.close()
        QMessageBox.critical(self, "Export Error", error_msg)
    
    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(
            self, "About",
            "ASCII Art Studio\n\n"
            "Export animations to YouTube Shorts format!\n\n"
            "Features:\n"
            "- Vertical video (9:16 aspect ratio)\n"
            "- Custom duration and FPS\n"
            "- Optional audio track\n"
            "- Real-time progress tracking"
        )

def get_monospace_font(size=10):
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    return font

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())