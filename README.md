<p align="center">
  <img src="assets/icon.png" width="80" height="80">
</p>

<h1 align="center">BackstageHero</h1>

<p align="center">Downloads background videos for your Clone Hero songs and lines them up with the charts.</p>

<p align="center">
  <a href="https://github.com/jmb988/BackstageHero/releases/latest">
    <img src="https://img.shields.io/github/v/release/jmb988/BackstageHero?label=download" alt="Download">
  </a>
</p>

---

Clone Hero plays a `video.mp4` from a song's folder as the background while you play. Getting one for every song by hand is a slog. This searches YouTube, downloads a match, and works out the timing so the song starts when the video's audio does.

Point it at your Songs folder and it handles everything that's missing a video.

## Install

Download `BackstageHero.exe` from the [latest release](https://github.com/jmb988/BackstageHero/releases/latest) and run it. It asks for your Songs folder the first time. ffmpeg is inside the exe, so there's nothing else to install.

Songs that already have a video are skipped, so re-running after you add charts is fine. You can close it mid-run; it cleans up the song it was on and picks up again next time.

## New in v2

- A GUI, instead of typing answers into a console. Lists your library, lets you search and filter by which songs have videos, and shows each one's resolution.
- Automatic timing. It fingerprints the video's audio against the chart audio to find where the song starts, rather than putting the same fixed offset on everything.
- A manual sync editor for the songs the fingerprinter can't place. Right-click a song, drag the offset, watch the preview update as you go.
- Shared offsets. Tick the share box and your timing gets sent up; once a few people land on the same value for a chart it becomes the default, so the next person downloading it gets it right with no work.
- One exe with ffmpeg bundled. v1 made you install ffmpeg separately.
- Self-updating, and it keeps yt-dlp current too (yt-dlp needs updating often to keep working against YouTube).

## How the timing works

Every music video opens with a different amount of intro before the track starts, so one fixed offset can't be right for all of them. Once a video is chosen, it grabs the audio on its own and fingerprints it against the song's stems, the same idea Shazam uses, matching the pattern of peaks instead of the raw waveform so a differently-mastered YouTube upload still lines up. If it's sure the recording matches, it writes the measured offset to `video_start_time`. If it isn't (a live take, a remix, the wrong song) it leaves a default instead of locking in a bad guess.

Official videos sort themselves out this way. Community packs are the awkward ones: trimmed intros, custom audio, charts with no audio to match against. The manual editor is there for those, and anything you set can be shared the same way.

## Why it still works

Most YouTube downloaders use the web player API, which now expects bot-challenge tokens that change constantly and are awkward to produce. This uses the Android VR client API instead, the backend the YouTube VR app talks to. It returns h264 up to 1080p with no cookies, no login, and no JavaScript engine, and it isn't behind those same checks. That's why the older tools kept breaking and this one holds up.

Push a big library hard enough and YouTube can still rate-limit your IP. When that happens it backs off and retries on its own; if it does give up, you re-run later and it continues from where it stopped.

## From source

```
git clone https://github.com/jmb988/BackstageHero
cd BackstageHero
pip install -r requirements.txt
python gui.py
```

Timing needs ffmpeg on your PATH or sitting in the project folder. Static Windows builds are at [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases).

To build the exe, run `python build.py`. It downloads ffmpeg and packs everything into `dist/`.

## Credits

ffmpeg ([BtbN builds](https://github.com/BtbN/FFmpeg-Builds), GPLv3) and [yt-dlp](https://github.com/yt-dlp/yt-dlp) do the real work.
