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
    SYNC_MANUAL, dump_video, get_rejected_sources, classify_candidate_title,
    configure_cookies,
    next_resume_at, get_active_schedule, record_throttle_episode,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
import updater
import resolver_client
import video_repair
import chart_rename
import metadata_enrichment
import dedupe_report
import static_art
import library_enrichment

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


# background_state.json holds structured, frequently-rewritten background-mode
# run state -- kept separate from settings.json (flat, rarely-written UI
# preferences loaded once at startup). Schema (a plain dict; no dataclass, since
# no controller consumes this yet and every helper below already speaks plain
# dicts):
#   phase             'downloading' | 'library_tools' | 'done'
#   resume_at         unix timestamp (float/int) the next retry is scheduled
#                     for, or None if no backoff is pending
#   throttle_count    int, consecutive long-backoff throttles this run
#   songs_folder      str, the library path this run is targeting
#   quality           str, the quality setting the run started with
#   replace           bool
#   resync            bool
#   remaining_folders list[str], folders not yet processed
#   tool_dry_run      dict[str, bool], each Library Tool's key (e.g.
#                     'fix_chart_names') -> its Dry run checkbox state at the
#                     moment background mode started, so a later controller
#                     with no live dialog to read from still knows each tool's
#                     preference.
_BACKGROUND_STATE_FILE = os.path.join(updater.data_dir(), 'background_state.json')


def _load_background_state():
    try:
        with open(_BACKGROUND_STATE_FILE, encoding='utf-8') as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_background_state(state):
    """Atomic write (temp file + os.replace), unlike _save_settings's plain
    open().write(). background_state.json is rewritten far more often, over a
    run that may span days and survive app/machine restarts -- a crash or
    forced-close mid-write must leave the previous valid state file intact,
    never a half-written one a resume would trust blindly."""
    try:
        tmp_path = _BACKGROUND_STATE_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            _json.dump(state, f)
        os.replace(tmp_path, _BACKGROUND_STATE_FILE)
    except Exception:
        pass


def _clear_background_state():
    """Removes background_state.json entirely (rather than resetting it to an
    empty dict on disk) once a run reaches 'done' -- so a startup resume check
    (_load_background_state() returning {}) can't be told apart from "never
    ran", which is exactly the behavior a finished run should have."""
    try:
        os.remove(_BACKGROUND_STATE_FILE)
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


def _video_kind(folder, title=None):
    """'lyric', 'gameplay', 'official'... for the attached video, or ''.

    Derived from the stored title rather than stored separately, so there is
    one fact on disk and no chance of the two disagreeing. Blank for anything
    downloaded before titles were recorded -- those can only be identified by
    re-querying YouTube, which is a deliberate opt-in, not something a library
    scan should do 376 times unprompted.

    Pass `title` if the caller already read it, to avoid a second parse of
    the same song.ini for the same key.
    """
    if title is None:
        title = _read_song_value(folder, 'backstagehero_video_title')
    if not title:
        return ''
    kind = classify_candidate_title(title)
    return '' if kind == 'unknown' else kind


def _read_song_value(folder, key, section=None):
    """One [song] value from a song.ini, or '' -- for building the CSV.

    Thin wrapper over VideoDownload's reader rather than a second parser, so
    the export can never disagree with what the app itself reads. Pass
    `section` (from VideoDownload._read_ini_section) to reuse an
    already-parsed file instead of re-reading it.
    """
    try:
        if section is not None:
            return section.get(key.lower()) or ''
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
        # Reuses VideoDownload's shared ini reader instead of a second,
        # hand-rolled line scan -- one canonical way to read video_start_time
        # across the whole app.
        val = _read_song_value(self._song.folder, 'video_start_time')
        if val.lstrip('-').isdigit():
            return int(val)
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

# Dependency order for "Run all tools": each step's OUTPUT is another step's
# INPUT, so running out of order means a tool sees stale state instead of the
# best data the others can give it.
#
#   1. migrate_review_folders -- gets any old misplaced review folders out of
#      the library root before anything below walks the tree.
#   2. fix_chart_names        -- dedupe_report.is_keeper_eligible() requires
#      chart_rename_status == 'confirmed_ok'; nothing scanned before this
#      runs is eligible to be picked as a dedupe keeper.
#   3. repair_videos / find_static_art -- finalize which videos are actually
#      present and playable (bad-codec removal, static-art-to-image
#      conversion) before dedupe scores has_video on them.
#   4. enrich_metadata        -- fills the year/genre/charter/album fields
#      dedupe's metadata_completeness scoring reads.
#   5. find_duplicates        -- last, so its keeper-selection scoring sees
#      the finished state of everything above instead of a half-repaired one.
_RUN_ALL_ORDER = (
    'migrate_review_folders', 'fix_chart_names', 'repair_videos',
    'find_static_art', 'enrich_metadata', 'find_duplicates',
)


def _run_library_tool(songs_folder, key, dry_run):
    """Dispatch one tool's library-wide scan by key. Raises on failure --
    callers each decide how to handle that.

    Module-level (not a LibraryToolsDialog method) so a background-mode
    controller can call this directly, with no dialog window ever open --
    LibraryToolsDialog._run_tool_scan is now a thin wrapper around this that
    just supplies self._songs_folder."""
    if key == 'repair_videos':
        return video_repair.scan_and_repair_video_library(songs_folder, dry_run=dry_run)
    elif key == 'fix_chart_names':
        return chart_rename.scan_and_fix_chart_library(songs_folder, dry_run=dry_run)
    elif key == 'enrich_metadata':
        return metadata_enrichment.enrich_song_ini_metadata_library(songs_folder, dry_run=dry_run)
    elif key == 'find_duplicates':
        return dedupe_report.generate_dedupe_report(songs_folder, dry_run=dry_run)
    elif key == 'find_static_art':
        return static_art.scan_and_convert_static_art_library(songs_folder, dry_run=dry_run)
    elif key == 'migrate_review_folders':
        return library_common.migrate_legacy_review_folders(songs_folder, dry_run=dry_run)
    return {}


def _format_tool_summary(key, counts, dry_run):
    """Module-level twin of _run_library_tool -- see that function's
    docstring. LibraryToolsDialog._format_summary is now a thin wrapper."""
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


class LibraryToolsDialog(ctk.CTkToplevel):
    """Library-wide hygiene scans: video repair, chart-name fixes, metadata
    enrichment, duplicate detection.

    Each runs the whole library in a background thread so the window stays
    responsive; the summary shown when it finishes is built from the scan's
    own returned counts dict, not parsed console output. Only one tool runs
    at a time -- fix_chart_names and find_duplicates both read/write the
    same per-folder chart_rename_status, so overlapping runs could race.
    """

    def __init__(self, parent, songs_folder, on_close=None, on_run_state=None,
                 dry_run_prefs=None, on_dry_run_change=None):
        super().__init__(parent)
        self._songs_folder = songs_folder
        self._on_close = on_close
        # Told when a tool starts and stops, so the main window can stay
        # locked for the worker's real lifetime rather than the dialog's --
        # closing this window does not stop the thread (there is no Stop
        # button to offer, and killing a scan mid-rename would be worse than
        # letting it finish).
        self._on_run_state = on_run_state
        # Per-tool dry-run checkbox state, persisted by the caller (App) so
        # an unattended background run started without this dialog open can
        # still know what the user last chose per tool. Both optional and
        # additive -- omitting them keeps every tool defaulting to dry-run
        # True with no persistence, exactly as before this was added.
        self._dry_run_prefs = dry_run_prefs if dry_run_prefs is not None else {}
        self._on_dry_run_change = on_dry_run_change
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
        self.grid_rowconfigure(2, weight=1)
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

        run_all_card = ctk.CTkFrame(self, fg_color='#1c2e4a', corner_radius=10,
                                    border_width=1, border_color=_BLUE)
        run_all_card.grid(row=1, column=0, sticky='ew', padx=12, pady=(12, 0))
        rtop = ctk.CTkFrame(run_all_card, fg_color='transparent')
        rtop.pack(fill='x', padx=16, pady=(12, 2))
        ctk.CTkLabel(rtop, text='Run all tools', font=ctk.CTkFont(size=13, weight='bold'),
                     text_color=_TEXT).pack(side='left')
        ctk.CTkLabel(
            run_all_card,
            text="Runs every tool below once, in the order each one benefits most "
                 "from the last: old review folders moved out, chart names fixed, "
                 "videos repaired and static-art converted, metadata filled in, then "
                 "duplicates found last so its scoring sees the finished result of "
                 "everything before it. Uses each tool's own Dry run checkbox below.",
            font=ctk.CTkFont(size=10), text_color=_SUBTEXT, wraplength=480,
            justify='left', anchor='w').pack(fill='x', padx=16, anchor='w')
        rrow = ctk.CTkFrame(run_all_card, fg_color='transparent')
        rrow.pack(fill='x', padx=16, pady=(10, 12))
        self._run_all_status_lbl = ctk.CTkLabel(
            rrow, text='Ready', font=ctk.CTkFont(size=10),
            text_color=_SUBTEXT, anchor='w')
        self._run_all_status_lbl.pack(side='left', fill='x', expand=True)
        self._run_all_btn = ctk.CTkButton(
            rrow, text='Run all', width=90, height=28,
            font=ctk.CTkFont(size=11, weight='bold'),
            fg_color=_BLUE, hover_color='#7aaef8', text_color='#11111b',
            command=self._run_all)
        self._run_all_btn.pack(side='right')
        # Shares the same disable/enable cycle as the per-tool buttons below
        # (_run_tool/_finish iterate self._run_buttons.values()), so a single
        # Run-all pass locks every button, including its own, exactly like a
        # single tool run does.
        self._run_buttons['__run_all__'] = self._run_all_btn

        # Scrollable so the window can be resized smaller (or run at a
        # higher OS text-scaling setting) without any card's content or the
        # Close button getting clipped off-screen.
        scroll = ctk.CTkScrollableFrame(self, fg_color='transparent')
        scroll.grid(row=2, column=0, sticky='nsew', padx=8, pady=(8, 0))

        for key, label, desc in _LIBRARY_TOOLS:
            card = ctk.CTkFrame(scroll, fg_color='#252540', corner_radius=10)
            card.pack(fill='x', padx=12, pady=(10, 0))

            top = ctk.CTkFrame(card, fg_color='transparent')
            top.pack(fill='x', padx=16, pady=(12, 2))
            ctk.CTkLabel(top, text=label, font=ctk.CTkFont(size=13, weight='bold'),
                         text_color=_TEXT).pack(side='left')

            dry_var = tk.BooleanVar(value=self._dry_run_prefs.get(key, True))
            self._dry_run_vars[key] = dry_var
            ctk.CTkCheckBox(top, text='Dry run', variable=dry_var,
                            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
                            checkbox_width=16, checkbox_height=16,
                            checkmark_color=_BG, fg_color=_BLUE,
                            hover_color='#7aaef8',
                            command=lambda k=key: self._on_dry_toggle(k)
                            ).pack(side='right')

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
                      command=self._close).grid(row=3, column=0, pady=18)

    def _on_dry_toggle(self, key):
        """Checkbox command for a tool's Dry run box. No-op when the dialog
        was constructed without on_dry_run_change (e.g. existing tests that
        build LibraryToolsDialog with only the original params)."""
        if self._on_dry_run_change is not None:
            self._on_dry_run_change(key, self._dry_run_vars[key].get())

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

    def _run_tool_scan(self, key, dry_run):
        """Dispatch one tool's library-wide scan by key. Raises on failure --
        callers (_worker for a single tool, _run_all_worker for the combined
        run) each decide how to handle that. Thin wrapper around the
        module-level _run_library_tool (see its docstring for why this is
        split out) supplying this dialog's own songs folder."""
        return _run_library_tool(self._songs_folder, key, dry_run)

    def _worker(self, key, dry_run):
        try:
            counts = self._run_tool_scan(key, dry_run)
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

    def _run_all(self):
        if self._running_key is not None:
            return
        self._running_key = 'run_all'
        # "previewing" only if every step in the sequence is dry-run; a
        # single live step means real files change, so the close-confirm
        # wording should say so rather than undersell it.
        self._dry_run_of_current = all(
            self._dry_run_vars[k].get() for k in _RUN_ALL_ORDER)
        self._notify_run_state(True)
        for btn in self._run_buttons.values():
            btn.configure(state='disabled')
        self._run_all_status_lbl.configure(text='Running...', text_color=_BLUE)
        threading.Thread(target=self._run_all_worker, daemon=True).start()

    def _run_all_worker(self):
        """Runs every tool in _RUN_ALL_ORDER, each with its own current Dry
        run checkbox. One tool's failure is logged and shown on its own card
        -- never abort the rest of the sequence over it, same "don't lose the
        batch to one bad step" rule the other library scans already follow."""
        ok = 0
        failed = 0
        for key in _RUN_ALL_ORDER:
            dry_run = self._dry_run_vars[key].get()
            try:
                self.after(0, lambda k=key: self._status_labels[k].configure(
                    text='Running...', text_color=_BLUE))
            except Exception:
                pass
            try:
                counts = self._run_tool_scan(key, dry_run)
                text = self._format_summary(key, counts, dry_run)
                color = _GREEN
                ok += 1
            except Exception as e:
                log.exception('Library tool %s failed during Run all', key)
                text = f'Error: {e}'
                color = _RED
                failed += 1
            try:
                self.after(0, lambda k=key, t=text, c=color:
                           self._status_labels[k].configure(text=t, text_color=c))
            except Exception:
                pass

        summary = f'{ok}/{len(_RUN_ALL_ORDER)} tools completed'
        if failed:
            summary += f', {failed} failed'
        self._notify_run_state(False)
        try:
            self.after(0, lambda: self._finish_run_all(
                summary, _RED if failed else _GREEN))
        except Exception:
            pass

    def _finish_run_all(self, text, color):
        self._running_key = None
        if not self.winfo_exists():
            return
        for btn in self._run_buttons.values():
            btn.configure(state='normal')
        self._run_all_status_lbl.configure(text=text, text_color=color)

    @staticmethod
    def _format_summary(key, counts, dry_run):
        """Thin wrapper around the module-level _format_tool_summary (see its
        docstring for why this is split out)."""
        return _format_tool_summary(key, counts, dry_run)

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
            if self._running_key == 'run_all':
                tool = 'Run all tools'
            else:
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
        # Is the active run an unattended background run? Set by _launch_background,
        # cleared when the background run ends. Read only as an in-memory "a
        # background run is active" indicator (the durable record is
        # background_state.json). A future GUI toggle (Task 12) reads it too.
        self._background_mode: bool = False
        # Fires _maybe_resume_background() exactly once, on the first
        # _on_library_scanned after this app process starts -- later rescans in
        # the same session (e.g. LibraryToolsDialog's post-run re-scan) must NOT
        # re-trigger a resume check. See _on_library_scanned/_maybe_resume_background.
        self._pending_background_resume_check: bool = True
        # A Library Tools worker can outlive its dialog, so this tracks the
        # THREAD, not the window. Without it, closing that dialog mid-scan
        # freed the user to start a download into the same folders a rename
        # sweep was still working through.
        self._tool_running: bool = False
        # Guards against a second _maybe_start_enrichment() (fired after a
        # rescan while a prior enrichment thread is still running) from
        # spawning a concurrent enrich_library() call -- two concurrent
        # calls each build their own CachedChorusClient against the same
        # default cache file, racing os.replace() on the same .tmp path
        # (WinError 32) and silently clobbering each other's cached entries.
        # A real Lock, unlike _tool_running, because it's set on the main
        # thread and cleared on the background thread.
        self._enrichment_lock = threading.Lock()
        self._enrichment_running: bool = False
        self._polling     : bool = False
        self._stop_evt    = threading.Event()
        self._queue       : queue.Queue = queue.Queue()
        self._songs_folder: str  = ''
        self._pending_update = None      # (version, asset, sha) deferred during a run
        self._search_after = None        # debounce handle for the search box
        self._settings    = _load_settings()
        resolver_client.set_sharing(self._settings.get('share_matches', True))
        # Push the persisted cookie-support setting into VideoDownload's
        # module state once at startup -- see _on_cookies_toggle/
        # _on_cookie_browser_change for the "changes take effect without a
        # restart" half of this. configure_cookies() is never called with
        # this omitted, but VideoDownload's own module-level defaults
        # (False/None) already match this default, so behavior is identical
        # even if this call were skipped.
        configure_cookies(self._settings.get('use_browser_cookies', False),
                          self._settings.get('cookie_browser', 'chrome'))
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

        # Booklet-data enrichment (instruments/NPS/features/high scores) --
        # runs in a background thread after each scan settles, same
        # threading.Thread pattern as _probe_resolutions/_scan_library, not
        # a subprocess -- no interpreter-path or stdout-piping fragility.
        self._enrich_var = tk.BooleanVar(
            value=self._settings.get('enrich_after_scan', True))
        enrich_cb = ctk.CTkCheckBox(
            foot, text='Enrich after scan', variable=self._enrich_var,
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=lambda: self._persist_setting(
                'enrich_after_scan', bool(self._enrich_var.get())))
        enrich_cb.grid(row=0, column=7, padx=(12, 4), pady=15)

        # Opt-in browser-cookie support for yt-dlp (off by default). Reduces
        # bot-detection frequency per SPEC-background-mode.md; the browser's
        # cookie store is read by yt-dlp itself -- no cookie value is ever
        # handled here, only the browser name string.
        self._cookies_var = tk.BooleanVar(
            value=self._settings.get('use_browser_cookies', False))
        cookies_cb = ctk.CTkCheckBox(
            foot, text='Use browser cookies', variable=self._cookies_var,
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=self._on_cookies_toggle)
        cookies_cb.grid(row=0, column=8, padx=(12, 4), pady=15)

        self._cookie_browser_var = tk.StringVar(
            value=self._settings.get('cookie_browser', 'chrome'))
        ctk.CTkOptionMenu(
            foot, variable=self._cookie_browser_var,
            values=['chrome', 'firefox', 'edge'], width=100, height=34,
            command=self._on_cookie_browser_change,
            font=_font).grid(row=0, column=9, padx=4, pady=15)

        # Unattended background-mode toggle (Task 12). Deliberately not
        # persisted to settings.json -- a multi-day unattended run should
        # require the user to explicitly re-arm it each time, not silently
        # resume from a stale "left it checked" state. Read at Start-click
        # time in _start_download, not on toggle.
        self._background_var = tk.BooleanVar(value=False)
        background_cb = ctk.CTkCheckBox(
            foot, text='Run in background', variable=self._background_var,
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18)
        background_cb.grid(row=0, column=10, padx=(12, 4), pady=15)

        # Progress + status (right side of footer)
        prog_frame = ctk.CTkFrame(foot, fg_color='transparent')
        prog_frame.grid(row=0, column=11, padx=(8, 16), pady=15, sticky='e')

        self._background_badge_lbl = ctk.CTkLabel(
            prog_frame, text='', font=ctk.CTkFont(size=10, weight='bold'),
            text_color=_BLUE, anchor='e', width=190)
        self._background_badge_lbl.pack()

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

    def _tool_dry_run_prefs(self):
        """Per-tool dry-run checkbox state, persisted across LibraryToolsDialog
        sessions (and readable without one being open, e.g. by
        _launch_background). Defaults every tool to dry-run True when never
        explicitly set -- matches this codebase's existing safety
        convention."""
        saved = self._settings.get('library_tool_dry_run', {})
        return {key: saved.get(key, True) for key, _, _ in _LIBRARY_TOOLS}

    def _on_tool_dry_run_change(self, key, value):
        prefs = dict(self._settings.get('library_tool_dry_run', {}))
        prefs[key] = value
        self._persist_setting('library_tool_dry_run', prefs)

    def _on_share_toggle(self):
        on = bool(self._share_var.get())
        resolver_client.set_sharing(on)
        self._persist_setting('share_matches', on)

    def _on_cookies_toggle(self):
        self._persist_setting('use_browser_cookies', bool(self._cookies_var.get()))
        self._push_cookie_config()

    def _on_cookie_browser_change(self, *_):
        self._persist_setting('cookie_browser', self._cookie_browser_var.get())
        self._push_cookie_config()

    def _push_cookie_config(self):
        """Re-push the current toggle/dropdown state into VideoDownload so a
        change takes effect on the next download without an app restart."""
        configure_cookies(bool(self._cookies_var.get()),
                          self._cookie_browser_var.get())

    def _maybe_start_enrichment(self):
        """Runs library_enrichment.enrich_library() in a background thread
        after a scan settles, if the user has it enabled. Deliberately
        touches no Tkinter widget from that thread -- unlike the download/
        probe flows, it has nothing the user needs to watch live, so there's
        no need to route anything through self._queue and no risk of a
        cross-thread widget access bug. A failure here costs the user an
        optional booklet-data file, never the app itself -- same philosophy
        as _export_library_csv, logged via `log` and otherwise ignored.

        A no-op (skip + log) if an enrichment run is already in flight from
        an earlier scan -- see self._enrichment_lock's comment in __init__."""
        if not self._enrich_var.get() or not self._songs_folder:
            return
        with self._enrichment_lock:
            if self._enrichment_running:
                log.info('Library enrichment already running; skipping')
                return
            self._enrichment_running = True
        threading.Thread(target=self._run_enrichment, daemon=True).start()

    def _run_enrichment(self):
        try:
            library_enrichment.enrich_library(self._songs_folder)
        except Exception as e:
            log.warning('Library enrichment failed: %s', e)
        finally:
            with self._enrichment_lock:
                self._enrichment_running = False

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
        if self._pending_background_resume_check:
            self._pending_background_resume_check = False
            self._maybe_resume_background()
        self._apply_filter()
        n = len(songs)
        unprobed = [s for s in songs if s.has_video and s.res == '...']
        will_probe = bool(unprobed and ffmpegAvailable)
        if will_probe:
            self._status_lbl.configure(
                text=f'{n} songs found, reading resolutions...')
            threading.Thread(target=self._probe_resolutions,
                             args=(unprobed, n), daemon=True).start()
        else:
            self._status_lbl.configure(
                text=f'{n} song{"s" if n != 1 else ""} found')
        self._export_library_csv()
        self._update_buttons()
        if not will_probe:
            # If resolutions need probing, enrichment waits for that to
            # settle instead (triggered from the 'csv_refresh' handler) --
            # otherwise it would run twice per scan, once redundantly.
            self._maybe_start_enrichment()

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
                            'Offset (ms)', 'Offset source', 'Video kind',
                            'Video title', 'Video ID', 'Dumped videos', 'Folder'])
                from VideoDownload import _read_ini_section
                for s in sorted(self._songs, key=lambda x: x.key):
                    artist, title = read_metadata(s.folder)
                    # One parse of song.ini for every field below, instead of
                    # a separate open+parse per column (this used to be 6-7
                    # reads of the same file per song).
                    section = _read_ini_section(s.folder) or {}
                    video_title = section.get('backstagehero_video_title') or ''
                    w.writerow([
                        s.label,
                        artist or '',
                        title or '',
                        _video_status(s),
                        s.res if s.has_video else '',
                        _read_song_value(s.folder, 'video_start_time', section),
                        # the provenance marker, so a spreadsheet sort shows at
                        # a glance which songs were never actually measured
                        _read_song_value(s.folder, 'backstagehero_sync', section),
                        # what KIND of video it is -- sorting on this column is
                        # how you find every lyric video and gameplay capture
                        # in one pass. Fingerprinting cannot tell these apart,
                        # because their audio is identical to the real thing.
                        _video_kind(s.folder, video_title),
                        video_title,
                        _read_song_value(s.folder, 'backstagehero_source', section),
                        ' '.join(sorted(get_rejected_sources(s.folder, section))),
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
                            on_run_state=self._set_tool_running,
                            dry_run_prefs=self._tool_dry_run_prefs(),
                            on_dry_run_change=self._on_tool_dry_run_change)

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

    def _set_background_mode(self, active):
        """Single choke point for self._background_mode -- keeps the footer's
        background-run badge in sync with it everywhere it changes, instead of
        duplicating the badge update at each of the 6 call sites that toggle
        this flag. Guards on the badge existing: _build_ui always creates it
        before any of these call sites can run in the real app, but several
        pre-existing bare-instance tests construct App via object.__new__
        without a full _build_ui and don't stub this particular label. Looked
        up via __dict__ rather than getattr(..., default) -- ctk.CTk/tk.Tk's
        own __getattr__ recurses into itself (chasing a likewise-missing
        self.tk) on such an uninitialized instance, which getattr's default
        does not protect against since it's a RecursionError, not
        AttributeError."""
        self._background_mode = active
        badge = self.__dict__.get('_background_badge_lbl')
        if badge is not None:
            badge.configure(text='● Background' if active else '')

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
        if self._background_var.get():
            self._launch_background(work, replace=replace, resync=False)
        else:
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

    def _launch_background(self, targets, replace, resync):
        """Start an unattended background run: the same per-song download loop
        as _launch, but a YouTube throttle triggers a long, escalating backoff
        and an automatic resume instead of ending the run, and true completion
        hands off to a single Library Tools pass.

        Mirrors _launch (validation, _running/_stop_evt/progress setup, pending
        row marking) but additionally captures the initial background_state.json
        snapshot -- phase, target list, and each Library Tool's dry-run
        preference -- before the worker starts, so the state survives a restart
        from the very first moment. Wired to the "Run in background" checkbox
        in the footer via _start_download.
        """
        if self._running or not targets:
            return
        quality = quality_format(1080 if self._quality_var.get() == '1080p' else 720)

        self._running = True
        self._set_background_mode(True)
        self._resync_run = resync
        self._stop_evt.clear()
        self._progress.set(0)
        self._update_buttons()

        for s in targets:
            s.status = '○  Pending'
            s.stag   = 'dim'
            self._update_row(s)

        # Persist the run's identity up front. tool_dry_run reads each tool's
        # persisted dry-run preference (_tool_dry_run_prefs), defaulting to
        # dry-run True for any tool the user never explicitly toggled in the
        # Library Tools dialog -- matches this codebase's
        # test_dry_run_defaults_on_for_every_tool safety convention. The
        # download loop rewrites only the volatile fields (resume_at,
        # throttle_count, remaining_folders, phase) from here on.
        _save_background_state({
            'phase': 'downloading',
            'resume_at': None,
            'throttle_count': 0,
            'songs_folder': self._songs_folder,
            'quality': self._quality_var.get(),
            'replace': replace,
            'resync': resync,
            'remaining_folders': [s.folder for s in targets],
            'tool_dry_run': self._tool_dry_run_prefs(),
        })
        log.info('Background mode started: %d target(s), replace=%s, resync=%s',
                 len(targets), replace, resync)

        threading.Thread(
            target=self._dl_thread,
            args=(targets, quality, replace, resync),
            kwargs={'background_mode': True},
            daemon=True).start()

    def _dl_thread(self, targets, quality, replace, resync, background_mode=False):
        total = len(targets)
        done = skipped = errors = 0
        # adaptive pacing: pause between songs that hit YouTube, scaled by how
        # the run is going. clean streaks creep the delay down, getting throttled
        # doubles it. skipped songs make no requests at all so they get no pause,
        # which is what makes re-runs over a mostly-done library fast.
        pace = 1.0
        clean_streak = 0
        prev_hit_network = False
        # Background-mode-only long-backoff bookkeeping. Untouched (and never
        # read) on the default non-background path, whose behavior must stay
        # byte-identical. throttle_count is the escalation depth WITHIN the
        # current throttle episode (0-indexed, feeds next_resume_at);
        # episode_started_at marks the first 'stop' of the current episode, or
        # None when not mid-episode -- record_throttle_episode needs that start
        # time when the episode finally resolves.
        throttle_count = 0
        episode_started_at = None
        # A while loop (not `for i, s in enumerate`) so background mode can retry
        # the SAME song after a long backoff (a bare `continue` without advancing
        # i) rather than skipping past the song that got throttled.
        i = 0
        while i < total:
            s = targets[i]
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
                if not background_mode:
                    # Default (non-background) behavior, unchanged: the short
                    # per-song retry is exhausted, so end the run and warn.
                    self._queue.put(('rate_limited', s, i, total))
                    return
                # Background mode: long escalating backoff instead of giving up.
                # Extracted to keep this loop readable; the helper does the
                # persist-then-wait and returns updated escalation bookkeeping.
                throttle_count, episode_started_at, stopped = \
                    self._handle_background_throttle(
                        s, i, total, targets, throttle_count,
                        episode_started_at, done, skipped, errors)
                if stopped:
                    # Manual Stop fired mid-wait; background_stopped already
                    # posted. End the run, leaving background_state.json intact.
                    return
                # Wait elapsed without cancellation: retry the SAME song. Do not
                # advance i and do not record the episode yet -- the episode is
                # only "resolved" once the song actually succeeds below.
                continue
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

            # Reaching any non-throttle outcome for a song that had been
            # throttled means the block lifted: the episode is resolved. The
            # helper records it (and resets the escalation) when there was one
            # in progress, and is a no-op otherwise.
            if background_mode:
                episode_started_at, throttle_count = \
                    self._resolve_background_episode(
                        episode_started_at, throttle_count)

            i += 1

        if not background_mode:
            self._queue.put(('finished', total, done, skipped, errors))
            return

        # Background mode, download phase truly complete: every target reached a
        # non-throttle outcome and no resume_at is pending (a throttle would have
        # `continue`d, a Stop would have returned). Hand off to a single Library
        # Tools pass, then mark the whole run done.
        self._run_background_library_tools(done, skipped, errors)

    def _handle_background_throttle(self, s, i, total, targets, throttle_count,
                                    episode_started_at, done, skipped, errors):
        """Background-mode-only: called when a song returns 'stop'. Computes the
        long-backoff resume_at, persists state (before waiting -- a crash during
        the wait must not lose resume_at or which songs are still to do), posts
        the background_throttled queue message, then waits cancellably.

        Returns (throttle_count, episode_started_at, stopped). The caller must
        `return` from _dl_thread if stopped is True (a manual Stop fired mid-wait
        -- background_stopped was already posted), otherwise `continue` the outer
        while loop WITHOUT advancing i, so the same song is retried."""
        now = time.time()
        if episode_started_at is None:
            episode_started_at = now
        resume_at = next_resume_at(
            throttle_count, now, schedule=get_active_schedule())
        throttle_count += 1
        # Persist BEFORE waiting (spec: a crash during the wait must not lose
        # resume_at or which songs are still to do). Preserve the launch-captured
        # identity (songs_folder/quality/replace/resync/tool_dry_run); only the
        # volatile fields change here.
        state = _load_background_state()
        state.update({
            'phase': 'downloading',
            'resume_at': resume_at,
            'throttle_count': throttle_count,
            'remaining_folders': [t.folder for t in targets[i:]],
        })
        _save_background_state(state)
        self._queue.put(('background_throttled', s, i, total, resume_at))
        log.info('Background mode: throttled on %s; resuming at unix %s '
                 '(escalation step %d)', s.label, resume_at, throttle_count - 1)
        # Cancellable wait -- a manual Stop must still work mid-backoff.
        if self._stop_evt.wait(max(0, resume_at - now)):
            # Stopped during the long wait. End the background run cleanly but
            # LEAVE background_state.json in place: it holds a valid resume_at
            # and remaining_folders, so a deliberate Stop is intentionally
            # indistinguishable from an interrupted run for a hypothetical
            # future resume-on-launch (Task 13) -- both are simply "an
            # unfinished background run". Only reaching 'done' clears the state.
            log.info('Background mode: stopped by user during backoff wait')
            self._queue.put(('background_stopped', i, total,
                             done, skipped, errors))
            return throttle_count, episode_started_at, True
        return throttle_count, episode_started_at, False

    def _resolve_background_episode(self, episode_started_at, throttle_count):
        """Background-mode-only: called after any non-throttle song outcome. If a
        throttle episode was in progress (episode_started_at is not None), the
        block has lifted -- record it (triggering the adaptive schedule recompute
        inside record_throttle_episode) and reset the escalation bookkeeping.
        Safe to call unconditionally when background_mode is True: a no-op
        (returns the inputs unchanged) if there was nothing to resolve.

        Returns (episode_started_at, throttle_count), always (None, 0) when an
        episode actually resolved. escalation_steps_used is the 0-indexed step
        the block finally cleared on (throttle_count - 1)."""
        if episode_started_at is None:
            return episode_started_at, throttle_count
        # Deliberately NOT schedule=get_active_schedule() -- see
        # maybe_recompute_schedule's docstring. Passing the already-adapted
        # schedule back in here would compound every recompute cycle instead
        # of independently re-deriving from the fixed default (the
        # /review-found Critical bug: a stable signal still collapsed the
        # schedule to the crash-prevention floor within ~7 cycles). This must
        # always use record_throttle_episode's own LONG_BACKOFF_SECONDS
        # default. (next_resume_at's schedule=get_active_schedule() in
        # _handle_background_throttle above is correct and different --
        # waiting must use the live/adapted schedule; only the recompute-
        # feeding call must not.)
        record_throttle_episode(
            episode_started_at, time.time(), throttle_count - 1)
        log.info('Background mode: throttle episode resolved after %d '
                 'escalation step(s)', throttle_count - 1)
        return None, 0

    def _run_background_library_tools(self, done, skipped, errors):
        """Background-mode hand-off after downloads complete: run one Library
        Tools "Run all" pass in _RUN_ALL_ORDER, then mark the run 'done' and
        clear the persisted state.

        Reads each tool's dry-run preference from the background_state.json
        snapshot captured at launch (defaulting to True/dry-run when a tool has
        no captured preference -- matching test_dry_run_defaults_on_for_every_tool
        -- so a run is never silently forced live), rather than from a live
        LibraryToolsDialog that may not be open during an unattended run.
        Mirrors _run_all_worker's "one tool's failure is logged, never aborts
        the batch" rule."""
        state = _load_background_state()
        state.update({'phase': 'library_tools', 'resume_at': None})
        _save_background_state(state)
        tool_dry_run = state.get('tool_dry_run') or {}
        songs_folder = state.get('songs_folder') or self._songs_folder
        self._queue.put(('background_library_tools', len(_RUN_ALL_ORDER)))
        log.info('Background mode: downloads complete (%d done, %d skipped, %d '
                 'error(s)); starting Library Tools pass', done, skipped, errors)

        tools_ok = 0
        for key in _RUN_ALL_ORDER:
            dry_run = tool_dry_run.get(key, True)
            try:
                counts = _run_library_tool(songs_folder, key, dry_run)
                log.info('Background Library Tools: %s -> %s', key,
                         _format_tool_summary(key, counts, dry_run))
                tools_ok += 1
            except Exception:
                # Don't lose the rest of the batch to one bad step.
                log.exception('Background Library Tools: %s failed', key)

        _clear_background_state()
        log.info('Background mode: run complete (%d/%d tools ok)',
                 tools_ok, len(_RUN_ALL_ORDER))
        self._queue.put(('background_done', done, skipped, errors, tools_ok))

    def _maybe_resume_background(self):
        """Dispatcher for Task 13's auto-resume-on-launch. Called exactly once
        per app session, from _on_library_scanned's one-shot gate, after the
        very first post-startup library scan lands.

        Fully automatic -- no confirmation dialog -- per SPEC-background-mode.md's
        success criteria ("come back days later, and find the app picked back
        up on its own"). A songs_folder mismatch (the persisted run targeted a
        different library than the one just scanned) means do nothing: never
        force-switch or resume against the wrong library.
        """
        state = _load_background_state()
        if not state:
            # No persisted state at all is the overwhelmingly common case --
            # it's true on every normal launch that never had a background
            # run, including every app's very first-ever launch. Logging it
            # every single time would just be routine noise with no signal in
            # it, so this one branch stays silent; the two branches below
            # (a real songs_folder mismatch, or a state file that exists but
            # has nothing left to resume) are the ones worth a log line,
            # since those are the "something happened but nothing visibly
            # resumed" cases a user would actually go looking for in log.txt.
            return
        persisted_folder = state.get('songs_folder')
        # Windows paths are case- and trailing-slash-insensitive; normalize
        # both sides before comparing so a persisted 'C:/Songs' still matches
        # a freshly-loaded 'c:/songs/' instead of silently failing to resume.
        if (persisted_folder is None or
                os.path.normcase(os.path.normpath(persisted_folder)) !=
                os.path.normcase(os.path.normpath(self._songs_folder))):
            log.info('Background mode: persisted run was for %r, current '
                     'library is %r -- not resuming',
                     persisted_folder, self._songs_folder)
            return
        phase = state.get('phase')
        if phase == 'library_tools':
            self._resume_background_library_tools(state)
        elif phase == 'downloading':
            self._resume_background_downloading(state)
        else:
            # phase == 'done', or any other/missing value: nothing to resume.
            log.info('Background mode: persisted state has phase=%r, '
                     'nothing to resume', phase)

    def _resume_background_library_tools(self, state):
        """The download phase already finished before the app closed/crashed;
        only the Library Tools pass was interrupted. done/skipped/errors from
        that finished download phase were never persisted (only Library Tools
        progress matters for a resume at this phase) -- the 0s passed below
        only affect this call's own log line, not correctness."""
        self._running = True
        self._set_background_mode(True)
        self._update_buttons()
        self._status_lbl.configure(text='Resuming background run: finishing Library Tools...')
        log.info('Background mode: resuming at startup mid-Library-Tools')
        threading.Thread(target=self._run_background_library_tools,
                          args=(0, 0, 0), daemon=True).start()

    def _resume_background_downloading(self, state):
        """The download phase was still in progress (or mid-backoff-wait) when
        the app last closed. Re-derives the actual remaining work from the
        FRESH scan rather than trusting the persisted remaining_folders list
        blindly -- the library may have changed while the app was closed (a
        video added manually, a folder removed)."""
        remaining = set(state.get('remaining_folders') or [])
        targets = [s for s in self._songs if s.folder in remaining and not s.has_video]
        if not targets:
            # Everything that was pending now has video (or the folders are
            # gone) -- nothing left to download, go straight to the Library
            # Tools hand-off.
            self._resume_background_library_tools(state)
            return

        resume_at = state.get('resume_at')
        replace = bool(state.get('replace'))
        resync = bool(state.get('resync'))

        self._running = True
        self._set_background_mode(True)
        self._stop_evt.clear()
        self._update_buttons()
        self._status_lbl.configure(text='Resuming background run...')
        log.info('Background mode: resuming at startup, %d song(s) still pending', len(targets))

        def _worker():
            if resume_at is not None:
                wait_for = max(0, resume_at - time.time())
                if wait_for > 0:
                    log.info('Background mode: waiting %.0fs for the persisted '
                             'resume_at before retrying', wait_for)
                    if self._stop_evt.wait(wait_for):
                        self.after(0, self._on_resume_wait_stopped)
                        return
            # Deliberate simplification: once the persisted resume_at has
            # elapsed (or there was none to wait on), the block is presumed
            # lifted and the resumed run starts a FRESH escalation at
            # throttle_count=0 rather than trying to thread Task 11's
            # in-memory throttle_count/episode_started_at across a process
            # restart. If YouTube is still actually blocking, the very next
            # song will produce a new 'stop' and re-escalate normally through
            # the existing Task 11 logic -- a reasonable, simple tradeoff, not
            # a bug to fix.
            self.after(0, lambda: self._finish_resume_and_launch(targets, replace, resync))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_resume_and_launch(self, targets, replace, resync):
        """Bounced onto the main thread via self.after(0, ...) so there's no
        cross-thread race with _launch_background's own state (Tkinter
        callbacks are single-threaded).

        _resume_background_downloading deliberately set self._running = True
        up front (so Stop works during the pre-resume wait, matching Task 11's
        cancellable-wait pattern). _launch_background's own guard
        (`if self._running or not targets: return`) would silently no-op if
        called while that's still True -- reset it first so the guard re-arms
        cleanly. _launch_background then re-sets self._running = True itself
        and saves a fresh background_state.json snapshot (new
        remaining_folders, throttle_count reset to 0, resume_at reset to None)
        exactly as it does for a fresh Start-click."""
        self._running = False
        self._launch_background(targets, replace, resync)

    def _on_resume_wait_stopped(self):
        """A manual Stop pressed during the pre-resume wait. background_state.json
        is deliberately left in place here (same policy as Task 11's mid-backoff
        Stop) -- a manual Stop is indistinguishable from an interrupted run, so
        a future resume attempt can still pick it up next launch."""
        self._running = False
        self._set_background_mode(False)
        self._update_buttons()
        self._status_lbl.configure(text='Background run stopped before resuming.')

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

        elif kind == 'background_throttled':
            # Background mode hit a throttle and is waiting out a long backoff
            # rather than ending the run. Keep _running True -- the run is still
            # alive, just paused until resume_at.
            _, s, i, total, resume_at = msg
            when = time.strftime('%H:%M', time.localtime(resume_at))
            s.status = f'⏳  Throttled, resuming {when}'
            s.stag   = 'busy'
            self._update_row(s)
            self._status_lbl.configure(
                text=f'YouTube throttled. Backing off, resuming at {when} '
                     '(background mode keeps retrying)')

        elif kind == 'background_library_tools':
            _, n_tools = msg
            self._status_lbl.configure(
                text=f'Downloads complete. Running {n_tools} Library Tools...')

        elif kind == 'background_done':
            _, done, skipped, errors, tools_ok = msg
            self._running = False
            self._set_background_mode(False)
            self._progress.set(1.0)
            self._status_lbl.configure(
                text='Background run complete. '
                     + self._run_summary(done, skipped, errors)
                     + f'; {tools_ok}/{len(_RUN_ALL_ORDER)} Library Tools ran')
            self._update_buttons()
            self._apply_filter()
            self._flush_pending_update()

        elif kind == 'background_stopped':
            _, i, total, done, skipped, errors = msg
            self._running = False
            self._set_background_mode(False)
            self._update_buttons()
            self._status_lbl.configure(
                text='Background run stopped. '
                     + self._run_summary(done, skipped, errors))
            self._apply_filter()
            self._flush_pending_update()

        elif kind == 'res_update':
            _, s, status_text = msg
            self._update_row(s)
            if not self._running:
                self._status_lbl.configure(text=status_text)

        elif kind == 'csv_refresh':
            self._export_library_csv()
            self._maybe_start_enrichment()

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
