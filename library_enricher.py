# library_enricher.py
# CLI entry point for the library-enrichment tool (Task 2.2 of
# tasks/plan-library-enrichment.md). Thin wrapper over
# library_enrichment.enrich_library() -- argument parsing, exit codes, and
# a printed summary; all real logic lives in library_enrichment.py so it
# stays independently testable and GUI-callable (Task 3.1) without a
# subprocess round-trip.
#
# No logging setup of its own: importing library_enrichment pulls in
# VideoDownload, whose module-level _setup_logging() already attaches a
# rotating file handler to the 'backstagehero' logger -- the same one
# every other module in this project logs through. Nothing to duplicate.

import argparse
import sys
from pathlib import Path

from library_enrichment import enrich_library

# Example paths shown in the interactive prompts. The Songs-library
# examples are real, confirmed paths from an actual scan of a working
# library this session -- not placeholders. The ch-data path is Unity's
# persistentDataPath convention for Clone Hero, confirmed against a real
# install (see SPEC-library-enrichment.md's --ch-data flag description).
_LIBRARY_PATH_EXAMPLES = (
    r'F:\Clone Hero\Library\Songs',
    r'M:\_Organized\Songs',
    r'C:\Users\<you>\Documents\Clone Hero\Songs',
)
_CH_DATA_EXAMPLE_WINDOWS = r'C:\Users\<you>\AppData\LocalLow\srylain Inc_\Clone Hero'
_CH_DATA_EXAMPLE_MAC = r'~/Library/Application Support/com.srylain.CloneHero'
_CH_DATA_EXAMPLE_LINUX = r'~/.config/unity3d/srylain Inc_/Clone Hero'

_MAX_LIBRARY_PATH_ATTEMPTS = 3


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Scan a Clone Hero library and enrich backstagehero_enrichment.json '
                     'with booklet-ready song data (instruments, NPS, features, high scores).')
    parser.add_argument('--library-path', type=str, default=None,
                         help='Path to your Clone Hero songs library folder. '
                              'Prompted for interactively if omitted.')
    parser.add_argument('--dry-run', action='store_true',
                         help='Preview mode: never modifies song.ini or other library files. '
                              'Still writes the enrichment sidecar cache (same as a normal run), '
                              'so the work is reused next time.')
    parser.add_argument('--force', action='store_true',
                         help='Recompute every song, ignoring the incremental (unchanged) skip.')
    parser.add_argument('--ch-data', type=str, default=None,
                         help='Clone Hero user data directory (for scores.bin). '
                              'Prompted for interactively if omitted; leave blank there to skip high scores.')
    parser.add_argument('--chorus-cache', type=str, default=None,
                         help='Path to the Chorus response cache file. Defaults to a file '
                              'alongside the sidecar in --library-path.')
    parser.add_argument('-v', '--verbose', action='store_true',
                         help='Log each song\'s enrichment status.')
    return parser.parse_args(argv)


def _prompt_for_library_path(input_fn):
    print()
    print("No --library-path given. Where is your Clone Hero Songs library?")
    print('Examples:')
    for example in _LIBRARY_PATH_EXAMPLES:
        print(f'  {example}')
    for attempt in range(_MAX_LIBRARY_PATH_ATTEMPTS):
        entered = input_fn('Songs library path: ').strip()
        if entered and Path(entered).is_dir():
            return entered
        remaining = _MAX_LIBRARY_PATH_ATTEMPTS - attempt - 1
        if remaining:
            print(f'That path does not exist or is not a folder ({remaining} attempt(s) left).')
    return None


def _prompt_for_ch_data(input_fn):
    print()
    print('No --ch-data given. Where is your Clone Hero user data folder (for high scores)?')
    print('Leave blank to skip high scores for now.')
    print('Examples:')
    print(f'  Windows: {_CH_DATA_EXAMPLE_WINDOWS}')
    print(f'  Mac:     {_CH_DATA_EXAMPLE_MAC}')
    print(f'  Linux:   {_CH_DATA_EXAMPLE_LINUX}')
    entered = input_fn('Clone Hero data path (blank to skip): ').strip()
    return entered or None


def main(argv=None, input_fn=input):
    args = parse_args(argv)

    library_path_str = args.library_path
    if library_path_str is None:
        library_path_str = _prompt_for_library_path(input_fn)
        if library_path_str is None:
            print('No valid library path given, giving up.', file=sys.stderr)
            return 1

    library_path = Path(library_path_str)
    if not library_path.is_dir():
        print(f'Library path does not exist or is not a directory: {library_path}', file=sys.stderr)
        return 1

    ch_data = args.ch_data
    if ch_data is None:
        ch_data = _prompt_for_ch_data(input_fn)

    summary = enrich_library(
        library_path=str(library_path),
        ch_data_path=ch_data,
        dry_run=args.dry_run,
        force=args.force,
        chorus_cache_path=args.chorus_cache,
        verbose=args.verbose,
    )

    mode = 'Dry run' if args.dry_run else 'Enrichment complete'
    print(f'{mode}: {summary["songs_processed"]} processed, '
          f'{summary["songs_skipped"]} skipped (unchanged), '
          f'{summary["problems_found"]} problem(s) found '
          f'({summary["duration_seconds"]:.1f}s).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
