# -*- coding: utf-8 -*-
"""存储与隔离测试：去重、按会话查询、游标、工作区隔离断言。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from atuowexin import isolate
from atuowexin.model import Conversation, Message
from atuowexin.store import Store, open_store


def _message(conversation_id: str, index: int, text: str = "") -> Message:
	"""构造一条测试消息。"""
	return Message(
		conversation_id=conversation_id,
		seq=index,
		server_id=index,
		sender_id=f"wxid_{conversation_id}_sender",
		sender_name="某人",
		is_self=index % 2 == 0,
		ts=1_760_000_000 + index * 60,
		type=1,
		kind="text",
		text=text or f"{conversation_id} 的第 {index} 条",
	)


class StoreTests(unittest.TestCase):
	"""``Store`` 的读写行为。"""

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-store-"))
		self.store = open_store(self.tmp)

	def tearDown(self):
		self.store.close()
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_upsert_dedupes_by_uid(self):
		messages = [_message("wxid_a", index) for index in range(5)]
		self.assertEqual(self.store.upsert_messages(messages), 5)
		# 同样的消息再写一遍：一条都不新增
		self.assertEqual(self.store.upsert_messages(messages), 0)
		self.assertEqual(self.store.count_messages("wxid_a"), 5)

	def test_queries_are_conversation_scoped(self):
		self.store.upsert_messages([_message("wxid_a", index) for index in range(3)])
		self.store.upsert_messages([_message("wxid_b", index) for index in range(4)])
		self.assertEqual(len(self.store.messages("wxid_a")), 3)
		self.assertEqual(len(self.store.messages("wxid_b")), 4)
		self.assertTrue(all(item.conversation_id == "wxid_a" for item in self.store.messages("wxid_a")))

	def test_messages_requires_conversation_id(self):
		with self.assertRaises(ValueError):
			self.store.messages("")

	def test_time_and_keyword_filters(self):
		self.store.upsert_messages([_message("wxid_a", index, text=("会议" if index == 2 else "闲聊")) for index in range(5)])
		self.assertEqual(len(self.store.messages("wxid_a", since=1_760_000_000 + 120)), 3)
		self.assertEqual(len(self.store.messages("wxid_a", keyword="会议")), 1)

	def test_cursor_and_stats(self):
		self.store.upsert_messages([_message("wxid_a", index) for index in range(5)])
		self.assertEqual(self.store.max_ts("wxid_a"), 1_760_000_000 + 240)
		stats = self.store.refresh_conversation_stats("wxid_a")
		self.assertEqual(stats["messages"], 5)
		self.assertEqual(stats["first_ts"], 1_760_000_000)

	def test_state_roundtrip(self):
		self.store.set_state("cursor:mock:wxid_a", 123)
		self.assertEqual(self.store.get_state("cursor:mock:wxid_a"), 123)
		self.assertIsNone(self.store.get_state("nope"))

	def test_summary_and_draft_and_audit(self):
		self.store.add_summary("wxid_a", content="总结正文", from_ts=1, to_ts=2, msg_count=3, model="m", style="s")
		latest = self.store.latest_summary("wxid_a")
		self.assertEqual(latest["content"], "总结正文")
		self.assertEqual(latest["to_ts"], 2)

		self.store.add_draft("d1", "wxid_a", text="你好", tone="稳妥", rationale="因为")
		self.assertEqual(self.store.get_draft("d1")["text"], "你好")
		self.assertTrue(self.store.update_draft_status("d1", "discarded"))
		self.assertEqual(self.store.get_draft("d1")["status"], "discarded")

		self.store.audit_event("send.blocked", "wxid_a", reason="默认安全模式")
		events = self.store.list_audit(limit=5)
		self.assertEqual(events[0]["event"], "send.blocked")
		self.assertEqual(events[0]["detail"]["reason"], "默认安全模式")

	def test_media_index(self):
		message = _message("wxid_a", 1)
		message.kind = "image"
		message.media = [{"kind": "image", "md5": "a" * 32}]
		self.store.upsert_messages([message])
		media = self.store.list_media("wxid_a")
		self.assertEqual(len(media), 1)
		self.assertEqual(media[0]["md5"], "a" * 32)

	def test_overview(self):
		self.store.upsert_messages([_message("wxid_a", index) for index in range(2)])
		overview = self.store.overview()
		self.assertEqual(overview["messages"], 2)
		self.assertEqual(overview["conversations"], 0)  # 会话要显式 upsert


class WorkspaceTests(unittest.TestCase):
	"""会话工作区的隔离保证。"""

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-ws-"))
		self.conversation = Conversation(id="wxid_a", name="小林", kind="friend")

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_workspace_paths_are_stable(self):
		first = isolate.workspace_for(self.tmp, self.conversation)
		second = isolate.workspace_for(self.tmp, self.conversation)
		self.assertEqual(first.root, second.root)
		self.assertIn("conversations", str(first.root))

	def test_append_is_idempotent(self):
		workspace = isolate.workspace_for(self.tmp, self.conversation).ensure()
		messages = [_message("wxid_a", index) for index in range(4)]
		self.assertEqual(workspace.append_messages(messages), 4)
		self.assertEqual(workspace.append_messages(messages), 0)
		self.assertEqual(len(list(workspace.iter_messages())), 4)

	def test_append_rejects_foreign_conversation(self):
		workspace = isolate.workspace_for(self.tmp, self.conversation).ensure()
		foreign = _message("wxid_OTHER", 1)
		with self.assertRaises(isolate.IsolationError):
			workspace.append_messages([foreign])
		# 拒绝之后不应该留下半截数据
		self.assertEqual(len(list(workspace.iter_messages())), 0)

	def test_summary_written_into_own_dir(self):
		workspace = isolate.workspace_for(self.tmp, self.conversation).ensure()
		path = workspace.write_summary("## 概览\n- 测试", {"messages": 1})
		self.assertTrue(Path(path).exists())
		self.assertIn("小林", workspace.read_summary())
		other = isolate.workspace_for(self.tmp, Conversation(id="wxid_b", name="小周")).ensure()
		self.assertEqual(other.read_summary(), "")

	def test_list_workspaces(self):
		workspace = isolate.workspace_for(self.tmp, self.conversation).ensure()
		workspace.append_messages([_message("wxid_a", index) for index in range(3)])
		entries = isolate.list_workspaces(self.tmp)
		self.assertEqual(len(entries), 1)
		self.assertEqual(entries[0]["conversation_id"], "wxid_a")
		self.assertEqual(entries[0]["messages"], 3)

	def test_notes_file_is_never_overwritten(self):
		workspace = isolate.workspace_for(self.tmp, self.conversation).ensure()
		workspace.notes_path.write_text("我的人设：简短", encoding="utf-8")
		workspace.ensure()
		self.assertEqual(workspace.read_notes(), "我的人设：简短")


if __name__ == "__main__":
	unittest.main()
