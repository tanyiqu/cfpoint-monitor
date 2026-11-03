# -*- coding: utf-8 -*-
"""
CF 个人信息总览 —— CF点查询脚本（纯标准库，无需安装依赖）

换账号怎么用
------------
只需要替换下面 CONFIG 区域里的 COOKIE 这一整段字符串（把浏览器里复制到的
整段 Cookie 原样粘进去即可），openid / access_token / appid / acctype 都会
自动从里面解析出来，其他参数（图表 id、ide token）所有账号通用，不用改。

Cookie 从哪来
-------------
1. 用目标账号登录 https://cf.qq.com/cp/a20241022query/page.html?type=9
2. F12 → Network → 过滤 `ide` → 点开对 cf.ams.game.qq.com/ide/ 的 POST 请求
3. Request Headers → Cookie，整行复制

原理
----
页面通过 Milo.emit() 向活动平台接口 POST 以下参数：

    POST https://cf.ams.game.qq.com/ide/
    Content-Type: application/x-www-form-urlencoded

    iChartId=354826        # 图表 id（个人信息总览 flow 的 mapid）
    iSubChartId=354826
    sIdeToken=j2CnLx
    ams_targetappid=wx79db870bffc454f4
    useItopPrdEnv=1
    needGopenid=1
    acctype=qc / openid=... / access_token=... / appid=...   # 登录凭证（关键）

返回：
    {"iRet":0,"jData":{"sData":{"coins":{"cfPoint":"3956", ...},
                                "personalMsg":{...}}}}

注意：
  * 登录凭证必须放在 **POST 请求体** 里；只放 Cookie 服务端不认，会返回
    {"iRet":101,"sMsg":"非常抱歉，请先登录！"}
  * access_token 有时效，过期后重新复制一次 Cookie 即可。
  * 参数来源：https://cf.qq.com/comm-htdocs/js/ams/actDesc/859/678859/gmi_act.desc.js
    （活动 678859 → flow 别名 205efa → mapid 354826 → ide token j2CnLx）

命令行用法
----------
    python cf_cfpoint.py                    # 用脚本里配置的 COOKIE
    python cf_cfpoint.py --cookie "..."     # 临时换一段 Cookie，不改文件
    python cf_cfpoint.py --json             # 输出原始 JSON，便于二次处理
"""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ======================================================================
#  ↓↓↓↓↓↓  配 置 区：换账号只改下面这一段  ↓↓↓↓↓↓
#  用两个双引号包住整段 Cookie，直接粘贴浏览器复制的内容即可（换行会被忽略）
# ======================================================================

COOKIE = """

"""


# ======================================================================
#  ↑↑↑↑↑↑  配 置 区 结 束（以下内容换账号不需要改动）  ↑↑↑↑↑↑
# ======================================================================


# ---------------------------------------------------------------- 常量配置

IDE_URL = "https://cf.ams.game.qq.com/ide/"

CHART_ID = "354826"        # 个人信息总览 flow 的 mapid
SUB_CHART_ID = "354826"
IDE_TOKEN = "j2CnLx"       # ide.flows["354826"].sIdeToken
AMS_TARGET_APPID = "wx79db870bffc454f4"

DEFAULT_APPID = "102800680"   # QQ 互联登录 appid（微信登录时以 Cookie 里的为准）

REFERER = "https://cf.qq.com/cp/a20241022query/page.html?type=9"
ORIGIN = "https://cf.qq.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# iRet 说明（来自页面 index.js 的 fail 分支）
RET_MESSAGES = {
    101: "登录态无效 / access_token 已过期，请重新从浏览器复制 Cookie",
    -127: "登录态失效，请重新进入活动页面",
    -9999: "登录已退出，请重新登录",
    99998: "需要先绑定游戏大区与角色",
    -991003: "需要先绑定游戏大区与角色",
}


# ---------------------------------------------------------------- 工具函数

def parse_cookie(cookie_str):
    """
    把整段 Cookie 字符串解析成字典。
    兼容浏览器复制的各种脏数据：换行、制表符、行首 Cookie: 前缀、多余空格。
    空值字段（如 refresh_token=）会被保留，部分字段本来就是空的。
    """
    text = (cookie_str or "").strip()
    # 去掉可能被一起复制进来的 "Cookie:" 前缀
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):]
    # 换行 / 制表符统一当作分隔符
    text = text.replace("\r", ";").replace("\n", ";").replace("\t", ";")

    jar = {}
    for item in text.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        # 冒号后可能残留空格，如 "openid: xxx"
        if not key:
            continue
        jar[key] = value
    return jar


def build_payload(creds):
    """组装 IDE 接口的 POST 请求体。"""
    return {
        # ---- 接口路由参数（决定查哪个 flow，全账号通用） ----
        "iChartId": CHART_ID,
        "iSubChartId": SUB_CHART_ID,
        "sIdeToken": IDE_TOKEN,
        "ams_targetappid": AMS_TARGET_APPID,
        "useItopPrdEnv": "1",
        "needGopenid": "1",
        # ---- 登录凭证（必须放在 body 里） ----
        "acctype": creds["acctype"],
        "openid": creds["openid"],
        "access_token": creds["access_token"],
        "appid": creds["appid"],
    }


def call_ide(cookie_jar, payload, timeout=15):
    """向 IDE 接口发起请求，返回解析后的 JSON dict。"""
    body = urlencode(payload).encode("utf-8")
    req = Request(
        IDE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": REFERER,
            "Origin": ORIGIN,
            "User-Agent": USER_AGENT,
            "Cookie": "; ".join("%s=%s" % (k, v) for k, v in cookie_jar.items()),
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("接口返回不是合法 JSON：%s" % raw[:300])


def extract_assets(data):
    """校验返回码并取出 coins / personalMsg。"""
    ok = str(data.get("iRet")) == "0" or str(data.get("ret")) == "0"
    if not ok:
        code = data.get("iRet", data.get("ret"))
        msg = data.get("sMsg", "未知错误")
        hint = ""
        if str(code).lstrip("-").isdigit():
            hint = RET_MESSAGES.get(int(code), "")
        raise RuntimeError(
            "查询失败 (iRet=%s)：%s%s\n原始返回：%s"
            % (code, msg, ("\n提示：" + hint) if hint else "", json.dumps(data, ensure_ascii=False))
        )

    sdata = (data.get("jData") or {}).get("sData") or {}
    coins = sdata.get("coins") or {}
    personal = sdata.get("personalMsg") or {}
    if "cfPoint" not in coins:
        raise RuntimeError("返回中没有 coins.cfPoint 字段：%s" % json.dumps(data, ensure_ascii=False))
    return coins, personal


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="查询 CF（穿越火线）个人信息总览中的 CF 点数量")
    ap.add_argument("--cookie", help="临时指定整段 Cookie（不修改脚本里的 COOKIE）")
    ap.add_argument("--openid", help="直接指定 openid，覆盖 Cookie 中的值")
    ap.add_argument("--access-token", dest="access_token", help="直接指定 access_token")
    ap.add_argument("--appid", help="直接指定 appid，默认取 Cookie 中的值")
    ap.add_argument("--json", action="store_true", help="输出接口原始 JSON")
    ap.add_argument("--timeout", type=float, default=15, help="超时秒数，默认 15")
    args = ap.parse_args()

    # 优先级：命令行 --cookie > 脚本里的 COOKIE；传空字符串表示完全不用 Cookie
    cookie_jar = parse_cookie(args.cookie if args.cookie is not None else COOKIE)

    creds = {
        "acctype": cookie_jar.get("acctype") or "qc",
        "openid": args.openid or cookie_jar.get("openid", ""),
        "access_token": args.access_token or cookie_jar.get("access_token", ""),
        "appid": args.appid or cookie_jar.get("appid") or DEFAULT_APPID,
    }
    if not creds["openid"] or not creds["access_token"]:
        sys.exit(
            "Cookie 里没解析到 openid / access_token。\n"
            "请确认粘贴的是登录 cf.qq.com 后、请求 cf.ams.game.qq.com/ide/ 那条的完整 Cookie。"
        )

    # 用命令行覆盖凭证时，同步覆盖 Cookie 里的同名字段，避免新旧账号混用
    if args.openid or args.access_token or args.appid:
        cookie_jar.update({k: v for k, v in creds.items() if v})

    try:
        data = call_ide(cookie_jar, build_payload(creds), timeout=args.timeout)
    except (HTTPError, URLError) as exc:
        sys.exit("网络请求失败：%s" % exc)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    try:
        coins, personal = extract_assets(data)
    except RuntimeError as exc:
        sys.exit(str(exc))

    print("=" * 42)
    print("CF 个人信息总览")
    print("=" * 42)
    if personal:
        print("角色名   : %s" % personal.get("roleName", "-"))
        print("军衔等级 : %s" % personal.get("lev", "-"))
        print("注册时间 : %s" % personal.get("regDate", "-"))
        print("对局次数 : %s" % personal.get("playCnt", "-"))
        print("-" * 42)
    print("CF点     : %s" % coins.get("cfPoint", "-"))
    print("火线币   : %s" % coins.get("cfCoin", "-"))
    print("GP点     : %s" % coins.get("gamePoint", "-"))
    print("红宝石   : %s" % coins.get("redGem", "-"))
    print("蓝宝石   : %s" % coins.get("blueGem", "-"))
    print("黄宝石   : %s" % coins.get("yellowGem", "-"))
    print("=" * 42)


if __name__ == "__main__":
    main()
