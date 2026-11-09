# -*- coding: utf-8 -*-
"""
后台采集调度。

一个常驻线程负责采集账号、比对 CF 点、写快照、触发 Webhook；
另有一条独立的“保活心跳”时间线，定期用各账号 Cookie 访问活动页。

两种采集模式（settings.schedule_mode）
------------------------------------
* interval  实时采集：按 poll_interval 循环轮询，只在 CF 点发生变化时推送。
* daily     定时采集：每天在 daily_times 指定的时间点各跑一轮；
            提醒方式由 daily_notify_mode 决定——
            change  仅在有变化时逐账号推送；
            always  每轮结束后发一条汇总推送（含全部账号当前数据）。
            保活心跳与采集模式无关，始终按 keepalive_* 配置独立运行。

设计要点
--------
* 单线程串行：账号之间加 1.2~3 秒随机停顿，模拟真人切换，不做并发轰炸。
* 抖动：轮询与心跳周期都乘一个随机系数（默认 ±15%），避免固定节奏被识别。
* 失败降速：Cookie 失效后不再按原频率打，改为 ≥30 分钟才探一次，
  防止无效高频请求把 IP / 账号拉进风控，那样连换新 Cookie 都会失败。
* 可重入保护：接口触发的“立即刷新”和后台线程不会同时查同一个账号。
"""

import random
import re
import threading
import time
from datetime import datetime, timedelta

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


# ---------------------------------------------------------------- 采集模式
# 采集模式相关的小工具都放在模块级，app.py 校验配置时也复用同一套解析逻辑，
# 避免前后端出现两种“时间点格式”。

# 定时模式下每天最多允许的时间点数量，防止误输入把接口当压力测试
MAX_DAILY_TIMES = 24

STATUS_TEXT = {
    "ok": "正常",
    "expired": "Cookie 失效",
    "bind": "未绑定大区",
    "error": "采集失败",
    "busy": "查询繁忙",
    "missing": "账号不存在",
}


def _mode(settings):
    """当前采集模式，取值非法时一律按实时采集处理。"""
    return "daily" if str(settings.get("schedule_mode") or "").strip() == "daily" else "interval"


def parse_daily_times(text):
    """
    解析 "06:00,18:30" 形式的时间点列表。

    返回规范化后的 ["06:00", "18:30"]：24 小时制、补零、去重、升序；
    非法片段直接丢弃，由调用方决定是否提示用户。
    超过 MAX_DAILY_TIMES 的部分会被截断。
    """
    out = []
    for chunk in re.split(r"[,;，；\s]+", str(text or "").strip()):
        if not chunk:
            continue
        match = re.match(r"^(\d{1,2})[:：](\d{1,2})$", chunk)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        slot = "%02d:%02d" % (hour, minute)
        if slot not in out:
            out.append(slot)
    return sorted(out)[:MAX_DAILY_TIMES]


def _slots(daily_times):
    """"06:00,18:30" -> [(6, 0), (18, 30)]，供 next_daily_at 使用。"""
    return [(int(t[:2]), int(t[3:])) for t in parse_daily_times(daily_times)]


def next_daily_at(slots, now=None):
    """
    计算下一次命中的时间戳（epoch 秒）。

    只在“今天 / 明天”两天的候选里取最小值，因此最多向前看 24 小时；
    slots 为空时返回 None。留 5 秒余量，避免同一分钟内反复触发。
    """
    if not slots:
        return None
    now = time.time() if now is None else now
    base = datetime.fromtimestamp(now)
    best = None
    for offset in (0, 1):
        day = base + timedelta(days=offset)
        for hour, minute in slots:
            target = datetime(day.year, day.month, day.day, hour, minute)
            ts = target.timestamp()
            if ts > now + 5 and (best is None or ts < best):
                best = ts
    return best


def _status_text(status):
    return STATUS_TEXT.get(status, status or "未知")


def _delta_note(before, after):
    """汇总里用的变动后缀：持平 / (+44) / (-44)。"""
    if before is None or after is None:
        return ""
    diff = after - before
    if diff == 0:
        return "（持平）"
    return "（%s）" % ("+%d" % diff if diff > 0 else str(diff))


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
        # 启动后按当前模式排期：实时采集立刻跑一轮，定时采集直接等到下一个时间点
        self.reschedule()
        self.next_keep_at = time.time() + 20
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                     # 线程不能因为单次异常死掉
                self.last_activity = "调度异常：%s" % exc
            self._stop.wait(1.0)

    # ------------------------------------------------------------ 调度
    def reschedule(self, run_now=True):
        """
        按当前配置重算下一次采集时间，配置变更后由接口层调用。

        * 实时采集：run_now=True 时下一轮立刻开始（保存配置 / 新增账号后的即时反馈），
          否则等一个完整周期。
        * 定时采集：永远跳到下一个配置时间点，改配置不会额外提前打接口。
        """
        settings = db.get_settings()
        now = time.time()
        if _mode(settings) == "daily":
            slots = _slots(settings.get("daily_times"))
            self.next_poll_at = next_daily_at(slots, now) or (now + 3600)
            return
        if run_now:
            self.next_poll_at = now
        else:
            interval = _clamp(settings.get("poll_interval"), 20, 86400, 300)
            self.next_poll_at = now + _jitter(interval, settings.get("jitter", "0.15"))

    def tick(self):
        settings = db.get_settings()
        now = time.time()

        if settings.get("monitor_paused") == "1":
            self.next_poll_at = now + 3
            self.next_keep_at = now + 3
            self.last_activity = "已暂停"
            return

        ratio = settings.get("jitter", "0.15")

        # ---- 采集（按模式分派）----
        if now >= self.next_poll_at:
            self.last_poll_at = now
            if _mode(settings) == "daily":
                self._run_daily(settings, now)
            else:
                self._run_interval(settings, now, ratio)

        # ---- 保活（与采集模式无关，保持原有逻辑）----
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

    def _run_interval(self, settings, now, ratio):
        """实时采集：排下一轮并轮询全部账号。"""
        interval = _clamp(settings.get("poll_interval"), 20, 86400, 300)
        self.next_poll_at = now + _jitter(interval, ratio)
        self._poll_all(settings, interval)

    def _run_daily(self, settings, now):
        """
        定时采集：跑到点的一轮，并把下一轮排到后面的时间点。

        定时模式一天只有几次请求，失效账号没必要再做“≥30 分钟”的降速保护，
        因此 _should_skip 的 interval 固定传 180（退避下限仍是 30 分钟）。
        """
        slots = _slots(settings.get("daily_times"))
        if not slots:
            # 配置缺失时退化为一小时一次，至少别让监控彻底停摆
            self.next_poll_at = now + 3600
            self.last_activity = "定时采集未配置时间点，暂按 1 小时一次"
            return

        self.next_poll_at = next_daily_at(slots, now) or (now + 3600)
        # 定时模式同样加一点点随机偏移，避免每天整点分秒不差地打接口
        if _clamp(settings.get("jitter"), 0, 0.5, 0.15) > 0:
            self.next_poll_at += random.uniform(0, 30)

        digest = settings.get("daily_notify_mode") == "always"
        self._poll_all(settings, 180, digest=digest)

    def _poll_all(self, settings, interval, digest=False):
        """
        串行采集全部启用账号。

        interval  只用于 _should_skip 的失效降速计算
        digest    True 表示本轮不做单账号通知，改在一轮结束后发一条汇总
        """
        accounts = db.list_accounts(enabled_only=True)
        if not accounts:
            self.last_activity = "暂无启用账号"
            return
        results = []
        for acc in accounts:
            if self._stop.is_set():
                return
            if self._should_skip(acc, interval, settings):
                continue
            result = self.check_account(acc["id"], settings=settings, source="auto",
                                        notify=not digest)
            results.append({"account": acc, "result": result})
            cf_client.random_pause()
        self.last_activity = "%s 完成 %d 个账号采集" % (
            datetime.now().strftime("%H:%M:%S"), len(results))
        if digest and results:
            self._notify_digest(results, settings)

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
    def check_account(self, account_id, settings=None, source="auto", notify=True,
                      notify_change=None):
        """
        查询一个账号并落库。返回 dict：
            {"ok": bool, "cfpoint": int|None, "changed": bool,
             "status": str, "message": str, "duration": float}

        notify         是否允许推送（失效 / 异常 / 恢复走这个开关）
        notify_change  是否允许推送 CF 点变动，默认跟随 notify；
                       定时汇总模式会把它关掉，把变动并入当天那一条汇总
        """
        if notify_change is None:
            notify_change = notify
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

            return self._handle_success(account, result, settings, notify, notify_change, started)

        finally:
            lock.release()

    def _handle_success(self, account, result, settings, notify, notify_change, started):
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

        if notify_change and changed:
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

    def _notify_digest(self, results, settings=None):
        """
        定时采集「每次采集后都提醒」：把本轮全部账号的结果汇总成一条推送。

        results 里每项是 {"account": 采集前的账号快照, "result": check_account 的返回值}，
        保留采集前快照是为了和本轮结果对比算变动。
        """
        settings = settings or db.get_settings()
        lines = []
        total = 0
        ok_count = 0
        for item in results:
            account = item["account"]
            result = item["result"]
            point = result.get("cfpoint")
            if result.get("ok"):
                ok_count += 1
                if point is not None:
                    total += point
                lines.append("%s：%s%s" % (account["name"], _fmt(point),
                                           _delta_note(account.get("cfpoint"), point)))
            else:
                lines.append("%s：%s｜%s" % (account["name"], _status_text(result.get("status")),
                                            str(result.get("message") or "无详情")[:80]))

        message = ("【定时采集】%s\n%s\n——\n合计 %s CF点 · 成功 %d/%d\n时间：%s"
                   % (datetime.now().strftime("%Y-%m-%d %H:%M"), "\n".join(lines),
                      _fmt(total), ok_count, len(results), db.now_str()))
        title = "定时采集：%d 个账号 · 合计 %s" % (len(results), _fmt(total))
        # 汇总不属于某一个账号，account_id 传 None，事件日志里按“全部账号”展示
        self._notify("digest", title, message, {"id": None, "name": "全部账号"},
                     after=total, settings=settings)

    def _notify(self, event, title, message, account, before=None, after=None,
                delta=None, settings=None, extra=""):
        event_id = db.add_event(account["id"], event, title, message)
        settings = settings or db.get_settings()

        if settings.get("webhook_enabled") != "1":
            db.update_event_push(event_id, "skipped", "Webhook 未启用")
            return False
        flag = {"change": "notify_on_change", "expired": "notify_on_expired",
                "recover": "notify_on_recover", "error": "notify_on_error",
                "digest": "notify_on_digest"}.get(event)
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
        mode = _mode(settings)
        return {
            "running": self.running,
            "paused": settings.get("monitor_paused") == "1",
            "schedule_mode": mode,
            "daily_times": settings.get("daily_times", ""),
            "daily_notify_mode": settings.get("daily_notify_mode", "change"),
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
