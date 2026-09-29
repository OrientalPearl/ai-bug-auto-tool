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
from dataclasses import dataclass
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

# What a Zentao item is. A bug and a task run the exact same local workflow
# (pending -> fixing -> need_solution -> await_review -> merged/closed), but they
# are two ID sequences in Zentao: task #5417 and bug #5417 can both exist. So the
# two live in separate tables instead of one table with a discriminator, and every
# child record hangs off its own parent. The pages union both kinds.
ITEM_KINDS = ("bug", "task")

# A need on a task is either the plan the owner has to approve (the first pass of
# a task is plan-only -- see AUTO_LOOP.md G13) or a real blocker hit while coding.
NEED_KINDS = ("block", "plan")


@dataclass(frozen=True)
class ItemTables:
    """Table names for one item kind; `fk` is the child column pointing at item.id."""

    item: str
    need: str
    revision: str
    review: str
    analysis: str
    fk: str
    kind: str


ITEM_TABLES = {
    "bug": ItemTables("bugs", "need_solution", "svn_revisions", "reviews",
                      "analyses", "bug_id", "bug"),
    "task": ItemTables("tasks", "task_needs", "task_revisions", "task_reviews",
                       "task_analyses", "task_id", "task"),
}


def tables_for(target: str = "bug") -> ItemTables:
    """Resolve the table set for an item kind; an unknown kind is a bug in the caller."""
    if target not in ITEM_TABLES:
        raise ValueError(f"unknown item kind: {target}")
    return ITEM_TABLES[target]


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

-- ---------------------------------------------------------------------
-- Zentao tasks (任务). Same workflow as bugs, own tables: a task number and a
-- bug number are different sequences and may collide. There is no `severity`
-- (Zentao does not rate a task) and the queue orders tasks by pri only.
-- `steps` holds the requirement text: the task description plus the linked
-- story's spec, which is where the real detail lives.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zentao_id       INTEGER NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    pri             INTEGER NOT NULL DEFAULT 3,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ({_STATUS_SQL})),
    branch          TEXT,
    steps           TEXT,
    steps_html      TEXT,
    module          TEXT,
    project_id      INTEGER,
    project_name    TEXT,
    story_id        INTEGER,
    story_title     TEXT,
    assigned_to     TEXT,
    assigned_to_name TEXT,
    opened_by       TEXT,
    opened_date     TEXT,
    zentao_status   TEXT,
    deadline        TEXT,
    estimate        TEXT,
    need_confirm    INTEGER NOT NULL DEFAULT 0,
    comments        TEXT,
    attachments     TEXT,
    product_id      INTEGER,
    product_name    TEXT,
    detail_synced_at TEXT,
    fix_summary     TEXT,
    verify_steps    TEXT,
    files_changed   TEXT,
    raw_json        TEXT,
    synced_at       TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_queue  ON tasks(pri, id);

CREATE TABLE IF NOT EXISTS task_needs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    -- 'plan' = the change proposal the owner must approve before any code is
    -- written (G13); 'block' = a blocker hit while implementing.
    need_kind   TEXT    NOT NULL DEFAULT 'plan'
                CHECK (need_kind IN ('block', 'plan')),
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
CREATE INDEX IF NOT EXISTS idx_tneed_task   ON task_needs(task_id);
CREATE INDEX IF NOT EXISTS idx_tneed_status ON task_needs(status);

CREATE TABLE IF NOT EXISTS task_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision   TEXT    NOT NULL,
    branch     TEXT,
    message    TEXT    NOT NULL DEFAULT '',
    author     TEXT,
    files      TEXT,
    git_commit TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trev_task ON task_revisions(task_id);

CREATE TABLE IF NOT EXISTS task_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    result          TEXT    NOT NULL CHECK (result IN ('{REVIEW_RESULTS[0]}', '{REVIEW_RESULTS[1]}')),
    reject_reason   TEXT,
    merged_revision TEXT,
    reviewer        TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_treview_task ON task_reviews(task_id);

CREATE TABLE IF NOT EXISTS task_analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL DEFAULT 'commit'
                 CHECK (kind IN ('commit', 'block', 'plan', 'manual')),
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
CREATE INDEX IF NOT EXISTS idx_tanalysis_task ON task_analyses(task_id);
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


def upsert_task_from_zentao(task: dict) -> tuple[int, str, int]:
    """Insert or refresh one Zentao task.

    Returns ``(task_id, 'created'|'updated', zentao_id)``. Same rule as bugs: the
    Zentao side owns the description, the local workflow state is never hijacked.
    """
    ts = now_str()
    zentao_id = int(task["zentao_id"])
    payload = {
        "zentao_id": zentao_id,
        "title": task.get("title") or "",
        "pri": int(task.get("pri") or 3),
        "steps": task.get("steps") or "",
        "module": task.get("module") or "",
        "project_id": int(task.get("project_id") or 0) or None,
        "project_name": task.get("project_name") or "",
        "story_id": int(task.get("story_id") or 0) or None,
        "story_title": task.get("story_title") or "",
        "assigned_to": task.get("assigned_to") or "",
        "opened_by": task.get("opened_by") or "",
        "opened_date": task.get("opened_date") or "",
        "zentao_status": task.get("zentao_status") or "",
        "deadline": task.get("deadline") or "",
        "estimate": str(task.get("estimate") or ""),
        "need_confirm": 1 if task.get("need_confirm") else 0,
        "product_id": int(task.get("product_id") or 0) or None,
        "product_name": task.get("product_name") or "",
        "raw_json": _json_dump(task.get("raw")),
        "synced_at": ts,
    }
    with tx() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE zentao_id = ?", (zentao_id,)).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO tasks (zentao_id, title, pri, status, steps, module,
                                   project_id, project_name, story_id, story_title,
                                   assigned_to, opened_by, opened_date, zentao_status,
                                   deadline, estimate, need_confirm, product_id,
                                   product_name, raw_json, synced_at, created_at, updated_at)
                VALUES (:zentao_id, :title, :pri, 'pending', :steps, :module,
                        :project_id, :project_name, :story_id, :story_title,
                        :assigned_to, :opened_by, :opened_date, :zentao_status,
                        :deadline, :estimate, :need_confirm, :product_id,
                        :product_name, :raw_json, :synced_at, :ts, :ts)
                """,
                {**payload, "ts": ts},
            )
            return cur.lastrowid, "created", zentao_id

        conn.execute(
            """
            UPDATE tasks
               SET title = :title, pri = :pri, steps = :steps, module = :module,
                   project_id = COALESCE(:project_id, project_id),
                   project_name = CASE WHEN :project_name = '' THEN project_name
                                       ELSE :project_name END,
                   story_id = COALESCE(:story_id, story_id),
                   story_title = CASE WHEN :story_title = '' THEN story_title
                                      ELSE :story_title END,
                   assigned_to = :assigned_to, opened_by = :opened_by,
                   opened_date = :opened_date,
                   zentao_status = :zentao_status, deadline = :deadline,
                   estimate = :estimate, need_confirm = :need_confirm,
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
    return get_item(bug_id, "bug")


def get_bug_by_zentao_id(zentao_id: int) -> dict | None:
    return get_item_by_zentao_id(zentao_id, "bug")


def get_item(item_id: int, target: str = "bug") -> dict | None:
    """One item by its local (per-table) primary key."""
    row = query_one(f"SELECT * FROM {tables_for(target).item} WHERE id = ?", (int(item_id),))
    return decorate_item(row, target) if row else None


def get_item_by_zentao_id(zentao_id: int, target: str = "bug") -> dict | None:
    """One item by its Zentao number, which is unique only inside its own kind."""
    row = query_one(f"SELECT * FROM {tables_for(target).item} WHERE zentao_id = ?",
                    (int(zentao_id),))
    return decorate_item(row, target) if row else None


def parse_ref(ref: Any, default: str = "bug") -> tuple[str, int]:
    """Split an executor-facing reference into ``(target, zentao_id)``.

    ``41468`` is a bug (unchanged behaviour), ``T5417`` is task 5417, and
    ``B5417`` spells out a bug. Both a task and a bug can be number 5417, so a
    bare number must keep its old meaning instead of guessing.
    """
    text = str(ref or "").strip()
    if not text:
        raise ValueError("空的条目编号")
    head, digits = text[0], text[1:]
    if head in ("T", "t") and digits.isdigit():
        return "task", int(digits)
    if head in ("B", "b") and digits.isdigit():
        return "bug", int(digits)
    if text.isdigit():
        return default, int(text)
    raise ValueError(f"无法识别的条目编号：{ref}（任务请写成 T5417）")


def item_slug(zentao_id: Any, target: str = "bug") -> str:
    """Number that is unique across kinds: a task is ``T5417``, a bug stays ``5417``.

    Used for the attachment folder and the draft branch name, the two places where
    a bug and a task with the same Zentao number would otherwise collide.
    """
    digits = str(zentao_id or "").strip()
    if target == "task" and not digits.upper().startswith("T"):
        return f"T{digits}"
    return digits


def draft_branch(zentao_id: Any, target: str = "bug", prefix: str | None = None) -> str:
    """The draft branch an item's work belongs on (``bugfix/zentao-T5417`` etc.)."""
    base = str(prefix or get_settings().branch_prefix or "bugfix/zentao-")
    return f"{base}{item_slug(zentao_id, target)}"


def decorate_bug(row: dict) -> dict:
    return decorate_item(row, "bug")


def decorate_item(row: dict, target: str = "bug") -> dict:
    """Decode JSON columns and add human readable helpers."""
    tab = tables_for(target)
    row["target"] = tab.kind
    row.setdefault("severity", None)
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
        f" FROM {tab.revision} WHERE {tab.fk} = ? ORDER BY id DESC",
        (row["id"],),
    )
    row["revisions"] = revs
    if revs:
        row["latest_revision"] = revs[0]["revision"]
    row["needs"] = query_all(
        f"{_need_columns(tab)} WHERE {tab.fk} = ? ORDER BY id DESC", (row["id"],)
    )
    row["reviews"] = query_all(
        f"SELECT * FROM {tab.review} WHERE {tab.fk} = ? ORDER BY id DESC", (row["id"],)
    )
    row["analyses"] = list_analyses(row["id"], target)
    # The executor's write-up is what the review is judged on. A manual note (for
    # instance a failed SVN push) is a trail, not a verdict, so it is exposed
    # separately instead of hiding the analysis underneath it.
    manual = [a for a in row["analyses"] if a.get("kind") == "manual"]
    primary = [a for a in row["analyses"] if a.get("kind") != "manual"]
    row["owner_notes"] = manual
    row["analysis"] = (primary or row["analyses"] or [None])[0]
    # A task may not be implemented until the owner has approved a plan (G13);
    # every consumer reads this instead of re-deriving it from the needs list.
    row["plan_approved"] = (approved_plan(row["id"], target) is not None)
    return row


def set_bug_status(zentao_id_or_id: int, status: str, by_zentao_id: bool = False,
                   **fields: Any) -> dict | None:
    return set_item_status(zentao_id_or_id, status, by_zentao_id=by_zentao_id,
                           target="bug", **fields)


EDITABLE_FIELDS = {"branch", "fix_summary", "verify_steps", "files_changed", "title"}


def set_item_status(zentao_id_or_id: int, status: str, *, by_zentao_id: bool = False,
                    target: str = "bug", **fields: Any) -> dict | None:
    """Move an item to ``status`` and optionally update extra columns."""
    if status not in BUG_STATUSES:
        raise ValueError(f"unknown item status: {status}")
    item = tables_for(target).item
    ts = now_str()
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, ts]
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            raise ValueError(f"column not editable here: {key}")
        if key == "files_changed":
            value = _json_dump(value)
        sets.append(f"{key} = ?")
        params.append(value)

    params.append(int(zentao_id_or_id))
    where = "zentao_id = ?" if by_zentao_id else "id = ?"

    with tx() as conn:
        conn.execute(f"UPDATE {item} SET {', '.join(sets)} WHERE {where}", params)
    key = "zentao_id" if by_zentao_id else "id"
    return query_one(f"SELECT * FROM {item} WHERE {key} = ?", (int(zentao_id_or_id),))


DETAIL_COLUMNS = {
    "steps", "steps_html", "comments", "attachments", "module", "product_id",
    "product_name", "assigned_to", "assigned_to_name", "opened_by", "opened_build",
    "severity", "pri", "title", "zentao_status", "zentao_resolution", "opened_date",
    "raw_json",
}

TASK_DETAIL_COLUMNS = DETAIL_COLUMNS | {
    "project_id", "project_name", "story_id", "story_title", "deadline",
    "estimate", "need_confirm",
}
TASK_DETAIL_COLUMNS.discard("severity")
TASK_DETAIL_COLUMNS.discard("opened_build")
TASK_DETAIL_COLUMNS.discard("zentao_resolution")


def update_bug_detail(bug_id: int, fields: dict[str, Any]) -> dict | None:
    return update_item_detail(bug_id, fields, "bug")


def update_item_detail(item_id: int, fields: dict[str, Any],
                       target: str = "bug") -> dict | None:
    """Store the enriched detail payload (steps HTML, notes, attachments)."""
    allowed = DETAIL_COLUMNS if target == "bug" else TASK_DETAIL_COLUMNS
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_item(item_id, target)
    for key in ("comments", "attachments", "raw_json"):
        if key in payload and not isinstance(payload[key], str):
            payload[key] = _json_dump(payload[key])
    payload["detail_synced_at"] = now_str()
    payload["updated_at"] = now_str()
    sets = ", ".join(f"{key} = :{key}" for key in payload)
    params = dict(payload, item_id=int(item_id))
    with tx() as conn:
        conn.execute(
            f"UPDATE {tables_for(target).item} SET {sets} WHERE id = :item_id", params)
    return get_item(item_id, target)


def kanban_columns(targets: Iterable[str] = ITEM_KINDS) -> list[dict]:
    """Items grouped per status, ordered by pri asc / severity desc.

    Bugs and tasks share the columns; each card carries ``target`` so the page can
    badge it and open the right detail endpoint.
    """
    rows: list[dict] = []
    for target in targets:
        tab = tables_for(target)
        severity_sql = "severity DESC, " if target == "bug" else ""
        found = query_all(
            f"""
            SELECT *, '{tab.kind}' AS target FROM {tab.item}
             ORDER BY CASE status
                         WHEN 'fixing' THEN 0
                         WHEN 'need_solution' THEN 1
                         WHEN 'rejected' THEN 2
                         WHEN 'pending' THEN 3
                         WHEN 'await_review' THEN 4
                         WHEN 'merged' THEN 5
                         ELSE 6 END,
                      pri ASC, {severity_sql}updated_at DESC
            """
        )
        rows.extend(found)
    grouped: dict[str, list[dict]] = {status: [] for status in BUG_STATUSES}
    for row in rows:
        grouped.setdefault(row["status"], []).append(row)
    columns = []
    for status in BUG_STATUSES:
        items = grouped.get(status, [])
        for item in items:
            tab = tables_for(item["target"])
            item["need_open"] = query_one(
                f"SELECT COUNT(*) AS c FROM {tab.need}"
                f" WHERE {tab.fk} = ? AND status != 'done'",
                (item["id"],),
            )["c"]
        columns.append({"status": status, "count": len(items), "bugs": items})
    return columns


def approved_plan(item_id: int, target: str = "task") -> dict | None:
    """The plan the owner has already answered for a task (G13).

    A task must not be implemented before this exists: an unanswered plan is not an
    approval, and a ``block``-type need never counts as one.
    """
    if target != "task":
        return None
    return query_one(
        "SELECT * FROM task_needs WHERE task_id = ? AND need_kind = 'plan'"
        " AND status = 'replied' AND owner_reply IS NOT NULL AND owner_reply != ''"
        " ORDER BY replied_at DESC LIMIT 1",
        (int(item_id),),
    )


def plan_gate_reason(task: dict) -> str:
    """Why a task may not be touched yet; empty means the gate is open."""
    if task.get("target") != "task" and task.get("kind") != "task":
        return ""
    if approved_plan(task["id"], "task"):
        return ""
    open_plan = query_one(
        "SELECT * FROM task_needs WHERE task_id = ? AND need_kind = 'plan'"
        " AND status = 'awaiting' ORDER BY id DESC",
        (int(task["id"]),),
    )
    if open_plan:
        return (f"#{task['zentao_id']} 是任务：方案（need#{open_plan['id']}）还等主人确认，"
                "确认前不许落码（AUTO_LOOP.md G13）。请登记方案后继续下一条")
    return (f"#{task['zentao_id']} 是任务：首轮只许给方案与修改细则，不许直接落码"
            "（AUTO_LOOP.md G13）。用 `plan` 命令登记方案后继续下一条")


def _queue_for(target: str, limit: int, stale_minutes: int) -> list[dict]:
    """The executor queue of one item kind."""
    tab = tables_for(target)
    stale_sql = ""
    params: list[Any] = []
    if stale_minutes > 0:
        # A claim older than the threshold means nobody is working on it any more.
        stale_sql = ("         OR (i.status = 'fixing' AND i.updated_at IS NOT NULL"
                     "             AND i.updated_at < datetime('now', 'localtime', ?))\n")
        params.append(f"-{stale_minutes} minutes")
    params.append(int(limit))
    severity_sql = "i.severity DESC, " if target == "bug" else ""
    # An answered plan counts as "ready to implement" only when it carries the
    # owner's words; an unanswered one is still waiting on the owner, and a
    # blocker (`block`) always counts, exactly like a bug's need_solution.
    replied_cond = ("1 = 1" if target == "bug" else
                    "(n.need_kind = 'block'"
                    " OR (n.need_kind = 'plan'"
                    "     AND n.owner_reply IS NOT NULL AND n.owner_reply != ''))")
    sql = f"""
        SELECT i.*, '{tab.kind}' AS target,
               (SELECT n.id FROM {tab.need} n
                 WHERE n.{tab.fk} = i.id AND n.status = 'replied'
                 ORDER BY n.replied_at DESC LIMIT 1) AS open_need_id,
               CASE
                 WHEN EXISTS (SELECT 1 FROM {tab.need} n
                               WHERE n.{tab.fk} = i.id AND n.status = 'replied'
                                 AND {replied_cond}) THEN 0
                 WHEN i.status = 'rejected' THEN 1
                 WHEN i.status = 'fixing' THEN 3
                 ELSE 2 END AS queue_rank
          FROM {tab.item} i
         WHERE i.status IN ('pending', 'rejected')
            OR (i.status NOT IN ('closed', 'merged', 'await_review')
                AND EXISTS (SELECT 1 FROM {tab.need} n
                            WHERE n.{tab.fk} = i.id AND n.status = 'replied'))
{stale_sql}         ORDER BY queue_rank ASC, i.pri ASC, {severity_sql}i.id ASC
         LIMIT ?
    """
    rows = query_all(sql, tuple(params))
    out = []
    for row in rows:
        row["files_changed"] = _json_load(row.get("files_changed")) or []
        row["plan_approved"] = approved_plan(row["id"], target) is not None
        if row.get("open_need_id"):
            row["owner_reply"] = query_one(
                f"SELECT * FROM {tab.need} WHERE id = ?", (row["open_need_id"],)
            )
            kind = (row["owner_reply"] or {}).get("need_kind") or "block"
            row["queue_kind"] = "plan_approved" if kind == "plan" else "replied"
        elif row["status"] == "rejected":
            last_review = query_one(
                f"SELECT * FROM {tab.review} WHERE {tab.fk} = ? ORDER BY id DESC",
                (row["id"],),
            )
            row["reject_reason"] = (last_review or {}).get("reject_reason")
            # An item that was already accepted once is a reopen, not a plain reject:
            # the executor must inherit the previous round instead of re-deriving it.
            row["prior_fix"] = prior_fix(row["id"], target)
            row["queue_kind"] = "reopened" if row["prior_fix"]["was_merged"] else "rejected"
        elif row["status"] == "fixing":
            # Reclaimed: some earlier run claimed it and died before reporting.
            row["queue_kind"] = "stale"
        elif target == "task":
            # First pass of a task: proposal only, never code.
            row["queue_kind"] = "plan_needed"
        else:
            row["queue_kind"] = "pending"
        out.append(row)
    return out


def fetch_task_queue(limit: int = 5,
                     targets: Iterable[str] = ITEM_KINDS) -> list[dict]:
    """Items the AI executor should work on, bugs and tasks together.

    Priority order:
      1. items whose need_solution got an owner reply (``replied``), or whose task
         plan was approved (``plan_approved``)
      2. rejected items (review failed, redo) -- an item that was already accepted
         once comes back as ``queue_kind='reopened'`` with ``prior_fix`` attached
      3. plain pending items, and tasks awaiting their first plan
         (``queue_kind='plan_needed'``)
      4. stale ``fixing`` claims (a run killed by a model rate limit would
         otherwise strand them forever) -- see ``STALE_CLAIM_MINUTES``
    Sorted by pri asc then severity desc (severity is a bug-only field). An item
    that already reached a terminal state is never queued again just because some
    old blocker got an answer.
    """
    stale = int(get_settings().stale_claim_minutes)
    merged: list[dict] = []
    for target in targets:
        merged.extend(_queue_for(target, limit, stale))
    merged.sort(key=lambda r: (r["queue_rank"], r["pri"],
                               -(r.get("severity") or 0), r["id"]))
    return merged[:int(limit)]


def counts_by_status(targets: Iterable[str] = ITEM_KINDS) -> dict[str, int]:
    result = {status: 0 for status in BUG_STATUSES}
    for target in targets:
        for row in query_all(
                f"SELECT status, COUNT(*) AS c FROM {tables_for(target).item} GROUP BY status"):
            result[row["status"]] = result.get(row["status"], 0) + row["c"]
    return result


# ---------------------------------------------------------------------------
# needs -- a question the owner has to answer: a blocker, or a task's plan
# ---------------------------------------------------------------------------

def _need_columns(tab: ItemTables) -> str:
    """Bug needs have no ``need_kind`` column; project a constant so rows match."""
    extra = "need_kind" if tab.kind == "task" else "'block' AS need_kind"
    return f"SELECT *, {extra} FROM {tab.need}"


def create_need(bug_id: int, question: str, ai_options: str = "", ai_advice: str = "",
                *, target: str = "bug", need_kind: str = "block") -> dict:
    """Register one question for the owner.

    ``need_kind='plan'`` is task-only: it is the change proposal the owner must
    approve before the executor is allowed to write code (AUTO_LOOP.md G13).
    """
    tab = tables_for(target)
    if need_kind not in NEED_KINDS:
        raise ValueError(f"unknown need kind: {need_kind}")
    if target == "bug" and need_kind != "block":
        raise ValueError("缺陷只能登记阻塞项，方案（plan）只属于任务")
    ts = now_str()
    if tab.kind == "bug":
        need_id = execute(
            "INSERT INTO need_solution (bug_id, question, ai_options, ai_advice,"
            " status, created_at) VALUES (?, ?, ?, ?, 'awaiting', ?)",
            (bug_id, question, ai_options, ai_advice, ts),
        )
    else:
        need_id = execute(
            "INSERT INTO task_needs (task_id, need_kind, question, ai_options,"
            " ai_advice, status, created_at) VALUES (?, ?, ?, ?, ?, 'awaiting', ?)",
            (bug_id, need_kind, question, ai_options, ai_advice, ts),
        )
    return query_one(f"{_need_columns(tab)} WHERE id = ?", (need_id,))


def get_need(need_id: int, target: str = "bug") -> dict | None:
    tab = tables_for(target)
    return query_one(f"{_need_columns(tab)} WHERE id = ?", (int(need_id),))


def list_needs(status: str | None = "awaiting",
               targets: Iterable[str] = ITEM_KINDS) -> list[dict]:
    """Needs of both kinds, newest decision first, each tagged with its ``target``."""
    parts: list[str] = []
    params: list[Any] = []
    for target in targets:
        tab = tables_for(target)
        severity = "i.severity" if target == "bug" else "NULL AS severity"
        need_kind = "n.need_kind" if tab.kind == "task" else "'block' AS need_kind"
        where = "WHERE n.status = ?" if status not in (None, "all") else ""
        # Both tables must project the exact same column list: a UNION compares
        # positions, and task_needs carries one column need_solution does not.
        parts.append(
            f"""
            SELECT n.id, n.{tab.fk} AS item_id, n.question, n.ai_options, n.ai_advice,
                   n.owner_reply, n.status, n.created_at, n.replied_at, n.done_at,
                   {need_kind}, '{tab.kind}' AS target,
                   i.zentao_id, i.title, i.pri, {severity},
                   i.status AS item_status, i.branch
              FROM {tab.need} n JOIN {tab.item} i ON i.id = n.{tab.fk}
              {where}
            """
        )
        if where:
            params.append(status)
    # A compound SELECT may only order by its own output columns, so the CASE has to
    # live in an outer query.
    sql = ("SELECT * FROM (" + " UNION ALL ".join(parts) + ") ORDER BY"
           " CASE status WHEN 'replied' THEN 0 WHEN 'awaiting' THEN 1 ELSE 2 END,"
           " created_at DESC")
    return query_all(sql, params)


def reply_need(need_id: int, owner_reply: str, target: str = "bug") -> dict | None:
    """Answer one need and hand the item back to the executor queue."""
    tab = tables_for(target)
    ts = now_str()
    with tx() as conn:
        row = conn.execute(f"SELECT * FROM {tab.need} WHERE id = ?",
                           (int(need_id),)).fetchone()
        if row is None:
            return None
        conn.execute(
            f"UPDATE {tab.need} SET owner_reply = ?, status = 'replied', replied_at = ?"
            " WHERE id = ?",
            (owner_reply, ts, need_id),
        )
        conn.execute(
            f"UPDATE {tab.item} SET status ="
            " CASE WHEN status IN ('need_solution') THEN 'pending' ELSE status END,"
            " updated_at = ? WHERE id = ?",
            (ts, row[tab.fk]),
        )
    return query_one(f"{_need_columns(tab)} WHERE id = ?", (need_id,))


def close_need(need_id: int, target: str = "bug") -> dict | None:
    tab = tables_for(target)
    ts = now_str()
    execute(f"UPDATE {tab.need} SET status = 'done', done_at = ? WHERE id = ?",
            (ts, int(need_id)))
    return query_one(f"{_need_columns(tab)} WHERE id = ?", (need_id,))


def close_open_needs(bug_id: int, target: str = "bug") -> int:
    """Settle every unanswered/replied need of one item; returns how many.

    Called when an item is closed by hand: the owner's verdict was "不修了", so the
    question must not keep sitting on the 需方案 page, and a ``replied`` need is
    itself a queue entry point (see ``fetch_task_queue``).
    """
    tab = tables_for(target)
    ts = now_str()
    with tx() as conn:
        cur = conn.execute(
            f"UPDATE {tab.need} SET status = 'done', done_at = ?"
            f" WHERE {tab.fk} = ? AND status IN ('awaiting', 'replied')",
            (ts, int(bug_id)),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# revisions
# ---------------------------------------------------------------------------

def add_revision(
    bug_id: int,
    revision: str,
    branch: str = "",
    message: str = "",
    author: str = "",
    files: list[str] | None = None,
    git_commit: str = "",
    target: str = "bug",
) -> dict:
    """Register one revision.

    ``git_commit`` records the draft commit the revision was made from; the next
    round of a reopened item diffs against it instead of against the mirror trunk.
    """
    tab = tables_for(target)
    rev_id = execute(
        f"""
        INSERT INTO {tab.revision} ({tab.fk}, revision, branch, message, author, files,
                                    git_commit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (bug_id, str(revision), branch, message, author, _json_dump(files or []),
         str(git_commit or ""), now_str()),
    )
    execute(f"UPDATE {tab.item} SET updated_at = ? WHERE id = ?", (now_str(), bug_id))
    return query_one(f"SELECT * FROM {tab.revision} WHERE id = ?", (rev_id,))


def list_revisions_by_bug(targets: Iterable[str] = ITEM_KINDS) -> list[dict]:
    """All revisions grouped per item (multiple commits stay contiguous)."""
    rows: list[dict] = []
    for target in targets:
        tab = tables_for(target)
        # Explicit projection: a UNION matches by position, and the two tables name
        # their parent column differently (bug_id / task_id).
        rows.extend(query_all(
            f"""
            SELECT r.id, r.{tab.fk} AS item_id, r.revision, r.branch, r.message,
                   r.author, r.files, r.git_commit, r.created_at,
                   '{tab.kind}' AS target,
                   i.zentao_id, i.title, i.status AS item_status,
                   i.branch AS item_branch
              FROM {tab.revision} r
              JOIN {tab.item} i ON i.id = r.{tab.fk}
             ORDER BY i.id DESC, r.id ASC
            """
        ))
    groups: dict[tuple[str, int], dict] = {}
    ordered: list[dict] = []
    for row in rows:
        row["files"] = _json_load(row.get("files")) or []
        key = (row["target"], row["item_id"])
        if key not in groups:
            groups[key] = {
                "item_id": key[1],
                "target": key[0],
                "zentao_id": row["zentao_id"],
                "title": row["title"],
                "item_status": row["item_status"],
                "branch": row["item_branch"],
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
    target: str = "bug",
) -> dict:
    """Insert one review verdict.

    ``change_status=False`` keeps a ledger entry without moving the item -- used by
    "reject and roll back", where the owner closes it instead of sending it back to
    the queue.
    """
    if result not in REVIEW_RESULTS:
        raise ValueError(f"unknown review result: {result}")
    tab = tables_for(target)
    ts = now_str()
    review_id = execute(
        f"""
        INSERT INTO {tab.review} ({tab.fk}, result, reject_reason, merged_revision,
                                  reviewer, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (bug_id, result, reject_reason, merged_revision, reviewer, ts),
    )
    if change_status:
        new_status = "merged" if result == "pass" else "rejected"
        execute(f"UPDATE {tab.item} SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, ts, bug_id))
    return query_one(f"SELECT * FROM {tab.review} WHERE id = ?", (review_id,))


def prior_fix(bug_id: int, target: str = "bug") -> dict:
    """Everything the previous round(s) of this item produced, for the next round.

    A reopened item must not start from scratch: the executor gets the earlier
    conclusion, the files that were touched, and the revision each round landed
    in (``git:<哈希>`` draft commits and real SVN numbers alike).
    """
    tab = tables_for(target)
    item = query_one(f"SELECT * FROM {tab.item} WHERE id = ?", (int(bug_id),))
    if item is None:
        return {}
    revisions = query_all(
        f"""
        SELECT revision, branch, message, author, files, git_commit, created_at
          FROM {tab.revision} WHERE {tab.fk} = ? ORDER BY id ASC
        """,
        (int(bug_id),),
    )
    for row in revisions:
        row["files"] = _json_load(row.get("files")) or []
    analyses = query_all(
        f"""
        SELECT kind, conclusion, root_cause, change_desc, impact, verify, unverified,
               created_at
          FROM {tab.analysis} WHERE {tab.fk} = ? AND kind NOT IN ('manual')
         ORDER BY id DESC
        """,
        (int(bug_id),),
    )
    last = analyses[0] if analyses else None
    reviews = query_all(
        f"SELECT result, reject_reason, merged_revision, created_at FROM {tab.review}"
        f" WHERE {tab.fk} = ? ORDER BY id ASC",
        (int(bug_id),),
    )
    passes = [r for r in reviews if r["result"] == "pass"]
    rejects = [r for r in reviews if r["result"] == "reject"]
    promoted = [r for r in revisions if str(r["revision"]).isdigit()]
    return {
        "rounds": len(analyses),
        "branch": item.get("branch") or "",
        "fix_summary": item.get("fix_summary") or "",
        "verify_steps": item.get("verify_steps") or "",
        "files_changed": _json_load(item.get("files_changed")) or [],
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
    return reopen_item(bug_id, reason, reviewer, "bug")


def reopen_item(item_id: int, reason: str, reviewer: str = "owner",
                target: str = "bug") -> dict:
    """Send an already merged (or closed) item back into the queue.

    Nothing is undone: the real revision stays registered, the draft branch stays
    where it is, and the review ledger keeps both verdicts -- pass first, reject
    now -- so the next round can see what was accepted and what was found lacking.
    """
    tab = tables_for(target)
    item = query_one(f"SELECT * FROM {tab.item} WHERE id = ?", (int(item_id),))
    if item is None:
        raise ValueError(f"unknown {tab.kind} id: {item_id}")
    if item["status"] not in ("merged", "closed"):
        raise ValueError(f"只有已合入或已结案的条目才能重开，当前是 {item['status']}")
    text = str(reason or "").strip()
    if not text:
        raise ValueError("重开必须写清哪里不完全")
    record_review(int(item_id), "reject", reject_reason=f"重开补修：{text}"[:500],
                  reviewer=reviewer, target=tab.kind)
    return query_one(f"SELECT * FROM {tab.item} WHERE id = ?", (int(item_id),)) or {}


def list_review_queue(targets: Iterable[str] = ITEM_KINDS) -> list[dict]:
    """Every item waiting for a human verdict, bugs and tasks together."""
    rows: list[dict] = []
    for target in targets:
        tab = tables_for(target)
        severity_sql = "severity DESC, " if target == "bug" else ""
        found = query_all(
            f"SELECT *, '{tab.kind}' AS target FROM {tab.item}"
            f" WHERE status = 'await_review'"
            f" ORDER BY pri ASC, {severity_sql}updated_at DESC"
        )
        rows.extend(decorate_item(row, target) for row in found)
    rows.sort(key=lambda r: (r["pri"], -(r.get("severity") or 0)))
    return rows


# ---------------------------------------------------------------------------
# analyses -- the executor's written conclusion for one item
# ---------------------------------------------------------------------------

ANALYSIS_FIELDS = (
    "symptom", "root_cause", "evidence", "call_chain", "change_desc",
    "impact", "verify", "unverified", "rollback", "conclusion",
)
ANALYSIS_KINDS = ("commit", "block", "manual")
TASK_ANALYSIS_KINDS = ANALYSIS_KINDS + ("plan",)


def add_analysis(item_id: int, fields: dict[str, Any], *, kind: str = "commit",
                 gates: dict[str, Any] | None = None, author: str = "ai",
                 target: str = "bug") -> dict:
    """Store one analysis record; unknown keys are ignored so callers stay loose."""
    tab = tables_for(target)
    allowed = TASK_ANALYSIS_KINDS if tab.kind == "task" else ANALYSIS_KINDS
    if kind not in allowed:
        raise ValueError(f"unknown analysis kind: {kind}")
    ts = now_str()
    payload = {key: str(fields.get(key) or "").strip() for key in ANALYSIS_FIELDS}
    payload["gates"] = _json_dump(gates or {}) or "{}"
    payload.update(item_id=int(item_id), kind=kind, author=author or "ai", ts=ts)
    columns = ", ".join([tab.fk, "kind", *ANALYSIS_FIELDS, "gates", "author", "created_at"])
    holders = ", ".join([":item_id", ":kind", *(f":{key}" for key in ANALYSIS_FIELDS),
                         ":gates", ":author", ":ts"])
    with tx() as conn:
        cur = conn.execute(f"INSERT INTO {tab.analysis} ({columns}) VALUES ({holders})",
                           payload)
        new_id = cur.lastrowid
    return _decode_analysis(
        query_one(f"SELECT * FROM {tab.analysis} WHERE id = ?", (new_id,))) or {}


def _decode_analysis(row: dict | None) -> dict | None:
    """Expose the gate verdicts as a dict next to the raw JSON column."""
    if row is not None:
        row["gates_parsed"] = _json_load(row.get("gates")) or {}
    return row


def list_analyses(item_id: int, target: str = "bug") -> list[dict]:
    tab = tables_for(target)
    rows = query_all(f"SELECT * FROM {tab.analysis} WHERE {tab.fk} = ? ORDER BY id DESC",
                     (int(item_id),))
    return [_decode_analysis(row) for row in rows]


def latest_analysis(item_id: int, target: str = "bug") -> dict | None:
    tab = tables_for(target)
    return _decode_analysis(query_one(
        f"SELECT * FROM {tab.analysis} WHERE {tab.fk} = ? ORDER BY id DESC LIMIT 1",
        (int(item_id),)))


def has_analysis(item_id: int, target: str = "bug") -> bool:
    tab = tables_for(target)
    row = query_one(f"SELECT COUNT(*) AS n FROM {tab.analysis} WHERE {tab.fk} = ?",
                    (int(item_id),))
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
    """Products present in the item tables, with per-product bug and task counts."""
    return query_all(
        """
        SELECT product_id,
               MAX(product_name) AS product_name,
               SUM(bugs) AS bugs,
               SUM(pending) AS pending,
               SUM(tasks) AS tasks,
               SUM(task_pending) AS task_pending
          FROM (
            SELECT COALESCE(product_id, 0) AS product_id,
                   MAX(COALESCE(product_name, '')) AS product_name,
                   COUNT(*) AS bugs,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   0 AS tasks, 0 AS task_pending
              FROM bugs GROUP BY COALESCE(product_id, 0)
            UNION ALL
            SELECT COALESCE(product_id, 0) AS product_id,
                   MAX(COALESCE(product_name, '')) AS product_name,
                   0, 0,
                   COUNT(*) AS tasks,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS task_pending
              FROM tasks GROUP BY COALESCE(product_id, 0)
          )
         GROUP BY product_id
         ORDER BY (SUM(bugs) + SUM(tasks)) DESC, product_id ASC
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
