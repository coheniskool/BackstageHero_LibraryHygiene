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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Scan a Clone Hero library and enrich backstagehero_enrichment.json '
                     'with booklet-ready song data (instruments, NPS, features, high scores).')
    parser.add_argument('--library-path', type=str, required=True,
                         help='Path to your Clone Hero songs library folder.')
    parser.add_argument('--dry-run', action='store_true',
                         help='Compute everything and print a summary without writing the sidecar.')
    parser.add_argument('--force', action='store_true',
                         help='Recompute every song, ignoring the incremental (unchanged) skip.')
    parser.add_argument('--ch-data', type=str, default=None,
                         help='Clone Hero user data directory (for scoredata.bin). '
                              'Auto-detection is not yet implemented -- omit to skip high scores.')
    parser.add_argument('--chorus-cache', type=str, default=None,
                         help='Path to the Chorus response cache file. Defaults to a file '
                              'alongside the sidecar in --library-path.')
    parser.add_argument('-v', '--verbose', action='store_true',
                         help='Log each song\'s enrichment status.')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    library_path = Path(args.library_path)
    if not library_path.is_dir():
        print(f'Library path does not exist or is not a directory: {library_path}', file=sys.stderr)
        return 1

    summary = enrich_library(
        library_path=str(library_path),
        ch_data_path=args.ch_data,
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
