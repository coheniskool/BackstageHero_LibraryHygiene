# dedupe_report.py
# Finds duplicate charts of the same song from different sources, scores
# each copy, and moves everything except the best-scoring "keeper" into
# _duplicates_review for manual review. Never deletes anything. Ported from
# clonehero-video-downloader's dedupe_report.py, repointed at
# library_common/chorus_client/chart_rename instead of importlib-loading
# CH-VideoScript.py.
#
# Score adaptation from the original: the old scoring model included an
# "offset_confidence" signal read from a video_meta.json field this
# project's predecessor wrote. BackstageHero's audiosync.py never persists
# a per-song sync-confidence value anywhere on disk (it's used transiently
# during download selection, then only sent to the community resolver) --
# there is no data source for that signal here, so it's dropped rather than
# faked. See tasks/todo.md's Phase 1 notes.

import argparse
import re
from difflib import SequenceMatcher
from pathlib import Path

import chart_rename
import chorus_client
import library_common

try:
    import acoustid
except ImportError as exc:
    acoustid = None
    print(f'Audio fingerprinting disabled (missing dependency: {exc}).')
    print("Run 'pip install pyacoustid' and ensure fpcalc (official AcoustID/Chromaprint release) "
          'is on PATH to enable it.')


# A duplicate copy is often annotated with a "[dup2]"/"[dup253]"-style
# suffix. Neither parse_folder_name() nor strip_title_noise() strips this
# (they only handle trailing parenthetical/pedal noise), so an exact-key or
# fuzzy match without this step would miss real cases like
# "Weezer - My Name Is Jonas [dup3]".
_DUP_SUFFIX_RE = re.compile(r'\s*[\[\(]dup\d*[\]\)]\s*$', re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r'\s*\[[^\]]*\]\s*$')

# A version tag changes the underlying audio -- a live recording is not a
# duplicate of the studio original, even when title/artist match exactly.
# Two folders are only ever candidates for grouping when their version tags
# (or lack thereof) match.
_VERSION_TAG_RE = re.compile(
    r'\(\s*(live|acoustic|remix|cover|demo|instrumental|unplugged|remaster(?:ed)?|extended|radio edit)\b[^)]*\)',
    re.IGNORECASE,
)

ARTIST_MATCH_THRESHOLD = 90
TITLE_MATCH_THRESHOLD = 85


def _clean_folder_name(name):
    name = _DUP_SUFFIX_RE.sub('', name)
    name = _TRAILING_BRACKET_RE.sub('', name)
    return name


def _version_tag(title):
    match = _VERSION_TAG_RE.search(title)
    return match.group(1).lower() if match else None


def group_candidates(song_folders):
    """Fuzzy-group song folders that are likely the same underlying song.

    Produces CANDIDATE groups only -- confirm_group() (audio fingerprinting)
    narrows each candidate group to fingerprint-confirmed duplicates before
    anything gets scored or moved. Two folders are NEVER grouped if either
    has a version tag (Live/Acoustic/Remix/...) the other lacks, even when
    their base artist/title fuzzy-match closely.
    """
    parsed = []
    for folder in song_folders:
        cleaned_name = _clean_folder_name(folder.name)
        # version tag must be read from the name BEFORE parse_folder_name()'s
        # internal strip_title_noise() call -- that function silently
        # removes "(Live)"/"(Acoustic)"/"(Remix)" as YouTube-search noise,
        # which would make the tag invisible to this check if read from its
        # output instead
        version_tag = _version_tag(cleaned_name)
        artist, title = library_common.parse_folder_name(cleaned_name)
        parsed.append({
            'folder': folder,
            'artist_norm': library_common.normalize_lookup_value(artist),
            'title_norm': library_common.normalize_lookup_value(title),
            'version_tag': version_tag,
        })

    groups = []
    used = set()
    for i, a in enumerate(parsed):
        if i in used:
            continue
        group = [a['folder']]
        for j in range(i + 1, len(parsed)):
            if j in used:
                continue
            b = parsed[j]
            if a['version_tag'] != b['version_tag']:
                continue
            artist_score = SequenceMatcher(None, a['artist_norm'], b['artist_norm']).ratio() * 100
            title_score = SequenceMatcher(None, a['title_norm'], b['title_norm']).ratio() * 100
            if artist_score >= ARTIST_MATCH_THRESHOLD and title_score >= TITLE_MATCH_THRESHOLD:
                group.append(b['folder'])
                used.add(j)
        if len(group) > 1:
            used.add(i)
            groups.append(group)

    return groups


# compare_fingerprints() returns a [0,1] similarity score; 0.95 requires a
# near-exact match (same recording, allowing for minor encode differences)
# without being so strict that ordinary lossy-encoding variance between two
# copies of the identical source audio would fail to match.
FINGERPRINT_MATCH_THRESHOLD = 0.95


def confirm_group(candidate_group):
    """Narrow a fuzzy-matched candidate group to fingerprint-confirmed duplicates.

    Fingerprints each folder's reference audio (library_common.find_song_audio)
    via pyacoustid/fpcalc and keeps only folders whose fingerprint actually
    matches the group's first successfully-fingerprinted folder. This is
    what catches cases fuzzy title matching alone can't: two folders with
    the same (or near-identical) title/artist that are nonetheless
    different underlying recordings.

    Returns a list of confirmed folders, or [] if fewer than two folders in
    the group are confirmed to match each other, if fingerprinting is
    unavailable, or if any fingerprint call fails -- never raises.
    """
    if acoustid is None:
        return []

    fingerprints = []
    for folder in candidate_group:
        audio_path = library_common.find_song_audio(folder)
        if audio_path is None:
            continue
        try:
            _duration, fingerprint = acoustid.fingerprint_file(str(audio_path))
        except Exception as e:
            print(f'Fingerprint error {folder}: {e}')
            continue
        fingerprints.append((folder, fingerprint))

    if len(fingerprints) < 2:
        return []

    reference_folder, reference_fp = fingerprints[0]
    confirmed = [reference_folder]
    for folder, fp in fingerprints[1:]:
        try:
            similarity = acoustid.compare_fingerprints(reference_fp, fp)
        except Exception as e:
            print(f'Fingerprint comparison error {folder}: {e}')
            continue
        if similarity >= FINGERPRINT_MATCH_THRESHOLD:
            confirmed.append(folder)

    return confirmed if len(confirmed) > 1 else []


# The 13 real diff_* fields confirmed from the Chorus schema -- Clone
# Hero's own song.ini uses the same key names for its per-instrument
# difficulty ratings, -1 meaning "not charted".
DIFF_KEYS = (
    'diff_band', 'diff_guitar', 'diff_guitar_coop', 'diff_rhythm', 'diff_bass',
    'diff_drums', 'diff_drums_real', 'diff_keys', 'diff_guitarghl',
    'diff_guitar_coop_ghl', 'diff_rhythm_ghl', 'diff_bassghl', 'diff_vocals',
)
METADATA_KEYS = ('year', 'genre', 'charter', 'album')


def _diff_is_charted(ini_fields, key):
    """True if a diff_* key marks a genuinely charted instrument.

    ini fields arrive as raw strings from read_song_ini_fields(), and real
    song.ini files list uncharted instruments explicitly as "diff_bass = -1"
    -- a naive `fields.get(key, -1) != -1` compares the STRING "-1" against
    the int -1 and wrongly counts every explicitly-uncharted instrument as
    charted, inflating instrument_count for exactly the sparse folders it's
    supposed to penalize. Unparseable values count as not charted.
    """
    raw = ini_fields.get(key)
    if raw is None:
        return False
    try:
        return int(str(raw).strip()) != -1
    except ValueError:
        return False


def score_folder(song_dir, video_meta, song_ini_fields, chorus_data):
    """Score a folder's quality/completeness signals for keeper selection.

    Returns (score, breakdown) -- breakdown is a dict of {signal_name:
    points} so the report can show *why* a folder won, not just the final
    number. Weighted heavily toward instrument/chart completeness (the
    actual playable content), with video presence, metadata completeness,
    and a Chorus quality signal as smaller factors.
    """
    breakdown = {}
    breakdown['has_video'] = 10 if video_meta.get('video_status') == 'present' else 0
    breakdown['instrument_count'] = sum(
        1 for key in DIFF_KEYS if _diff_is_charted(song_ini_fields, key)
    ) * 5
    breakdown['metadata_completeness'] = sum(
        1 for key in METADATA_KEYS if song_ini_fields.get(key)
    ) * 2
    # A small bonus when Chorus's own record has no known issues -- a
    # quality flag, not a popularity/rating measure, since no rating field
    # exists.
    breakdown['chorus_signal'] = (
        5 if chorus_data and not chorus_data.get('folderIssues') and not chorus_data.get('metadataIssues') else 0
    )
    return sum(breakdown.values()), breakdown


def is_keeper_eligible(video_meta):
    """True only if chart_rename_status is exactly 'confirmed_ok'.

    Absence (not yet scanned by chart_rename.py) is treated identically to
    'needs_review' -- an unscanned folder's actual chart/audio content is
    just as unconfirmed as a flagged one, and picking either as a keeper
    would permanently promote a potentially wrong chart under the right
    folder name.
    """
    return video_meta.get('chart_rename_status') == 'confirmed_ok'


def select_keeper(group, scores, eligibility):
    """Pick the highest-scoring keeper-eligible folder in a group.

    Hard precondition, not just documented intent: never returns a folder
    that isn't keeper-eligible, even if it scored highest. Returns None if
    no folder in the group is eligible -- the caller must skip the whole
    group and flag it for manual attention rather than auto-resolving it.
    """
    eligible = [folder for folder in group if eligibility.get(folder)]
    if not eligible:
        return None
    return max(eligible, key=lambda folder: scores[folder])


def flag_borrow_candidates(keeper_ini_fields, keeper_video_meta, loser_ini_fields, loser_video_meta):
    """Report-only: flag things a loser has that the keeper lacks.

    Never acts on these -- purely informational so the user can decide
    whether a manual merge is worth doing before deleting the loser (e.g. a
    Pro Drums track, a set difficulty rating, a background video the keeper
    doesn't have). Never writes to any chart/song.ini/audio file.
    """
    flags = []
    for key in DIFF_KEYS:
        keeper_has = _diff_is_charted(keeper_ini_fields, key)
        loser_has = _diff_is_charted(loser_ini_fields, key)
        if loser_has and not keeper_has:
            flags.append(f'{key} (loser has it, keeper does not)')

    if loser_video_meta.get('video_status') == 'present' and keeper_video_meta.get('video_status') != 'present':
        flags.append('video background (loser has it, keeper does not)')

    return flags


def _build_score_inputs(song_dir):
    """Translate folder contents + chart_rename_status into score_folder()'s expected shape."""
    video_meta = {
        'video_status': 'present' if library_common.find_video_file(song_dir) is not None else 'no_video',
        'chart_rename_status': chart_rename.load_chart_rename_status(song_dir),
    }

    ini_files = sorted(Path(song_dir).glob('*.ini'))
    if not ini_files:
        return video_meta, {}
    ini_target = next((p for p in ini_files if p.name.lower() == 'song.ini'), ini_files[0])
    ini_fields = library_common.read_song_ini_fields(ini_target, ('name', 'artist') + DIFF_KEYS + METADATA_KEYS)
    return video_meta, ini_fields


def generate_dedupe_report(home_folder, dry_run=False):
    """Full library-wide dedupe pass: group, confirm, score, move losers, flag borrows.

    Never deletes anything -- losers are relocated intact to
    _duplicates_review/ for the user's own manual review.

    Returns {'candidate_groups', 'resolved', 'skipped_all_ineligible',
    'skipped_not_confirmed'}, so a caller (e.g. the GUI) can build its own
    summary without re-parsing printed output.
    """
    home_folder = Path(home_folder)
    song_folders = [f for f in home_folder.iterdir() if f.is_dir() and not f.name.startswith('_')]

    print('=' * 70)
    print('DUPLICATE SONG DETECTION' + (' (DRY RUN)' if dry_run else ''))
    print('=' * 70)

    candidate_groups = group_candidates(song_folders)
    print(f'Found {len(candidate_groups)} candidate group(s) from {len(song_folders)} folder(s) '
          '(fuzzy match, pre-fingerprint).')
    if acoustid is None:
        print('WARNING: audio fingerprinting is unavailable (see dependency warning above) -- '
              'no group can be confirmed.')
    print()

    resolved = 0
    skipped_all_ineligible = 0
    skipped_not_confirmed = 0

    for group in candidate_groups:
        confirmed = confirm_group(group)
        if len(confirmed) < 2:
            skipped_not_confirmed += 1
            continue

        video_metas, ini_fields_map, scores, eligibility = {}, {}, {}, {}
        for folder in confirmed:
            video_meta, ini_fields = _build_score_inputs(folder)
            chorus_data = None
            if ini_fields.get('artist') and ini_fields.get('name'):
                chorus_data = chorus_client.search_by_artist_title(ini_fields['artist'], ini_fields['name'])
            score, _breakdown = score_folder(folder, video_meta, ini_fields, chorus_data)
            video_metas[folder] = video_meta
            ini_fields_map[folder] = ini_fields
            scores[folder] = score
            eligibility[folder] = is_keeper_eligible(video_meta)

        keeper = select_keeper(confirmed, scores, eligibility)
        if keeper is None:
            skipped_all_ineligible += 1
            print(f"  SKIPPED (every folder needs_review/unscanned): {[f.name for f in confirmed]}")
            continue

        print(f'  Keeper: {keeper.name} (score {scores[keeper]})')
        for folder in confirmed:
            if folder == keeper:
                continue
            flags = flag_borrow_candidates(ini_fields_map[keeper], video_metas[keeper],
                                            ini_fields_map[folder], video_metas[folder])
            reason = f'lower score than keeper {keeper.name} ({scores[folder]} vs {scores[keeper]})'
            if not dry_run:
                library_common.move_to_review(folder, home_folder, '_duplicates_review', reason,
                                               extra_manifest_fields={'score': scores[folder]})
            print(f'    Loser: {folder.name} -> _duplicates_review/ (score {scores[folder]})')
            if flags:
                print(f"      Borrow candidates: {', '.join(flags)}")
        resolved += 1

    print()
    print(
        f'Dedupe complete: {resolved} group(s) resolved, '
        f'{skipped_all_ineligible} skipped (every folder needs_review/unscanned), '
        f'{skipped_not_confirmed} skipped (fingerprint did not confirm a real duplicate).'
    )
    print('=' * 70)
    print()
    return {
        'candidate_groups': len(candidate_groups),
        'resolved': resolved,
        'skipped_all_ineligible': skipped_all_ineligible,
        'skipped_not_confirmed': skipped_not_confirmed,
    }


def parse_args():
    parser = argparse.ArgumentParser(description='Find and relocate duplicate Clone Hero song folders.')
    parser.add_argument('--library-path', type=str, required=True, help='Path to your Clone Hero songs library folder.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Compute groups/scores/borrow-candidate flags and log them without moving any folder.',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    generate_dedupe_report(args.library_path, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
