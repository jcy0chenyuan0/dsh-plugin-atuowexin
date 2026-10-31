# -*- coding: utf-8 -*-
"""CLI 测试：通过真实子进程调用 ``python -m atuowexin``，验证 ``--json`` 契约与退出码。

这一层的价值：DSH 插件的 JS 半边就是靠「解析 stdout 上的这一个 JSON 对象」工作的，
所以这里断言的重点是 **stdout 必须是纯 JSON**（进度全部走 stderr）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
	"""命令行端到端行为。"""

	@classmethod
	def setUpClass(cls):
		cls.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-cli-"))
		cls.work = cls.tmp / "work"
		cls.env = dict(os.environ)
		cls.env["PYTHONPATH"] = str(ENGINE_DIR) + os.pathsep + cls.env.get("PYTHONPATH", "")
		cls.env["PYTHONUTF8"] = "1"
		cls.env["PYTHONIOENCODING"] = "utf-8"
		cls.env.pop("DEEPSEEK_API_KEY", None)
		cls.env["ATUOWEXIN_CONFIG"] = str(cls.tmp / "config.json")
		cls.engine_dir = ENGINE_DIR
		cls.conversation_id = ""

	@classmethod
	def tearDownClass(cls):
		shutil.rmtree(cls.tmp, ignore_errors=True)

	def conv_id(self) -> str:
		"""拿到一个已摘录会话的 id（不依赖测试执行顺序）。"""
		cls = type(self)
		if not cls.conversation_id:
			payload = self.run_cli("conversations")
			cls.conversation_id = payload["conversations"][0]["id"]
		return cls.conversation_id

	def run_cli(self, *args: str, expect: int = 0) -> dict:
		"""跑一次 CLI，断言退出码并解析 stdout 的 JSON。"""
		command = [
			sys.executable,
			"-m",
			"atuowexin",
			"--json",
			"--workdir",
			str(self.work),
			"--backend",
			"mock",
			*args,
		]
		completed = subprocess.run(command, cwd=str(ENGINE_DIR), env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=180)
		self.assertEqual(completed.returncode, expect, f"命令返回 {completed.returncode}：{completed.stdout}\n{completed.stderr}")
		text = completed.stdout.strip()
		self.assertTrue(text, f"stdout 为空；stderr={completed.stderr}")
		try:
			return json.loads(text)
		except ValueError as error:  # pragma: no cover - 出问题时给出可读诊断
			self.fail(f"stdout 不是纯 JSON（{error}）：{text[:400]}")

	def test_01_config_validate(self):
		payload = self.run_cli("config", "validate")
		self.assertTrue(payload["ok"], payload.get("problems"))
		self.assertEqual(payload["problems"], [])

	def test_02_sync(self):
		payload = self.run_cli("sync")
		self.assertEqual(payload["backend"], "mock")
		self.assertTrue(payload["synthetic"])
		self.assertEqual(payload["conversations"], 3)
		self.assertGreater(payload["added"], 0)
		self.assertEqual(payload["errors"], [])
		# 再同步一次：零新增（增量幂等）
		again = self.run_cli("sync")
		self.assertEqual(again["added"], 0)

	def test_03_conversations(self):
		payload = self.run_cli("conversations")
		items = payload["conversations"]
		self.assertEqual(len(items), 3)
		group = [item for item in items if item["kind"] == "group"][0]
		types = {item["kind"] for item in items}
		self.assertEqual(types, {"group", "friend"})
		self.assertGreater(group["member_count"], 0)
		type(self).conversation_id = items[0]["id"]

	def test_04_messages(self):
		payload = self.run_cli("messages", "--conversation", self.conv_id(), "--limit", "3")
		self.assertEqual(payload["count"], 3)
		self.assertTrue(all(item["conversation_id"] == self.conv_id() for item in payload["messages"]))

	def test_05_messages_unknown_conversation_fails_cleanly(self):
		payload = self.run_cli("messages", "--conversation", "根本不存在的会话", expect=1)
		self.assertFalse(payload["ok"])
		self.assertIn("找不到会话", payload["error"])

	def test_06_export_and_workspaces(self):
		payload = self.run_cli("export", "--conversation", self.conv_id())
		self.assertGreater(payload["messages"], 0)
		for item in payload["files"]:
			self.assertTrue(Path(item).exists(), item)

		workspaces = self.run_cli("workspaces")
		self.assertEqual(len(workspaces["workspaces"]), 3)
		self.assertTrue(all(item["messages"] > 0 for item in workspaces["workspaces"]))

	def test_07_doctor(self):
		payload = self.run_cli("doctor")
		self.assertIn("checks", payload)
		names = {item["name"] for item in payload["checks"]}
		self.assertIn("工作目录可写", names)
		self.assertIn("当前生效后端", names)
		self.assertTrue(payload["ok"], [item for item in payload["checks"] if not item["ok"]])

	def test_08_watch_once_is_read_only(self):
		payload = self.run_cli("watch", "--once")
		self.assertEqual(payload["iterations"], 1)
		self.assertEqual(len(payload["rounds"]), 1)
		self.assertIn("只读", payload["note"])

	def _raw(self, *args: str) -> subprocess.CompletedProcess:
		"""直接跑 CLI 并拿完整结果（用于断言"命令不存在"这类退出码 2 的情况）。"""
		command = [
			sys.executable, "-m", "atuowexin", "--workdir", str(self.work), "--backend", "mock", *args,
		]
		return subprocess.run(
			command, cwd=str(self.engine_dir), env=self.env,
			capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
		)

	def test_09_removed_commands_are_gone(self):
		"""只读版本里发送/总结/草稿命令必须彻底不存在（不是"藏起来"）。"""
		for command in ("summarize", "reply", "send", "audit"):
			completed = self._raw(command, "--conversation", "x")
			self.assertEqual(completed.returncode, 2, f"{command} 竟然还能用：{completed.stdout}")
			self.assertIn("invalid choice", completed.stderr)

	def test_10_config_has_no_model_or_send_sections(self):
		"""配置里不再有 llm / send 段：没有 key 要配，也没有闸门要开。"""
		payload = self.run_cli("config", "show")
		self.assertNotIn("llm", payload)
		self.assertNotIn("send", payload)
		self.assertIn("backend", payload)
		self.assertIn("privacy", payload)

	def test_11_send_command_absent_from_help(self):
		completed = self._raw("--help")
		self.assertEqual(completed.returncode, 0)
		for command in ("summarize", "reply", "send"):
			self.assertNotIn(command, completed.stdout)
		self.assertIn("messages", completed.stdout)


if __name__ == "__main__":
	unittest.main()
