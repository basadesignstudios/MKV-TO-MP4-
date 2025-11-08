"""MKV to MP4 Converter GUI application for Windows.

This script provides a drag-and-drop interface for converting MKV files
into MP4 containers using FFmpeg. It supports both stream copy when the
source streams are already compatible, and full transcoding with sensible
defaults when they are not.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from app_icon_data import APP_ICON_BYTES, write_app_icon


APP_TITLE = "MKV → MP4 Converter"
SUPPORTED_EXTENSIONS = {".mkv"}
DEFAULT_VIDEO_CODEC = "libx264"
DEFAULT_AUDIO_CODEC = "aac"
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_CRF = "21"

WM_DROPFILES = 0x0233
GWL_WNDPROC = -4


class DragAndDropSupport:
    """Registers a Tk window to accept native file drops on Windows."""

    def __init__(self, widget: tk.Tk, callback):
        self.widget = widget
        self.callback = callback
        self.hwnd: Optional[int] = None
        self._old_proc = None
        self._new_proc = None
        self._set_window_proc = None
        self.error: Optional[BaseException] = None

        if sys.platform != "win32":
            return

        self.hwnd = widget.winfo_id()
        try:
            self._register()
        except (AttributeError, OSError) as exc:
            # Some Windows configurations (notably older DLL builds) lack the
            # ordinals required for SetWindowLongPtrW/DragAcceptFiles. Record
            # the error so the caller can present a graceful fallback.
            self.error = exc
            self.hwnd = None

    def _register(self):
        HWND = wintypes.HWND
        UINT = wintypes.UINT
        WPARAM = wintypes.WPARAM
        LPARAM = wintypes.LPARAM
        LRESULT = wintypes.LRESULT

        def py_wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                self._handle_drop(wparam)
                return 0
            if self._old_proc:
                return ctypes.windll.user32.CallWindowProcW(
                    self._old_proc, hwnd, msg, wparam, lparam
                )
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wndproc_type = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
        self._new_proc = wndproc_type(py_wnd_proc)

        hwnd = HWND(self.hwnd)
        ctypes.windll.shell32.DragAcceptFiles(hwnd, True)

        user32 = ctypes.windll.user32
        set_window_long = getattr(user32, "SetWindowLongPtrW", None)
        if set_window_long is None:
            set_window_long = user32.SetWindowLongW
        self._set_window_proc = set_window_long

        # Store the original window procedure so we can forward messages.
        self._old_proc = self._set_window_proc(hwnd, GWL_WNDPROC, self._new_proc)

    def unregister(self):
        if sys.platform != "win32" or not self._old_proc or self.hwnd is None:
            return
        hwnd = wintypes.HWND(self.hwnd)
        if self._set_window_proc is not None:
            self._set_window_proc(hwnd, GWL_WNDPROC, self._old_proc)
        ctypes.windll.shell32.DragAcceptFiles(hwnd, False)
        self._old_proc = None

    def _handle_drop(self, hdrop):
        count = ctypes.windll.shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            length = ctypes.windll.shell32.DragQueryFileW(hdrop, index, None, 0) + 1
            buffer = ctypes.create_unicode_buffer(length)
            ctypes.windll.shell32.DragQueryFileW(hdrop, index, buffer, length)
            paths.append(buffer.value)
        ctypes.windll.shell32.DragFinish(hdrop)
        if paths:
            self.widget.after(0, lambda: self.callback(paths))


class ConversionError(Exception):
    pass


class MKVToMP4ConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("640x480")
        self.root.configure(bg="#f2f2f2")
        self.root.minsize(540, 420)

        icon_path = self._ensure_window_icon()
        if sys.platform == "win32" and icon_path is not None:
            try:
                self.root.iconbitmap(icon_path)
            except tk.TclError:
                pass

        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="Ready")

        self.file_queue: queue.Queue[Path] = queue.Queue()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.active_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.ffmpeg_path = self._resource_path("ffmpeg_bin/ffmpeg.exe")
        self.ffprobe_path = self._resource_path("ffmpeg_bin/ffprobe.exe")

        self._build_ui()
        self.drag_support = DragAndDropSupport(self.root, self.add_files)

        if getattr(self.drag_support, "error", None):
            self.drop_label.configure(
                text=(
                    "Drag-and-drop is unavailable on this system.\n"
                    "Use the Convert button or drop files onto the app icon."
                )
            )
            detail = str(self.drag_support.error) if self.drag_support.error else ""
            if detail:
                self._log(
                    f"Native drag-and-drop registration failed ({detail}). "
                    "Falling back to manual file selection."
                )
            else:
                self._log(
                    "Native drag-and-drop registration failed. "
                    "Falling back to manual file selection."
                )
            if sys.platform == "win32":
                self.root.after(
                    200,
                    lambda: messagebox.showwarning(
                        APP_TITLE,
                        "Native drag-and-drop could not be enabled on this system.\n"
                        "Please use the Convert button or drop files onto the app icon.",
                    ),
                )

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queues)

        missing = self._validate_binaries()
        if missing:
            messagebox.showerror(APP_TITLE, missing)

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------
    def _build_ui(self):
        container = ttk.Frame(self.root, padding=20)
        container.pack(fill=tk.BOTH, expand=True)

        drop_frame = ttk.Frame(container)
        drop_frame.pack(fill=tk.BOTH, expand=True)
        drop_frame.columnconfigure(0, weight=1)
        drop_frame.rowconfigure(0, weight=1)

        self.drop_label = tk.Label(
            drop_frame,
            text="Drag MKV files here or drop them onto this window.",
            bg="#e6e6e6",
            relief="solid",
            bd=1,
            justify="center",
            wraplength=400,
            font=("Segoe UI", 12),
            padx=20,
            pady=20,
        )
        self.drop_label.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        control_frame = ttk.Frame(container)
        control_frame.pack(fill=tk.X, pady=(15, 0))

        self.convert_button = ttk.Button(control_frame, text="Convert", command=self.select_files)
        self.convert_button.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(control_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(15, 0))

        self.status_label = ttk.Label(control_frame, textvariable=self.status_var)
        self.status_label.pack(side=tk.RIGHT, padx=(10, 0))

        log_frame = ttk.Frame(container)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))

        self.log_text = ScrolledText(log_frame, height=8, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------
    def add_files(self, paths: Iterable[str]):
        mkv_files = []
        for path_str in paths:
            path = Path(path_str).expanduser().resolve()
            if path.suffix.lower() in SUPPORTED_EXTENSIONS and path.is_file():
                mkv_files.append(path)
        if not mkv_files:
            self._log("No valid MKV files detected in drop.")
            return
        if not self._binaries_exist():
            self._log(
                "FFmpeg binaries missing. Please place ffmpeg.exe and ffprobe.exe inside the ffmpeg_bin folder."
            )
            return
        for path in mkv_files:
            self.file_queue.put(path)
            self._log(f"Queued: {path}")
        if not self.active_thread or not self.active_thread.is_alive():
            self._start_conversion_thread()

    def select_files(self):
        filenames = filedialog.askopenfilenames(
            title="Select MKV files",
            filetypes=[("MKV Files", "*.mkv"), ("All Files", "*.*")]
        )
        if filenames:
            self.add_files(filenames)

    def on_close(self):
        self.stop_event.set()
        if self.drag_support:
            self.drag_support.unregister()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Conversion handling
    # ------------------------------------------------------------------
    def _start_conversion_thread(self):
        self.active_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.active_thread.start()

    def _process_queue(self):
        while not self.file_queue.empty() and not self.stop_event.is_set():
            path = self.file_queue.get()
            try:
                self._set_status("Converting…")
                self._update_progress(0)
                self._convert_single(path)
                self._log(f"Finished: {path}")
            except ConversionError as exc:
                self._log(f"Error converting {path}: {exc}")
            finally:
                self.file_queue.task_done()
        self.root.after(0, self._on_queue_complete)

    def _on_queue_complete(self):
        if self.file_queue.empty():
            self._set_status("Done")
            self._update_progress(0)
            self._log("All queued conversions finished.")
            if sys.platform == "win32":
                self.root.after(100, lambda: messagebox.showinfo(APP_TITLE, "Conversion complete."))

    def _convert_single(self, input_path: Path):
        if not self._binaries_exist():
            raise ConversionError("FFmpeg binaries not found. Please ensure ffmpeg.exe and ffprobe.exe are present.")

        output_path = self._build_output_path(input_path)
        duration = self._probe_duration(input_path)

        copy_mode = self._is_stream_copy_compatible(input_path)
        has_audio = self._has_audio_stream(input_path)

        if copy_mode:
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-i",
                str(input_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output_path),
            ]
        else:
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-i",
                str(input_path),
                "-map",
                "0",
                "-c:v",
                DEFAULT_VIDEO_CODEC,
                "-preset",
                "medium",
                "-crf",
                DEFAULT_CRF,
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output_path),
            ]
            if has_audio:
                cmd[cmd.index("-movflags") : cmd.index("-movflags")] = [
                    "-c:a",
                    DEFAULT_AUDIO_CODEC,
                    "-b:a",
                    DEFAULT_AUDIO_BITRATE,
                ]

        self._run_ffmpeg(cmd, duration)

    def _build_output_path(self, input_path: Path) -> Path:
        output_path = input_path.with_suffix(".mp4")
        counter = 1
        while output_path.exists():
            output_path = input_path.with_name(f"{input_path.stem}_{counter}.mp4")
            counter += 1
        return output_path

    # ------------------------------------------------------------------
    # FFmpeg helpers
    # ------------------------------------------------------------------
    def _is_stream_copy_compatible(self, input_path: Path) -> bool:
        try:
            video_codec = self._probe_stream_codec(input_path, stream="v:0")
        except ConversionError:
            return False

        try:
            audio_codec = self._probe_stream_codec(input_path, stream="a:0")
        except ConversionError:
            audio_codec = ""

        video_ok = video_codec.lower() in {"h264", "avc", "mpeg4"}
        audio_ok = (not audio_codec) or audio_codec.lower() in {"aac", "mp3", "mp2"}
        return video_ok and audio_ok

    def _has_audio_stream(self, input_path: Path) -> bool:
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(input_path),
        ]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
        return bool(output)

    def _probe_stream_codec(self, input_path: Path, stream: str) -> str:
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise ConversionError(f"Unable to inspect streams with ffprobe: {exc}")
        if not output:
            raise ConversionError("Stream codec information not found.")
        return output

    def _probe_duration(self, input_path: Path) -> float:
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
            return float(output)
        except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
            return 0.0

    def _run_ffmpeg(self, cmd: List[str], duration: float):
        self._log("Running: " + " ".join(cmd))
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                universal_newlines=True,
            )
        except FileNotFoundError:
            raise ConversionError("FFmpeg executable not found.")

        last_progress = 0.0
        start_time = time.time()
        if not duration:
            duration = 1.0

        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            line = line.strip()
            if line:
                self._log(line)
            if line.startswith("out_time_ms="):
                try:
                    current = int(line.split("=", 1)[1]) / 1_000_000
                    progress = min((current / duration) * 100, 100)
                except ValueError:
                    progress = last_progress
                last_progress = progress
                self._update_progress(progress)
            elif line.startswith("progress=") and line.endswith("end"):
                self._update_progress(100)

        process.wait()
        if process.returncode != 0:
            raise ConversionError(f"FFmpeg exited with code {process.returncode}")
        elapsed = time.time() - start_time
        self._log(f"Completed in {elapsed:.1f} seconds.")
        self._update_progress(100)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _update_progress(self, value: float):
        self.root.after(0, self.progress_var.set, value)

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _set_status(self, text: str):
        self.root.after(0, self.status_var.set, text)

    def _poll_queues(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, message + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queues)

    def _validate_binaries(self) -> Optional[str]:
        if not self._binaries_exist():
            return (
                "Required binaries missing. Please place ffmpeg.exe and ffprobe.exe inside "
                "the ffmpeg_bin folder next to the executable."
            )
        return None

    def _binaries_exist(self) -> bool:
        return Path(self.ffmpeg_path).exists() and Path(self.ffprobe_path).exists()

    def _ensure_window_icon(self) -> Optional[str]:
        """Make sure a window icon exists on disk and return its path."""

        try:
            icon_dir = Path(tempfile.gettempdir()) / "mkv_to_mp4_converter"
            icon_dir.mkdir(parents=True, exist_ok=True)
            icon_path = icon_dir / "app_icon.ico"
            if not icon_path.exists() or icon_path.stat().st_size != len(APP_ICON_BYTES):
                write_app_icon(icon_path)
            return str(icon_path)
        except OSError:
            return None

    def _resource_path(self, relative: str) -> str:
        base_path = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
        return str(Path(base_path) / relative)


def handle_startup_arguments(app: MKVToMP4ConverterApp):
    if len(sys.argv) <= 1:
        return
    dropped_files = [arg for arg in sys.argv[1:] if Path(arg).suffix.lower() in SUPPORTED_EXTENSIONS]
    if dropped_files:
        app.add_files(dropped_files)


def main():
    root = tk.Tk()
    app = MKVToMP4ConverterApp(root)
    handle_startup_arguments(app)
    root.mainloop()


if __name__ == "__main__":
    main()
