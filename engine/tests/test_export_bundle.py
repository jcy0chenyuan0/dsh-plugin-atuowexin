# -*- coding: utf-8 -*-
"""整套 Markdown 聊天记录导出（``export --all``）的测试。

覆盖：总览表、单会话文件、合订本与目录、排序与编号、多行正文不破坏列表结构、
脱敏开关、以及 CLI 两条入口（单会话 / 全部）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from atuowexin.cli import main
from atuowexin.extract import export_markdown_bundle
from atuowexin.model import Conversation, Message
from atuowexin.store import open_store

BASE_TS = 1791200000  # 固定时间，保证断言稳定


class ExportMarkdownBundleTests(unittest.TestCase):
	"""导出整包 Markdown。"""

	def setUp(self) -> None:
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-md-"))
		self.store = open_store(self.tmp / "work")
		self.store.upsert_conversation(Conversation(id="wxid_zhang", name="张三", kind="friend", msg_count=2, last_time=BASE_TS + 100))
		self.store.upsert_conversation(
			Conversation(id="12345@chatroom", name="项目群", kind="group", member_count=42, msg_count=3, last_time=BASE_TS + 400)
		)
		self.store.upsert_conversation(Conversation(id="gh_news", name="某公众号", kind="official", msg_count=1, last_time=BASE_TS + 200))
		self.store.upsert_messages(
			[
				Message(conversation_id="wxid_zhang", seq=1, sender_id="wxid_zhang", sender_name="张三", ts=BASE_TS + 10, text="晚上一起吃饭？"),
				Message(conversation_id="wxid_zhang", seq=2, sender_id="me", sender_name="我", is_self=True, ts=BASE_TS + 100, text="好啊\n我七点到"),
				Message(conversation_id="12345@chatroom", seq=1, sender_id="wxid_zhang", sender_name="张三", ts=BASE_TS + 200, text="我的手机号 13800138000"),
				Message(conversation_id="12345@chatroom", seq=2, sender_id="wxid_li", sender_name="李四", ts=BASE_TS + 300, text="[图片]"),
				Message(conversation_id="12345@chatroom", seq=3, sender_id="me", sender_name="我", is_self=True, ts=BASE_TS + 400, text="收到"),
				Message(conversation_id="gh_news", seq=1, sender_id="gh_news", sender_name="某公众号", ts=BASE_TS + 200, text="今日推送"),
			]
		)

	def tearDown(self) -> None:
		self.store.close()
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_bundle_shape_and_content(self) -> None:
		"""总览 + 单会话文件 + 合订本，内容与顺序都对。"""
		out = self.tmp / "out"
		result = export_markdown_bundle(self.store, out)
		self.assertEqual(result["conversations"], 3)
		self.assertEqual(result["messages"], 6)

		overview = Path(result["overview"])
		self.assertTrue(overview.is_file())
		text = overview.read_text(encoding="utf-8")
		self.assertIn("# 微信聊天记录总览", text)
		self.assertIn("| # | 会话 | 类型 | 消息数 | 时间范围 | 文件 |", text)
		for name in ("张三", "项目群", "某公众号"):
			self.assertIn(name, text)
		self.assertIn("群聊（42 人）", text)
		self.assertIn("按会话/", text)

		# 排序：last_time 倒序 → 项目群(最新) 第 1、某公众号 第 2、张三 第 3
		first = Path(result["items"][0]["file"])
		self.assertIn("项目群", first.read_text(encoding="utf-8"))
		self.assertTrue(first.name.startswith("01-"))
		self.assertEqual(result["items"][1]["conversation_id"], "gh_news")

		# 单会话文件里消息齐全，且带时间与说话人
		zhang = Path(next(item["file"] for item in result["items"] if item["conversation_id"] == "wxid_zhang"))
		body = zhang.read_text(encoding="utf-8")
		self.assertIn("晚上一起吃饭？", body)
		self.assertIn("**", body)
		self.assertIn("我", body)
		# 多行正文：续行必须缩进，不能出现裸露的 "- " 开头
		self.assertIn("好啊\n  我七点到", body)
		self.assertNotIn("\n- 我七点到", body)

		combined = Path(result["combined"])
		self.assertTrue(combined.is_file())
		merged = combined.read_text(encoding="utf-8")
		self.assertIn("# 全部聊天记录", merged)
		self.assertIn("## 目录", merged)
		self.assertEqual(merged.count("## "), 3 + 1)  # 3 个会话小节 + "## 目录"
		for name in ("张三", "项目群", "某公众号"):
			self.assertIn(f"## {name}", merged)

	def test_redaction_switch(self) -> None:
		"""--redacted 时手机号必须被打码。"""
		plain = export_markdown_bundle(self.store, self.tmp / "plain")
		self.assertIn("13800138000", Path(plain["combined"]).read_text(encoding="utf-8"))
		masked = export_markdown_bundle(self.store, self.tmp / "masked", redacted=True)
		text = Path(masked["combined"]).read_text(encoding="utf-8")
		self.assertNotIn("13800138000", text)
		self.assertIn("*", text)

	def test_subset_and_flags(self) -> None:
		"""指定会话 + 只出单文件（不生成合订本）。"""
		target = [self.store.get_conversation("wxid_zhang")]
		result = export_markdown_bundle(self.store, self.tmp / "subset", conversations=target, include_combined=False)
		self.assertEqual(result["conversations"], 1)
		self.assertEqual(result["combined"], "")
		self.assertFalse((self.tmp / "subset" / "全部聊天记录.md").exists())
		self.assertTrue(Path(result["overview"]).is_file())

	def test_ordering_by_msg_count(self) -> None:
		"""order=msg_count 时按条数排（项目群 3 条最多，排第一）。"""
		result = export_markdown_bundle(self.store, self.tmp / "bycount", order="msg_count")
		self.assertEqual(result["items"][0]["conversation_id"], "12345@chatroom")

	def test_cli_all_and_single(self) -> None:
		"""CLI：--all 出整包；单会话仍可用；两者都不给时报错。"""
		import contextlib
		import io

		def run(argv: list[str]) -> int:
			"""跑 CLI 并吞掉 stdout（--json 会往 stdout 打 JSON，别污染测试输出）。"""
			with contextlib.redirect_stdout(io.StringIO()):
				return main(argv)

		work = self.tmp / "cli"
		code = run(["--json", "--workdir", str(work), "export", "--all", "--out", str(work / "md")])
		self.assertEqual(code, 0)

		work2 = self.tmp / "cli2"
		store = open_store(work2)
		try:
			store.upsert_conversation(Conversation(id="wxid_zhang", name="张三", kind="friend"))
			store.upsert_messages([Message(conversation_id="wxid_zhang", seq=1, ts=BASE_TS, text="hi")])
		finally:
			store.close()
		code = run(["--json", "--workdir", str(work2), "export", "--all", "--out", str(work2 / "md")])
		self.assertEqual(code, 0)
		overview = (work2 / "md" / "00-总览.md").read_text(encoding="utf-8")
		self.assertIn("张三", overview)
		self.assertTrue((work2 / "md" / "全部聊天记录.md").is_file())

		code = run(["--json", "--workdir", str(work2), "export", "--conversation", "wxid_zhang", "--out", str(work2 / "one")])
		self.assertEqual(code, 0)
		self.assertTrue(any((work2 / "one").rglob("messages.md")))

		code = run(["--json", "--workdir", str(work2), "export"])
		self.assertNotEqual(code, 0, "既没 --conversation 也没 --all 应当报错")


if __name__ == "__main__":
	unittest.main()
