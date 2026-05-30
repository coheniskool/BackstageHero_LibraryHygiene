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
import urllib.request
import zipfile


def pip_install(package):
    print(f'Installing {package}...')
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--quiet', package],
        check=True,
    )


# 1. Ensure PyInstaller is available

check = subprocess.run(
    [sys.executable, '-m', 'PyInstaller', '--version'],
    capture_output=True,
)
if check.returncode != 0:
    pip_install('pyinstaller')


# 2. Convert PNG icon to ICO (via subprocess so Pillow is always importable)

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


# 3. Bundle ffmpeg - download automatically if not already present.
# ffmpeg is required for audio sync and video remuxing. We pull the static
# Windows build from BtbN's releases so the resulting exe is fully self-contained.

if not os.path.exists('ffmpeg.exe'):
    print('ffmpeg.exe not found - downloading (this takes a minute)...')
    url = ('https://github.com/BtbN/ffmpeg-builds/releases/latest/download/'
           'ffmpeg-master-latest-win64-gpl.zip')
    tmp_zip = 'ffmpeg_tmp.zip'
    try:
        def _progress(count, block, total):
            if total > 0:
                print(f'\r  {min(100, count * block * 100 // total)}%', end='', flush=True)
        urllib.request.urlretrieve(url, tmp_zip, reporthook=_progress)
        print()
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            for exe in ('ffmpeg.exe', 'ffplay.exe'):
                member = next((n for n in names if os.path.basename(n) == exe), None)
                if member:
                    with zf.open(member) as src, open(exe, 'wb') as dst:
                        dst.write(src.read())
            if not os.path.exists('ffmpeg.exe'):
                raise RuntimeError('ffmpeg.exe not found inside downloaded zip')
        print('ffmpeg.exe + ffplay.exe ready.')
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

ffmpeg_flag = []
for _exe in ('ffmpeg.exe', 'ffplay.exe'):
    if os.path.exists(_exe):
        ffmpeg_flag += ['--add-binary', _exe + os.pathsep + '.']
        print(f'Bundling {_exe}.')


# 4. Run PyInstaller

print('\nBuilding BackstageHero.exe...\n')
subprocess.run(
    [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                      # single portable exe
        '--noconfirm',                    # overwrite previous build without asking
        '--noconsole',                    # GUI app - no terminal window
        '--collect-all', 'yt_dlp',        # bundle all yt-dlp extractors (dynamic imports)
        '--collect-all', 'customtkinter', # bundle customtkinter themes and assets
        '--collect-all', 'numpy',         # required by audiosync for fingerprint sync
        '--hidden-import', 'gui',         # GUI module (conditional import)
        '--hidden-import', 'audiosync',   # fingerprint sync module
        '--add-data', 'assets' + os.pathsep + 'assets',  # icon + other assets
        '--name', 'BackstageHero',
        *ffmpeg_flag,
        *icon_flag,
        'VideoDownload.py',
    ],
    check=True,
)


# 5. Write a SHA-256 sidecar
# The in-app updater verifies a downloaded exe against this hash when it is
# published next to the release asset. Upload dist\BackstageHero.exe.sha256
# alongside the exe and auto-update gains a tamper check on top of HTTPS.

import hashlib

exe_path = os.path.join('dist', 'BackstageHero.exe')
sha_path = exe_path + '.sha256'
if os.path.exists(exe_path):
    h = hashlib.sha256()
    with open(exe_path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    with open(sha_path, 'w') as f:
        f.write(h.hexdigest() + '  BackstageHero.exe\n')
    print('Wrote checksum -->  ' + sha_path)


# 6. Done

print('\n' + '-' * 60)
print('Build complete!  -->  dist\\BackstageHero.exe')
print('-' * 60)
print('To deploy:')
print('  Copy dist\\BackstageHero.exe to your Clone Hero directory')
print('  (the folder that contains your Songs\\ folder), then run it.')
print('  ffmpeg is bundled inside the exe - nothing else to install.')
print('\nTo release (so auto-update reaches users):')
print('  Tag the GitHub release v.2.0.0 (must be newer than the previous tag),')
print('  then upload BOTH dist\\BackstageHero.exe and dist\\BackstageHero.exe.sha256.')
print('-' * 60)
