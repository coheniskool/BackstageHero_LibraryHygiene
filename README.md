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
- Lines each video up with the chart automatically when it can confirm the same recording
- Skips songs that already have a video — safe to re-run after adding new songs
- Resumes cleanly after any interruption; nothing is left corrupt
- Handles libraries with thousands of songs in deeply nested folders

---

## Getting started

### Option 1 — Pre-built executable (Windows)

1. Download `BackstageHero.exe` from the [Releases page](https://github.com/jmb988/BackstageHero/releases/latest).
2. Place it in your Clone Hero directory — the folder that **contains** your `Songs` folder, not inside it.
3. Run it.

That's the whole setup. ffmpeg is bundled inside the exe, so automatic sync and 1080p remuxing work out of the box with nothing else to install.

### Option 2 — Run from source

Requires Python 3.8 or later.

```
git clone https://github.com/jmb988/BackstageHero
cd BackstageHero
pip install -r requirements.txt
```

Place `VideoDownload.py` in your Clone Hero directory (the folder containing `Songs\`), then run:

```
python VideoDownload.py
```

Running from source has no bundled ffmpeg. For automatic sync and 1080p remuxing, put an `ffmpeg.exe` next to the program (or anywhere on your `PATH`). Grab it from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases). Without it, videos still download and play; they just use the default sync offset.

### Option 3 — Build the exe yourself

```
python build.py
```

The finished `BackstageHero.exe` is placed in `dist\`. PyInstaller is installed automatically if not already present. To bundle ffmpeg into the exe (so sync works with no setup), drop an `ffmpeg.exe` into the project folder before building — the build picks it up automatically.

---

## Quality options

When you run the tool, you are prompted to choose a quality level:

| # | Option | Notes |
|---|--------|-------|
| 1 | 720p | Default. Smaller files. |
| 2 | 1080p | Best quality where available; significantly larger files. |
| 3 | Replace all with 1080p | Deletes existing videos and re-downloads everything at 1080p. Use with caution. |
| 4 | Re-sync existing videos | Re-times videos you already have, without re-downloading them. Only fetches a little audio per song. |

720p is the recommended starting point. 1080p files are typically 2–3× larger and require considerably more download time and disk space.

**Already have a library from an earlier version?** Re-running a normal pass won't re-time videos you already have — it only touches songs that are missing one. Use option 4 to add automatic sync to an existing library without re-downloading the videos.

---

## How it works

### Bypassing YouTube's bot detection

YouTube's standard web player API now requires bot-challenge tokens that change frequently and are difficult to generate programmatically. Rather than fighting that, this tool uses YouTube's **Android VR client API** — the same backend endpoint that the official YouTube VR app communicates with. That API delivers h264/AVC video up to 1080p and is not subject to the bot-detection checks applied to the web client. No cookies, no browser sign-in, and no JavaScript runtime are required.

This is why every other tool in this space stopped working: they all use the web client by default. Switching to the Android VR API endpoint is the fix.

### File integrity

`video.mp4` inside a song folder is only ever created by a final rename from a temporary staging file, once the download (and optional remux) is fully complete. If the process is killed mid-download, the partial file sits under a different name (`video.download.mp4` or `video.tmp.mp4`) and is cleaned up automatically on the next run. A completed `video.mp4` is never modified or deleted during a normal run.

On startup, the tool scans the full library and removes any leftover temp files and zero-byte videos from prior interrupted runs before processing begins.

### Automatic sync

A background video only looks right if the music in the video lines up with the chart. Every music video opens with a different amount of intro before the song actually starts, so any fixed offset is just a guess.

After downloading a video, the tool fetches the matching audio track and fingerprints it against the chart's own audio — the same landmark-matching technique Shazam uses. When it can confirm the video uses the same recording as the chart, it measures exactly how far into the video the song begins and writes that as `video_start_time`, so the video lines up on its own. When it can't — the top result is a live take, a remix, or a different master — it falls back to a sensible default offset instead of guessing wrong.

This matching is robust to the EQ, loudness, and compression differences between a YouTube upload and the chart audio, because it compares the *pattern* of audio peaks rather than the raw waveform. It requires ffmpeg. You can always fine-tune `video_start_time` in a song's `song.ini` by hand afterwards.

### ffmpeg

ffmpeg does two jobs here: remuxing 1080p downloads into a container Clone Hero handles cleanly, and decoding audio for automatic sync. **The Windows exe bundles ffmpeg**, so both work with no setup. If you run from source instead, install ffmpeg separately (see below) — without it, videos still download and play, they just skip remuxing and use the default sync offset.

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

## Credits

The Windows release bundles [ffmpeg](https://ffmpeg.org), used under the GPLv3 (build by [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)). ffmpeg is a separate program invoked by this tool; its source is available from [ffmpeg.org/download](https://www.ffmpeg.org/download.html). Downloading is handled by [yt-dlp](https://github.com/yt-dlp/yt-dlp).

---

*Successor to [jshackles/CloneHeroVideoDownloader](https://github.com/jshackles/CloneHeroVideoDownloader), updated to work against current YouTube infrastructure.*
