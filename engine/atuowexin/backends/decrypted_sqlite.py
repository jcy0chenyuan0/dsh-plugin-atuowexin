# -*- coding: utf-8 -*-
"""已解密 SQLite 后端：直读**你已解密**的微信本地库（合规最优路径）。

本模块**不解密、不取密钥**——那是法律与风控风险最高的部分，交给外部工具
（chatlog / MemoTrace / PyWxDump 等）或用户自己完成。这里只做一件事：
把解密后的库翻译成归一化 :class:`Conversation` / :class:`Message`。

同时兼容两代结构：

- **微信 4.x**（``xwechat_files/<账号>/db_storage/``）：``contact.db`` + ``session.db``
  + ``message_0.db…``，其中**每个会话一张表** ``Msg_<md5(username)>``，字段是
  ``local_id / server_id / local_type / real_sender_id / create_time / message_content``，
  真实类型 = ``local_type & 0xFFFF``，发送者要经 ``real_sender_id → Name2Id.rowid → wxid``。
- **微信 3.x**（``WeChat Files/<wxid>/Msg/Multi/MSG*.db``）：单表 ``MSG``，字段
  ``StrTalker / StrContent / Type / SubType / IsSender / CreateTime / BytesExtra``。

群成员身份：3.x 从 ``BytesExtra`` 的 protobuf（``MessageBytesExtra.message2`` 中
``field1 == 1`` 的 ``field2``）取发送者 wxid；4.x 优先用 ``real_sender_id``，失败再退回
``packed_info_data``。两者都失败时该条消息标记为「未知发送者」，**不猜**。

已知边界（会在 doctor 里如实报告）：``message_content`` 常被 WCDB 用 zstd 压缩
（``WCDB_CT_message_content == 4``，魔数 ``28 B5 2F FD``）。本模块会**自动解压**——
前提是本机能 import 到 ``zstandard`` / ``pyzstd``（推荐装进交付物的可选依赖目录：
``pip install --target <repo>/.tools/python zstandard``）；装不上时该条只保留占位符，
其余功能不受影响。图片/语音等媒体本身仍是加密 ``.dat``，本后端只登记引用。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..model import Conversation, Message, classify, conversation_kind, normalize_ts, strip_markup, xml_fields
from .base import BaseBackend, BackendError, app_subtype_from_xml, media_refs_from_xml

#: 目录扫描的最大深度（``db_storage/message/message_0.db`` 是 2 层，留足余量）。
MAX_WALK_DEPTH = 5
#: ``md5:<hex>`` 形式的合成会话 id 前缀（表名能对上、但联系表里没有该 md5 时使用）。
UNKNOWN_PREFIX = "md5:"
#: 时间戳单位判定阈值。
_MS_THRESHOLD = 10**11
#: zstd 魔数（WCDB 压缩过的正文）。
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
#: zstd 可选依赖的探测结果缓存：``None`` = 还没探测，``False`` = 不可用，其他 = 模块对象。
_ZSTD_STATE: Any = None
#: 微信自己的文件/媒体 md5 长度。
_MD5_LEN = 32
#: 4.x 群消息正文前的冗余发送者前缀（实测与 ``real_sender_id`` 完全一致）。
_SENDER_PREFIX_RE = re.compile(r"^(wxid_[0-9A-Za-z]+|gh_[0-9A-Za-z]+|\d+@chatroom)\s*:\s*")
#: 4.x 账号目录名尾部的哈希，例如 ``wxid_ab12cd34ef56gh78_f4b1``。
_ACCOUNT_SUFFIX_RE = re.compile(r"_[0-9a-fA-F]{4,16}$")


def strip_account_suffix(name: str) -> str:
	"""剥掉 4.x 账号目录名尾部的哈希：``wxid_ab12cd34ef56gh78_f4b1`` → ``wxid_ab12cd34ef56gh78``。

	这一步是 ``is_self``（哪条是「我」发的）能不能对上的关键：目录名带哈希，
	而 ``Name2Id`` 里存的是不带哈希的真 wxid。
	"""
	value = (name or "").strip()
	if not value:
		return ""
	stripped = _ACCOUNT_SUFFIX_RE.sub("", value)
	return stripped if stripped.startswith(("wxid_", "gh_")) and len(stripped) > 6 else value


class DecryptedSqliteBackend(BaseBackend):
	"""读取已解密的微信 SQLite 库（3.x / 4.x 自适应）。"""

	name = "decrypted_sqlite"

	def __init__(self, config: dict[str, Any] | None = None):
		settings = dict(config or {})
		root = str(settings.get("root") or "").strip()
		if not root:
			raise BackendError("未配置 backend.decrypted_sqlite.root：请指向已解密的微信数据目录")
		self.root = Path(root).expanduser()
		if not self.root.exists():
			raise BackendError(f"已解密数据目录不存在：{self.root}")
		self._requested_schema = str(settings.get("schema") or "auto")
		# 账号自己的 wxid：显式配置优先（ingest 会自动写入），否则退化为目录名推断
		self._configured_self = str(settings.get("self_wxid") or "").strip()
		self._dbs = self._discover_dbs()
		self._schema = self._detect_schema()
		self._contacts: dict[str, dict[str, Any]] = {}
		self._sessions: dict[str, dict[str, Any]] = {}
		self._rooms: dict[str, dict[str, Any]] = {}
		self._table_locations: dict[str, Path] = {}
		# 元数据加载失败（最常见的是「库还是加密的」）不应该让构造直接炸：
		# 记下来交给 health()/list_conversations()，这样 doctor 能显示可执行的原因。
		self._load_error = ""
		try:
			self._load_metadata()
		except BackendError as error:
			self._load_error = str(error)

	# -- 探测 --------------------------------------------------------------- #

	def _discover_dbs(self) -> dict[str, list[Path]]:
		"""扫描数据目录，把 ``*.db`` 按用途归类。"""
		groups: dict[str, list[Path]] = {
			"contact": [],
			"session": [],
			"message": [],
			"media": [],
			"hardlink": [],
			"v3_msg": [],
			"v3_main": [],
		}
		base_depth = len(self.root.parts)
		for current, dirnames, filenames in os.walk(self.root):
			depth = len(Path(current).parts) - base_depth
			if depth >= MAX_WALK_DEPTH:
				dirnames[:] = []
			dirnames[:] = [name for name in dirnames if not name.startswith(".")]
			for filename in filenames:
				lower = filename.lower()
				if not lower.endswith(".db"):
					continue
				if lower.endswith(("-wal", "-shm", "-journal")):
					continue
				path = Path(current) / filename
				if lower == "contact.db":
					groups["contact"].append(path)
				elif lower == "session.db":
					groups["session"].append(path)
				elif lower.startswith("message"):
					groups["message"].append(path)
				elif lower.startswith("media") and lower.endswith((".db",)):
					groups["media"].append(path)
					groups["v3_main"].append(path)
				elif lower == "hardlink.db":
					groups["hardlink"].append(path)
				elif re.fullmatch(r"msg\d*\.db", lower):
					groups["v3_msg"].append(path)
				elif lower in {"micromsg.db", "misc.db", "emotion.db", "favorite.db"}:
					groups["v3_main"].append(path)
		return groups

	def _detect_schema(self) -> str:
		"""判定是 4.x 还是 3.x 结构。"""
		if self._requested_schema in {"v3", "v4"}:
			return self._requested_schema
		if self._dbs["session"] or self._dbs["message"]:
			return "v4"
		if self._dbs["v3_msg"]:
			return "v3"
		for candidate in self._dbs["v3_main"]:
			tables = self._tables(candidate)
			if "MSG" in tables or "ChatRoom" in tables or "Contact" in tables:
				return "v3"
		return "unknown"

	def _load_metadata(self) -> None:
		"""预载联系人 / 会话 / 群表，以及 ``Msg_*`` 表名到库文件的映射。"""
		if self._schema == "v4":
			for path in self._dbs["contact"]:
				self._load_contact_v4(path)
				self._load_rooms(path)
			for path in self._dbs["session"]:
				self._load_session_v4(path)
			for path in self._dbs["message"] + self._dbs["media"]:
				for table in self._tables(path):
					if table.lower().startswith("msg_"):
						self._table_locations.setdefault(table.lower(), path)
		elif self._schema == "v3":
			for path in self._dbs["v3_main"]:
				self._load_contact_v3(path)
				self._load_rooms(path)
			for path in self._dbs["v3_msg"]:
				self._table_locations.setdefault("msg", path)

	def _load_contact_v4(self, path: Path) -> None:
		"""读 4.x ``contact`` 表。"""
		with self._connect(path) as conn:
			if "contact" not in self._tables(path, conn):
				return
			for row in self._query(conn, "SELECT username, local_type, alias, remark, nick_name FROM contact"):
				username = str(row[0] or "")
				if not username:
					continue
				self._contacts[username] = {
					"local_type": int(row[1] or 0),
					"alias": str(row[2] or ""),
					"remark": str(row[3] or ""),
					"nick_name": str(row[4] or ""),
				}

	def _load_session_v4(self, path: Path) -> None:
		"""读 4.x ``SessionTable``。"""
		with self._connect(path) as conn:
			table = _find_table(self._tables(path, conn), "SessionTable")
			if table is None:
				return
			columns = self._columns(conn, table)
			username_col = _first_column(columns, ("username", "user_name", "strTalker"))
			if username_col is None:
				return
			time_col = _first_column(columns, ("sort_timestamp", "last_timestamp", "nTime"))
			summary_col = _first_column(columns, ("summary", "content", "digest"))
			select = ", ".join(item for item in (username_col, time_col, summary_col) if item)
			for row in self._query(conn, f'SELECT {select} FROM "{table}"'):
				username = str(row[0] or "")
				if not username:
					continue
				self._sessions[username] = {
					"last_time": normalize_ts(row[1]) if time_col and len(row) > 1 else 0,
					"summary": str(row[2] or "") if summary_col and len(row) > 2 else "",
				}

	def _load_contact_v3(self, path: Path) -> None:
		"""读 3.x ``Contact`` 表。"""
		with self._connect(path) as conn:
			tables = self._tables(path, conn)
			if "Contact" in tables:
				columns = self._columns(conn, "Contact")
				user_col = _first_column(columns, ("UserName", "username"))
				if user_col:
					nick_col = _first_column(columns, ("NickName", "nick_name"))
					remark_col = _first_column(columns, ("Remark", "remark"))
					alias_col = _first_column(columns, ("Alias", "alias"))
					select = ", ".join(item for item in (user_col, nick_col, remark_col, alias_col) if item)
					for row in self._query(conn, f'SELECT {select} FROM "Contact"'):
						username = str(row[0] or "")
						if not username:
							continue
						self._contacts[username] = {
							"nick_name": str(row[1] or "") if nick_col and len(row) > 1 else "",
							"remark": str(row[2] or "") if remark_col and len(row) > 2 else "",
							"alias": str(row[3] or "") if alias_col and len(row) > 3 else "",
						}

	def _load_rooms(self, path: Path) -> None:
		"""读群表：3.x 是 ``ChatRoom(ChatRoomName, RoomData)``，4.x 是 ``chat_room(username, ext_buffer)``。

		两个版本的 ``RoomData`` / ``ext_buffer`` 都是 protobuf，用同一个极小解析器处理。
		"""
		with self._connect(path) as conn:
			tables = self._tables(path, conn)
			for candidate in ("chat_room", "ChatRoom", "chatroom"):
				table = _find_table(tables, candidate)
				if table is None:
					continue
				columns = self._columns(conn, table)
				name_col = _first_column(columns, ("username", "ChatRoomName", "chatroomname"))
				data_col = _first_column(columns, ("ext_buffer", "RoomData", "roomdata"))
				if name_col is None or data_col is None:
					continue
				for row in self._query(conn, f'SELECT "{name_col}", "{data_col}" FROM "{table}"'):
					username = str(row[0] or "")
					if not username:
						continue
					blob = row[1] if isinstance(row[1], (bytes, bytearray)) else b""
					members = _pb_room_members(bytes(blob))
					existing = self._rooms.get(username) or {}
					if not members and existing:
						continue
					self._rooms[username] = {"members": members, "member_count": len(members)}

	# -- Backend ----------------------------------------------------------- #

	def health(self) -> dict[str, Any]:
		"""体检：目录、库文件、结构判定、能否真的查出数据。"""
		if self._load_error:
			return {"ok": False, "synthetic": False, "detail": self._load_error, "root": str(self.root), "schema": self._schema}
		detail_parts = [f"root={self.root}", f"schema={self._schema}"]
		db_count = sum(len(items) for items in self._dbs.values())
		detail_parts.append(f"db={db_count}")
		if self._schema == "unknown":
			return {
				"ok": False,
				"synthetic": False,
				"detail": "无法识别已解密数据结构：既没有 4.x 的 session.db/message_*.db，也没有 3.x 的 MSG*.db。"
				+ "请确认该目录是**解密后**的微信数据目录。",
				"root": str(self.root),
			}
		try:
			probe = self.list_conversations()
		except BackendError as error:
			return {"ok": False, "synthetic": False, "detail": str(error), "root": str(self.root)}
		detail_parts.append(f"conversations={len(probe)}")
		return {
			"ok": bool(probe),
			"synthetic": False,
			"detail": "；".join(detail_parts) if probe else "；".join(detail_parts) + "；未发现任何会话",
			"root": str(self.root),
			"schema": self._schema,
		}

	def list_conversations(self) -> list[Conversation]:
		"""会话列表：4.x 取 session ∪ 有消息表的账号；3.x 取 ``Session`` ∪ 出现过的 talker。"""
		if self._load_error:
			# 最常见的场景：这些 .db 还是 SQLCipher 加密的。把可执行的指引原样抛出去。
			raise BackendError(self._load_error)
		if self._schema == "v4":
			return self._list_v4()
		if self._schema == "v3":
			return self._list_v3()
		return []

	def iter_messages(self, conversation_id: str, *, since: int = 0, until: int = 0, limit: int = 0) -> Iterator[Message]:
		"""按会话迭代消息（4.x 走 ``Msg_<md5>`` 分表，3.x 走 ``MSG`` 单表）。"""
		if self._schema == "v4":
			yield from self._iter_v4(conversation_id, since=since, until=until, limit=limit)
		elif self._schema == "v3":
			yield from self._iter_v3(conversation_id, since=since, until=until, limit=limit)

	def describe(self) -> dict[str, Any]:
		"""自述信息（含结构判定结果）。"""
		return {
			"name": self.name,
			"synthetic": False,
			"root": str(self.root),
			"schema": self._schema,
			"contacts": len(self._contacts),
			"sessions": len(self._sessions),
			"groups": len(self._rooms),
			"self_wxid": self._self_username(),
		}

	# -- 4.x --------------------------------------------------------------- #

	def _list_v4(self) -> list[Conversation]:
		"""合并 session / contact / 消息表三处线索，得到 4.x 会话列表。"""
		usernames: dict[str, dict[str, Any]] = {}
		for username, session in self._sessions.items():
			usernames[username] = {"last_time": session.get("last_time", 0), "summary": session.get("summary", "")}
		for table_lower in self._table_locations:
			if not table_lower.startswith("msg_"):
				continue
			suffix = table_lower[4:]
			matched = [name for name in self._contacts if hashlib.md5(name.encode("utf-8")).hexdigest() == suffix]
			if matched:
				usernames.setdefault(matched[0], {})
			else:
				usernames.setdefault(f"{UNKNOWN_PREFIX}{suffix}", {"unresolved": True})
		conversations: list[Conversation] = []
		for username, extra in usernames.items():
			contact = self._contacts.get(username, {})
			kind = conversation_kind(username) if not extra.get("unresolved") else "friend"
			if contact.get("local_type") == 2:
				kind = "group"
			room = self._rooms.get(username, {})
			conversations.append(
				Conversation(
					id=username,
					name=str(contact.get("nick_name") or extra.get("summary") or ""),
					remark=str(contact.get("remark") or contact.get("alias") or ""),
					kind=kind,
					last_time=int(extra.get("last_time") or 0),
					member_count=int(room.get("member_count") or 0),
					extra={"session_summary": str(extra.get("summary") or "")[:200]} if extra.get("summary") else {},
				)
			)
		conversations.sort(key=lambda item: item.last_time, reverse=True)
		return conversations

	def _iter_v4(self, conversation_id: str, *, since: int, until: int, limit: int) -> Iterator[Message]:
		"""读一个 4.x 会话的消息表。"""
		suffix = conversation_id[len(UNKNOWN_PREFIX) :] if conversation_id.startswith(UNKNOWN_PREFIX) else None
		table_name = f"msg_{suffix}" if suffix else f"msg_{hashlib.md5(conversation_id.encode('utf-8')).hexdigest()}"
		path = self._table_locations.get(table_name.lower())
		if path is None:
			return
		with self._connect(path) as conn:
			real_table = _find_table(self._tables(path, conn), table_name)
			if real_table is None:
				return
			columns = self._columns(conn, real_table)
			mapping = {
				"local": _first_column(columns, ("local_id", "localId", "sort_seq", "localid")),
				"server": _first_column(columns, ("server_id", "serverId", "MsgSvrID")),
				"type": _first_column(columns, ("local_type", "localType", "Type")),
				"time": _first_column(columns, ("create_time", "createTime", "CreateTime")),
				"sender": _first_column(columns, ("real_sender_id", "realSenderId", "TalkerId")),
				"content": _first_column(columns, ("message_content", "messageContent", "StrContent")),
				"compress": _first_column(columns, ("compress_content", "compressContent", "CompressContent")),
				"extra": _first_column(columns, ("packed_info_data", "packedInfoData", "BytesExtra")),
			}
			if mapping["time"] is None:
				return
			scale = self._time_scale(conn, real_table, mapping["time"])
			select_cols = [value for value in mapping.values() if value]
			sql = f'SELECT {", ".join(chr(34) + col + chr(34) for col in select_cols)} FROM "{real_table}"'
			params: list[Any] = []
			if since:
				sql += f' WHERE "{mapping["time"]}" > ?'
				params.append(int(since * scale))
			sql += f' ORDER BY "{mapping["time"]}" ASC'
			if limit and limit > 0:
				sql += " LIMIT ?"
				params.append(int(limit) * (2 if scale == 1000 else 1))
			index = {column: position for position, column in enumerate(select_cols)}
			name_map = self._name2id(conn, path)
			produced = 0
			for row in self._query(conn, sql, params):
				value = lambda key: row[index[mapping[key]]] if mapping[key] and mapping[key] in index else None  # noqa: E731
				ts = normalize_ts(value("time"))
				if since and ts < since:
					continue
				if until and ts > until:
					continue
				raw_type = int(value("type") or 0)
				msg_type = raw_type & 0xFFFF
				raw_content = _as_text(value("content"))
				compress = _as_text(value("compress"))
				extra_blob = value("extra")
				if isinstance(extra_blob, str):
					extra_blob = extra_blob.encode("utf-8", "replace")
				sender_id = name_map.get(int(value("sender") or 0), "")
				if not _looks_like_wxid(sender_id):
					probed = _pb_sender(extra_blob if isinstance(extra_blob, (bytes, bytearray)) else b"")
					if probed:
						sender_id = probed
				text, kind, sub_type, media = _render_content(msg_type, 0, raw_content, compress, "")
				# 群消息正文常带 ``wxid_xxx: `` 前缀：与前缀一致就剔掉，不一致就拿它补发送者
				text, sender_id = _apply_sender_prefix(text, sender_id)
				sender_name = self._display_name(sender_id)
				if not sender_id and kind in {"pat", "revoke", "system"}:
					# 拍一拍/撤回/系统通知没有真人发送者，标成「系统」比「未知发送者」准确
					sender_name = "系统"
				message = Message(
					conversation_id=conversation_id,
					seq=int(value("local") or 0),
					server_id=int(value("server") or 0),
					sender_id=sender_id,
					sender_name=sender_name,
					is_self=bool(sender_id) and sender_id == self._self_username(),
					ts=ts,
					type=msg_type,
					sub_type=sub_type,
					kind=kind,
					text=text,
					media=media,
					raw={"schema": "v4", "raw_type": raw_type},
				)
				yield message
				produced += 1
				if limit and produced >= limit:
					return

	def _name2id(self, conn: sqlite3.Connection, path: Path) -> dict[int, str]:
		"""读 ``Name2Id``：``real_sender_id`` → wxid。"""
		table = _find_table_direct(conn, ("Name2Id", "name2id"))
		if table is None:
			return {}
		columns = self._columns(conn, table)
		user_col = _first_column(columns, ("user_name", "username", "UserName"))
		if user_col is None:
			return {}
		mapping: dict[int, str] = {}
		for row in self._query(conn, f'SELECT rowid, "{user_col}" FROM "{table}"'):
			mapping[int(row[0])] = str(row[1] or "")
		return mapping

	def _time_scale(self, conn: sqlite3.Connection, table: str, column: str) -> int:
		"""判断该表的时间列是秒还是毫秒（返回 1 或 1000 的比例因子）。"""
		row = self._query(conn, f'SELECT MAX("{column}") FROM "{table}"')
		if not row or row[0][0] is None:
			return 1
		try:
			return 1000 if float(row[0][0]) > _MS_THRESHOLD else 1
		except (TypeError, ValueError):
			return 1

	def _self_username(self) -> str:
		"""当前账号 wxid。

		这是 ``is_self``（哪条是「我」发的）的唯一依据，所以不能猜错：

		1. 配置里的 ``backend.decrypted_sqlite.self_wxid``（``ingest`` 会从数据目录名推导后写入）；
		2. 否则从目录名推断——微信 4.x 的账号目录形如 ``wxid_xxx_f4b1``，
		   尾部那截是目录哈希，**必须剥掉**才能和 ``Name2Id`` 里的真 wxid 对上；
		3. 再取不到就返回空串：此时 ``is_self`` 一律为假，宁可标成"对方"也不乱认。
		"""
		cached = getattr(self, "_self_cache", None)
		if cached is not None:
			return cached
		guess = self._configured_self
		if not guess:
			try:
				for child in self.root.iterdir():
					if child.is_dir() and (child / "db_storage").is_dir() and child.name.startswith("wxid_"):
						guess = child.name
						break
				if not guess:
					parent = self.root.parent
					if parent.name.startswith("wxid_"):
						guess = parent.name
			except OSError:
				guess = ""
		self._self_cache = strip_account_suffix(guess)
		return self._self_cache

	def _display_name(self, sender_id: str) -> str:
		"""sender wxid → 显示名（备注 > 昵称 > 群名片 > wxid）。"""
		if not sender_id:
			return "未知发送者"
		if sender_id in self._contacts:
			contact = self._contacts[sender_id]
			return str(contact.get("remark") or contact.get("nick_name") or sender_id)
		for room in self._rooms.values():
			members = room.get("members") or {}
			if isinstance(members, dict) and sender_id in members:
				return str(members[sender_id] or sender_id)
		return sender_id

	# -- 3.x --------------------------------------------------------------- #

	def _list_v3(self) -> list[Conversation]:
		"""3.x：``Session`` 表 + 实际出现过的 ``StrTalker``。"""
		usernames: dict[str, int] = {}
		for path in self._dbs["v3_main"] + self._dbs["v3_msg"]:
			with self._connect(path) as conn:
				tables = self._tables(path, conn)
				if "Session" in tables:
					columns = self._columns(conn, "Session")
					talker_col = _first_column(columns, ("StrTalker", "strTalker"))
					time_col = _first_column(columns, ("nTime", "ntime"))
					if talker_col:
						select = f'"{talker_col}"' + (f', "{time_col}"' if time_col else "")
						for row in self._query(conn, f'SELECT {select} FROM "Session"'):
							username = str(row[0] or "")
							if username:
								usernames[username] = max(usernames.get(username, 0), normalize_ts(row[1]) if time_col and len(row) > 1 else 0)
				if "MSG" in tables and self._schema == "v3":
					for row in self._query(conn, "SELECT StrTalker, MAX(CreateTime) FROM MSG GROUP BY StrTalker"):
						username = str(row[0] or "")
						if username:
							usernames[username] = max(usernames.get(username, 0), normalize_ts(row[1]))
		conversations = []
		for username, last_time in usernames.items():
			contact = self._contacts.get(username, {})
			kind = conversation_kind(username)
			conversations.append(
				Conversation(
					id=username,
					name=str(contact.get("nick_name") or ""),
					remark=str(contact.get("remark") or contact.get("alias") or ""),
					kind=kind,
					last_time=int(last_time or 0),
					member_count=int(self._rooms.get(username, {}).get("member_count") or 0),
				)
			)
		conversations.sort(key=lambda item: item.last_time, reverse=True)
		return conversations

	def _iter_v3(self, conversation_id: str, *, since: int, until: int, limit: int) -> Iterator[Message]:
		"""3.x：在 ``MSG*.db`` 里按 ``StrTalker`` 拉取。"""
		produced = 0
		for path in self._dbs["v3_msg"]:
			with self._connect(path) as conn:
				tables = self._tables(path, conn)
				table = _find_table(tables, "MSG")
				if table is None:
					continue
				columns = self._columns(conn, table)
				wanted = [
					"localId",
					"MsgSvrID",
					"Type",
					"SubType",
					"IsSender",
					"CreateTime",
					"Sequence",
					"StrTalker",
					"StrContent",
					"DisplayContent",
					"CompressContent",
					"BytesExtra",
				]
				present = [column for column in wanted if column in columns]
				if "StrTalker" not in present or "CreateTime" not in present:
					continue
				select = ", ".join(f'"{column}"' for column in present)
				sql = f'SELECT {select} FROM "{table}" WHERE StrTalker = ?'
				params: list[Any] = [conversation_id]
				if since:
					sql += " AND CreateTime > ?"
					params.append(int(since))
				sql += " ORDER BY CreateTime ASC"
				if limit and limit > 0:
					sql += " LIMIT ?"
					params.append(int(limit))
				index = {column: position for position, column in enumerate(present)}
				for row in self._query(conn, sql, params):
					value = lambda column: row[index[column]] if column in index else None  # noqa: E731
					ts = normalize_ts(value("CreateTime"))
					if since and ts < since:
						continue
					if until and ts > until:
						continue
					msg_type = int(value("Type") or 0)
					sub_type = int(value("SubType") or 0)
					is_sender = bool(value("IsSender"))
					extra_blob = value("BytesExtra")
					if isinstance(extra_blob, str):
						extra_blob = extra_blob.encode("utf-8", "replace")
					sender_id = ""
					if conversation_id.endswith("@chatroom") and not is_sender:
						sender_id = _pb_sender(extra_blob if isinstance(extra_blob, (bytes, bytearray)) else b"")
					if not sender_id:
						sender_id = self._self_username() if is_sender else conversation_id
					content = _as_text(value("StrContent"))
					compress = _as_text(value("CompressContent"))
					display = _as_text(value("DisplayContent"))
					text, kind, sub_type, media = _render_content(msg_type, sub_type, content, compress, display)
					yield Message(
						conversation_id=conversation_id,
						seq=int(value("localId") or value("Sequence") or 0),
						server_id=int(value("MsgSvrID") or 0),
						sender_id=sender_id,
						sender_name="我" if is_sender else self._display_name(sender_id),
						is_self=is_sender,
						ts=ts,
						type=msg_type,
						sub_type=sub_type,
						kind=kind,
						text=text,
						media=media,
						raw={"schema": "v3"},
					)
					produced += 1
					if limit and produced >= limit:
						return

	# -- SQLite 基础设施 ---------------------------------------------------- #

	def _connect(self, path: Path) -> "_RoConnection":
		"""以**只读**方式打开库，绝不改动用户数据。"""
		return _RoConnection(path)

	def _tables(self, path: Path, conn: sqlite3.Connection | None = None) -> set[str]:
		"""列出库里的表名。"""
		if conn is not None:
			return {str(row[0]) for row in self._query(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
		with self._connect(path) as opened:
			return {str(row[0]) for row in self._query(opened, "SELECT name FROM sqlite_master WHERE type='table'")}

	def _columns(self, conn: sqlite3.Connection, table: str) -> list[str]:
		"""列出表的列名。"""
		rows = self._query(conn, f'PRAGMA table_info("{table}")')
		return [str(row[1]) for row in rows]

	def _query(self, conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
		"""执行只读查询；把「文件不是数据库」翻译成「你还得先解密」。"""
		try:
			return conn.execute(sql, tuple(params)).fetchall()
		except sqlite3.DatabaseError as error:
			message = str(error)
			if "not a database" in message.lower() or "encrypted" in message.lower():
				raise BackendError(
					"该 .db 仍是加密的（SQLCipher），标准 sqlite3 打不开。"
					"请先用 chatlog / MemoTrace / PyWxDump 解密，或使用本插件的 decrypt 命令（需要你自备密钥）。"
					"注意：微信 ≥ 4.1.10 起密钥「零驻留」，纯内存扫描拿不到——需要用外部工具在旧版本提取，"
					"或在解锁瞬间用调试器抓取。"
				) from error
			raise BackendError(f"查询失败：{message}（SQL: {sql[:120]}）") from error


class _RoConnection:
	"""只读连接的上下文管理器（``file:...?mode=ro`` + ``immutable=0``）。"""

	def __init__(self, path: Path):
		self.path = path
		self.conn: sqlite3.Connection | None = None

	def __enter__(self) -> sqlite3.Connection:
		uri = f"file:{self.path.as_posix()}?mode=ro"
		try:
			self.conn = sqlite3.connect(uri, uri=True, timeout=10.0)
		except sqlite3.Error as error:
			raise BackendError(f"打不开数据库 {self.path}：{error}") from error
		return self.conn

	def __exit__(self, *_exc: object) -> None:
		if self.conn is not None:
			try:
				self.conn.close()
			except sqlite3.Error:
				pass


# --------------------------------------------------------------------------- #
# 列名 / 正文 / protobuf 工具
# --------------------------------------------------------------------------- #


def _first_column(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
	"""按候选名（大小写不敏感）挑第一个存在的列。"""
	lowered = {column.lower(): column for column in columns}
	for candidate in candidates:
		if candidate.lower() in lowered:
			return lowered[candidate.lower()]
	return None


def _find_table(tables: set[str], name: str) -> str | None:
	"""按大小写不敏感查找表名。"""
	lowered = name.lower()
	for table in tables:
		if table.lower() == lowered:
			return table
	return None


def _find_table_direct(conn: sqlite3.Connection, names: Sequence[str]) -> str | None:
	"""直接在 sqlite_master 里按候选名找表。"""
	wanted = {name.lower() for name in names}
	for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
		if str(row[0]).lower() in wanted:
			return str(row[0])
	return None


def _zstd_module():
	"""按需加载 zstd 实现。

	标准库没有 zstd，所以这里是**可选依赖**：

	1. 先试当前解释器里已装的 ``zstandard`` / ``pyzstd`` / ``zstd``；
	2. 再试交付物里的可选依赖目录 ``<repo>/.tools/python``（用
	   ``python -m atuowexin deps install`` 或
	   ``pip install --target <repo>/.tools/python zstandard`` 装进去），
	   或环境变量 ``ATUOWEXIN_PYTHONPATH`` 指定的目录。

	找不到就返回 ``False``：此时 zstd 正文只留占位符，**其余功能完全不受影响**。
	"""
	global _ZSTD_STATE
	if _ZSTD_STATE is not None:
		return _ZSTD_STATE
	import importlib

	from .. import deps

	deps.ensure_optional_path()
	for name in ("zstandard", "pyzstd", "zstd"):
		try:
			_ZSTD_STATE = importlib.import_module(name)
			return _ZSTD_STATE
		except Exception:
			continue
	_ZSTD_STATE = False
	return False


def _decompress_zstd(payload: bytes) -> bytes | None:
	"""解压一个 zstd 帧；失败或缺少依赖时返回 ``None``（绝不假装成功）。"""
	module = _zstd_module()
	if not module:
		return None
	try:
		if getattr(module, "__name__", "") == "zstandard":
			decompressor = module.ZstdDecompressor()
			try:
				return decompressor.decompress(payload)
			except Exception:
				# 帧里没有写入内容大小（流式帧）时走 decompressobj
				return decompressor.decompressobj().decompress(payload)
		return module.decompress(payload)
	except Exception:
		return None


def _as_text(value: Any) -> str:
	"""把 TEXT/BLOB 列统一成字符串，并顺手解掉 WCDB 的 zstd 压缩正文。"""
	if value is None:
		return ""
	if isinstance(value, (bytes, bytearray)):
		raw = bytes(value)
		if raw[:4] == _ZSTD_MAGIC:
			plain = _decompress_zstd(raw)
			if plain is not None:
				return plain.decode("utf-8", "replace")
			return "[zstd 压缩正文：缺少 zstandard 可选依赖，未能解压]"
		return raw.decode("utf-8", "replace")
	return str(value)


def _looks_like_wxid(value: str) -> bool:
	"""粗判一个字符串是不是 wxid/群 id。"""
	if not value:
		return False
	if value.endswith("@chatroom"):
		return True
	if value.startswith(("wxid_", "gh_", "mp_")):
		return True
	return len(value) > 12 and not value.isdigit()


def _apply_sender_prefix(text: str, sender_id: str) -> tuple[str, str]:
	"""处理 4.x 群消息正文里 ``wxid_xxx: `` 形式的前缀。

	实测（微信 4.1.7）该前缀与 ``real_sender_id`` 解出来的发送者完全一致，于是两种情况都有用：

	* 前缀 == 已解析的发送者 → 把冗余前缀从正文里去掉，正文更干净；
	* 发送者解析不出来（``Name2Id`` 里那行 ``user_name`` 为空）→ 用前缀把发送者补上。

	不一致时**两边都不动**：那说明正文里本来就有引用/转发内容，不能乱删。
	"""
	if not text:
		return text, sender_id
	match = _SENDER_PREFIX_RE.match(text)
	if match is None:
		return text, sender_id
	candidate = match.group(1)
	if candidate == sender_id:
		return text[match.end():].lstrip(), sender_id
	if not _looks_like_wxid(sender_id):
		return text[match.end():].lstrip(), candidate
	return text, sender_id


def _render_content(msg_type: int, sub_type: int, content: str, compress: str, display: str) -> tuple[str, str, int, list[dict[str, Any]]]:
	"""把原始正文渲染成 (text, kind, sub_type, media)。

	:param msg_type: 真实消息类型（4.x 已取过 ``& 0xFFFF``）。
	:param sub_type: 3.x 的 SubType；4.x 传 0，由 XML 推断。
	:param content: ``StrContent`` / ``message_content``。
	:param compress: ``CompressContent``（引用、链接、文件正文常在这里）。
	:param display: ``DisplayContent``（拍一拍等）。
	:return: 渲染结果四元组。
	"""
	raw_xml = content or ""
	effective_sub = sub_type
	if msg_type == 49 and not effective_sub:
		effective_sub = app_subtype_from_xml(raw_xml or compress)
	kind = classify(msg_type, effective_sub)
	media: list[dict[str, Any]] = []
	text = ""

	if msg_type == 1:
		text = strip_markup(raw_xml) if "<" in raw_xml[:32] else raw_xml.strip()
	elif msg_type == 3:
		media = media_refs_from_xml(raw_xml, "image")
		text = "[图片]"
	elif msg_type == 34:
		media = [{"kind": "voice"}]
		text = "[语音]"
	elif msg_type == 43:
		media = media_refs_from_xml(raw_xml, "video")
		text = "[视频]"
	elif msg_type == 47:
		media = media_refs_from_xml(raw_xml, "sticker")
		text = "[表情]"
	elif msg_type == 48:
		fields = xml_fields(raw_xml)
		text = fields.get("title") or "[位置]"
	elif msg_type == 49:
		source = compress or raw_xml
		fields = xml_fields(source if "<" in source else raw_xml)
		title = fields.get("title") or ""
		des = fields.get("des") or ""
		filename = fields.get("filename") or ""
		if "拍了拍" in title:
			# 拍一拍：正文就是那句 title；发送者列往往为空（rsid 指向 Name2Id 的空行），
			# 这是语义上的"系统事件"，不是某个人发的消息。
			return f"[拍一拍] {title}".strip(), "pat", effective_sub, media
		if kind == "file":
			media = media_refs_from_xml(source if "<" in source else raw_xml, "file")
			text = f"[文件] {filename or title}".strip()
		elif kind == "quote":
			quote_title = xml_fields(raw_xml).get("title") or ""
			text = f"[引用] {title or quote_title}".strip()
		elif kind == "merged_forward":
			text = f"[合并转发] {title}".strip()
		elif kind == "link":
			text = f"[链接] {title}".strip() + (f" —— {des}" if des else "")
		elif kind == "mini_program":
			text = f"[小程序] {title}".strip()
		elif kind == "transfer":
			text = f"[转账] {des or title}".strip()
		elif kind == "red_packet":
			text = f"[红包] {des or title}".strip()
		else:
			text = strip_markup(source if "<" in source else raw_xml)[:300] or f"[{classify(msg_type, effective_sub)}]"
	elif msg_type == 10000:
		flat = strip_markup(display or raw_xml) or "[系统消息]"
		if "revokemsg" in (raw_xml or "") or "撤回" in flat:
			kind = "revoke"
			text = f"[撤回] {flat}"[:200]
		else:
			text = flat
	elif msg_type == 10002:
		text = "[撤回了一条消息]"
	else:
		text = strip_markup(raw_xml or compress or display)[:300] or f"[{classify(msg_type, effective_sub)}]"

	if not text.strip():
		text = f"[{classify(msg_type, effective_sub)}]"
	return text, kind, effective_sub, media


# --------------------------------------------------------------------------- #
# 极简 protobuf 解析（只取字符串字段，不引入 protobuf 依赖）
# --------------------------------------------------------------------------- #


def _pb_varint(buffer: bytes, index: int) -> tuple[int | None, int]:
	"""读一个 varint。"""
	result = 0
	shift = 0
	while index < len(buffer):
		byte = buffer[index]
		index += 1
		result |= (byte & 0x7F) << shift
		if not byte & 0x80:
			return result, index
		shift += 7
		if shift > 63:
			break
	return None, index


def _pb_fields(buffer: bytes):
	"""逐字段迭代 protobuf：产出 ``(field_number, wire_type, value)``。"""
	index = 0
	length = len(buffer)
	while index < length:
		key, index = _pb_varint(buffer, index)
		if key is None:
			return
		field, wire = key >> 3, key & 7
		if wire == 0:
			value, index = _pb_varint(buffer, index)
			if value is None:
				return
			yield field, wire, value
		elif wire == 2:
			size, index = _pb_varint(buffer, index)
			if size is None or index + size > length:
				return
			yield field, wire, buffer[index : index + size]
			index += size
		elif wire == 5:
			if index + 4 > length:
				return
			yield field, wire, int.from_bytes(buffer[index : index + 4], "little")
			index += 4
		elif wire == 1:
			if index + 8 > length:
				return
			yield field, wire, int.from_bytes(buffer[index : index + 8], "little")
			index += 8
		else:  # 3/4 组类型（微信不用）
			return


def _pb_sender(blob: bytes) -> str:
	"""从 ``BytesExtra`` / ``packed_info_data`` 里取群消息发送者 wxid。

	结构（微信 3.x/4.x 通用）::

	    MessageBytesExtra { message2: [ { field1: int, field2: string } ] }

	其中 ``field1 == 1`` 的那条 ``field2`` 是发送者。取不到就返回空串——
	宁可标「未知发送者」，也不猜错人。
	"""
	if not blob:
		return ""
	best = ""
	for field, wire, value in _pb_fields(blob):
		if field != 1 or wire != 2 or not isinstance(value, (bytes, bytearray)):
			continue
		flag: int | None = None
		texts: list[str] = []
		for sub_field, sub_wire, sub_value in _pb_fields(bytes(value)):
			if sub_field == 1 and sub_wire == 0:
				flag = int(sub_value)  # type: ignore[arg-type]
			elif sub_field == 2 and sub_wire == 2 and isinstance(sub_value, (bytes, bytearray)):
				candidate = bytes(sub_value).decode("utf-8", "replace")
				if candidate:
					texts.append(candidate)
		if flag == 1 and texts:
			return texts[0]
		if not best and texts and _looks_like_wxid(texts[0]):
			best = texts[0]
	return best


def _pb_room_members(blob: bytes) -> dict[str, str]:
	"""从 ``RoomData`` / ``ext_buffer`` 里取群成员 ``{wxid: 群名片}``。"""
	members: dict[str, str] = {}
	if not blob:
		return members
	for field, wire, value in _pb_fields(blob):
		if field != 1 or wire != 2 or not isinstance(value, (bytes, bytearray)):
			continue
		wxid = ""
		display = ""
		for sub_field, sub_wire, sub_value in _pb_fields(bytes(value)):
			if sub_wire != 2 or not isinstance(sub_value, (bytes, bytearray)):
				continue
			candidate = bytes(sub_value).decode("utf-8", "replace")
			if sub_field == 1 and not wxid:
				wxid = candidate
			elif sub_field == 2 and not display:
				display = candidate
		if wxid:
			members[wxid] = display or wxid
	return members


def _now() -> int:
	"""当前秒级时间戳（供测试打桩）。"""
	return int(time.time())


__all__ = ["DecryptedSqliteBackend"]
