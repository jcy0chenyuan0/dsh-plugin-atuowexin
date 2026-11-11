# dsh-plugin-atuowexin v0.7.1 — 微信本地聊天记录只读

> 只做一件事：**读取本机微信聊天记录**。不发消息、不写微信库、不联网、不调用云端模型。
>
> 8 个主工具：check / locate / close / hook / list / read / decrypt / unread
>
> 4.1.15.56 实测：Frida 取 passphrase → 20/20 库解密 → 全量 12,657 条（WCDB 接口单会话上限 1,200）

## 兼容性

| 项 | 要求 / 现状 |
|---|---|
| 系统 | Windows 10/11 x64（依赖 ctypes、tasklist、原生 DLL；macOS / Linux 未适配） |
| Python | 3.10+（用到 str | Path 等新语法） |
| 微信 | 4.x。4.1.15.56 已实测；其它 4.1.x 用 scan_lea.py 现算函数地址，不写死 |
| Node | 可选：backends.wcdb 需要 node + koffi |
| Frida | 可选：工具 7 decrypt 需要 frida |
| 加密库 | 可选：decrypt 需要 pycryptodome |

缺依赖时对应工具会明确报错，不会静默降级。

## 安装额外依赖（只影响工具 7）

    python -m pip install --target engine/pylibs frida pycryptodome

## 安全与脱敏

- **密钥**：只出现在内存与 workdir 下的 key 文件；CLI 默认不回显明文（只显示前 8 位）。
- **脱敏**：--redacted 遮蔽 手机号 / 身份证 / 银行卡 / 邮箱 / wxid / 车牌；read 与 unread 都支持。
- **只读**：读取侧只调 wcdb_get_* 与 sqlite3 只读连接；Frida 只读寄存器指向的内存，不写目标进程。
- **解密产物**：明文库写在 <workdir>/decrypted（或 --out-dir），请自行保管或删除。
- **第三方脚本**：engine/tools/frida/ 移植自 AyaReiOwO/wechat-4.1.15-key-capture（MIT），见该目录 ATTRIBUTION.md。

## 八个工具

| # | 工具 | 作用 |
|---|---|---|
| 1 | wechat_check | 检测微信是否安装 |
| 2 | wechat_locate | 找聊天记录位置（账号目录 + db_storage） |
| 3 | wechat_close | 微信在跑就退出，没跑跳过 |
| 4 | wechat_hook | 先起 Hook → 拉起微信停登录页 → 等登录 → 取密钥（默认 5 分钟；超时回第 2 步） |
| 5 | wechat_list | 会话名称 + 最新时间 |
| 6 | wechat_read | 按名/ID 批量读内容（支持脱敏；单会话上限 1200 条） |
| 7 | wechat_decrypt | Frida 取 passphrase → 派生每库 enc_key → AES 整库解密（全量可读） |
| 8 | wechat_unread | 只看未读（读解密后 session.db 的 unread_count） |

## 命令行

    python -m atuowexin wx check
    python -m atuowexin wx locate
    python -m atuowexin wx close
    python -m atuowexin wx hook --scan 300
    python -m atuowexin wx list --limit 50
    python -m atuowexin wx read --conversation "<会话名>" --limit 200 --redacted
    python -m atuowexin wx decrypt --seconds 180
    python -m atuowexin wx unread --limit 50 --redacted

## 目录

    engine/atuowexin/
      wxtool.py       六个核心动作 + unread
      keyfrida.py     Frida 取 passphrase + AES 整库解密
      capture.py      启动期 Hook 捕获（wx_key.dll）
      autopilot.py    关微信 → 起 Hook → 拉起微信 的编排
      backends/       chatlog / wcdb / decrypted_sqlite / mock
      model.py        归一化模型 + redact / redact_pii
    engine/tools/
      capture_key.cjs   启动期 Hook（每库一把 key）
      wcdb_server.js    WCDB 只读 HTTP 服务
      frida/            scan_lea / frida_hook / validate（第三方，MIT）
    lib/
      index.js        插件入口
      core.js         8 个工具的注册

## 许可

插件本体 MIT；engine/tools/frida 下脚本遵循上游 MIT；engine/tools/dll 下的原生库来自腾讯，仅在本机只读使用。
