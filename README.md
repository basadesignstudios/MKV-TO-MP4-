# MKV → MP4 Converter

A drag-and-drop Windows desktop application that batches MKV files through FFmpeg
and outputs MP4 files in the same folder. The app automatically stream-copies
compatible codecs or transcodes to H.264/AAC when necessary.

## Features

- Native drag-and-drop onto the window or the executable icon
- Automatic stream copy when video is already H.264 and audio is AAC/MP3
- Progress bar with live FFmpeg log output
- Batch conversion queue with status updates
- Optional Windows notification on completion (message box)

## Prerequisites

Place the FFmpeg binaries next to the script/executable:

```
ffmpeg_bin/
  ├── ffmpeg.exe
  └── ffprobe.exe
```

The application will warn you if the binaries are missing.

### ⚙️ Local Setup
1. Download FFmpeg static build from https://www.gyan.dev/ffmpeg/builds/
2. Copy `ffmpeg.exe` and `ffprobe.exe` into `ffmpeg_bin/` inside the project folder.
3. (Optional) Generate the application icon for packaging with PyInstaller:
   ```bash
   python tools/generate_app_icon.py
   ```
4. Run the converter with:
   ```bash
   python mkv_to_mp4_converter.py
   ```

To build the standalone Windows app:

```
python tools/generate_app_icon.py
pyinstaller --onefile --noconsole --icon=app_icon.ico --add-binary "ffmpeg_bin/*;ffmpeg_bin" mkv_to_mp4_converter.py
```

## Running from Source

```bash
python mkv_to_mp4_converter.py
```

On launch you can drag MKV files into the window to begin conversion.

## Building the Standalone Executable

1. Ensure Python 3.10+ is installed.
2. Install PyInstaller:
   ```bash
   pip install pyinstaller
   ```
3. Generate the icon file (ignored by git but required for the `--icon` flag):
   ```bash
   python tools/generate_app_icon.py
   ```
4. Run the build command from the project root:
   ```bash
   pyinstaller --onefile --noconsole --icon=app_icon.ico --add-binary "ffmpeg_bin/*;ffmpeg_bin" mkv_to_mp4_converter.py
   ```
5. The resulting executable will be located at `dist/mkv_to_mp4_converter.exe`.

### 🖼️ Application Icon

- The repository stores the icon as base64 text (`app_icon_data.py`) to keep pull requests free of binary diffs.
- Run `python tools/generate_app_icon.py` whenever you need the physical `app_icon.ico` file for packaging.
- The application extracts the icon automatically at runtime, so running from source does not require manual steps.

## Notes

- Converted files are written to the same directory as their source MKV files.
- Existing MP4 files are not overwritten; numeric suffixes are appended instead.
- If FFmpeg encounters unsupported streams, error details are displayed in the log box.
- Keep binary artifacts (FFmpeg builds, generated executables, extra icons) out of commits.
- This keeps pull requests text-only for easier review.
