"""SQLite 数据访问层。

表结构：
  plans         计划头（每次新建/实测重算产生一条版本，仅一条 active）
  plan_periods  计划逐段输入与求解结果（含实测标记）
  actuals       按时段顺序登记的实测数据
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from .optimizer import PERIODS, period_label

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    status          TEXT    NOT NULL DEFAULT 'active',   -- active | history
    source          TEXT    NOT NULL,                     -- new | actuals
    executed_count  INTEGER NOT NULL DEFAULT 0,
    capacity        INTEGER NOT NULL,
    reserve         INTEGER NOT NULL,
    initial_soc     INTEGER NOT NULL,
    charge_limit    INTEGER NOT NULL,
    discharge_limit INTEGER NOT NULL,
    total_cost      REAL    NOT NULL DEFAULT 0,
    warning         TEXT
);

CREATE TABLE IF NOT EXISTS plan_periods (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    period          INTEGER NOT NULL,        -- 0..95
    time_label      TEXT    NOT NULL,
    load            INTEGER NOT NULL,        -- 计划输入（预测或实测冻结值）
    pv              INTEGER NOT NULL,
    grid_limit      INTEGER NOT NULL,
    price           REAL    NOT NULL,
    grid_buy        INTEGER NOT NULL DEFAULT 0,
    charge          INTEGER NOT NULL DEFAULT 0,
    discharge       INTEGER NOT NULL DEFAULT 0,
    curtail         INTEGER NOT NULL DEFAULT 0,
    soc_end         INTEGER NOT NULL DEFAULT 0,
    cost            REAL    NOT NULL DEFAULT 0,
    is_executed     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(plan_id, period)
);

CREATE TABLE IF NOT EXISTS actuals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          INTEGER NOT NULL UNIQUE, -- 0..95
    time_label      TEXT    NOT NULL,
    load            INTEGER NOT NULL,
    pv              INTEGER NOT NULL,
    grid_buy        INTEGER NOT NULL,
    soc_end         INTEGER NOT NULL,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "/data/ems.db")


@contextmanager
def get_conn():
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def reset_db() -> None:
    """清空全部数据（演示/重新规划用）。"""
    with get_conn() as conn:
        conn.executescript(
            "DELETE FROM actuals; DELETE FROM plan_periods; DELETE FROM plans;"
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('plans','plan_periods','actuals');"
        )


# ---------- plans ----------

def get_active_plan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM plans WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM plans ORDER BY id DESC"
    ).fetchall())


def get_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE id=?",
                        (plan_id,)).fetchone()


def get_plan_periods(conn: sqlite3.Connection,
                     plan_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM plan_periods WHERE plan_id=? ORDER BY period",
        (plan_id,)).fetchall())


def get_actuals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM actuals ORDER BY period"
    ).fetchall())


def insert_plan(conn: sqlite3.Connection, *, source: str, battery: dict,
                periods_in: list[dict], results: list[dict] | None,
                executed_count: int, total_cost: float,
                warning: str | None = None) -> int:
    """写入一条计划及其逐段数据，并把旧计划置为 history。results 为 None 时列补 0。"""
    conn.execute("UPDATE plans SET status='history' WHERE status='active'")
    cur = conn.execute(
        """INSERT INTO plans
           (status, source, executed_count, capacity, reserve, initial_soc,
            charge_limit, discharge_limit, total_cost, warning)
           VALUES ('active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, executed_count, battery["capacity"], battery["reserve"],
         battery["initial_soc"], battery["charge_limit"],
         battery["discharge_limit"], total_cost, warning),
    )
    plan_id = cur.lastrowid
    for t, pin in enumerate(periods_in):
        r = results[t] if results else {}
        conn.execute(
            """INSERT INTO plan_periods
               (plan_id, period, time_label, load, pv, grid_limit, price,
                grid_buy, charge, discharge, curtail, soc_end, cost, is_executed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, t, period_label(t), pin["load"], pin["pv"],
             pin["grid_limit"], pin["price"],
             r.get("grid_buy", 0), r.get("charge", 0), r.get("discharge", 0),
             r.get("curtail", 0), r.get("soc_end", battery["initial_soc"]),
             r.get("cost", 0.0), 1 if t < executed_count else 0),
        )
    return plan_id


def insert_actual(conn: sqlite3.Connection, t: int, actual: dict) -> None:
    conn.execute(
        """INSERT INTO actuals (period, time_label, load, pv, grid_buy, soc_end)
           VALUES (?,?,?,?,?,?)""",
        (t, period_label(t), actual["load"], actual["pv"],
         actual["grid_buy"], actual["soc_end"]),
    )


def count_actuals(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) c FROM actuals").fetchone()["c"]


__all__ = ["PERIODS", "init_db", "reset_db", "get_conn", "get_active_plan",
           "list_plans", "get_plan", "get_plan_periods", "get_actuals",
           "insert_plan", "insert_actual", "count_actuals"]
