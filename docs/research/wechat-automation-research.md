# Windows 微信「自动发送/UI 自动化」技术调研简报

调研时间：2026-10。所有结论均来自实际抓取的 GitHub / PyPI / 官方文档页面，未抓到的标注「未找到可靠资料」。

---

## 一、结论速览

1. **微信 4.x 已把 hook 路线基本打死，UI Automation（UIA）是当下唯一可持续路线。** 微信 4.x 客户端 UI 改为「实时渲染」：只有屏幕可见区内的消息才注册 UIA 控件，滚出可见区立即销毁并重新生成新运行时 ID。这直接导致 3.9 时代的滚动取历史、消息对象长期持有等做法全部失效，wxauto 作者被迫重构 API（`LoadMoreMessage` → `GetHistoryMessage`，`GetNextNewMessage` 增加 callback 且执行后自动关窗）。([机制说明](https://docs.wxauto.org/blog/A9dYFme8))
2. **wxauto 生态现状（作者均为 Cluic）**：
   - `wxauto`（开源，GitHub [cluic/wxauto](https://github.com/cluic/wxauto)）：**只支持微信 3.9.X**，代码内 `VERSION = '3.9.11.17'`。README 明文「仅用于 UIAutomation 技术交流学习，禁止用于实际生产项目」。PyPI 上 `wxauto` 包名**已不存在（404）**，只能从 GitHub/官网获取。
   - `wxauto4`（PyPI [wxauto4](https://pypi.org/project/wxauto4/)，**免费**）：面向微信 4.x，`pip install wxauto4`，版本号 41.1.7（2026-09-01），Python 3.9–3.13，**免费版仅兼容到微信客户端 4.1.8.107**。依赖：`comtypes / pywin32 / pyperclip / psutil / pillow / httpx / tenacity / colorama`（`pyperclip` 说明发送走剪贴板）。
   - `wxautox4`（PyPI [wxautox4](https://pypi.org/project/wxautox4/)，**收费 Plus**）：`pip install wxautox4` → `wxautox4 auth activate 激活码`。41.2.1（2026-09-23）。核心模块用 Nuitka 编译、**内置许可证校验**，一码一机、不可解绑，订阅期 1 年（文档见 [激活码说明](https://docs.wxauto.org/docs/issues)）。文档中方法名前带 ✨ 的即 Plus 专属。
   - 两者 import 名不同：`from wxauto4 import WeChat` / `from wxautox4 import WeChat`；Plus 独有的 msgs 子模块：`from wxautox4.msgs import FriendMessage`。
3. **API 在 3.9 与 4.x 之间不兼容，且监听模型完全不同**：3.9 是「打开独立子窗口 + 轮询 `GetListenMessage`」；4.x 是「`AddListenChat(nickname, callback)` + 守护线程回调 + `KeepRunning()`」，并且 `GetListenMessage` **在 4.x 文档中已不存在**（被 callback 模式取代）。
4. **官方文档明确给出安全设计方向**：可接受使用政策要求「对可能产生外部影响的操作设置目标核验、人工确认、日志记录和异常停止机制」，`WxParam.SEND_CONTENT_RATIO`（默认 0.9，输入框内容相似度校验后才触发 Enter）正是「防发错内容」的内置机制。([可接受使用政策](https://docs.wxauto.org/legal/acceptable-use))
5. **风控是真实的**：腾讯有专门的[打击外挂公告](https://mp.weixin.qq.com/s/YfYKVyK09gOelvM840-qJQ)；chatgpt-on-wechat（现 CowAgent）仓库 issue #2568 标题直接写明「给该项目适配 WechatFerry 协议【暂时不要使用了，会引起风控】」([issue](https://github.com/zhayujie/CowAgent/issues/2568))。2025 年「豆包手机」登录微信被下线事件也说明平台对第三方自动化调用 App 能力的容忍度极低（[报道](https://www.stcn.com/article/detail/3523293.html)）。
6. 对插件的最优工程决策：**默认「只生草稿、人工点发送」**；自动发送作为可选开关；内核用 wxauto4（免费，覆盖 4.1.8 及以下）或 wxautox4（付费，跟进最新版）。

---

## 二、方案对比表

| 方案 | 原理 | 支持微信版本 | 维护状态 | 优点 | 缺点/风险 |
|---|---|---|---|---|---|
| **wxauto**（开源） | UIA 控件树，模拟人工点击/剪贴板粘贴 | 仅 3.9.X | 仓库仍在（master README 标注 2021-2025），PyPI 包已下架 | 免费、API 简单、`GetListenMessage` 轮询式监听直观 | 3.9 客户端已过时；禁止生产使用 |
| **wxauto4**（免费） | UIA（含自动拉高窗口保锚点） | 微信 4.x，**最新支持 4.1.8.107** | 活跃（2026-09 仍在发版） | 免费、官方文档完整、可控性高 | 版本兼容窗口窄；微信小版本升级即可能失效 |
| **wxautox4**（Plus） | UIA + Nuitka 编译 + 授权校验 | 微信 4.1（含 4.1.9+ 语音条等） | 活跃（2026-09-23 v41.2.1） | 功能最全（历史消息、朋友圈、好友详情、OCR、下载），跟进新版本快 | 收费、一码一机不可解绑、订阅 1 年 |
| **WeChatFerry / wcferry** | DLL 注入 + Hook + RPC（pynng 端口 10086/10087），可查库、可收发 | **仅 3.9.x**（客户端版本号 `39.5.2.0`，`w` 段=39 即 3.9） | [文档](https://wechatferry.readthedocs.io/zh/latest/readme_link.html)在维护，但 wcfhttp 已弃用 | 功能最全（DB 查询、转账、群管理）、延迟最低、wxid 级精确 | 注入行为最易被判定外挂；4.x 不可用；有风控报告 |
| **wxhelper** | 微信 3.9 注入 Hook（DLL/HTTP 接口） | 3.9.x | 社区项目，跟进滞后 | 接口化、便于多语言调用 | 同上，且更新依赖作者适配速度 |
| **ntchat** | 3.9 hook（Python 封装） | 3.9.x | 已基本停滞（社区存在无头客户端封装 [ntchat-client](https://github.com/JustUndertaker/ntchat-client)） | 曾经的 Python 友好 hook 方案 | 版本停在 3.9 |
| **pywinauto** | Win32/UIA 后端，控件级操作 | 任意（需自己写控件定位） | 长期维护 | 通用、文档成熟，`type_keys`/`set_text` 可控 | 需要自己适配微信 4.x 控件树；中文 `type_keys` 不可靠，通常要配剪贴板 |
| **uiautomation** | 直接封装 UIA COM API（wxauto 内部即用它） | 任意 | 维护良好 | 最底层最灵活，可自由探索控件树 | 全部定位逻辑要自己写 |
| **pyautogui** | 屏幕坐标 + 模拟键鼠/截图 | 任意 | 维护中 | 零依赖、跨应用 | **最脆弱**：分辨率/DPI/窗口位置一变即失效，无控件语义，易发错人 |

---

## 三、wxauto 精确 API 示例

### 3.1 开源版 wxauto（微信 3.9.X，签名取自源码 `wxauto/wxauto.py`，VERSION 3.9.11.17）

```python
from wxauto import WeChat
wx = WeChat(language='cn', debug=False)       # __init__(language='cn', debug=False)
# 内部：UiaAPI = uia.WindowControl(ClassName='WeChatMainWndForPC', searchDepth=1)
# A_ChatIcon/A_ContactsIcon(导航)  B_Search=EditControl(Name='搜索')  C_MsgList=ListControl(Name='消息')
```

| 方法 | 签名 | 返回值 |
|---|---|---|
| `ChatWith` | `ChatWith(who, timeout=2)` | `str` 完整匹配名；失败 `False`；Ctrl+F 搜索后**取第一个结果** |
| `SendMsg` | `SendMsg(msg, who=None, clear=True, at=None)` | **无返回值（None）** |
| `SendFiles` | `SendFiles(filepath, who=None)`（str/list/tuple/set） | `bool` |
| `GetAllMessage` | `GetAllMessage(savepic=False, savefile=False, savevoice=False)` | `list`，元素为 `[sender, content, id...]` 列表，`msg[-1]` 为消息 id |
| `AddListenChat` | `AddListenChat(who, savepic=False, savefile=False, savevoice=False)` | 无（写入 `self.listen[who] = ChatWnd(...)`，**独立聊天窗口**） |
| `GetListenMessage` | `GetListenMessage(who=None)` | 有 who → 该对象新消息；无 who → `{chat: msg}`；无新消息 → `None`/`{}` |
| `GetNextNewMessage` | `GetNextNewMessage(savepic=False, savefile=False, savevoice=False, timeout=10)` | `{聊天名: msgs}`，无新消息 `{}` |
| `GetAllNewMessage` | `GetAllNewMessage(max_round=10)` | `dict` |
| `GetSessionList` | `GetSessionList(reset=False, newmessage=False)` | `{聊天名: 新消息条数}` |
| `GetSession` | `GetSession()` | `List[SessionElement]`（`.name/.time/.content/.isnew`） |
| `CheckNewMessage` | `CheckNewMessage()` | `bool`，基于**导航栏图标红色像素**判断 |
| `LoadMoreMessage` | `LoadMoreMessage()` | `bool`（向上滚轮加载历史） |
| `AtAll` | `AtAll(msg=None, who=None)` | 无 |
| `GetGroupMembers` / `GetAllFriends` / `GetNewFriends` / `AddNewFriend` / `RemoveListenChat` / `SwitchToChat` / `SwitchToContact` | 见源码 | — |

**3.9 发送实现（关键，直接决定可靠性设计）**：

```python
# SendMsg 内部（精简）
editbox = self.ChatBox.EditControl(searchDepth=10)   # 输入框
if clear: editbox.SendKeys('{Ctrl}a')
if not editbox.HasKeyboardFocus: editbox.Click(simulateMove=False)
t0 = time.time()
while True:                                          # 校验循环，10s 超时
    if time.time() - t0 > 10: raise TimeoutError(...)
    SetClipboardText(msg)                            # 剪贴板写入（支持中文/表情）
    editbox.SendKeys('{Ctrl}v')                      # 粘贴
    if editbox.GetValuePattern().Value: break        # 读回输入框确认非空才发
editbox.SendKeys('{Enter}')                          # 回车发送
```

### 3.2 wxauto4 / wxautox4（微信 4.x，签名取自 [docs.wxauto.org](https://docs.wxauto.org/llms-full.txt)）

```python
from wxauto4 import WeChat        # 免费版；Plus 版：from wxautox4 import WeChat
from wxautox4.msgs import FriendMessage          # Plus 专用消息类型
from wxautox4 import WxParam

wx = WeChat()                                    # __init__(nickname=None, debug=False)
```

| 方法 | 参数（名称/类型/默认值） | 返回值 |
|---|---|---|
| `ChatWith` | `who: str`（必填）、`exact: bool=False` | 无 |
| `ChatInfo` | — | `dict`，如 `{'chat_type':'friend','chat_name':'张三'}`；群聊含 `group_member_count` |
| `SendMsg` | `msg: str`(必填)、`who: str=None`、`clear: bool=True`、`at: str|list=None`、`exact: bool=False` | `WxResponse`（`if result:` 判成功，失败取 `result['message']`） |
| `SendFiles` | `filepath: str|list`(必填)、`who=None`、`exact=False` | `WxResponse` |
| `GetAllMessage` | — | `List[Message]`（当前窗口） |
| `GetSession` | — | `List[SessionElement]`（`.info`） |
| `AddListenChat` ✨ | `nickname: str`(必填)、`callback: Callable[[Message, Chat], None]`(必填) | 成功→`Chat` 子窗口实例；失败→`WxResponse` |
| `RemoveListenChat` ✨ | `nickname: str` | `WxResponse` |
| `StartListening` / `StopListening` ✨ | `StopListening(remove: bool=True)` | 无 |
| `GetNextNewMessage` ✨ | `filter_mute: bool=False`、`callback: Callable[[Message],None]=None` | `Dict[str, List[Message]]`，键为聊天名；**执行后自动关闭当前窗口** |
| `GetHistoryMessage` ✨ | `n: int`(必填)、`callback=None`、`interval: float=0.2`、`speed: int=1`、`goback: bool=True` | `List[Message]`；callback 返回 `WxParam.CALLBACK_STOP_SIGN` 可提前停止 |
| `KeepRunning` ✨ | — | 阻塞主线程（监听走守护线程，不调用则主线程退出即结束） |
| `GetSubWindow` / `GetAllSubWindow` ✨ | `nickname` | `Chat` / `List[Chat]` |
| `IsOnline` / `GetMyInfo` / `GetDialog` ✨ | `GetDialog(wait=3)` | `bool` / `Dict` / `WeChatDialog` |

**4.x 监听官方示例（照抄即可跑）**：

```python
from wxautox4 import WeChat
from wxautox4.msgs import FriendMessage

wx = WeChat()

def on_message(msg, chat):                 # callback 签名固定为 (Message, Chat)
    print(f'收到来自 {chat} 的消息: {msg.content}')
    if isinstance(msg, FriendMessage):     # 过滤自己发的消息
        chat.SendMsg('收到')               # 子窗口发送，不切换主窗口

wx.AddListenChat('文件传输助手', on_message)
wx.KeepRunning()                           # 关键：保持进程存活
# 退出时：wx.RemoveListenChat(nickname="文件传输助手") / wx.StopListening()
```

**Message / WxResponse / WxParam 要点**：
- `Message` 固定属性：`type`（time/text/quote/voice/image/video/file/location/link/emotion/merge/personal_card/note/other）、`attr`（system/self/friend/other）、`id`（UIA 运行时 ID，**切 UI 就变**）、`✨hash`（切 UI 不变，可作去重键）、`sender`、`content`、`info`。
- 类型化子类可精确判断：`FriendMessage`/`SelfMessage` × `TextMessage`/`ImageMessage`/...；方法：`msg.reply(text)`、`msg.quote(text, at=None, timeout=3)`、`msg.forward(targets, message=None, timeout=3)`、`msg.download()`（图片/视频/文件）、`msg.ocr(timeout=3)`（图片取字）、`msg.to_text()`（语音转文字）、`msg.sender_info()`、`msg.click()`、`msg.select_option("复制")`。
- `WxResponse`：`if result:` 成功，`result['data']` / `result['message']`。
- `WxParam` 关键项：`LISTEN_INTERVAL=1`（监听间隔秒）、`SEARCH_CHAT_TIMEOUT`（免费版 2 / Plus 5）、`SEND_FILE_TIMEOUT=10`、`CHAT_WINDOW_SIZE=(800, 6000)`（4.x 靠拉高窗口保留消息锚点）、**`SEND_CONTENT_RATIO=0.9`（输入框内容相似度校验阈值）**、`GET_NEXT_MAX_QUANTITY=30`、`GET_NEXT_MAX_RUNTIME=10`、`CALLBACK_STOP_SIGN='stop'`、`MESSAGE_HASH`（✨，默认 False）、`SPECIAL_SESSION_NAME=['公众号','折叠的聊天','QQ邮箱提醒','服务号']`。

### 3.3 wcferry（WeChatFerry，Python 客户端 39.5.2.0，hook 路线对照）

```python
from wcferry import Wcf
wcf = Wcf(host=None, port=10086, debug=False, block=True)   # 本地模式自动加载 sdk.dll
wcf.send_text(msg, receiver, aters="")        # receiver 用 wxid/roomid（非昵称）；返回 int，0 成功
wcf.enable_receiving_msg(pyq=False) -> bool   # 之后用 get_msg()
msg = wcf.get_msg(block=True)                 # WxMsg，超时抛 Empty
wcf.send_image(path, receiver); wcf.send_file(path, receiver)
wcf.query_sql("MicroMsg.db", "SELECT ...")    # 直接查本地库（wxid 级精确）
wcf.get_contacts() / get_friends() / get_chatroom_members(roomid)
```
优点：wxid 精确寻址、无需 UI、延迟最低。缺点：DLL 注入 + 版本强绑定在 3.9，**不适用于 4.x**，风控与合规风险最高。

---

## 四、监听与发送实现要点

**发送链路（可靠做法）**
1. **定位会话**：优先「会话列表已有条目直接点击」→ 否则 `Ctrl+F` 打开搜索 → 输入名字 → **必须断言搜索结果里存在完全匹配项**（wxauto 3.9 源码用 `SessionBox.TextControl(Name=f"<em>{who}</em>")` 匹配高亮全等项），没有全等就**放弃发送而不是取第一个**。这是防发错人的第一道闸。
2. **发送前二次核验**：`ChatInfo()['chat_name'] == 目标` 才发（官方 demo 就是这么写的）；同时校验 `chat_type`（friend/group/service/official）避免把私聊内容发到同名群。
3. **写入输入框**：**中文必须走剪贴板**。`editbox.SendKeys('中文')` 在 UIA/SendInput 下丢字、乱序、被输入法拦截；正确做法是 `pyperclip.copy(text)` / `SetClipboardData(CF_UNICODETEXT)` → 点击输入框取得键盘焦点 → `SendKeys('{Ctrl}v')`。wxauto4 依赖 `pyperclip` 即是此意。
4. **写后校验**：`editbox.GetValuePattern().Value` 读回，或用 `SEND_CONTENT_RATIO=0.9` 相似度判定（表情/特殊符号会被转码，不能要求 100% 相等）。校验失败 → 清空重试，绝不在未校验时回车。
5. **发送快捷键**：默认 **Enter 发送**。微信可在「设置 → 聊天 → 发送消息快捷键」改为 `Ctrl+Enter`，插件应把它做成配置项并**在发送前检测当前设置**（否则 Enter 会变成换行，消息发不出去却以为成功）。
6. **发送成功确认**：UIA 路线没有服务端回执，可靠确认只有两招——(a) 发送后 `GetAllMessage()` 检查末尾是否出现 `attr == 'self'`、`content` 匹配的新消息；(b) 输入框已被清空（`clear=True` 行为）。hook 路线 `send_text` 返回 0 才是真回执。
7. **重试与去重**：以 `Message.id`/`✨hash` 作幂等键，落库记录「已回复的 msg id」；失败重试要**指数退避 + 上限 3 次 + 超时熔断**；发送前后加互斥锁，避免多个回调同时操作同一输入框（UIA 操作非线程安全）。

**监听链路（轮询 vs 回调）**
- **3.9 轮询式**：`AddListenChat(who)` 弹出独立 `ChatWnd` 子窗口（保证 UI 元素不因主窗口切换而丢失），再循环 `GetListenMessage()`；新消息通过对比 `usedmsgid` 集合识别。延迟由轮询间隔决定。
- **4.x 回调式**：`AddListenChat(nickname, callback)` 内部起守护线程，间隔约 `WxParam.LISTEN_INTERVAL=1s`。**回调内禁止发送/引用**（官方警告：会重置消息窗口导致后续消息取不到）——正确做法是回调里只把消息推进队列/事件总线，由独立工作线程做 LLM 生成，再通过保存的 `chat` 子窗口对象发送。
- **资源占用**：UIA 轮询是「遍历控件树 + 读 Name」，单次约几十毫秒；间隔 1s、监听 5–10 个会话时 CPU 占用通常可接受，但**监听越多越慢，且每个监听对象都是一个真实打开的窗口**。
- **未读识别**：3 条路径——(a) 会话列表项文本/子控件里的「N条新消息」（3.9 源码 `GetSessionAmont` 用正则 `\d+条新消息`）；(b) 导航栏图标红点像素（`CheckNewMessage` → `IsRedPixel(A_ChatIcon)`）；(c) 4.x 的 `GetNextNewMessage(filter_mute=True)` 可过滤免打扰会话。**注意**：4.x 下 `GetNextNewMessage` 执行后会自动关闭窗口，让该会话重新累加未读计数，这是"锚点丢失"的变通方案，不是 bug。
- **4.x 特有坑**：窗口太矮或消息瞬间刷屏 → 新消息 UI 一闪而过被卸载，监听直接失效。必须把 `CHAT_WINDOW_SIZE` 的纵向尺寸拉大（官方建议甚至用竖屏显示器）。

---

## 五、风控与合规

**平台风险（有实据）**
- 腾讯发布过[针对在微信使用"外挂"行为的打击公告](https://mp.weixin.qq.com/s/YfYKVyK09gOelvM840-qJQ)；微信官方公告明确禁止使用外挂/自动化工具（[光明网报道](https://m.gmw.cn/2024-03/28/content_1303698258.htm)、[湖南公安转载](https://gat.hunan.gov.cn/gat/ztzl2/qfqz/202404/t20240402_33268115.html)）。
- chatgpt-on-wechat 仓库 [issue #2568](https://github.com/zhayujie/CowAgent/issues/2568) 明确写「适配 WechatFerry 协议【暂时不要使用了，会引起风控】」。
- 2025 年「豆包手机」助手调用微信被下线（[证券时报](https://www.stcn.com/article/detail/3523293.html)）——平台对第三方自动化接管 App 能力的处置是即时且彻底的。
- **wxauto 官方免责声明**（[第三方服务说明](https://docs.wxauto.org/legal/third-party-services)）：非腾讯官方/授权软件；不保证不发生功能限制、账号限制；**不提供任何规避风控的能力**。5.x 常见问题页同款措辞。
- 关于「具体封号阈值/频率数字」：**未找到可靠资料**。wxauto 可接受使用政策反而明确要求「不根据所谓'安全阈值'或'封号阈值'设计自动化行为」。任何网传"每天 N 条安全"的说法都无权威来源，不应作为设计依据。

**频率建议（按合规姿势而非阈值反推）**
- 监听间隔 ≥ 1s；单账号自动发送建议 **≤ 每分钟 1–3 条、≤ 每小时 20 条**（保守值，非官方）；同一会话连续回复加 2–5s 随机抖动。
- 只对**白名单会话**生效；群聊仅在被 @ 或命中关键词时响应。
- 禁止行为：群发/营销、自动加好友、多账号群控、自动领红包/转账（wxauto 明确不支持金钱类操作）。
- 异常停止：连续 N 次发送失败、微信进程消失、窗口句柄失效 → 自动停机并告警。

**法律合规（中国大陆）**
- 《个人信息保护法》：读取他人聊天内容属处理个人信息，需**告知 + 取得同意**，遵守最小必要、目的限定、存储期限；不得未经授权导出、转交第三方。
- 《数据安全法》《网络安全法》：破坏他人计算机信息系统功能/干扰第三方服务正常运行（提供侵入、非法控制计算机信息系统程序工具）有刑事风险，hook/注入类方案风险显著高于 UIA 模拟操作。
- 《反不正当竞争法》与平台协议：违反微信服务协议本身会带来民事责任与账号处置。
- 若接入 LLM 云 API，聊天内容出境/传输给第三方属于「向第三方提供个人信息」，需单独告知并取得同意。
- **落地建议**：在插件内提供隐私说明与开关；默认不上传聊天原文（本地脱敏/仅传必要上下文）；提供日志脱敏与一键清除。

**「只生草稿不自动发送」安全模式设计建议**
1. **双模式**：`draft_only`（默认）/ `auto_send`（需显式开启 + 二次确认 + 每次会话级授权）。
2. **草稿落地**：把生成内容写入「文件传输助手」由用户复制，或直接写入输入框但**不按 Enter**，并在 UI 上高亮「待发送」。
3. **目标核验闸**：发送前断言 `ChatInfo()['chat_name']` 与目标严格相等 + 会话类型匹配 + `SEND_CONTENT_RATIO` 校验通过。
4. **人工确认通道**：草稿推送到本地 Web/托盘气泡，用户按快捷键或点击按钮才真正 `SendMsg`。
5. **可撤销**：发送后 N 秒内允许撤回（需要 Plus 或 hook 能力；开源版无撤回 API）。
6. **审计**：记录每一次「读到的消息 → 生成的草稿 → 是否采纳 → 是否发送」四元组，出问题时能自证是人工确认的。

---

## 六、可借鉴项目

| 项目 | 架构与借鉴点 | 状态 |
|---|---|---|
| **zhayujie/chatgpt-on-wechat → 已更名 [CowAgent](https://github.com/zhayujie/CowAgent)** | 分层解耦：**Channel（微信/企微/公众号/飞书/钉钉/QQ/Telegram/Slack）→ Agent Core（规划+工具+技能）→ Model（可切换供应商）→ 回发 Channel**。上下文管理为**三层记忆**：会话上下文（短期）→ 每日记忆（中期）→ `MEMORY.md`（长期），夜间 Deep Dream 蒸馏 + 关键词/向量混合检索。渠道无关的 `Channel` 抽象、会话 ID 隔离、多模型可插拔是最值得抄的三件事。 | 活跃（v2.2.0，2026-09-30） |
| **KouriChat/KouriChat（原 WeChatBot_WXAUTO_SE 上游）** | 曾是 wxauto 系最完整的 AI 微信机器人：`avatars/` 角色配置 + `src/handlers`（功能处理器）→ `src/services`（AI 接口）→ `src/webui`（可视化配置）→ `data`（持久化）。**按角色/会话隔离人设与记忆**、对话分段与表情包、持久记忆、WebUI 配置。借鉴点：角色卡 + 每会话独立记忆 + WebUI 热配置。 | `WeChat-wxauto` 分支基于 wxauto（Python 3.11，需小号+备用设备）；主线 1.5 已重构为 **OneBot 协议 + 独立客户端**，不再走 UIA |
| **iwyxdxl/WeChatBot_WXAUTO_SE** | KouriChat 的二次修改版，主打「LLM API 聚合对话工具」。 | **已停止维护（README 标注【停止维护】）**（[仓库](https://github.com/iwyxdxl/WeChatBot_WXAUTO_SE)；其 README 页面抓取失败，仅取到仓库简介） |
| **lich0821/WeChatRobot + [WeChatFerry](https://github.com/lich0821/WeChatFerry)** | hook 路线参考实现：`wcf.enable_receiving_msg()` + `get_msg()` 消费 `WxMsg`，`send_text/send_image/send_file` 回发；wxid/roomid 级寻址天然解决「发错人」。借鉴点：**用稳定 ID 而非昵称寻址**、消息队列解耦收发。 | 文档维护中（客户端 39.5.2.0，仅 3.9） |
| 其他 | `fastknifes/wxauto4-plus`：第三方「免授权封装」改写 wxautox4 授权，**属绕过商业授权，合规上不可用，仅作为生态风险提示**（[仓库](https://github.com/fastknifes/wxauto4-plus)）。 | — |

---

## 七、参考链接

- wxauto 开源仓库（3.9.X）：https://github.com/cluic/wxauto
- wxauto 3.9 源码（API 与发送实现）：https://raw.githubusercontent.com/cluic/wxauto/main/wxauto/wxauto.py
- wxauto 官方文档全文（含 4.x API）：https://docs.wxauto.org/llms-full.txt
- 安装与激活：https://docs.wxauto.org/docs/install
- 3.9 → 4.x 适配机制变化（关键）：https://docs.wxauto.org/blog/A9dYFme8
- 可接受使用政策 / 合理使用边界：https://docs.wxauto.org/legal/acceptable-use ・ https://docs.wxauto.org/blog/reasonable-use
- 第三方服务说明（免责）：https://docs.wxauto.org/legal/third-party-services
- PyPI wxauto4（免费）：https://pypi.org/project/wxauto4/
- PyPI wxautox4（Plus）：https://pypi.org/project/wxautox4/
- WeChatFerry 文档：https://wechatferry.readthedocs.io/zh/latest/readme_link.html
- wcferry Python 客户端源码：https://raw.githubusercontent.com/lich0821/WeChatFerry/master/clients/python/wcferry/client.py
- wxhelper（hook）：https://github.com/ttttupup/wxhelper
- ntchat 无头客户端封装：https://github.com/JustUndertaker/ntchat-client
- CowAgent（原 chatgpt-on-wechat）：https://github.com/zhayujie/CowAgent
- CowAgent issue #2568（WeChatFerry 风控警告）：https://github.com/zhayujie/CowAgent/issues/2568
- KouriChat（wxauto 分支）：https://github.com/KouriChat/KouriChat/tree/WeChat-wxauto
- WeChatBot_WXAUTO_SE（已停止维护）：https://github.com/iwyxdxl/WeChatBot_WXAUTO_SE
- 腾讯打击外挂公告：https://mp.weixin.qq.com/s/YfYKVyK09gOelvM840-qJQ
- 微信官方「禁止使用」公告报道：https://m.gmw.cn/2024-03/28/content_1303698258.htm
- 豆包手机登录微信被下线：https://www.stcn.com/article/detail/3523293.html
- 基于 UI Automation 的桌面版微信自动化应用（论文）：http://gzqx.ijournals.cn/ch/reader/create_pdf.aspx?file_no=20220416&year_id=2022&quarter_id=4&falg=1

**未找到可靠资料**：微信封号的具体触发频率阈值/数值化风控规则；WeChatBot_WXAUTO_SE 的 README 正文（GitHub 页面抓取失败，仅有仓库简介与「停止维护」标注）；wxauto 在微信 4.1.9+ 上的官方兼容承诺（文档仅说明免费版支持到 4.1.8.107）。
