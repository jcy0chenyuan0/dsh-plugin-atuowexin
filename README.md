# dsh-plugin-atuowexin v0.5.0（只读版）—— 微信本地聊天记录摘录

> **只读**：解密 → 完整摘录 → 会话隔离 → 导出。不含发送消息、不注入微信进程、不调用云端模型。
> 交付形态：DSH 插件（5 个工具）+ Python 引擎（只用标准库）+ 命令行 CLI。

---

## 1. 它能做什么

| 能力 | 说明 | 代码位置 |
| --- | --- | --- |
| 完整摘录 | 后端（本机 chatlog / 已解密 SQLite / 合成）→ 归一化 SQLite + 每会话 `messages.jsonl`；默认摘**全部会话、不限条数**，保留原始字段与媒体引用 | `engine/atuowexin/extract.py`、`backends/` |
| 解密（含 WAL） | 用你自己的密钥把 SQLCipher 库解成明文；**自动合并 `-wal` 里尚未 checkpoint 的已提交数据**，并忽略 SQLite 重置后的残留帧（照抄会写坏主库） | `engine/atuowexin/sqlcipher.py`、`ingest.py` |
| 会话隔离 | 三层：目录（`conversations/<slug>/`）、读写（跨会话写入抛 `IsolationError`）、上下文（查询 API 形状上没有"查全部"） | `isolate.py`、`store.py` |
| 导出 | 单会话 jsonl/csv/md；`export --all` 出「总览 + 每会话一个 Markdown + 合订本」，可 `--redacted` 打码 | `extract.py` |
| 环境体检 | `doctor` 逐项告诉你缺什么、下一步做什么（含 WAL 待合并数据、可选依赖） | `doctor.py` |
| 自动提取密钥 | **先看有没有、缺了才读**：从运行中的微信进程内存里找出 SQLCipher 密钥（只读扫描 + 逐候选 HMAC 校验），合并写进 keymap.json | `keyfinder.py` |

**明确不做**（v0.3.0 起从代码里删掉，不是靠配置关掉）：发送消息、UI 自动化、微信进程注入、内存扫描密钥、DeepSeek/任何云端模型调用。

---

## 2. 30 秒上手

```powershell
# 自动搜索 Python：优先 DSH 自带运行时，其次 PATH
$py = Get-ChildItem "$env:USERPROFILE\.dsh\dsh-runtimes\*\dependencies\python\python.exe" -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $py) { $py = (Get-Command python -ErrorAction Stop).Source }
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = (Join-Path (Get-Location) "engine")

& $py -m atuowexin deps install            # 可选：zstandard（解 WCDB 压缩正文），装进 .tools/python
& $py -m atuowexin doctor                  # 第一步永远跑它

# 不装微信也能跑通链路（合成数据）
& $py -m atuowexin --backend mock sync
& $py -m atuowexin --backend mock conversations
& $py -m atuowexin --backend mock messages --conversation wxid_linda2024 --limit 5

# 接真实数据（一条命令：解密(含 WAL) → 校验 → 写配置 → 全量摘录）
& $py -m atuowexin ingest --src "<...>\db_storage" --keymap <keymap.json 路径>
#    单把 key 用 --key <64位hex>；只验密钥加 --probe-only；不合并 WAL 加 --no-wal

# 导出成 Markdown（总览 + 每会话文件 + 合订本）
& $py -m atuowexin export --all --out "<导出目录>"
```

### 命令行清单

```
doctor       环境体检
config       init | show | path | validate
decrypt      SQLCipher 解密（--tree 批量，--no-wal 关闭合并）
ingest       一键接入：解密 → 校验 → 写配置 → 全量摘录
sync         完整摘录（默认增量）
conversations / messages    列出会话 / 读某会话消息（messages 必填 --conversation）
export       单会话导出或 --all 导出整套 Markdown
watch        定时增量摘录（只读）
workspaces   列出会话隔离工作区
deps         status | install | dir（可选依赖）
key ensure   先看密钥有没有，缺了才去读（--check-only 只检查；--watch 轮询）
key find     无条件扫一次，提取解密密钥（只读扫描）
```

配置：`%USERPROFILE%\.atuowexin\config.json`（`config init` 生成模板）。0.3.0 起配置里**没有 `llm` 与 `send` 段**。

---

## 3. 会话隔离：三层落地

```
<workdir>/
├─ atuowexin.db                 归一化库（messages 每行都带 conversation_id，查询强制按会话）
├─ conversations/
│  ├─ 产品研发群-8a066ec4/      群聊 A 的独立工作区
│  │  ├─ messages.jsonl          只含本会话消息（写入时逐条断言 conversation_id）
│  │  └─ profile.json            类型/成员数/统计
│  └─ 林达_产品-ac325878/       私聊 B 的独立工作区
└─ exports/                     导出产物
```

`slug = 可读名 + md5(会话 id)[:8]` → 会话改名不换目录，同名不同 id 不撞车。

---

## 4. DSH 插件

### 4.1 安装（在 GUI 的「设置 → 插件」里可见/可开关）

> **首次安装**：先在本目录运行 `install.ps1`（Windows）或 `node install.mjs`（macOS/Linux）。
> 脚本按当前目录自动生成 `cordis.patch.yml`（已 `.gitignore`），不写死任何盘符。

本包是一个 **DSH bundle**：`package.json` 里的 `dsh.bundle.patch` 指向安装时生成的 `cordis.patch.yml`
（模板见 [cordis.patch.yml.tpl](cordis.patch.yml.tpl)），由它把插件挂到 profile 的 loader 树上。

```powershell
# 用 DSH 的插件管理装（GUI 里点也行：设置 → 插件 → 安装 → 本地路径）
plugin_manager install_bundle  file:<本目录的绝对路径>
```

装完在 `list_bundles` 里能看到 `dsh-plugin-atuowexin`（GUI 插件页刷新后同样可见），
可以整体**启用/停用**；卸载用 GUI 的移除按钮或 `plugin_manager remove_bundle dsh-plugin-atuowexin`。

> 机制说明：pnpm 会把本包**复制**到 `<profile>/node_modules/dsh-plugin-atuowexin`，
> 而 bundle 的 patch 层里挂的是**工作区绝对路径**的 `lib/index.js`，所以
> **改插件代码与引擎代码都是即时生效的**（重启 DSH 后加载）；
> 只有改了 `cordis.patch.yml`（挂载参数）才需要重新 `install_bundle` 刷新那份副本。

### 4.2 工具清单（6 个，全部只读）

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `wechat_doctor` | 环境体检 | — |
| `wechat_sync` | 完整摘录（默认增量） | `full`、`conversation[]`、`since`、`limit` |
| `wechat_conversations` | 列出会话（拿寻址 id） | `query`、`kind`、`limit` |
| `wechat_messages` | 读**某一条**会话的消息 | `conversation`(必填)、`since/until/keyword/limit/offset`、`redacted` |
| `wechat_export` | 导出 jsonl/csv/md + 元数据 | `conversation`、`out`、`formats`、`redacted` |
| `wechat_find_key` | 从微信进程内存自动提取密钥 → keymap.json（只读；默认不回显明文） | `root`、`out`、`watch`、`seconds`、`sqlcipher_version` |

JS 半边是**零依赖薄桥**：把参数翻译成 `python -m atuowexin --json …`，解析 stdout 上唯一的 JSON 对象。因此 CLI 与插件永远不会行为分叉。

---

## 6. 配置参考

| 段 | 关键键 | 说明 |
| --- | --- | --- |
| `backend` | `name`、`chatlog.base_url`、`decrypted_sqlite.root`、`mock.*` | `auto` 的优先级：chatlog → 已解密库 → 合成数据（并说明原因） |
| `extract` | `max_messages_per_conversation`(0=不限)、`write_workspace_jsonl`、`include_media` | 「完整摘录」的开关 |
| `privacy` | `redact_phone/id_card/bank_card` | 导出脱敏（默认开） |

环境变量：`ATUOWEXIN_WORKDIR`、`ATUOWEXIN_BACKEND`、`ATUOWEXIN_CHATLOG_URL`、`ATUOWEXIN_DB_ROOT` 等。

---

## 5. 密钥：每次用之前先看有没有，缺了自动去读

```powershell
# 推荐：先检查、缺了才提取（全都有时**不碰内存**，毫秒级返回）
python -m atuowexin key ensure --root "<...>\db_storage"

# 只检查不提取（CI / 判断"现在能不能读"）
python -m atuowexin key ensure --root "<...>\db_storage" --check-only

# 无条件扫一次；密钥可能只在重新登录瞬间驻留时加 --watch
python -m atuowexin key find --root "<...>\db_storage"
python -m atuowexin key find --root "<...>\db_storage" --watch --seconds 120
```

插件侧对应工具：`wechat_find_key`（默认 `ensure=true`，即"先看有没有、缺了才读"）。
`doctor` 里也会有一行 **解密密钥**：告诉你 keymap 在哪、本机加密库覆盖了几个、微信在不在运行、
以及下一步该跑什么。

**红线（写死在代码里）**：只 `OpenProcess(PROCESS_VM_READ)` + `ReadProcessMemory`，
**不注入、不写目标内存、不 hook、不碰协议**；每把候选密钥都必须通过目标库第 1 页的 HMAC 校验
才会被采纳（与 `decrypt` 同一判据，绝不猜）；找不到就返回空 + 原因，**绝不写出空 keymap**；
默认**不回显密钥明文**（只给前 8 位与文件路径）。

### 5.1 它是怎么找的（两条路径）

1. **路标路径（快，默认首选）**：拿已有 `keymap.json` 里的密钥当路标。真机实测：
   同一批库的密钥在内存里**聚簇**（同一库的 6 次出现只落在 3 个区域，彼此相隔约 1.6 KB），
   所以只要有一把，就能把同一邻域里其他库的密钥一起找出来——**只扫十几 MB**。
2. **锚点路径（通用，慢）**：没有路标时，按数据库**路径字符串**定位（实测路径离密钥
   344 B ~ 635 KB），在锚点周围滑窗逐个校验。

> 两个真机踩出来的坑（都已修 + 有回归测试）：
> ① **salt 不能当锚点**——4.1.7.30 实测 salt 与密钥相距 **5.8 万 ~ 292 MB**，
> "密钥紧挨着 salt"是 4.0.x 的老经验，照搬会扫 7 分钟一无所获；
> ② **候选滑窗必须按绝对地址对齐**——锚点命中的地址往往不是 8 的倍数，
> 从块首按 8 步进会让整个候选网格错位，真密钥永远落不到网格上。

### 5.2 真机结果（微信 4.1.7.30，本机实测）

```
key ensure（路标路径）→ 提取 15 把密钥，全部通过 HMAC 校验
用提取结果解密 → 成功 15 / 失败 0，且与归档里的 keymap **逐字节一致**
仍缺 5 个库：favorite.db / favorite_fts.db / chatbot_message.db / weclaw.db / third_app_icon.db
  （都是本次没被微信打开过的库——在微信里点开收藏等功能后再跑一次即可）
```

**版本限制**：微信 **≥4.1.10 起密钥不再常驻内存**（本项目在 4.1.13.65 上实测：
657 MB 内存里 17 把已知密钥一把都没有）；**≤4.1.9 可正常提取**（上表就是 4.1.7.30 的结果）。
遇到前者只能：用 `--watch` 抓重新登录瞬间、降到 ≤4.1.9 提一次、用 chatlog，或用已有的 keymap。

> ⚠️ `keymap.json` 是**无法轮换**的数据库万能钥匙：不要放云盘、不要进仓库、不要截图转发。
> `.gitignore` 已排除 `keymap*.json`。

## 6. 配置参考

| 段 | 关键键 | 说明 |
| --- | --- | --- |
| `backend` | `name`、`chatlog.base_url`、`decrypted_sqlite.root`、`mock.*` | `auto` 的优先级：chatlog → 已解密库 → 合成数据（并说明原因） |
| `extract` | `max_messages_per_conversation`(0=不限)、`write_workspace_jsonl`、`include_media` | 「完整摘录」的开关 |
| `privacy` | `redact_phone/id_card/bank_card` | 导出脱敏（默认开） |

环境变量：`ATUOWEXIN_WORKDIR`、`ATUOWEXIN_BACKEND`、`ATUOWEXIN_CHATLOG_URL`、`ATUOWEXIN_DB_ROOT` 等。

---

## 5. 自动提取密钥（`key find`，可选）

**做什么**：从**正在运行的微信进程**内存里找出 SQLCipher 解密密钥，写成 `keymap.json`，
之后 `ingest --keymap` 就能用。它替代的是「手工用外部工具提密钥」这一步。

```powershell
python -m atuowexin key find --root "<...>\db_storage"             # 单次扫描
python -m atuowexin key find --root "<...>\db_storage" --watch     # 轮询 60s（密钥可能只在登录瞬间驻留）
python -m atuowexin key find --root "<...>\db_storage" --show-keys # 显式要求回显明文（默认只写文件）
```

**红线（写死在代码里，不是建议）**：

* 只调用 `OpenProcess(PROCESS_VM_READ)` / `VirtualQueryEx` / `ReadProcessMemory`；
* **不注入** DLL 或线程、**不写**目标进程内存、不改寄存器、不 hook、不碰微信网络协议；
* **每把候选密钥都必须通过目标库第 1 页的 HMAC 校验**才会被采纳（与 `decrypt` 用同一套判据）——
  绝不猜、绝不返回未验证的密钥；找不到时 `found` 为空并说明原因，**不会写出一个空 keymap**；
* 默认**不回显密钥明文**（只显示每个库的密钥前 8 位 + 文件路径），避免密钥顺着终端日志/会话记录扩散。

**怎么找**：先按库第 1 页的 16 字节 salt 在内存里定位，再把命中点前后一小段窗口里按 8 字节对齐的
32 字节片段当候选，逐把校验。这利用了「微信把每库的 raw key 与 salt 放在同一个结构里」这一事实。

**关键限制（真机实测，务必先读）**：

| 环境 | 结果 |
| --- | --- |
| 微信 **4.1.13.65**（本机） | ❌ **提不到**：扫描 602 MB 内存 / 3.4 秒，**17 把已知密钥一个都不在**（原始字节与 hex 文本都没有），salt 也不在 → 印证了「≥4.1.10 起密钥零驻留」 |
| 微信 ≤ 4.1.9（以及任何密钥仍驻留内存的版本） | ✅ 可用（这是 PyWxDump 时代被反复验证过的路径） |

所以在本机这个版本上，**自动提取密钥不是能力问题，是微信不再把密钥放在内存里**。
可选的下一步：① `--watch` 在微信**退出登录 → 重新登录**的那几秒持续扫描；② 降到 ≤4.1.9
提取一次 passphrase（该口令跨版本长期有效，之后再升回来）；③ 继续用 chatlog 那类外部工具；
④ 手上已有 `keymap.json` 的话（本机归档里那份 17 把仍然有效）直接用 `ingest --keymap`。

> ⚠️ `keymap.json` 是**无法轮换**的数据库万能钥匙：不要放云盘、不要进仓库、不要截图转发；
> 不需要时请移进加密卷或删除。`.gitignore` 已经把 `keymap*.json` 排除。

---

## 7. 验证记录（真的跑过）

```powershell
cd <解压目录>\engine ; $env:PYTHONUTF8=1 ; $env:PYTHONPATH=$PWD
python -m unittest discover -s tests -t .     # → Ran 142 tests ... OK
node <解压目录>\tests\test-plugin.mjs        # → 15/15 passed
```

真实微信数据（微信 4.1.13.65、17/20 个库有密钥）：99 会话 / 3297 条消息；`ingest` 报告
「1 个库回放 WAL 4 帧、15 个 -wal 判为重置残留帧并忽略」；把 99 会话重新导出成 Markdown，
与 v0.1.0 的产物逐文件对齐；`--incremental` 重跑 `fetched=0 added=0`（幂等）。

关键行为覆盖：源库消息逐条对齐、增量幂等、工作区磁盘级隔离、跨会话写入被拒、失败 key/错 salt/
翻转 1 bit 的 HMAC 拒绝、每库一把 key（单 key 只解开 1 个 vs keymap 全解开）、WAL 帧 HMAC 与
校验链、重置残留帧必须忽略、撕裂 WAL 必须报错、CLI `--json` stdout 纯净性、错误原样透传。

---

## 8. 目录结构

```
atuowexin/
├─ package.json              DSH 插件包元数据（type: module，零运行时依赖）+ dsh.bundle 声明
├─ cordis.patch.yml.tpl      bundle 补丁层模板（install 脚本生成 cordis.patch.yml）
├─ config.example.json       不含任何密钥的配置模板
├─ lib/index.js              DSH host 半边：5 个只读工具 + CLI 桥
├─ engine/
│  ├─ atuowexin/             引擎（全部只用 Python 标准库）
│  │  ├─ cli.py  config.py  paths.py  store.py  model.py
│  │  ├─ extract.py           完整摘录 / 增量同步 / 导出（单会话 + Markdown 整包）
│  │  ├─ isolate.py           会话隔离工作区
│  │  ├─ ingest.py            一键接入：解密（含 WAL 合并）→ 校验 → 写配置 → 全量摘录
│  │  ├─ sqlcipher.py         SQLCipher 解密 + WAL 解析/合并
│  │  ├─ keyfinder.py         密钥自动提取（只读内存扫描 + HMAC 校验）
│  │  ├─ deps.py              可选依赖的发现与安装
│  │  ├─ doctor.py            环境体检（会给出可执行的下一步）
│  │  ├─ backends/            chatlog HTTP / 已解密 SQLite / 合成数据
│  │  └─ tools/sqlcipher.ps1  页/WAL 编解码（.NET 加密，PS 5.1 可用）
│  └─ tests/                 142 项 unittest + 合成微信库/mock chatlog/WAL/内存夹具
├─ tests/test-plugin.mjs     插件端到端脚手架（15 项断言）
├─ docs/                     调研资料（含来源链接）
└─ PACKAGE_NOTES.md          打包说明与已知限制
```

---

## 9. 合规与风险

- **只读**：不含 DLL 注入、协议破解，也不发送任何消息。密钥提取（`key find`）是「只读内存扫描 +
  HMAC 校验」：不写目标进程、不注入，且候选密钥必须真实解开目标库才会被采纳。
- **密钥自理**：本包不含任何密钥提取代码（微信 ≥4.1.10 起密钥零驻留，内存扫描无效）。
- **生态现状**：`sjzar/chatlog` 已于 2025-10-20 被作者删除；2026-01-08 腾讯 DMCA 下架 30+ 相关仓库。
- **隐私**：`export --all` 产出的是**完整明文聊天记录**；给别人前先 `--redacted`，并确认对方有权看到。
- 别把 `data/`、`keymap.json`、`config.json` 提交到任何仓库或发给别人。

---

## 可移植性 / 换机器安装

这个仓库**不包含任何本机绝对路径或私有数据**：`cordis.patch.yml` 由安装脚本按当前目录生成（已 `.gitignore`），`config.json`、`data/`、`selftest.json`、`*.log`、密钥文件都不入库。

换一台机器：

1. `git clone` 本仓库到任意目录；
2. 在仓库目录运行 `powershell -ExecutionPolicy Bypass -File .\install.ps1`（Windows）或 `node install.mjs`（macOS/Linux）——生成 `cordis.patch.yml`，并准备一份 `config.json`；
3. 在 DSH 里执行 `plugin_manager install_bundle  file:<本目录的绝对路径>`；
4. 按需修改 `config.json`（`workdir` 默认是本目录下的 `data/`）。

### 运行环境

- **Windows 10/11（x64）**：微信 4.x 的本地数据目录、WAL 合并与解密都按 Windows 路径实现。
- **Python 3.11+**：优先使用 DSH 自带运行时（`%USERPROFILE%\.dsh\dsh-runtimes`），找不到时回退 PATH 上的 `python`。
- **macOS / Linux 未适配**：引擎的数据发现、PowerShell 解密路径需要自行移植；JS 半边本身是跨平台的。
- 只读：不需要 API Key，不注入微信进程，不调用云端模型。
