# SOP：读取微信群/好友「最近 N 条」消息

> 适用工具：atuowexin 插件（`wechat_conversations` / `wechat_messages` / `wechat_export` / `wechat_doctor`）
> 目标：用**最少工具调用**拿到某会话最近 N 条，不踩已知坑。

## 一、两步法（默认走这条）

| 步骤 | 调用 | 拿到什么 |
|---|---|---|
| 1 | `wechat_conversations(query="<关键词>")` | 会话**完整 id**（如 `12345678901@chatroom`）+ **消息数 N** + 最近时间 |
| 2 | `wechat_messages(conversation="<完整 id>", limit=N, offset=N-100)` | 最近 100 条，**升序**返回（最早 → 最新） |

示例（本次「25」群）：

```
wechat_conversations(query="25")
  → 25 | 12345678901@chatroom | group | 1876 | 1791211320

wechat_messages(conversation="12345678901@chatroom", limit=100, offset=1776)
  → 100 条，末条即最新（1791211320）
```

## 二、必须记住的 5 个点

1. **第 2 步永远传完整 id**。会话名可能多义：`"25"` 会同时匹配「25」群、「乐（高中同学，26…）」「2026年物天学院天文专业」「云大A605」4 个会话，用名字会被拒绝并浪费一次调用。
2. **没有 `tail`/`latest` 参数**。`offset` 的语义是「从最早跳过多少条」（`ORDER BY ts ASC LIMIT ? OFFSET ?`），所以取最近 N 条只能 `offset = 总条数 - N`。总条数来自第 1 步的「消息数」。
3. `N ≤ limit` 时省略 `offset`。
4. 返回是**升序**，最后一条就是最新；可用第 1 步的「最近时间」与末条时间戳比对，一致说明已到最新（时间戳是秒级 Unix 时间，Asia/Shanghai）。
5. **不要先跑 `wechat_doctor`**。它只在「读不到数据」时用来定位；正常读取时它纯属额外开销。

## 三、不要走的弯路（本次实测）

| 弯路 | 为什么不必 | 什么时候才值得 |
|---|---|---|
| 先 `wechat_doctor` | 只在读取失败/报错时才有价值 | 连不上后端、数量对不上、怀疑数据陈旧 |
| 用会话名调用 `wechat_messages` | 多义匹配直接失败，多一次重试 | 名字唯一时可用，但传 id 更稳 |
| `wechat_export` + `glob` + Python 解析 | 只为「读最近 N 条」时完全多余 | 需要落盘交付、精确时间格式化、按发言人统计 |
| 手算 offset 后不校验 | 可能不知道是否已到最新 | 用第 1 步「最近时间」对比末条 ts |
| 反复试 time 参数 | `since/until` 收窄的是窗口，取到的仍是**窗口内最早**的 limit 条 | 想要「窗口内最新」时同样要配 `offset` |

## 四、常用组合速查

| 目的 | 调用 |
|---|---|
| 最近 N 条 | `wechat_conversations(query)` → `wechat_messages(id, limit=N, offset=总数-N)` |
| 关键词命中 | `wechat_messages(id, keyword="xxx", limit=50)` |
| 某天/某段 | `wechat_messages(id, since="2026-10-05", until="2026-10-06")` |
| 只要最新几条 | `wechat_messages(id, limit=20, offset=总数-20)` |
| 离线交给别人 | 先按上面拿到内容，确认有用再 `wechat_export(id, formats="jsonl,md")` |

## 五、可选的一行改进（未实施）

引擎里 `store.recent_messages()`（`ORDER BY ts DESC` 后反转）和 `store.count_messages()` 都已存在，但 CLI/工具没暴露。若哪天愿意改插件，在 `atuowexin/lib/index.js` 的 `wechat_messages` 增加布尔参数 `latest`，映射到「先 count 再倒序取 N」，即可把「最近 N 条」压成**一次调用**（传名字时仍受多义匹配限制，传 id 则完全一步到位）。本次按要求只固化流程，不改代码。

## 六、附：一次「假故障」的说明

本次首轮调用曾报 `tool "wechat_conversations" returned invalid output: "value.sandbox" is not a declared property`，同时 `pwsh` 报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)`。这是**当时文件沙箱对工作目录授权失败**导致的宿主侧连锁现象，不是工具用法问题；权限恢复正常后原样重试即通。遇到同类报错：先重试一次，仍失败再考虑提权，不必改写调用方式。
