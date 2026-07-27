# Server-side tests for resolver/db.py (the SQLite storage + consensus logic).
#
# There was no existing coverage of this module -- test_resolver_client.py and
# test_resolver_integration.py only exercise the client side. These tests use a
# real, hermetic on-disk SQLite DB per test (tmp_path), no mocking of sqlite3,
# so they verify actual SQL correctness. They pin the behavior of the query
# consolidations in findings E19-E23: each consolidated/rewritten function must
# return values identical to the original multi-query version.

import os
import sys

# resolver/ is its own package -- resolver/app.py does a bare `import db`, so the
# resolver dir (not just the repo root) has to be on sys.path to import it here.
_RESOLVER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resolver')
if _RESOLVER_DIR not in sys.path:
    sys.path.insert(0, _RESOLVER_DIR)

import db  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    """A real, initialized, empty SQLite DB on disk, one per test."""
    path = str(tmp_path / 'test.sqlite3')
    db.init_db(path)
    c = db.connect(path)
    try:
        yield c
    finally:
        c.close()


def _set_last_seen(conn, client_id, last_seen, sharing=0):
    """Insert a client_pings row with an explicit last_seen (record_ping always
    stamps `now`, so we write directly to control the time windows)."""
    conn.execute(
        'INSERT INTO client_pings (client_id, last_seen, sharing, app_version) '
        'VALUES (?,?,?,?)',
        (client_id, int(last_seen), 1 if sharing else 0, '1.0'))
    conn.commit()


# --------------------------------------------------------------------------
# E19 -- client_stats() consolidated into one conditional-SUM query.
# --------------------------------------------------------------------------

def test_client_stats_empty_db_all_zero(conn):
    # SUM(...) is NULL on an empty table; the function must coalesce to 0 to
    # match the old per-window COUNT(*) behavior.
    assert db.client_stats(conn) == {
        'total_ever': 0, 'active_24h': 0, 'active_7d': 0,
        'active_30d': 0, 'sharing_24h': 0, 'sharing_7d': 0,
    }


def test_client_stats_windows_and_sharing(conn):
    import time
    now = int(time.time())
    HOUR = 3600
    DAY = 86400
    # Spread pings across the 24h / 7d / 30d windows, some sharing, some not.
    _set_last_seen(conn, 'a', now - 1 * HOUR, sharing=1)   # in 24h, sharing
    _set_last_seen(conn, 'b', now - 2 * HOUR, sharing=0)   # in 24h, not sharing
    _set_last_seen(conn, 'c', now - 3 * DAY,  sharing=1)   # in 7d,  sharing
    _set_last_seen(conn, 'd', now - 10 * DAY, sharing=1)   # in 30d, sharing
    _set_last_seen(conn, 'e', now - 40 * DAY, sharing=1)   # older than 30d

    s = db.client_stats(conn)
    assert s['total_ever'] == 5
    assert s['active_24h'] == 2    # a, b
    assert s['active_7d'] == 3     # a, b, c
    assert s['active_30d'] == 4    # a, b, c, d
    assert s['sharing_24h'] == 1   # a
    assert s['sharing_7d'] == 2    # a, c


def test_client_stats_matches_naive_multiquery(conn, monkeypatch):
    """The consolidated query must return exactly what six independent COUNT(*)
    queries (the pre-E19 form) would have returned on the same data.

    Two of the 20 pings (i=1, i=7) sit exactly on the 24h/7d window
    boundaries, which client_stats() includes via `>=` (db.py:261-265). That
    makes this comparison dependent on the test's `now` and client_stats()'s
    own internal `now = int(time.time())` (db.py:253) being the *same*
    instant -- true almost always, but not guaranteed, and a real clock tick
    between the two independent time.time() calls silently drops those
    boundary entries from client_stats()'s result while `naive()` (which
    reuses the test's frozen `now`) still counts them. Freeze the clock so
    the comparison is deterministic regardless of how long the test setup
    takes on a given run.
    """
    import time
    now = int(time.time())
    monkeypatch.setattr(time, 'time', lambda: float(now))
    DAY = 86400
    for i in range(20):
        _set_last_seen(conn, f'c{i}', now - i * DAY, sharing=(i % 2))

    def naive(window=None, sharing_only=False):
        sql = 'SELECT COUNT(*) FROM client_pings'
        params = []
        clauses = []
        if window is not None:
            clauses.append('last_seen >= ?')
            params.append(now - window)
        if sharing_only:
            clauses.append('sharing=1')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        return conn.execute(sql, params).fetchone()[0]

    expected = {
        'total_ever': naive(),
        'active_24h': naive(86400),
        'active_7d': naive(604800),
        'active_30d': naive(2592000),
        'sharing_24h': naive(86400, sharing_only=True),
        'sharing_7d': naive(604800, sharing_only=True),
    }
    assert db.client_stats(conn) == expected


# --------------------------------------------------------------------------
# E20 -- stats() folds the three mappings queries into one.
# --------------------------------------------------------------------------

def test_stats_empty_db(conn):
    s = db.stats(conn)
    assert s['charts'] == 0
    assert s['mappings'] == 0
    assert s['approved'] == 0
    assert s['pending'] == 0
    assert s['votes'] == 0
    # client_stats keys are merged in too.
    assert s['total_ever'] == 0


def test_stats_counts_and_matches_multiquery(conn):
    # Seed charts, mappings (mixed status), votes.
    for h in ('h1', 'h2', 'h3'):
        conn.execute('INSERT INTO charts (chart_hash) VALUES (?)', (h,))
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status) VALUES ('h1','v1','approved')")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status) VALUES ('h2','v2','pending')")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status) VALUES ('h3','v3','pending')")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status) VALUES ('h1','v9','rejected')")
    conn.execute(
        "INSERT INTO votes (chart_hash, video_id, client_id, start_ms, ts) "
        "VALUES ('h1','v1','cid1',100,1)")
    conn.commit()

    s = db.stats(conn)
    # Compare against the pre-E20 independent-query form.
    assert s['charts'] == conn.execute('SELECT COUNT(*) FROM charts').fetchone()[0]
    assert s['mappings'] == conn.execute('SELECT COUNT(*) FROM mappings').fetchone()[0]
    assert s['approved'] == conn.execute(
        "SELECT COUNT(*) FROM mappings WHERE status='approved'").fetchone()[0]
    assert s['pending'] == conn.execute(
        "SELECT COUNT(*) FROM mappings WHERE status='pending'").fetchone()[0]
    assert s['votes'] == conn.execute('SELECT COUNT(*) FROM votes').fetchone()[0]
    assert s['mappings'] == 4 and s['approved'] == 1 and s['pending'] == 2


# --------------------------------------------------------------------------
# E21 -- index exists + list_pending() still excludes approved charts.
# --------------------------------------------------------------------------

def test_status_index_exists(conn):
    names = {r['name'] for r in conn.execute('PRAGMA index_list(mappings)')}
    assert 'idx_mappings_status' in names


def test_list_pending_excludes_charts_with_an_approved_mapping(conn):
    # chart A: has a pending row AND an approved row -> excluded entirely.
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('A','a1','pending',10)")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('A','a2','approved',11)")
    # chart B: only pending -> included.
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('B','b1','pending',20)")
    # chart C: only rejected (no approved) -> included.
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('C','c1','rejected',30)")
    conn.commit()

    hashes = {r['chart_hash'] for r in db.list_pending(conn)}
    assert 'A' not in hashes         # excluded: has an approved sibling
    assert 'B' in hashes             # only pending
    assert 'C' in hashes             # rejected, but no approved


def test_list_pending_orders_by_last_seen_desc_and_respects_limit(conn):
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('B','b1','pending',20)")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('C','c1','pending',30)")
    conn.execute("INSERT INTO mappings (chart_hash, video_id, status, last_seen) VALUES ('D','d1','pending',10)")
    conn.commit()
    rows = db.list_pending(conn)
    assert [r['chart_hash'] for r in rows] == ['C', 'B', 'D']
    assert [r['chart_hash'] for r in db.list_pending(conn, limit=1)] == ['C']


# --------------------------------------------------------------------------
# E22 -- record_vote() returns the status _refresh_mapping computed, no re-query.
# --------------------------------------------------------------------------

def test_record_vote_returns_pending_below_quorum(conn):
    status = db.record_vote(conn, 'chartX', 'vidX', start_ms=100,
                            client_id='c1', confidence=0.9, ip='1.1.1.1')
    assert status == 'pending'
    db_status = conn.execute(
        "SELECT status FROM mappings WHERE chart_hash='chartX' AND video_id='vidX'"
    ).fetchone()['status']
    assert db_status == 'pending'


def test_record_vote_returns_approved_at_quorum_and_matches_db(conn):
    # QUORUM distinct source IPs on the same (chart, video) -> approved.
    ret = None
    for i in range(db.QUORUM):
        ret = db.record_vote(conn, 'chartQ', 'vidQ', start_ms=100 + i,
                             client_id=f'c{i}', confidence=0.9,
                             ip=f'10.0.0.{i}')
    assert ret == 'approved'
    db_status = conn.execute(
        "SELECT status FROM mappings WHERE chart_hash='chartQ' AND video_id='vidQ'"
    ).fetchone()['status']
    assert db_status == 'approved'
    # Returned value is the freshly-written one, not a stale re-read.
    assert ret == db_status


def test_record_vote_distinct_sources_not_raw_rows(conn):
    # Same IP voting QUORUM times from different client UUIDs counts once, so it
    # must NOT reach quorum (guards the distinct-ip_hash consensus rule).
    ret = None
    for i in range(db.QUORUM + 1):
        ret = db.record_vote(conn, 'chartS', 'vidS', start_ms=100,
                             client_id=f'same-ip-{i}', confidence=0.9,
                             ip='9.9.9.9')
    assert ret == 'pending'


# --------------------------------------------------------------------------
# E22 sibling caller -- set_status() still works after _refresh_mapping gained
# a return value (its return is ignored there, which is fine).
# --------------------------------------------------------------------------

def test_set_status_still_locks_and_refreshes(conn):
    db.record_vote(conn, 'chartL', 'vidL', start_ms=100, client_id='c1',
                   confidence=0.9, ip='2.2.2.2')
    db.set_status(conn, 'chartL', 'vidL', 'approved', lock=True)
    row = conn.execute(
        "SELECT status, locked FROM mappings WHERE chart_hash='chartL' AND video_id='vidL'"
    ).fetchone()
    assert row['status'] == 'approved'
    assert row['locked'] == 1
    # resolve() should now serve it.
    assert db.resolve(conn, 'chartL')['status'] == 'approved'
