# BackstageHero GUI.
# All download/sync logic lives in VideoDownload.py; this file owns the window.

import os
import sys

# pythonw.exe (no console attached -- a plain double-click launch, or this
# project's own "Launch BackstageHero.bat") sets sys.stdout/stderr to None,
# not just closed. The first print() or warnings.warn() call anywhere in
# this app or a dependency then crashes immediately with no visible error,
# no window, nothing -- confirmed the hard way (2026-07-18): it looked fine
# invoked through a shell that happened to inherit real stdio handles, but
# failed silently on a genuine Explorer double-click. Redirect to a discard
# sink before any other import runs, matching this codebase's own existing
# assumption that print() "goes nowhere" without a console (see
# VideoDownload._setup_logging()) -- true for a frozen --noconsole build,
# but not for plain pythonw without this guard.
# The guard itself lives in library_common so it can actually be unit-tested;
# as inline import-time code here it was unreachable under pytest, so nothing
# in the suite would have caught its removal. library_common imports only
# stdlib and prints nothing, so it is safe to load before the redirect.
import library_common
library_common.ensure_stdio_not_none()

import csv
import glob
import logging
import random
import re
import subprocess
import tempfile
import threading
import queue
import time
from dataclasses import dataclass

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from VideoDownload import (
    read_metadata, build_query, run_song_with_backoff,
    quality_format, get_stored_resolution, set_ini_values,
    ffmpegAvailable, ffplayPath, audiosync, __version__,
    DEFAULT_START_TIME, get_stored_source, NO_WINDOW,
    SONG_DELAY_MIN, SONG_DELAY_MAX, probe_resolution, scan_song,
    SYNC_MANUAL, dump_video, get_rejected_sources,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
import updater
import resolver_client
import video_repair
import chart_rename
import metadata_enrichment
import dedupe_report
import static_art

log = logging.getLogger('backstagehero')

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('blue')


def _asset_path(name):
    """Resolve a bundled asset path - works both frozen (PyInstaller) and from source."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'assets', name)


def _load_mpv():
    """Load the optional embedded player (libmpv via python-mpv).

    Returns the mpv module if libmpv-2.dll is present and loads, else None.
    The sync editor uses it for a live, in-window preview; if it's missing
    the editor falls back to launching ffplay."""
    try:
        dirs = []
        if getattr(sys, 'frozen', False):
            dirs.append(getattr(sys, '_MEIPASS', ''))
            dirs.append(os.path.dirname(sys.executable))
        else:
            dirs.append(os.path.dirname(os.path.abspath(__file__)))
        for d in dirs:
            if d and os.path.exists(os.path.join(d, 'libmpv-2.dll')):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
                break
        import mpv as _mpv
        return _mpv
    except Exception:
        return None


mpvlib = _load_mpv()

_BG      = '#1e1e2e'
_SURFACE = '#252535'
_BORDER  = '#383850'
_TEXT    = '#cdd6f4'
_SUBTEXT = '#7f849c'
_GREEN   = '#a6e3a1'
_RED     = '#f38ba8'
_YELLOW  = '#f9e2af'
_BLUE    = '#89b4fa'
_MAUVE   = '#cba6f7'

_SONGS_FILE = os.path.join(updater.data_dir(), 'songs_path.txt')


def _load_songs_path():
    try:
        with open(_SONGS_FILE) as f:
            return f.read().strip()
    except Exception:
        return ''


def _save_songs_path(path):
    try:
        with open(_SONGS_FILE, 'w') as f:
            f.write(path)
    except Exception:
        pass


import json as _json
_SETTINGS_FILE = os.path.join(updater.data_dir(), 'settings.json')


def _load_settings():
    try:
        with open(_SETTINGS_FILE, encoding='utf-8') as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    try:
        with open(_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            _json.dump(data, f)
    except Exception:
        pass


def _validate_folder(path):
    """Returns (ok: bool, message: str)."""
    if not path or not os.path.isdir(path):
        return False, 'That path does not exist or is not a folder.'

    # They picked an individual song folder (song.ini at the root level)
    if os.path.exists(os.path.join(path, 'song.ini')):
        return False, (
            'That looks like an individual song folder, not your Songs library.\n\n'
            'Please select the folder that contains all your song packs, '
            'not a folder inside one of them.')

    # Check for any song.ini anywhere inside
    found = next(
        glob.iglob(os.path.join(glob.escape(path), '**', 'song.ini'),
                   recursive=True), None)
    if not found:
        return False, (
            'No songs found in that folder.\n\n'
            'Make sure you\'re selecting your Clone Hero Songs folder.\n'
            'It should contain your downloaded song packs, each with a '
            'song.ini file inside.')

    return True, ''


@dataclass
class Song:
    filename : str          # abs path to song.ini
    folder   : str
    label    : str          # "Artist – Title" for display
    key      : str          # label.lower() for search
    has_video: bool
    res      : str          # "720p" / "1080p" / "480p" / "-" / "..."
    checked  : bool = False
    status   : str  = ''    # live status text during a run
    stag     : str  = ''    # colour tag: 'done'|'error'|'busy'|'dim'|''


def _open_in_file_manager(path):
    """Open a folder in the OS file manager (Windows/macOS/Linux)."""
    try:
        if sys.platform == 'win32':
            os.startfile(path)              # noqa: only exists on Windows
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        log.warning('Could not open folder: %s', path)


def _video_status(song):
    """What the 'Has video' column should say.

    The app counts only video.mp4 as a video -- that is deliberate, since a
    video.webm left by another tool is usually VP9, which this Clone Hero
    build cannot decode, so the song is meant to re-download. But writing a
    bare 'no' next to a folder that visibly contains a video file reads as a
    bug in the export. Name the file instead, so the row explains itself.
    """
    if song.has_video:
        return 'yes'
    for name in library_common.VIDEO_NAMES:
        if name != 'video.mp4' and os.path.exists(os.path.join(song.folder, name)):
            return f'no ({name} present, not playable - will re-download)'
    return 'no'


def _read_song_value(folder, key):
    """One [song] value from a song.ini, or '' -- for building the CSV.

    Thin wrapper over VideoDownload's reader rather than a second parser, so
    the export can never disagree with what the app itself reads.
    """
    try:
        from VideoDownload import _read_ini_value
        return _read_ini_value(folder, key) or ''
    except Exception:
        return ''


def _scan_library(songs_folder, progress=None):
    """Return a list[Song] for the folder, sorted alphabetically.
    progress(count) is called periodically so the UI can show a live tally."""
    songs = []
    for ini in glob.iglob(
            os.path.join(glob.escape(songs_folder), '**', 'song.ini'),
            recursive=True):
        folder = os.path.dirname(ini)
        artist, title, stored = scan_song(folder)
        label = build_query(artist, title) or os.path.basename(folder)
        has_vid = os.path.exists(os.path.join(folder, 'video.mp4'))
        res = '-'
        if has_vid:
            res = stored if stored else '...'   # '...' = needs probing
        songs.append(Song(
            filename=ini, folder=folder,
            label=label, key=label.lower(),
            has_video=has_vid, res=res))
        if progress and len(songs) % 50 == 0:
            progress(len(songs))
    songs.sort(key=lambda s: s.key)
    return songs


class SyncEditor(ctk.CTkToplevel):
    """Manual video-offset editor.

    When libmpv is available the video plays embedded in this window and the
    slider shifts the audio against it live, with no respawn. Otherwise it
    falls back to launching ffplay for a one-shot preview."""

    # The slider's STARTING window, not a limit on the offset itself.
    #
    # These used to be hard bounds, clamped by both the slider and the
    # fine-tune buttons, so a chart genuinely needing -78s could not be set at
    # all -- it snapped back to -30s and silently stayed wrong. A real
    # video_start_time has no natural bound (a long intro, a compilation
    # upload, a chart cut from the middle of a set), so the window now grows to
    # fit whatever is needed and the numeric box below accepts any value.
    _MS_MIN_DEFAULT = -30_000
    _MS_MAX_DEFAULT = 90_000
    # how much headroom to add past the value when the window has to grow, so
    # dragging to the end doesn't immediately need another resize
    _MS_WINDOW_PAD = 30_000

    def __init__(self, parent, song: Song, on_save=None):
        super().__init__(parent)
        self._song      = song
        self._on_save   = on_save
        self._player    = None    # mpv handle (embedded preview)
        self._proc      = None    # ffplay process (fallback)
        self._proc_aux  = None    # ffmpeg feeder (fallback)
        self._tmp_mux   = None    # temp muxed video+audio file for the preview
        self._embedded  = mpvlib is not None and ffmpegAvailable

        # SW_SHOWNOACTIVATE: ffplay (fallback) opens without stealing focus
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        self._si.wShowWindow = 4

        self._ms    = tk.IntVar(value=self._read_offset())
        self._share = tk.BooleanVar(value=True)
        # widen the initial window if this song's stored offset already sits
        # outside it -- otherwise opening the editor would misrepresent the
        # value it is supposed to be showing
        self._ms_min = self._MS_MIN_DEFAULT
        self._ms_max = self._MS_MAX_DEFAULT
        self._grow_window_for(self._ms.get())

        self.title('Sync Editor')
        self.geometry('640x760' if self._embedded else '500x470')
        self.resizable(False, False)
        self.configure(fg_color=_BG)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self._close)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._build()
        self._refresh()
        self.after(50, self._center)
        if self._embedded:
            self.after(120, self._start_embedded)
        elif ffplayPath:
            self.after(200, self._launch_preview)

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=60)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=self._song.label,
                     font=ctk.CTkFont(size=14, weight='bold'),
                     text_color=_TEXT, anchor='w').pack(
            side='left', padx=18, fill='y')
        res = self._song.res if self._song.res not in ('-', '...', '') else '?'
        ctk.CTkLabel(hdr, text=res,
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTEXT).pack(side='right', padx=18)

        # Embedded video surface (mpv renders into this frame)
        self._video_frame = None
        if self._embedded:
            holder = ctk.CTkFrame(self, fg_color='#000000', corner_radius=8,
                                  width=600, height=338)
            holder.pack(padx=20, pady=(16, 0))
            holder.pack_propagate(False)
            # plain tk.Frame gives a stable native window id for mpv's wid
            self._video_frame = tk.Frame(holder, bg='#000000',
                                         width=600, height=338,
                                         highlightthickness=0, bd=0)
            self._video_frame.pack(fill='both', expand=True)

        # Offset readout card
        card = ctk.CTkFrame(self, fg_color='#252540', corner_radius=10)
        card.pack(fill='x', padx=20, pady=(18, 0))
        top_row = ctk.CTkFrame(card, fg_color='transparent')
        top_row.pack(fill='x', padx=16, pady=(12, 0))
        self._ms_lbl = ctk.CTkLabel(
            top_row, text='',
            font=ctk.CTkFont(size=30, weight='bold'), text_color=_BLUE)
        self._ms_lbl.pack(side='left')
        # live indicator, only when ffplay is around
        if ffplayPath:
            self._live_lbl = ctk.CTkLabel(
                top_row, text='',
                font=ctk.CTkFont(size=10), text_color=_SUBTEXT)
            self._live_lbl.pack(side='right', padx=4, pady=(8, 0))
        else:
            self._live_lbl = None
        self._desc_lbl = ctk.CTkLabel(
            card, text='',
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT)
        self._desc_lbl.pack(pady=(2, 12))

        # Slider. Its ends are labelled from the live window, not hard-coded,
        # because the window grows to whatever this song actually needs.
        sf = ctk.CTkFrame(self, fg_color='transparent')
        sf.pack(fill='x', padx=20, pady=(14, 0))
        self._min_lbl = ctk.CTkLabel(sf, text='', font=ctk.CTkFont(size=10),
                                     text_color=_SUBTEXT)
        self._min_lbl.pack(side='left')
        self._max_lbl = ctk.CTkLabel(sf, text='', font=ctk.CTkFont(size=10),
                                     text_color=_SUBTEXT)
        self._max_lbl.pack(side='right')
        self._slider = ctk.CTkSlider(
            sf, from_=self._ms_min, to=self._ms_max,
            command=self._on_slider, height=16,
            button_color=_BLUE, button_hover_color='#7aaef8',
            progress_color=_BLUE)
        self._slider.set(self._ms.get())
        self._slider.pack(fill='x', padx=8)
        self._sync_slider_range()

        # Exact-value entry, so an offset can be typed rather than dragged to.
        ef = ctk.CTkFrame(self, fg_color='transparent')
        ef.pack(fill='x', padx=20, pady=(8, 0))
        ctk.CTkLabel(ef, text='Exact offset (ms):', font=ctk.CTkFont(size=11),
                     text_color=_SUBTEXT).pack(side='left', padx=(8, 6))
        self._ms_entry = ctk.CTkEntry(ef, width=110, height=26,
                                      font=ctk.CTkFont(size=11))
        self._ms_entry.pack(side='left')
        self._ms_entry.bind('<Return>', self._on_entry_commit)
        self._ms_entry.bind('<FocusOut>', self._on_entry_commit)
        ctk.CTkButton(ef, text='Set', width=46, height=26,
                      fg_color='#2a2a3e', hover_color='#383858',
                      text_color=_TEXT, font=ctk.CTkFont(size=10),
                      command=self._on_entry_commit).pack(side='left', padx=6)

        # Fine-tune buttons
        bf = ctk.CTkFrame(self, fg_color='transparent')
        bf.pack(fill='x', padx=20, pady=(10, 0))
        _b = dict(height=26, corner_radius=6,
                  fg_color='#2a2a3e', hover_color='#383858',
                  text_color=_TEXT, font=ctk.CTkFont(size=10))
        for lbl, delta in (('-5s', -5000), ('-1s', -1000),
                           ('-100ms', -100), ('-10ms', -10),
                           ('+10ms', 10),   ('+100ms', 100),
                           ('+1s', 1000),   ('+5s', 5000)):
            ctk.CTkButton(bf, text=lbl, width=54,
                          command=lambda d=delta: self._nudge(d), **_b).pack(
                side='left', padx=2)

        # Transport row
        prev_row = ctk.CTkFrame(self, fg_color='transparent')
        prev_row.pack(fill='x', padx=20, pady=(10, 0))
        if self._embedded:
            self._play_btn = ctk.CTkButton(
                prev_row, text='⏸  Pause', width=100, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._toggle_play)
            self._play_btn.pack(side='left')
            ctk.CTkButton(
                prev_row, text='↻  Restart', width=100, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._restart_at_song).pack(side='left', padx=(8, 0))
            ctk.CTkLabel(prev_row,
                         text='Drag the slider, sync updates live as it plays.',
                         font=ctk.CTkFont(size=10),
                         text_color=_SUBTEXT).pack(side='right')
        elif ffplayPath:
            ctk.CTkButton(
                prev_row, text='▶  Preview', width=110, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._launch_preview).pack(side='left')
        else:
            ctk.CTkLabel(prev_row, text='Player not bundled. Adjust the offset and Save.',
                         font=ctk.CTkFont(size=10),
                         text_color=_SUBTEXT).pack(side='left')

        # Divider
        ctk.CTkFrame(self, fg_color=_BORDER, height=1).pack(
            fill='x', padx=20, pady=14)

        # Share
        shr = ctk.CTkFrame(self, fg_color='transparent')
        shr.pack(fill='x', padx=20)
        ctk.CTkCheckBox(shr, text='Share with community',
                        variable=self._share,
                        font=ctk.CTkFont(size=12), text_color=_TEXT,
                        checkbox_width=18, checkbox_height=18,
                        checkmark_color=_BG, fg_color=_BLUE,
                        hover_color='#7aaef8').pack(anchor='w')
        ctk.CTkLabel(shr,
                     text='Once enough users confirm the same offset it becomes\n'
                          'the default for this chart, no fingerprinting needed.',
                     font=ctk.CTkFont(size=10), text_color=_SUBTEXT,
                     justify='left', anchor='w').pack(anchor='w', pady=(4, 0))

        # Cancel / Save
        foot = ctk.CTkFrame(self, fg_color='transparent')
        foot.pack(fill='x', padx=20, pady=(14, 20))
        ctk.CTkButton(foot, text='Cancel', width=110,
                      fg_color='transparent', border_width=1,
                      border_color=_BORDER, hover_color='#30304a',
                      text_color=_SUBTEXT, font=ctk.CTkFont(size=12),
                      command=self._close).pack(side='left')
        ctk.CTkButton(foot, text='Save', width=110,
                      fg_color=_BLUE, hover_color='#7aaef8',
                      text_color='#11111b',
                      font=ctk.CTkFont(size=13, weight='bold'),
                      command=self._save).pack(side='right')

    def _read_offset(self):
        path = os.path.join(self._song.folder, 'song.ini')
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as f:
                for line in f:
                    if '=' in line:
                        k, _, v = line.partition('=')
                        if k.strip().lower() == 'video_start_time':
                            val = v.strip()
                            if val.lstrip('-').isdigit():
                                return int(val)
        except Exception:
            pass
        return DEFAULT_START_TIME

    def _grow_window_for(self, ms):
        """Widen the slider window so `ms` is representable. Returns True if it
        changed. Only ever grows -- shrinking mid-edit would yank the handle."""
        changed = False
        if ms < self._ms_min:
            self._ms_min = ms - self._MS_WINDOW_PAD
            changed = True
        if ms > self._ms_max:
            self._ms_max = ms + self._MS_WINDOW_PAD
            changed = True
        return changed

    def _sync_slider_range(self):
        self._slider.configure(from_=self._ms_min, to=self._ms_max)
        self._min_lbl.configure(text=f'{self._ms_min / 1000:g}s')
        self._max_lbl.configure(text=f'+{self._ms_max / 1000:g}s'
                                if self._ms_max > 0 else f'{self._ms_max / 1000:g}s')

    def _set_ms(self, new):
        """The single place the offset changes. No clamping: the window moves
        to fit the value, never the other way round."""
        new = int(new)
        if self._grow_window_for(new):
            self._sync_slider_range()
        self._ms.set(new)
        self._slider.set(new)
        self._refresh()
        self._apply_live_delay()

    def _on_entry_commit(self, event=None):
        raw = self._ms_entry.get().strip().replace(',', '')
        if not raw:
            self._refresh()
            return
        try:
            self._set_ms(int(float(raw)))
        except ValueError:
            self._refresh()      # unparseable: put the real value back

    def _refresh(self):
        ms = self._ms.get()
        sign = '+' if ms > 0 else ''
        self._ms_lbl.configure(text=f'{sign}{ms:,} ms' if ms != 0 else '0 ms')
        if getattr(self, '_ms_entry', None) is not None:
            # keep the box in step with the slider, but don't fight the user
            # while they are typing in it
            if self.focus_get() is not self._ms_entry:
                self._ms_entry.delete(0, 'end')
                self._ms_entry.insert(0, str(ms))
        s = abs(ms) / 1000.0
        if ms < -50:
            desc = f'Video has a {s:.1f}s intro before the song starts'
        elif ms > 50:
            desc = f'Song plays {s:.1f}s before the video starts'
        else:
            desc = 'Video and song start together'
        self._desc_lbl.configure(text=desc)

    def _on_slider(self, value):
        # no _set_ms here: the handle is already where the user put it, and
        # re-setting it mid-drag fights the widget
        self._ms.set(int(round(value)))
        self._refresh()
        self._apply_live_delay()

    def _nudge(self, delta):
        # deliberately unclamped -- the window grows instead. Clamping here was
        # half of why an offset past -30s could not be reached.
        self._set_ms(self._ms.get() + delta)

    def _audio_delay(self):
        """mpv audio-delay (seconds) for the current offset. A negative
        video_start_time means the video has intro before the song, so the chart
        audio comes in that much later, which is a positive delay."""
        return -self._ms.get() / 1000.0

    def _song_start_pos(self):
        """Where playback / Restart begins: a couple seconds before the spot
        where the song lines up with the video."""
        return max(0.0, (-self._ms.get() / 1000.0) - 2.0)

    def _find_stems(self):
        """Every audio stem in the song folder. Clone Hero splits a song into
        stems (song/guitar/bass/drums/vocals/...) and plays them all together; on
        its own a single stem has gaps where the others carry the song, so the
        preview mixes the lot or you'd hear it cut in and out. Same rule the
        fingerprinter uses so the preview matches what gets analysed."""
        if audiosync is not None:
            return audiosync.chart_stems(self._song.folder)
        # if audiosync didn't import, same extensions/exclusions by hand
        folder = self._song.folder
        stems = []
        try:
            for f in sorted(os.listdir(folder)):
                base, ext = os.path.splitext(f.lower())
                if ext in ('.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac') \
                        and base not in ('preview', 'crowd'):
                    stems.append(os.path.join(folder, f))
        except Exception:
            pass
        return stems

    # ---- embedded mpv preview --------------------------------------------

    def _build_preview_source(self, video):
        """Combine the silent video with all the chart's stems mixed down into one
        temp file, so mpv just plays a single file with the full song. The mix is
        re-encoded to PCM, the video is copied. No stems or no ffmpeg and it falls
        back to the silent video."""
        stems = self._find_stems()
        if not (stems and ffmpegAvailable):
            return video
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               f'bh_preview_{os.getpid()}_{id(self) & 0xffffff}.mkv')
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', video]
            for s in stems:
                cmd += ['-i', s]
            n = len(stems)
            if n == 1:
                cmd += ['-map', '0:v:0', '-map', '1:a:0']
            else:
                # sum the stems. normalize=0 keeps their levels, so you get the
                # original mix back instead of everything scaled down by count.
                labels = ''.join(f'[{i + 1}:a]' for i in range(n))
                cmd += ['-filter_complex',
                        f'{labels}amix=inputs={n}:normalize=0:duration=longest[a]',
                        '-map', '0:v:0', '-map', '[a]']
            cmd += ['-c:v', 'copy', '-c:a', 'pcm_s16le', '-y', tmp]
            subprocess.run(cmd, check=True, creationflags=NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                self._tmp_mux = tmp
                return tmp
        except Exception:
            pass
        return video

    def _start_embedded(self):
        video = os.path.join(self._song.folder, 'video.mp4')
        if not (self._video_frame and os.path.exists(video)):
            self._embedded = False
            return
        # mixing the stems takes a sec, so do it off the UI thread and start
        # playback back on the main thread once the file's ready
        threading.Thread(target=self._prepare_and_play,
                         args=(video,), daemon=True).start()

    def _prepare_and_play(self, video):
        source = self._build_preview_source(video)
        self.after(0, lambda: self._play_source(source))

    def _play_source(self, source):
        if not self._video_frame or not self._video_frame.winfo_exists():
            return   # editor closed while preparing
        try:
            wid = int(self._video_frame.winfo_id())
            _kw = dict(
                wid=str(wid), vo='gpu', osc=False, keep_open='yes',
                force_media_title=f'{self._song.label} (BackstageHero preview)',
                log_handler=lambda *a: None)
            _dbg = os.environ.get('BH_MPV_LOG')   # diagnostics: path to mpv log file
            if _dbg:
                _kw['log_file'] = _dbg
                _kw['msg_level'] = 'all=v,ao=debug'
            self._player = mpvlib.MPV(**_kw)
            a = self._song_start_pos()
            self._player.audio_delay = self._audio_delay()
            self._player.loadfile(source, 'replace', start=str(a))
            self.after(250, self._post_load)
        except Exception:
            self._teardown_player()
            self._embedded = False

    def _post_load(self):
        """Make sure the preview is audible once the file is loaded. No looping:
        the clip plays through so the video gets past its intro into real
        footage, and Restart replays from the sync point on demand."""
        p = self._player
        if not p:
            return
        try:
            p.mute = False
            p.volume = 100
        except Exception:
            pass

    def _apply_live_delay(self):
        if self._player:
            try:
                self._player.audio_delay = self._audio_delay()
            except Exception:
                pass

    def _toggle_play(self):
        if not self._player:
            return
        try:
            paused = not bool(self._player.pause)
            self._player.pause = paused
            self._play_btn.configure(text='▶  Play' if paused else '⏸  Pause')
        except Exception:
            pass

    def _restart_at_song(self):
        if not self._player:
            return
        try:
            self._player.pause = False
            self._play_btn.configure(text='⏸  Pause')
            self._player.seek(self._song_start_pos(), reference='absolute')
        except Exception:
            pass

    def _teardown_player(self):
        if self._player:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        if self._tmp_mux:
            try:
                os.remove(self._tmp_mux)
            except Exception:
                pass
            self._tmp_mux = None

    # ---- ffplay fallback (no libmpv) -------------------------------------

    def _kill_preview(self):
        for p in (self._proc, self._proc_aux):
            if p and p.poll() is None:
                p.terminate()
        self._proc = self._proc_aux = None

    def _launch_preview(self):
        if not ffplayPath:
            return
        self._kill_preview()
        ms     = self._ms.get()
        v_seek = max(0.0, -ms / 1000.0)
        a_seek = max(0.0,  ms / 1000.0)
        video  = os.path.join(self._song.folder, 'video.mp4')
        if not os.path.exists(video):
            return
        stems = self._find_stems()
        ffplay_base = [ffplayPath, '-hide_banner',
                       '-x', '640', '-y', '360', '-autoexit',
                       '-window_title', f'Preview: {self._song.label}']
        if stems and ffmpegAvailable:
            # Mix all stems (so the audio doesn't drop out on guitar-only
            # sections) and copy the video, piped to ffplay.
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                   '-ss', f'{v_seek:.3f}', '-i', video]
            for s in stems:
                cmd += ['-ss', f'{a_seek:.3f}', '-i', s]
            n = len(stems)
            if n == 1:
                cmd += ['-map', '0:v', '-map', '1:a']
            else:
                labels = ''.join(f'[{i + 1}:a]' for i in range(n))
                cmd += ['-filter_complex',
                        f'{labels}amix=inputs={n}:normalize=0:duration=longest[a]',
                        '-map', '0:v', '-map', '[a]']
            cmd += ['-c:v', 'copy', '-shortest', '-f', 'nut', 'pipe:1']
            feeder = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            self._proc = subprocess.Popen(
                ffplay_base + ['-'], stdin=feeder.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)
            feeder.stdout.close()
            self._proc_aux = feeder
        else:
            self._proc = subprocess.Popen(
                ffplay_base + ['-ss', f'{v_seek:.3f}', video],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)
        if self._live_lbl:
            self._live_lbl.configure(text='● live', text_color=_GREEN)

    def _save(self):
        ms, share = self._ms.get(), self._share.get()
        self._close()
        if self._on_save:
            self._on_save(ms, share)

    def _close(self):
        self._teardown_player()
        self._kill_preview()
        self.grab_release()
        self.destroy()

    def _center(self):
        self.update_idletasks()
        pw = self.master.winfo_x() + self.master.winfo_width() // 2
        ph = self.master.winfo_y() + self.master.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{pw - w // 2}+{ph - h // 2}')


class UpdateDialog(ctk.CTkToplevel):
    """Small modal window shown while the app updates itself: status line and a
    progress bar so the user always sees what the updater is doing."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title('Updating BackstageHero')
        self.geometry('420x150')
        self.resizable(False, False)
        self.configure(fg_color=_BG)
        self.transient(parent)
        # no close button, the updater runs this to the end itself
        self.protocol('WM_DELETE_WINDOW', lambda: None)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        ctk.CTkLabel(self, text='Updating BackstageHero',
                     font=ctk.CTkFont(size=15, weight='bold'),
                     text_color=_TEXT).pack(padx=24, pady=(22, 4), anchor='w')
        self._stat = ctk.CTkLabel(self, text='Preparing...',
                                  font=ctk.CTkFont(size=12), text_color=_SUBTEXT)
        self._stat.pack(padx=24, anchor='w')
        self._bar = ctk.CTkProgressBar(self, width=372, height=14,
                                       progress_color=_BLUE)
        self._bar.set(0)
        self._bar.pack(padx=24, pady=(12, 0))
        self._bar.configure(mode='indeterminate')
        self._bar.start()
        self._detail = ctk.CTkLabel(self, text='',
                                    font=ctk.CTkFont(size=10), text_color=_SUBTEXT)
        self._detail.pack(padx=24, pady=(6, 0), anchor='w')

        self.after(50, self._center)
        self.grab_set()

    def _center(self):
        self.update_idletasks()
        try:
            pw = self.master.winfo_x() + self.master.winfo_width() // 2
            ph = self.master.winfo_y() + self.master.winfo_height() // 2
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f'+{pw - w // 2}+{ph - h // 2}')
        except Exception:
            pass

    def _det(self):
        # Stop any running indeterminate animation before showing a real value.
        try:
            self._bar.stop()
        except Exception:
            pass
        self._bar.configure(mode='determinate')

    def update_stage(self, stage, **info):
        if not self.winfo_exists():
            return
        if stage == 'download':
            got, total = info.get('got', 0), info.get('total', 0)
            self._stat.configure(text='Downloading the new version...')
            if total:
                self._det()
                self._bar.set(got / total)
                self._detail.configure(
                    text=f'{got / 1048576:.1f} MB of {total / 1048576:.1f} MB')
        elif stage == 'verify':
            self._bar.configure(mode='indeterminate'); self._bar.start()
            self._stat.configure(text='Verifying the download...')
            self._detail.configure(text='')
        elif stage == 'install':
            self._stat.configure(text='Installing...')
        elif stage == 'restart':
            self._det(); self._bar.set(1.0)
            self._stat.configure(text='Done, restarting BackstageHero...')
            self._detail.configure(text='')
        elif stage == 'error':
            self._det(); self._bar.set(0)
            self._stat.configure(text='Update failed. Keeping the current version.',
                                 text_color=_RED)
            self._detail.configure(text=info.get('msg', ''))
            self.protocol('WM_DELETE_WINDOW', self.destroy)
            ctk.CTkButton(self, text='Close', width=80, command=self.destroy,
                          fg_color='#313244', hover_color='#414160').pack(pady=(8, 0))


# (key, label, description) -- each tool scans the whole library and
# supports dry-run; the description is shown verbatim in the dialog.
_LIBRARY_TOOLS = (
    ('repair_videos', 'Repair videos',
     'Detects variable-frame-rate video and re-encodes it to a constant '
     'frame rate. Also removes unsupported (non-VP8) WebM files left by '
     'other tools -- the song then re-downloads on the next run.'),
    ('fix_chart_names', 'Fix chart names',
     'Renames ID-suffixed song.ini/notes.chart/audio-stem/album-art files, '
     'verifying content matches first. Anything unconfirmed is moved to '
     '_needs_review/, never guessed at.'),
    ('enrich_metadata', 'Enrich metadata',
     'Fills blank song.ini fields (year/genre/charter/album) from a '
     'confident Chorus Encore match. Never overwrites an existing value.'),
    ('find_duplicates', 'Find duplicates',
     'Finds duplicate charts of the same song, scores each copy, and moves '
     'everything but the best-scoring keeper to _duplicates_review/. Never '
     'deletes anything.'),
    ('find_static_art', 'Find static album-art videos',
     'Detects videos that are just an album cover held for the whole song, '
     'converts them to album art, and removes the video. Anything uncertain '
     '-- a slow zoom, a visualizer -- is only reported, never acted on.'),
    ('migrate_review_folders', 'Move old review folders out of the library',
     'Earlier versions put _needs_review inside your Songs folder, where Clone '
     'Hero still loads them and this app still downloads videos for them, but '
     'no repair scan can find them again. Moves any it finds to a folder '
     'alongside your library instead. Nothing is deleted.'),
)


class LibraryToolsDialog(ctk.CTkToplevel):
    """Library-wide hygiene scans: video repair, chart-name fixes, metadata
    enrichment, duplicate detection.

    Each runs the whole library in a background thread so the window stays
    responsive; the summary shown when it finishes is built from the scan's
    own returned counts dict, not parsed console output. Only one tool runs
    at a time -- fix_chart_names and find_duplicates both read/write the
    same per-folder chart_rename_status, so overlapping runs could race.
    """

    def __init__(self, parent, songs_folder, on_close=None, on_run_state=None):
        super().__init__(parent)
        self._songs_folder = songs_folder
        self._on_close = on_close
        # Told when a tool starts and stops, so the main window can stay
        # locked for the worker's real lifetime rather than the dialog's --
        # closing this window does not stop the thread (there is no Stop
        # button to offer, and killing a scan mid-rename would be worse than
        # letting it finish).
        self._on_run_state = on_run_state
        self._running_key = None
        self._dry_run_vars = {}
        self._status_labels = {}
        self._run_buttons = {}

        self.title('Library Tools')
        self.geometry('600x620')
        self.minsize(480, 380)
        self.resizable(True, True)
        self.configure(fg_color=_BG)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self._close)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._build()
        self.after(50, self._center)

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.grid(row=0, column=0, sticky='ew')
        ctk.CTkLabel(header, text='Library Tools',
                     font=ctk.CTkFont(size=16, weight='bold'),
                     text_color=_TEXT).pack(padx=20, pady=(18, 2), anchor='w')
        ctk.CTkLabel(
            header,
            text='Each tool scans your whole library. Dry run previews what '
                 'would happen without changing anything on disk.',
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            wraplength=530, justify='left').pack(padx=20, anchor='w')

        # Scrollable so the window can be resized smaller (or run at a
        # higher OS text-scaling setting) without any card's content or the
        # Close button getting clipped off-screen.
        scroll = ctk.CTkScrollableFrame(self, fg_color='transparent')
        scroll.grid(row=1, column=0, sticky='nsew', padx=8, pady=(8, 0))

        for key, label, desc in _LIBRARY_TOOLS:
            card = ctk.CTkFrame(scroll, fg_color='#252540', corner_radius=10)
            card.pack(fill='x', padx=12, pady=(10, 0))

            top = ctk.CTkFrame(card, fg_color='transparent')
            top.pack(fill='x', padx=16, pady=(12, 2))
            ctk.CTkLabel(top, text=label, font=ctk.CTkFont(size=13, weight='bold'),
                         text_color=_TEXT).pack(side='left')

            dry_var = tk.BooleanVar(value=True)
            self._dry_run_vars[key] = dry_var
            ctk.CTkCheckBox(top, text='Dry run', variable=dry_var,
                            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
                            checkbox_width=16, checkbox_height=16,
                            checkmark_color=_BG, fg_color=_BLUE,
                            hover_color='#7aaef8').pack(side='right')

            ctk.CTkLabel(card, text=desc, font=ctk.CTkFont(size=10),
                         text_color=_SUBTEXT, wraplength=480, justify='left',
                         anchor='w').pack(fill='x', padx=16, anchor='w')

            row = ctk.CTkFrame(card, fg_color='transparent')
            row.pack(fill='x', padx=16, pady=(10, 12))
            status_lbl = ctk.CTkLabel(row, text='Ready', font=ctk.CTkFont(size=10),
                                      text_color=_SUBTEXT, anchor='w')
            status_lbl.pack(side='left', fill='x', expand=True)
            self._status_labels[key] = status_lbl

            run_btn = ctk.CTkButton(row, text='Run', width=90, height=28,
                                    font=ctk.CTkFont(size=11),
                                    fg_color='#313244', hover_color='#414160',
                                    command=lambda k=key: self._run_tool(k))
            run_btn.pack(side='right')
            self._run_buttons[key] = run_btn

        ctk.CTkButton(self, text='Close', width=100,
                      fg_color='transparent', border_width=1,
                      border_color=_BORDER, hover_color='#30304a',
                      text_color=_SUBTEXT, font=ctk.CTkFont(size=12),
                      command=self._close).grid(row=2, column=0, pady=18)

    def _run_tool(self, key):
        if self._running_key is not None:
            return
        dry_run = self._dry_run_vars[key].get()
        self._running_key = key
        self._dry_run_of_current = dry_run
        self._notify_run_state(True)
        for btn in self._run_buttons.values():
            btn.configure(state='disabled')
        self._status_labels[key].configure(text='Running...', text_color=_BLUE)
        threading.Thread(target=self._worker, args=(key, dry_run), daemon=True).start()

    def _notify_run_state(self, running):
        """Tell the main window a worker started or stopped.

        Called straight from the worker thread, deliberately NOT marshalled
        through self.after(): this dialog may already be destroyed by the time
        a scan ends, and that is precisely when the main window most needs the
        news. The receiver owns its own thread-safety (it sets a plain flag
        first, then schedules its own UI refresh) -- see App._set_tool_running.
        """
        if self._on_run_state is None:
            return
        try:
            self._on_run_state(running)
        except Exception:
            log.exception('Failed to report library-tool run state (running=%s)', running)

    def _worker(self, key, dry_run):
        try:
            if key == 'repair_videos':
                counts = video_repair.scan_and_repair_video_library(self._songs_folder, dry_run=dry_run)
            elif key == 'fix_chart_names':
                counts = chart_rename.scan_and_fix_chart_library(self._songs_folder, dry_run=dry_run)
            elif key == 'enrich_metadata':
                counts = metadata_enrichment.enrich_song_ini_metadata_library(self._songs_folder, dry_run=dry_run)
            elif key == 'find_duplicates':
                counts = dedupe_report.generate_dedupe_report(self._songs_folder, dry_run=dry_run)
            elif key == 'find_static_art':
                counts = static_art.scan_and_convert_static_art_library(self._songs_folder, dry_run=dry_run)
            elif key == 'migrate_review_folders':
                counts = library_common.migrate_legacy_review_folders(self._songs_folder, dry_run=dry_run)
            else:
                counts = {}
            text = self._format_summary(key, counts, dry_run)
            color = _GREEN
        except Exception as e:
            log.exception('Library tool %s failed', key)
            text = f'Error: {e}'
            color = _RED
        # unlock the main window on the parent's loop, which is still alive
        # whether or not this dialog is -- must happen before the _finish
        # attempt below, since that one legitimately fails on a closed dialog
        self._notify_run_state(False)
        try:
            # the dialog may have been closed while the scan was running --
            # scheduling on a destroyed Toplevel raises, so this is a no-op
            # in that case rather than a crash
            self.after(0, lambda: self._finish(key, text, color))
        except Exception:
            pass

    @staticmethod
    def _format_summary(key, counts, dry_run):
        suffix = ' (dry run)' if dry_run else ''
        if key == 'repair_videos':
            body = (f"{counts.get('ok', 0)} ok, {counts.get('reencoded_cfr', 0)} re-encoded, "
                    f"{counts.get('removed_unsupported_codec', 0)} removed, "
                    f"{counts.get('reencode_failed', 0)} failed")
        elif key == 'fix_chart_names':
            body = (f"{counts.get('confirmed_ok', 0)} confirmed, "
                    f"{counts.get('needs_review', 0)} need review, "
                    f"{counts.get('skipped_settled', 0)} already settled")
        elif key == 'enrich_metadata':
            body = (f"{counts.get('filled', 0)} filled, {counts.get('no_change', 0)} no change, "
                    f"{counts.get('no_match', 0)} no match, {counts.get('error', 0)} error(s)")
        elif key == 'find_duplicates':
            body = (f"{counts.get('resolved', 0)} resolved, "
                    f"{counts.get('skipped_all_ineligible', 0)} unscanned, "
                    f"{counts.get('skipped_not_confirmed', 0)} unconfirmed")
        elif key == 'find_static_art':
            body = (f"{counts.get('converted', 0)} converted, "
                    f"{counts.get('near_static', 0)} near-static (reported), "
                    f"{counts.get('ok', 0)} real videos left alone")
        elif key == 'migrate_review_folders':
            if not counts:
                body = 'nothing to migrate - no old review folders inside your library'
            else:
                # past tense on a dry run would read as if files had moved
                moved = counts.get('would_move', 0) if dry_run else counts.get('moved', 0)
                body = (f"{moved} folder(s) would move out" if dry_run
                        else f"{moved} folder(s) moved out")
                if counts.get('conflict'):
                    body += f", {counts['conflict']} already existed (left in place)"
                if counts.get('failed'):
                    body += f", {counts['failed']} failed (details in log)"
        else:
            body = str(counts)
        return body + suffix

    def _finish(self, key, text, color):
        self._running_key = None
        if not self.winfo_exists():
            return
        for btn in self._run_buttons.values():
            btn.configure(state='normal')
        self._status_labels[key].configure(text=text, text_color=color)

    def _close(self):
        # Closing does not stop the worker. Say so plainly: the thread keeps
        # renaming and relocating files, and the previous version released the
        # modal grab silently -- leaving the user free to press Start and race
        # a download against a scan still mutating the same folders.
        if self._running_key is not None:
            tool = next((label for k, label, _ in _LIBRARY_TOOLS
                         if k == self._running_key), self._running_key)
            verb = 'previewing' if getattr(self, '_dry_run_of_current', True) else 'changing files in'
            if not messagebox.askokcancel(
                    'Scan still running',
                    f'"{tool}" is still {verb} your library.\n\n'
                    'Closing this window will not stop it. The scan finishes on its own, '
                    'and the main window stays locked until it does.\n\nClose anyway?',
                    parent=self):
                return
        self.grab_release()
        self.destroy()
        # Skip the caller's library reload while a scan is still running -- it
        # would read a folder tree being rewritten underneath it. The run-state
        # callback reloads once the worker actually finishes.
        if self._on_close and self._running_key is None:
            self._on_close()

    def _center(self):
        self.update_idletasks()
        pw = self.master.winfo_x() + self.master.winfo_width() // 2
        ph = self.master.winfo_y() + self.master.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{pw - w // 2}+{ph - h // 2}')


class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title(f'BackstageHero  v{__version__}')
        self.geometry('1020x680')
        self.minsize(900, 520)
        self.configure(fg_color=_BG)

        # Window + taskbar icon
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        # State
        self._songs       : list[Song]       = []
        self._filtered    : list[Song]       = []
        self._iid_map     : dict[str, Song]  = {}
        self._iid_of      : dict[int, str]   = {}   # id(Song) -> tree iid (O(1) row updates)
        self._sort_col    : str  = 'label'
        self._sort_asc    : bool = True
        self._filter_mode : str  = 'missing'
        self._running     : bool = False
        self._resync_run  : bool = False   # is the active run Auto-sync, not download?
        # A Library Tools worker can outlive its dialog, so this tracks the
        # THREAD, not the window. Without it, closing that dialog mid-scan
        # freed the user to start a download into the same folders a rename
        # sweep was still working through.
        self._tool_running: bool = False
        self._polling     : bool = False
        self._stop_evt    = threading.Event()
        self._queue       : queue.Queue = queue.Queue()
        self._songs_folder: str  = ''
        self._pending_update = None      # (version, asset, sha) deferred during a run
        self._search_after = None        # debounce handle for the search box
        self._settings    = _load_settings()
        resolver_client.set_sharing(self._settings.get('share_matches', True))
        self._sync_ready  : bool = (
            ffmpegAvailable and audiosync is not None
            and audiosync.is_available())

        self._build_ui()
        # Poll the queue from the start so update/ping results are handled even
        # before a library is loaded.
        self._polling = True
        self.after(200, self._poll_queue)
        self.after(120, self._startup)
        self.after(500, self._start_update_check)

    def _build_ui(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=52)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.grid_propagate(False)
        # col 1 is a spacer; col 2 holds the folder path and expands
        hdr.grid_columnconfigure(1, weight=0)
        hdr.grid_columnconfigure(2, weight=1)

        logo_png = _asset_path('logo.png')
        if os.path.exists(logo_png):
            from PIL import Image as _PILImage
            _img = _PILImage.open(logo_png)
            _h = 30
            _w = int(_img.width * _h / _img.height)
            ctk.CTkLabel(hdr, text='',
                         image=ctk.CTkImage(_img, size=(_w, _h))).grid(
                row=0, column=0, padx=(16, 6), pady=11)
        else:
            ctk.CTkLabel(hdr,
                         text='BackstageHero',
                         font=ctk.CTkFont(size=17, weight='bold'),
                         text_color=_TEXT).grid(row=0, column=0, padx=(16, 6), pady=14)
        ctk.CTkLabel(hdr,
                     text=f'v{__version__}',
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTEXT).grid(row=0, column=1, pady=14,
                                               padx=(0, 16), sticky='w')

        folder_row = ctk.CTkFrame(hdr, fg_color='transparent')
        folder_row.grid(row=0, column=2, padx=(0, 12), sticky='e')
        folder_row.grid_columnconfigure(0, weight=1)
        self._folder_lbl = ctk.CTkLabel(
            folder_row, text='No folder selected',
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT, anchor='e')
        self._folder_lbl.grid(row=0, column=0, padx=(0, 10), sticky='e')
        ctk.CTkButton(folder_row, text='Library Tools',
                      width=110, height=28, font=ctk.CTkFont(size=11),
                      fg_color='#313244', hover_color='#414160',
                      command=self._open_library_tools).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(folder_row, text='Change folder',
                      width=115, height=28, font=ctk.CTkFont(size=11),
                      command=self._pick_folder).grid(row=0, column=2)

        # Filter / search bar
        fbar = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=50)
        fbar.grid(row=1, column=0, sticky='ew', pady=(1, 0))
        fbar.grid_propagate(False)
        # Column 3 is a spacer that absorbs leftover space;
        # column 4 is the search box pinned to the right.
        fbar.grid_columnconfigure(3, weight=1)

        _btn = dict(height=30, width=130, font=ctk.CTkFont(size=12),
                    corner_radius=6)

        self._btn_missing = ctk.CTkButton(
            fbar, text='Missing video',
            fg_color=_BLUE, text_color='#11111b', hover_color='#7aaef8',
            command=lambda: self._set_filter('missing'), **_btn)
        self._btn_missing.grid(row=0, column=0, padx=(12, 4), pady=10)

        self._btn_has = ctk.CTkButton(
            fbar, text='Has video',
            fg_color=_SURFACE, text_color=_TEXT,
            border_color=_BORDER, border_width=1, hover_color='#30304a',
            command=lambda: self._set_filter('has'), **_btn)
        self._btn_has.grid(row=0, column=1, padx=4, pady=10)

        self._btn_all = ctk.CTkButton(
            fbar, text='All',
            fg_color=_SURFACE, text_color=_TEXT,
            border_color=_BORDER, border_width=1, hover_color='#30304a',
            command=lambda: self._set_filter('all'), **_btn)
        self._btn_all.grid(row=0, column=2, padx=4, pady=10)

        # Search expands with the window, minimum 200px, no fixed width
        self._search_var = tk.StringVar()
        self._search_var.trace_add('write', lambda *_: self._debounced_filter())
        search = ctk.CTkEntry(fbar, textvariable=self._search_var,
                              placeholder_text='Search artist or title...',
                              height=30)
        search.grid(row=0, column=4, padx=(4, 12), pady=10, sticky='ew')
        fbar.grid_columnconfigure(4, weight=2, minsize=200)

        # Treeview
        tree_outer = tk.Frame(self, bg=_BG, bd=0)
        tree_outer.grid(row=2, column=0, sticky='nsew')
        tree_outer.grid_rowconfigure(0, weight=1)
        tree_outer.grid_columnconfigure(0, weight=1)

        sty = ttk.Style()
        sty.theme_use('clam')
        sty.configure('BH.Treeview',
                       background=_BG, foreground=_TEXT,
                       fieldbackground=_BG, rowheight=30,
                       font=('Segoe UI', 11), borderwidth=0, relief='flat')
        sty.configure('BH.Treeview.Heading',
                       background='#181825', foreground=_SUBTEXT,
                       font=('Segoe UI', 10, 'bold'),
                       relief='flat', borderwidth=0)
        sty.map('BH.Treeview',
                background=[('selected', '#313244')],
                foreground=[('selected', _TEXT)])
        sty.map('BH.Treeview.Heading',
                background=[('active', '#1e1e2e'), ('pressed', '#1e1e2e')])
        sty.layout('BH.Treeview', [('BH.Treeview.treearea', {'sticky': 'nswe'})])
        sty.configure('BH.Vertical.TScrollbar',
                       background=_BORDER, troughcolor=_BG,
                       borderwidth=0, relief='flat', arrowsize=12)

        self._tree = ttk.Treeview(
            tree_outer, style='BH.Treeview',
            columns=('check', 'label', 'res', 'status'),
            show='headings', selectmode='none')

        self._tree.heading('check',  text='',
                           anchor='center', command=self._toggle_all)
        self._tree.heading('label',  text='Song / Artist  ↕',
                           anchor='w',      command=lambda: self._sort('label'))
        self._tree.heading('res',    text='Resolution  ↕',
                           anchor='center', command=lambda: self._sort('res'))
        self._tree.heading('status', text='Status', anchor='w')

        self._tree.column('check',  width=36,  minwidth=36,  anchor='center', stretch=False)
        self._tree.column('label',  width=520, minwidth=200, anchor='w',      stretch=True)
        self._tree.column('res',    width=95,  minwidth=70,  anchor='center', stretch=False)
        self._tree.column('status', width=170, minwidth=100, anchor='w',      stretch=False)

        self._tree.tag_configure('done',  foreground=_GREEN)
        self._tree.tag_configure('error', foreground=_RED)
        self._tree.tag_configure('busy',  foreground=_BLUE)
        self._tree.tag_configure('warn',  foreground=_YELLOW)
        self._tree.tag_configure('dim',   foreground=_SUBTEXT)
        # Slightly more contrast between rows so a long list is easy to scan
        self._tree.tag_configure('row_even', background='#272740')
        self._tree.tag_configure('row_odd',  background='#1e1e2e')

        vsb = ttk.Scrollbar(tree_outer, orient='vertical',
                            style='BH.Vertical.TScrollbar',
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        self._tree.bind('<Button-1>', self._on_tree_click)
        self._tree.bind('<Button-3>', self._on_tree_right_click)

        # Footer
        foot = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=64)
        foot.grid(row=3, column=0, sticky='ew', pady=(1, 0))
        foot.grid_propagate(False)
        foot.grid_columnconfigure(7, weight=1)

        _fbtn = dict(height=34, corner_radius=7)
        _font  = ctk.CTkFont(size=12)
        _font_bold = ctk.CTkFont(size=13, weight='bold')

        self._sel_btn = ctk.CTkButton(
            foot, text='Select all', width=105, font=_font,
            fg_color=_BORDER, hover_color='#44445a', text_color=_TEXT,
            command=self._select_all_visible, **_fbtn)
        self._sel_btn.grid(row=0, column=0, padx=(12, 4), pady=15)

        self._clr_btn = ctk.CTkButton(
            foot, text='Clear all', width=90, font=_font,
            fg_color='transparent', hover_color='#30304a',
            text_color=_SUBTEXT, border_color=_BORDER, border_width=1,
            command=self._clear_all, **_fbtn)
        self._clr_btn.grid(row=0, column=1, padx=4, pady=15)

        q = self._settings.get('quality', '720p')
        self._quality_var = tk.StringVar(value=q if q in ('720p', '1080p') else '720p')
        ctk.CTkOptionMenu(
            foot, variable=self._quality_var,
            values=['720p', '1080p'], width=88, height=34,
            command=lambda *_: self._persist_setting('quality', self._quality_var.get()),
            font=_font).grid(row=0, column=2, padx=4, pady=15)

        self._start_btn = ctk.CTkButton(
            foot, text='▶  Search & Download', width=175, font=_font_bold,
            command=self._start_download, **_fbtn)
        self._start_btn.grid(row=0, column=3, padx=4, pady=15)

        self._resync_btn = ctk.CTkButton(
            foot, text='↺  Auto-sync', width=120, font=_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._start_resync, **_fbtn)
        self._resync_btn.grid(row=0, column=4, padx=4, pady=15)

        self._stop_btn = ctk.CTkButton(
            foot, text='■  Stop', width=90, font=_font,
            fg_color='#4a1a2a', hover_color='#6a2a3e', text_color=_RED,
            state='disabled', command=self._stop, **_fbtn)
        self._stop_btn.grid(row=0, column=5, padx=4, pady=15)

        # Opt-in/out of contributing confirmed matches back to the community pool.
        self._share_var = tk.BooleanVar(
            value=self._settings.get('share_matches', True))
        share_cb = ctk.CTkCheckBox(
            foot, text='Share matches', variable=self._share_var,
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=self._on_share_toggle)
        share_cb.grid(row=0, column=6, padx=(12, 4), pady=15)

        # Progress + status (right side of footer)
        prog_frame = ctk.CTkFrame(foot, fg_color='transparent')
        prog_frame.grid(row=0, column=7, padx=(8, 16), pady=15, sticky='e')

        self._progress = ctk.CTkProgressBar(prog_frame, width=190, height=8,
                                             corner_radius=4)
        self._progress.set(0)
        self._progress.pack(pady=(2, 4))

        self._status_lbl = ctk.CTkLabel(
            prog_frame, text='Ready',
            font=ctk.CTkFont(size=10), text_color=_SUBTEXT,
            anchor='e', width=190)
        self._status_lbl.pack()

        self._update_buttons()

    def _persist_setting(self, key, value):
        self._settings[key] = value
        _save_settings(self._settings)

    def _on_share_toggle(self):
        on = bool(self._share_var.get())
        resolver_client.set_sharing(on)
        self._persist_setting('share_matches', on)

    def _startup(self):
        saved = _load_songs_path()
        if saved:
            ok, _ = _validate_folder(saved)
            if ok:
                self._load_library(saved)
                return
            messagebox.showwarning(
                'Songs folder not found',
                f'Your previously used Songs folder can\'t be found at:\n\n'
                f'  {saved}\n\n'
                f'Has it been moved or renamed? Please choose its new location.')
        self._pick_folder(first_run=True)

    def _start_update_check(self):
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        """Background thread: ping the resolver, then check for app and yt-dlp updates."""
        try:
            import VideoDownload as _vd
            cur_ver   = _vd.__version__
            ytdlp_ver = getattr(_vd.yt_dlp.version, '__version__', '0')

            resolver_client.ping(sharing=resolver_client.sharing_enabled(),
                                 app_version=cur_ver)

            info = updater.check_app_update(cur_ver)
            if info:
                latest, asset, sha = info
                self._queue.put(('app_update_available', latest, asset, sha))

            new_ytdlp = updater.maybe_update_ytdlp(ytdlp_ver)
            if new_ytdlp:
                self._queue.put(('ytdlp_updated', new_ytdlp))
        except Exception:
            pass

    def _offer_update(self, latest, asset, sha):
        if messagebox.askyesno(
                'Update available',
                f'v{latest} is available. Install now?\n\n'
                'The app will restart automatically.'):
            self._do_app_update(asset, sha)

    def _flush_pending_update(self):
        """Show a deferred update prompt once a run has finished."""
        if self._pending_update and not self._running:
            latest, asset, sha = self._pending_update
            self._pending_update = None
            self._offer_update(latest, asset, sha)

    def _do_app_update(self, asset, sha):
        """Called on the main thread after user confirms. Shows a progress
        window and runs the download/install on a background thread."""
        dlg = UpdateDialog(self)

        def on_status(stage, **info):
            # runs on the worker thread, bounce it back onto the UI thread
            self.after(0, lambda: dlg.update_stage(stage, **info))

        def _worker():
            ok = updater.apply_app_update(asset, sha, status_cb=on_status)
            if ok:
                self.after(600, self.destroy)   # let "Restarting..." show briefly
        threading.Thread(target=_worker, daemon=True).start()

    def _pick_folder(self, first_run=False):
        while True:
            path = filedialog.askdirectory(
                title='Select your Clone Hero Songs folder',
                mustexist=True)
            if not path:
                if first_run:
                    if messagebox.askyesno(
                            'No folder selected',
                            'A Songs folder is required to continue.\n\n'
                            'Would you like to choose one now?'):
                        continue
                    self.destroy()
                return

            ok, msg = _validate_folder(path)
            if ok:
                _save_songs_path(path)
                self._load_library(path)
                return

            messagebox.showerror('Invalid Songs folder', msg +
                                 '\n\nPlease try again.')

    def _load_library(self, path):
        self._songs_folder = path
        display = path if len(path) < 58 else '...' + path[-55:]
        self._folder_lbl.configure(text=f'Songs:  {display}')
        self._status_lbl.configure(text='Scanning your library...')

        # Make sure the queue is being polled before the scan thread posts to it.
        if not self._polling:
            self._polling = True
            self.after(200, self._poll_queue)

        # Scan off the UI thread so the window stays responsive on big libraries.
        def _worker():
            songs = _scan_library(
                path, progress=lambda c: self._queue.put(('scan_progress', c)))
            self._queue.put(('library_scanned', songs))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_library_scanned(self, songs):
        self._songs = songs
        self._apply_filter()
        n = len(songs)
        unprobed = [s for s in songs if s.has_video and s.res == '...']
        if unprobed and ffmpegAvailable:
            self._status_lbl.configure(
                text=f'{n} songs found, reading resolutions...')
            threading.Thread(target=self._probe_resolutions,
                             args=(unprobed, n), daemon=True).start()
        else:
            self._status_lbl.configure(
                text=f'{n} song{"s" if n != 1 else ""} found')
        self._export_library_csv()
        self._update_buttons()

    CSV_NAME = 'backstagehero_library.csv'

    def _export_library_csv(self):
        """Write a spreadsheet of the library next to the songs themselves.

        Rewritten after every scan so it never quietly goes stale. Failure is
        logged and otherwise ignored on purpose -- a read-only or full drive
        should cost the user a convenience file, not the ability to use the
        app. Clone Hero ignores a loose .csv in the Songs root.
        """
        if not self._songs_folder or not self._songs:
            return
        path = os.path.join(self._songs_folder, self.CSV_NAME)
        try:
            # newline='' is required by csv on Windows, otherwise every row is
            # written with a blank line between it and the next
            with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.writer(fh)
                w.writerow(['Song', 'Artist', 'Title', 'Has video', 'Resolution',
                            'Offset (ms)', 'Offset source', 'Video ID',
                            'Dumped videos', 'Folder'])
                for s in sorted(self._songs, key=lambda x: x.key):
                    artist, title = read_metadata(s.folder)
                    w.writerow([
                        s.label,
                        artist or '',
                        title or '',
                        _video_status(s),
                        s.res if s.has_video else '',
                        _read_song_value(s.folder, 'video_start_time'),
                        # the provenance marker, so a spreadsheet sort shows at
                        # a glance which songs were never actually measured
                        _read_song_value(s.folder, 'backstagehero_sync'),
                        _read_song_value(s.folder, 'backstagehero_source'),
                        ' '.join(sorted(get_rejected_sources(s.folder))),
                        s.folder,
                    ])
        except OSError as e:
            log.warning('Could not write %s: %s', path, e)

    def _probe_resolutions(self, songs, total_songs):
        """Background thread: probe resolutions for unprobed videos, a few in
        parallel (ffmpeg is the bottleneck, so this is ~Nx faster on first scan)."""
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(probe_resolution, s.folder): s for s in songs}
            for fut in as_completed(futures):
                if self._stop_evt.is_set():
                    break
                s = futures[fut]
                try:
                    s.res = fut.result() or '?'
                except Exception:
                    s.res = '?'
                done += 1
                remaining = len(songs) - done
                self._queue.put(('res_update', s,
                                 f'{total_songs} songs found, reading resolutions ({remaining} left)...'
                                 if remaining else f'{total_songs} songs found'))
        # rewrite the CSV once resolutions are known -- the copy written right
        # after the scan has '...' placeholders in that column
        if not self._stop_evt.is_set():
            self._queue.put(('csv_refresh',))

    def _set_filter(self, mode):
        self._filter_mode = mode
        _active   = dict(fg_color=_BLUE, text_color='#11111b',
                         hover_color='#7aaef8', border_width=0)
        _inactive = dict(fg_color=_SURFACE, text_color=_TEXT,
                         border_color=_BORDER, border_width=1,
                         hover_color='#30304a')
        self._btn_missing.configure(**(_active if mode == 'missing' else _inactive))
        self._btn_has.configure(    **(_active if mode == 'has'     else _inactive))
        self._btn_all.configure(    **(_active if mode == 'all'     else _inactive))
        self._apply_filter()

    def _debounced_filter(self):
        """Coalesce rapid keystrokes so a big library isn't re-filtered on every
        character."""
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.after(180, self._apply_filter)

    def _apply_filter(self):
        self._search_after = None
        term = self._search_var.get().lower()
        mode = self._filter_mode
        out  = []
        for s in self._songs:
            if mode == 'missing' and s.has_video:     continue
            if mode == 'has'     and not s.has_video: continue
            if term and term not in s.key:            continue
            out.append(s)
        # Sort
        if self._sort_col == 'res':
            def _res_key(s):
                m = re.match(r'(\d+)', s.res)
                return int(m.group(1)) if m else 0
            out.sort(key=_res_key, reverse=not self._sort_asc)
        else:
            out.sort(key=lambda s: s.key, reverse=not self._sort_asc)
        self._filtered = out
        self._refresh_tree()

    def _sort(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        up = '↑' if self._sort_asc else '↓'
        self._tree.heading('label',
            text=f'Song / Artist  {up if col == "label" else "↕"}')
        self._tree.heading('res',
            text=f'Resolution  {up if col == "res" else "↕"}')
        self._apply_filter()

    def _refresh_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._iid_map.clear()
        self._iid_of.clear()
        for i, s in enumerate(self._filtered):
            chk         = '  ☑' if s.checked else '  ☐'
            res_disp    = s.res or '-'
            status_text = s.status
            if not status_text:
                status_text = '✔' if s.has_video else '✗'
            stag     = s.stag or ('dim' if s.has_video else 'error')
            row_tag  = 'row_even' if i % 2 == 0 else 'row_odd'
            tags     = (row_tag,) + ((stag,) if stag else ())
            iid = self._tree.insert('', 'end',
                                    values=(chk, s.label, res_disp, status_text),
                                    tags=tags)
            self._iid_map[iid] = s
            self._iid_of[id(s)] = iid
        self._update_buttons()

    def _update_row(self, s: Song):
        """Refresh the single visible row for Song s (O(1) lookup)."""
        iid = self._iid_of.get(id(s))
        if not iid:
            return   # not currently visible under the active filter
        chk         = '  ☑' if s.checked else '  ☐'
        res_disp    = s.res or '-'
        status_text = s.status or ('✔' if s.has_video else '✗')
        stag        = s.stag or ('dim' if s.has_video else 'error')
        existing    = list(self._tree.item(iid, 'tags'))
        row_tag     = next((t for t in existing
                            if t in ('row_even', 'row_odd')), 'row_odd')
        tags = (row_tag,) + ((stag,) if stag else ())
        self._tree.item(iid,
                        values=(chk, s.label, res_disp, status_text),
                        tags=tags)

    def _on_tree_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region == 'heading':
            return      # column-header command handles sort
        if region in ('cell', 'tree'):
            iid = self._tree.identify_row(event.y)
            if iid and iid in self._iid_map:
                s = self._iid_map[iid]
                s.checked = not s.checked
                self._update_row(s)
                self._update_buttons()

    def _toggle_all(self):
        """Click on the checkbox column header: select/deselect all visible."""
        all_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        for s in self._filtered:
            s.checked = not all_on
        self._refresh_tree()

    def _select_all_visible(self):
        """Select/Deselect all, just the songs in the current filter view."""
        all_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        for s in self._filtered:
            s.checked = not all_on
        self._refresh_tree()

    def _clear_all(self):
        """Uncheck every song in the full library, regardless of current filter."""
        for s in self._songs:
            s.checked = False
        self._refresh_tree()

    def _on_tree_right_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid or iid not in self._iid_map:
            return
        s = self._iid_map[iid]
        menu = tk.Menu(self, tearoff=0,
                       bg=_SURFACE, fg=_TEXT,
                       activebackground='#313244', activeforeground=_TEXT,
                       relief='flat', borderwidth=1,
                       activeborderwidth=0)
        if s.has_video:
            menu.add_command(label='Adjust sync offset',
                             command=lambda: self._open_sync_editor(s))
            menu.add_command(label='Dump this video (wrong video?)',
                             command=lambda: self._dump_video(s))
            menu.add_separator()
        menu.add_command(label='Open folder',
                         command=lambda: _open_in_file_manager(s.folder))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _dump_video(self, song: Song):
        """Throw away a video that turned out to be the wrong thing entirely.

        Confirmed first: this deletes a file and is not undoable from inside
        the app. The rejection it records is what stops the next run simply
        fetching the same upload again.
        """
        if self._running or self._tool_running:
            messagebox.showinfo('Busy', 'Wait for the current run to finish first.')
            return
        if not messagebox.askokcancel(
                'Dump this video?',
                f'{song.label}\n\n'
                'Deletes the downloaded video and remembers this particular '
                'upload so it is skipped next time.\n\n'
                'The song will be downloaded again on the next run, and should '
                'pick something different.'):
            return

        result = dump_video(song.folder)
        if result['status'] == 'failed':
            messagebox.showerror('Could not dump the video', result['detail'])
            return
        if result['status'] == 'nothing_to_dump':
            messagebox.showinfo('Nothing to dump', result['detail'])
            return

        song.has_video = False
        song.res       = '-'
        song.status    = 'Dumped - will re-download'
        song.stag      = 'dim'
        self._update_row(song)
        self._apply_filter()
        self._export_library_csv()
        self._status_lbl.configure(text=f'Dumped: {result["detail"]}')

    def _open_sync_editor(self, song: Song):
        def on_save(ms: int, share: bool):
            # a hand-set offset outranks anything automatic - mark it so a later
            # re-sync sweep can be told to leave the user's own work alone
            set_ini_values(song.folder, {'video_start_time': str(ms),
                                         'backstagehero_sync': SYNC_MANUAL})
            s_abs = abs(ms) / 1000.0
            if ms < -50:
                song.status = f'Synced  (−{s_abs:.1f}s intro)'
            elif ms > 50:
                song.status = f'Synced  (+{s_abs:.1f}s)'
            else:
                song.status = 'Synced  (aligned)'
            song.stag = 'done'
            self._update_row(song)
            if share and resolver_client.enabled():
                ch  = resolver_client.chart_hash(song.folder)
                vid = get_stored_source(song.folder)
                if ch and vid:
                    artist, title = read_metadata(song.folder)
                    resolver_client.report(ch, vid, ms, 0.5, artist, title)
        SyncEditor(self, song, on_save=on_save)

    def _open_library_tools(self):
        if self._running:
            messagebox.showinfo('Busy', 'Wait for the current download/sync run to finish first.')
            return
        if self._tool_running:
            # a worker from a previously-closed dialog is still going; a second
            # dialog would happily start a second tool over the same library
            messagebox.showinfo(
                'Busy', 'A library scan is still running. Wait for it to finish first.')
            return
        if not self._songs_folder:
            messagebox.showinfo('No folder selected', 'Pick your Songs folder first.')
            return
        LibraryToolsDialog(self, self._songs_folder,
                            on_close=lambda: self._load_library(self._songs_folder),
                            on_run_state=self._set_tool_running)

    def _set_tool_running(self, running):
        """Called FROM THE WORKER THREAD when a Library Tools scan starts/stops.

        The flag assignment is what actually guards the library, so it happens
        here and now -- a bare attribute write, atomic under the GIL, with no
        Tk involved. Only the UI refresh is marshalled onto the main loop, and
        if that scheduling fails the guard is still correct: worst case the
        buttons look stale, rather than the window staying locked forever with
        no way back.
        """
        self._tool_running = running
        try:
            self.after(0, lambda: self._on_tool_state_changed(running))
        except Exception:
            log.exception('Could not schedule UI refresh after tool state change')

    def _on_tool_state_changed(self, running):
        self._update_buttons()
        if not running and self._songs_folder:
            # the scan has genuinely finished, so reloading reads a settled
            # tree. Covers the case where the dialog was closed mid-run and its
            # own on_close reload was deliberately skipped for that reason.
            self._load_library(self._songs_folder)

    def _update_buttons(self):
        checked    = [s for s in self._songs if s.checked]
        n          = len(checked)
        has_vid    = [s for s in checked if s.has_video]
        # _tool_running blocks both: a Library Tools scan and a download run
        # are two independent mutation paths over one library
        can_start  = n > 0 and not self._running and not self._tool_running
        can_resync = (len(has_vid) > 0 and not self._running
                      and not self._tool_running and self._sync_ready)

        self._start_btn.configure(
            text=f'▶  Search & Download ({n})' if n else '▶  Search & Download',
            state='normal' if can_start else 'disabled')
        self._resync_btn.configure(
            state='normal' if can_resync else 'disabled')
        self._stop_btn.configure(
            state='normal' if self._running else 'disabled')

        all_vis_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        self._sel_btn.configure(
            text='Deselect all' if all_vis_on else 'Select all')

    _BIG_BATCH = 25   # confirm before kicking off a run this large

    def _start_download(self):
        if self._running:
            return
        checked = [s for s in self._songs if s.checked]
        if not checked:
            return
        missing  = [s for s in checked if not s.has_video]
        existing = [s for s in checked if s.has_video]

        if missing:
            # usual case: just fill the gaps, leave the done ones alone
            work, replace = missing, False
            if existing:
                for s in existing:
                    s.status = '✔  Has video'
                    s.stag   = 'dim'
                    self._update_row(s)
                self._status_lbl.configure(
                    text=f'{len(existing)} already have video, skipping; '
                         f'downloading {len(missing)} missing')
        else:
            # they only picked songs that already have a video, so Start would do
            # nothing. probably means they want them re-fetched, so ask first.
            ne = len(existing)
            if not messagebox.askyesno(
                    'Re-download videos?',
                    f'All {ne} selected song{"s" if ne != 1 else ""} already '
                    f'have a video.\n\nRe-download from YouTube and replace them?'):
                self._status_lbl.configure(
                    text='Nothing to download, selection already has videos')
                return
            work, replace = existing, True

        if not self._confirm_batch(work, 'download'):
            return
        self._launch(work, replace=replace, resync=False)

    def _start_resync(self):
        if self._running:
            return
        work = [s for s in self._songs if s.checked and s.has_video]
        if not work:
            return
        if not self._confirm_batch(work, 'auto-sync'):
            return
        self._launch(work, replace=False, resync=True)

    def _confirm_batch(self, work, verb):
        """Guard against an accidental huge run. Returns False to abort."""
        if len(work) <= self._BIG_BATCH:
            return True
        return messagebox.askyesno(
            'Large batch',
            f'About to {verb} {len(work)} songs. This can take a while'
            + (' and use a lot of bandwidth and disk space' if verb == 'download' else '')
            + '.\n\nContinue?')

    def _launch(self, targets, replace, resync):
        if self._running or not targets:
            return
        quality = quality_format(1080 if self._quality_var.get() == '1080p' else 720)

        self._running = True
        # what "skipped" means differs by run type: a download run skips songs
        # that already have a video, a resync run skips ones the user synced by
        # hand. Set before the worker starts and only read on the main thread.
        self._resync_run = resync
        self._stop_evt.clear()
        self._progress.set(0)
        self._update_buttons()

        for s in targets:
            s.status = '○  Pending'
            s.stag   = 'dim'
            self._update_row(s)

        threading.Thread(
            target=self._dl_thread,
            args=(targets, quality, replace, resync),
            daemon=True).start()

    def _dl_thread(self, targets, quality, replace, resync):
        total = len(targets)
        done = skipped = errors = 0
        # adaptive pacing: pause between songs that hit YouTube, scaled by how
        # the run is going. clean streaks creep the delay down, getting throttled
        # doubles it. skipped songs make no requests at all so they get no pause,
        # which is what makes re-runs over a mostly-done library fast.
        pace = 1.0
        clean_streak = 0
        prev_hit_network = False
        for i, s in enumerate(targets):
            if self._stop_evt.is_set():
                self._queue.put(('stopped', i, total, done, skipped, errors))
                return

            if prev_hit_network and self._stop_evt.wait(
                    random.uniform(SONG_DELAY_MIN, SONG_DELAY_MAX) * pace):
                self._queue.put(('stopped', i, total, done, skipped, errors))
                return

            self._queue.put(('song_start', s, i, total))
            errored = []
            events  = []
            result  = run_song_with_backoff(
                s.folder, s.label, quality,
                self._sync_ready,
                replace=replace, resync=resync,
                errored=errored, stop_evt=self._stop_evt, events=events)

            prev_hit_network = result != 'skipped'
            if events:
                # got pushed back this song, slow right down for a while
                pace = min(pace * 2.0, 6.0)
                clean_streak = 0
            elif prev_hit_network:
                clean_streak += 1
                if clean_streak >= 8:
                    # going fine, ease off the brake a notch
                    pace = max(pace * 0.7, 0.5)
                    clean_streak = 0

            if result == 'stop':
                self._queue.put(('rate_limited', s, i, total))
                return
            if result == 'stopped':
                self._queue.put(('stopped', i, total, done, skipped, errors))
                return

            if result == 'skipped':
                skipped += 1
                self._queue.put(('song_skipped', s, i, total))
            elif errored:
                errors += 1
                self._queue.put(('song_error', s, i, total, errored[-1]))
            else:
                done += 1
                # process_download already probed and stored the resolution in song.ini
                if not resync:
                    stored = get_stored_resolution(s.folder)
                    if stored:
                        s.res = stored
                self._queue.put(('song_done', s, i, total))

        self._queue.put(('finished', total, done, skipped, errors))

    def _run_summary(self, done, skipped, errors):
        parts = []
        if done:
            parts.append(f'{done} re-synced' if self._resync_run else f'{done} downloaded')
        if skipped:
            parts.append(f'{skipped} manually synced, left alone' if self._resync_run
                         else f'{skipped} already had video')
        if errors:  parts.append(f'{errors} failed (details in log)')
        return ', '.join(parts) if parts else 'nothing to do'

    def _stop(self):
        if not self._running:
            return
        self._stop_evt.set()
        # Reflect the press immediately so an impatient user isn't left guessing.
        self._stop_btn.configure(state='disabled')
        self._status_lbl.configure(text='Stopping after current song...')

    def _poll_queue(self):
        try:
            while True:
                self._handle_msg(self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg[0]

        if kind == 'song_start':
            _, s, i, total = msg
            s.status = '⟳  Downloading...'
            s.stag   = 'busy'
            self._update_row(s)
            self._progress.set(i / total)
            self._status_lbl.configure(
                text=f'{i + 1} / {total}   {s.label}')

        elif kind == 'song_done':
            _, s, i, total = msg
            s.status    = '✔  Done'
            s.stag      = 'done'
            s.has_video = True
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'song_skipped':
            _, s, i, total = msg
            s.status    = ('✔  Manually synced' if self._resync_run
                           else '✔  Already had video')
            s.stag      = 'dim'
            s.has_video = True
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'song_error':
            _, s, i, total, detail = msg
            short = (detail or '').strip().splitlines()[0] if detail else ''
            if len(short) > 40:
                short = short[:38] + '...'
            s.status = f'✗  {short}' if short else '✗  Error'
            s.stag   = 'error'
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'rate_limited':
            _, s, i, total = msg
            s.status = '✗  Rate limited'
            s.stag   = 'error'
            self._update_row(s)
            self._running = False
            self._update_buttons()
            self._status_lbl.configure(text='Rate limited, wait and try again')
            messagebox.showwarning(
                'YouTube rate limit',
                'YouTube is asking to confirm you\'re not a bot.\n\n'
                'Your IP is being rate-limited. Wait a little while and '
                'try again. Everything already downloaded is safe.')
            self._flush_pending_update()

        elif kind == 'stopped':
            _, i, total, done, skipped, errors = msg
            self._running = False
            self._update_buttons()
            self._status_lbl.configure(
                text='Stopped. ' + self._run_summary(done, skipped, errors))
            self._apply_filter()
            self._flush_pending_update()

        elif kind == 'finished':
            _, total, done, skipped, errors = msg
            self._running = False
            self._progress.set(1.0)
            self._status_lbl.configure(
                text='Done. ' + self._run_summary(done, skipped, errors))
            self._update_buttons()
            # Re-apply filter so has_video status reflects new downloads
            self._apply_filter()
            self._flush_pending_update()

        elif kind == 'res_update':
            _, s, status_text = msg
            self._update_row(s)
            if not self._running:
                self._status_lbl.configure(text=status_text)

        elif kind == 'csv_refresh':
            self._export_library_csv()

        elif kind == 'app_update_available':
            _, latest, asset, sha = msg
            # don't restart in the middle of a run, wait until it's idle
            if self._running:
                self._pending_update = (latest, asset, sha)
            else:
                self._offer_update(latest, asset, sha)

        elif kind == 'scan_progress':
            self._status_lbl.configure(
                text=f'Scanning your library... {msg[1]} songs')

        elif kind == 'library_scanned':
            self._on_library_scanned(msg[1])

        elif kind == 'ytdlp_updated':
            _, ver = msg
            self._status_lbl.configure(
                text=f'Downloader updated ({ver}), active next launch')

    def destroy(self):
        self._stop_evt.set()
        super().destroy()


def run():
    """Called from VideoDownload.__main__ when running as the frozen exe."""
    # Tell Windows to group this process under its own taskbar identity,
    # not under python.exe - must be called before the window is created.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('BackstageHero.App')
    except Exception:
        pass

    updater._cleanup_old_exe()   # fast, no network, fine to call at startup

    app = App()
    app.update()                 # draw the window before dropping the splash
    try:
        import pyi_splash        # hand off from the bootloader splash to the window
        pyi_splash.close()
    except Exception:
        pass
    app.mainloop()


if __name__ == '__main__':
    run()
