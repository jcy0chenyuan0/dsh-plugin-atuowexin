# chatlog 技术调研简报

> 调研时间：2026-10-05 ｜ 调研对象：GitHub `sjzar/chatlog`（v0.0.31，2025-10-14 为最后版本）

## 0. 结论速览（先看这个）

**⚠️ 该项目已于 2025-10-20 被作者主动清空删除。** 作者收到微信官方函件，认为核心功能存在合规风险，决定「移除本项目的全部代码与提交历史记录」，明确表示不再提供代码/预编译程序下载、不再提供任何支持与安全修复，并**恳请本地保留旧版本副本的用户及时删除**。仓库现在只剩一份说明文件（[来源](https://raw.githubusercontent.com/sjzar/chatlog/main/README.md)）。官方文档站 `chatlog.sjzar.com` 已 DNS 解析失败（域名下线）。

| 项目 | 结论 |
|---|---|
| 语言 | Go（gin + cobra + zerolog + mattn/go-sqlite3，**cgo 依赖**） |
| 平台 | Windows / macOS（`platform` 可选 `windows`/`darwin`） |
| 微信版本 | 3.x 与 4.x 均支持；**密钥自动提取仅在 Windows < 4.0.3.36 / macOS < 4.0.3.80 有效**（[README](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31)） |
| 许可证 | Apache-2.0 |
| 最后版本 | v0.0.31（2025-10-14） |
| 当前可用性 | GitHub 源码/Release **不可用**；Docker 镜像 **仍可用**；Go module proxy 缓存**可能仍可用** |

**已实测（本机 PowerShell 直连，2026-10-05）**：`ghcr.io` 匿名令牌可正常拉取，`sjzar/chatlog:latest` 及 `v0.0.20`–`v0.0.31` 全部 tag（含 `-amd64`/`-arm64`）仍然存在。镜像 `Entrypoint=entrypoint.sh`、`Cmd=chatlog server`、`WorkingDir=/app`，内置 `CHATLOG_DATA_DIR=/app/data`、`CHATLOG_WORK_DIR=/app/work`、`CHATLOG_HTTP_ADDR=0.0.0.0:5030`。

**给「AI 总结 + 自动回复插件」的关键判断**：chatlog 只能**读**（数据库 + 媒体 + Webhook 通知），**完全没有发送消息/回复的接口**。自动回复必须另找发送侧方案（如 Windows UI 自动化、wechatferry、或 4.x 的 hook 类项目），chatlog 只能承担「读 + 总结」+ 通过 `webhook` 触发你自己的回复流程。另外 **HTTP API 与 MCP 都没有任何鉴权**，只能靠监听地址隔离。

---

## 1. 安装与启动

### 安装（现状）
- **Docker（当前最可靠途径）**：
  ```shell
  docker pull ghcr.io/sjzar/chatlog:latest     # 已验证 tag 存在
  # 或 docker pull sjzar/chatlog:latest         # Docker Hub，本机网络不可达未能验证
  ```
- **Go 源码安装**：`go install github.com/sjzar/chatlog@v0.0.31`。仓库虽已删除，但 Go module proxy 通常仍保留代理缓存副本；必须锁定 `@v0.0.31`（`@latest` 会因仓库消失而失败）。编译需本地 C 编译环境（cgo）。
- **GitHub Releases 预编译二进制**：已随仓库删除而失效。
- **brew / scoop**：未找到任何可靠资料表明官方支持，不要假设存在。
- **可用的第三方镜像/fork（社区备份，非官方，使用需自行评估风险）**：[josh202310/chatlog_backup](https://github.com/josh202310/chatlog_backup)、[DavidLam-oss/chatlogbackup](https://github.com/DavidLam-oss/chatlogbackup)、[imldy/chatlog](https://github.com/imldy/chatlog)、[jackcfan/wxchatlog](https://github.com/jackcfan/wxchatlog)。

### 启动命令与参数
```shell
chatlog                 # Terminal UI（推荐首次使用：解密数据 → 开启 HTTP 服务）
chatlog key             # 获取密钥；-p/--pid 指定 PID，-f/--force 强制，-x/--xor-key 显示 xor key
chatlog decrypt         # 解密数据库
chatlog server          # 启动 HTTP 服务（无 TUI）
```
`chatlog server` 参数（[cmd_server.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/cmd/chatlog/cmd_server.go)）：

| 短参 | 长参 | 说明 |
|---|---|---|
| `-a` | `--addr` | HTTP 监听地址（映射到 `http_addr`） |
| `-d` | `--data-dir` | 微信数据目录 |
| `-k` | `--data-key` | 数据密钥 |
| `-i` | `--img-key` | 图片密钥 |
| `-w` | `--work-dir` | 解密后数据的工作目录 |
| `-p` | `--platform` | 平台：`windows` / `darwin`（**注意 `-p` 是 platform 不是 port**） |
| `-v` | `--version` | 微信大版本：`3` / `4` |
| | `--auto-decrypt` | 开启自动解密（Webhook 前置条件） |
| | `--debug` | 调试日志 |

配置优先级：命令行 > `CHATLOG_*` 环境变量 > `<data_dir>/chatlog.json` > 配置文件。环境变量前缀 `CHATLOG`，配置目录可用 `CHATLOG_DIR` 指定；TUI 配置在 `$HOME/.chatlog/chatlog.json`（Windows：`%USERPROFILE%\.chatlog\chatlog.json`）。

---

## 2. 工作流程（密钥与前置条件）

- **获取密钥方式：内存搜索**。Windows 侧 `internal/wechat/key/windows` 分 `V3Extractor`/`V4Extractor`，通过 `OpenProcess(PROCESS_VM_READ|PROCESS_QUERY_INFORMATION)` + `VirtualQueryEx` 遍历 ≥1MB 的 `MEM_COMMIT` + `PAGE_READWRITE` + `MEM_PRIVATE` 内存区，用特征字节串定位指针，读 32 字节候选密钥并用 `Validator.Validate` / `ValidateImgKey` 校验（[v4_windows.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/wechat/key/windows/v4_windows.go)）。macOS 侧对应 `internal/wechat/key/darwin`（含 `glance` 子包），README 要求**临时关闭 SIP**（`csrutil disable`）+ 安装 Xcode Command Line Tools，取到密钥后可重新启用 SIP。
- **微信状态**：必须**正在运行**。代码在 `proc.Status == StatusOffline` 时直接返回 `ErrWeChatOffline`；全部内存区扫描完仍无有效密钥则返回 `ErrNoValidKey`。
- **管理员权限**：代码未显式请求提权，但 `ReadProcessMemory` 读取微信进程内存通常需要管理员/足够权限，实践中**以管理员身份运行**（Windows）。
- **可绕过自动取密钥**：`-k/--data-key`、`-i/--img-key`、`CHATLOG_DATA_KEY/CHATLOG_IMG_KEY`，或数据目录下的 `chatlog.json`（Docker/NAS 场景标准做法）。密钥为**纯十六进制、不带方括号**（`c0163eac3dc6`，非 `[c0163e***ac3dc6]`）。
- **解密**：`chatlog decrypt` 或 TUI「解密数据」；`--auto-decrypt` 开启文件监听（fsnotify）持续解密新库，这是 Webhook 推送的前提。

---

## 3. HTTP API 清单

来源：源码 [route.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/route.go)、[middleware.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/middleware.go)、[service.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/service.go)。

**监听地址与鉴权**：TUI 模式默认 `127.0.0.1:5030`；**`chatlog server` 模式默认 `0.0.0.0:5030`**（`conf.DefalutHTTPAddr`）。**没有任何鉴权**：无 token、无 header 校验，仅 CORS 中间件固定回 `Access-Control-Allow-Origin: *`。官方 Docker 文档原文警告：「chatlog 本身未提供授权机制，一定要确保服务处于安全网络环境中」。`/api/v1/*` 有数据库状态中间件，未就绪时返回 503 + `{"error":"database is not ready"|"database is decrypting, please wait"|"database is error: ..."}`。

| 方法 | 路径 | 参数（query） | 返回 |
|---|---|---|---|
| GET | `/health` | — | `{"status":"ok"}` |
| GET | `/api/v1/chatlog` | `time`（必填，`2023-01-01`、`2023-01-01~2023-01-31`、`2023-04-18/16:00~2023-04-18/17:00`、`2023`、`2023-04`）、`talker`（**必填**，wxid/群 ID/备注/昵称，逗号分隔多个）、`sender`（逗号分隔）、`keyword`（**Go 正则**）、`limit`、`offset`、`format`（`json`/`csv`/空=纯文本） | `json` → Message 数组；`csv` → 附件下载，表头 `Time,SenderName,Sender,TalkerName,Talker,Content`；默认纯文本 Markdown |
| GET | `/api/v1/contact` | `keyword`、`limit`、`offset`、`format`（`json`/`csv`/文本） | `json` → `{items:[{userName,alias,remark,nickName,...}],...}` |
| GET | `/api/v1/chatroom` | 同上 | 群列表，含 `owner`、`users[]`、`user2DisplayName` |
| GET | `/api/v1/session` | 同上 | 最近会话 `{items:[{userName,nOrder,nickName,content,nTime}]}`，按 `sort_timestamp DESC` |
| GET | `/image/<key>` | `key` = md5 或相对路径，可逗号分隔多个；加 `info` 参数返回 JSON 元信息 | 302 → `/data/<path>`（`.dat` 实时解密）；绝对路径命中直接 302 |
| GET | `/video/<key>` | 同上 | 302 → `/data/...`（含 `_thumb.jpg`） |
| GET | `/file/<key>` | 同上 | 302 → `/data/...` |
| GET | `/voice/<key>` | `key` = `svr_id` | **200 + `audio/mp3`**（SILK 实时转 MP3，失败回退 `audio/silk`） |
| GET | `/data/*path` | 数据目录相对路径 | 原始文件；`.dat` 走 dat2img 实时解密后按真实类型返回 |
| ANY | `/mcp` | — | MCP Streamable HTTP |
| ANY | `/sse`、`/message` | — | MCP SSE（旧客户端，旧文档写 `/sse`） |

**导出格式**：支持 JSON / CSV / 纯文本（Markdown 风格）。**不支持**直接导出 Markdown 文件或 HTML；纯文本模式输出 `昵称(ID) 时间\n内容`，图片输出 `![图片](http://<host>/image/<md5>)`。

**Message JSON 字段**（[message.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/model/message.go)）：`seq, time, talker, talkerName, isChatRoom, sender, senderName, isSelf, type, subType, content, contents{}`。`type`：1 文本、3 图片、34 语音、42 名片、43 视频、47 动画表情、48 位置、49 分享（`subType` 6=文件、19=合并转发、57=引用、2000/2001=转账/红包）、10000 系统。图片/视频/文件下载键在 `contents.md5`（视频另有 `rawmd5`）、`contents.path`、`contents.thumbpath`、`contents.voice`。

---

## 4. MCP 接口

**内置**，无需额外进程，随 HTTP 服务启动（[mcp.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/mcp.go)）。Server 名 `chatlog`，用 `mark3labs/mcp-go`。同一端口同时暴露：
- **Streamable HTTP**：`http://127.0.0.1:5030/mcp`（ChatWise、Cherry Studio 直接填这个）
- **SSE**：`http://127.0.0.1:5030/sse` + `/message`（旧版文档用这个；Claude Desktop / Monica Code 需用 [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) 把 stdio 转 SSE）

| 工具名 | 参数 | 返回 |
|---|---|---|
| `query_chat_log` | `time`（**必填**）、`talker`（**必填**）、`sender`、`keyword`（正则） | 纯文本消息流；返回格式 `昵称(ID) 时间\n内容`，多 talker 时 `昵称(ID)\n[TalkerName(Talker)] 时间\n内容` |
| `query_contact` | `keyword`（可选） | CSV 文本 `UserName,Alias,Remark,NickName` |
| `query_chat_room` | `keyword`（可选） | CSV 文本 `Name,Remark,NickName,Owner,UserCount` |
| `query_recent_chat` | 无 | 最近会话纯文本（每条截断 120 字符） |
| `current_time` | 无 | RFC3339 本地时间，如 `2025-04-18T21:29:00+08:00` |

**给 AI 总结的关键细节**：`query_chat_log` 的 description 内置了一段强制「多步查询」提示词——先用 `keyword`/`sender` + 宽时间范围定位，再**移除 keyword/sender**、对每个命中时间点做 ±15~30 分钟的窄范围上下文查询，最后综合分析。写总结功能时可直接复用这个两阶段检索策略（先定位、再取上下文），既省 token 又保留上下文完整性。MCP 同样**无鉴权**；`talker`/`sender` 以逗号分隔即视为多对象（会改变返回格式）。

---

## 5. 数据落盘

- **配置**：`$HOME/.chatlog/chatlog.json`（TUI）；`<data_dir>/chatlog.json` 也会被写入/读取（含 `platform`、`version`、`data_key`、`img_key`）。`CHATLOG_DIR` 可改配置目录。
- **解密产物**：**工作目录（work dir，`-w` / `CHATLOG_WORK_DIR`）**，Docker 中为 `/app/work`。内部用独立缓存（`os.TempDir()` 下 `filecopy_<进程名>`）做高性能拷贝，原子重命名 + 按内容哈希去重。
- **微信原始数据目录**（Docker 文档给出）：
  - Windows 3.x：`%USERPROFILE%\Documents\WeChat Files\{微信ID}`；4.x：`%USERPROFILE%\Documents\xwechat_files\{微信ID}`
  - macOS 3.x：`~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/{版本号}/{微信ID}`；4.x：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/{微信ID}`
- **格式：SQLite**（4.x 数据源 [datasource.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/wechatdb/datasource/v4/datasource.go)）。按文件名分 5 组：
  - `message`：`^message_(\d?\d)?\.db$`（分片）
  - `contact`：`^contact\.db$`
  - `session`：`session\.db$`
  - `media`：`^hardlink\.db$`
  - `voice`：`^media_(\d?\d)?\.db$`
- **关键表结构（4.x）**：
  - `message_*.db / Timestamp(timestamp)` — 用于确定分片时间范围，据此路由查询
  - `message_*.db / Msg_<md5(username)>` — **消息表按 talker 的 MD5 命名**；列 `sort_seq, server_id, local_type, create_time, message_content, packed_info_data, status`；`LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid` 取发送者
  - `contact.db / contact(username, local_type, alias, remark, nick_name)`、`chat_room(username, owner, ext_buffer)`
  - `session.db / SessionTable(username, summary, last_timestamp, last_msg_sender, last_sender_display_name, sort_timestamp)`
  - `hardlink.db / {image|video|file}_hardlink_info_v3|v4(md5, file_name, file_size, modify_time, dir1, dir2)` + `dir2id`
  - `media_*.db / VoiceInfo(svr_id, voice_data BLOB)`
- 另有 3.x 数据源：`windowsv3`、`darwinv3`，以及旧格式 `dbm`（SQLite 加密库）。注意 `Msg_<md5>` 表名意味着**写代码时不能硬编码表名，必须按 talker MD5 计算**（chatlog 已代劳，但直读 SQLite 时要注意）。

---

## 6. 限制与风险（写代码前必读）

**合规/法律（最重要）**
- 项目因微信官方函件被作者删除，核心功能被官方明确认定为**存在合规风险**；继续使用需自行承担全部风险（[删除说明](https://raw.githubusercontent.com/sjzar/chatlog/main/README.md)、[V2EX 讨论](https://global.v2ex.co/t/1167055)）。
- 原 README 免责声明：仅限处理自己合法拥有或已获授权的数据；严禁未授权获取/查看他人聊天记录；使用第三方 LLM 服务时须遵守其条款——**把聊天记录发给云端模型前务必评估隐私与合规**（建议本地模型或脱敏）。
- **不提供发送消息能力**，也无法通过 chatlog 实现自动回复的「发」这一半。

**安全**
- API/MCP **零鉴权** + `chatlog server` 默认绑 `0.0.0.0:5030` + CORS `*`。若监听非本机，必须自己加前置反代/防火墙/仅内网绑定（`-a 127.0.0.1:5030`）。

**功能与兼容性坑**
- 密钥提取**仅支持 Windows < 4.0.3.36 / macOS < 4.0.3.80**；更新版本需自行想办法拿密钥（该限制是 chatlog 自身限制，不代表新版本微信数据无法解密）。
- macOS 取密钥须**临时关闭 SIP**，属系统性安全降级。
- `keyword` 是**服务端 Go 正则**，非 SQL 全文索引（作者自述「还有不少可以改进的地方（比如全文索引等）」），大时间跨度 + 宽关键词会全量扫描多库，注意 `limit`。
- 时间范围查询会按 `Timestamp` 表把请求路由到对应分片；跨月大范围查询扫描成本高。
- 图片类型为 3 时 `contents` 可能同时含 `md5`/`path`/`thumbpath`，`/image/<key>` 支持逗号分隔多 key 依次尝试。
- 语音走 `/voice/<svr_id>` 实时转码，有 CPU 开销；离线/未解密的语音返回 404。
- 已知常见启动失败：微信未运行 / 非登录状态（`ErrWeChatOffline`）、无有效密钥（`ErrNoValidKey`）、进程权限不足、密钥格式带方括号、`data_dir` 挂载错误、5030 端口占用、数据目录权限不足、微信版本检测失败（用手动 `-p`/`-v` 覆盖）、终端花屏需用 Windows Terminal、Apple Silicon 下微信/chatlog/终端混用 Rosetta。
- 是否会被封号/风控：**未找到可靠资料**说明 chatlog 自身导致封号。它只读本地磁盘文件与内存，不向微信服务器发消息；但 4.x 新版本取密钥手段收紧本身即官方风控态度的体现，仍建议谨慎、避免高频轮询。
- Webhook 延迟：本地约 13 秒、远程同步约 45 秒（官方 README 实测），做「实时」自动回复要接受这个粒度。
- 原 FAQ Issue（#197 无法获取密钥、#131 版本兼容、#211 稳定启动版本）已随仓库删除，**目前无法访问**。

**配置细节坑**：`chatlog server` 的 `-p` 是 **platform 不是 port**，端口用 `-a`；数据目录中存在 `chatlog.json` 时其中的 `type/platform/version/full_version/data_key/img_key` 会覆盖命令行以外的配置。

---

## 参考链接

- 主仓库（现已清空）：[github.com/sjzar/chatlog](https://github.com/sjzar/chatlog) ｜ [删除通知原文](https://raw.githubusercontent.com/sjzar/chatlog/main/README.md)
- 完整 README（镜像快照）：[pkg.go.dev/github.com/sjzar/chatlog@v0.0.31](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31)
- HTTP 包 API 文档：[internal/chatlog/http@v0.0.22](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.22/internal/chatlog/http) ｜ [conf 包](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31/internal/chatlog/conf) ｜ [datasource/v4](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31/internal/wechatdb/datasource/v4) ｜ [版本列表](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31?tab=versions)
- 源码（社区备份）：[route.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/route.go)、[mcp.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/mcp.go)、[middleware.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/middleware.go)、[service.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/http/service.go)、[cmd_server.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/cmd/chatlog/cmd_server.go)、[v4_windows.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/wechat/key/windows/v4_windows.go)、[v4/datasource.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/wechatdb/datasource/v4/datasource.go)、[model/message.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/model/message.go)、[conf/server.go](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/internal/chatlog/conf/server.go)
- 文档：[Docker 部署指南](https://raw.githubusercontent.com/DavidLam-oss/chatlogbackup/main/docs/docker.md) ｜ [MCP 集成指南](https://raw.githubusercontent.com/josh202310/chatlog_backup/main/docs/mcp.md) ｜ [DeepWiki（可能受访问限制）](https://deepwiki.com/sjzar/chatlog/8.1-server-command-and-configuration)
- 第三方介绍：[小众软件介绍文](https://www.appinn.com/chatlog-mcp/) ｜ [V2EX：项目被律师函删库](https://global.v2ex.co/t/1167055)
- 镜像：[ghcr.io/sjzar/chatlog](https://ghcr.io/sjzar/chatlog)（**实测可用，含 v0.0.20–v0.0.31**）｜ [Docker Hub](https://hub.docker.com/r/sjzar/chatlog)
- 社区 fork：[josh202310/chatlog_backup](https://github.com/josh202310/chatlog_backup) ｜ [DavidLam-oss/chatlogbackup](https://github.com/DavidLam-oss/chatlogbackup) ｜ [imldy/chatlog](https://github.com/imldy/chatlog) ｜ [jackcfan/wxchatlog](https://github.com/jackcfan/wxchatlog) ｜ [yhai3596/MCP-chatlog](https://github.com/yhai3596/MCP-chatlog)
- 前置依赖：[wechat-dump-rs](https://github.com/0xlane/wechat-dump-rs)、[PyWxDump](https://github.com/xaoyaoo/PyWxDump)、[mcp-proxy](https://github.com/sparfenyuk/mcp-proxy)

**未找到可靠资料的事项**：brew/scoop 官方包；chatlog 导致微信封号的实证；`keyword` 的全文索引方案（作者明确说未实现）；`chatlog.sjzar.com` 的历史内容（域名已下线，Wayback 本机不可达未验证）。
