# BackstageHero GUI.
# All download/sync logic lives in VideoDownload.py; this file owns the window.

import os
import sys
import glob
import re
import subprocess
import threading
import queue
import time
from dataclasses import dataclass, field

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from VideoDownload import (
    read_metadata, build_query, run_song_with_backoff,
    quality_format, get_stored_resolution, set_ini_values,
    ffmpegAvailable, ffplayPath, audiosync, __version__,
    DEFAULT_START_TIME, get_stored_source, NO_WINDOW,
)
import updater
import resolver_client

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('blue')


def _asset_path(name):
    """Resolve a bundled asset path - works both frozen (PyInstaller) and from source."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'assets', name)

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


def _validate_folder(path):
    """Returns (ok: bool, message: str)."""
    if not path or not os.path.isdir(path):
        return False, 'That path does not exist or is not a folder.'

    # They picked an individual song folder (song.ini at the root level)
    if os.path.exists(os.path.join(path, 'song.ini')):
        return False, (
            'That looks like an individual song folder, not your Songs library.\n\n'
            'Please select the folder that contains all your song packs — '
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
    res      : str          # "720p" / "1080p" / "480p" / "—" / "…"
    checked  : bool = False
    status   : str  = ''    # live status text during a run
    stag     : str  = ''    # colour tag: 'done'|'error'|'busy'|'dim'|''


def _scan_library(songs_folder):
    """Return a list[Song] for the folder, sorted alphabetically."""
    songs = []
    for ini in glob.iglob(
            os.path.join(glob.escape(songs_folder), '**', 'song.ini'),
            recursive=True):
        folder = os.path.dirname(ini)
        artist, title = read_metadata(folder)
        label = build_query(artist, title) or os.path.basename(folder)
        has_vid = os.path.exists(os.path.join(folder, 'video.mp4'))
        res = '—'
        if has_vid:
            stored = get_stored_resolution(folder)
            res = stored if stored else '…'   # '…' = needs probing
        songs.append(Song(
            filename=ini, folder=folder,
            label=label, key=label.lower(),
            has_video=has_vid, res=res))
    songs.sort(key=lambda s: s.key)
    return songs


class SyncEditor(ctk.CTkToplevel):
    """Manual video-offset editor.
    Adjusting the slider or nudge buttons restarts the preview automatically
    after a short debounce so the user never needs to click a preview button."""

    _MS_MIN = -30_000
    _MS_MAX =  90_000
    _DEBOUNCE_MS = 350   # wait this long after last change before relaunching ffplay

    def __init__(self, parent, song: Song, on_save=None):
        super().__init__(parent)
        self._song         = song
        self._on_save      = on_save
        self._proc         = None   # running ffplay process
        self._proc_aux     = None   # ffmpeg feeder process when piping audio
        self._after_id     = None   # pending debounce after() id

        # SW_SHOWNOACTIVATE: ffplay window opens without stealing keyboard/mouse focus
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        self._si.wShowWindow = 4   # SW_SHOWNOACTIVATE

        self._ms    = tk.IntVar(value=self._read_offset())
        self._share = tk.BooleanVar(value=True)

        self.title('Sync Editor')
        self.geometry('500x490')
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

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=60)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=self._song.label,
                     font=ctk.CTkFont(size=14, weight='bold'),
                     text_color=_TEXT, anchor='w').pack(
            side='left', padx=18, fill='y')
        res = self._song.res if self._song.res not in ('—', '…', '') else '?'
        ctk.CTkLabel(hdr, text=res,
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTEXT).pack(side='right', padx=18)

        # Offset readout card
        card = ctk.CTkFrame(self, fg_color='#252540', corner_radius=10)
        card.pack(fill='x', padx=20, pady=(18, 0))
        top_row = ctk.CTkFrame(card, fg_color='transparent')
        top_row.pack(fill='x', padx=16, pady=(12, 0))
        self._ms_lbl = ctk.CTkLabel(
            top_row, text='',
            font=ctk.CTkFont(size=30, weight='bold'), text_color=_BLUE)
        self._ms_lbl.pack(side='left')
        # Live indicator — only shown when ffplay is available
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

        # Slider
        sf = ctk.CTkFrame(self, fg_color='transparent')
        sf.pack(fill='x', padx=20, pady=(14, 0))
        ctk.CTkLabel(sf, text='-30s', font=ctk.CTkFont(size=10),
                     text_color=_SUBTEXT).pack(side='left')
        ctk.CTkLabel(sf, text='+90s', font=ctk.CTkFont(size=10),
                     text_color=_SUBTEXT).pack(side='right')
        self._slider = ctk.CTkSlider(
            sf, from_=self._MS_MIN, to=self._MS_MAX,
            command=self._on_slider, height=16,
            button_color=_BLUE, button_hover_color='#7aaef8',
            progress_color=_BLUE)
        self._slider.set(self._ms.get())
        self._slider.pack(fill='x', padx=8)

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

        # Hint line
        hint = ('Preview updates automatically as you adjust.' if ffplayPath
                else 'ffplay not bundled — adjust the offset and Save.')
        ctk.CTkLabel(self, text=hint,
                     font=ctk.CTkFont(size=10),
                     text_color=_SUBTEXT).pack(pady=(8, 0))

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
                          'the default for this chart — no fingerprinting needed.',
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

    def _refresh(self):
        ms = self._ms.get()
        sign = '+' if ms > 0 else ''
        self._ms_lbl.configure(text=f'{sign}{ms:,} ms' if ms != 0 else '0 ms')
        s = abs(ms) / 1000.0
        if ms < -50:
            desc = f'Video has a {s:.1f}s intro before the song starts'
        elif ms > 50:
            desc = f'Song plays {s:.1f}s before the video starts'
        else:
            desc = 'Video and song start together'
        self._desc_lbl.configure(text=desc)

    def _on_slider(self, value):
        self._ms.set(int(round(value)))
        self._refresh()
        self._schedule_preview()

    def _nudge(self, delta):
        new = max(self._MS_MIN, min(self._MS_MAX, self._ms.get() + delta))
        self._ms.set(new)
        self._slider.set(new)
        self._refresh()
        self._schedule_preview()

    def _schedule_preview(self):
        """Debounce rapid changes — only relaunch ffplay once the user pauses."""
        if not ffplayPath:
            return
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(self._DEBOUNCE_MS, self._launch_preview)

    def _find_preview_audio(self):
        """Return a path to use as the audio track in the preview, or None.
        Prefers video.sync.* (music-video audio downloaded during fingerprinting)
        so the user can hear when the song kicks in inside the video.
        Falls back to chart stems if the sync file is gone."""
        folder = self._song.folder
        for f in sorted(os.listdir(folder)):
            if f.startswith('video.sync.'):
                return os.path.join(folder, f)
        for name in ('song.ogg', 'guitar.ogg', 'rhythm.ogg', 'bass.ogg', 'song.mp3'):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                return p
        return None

    def _kill_preview(self):
        """Terminate any running ffplay / ffmpeg preview processes."""
        for p in (self._proc, self._proc_aux):
            if p and p.poll() is None:
                p.terminate()
        self._proc = self._proc_aux = None

    def _launch_preview(self):
        self._after_id = None
        if not ffplayPath:
            return
        self._kill_preview()

        ms      = self._ms.get()
        v_seek  = max(0.0, -ms / 1000.0)  # how far into the video to start
        a_seek  = max(0.0,  ms / 1000.0)  # how far into the audio to start
        video   = os.path.join(self._song.folder, 'video.mp4')
        if not os.path.exists(video):
            return

        audio = self._find_preview_audio()

        ffplay_base = [ffplayPath, '-hide_banner',
                       '-x', '640', '-y', '360', '-autoexit',
                       '-window_title', f'Preview — {self._song.label}']

        if audio and ffmpegAvailable:
            # Pipe ffmpeg (video + audio merged) into ffplay so the user
            # can hear when the music starts inside the video.
            # video.sync.* and video.mp4 share the same seek point;
            # chart stems use a_seek so the chart audio is at the right offset.
            audio_seek = v_seek if os.path.basename(audio).startswith('video.sync.') else a_seek
            feeder = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-ss', f'{v_seek:.3f}',     '-i', video,
                 '-ss', f'{audio_seek:.3f}', '-i', audio,
                 '-map', '0:v', '-map', '1:a',
                 '-shortest', '-f', 'nut', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            self._proc = subprocess.Popen(
                ffplay_base + ['-'],
                stdin=feeder.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)
            feeder.stdout.close()
            self._proc_aux = feeder
        else:
            # No audio available — play video-only, still without focus steal
            self._proc = subprocess.Popen(
                ffplay_base + ['-ss', f'{v_seek:.3f}', video],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)

        if self._live_lbl:
            self._live_lbl.configure(text='● live', text_color=_GREEN)
        self._poll_live()

    def _poll_live(self):
        """Keep the live indicator accurate while ffplay is running."""
        if not self._live_lbl or not self._proc:
            return
        if self._proc.poll() is None:
            self.after(400, self._poll_live)
        else:
            self._live_lbl.configure(text='', text_color=_SUBTEXT)

    def _save(self):
        ms, share = self._ms.get(), self._share.get()
        self._close()
        if self._on_save:
            self._on_save(ms, share)

    def _close(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._kill_preview()
        self.grab_release()
        self.destroy()

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
        self._sort_col    : str  = 'label'
        self._sort_asc    : bool = True
        self._filter_mode : str  = 'missing'
        self._running     : bool = False
        self._polling     : bool = False
        self._stop_evt    = threading.Event()
        self._queue       : queue.Queue = queue.Queue()
        self._songs_folder: str  = ''
        self._sync_ready  : bool = (
            ffmpegAvailable and audiosync is not None
            and audiosync.is_available())

        self._build_ui()
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
        ctk.CTkButton(folder_row, text='Change folder',
                      width=115, height=28, font=ctk.CTkFont(size=11),
                      command=self._pick_folder).grid(row=0, column=1)

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
        self._search_var.trace_add('write', lambda *_: self._apply_filter())
        search = ctk.CTkEntry(fbar, textvariable=self._search_var,
                              placeholder_text='Search artist or title…',
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

        self._quality_var = tk.StringVar(value='720p')
        ctk.CTkOptionMenu(
            foot, variable=self._quality_var,
            values=['720p', '1080p'], width=88, height=34,
            font=_font).grid(row=0, column=2, padx=4, pady=15)

        self._start_btn = ctk.CTkButton(
            foot, text='▶  Start', width=135, font=_font_bold,
            command=self._start_download, **_fbtn)
        self._start_btn.grid(row=0, column=3, padx=4, pady=15)

        self._resync_btn = ctk.CTkButton(
            foot, text='↺  Re-sync', width=115, font=_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._start_resync, **_fbtn)
        self._resync_btn.grid(row=0, column=4, padx=4, pady=15)

        self._sync_btn = ctk.CTkButton(
            foot, text='↔  Sync', width=90, font=_font,
            fg_color='transparent', border_width=1,
            border_color=_BORDER, hover_color='#2a2a42',
            text_color=_MAUVE,
            state='disabled', command=self._sync_selected, **_fbtn)
        self._sync_btn.grid(row=0, column=5, padx=4, pady=15)

        self._stop_btn = ctk.CTkButton(
            foot, text='■  Stop', width=90, font=_font,
            fg_color='#4a1a2a', hover_color='#6a2a3e', text_color=_RED,
            state='disabled', command=self._stop, **_fbtn)
        self._stop_btn.grid(row=0, column=6, padx=4, pady=15)

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

            resolver_client.ping(sharing=resolver_client.enabled(),
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

    def _do_app_update(self, asset, sha):
        """Called on the main thread after user confirms. Runs download on a thread."""
        def _worker():
            ok = updater.apply_app_update(asset, sha)
            if ok:
                self.after(0, self.destroy)
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
        display = path if len(path) < 58 else '…' + path[-55:]
        self._folder_lbl.configure(text=f'Songs:  {display}')
        self._status_lbl.configure(text='Scanning library…')
        self.update_idletasks()

        self._songs = _scan_library(path)
        self._apply_filter()
        n = len(self._songs)
        unprobed = [s for s in self._songs if s.has_video and s.res == '…']

        if unprobed and ffmpegAvailable:
            self._status_lbl.configure(
                text=f'{n} songs found — reading resolutions…')
            threading.Thread(target=self._probe_resolutions,
                             args=(unprobed, n), daemon=True).start()
        else:
            self._status_lbl.configure(
                text=f'{n} song{"s" if n != 1 else ""} found')

        self._update_buttons()
        if not self._polling:
            self._polling = True
            self.after(200, self._poll_queue)

    def _probe_resolutions(self, songs, total_songs):
        """Background thread: probe and store resolution for unprobed videos."""
        for i, s in enumerate(songs):
            if self._stop_evt.is_set():
                break
            video = os.path.join(s.folder, 'video.mp4')
            try:
                r = subprocess.run(
                    ['ffmpeg', '-hide_banner', '-i', video],
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW)
                video_line = next((l for l in r.stderr.splitlines() if 'Video:' in l), '')
                m = re.search(r'(\d{3,4})x(\d{3,4})', video_line or r.stderr)
                if m:
                    s.res = f'{int(m.group(2))}p'
                    set_ini_values(s.folder, {'backstagehero_res': s.res})
                else:
                    s.res = '?'
            except Exception:
                s.res = '?'
            remaining = len(songs) - i - 1
            self._queue.put(('res_update', s,
                             f'{total_songs} songs found — reading resolutions ({remaining} left)…'
                             if remaining else f'{total_songs} songs found'))

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

    def _apply_filter(self):
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
        for i, s in enumerate(self._filtered):
            chk         = '  ☑' if s.checked else '  ☐'
            res_disp    = s.res or '—'
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
        self._update_buttons()

    def _update_row(self, s: Song):
        """Refresh the single visible row that corresponds to Song s."""
        for iid, song in self._iid_map.items():
            if song is s:
                chk         = '  ☑' if s.checked else '  ☐'
                res_disp    = s.res or '—'
                status_text = s.status or ('✔' if s.has_video else '✗')
                stag        = s.stag or ('dim' if s.has_video else 'error')
                existing    = list(self._tree.item(iid, 'tags'))
                row_tag     = next((t for t in existing
                                    if t in ('row_even', 'row_odd')), 'row_odd')
                tags = (row_tag,) + ((stag,) if stag else ())
                self._tree.item(iid,
                                values=(chk, s.label, res_disp, status_text),
                                tags=tags)
                return

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
        """Select All / Deselect All button — applies to the current filter view."""
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
            menu.add_separator()
        menu.add_command(label='Open folder',
                         command=lambda: os.startfile(s.folder))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _sync_selected(self):
        targets = [s for s in self._songs if s.checked and s.has_video]
        if len(targets) == 1:
            self._open_sync_editor(targets[0])

    def _open_sync_editor(self, song: Song):
        def on_save(ms: int, share: bool):
            set_ini_values(song.folder, {'video_start_time': str(ms)})
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

    def _update_buttons(self):
        checked    = [s for s in self._songs if s.checked]
        n          = len(checked)
        has_vid    = [s for s in checked if s.has_video]
        can_start  = n > 0 and not self._running
        can_resync = len(has_vid) > 0 and not self._running and self._sync_ready

        sync_targets = [s for s in self._songs if s.checked and s.has_video]
        can_sync = len(sync_targets) == 1 and not self._running

        self._start_btn.configure(
            text=f'▶  Start ({n})' if n else '▶  Start',
            state='normal' if can_start else 'disabled')
        self._resync_btn.configure(
            state='normal' if can_resync else 'disabled')
        self._sync_btn.configure(
            state='normal' if can_sync else 'disabled')
        self._stop_btn.configure(
            state='normal' if self._running else 'disabled')

        all_vis_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        self._sel_btn.configure(
            text='Deselect all' if all_vis_on else 'Select all')

    def _start_download(self):
        self._run(resync=False)

    def _start_resync(self):
        self._run(resync=True)

    def _run(self, resync):
        targets = [s for s in self._songs if s.checked]
        if resync:
            targets = [s for s in targets if s.has_video]
        if not targets:
            return

        quality = quality_format(1080 if self._quality_var.get() == '1080p' else 720)

        self._running = True
        self._stop_evt.clear()
        self._progress.set(0)
        self._update_buttons()

        for s in targets:
            s.status = '○  Pending'
            s.stag   = 'dim'
            self._update_row(s)

        threading.Thread(
            target=self._dl_thread,
            args=(targets, quality, resync),
            daemon=True).start()

    def _dl_thread(self, targets, quality, resync):
        total = len(targets)
        for i, s in enumerate(targets):
            if self._stop_evt.is_set():
                self._queue.put(('stopped', i, total))
                return

            self._queue.put(('song_start', s, i, total))
            errored = []
            result  = run_song_with_backoff(
                s.folder, s.label, quality,
                self._sync_ready,
                replace=True, resync=resync,
                errored=errored)

            if result == 'stop':
                self._queue.put(('rate_limited', s, i, total))
                return

            if errored:
                self._queue.put(('song_error', s, i, total))
            else:
                # process_download already probed and stored the resolution in song.ini
                if not resync:
                    stored = get_stored_resolution(s.folder)
                    if stored:
                        s.res = stored
                self._queue.put(('song_done', s, i, total))

        self._queue.put(('finished', total))

    def _stop(self):
        self._stop_evt.set()
        self._status_lbl.configure(text='Stopping after current song…')

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
            s.status = '⟳  Downloading…'
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

        elif kind == 'song_error':
            _, s, i, total = msg
            s.status = '✗  Error'
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
            self._status_lbl.configure(text='Rate limited — wait and try again')
            messagebox.showwarning(
                'YouTube rate limit',
                'YouTube is asking to confirm you\'re not a bot.\n\n'
                'Your IP is being rate-limited. Wait a little while and '
                'try again — everything already downloaded is safe.')

        elif kind == 'stopped':
            self._running = False
            self._update_buttons()
            self._status_lbl.configure(text='Stopped')

        elif kind == 'finished':
            total = msg[1]
            self._running = False
            self._progress.set(1.0)
            n = total
            self._status_lbl.configure(
                text=f'Done — {n} song{"s" if n != 1 else ""} processed')
            self._update_buttons()
            # Re-apply filter so has_video status reflects new downloads
            self._apply_filter()

        elif kind == 'res_update':
            _, s, status_text = msg
            self._update_row(s)
            if not self._running:
                self._status_lbl.configure(text=status_text)

        elif kind == 'app_update_available':
            _, latest, asset, sha = msg
            if messagebox.askyesno(
                    'Update available',
                    f'v{latest} is available. Install now?\n\n'
                    'The app will restart automatically.'):
                self._do_app_update(asset, sha)

        elif kind == 'ytdlp_updated':
            _, ver = msg
            self._status_lbl.configure(
                text=f'Downloader updated ({ver}) — active next launch')

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

    updater._cleanup_old_exe()   # fast, no network — always safe at startup

    app = App()
    app.mainloop()


if __name__ == '__main__':
    run()
