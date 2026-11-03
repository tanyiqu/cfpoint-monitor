# -*- coding: utf-8 -*-
"""
CF 点接口客户端。

这里 **不修改** cf_cfpoint.py，而是把它当成一个“库”导入，复用它已经调通的：
    - parse_cookie()   Cookie 解析
    - build_payload()  请求体组装（iChartId / sIdeToken / 登录凭证）
    - call_ide()       POST 请求
    - extract_assets() 返回码校验 + 取 coins/personalMsg

在此之上补充三件事：
    1. 把各种失败归类（Cookie 过期 / 未绑定大区 / 网络异常），便于界面区分提示；
    2. 提供 keepalive()：用同一套请求指纹 GET 活动页，给 Cookie 续命；
    3. 统一异常类型 CFError，交给上层记录状态。
"""

import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cf_cfpoint as cf

PAGE_URL = cf.REFERER                      # 活动页，保活心跳打这里
HOME_URL = "https://cf.qq.com/"            # 站点首页，第二跳
IDE_ORIGIN = cf.ORIGIN

# 这些返回码说明登录态没了，不是“临时错误”，不应该高频重试
EXPIRED_CODES = {101, -127, -9999, 1001}
# 这些说明账号还没绑定大区角色，需要人工处理
BIND_CODES = {99998, -991003}

ACCEPT_HTML = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8")
ACCEPT_JSON = "application/json, text/javascript, */*; q=0.01"


class CFError(Exception):
    """kind: expired / bind / cookie / network / error"""

    def __init__(self, message, kind="error", code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code

    @property
    def is_expired(self):
        return self.kind in ("expired", "cookie")


# ---------------------------------------------------------------- 内部工具

def parse_credentials(cookie_raw):
    """解析 Cookie，返回 (jar, creds)。"""
    jar = cf.parse_cookie(cookie_raw)
    creds = {
        "acctype": jar.get("acctype") or "qc",
        "openid": jar.get("openid", ""),
        "access_token": jar.get("access_token", ""),
        "appid": jar.get("appid") or cf.DEFAULT_APPID,
    }
    return jar, creds


def cookie_header(jar):
    """按原始顺序拼回 Cookie 头，尽量和浏览器一致。"""
    return "; ".join("%s=%s" % (k, v) for k, v in jar.items())


def _browser_headers(cookie_str, ajax=False):
    """统一的浏览器指纹：UA / Accept / Referer / Origin 全部对齐真实页面。"""
    if ajax:
        return {
            "User-Agent": cf.USER_AGENT,
            "Accept": ACCEPT_JSON,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": PAGE_URL,
            "Origin": IDE_ORIGIN,
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_str,
        }
    return {
        "User-Agent": cf.USER_AGENT,
        "Accept": ACCEPT_HTML,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Cookie": cookie_str,
    }


def _http_get(url, headers, timeout=15):
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        resp.read(2048)   # 只需要让服务端记一次会话活跃，内容不重要
        return resp.status


# ---------------------------------------------------------------- 对外接口

def query(cookie_raw, timeout=15):
    """
    查询一次 CF 点。成功返回 dict：
        {"coins": {...}, "personal": {...}, "raw": {...}}
    失败抛 CFError。
    """
    jar, creds = parse_credentials(cookie_raw)
    if not creds["openid"] or not creds["access_token"]:
        raise CFError(
            "Cookie 里没有 openid / access_token，请确认粘贴的是登录后请求 "
            "cf.ams.game.qq.com/ide/ 那条的完整 Cookie", "cookie")

    payload = cf.build_payload(creds)
    try:
        data = cf.call_ide(jar, payload, timeout=timeout)
    except HTTPError as exc:
        raise CFError("接口返回 HTTP %s" % exc.code, "network")
    except URLError as exc:
        raise CFError("网络请求失败：%s" % getattr(exc, "reason", exc), "network")
    except RuntimeError as exc:
        raise CFError(str(exc), "error")
    except Exception as exc:                       # 超时等
        raise CFError("请求异常：%s" % exc, "network")

    # ---- 返回码判别（和原脚本口径一致）----
    ok = str(data.get("iRet")) == "0" or str(data.get("ret")) == "0"
    if not ok:
        raw_code = data.get("iRet", data.get("ret"))
        msg = data.get("sMsg") or "未知错误"
        code = None
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            pass
        if code in EXPIRED_CODES:
            kind = "expired"
        elif code in BIND_CODES:
            kind = "bind"
        else:
            kind = "error"
        hint = cf.RET_MESSAGES.get(code, "")
        raise CFError("%s%s" % (msg, ("（%s）" % hint) if hint else ""), kind, code)

    try:
        coins, personal = cf.extract_assets(data)
    except RuntimeError as exc:
        raise CFError(str(exc), "error")

    return {"coins": coins, "personal": personal, "raw": data}


def keepalive(cookie_raw, timeout=15):
    """
    Cookie 保活心跳：模拟用户重新打开一次活动页。

    为什么这样做有效
    ----------------
    活动页加载时会向 cf.qq.com 及其登录态域带 Cookie 发请求，
    服务端据此刷新会话的活跃时间。只发 ide 接口不会刷新页面会话，
    只发页面请求则最接近真人行为，是延长 Cookie 寿命性价比最高的动作。

    返回 (ok: bool, detail: str)
    """
    jar, _ = parse_credentials(cookie_raw)
    header = cookie_header(jar)
    if not header:
        return False, "Cookie 为空"

    detail = []
    ok_any = False
    for url in (PAGE_URL, HOME_URL):
        try:
            status = _http_get(url, _browser_headers(header), timeout=timeout)
            detail.append("%s -> %s" % (url, status))
            ok_any = True
        except Exception as exc:
            detail.append("%s -> %s" % (url, exc))
        # 两跳之间停顿一下，别像机器
        time.sleep(random.uniform(0.6, 1.4))
    return ok_any, " | ".join(detail)


def random_pause(lo=1.2, hi=3.0):
    """账号之间串行采集时的随机间隔。"""
    time.sleep(random.uniform(lo, hi))
