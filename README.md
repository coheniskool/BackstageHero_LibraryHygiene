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

![BackstageHero](assets/screenshot.png)

If a song folder has a `video.mp4` in it, Clone Hero plays it behind the chart. Doing that by hand across a whole library takes forever. This searches YouTube for the ones that are missing it, downloads a match, and lines the video up so it starts with the song.

Point it at your Songs folder and it fills in whatever doesn't have a video yet.

## Install

Download `BackstageHero.exe` from the [latest release](https://github.com/jmb988/BackstageHero/releases/latest) and run it. It asks for your Songs folder the first time. ffmpeg is inside the exe, so there's nothing else to install.

Songs that already have a video are skipped, so re-running after you add charts is fine. You can close it mid-run; it cleans up the song it was on and picks up again next time.

## New in v2

- proper GUI instead of the old console prompts. you can see the whole library, filter by what's missing a video, and check resolutions
- it works out the timing itself now, by matching the video's audio against the chart audio instead of dropping the same offset on everything
- manual offset editor for the ones it gets wrong. right click a song, drag the slider, the preview follows
- optional sharing: turn it on and once a few people land on the same offset for a chart, that becomes the default for anyone else who downloads it
- updates itself, and keeps yt-dlp updated too (it has to, YouTube breaks it every few weeks)

## How the timing works

Every music video has a different amount of intro before the song starts, so you can't use one offset for everything. Once it's picked a video it grabs the audio and matches it against the chart's stems to find where they line up. It compares the pattern of peaks rather than the raw audio, so a louder or differently mastered YouTube version still matches. If it's sure, it writes the offset into `video_start_time`. If it isn't (live version, remix, just the wrong video) it leaves the default and you fix that one by hand.

Official music videos usually just work. The ones that need a manual pass are custom charts with edited intros, replaced audio, or no audio in the chart to match against at all. Whatever you set in the editor can be shared too.

## Why it still works

Most YouTube downloaders use the web player API, which now wants bot-challenge tokens that rotate constantly. This goes through the TV embedded and Android client APIs instead, which don't need those tokens. It also keeps yt-dlp updated on its own, so when YouTube breaks something and yt-dlp patches it, the fix shows up within a day or so, no new release needed.

On a big run YouTube will start throttling. It paces itself to stay under that and eases off automatically when it gets pushback, so most runs just ride it out. A hard IP block is on YouTube's end and waiting it out is the only fix there, but nothing gets lost or redone when you come back to it, since anything that already has a video is skipped.

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
