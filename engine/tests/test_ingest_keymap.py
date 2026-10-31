# -*- coding: utf-8 -*-
"""``ingest --keymap`` 测试：微信 4.x「每库一把 key」的批量接入。

为什么必须专门测这条路径：真机上单把 key **只能解出 17 个库里的 1 个**
（其余报 hmac mismatch，因为每个库的 salt 不同、派生出的 enc key 也不同）。
所以 keymap 不是"锦上添花"，而是这批数据的唯一可行解。

这里的夹具刻意让**每个库用不同的 raw key** 加密，从而真实复现"一把 key 解不动全部"，
再验证 keymap 能全部解开。
"""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from atuowexin import sqlcipher
from atuowexin.ingest import db_salt, ingest, load_keymap

PAGE_SIZE = 4096
RESERVE = 80  # SQLCipher 4
ROWS = 12


def make_reserved_db(path: Path, rows: int = ROWS, reserve: int = RESERVE) -> Path:
	"""造一个"带每页保留区"的 SQLite（否则加密器无处放 IV/HMAC）。

	先按 SQLCipher 的页格式写好第 1 页头（含保留字节数），再让 SQLite 去写数据，
	这样 SQLite 会为每页都留出保留区。
	"""
	header = bytearray(PAGE_SIZE)
	header[0:16] = b"SQLite format 3\x00"
	header[16:18] = struct.pack(">H", PAGE_SIZE)
	header[18] = 1
	header[19] = 1
	header[20] = reserve
	header[21] = 64
	header[22] = 32
	header[23] = 32
	header[24:28] = struct.pack(">I", 1)
	header[28:32] = struct.pack(">I", 1)
	header[44:48] = struct.pack(">I", 4)
	header[56:60] = struct.pack(">I", 1)
	header[92:96] = struct.pack(">I", 1)
	header[96:100] = struct.pack(">I", 3045000)
	header[100] = 0x0D
	header[101:103] = struct.pack(">H", 0)
	header[103:105] = struct.pack(">H", 0)
	header[105:107] = struct.pack(">H", PAGE_SIZE - reserve)
	header[107] = 0
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(bytes(header))

	connection = sqlite3.connect(str(path))
	try:
		connection.execute("CREATE TABLE msg(id INTEGER PRIMARY KEY, body TEXT)")
		for index in range(rows):
			connection.execute(
				"INSERT INTO msg(id, body) VALUES(?, ?)",
				(index, f"第{index}条 中文内容 {'x' * 300}"),
			)
		connection.commit()
	finally:
		connection.close()
	return path


def readable(path: Path, expected: int = ROWS) -> bool:
	"""解密产物是不是能读的 SQLite（行数要对得上）。"""
	try:
		connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
		try:
			return connection.execute("SELECT count(*) FROM msg").fetchone()[0] == expected
		finally:
			connection.close()
	except sqlite3.Error:
		return False


class KeymapHelpersTests(unittest.TestCase):
	"""keymap 解析与 salt 读取。"""

	def setUp(self) -> None:
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-keymap-"))

	def tearDown(self) -> None:
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_db_salt_reads_first_16_bytes(self) -> None:
		"""salt 就是文件前 16 字节的 hex（keymap 的键）。"""
		path = self.tmp / "x.db"
		path.write_bytes(bytes(range(32)))
		self.assertEqual(db_salt(path), bytes(range(16)).hex())

	def test_load_keymap_normalises_keys(self) -> None:
		"""键转小写、剥掉 x'...' 包裹、跳过非字符串值。"""
		table = load_keymap(
			{
				"AA5DC2D3527A128046C7027E3649D746": "x'70E0D9D3'",
				"other": "ABCDEF",
				"bad": 123,
			}
		)
		self.assertEqual(table["aa5dc2d3527a128046c7027e3649d746"], "70E0D9D3")
		self.assertNotIn("bad", table)

	def test_load_keymap_from_file(self) -> None:
		"""支持直接给 keymap JSON 路径。"""
		path = self.tmp / "keymap.json"
		path.write_text(json.dumps({"aa": "bb"}), encoding="utf-8")
		self.assertEqual(load_keymap(path), {"aa": "bb"})

	def test_load_keymap_rejects_bad_input(self) -> None:
		"""文件不存在 / 不是 JSON 对象 → 明确报错，而不是静默当空。"""
		with self.assertRaises(ValueError):
			load_keymap(self.tmp / "nope.json")
		bad = self.tmp / "bad.json"
		bad.write_text("[1, 2]", encoding="utf-8")
		with self.assertRaises(ValueError):
			load_keymap(bad)
		self.assertEqual(load_keymap(None), {})


@unittest.skipUnless(sqlcipher.is_available(), "需要 PowerShell 才能加解密")
class IngestKeymapTests(unittest.TestCase):
	"""每库一把 key 的批量解密。"""

	@classmethod
	def setUpClass(cls) -> None:
		cls.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-ingest-keymap-"))
		cls.plain = cls.tmp / "plain"
		cls.encrypted = cls.tmp / "encrypted"
		cls.keys: dict[str, str] = {}  # 库名 -> raw key（每库不同！）
		cls.rows: dict[str, int] = {}  # 库名 -> 该库的行数（校验用）

		layout = {
			"db_storage/contact/contact.db": 6,
			"db_storage/session/session.db": 8,
			"db_storage/message/message_0.db": 20,
		}
		for relative, rows in layout.items():
			source = make_reserved_db(cls.plain / relative, rows=rows)
			key = secrets.token_hex(32)  # 每个库一把独立的 key
			target = cls.encrypted / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			result = sqlcipher.encrypt_file(source, target, key=key, key_type="raw", version=4)
			if not result.ok:  # pragma: no cover - 环境缺 PowerShell 时会被 skip
				raise unittest.SkipTest(f"加密夹具失败：{result.error}")
			cls.keys[relative] = key
			cls.rows[relative] = rows

		# keymap 用**加密后**文件的 salt 作键（这正是真机上的配对方式）
		cls.keymap = {db_salt(cls.encrypted / relative): key for relative, key in cls.keys.items()}

	@classmethod
	def tearDownClass(cls) -> None:
		shutil.rmtree(cls.tmp, ignore_errors=True)

	def _config(self, name: str) -> dict:
		"""每个用例一个干净工作目录。"""
		workdir = self.tmp / f"work-{name}"
		workdir.mkdir(parents=True, exist_ok=True)
		return {"workdir": str(workdir), "backend": {}}

	def test_single_key_cannot_open_all_but_keymap_can(self) -> None:
		"""核心事实：单把 key 只解得出探针那一个；keymap 才能全解。"""
		from atuowexin.ingest import _find_probe

		# 单把 key 必须与**探针库**匹配，否则会在探针阶段就失败（见下一个用例）
		probe = _find_probe(self.encrypted)
		self.assertIsNotNone(probe)
		probe_relative = probe.relative_to(self.encrypted).as_posix()
		one_key = self.keys[probe_relative]

		bad = ingest(
			self._config("single"),
			source=self.encrypted,
			key=one_key,
			key_type="raw",
			out=self.tmp / "out-single",
			run_sync=False,
			persist=False,
		)
		self.assertEqual(bad.decrypted_ok, 1, "每库独立加密时，单把 key 只应解开探针那 1 个库")
		self.assertEqual(bad.decrypted_failed, 2)

		good = ingest(
			self._config("keymap"),
			source=self.encrypted,
			keymap=self.keymap,
			out=self.tmp / "out-keymap",
			run_sync=False,
			persist=False,
		)
		self.assertTrue(good.ok, good.error)
		self.assertEqual(good.decrypted_ok, 3)
		self.assertEqual(good.decrypted_failed, 0)
		self.assertEqual(good.keymap_entries, 3)
		for relative in self.keys:
			self.assertTrue(readable(self.tmp / "out-keymap" / relative, self.rows[relative]), relative)

	def test_wrong_single_key_fails_at_probe_stage(self) -> None:
		"""单把 key 与探针库不匹配 → 在探针阶段就失败（不白解一遍全库）。"""
		from atuowexin.ingest import _find_probe

		probe = _find_probe(self.encrypted)
		probe_relative = probe.relative_to(self.encrypted).as_posix()
		wrong = next(key for relative, key in self.keys.items() if relative != probe_relative)
		report = ingest(
			self._config("wrong"),
			source=self.encrypted,
			key=wrong,
			key_type="raw",
			out=self.tmp / "out-wrong",
			run_sync=False,
			persist=False,
		)
		self.assertFalse(report.ok)
		self.assertEqual(report.stage, "probe")
		self.assertEqual(report.decrypted_ok, 0)
		self.assertFalse(report.probe.get("ok"))
		self.assertIn("hmac", str(report.probe.get("error", "")).lower())

	def test_keymap_file_path_is_accepted(self) -> None:
		"""--keymap 给文件路径也能用。"""
		path = self.tmp / "keymap.json"
		path.write_text(json.dumps(self.keymap), encoding="utf-8")
		report = ingest(
			self._config("file"),
			source=self.encrypted,
			keymap=path,
			out=self.tmp / "out-file",
			run_sync=False,
			persist=False,
		)
		self.assertEqual(report.decrypted_ok, 3)
		self.assertEqual(report.keymap_entries, 3)

	def test_missing_keymap_file_is_reported(self) -> None:
		"""keymap 路径不存在 → 直接报错（不静默降级成"没密钥"，那样会让人猜半天）。"""
		report = ingest(
			self._config("missing"),
			source=self.encrypted,
			keymap=self.tmp / "nope.json",
			out=self.tmp / "out-missing",
			run_sync=False,
			persist=False,
		)
		self.assertFalse(report.ok)
		self.assertIn("keymap 文件不存在", report.error)

	def test_keymap_without_probe_entry_reports_salt(self) -> None:
		"""keymap 一个都不匹配时不硬试，而是把 salt 打出来让人核对。"""
		report = ingest(
			self._config("nomatch"),
			source=self.encrypted,
			keymap={"deadbeef" * 4: "aa" * 32},
			out=self.tmp / "out-nomatch",
			run_sync=False,
			persist=False,
		)
		self.assertFalse(report.ok)
		self.assertIn("keymap 里没有可用于探针的库", report.error)
		self.assertIn("salt=", report.error)

	def test_partial_keymap_falls_back_to_single_key(self) -> None:
		"""只覆盖一部分库时，剩下的库用 --key 兜底（避免整批失败）。"""
		relative = "db_storage/message/message_0.db"
		others = [item for item in self.keys if item != relative]
		partial = {db_salt(self.encrypted / relative): self.keys[relative]}
		# 兜底 key 取某个未覆盖库的 key —— 它只能解开那一个，其余未覆盖的应当失败
		fallback = self.keys[others[0]]
		report = ingest(
			self._config("partial"),
			source=self.encrypted,
			key=fallback,
			key_type="raw",
			keymap=partial,
			out=self.tmp / "out-partial",
			run_sync=False,
			persist=False,
		)
		self.assertEqual(report.decrypted_ok, 2, "keymap 命中 1 个 + 兜底 key 命中 1 个")
		self.assertEqual(report.decrypted_failed, 1)
		self.assertEqual(report.keymap_entries, 1)

	def test_probe_only_does_not_write_plaintext(self) -> None:
		"""--probe-only 只验密钥，不产出明文。"""
		out = self.tmp / "out-probe"
		report = ingest(
			self._config("probe"),
			source=self.encrypted,
			keymap=self.keymap,
			out=out,
			probe_only=True,
			run_sync=False,
			persist=False,
		)
		self.assertTrue(report.ok, report.error)
		self.assertEqual(report.stage, "probe")
		self.assertFalse(out.exists() and any(out.rglob("*.db")))


if __name__ == "__main__":
	unittest.main()
