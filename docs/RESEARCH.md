# 调研资料：微信数据读取器与自动发送方案

> 这份文档是写这个插件之前**实际联网抓取**的资料汇总（GitHub README / PyPI / 官方文档 /
> DMCA 原文 / pkg.go.dev 源码快照），所有结论都附来源链接。原始长篇简报在
> `docs/research/` 下：`chatlog-research-brief.md`、`wechat-data-research.md`、
> `wechat-automation-research.md`。
>
> 结论会随生态变化而失效（这个领域一年之内被清场过一次），**落地前请自行核对**。

---

## 0. 三条改变技术路线的结论

1. **生态已被清场。** `sjzar/chatlog` 于 2025-10-20 由作者主动删除全部代码与提交历史
   （收到微信官方函件，认定核心功能有合规风险），作者请求保留旧副本的用户删除
   （[删除通知原文](https://raw.githubusercontent.com/sjzar/chatlog/main/README.md)）。
   随后 2026-01-08 腾讯以 DMCA 一次性投诉下架 30+ 相关仓库，
   含 `xaoyaoo/PyWxDump`、`LC044/WeChatMsg`、`wechat-dump-rs`、`SharpWxDump` 等
   （[DMCA 原文](https://github.com/github/dmca/blob/master/2026/01/2026-01-08-tencent.md)、
   [清单](https://www.cnblogs.com/longware/p/19479076)）。
   → **本插件不含任何密钥提取代码，也不随包分发第三方二进制。**
2. **微信 4.1.10+ 的密钥是硬断点。** 4.0.x–4.1.9.x 内存里还有可扫的
   `x'<64hex key><32hex salt>'`；4.1.1+ 只剩 passphrase；4.1.10+ 起「零驻留」，
   纯内存扫描确定失效，只能断点调 / Frida spawn / DLL 注入，或降级到 ≤4.1.9 提取
   （passphrase 跨版本永久有效）。chatlog 的自动密钥提取上限也是
   Windows < 4.0.3.36。
3. **chatlog 只能读，不能发。** 它没有任何发送/回复 API，只有 DB + 媒体 + Webhook。
   「发」必须另找方案：本项目用 **wxauto4（纯 UI Automation）**，并且默认关闭。

---

## 1. 读取侧：三个可用方案

| 方案 | 语言/许可 | 版本支持 | 现状 | 本项目如何用 |
| --- | --- | --- | --- | --- |
| **chatlog** (`sjzar/chatlog` v0.0.31) | Go / Apache-2.0 | 微信 3.x + **4.x** | 仓库已删；ghcr.io 镜像与锁定版本的 go install 仍可用 | `backends/chatlog_http.py` 读它的 HTTP JSON |
| **PyWxDump** | Python / MIT | 3.x 全系列；4.0 支持弱 | 仓库删库；PyPI `pywxdump` 3.1.46 与续命发行 `haowxdump` 3.1.46 仍可 `pip install` | 不在代码里依赖；它解出来的库可交给 `decrypted_sqlite` 后端 |
| **WeChatMsg（留痕 / MemoTrace）** | Python+PyQt / MIT | **明确支持 4.0** | 原库 README 404，靠 fork/镜像与官网 exe 存活 | 同上：导出 HTML/CSV/sqlite 后走 `decrypted_sqlite` |
| **本插件 `mock`** | Python 标准库 | — | 合成数据 | 离线自测专用，CLI 会标注 `synthetic: true` |

### 1.1 chatlog 的接口（`backends/chatlog_http.py` 按此实现）

默认 `127.0.0.1:5030`（TUI）或 `0.0.0.0:5030`（`server` 模式，**零鉴权 + CORS `*`**，
务必只绑回环）。

| 方法 | 路径 | 关键参数 | 返回 |
| --- | --- | --- | --- |
| GET | `/health` | — | `{"status":"ok"}` |
| GET | `/api/v1/session` | `limit`/`offset` | `{items:[{userName,nickName,content,nTime}]}` |
| GET | `/api/v1/contact` | `keyword`/`limit`/`offset` | `{items:[{userName,alias,remark,nickName}]}` |
| GET | `/api/v1/chatroom` | `limit`/`offset` | `{items:[…owner,users[]]}` |
| GET | `/api/v1/chatlog` | `time`(必填)、`talker`(必填)、`sender`、`keyword`(Go 正则)、`limit`、`offset`、`format=json` | Message 数组 |
| GET | `/image` `/video` `/file` `/voice/<key>` | key = md5 / svr_id | 媒体（`/voice` 实时 SILK→mp3） |
| MCP | `/mcp`（Streamable HTTP）、`/sse` | 无鉴权 | 5 个工具：`query_chat_log`、`query_contact`、`query_chat_room`、`query_recent_chat`、`current_time` |

Message JSON：`seq,time,talker,talkerName,isChatRoom,sender,senderName,isSelf,type,subType,content,contents{}`；
媒体键在 `contents.md5 / path / thumbpath / voice`。**503** 表示「库未就绪/正在解密」。

坑：`chatlog server` 的 `-p` 是 platform 不是 port（端口用 `-a`）；解密产物在**工作目录**
（`-w`）而不是数据目录；`keyword` 无全文索引，全量扫，务必配 `limit`。

### 1.2 已解密库的结构（`backends/decrypted_sqlite.py` 按此实现）

**微信 4.x**：`xwechat_files/<账号>/db_storage/{contact,session,message,media,hardlink}/`
- 每会话一张表 `Msg_<md5(username)>`，分片在 `message_0.db…message_9.db`
- 字段：`local_id / server_id / local_type / real_sender_id / create_time / message_content / compress_content / packed_info_data`
- 真实类型 = `local_type & 0xFFFF`；**子类型在正文 XML 的 `<appmsg><type>`** 里
- 发送者：`real_sender_id → Name2Id.rowid → wxid`（注意 message_1/2.db 里这个映射可能
  是会话内角色编号，逐库要校验）→ 失败再退回 `packed_info_data` 的 protobuf
- `WCDB_CT_message_content == 4` 表示正文被 zstd 压缩（魔数 `28 B5 2F FD`）

**微信 3.x**：`WeChat Files/<wxid>/Msg/Multi/MSG0.db…` + `MicroMsg.db`
- `MSG(localId, MsgSvrID, Type, SubType, IsSender, CreateTime, Sequence, StrTalker, StrContent, DisplayContent, CompressContent, BytesExtra)`
- `CreateTime` 是**秒**；群聊发送者 wxid 在 `BytesExtra` 的 `MessageBytesExtra.message2`
  （`field1 == 1` 的那条 `field2`）
- 群成员名片在 `ChatRoom.RoomData`（protobuf，不是文本）

**消息类型**：1 文本、3 图片、34 语音、43 视频、47 动图、48 位置、
49/5 链接、49/6 文件、49/19 合并转发、**49/57 引用**、49/2000 转账、49/2001 红包、
10000 系统、10000/4 拍一拍；4.x 的 `local_type` 用同一套数值。

### 1.3 SQLCipher 参数（`sqlcipher.py` + `tools/sqlcipher.ps1` 按此实现）

| 参数 | 3.x | 4.x |
| --- | --- | --- |
| 页大小 | 4096 | 4096 |
| KDF 迭代 | 64000 | 256000（PBKDF2-HMAC-SHA512） |
| HMAC | SHA1（20B） | HMAC-SHA512（64B） |
| reserve（IV+HMAC 向上取整到 16B 块） | 48 | 80 |

页布局：`[第1页前16字节=salt][密文][16B IV][HMAC]`，HMAC 输入为
`密文‖IV‖页号(小端)` 且**不含第 1 页 salt**；第 1 页明文要补回 `SQLite format 3\0`。
**两种 key 形态不能混**：raw 32 字节 key 直接作 AES key（不再过 PBKDF2），mac key =
`PBKDF2(raw, salt^0x3a, 2, 32)`；passphrase 形态才需要 `PBKDF2(passphrase, salt, iterations, 32)`，
且**每库 salt 不同 → 每库一把 enc key**。

来源：[SQLCipher crypto_impl.c](https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto_impl.c)、
[crypto.h](https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto.h)、
[SQLCipher Design](https://www.zetetic.net/sqlcipher/design/)。
**本项目的实现细节与不确定点写在该模块 docstring 里（WAL 不支持、无真实样本）。**

---

## 2. 发送侧：wxauto4（纯 UIA，不注入）

| 包 | 定位 | 支持版本 | 说明 |
| --- | --- | --- | --- |
| `wxauto` | 开源 | **仅微信 3.9.x** | PyPI 包名已 404，只能从 GitHub 拿；README 明写禁止用于生产 |
| **`wxauto4`** | **免费** | 兼容到微信 **4.1.8.107** | `pip install wxauto4`，纯 UI Automation，本项目采用 |
| `wxautox4` | 收费 Plus | 更高版本 | Nuitka 编译 + 授权校验，一码一机 |

关键 API：`WeChat()`、`ChatWith(name)`、`chat.SendMsg(text)`（返回 `WxResponse`，真值判成功）、
`AddListenChat(nickname, callback)`。文档：<https://docs.wxauto.org/>。

**微信 4.x 的 UI 是实时渲染**：只有可见区消息有 UIA 控件，滚出即销毁；监听改回调式，
且**回调内禁止发送**（会重置消息窗口导致漏消息）——所以正确做法是回调只入队，
另起工作线程发送。窗口太矮还会让新消息「一闪而过」，官方建议拉大窗口高度
（本项目默认 `[800, 6000]`）。

**发送可靠性的硬要求**（`send/wxauto4_sender.py` 按此实现）：
1. 中文**必须走剪贴板**（`Ctrl+V`），SendKeys 直打会丢字/被输入法拦；
2. 发送前回读输入框内容做相似度校验（wxauto 内置 `SEND_CONTENT_RATIO = 0.9`）；
3. **防发错人**：`Ctrl+F` 搜索时只接受**完全匹配**项，搜不到就放弃，绝不能取第一个结果；
   发送前再断言 `ChatInfo()['chat_name']` 严格等于目标；
4. 不重试——UIA 没有服务端回执，重试极易对真人重复投递。

**风控与合规**：腾讯有专门打击外挂/自动化的公告
（<https://mp.weixin.qq.com/s/YfYKVyK09gOelvM840-qJQ>）；CowAgent 的 issue 标题直接写
「会引起风控」（<https://github.com/zhayujie/CowAgent/issues/2568>）。
**没有任何可靠的"封号阈值"资料**，wxauto 官方政策反而要求不要按所谓阈值设计行为。
因此本项目的立场是：默认不发送 + 白名单 + 人工确认 + 限频 + 全程审计，
并明确声明这些**只降险、不构成安全保证**。

---

## 3. 可借鉴的设计（已吸收进本项目）

- **CowAgent（原 chatgpt-on-wechat）**：Channel → Agent Core → Model 分层 + 三层记忆
  → 本项目采用「归一化模型层 + 每会话独立工作区 + 增量总结」的对应设计。
- **KouriChat 的 wxauto 分支**：每会话独立记忆与角色卡 → 对应 `notes.md`（人设）+ 会话隔离目录。
- **WeChatFerry 的"用 wxid 而非昵称寻址"** → 本项目发送层优先用 `conversation_id` 寻址，
  昵称只作为二次校验。
- **chatlog 内置的两阶段检索提示词**（先用关键词定位 → 再取 ±15~30 分钟上下文）
  → 本项目的窗口化 map-reduce 总结与之同源：先分段抽取，再归并，避免一次塞满上下文。

---

## 4. 原始简报

| 文件 | 内容 |
| --- | --- |
| [`docs/research/chatlog-research-brief.md`](research/chatlog-research-brief.md) | chatlog 源码级接口清单、MCP 工具、数据落盘、坑与替代 fork |
| [`docs/research/wechat-data-research.md`](research/wechat-data-research.md) | PyWxDump / WeChatMsg / wechat-dump-rs 对比、密钥原理三阶段、SQLCipher 参数、表结构、消息类型、`.dat` 图片三种加密格式 |
| [`docs/research/wechat-automation-research.md`](research/wechat-automation-research.md) | wxauto / wxauto4 / wxautox4 精确定位与 API、监听与发送要点、风控合规证据、可借鉴项目 |
