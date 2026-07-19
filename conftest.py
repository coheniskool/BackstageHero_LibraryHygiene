import os
import sys

# Repo root isn't on sys.path by default under pytest's "prepend" import mode
# (only the tests/ directory is, since it has no __init__.py above it). The
# modules under test (VideoDownload, resolver_client, updater, audiosync,
# library_common) live at the repo root, so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
