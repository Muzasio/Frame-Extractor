#!/usr/bin/env python3
import sys
import os
import logging
import threading
import cv2
import numpy as np
from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QSlider, QVBoxLayout,
                              QHBoxLayout, QPushButton, QFileDialog, QMessageBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence


class BestSourceBackend:
    def __init__(self, path):
        import vapoursynth as vs
        logging.disable(logging.CRITICAL)
        try:
            core = vs.core
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(path)), ".bsindex")
            os.makedirs(cache_dir, exist_ok=True)
            clip = core.bs.VideoSource(source=path, cachepath=cache_dir)
            self.clip = core.resize.Bicubic(clip, format=vs.RGB24, matrix_in_s="709")
            self.total_frames = len(self.clip)
            self.fps = float(clip.fps_num) / float(clip.fps_den) if clip.fps_den else 30.0
            self.width = self.clip.width
            self.height = self.clip.height
        finally:
            logging.disable(logging.NOTSET)

    def get_frame_rgb(self, idx):
        idx = max(0, min(self.total_frames - 1, idx))
        vsframe = self.clip.get_frame(idx)
        planes = [np.asarray(vsframe[p]) for p in range(3)]
        return np.dstack(planes)

    def release(self):
        pass


class OpenCVBackend:
    def __init__(self, path):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video: {path}")
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def get_frame_rgb(self, idx, max_retries=3):
        idx = max(0, min(self.total_frames - 1, idx))
        target = idx
        frame = None
        for _ in range(max_retries):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ret, frame = self.cap.read()
            if not ret:
                return None
            # POS_FRAMES after read() reports the *next* frame to be read
            actual = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            if actual == idx:
                break
            target += (idx - actual)
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def release(self):
        self.cap.release()


def open_backend(path, bestsource_timeout=6):
    result = {}

    def target():
        try:
            result["backend"] = BestSourceBackend(path)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=bestsource_timeout)

    if t.is_alive():
        return OpenCVBackend(path), "OpenCV (BestSource timed out, fell back)"
    if "backend" in result:
        return result["backend"], "BestSource (frame-accurate)"
    return OpenCVBackend(path), "OpenCV (seek-verify fallback)"


class LoaderThread(QThread):
    loaded = pyqtSignal(object, str)
    failed = pyqtSignal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            backend, name = open_backend(self.path)
            self.loaded.emit(backend, name)
        except Exception as e:
            self.failed.emit(str(e))


class FrameExtractor(QWidget):
    def __init__(self, video_path=None):
        super().__init__()
        self.setAcceptDrops(True)

        self.backend = None
        self.backend_name = ""
        self.video_path = None
        self.video_name = ""
        self.total_frames = 0
        self.fps = 30.0
        self.view_start = 0
        self.view_end = 0
        self.current_frame = 0
        self.last_frame_rgb = None
        self.output_dir = ""
        self._loader = None
        self._pending_path = None

        self.setWindowTitle("FrameExtract - drop a video or click Open Video")
        self.resize(1000, 700)

        self.preview = QLabel("Drag a video here, or click Open Video (Ctrl+O)")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background-color: black; color: #888; font-size: 16px;")

        self.open_btn = QPushButton("Open Video")
        self.open_btn.clicked.connect(self.browse_video)

        self.output_btn = QPushButton("Output Folder")
        self.output_btn.clicked.connect(self.choose_output_dir)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.open_btn)
        top_bar.addWidget(self.output_btn)
        top_bar.addStretch()

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self.on_slider_change)
        self.slider.setEnabled(False)

        self.status = QLabel("no video loaded")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout()
        layout.addLayout(top_bar)
        layout.addWidget(self.preview, stretch=1)
        layout.addWidget(self.slider)
        layout.addWidget(self.status)
        self.setLayout(layout)

        self.slider.wheelEvent = self.slider_wheel

        QShortcut(QKeySequence(Qt.Key.Key_Left), self).activated.connect(lambda: self.step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self).activated.connect(lambda: self.step(1))
        QShortcut(QKeySequence("Shift+Left"), self).activated.connect(lambda: self.step(-10))
        QShortcut(QKeySequence("Shift+Right"), self).activated.connect(lambda: self.step(10))
        QShortcut(QKeySequence(Qt.Key.Key_S), self).activated.connect(self.save_frame)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self.save_frame)
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(self.browse_video)

        if video_path:
            self.load_new_video(video_path)

    def browse_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select video", "", "Video files (*.mp4 *.mkv *.avi *.mov *.webm)")
        if path:
            self.load_new_video(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        self.load_new_video(path)

    def load_new_video(self, path):
        if self._loader is not None and self._loader.isRunning():
            return
        self._pending_path = path
        self.open_btn.setEnabled(False)
        self.preview.setText(f"Loading {os.path.basename(path)}...")
        self.status.setText("indexing video, this can take a few seconds on first open...")
        self._loader = LoaderThread(path)
        self._loader.loaded.connect(self._on_backend_loaded)
        self._loader.failed.connect(self._on_backend_failed)
        self._loader.start()

    def _on_backend_loaded(self, new_backend, backend_name):
        path = self._pending_path
        if self.backend is not None:
            self.backend.release()
        self.backend = new_backend
        self.backend_name = backend_name
        self.video_path = path
        self.video_name = os.path.splitext(os.path.basename(path))[0]
        self.total_frames = self.backend.total_frames
        self.fps = self.backend.fps
        self.view_start = 0
        self.view_end = self.total_frames - 1
        self.current_frame = 0
        self.output_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                        "extracted_frames")
        os.makedirs(self.output_dir, exist_ok=True)
        self.setWindowTitle(f"FrameExtract - {os.path.basename(path)} [{backend_name}]")
        self.open_btn.setEnabled(True)
        self.slider.setEnabled(True)
        self.slider.blockSignals(True)
        self.slider.setMinimum(self.view_start)
        self.slider.setMaximum(self.view_end)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.show_frame(0)

    def _on_backend_failed(self, message):
        self.open_btn.setEnabled(True)
        self.preview.setText("Drag a video here, or click Open Video (Ctrl+O)")
        self.status.setText("no video loaded")
        QMessageBox.critical(self, "Error", f"Cannot open video: {self._pending_path}\n{message}")

    def slider_wheel(self, event):
        if self.backend is None:
            return
        delta = event.angleDelta().y()
        span = self.view_end - self.view_start
        if delta > 0:
            new_span = max(10, int(span * 0.6))
        else:
            new_span = min(self.total_frames - 1, int(span * 1.6) if span > 0 else 200)

        center = self.current_frame
        half = new_span // 2
        new_start = max(0, center - half)
        new_end = min(self.total_frames - 1, new_start + new_span)
        new_start = max(0, new_end - new_span)

        self.view_start = new_start
        self.view_end = new_end

        self.slider.blockSignals(True)
        self.slider.setMinimum(self.view_start)
        self.slider.setMaximum(self.view_end)
        self.slider.setValue(self.current_frame)
        self.slider.blockSignals(False)
        self.update_status()

    def on_slider_change(self, value):
        self.current_frame = value
        self.show_frame(value)

    def step(self, delta):
        if self.backend is None:
            return
        new_frame = max(0, min(self.total_frames - 1, self.current_frame + delta))
        if new_frame < self.view_start or new_frame > self.view_end:
            span = self.view_end - self.view_start
            self.view_start = max(0, new_frame - span // 2)
            self.view_end = min(self.total_frames - 1, self.view_start + span)
            self.view_start = max(0, self.view_end - span)
            self.slider.blockSignals(True)
            self.slider.setMinimum(self.view_start)
            self.slider.setMaximum(self.view_end)
            self.slider.blockSignals(False)
        self.slider.setValue(new_frame)

    def show_frame(self, frame_idx):
        if self.backend is None:
            return
        rgb = self.backend.get_frame_rgb(frame_idx)
        if rgb is None:
            return
        rgb = np.ascontiguousarray(rgb)
        self.last_frame_rgb = rgb
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg).scaled(
            self.preview.width(), self.preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pixmap)
        self.update_status()

    def update_status(self):
        t = self.current_frame / self.fps
        mm, ss = divmod(t, 60)
        self.status.setText(
            f"frame {self.current_frame}/{self.total_frames - 1}  |  "
            f"{int(mm):02d}:{ss:05.2f}  |  "
            f"zoom [{self.view_start}-{self.view_end}]  |  "
            f"{self.backend_name}  |  "
            f"out: {self.output_dir}"
        )

    def save_frame(self):
        if self.backend is None or self.last_frame_rgb is None:
            return
        filename = f"{self.video_name}_frame{self.current_frame:06d}.png"
        path = os.path.join(self.output_dir, filename)
        bgr = cv2.cvtColor(self.last_frame_rgb, cv2.COLOR_RGB2BGR)
        cv2.imwrite(path, bgr, [cv2.IMWRITE_PNG_COMPRESSION, 0])
        h, w = self.last_frame_rgb.shape[:2]
        self.setWindowTitle(f"saved {filename} ({w}x{h}px)")

    def choose_output_dir(self):
        start_dir = self.output_dir or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Output folder", start_dir)
        if d:
            self.output_dir = d
            self.update_status()

    def resizeEvent(self, event):
        self.show_frame(self.current_frame)
        super().resizeEvent(event)

    def closeEvent(self, event):
        if self.backend is not None:
            self.backend.release()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    video_path = sys.argv[1] if len(sys.argv) > 1 else None
    win = FrameExtractor(video_path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
