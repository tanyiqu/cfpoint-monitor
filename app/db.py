# -*- coding: utf-8 -*-
"""
数据层：SQLite 封装。

设计要点
--------
1. 每次操作开一个新连接（sqlite3 在线程间共享连接不安全），
   用 WAL 模式 + busy_timeout 保证后台线程与 Flask 请求线程并发读写不冲突。
2. 写操作统一走 _WRITE_LOCK，避免 "database is locked"。
3. 所有时间都用本地时间字符串 "YYYY-MM-DD HH:MM:SS"，方便直接在界面上展示。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "cfmon.db")

_WRITE_LOCK = threading.RLock()

# ---------------------------------------------------------------- 默认配置项
# 全部以字符串存库，取值时再转换，避免 sqlite 类型推断的坑。
DEFAULT_SETTINGS = {
    # ---- 采集 ----
    "poll_interval": "300",        # 轮询间隔（秒）
    "jitter": "0.15",              # 随机抖动比例 0~0.5，避免固定周期被风控
    "request_timeout": "15",       # 单次接口超时（秒）
    "history_keep_days": "30",     # 快照保留天数
    "monitor_paused": "0",         # 1 = 暂停全部采集

    # ---- 保活 ----
    "keepalive_enabled": "1",      # 是否开启 Cookie 保活心跳
    "keepalive_interval": "900",   # 心跳间隔（秒）

    # ---- Webhook ----
    "webhook_enabled": "0",
    "webhook_type": "generic",     # generic/wecom/dingtalk/feishu/serverchan/bark/custom
    "webhook_url": "",
    "webhook_secret": "",          # 钉钉加签密钥（其他类型留空）
    "webhook_headers": "",         # 自定义请求头，JSON 字符串
    "webhook_body_tpl": "",        # 自定义 body 模板（type=custom 时生效）
    "webhook_timeout": "10",

    # ---- 推送场景开关 ----
    "notify_on_change": "1",       # CF点变化
    "notify_on_expired": "1",      # Cookie 失效
    "notify_on_recover": "1",      # 从异常恢复
    "notify_on_error": "0",        # 连续请求失败
    "error_notify_after": "3",     # 连续失败几次后推送
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    remark          TEXT    DEFAULT '',
    cookie          TEXT    NOT NULL,
    acctype         TEXT    DEFAULT 'qc',
    openid          TEXT    DEFAULT '',
    access_token    TEXT    DEFAULT '',
    appid           TEXT    DEFAULT '',
    role_name       TEXT    DEFAULT '',
    level           TEXT    DEFAULT '',
    reg_date        TEXT    DEFAULT '',
    play_cnt        INTEGER DEFAULT 0,
    cfpoint         INTEGER,
    coins_json      TEXT    DEFAULT '',
    status          TEXT    DEFAULT 'unknown',   -- unknown/ok/expired/bind/error/disabled
    status_msg      TEXT    DEFAULT '',
    fail_count      INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    last_check      TEXT    DEFAULT '',
    last_ok         TEXT    DEFAULT '',
    last_change     TEXT    DEFAULT '',
    cookie_added_at TEXT    DEFAULT '',
    cookie_updated_at TEXT  DEFAULT '',
    created_at      TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    cfpoint     INTEGER,
    coins_json  TEXT DEFAULT '',
    created_at  TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_snap_acc ON snapshots(account_id, id DESC);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER,
    kind        TEXT DEFAULT 'info',   -- change/expired/recover/error/info/keepalive/test
    title       TEXT DEFAULT '',
    message     TEXT DEFAULT '',
    push_status TEXT DEFAULT 'none',   -- none/success/failed/skipped
    push_detail TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evt_time ON events(id DESC);

CREATE TABLE IF NOT EXISTS keepalive_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    result      TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ka_acc ON keepalive_logs(account_id, id DESC);
"""


# ---------------------------------------------------------------- 基础工具

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


@contextmanager
def conn_ctx():
    """自动提交 / 自动关闭的上下文。"""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, v))


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 账号

ACCOUNT_FIELDS = [
    "name", "remark", "cookie", "acctype", "openid", "access_token", "appid",
    "role_name", "level", "reg_date", "play_cnt", "cfpoint", "coins_json",
    "status", "status_msg", "fail_count", "enabled",
    "last_check", "last_ok", "last_change", "cookie_added_at",
    "cookie_updated_at",
]


def list_accounts(enabled_only=False):
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id ASC"
    with conn_ctx() as conn:
        return rows_to_dicts(conn.execute(sql).fetchall())


def get_account(account_id):
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def create_account(data):
    data = dict(data)
    data.setdefault("created_at", now_str())
    cols = [c for c in data if c in ACCOUNT_FIELDS or c == "created_at"]
    sql = "INSERT INTO accounts(%s) VALUES(%s)" % (
        ", ".join(cols), ", ".join("?" for _ in cols))
    with _WRITE_LOCK, conn_ctx() as conn:
        cur = conn.execute(sql, [data[c] for c in cols])
        return cur.lastrowid


def update_account(account_id, **fields):
    fields = {k: v for k, v in fields.items() if k in ACCOUNT_FIELDS}
    if not fields:
        return
    sets = ", ".join("%s = ?" % k for k in fields)
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute("UPDATE accounts SET %s WHERE id = ?" % sets,
                     list(fields.values()) + [account_id])


def delete_account(account_id):
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM snapshots WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM keepalive_logs WHERE account_id = ?", (account_id,))
        conn.execute("UPDATE events SET account_id = NULL WHERE account_id = ?", (account_id,))


# ---------------------------------------------------------------- 快照

def add_snapshot(account_id, cfpoint, coins_json=""):
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute(
            "INSERT INTO snapshots(account_id, cfpoint, coins_json, created_at) VALUES(?,?,?,?)",
            (account_id, cfpoint, coins_json, now_str()),
        )


def list_snapshots(account_id, limit=60, since_days=None):
    sql = "SELECT id, cfpoint, created_at FROM snapshots WHERE account_id = ?"
    args = [account_id]
    if since_days:
        sql += " AND created_at >= ?"
        args.append((datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S"))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with conn_ctx() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    rows.reverse()
    return rows


def prune_snapshots(keep_days=30):
    cutoff = (datetime.now() - timedelta(days=int(keep_days))).strftime("%Y-%m-%d %H:%M:%S")
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute("DELETE FROM snapshots WHERE created_at < ?", (cutoff,))
        # 每个账号最多留 5000 条，防止长期运行撑爆库
        conn.execute(
            "DELETE FROM snapshots WHERE id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
            "  (PARTITION BY account_id ORDER BY id DESC) AS rn FROM snapshots) "
            "  WHERE rn <= 5000)"
        )


# ---------------------------------------------------------------- 保活日志

def add_keepalive_log(account_id, result, detail=""):
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute(
            "INSERT INTO keepalive_logs(account_id, result, detail, created_at) VALUES(?,?,?,?)",
            (account_id, result, detail, now_str()),
        )
        # 每个账号只留最近 200 条
        conn.execute(
            "DELETE FROM keepalive_logs WHERE account_id = ? AND id NOT IN "
            "(SELECT id FROM keepalive_logs WHERE account_id = ? ORDER BY id DESC LIMIT 200)",
            (account_id, account_id),
        )


def last_keepalive(account_id):
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM keepalive_logs WHERE account_id = ? ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- 事件日志

def add_event(account_id, kind, title, message, push_status="none", push_detail=""):
    with _WRITE_LOCK, conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO events(account_id, kind, title, message, push_status, push_detail, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (account_id, kind, title, message, push_status, push_detail, now_str()),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")
        return cur.lastrowid


def update_event_push(event_id, status, detail=""):
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute("UPDATE events SET push_status = ?, push_detail = ? WHERE id = ?",
                     (status, detail, event_id))


def list_events(limit=100, account_id=None, kind=None):
    sql = "SELECT e.*, a.name AS account_name FROM events e LEFT JOIN accounts a ON a.id = e.account_id WHERE 1=1"
    args = []
    if account_id:
        sql += " AND e.account_id = ?"
        args.append(account_id)
    if kind:
        sql += " AND e.kind = ?"
        args.append(kind)
    sql += " ORDER BY e.id DESC LIMIT ?"
    args.append(int(limit))
    with conn_ctx() as conn:
        return rows_to_dicts(conn.execute(sql, args).fetchall())


def clear_events():
    with _WRITE_LOCK, conn_ctx() as conn:
        conn.execute("DELETE FROM events")


# ---------------------------------------------------------------- 配置

def get_settings():
    with conn_ctx() as conn:
        rows = conn.execute("SELECT k, v FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["k"]: r["v"] for r in rows})
    return data


def save_settings(pairs):
    with _WRITE_LOCK, conn_ctx() as conn:
        for k, v in pairs.items():
            if k not in DEFAULT_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings(k, v) VALUES(?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (k, "" if v is None else str(v)),
            )


def get_setting_float(key, default=0.0):
    try:
        return float(get_settings().get(key, default))
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------- 汇总

def summary():
    accounts = list_accounts()
    total = sum(a["cfpoint"] or 0 for a in accounts if a["cfpoint"] is not None)
    ok = sum(1 for a in accounts if a["status"] == "ok" and a["enabled"])
    bad = sum(1 for a in accounts if a["status"] in ("expired", "bind", "error") and a["enabled"])
    disabled = sum(1 for a in accounts if not a["enabled"])
    return {
        "total_accounts": len(accounts),
        "ok": ok,
        "bad": bad,
        "disabled": disabled,
        "total_cfpoint": total,
    }
