<p align="center">
  <img src="assets/icon.png" width="80" height="80">
</p>

<h1 align="center">BackstageHero</h1>

<p align="center">
  Automatically downloads background music videos for every song in your Clone Hero library.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey" alt="Windows">
  <img src="https://img.shields.io/badge/powered_by-yt--dlp-red" alt="yt-dlp">
</p>

---

Clone Hero displays a `video.mp4` file as a background during gameplay if one exists in the song folder. This tool scans your entire library, finds every song missing a video, searches YouTube for a matching music video, and downloads it into the right place — all in one run.

- Searches YouTube by folder name and downloads the top result
- Falls back to the second result automatically if the first fails
- Skips songs that already have a video — safe to re-run after adding new songs
- Resumes cleanly after any interruption; nothing is left corrupt
- Handles libraries with thousands of songs in deeply nested folders

---

## Getting started

### Option 1 — Pre-built executable (Windows)

1. Download `BackstageHero.exe` from the [Releases page](https://github.com/jmb988/BackstageHero/releases/latest).
2. Place it in your Clone Hero directory — the folder that **contains** your `Songs` folder, not inside it.
3. Run it.

For 1080p downloads, also place `ffmpeg.exe` in the same directory. Grab it from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) — download the latest `ffmpeg-master-latest-win64-gpl.zip` and extract `ffmpeg.exe` from the `bin` folder.

### Option 2 — Run from source

Requires Python 3.8 or later.

```
git clone https://github.com/jmb988/BackstageHero
cd CloneHeroVideoDownloader
pip install -r requirements.txt
```

Place `VideoDownload.py` in your Clone Hero directory (the folder containing `Songs\`), then run:

```
python VideoDownload.py
```

### Option 3 — Build the exe yourself

```
python build.py
```

The finished `BackstageHero.exe` is placed in `dist\`. PyInstaller is installed automatically if not already present.

---

## Quality options

When you run the tool, you are prompted to choose a quality level:

| # | Quality | Notes |
|---|---------|-------|
| 1 | 720p | Default. Smaller files, works without ffmpeg. |
| 2 | 1080p | Significantly larger files. ffmpeg recommended but not required. |
| 3 | Replace all with 1080p | Deletes existing videos and re-downloads everything at 1080p. Use with caution. |

720p is the recommended starting point. 1080p files are typically 2–3× larger and require considerably more download time and disk space.

---

## How it works

### Bypassing YouTube's bot detection

YouTube's standard web player API now requires bot-challenge tokens that change frequently and are difficult to generate programmatically. Rather than fighting that, this tool uses YouTube's **Android VR client API** — the same backend endpoint that the official YouTube VR app communicates with. That API delivers h264/AVC video up to 1080p and is not subject to the bot-detection checks applied to the web client. No cookies, no browser sign-in, and no JavaScript runtime are required.

This is why every other tool in this space stopped working: they all use the web client by default. Switching to the Android VR API endpoint is the fix.

### File integrity

`video.mp4` inside a song folder is only ever created by a final rename from a temporary staging file, once the download (and optional remux) is fully complete. If the process is killed mid-download, the partial file sits under a different name (`video.download.mp4` or `video.tmp.mp4`) and is cleaned up automatically on the next run. A completed `video.mp4` is never modified or deleted during a normal run.

On startup, the tool scans the full library and removes any leftover temp files and zero-byte videos from prior interrupted runs before processing begins.

### ffmpeg

For 1080p, the tool optionally remuxes the downloaded file into a container format that Clone Hero handles cleanly. Because the Android VR API already delivers h264/MP4, the raw download plays correctly in Clone Hero even without remuxing — ffmpeg is not required. If ffmpeg is absent, the download is used as-is with a one-time notice.

---

## Notes

**Song folder naming**  
The folder containing `song.ini` should be named `Artist - Song Title`. That name is what gets submitted to YouTube as the search query. Malformed folder names produce irrelevant results.

**Rate limiting**  
On very large libraries downloaded in a single sitting, YouTube may start returning "Sign in to confirm you're not a bot" errors. This is IP-based rate limiting. Stop the program, wait a while, and re-run — it will skip everything already downloaded and pick up from where it left off.

**Interrupting a run**  
The program can be stopped at any time with `Ctrl+C` or by closing the window. The song currently in progress is cleaned up before exit. On the next run, completed songs are skipped and the interrupted one is retried.

**Re-downloading a video**  
To replace a specific video, delete the `video.mp4` from that song's folder and re-run the tool.

---

*Successor to [jshackles/CloneHeroVideoDownloader](https://github.com/jshackles/CloneHeroVideoDownloader), updated to work against current YouTube infrastructure.*
