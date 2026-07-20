# library_scores.py
# Clone Hero high-score lookup for the library-enrichment tool (Task 1.2 of
# tasks/plan-library-enrichment.md).
#
# notes_mid_md5() is fully implemented: it's a plain MD5-of-a-file
# operation with no format ambiguity. It is deliberately a SEPARATE hash
# from resolver_client.chart_hash() (SHA256, prefers notes.chart when both
# exist) -- Clone Hero's own score file keys entries by MD5(notes.mid)
# specifically, a different algorithm serving a different purpose. See
# SPEC-library-enrichment.md's Sidecar Format section.
#
# read_scoredata() is a STUB pending a real-install spike. A hex dump of an
# actual Clone Hero installation's score file (2026-07-20) found two things
# that contradict the initially assumed format:
#   1. The file is named `scores.bin`, not `scoredata.bin`.
#   2. Entries are NOT the raw-16-byte-MD5 layout a third-party reader's
#      README described -- the observed layout starts with a length-prefixed
#      ASCII hex string (1 byte length=32, then 32 hex chars), not raw bytes.
# Implementing a full binary parser against either the wrong or an
# unconfirmed layout would produce silently wrong scores, which is worse
# than no scores -- so this returns {} until re-verified against a real
# chart+score pair. See tasks/todo-library-enrichment.md, Task 1.2.

import hashlib
import logging
from pathlib import Path

log = logging.getLogger('backstagehero')

# Real filename confirmed against a live install; kept as a constant here
# (not yet wired into auto-detection, since read_scoredata() doesn't parse
# it yet) so the correction is visible in one place.
SCOREDATA_FILENAME = 'scores.bin'


def notes_mid_md5(song_folder):
    """MD5 hex digest of song_folder/notes.mid, or None if it doesn't exist
    or can't be read. This is the key Clone Hero's own score file uses --
    NOT resolver_client.chart_hash(), which is a different hash for a
    different purpose (see module docstring)."""
    mid_path = Path(song_folder) / 'notes.mid'
    try:
        with open(mid_path, 'rb') as f:
            h = hashlib.md5()
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
            return h.hexdigest()
    except OSError:
        return None


def read_scoredata(ch_data_path):
    """High scores keyed by notes_mid_md5(), or {} -- always {} right now.

    STUB: no real parser yet. See module docstring for why. Callers must
    already treat an empty result as "no scores available" (per spec
    Boundaries -> Never Do: never depend on scoredata.bin being present),
    so this is a safe, honest placeholder rather than a half-implementation
    that would need a second boundary check bolted on later.
    """
    return {}
