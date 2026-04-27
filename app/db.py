"""SQLite database access layer."""
from __future__ import annotations

import sqlite3
import json
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional

from werkzeug.security import generate_password_hash


SITE_MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site_name_mapping.json")


SCHEMA = """
CREATE TABLE IF NOT EXISTS Tb_user (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    full_name       TEXT,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    jira_pat        TEXT,
    created_date    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS Tb_Xray_Testcase_Created (
    SL_No            INTEGER PRIMARY KEY AUTOINCREMENT,
    Jira_id          TEXT    NOT NULL,
    Site_Name        TEXT,
    Test_id          TEXT    NOT NULL,
    Test_Description TEXT,
    Test_Steps       TEXT,
    Test_expected    TEXT,
    Created_By       TEXT    NOT NULL,
    Created_date     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS Tb_Testcase_Export (
    Sl_No             INTEGER PRIMARY KEY AUTOINCREMENT,
    Jira_id           TEXT    NOT NULL,
    Site_Name         TEXT,
    Test_name         TEXT,
    NoOfTest_Created  INTEGER NOT NULL,
    Created_By        TEXT    NOT NULL,
    Created_date      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS Tb_Pending_Generation (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    story_key        TEXT    NOT NULL,
    action           TEXT    NOT NULL,
    testcases_json   TEXT    NOT NULL,
    created_date     TEXT    NOT NULL
);
"""


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_site_name_mapping() -> dict[str, str]:
    """Load Jira prefix to site-name mapping from JSON config file."""
    try:
        with open(SITE_MAPPING_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            k = str(key or "").strip().upper()
            v = str(value or "").strip()
            if k and v:
                mapping[k] = v
    return mapping


def resolve_site_name(jira_id: str) -> Optional[str]:
    key = (jira_id or "").strip().upper()
    if not key:
        return None
    for prefix, site_name in load_site_name_mapping().items():
        if key.startswith(prefix):
            return site_name
    return None


def _backfill_site_name(conn: sqlite3.Connection, table_name: str) -> None:
    mapping = load_site_name_mapping()
    if not mapping:
        return
    for prefix, site_name in mapping.items():
        conn.execute(
            f"UPDATE {table_name} "
            "SET Site_Name = ? "
            "WHERE (Site_Name IS NULL OR TRIM(Site_Name) = '') "
            "AND UPPER(Jira_id) LIKE ?",
            (site_name, f"{prefix}%"),
        )


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migration: add full_name column if missing
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(Tb_user)").fetchall()}
        if "full_name" not in cols:
            conn.execute("ALTER TABLE Tb_user ADD COLUMN full_name TEXT")

        xray_cols = {r["name"] for r in conn.execute("PRAGMA table_info(Tb_Xray_Testcase_Created)").fetchall()}
        if "Site_Name" not in xray_cols:
            conn.execute("ALTER TABLE Tb_Xray_Testcase_Created ADD COLUMN Site_Name TEXT")

        export_cols = {r["name"] for r in conn.execute("PRAGMA table_info(Tb_Testcase_Export)").fetchall()}
        if "Site_Name" not in export_cols:
            conn.execute("ALTER TABLE Tb_Testcase_Export ADD COLUMN Site_Name TEXT")

        _backfill_site_name(conn, "Tb_Xray_Testcase_Created")
        _backfill_site_name(conn, "Tb_Testcase_Export")

        # Seed default admin user
        cur = conn.execute("SELECT id FROM Tb_user WHERE username = ?", ("Admin",))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO Tb_user (username, password_hash, full_name, is_admin, is_active, created_date) "
                "VALUES (?, ?, ?, 1, 1, ?)",
                ("Admin", generate_password_hash("Welcome@1"), "Administrator",
                 datetime.utcnow().isoformat(timespec="seconds")),
            )


# ---- Users ----

def get_user_by_id(db_path: str, user_id: int):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM Tb_user WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(db_path: str, username: str):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM Tb_user WHERE username = ?", (username,)).fetchone()


def create_user(db_path: str, username: str, password_hash: str, jira_pat: Optional[str] = None,
                is_admin: bool = False, full_name: Optional[str] = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO Tb_user (username, password_hash, full_name, is_admin, is_active, jira_pat, created_date) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (username, password_hash, full_name, 1 if is_admin else 0, jira_pat,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def list_users(db_path: str):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM Tb_user ORDER BY id").fetchall()


def update_user(db_path: str, user_id: int, username: Optional[str] = None,
                jira_pat: Optional[str] = None, is_admin: Optional[bool] = None) -> None:
    sets, params = [], []
    if username is not None:
        sets.append("username = ?"); params.append(username)
    if jira_pat is not None:
        sets.append("jira_pat = ?"); params.append(jira_pat)
    if is_admin is not None:
        sets.append("is_admin = ?"); params.append(1 if is_admin else 0)
    if not sets:
        return
    params.append(user_id)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE Tb_user SET {', '.join(sets)} WHERE id = ?", params)


def set_user_active(db_path: str, user_id: int, active: bool) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE Tb_user SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))


def delete_user(db_path: str, user_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM Tb_user WHERE id = ?", (user_id,))


def update_jira_pat(db_path: str, user_id: int, jira_pat: str) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE Tb_user SET jira_pat = ? WHERE id = ?", (jira_pat, user_id))


# ---- Xray history ----

def insert_xray_test(db_path: str, *, jira_id: str, test_id: str, description: str,
                     steps: str, expected: str, created_by: str) -> None:
    site_name = resolve_site_name(jira_id)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO Tb_Xray_Testcase_Created "
            "(Jira_id, Site_Name, Test_id, Test_Description, Test_Steps, Test_expected, Created_By, Created_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (jira_id, site_name, test_id, description, steps, expected, created_by,
             datetime.utcnow().isoformat(timespec="seconds")),
        )


def list_xray_tests(db_path: str, created_by: Optional[str] = None):
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                "SELECT * FROM Tb_Xray_Testcase_Created "
                "WHERE UPPER(TRIM(Created_By)) = UPPER(TRIM(?)) "
                "ORDER BY Created_date ASC, SL_No ASC",
                (created_by,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM Tb_Xray_Testcase_Created ORDER BY Created_date ASC, SL_No ASC"
        ).fetchall()


def list_xray_site_summary(db_path: str, created_by: Optional[str] = None):
    """Return site-level summary: distinct Jira count and distinct Test count."""
    base_sql = (
        "SELECT "
        "COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown') AS Site_Name, "
        "COUNT(DISTINCT Jira_id) AS Jira_Count, "
        "COUNT(DISTINCT Test_id) AS Test_Count "
        "FROM Tb_Xray_Testcase_Created "
    )
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                base_sql +
                "WHERE UPPER(TRIM(Created_By)) = UPPER(TRIM(?)) "
                "GROUP BY COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown') "
                "ORDER BY Site_Name ASC",
                (created_by,),
            ).fetchall()
        return conn.execute(
            base_sql +
            "GROUP BY COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown') "
            "ORDER BY Site_Name ASC"
        ).fetchall()


def list_xray_tests_by_site(db_path: str, site_name: str, created_by: Optional[str] = None):
    """Return imported test rows filtered by site name for drill-down detail view."""
    site = (site_name or "").strip()
    normalized_expr = "COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown')"
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                "SELECT * FROM Tb_Xray_Testcase_Created "
                f"WHERE {normalized_expr} = ? "
                "AND UPPER(TRIM(Created_By)) = UPPER(TRIM(?)) "
                "ORDER BY Created_date ASC, SL_No ASC",
                (site, created_by),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM Tb_Xray_Testcase_Created "
            f"WHERE {normalized_expr} = ? "
            "ORDER BY Created_date ASC, SL_No ASC",
            (site,),
        ).fetchall()


def list_sitewise_report_summary(db_path: str):
    """Return site summary with Jira/Test counts for imports and exports."""
    sql = """
        WITH x AS (
            SELECT
                COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown') AS Site_Name,
                COUNT(DISTINCT Jira_id) AS Jira_Count,
                COUNT(DISTINCT Test_id) AS Test_Count
            FROM Tb_Xray_Testcase_Created
            GROUP BY COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown')
        ),
        e AS (
            SELECT
                COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown') AS Site_Name,
                COUNT(DISTINCT Jira_id) AS Jira_Exported_Count,
                COALESCE(SUM(NoOfTest_Created), 0) AS Test_Exported_Count
            FROM Tb_Testcase_Export
            GROUP BY COALESCE(NULLIF(TRIM(Site_Name), ''), 'Unknown')
        ),
        s AS (
            SELECT Site_Name FROM x
            UNION
            SELECT Site_Name FROM e
        )
        SELECT
            s.Site_Name AS Site_Name,
            COALESCE(x.Jira_Count, 0) AS Jira_Count,
            COALESCE(x.Test_Count, 0) AS Test_Count,
            COALESCE(e.Jira_Exported_Count, 0) AS Jira_Exported_Count,
            COALESCE(e.Test_Exported_Count, 0) AS Test_Exported_Count
        FROM s
        LEFT JOIN x ON x.Site_Name = s.Site_Name
        LEFT JOIN e ON e.Site_Name = s.Site_Name
        ORDER BY s.Site_Name ASC
    """
    with connect(db_path) as conn:
        return conn.execute(sql).fetchall()


# ---- Export history ----

def insert_export(db_path: str, *, jira_id: str, test_name: str, count: int, created_by: str) -> int:
    site_name = resolve_site_name(jira_id)
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO Tb_Testcase_Export "
            "(Jira_id, Site_Name, Test_name, NoOfTest_Created, Created_By, Created_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (jira_id, site_name, test_name, count, created_by,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        return int(cur.lastrowid)


def list_exports(db_path: str, created_by: Optional[str] = None):
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                "SELECT * FROM Tb_Testcase_Export "
                "WHERE UPPER(TRIM(Created_By)) = UPPER(TRIM(?)) "
                "ORDER BY Created_date ASC, Sl_No ASC",
                (created_by,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM Tb_Testcase_Export ORDER BY Created_date ASC, Sl_No ASC"
        ).fetchall()


def get_export(db_path: str, sl_no: int):
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM Tb_Testcase_Export WHERE Sl_No = ?",
            (sl_no,),
        ).fetchone()


# ---- Pending generation (server-side) ----

def upsert_pending_generation(db_path: str, *, user_id: int, story_key: str,
                              action: str, testcases: list) -> int:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM Tb_Pending_Generation WHERE user_id = ?", (user_id,))
        cur = conn.execute(
            "INSERT INTO Tb_Pending_Generation "
            "(user_id, story_key, action, testcases_json, created_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                story_key,
                action,
                json.dumps(testcases, ensure_ascii=True),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)


def get_pending_generation(db_path: str, pending_id: int, user_id: int):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM Tb_Pending_Generation WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        ).fetchone()
    if not row:
        return None
    try:
        testcases = json.loads(row["testcases_json"] or "[]")
    except Exception:
        testcases = []
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "story_key": row["story_key"],
        "action": row["action"],
        "testcases": testcases,
        "created_date": row["created_date"],
    }


def clear_pending_generation(db_path: str, pending_id: int, user_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM Tb_Pending_Generation WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        )
