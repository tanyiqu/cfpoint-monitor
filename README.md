# CF 点监控

实时监控穿越火线账号里的 **CF 点**变化，一旦发生变动就通过 Webhook 推送消息。
自带一个本地后台网页，可以管理多个账号、随时实时预览、自由调整采集频率。

底层查询逻辑复用已有的 `cf_cfpoint.py`（**该文件未做任何修改**，仅作为模块导入调用其
`parse_cookie` / `build_payload` / `call_ide` / `extract_assets` 四个函数）。

---

## 功能

| 模块 | 说明 |
| --- | --- |
| 总览 | 账号数、CF 点合计、正常/异常数量、下次采集倒计时、账号卡片 + 迷你走势图 |
| 账号管理 | 添加/编辑/删除账号，粘贴 Cookie 自动解析校验，一键立即采集、手动保活 |
| 实时预览 | 点击任意账号弹出抽屉，直连接口拉取最新数据（角色名、军衔、注册时间、对局次数、六种货币）+ 历史走势曲线，**不写入采集历史** |
| Webhook | 7 种推送目标、4 类触发场景、自定义请求头与 Body 模板，支持一键测试 |
| 采集设置 | 轮询间隔（预设 + 任意秒数）、随机抖动、请求超时、历史保留天数、一键暂停 |
| 事件日志 | 每一次变动、失效、推送结果都留痕，可筛选、可清空 |

## 快速开始

```bash
# Windows 双击 start.bat 即可（首次会自动建虚拟环境并装依赖）

# 或者手动：
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
.venv\Scripts\python app.py
```

打开 <http://127.0.0.1:8770>

```bash
python app.py --host 0.0.0.0 --port 9000   # 换端口 / 局域网访问
```

依赖只有 Flask 一个，其余全是标准库。

## 怎么获取 Cookie

1. 用目标账号登录 <https://cf.qq.com/cp/a20241022query/page.html?type=9>
2. `F12` → **Network** → 过滤 `ide`
3. 点开对 `cf.ams.game.qq.com/ide/` 的 **POST** 请求
4. **Request Headers** → `Cookie`，整行复制
5. 后台「账号管理 → 添加账号」，粘贴进去，点「添加并验证」

`openid` / `access_token` / `appid` / `acctype` 会自动从 Cookie 里解析，不用手填。
校验失败时仍可选择「仍然保存」（例如先在游戏里绑定大区再来修）。

## 关于 Cookie 保活

先说结论：**没有任何手段能让 Cookie 永久有效**，`access_token` 是 OAuth 凭证，到期就是到期。
本项目能做的是把「本来 3 天就死」拉长到「能撑多久撑多久」，并且在失效时第一时间告诉你。

具体做了这些：

1. **固定浏览器指纹**
   所有请求的 `User-Agent`、`Accept`、`Accept-Language`、`Referer`、`Origin` 都与真实页面完全一致，
   Cookie 也按浏览器原始顺序回放，避免因为「请求特征异常」被提前判死。

2. **页面心跳（核心）**
   每隔 `keepalive_interval`（默认 15 分钟），用该账号的 Cookie 真正 GET 一次活动页
   `page.html?type=9`，再跟一次 `cf.qq.com` 首页。
   这正是真人重新打开页面的行为，服务端会据此刷新会话活跃时间——
   只打 `ide` 接口不会续页面会话，只打页面才是真正的续命动作。

3. **随机抖动**
   轮询和心跳的周期都乘一个 `1 ± 15%` 的随机系数，账号之间还有 1~3 秒随机停顿，
   避免「整点、等间隔」这种明显的机器节奏。

4. **失效后主动降速**
   一旦返回 `101 / -127 / -9999`，立刻把账号标记为「Cookie 失效」，
   把探测频率从「每 5 分钟」降到「至少每 30 分钟一次」。
   这一步很关键：失效后继续高频请求，很容易把这个 IP 打进风控名单，
   到时候连换新 Cookie 都查不出来。

5. **凭证寿命可视化**
   记录每个 Cookie 的贴入时间，界面上显示「Cookie 已用 X 天」，
   建议 15~20 天主动换一次，别等它自己挂。

6. **串行不并发**
   所有账号排队采集，绝不同时打接口，降低整体风险。

7. **不做自动重登**
   自动刷新 token 需要账号密码或扫码，风险高、也更容易触发风控，本项目不碰这条线。
   失效后通过 Webhook 通知你人工换 Cookie，这是最稳的做法。

## Webhook 支持的目标

| 类型 | 地址填什么 | 备注 |
| --- | --- | --- |
| 通用 JSON | 你的服务地址 | POST 结构化 JSON，见下方示例 |
| 企业微信 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` | 群机器人 |
| 钉钉 | `https://oapi.dingtalk.com/robot/send?access_token=...` | 安全设置选了「加签」时把 SEC 密钥填进「钉钉加签密钥」 |
| 飞书 | `https://open.feishu.cn/open-apis/bot/v2/hook/...` | 群机器人 |
| Server酱 | `https://sctapi.ftqq.com/SCTxxxx.send` | 推送到微信 |
| Bark | `https://api.day.app/你的Key` | 推送到 iOS |
| 自定义 | 任意地址 | Body 自己写模板，变量见下 |

通用 JSON 的推送体：

```json
{
  "event": "change",
  "event_label": "CF点变动",
  "title": "CF点变动：主号 3,956 → 4,000",
  "message": "【CF点变动】主号\n角色：孤影\n3,956 → 4,000（+44）\n时间：2026-11-03 11:20:00",
  "account": { "id": 1, "name": "主号", "role": "孤影" },
  "cfpoint": { "before": 3956, "after": 4000, "delta": 44, "current": 4000 },
  "time": "2026-11-03 11:20:00",
  "extra": "+44"
}
```

自定义模板可用变量：`{{event}}` `{{event_label}}` `{{title}}` `{{message}}` `{{account}}`
`{{account_id}}` `{{role}}` `{{openid_tail}}` `{{before}}` `{{after}}` `{{delta}}`
`{{delta_text}}` `{{cfpoint}}` `{{time}}` `{{extra}}`

触发场景：CF 点变化 / Cookie 失效 / 从异常恢复 / 连续采集失败（阈值可调）。

## 目录结构

```
cfpoint-monitor/
├─ cf_cfpoint.py        # 原始查询脚本（未修改，被当作库导入）
├─ cf_client.py         # 在其之上封装：错误分类 + 保活心跳
├─ db.py                # SQLite 数据层（账号 / 快照 / 事件 / 配置）
├─ monitor.py           # 后台调度线程：轮询、比对、推送、保活
├─ webhook.py           # 7 种推送目标 + 模板渲染
├─ app.py               # Flask 后端与 REST API
├─ templates/index.html # 后台页面
├─ static/css/app.css
├─ static/js/app.js
├─ data/cfmon.db        # 运行后自动生成（含 Cookie，注意保密）
├─ requirements.txt
└─ start.bat            # 一键启动
```

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 账号 + 配置 + 汇总 + 调度状态 |
| POST | `/api/accounts` | 新增账号 |
| PUT | `/api/accounts/<id>` | 修改账号 |
| DELETE | `/api/accounts/<id>` | 删除账号 |
| GET | `/api/accounts/<id>/cookie` | 查看完整 Cookie |
| GET | `/api/accounts/<id>/preview` | 实时预览（不落库） |
| GET | `/api/accounts/<id>/history` | 历史快照 |
| POST | `/api/accounts/<id>/refresh` | 立即采集并落库 |
| POST | `/api/accounts/<id>/keepalive` | 手动保活 |
| POST | `/api/settings` | 保存配置 |
| POST | `/api/webhook/test` | 发送测试推送 |
| POST | `/api/monitor/pause` | 暂停 / 恢复采集 |
| GET | `/api/events` | 事件日志 |
| POST | `/api/events/clear` | 清空日志 |

## 注意事项

- **Cookie 就是账号凭证**，`data/cfmon.db` 里存的是明文，已在 `.gitignore` 中排除，别外传、别提交。
- 服务默认只监听 `127.0.0.1`，如果要用 `--host 0.0.0.0` 开放到局域网，请自行加访问控制。
- 采集频率不建议低于 60 秒，账号多的时候更要放宽，别把接口当压力测试。
- 本项目只读取公开活动页展示的数据，请自行控制频率并遵守平台规则。
