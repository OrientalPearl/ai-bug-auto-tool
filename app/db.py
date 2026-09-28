"""SQLite storage layer: schema, connection helpers and all data access.

Design goals
------------
* One single file database (`bug_system.db`) so it can be copied/backup easily.
* Every write goes through a short-lived connection with WAL disabled
  (Windows friendly) so the web app, the CLI and the AI executor can all
  touch the same file without holding locks.
* Zentao bug id is the natural key (`zentao_id` UNIQUE), so syncing is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import get_settings

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

BUG_STATUSES = (
    "pending",        # pulled from Zentao, not started yet
    "fixing",         # AI is working on it
    "need_solution",  # AI is blocked, waiting for the owner decision
    "await_review",   # committed on a branch, waiting for human review
    "rejected",       # review failed, must be fixed again
    "merged",         # review passed and merged into trunk
    "closed",         # closed in Zentao by the owner
)

NEED_STATUSES = ("awaiting", "replied", "done")
REVIEW_RESULTS = ("pass", "reject")
SYNC_ACTIONS = ("sync", "update", "comment", "error")

# Extensions rendered as pictures in the UI (anything else is a plain file).
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}

_STATUS_SQL = ",".join(f"'{s}'" for s in BUG_STATUSES)

SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bugs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zentao_id       INTEGER NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    severity        INTEGER NOT NULL DEFAULT 3,
    pri             INTEGER NOT NULL DEFAULT 3,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ({_STATUS_SQL})),
    branch          TEXT,
    steps           TEXT,
    module          TEXT,
    assigned_to     TEXT,
    opened_by       TEXT,
    opened_build    TEXT,
    zentao_status   TEXT,
    zentao_resolution TEXT,
    opened_date     TEXT,
    steps_html      TEXT,
    comments        TEXT,
    attachments     TEXT,
    product_id      INTEGER,
    product_name    TEXT,
    assigned_to_name TEXT,
    detail_synced_at TEXT,
    fix_summary     TEXT,
    verify_steps    TEXT,
    files_changed   TEXT,
    raw_json        TEXT,
    synced_at       TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bugs_status ON bugs(status);
CREATE INDEX IF NOT EXISTS idx_bugs_queue  ON bugs(pri, severity);

CREATE TABLE IF NOT EXISTS need_solution (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id      INTEGER NOT NULL REFERENCES bugs(id) ON DELETE CASCADE,
    question    TEXT    NOT NULL,
    ai_options  TEXT    NOT NULL DEFAULT '',
    ai_advice   TEXT    NOT NULL DEFAULT '',
    owner_reply TEXT,
    status      TEXT    NOT NULL DEFAULT 'awaiting'
                CHECK (status IN ('awaiting', 'replied', 'done')),
    created_at  TEXT    NOT NULL,
    replied_at  TEXT,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_need_bug    ON need_solution(bug_id);
CREATE INDEX IF NOT EXISTS idx_need_status ON need_solution(status);

CREATE TABLE IF NOT EXISTS svn_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id     INTEGER NOT NULL REFERENCES bugs(id) ON DELETE CASCADE,
    revision   TEXT    NOT NULL,
    branch     TEXT,
    message    TEXT    NOT NULL DEFAULT '',
    author     TEXT,
    files      TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rev_bug ON svn_revisions(bug_id);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id          INTEGER NOT NULL REFERENCES bugs(id) ON DELETE CASCADE,
    result          TEXT    NOT NULL CHECK (result IN ('{REVIEW_RESULTS[0]}', '{REVIEW_RESULTS[1]}')),
    reject_reason   TEXT,
    merged_revision TEXT,
    reviewer        TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_bug ON reviews(bug_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT    NOT NULL CHECK (action IN ('sync', 'update', 'comment', 'error')),
    zentao_id  INTEGER,
    payload    TEXT    NOT NULL DEFAULT '{{}}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_created ON sync_log(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- What the executor actually concluded about one bug: symptom, root cause, the
-- evidence it read, blast radius, per-gate results and what is still unverified.
-- One bug can accumulate several analyses (rework after a rejection, block...).
CREATE TABLE IF NOT EXISTS analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id       INTEGER NOT NULL REFERENCES bugs(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL DEFAULT 'commit'
                 CHECK (kind IN ('commit', 'block', 'manual')),
    symptom      TEXT    NOT NULL DEFAULT '',
    root_cause   TEXT    NOT NULL DEFAULT '',
    evidence     TEXT    NOT NULL DEFAULT '',
    call_chain   TEXT    NOT NULL DEFAULT '',
    change_desc  TEXT    NOT NULL DEFAULT '',
    impact       TEXT    NOT NULL DEFAULT '',
    verify       TEXT    NOT NULL DEFAULT '',
    gates        TEXT    NOT NULL DEFAULT '{{}}',
    unverified   TEXT    NOT NULL DEFAULT '',
    rollback     TEXT    NOT NULL DEFAULT '',
    conclusion   TEXT    NOT NULL DEFAULT '',
    author       TEXT    NOT NULL DEFAULT 'ai',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_bug ON analyses(bug_id);

-- One Zentao product == one code repository. Rows here override the global
-- SVN_* defaults in .env, so a bug is always committed to its own repo.
-- working_copy     = where the executor edits code (the git-svn mirror draft area)
-- svn_working_copy = the human's real SVN checkout, used only by the i18n
--                    direct-commit channel (AUTO_LOOP.md G12)
CREATE TABLE IF NOT EXISTS product_repos (
    product_id     INTEGER PRIMARY KEY,
    product_name   TEXT    NOT NULL DEFAULT '',
    repo_url       TEXT    NOT NULL,
    trunk_path     TEXT    NOT NULL DEFAULT '/trunk',
    branch_root    TEXT    NOT NULL DEFAULT '/branches',
    working_copy   TEXT    NOT NULL DEFAULT '',
    svn_working_copy TEXT  NOT NULL DEFAULT '',
    branch_prefix  TEXT    NOT NULL DEFAULT '',
    build_command  TEXT    NOT NULL DEFAULT '',
    test_command   TEXT    NOT NULL DEFAULT '',
    note           TEXT    NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_path() -> Path:
    return Path(get_settings().db_path)


def connect() -> sqlite3.Connection:
    """Open a connection that returns dict-like rows."""
    settings = get_settings()
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx(autocommit_select: bool = False) -> Iterator[sqlite3.Connection]:
    """Context manager committing on success and rolling back on error."""
    conn = connect()
    try:
        yield conn
        if not autocommit_select:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> Path:
    """Create the database file and every table. Safe to call repeatedly."""
    with tx() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
        )
    migrate()
    return db_path()


# Columns added after the first release; existing databases get them in place.
EXTRA_COLUMNS = (
    ("bugs", "steps_html", "TEXT"),
    ("bugs", "comments", "TEXT"),
    ("bugs", "attachments", "TEXT"),
    ("bugs", "product_id", "INTEGER"),
    ("bugs", "product_name", "TEXT"),
    ("bugs", "assigned_to_name", "TEXT"),
    ("bugs", "detail_synced_at", "TEXT"),
    ("svn_revisions", "files", "TEXT"),
    # Which draft commit an SVN revision was actually made from. Round two of a
    # reopened bug must diff against that commit, not against a stale mirror
    # trunk, or the first round's already-landed change looks like a conflict.
    ("svn_revisions", "git_commit", "TEXT NOT NULL DEFAULT ''"),
    ("need_solution", "done_at", "TEXT"),
    ("product_repos", "svn_working_copy", "TEXT NOT NULL DEFAULT ''"),
)


def migrate() -> list[str]:
    """Add missing columns without touching existing rows or statuses."""
    added: list[str] = []
    with tx() as conn:
        for table, column, ddl in EXTRA_COLUMNS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                added.append(f"{table}.{column}")
    return added


def ensure_db() -> None:
    """Make the schema match the code: every statement is `IF NOT EXISTS`."""
    init_db()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with tx(autocommit_select=True) as conn:
        cur = conn.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query_all(sql + " LIMIT 1", params) if "LIMIT" not in sql.upper() else query_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or 0


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_load(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


# ---------------------------------------------------------------------------
# bugs
# ---------------------------------------------------------------------------

def upsert_bug_from_zentao(bug: dict) -> tuple[int, str, int]:
    """Insert or refresh one Zentao bug.

    Returns ``(bug_id, 'created'|'updated', zentao_id)``.
    Existing workflow status is preserved; new bugs start as ``pending``.
    """
    ts = now_str()
    zentao_id = int(bug["zentao_id"])
    payload = {
        "zentao_id": zentao_id,
        "title": bug.get("title") or "",
        "severity": int(bug.get("severity") or 3),
        "pri": int(bug.get("pri") or 3),
        "steps": bug.get("steps") or "",
        "module": bug.get("module") or "",
        "assigned_to": bug.get("assigned_to") or "",
        "opened_by": bug.get("opened_by") or "",
        "opened_build": bug.get("opened_build") or "",
        "zentao_status": bug.get("zentao_status") or "",
        "zentao_resolution": bug.get("zentao_resolution") or "",
        "opened_date": bug.get("opened_date") or "",
        "product_id": int(bug.get("product_id") or 0) or None,
        "product_name": bug.get("product_name") or "",
        "raw_json": _json_dump(bug.get("raw")),
        "synced_at": ts,
    }
    with tx() as conn:
        row = conn.execute("SELECT id, status, branch FROM bugs WHERE zentao_id = ?", (zentao_id,)).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO bugs (zentao_id, title, severity, pri, status, steps, module,
                                  assigned_to, opened_by, opened_build, zentao_status,
                                  zentao_resolution, opened_date, product_id, product_name,
                                  raw_json, synced_at, created_at, updated_at)
                VALUES (:zentao_id, :title, :severity, :pri, 'pending', :steps, :module,
                        :assigned_to, :opened_by, :opened_build, :zentao_status,
                        :zentao_resolution, :opened_date, :product_id, :product_name,
                        :raw_json, :synced_at, :ts, :ts)
                """,
                {**payload, "ts": ts},
            )
            return cur.lastrowid, "created", zentao_id

        # Refresh Zentao-side fields only; never hijack the local workflow state.
        conn.execute(
            """
            UPDATE bugs
               SET title = :title, severity = :severity, pri = :pri, steps = :steps,
                   module = :module, assigned_to = :assigned_to, opened_by = :opened_by,
                   opened_build = :opened_build, zentao_status = :zentao_status,
                   zentao_resolution = :zentao_resolution, opened_date = :opened_date,
                   product_id = COALESCE(:product_id, product_id),
                   product_name = CASE WHEN :product_name = '' THEN product_name
                                       ELSE :product_name END,
                   raw_json = :raw_json, synced_at = :synced_at, updated_at = :ts
             WHERE zentao_id = :zentao_id
            """,
            {**payload, "ts": ts},
        )
        return row["id"], "updated", zentao_id


def get_bug(bug_id: int) -> dict | None:
    rows = query_all("SELECT * FROM bugs WHERE id = ?", (bug_id,))
    if not rows:
        return None
    return decorate_bug(rows[0])


def get_bug_by_zentao_id(zentao_id: int) -> dict | None:
    rows = query_all("SELECT * FROM bugs WHERE zentao_id = ?", (zentao_id,))
    if not rows:
        return None
    return decorate_bug(rows[0])


def decorate_bug(row: dict) -> dict:
    """Decode JSON columns and add human readable helpers."""
    row["files_changed"] = _json_load(row.get("files_changed")) or []
    row["comments"] = _json_load(row.get("comments")) or []
    row["attachments"] = _json_load(row.get("attachments")) or []
    all_pics = [
        a for a in row["attachments"]
        if str(a.get("ext", "")).lower() in IMAGE_EXTS and a.get("web_path")
    ]
    # A picture a vision model refuses (a 1x1 Zentao placeholder) is not something
    # the executor can look at, so it must not sit in the "must read the
    # screenshot" list; it stays visible on the page as a skipped one instead.
    row["images"] = [a for a in all_pics if not a.get("unreadable")]
    row["images_skipped"] = [a for a in all_pics if a.get("unreadable")]
    row["docs"] = [a for a in row["attachments"] if a not in all_pics]
    row["latest_revision"] = None
    revs = query_all(
        "SELECT revision, branch, message, author, git_commit, created_at"
        " FROM svn_revisions WHERE bug_id = ? ORDER BY id DESC",
        (row["id"],),
    )
    row["revisions"] = revs
    if revs:
        row["latest_revision"] = revs[0]["revision"]
    row["needs"] = query_all(
        "SELECT * FROM need_solution WHERE bug_id = ? ORDER BY id DESC", (row["id"],)
    )
    row["reviews"] = query_all(
        "SELECT * FROM reviews WHERE bug_id = ? ORDER BY id DESC", (row["id"],)
    )
    row["analyses"] = list_analyses(row["id"])
    # The executor's write-up is what the review is judged on. A manual note (for
    # instance a failed SVN push) is a trail, not a verdict, so it is exposed
    # separately instead of hiding the analysis underneath it.
    manual = [a for a in row["analyses"] if a.get("kind") == "manual"]
    primary = [a for a in row["analyses"] if a.get("kind") != "manual"]
    row["owner_notes"] = manual
    row["analysis"] = (primary or row["analyses"] or [None])[0]
    return row


def set_bug_status(zentao_id_or_id: int, status: str, by_zentao_id: bool = False, **fields: Any) -> dict | None:
    """Move a bug to ``status`` and optionally update extra columns."""
    if status not in BUG_STATUSES:
        raise ValueError(f"unknown bug status: {status}")
    ts = now_str()
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, ts]
    for key, value in fields.items():
        if key not in {"branch", "fix_summary", "verify_steps", "files_changed", "title"}:
            raise ValueError(f"column not editable here: {key}")
        if key == "files_changed":
            value = _json_dump(value)
        sets.append(f"{key} = ?")
        params.append(value)

    if by_zentao_id:
        params.append(int(zentao_id_or_id))
        where = "zentao_id = ?"
    else:
        params.append(int(zentao_id_or_id))
        where = "id = ?"

    with tx() as conn:
        conn.execute(f"UPDATE bugs SET {', '.join(sets)} WHERE {where}", params)
    key = "zentao_id" if by_zentao_id else "id"
    return query_one(f"SELECT * FROM bugs WHERE {key} = ?", (int(zentao_id_or_id),))


DETAIL_COLUMNS = {
    "steps", "steps_html", "comments", "attachments", "module", "product_id",
    "product_name", "assigned_to", "assigned_to_name", "opened_by", "opened_build",
    "severity", "pri", "title", "zentao_status", "zentao_resolution", "opened_date",
    "raw_json",
}


def update_bug_detail(bug_id: int, fields: dict[str, Any]) -> dict | None:
    """Store the enriched detail payload (steps HTML, notes, attachments)."""
    payload = {k: v for k, v in fields.items() if k in DETAIL_COLUMNS}
    if not payload:
        return get_bug(bug_id)
    for key in ("comments", "attachments", "raw_json"):
        if key in payload and not isinstance(payload[key], str):
            payload[key] = _json_dump(payload[key])
    payload["detail_synced_at"] = now_str()
    payload["updated_at"] = now_str()
    sets = ", ".join(f"{key} = :{key}" for key in payload)
    params = dict(payload, bug_id=int(bug_id))
    with tx() as conn:
        conn.execute(f"UPDATE bugs SET {sets} WHERE id = :bug_id", params)
    return get_bug(bug_id)


def kanban_columns() -> list[dict]:
    """Bugs grouped per status, ordered by pri asc / severity desc."""
    rows = query_all(
        f"""
        SELECT * FROM bugs
        ORDER BY CASE status
                    WHEN 'fixing' THEN 0
                    WHEN 'need_solution' THEN 1
                    WHEN 'rejected' THEN 2
                    WHEN 'pending' THEN 3
                    WHEN 'await_review' THEN 4
                    WHEN 'merged' THEN 5
                    ELSE 6 END,
                 pri ASC, severity DESC, updated_at DESC
        """
    )
    grouped: dict[str, list[dict]] = {status: [] for status in BUG_STATUSES}
    for row in rows:
        grouped.setdefault(row["status"], []).append(row)
    columns = []
    for status in BUG_STATUSES:
        items = grouped.get(status, [])
        for item in items:
            item["need_open"] = query_one(
                "SELECT COUNT(*) AS c FROM need_solution WHERE bug_id = ? AND status != 'done'",
                (item["id"],),
            )["c"]
        columns.append({"status": status, "count": len(items), "bugs": items})
    return columns


def fetch_task_queue(limit: int = 5) -> list[dict]:
    """Bugs the AI executor should work on.

    Priority order:
      1. bugs whose need_solution got an owner reply (``replied``)
      2. rejected bugs (review failed, redo) -- a bug that was already accepted
         once comes back as ``queue_kind='reopened'`` with ``prior_fix`` attached
      3. plain pending bugs
      4. stale ``fixing`` claims (a run killed by a model rate limit would
         otherwise strand them forever) -- see ``STALE_CLAIM_MINUTES``
    Sorted by pri asc then severity desc. A bug that already reached a terminal
    state is never queued again just because some old blocker got an answer.
    """
    stale = int(get_settings().stale_claim_minutes)
    stale_sql = ""
    params: list[Any] = []
    if stale > 0:
        # A claim older than the threshold means nobody is working on it any more.
        stale_sql = (
            "         OR (b.status = 'fixing' AND b.updated_at IS NOT NULL"
            "             AND b.updated_at < datetime('now', 'localtime', ?))\n"
        )
        params.append(f"-{stale} minutes")
    params.append(int(limit))
    sql = f"""
        SELECT b.*,
               (SELECT n.id FROM need_solution n
                 WHERE n.bug_id = b.id AND n.status = 'replied'
                 ORDER BY n.replied_at DESC LIMIT 1) AS open_need_id,
               CASE
                 WHEN (SELECT COUNT(*) FROM need_solution n
                        WHERE n.bug_id = b.id AND n.status = 'replied') > 0 THEN 0
                 WHEN b.status = 'rejected' THEN 1
                 WHEN b.status = 'fixing' THEN 3
                 ELSE 2 END AS queue_rank
          FROM bugs b
         WHERE b.status IN ('pending', 'rejected')
            OR (b.status NOT IN ('closed', 'merged', 'await_review')
                AND EXISTS (SELECT 1 FROM need_solution n
                            WHERE n.bug_id = b.id AND n.status = 'replied'))
{stale_sql}         ORDER BY queue_rank ASC, b.pri ASC, b.severity DESC, b.id ASC
         LIMIT ?
    """
    rows = query_all(sql, tuple(params))
    out = []
    for row in rows:
        row["files_changed"] = _json_load(row.get("files_changed")) or []
        if row.get("open_need_id"):
            row["owner_reply"] = query_one(
                "SELECT * FROM need_solution WHERE id = ?", (row["open_need_id"],)
            )
            row["queue_kind"] = "replied"
        elif row["status"] == "rejected":
            last_review = query_one(
                "SELECT * FROM reviews WHERE bug_id = ? ORDER BY id DESC", (row["id"],)
            )
            row["reject_reason"] = (last_review or {}).get("reject_reason")
            # A bug that was already accepted once is a reopen, not a plain reject:
            # the executor must inherit the previous round instead of re-deriving it.
            row["prior_fix"] = prior_fix(row["id"])
            row["queue_kind"] = "reopened" if row["prior_fix"]["was_merged"] else "rejected"
        elif row["status"] == "fixing":
            # Reclaimed: some earlier run claimed it and died before reporting.
            row["queue_kind"] = "stale"
        else:
            row["queue_kind"] = "pending"
        out.append(row)
    return out


def counts_by_status() -> dict[str, int]:
    rows = query_all("SELECT status, COUNT(*) AS c FROM bugs GROUP BY status")
    result = {status: 0 for status in BUG_STATUSES}
    for row in rows:
        result[row["status"]] = row["c"]
    return result


# ---------------------------------------------------------------------------
# need_solution
# ---------------------------------------------------------------------------

def create_need(bug_id: int, question: str, ai_options: str = "", ai_advice: str = "") -> dict:
    ts = now_str()
    need_id = execute(
        """
        INSERT INTO need_solution (bug_id, question, ai_options, ai_advice, status, created_at)
        VALUES (?, ?, ?, ?, 'awaiting', ?)
        """,
        (bug_id, question, ai_options, ai_advice, ts),
    )
    return query_one("SELECT * FROM need_solution WHERE id = ?", (need_id,))


def list_needs(status: str | None = "awaiting") -> list[dict]:
    if status == "all":
        sql = (
            "SELECT n.*, b.zentao_id, b.title, b.pri, b.severity, b.status AS bug_status, b.branch "
            "FROM need_solution n JOIN bugs b ON b.id = n.bug_id "
            "ORDER BY CASE n.status WHEN 'replied' THEN 0 WHEN 'awaiting' THEN 1 ELSE 2 END, n.id DESC"
        )
        params: tuple = ()
    else:
        sql = (
            "SELECT n.*, b.zentao_id, b.title, b.pri, b.severity, b.status AS bug_status, b.branch "
            "FROM need_solution n JOIN bugs b ON b.id = n.bug_id WHERE n.status = ? "
            "ORDER BY n.id DESC"
        )
        params = (status,)
    return query_all(sql, params)


def reply_need(need_id: int, owner_reply: str) -> dict | None:
    ts = now_str()
    with tx() as conn:
        row = conn.execute("SELECT * FROM need_solution WHERE id = ?", (need_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE need_solution SET owner_reply = ?, status = 'replied', replied_at = ? WHERE id = ?",
            (owner_reply, ts, need_id),
        )
        # Hand the bug back to the executor queue.
        conn.execute(
            "UPDATE bugs SET status = CASE WHEN status IN ('need_solution') THEN 'pending' ELSE status END,"
            " updated_at = ? WHERE id = ?",
            (ts, row["bug_id"]),
        )
    return query_one("SELECT * FROM need_solution WHERE id = ?", (need_id,))


def close_need(need_id: int) -> dict | None:
    ts = now_str()
    execute("UPDATE need_solution SET status = 'done', done_at = ? WHERE id = ?", (ts, need_id))
    return query_one("SELECT * FROM need_solution WHERE id = ?", (need_id,))


def close_open_needs(bug_id: int) -> int:
    """Settle every unanswered/replied blocker of one bug; returns how many.

    Called when a bug is closed by hand: the owner's verdict was "不修了", so the
    question must not keep sitting on the 需方案 page, and a ``replied`` blocker is
    itself a queue entry point (see ``fetch_task_queue``).
    """
    ts = now_str()
    with tx() as conn:
        cur = conn.execute(
            "UPDATE need_solution SET status = 'done', done_at = ?"
            " WHERE bug_id = ? AND status IN ('awaiting', 'replied')",
            (ts, int(bug_id)),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# svn_revisions
# ---------------------------------------------------------------------------

def add_revision(
    bug_id: int,
    revision: str,
    branch: str = "",
    message: str = "",
    author: str = "",
    files: list[str] | None = None,
    git_commit: str = "",
) -> dict:
    """Register one revision.

    ``git_commit`` records the draft commit the revision was made from; the next
    round of a reopened bug diffs against it instead of against the mirror trunk.
    """
    rev_id = execute(
        """
        INSERT INTO svn_revisions (bug_id, revision, branch, message, author, files,
                                  git_commit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (bug_id, str(revision), branch, message, author, _json_dump(files or []),
         str(git_commit or ""), now_str()),
    )
    execute("UPDATE bugs SET updated_at = ? WHERE id = ?", (now_str(), bug_id))
    return query_one("SELECT * FROM svn_revisions WHERE id = ?", (rev_id,))


def list_revisions_by_bug() -> list[dict]:
    """All revisions grouped per bug (multiple commits stay contiguous)."""
    rows = query_all(
        """
        SELECT r.*, b.zentao_id, b.title, b.status AS bug_status, b.branch
          FROM svn_revisions r
          JOIN bugs b ON b.id = r.bug_id
         ORDER BY b.id DESC, r.id ASC
        """
    )
    groups: dict[int, dict] = {}
    ordered: list[dict] = []
    for row in rows:
        row["files"] = _json_load(row.get("files")) or []
        key = row["bug_id"]
        if key not in groups:
            groups[key] = {
                "bug_id": key,
                "zentao_id": row["zentao_id"],
                "title": row["title"],
                "bug_status": row["bug_status"],
                "branch": row["branch"],
                "revisions": [],
            }
            ordered.append(groups[key])
        groups[key]["revisions"].append(row)
    return ordered


# ---------------------------------------------------------------------------
# reviews
# ---------------------------------------------------------------------------

def record_review(
    bug_id: int,
    result: str,
    reject_reason: str = "",
    merged_revision: str = "",
    reviewer: str = "owner",
    change_status: bool = True,
) -> dict:
    """Insert one review verdict.

    ``change_status=False`` keeps a ledger entry without moving the bug -- used by
    "reject and roll back", where the owner closes the bug instead of sending it
    back to the queue.
    """
    if result not in REVIEW_RESULTS:
        raise ValueError(f"unknown review result: {result}")
    ts = now_str()
    review_id = execute(
        """
        INSERT INTO reviews (bug_id, result, reject_reason, merged_revision, reviewer, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (bug_id, result, reject_reason, merged_revision, reviewer, ts),
    )
    if change_status:
        new_status = "merged" if result == "pass" else "rejected"
        execute("UPDATE bugs SET status = ?, updated_at = ? WHERE id = ?", (new_status, ts, bug_id))
    return query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))


def prior_fix(bug_id: int) -> dict:
    """Everything the previous round(s) of this bug produced, for the next round.

    A reopened bug must not start from scratch: the executor gets the earlier
    conclusion, the files that were touched, and the revision each round landed
    in (``git:<哈希>`` draft commits and real SVN numbers alike).
    """
    bug = query_one("SELECT * FROM bugs WHERE id = ?", (int(bug_id),))
    if bug is None:
        return {}
    revisions = query_all(
        """
        SELECT revision, branch, message, author, files, git_commit, created_at
          FROM svn_revisions WHERE bug_id = ? ORDER BY id ASC
        """,
        (int(bug_id),),
    )
    for row in revisions:
        row["files"] = _json_load(row.get("files")) or []
    analyses = query_all(
        """
        SELECT kind, conclusion, root_cause, change_desc, impact, verify, unverified, created_at
          FROM analyses WHERE bug_id = ? AND kind != 'manual' ORDER BY id DESC
        """,
        (int(bug_id),),
    )
    last = analyses[0] if analyses else None
    reviews = query_all(
        "SELECT result, reject_reason, merged_revision, created_at FROM reviews"
        " WHERE bug_id = ? ORDER BY id ASC",
        (int(bug_id),),
    )
    passes = [r for r in reviews if r["result"] == "pass"]
    rejects = [r for r in reviews if r["result"] == "reject"]
    promoted = [r for r in revisions if str(r["revision"]).isdigit()]
    return {
        "rounds": len(analyses),
        "branch": bug.get("branch") or "",
        "fix_summary": bug.get("fix_summary") or "",
        "verify_steps": bug.get("verify_steps") or "",
        "files_changed": _json_load(bug.get("files_changed")) or [],
        "revisions": revisions,
        "svn_revisions": [r["revision"] for r in promoted],
        "promoted_commit": (promoted[-1].get("git_commit") or "") if promoted else "",
        "promoted_at": promoted[-1]["created_at"] if promoted else "",
        "last_analysis": last,
        "was_merged": bool(passes),
        "merged_revision": (passes[-1].get("merged_revision") or "") if passes else "",
        "reject_reason": (rejects[-1].get("reject_reason") or "") if rejects else "",
    }


def reopen_bug(bug_id: int, reason: str, reviewer: str = "owner") -> dict:
    """Send an already merged (or closed) bug back into the queue.

    Nothing is undone: the real revision stays registered, the draft branch stays
    where it is, and the review ledger keeps both verdicts -- pass first, reject
    now -- so the next round can see what was accepted and what was found lacking.
    """
    bug = query_one("SELECT * FROM bugs WHERE id = ?", (int(bug_id),))
    if bug is None:
        raise ValueError(f"unknown bug id: {bug_id}")
    if bug["status"] not in ("merged", "closed"):
        raise ValueError(f"只有已合入或已结案的条目才能重开，当前是 {bug['status']}")
    text = str(reason or "").strip()
    if not text:
        raise ValueError("重开必须写清哪里不完全")
    record_review(int(bug_id), "reject", reject_reason=f"重开补修：{text}"[:500],
                  reviewer=reviewer)
    return query_one("SELECT * FROM bugs WHERE id = ?", (int(bug_id),)) or {}


def list_review_queue() -> list[dict]:
    rows = query_all(
        "SELECT * FROM bugs WHERE status = 'await_review' ORDER BY pri ASC, severity DESC, updated_at DESC"
    )
    return [decorate_bug(row) for row in rows]


# ---------------------------------------------------------------------------
# analyses -- the executor's written conclusion for one bug
# ---------------------------------------------------------------------------

ANALYSIS_FIELDS = (
    "symptom", "root_cause", "evidence", "call_chain", "change_desc",
    "impact", "verify", "unverified", "rollback", "conclusion",
)
ANALYSIS_KINDS = ("commit", "block", "manual")


def add_analysis(bug_id: int, fields: dict[str, Any], *, kind: str = "commit",
                 gates: dict[str, Any] | None = None, author: str = "ai") -> dict:
    """Store one analysis record; unknown keys are ignored so callers stay loose."""
    if kind not in ANALYSIS_KINDS:
        raise ValueError(f"unknown analysis kind: {kind}")
    ts = now_str()
    payload = {key: str(fields.get(key) or "").strip() for key in ANALYSIS_FIELDS}
    payload["gates"] = _json_dump(gates or {}) or "{}"
    payload.update(bug_id=int(bug_id), kind=kind, author=author or "ai", ts=ts)
    columns = ", ".join(["bug_id", "kind", *ANALYSIS_FIELDS, "gates", "author", "created_at"])
    holders = ", ".join([":bug_id", ":kind", *(f":{key}" for key in ANALYSIS_FIELDS),
                         ":gates", ":author", ":ts"])
    with tx() as conn:
        cur = conn.execute(f"INSERT INTO analyses ({columns}) VALUES ({holders})", payload)
        new_id = cur.lastrowid
    return _decode_analysis(query_one("SELECT * FROM analyses WHERE id = ?", (new_id,))) or {}


def _decode_analysis(row: dict | None) -> dict | None:
    """Expose the gate verdicts as a dict next to the raw JSON column."""
    if row is not None:
        row["gates_parsed"] = _json_load(row.get("gates")) or {}
    return row


def list_analyses(bug_id: int) -> list[dict]:
    rows = query_all("SELECT * FROM analyses WHERE bug_id = ? ORDER BY id DESC", (int(bug_id),))
    return [_decode_analysis(row) for row in rows]


def latest_analysis(bug_id: int) -> dict | None:
    return _decode_analysis(query_one(
        "SELECT * FROM analyses WHERE bug_id = ? ORDER BY id DESC LIMIT 1", (int(bug_id),)))


def has_analysis(bug_id: int) -> bool:
    row = query_one("SELECT COUNT(*) AS n FROM analyses WHERE bug_id = ?", (int(bug_id),))
    return bool(row and int(row["n"] or 0) > 0)


# ---------------------------------------------------------------------------
# product_repos -- one Zentao product == one code repository
# ---------------------------------------------------------------------------

REPO_COLUMNS = {
    "product_name", "repo_url", "trunk_path", "branch_root", "working_copy",
    "svn_working_copy",
    "branch_prefix", "build_command", "test_command", "note", "enabled",
}


def _repo_enabled(value: Any) -> int:
    """Normalize the enabled flag coming from CLI ints or HTML form strings."""
    return 0 if str(value).strip().lower() in ("", "0", "false", "off", "none") else 1


def bind_product_repo(product_id: int, fields: dict[str, Any]) -> dict:
    """Create or update the repository bound to a Zentao product.

    Only the keys present in `fields` are written. A partial dict must never blank the
    columns the caller omitted: that used to wipe product_name and silently flip
    enabled to 0, because every missing key defaulted to "".
    """
    unknown = sorted(set(fields) - REPO_COLUMNS)
    if unknown:
        raise ValueError(f"未知字段：{', '.join(unknown)}")
    if "repo_url" in fields and not str(fields.get("repo_url") or "").strip():
        raise ValueError("绑定产品代码库必须提供非空 repo_url")

    ts = now_str()
    pid = int(product_id)
    # A None value means "not provided": writing it would hit a NOT NULL column, and a
    # caller copying an existing row may well carry None for columns it does not know.
    updates = {key: fields[key] for key in REPO_COLUMNS
               if key in fields and fields[key] is not None}
    if "enabled" in updates:
        updates["enabled"] = _repo_enabled(updates["enabled"])

    with tx() as conn:
        exists = conn.execute(
            "SELECT product_id FROM product_repos WHERE product_id = ?", (pid,)
        ).fetchone()
        if exists is None:
            if not str(updates.get("repo_url") or "").strip():
                raise ValueError("新建绑定必须提供 repo_url")
            row = {key: updates.get(key, "") for key in REPO_COLUMNS}
            row["enabled"] = updates.get("enabled", 1)
            conn.execute(
                """
                INSERT INTO product_repos (product_id, product_name, repo_url, trunk_path,
                                           branch_root, working_copy, svn_working_copy,
                                           branch_prefix, build_command, test_command,
                                           note, enabled, created_at, updated_at)
                VALUES (:product_id, :product_name, :repo_url, :trunk_path, :branch_root,
                        :working_copy, :svn_working_copy, :branch_prefix, :build_command,
                        :test_command, :note, :enabled, :ts, :ts)
                """,
                {**row, "product_id": pid, "ts": ts},
            )
        elif updates:
            sets = ", ".join(f"{key} = :{key}" for key in updates)
            conn.execute(
                f"UPDATE product_repos SET {sets}, updated_at = :ts WHERE product_id = :pid",
                {**updates, "ts": ts, "pid": pid},
            )
    return get_product_repo(pid) or {}


def get_product_repo(product_id: int | None) -> dict | None:
    if not product_id:
        return None
    return query_one("SELECT * FROM product_repos WHERE product_id = ?", (int(product_id),))


def list_product_repos() -> list[dict]:
    return query_all("SELECT * FROM product_repos ORDER BY product_id")


def unbind_product_repo(product_id: int) -> None:
    execute("DELETE FROM product_repos WHERE product_id = ?", (int(product_id),))


def products_in_use() -> list[dict]:
    """Products present in the bug table, with per-product bug counts."""
    return query_all(
        """
        SELECT COALESCE(product_id, 0) AS product_id,
               MAX(COALESCE(product_name, '')) AS product_name,
               COUNT(*) AS bugs,
               SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
          FROM bugs
         GROUP BY COALESCE(product_id, 0)
         ORDER BY bugs DESC, product_id ASC
        """
    )


# ---------------------------------------------------------------------------
# sync_log & meta
# ---------------------------------------------------------------------------

def log_sync(action: str, zentao_id: int | None, payload: Any) -> None:
    if action not in SYNC_ACTIONS:
        action = "update"
    execute(
        "INSERT INTO sync_log (action, zentao_id, payload, created_at) VALUES (?, ?, ?, ?)",
        (action, zentao_id, _json_dump(payload) or "{}", now_str()),
    )


def list_sync_log(limit: int = 30) -> list[dict]:
    rows = query_all("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (int(limit),))
    for row in rows:
        row["payload_obj"] = _json_load(row.get("payload"))
    return rows


def last_sync() -> dict | None:
    return query_one("SELECT * FROM sync_log WHERE action IN ('sync', 'error') ORDER BY id DESC")


def get_meta(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
