# Clipboard Image Merger

Desktop utility that watches the clipboard for copied images, keeps the latest 10 captures, and lets you copy a vertically merged image based on your selection order.

## Run

```bat
run.bat
```

Or directly:

```bat
python app.py
```

## Dependencies

Install from the local requirements file:

```bat
pip install -r requirements.txt
```

## Build Single EXE

Build a one-file Windows executable with PyInstaller:

```bat
build_single_exe.bat
```

Or directly:

```bat
python build_single_exe.py
```

Build-time dependencies:

```bat
pip install -r requirements-build.txt
```

Expected output:

```text
clipboard_image_merger\dist\ClipboardImageMerger.exe
```
