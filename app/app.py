# -*- coding: utf-8 -*-
"""
CF 点监控 —— Flask 后端。

启动：
    python app.py            # 默认 http://127.0.0.1:8770
    python app.py --port 9000 --host 0.0.0.0

接口一览
--------
    GET    /                         后台页面
    GET    /api/state                全量状态（账号 + 配置 + 汇总 + 调度状态）
    POST   /api/accounts             新增账号
    PUT    /api/accounts/<id>        修改账号（名称 / 备注 / Cookie / 启停）
    DELETE /api/accounts/<id>        删除账号
    GET    /api/accounts/<id>/cookie 查看完整 Cookie（编辑时回填）
    GET    /api/accounts/<id>/preview 实时预览（真实请求，不写库）
    GET    /api/accounts/<id>/history 历史快照
    POST   /api/accounts/<id>/refresh  立即采集一次并落库
    POST   /api/accounts/<id>/keepalive 手动保活
    POST   /api/settings             保存配置
    POST   /api/webhook/test         测试推送
    GET    /api/events               事件日志
    POST   /api/events/clear         清空日志
"""

import argparse
import json
import os
import re
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import cf_client
import db
import monitor as monitor_mod
import webhook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_AS_ASCII"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# 模板/静态文件改动即时生效，方便二次开发调试
app.config["TEMPLATES_AUTO_RELOAD"] = True

_started = threading.Event()
monitor = monitor_mod.monitor

WEBHOOK_TYPES = ["generic", "wecom", "dingtalk", "feishu", "serverchan", "bark", "custom"]

# 采集模式：interval=实时采集（按间隔轮询）/ daily=定时采集（每天固定时间点）
SCHEDULE_MODES = ["interval", "daily"]
# 定时采集的提醒方式：change=仅在有变化时提醒 / always=每次采集后汇总提醒
DAILY_NOTIFY_MODES = ["change", "always"]


# ================================================================ 工具

def err(message, code=400, **extra):
    payload = {"ok": False, "message": message}
    payload.update(extra)
    return jsonify(payload), code


def body_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def cookie_age_hours(account):
    raw = account.get("cookie_updated_at") or account.get("cookie_added_at") or ""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return round((datetime.now() - dt).total_seconds() / 3600.0, 1)


def serialize_account(account, with_spark=True):
    coins = {}
    if account.get("coins_json"):
        try:
            coins = json.loads(account["coins_json"])
        except (TypeError, ValueError):
            coins = {}
    openid = account.get("openid") or ""
    item = {
        "id": account["id"],
        "name": account["name"],
        "remark": account.get("remark") or "",
        "role_name": account.get("role_name") or "",
        "level": account.get("level") or "",
        "reg_date": account.get("reg_date") or "",
        "play_cnt": account.get("play_cnt") or 0,
        "cfpoint": account.get("cfpoint"),
        "coins": coins,
        "status": account.get("status") or "unknown",
        "status_msg": account.get("status_msg") or "",
        "fail_count": account.get("fail_count") or 0,
        "enabled": bool(account.get("enabled")),
        "last_check": account.get("last_check") or "",
        "last_ok": account.get("last_ok") or "",
        "last_change": account.get("last_change") or "",
        "cookie_age_hours": cookie_age_hours(account),
        "openid_tail": openid[-6:] if openid else "",
        "acctype": account.get("acctype") or "qc",
        "appid": account.get("appid") or "",
        "created_at": account.get("created_at") or "",
    }
    if with_spark:
        snaps = db.list_snapshots(account["id"], limit=30)
        item["spark"] = [s["cfpoint"] for s in snaps if s["cfpoint"] is not None]
    return item


def validate_settings(raw):
    """校验并归一化配置，返回 (settings_dict, warnings)。"""
    current = db.get_settings()
    out = {}
    warnings = []

    def as_int(key, lo, hi, label):
        value = raw.get(key, current.get(key))
        try:
            iv = int(float(str(value).strip()))
        except (TypeError, ValueError):
            warnings.append("%s 取值无效，已保留原值" % label)
            return None
        iv = max(lo, min(hi, iv))
        return str(iv)

    def as_bool(key):
        value = raw.get(key, current.get(key))
        if isinstance(value, bool):
            return "1" if value else "0"
        return "1" if str(value).strip() in ("1", "true", "True", "on", "yes") else "0"

    v = as_int("poll_interval", 20, 86400, "获取频率")
    if v:
        out["poll_interval"] = v
    v = as_int("keepalive_interval", 120, 86400, "保活间隔")
    if v:
        out["keepalive_interval"] = v
    v = as_int("request_timeout", 5, 60, "请求超时")
    if v:
        out["request_timeout"] = v
    v = as_int("history_keep_days", 1, 3650, "历史保留天数")
    if v:
        out["history_keep_days"] = v
    v = as_int("webhook_timeout", 3, 60, "推送超时")
    if v:
        out["webhook_timeout"] = v
    v = as_int("error_notify_after", 1, 50, "异常推送阈值")
    if v:
        out["error_notify_after"] = v

    jitter = raw.get("jitter", current.get("jitter"))
    try:
        jv = min(0.5, max(0.0, float(str(jitter).strip())))
        out["jitter"] = ("%.2f" % jv).rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        warnings.append("随机抖动取值无效，已保留原值")

    # ---- 采集模式 ----
    mode = str(raw.get("schedule_mode", current.get("schedule_mode")) or "interval").strip()
    if mode not in SCHEDULE_MODES:
        warnings.append("未知的采集模式，已回退为实时采集")
        mode = "interval"
    out["schedule_mode"] = mode

    raw_times = str(raw.get("daily_times", current.get("daily_times")) or "")
    slots = monitor_mod.parse_daily_times(raw_times)
    if not slots:
        warnings.append("定时采集时间点为空或格式无效（应为 HH:MM），已保留原值")
        out["daily_times"] = current.get("daily_times") or "06:00"
    else:
        submitted = [x for x in re.split(r"[,;，；\s]+", raw_times.strip()) if x]
        if len(submitted) > len(slots):
            warnings.append("部分时间点格式无效已忽略，仅保留 %s" % ",".join(slots))
        out["daily_times"] = ",".join(slots)

    notify_mode = str(raw.get("daily_notify_mode",
                              current.get("daily_notify_mode")) or "change").strip()
    if notify_mode not in DAILY_NOTIFY_MODES:
        warnings.append("未知的定时提醒方式，已回退为有变化时提醒")
        notify_mode = "change"
    out["daily_notify_mode"] = notify_mode

    for key in ("webhook_enabled", "keepalive_enabled", "monitor_paused",
                "notify_on_change", "notify_on_expired", "notify_on_recover",
                "notify_on_error", "notify_on_digest"):
        out[key] = as_bool(key)

    wtype = str(raw.get("webhook_type", current.get("webhook_type")) or "generic").strip()
    if wtype not in WEBHOOK_TYPES:
        warnings.append("未知的 Webhook 类型，已回退为通用 JSON")
        wtype = "generic"
    out["webhook_type"] = wtype

    url = str(raw.get("webhook_url", current.get("webhook_url")) or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        warnings.append("Webhook 地址必须以 http:// 或 https:// 开头，已忽略该地址")
        url = current.get("webhook_url") or ""
    out["webhook_url"] = url

    out["webhook_secret"] = str(raw.get("webhook_secret", current.get("webhook_secret")) or "").strip()

    headers = str(raw.get("webhook_headers", current.get("webhook_headers")) or "").strip()
    if headers:
        try:
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                raise ValueError
        except ValueError:
            warnings.append("自定义请求头不是合法 JSON 对象，已忽略")
            headers = current.get("webhook_headers") or ""
    out["webhook_headers"] = headers

    out["webhook_body_tpl"] = str(raw.get("webhook_body_tpl",
                                          current.get("webhook_body_tpl")) or "")

    return out, warnings


# ================================================================ 页面

@app.route("/")
def index():
    return render_template("index.html")


@app.after_request
def add_headers(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ================================================================ 状态

@app.get("/api/state")
def api_state():
    accounts = [serialize_account(a) for a in db.list_accounts()]
    settings = db.get_settings()
    return jsonify({
        "ok": True,
        "accounts": accounts,
        "settings": settings,
        "summary": db.summary(),
        "monitor": monitor.status(),
        "webhook_types": WEBHOOK_TYPES,
        "server_time": db.now_str(),
    })


@app.get("/api/events")
def api_events():
    try:
        limit = min(500, max(1, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100
    kind = request.args.get("kind") or None
    account_id = request.args.get("account_id")
    try:
        account_id = int(account_id) if account_id else None
    except ValueError:
        account_id = None
    return jsonify({"ok": True, "events": db.list_events(limit=limit, account_id=account_id, kind=kind)})


@app.post("/api/events/clear")
def api_events_clear():
    db.clear_events()
    return jsonify({"ok": True, "message": "日志已清空"})


# ================================================================ 账号

@app.post("/api/accounts")
def api_account_create():
    data = body_json()
    cookie = (data.get("cookie") or "").strip()
    if not cookie:
        return err("请粘贴 Cookie")

    jar, creds = cf_client.parse_credentials(cookie)
    if not creds["openid"] or not creds["access_token"]:
        return err("Cookie 里没有解析到 openid / access_token。请确认粘贴的是登录后"
                   "请求 cf.ams.game.qq.com/ide/ 那条的完整 Cookie。")

    force = bool(data.get("force"))
    status, status_msg, role_name, level, reg_date, play_cnt = "unknown", "", "", "", "", 0
    cfpoint, coins_json = None, ""

    try:
        result = cf_client.query(cookie, timeout=15)
        coins = result["coins"] or {}
        personal = result["personal"] or {}
        cfpoint = monitor_mod._to_int(coins.get("cfPoint"))
        coins_json = json.dumps(coins, ensure_ascii=False)
        role_name = str(personal.get("roleName") or "")
        level = str(personal.get("lev") or "")
        reg_date = str(personal.get("regDate") or "")
        play_cnt = monitor_mod._to_int(personal.get("playCnt"), 0)
        status = "ok"
    except cf_client.CFError as exc:
        if not force:
            return err("Cookie 校验失败：%s" % exc, code=422, need_force=True,
                       kind=exc.kind)
        status = "expired" if exc.is_expired else ("bind" if exc.kind == "bind" else "error")
        status_msg = str(exc)

    name = (data.get("name") or "").strip() or role_name or ("账号 %s" % creds["openid"][-6:])
    now = db.now_str()
    account_id = db.create_account({
        "name": name,
        "remark": (data.get("remark") or "").strip(),
        "cookie": cookie,
        "acctype": creds["acctype"],
        "openid": creds["openid"],
        "access_token": creds["access_token"],
        "appid": creds["appid"],
        "role_name": role_name,
        "level": level,
        "reg_date": reg_date,
        "play_cnt": play_cnt,
        "cfpoint": cfpoint,
        "coins_json": coins_json,
        "status": status,
        "status_msg": status_msg,
        "fail_count": 0,
        "enabled": 1 if data.get("enabled", True) else 0,
        "last_check": now,
        "last_ok": now if status == "ok" else "",
        "last_change": now if cfpoint is not None else "",
        "cookie_added_at": now,
        "cookie_updated_at": now,
        "created_at": now,
    })
    if cfpoint is not None:
        db.add_snapshot(account_id, cfpoint, coins_json)
    db.add_event(account_id, "info", "新增账号：%s" % name, "已添加账号 %s" % name)

    # 新增后立刻进入采集节奏（定时模式则排到下一个时间点）
    monitor.reschedule()
    return jsonify({"ok": True, "id": account_id, "message": "账号已添加",
                    "account": serialize_account(db.get_account(account_id))})


@app.put("/api/accounts/<int:account_id>")
def api_account_update(account_id):
    account = db.get_account(account_id)
    if not account:
        return err("账号不存在", 404)
    data = body_json()
    fields = {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        if name:
            fields["name"] = name
    if "remark" in data:
        fields["remark"] = (data.get("remark") or "").strip()
    if "enabled" in data:
        fields["enabled"] = 1 if data.get("enabled") else 0
        if not data.get("enabled"):
            fields["status"] = "disabled"
        elif account.get("status") == "disabled":
            fields["status"] = "unknown"

    cookie = (data.get("cookie") or "").strip()
    if cookie and cookie != account["cookie"]:
        jar, creds = cf_client.parse_credentials(cookie)
        if not creds["openid"] or not creds["access_token"]:
            return err("新 Cookie 里没有解析到 openid / access_token")
        now = db.now_str()
        fields.update({
            "cookie": cookie,
            "acctype": creds["acctype"],
            "openid": creds["openid"],
            "access_token": creds["access_token"],
            "appid": creds["appid"],
            "cookie_updated_at": now,
            "status": "unknown",
            "status_msg": "",
            "fail_count": 0,
        })

    if not fields:
        return err("没有需要更新的内容")

    db.update_account(account_id, **fields)

    # 换了 Cookie 就立刻验一次，把角色名 / 当前点数补齐
    result = None
    if "cookie" in fields:
        result = monitor.check_account(account_id, source="manual", notify=False)
        monitor.reschedule()

    return jsonify({"ok": True, "message": "已保存",
                    "account": serialize_account(db.get_account(account_id)),
                    "check": result})


@app.delete("/api/accounts/<int:account_id>")
def api_account_delete(account_id):
    account = db.get_account(account_id)
    if not account:
        return err("账号不存在", 404)
    db.delete_account(account_id)
    db.add_event(None, "info", "删除账号：%s" % account["name"], "账号及其历史已删除")
    return jsonify({"ok": True, "message": "已删除"})


@app.get("/api/accounts/<int:account_id>/cookie")
def api_account_cookie(account_id):
    account = db.get_account(account_id)
    if not account:
        return err("账号不存在", 404)
    return jsonify({"ok": True, "cookie": account["cookie"],
                    "cookie_updated_at": account.get("cookie_updated_at") or ""})


@app.get("/api/accounts/<int:account_id>/preview")
def api_account_preview(account_id):
    """实时预览：直接打接口拿最新数据，不写库、不影响采集历史。"""
    account = db.get_account(account_id)
    if not account:
        return err("账号不存在", 404)
    started = time.time()
    try:
        result = cf_client.query(account["cookie"], timeout=15)
    except cf_client.CFError as exc:
        return jsonify({
            "ok": False,
            "kind": exc.kind,
            "message": str(exc),
            "account": serialize_account(account, with_spark=False),
            "duration": round(time.time() - started, 2),
        }), 200

    coins = result["coins"] or {}
    personal = result["personal"] or {}
    current = monitor_mod._to_int(coins.get("cfPoint"))
    stored = account.get("cfpoint")
    return jsonify({
        "ok": True,
        "account": serialize_account(account, with_spark=False),
        "coins": coins,
        "personal": personal,
        "cfpoint": current,
        "stored_cfpoint": stored,
        "diff": (current - stored) if (current is not None and stored is not None) else None,
        "duration": round(time.time() - started, 2),
        "fetched_at": db.now_str(),
        "history": db.list_snapshots(account_id, limit=200),
    })


@app.get("/api/accounts/<int:account_id>/history")
def api_account_history(account_id):
    if not db.get_account(account_id):
        return err("账号不存在", 404)
    try:
        limit = min(2000, max(2, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    return jsonify({"ok": True, "history": db.list_snapshots(account_id, limit=limit)})


@app.post("/api/accounts/<int:account_id>/refresh")
def api_account_refresh(account_id):
    if not db.get_account(account_id):
        return err("账号不存在", 404)
    result = monitor.check_account(account_id, source="manual")
    return jsonify({"ok": True, "result": result,
                    "account": serialize_account(db.get_account(account_id))})


@app.post("/api/accounts/<int:account_id>/keepalive")
def api_account_keepalive(account_id):
    if not db.get_account(account_id):
        return err("账号不存在", 404)
    result = monitor.keepalive(account_id)
    return jsonify({"ok": result["ok"], "detail": result["detail"],
                    "message": "保活请求已发出" if result["ok"] else "保活请求失败"}), 200


# ================================================================ 配置

@app.post("/api/settings")
def api_settings_save():
    data = body_json()
    values, warnings = validate_settings(data)
    if not values:
        return err("没有可保存的配置项")
    db.save_settings(values)
    # 采集模式 / 频率改了立刻生效：实时模式马上补一轮，定时模式重排到下一个时间点
    monitor.reschedule()
    return jsonify({"ok": True, "message": "配置已保存" + ("（%s）" % "；".join(warnings) if warnings else ""),
                    "settings": db.get_settings(), "warnings": warnings})


@app.post("/api/webhook/test")
def api_webhook_test():
    data = body_json()
    values, warnings = validate_settings(data) if data else (db.get_settings(), [])
    settings = db.get_settings()
    settings.update(values)
    if not settings.get("webhook_url"):
        return err("请先填写 Webhook 地址")

    demo = {"id": 0, "name": "测试账号", "role_name": "测试角色", "openid": "TEST0000"}
    ctx = webhook.make_context(
        "test", "CF点监控 · 测试推送",
        "【CF点监控】这是一条测试推送\n账号：测试账号\n示例变动：3956 → 4000（+44）\n时间：%s"
        % db.now_str(),
        account=demo, before=3956, after=4000, delta=44, extra="+44")
    ok, detail = webhook.send(settings, ctx)
    if ok is None:
        return err(detail or "推送未发送")
    return jsonify({"ok": bool(ok), "message": "推送成功" if ok else "推送失败",
                    "detail": detail, "warnings": warnings})


@app.post("/api/monitor/pause")
def api_monitor_pause():
    data = body_json()
    paused = "1" if data.get("paused") else "0"
    db.save_settings({"monitor_paused": paused})
    monitor.reschedule()
    return jsonify({"ok": True, "paused": paused == "1",
                    "message": "已暂停采集" if paused == "1" else "已恢复采集"})


# ================================================================ 启动

def ensure_started():
    if _started.is_set():
        return
    _started.set()
    db.init_db()
    monitor.start()


@app.before_request
def _before():
    ensure_started()


def main():
    parser = argparse.ArgumentParser(description="CF 点监控后台")
    parser.add_argument("--host", default=os.environ.get("CFMON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CFMON_PORT", 8770)))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    ensure_started()
    print("=" * 56)
    print(" CF 点监控后台已启动")
    print(" 访问地址：http://%s:%s" % (args.host, args.port))
    print(" 数据库：%s" % db.DB_PATH)
    print("=" * 56)
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
