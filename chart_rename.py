# chart_rename.py
# Repairs ID-suffixed chart/audio-stem/album-art filenames Clone Hero can't
# load at all (song_2400.ini instead of song.ini, notes_454.chart instead of
# notes.chart -- the same bug also hits audio-stem and album-art filenames).
# Verifies content before ever renaming; anything unconfirmed or ambiguous
# is relocated intact to _needs_review/, never guessed at. Ported from
# clonehero-video-downloader's clonehero_video_offset.py.

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path

import library_common

log = logging.getLogger('backstagehero')

# Clone Hero requires these exact literal filenames -- confirmed against the
# official wiki (Adding Custom Songs / song.ini Guide). A folder whose .ini
# or chart file is numeric-ID-suffixed (song_2400.ini, notes_454.chart)
# instead of literal can't be loaded by the game at all, regardless of file
# content being otherwise correct.
CANONICAL_CHART_NAMES = {'song.ini', 'notes.chart', 'notes.mid'}


def _notes_candidates(song_dir):
    """Chart files matching the notes.chart/notes_*.chart/.mid patterns only.

    The single source of truth for which files count as chart candidates --
    detection, verification, and rename must all use this same set. A stray
    non-notes .chart file (e.g. "AAA.chart") is deliberately invisible here:
    a broad *.chart glob in the later steps could sort it first and verify/
    rename the wrong file, leaving the real notes_NNNN.chart behind.
    """
    song_dir = Path(song_dir)
    return (
        sorted(song_dir.glob('notes.chart')) + sorted(song_dir.glob('notes_*.chart'))
        + sorted(song_dir.glob('notes.mid')) + sorted(song_dir.glob('notes_*.mid'))
    )


def scan_song_folder_chart_names(song_dir):
    """Detect (but do not verify or rename) ID-suffixed chart filenames.

    Returns {'status': ..., 'detail': ...} where status is one of: 'ok'
    (song.ini and a notes.chart/.mid are both present with literal names),
    'id_suffixed' (the .ini and/or chart file is numeric-ID-suffixed --
    detail lists which), 'no_ini' (no *.ini file at all), 'no_chart_file'
    (a literal or ID-suffixed .ini exists but no notes.chart/.mid does),
    'ambiguous' (more than one .ini, or more than one chart/.mid candidate,
    exists in the same folder -- never silently pick one and ignore the
    other).
    """
    song_dir = Path(song_dir)
    ini_files = sorted(song_dir.glob('*.ini'))
    if not ini_files:
        return {'status': 'no_ini', 'detail': ''}
    if len(ini_files) > 1:
        return {'status': 'ambiguous', 'detail': ', '.join(p.name for p in ini_files)}
    ini_file = ini_files[0]

    chart_candidates = _notes_candidates(song_dir)
    if len(chart_candidates) > 1:
        return {'status': 'ambiguous', 'detail': ', '.join(p.name for p in chart_candidates)}
    chart_file = chart_candidates[0] if chart_candidates else None

    if chart_file is None:
        return {'status': 'no_chart_file', 'detail': ini_file.name}

    id_suffixed = [
        p.name for p in (ini_file, chart_file)
        if p.name.lower() not in CANONICAL_CHART_NAMES
    ]
    if id_suffixed:
        return {'status': 'id_suffixed', 'detail': ', '.join(id_suffixed)}

    return {'status': 'ok', 'detail': f'{ini_file.name}, {chart_file.name}'}


# Both Name AND Artist must independently clear this -- set higher than a
# typical fuzzy-match band because a wrong rename is a less-reversible
# mistake than a low-confidence video match.
CHART_NAME_MATCH_THRESHOLD = 85

# Standard MIDI files carry no equivalent human-readable song-name text
# chunk Clone Hero charts reliably populate, so .mid folders fall back to
# comparing the paired audio's real duration against song.ini's
# song_length (ms).
MID_DURATION_TOLERANCE_MS = 2000


def verify_chart_content_match(song_dir, ini_fields, chart_file=None):
    """Fuzzy-verify a chart file's content against song.ini.

    Returns (matched, reason). For a .chart file: both Name and Artist must
    independently score >= CHART_NAME_MATCH_THRESHOLD (SequenceMatcher
    ratio*100) -- an OR check would wrongly pass a case where Artist
    matches but Name is a completely different song. For a .mid file (no
    embedded text metadata to compare), falls back to comparing the paired
    audio's real duration against song.ini's song_length, within
    MID_DURATION_TOLERANCE_MS.

    chart_file, when given, is the specific candidate to verify (the one
    detection selected). When None it's derived via _notes_candidates() --
    never a broad *.chart glob, which could pick up a stray non-notes file
    and verify the wrong one.
    """
    song_dir = Path(song_dir)
    if chart_file is None:
        candidates = _notes_candidates(song_dir)
        chart_file = candidates[0] if candidates else None
    if chart_file is None:
        return False, 'no .chart or .mid file found to verify against'

    if chart_file.suffix.lower() == '.chart':
        chart_fields = library_common.read_chart_song_fields(chart_file)
        if not chart_fields:
            return False, f'{chart_file.name}: no Name/Artist fields found'

        scores = {}
        for key in ('name', 'artist'):
            chart_value = library_common.normalize_lookup_value(chart_fields.get(key))
            ini_value = library_common.normalize_lookup_value(ini_fields.get(key))
            scores[key] = round(SequenceMatcher(None, chart_value, ini_value).ratio() * 100)

        failing = [key for key, score in scores.items() if score < CHART_NAME_MATCH_THRESHOLD]
        if failing:
            detail = ', '.join(f'{key} score {scores[key]}' for key in failing)
            return False, f'{chart_file.name}: {detail} below threshold {CHART_NAME_MATCH_THRESHOLD}'
        return True, ''

    mid_file = chart_file
    song_length_raw = ini_fields.get('song_length')
    if not song_length_raw:
        return False, 'song.ini has no song_length to compare against'
    try:
        expected_ms = int(str(song_length_raw).strip())
    except ValueError:
        return False, f'song.ini song_length {song_length_raw!r} is not numeric'

    audio_path = library_common.find_song_audio(song_dir)
    if audio_path is None:
        return False, 'no audio file found to probe duration against'

    actual_ms = library_common.probe_audio_duration_ms(audio_path)
    if actual_ms is None:
        return False, f'{audio_path.name}: ffprobe could not determine duration'

    diff_ms = abs(actual_ms - expected_ms)
    if diff_ms > MID_DURATION_TOLERANCE_MS:
        return False, (
            f'{mid_file.name}: audio duration {actual_ms}ms differs from '
            f'song_length {expected_ms}ms by {diff_ms}ms, exceeds tolerance {MID_DURATION_TOLERANCE_MS}ms'
        )
    return True, ''


# Complete reserved stem-role set per the canonical chart-format reference
# (https://thenathannator.github.io/GuitarGame_ChartFormats/Chart-File-Formats/
# Supported-Audio-Files/, linked from the official Clone Hero wiki).
STEM_ROLES = (
    'preview', 'song', 'guitar', 'rhythm', 'bass', 'keys', 'crowd',
    'drums', 'drums_1', 'drums_2', 'drums_3', 'drums_4',
    'vocals', 'vocals_1', 'vocals_2',
    'vocals_explicit', 'vocals_explicit_1', 'vocals_explicit_2',
)
AUDIO_STEM_EXTENSIONS = ('.ogg', '.mp3', '.wav', '.opus')


def _match_stem_role(file_stem):
    """Return (role, is_id_suffixed) for a filename stem, or (None, None).

    Two passes so a filename like "vocals_1.ogg" is recognized as a literal
    match for the reserved role "vocals_1" (a real harmony-vocals stem)
    rather than mistakenly parsed as role "vocals" with numeric ID "1".
    """
    lowered = file_stem.lower()
    for role in STEM_ROLES:
        if lowered == role:
            return role, False
    for role in STEM_ROLES:
        prefix = role + '_'
        if lowered.startswith(prefix) and lowered[len(prefix):].isdigit():
            return role, True
    return None, None


def scan_song_folder_audio_stems(song_dir):
    """Classify each recognized audio-stem role's naming state in a folder.

    Returns {'status': ..., 'detail': ...}. Statuses: 'ok' (every present
    role has exactly one literally-named file, or no audio at all --
    absence isn't this check's concern), 'rename_candidate' (one or more
    roles each have exactly one ID-suffixed candidate and nothing else --
    safe to rename, weak verification since there's no embedded metadata to
    check against), 'needs_review' (a role has multiple candidate files,
    literal+ID-suffixed both present for the same role, or unrecognized
    ambiguity -- never auto-picked).
    """
    song_dir = Path(song_dir)
    by_role = {}
    for path in song_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_STEM_EXTENSIONS:
            continue
        role, is_id_suffixed = _match_stem_role(path.stem)
        if role is None:
            continue
        by_role.setdefault(role, []).append((path, is_id_suffixed))

    ambiguous_roles = {role: files for role, files in by_role.items() if len(files) > 1}
    if ambiguous_roles:
        detail = '; '.join(
            f'{role}: {", ".join(p.name for p, _ in files)}'
            for role, files in sorted(ambiguous_roles.items())
        )
        return {'status': 'needs_review', 'detail': detail}

    rename_candidates = {
        role: files[0][0] for role, files in by_role.items()
        if files[0][1]  # the sole candidate is ID-suffixed
    }
    if rename_candidates:
        detail = ', '.join(f'{role}: {path.name}' for role, path in sorted(rename_candidates.items()))
        return {'status': 'rename_candidate', 'detail': detail}

    return {'status': 'ok', 'detail': ''}


# A "rename_candidate" role has no OTHER file to confuse it for -- the
# ambiguity check above already ruled that out -- so uniqueness alone is
# the safety bar for most roles. The "song" role is the one exception: it's
# the full backing mix Clone Hero requires to load the song at all, so a
# wrong file landing there (not a naming collision, just genuinely the
# wrong audio) is worth one extra check before committing to it: its
# duration should roughly match song.ini's song_length, the same signal
# verify_chart_content_match() already uses for a .mid chart's duration
# check. Reusing that tolerance here rather than inventing a second one.
STEM_DURATION_TOLERANCE_MS = MID_DURATION_TOLERANCE_MS


def _apply_renames_atomically(plan):
    """Apply (source, target) renames, undoing them all if any one fails.

    Returns (completed, None) on success, or (completed, (path, error,
    unwound)) on failure, where `unwound` says whether every rename that had
    already landed was successfully put back. Rolling back is itself a file
    operation and can fail (the same lock that blocked the rename can block
    the undo), so the caller is told which of the two states it's in rather
    than being left to assume.
    """
    done = []
    for path, target in plan:
        try:
            path.rename(target)
        except OSError as exc:
            unwound = True
            for moved_from, moved_to in reversed(done):
                try:
                    moved_to.rename(moved_from)
                except OSError:
                    unwound = False
            return done, (path, exc, unwound)
        done.append((path, target))
    return done, None


def apply_stem_renames(song_dir, ini_fields, dry_run=False):
    """Rename every unambiguous ID-suffixed audio stem to its literal name.

    Each stem role is judged and acted on independently -- genuine
    ambiguity in one role (e.g. four "guitar" candidates) says nothing
    about whether a completely different role (e.g. the sole "song" file)
    is safe to rename, so it must not block it. This deliberately does NOT
    delegate to scan_song_folder_audio_stems()'s single aggregate status,
    which returns 'needs_review' for the whole folder the instant ANY role
    is ambiguous -- that would silently skip every safe rename in a folder
    that also happens to have one ambiguous role (the real-world shape this
    was built to fix: an unambiguous full-mix stem sitting alongside
    multiple ambiguous guitar/rhythm candidates).

    Returns {'status': 'ok'|'needs_review', 'detail': ...}. 'ok' covers
    "renamed successfully" and "nothing to do"; 'needs_review' means at
    least one role couldn't be resolved (genuine multi-candidate ambiguity,
    a 'song'-role duration mismatch against song.ini's song_length, or a
    rename-target collision) -- any OTHER role's safe rename still went
    ahead regardless.

    dry_run=True reports what WOULD be renamed without touching any file.
    """
    song_dir = Path(song_dir)
    by_role = {}
    for path in song_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_STEM_EXTENSIONS:
            continue
        role, is_id_suffixed = _match_stem_role(path.stem)
        if role is None:
            continue
        by_role.setdefault(role, []).append((path, is_id_suffixed))

    plan = []
    blocked = []
    for role, files in sorted(by_role.items()):
        if len(files) > 1:
            blocked.append(f'{role}: {", ".join(p.name for p, _ in files)}')
            continue

        path, is_id_suffixed = files[0]
        if not is_id_suffixed:
            continue  # already literal, nothing to do for this role

        if role == 'song':
            # Two different "can't check" cases here, and they are NOT the same:
            #
            #   no song_length / unparseable -> no reference value exists, so
            #       there is nothing to check against. Fall through: this role
            #       already passed the check that actually protects it (exactly
            #       one candidate), and treating a missing ini field as grounds
            #       to relocate would sweep out large numbers of otherwise-fine
            #       folders. Deliberate -- see the test named for it.
            #
            #   song_length present but ffprobe can't read the file -> we have
            #       a reference value, tried to verify against it, and failed.
            #       That is verification attempted-and-failed, not verification
            #       unavailable, and a 'song' stem ffprobe cannot decode is
            #       itself a reason for suspicion. Block. (This one used to
            #       fall through with the rest, so the presence of a real check
            #       silently bought nothing whenever the probe errored.)
            #
            # Contrast verify_chart_content_match()'s .mid branch, which fails
            # closed on all three: there the duration IS the only evidence,
            # because a .mid carries no name/artist text to compare instead.
            expected_ms = None
            expected_raw = ini_fields.get('song_length')
            if expected_raw:
                try:
                    expected_ms = int(str(expected_raw).strip())
                except ValueError:
                    expected_ms = None  # unparseable -- no usable reference value
            if expected_ms is not None:
                actual_ms = library_common.probe_audio_duration_ms(path)
                if actual_ms is None:
                    blocked.append(
                        f'{path.name}: song_length says {expected_ms}ms but ffprobe could '
                        f'not read the file to check it')
                    continue
                if abs(actual_ms - expected_ms) > STEM_DURATION_TOLERANCE_MS:
                    blocked.append(
                        f'{path.name}: duration {actual_ms}ms differs from song_length '
                        f'{expected_ms}ms by {abs(actual_ms - expected_ms)}ms')
                    continue

        target = song_dir / f'{role}{path.suffix.lower()}'
        if target.exists():
            blocked.append(f'{path.name}: {target.name} already exists')
            continue
        plan.append((path, target))

    if not dry_run and plan:
        done, failure = _apply_renames_atomically(plan)
        if failure is not None:
            # Half a rename plan is worse than none of it: Clone Hero needs the
            # whole set to load the song, and a partially-renamed folder looks
            # settled to the next run. An ordinary Windows file lock (antivirus
            # mid-scan, an open Explorer preview) is enough to trigger this, so
            # undo what landed and report rather than propagating -- an
            # exception here aborted the entire library scan.
            failed_path, error, unwound = failure
            detail = f'{failed_path.name}: rename failed ({error})'
            if not unwound:
                detail += (f'; could not undo {len(done)} earlier rename(s) in this '
                           f'folder -- it is left partially renamed, fix by hand')
            return {'status': 'needs_review', 'detail': detail}

    parts = []
    if plan:
        renamed_detail = '; '.join(f'{p.name} -> {t.name}' for p, t in plan)
        if dry_run:
            renamed_detail += ' (dry-run, not applied)'
        parts.append(renamed_detail)
    if blocked:
        parts.append('; '.join(blocked))

    status = 'needs_review' if blocked else 'ok'
    detail = '; '.join(parts) if parts else 'already correct'
    return {'status': status, 'detail': detail}


# Album art is expected in the track-selection screen but never blocks
# loading a song the way a missing chart/audio file does.
ALBUM_ART_EXTENSIONS = ('.png', '.jpg', '.jpeg')


def scan_song_folder_album_art(song_dir):
    """Classify album-art naming state: literal 'album.{png,jpg,jpeg}'.

    Returns {'status': ..., 'detail': ...}. Statuses: 'ok' (a literal match
    exists, or none at all -- album art isn't hard-required),
    'rename_candidate' (exactly one ID-suffixed candidate and nothing
    else -- safe to rename, no embedded metadata to verify against),
    'needs_review' (multiple candidates, or a literal name coexisting with
    an ID-suffixed one -- never auto-picked).
    """
    song_dir = Path(song_dir)
    candidates = []
    for ext in ALBUM_ART_EXTENSIONS:
        for path in song_dir.glob('album*' + ext):
            stem = path.stem.lower()
            if stem == 'album' or (stem.startswith('album_') and stem[len('album_'):].isdigit()):
                candidates.append(path)

    if len(candidates) > 1:
        detail = ', '.join(p.name for p in sorted(candidates, key=lambda p: p.name))
        return {'status': 'needs_review', 'detail': detail}

    if not candidates:
        return {'status': 'ok', 'detail': ''}

    sole = candidates[0]
    if sole.stem.lower() == 'album':
        return {'status': 'ok', 'detail': sole.name}
    return {'status': 'rename_candidate', 'detail': sole.name}


def apply_album_art_rename(song_dir, dry_run=False):
    """Rename an unambiguous ID-suffixed album-art file to its literal name.

    For user-provided or external album art: no content verification is
    feasible -- no embedded metadata, and image content can't be meaningfully
    cross-checked against song.ini. For album art extracted from a static
    video: static_art.py's frame-based verification has already confirmed
    content. In both cases, uniqueness alone (guaranteed by
    scan_song_folder_album_art() before this is reached) is the safety bar,
    consistent with album art's non-blocking status in Clone Hero.

    Returns {'status': 'ok'|'needs_review', 'detail': ...}, matching
    scan_song_folder_album_art()'s vocabulary. dry_run=True reports what
    WOULD be renamed without touching the file.
    """
    song_dir = Path(song_dir)
    detection = scan_song_folder_album_art(song_dir)
    if detection['status'] != 'rename_candidate':
        return detection

    sole = song_dir / detection['detail']
    target = song_dir / f'album{sole.suffix.lower()}'
    if target.exists():
        return {'status': 'needs_review', 'detail': f'{sole.name}: {target.name} already exists'}

    if not dry_run:
        sole.rename(target)
    detail = f'{sole.name} -> {target.name}'
    if dry_run:
        detail += ' (dry-run, not applied)'
    return {'status': 'ok', 'detail': detail}


def is_sng_packaged(song_dir):
    """True if the folder contains a .sng single-file chart container.

    Clone Hero's newer .sng format bundles and replaces the loose-file
    structure entirely -- "cannot be edited manually" per the official
    wiki. Callers must skip all rename/verification checks for such a
    folder; there's nothing loose to verify or rename.
    """
    return any(Path(song_dir).glob('*.sng'))


def process_chart_folder_names(song_dir, dry_run=False):
    """Verify and rename ID-suffixed song.ini/notes.chart/notes.mid, with a collision guard.

    Returns {'status': ..., 'detail': ...}. Statuses: 'confirmed_ok'
    (already literally named, or safely renamed after content verification
    passed), 'needs_review' (content couldn't be confirmed, a rename
    target already exists, or there's nothing to verify against),
    'skipped_sng' (is_sng_packaged() -- left completely untouched).

    dry_run=True computes and returns the same status/detail without
    renaming anything -- the reported outcome describes what WOULD happen.
    """
    song_dir = Path(song_dir)
    if is_sng_packaged(song_dir):
        return {'status': 'skipped_sng', 'detail': ''}

    detection = scan_song_folder_chart_names(song_dir)
    if detection['status'] == 'ok':
        return {'status': 'confirmed_ok', 'detail': detection['detail']}
    if detection['status'] in ('no_ini', 'no_chart_file', 'ambiguous'):
        return {'status': 'needs_review', 'detail': f"{detection['status']}: {detection['detail']}"}

    # detection['status'] == 'id_suffixed' -- verify content before touching anything
    ini_files = sorted(song_dir.glob('*.ini'))
    ini_file = next((p for p in ini_files if p.name.lower() == 'song.ini'), ini_files[0])
    ini_fields = library_common.read_song_ini_fields(ini_file, ('name', 'artist', 'song_length'))

    # the SAME candidate detection selected -- detection guaranteed exactly
    # one notes-pattern file exists, and verifying/renaming must target that
    # file, never a broad *.chart re-glob (a stray non-notes .chart file
    # could sort first and get verified/renamed in its place)
    chart_candidates = _notes_candidates(song_dir)
    if not chart_candidates:
        return {'status': 'needs_review', 'detail': 'no .chart or .mid file found to rename'}
    chart_file = chart_candidates[0]

    matched, reason = verify_chart_content_match(song_dir, ini_fields, chart_file=chart_file)
    if not matched:
        return {'status': 'needs_review', 'detail': reason}
    target_chart_name = 'notes.chart' if chart_file.suffix.lower() == '.chart' else 'notes.mid'
    target_chart = song_dir / target_chart_name

    # collision guard: never overwrite a file that already exists at the target
    # name -- can happen if a prior partial run or manual edit left both present
    target_ini = song_dir / 'song.ini'
    if ini_file.name.lower() != 'song.ini' and target_ini.exists():
        return {'status': 'needs_review', 'detail': f'{ini_file.name}: song.ini already exists'}
    if chart_file.name.lower() != target_chart_name and target_chart.exists():
        return {'status': 'needs_review', 'detail': f'{chart_file.name}: {target_chart_name} already exists'}

    renamed = []
    if ini_file.name.lower() != 'song.ini':
        if not dry_run:
            ini_file.rename(target_ini)
        renamed.append(f'{ini_file.name} -> song.ini')
    if chart_file.name.lower() != target_chart_name:
        if not dry_run:
            chart_file.rename(target_chart)
        renamed.append(f'{chart_file.name} -> {target_chart_name}')

    detail = '; '.join(renamed) if renamed else 'already correct'
    if dry_run and renamed:
        detail += ' (dry-run, not applied)'
    return {'status': 'confirmed_ok', 'detail': detail}


# Same file dedupe_report.py's keeper-scoring points at for its
# chart_rename_status gate -- shared per-folder metadata, not duplicated.
CHART_RENAME_METADATA_FILENAME = 'video_meta.json'


def load_chart_rename_status(song_dir):
    """Return the persisted chart_rename_status, or None if not yet scanned.

    Absence (None) is a distinct third state from 'confirmed_ok'/
    'needs_review' -- callers (notably dedupe_report.py's keeper-scoring)
    must treat it identically to 'needs_review', never as "assumed clean".
    """
    metadata_path = Path(song_dir) / CHART_RENAME_METADATA_FILENAME
    if not metadata_path.exists():
        return None
    try:
        with metadata_path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data.get('chart_rename_status')
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_chart_rename_status(song_dir, status, detail=''):
    """Persist chart_rename_status into video_meta.json, merging with existing fields.

    Merges rather than overwrites so this never clobbers other fields
    (offset/video-match data, etc.) a different feature already wrote for
    the same folder.
    """
    metadata_path = Path(song_dir) / CHART_RENAME_METADATA_FILENAME
    metadata = {}
    if metadata_path.exists():
        try:
            with metadata_path.open('r', encoding='utf-8') as handle:
                metadata = json.load(handle)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
    metadata['chart_rename_status'] = status
    metadata['chart_rename_detail'] = detail
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')


def process_song_folder_for_chart_rename(song_dir, home_folder, dry_run=False):
    """Full per-folder chart-rename pass: names + audio-stems + album-art.

    Returns {'status': ..., 'detail': ...}. Statuses: 'confirmed_ok' (the
    ini/chart/mid check passes, and every unambiguous audio-stem/album-art
    rename candidate was actually renamed -- see apply_stem_renames()/
    apply_album_art_rename()), 'needs_review' (genuine ambiguity remains
    somewhere -- the folder is relocated intact to a sibling _needs_review
    folder via library_common.move_to_review(), unless dry_run),
    'skipped_sng', 'skipped_settled' (chart_rename_status was already
    'confirmed_ok' on a prior run -- resumability).

    dry_run=True computes the same outcome without renaming, relocating, or
    persisting anything.
    """
    song_dir = Path(song_dir)

    if is_sng_packaged(song_dir):
        return {'status': 'skipped_sng', 'detail': ''}

    if load_chart_rename_status(song_dir) == 'confirmed_ok':
        return {'status': 'skipped_settled', 'detail': 'already confirmed_ok'}

    names_result = process_chart_folder_names(song_dir, dry_run=dry_run)

    ini_path = library_common.find_song_ini(song_dir)
    ini_fields = library_common.read_song_ini_fields(ini_path, ('song_length',)) if ini_path else {}

    audio_result = apply_stem_renames(song_dir, ini_fields, dry_run=dry_run)
    album_art_result = apply_album_art_rename(song_dir, dry_run=dry_run)

    failures = [
        r['detail'] for r in (names_result, audio_result, album_art_result)
        if r['status'] not in ('confirmed_ok', 'ok')
    ]

    if failures:
        detail = '; '.join(failures)
        if not dry_run:
            library_common.move_to_review(song_dir, home_folder, '_needs_review', detail)
            # status intentionally not persisted here -- the folder no
            # longer exists at song_dir once relocated, and the manifest
            # already records the relocation and its reason
        return {'status': 'needs_review', 'detail': detail}

    if not dry_run:
        save_chart_rename_status(song_dir, 'confirmed_ok', names_result['detail'])
    return {'status': 'confirmed_ok', 'detail': names_result['detail']}


def scan_and_fix_chart_library(home_folder, dry_run=False):
    """Scan every song folder under home_folder and fix ID-suffixed chart naming.

    Opt-in, not run automatically -- this relocates whole folders out of the
    library (more invasive than video_repair.py's in-place fixes).

    Returns the counts dict (status -> number of folders), so a caller
    (e.g. the GUI) can build its own summary without re-parsing printed
    output.
    """
    library_common.make_console_encoding_safe()
    print('=' * 70)
    print('SCANNING CHART FILE NAMING' + (' (DRY RUN)' if dry_run else ''))
    print('=' * 70)

    counts = {}
    needs_review = []

    # Recursive discovery, matching the app's own **/song.ini scan. Walking a
    # single level treated each PACK folder of a nested library as a song
    # folder with no .ini and relocated the whole pack -- see the discovery
    # note in library_common.
    for folder in library_common.iter_song_folders(home_folder):
        # nested libraries make bare folder.name ambiguous (two packs can both
        # contain "Intro"), so report the path relative to the library root
        try:
            label = str(folder.relative_to(home_folder))
        except ValueError:
            label = folder.name

        result = process_song_folder_for_chart_rename(folder, home_folder, dry_run=dry_run)
        counts[result['status']] = counts.get(result['status'], 0) + 1

        if result['status'] == 'needs_review':
            needs_review.append((label, result['detail']))
            print(f"  NEEDS REVIEW: {label}: {result['detail']}")
        elif result['status'] == 'confirmed_ok' and ' -> ' in result['detail']:
            # ' -> ' only appears in process_chart_folder_names()'s detail
            # when an actual rename happened -- an already-literal folder's
            # detail is just the filenames (no arrow) and shouldn't be
            # logged as if something were renamed
            print(f"  Renamed: {label}: {result['detail']}")

    print()
    print(
        f"Scan complete: {counts.get('confirmed_ok', 0)} confirmed ok, "
        f"{counts.get('needs_review', 0)} need review, "
        f"{counts.get('skipped_settled', 0)} already settled, "
        f"{counts.get('skipped_sng', 0)} .sng-packaged (skipped)."
    )
    if needs_review:
        print(f"{len(needs_review)} folder(s) need manual review:")
        for name, reason in needs_review:
            print(f"  - {name}: {reason}")
    print('=' * 70)
    print()
    return counts
