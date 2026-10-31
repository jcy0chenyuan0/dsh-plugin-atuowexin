# -*- coding: utf-8 -*-
"""合成数据后端：**离线自测专用**。

它存在的理由是「不装微信也要能把整条链路跑通并验证」——摘录、隔离、总结、回应、
发送闸门全部可以在这份确定性数据上做端到端断言。它永远会声明
``synthetic: True``，CLI 与 doctor 都会把它明确标成合成数据，避免误当成真实聊天。
"""

from __future__ import annotations

import random
from typing import Any, Iterator

from ..model import Conversation, Message, classify, conversation_kind
from .base import BaseBackend

_FRIENDS = [
	("wxid_linda2024", "林达", "林达（产品）"),
	("wxid_zhouqi", "周琦", "周琦（后端）"),
	("wxid_amy", "Amy", "Amy（设计）"),
]

_GROUP = ("45678901234@chatroom", "产品研发群", "产品研发群")

_OFFICIAL = ("gh_9f2c8b7a6d", "示例公众号", "示例公众号")

_LINES = [
	"这版的原型我看过了，整体方向没问题，细节我们再对一下。",
	"接口文档我更新到最新了，麻烦确认下字段。",
	"明天下午三点开个短会对齐一下排期？",
	"这个 bug 我复现了，是并发写导致的，我提个修复。",
	"设计稿已经改好，主要调了间距和按钮层级。",
	"收到，我今天下班前给结论。",
	"客户那边希望这周能看到 demo。",
	"我们先按最小可用版本切，剩下的下个迭代做。",
	"好，那就这么定了。",
	"刚发了一份数据，你看下表格第二页。",
	"这个需求要不要砍掉？收益不明显。",
	"本地跑没问题，线上还得压测一轮。",
]

_EDGE_LINES = [
	"（发来一张截图）",
	"（发送了一个文件：需求说明书.docx）",
	"（语音 12 秒）",
]


def _message(conversation: Conversation, rng: random.Random, ts: int, sender_id: str, sender_name: str, text: str, is_self: bool, kind: str) -> Message:
	"""构造一条合成消息（类型号与微信实际取值保持一致）。"""
	type_map = {"text": 1, "image": 3, "voice": 34, "file": 49, "system": 10000, "quote": 49, "link": 49, "pat": 10000}
	sub_map = {"file": 6, "quote": 57, "link": 5, "pat": 4}
	msg_type = type_map.get(kind, 1)
	sub_type = sub_map.get(kind, 0)
	media: list[dict[str, Any]] = []
	if kind == "image":
		media = [{"kind": "image", "md5": f"{rng.getrandbits(32):08x}{rng.getrandbits(32):08x}"}]
	elif kind == "voice":
		media = [{"kind": "voice", "svr_id": str(rng.getrandbits(30))}]
	elif kind == "file":
		media = [{"kind": "file", "md5": f"{rng.getrandbits(32):08x}{rng.getrandbits(32):08x}", "name": "需求说明书.docx"}]
	return Message(
		conversation_id=conversation.id,
		seq=0,
		server_id=rng.getrandbits(48),
		sender_id=sender_id,
		sender_name=sender_name,
		is_self=is_self,
		ts=ts,
		type=msg_type,
		sub_type=sub_type,
		kind=kind,
		text=text,
		media=media,
		raw={"synthetic": True},
	)


class MockBackend(BaseBackend):
	"""确定性合成后端：同样的 seed 永远产出同样的会话与消息。"""

	name = "mock"

	def __init__(self, config: dict[str, Any] | None = None):
		settings = dict(config or {})
		self.conversations_count = int(settings.get("conversations", 3))
		self.messages_per = int(settings.get("messages_per", 25))
		self.days = int(settings.get("days", 30))
		self.seed = int(settings.get("seed", 7))
		self._now = int(settings.get("now", 0)) or _now_seconds()
		self._conversations, self._messages = self._build()

	# -- Backend ----------------------------------------------------------- #

	def health(self) -> dict[str, Any]:
		"""合成后端永远可用。"""
		return {
			"ok": True,
			"synthetic": True,
			"detail": "合成数据后端（离线自测用），不是真实微信数据",
			"conversations": len(self._conversations),
			"messages": sum(len(items) for items in self._messages.values()),
		}

	def list_conversations(self) -> list[Conversation]:
		"""返回合成会话。"""
		return [Conversation(**conv.to_dict()) for conv in self._conversations]

	def iter_messages(self, conversation_id: str, *, since: int = 0, until: int = 0, limit: int = 0) -> Iterator[Message]:
		"""按时间升序产出该会话的合成消息。"""
		produced = 0
		for message in self._messages.get(conversation_id, []):
			if since and message.ts < since:
				continue
			if until and message.ts > until:
				continue
			yield message
			produced += 1
			if limit and produced >= limit:
				return

	def describe(self) -> dict[str, Any]:
		"""标注为合成数据。"""
		return {"name": self.name, "synthetic": True, "seed": self.seed}

	# -- 构造 --------------------------------------------------------------- #

	def _build(self) -> tuple[list[Conversation], dict[str, list[Message]]]:
		rng = random.Random(self.seed)
		conversations: list[Conversation] = []
		messages: dict[str, list[Message]] = {}

		blueprints: list[tuple[str, str, str]] = []
		blueprints.append(_GROUP)
		blueprints.extend(_FRIENDS)
		if self.conversations_count >= 5:
			blueprints.append(_OFFICIAL)
		elif self.conversations_count <= 0:
			blueprints = [_GROUP]

		for username, name, remark in blueprints[: max(1, self.conversations_count)]:
			kind = conversation_kind(username)
			conversation = Conversation(
				id=username,
				name=name,
				remark=remark,
				kind=kind,
				member_count=len(_FRIENDS) + 1 if kind == "group" else 0,
				extra={},
			)
			conversations.append(conversation)
			messages[username] = self._build_messages(conversation, rng)

		for conversation in conversations:
			items = messages[conversation.id]
			conversation.msg_count = len(items)
			conversation.last_time = items[-1].ts if items else 0
		return conversations, messages

	def _build_messages(self, conversation: Conversation, rng: random.Random) -> list[Message]:
		"""在 ``days`` 天里均匀铺开 ``messages_per`` 条消息，并掺入非文本类型。"""
		items: list[Message] = []
		total = max(1, self.messages_per)
		span = max(1, self.days) * 86400
		start = self._now - span
		for index in range(total):
			ts = start + int(span * (index + 1) / (total + 1)) + rng.randint(-600, 600)
			if conversation.kind == "group":
				who_id, who_name, _ = rng.choice(_FRIENDS)
			elif conversation.kind == "official":
				who_id, who_name = conversation.id, conversation.display_name
			else:
				who_id, who_name = conversation.id, conversation.display_name
			is_self = rng.random() < 0.45 and conversation.kind != "official"
			kind = "text"
			text = rng.choice(_LINES)
			roll = rng.random()
			if roll < 0.12:
				kind = rng.choice(["image", "voice", "file"])
				text = _EDGE_LINES[["image", "voice", "file"].index(kind)]
			elif roll < 0.16:
				kind = "quote"
				text = f"引用：{rng.choice(_LINES)[:18]} …… 我的看法是同意。"
			elif roll < 0.19:
				kind = "link"
				text = "分享了一个链接：技术方案对比"
			elif roll < 0.22:
				kind = "pat"
				text = f"{who_name} 拍了拍你"
			elif roll < 0.24:
				kind = "system"
				text = f"{who_name} 邀请 新同学 加入了群聊"
			if conversation.kind == "official":
				kind = "text"
				text = f"[推送] {rng.choice(_LINES)}"
				is_self = False
			message = _message(conversation, rng, ts, who_id, who_name, text, is_self, kind)
			if message.kind != kind:  # 保证 kind 与类型号一致
				message.kind = kind or classify(message.type, message.sub_type)
			items.append(message)
		items.sort(key=lambda item: item.ts)
		for index, message in enumerate(items, start=1):
			message.seq = index
		return items


def _now_seconds() -> int:
	"""当前秒级时间戳。"""
	import time

	return int(time.time())


__all__ = ["MockBackend"]
