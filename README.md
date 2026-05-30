<p align="center">
  <img src="assets/icon.png" width="80" height="80">
</p>

<h1 align="center">BackstageHero</h1>

<p align="center">
  Downloads background videos for your entire Clone Hero library.<br>
  Finds the right video, lines it up with the chart, saves it where Clone Hero expects it.
</p>

<p align="center">
  <a href="https://github.com/jmb988/BackstageHero/releases/latest">
    <img src="https://img.shields.io/github/v/release/jmb988/BackstageHero?label=download&style=for-the-badge" alt="Download">
  </a>
  &nbsp;
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey?style=for-the-badge" alt="Windows">
</p>

---

The first release was a working script. This is the version it should have been from the start.

---

## What's in v2

| | |
|---|---|
| **GUI** | Dark-theme interface. See your full library, filter by status, search by name, track resolution per song. |
| **Audio fingerprinting** | Landmark-based matching (the same idea Shazam uses) measures exactly where the song starts in the video. No fixed-offset guessing. |
| **Community resolver** | Crowdsourced video-to-chart mappings. Once one person confirms the right video, every install gets it automatically — no fingerprinting needed for known charts. |
| **Sync editor** | Adjust timing with a live preview. Nudge in 10ms steps, drag the slider, see the result immediately. Save locally and optionally share the offset back to the community. |
| **Single exe** | ffmpeg is bundled inside. Download, run, done. Nothing else to install. |
| **Auto-updater** | Both the app and yt-dlp update themselves. yt-dlp breaks every few weeks as YouTube changes — the tool fixes itself overnight. |

---

## Getting started

1. Download `BackstageHero.exe` from the **[Releases page](https://github.com/jmb988/BackstageHero/releases/latest)**
2. Run it from anywhere — no installation, no dependencies
3. Point it at your Clone Hero `Songs` folder on first launch

It scans your library, skips anything that already has a video, and works through the rest. Close it at any time — nothing is left corrupt and it picks up where it left off on the next run.

---

## How the sync works

Every music video has a different amount of intro before the song actually starts, so any fixed offset is a guess. BackstageHero solves this properly:

After finding a candidate video, it fetches just the audio and fingerprints it against the chart's own stems. If the pattern of peaks matches — meaning it's the same recording — it measures exactly how far into the video the song begins and writes that as `video_start_time`. If it doesn't match (live take, remix, wrong master) it falls back to a default rather than writing a wrong value.

This is robust to the EQ and loudness differences between a YouTube upload and chart audio because it compares structure, not waveform.

**For charts where fingerprinting can't match** — community packs with trimmed intros, unusual masters, no chart audio — use the sync editor. Right-click any song that has a video, adjust the offset with live preview, and save. Checking *Share with community* votes that offset into the resolver; once enough users confirm the same value it becomes the default for that chart automatically.

---

## Why it still works when other tools don't

YouTube's standard web player API requires bot-challenge tokens that change constantly. This tool uses the **Android VR client API** — the same endpoint the official YouTube VR app uses. It delivers h264 up to 1080p with no cookies, no browser sign-in, and no JavaScript. That's why the web-client approach breaks repeatedly and this doesn't.

---

## Rate limiting

On large libraries downloaded in one sitting, YouTube may start returning bot-challenge errors. Stop, wait an hour, re-run — completed songs are skipped automatically. The app detects this and warns you when it happens.

---

## Running from source

```
git clone https://github.com/jmb988/BackstageHero
cd BackstageHero
pip install -r requirements.txt
python gui.py
```

For audio sync to work from source, ffmpeg needs to be on your PATH or placed in the project folder. Grab a static build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases).

### Building the exe

```
python build.py
```

Downloads ffmpeg automatically, bundles everything with PyInstaller, and produces `dist\BackstageHero.exe`. A SHA-256 sidecar is written alongside it for the auto-updater to verify against.

---

## Credits

Bundles [ffmpeg](https://ffmpeg.org) (GPLv3, build by [BtbN](https://github.com/BtbN/FFmpeg-Builds)). Downloading handled by [yt-dlp](https://github.com/yt-dlp/yt-dlp).
