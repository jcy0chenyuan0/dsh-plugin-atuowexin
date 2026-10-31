# -*- coding: utf-8 -*-
"""路径探测测试：确认插件能发现「非默认父目录」下的微信数据。

这一条是被真机环境逼出来的：本机微信 4.1 的数据在
``Documents\\Tencent Files\\xwechat_files\\<账号>\\db_storage``，
而最初的探测只找 ``Documents\\xwechat_files``，于是 doctor 报了「未检测到微信数据目录」——
一个会让人白排查很久的假阴性。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atuowexin import paths


class DetectRootsTests(unittest.TestCase):
	"""``detect_wechat_data_roots`` 要能穿透自定义父目录。"""

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-paths-"))
		# 造出三种真实形态
		self.nested = self.tmp / "Documents" / "Tencent Files" / "xwechat_files" / "wxid_demo_f4b1" / "db_storage"
		self.nested.mkdir(parents=True)
		self.plain = self.tmp / "Documents" / "xwechat_files" / "wxid_plain" / "db_storage"
		self.plain.mkdir(parents=True)
		self.v3 = self.tmp / "Documents" / "WeChat Files" / "wxid_old" / "Msg"
		self.v3.mkdir(parents=True)
		self.documents = self.tmp / "Documents"

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)

	def test_finds_all_three_layouts(self):
		with mock.patch.object(Path, "home", staticmethod(lambda: self.tmp)), mock.patch.object(paths, "_registered_save_path", lambda: None):
			roots = paths.detect_wechat_data_roots()
		found = {str(item["path"]).lower(): item for item in roots}
		# 探测返回的是**数据根目录**（xwechat_files / WeChat Files），不是账号目录
		nested_root = self.nested.parents[1]
		plain_root = self.plain.parents[1]
		v3_root = self.v3.parents[1]
		self.assertIn(str(nested_root).lower(), found, f"未发现 Tencent Files 形态：{sorted(found)}")
		self.assertIn(str(plain_root).lower(), found)
		self.assertIn(str(v3_root).lower(), found)
		self.assertEqual(found[str(nested_root).lower()]["schema"], "v4")
		self.assertEqual(found[str(v3_root).lower()]["schema"], "v3")

	def test_accounts_are_listed(self):
		with mock.patch.object(Path, "home", staticmethod(lambda: self.tmp)), mock.patch.object(paths, "_registered_save_path", lambda: None):
			roots = paths.detect_wechat_data_roots()
		v4 = [item for item in roots if item["schema"] == "v4"]
		accounts = {name for item in v4 for name in item["accounts"]}
		self.assertIn("wxid_demo_f4b1", accounts)
		self.assertIn("wxid_plain", accounts)

	def test_no_duplicates(self):
		with mock.patch.object(Path, "home", staticmethod(lambda: self.tmp)), mock.patch.object(paths, "_registered_save_path", lambda: None):
			roots = paths.detect_wechat_data_roots()
		seen = [str(item["path"]).lower() for item in roots]
		self.assertEqual(len(seen), len(set(seen)))

	def test_looks_like_decrypted_root(self):
		self.assertEqual(paths.looks_like_decrypted_root(self.nested.parents[1]), "v4")
		self.assertEqual(paths.looks_like_decrypted_root(self.v3.parents[1]), "v3")
		self.assertEqual(paths.looks_like_decrypted_root(self.tmp / "nope"), "unknown")


if __name__ == "__main__":
	unittest.main()
