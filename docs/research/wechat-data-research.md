# 微信本地数据库解密/导出 开源工具技术调研简报

> 调研日期：2026-10-05。所有结论均来自下列实际抓取来源，未标注来源处为「未找到可靠资料」。

## 结论速览

- **重大生态变化**：2026-01-08 腾讯以 DMCA（绕过技术保护措施 + 违反禁止逆向条款 + 侵犯知识产权）一次性投诉下架 30+ 个微信相关 GitHub 仓库：[DMCA 原文](https://github.com/github/dmca/blob/master/2026/01/2026-01-08-tencent.md)、[清单与 star 数](https://www.cnblogs.com/longware/p/19479076)、[V2EX 讨论](https://global.v2ex.co/t/1186673)。
- **上游仓库现状**：`xaoyaoo/PyWxDump` 页面标题已变为「删库」（[仓库页](https://github.com/xaoyaoo/PyWxDump)，README 仍返回 404）；`LC044/WeChatMsg` README 404，项目已更名 **MemoTrace/留痕** 并维护镜像 README（[镜像](https://raw.githubusercontent.com/shixiaogaoya/MemoTrace/master/readme.md)）。**唯一仍可 pip 安装的 Python 路径是 PyPI 上的 `pywxdump` / `haowxdump`，以及 WeChatMsg 的 fork/镜像**。
- **微信 4.1.10+ 是硬断点**：Windows 4.1.10 起 enc_key「零驻留」，纯内存扫描失效（详见第 3 节），必须用断点/Frida/DLL Hook 或降级提取。
- **对本项目最务实的选型**：读数据用 `pywxdump`（3.9.x 全版本，MIT，已停止维护）或直接自研 SQLCipher4 解密；**自动回复不要走数据库**，用 **wxauto4（4.1，UI Automation，无注入，封号风险最低）**。

## 工具对比表

| 项目 | 定位 | 语言/许可 | 微信版本支持 | 活跃度/规模 |
|---|---|---|---|---|
| [PyWxDump](https://pypi.org/project/pywxdump/) | 取账号信息+密钥、解密 DB、Web 查看、导出 html/csv | Python，**MIT** | 3.x 全系列；README 未声明 4.0 | 仓库「删库」；PyPI 最新 **3.1.46（2025-10-15）**；约 **9.7k star**（[来源](https://www.star-history.com:2096/xaoyaoo/pywxdump/)） |
| [haowxdump](https://pypi.org/project/haowxdump/) | PyWxDump 同名包，可视为续命/镜像发行 | Python，MIT | 同 pywxdump | 3.1.46（**2026-01-16**），maintainer haoqi7 |
| [WeChatMsg / 留痕 MemoTrace](https://raw.githubusercontent.com/shixiaogaoya/MemoTrace/master/readme.md) | 聊天记录导出 HTML/CSV/TXT/Word/sqlite + 年度报告 + AI 聊天 | Python + PyQt + Flask，**MIT** | **明确「支持微信 4.0」**，有 4.0 设计文档 | 原仓库 404；约 **42.1k star**（[来源](https://www.star-history.com:2096/lc044/wechatmsg/)） |
| [0xlane/wechat-dump-rs](https://awesome.ecosyste.ms/projects/github.com%2F0xlane%2Fwechat-dump-rs) | Rust CLI，导出 key + 自动/离线批量解密 | Rust | 3.9.6–3.9.12.17 + **4.0.0.26–4.0.3.22**（默认 `--vv 4`） | 死于 DMCA 投诉（2026-01 清单 1849 star）；代码最后推送 2025-04 |
| AdminTest0/SharpWxDump | C# 版密钥提取（PyWxDump 的上游） | C# | 3.x 各版本偏移 | DMCA 清单 **4443 star**；[Gitee 镜像](https://gitee.com/kaoiki/SharpWxDump) |
| [ppwwyyxx/wechat-dump](https://github.com/ppwwyyxx/wechat-dump) | **安卓**（需 root）EnMicroMsg.db 解析、渲染 HTML | Python | 安卓微信，2025-01-01 验证可用 | DMCA 清单 1825 star |
| `wxdump` / `wxdump_web` | 前者多为 PyWxDump 的 fork 命名（b3nguang/wxdump 0 star 等）；`wxdump_web` 是 PyWxDump 的 **Vue+ElementUI 前端**，非独立工具 | Vue/JS | 跟随 PyWxDump | 均随上游一起停更 |
| [WechatExporter](https://github.com/mengzhisuoliu/WechatExporter) | iOS **iTunes 备份**解析导出（非 PC 库解密） | C# | 走手机备份，不涉 PC 密钥 | 不在 DMCA 清单 |

## 密钥与解密原理

### 1) 3.x 的密钥获取（PyWxDump / SharpWxDump 路线）

1. 定位 `WeChat.exe` 主进程 → 加载 `WeChatWin.dll`，取模块基址。
2. 按版本在 `version_list.json` 查 6 个偏移，顺序为 **昵称、账号、手机号、邮箱（3.7+ 多为 0）、KEY、wxid**；返回值形如 `{'3.9.7.29': [63486984, 63488320, 63486792, 0, 63488256, 56006136]}`（[python1.0_README](https://raw.githubusercontent.com/pxx199181/PyWxDump/master/doc/python1.0_README.md)）。
3. 偏移失效时用 `BiasAddr(account, mobile, name, key, db_path).run()` 反查：以内存中已知的手机号/昵称/账号字符串为锚点，搜其附近 **16 字节对齐**、内容为 **32 字节（64 hex）** 的候选密钥，逐个尝试解密验证（[CE 获取基址思路](https://www.ctfiot.com/48924.html)、[免登录读取微信聊天记录](http://cn-sec.com/archives/3236528.html)）。
4. 3.9.x 实测偏移样本：`WeChatWin.dll + 597D490 - 0xb0`（仅该版本有效）。
5. 硬约束：**微信必须已登录**。版本 `< 3.7.0.30` 仅运行不登录也能读到个人信息，`> 3.7.0.30` 必须登录后才能拿到密钥。

### 2) 4.0.x 的内存形态（仍可扫描）

微信 4.0 改用 **HMAC-SHA512**，v3 的偏移法失效；WCDB 会把派生后的 **raw key 以 SQL 字面量形式缓存**：`x'<64hex enc_key><32hex salt>'`（96 hex 总长）。因此 4.0.x 的最优策略是**暴力搜索 `x'[0-9a-fA-F]{64,192}'` 正则**，取前 64 hex 当候选 key、后 32 hex 当内嵌 salt；内嵌 salt 命中某 DB 的盐就直接验该库，否则遍历未匹配库，用 SQLCipher4 的 page-1 MAC 校验确认（[wxkey/internal/scan 权威描述](https://pkg.go.dev/github.com/r266-tech/wxkey/internal/scan)、[wechat-decrypt 原理段](https://raw.githubusercontent.com/328336690/wechat-decrypt/main/README.md)）。wechat-dump-rs 补充：v4 用**登录手机号**在内存中定位，key 通常在其前方 16 字节对齐处；未绑定手机号需特殊处理（[README](https://awesome.ecosyste.ms/projects/github.com%2F0xlane%2Fwechat-dump-rs)）。

### 3) 4.1.x 的机制变迁（关键坑）

| 阶段 | 版本 | 内存形态 | 被动扫描 |
|---|---|---|---|
| A | 4.0.x ~ 4.1.9.x（Win） | `x'<64hex enc_key><32hex salt>'` | ✅ |
| B | 4.1.1+ | 32 字节 passphrase（需 PBKDF2 派生） | ⚠️ 可用但需派生 |
| C | **4.1.10+（2025-05 起）** | enc_key 即用即弃，稳态内存**零可搜形态** | ❌ 确定失效 |

- 时间线：2025-05-13 Issue #96 首报 Linux/macOS 不再缓存 raw key；2025-05-24 Win 4.1.9.57 仍 27/27 命中；2025-05-27 Win 4.1.10.24 首报 0 候选（Issue #125）；对 4.1.10.53 做 1.3GB dump 离线穷举确认零驻留（Issue #152）。
- 替代方案（[wxlocal 4.1.13 研究文档](https://raw.githubusercontent.com/NinjaSln-labs/wxlocal/main/docs/WECHAT_4.1.13_RESEARCH.md)、[wechat-cli + wx_key 实录](https://sikinzen.github.io/posts/howtoconnectwechatandai/)）：
  - **断点**：x64dbg 在 `Weixin.dll` 的 `com.Tencent.WCDB.Config.Cipher` / `MMV1` 函数下断（4.1.1.19 RVA `0x4AEA40`，4.1.12.26 `0x3486140`，每版本必变），命中后读 RCX（4.1.12+）或 RDX（4.1.1.x）指向的 32 字节 passphrase。
  - **Frida spawn Hook**：4.1.12.26 验证 25/25 成功，必须 **spawn 注入**（attach 会错过设置时机）。
  - **DLL 注入 GUI**：[wx_key](https://github.com/ycccccccy/wx_key)，实测到 4.1.5.11，社区反馈可配合 chatlog 使用。
  - **降级提取（最稳）**：VM 装 ≤4.1.9 → 提密钥 → 升级回新版，密钥跨版本永久有效（加密算法与 passphrase 不随升级改变）。
  - **key_info.db**（`xwechat_files\all_users\login\<account>\key_info.db`，字段 `key_md5/key_info_md5/key_info_data`）**不能**直接当 passphrase，实测无法通过 HMAC 校验。
- macOS 4.1.x 另一条通用路线：lldb 断 `CCCryptorCreate`，`x4==32` 时读 `x3` 的 32 字节（[how-it-works](https://raw.githubusercontent.com/rmqg/wechat-mac-export/main/docs/how-it-works.md)）。

### 4) SQLCipher 参数（3.x vs 4.x）

| 参数 | 微信 3.x（SQLCipher 3） | 微信 4.x（SQLCipher 4） |
|---|---|---|
| 加密 | AES-256-CBC | AES-256-CBC |
| HMAC | **SHA1（20B）** | **HMAC-SHA512（64B）** |
| KDF 迭代 | **64000** | **256000**（PBKDF2-HMAC-SHA512） |
| KDF 算法 | SHA1 | SHA512 |
| 页大小 | 4096 | 4096 |
| reserve（IV+HMAC） | **48** | **80（IV 16 + HMAC 64）** |
| 每库 salt | 文件前 16 字节 | 文件前 16 字节 |

> 来源：[WEIXIN_DECRYPT_GUIDE](https://raw.githubusercontent.com/ZedeX/weixin-decrypte-script/main/WEIXIN_DECRYPT_GUIDE.md)、wechat-dump-rs README（V3 自定义页大小4096/KDF 64000/HMAC SHA1；V4 用 sqlcipher4 默认）。

**两个必须区分的 key 形态（最高频踩坑点）**：
- **raw key 形态**（4.0.x 内存里就是它）：`enc_key` 32 字节**直接**用于 AES，**不再过 PBKDF2**；mac key 才需派生：`mac_key = PBKDF2-HMAC-SHA512(enc_key, salt XOR 0x3a, iterations=2, dklen=32)`（[how-it-works](https://raw.githubusercontent.com/rmqg/wechat-mac-export/main/docs/how-it-works.md)）。
- **passphrase 形态**（4.1+ 内存里只有它）：`enc_key = PBKDF2-HMAC-SHA512(passphrase, salt, 256000, dklen=32)`，**每个 .db 的 salt 不同 → 每个库一个 enc_key**（[wechat-cli 实录](https://sikinzen.github.io/posts/howtoconnectwechatandai/)）。

**页面布局（4096B/页）**：`[0:16]=salt（仅第1页）`、`[16:4016]=密文`、`[4016:4032]=IV`、`[4032:4096]=HMAC`。第 1 页解出的明文需补回 `b"SQLite format 3\x00"` 头。**解密后即为标准 SQLite**，可直接 `sqlite3.connect()` / DB Browser 打开（[wechat-decrypt](https://raw.githubusercontent.com/328336690/wechat-decrypt/main/README.md)）。

## 数据库与表结构

### 数据目录模板

```
# 微信 3.x
%USERPROFILE%\Documents\WeChat Files\<wxid_xxx>\
  ├─ Msg\  Multi\MSG0.db…  MediaMSG0.db  MicroMsg.db  Emotion.db
  │        Favorite.db  Sns.db  Misc.db  PublicMsg.db  BizChat*.db
  │        FTSMSG.db  MultiSearchChatMsg.db  HardLink.db  Media.db
  └─ FileStorage\{MsgAttach,Image,Video,File,Cache}\<YYYY-MM>\…

# 微信 4.x
<自定义路径>\xwechat_files\
  ├─ <wxid_hash>\db_storage\{contact,session,message,media,favorite,
  │      emoticon,head_image,sns,hardlink,general,MMKV,solitaire}\*.db   # 约 25~30 个
  ├─ <wxid_hash>\msg\attach\<md5(username)>\<YYYY-MM>\Img\*.dat / *_t.dat
  └─ all_users\login\<account>\key_info.db
```
来源：WeChatMsg [数据库介绍](https://raw.githubusercontent.com/920922786/WeChatMsg/master/doc/%E6%95%B0%E6%8D%AE%E5%BA%93%E4%BB%8B%E7%BB%8D.md)、[wxlocal 4.1.13](https://raw.githubusercontent.com/NinjaSln-labs/wxlocal/main/docs/WECHAT_4.1.13_RESEARCH.md)、[wechat-decrypt](https://raw.githubusercontent.com/328336690/wechat-decrypt/main/README.md)。

### 各库职责

| DB | 关键表 | 内容 |
|---|---|---|
| **MSG0.db…** | `MSG`、`Name2ID` | 聊天记录核心；超 240MB 自动生成 MSG1.db |
| **MicroMsg.db** | `Contact`、`ContactHeadImgUrl`、`ChatRoom`、`ChatRoomInfo`、`Session`、`ChatInfo`、`ContactLabel`、`ExtraBuf`、`BizSessionNewFeeds` | 联系人/群/会话核心 |
| **MediaMSG0.db** | `Media` | 语音：`Key`、`Reserved0`（=MSG.MsgSvrID）、`Buf`（silk 数据） |
| **Emotion.db** | `CustomEmotion`、`EmotionPackageItem` | 表情包 |
| **Favorite.db** | `FavItems`、`FavDataItem`、`FavTags` | 收藏 |
| **Sns.db** | `FeedsV20`、`CommentV20` | 朋友圈 |
| **FTSMSG/FTSContact/FTSFavorite.db** | `FTSChatMsg2_content`(docid,c0content,c1entityId)、`FTSChatMsg2_MetaData`(docid,msgId,entityId,type) | 搜索索引，与 MSG 靠 `msgId` 关联 |
| Media/Misc/HardLink/BizChat* | `MediaInfo`/`ChatCRVoice`、`ContactHeadImg1`、硬链接映射 | 媒体元数据、头像、文件落盘目录 |

### 核心字段表（`MSG`，3.x 解密库）

| 字段 | 类型 | 含义 |
|---|---|---|
| `localId` | INTEGER | 本地消息 ID（PK） |
| `TalkerId` | INTEGER | **所在会话（房间）ID**，对应 `Name2ID` 行号，非发送者 |
| `MsgSvrID` | INTEGER | 服务器消息 ID，用于关联 MediaMSG.Media.Reserved0 |
| `Type` / `SubType` | INTEGER | 消息类型 / 子类型（见下节） |
| `IsSender` | INTEGER | 0=对方，1=自己（左/右气泡） |
| `CreateTime` | INTEGER | **秒级** Unix 时间戳 |
| `Sequence` | INTEGER | 非毫秒时间戳；= `CreateTime` 拼 3 位序号（同秒递增） |
| `StrTalker` | TEXT | 会话 username：`wxid_xxx` 私聊 / `xxx@chatroom` 群聊 / `gh_xxx` 公众号 |
| `StrContent` | TEXT | 文本消息正文；非文本时多为 **XML**（含图片宽高、语音时长等） |
| `DisplayContent` | TEXT | 拍一拍等展示文本 |
| `CompressContent` | BLOB | 引用/链接/文件等正文，官方称 **lz4 压缩**；4.0 起为 **zstd**（魔数 `28 B5 2F FD`） |
| `BytesExtra` | BLOB | **protobuf**（`MessageBytesExtra`）：群消息发送者 `message2.field1==1 → field2=wxid`，以及图片/视频本地缓存路径 |
| `BytesTrans` | BLOB | 恒空 |
| `Status/FlagEx/StatusEx/Reserved0~6` | — | 官方语义不详 |

**群聊 vs 私聊**：`StrTalker LIKE '%@chatroom'` 即群；群内「谁发的」必须两步——(1) 解析 `BytesExtra` 的 `MessageBytesExtra` 取 wxid；(2) 该 wxid 去 `ChatRoom.RoomData` 找群名片，找不到回落 `Contact.NickName`。

**群成员映射（`ChatRoom.RoomData` 是 protobuf，非文本）**：用 WeChatMsg 的 `roomdata_pb2.ChatRoomData` 反序列化，遍历 `members`，`member.wxID → member.displayName`（displayName 为空则回落 `Contact.NickName`）。官方参考实现（[package_msg.py](https://codeberg.org/kangroo/WeChatMsg/raw/commit/55e4ee77633a9f37fac2cc5b8cdd0b560bccc55a/app/DataBase/package_msg.py)）：

```python
from app.util.protocbuf.roomdata_pb2 import ChatRoomData
from app.util.protocbuf.msg_pb2 import MessageBytesExtra
crd = ChatRoomData(); crd.ParseFromString(room_data_blob)
members = {m.wxID: m.displayName for m in crd.members if m.displayName}
mb = MessageBytesExtra(); mb.ParseFromString(bytes_extra)
wxid = next(t.field2 for t in mb.message2 if t.field1 == 1)
sender = members.get(wxid) or contact_nick.get(wxid, "")
```

**4.0.x 表结构变化（重要）**：不再是一库一表，而是**每会话一张 `Msg_<md5(username)>` 表**，分片在 `message_0.db…message_9.db`；字段改名/新增：

| 4.x 字段 | 说明 |
|---|---|
| `local_id` / `server_id` | 本地/服务器 ID |
| `local_type` | 消息类型，真实类型 = `local_type & 0xFFFF`（高位打包子类型） |
| `real_sender_id` | → 同库 `Name2Id.rowid` → 发送者 wxid |
| `create_time` | 秒级时间戳 |
| `message_content` | BLOB，可能是 zstd 压缩体（`WCDB_CT_message_content == 4` 时需 `zstandard.decompress`） |
| `compress_content` | 备用压缩字段 |
| `packed_info_data` | protobuf 附件元数据（图片文件名等） |
| `Name2Id(user_name, is_group)` | 会话/发送者映射表 |

⚠️ **逐库校验坑**：`message_0.db`（最新）的 `real_sender_id` 是全局 `Name2Id.rowid`，但 `message_1/2.db`（历史）可能是会话内角色编号（1/2）且不同库含义相反，导出前必须用「锚点句」逐库校验发送者映射。
来源：[wxlocal](https://raw.githubusercontent.com/NinjaSln-labs/wxlocal/main/docs/WECHAT_4.1.13_RESEARCH.md)、[WEIXIN_DECRYPT_GUIDE](https://raw.githubusercontent.com/ZedeX/weixin-decrypte-script/main/WEIXIN_DECRYPT_GUIDE.md)、[how-it-works](https://raw.githubusercontent.com/rmqg/wechat-mac-export/main/docs/how-it-works.md)。

## 消息类型映射

### `Type` / `SubType`（3.x，官方 README 表）

| Type | SubType | 含义 |
|---|---|---|
| 1 | 0 | 文本 |
| 3 | 0 | 图片 |
| 34 | 0 | 语音 |
| 43 | 0 | 视频 |
| 47 | 0 | 动画表情（第三方表情包） |
| 48 | — | 位置（3.x 常见，来源：多方一致但**官方表未列**） |
| 49 | 1 | 类文本消息（如阿里云盘邀请） |
| 49 | 5 | 卡片式链接（标题/简介在 CompressContent，封面缓存路径在 BytesExtra） |
| 49 | 6 | 文件（文件名+下载链接在 CompressContent，本地路径在 BytesExtra） |
| 49 | 8 | 用户上传 GIF 表情（CDN 链接） |
| 49 | 19 | 合并转发的聊天记录 |
| 49 | 33/36 | 分享的小程序 |
| 49 | 57 | **引用消息**（StrContent 为空，收发内容都在 CompressContent） |
| 49 | 63/88 | 视频号直播/回放 |
| 49 | 87 | 群公告 |
| 49 | 2000 | 转账（发出/接收/退还） |
| 49 | 2003 | 赠送红包封面 |
| 10000 | 0 | 系统通知（灰色居中） |
| 10000 | 4 | 拍一拍（拍者/被拍者账号在 DisplayContent） |
| 10000 | 8000 | 系统通知（含「邀请入群」） |

来源：[微信PC端数据库简述（PyWxDump 原文，WeChatMsg 转载）](https://raw.githubusercontent.com/922786/WeChatMsg/master/doc/%E6%95%B0%E6%8D%AE%E5%BA%93%E4%BB%8B%E7%BB%8D.md)。4.x 的 `local_type` 用同一套数值（1/3/34/43/47/49/10000），真实类型取 `& 0xFFFF`。

### 语音 SILK → MP3

- 语音源：`MediaMSG.db` 的 `Media.Buf`（silk v3）或 `msg\attach` 下 `.amr/.silk` 文件；需按 `MSG.MsgSvrID == Media.Reserved0` 关联。
- Python 方案：**`pilk`**（python silk codec bindings，支持微信语音编解码，[repo](https://github.com/foyoux/pilk)，PyPI `pilk`，含预编译二进制）：`pilk.silk_to_pcm2` 拿到 PCM 后交 ffmpeg/pydub 转 mp3；或调用 `silk-v3-decoder`（需自编译 `silk_v3_decoder`）。
- 另可用 `wxlocal` / wechat-dump-rs 的做法：silk → pcm → ffmpeg。

### 图片 `.dat` 解密（三种格式）

| 格式 | 时期 | 文件头 | 算法 | 密钥来源 |
|---|---|---|---|---|
| 旧 XOR | ~2025-07 | 无 | 单字节 XOR | 自动推断：`header[0] ^ magic[0]`（JPEG `FF D8 FF`、PNG `89 50 4E 47`、GIF `47 49 46 38`、BMP `42 4D`、TIFF `49 49 2A 00`） |
| V1 | 过渡期 | `07 08 56 31 08 07` | AES-128-ECB + XOR | **固定 AES key `cfcd208495d565ef`**（=`md5("0")[:16]`），XOR 默认 `0x88` |
| V2 | 2025-08+ | `07 08 56 32 08 07` | AES-128-ECB + XOR | AES key 需从进程内存提取（**仅在微信查看图片时临时加载**） |

V1/V2 结构：`[6B 签名][4B aes_size LE][4B xor_size LE][1B padding]` + `[AES-ECB 段][原文段][XOR 段]`，`aligned_aes_size = aes_size - (~(~aes_size % 16))`。
参考实现：[chatlog `dat2img`（Go，常量 `V4XorKey=0x37`、`V4DatHeader=07 08 56 31`、`ScanAndSetXorKey` 扫 `_t.dat` 反推 XOR key）](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.11/pkg/util/dat2img)、[WEIXIN_DECRYPT_GUIDE §§6.2–6.5](https://raw.githubusercontent.com/ZedeX/weixin-decrypte-script/main/WEIXIN_DECRYPT_GUIDE.md)。

## Python 调用示例

### PyWxDump（README 官方示例 + 源码签名）

```bash
pip install -U pywxdump     # 或 haowxdump；Python >=3.8,<4
```

```python
from pywxdump import (WX_OFFS, read_info, get_wechat_db, batch_decrypt,
                      BiasAddr, get_wx_info, merge_db, decrypt_merge,
                      all_merge_real_time_db)
import pythoncom

pythoncom.CoInitialize()                       # COM 必须先初始化，否则报错
wx_info = get_wx_info(WX_OFFS)                 # list[dict]: pid/version/key/name/account/mobile/mail/wxid
pythoncom.CoUninitialize()
# 等价：read_info(WX_OFFS, True)

user_dirs = get_wechat_db("all", "WeChat Files", "wxid_", True)   # 定位 db 目录

bias = BiasAddr("微信账号", "13800138000", "微信昵称", "", "微信文件夹路径")
print(bias.run(True, "WX_OFFS_path"))          # 无偏移版本时反查基址

ok, out = batch_decrypt("密钥", "db路径(目录或文件)", "out目录", True)  # 解密
db_path  = get_wx_db("微信文件夹路径")          # 再 merge_db(db_path, out_path)
```

来源：[UserGuide.md](https://raw.githubusercontent.com/tqjason/PyWxDump/master/doc/UserGuide.md)。CLI 等价：`wxdump info` / `wxdump decrypt -k <key> -i <path> -o <out>` / `wxdump merge` / `wxdump ui` / `wxdump api`（FastAPI，默认 5000 端口，**可直接给插件当 HTTP 后端**）。

### PyWxDump 内置 HTTP API（`wxdump api`）

| 方法 | 路径 | 参数 |
|---|---|---|
| POST | `/init_last_local_wxid` | — |
| POST | `/init_last` | `my_wxid` |
| POST | `/init_key` | `{wx_path, key, my_wxid}` → 自动 `decrypt_merge` |
| POST | `/init_nokey` | `{merge_path, wx_path, my_wxid}` |
| GET/POST | `/wxinfo` | → `get_wx_info(WX_OFFS)` |
| POST | `/biasaddr` | `{mobile, name, account, key, wxdbPath}` |
| GET/POST | `/decrypt` | `key, wxdbPath, outPath` |
| POST | `/merge` | `{dbPath, outPath}` |
| GET/POST | `/realtimemsg` | 增量合并实时消息 |

来源：[local_server.py](https://raw.githubusercontent.com/tqjason/PyWxDump/master/pywxdump/api/local_server.py)。

### WeChatMsg 是否有 API

**未找到官方 Python API/HTTP API。** 它是 PyQt 桌面程序（`python main.py`），代码内可复用模块：`app/DataBase/`（解密+导出）、`app/util/protocbuf/`（`msg_pb2`、`roomdata_pb2`）、`app/analysis/`、`app/web_ui/`（Flask 年报）。解密后库落在 `./app/DataBase/Msg`，官方建议直接用 DB Browser 打开。工程目录说明见[开发者手册](https://raw.githubusercontent.com/920922786/WeChatMsg/master/doc/%E5%BC%80%E5%8F%91%E8%80%85%E6%89%8B%E5%86%8C.md)。另有 `MemoAI/` 目录（AI 聊天）。

### 自动回复（本项目插件部分，别用数据库做发送）

数据库**只能读**；发消息必须走 UI 自动化或 Hook。推荐 **wxauto4**（`pip install wxauto4`，UI Automation，**不做注入/内存修改**，封号风险最低）：

```python
from wxauto4 import WeChat
from wxauto4.msgs import FriendMessage

wx = WeChat()
def on_message(msg, chat):
    if isinstance(msg, FriendMessage):
        reply = my_ai_summarize_and_reply(msg.content)   # 你的 AI 逻辑
        chat.SendMsg(reply)
wx.AddListenChat('文件传输助手', on_message)
wx.KeepRunning()
```

- 支持微信 **4.1**；免费版兼容到客户端 **4.1.8.107**，Python 3.9–3.12（Plus 版 `wxautox4` 到 3.13，需激活码）。
- 4.x 的 UI 是**实时渲染**：消息滚出可视区即销毁，`GetNextNewMessage` 改为 **callback 模式**，且回调内**不能**发消息/引用（会重置窗口）。官方会**自动把聊天窗口拉到最大高度**以保证监听锚点。
- 来源：[wxauto 官方文档](https://docs.wxauto.org/llms-full.txt)（含[从 3.9 到 4.x 适配机制变化](https://docs.wxauto.org/blog/A9dYFme8.html)）。

## 限制与风险

1. **必须微信运行且已登录**才能读密钥（`>3.7.0.30` 起）；登录态下才有 key。
2. **版本升级即失效**：3.x 路线靠 `version_list.json` 的每版本偏移（昵称/账号/手机/邮箱/KEY/wxid 六元组），4.1 路线靠 `Weixin.dll` 的 RVA——**每个小版本几乎必变**，需重新逆向。
3. **4.1.10+ 零驻留**：纯内存扫描在这些版本上**确定无效**，必须断点/Frida/DLL Hook，或降级到 ≤4.1.9 提取（passphrase 跨版本永久有效，但换账号/换平台需重提）。
4. **权限与杀软**：`ReadProcessMemory` 通常需**管理员权限**；打包 exe 常被误报，WeChatMsg 官方指引是「右击以管理员身份运行 + 杀软提示选略过」，`key 为 none 可重启电脑`。注入类（Frida/DLL）可能触发反注入检测→崩溃、报错甚至**封号**（真实案例存在）；被动内存读取风险相对低。
5. **法律与合规（最高风险）**：2026-01 腾讯 DMCA 一次性下架 30+ 仓库，**PyWxDump、WeChatMsg、wechat-dump-rs、SharpWxDump、chatlog、wx-cli 等均已下架或删库**，多个作者收到律师函；`sjzar/chatlog` 于 2025-10 删库。账号层面违反微信服务协议，可能被限制；开发者层面「二次开发/分发」风险高于自用。
6. **多账号/多平台**：每个 wxid 独立 passphrase；同账号 Windows 与 macOS 的 key 不通用；手机端 `Backup.db` 独立密钥，PC 工具无法解密。
7. **媒体 ≠ 数据库**：库解密了图片仍是加密 `.dat`；V2 图片的 AES key 只在微信**查看该图那一刻**在内存，需边看图边抓；视频/文件同理。
8. **WAL**：实时消息常在 `-wal`（微信预分配固定 4MB，**mtime 会变而大小不变**，所以必须用 mtime 检测），解密 WAL frame 还需校验 salt 跳过旧周期残留。
9. **惰性加载**：部分 .db 只有访问对应聊天后才被打开，冷启动扫描可能漏库。
10. **合规建议**：仓库不内置密钥提取、只做「用户自行填入 key 的加密 SQLite 导出工具」可显著降低风险（社区共识）；只在本人设备/本人账号/自有会话上使用，对发送类操作加频率限制、目标核验与人工确认。

## 参考链接

- DMCA 与生态：[Tencent DMCA 2026-01-08](https://github.com/github/dmca/blob/master/2026/01/2026-01-08-tencent.md)｜[下架清单+star](https://www.cnblogs.com/longware/p/19479076)｜[V2EX 讨论](https://global.v2ex.co/t/1186673)
- PyWxDump：[PyPI pywxdump](https://pypi.org/project/pywxdump/)｜[PyPI haowxdump](https://pypi.org/project/haowxdump/)｜[UserGuide](https://raw.githubusercontent.com/tqjason/PyWxDump/master/doc/UserGuide.md)｜[python1.0_README](https://raw.githubusercontent.com/pxx199181/PyWxDump/master/doc/python1.0_README.md)｜[local_server.py](https://raw.githubusercontent.com/tqjason/PyWxDump/master/pywxdump/api/local_server.py)｜[wxdump_web](https://github.com/xaoyaoo/wxdump_web)
- WeChatMsg / MemoTrace：[镜像 README](https://raw.githubusercontent.com/shixiaogaoya/MemoTrace/master/readme.md)｜[开发者手册](https://raw.githubusercontent.com/920922786/WeChatMsg/master/doc/%E5%BC%80%E5%8F%91%E8%80%85%E6%89%8B%E5%86%8C.md)｜[数据库介绍](https://raw.githubusercontent.com/920922786/WeChatMsg/master/doc/%E6%95%B0%E6%8D%AE%E5%BA%93%E4%BB%8B%E7%BB%8D.md)｜[package_msg.py](https://codeberg.org/kangroo/WeChatMsg/raw/commit/55e4ee77633a9f37fac2cc5b8cdd0b560bccc55a/app/DataBase/package_msg.py)
- 数据库字段与类型：[微信PC端数据库简述](https://raw.githubusercontent.com/yzqzy/wechat-assistant/main/docs/database.md)
- 4.x 机制与 SQLCipher 参数：[wxlocal 4.1.13 研究](https://raw.githubusercontent.com/NinjaSln-labs/wxlocal/main/docs/WECHAT_4.1.13_RESEARCH.md)｜[WEIXIN_DECRYPT_GUIDE](https://raw.githubusercontent.com/ZedeX/weixin-decrypte-script/main/WEIXIN_DECRYPT_GUIDE.md)｜[wechat-decrypt README](https://raw.githubusercontent.com/328336690/wechat-decrypt/main/README.md)｜[wechat-mac-export how-it-works](https://raw.githubusercontent.com/rmqg/wechat-mac-export/main/docs/how-it-works.md)｜[wxkey scan 包文档](https://pkg.go.dev/github.com/r266-tech/wxkey/internal/scan)
- 3.x 密钥搜索原理：[SharpWxDump 取证文](https://www.ctfiot.com/48924.html)｜[免登录读取微信聊天记录](http://cn-sec.com/archives/3236528.html)｜[wechat-dump-rs](https://awesome.ecosyste.ms/projects/github.com%2F0xlane%2Fwechat-dump-rs)
- 图片/语音：[chatlog dat2img](https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.11/pkg/util/dat2img)｜[pilk](https://github.com/foyoux/pilk)
- 自动回复：[wxauto 官方文档](https://docs.wxauto.org/llms-full.txt)｜[3.9→4.x 适配变化](https://docs.wxauto.org/blog/A9dYFme8.html)
