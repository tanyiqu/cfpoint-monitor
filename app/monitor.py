# -*- coding: utf-8 -*-
"""
后台采集调度。

一个常驻线程按配置的间隔轮询账号、比对 CF 点、写快照、触发 Webhook；
另有一条独立的“保活心跳”时间线，定期用各账号 Cookie 访问活动页。

设计要点
--------
* 单线程串行：账号之间加 1.2~3 秒随机停顿，模拟真人切换，不做并发轰炸。
* 抖动：轮询与心跳周期都乘一个随机系数（默认 ±15%），避免固定节奏被识别。
* 失败降速：Cookie 失效后不再按原频率打，改为 ≥30 分钟才探一次，
  防止无效高频请求把 IP / 账号拉进风控，那样连换新 Cookie 都会失败。
* 可重入保护：接口触发的“立即刷新”和后台线程不会同时查同一个账号。
"""

import random
import threading
import time
from datetime import datetime

import cf_client
import db
import webhook


def _to_int(value, default=None):
    if value is None:
        return default
    text = str(value).replace(",", "").replace(" ", "").strip()
    if text == "":
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _jitter(seconds, ratio):
    """给周期加随机抖动，返回实际等待秒数。"""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = 0.0
    r = max(0.0, min(0.5, r))
    return seconds * (1 + random.uniform(-r, r))


class Monitor:

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._locks = {}
        self._locks_guard = threading.Lock()
        self.next_poll_at = 0.0
        self.next_keep_at = 0.0
        self.last_poll_at = 0.0
        self.last_keep_at = 0.0
        self.last_activity = ""
        self._last_prune = 0.0

    # ------------------------------------------------------------ 线程管理
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cf-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _lock_for(self, account_id):
        with self._locks_guard:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    def _loop(self):
        # 启动后先做一轮，服务重启不必等一个完整周期
        self.next_poll_at = time.time()
        self.next_keep_at = time.time() + 20
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                     # 线程不能因为单次异常死掉
                self.last_activity = "调度异常：%s" % exc
            self._stop.wait(1.0)

    # ------------------------------------------------------------ 调度
    def tick(self):
        settings = db.get_settings()
        now = time.time()

        if settings.get("monitor_paused") == "1":
            self.next_poll_at = now + 3
            self.next_keep_at = now + 3
            self.last_activity = "已暂停"
            return

        interval = _clamp(settings.get("poll_interval"), 20, 86400, 300)
        ratio = settings.get("jitter", "0.15")

        # ---- 采集 ----
        if now >= self.next_poll_at:
            self.next_poll_at = now + _jitter(interval, ratio)
            self.last_poll_at = now
            self._poll_all(settings, interval)

        # ---- 保活 ----
        if settings.get("keepalive_enabled") == "1" and now >= self.next_keep_at:
            ka_interval = _clamp(settings.get("keepalive_interval"), 120, 86400, 900)
            self.next_keep_at = now + _jitter(ka_interval, ratio)
            self.last_keep_at = now
            self._keepalive_all(settings)

        # ---- 每天清一次历史 ----
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                db.prune_snapshots(_clamp(settings.get("history_keep_days"), 1, 3650, 30))
            except Exception:
                pass

    def _poll_all(self, settings, interval):
        accounts = db.list_accounts(enabled_only=True)
        if not accounts:
            self.last_activity = "暂无启用账号"
            return
        for acc in accounts:
            if self._stop.is_set():
                return
            if self._should_skip(acc, interval, settings):
                continue
            self.check_account(acc["id"], settings=settings, source="auto")
            cf_client.random_pause()
        self.last_activity = "%s 完成 %d 个账号采集" % (
            datetime.now().strftime("%H:%M:%S"), len(accounts))

    def _should_skip(self, account, interval, settings):
        """Cookie 失效的账号降速，避免无谓请求。"""
        if account.get("status") != "expired":
            return False
        backoff = max(interval * 10, 1800)
        last = account.get("last_check") or ""
        if not last:
            return False
        try:
            elapsed = (datetime.now()
                       - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except ValueError:
            return False
        return elapsed < backoff

    # ------------------------------------------------------------ 单账号采集
    def check_account(self, account_id, settings=None, source="auto", notify=True):
        """
        查询一个账号并落库。返回 dict：
            {"ok": bool, "cfpoint": int|None, "changed": bool,
             "status": str, "message": str, "duration": float}
        """
        lock = self._lock_for(account_id)
        if not lock.acquire(blocking=False):
            return {"ok": False, "cfpoint": None, "changed": False,
                    "status": "busy", "message": "该账号正在查询中", "duration": 0}

        started = time.time()
        try:
            settings = settings or db.get_settings()
            account = db.get_account(account_id)
            if not account:
                return {"ok": False, "cfpoint": None, "changed": False,
                        "status": "missing", "message": "账号不存在", "duration": 0}

            timeout = _clamp(settings.get("request_timeout"), 5, 60, 15)
            try:
                result = cf_client.query(account["cookie"], timeout=timeout)
            except cf_client.CFError as exc:
                return self._handle_failure(account, exc, settings, notify, started)
            except Exception as exc:
                err = cf_client.CFError("查询异常：%s" % exc, "error")
                return self._handle_failure(account, err, settings, notify, started)

            return self._handle_success(account, result, settings, notify, started)

        finally:
            lock.release()

    def _handle_success(self, account, result, settings, notify, started):
        coins = result["coins"] or {}
        personal = result["personal"] or {}
        new_point = _to_int(coins.get("cfPoint"))
        prev_point = account.get("cfpoint")
        was_status = account.get("status") or "unknown"
        changed = prev_point is not None and new_point is not None and new_point != prev_point

        fields = {
            "cfpoint": new_point,
            "coins_json": _dump(coins),
            "status": "ok",
            "status_msg": "",
            "fail_count": 0,
            "last_check": db.now_str(),
            "last_ok": db.now_str(),
        }
        if personal.get("roleName"):
            fields["role_name"] = str(personal.get("roleName"))
        if personal.get("lev") not in (None, ""):
            fields["level"] = str(personal.get("lev"))
        if personal.get("regDate"):
            fields["reg_date"] = str(personal.get("regDate"))
        if personal.get("playCnt") not in (None, ""):
            fields["play_cnt"] = _to_int(personal.get("playCnt"), 0)
        if changed:
            fields["last_change"] = db.now_str()

        db.update_account(account["id"], **fields)
        db.add_snapshot(account["id"], new_point, fields["coins_json"])

        account = dict(account)
        account.update(fields)

        if notify and changed:
            delta = new_point - prev_point
            self._notify_change(account, prev_point, new_point, delta, settings)
        elif notify and was_status in ("expired", "error", "bind", "unknown") and prev_point is not None:
            self._notify("recover", "监控已恢复：%s" % account["name"],
                         "【监控恢复】%s\nCookie 已恢复正常，当前 CF点 %s。\n时间：%s"
                         % (account["name"], _fmt(new_point), db.now_str()),
                         account, after=new_point, settings=settings)

        return {
            "ok": True,
            "cfpoint": new_point,
            "changed": changed,
            "status": "ok",
            "message": "查询成功",
            "duration": time.time() - started,
            "coins": coins,
            "personal": personal,
        }

    def _handle_failure(self, account, exc, settings, notify, started):
        fail_count = (account.get("fail_count") or 0) + 1
        was_status = account.get("status") or "unknown"
        if exc.is_expired:
            status = "expired"
        elif exc.kind == "bind":
            status = "bind"
        else:
            status = "error"

        db.update_account(account["id"], status=status,
                          status_msg=str(exc)[:500],
                          fail_count=fail_count,
                          last_check=db.now_str())

        if notify and status == "expired" and was_status != "expired":
            self._notify("expired", "Cookie 已失效：%s" % account["name"],
                         "【Cookie 失效】%s\n原因：%s\n请重新登录活动页，F12 复制 Cookie 后更新该账号。\n时间：%s"
                         % (account["name"], str(exc), db.now_str()),
                         account, settings=settings)

        if notify and status == "error":
            threshold = _to_int(settings.get("error_notify_after"), 3) or 3
            if fail_count >= threshold and (was_status != "error" or fail_count % max(threshold, 1) == 0):
                self._notify("error", "采集连续失败：%s" % account["name"],
                             "【采集异常】%s\n已连续失败 %d 次：%s\n时间：%s"
                             % (account["name"], fail_count, str(exc), db.now_str()),
                             account, settings=settings)

        return {
            "ok": False,
            "cfpoint": account.get("cfpoint"),
            "changed": False,
            "status": status,
            "message": str(exc),
            "kind": exc.kind,
            "duration": time.time() - started,
        }

    # ------------------------------------------------------------ 保活
    def _keepalive_all(self, settings):
        accounts = db.list_accounts(enabled_only=True)
        timeout = _clamp(settings.get("request_timeout"), 5, 60, 15)
        for acc in accounts:
            if self._stop.is_set():
                return
            if acc.get("status") == "expired":
                continue
            self.keepalive(acc["id"], timeout=timeout)
            time.sleep(random.uniform(1.5, 3.5))
        self.last_activity = "%s 保活心跳完成" % datetime.now().strftime("%H:%M:%S")

    def keepalive(self, account_id, timeout=15):
        account = db.get_account(account_id)
        if not account:
            return {"ok": False, "detail": "账号不存在"}
        ok, detail = cf_client.keepalive(account["cookie"], timeout=timeout)
        db.add_keepalive_log(account_id, "ok" if ok else "fail", detail)
        return {"ok": ok, "detail": detail}

    # ------------------------------------------------------------ 通知
    def _notify_change(self, account, before, after, delta, settings):
        sign = "+%d" % delta if delta > 0 else str(delta)
        role = account.get("role_name") or "-"
        message = ("【CF点变动】%s\n角色：%s\n%s → %s（%s）\n时间：%s"
                   % (account["name"], role, _fmt(before), _fmt(after), sign, db.now_str()))
        self._notify("change", "CF点变动：%s %s → %s" % (account["name"], _fmt(before), _fmt(after)),
                     message, account, before=before, after=after, delta=delta,
                     settings=settings, extra=sign)

    def _notify(self, event, title, message, account, before=None, after=None,
                delta=None, settings=None, extra=""):
        event_id = db.add_event(account["id"], event, title, message)
        settings = settings or db.get_settings()

        if settings.get("webhook_enabled") != "1":
            db.update_event_push(event_id, "skipped", "Webhook 未启用")
            return False
        flag = {"change": "notify_on_change", "expired": "notify_on_expired",
                "recover": "notify_on_recover", "error": "notify_on_error"}.get(event)
        if flag and settings.get(flag) != "1":
            db.update_event_push(event_id, "skipped", "该类通知已关闭")
            return False

        ctx = webhook.make_context(event, title, message, account=account,
                                   before=before, after=after, delta=delta, extra=extra)
        ok, detail = webhook.send(settings, ctx)
        if ok is None:
            db.update_event_push(event_id, "skipped", detail)
            return False
        db.update_event_push(event_id, "success" if ok else "failed", detail)
        return bool(ok)

    # ------------------------------------------------------------ 状态
    def status(self):
        settings = db.get_settings()
        now = time.time()
        return {
            "running": self.running,
            "paused": settings.get("monitor_paused") == "1",
            "poll_interval": _to_int(settings.get("poll_interval"), 300),
            "keepalive_enabled": settings.get("keepalive_enabled") == "1",
            "keepalive_interval": _to_int(settings.get("keepalive_interval"), 900),
            "next_poll_in": max(0, int(self.next_poll_at - now)) if self.running else None,
            "next_keep_in": max(0, int(self.next_keep_at - now)) if self.running else None,
            "last_poll_at": _fmt_time(self.last_poll_at),
            "last_keep_at": _fmt_time(self.last_keep_at),
            "last_activity": self.last_activity,
        }


def _dump(obj):
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _fmt(value):
    if value is None:
        return "-"
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return str(value)


def _fmt_time(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# 全局单例
monitor = Monitor()
