# SQLite storage and consensus logic.
# A mapping goes from 'pending' to 'approved' once QUORUM different clients report
# the same chart->video pairing. Maintainer can override from the dashboard.

import hashlib
import os
import sqlite3
import statistics
import time

# how many different sources have to agree on a (chart, video) before it goes
# live. a "source" is a hashed reporter IP, not the client_id the app makes up,
# so one person can't fake a quorum by generating a pile of UUIDs.
QUORUM = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS votes (
    chart_hash TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    ip_hash    TEXT NOT NULL DEFAULT '',
    start_ms   INTEGER NOT NULL,
    confidence REAL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (chart_hash, video_id, client_id)
);
CREATE TABLE IF NOT EXISTS mappings (
    chart_hash TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    votes      INTEGER NOT NULL DEFAULT 0,
    start_ms   INTEGER,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    locked     INTEGER NOT NULL DEFAULT 0,        -- 1 = curator decision, ignores votes
    first_seen INTEGER,
    last_seen  INTEGER,
    PRIMARY KEY (chart_hash, video_id)
);
CREATE TABLE IF NOT EXISTS charts (
    chart_hash TEXT PRIMARY KEY,
    artist     TEXT,
    title      TEXT,
    last_seen  INTEGER
);
CREATE TABLE IF NOT EXISTS client_pings (
    client_id    TEXT PRIMARY KEY,
    last_seen    INTEGER NOT NULL,
    sharing      INTEGER NOT NULL DEFAULT 0,  -- 1 if the user has sharing enabled
    app_version  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mappings_hash ON mappings(chart_hash);
CREATE INDEX IF NOT EXISTS idx_mappings_status ON mappings(status);
CREATE INDEX IF NOT EXISTS idx_pings_last_seen ON client_pings(last_seen);
"""


def connect(path):
    """Open a connection with WAL so concurrent vote writes and reads coexist."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def init_db(path):
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        # Migrate older DBs that predate the ip_hash column.
        cols = [r['name'] for r in conn.execute('PRAGMA table_info(votes)')]
        if 'ip_hash' not in cols:
            conn.execute("ALTER TABLE votes ADD COLUMN ip_hash TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def _ip_hash(ip, client_id):
    """Key for one vote source. Hash the IP so we never store the raw address.
    No IP (e.g. behind something that strips it), fall back to the client_id so
    it just counts per-client like it used to instead of all collapsing to one."""
    if ip:
        return 'ip:' + hashlib.sha256(ip.encode('utf-8')).hexdigest()[:16]
    return 'cid:' + (client_id or '')


def _refresh_mapping(conn, chart_hash, video_id, now):
    """Recompute one mapping's aggregates and (unless locked) its status.

    Returns the resulting status string ('approved' | 'pending' | 'rejected')
    so callers don't have to re-read it back out of the mappings table."""
    # E23: the distinct-source count could be pushed to SQL via
    # SELECT COUNT(DISTINCT ip_hash), but median_ms below needs every row's
    # start_ms anyway (SQLite has no median aggregate), so pulling all rows in
    # this single query is already the minimal-query-count option -- splitting
    # out a COUNT(DISTINCT) would add a query, not remove one. Left as-is.
    rows = conn.execute(
        'SELECT start_ms, ip_hash FROM votes WHERE chart_hash=? AND video_id=?',
        (chart_hash, video_id)).fetchall()
    # Quorum counts distinct sources (hashed IPs), not raw rows, so several
    # client UUIDs from one machine count once.
    votes = len({r['ip_hash'] for r in rows})
    median_ms = int(statistics.median([r['start_ms'] for r in rows])) if rows else None

    existing = conn.execute(
        'SELECT status, locked, first_seen FROM mappings WHERE chart_hash=? AND video_id=?',
        (chart_hash, video_id)).fetchone()
    locked = existing['locked'] if existing else 0
    first_seen = existing['first_seen'] if existing else now

    if locked:
        status = existing['status']            # curator decision stands
    else:
        # Don't auto-approve if the curator has already locked another video for this chart,
        # otherwise votes can re-approve a sibling and create two 'approved' rows.
        locked_sibling = conn.execute(
            "SELECT 1 FROM mappings WHERE chart_hash=? AND video_id!=? AND status='approved' AND locked=1",
            (chart_hash, video_id)).fetchone()
        status = 'approved' if (votes >= QUORUM and not locked_sibling) else 'pending'

    conn.execute(
        """INSERT INTO mappings (chart_hash, video_id, votes, start_ms, status, locked, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(chart_hash, video_id) DO UPDATE SET
             votes=excluded.votes, start_ms=excluded.start_ms,
             status=excluded.status, last_seen=excluded.last_seen""",
        (chart_hash, video_id, votes, median_ms, status, locked, first_seen, now))

    # Only one auto-approved video per chart: if this one just auto-approved,
    # demote any other non-locked approved sibling back to pending.
    if status == 'approved' and not locked:
        conn.execute(
            """UPDATE mappings SET status='pending'
               WHERE chart_hash=? AND video_id!=? AND status='approved' AND locked=0""",
            (chart_hash, video_id))

    return status


def record_vote(conn, chart_hash, video_id, start_ms, client_id, confidence,
                artist='', title='', ip=''):
    """Record one client's vote and recompute consensus. Returns the mapping's
    status afterwards ('approved' | 'pending' | 'rejected')."""
    now = int(time.time())
    iph = _ip_hash(ip, client_id)
    conn.execute(
        """INSERT INTO votes (chart_hash, video_id, client_id, ip_hash, start_ms, confidence, ts)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(chart_hash, video_id, client_id) DO UPDATE SET
             ip_hash=excluded.ip_hash, start_ms=excluded.start_ms,
             confidence=excluded.confidence, ts=excluded.ts""",
        (chart_hash, video_id, client_id, iph, int(start_ms), float(confidence), now))

    conn.execute(
        """INSERT INTO charts (chart_hash, artist, title, last_seen) VALUES (?,?,?,?)
           ON CONFLICT(chart_hash) DO UPDATE SET
             artist=CASE WHEN excluded.artist!='' THEN excluded.artist ELSE charts.artist END,
             title =CASE WHEN excluded.title !='' THEN excluded.title  ELSE charts.title  END,
             last_seen=excluded.last_seen""",
        (chart_hash, artist, title, now))

    status = _refresh_mapping(conn, chart_hash, video_id, now)
    conn.commit()
    return status


def resolve(conn, chart_hash):
    """Best confirmed mapping for a chart, or a 'none' result.

    Serves only approved mappings (curator-locked first, then the most-voted), so
    an unconfirmed guess is never handed out as if it were trusted."""
    row = conn.execute(
        """SELECT video_id, start_ms, votes, status FROM mappings
           WHERE chart_hash=? AND status='approved'
           ORDER BY locked DESC, votes DESC LIMIT 1""",
        (chart_hash,)).fetchone()
    if not row:
        return {'hash': chart_hash, 'status': 'none'}
    return {
        'hash': chart_hash,
        'status': 'approved',
        'video_id': row['video_id'],
        'start_ms': row['start_ms'],
        'votes': row['votes'],
    }


def set_status(conn, chart_hash, video_id, status, lock=True):
    """Curator override. Locks the decision so votes can't undo it. Approving one
    video demotes any other approved sibling for the same chart."""
    now = int(time.time())
    locked = 1 if lock else 0
    conn.execute(
        """INSERT INTO mappings (chart_hash, video_id, votes, start_ms, status, locked, first_seen, last_seen)
           VALUES (?,?,0,NULL,?,?,?,?)
           ON CONFLICT(chart_hash, video_id) DO UPDATE SET status=excluded.status,
             locked=excluded.locked, last_seen=excluded.last_seen""",
        (chart_hash, video_id, status, locked, now, now))
    if status == 'approved':
        conn.execute(
            """UPDATE mappings SET status='pending', locked=0
               WHERE chart_hash=? AND video_id!=? AND status='approved'""",
            (chart_hash, video_id))
    # Recompute so the locked status sticks but the vote count/median stay fresh.
    _refresh_mapping(conn, chart_hash, video_id, now)
    conn.commit()


def set_start_ms(conn, chart_hash, video_id, start_ms):
    """Curator override of the served offset (locks the mapping's timing)."""
    conn.execute(
        'UPDATE mappings SET start_ms=?, locked=1 WHERE chart_hash=? AND video_id=?',
        (int(start_ms), chart_hash, video_id))
    conn.commit()


def list_pending(conn, limit=200):
    """Charts awaiting a decision: those with no approved mapping yet, newest
    activity first. This is the maintainer's work queue."""
    # E21: NOT EXISTS (SQLite plans it better than NOT IN) over the
    # idx_mappings_status index -- filters the "has an approved sibling" check
    # without a full scan of mappings on every call.
    return [dict(r) for r in conn.execute(
        """SELECT m.chart_hash, m.video_id, m.votes, m.start_ms, m.status,
                  c.artist, c.title, m.last_seen
           FROM mappings m LEFT JOIN charts c ON c.chart_hash=m.chart_hash
           WHERE NOT EXISTS (
               SELECT 1 FROM mappings m2
               WHERE m2.chart_hash=m.chart_hash AND m2.status='approved')
           ORDER BY m.last_seen DESC LIMIT ?""", (limit,)).fetchall()]


def record_ping(conn, client_id, sharing, app_version=''):
    """Upsert a client heartbeat. Called on app startup."""
    now = int(time.time())
    conn.execute(
        """INSERT INTO client_pings (client_id, last_seen, sharing, app_version)
           VALUES (?,?,?,?)
           ON CONFLICT(client_id) DO UPDATE SET
             last_seen=excluded.last_seen,
             sharing=excluded.sharing,
             app_version=excluded.app_version""",
        (client_id, now, 1 if sharing else 0, app_version or ''))
    conn.commit()


def client_stats(conn):
    """Active user counts over several windows."""
    now = int(time.time())
    d1, d7, d30 = now - 86400, now - 604800, now - 2592000
    # E19: one pass over client_pings with conditional SUMs instead of six
    # separate COUNT(*) queries. SUM(...) is NULL on an empty table, so coalesce
    # each window back to 0 to match the old per-query COUNT(*) behavior.
    row = conn.execute(
        """SELECT
             COUNT(*)                                                      AS total_ever,
             SUM(CASE WHEN last_seen >= ?               THEN 1 ELSE 0 END) AS active_24h,
             SUM(CASE WHEN last_seen >= ?               THEN 1 ELSE 0 END) AS active_7d,
             SUM(CASE WHEN last_seen >= ?               THEN 1 ELSE 0 END) AS active_30d,
             SUM(CASE WHEN last_seen >= ? AND sharing=1 THEN 1 ELSE 0 END) AS sharing_24h,
             SUM(CASE WHEN last_seen >= ? AND sharing=1 THEN 1 ELSE 0 END) AS sharing_7d
           FROM client_pings""",
        (d1, d7, d30, d1, d7)).fetchone()
    return {
        'total_ever':  row['total_ever'],
        'active_24h':  row['active_24h'] or 0,
        'active_7d':   row['active_7d'] or 0,
        'active_30d':  row['active_30d'] or 0,
        'sharing_24h': row['sharing_24h'] or 0,
        'sharing_7d':  row['sharing_7d'] or 0,
    }


def stats(conn):
    def scalar(sql):
        return conn.execute(sql).fetchone()[0]
    # E20: total/approved/pending all hit the mappings table -- fold them into
    # one pass. (charts and votes are different tables, so they stay separate.)
    # SUM(...) is NULL on an empty table, so coalesce approved/pending to 0.
    m = conn.execute(
        """SELECT COUNT(*)                                             AS total,
                  SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END)   AS approved,
                  SUM(CASE WHEN status='pending'  THEN 1 ELSE 0 END)   AS pending
           FROM mappings""").fetchone()
    s = {
        'charts':   scalar('SELECT COUNT(*) FROM charts'),
        'mappings': m['total'],
        'approved': m['approved'] or 0,
        'pending':  m['pending'] or 0,
        'votes':    scalar('SELECT COUNT(*) FROM votes'),
    }
    s.update(client_stats(conn))
    return s
