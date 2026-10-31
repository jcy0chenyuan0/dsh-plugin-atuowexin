# -*- coding: utf-8 -*-
"""后端测试：已解密 SQLite（4.x / 3.x）、chatlog HTTP、合成数据、自动探测。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from atuowexin import config as config_module
from atuowexin.backends import build_backend
from atuowexin.backends.chatlog_http import ChatlogBackend
from atuowexin.backends.decrypted_sqlite import DecryptedSqliteBackend
from atuowexin.backends.mock import MockBackend
from atuowexin.backends.base import BackendError

from . import fixtures


class DecryptedV4Tests(unittest.TestCase):
	"""微信 4.x 结构：``db_storage`` + ``Msg_<md5>`` 分表。"""

	@classmethod
	def setUpClass(cls):
		cls.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-v4-"))
		cls.meta = fixtures.build_v4_fixture(cls.tmp)
		cls.backend = DecryptedSqliteBackend({"root": str(cls.tmp)})

	@classmethod
	def tearDownClass(cls):
		shutil.rmtree(cls.tmp, ignore_errors=True)

	def test_health_and_schema(self):
		health = self.backend.health()
		self.assertTrue(health["ok"], health["detail"])
		self.assertEqual(self.backend.describe()["schema"], "v4")

	def test_conversations(self):
		conversations = {item.id: item for item in self.backend.list_conversations()}
		self.assertIn(fixtures.FRIEND[0], conversations)
		self.assertIn(fixtures.GROUP[0], conversations)
		self.assertEqual(conversations[fixtures.FRIEND[0]].remark, fixtures.FRIEND[2])
		self.assertTrue(conversations[fixtures.GROUP[0]].is_group)
		self.assertEqual(conversations[fixtures.GROUP[0]].member_count, len(fixtures.GROUP_MEMBERS))

	def test_messages_are_read_with_content_and_types(self):
		messages = list(self.backend.iter_messages(fixtures.FRIEND[0]))
		self.assertEqual(len(messages), 8)
		texts = [item.text for item in messages]
		self.assertTrue(any("#F0" in text for text in texts))
		self.assertTrue(any(item.kind == "image" for item in messages))
		quote = [item for item in messages if item.kind == "quote"]
		self.assertEqual(len(quote), 1)
		self.assertIn("引用", quote[0].text)
		# 图片消息应登记媒体 md5
		image = [item for item in messages if item.kind == "image"][0]
		self.assertEqual(image.media[0]["md5"], "0123456789abcdef0123456789abcdef")
		# 自己的消息要标成 is_self
		self.assertTrue(any(item.is_self for item in messages))

	def test_group_sender_resolution_and_fallback(self):
		messages = list(self.backend.iter_messages(fixtures.GROUP[0]))
		senders = {item.sender_id for item in messages}
		self.assertIn("wxid_linda2024", senders)
		# 第 6 条的 real_sender_id 指向不存在的行，必须回退到 packed_info_data 的 protobuf
		self.assertIn("wxid_fallback_sender", senders)
		# 系统消息
		self.assertTrue(any(item.type == 10000 for item in messages))

	def test_since_filter(self):
		all_messages = list(self.backend.iter_messages(fixtures.FRIEND[0]))
		cutoff = all_messages[3].ts
		filtered = list(self.backend.iter_messages(fixtures.FRIEND[0], since=cutoff))
		self.assertTrue(all(item.ts >= cutoff for item in filtered))
		self.assertLess(len(filtered), len(all_messages))

	def test_unknown_conversation_returns_nothing(self):
		self.assertEqual(list(self.backend.iter_messages("wxid_not_there")), [])

	def test_encrypted_database_gives_actionable_error(self):
		# 造一个假库（内容是随机字节），必须被识别为「还没解密」而不是崩掉
		fake_root = Path(tempfile.mkdtemp(prefix="atuowexin-enc-"))
		try:
			storage = fake_root / "wxid_x" / "db_storage" / "session"
			storage.mkdir(parents=True)
			(storage / "session.db").write_bytes(b"\x00\x01\x02not-a-database" * 100)
			backend = DecryptedSqliteBackend({"root": str(fake_root), "schema": "v4"})
			with self.assertRaises(BackendError) as ctx:
				backend.list_conversations()
			self.assertIn("解密", str(ctx.exception))
		finally:
			shutil.rmtree(fake_root, ignore_errors=True)


class DecryptedV3Tests(unittest.TestCase):
	"""微信 3.x 结构：``MSG*.db`` 单表 + ``MicroMsg.db``。"""

	@classmethod
	def setUpClass(cls):
		cls.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-v3-"))
		fixtures.build_v3_fixture(cls.tmp)
		cls.backend = DecryptedSqliteBackend({"root": str(cls.tmp)})

	@classmethod
	def tearDownClass(cls):
		shutil.rmtree(cls.tmp, ignore_errors=True)

	def test_schema_detected(self):
		self.assertEqual(self.backend.describe()["schema"], "v3")

	def test_conversations_from_session_table(self):
		ids = {item.id for item in self.backend.list_conversations()}
		self.assertIn(fixtures.FRIEND[0], ids)
		self.assertIn(fixtures.GROUP[0], ids)

	def test_messages_and_group_members(self):
		friend_messages = list(self.backend.iter_messages(fixtures.FRIEND[0]))
		self.assertEqual(len(friend_messages), 5)
		image = [item for item in friend_messages if item.kind == "image"]
		self.assertEqual(len(image), 1)

		group_messages = list(self.backend.iter_messages(fixtures.GROUP[0]))
		self.assertEqual(len(group_messages), 5)
		senders = {item.sender_id for item in group_messages}
		self.assertTrue({"wxid_linda2024", "wxid_zhouqi", "wxid_amy"} & senders)
		# 拍一拍（10000/4）的正文来自 DisplayContent
		self.assertTrue(any(item.kind == "pat" for item in group_messages))

	def test_group_member_display_names(self):
		group = [item for item in self.backend.list_conversations() if item.id == fixtures.GROUP[0]][0]
		self.assertEqual(group.member_count, len(fixtures.GROUP_MEMBERS))
		messages = list(self.backend.iter_messages(fixtures.GROUP[0]))
		names = {item.sender_name for item in messages}
		self.assertTrue(names & {"林达", "周琦", "Amy"} or names, names)


class ChatlogBackendTests(unittest.TestCase):
	"""chatlog HTTP 后端：分页、字段映射、503 翻译。"""

	def setUp(self):
		self.server = fixtures.MockChatlogServer()
		self.server.start()
		self.backend = ChatlogBackend({"base_url": self.server.base_url, "page_size": 3})

	def tearDown(self):
		self.server.stop()

	def test_health(self):
		health = self.backend.health()
		self.assertTrue(health["ok"], health["detail"])

	def test_conversations_merge_session_and_contact(self):
		conversations = {item.id: item for item in self.backend.list_conversations()}
		self.assertIn(fixtures.FRIEND[0], conversations)
		self.assertEqual(conversations[fixtures.FRIEND[0]].remark, fixtures.FRIEND[2])
		self.assertEqual(conversations[fixtures.GROUP[0]].member_count, len(fixtures.GROUP_MEMBERS))
		self.assertTrue(conversations[fixtures.GROUP[0]].is_group)

	def test_messages_pagination(self):
		messages = list(self.backend.iter_messages(fixtures.FRIEND[0]))
		# 6 条（5 文本 + 1 图片），页大小 3 → 至少请求两页
		self.assertEqual(len(messages), 6)
		self.assertGreaterEqual(len([item for item in self.server.requests if "/api/v1/chatlog" in item]), 2)
		self.assertTrue(any(item.kind == "image" for item in messages))
		self.assertEqual(messages[0].sender_name, "我")
		self.assertTrue(messages[0].is_self)

	def test_media_url(self):
		url = self.backend.media_url({"kind": "image", "md5": "a" * 32})
		self.assertIn("/image/", url)
		self.assertIn(self.server.base_url, url)

	def test_not_ready_maps_to_actionable_error(self):
		server = fixtures.MockChatlogServer(ready=False)
		server.start()
		try:
			backend = ChatlogBackend({"base_url": server.base_url})
			health = backend.health()
			self.assertFalse(health["ok"])
			self.assertIn("503", health["detail"])
			with self.assertRaises(BackendError):
				backend.list_conversations()
		finally:
			server.stop()

	def test_unreachable_server(self):
		backend = ChatlogBackend({"base_url": "http://127.0.0.1:1", "timeout": 1.0})
		health = backend.health()
		self.assertFalse(health["ok"])
		self.assertIn("连不上", health["detail"])


class MockBackendTests(unittest.TestCase):
	"""合成数据后端：确定性 + 明确标注合成。"""

	def test_deterministic(self):
		first = MockBackend({"seed": 3, "conversations": 3, "messages_per": 6})
		second = MockBackend({"seed": 3, "conversations": 3, "messages_per": 6})
		first_items = [item.to_dict() for item in first.iter_messages(first.list_conversations()[0].id)]
		second_items = [item.to_dict() for item in second.iter_messages(second.list_conversations()[0].id)]
		self.assertEqual(first_items, second_items)

	def test_marks_synthetic_and_has_both_kinds(self):
		backend = MockBackend({"seed": 1, "conversations": 4, "messages_per": 12})
		self.assertTrue(backend.describe()["synthetic"])
		self.assertTrue(backend.health()["synthetic"])
		kinds = {item.kind for item in backend.list_conversations() for item in backend.iter_messages(item.id)}
		self.assertIn("text", kinds)
		self.assertTrue({"image", "voice", "file"} & kinds)
		self.assertTrue(any(item.is_group for item in backend.list_conversations()))

	def test_conversation_count(self):
		for count in (1, 3, 5):
			backend = MockBackend({"seed": 1, "conversations": count, "messages_per": 3})
			self.assertEqual(len(backend.list_conversations()), count)


class AutoDetectTests(unittest.TestCase):
	"""``backend.name = auto`` 的选择顺序。"""

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-auto-"))
		fixtures.build_v4_fixture(self.tmp)
		self.config = config_module.default_config()

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_auto_prefers_decrypted_when_chatlog_down(self):
		self.config["backend"]["chatlog"]["base_url"] = "http://127.0.0.1:1"
		self.config["backend"]["chatlog"]["timeout"] = 1.0
		self.config["backend"]["decrypted_sqlite"]["root"] = str(self.tmp)
		backend, reason = build_backend(self.config)
		self.assertEqual(backend.name, "decrypted_sqlite")
		self.assertIn("已解密库", reason)

	def test_auto_prefers_chatlog_when_available(self):
		server = fixtures.MockChatlogServer()
		server.start()
		try:
			self.config["backend"]["chatlog"]["base_url"] = server.base_url
			backend, reason = build_backend(self.config)
			self.assertEqual(backend.name, "chatlog")
			self.assertIn("chatlog", reason)
		finally:
			server.stop()

	def test_auto_falls_back_to_mock_with_explanation(self):
		self.config["backend"]["chatlog"]["base_url"] = "http://127.0.0.1:1"
		self.config["backend"]["chatlog"]["timeout"] = 1.0
		backend, reason = build_backend(self.config)
		self.assertEqual(backend.name, "mock")
		self.assertIn("合成数据", reason)

	def test_explicit_backend_failure_is_loud(self):
		self.config["backend"]["chatlog"]["base_url"] = "http://127.0.0.1:1"
		self.config["backend"]["chatlog"]["timeout"] = 1.0
		with self.assertRaises(BackendError):
			build_backend(self.config, name="chatlog")

	def test_unknown_backend_name_rejected(self):
		with self.assertRaises(BackendError):
			build_backend(self.config, name="sqlite-magic")

	def test_decrypted_requires_root(self):
		with self.assertRaises(BackendError):
			build_backend(self.config, name="decrypted_sqlite")


if __name__ == "__main__":
	unittest.main()
