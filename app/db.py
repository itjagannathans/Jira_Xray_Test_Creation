"""SQLite database access layer."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional

from werkzeug.security import generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS Tb_user (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    jira_pat        TEXT,
    created_date    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS Tb_Xray_Testcase_Created (
    SL_No            INTEGER PRIMARY KEY AUTOINCREMENT,
    Jira_id          TEXT    NOT NULL,
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
    Test_name         TEXT,
    NoOfTest_Created  INTEGER NOT NULL,
    Created_By        TEXT    NOT NULL,
    Created_date      TEXT    NOT NULL
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


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Seed default admin user
        cur = conn.execute("SELECT id FROM Tb_user WHERE username = ?", ("Admin",))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO Tb_user (username, password_hash, is_admin, is_active, created_date) "
                "VALUES (?, ?, 1, 1, ?)",
                ("Admin", generate_password_hash("Welcome@1"), datetime.utcnow().isoformat(timespec="seconds")),
            )


# ---- Users ----

def get_user_by_id(db_path: str, user_id: int):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM Tb_user WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(db_path: str, username: str):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM Tb_user WHERE username = ?", (username,)).fetchone()


def create_user(db_path: str, username: str, password_hash: str, jira_pat: Optional[str] = None,
                is_admin: bool = False) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO Tb_user (username, password_hash, is_admin, is_active, jira_pat, created_date) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (username, password_hash, 1 if is_admin else 0, jira_pat,
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
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO Tb_Xray_Testcase_Created "
            "(Jira_id, Test_id, Test_Description, Test_Steps, Test_expected, Created_By, Created_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (jira_id, test_id, description, steps, expected, created_by,
             datetime.utcnow().isoformat(timespec="seconds")),
        )


def list_xray_tests(db_path: str, created_by: Optional[str] = None):
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                "SELECT * FROM Tb_Xray_Testcase_Created WHERE Created_By = ? ORDER BY SL_No DESC",
                (created_by,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM Tb_Xray_Testcase_Created ORDER BY SL_No DESC"
        ).fetchall()


# ---- Export history ----

def insert_export(db_path: str, *, jira_id: str, test_name: str, count: int, created_by: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO Tb_Testcase_Export "
            "(Jira_id, Test_name, NoOfTest_Created, Created_By, Created_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (jira_id, test_name, count, created_by,
             datetime.utcnow().isoformat(timespec="seconds")),
        )


def list_exports(db_path: str, created_by: Optional[str] = None):
    with connect(db_path) as conn:
        if created_by:
            return conn.execute(
                "SELECT * FROM Tb_Testcase_Export WHERE Created_By = ? ORDER BY Sl_No DESC",
                (created_by,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM Tb_Testcase_Export ORDER BY Sl_No DESC"
        ).fetchall()
