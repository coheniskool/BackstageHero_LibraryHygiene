"""
Build script -- produces BackstageHero.exe from VideoDownload.py.

Usage:
    python build.py

Requirements: Python 3.8+, internet connection (first run only).
PyInstaller and Pillow are installed automatically if missing.
The finished exe appears in dist\\BackstageHero.exe.
"""

import subprocess
import sys
import os


def pip_install(package):
    print(f'Installing {package}...')
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--quiet', package],
        check=True,
    )


# ── 1. Ensure PyInstaller is available ───────────────────────────────────────

check = subprocess.run(
    [sys.executable, '-m', 'PyInstaller', '--version'],
    capture_output=True,
)
if check.returncode != 0:
    pip_install('pyinstaller')


# ── 2. Convert PNG icon → ICO (via subprocess so Pillow is always importable) ─

icon_png = os.path.join('assets', 'icon.png')
icon_ico = os.path.join('assets', 'icon.ico')
icon_flag = []

if os.path.exists(icon_png):
    convert = (
        'from PIL import Image; '
        f'img = Image.open({repr(icon_png)}); '
        f'img.save({repr(icon_ico)}, format="ICO", '
        'sizes=[(256,256),(128,128),(64,64),(32,32),(16,16)])'
    )
    result = subprocess.run([sys.executable, '-c', convert], capture_output=True)
    if result.returncode != 0:
        pip_install('pillow')
        subprocess.run([sys.executable, '-c', convert], check=True)
    print('Icon converted.')
    icon_flag = ['--icon', icon_ico]
else:
    print('Warning: assets/icon.png not found, building without custom icon.')


# ── 3. Bundle ffmpeg if it is sitting next to this script ─────────────────────
# Placing ffmpeg.exe in this folder makes auto-sync (and 1080p remux) work out of
# the box, with nothing for the user to install. Build without it and the exe
# still runs; sync just needs an ffmpeg.exe at runtime instead.

ffmpeg_flag = []
if os.path.exists('ffmpeg.exe'):
    ffmpeg_flag = ['--add-binary', 'ffmpeg.exe' + os.pathsep + '.']
    print('Bundling ffmpeg.exe into the exe.')
else:
    print('No ffmpeg.exe in this folder - building without it.')
    print('(Auto-sync will then need an ffmpeg.exe next to the program at runtime.)')


# ── 4. Run PyInstaller ────────────────────────────────────────────────────────

print('\nBuilding BackstageHero.exe...\n')
subprocess.run(
    [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                  # single portable exe
        '--noconfirm',                # overwrite previous build without asking
        '--collect-all', 'yt_dlp',   # bundle all yt-dlp extractors (dynamic imports)
        '--name', 'BackstageHero',
        *ffmpeg_flag,
        *icon_flag,
        'VideoDownload.py',
    ],
    check=True,
)


# ── 5. Done ───────────────────────────────────────────────────────────────────

print('\n' + '-' * 60)
print('Build complete!  -->  dist\\BackstageHero.exe')
print('-' * 60)
print('To deploy:')
print('  Copy dist\\BackstageHero.exe to your Clone Hero directory')
print('  (the folder that contains your Songs\\ folder), then run it.')
if ffmpeg_flag:
    print('  ffmpeg is bundled inside the exe, so there is nothing else to install.')
print('-' * 60)
