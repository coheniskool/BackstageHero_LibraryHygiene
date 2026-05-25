import configparser
import glob
import os
import shutil
import sys
import time
import yt_dlp
import ffmpeg
from tqdm import tqdm

# audiosync aligns each video to the chart automatically by fingerprinting the
# audio. It depends on numpy and ffmpeg; if either is missing we fall back to a
# fixed offset, so the import is allowed to fail without breaking anything.
try:
    import audiosync
except Exception:
    audiosync = None

# When running as a PyInstaller exe, add the exe's directory to PATH so that
# ffmpeg.exe placed alongside the program is found by ffmpeg and yt-dlp.
if getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(sys.executable)
    os.environ['PATH'] = exe_dir + os.pathsep + os.environ.get('PATH', '')

# ffmpeg is optional. The android_vr client already delivers h264/mp4 that Clone
# Hero plays, so when ffmpeg is missing we simply use the download as-is. When it
# is present, 1080p downloads are remuxed for maximum compatibility.
ffmpegAvailable = shutil.which('ffmpeg') is not None

# YouTube clients that serve h264/AVC formats without cookies, PO tokens, or a
# JavaScript runtime. android_vr provides up to 1080p; android is a 360p fallback.
# These clients are incompatible with cookies, so none are used.
YOUTUBE_CLIENTS = ['android_vr', 'android']

# Intermediate files that must never be mistaken for a finished video. A real
# 'video.mp4' is only ever created by an atomic rename once it is fully complete,
# so anything below is a leftover from an interrupted run and is safe to delete.
# (output.mp4 / video.mp4.part are also cleaned to heal libraries touched by
# older versions of this tool.)
TEMP_ARTIFACTS = [
    'video.download.mp4', 'video.download.mp4.part', 'video.download.mp4.ytdl',
    'video.tmp.mp4', 'output.mp4', 'video.mp4.part', 'video.mp4.ytdl',
    'song.ini.tmp',
]


def cleanup_temp_files(folder='.'):
    """Delete any interrupted-download leftovers in a song folder. Never touches
    a finished video.mp4."""
    for name in TEMP_ARTIFACTS:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    # The throwaway audio fetched for syncing has a variable extension.
    for path in glob.glob(os.path.join(folder, 'video.sync.*')):
        try:
            os.remove(path)
        except OSError:
            pass


def search_youtube(query):
    search_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': YOUTUBE_CLIENTS}},
    }
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        info = ydl.extract_info(f"ytsearch2:{query}", download=False)
    if not info or 'entries' not in info or not info['entries']:
        raise Exception("No search results found for: " + query)
    entries = info['entries']
    url1 = f"https://www.youtube.com/watch?v={entries[0]['id']}"
    url2 = f"https://www.youtube.com/watch?v={entries[1]['id']}" if len(entries) > 1 else url1
    title = entries[0].get('title', 'Unknown')
    return url1, url2, title


def download_video(ydl, url):
    # Start every attempt from a clean slate so a partial file from a previous
    # attempt (e.g. the first search result) can never corrupt this download.
    cleanup_temp_files()
    ydl.download([url])  # writes 'video.download.mp4'

    if videoQuality == 'mp4' or not ffmpegAvailable:
        # 720p needs no remux; and when ffmpeg is missing the raw android_vr
        # download is already h264/mp4 and plays in Clone Hero. Promote as-is.
        os.replace('video.download.mp4', 'video.mp4')
        return url

    # Remux into a Clone Hero-friendly container, then atomically promote so that
    # 'video.mp4' only ever appears in its final, correct form.
    print('Formatting downloaded video for Clone Hero')
    try:
        stream = ffmpeg.input('video.download.mp4')
        stream = ffmpeg.output(stream, 'video.tmp.mp4', vcodec='copy')
        ffmpeg.run(stream, overwrite_output=True, quiet=True)
    except Exception as e:
        # ffmpeg missing or failed: fall back to the raw download, which is
        # already h264/mp4 and usually plays fine in Clone Hero.
        print('Could not remux (using raw download instead). Error: ' + str(e))
        if os.path.exists('video.tmp.mp4'):
            try:
                os.remove('video.tmp.mp4')
            except OSError:
                pass
        os.replace('video.download.mp4', 'video.mp4')
    else:
        os.remove('video.download.mp4')
        os.replace('video.tmp.mp4', 'video.mp4')
        print('Video ready')
    return url


def write_song_ini(config):
    """Write song.ini atomically so an interrupt can never truncate the user's
    song metadata."""
    with open('song.ini.tmp', 'w', encoding='utf-8') as configfile:
        config.write(configfile)
    os.replace('song.ini.tmp', 'song.ini')


# A music video almost always opens with a few seconds of intro before the song
# itself begins, so this is the fallback head start used when alignment can't run
# or isn't confident. It matches what this tool has always shipped.
DEFAULT_START_TIME = -3000


def fetch_sync_audio(url):
    """Download just the audio of the chosen video, for fingerprinting only.

    The saved video.mp4 is video-only (Clone Hero supplies the audio), so there
    is nothing in it to align against. This grabs the matching audio track to a
    throwaway file and returns its path, or None on failure. The file is small
    and is deleted as soon as the offset has been computed."""
    cleanup_temp_files()
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'video.sync.%(ext)s',
        'noplaylist': 1,
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': YOUTUBE_CLIENTS}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    matches = glob.glob('video.sync.*')
    return matches[0] if matches else None


def determine_start_time(url):
    """Return video_start_time (ms) for the song in the current folder.

    Fetches the chosen video's audio and fingerprints it against the chart's own
    stems to find where the song actually starts, so the video lines up on its
    own. Falls back to DEFAULT_START_TIME when ffmpeg/numpy are unavailable or the
    match is not trustworthy (e.g. the top YouTube result is a live take, a remix,
    or a different master than the chart)."""
    if not ffmpegAvailable or audiosync is None or not audiosync.is_available():
        return DEFAULT_START_TIME
    try:
        audio = fetch_sync_audio(url)
        if not audio:
            return DEFAULT_START_TIME
        ms, info = audiosync.compute_offset_ms('.', audio)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print('  Auto-sync error (using default offset): ' + str(e))
        return DEFAULT_START_TIME
    finally:
        cleanup_temp_files()
    if ms is None:
        print('  Auto-sync skipped (' + info + '); using default offset')
        return DEFAULT_START_TIME
    print('  Auto-synced: ' + info)
    return ms


print('Checking for home folder...')
songsFolder = os.getcwd() + "\\songs"
time.sleep(0.5)
if os.path.exists(songsFolder):
    print('Songs folder found\n')
    time.sleep(0.5)
    replace = 'false'
    videoQuality = '720p'

    qualityInput = input('Type the number to pick from the following options:\n'
                      + '1. Default quality (720p)\n'
                      + '2. Best quality (1080p, where available; significantly bigger files):\n'
                      + '3. [EXPERIMENTAL] Replace existing videos with 1080p (Caution: Use at your own risk. May malfunction and delete videos)\n\n'
                      + 'Pick between 1-3: ')

    if qualityInput == '1':
        print('Set to 720p')
        videoQuality = 'mp4'
    elif qualityInput == '2':
        print('Set to 1080p. Poor hard drive!')
        videoQuality = 'bestvideo[vcodec^=avc]/best[ext=mp4]/best'
    elif qualityInput == '3':
        print('Replacing all videos with 1080p. You have time for a nap!')
        videoQuality = 'bestvideo[vcodec^=avc]/best[ext=mp4]/best'
        replace = 'true'
    else:
        print('You must choose between 1-3. Try again')
        exit()

    # Auto-sync needs ffmpeg (to decode audio for fingerprinting) and numpy.
    syncReady = ffmpegAvailable and audiosync is not None and audiosync.is_available()

    if not ffmpegAvailable:
        print('\nNote: ffmpeg was not found.')
        if videoQuality != 'mp4':
            print('  - 1080p videos are saved as-is (still h264 — they play fine in Clone Hero).')
        print('  - Auto-sync is off, so videos use a default 3-second offset.')
        print('  Place ffmpeg.exe next to this program to enable both (see the README).')
    elif syncReady:
        print('\nAuto-sync is on: each video is lined up to its chart automatically')
        print('(this fetches a little extra audio per song to match them up).')
    else:
        print('\nNote: auto-sync is off (numpy unavailable); using the default offset.')

    homeFolder = os.path.abspath(os.getcwd() + "\\songs")
    os.chdir(homeFolder)
    videoTitle = ''
    i = 0
    erroredSongs = []
    erroredSongNames = []

    # Pre-scan: count songs and, in the same pass, heal any leftover corruption
    # from a previously interrupted run (partial temp files, zero-byte videos).
    print('Scanning library and cleaning up any interrupted downloads...')
    for filename in glob.iglob(homeFolder + "/**/song.ini", recursive=True):
        i += 1
        folder = os.path.dirname(filename)
        cleanup_temp_files(folder)
        # A zero-byte video.mp4 is a corpse from a hard kill; drop it so it re-downloads.
        vid = os.path.join(folder, 'video.mp4')
        if os.path.exists(vid) and os.path.getsize(vid) == 0:
            try:
                os.remove(vid)
            except OSError:
                pass

    totalcount = i

    print('\n' + '-' * 64)
    print('You can safely stop this program at any time (Ctrl+C or close the')
    print('window). Nothing will be left corrupt, and re-running it later resumes')
    print('where you left off — songs that already have a video are skipped.')
    print('-' * 64 + '\n')

    interrupted = False
    try:
        with tqdm(total=i, unit="videos") as pbar:
            for filename in glob.iglob(homeFolder + "/**/song.ini", recursive=True):
                currentSongFileFolder = os.path.dirname(filename)
                currentSongName = os.path.basename(currentSongFileFolder)
                os.chdir(currentSongFileFolder)
                pbar.update(1)

                if (not os.path.exists("video.mp4") and currentSongName not in erroredSongNames) or replace == 'true':
                    try:
                        # In replace mode, drop the existing video first.
                        if replace == 'true' and os.path.exists('video.mp4'):
                            os.remove('video.mp4')
                        cleanup_temp_files()

                        # Strip strings that cause YouTube to return Clone Hero/Rock Band playthrough results
                        titleIssues = ['(2x Bass Pedal Expert+)', '(2x Bass Pedal)', 'RB3', '(RB3 version)', '(Rh)']
                        for issue in titleIssues:
                            currentSongName = currentSongName.replace(issue, '')

                        query = '{} (Official Music Video)'.format(currentSongName)
                        print('\nLooking on YouTube for: ' + query)

                        url, url2, videoTitle = search_youtube(query)
                        print("Search success. Now downloading: " + videoTitle)

                        ydl_opts = {
                            'outtmpl': 'video.download.mp4',
                            'format': videoQuality,
                            'nooverwrites': 0,
                            'noplaylist': 1,
                            'quiet': True,
                            'no_warnings': True,
                            # android_vr serves h264 up to 1080p with no cookies/PO-token/JS needed.
                            'extractor_args': {'youtube': {'player_client': YOUTUBE_CLIENTS}},
                        }

                        usedUrl = url
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            try:
                                usedUrl = download_video(ydl, url)
                            except KeyboardInterrupt:
                                raise
                            except Exception as e:
                                print('Error while downloading: ' + str(e) + '. Trying second video')
                                usedUrl = download_video(ydl, url2)

                        # Read song.ini fully and close it before writing, so the
                        # atomic replace isn't blocked by an open handle on Windows.
                        # utf-8-sig strips a leading BOM that some song.ini files carry.
                        with open('song.ini', encoding='utf-8-sig') as songCheck:
                            songContents = songCheck.read()

                        # check if the ini file contains unexpected phase shift converter text
                        if '//Converted' in songContents:
                            erroredSongs.append(filename)
                        else:
                            config = configparser.ConfigParser()
                            config.read_string(songContents)
                            startTime = str(determine_start_time(usedUrl))
                            # Check uppercase/lowercase config section name
                            if config.has_section('song'):
                                config.set('song', 'video_start_time', startTime)
                                print('Song ready. Next song...\n')
                            elif config.has_section('Song'):
                                config.set('Song', 'video_start_time', startTime)
                                print('Song ready. Next song...\n')
                            else:
                                print('Could not update song.ini. Check the song.ini for potential issues\n')
                                erroredSongs.append(filename)
                            write_song_ini(config)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        print(e)
                        print("Error downloading song: " + currentSongName + ". Skipping")
                        cleanup_temp_files()
                        erroredSongs.append(videoTitle)
                        erroredSongNames.append(currentSongName)
                        continue
    except KeyboardInterrupt:
        interrupted = True
        # cwd is the song that was mid-flight; remove its partial artifacts so it
        # is treated as "not yet downloaded" and retried cleanly next time.
        cleanup_temp_files()
        print('\n\nStopped. The song in progress was cleaned up — nothing corrupt was left behind.')
        print('Everything already downloaded is safe. Re-run this program any time to')
        print('resume; finished songs are skipped automatically.')

    if interrupted:
        input('\nPress Enter to exit...')
    else:
        if erroredSongs:
            print("The following songs ran into problems and may need a manual look:")
            for song in erroredSongs:
                print(song, end='\n')
        print('\nTip: you can re-run this program any time to fill in anything that')
        print('was skipped or errored — it only downloads what is still missing.')
        input("All downloads complete. Checked a total of " + str(totalcount) + " songs. Press Enter button to exit.")
else:
    input("Did not detect a 'Songs' folder. Check you have placed the .exe file in the directory one level above it. Press any button to exit")
