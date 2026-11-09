# -*- coding: utf-8 -*-
"""
Webhook 推送。

支持 7 种目标：
    generic     通用 JSON POST（自建服务、n8n、Node-RED 等）
    wecom       企业微信群机器人
    dingtalk    钉钉群机器人（可选加签）
    feishu      飞书群机器人
    serverchan  Server酱（Server酱³ / Turbo）
    bark        Bark（iOS）
    custom      完全自定义 body 模板

统一入口 send(settings, ctx) -> (ok, detail)
ctx 里放的是“已经算好”的字段，模板渲染和各类预设都从这里取值。
"""

import base64
import hashlib
import hmac
import json
import time
from urllib import parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "CFPointMonitor/1.0",
}

# 自定义模板可用的占位符
TPL_KEYS = [
    "event", "event_label", "title", "message", "account", "account_id",
    "role", "openid_tail", "before", "after", "delta", "delta_text",
    "cfpoint", "time", "extra",
]


def _post(url, body, headers, timeout=10):
    req = Request(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return True, "HTTP %s %s" % (resp.status, raw[:300])
    except HTTPError as exc:
        try:
            body_txt = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body_txt = ""
        return False, "HTTP %s %s" % (exc.code, body_txt)
    except URLError as exc:
        return False, "网络错误：%s" % getattr(exc, "reason", exc)
    except Exception as exc:
        return False, "推送异常：%s" % exc


def _dingtalk_sign(secret, timestamp_ms):
    string_to_sign = "%s\n%s" % (timestamp_ms, secret)
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256)
    return parse.quote_plus(base64.b64encode(h.digest()))


def _merge_headers(extra_json):
    headers = dict(DEFAULT_HEADERS)
    if extra_json and extra_json.strip():
        try:
            extra = json.loads(extra_json)
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
        except ValueError:
            pass
    return headers


def render_template(tpl, ctx):
    out = tpl
    for key in TPL_KEYS:
        out = out.replace("{{%s}}" % key, str(ctx.get(key, "")))
    return out


def build_body(wtype, settings, ctx):
    """按类型构造 (url, body_bytes, headers)。"""
    url = (settings.get("webhook_url") or "").strip()
    headers = _merge_headers(settings.get("webhook_headers"))
    title = ctx["title"]
    message = ctx["message"]

    if wtype == "wecom":
        payload = {"msgtype": "text", "text": {"content": message}}
        return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers

    if wtype == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": message}}
        secret = (settings.get("webhook_secret") or "").strip()
        if secret:
            ts = str(int(time.time() * 1000))
            sep = "&" if "?" in url else "?"
            url = "%s%stimestamp=%s&sign=%s" % (url, sep, ts, _dingtalk_sign(secret, ts))
        return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers

    if wtype == "feishu":
        payload = {"msg_type": "text", "content": {"text": message}}
        return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers

    if wtype == "serverchan":
        payload = {"title": title, "desp": message.replace("\n", "\n\n")}
        return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers

    if wtype == "bark":
        payload = {"title": title, "body": message, "group": "CF点监控",
                   "icon": "https://cf.qq.com/favicon.ico"}
        return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers

    if wtype == "custom":
        tpl = settings.get("webhook_body_tpl") or ""
        rendered = render_template(tpl, ctx)
        return url, rendered.encode("utf-8"), headers

    # generic
    payload = {
        "event": ctx["event"],
        "event_label": ctx["event_label"],
        "title": title,
        "message": message,
        "account": {
            "id": ctx["account_id"],
            "name": ctx["account"],
            "role": ctx["role"],
        },
        "cfpoint": {
            "before": ctx["before"],
            "after": ctx["after"],
            "delta": ctx["delta"],
            "current": ctx["cfpoint"],
        },
        "time": ctx["time"],
        "extra": ctx["extra"],
    }
    return url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers


def send(settings, ctx):
    """发送一条推送，返回 (ok, detail)。未启用时返回 (None, "未启用")。"""
    wtype = settings.get("webhook_type") or "generic"
    url = (settings.get("webhook_url") or "").strip()
    if not url and wtype != "custom":
        return None, "未配置 Webhook 地址"
    try:
        timeout = float(settings.get("webhook_timeout") or 10)
    except (TypeError, ValueError):
        timeout = 10

    url, body, headers = build_body(wtype, settings, ctx)
    if not url:
        return None, "未配置 Webhook 地址"
    if not body:
        return None, "推送内容为空"
    return _post(url, body, headers, timeout)


def make_context(event, title, message, account=None, before=None, after=None,
                 delta=None, extra=None):
    """组装模板/预设共用的字段表。"""
    account = account or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if delta is None and before is not None and after is not None:
        try:
            delta = int(after) - int(before)
        except (TypeError, ValueError):
            delta = None
    if delta is None:
        delta_text = ""
    elif delta > 0:
        delta_text = "+%d" % delta
    else:
        delta_text = str(delta)

    openid = account.get("openid") or ""
    labels = {
        "change": "CF点变动",
        "expired": "Cookie 失效",
        "recover": "监控恢复",
        "error": "采集异常",
        "digest": "定时采集汇总",
        "keepalive": "保活异常",
        "test": "测试推送",
        "info": "通知",
    }
    return {
        "event": event,
        "event_label": labels.get(event, event),
        "title": title,
        "message": message,
        "account": account.get("name") or "未命名账号",
        "account_id": account.get("id") or "",
        "role": account.get("role_name") or "",
        "openid_tail": openid[-6:] if openid else "",
        "before": "" if before is None else before,
        "after": "" if after is None else after,
        "delta": "" if delta is None else delta,
        "delta_text": delta_text,
        "cfpoint": "" if after is None else after,
        "time": now,
        "extra": extra or "",
    }
