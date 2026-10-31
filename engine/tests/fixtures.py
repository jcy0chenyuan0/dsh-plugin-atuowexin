# -*- coding: utf-8 -*-
"""测试夹具：**合成**的微信解密库、合成 chatlog 服务、protobuf 构造器。

存在的意义：真实微信数据（尤其是加密库与密钥）不可能进 CI，也不该进仓库。
这里用标准库把两种真实结构（4.x 的 ``db_storage`` 分表 + 3.x 的 ``MSG`` 单表）
和 chatlog 的 HTTP 契约都造出来，于是整条链路可以离线、确定性地验证。

**所有数据都是编造的**：人名、wxid、消息内容均为虚构，不含任何真实聊天。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 合成数据素材（虚构）
# --------------------------------------------------------------------------- #

SELF_WXID = "wxid_demoacct"
FRIEND = ("wxid_linda2024", "林达", "林达（产品）")
GROUP = ("45678901234@chatroom", "产品研发群")
GROUP_MEMBERS = [
	("wxid_linda2024", "林达"),
	("wxid_zhouqi", "周琦"),
	("wxid_amy", "Amy"),
]

LINES = [
	"接口文档更新了，麻烦确认下字段。",
	"明天的评审会改到下午三点。",
	"这个 bug 我复现了，是并发写导致的。",
	"设计稿已经改好，主要调了间距。",
	"收到，我今天下班前给结论。",
	"客户希望这周能看到 demo。",
	"先按最小可用版本切。",
	"好，那就这么定了。",
]


# --------------------------------------------------------------------------- #
# 极简 protobuf 构造（与 backends.decrypted_sqlite 的解析器对应）
# --------------------------------------------------------------------------- #


def pb_varint(value: int) -> bytes:
	"""编码一个 varint。"""
	out = bytearray()
	while True:
		byte = value & 0x7F
		value >>= 7
		if value:
			out.append(byte | 0x80)
		else:
			out.append(byte)
			return bytes(out)


def pb_bytes(field: int, payload: bytes) -> bytes:
	"""编码一个 length-delimited 字段。"""
	return pb_varint((field << 3) | 2) + pb_varint(len(payload)) + payload


def pb_int(field: int, value: int) -> bytes:
	"""编码一个 varint 字段。"""
	return pb_varint((field << 3) | 0) + pb_varint(value)


def bytes_extra(sender_wxid: str) -> bytes:
	"""构造 ``MessageBytesExtra``：``message2 = {field1: 1, field2: sender}``。"""
	inner = pb_int(1, 1) + pb_bytes(2, sender_wxid.encode("utf-8"))
	return pb_bytes(1, inner)


def room_data(members: list[tuple[str, str]]) -> bytes:
	"""构造群成员 ``RoomData``：每个成员一个子消息（field1=wxid，field2=群名片）。"""
	payload = b""
	for wxid, display in members:
		payload += pb_bytes(1, pb_bytes(1, wxid.encode("utf-8")) + pb_bytes(2, display.encode("utf-8")))
	return payload


def _md5(text: str) -> str:
	"""微信 4.x 用 ``md5(username)`` 当分表后缀。"""
	return hashlib.md5(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 微信 4.x：xwechat_files/<账号>/db_storage/**
# --------------------------------------------------------------------------- #


def v4_message_rows() -> dict[str, list[dict]]:
	"""构造 4.x 的合成消息：私聊 + 群聊，覆盖文本/图片/引用/系统/撤退。"""
	base = 1_760_000_000  # 2025-10 附近的固定时间，保证测试可重复
	messages: dict[str, list[dict]] = {}

	friend_id = FRIEND[0]
	rows = []
	for index in range(6):
		is_self = index % 3 == 0
		sender = SELF_WXID if is_self else friend_id
		rows.append(
			{
				"local_id": index + 1,
				"server_id": 1000 + index,
				"local_type": 1,
				"real_sender_id": 1 if is_self else 2,
				"sender_hint": sender,
				"create_time": base + index * 3600,
				"message_content": f"{LINES[index % len(LINES)]} [#F{index}]",
				"compress_content": "",
				"packed": b"",
			}
		)
	# 图片消息（XML 里带 md5）
	rows.append(
		{
			"local_id": 7,
			"server_id": 1007,
			"local_type": 3,
			"real_sender_id": 2,
			"sender_hint": friend_id,
			"create_time": base + 7 * 3600,
			"message_content": '<msg><img aeskey="abc" md5="0123456789abcdef0123456789abcdef" /></msg>',
			"compress_content": "",
			"packed": b"",
		}
	)
	# 引用消息（49/57，正文在 compress_content）
	rows.append(
		{
			"local_id": 8,
			"server_id": 1008,
			"local_type": 49,
			"real_sender_id": 2,
			"sender_hint": friend_id,
			"create_time": base + 8 * 3600,
			"message_content": "",
			"compress_content": '<appmsg><title>引用了一条消息</title><type>57</type></appmsg>',
			"packed": b"",
		}
	)
	messages[friend_id] = rows

	group_id = GROUP[0]
	group_rows = []
	for index in range(6):
		wxid, name = GROUP_MEMBERS[index % len(GROUP_MEMBERS)]
		if index == 5:
			# 这一条的 real_sender_id 指向不存在的 Name2Id 行，用于验证 protobuf 回退路径
			group_rows.append(
				{
					"local_id": index + 1,
					"server_id": 2000 + index,
					"local_type": 1,
					"real_sender_id": 999,
					"sender_hint": "wxid_fallback_sender",
					"create_time": base + index * 1800,
					"message_content": f"{LINES[(index + 2) % len(LINES)]} [#GX{index}]",
					"compress_content": "",
					"packed": bytes_extra("wxid_fallback_sender"),
				}
			)
		else:
			group_rows.append(
				{
					"local_id": index + 1,
					"server_id": 2000 + index,
					"local_type": 1,
					"real_sender_id": 2 + (index % len(GROUP_MEMBERS)),
					"sender_hint": wxid,
					"create_time": base + index * 1800,
					"message_content": f"{LINES[index % len(LINES)]} [#G{index}]",
					"compress_content": "",
					"packed": bytes_extra(wxid),
				}
			)
	# 系统消息
	group_rows.append(
		{
			"local_id": 7,
			"server_id": 2007,
			"local_type": 10000,
			"real_sender_id": 2,
			"sender_hint": "",
			"create_time": base + 7 * 1800,
			"message_content": "",
			"compress_content": "",
			"packed": b"",
		}
	)
	messages[group_id] = group_rows
	return messages


def build_v4_fixture(root: str | Path) -> dict:
	"""造一个 4.x 形态的**已解密**数据目录。

	:param root: 目标目录（会被创建）。
	:return: ``{"root": Path, "db_storage": Path, "conversations": {id: 消息数}}``。
	"""
	root = Path(root)
	storage = root / SELF_WXID / "db_storage"
	for name in ("contact", "session", "message", "media"):
		(storage / name).mkdir(parents=True, exist_ok=True)
	rows = v4_message_rows()

	# contact.db
	contact_db = storage / "contact" / "contact.db"
	conn = sqlite3.connect(str(contact_db))
	conn.execute("CREATE TABLE contact (username TEXT, local_type INTEGER, alias TEXT, remark TEXT, nick_name TEXT)")
	conn.execute("CREATE TABLE chat_room (username TEXT, owner TEXT, ext_buffer BLOB)")
	conn.executemany(
		"INSERT INTO contact VALUES (?, ?, ?, ?, ?)",
		[
			(FRIEND[0], 1, "linda", FRIEND[2], FRIEND[1]),
			(GROUP[0], 2, "", "", GROUP[1]),
			(SELF_WXID, 1, "", "", "我"),
			("wxid_fallback_sender", 1, "", "", "回退发送者"),
		],
	)
	conn.execute("INSERT INTO chat_room VALUES (?, ?, ?)", (GROUP[0], FRIEND[0], room_data(GROUP_MEMBERS)))
	conn.commit()
	conn.close()

	# session.db
	session_db = storage / "session" / "session.db"
	conn = sqlite3.connect(str(session_db))
	conn.execute("CREATE TABLE SessionTable (username TEXT, summary TEXT, last_timestamp INTEGER, sort_timestamp INTEGER)")
	conn.executemany(
		"INSERT INTO SessionTable VALUES (?, ?, ?, ?)",
		[
			(GROUP[0], "[3 条] 最新消息", max(row["create_time"] for row in rows[GROUP[0]]), max(row["create_time"] for row in rows[GROUP[0]])),
			(FRIEND[0], "接口文档更新了", max(row["create_time"] for row in rows[FRIEND[0]]), max(row["create_time"] for row in rows[FRIEND[0]])),
		],
	)
	conn.commit()
	conn.close()

	# message_0.db：Name2Id + 每会话一张 Msg_<md5>
	message_db = storage / "message" / "message_0.db"
	conn = sqlite3.connect(str(message_db))
	conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
	conn.executemany("INSERT INTO Name2Id (user_name) VALUES (?)", [(SELF_WXID,)] + [(wxid,) for wxid, _ in GROUP_MEMBERS] + [(FRIEND[0],)])
	for conversation_id, items in rows.items():
		table = f'"Msg_{_md5(conversation_id)}"'
		conn.execute(
			f"CREATE TABLE {table} (local_id INTEGER, server_id INTEGER, local_type INTEGER, "
			"real_sender_id INTEGER, create_time INTEGER, message_content TEXT, compress_content TEXT, packed_info_data BLOB)"
		)
		conn.executemany(
			f"INSERT INTO {table} (local_id, server_id, local_type, real_sender_id, create_time, message_content, compress_content, packed_info_data) "
			"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
			[
				(
					row["local_id"],
					row["server_id"],
					row["local_type"],
					row["real_sender_id"],
					row["create_time"],
					row["message_content"],
					row["compress_content"],
					row["packed"],
				)
				for row in items
			],
		)
	conn.commit()
	conn.close()

	# media_0.db：媒体引用表（本后端只登记，不下载）
	media_db = storage / "media" / "media_0.db"
	conn = sqlite3.connect(str(media_db))
	conn.execute("CREATE TABLE image_hardlink_info_v4 (md5 TEXT, file_name TEXT, file_size INTEGER)")
	conn.execute("INSERT INTO image_hardlink_info_v4 VALUES (?, ?, ?)", ("0123456789abcdef0123456789abcdef", "demo.jpg", 1024))
	conn.commit()
	conn.close()

	return {
		"root": root,
		"db_storage": storage,
		"conversations": {key: len(value) for key, value in rows.items()},
		"self_wxid": SELF_WXID,
	}


# --------------------------------------------------------------------------- #
# 微信 3.x：WeChat Files/<wxid>/Msg/Multi/MSG*.db + MicroMsg.db
# --------------------------------------------------------------------------- #


def build_v3_fixture(root: str | Path) -> dict:
	"""造一个 3.x 形态的**已解密**数据目录。"""
	root = Path(root)
	multi = root / "wxid_oldacct" / "Msg" / "Multi"
	multi.mkdir(parents=True, exist_ok=True)
	base = 1_700_000_000
	friend_id = FRIEND[0]
	group_id = GROUP[0]

	msg_db = multi / "MSG0.db"
	conn = sqlite3.connect(str(msg_db))
	conn.execute(
		"CREATE TABLE MSG (localId INTEGER PRIMARY KEY, MsgSvrID INTEGER, Type INTEGER, SubType INTEGER, "
		"IsSender INTEGER, CreateTime INTEGER, Sequence INTEGER, StrTalker TEXT, StrContent TEXT, "
		"DisplayContent TEXT, CompressContent TEXT, BytesExtra BLOB)"
	)
	insert = "INSERT INTO MSG (localId, MsgSvrID, Type, SubType, IsSender, CreateTime, Sequence, StrTalker, StrContent, DisplayContent, CompressContent, BytesExtra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
	for index in range(4):
		conn.execute(
			insert,
			(index + 1, 500 + index, 1, 0, index % 2, base + index * 600, index, friend_id, LINES[index], "", "", None),
		)
	for index in range(4):
		wxid, name = GROUP_MEMBERS[index % len(GROUP_MEMBERS)]
		conn.execute(
			insert,
			(
				index + 10,
				600 + index,
				1,
				0,
				0,
				base + index * 300,
				index,
				group_id,
				LINES[(index + 1) % len(LINES)],
				"",
				"",
				bytes_extra(wxid),
			),
		)
	# 3.x 的图片消息（XML 带 md5）
	conn.execute(insert, (20, 700, 3, 0, 1, base + 5000, 9, friend_id, '<msg><img md5="ffffffffffffffffffffffffffffffff"/></msg>', "", "", None))
	conn.execute(insert, (21, 701, 10000, 4, 0, base + 5100, 10, group_id, "", "周琦 拍了拍 我", "", None))
	conn.commit()
	conn.close()

	main_db = root / "wxid_oldacct" / "MicroMsg.db"
	conn = sqlite3.connect(str(main_db))
	conn.execute("CREATE TABLE Contact (UserName TEXT, NickName TEXT, Remark TEXT, Alias TEXT)")
	conn.executemany(
		"INSERT INTO Contact VALUES (?,?,?,?)",
		[(friend_id, FRIEND[1], FRIEND[2], "linda"), (group_id, GROUP[1], "", ""), (SELF_WXID, "我", "", "")],
	)
	conn.execute("CREATE TABLE ChatRoom (ChatRoomName TEXT, RoomData BLOB)")
	conn.execute("INSERT INTO ChatRoom VALUES (?, ?)", (group_id, room_data(GROUP_MEMBERS)))
	conn.execute("CREATE TABLE Session (StrTalker TEXT, nTime INTEGER)")
	conn.executemany("INSERT INTO Session VALUES (?,?)", [(friend_id, base + 5000), (group_id, base + 5100)])
	conn.commit()
	conn.close()

	return {"root": root, "conversations": {friend_id: 5, group_id: 5}, "self_wxid": SELF_WXID}


# --------------------------------------------------------------------------- #
# 合成 chatlog HTTP 服务
# --------------------------------------------------------------------------- #


def chatlog_payload() -> dict:
	"""构造与 chatlog ``/api/v1/*`` 同形的数据。"""
	base = 1_760_000_000
	sessions = [
		{"userName": FRIEND[0], "nickName": FRIEND[1], "content": "接口文档更新了", "nTime": base + 7200},
		{"userName": GROUP[0], "nickName": GROUP[1], "content": "[3 条] 最新消息", "nTime": base + 9000},
	]
	contacts = [
		{"userName": FRIEND[0], "alias": "linda", "remark": FRIEND[2], "nickName": FRIEND[1]},
		{"userName": GROUP[0], "alias": "", "remark": "", "nickName": GROUP[1]},
		{"userName": SELF_WXID, "alias": "", "remark": "", "nickName": "我"},
	]
	rooms = [{"userName": GROUP[0], "nickName": GROUP[1], "users": [wxid for wxid, _ in GROUP_MEMBERS]}]

	def message(index: int, talker: str, sender: str, sender_name: str, is_self: bool, content: str, msg_type: int = 1, sub_type: int = 0, contents=None) -> dict:
		return {
			"seq": index,
			"time": _iso(base + index * 600),
			"talker": talker,
			"talkerName": FRIEND[1] if talker == FRIEND[0] else GROUP[1],
			"isChatRoom": talker.endswith("@chatroom"),
			"sender": sender,
			"senderName": sender_name,
			"isSelf": is_self,
			"type": msg_type,
			"subType": sub_type,
			"content": content,
			"contents": contents or {},
		}

	messages: dict[str, list[dict]] = {}
	friend_messages = []
	for index in range(5):
		friend_messages.append(
			message(
				index + 1,
				FRIEND[0],
				SELF_WXID if index % 2 == 0 else FRIEND[0],
				"我" if index % 2 == 0 else FRIEND[1],
				index % 2 == 0,
				LINES[index],
			)
		)
	friend_messages.append(
		message(6, FRIEND[0], FRIEND[0], FRIEND[1], False, "", 3, 0, {"md5": "0123456789abcdef0123456789abcdef"})
	)
	messages[FRIEND[0]] = friend_messages

	group_messages = []
	for index in range(5):
		wxid, name = GROUP_MEMBERS[index % len(GROUP_MEMBERS)]
		group_messages.append(message(index + 1, GROUP[0], wxid, name, False, LINES[(index + 1) % len(LINES)]))
	group_messages.append(message(6, GROUP[0], GROUP[0], GROUP[1], False, "周琦 拍了拍 我", 10000, 4))
	messages[GROUP[0]] = group_messages
	return {"sessions": sessions, "contacts": contacts, "rooms": rooms, "messages": messages, "base": base}


def _iso(ts: int) -> str:
	"""秒级时间戳 → 带 +08:00 偏移的 RFC3339（chatlog 的形状）。"""
	import datetime as _dt

	moment = _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=8)))
	return moment.strftime("%Y-%m-%dT%H:%M:%S+08:00")


class MockChatlogServer:
	"""最小可用的 chatlog 替身：实现插件真正调用的那几个路由。

	:param ready: ``False`` 时所有 ``/api/v1/*`` 返回 503（模拟「数据库还在解密」）。
	"""

	def __init__(self, payload: dict | None = None, *, ready: bool = True):
		self.payload = payload or chatlog_payload()
		self.ready = ready
		self.requests: list[str] = []
		self._server: ThreadingHTTPServer | None = None
		self._thread: threading.Thread | None = None

	@property
	def base_url(self) -> str:
		"""启动后的基地址；未启动时为占位值。"""
		if self._server is None:
			return "http://127.0.0.1:0"
		host, port = self._server.server_address[:2]
		return f"http://{host}:{port}"

	def start(self) -> str:
		"""在后台线程启动服务，返回基地址。"""
		outer = self

		class Handler(BaseHTTPRequestHandler):
			protocol_version = "HTTP/1.1"

			def log_message(self, *_args: object) -> None:  # 静音
				return

			def _send(self, status: int, payload: dict | list | None) -> None:
				body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
				self.send_response(status)
				self.send_header("Content-Type", "application/json; charset=utf-8")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				if body:
					self.wfile.write(body)

			def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
				parsed = urlparse(self.path)
				outer.requests.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
				query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
				if parsed.path == "/health":
					self._send(200, {"status": "ok"})
					return
				if not parsed.path.startswith("/api/v1/"):
					self._send(404, {"error": "not found"})
					return
				if not outer.ready:
					self._send(503, {"error": "database is decrypting, please wait"})
					return
				if parsed.path == "/api/v1/session":
					self._send(200, {"items": outer.payload["sessions"]})
					return
				if parsed.path == "/api/v1/contact":
					self._send(200, {"items": outer.payload["contacts"]})
					return
				if parsed.path == "/api/v1/chatroom":
					self._send(200, {"items": outer.payload["rooms"]})
					return
				if parsed.path == "/api/v1/chatlog":
					talker = query.get("talker", "")
					limit = int(query.get("limit", "500"))
					offset = int(query.get("offset", "0"))
					items = outer.payload["messages"].get(talker, [])
					page = items[offset : offset + limit]
					self._send(200, page)
					return
				self._send(404, {"error": "not found"})

		self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
		self._thread.start()
		return self.base_url

	def stop(self) -> None:
		"""停止服务。"""
		if self._server is not None:
			self._server.shutdown()
			self._server.server_close()
			self._server = None
		if self._thread is not None:
			self._thread.join(timeout=5)
			self._thread = None

	def __enter__(self) -> "MockChatlogServer":
		self.start()
		return self

	def __exit__(self, *_exc: object) -> None:
		self.stop()


__all__ = [
	"SELF_WXID",
	"FRIEND",
	"GROUP",
	"GROUP_MEMBERS",
	"build_v4_fixture",
	"build_v3_fixture",
	"chatlog_payload",
	"MockChatlogServer",
	"bytes_extra",
	"room_data",
	"pb_bytes",
	"pb_int",
	"pb_varint",
]
