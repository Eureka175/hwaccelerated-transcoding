
import os
import sys
import shutil
import subprocess
import zipfile
from pathlib import Path

# ==================== Configuration ====================
TARGET_PYTHON = r"C:\Users\吴汶睿\AppData\Local\Programs\Python\Python313\python.exe"
SOURCE_SCRIPT = "transcode_hw_main.py"
OUTPUT_NAME = "transcode_hw_main_1.1"
DIST_DIR = Path("dist")
BUILD_DIR = Path("build")
PACKAGE_DIR = Path("package")
ZIP_NAME = "transcode_hw_v1.1.zip"

# =======================================================

def check_target_python():
    """Verify the target Python interpreter exists."""
    p = Path(TARGET_PYTHON)
    if not p.exists():
        print(f"[ERROR] Target Python not found: {TARGET_PYTHON}")
        print("        Please update the TARGET_PYTHON variable in build.py")
        sys.exit(1)
    ver = subprocess.run([str(p), "--version"], capture_output=True, text=True)
    print(f"[OK] Target Python: {ver.stdout.strip() or ver.stderr.strip()}")
    return str(p)

def find_ffmpeg():
    """Auto-detect ffmpeg.exe and ffprobe.exe location."""
    print("\n[Locating FFmpeg]")

    # Method 1: shutil.which (PATH)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        dir_ = Path(ffmpeg).parent
        print(f"  Found in PATH: {dir_}")
        return dir_

    # Method 2: Common installation paths
    candidates = [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        r"C:\Program Files (x86)\ffmpeg\bin",
        r"C:\tools\ffmpeg\bin",
        r"C:\Users\吴汶睿\ffmpeg\bin",
        r"C:\Users\吴汶睿\Downloads\ffmpeg\bin",
        r"C:\Users\吴汶睿\Desktop\ffmpeg\bin",
    ]
    for p in candidates:
        fp = Path(p) / "ffmpeg.exe"
        fpr = Path(p) / "ffprobe.exe"
        if fp.exists() and fpr.exists():
            print(f"  Found in common path: {p}")
            return Path(p)

    # Method 3: Manual input
    print("  [WARN] Auto-detection failed. Please specify the FFmpeg directory manually.")
    print("         (This directory should contain both ffmpeg.exe and ffprobe.exe)")
    while True:
        user_input = input("  Enter path: ").strip().strip('"').strip("'")
        p = Path(user_input)
        if (p / "ffmpeg.exe").exists() and (p / "ffprobe.exe").exists():
            print(f"  Using user-specified path: {p}")
            return p
        print(f"  [ERROR] ffmpeg.exe or ffprobe.exe not found in this directory. Please try again.")

def install_pyinstaller(python_exe):
    """Ensure PyInstaller is installed in the target environment."""
    print("\n[Checking PyInstaller]")
    rc = subprocess.run(
        [python_exe, "-m", "pip", "show", "pyinstaller"],
        capture_output=True
    ).returncode
    if rc != 0:
        print("  Not installed. Installing PyInstaller now...")
        subprocess.check_call([python_exe, "-m", "pip", "install", "pyinstaller"])
        print("  [OK] Installation complete")
    else:
        print("  [OK] Already installed")

def clean_old_build():
    """Remove previous build artifacts."""
    print("\n[Cleaning old builds]")
    for d in [BUILD_DIR, DIST_DIR, PACKAGE_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Removed: {d}")
    if Path("__pycache__").exists():
        shutil.rmtree("__pycache__")
        print(f"  Removed: __pycache__")

def build_exe(python_exe):
    """Build single-file executable using PyInstaller."""
    print(f"\n[Building executable with PyInstaller]")
    print(f"  Source: {SOURCE_SCRIPT}")
    print(f"  Output: {OUTPUT_NAME}.exe")

    cmd = [
        python_exe, "-m", "PyInstaller",
        "--onefile",              # Single-file mode
        "--name", OUTPUT_NAME,    # Output filename
        "--clean",                # Clean cache
        "--noconfirm",            # Skip overwrite confirmation
        SOURCE_SCRIPT
    ]
    subprocess.check_call(cmd)
    print(f"  [OK] Build complete: {DIST_DIR / (OUTPUT_NAME + '.exe')}")

def create_package(ffmpeg_dir):
    """Create final distribution directory and ZIP archive."""
    print(f"\n[Creating distribution package]")
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Copy executable
    exe_src = DIST_DIR / f"{OUTPUT_NAME}.exe"
    exe_dst = PACKAGE_DIR / f"{OUTPUT_NAME}.exe"
    shutil.copy2(exe_src, exe_dst)
    print(f"  Copied: {exe_dst.name}")

    # Copy FFmpeg binaries
    ffmpeg_pkg = PACKAGE_DIR / "ffmpeg"
    ffmpeg_pkg.mkdir(exist_ok=True)
    for name in ["ffmpeg.exe", "ffprobe.exe"]:
        src = ffmpeg_dir / name
        dst = ffmpeg_pkg / name
        if src.exists():
            shutil.copy2(src, dst)
            size = dst.stat().st_size / 1024 / 1024
            print(f"  Copied: ffmpeg/{name} ({size:.1f} MB)")
        else:
            print(f"  [WARN] Not found: {src}")

    # Copy README
    readme = Path("README.md")
    if readme.exists():
        shutil.copy2(readme, PACKAGE_DIR / "README.md")
        print(f"  Copied: README.md")

    # Create ZIP
    zip_path = Path(ZIP_NAME)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PACKAGE_DIR):
            for file in files:
                file_path = Path(root) / file
                arcname = str(file_path.relative_to(PACKAGE_DIR))
                zf.write(file_path, arcname)

    zip_size = zip_path.stat().st_size / 1024 / 1024
    print(f"\n[OK] Distribution package: {zip_path.absolute()}")
    print(f"     Size: {zip_size:.1f} MB")

def verify_package():
    """Verify the distribution package structure."""
    print(f"\n[Verifying package]")
    exe = PACKAGE_DIR / f"{OUTPUT_NAME}.exe"
    ffmpeg = PACKAGE_DIR / "ffmpeg" / "ffmpeg.exe"
    ffprobe = PACKAGE_DIR / "ffmpeg" / "ffprobe.exe"

    ok = True
    for p, label in [(exe, "Main executable"), (ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")]:
        if p.exists():
            size = p.stat().st_size / 1024 / 1024
            print(f"  [OK] {label}: {p.name} ({size:.1f} MB)")
        else:
            print(f"  [FAIL] {label}: Missing!")
            ok = False
    return ok

def print_usage():
    """Print end-user usage instructions."""
    print("\n" + "=" * 50)
    print("Packaging complete!")
    print("=" * 50)
    print(f"\nDistribution package: {Path(ZIP_NAME).absolute()}")
    print(f"\nPackage structure:")
    print(f"  {OUTPUT_NAME}.exe")
    print(f"  ffmpeg/")
    print(f"    ├── ffmpeg.exe")
    print(f"    └── ffprobe.exe")
    print(f"  README.md")

def main():
    print("=" * 50)
    print(" transcode_hw_main Packaging Script")
    print("=" * 50)

    # Check source file
    if not Path(SOURCE_SCRIPT).exists():
        print(f"\n[ERROR] Source file not found: {SOURCE_SCRIPT}")
        print(f"        Please ensure build.py is in the same directory as {SOURCE_SCRIPT}")
        sys.exit(1)

    # Execute packaging pipeline
    python_exe = check_target_python()
    ffmpeg_dir = find_ffmpeg()
    install_pyinstaller(python_exe)
    clean_old_build()
    build_exe(python_exe)
    create_package(ffmpeg_dir)

    if verify_package():
        print_usage()
    else:
        print("\n[ERROR] Verification failed. Please check the logs above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
