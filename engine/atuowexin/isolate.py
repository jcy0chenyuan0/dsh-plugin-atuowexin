# -*- coding: utf-8 -*-
"""会话隔离：一条会话 = 一个独立目录 + 一份独立上下文。

这是需求里「对每个群聊/对话隔离」的**执行点**，用三层来保证：

1. **目录隔离**：``<workdir>/conversations/<slug>/``，slug = 可读名 + username 短哈希，
   同名不同 id 的会话不会撞车，会话改名也不会换目录。
2. **读写隔离**：本模块是唯一允许碰会话文件的入口，且 :meth:`ConversationWorkspace.append_messages`
   会逐条断言 ``message.conversation_id == 本会话 id``，写错会话直接抛 :class:`IsolationError`。
3. **上下文隔离**：总结/回应只从本会话工作区取消息（见 ``summarize`` / ``reply``），
   跨会话数据在 API 形状上就取不到。

目录内容::

    conversations/<slug>/
        messages.jsonl      完整摘录（每条消息一行 JSON）
        summary.md          最新 AI 总结（人读）
        summary.json        最新 AI 总结的结构化版本 + 覆盖范围元数据
        profile.json        会话画像：类型、成员数、统计、备注、人设
        notes.md            你手写的备注（引擎绝不覆盖）
        drafts/             生成的回应草稿，一稿一文件
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .model import Conversation, Message


class IsolationError(RuntimeError):
	"""试图把某会话的数据写进另一个会话的工作区。"""


@dataclass
class ConversationWorkspace:
	"""一条会话的隔离工作区。"""

	root: Path
	conversation: Conversation
	_backend_name: str = ""
	_uids: set[str] | None = field(default=None, repr=False, compare=False)

	# -- 路径 --------------------------------------------------------------- #

	@property
	def messages_path(self) -> Path:
		"""``messages.jsonl`` 路径。"""
		return self.root / "messages.jsonl"

	@property
	def summary_md_path(self) -> Path:
		"""``summary.md`` 路径。"""
		return self.root / "summary.md"

	@property
	def summary_json_path(self) -> Path:
		"""``summary.json`` 路径。"""
		return self.root / "summary.json"

	@property
	def profile_path(self) -> Path:
		"""``profile.json`` 路径。"""
		return self.root / "profile.json"

	@property
	def notes_path(self) -> Path:
		"""``notes.md`` 路径（用户手写，引擎只读不写）。"""
		return self.root / "notes.md"

	@property
	def drafts_dir(self) -> Path:
		"""``drafts/`` 目录。"""
		return self.root / "drafts"

	def ensure(self) -> "ConversationWorkspace":
		"""创建目录结构并刷新画像（幂等）。"""
		self.root.mkdir(parents=True, exist_ok=True)
		self.drafts_dir.mkdir(parents=True, exist_ok=True)
		if not self.notes_path.exists():
			self.notes_path.write_text(
				f"# {self.conversation.display_name} 的手写备注\n\n"
				"（引擎不会覆盖这个文件。可以在这里写人设、注意事项、称呼偏好等，"
				"生成回应时会把内容作为背景信息带上。）\n",
				encoding="utf-8",
			)
		self.write_profile()
		return self

	# -- 消息 --------------------------------------------------------------- #

	def append_messages(self, messages: Iterable[Message]) -> int:
		"""把新消息追加到 ``messages.jsonl``，按 uid 去重；返回新增条数。

		:raises IsolationError: 出现不属于本会话的消息（隔离断言）。
		"""
		known = self.existing_uids()
		added = 0
		self.root.mkdir(parents=True, exist_ok=True)
		with self.messages_path.open("a", encoding="utf-8") as handle:
			for message in messages:
				if message.conversation_id != self.conversation.id:
					raise IsolationError(
						f"拒绝跨会话写入：工作区属于 {self.conversation.id}，"
						f"但收到 {message.conversation_id} 的消息（uid={message.uid}）"
					)
				if message.uid in known:
					continue
				handle.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
				known.add(message.uid)
				added += 1
		if added:
			self.write_profile()
		return added

	def iter_messages(self) -> Iterator[Message]:
		"""逐条读回本会话的消息（坏行跳过，不让一个脏行毁掉整份记录）。"""
		if not self.messages_path.exists():
			return
		with self.messages_path.open("r", encoding="utf-8") as handle:
			for line in handle:
				text = line.strip()
				if not text:
					continue
				try:
					payload = json.loads(text)
				except ValueError:
					continue
				if isinstance(payload, dict):
					yield Message.from_dict(payload)

	def existing_uids(self) -> set[str]:
		"""本会话已落盘消息的 uid 集合（首次调用后缓存在内存）。"""
		if self._uids is None:
			self._uids = {message.uid for message in self.iter_messages()}
		return self._uids

	# -- 总结 / 画像 / 草稿 -------------------------------------------------- #

	def write_summary(self, markdown: str, meta: dict[str, Any] | None = None) -> Path:
		"""写入最新总结（人读 Markdown + 结构化 JSON）。"""
		self.root.mkdir(parents=True, exist_ok=True)
		header = f"# {self.conversation.display_name} · AI 总结\n\n"
		header += f"> 会话 id：`{self.conversation.id}`　类型：{self.conversation.kind}　"
		header += f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
		self.summary_md_path.write_text(header + markdown.strip() + "\n", encoding="utf-8")
		self.summary_json_path.write_text(
			json.dumps({"conversation_id": self.conversation.id, "meta": meta or {}, "content": markdown}, ensure_ascii=False, indent=2) + "\n",
			encoding="utf-8",
		)
		return self.summary_md_path

	def read_summary(self) -> str:
		"""读回最新总结（没有则空串）。"""
		if not self.summary_md_path.exists():
			return ""
		try:
			return self.summary_md_path.read_text(encoding="utf-8")
		except OSError:
			return ""

	def read_summary_meta(self) -> dict[str, Any]:
		"""读回总结的结构化元数据。"""
		if not self.summary_json_path.exists():
			return {}
		try:
			payload = json.loads(self.summary_json_path.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return {}
		return payload.get("meta") or {} if isinstance(payload, dict) else {}

	def write_profile(self, extra: dict[str, Any] | None = None) -> Path:
		"""写入会话画像（统计 + 配置里的备注说明）。"""
		stats: dict[str, Any] = {}
		try:
			messages = list(self.iter_messages())
			if messages:
				stats = {
					"messages": len(messages),
					"first_ts": min(item.ts for item in messages),
					"last_ts": max(item.ts for item in messages),
				}
		except OSError:
			stats = {}
		profile = {
			"conversation_id": self.conversation.id,
			"display_name": self.conversation.display_name,
			"kind": self.conversation.kind,
			"is_group": self.conversation.is_group,
			"member_count": self.conversation.member_count,
			"backend": self._backend_name,
			"stats": stats,
			"updated_at": int(time.time()),
		}
		if isinstance(self.conversation.extra, dict):
			profile["conversation_extra"] = self.conversation.extra
		if extra:
			profile.update(extra)
		self.root.mkdir(parents=True, exist_ok=True)
		self.profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
		return self.profile_path

	def read_profile(self) -> dict[str, Any]:
		"""读回会话画像。"""
		if not self.profile_path.exists():
			return {}
		try:
			payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return {}
		return payload if isinstance(payload, dict) else {}

	def read_notes(self) -> str:
		"""读回手写备注（生成回应时的背景信息）。"""
		if not self.notes_path.exists():
			return ""
		try:
			return self.notes_path.read_text(encoding="utf-8")
		except OSError:
			return ""

	def save_draft(self, draft_id: str, text: str, meta: dict[str, Any] | None = None) -> Path:
		"""把一条草稿写成 ``drafts/<id>.md``。"""
		self.drafts_dir.mkdir(parents=True, exist_ok=True)
		target = self.drafts_dir / f"{draft_id}.md"
		body = f"# 草稿 {draft_id}\n\n"
		body += f"- 会话：{self.conversation.display_name}（`{self.conversation.id}`）\n"
		body += f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
		if meta:
			for key, value in meta.items():
				body += f"- {key}：{value}\n"
		body += f"\n---\n\n{text.strip()}\n\n---\n\n> 这是**草稿**，引擎不会自动发送。\n"
		target.write_text(body, encoding="utf-8")
		return target

	def context_fingerprint(self) -> str:
		"""工作区指纹（会话 id + 消息条数），用于自检隔离是否被破坏。"""
		import hashlib

		payload = f"{self.conversation.id}|{len(self.existing_uids())}".encode("utf-8")
		return hashlib.md5(payload).hexdigest()


def workspace_for(workdir: str | os.PathLike[str], conversation: Conversation, *, backend_name: str = "") -> ConversationWorkspace:
	"""按会话取（必要时创建）隔离工作区。"""
	return ConversationWorkspace(root=Path(workdir) / "conversations" / conversation.slug, conversation=conversation, _backend_name=backend_name)


def list_workspaces(workdir: str | os.PathLike[str]) -> list[dict[str, Any]]:
	"""列出工作目录下所有会话工作区及其画像。"""
	root = Path(workdir) / "conversations"
	if not root.is_dir():
		return []
	results: list[dict[str, Any]] = []
	for child in sorted(root.iterdir()):
		if not child.is_dir():
			continue
		profile_path = child / "profile.json"
		profile: dict[str, Any] = {}
		if profile_path.exists():
			try:
				profile = json.loads(profile_path.read_text(encoding="utf-8"))
			except (OSError, ValueError):
				profile = {}
		messages_path = child / "messages.jsonl"
		lines = 0
		if messages_path.exists():
			try:
				with messages_path.open("r", encoding="utf-8") as handle:
					lines = sum(1 for line in handle if line.strip())
			except OSError:
				lines = 0
		results.append(
			{
				"dir": str(child),
				"slug": child.name,
				"conversation_id": profile.get("conversation_id", ""),
				"display_name": profile.get("display_name", child.name),
				"kind": profile.get("kind", ""),
				"messages": lines,
				"has_summary": (child / "summary.md").exists(),
				"drafts": len(list((child / "drafts").glob("*.md"))) if (child / "drafts").is_dir() else 0,
			}
		)
	return results


__all__ = [
	"ConversationWorkspace",
	"IsolationError",
	"workspace_for",
	"list_workspaces",
]
