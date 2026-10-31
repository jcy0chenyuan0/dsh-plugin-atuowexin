# -*- coding: utf-8 -*-
"""归一化存储：所有摘录结果、总结、草稿、审计都落在这一个 SQLite 里。

用 SQLite 而不是一堆 JSON 文件，是因为「完整摘录 + 增量同步 + 按会话查询」这三件事
本质上就是数据库问题：``messages`` 的 ``uid`` 主键天然去重，``(conversation_id, ts)``
索引让「只取本会话最近 N 条」这一隔离操作变成一次索引扫描。

**隔离的落点就在这张表**：每条消息都带 ``conversation_id``，且所有查询方法都强制要求
``conversation_id`` 参数（没有「查全部消息」的入口），从 API 形状上杜绝跨会话串味。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import paths
from .model import MESSAGE_COLUMNS, Conversation, Message

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    remark       TEXT,
    kind         TEXT,
    is_group     INTEGER DEFAULT 0,
    member_count INTEGER DEFAULT 0,
    msg_count    INTEGER DEFAULT 0,
    first_time   INTEGER DEFAULT 0,
    last_time    INTEGER DEFAULT 0,
    backend      TEXT,
    extra        TEXT,
    updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    uid             TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq             INTEGER DEFAULT 0,
    server_id       INTEGER DEFAULT 0,
    sender_id       TEXT,
    sender_name     TEXT,
    is_self         INTEGER DEFAULT 0,
    ts              INTEGER DEFAULT 0,
    type            INTEGER DEFAULT 0,
    sub_type        INTEGER DEFAULT 0,
    kind            TEXT,
    text            TEXT,
    media           TEXT,
    raw             TEXT,
    inserted_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_ts   ON messages (conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_conv_kind ON messages (conversation_id, kind);
CREATE INDEX IF NOT EXISTS idx_messages_server_id ON messages (conversation_id, server_id);

CREATE TABLE IF NOT EXISTS media (
    key             TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_uid     TEXT,
    kind            TEXT,
    md5             TEXT,
    path            TEXT,
    url             TEXT,
    size            INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'referenced',
    extra           TEXT,
    created_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_conv ON media (conversation_id, kind);

CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    from_ts         INTEGER DEFAULT 0,
    to_ts           INTEGER DEFAULT 0,
    msg_count       INTEGER DEFAULT 0,
    model           TEXT,
    style           TEXT,
    content         TEXT,
    meta            TEXT,
    created_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_summaries_conv ON summaries (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at      INTEGER,
    model           TEXT,
    instruction     TEXT,
    text            TEXT,
    rationale       TEXT,
    tone            TEXT,
    meta            TEXT,
    status          TEXT DEFAULT 'draft'
);
CREATE INDEX IF NOT EXISTS idx_drafts_conv ON drafts (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER,
    event           TEXT,
    conversation_id TEXT,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER
);
"""

#: 查询消息时固定的列顺序，必须与 :meth:`atuowexin.model.Message.from_row` 一致。
_MSG_SELECT = "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages"


class Store:
	"""归一化数据库的读写门面。"""

	def __init__(self, path: str | Path):
		self.path = Path(path)
		self.path.parent.mkdir(parents=True, exist_ok=True)
		self._conn = sqlite3.connect(str(self.path))
		self._conn.row_factory = None
		self._conn.executescript(_SCHEMA)
		self._conn.commit()

	# -- 基础设施 ----------------------------------------------------------- #

	@contextmanager
	def transaction(self) -> Iterator[sqlite3.Connection]:
		"""事务上下文：异常回滚。"""
		try:
			yield self._conn
			self._conn.commit()
		except Exception:
			self._conn.rollback()
			raise

	def close(self) -> None:
		"""关闭连接（幂等）。"""
		try:
			self._conn.close()
		except sqlite3.Error:
			pass

	def __enter__(self) -> "Store":
		return self

	def __exit__(self, *_exc: object) -> None:
		self.close()

	# -- 会话 --------------------------------------------------------------- #

	def upsert_conversation(self, conversation: Conversation, *, backend: str = "") -> None:
		"""写入或更新一条会话（不覆盖已有的统计字段）。"""
		extra = json.dumps(conversation.extra or {}, ensure_ascii=False)
		self._conn.execute(
			"""
            INSERT INTO conversations (id, name, remark, kind, is_group, member_count,
                                       msg_count, last_time, backend, extra, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name         = excluded.name,
                remark       = excluded.remark,
                kind         = excluded.kind,
                is_group     = excluded.is_group,
                member_count = MAX(conversations.member_count, excluded.member_count),
                last_time    = MAX(conversations.last_time, excluded.last_time),
                backend      = excluded.backend,
                extra        = excluded.extra,
                updated_at   = excluded.updated_at
            """,
			(
				conversation.id,
				conversation.name,
				conversation.remark,
				conversation.kind,
				1 if conversation.is_group else 0,
				conversation.member_count,
				conversation.msg_count,
				conversation.last_time,
				backend,
				extra,
				int(time.time()),
			),
		)
		self._conn.commit()

	def upsert_conversations(self, conversations: Iterable[Conversation], *, backend: str = "") -> int:
		"""批量写入会话，返回处理条数。"""
		count = 0
		with self.transaction():
			for conversation in conversations:
				self.upsert_conversation(conversation, backend=backend)
				count += 1
		return count

	def get_conversation(self, conversation_id: str) -> Conversation | None:
		"""按 username 取会话。"""
		row = self._conn.execute(
			"SELECT id, name, remark, kind, member_count, msg_count, last_time, extra FROM conversations WHERE id = ?",
			(conversation_id,),
		).fetchone()
		if row is None:
			return None
		return Conversation(
			id=row[0],
			name=row[1] or "",
			remark=row[2] or "",
			kind=row[3] or "friend",
			member_count=row[4] or 0,
			msg_count=row[5] or 0,
			last_time=row[6] or 0,
			extra=_load_json(row[7], {}),
		)

	def list_conversations(
		self,
		*,
		query: str = "",
		kind: str = "",
		limit: int = 0,
		order: str = "last_time",
	) -> list[Conversation]:
		"""列出已知会话，可按名字/备注模糊过滤。

		:param query: 关键词（匹配 name/remark/id）。
		:param kind: 只看某类会话（friend/group/official/service）。
		:param limit: 0 表示不限。
		:param order: ``last_time`` / ``msg_count`` / ``name``。
		"""
		order_sql = {
			"last_time": "last_time DESC",
			"msg_count": "msg_count DESC",
			"name": "name ASC",
		}.get(order, "last_time DESC")
		sql = "SELECT id, name, remark, kind, member_count, msg_count, last_time, extra FROM conversations"
		clauses: list[str] = []
		params: list[Any] = []
		if query:
			clauses.append("(name LIKE ? OR remark LIKE ? OR id LIKE ?)")
			like = f"%{query}%"
			params.extend([like, like, like])
		if kind:
			clauses.append("kind = ?")
			params.append(kind)
		if clauses:
			sql += " WHERE " + " AND ".join(clauses)
		sql += f" ORDER BY {order_sql}"
		if limit and limit > 0:
			sql += " LIMIT ?"
			params.append(int(limit))
		rows = self._conn.execute(sql, params).fetchall()
		return [
			Conversation(
				id=row[0],
				name=row[1] or "",
				remark=row[2] or "",
				kind=row[3] or "friend",
				member_count=row[4] or 0,
				msg_count=row[5] or 0,
				last_time=row[6] or 0,
				extra=_load_json(row[7], {}),
			)
			for row in rows
		]

	def update_conversation_extra(self, conversation_id: str, extra: dict[str, Any]) -> None:
		"""覆盖某会话的 ``extra``（人设、备注说明等，属于隔离工作区的一部分）。"""
		self._conn.execute(
			"UPDATE conversations SET extra = ?, updated_at = ? WHERE id = ?",
			(json.dumps(extra or {}, ensure_ascii=False), int(time.time()), conversation_id),
		)
		self._conn.commit()

	# -- 消息 --------------------------------------------------------------- #

	def upsert_messages(self, messages: Iterable[Message], *, replace: bool = False) -> int:
		"""写入消息，返回**新增**条数（已存在的 ``uid`` 不会重复计入）。

		:param messages: 归一化消息序列。
		:param replace: ``True`` 时覆盖同 uid 的旧内容（后端数据修正场景）。
		"""
		verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
		sql = (
			f"{verb} INTO messages ({', '.join(MESSAGE_COLUMNS)}, inserted_at) "
			f"VALUES ({', '.join(['?'] * (len(MESSAGE_COLUMNS) + 1))})"
		)
		inserted = 0
		now = int(time.time())
		with self.transaction():
			for message in messages:
				cursor = self._conn.execute(sql, (*message.to_row(), now))
				if cursor.rowcount and cursor.rowcount > 0:
					inserted += 1
					self._index_media(message)
		return inserted

	def _index_media(self, message: Message) -> None:
		"""把消息里的媒体引用写进 ``media`` 索引表（不下载，只登记）。"""
		for ref in message.media or []:
			if not isinstance(ref, dict):
				continue
			md5 = str(ref.get("md5") or "")
			path = str(ref.get("path") or "")
			kind = str(ref.get("kind") or message.kind)
			key = f"{message.conversation_id}|{md5 or path or message.uid}"
			self._conn.execute(
				"""
                INSERT INTO media (key, conversation_id, message_uid, kind, md5, path, url, size, status, extra, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    path = COALESCE(NULLIF(excluded.path, ''), media.path),
                    url  = COALESCE(NULLIF(excluded.url, ''), media.url)
                """,
				(
					key,
					message.conversation_id,
					message.uid,
					kind,
					md5,
					path,
					str(ref.get("url") or ""),
					int(ref.get("size") or 0),
					str(ref.get("status") or "referenced"),
					json.dumps(ref, ensure_ascii=False),
					int(time.time()),
				),
			)

	def messages(
		self,
		conversation_id: str,
		*,
		since: int = 0,
		until: int = 0,
		keyword: str = "",
		kinds: Sequence[str] | None = None,
		limit: int = 0,
		offset: int = 0,
		order: str = "asc",
	) -> list[Message]:
		"""**只按会话**查询消息（隔离的关键入口）。"""
		if not conversation_id:
			raise ValueError("messages() 必须指定 conversation_id：引擎不允许跨会话查询")
		sql = f"{_MSG_SELECT} WHERE conversation_id = ?"
		params: list[Any] = [conversation_id]
		if since:
			sql += " AND ts >= ?"
			params.append(int(since))
		if until:
			sql += " AND ts <= ?"
			params.append(int(until))
		if keyword:
			sql += " AND text LIKE ?"
			params.append(f"%{keyword}%")
		if kinds:
			placeholders = ", ".join(["?"] * len(kinds))
			sql += f" AND kind IN ({placeholders})"
			params.extend(list(kinds))
		sql += " ORDER BY ts " + ("DESC" if order.lower() == "desc" else "ASC")
		if limit and limit > 0:
			sql += " LIMIT ? OFFSET ?"
			params.extend([int(limit), int(offset)])
		rows = self._conn.execute(sql, params).fetchall()
		return [Message.from_row(row) for row in rows]

	def recent_messages(self, conversation_id: str, count: int = 40) -> list[Message]:
		"""取某会话最近 ``count`` 条，并按时间正序返回（喂 LLM 用）。"""
		rows = self.messages(conversation_id, limit=count, order="desc")
		return list(reversed(rows))

	def count_messages(self, conversation_id: str, *, since: int = 0, until: int = 0, keyword: str = "") -> int:
		"""统计某会话消息条数。"""
		sql = "SELECT COUNT(*) FROM messages WHERE conversation_id = ?"
		params: list[Any] = [conversation_id]
		if since:
			sql += " AND ts >= ?"
			params.append(int(since))
		if until:
			sql += " AND ts <= ?"
			params.append(int(until))
		if keyword:
			sql += " AND text LIKE ?"
			params.append(f"%{keyword}%")
		row = self._conn.execute(sql, params).fetchone()
		return int(row[0] or 0)

	def max_ts(self, conversation_id: str) -> int:
		"""某会话已摘录的最大时间戳（增量同步游标）。"""
		row = self._conn.execute("SELECT MAX(ts) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()
		return int(row[0] or 0)

	def refresh_conversation_stats(self, conversation_id: str) -> dict[str, Any]:
		"""重算并写回某会话的 msg_count / first_time / last_time。"""
		row = self._conn.execute(
			"SELECT COUNT(*), MIN(ts), MAX(ts) FROM messages WHERE conversation_id = ?",
			(conversation_id,),
		).fetchone()
		count, first_ts, last_ts = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
		self._conn.execute(
			"UPDATE conversations SET msg_count = ?, first_time = ?, last_time = ?, updated_at = ? WHERE id = ?",
			(count, first_ts, last_ts, int(time.time()), conversation_id),
		)
		self._conn.commit()
		return {"conversation_id": conversation_id, "messages": count, "first_ts": first_ts, "last_ts": last_ts}

	def kind_histogram(self, conversation_id: str) -> dict[str, int]:
		"""某会话的消息类型分布（总结时给模型一个「这段对话里有什么」的先验）。"""
		rows = self._conn.execute(
			"SELECT kind, COUNT(*) FROM messages WHERE conversation_id = ? GROUP BY kind ORDER BY COUNT(*) DESC",
			(conversation_id,),
		).fetchall()
		return {str(row[0] or "unknown"): int(row[1] or 0) for row in rows}

	def top_senders(self, conversation_id: str, limit: int = 10) -> list[dict[str, Any]]:
		"""某会话发言最多的人（群聊摘要用）。"""
		rows = self._conn.execute(
			"""
            SELECT COALESCE(NULLIF(sender_name, ''), sender_id) AS who, COUNT(*) AS n
            FROM messages WHERE conversation_id = ?
            GROUP BY who ORDER BY n DESC LIMIT ?
            """,
			(conversation_id, int(limit)),
		).fetchall()
		return [{"sender": row[0] or "未知", "messages": int(row[1] or 0)} for row in rows]

	# -- 媒体 --------------------------------------------------------------- #

	def list_media(self, conversation_id: str, *, kind: str = "", limit: int = 0) -> list[dict[str, Any]]:
		"""列出某会话登记过的媒体引用。"""
		sql = "SELECT key, kind, md5, path, url, size, status FROM media WHERE conversation_id = ?"
		params: list[Any] = [conversation_id]
		if kind:
			sql += " AND kind = ?"
			params.append(kind)
		sql += " ORDER BY created_at DESC"
		if limit and limit > 0:
			sql += " LIMIT ?"
			params.append(int(limit))
		rows = self._conn.execute(sql, params).fetchall()
		return [
			{
				"key": row[0],
				"kind": row[1],
				"md5": row[2],
				"path": row[3],
				"url": row[4],
				"size": row[5],
				"status": row[6],
			}
			for row in rows
		]

	# -- 同步游标 ----------------------------------------------------------- #

	def get_state(self, key: str, default: Any = None) -> Any:
		"""读一个同步状态值（JSON 解码）。"""
		row = self._conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
		if row is None:
			return default
		return _load_json(row[0], default)

	def set_state(self, key: str, value: Any) -> None:
		"""写一个同步状态值（JSON 编码）。"""
		self._conn.execute(
			"""
            INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
			(key, json.dumps(value, ensure_ascii=False), int(time.time())),
		)
		self._conn.commit()

	# -- 总结 --------------------------------------------------------------- #

	def add_summary(
		self,
		conversation_id: str,
		*,
		content: str,
		from_ts: int = 0,
		to_ts: int = 0,
		msg_count: int = 0,
		model: str = "",
		style: str = "",
		meta: dict[str, Any] | None = None,
	) -> int:
		"""写入一条总结，返回自增 id。"""
		cursor = self._conn.execute(
			"""
            INSERT INTO summaries (conversation_id, from_ts, to_ts, msg_count, model, style, content, meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
			(
				conversation_id,
				int(from_ts),
				int(to_ts),
				int(msg_count),
				model,
				style,
				content,
				json.dumps(meta or {}, ensure_ascii=False),
				int(time.time()),
			),
		)
		self._conn.commit()
		return int(cursor.lastrowid or 0)

	def latest_summary(self, conversation_id: str) -> dict[str, Any] | None:
		"""取某会话最近一条总结。"""
		row = self._conn.execute(
			"""
            SELECT id, from_ts, to_ts, msg_count, model, style, content, meta, created_at
            FROM summaries WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
            """,
			(conversation_id,),
		).fetchone()
		if row is None:
			return None
		return {
			"id": row[0],
			"from_ts": row[1],
			"to_ts": row[2],
			"msg_count": row[3],
			"model": row[4],
			"style": row[5],
			"content": row[6],
			"meta": _load_json(row[7], {}),
			"created_at": row[8],
		}

	def list_summaries(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
		"""列出某会话的总结历史（新→旧）。"""
		rows = self._conn.execute(
			"SELECT id, from_ts, to_ts, msg_count, created_at FROM summaries WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
			(conversation_id, int(limit)),
		).fetchall()
		return [
			{"id": row[0], "from_ts": row[1], "to_ts": row[2], "msg_count": row[3], "created_at": row[4]}
			for row in rows
		]

	# -- 草稿 --------------------------------------------------------------- #

	def add_draft(
		self,
		draft_id: str,
		conversation_id: str,
		*,
		text: str,
		model: str = "",
		instruction: str = "",
		rationale: str = "",
		tone: str = "",
		meta: dict[str, Any] | None = None,
		status: str = "draft",
	) -> None:
		"""保存一条回应草稿（草稿本身永远不会被自动发送）。"""
		self._conn.execute(
			"""
            INSERT OR REPLACE INTO drafts (id, conversation_id, created_at, model, instruction, text, rationale, tone, meta, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
			(
				draft_id,
				conversation_id,
				int(time.time()),
				model,
				instruction,
				text,
				rationale,
				tone,
				json.dumps(meta or {}, ensure_ascii=False),
				status,
			),
		)
		self._conn.commit()

	def get_draft(self, draft_id: str) -> dict[str, Any] | None:
		"""按 id 取草稿。"""
		row = self._conn.execute(
			"SELECT id, conversation_id, created_at, model, instruction, text, rationale, tone, meta, status FROM drafts WHERE id = ?",
			(draft_id,),
		).fetchone()
		if row is None:
			return None
		return {
			"id": row[0],
			"conversation_id": row[1],
			"created_at": row[2],
			"model": row[3],
			"instruction": row[4],
			"text": row[5],
			"rationale": row[6],
			"tone": row[7],
			"meta": _load_json(row[8], {}),
			"status": row[9],
		}

	def list_drafts(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
		"""列出某会话的草稿（新→旧）。"""
		rows = self._conn.execute(
			"SELECT id, text, tone, status, created_at FROM drafts WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
			(conversation_id, int(limit)),
		).fetchall()
		return [
			{"id": row[0], "text": row[1], "tone": row[2], "status": row[3], "created_at": row[4]}
			for row in rows
		]

	def update_draft_status(self, draft_id: str, status: str) -> bool:
		"""更新草稿状态（draft/sent/discarded/blocked）。"""
		cursor = self._conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
		self._conn.commit()
		return bool(cursor.rowcount)

	# -- 审计 --------------------------------------------------------------- #

	def audit_event(self, event: str, conversation_id: str = "", **detail: Any) -> None:
		"""写一条审计记录。发送层与同步流程都会调用它。"""
		self._conn.execute(
			"INSERT INTO audit (ts, event, conversation_id, detail) VALUES (?, ?, ?, ?)",
			(int(time.time()), event, conversation_id or "", json.dumps(detail, ensure_ascii=False, default=str)),
		)
		self._conn.commit()

	def list_audit(self, *, limit: int = 50, conversation_id: str = "") -> list[dict[str, Any]]:
		"""读审计记录（新→旧）。"""
		sql = "SELECT id, ts, event, conversation_id, detail FROM audit"
		params: list[Any] = []
		if conversation_id:
			sql += " WHERE conversation_id = ?"
			params.append(conversation_id)
		sql += " ORDER BY id DESC LIMIT ?"
		params.append(int(limit))
		rows = self._conn.execute(sql, params).fetchall()
		return [
			{"id": row[0], "ts": row[1], "event": row[2], "conversation_id": row[3], "detail": _load_json(row[4], {})}
			for row in rows
		]

	# -- 概览 --------------------------------------------------------------- #

	def overview(self) -> dict[str, Any]:
		"""库级统计（doctor / 报告用）。"""
		def _count(table: str) -> int:
			row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
			return int(row[0] or 0)

		row = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM messages").fetchone()
		return {
			"path": str(self.path),
			"conversations": _count("conversations"),
			"messages": _count("messages"),
			"media": _count("media"),
			"summaries": _count("summaries"),
			"drafts": _count("drafts"),
			"audit": _count("audit"),
			"first_ts": int(row[0] or 0),
			"last_ts": int(row[1] or 0),
		}

	def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
		"""逃生口：给诊断/测试用的只读查询（生产代码优先用上面的具名方法）。"""
		return self._conn.execute(sql, tuple(params)).fetchall()


def open_store(workdir: str | Path) -> Store:
	"""打开（必要时创建）工作目录下的归一化数据库。"""
	return Store(paths.db_path(workdir))


def _load_json(value: Any, fallback: Any) -> Any:
	"""宽容的 JSON 解码：坏了就返回 fallback，绝不让读侧崩。"""
	if value in (None, ""):
		return fallback
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return fallback


__all__ = ["Store", "open_store"]
