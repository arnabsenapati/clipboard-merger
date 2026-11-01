from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


APP_NAME = "ClipboardImageMerger"
DEPLOY_EXE_PATH = Path(r"C:\Users\Arnab Senapati\Software\ClipboardMerger\ClipboardImageMerger.exe")


def ensure_module(module_name: str, package_name: str | None = None) -> None:
    try:
        __import__(module_name)
    except ImportError:
        package = package_name or module_name
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


def build_icon(icon_png: Path, icon_ico: Path) -> None:
    from PIL import Image

    icon_ico.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(icon_png) as image:
        image.save(icon_ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def stop_running_app() -> None:
    try:
        subprocess.run(
            ["taskkill", "/IM", f"{APP_NAME}.exe", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        time.sleep(1.0)


def deploy_and_launch(source_exe: Path, target_exe: Path) -> None:
    target_exe.parent.mkdir(parents=True, exist_ok=True)
    stop_running_app()
    shutil.copy2(source_exe, target_exe)
    subprocess.Popen([str(target_exe)], cwd=target_exe.parent)


def main() -> None:
    app_dir = Path(__file__).resolve().parent
    repo_root = app_dir.parent
    icon_png = repo_root / "icons" / "camera-big.png"
    icon_ico = app_dir / "build_assets" / "camera-big.ico"
    build_dir = app_dir / "build"
    dist_dir = app_dir / "dist"
    spec_path = app_dir / f"{APP_NAME}.spec"
    app_path = app_dir / "app.py"

    ensure_module("PyInstaller", "pyinstaller")
    ensure_module("PIL", "Pillow")

    if not icon_png.exists():
        raise FileNotFoundError(f"Icon file not found: {icon_png}")

    build_icon(icon_png, icon_ico)

    if build_dir.exists():
        shutil.rmtree(build_dir)
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    if spec_path.exists():
        spec_path.unlink()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(icon_ico),
        "--add-data",
        f"{repo_root / 'icons'};icons",
        str(app_path),
    ]

    print("Running:", " ".join(f'"{part}"' if " " in part else part for part in command))
    subprocess.check_call(command, cwd=app_dir)
    built_exe = dist_dir / f"{APP_NAME}.exe"
    deploy_and_launch(built_exe, DEPLOY_EXE_PATH)
    print(f"\nBuild complete: {built_exe}")
    print(f"Deployed to: {DEPLOY_EXE_PATH}")
    print("Application restarted.")


if __name__ == "__main__":
    main()
