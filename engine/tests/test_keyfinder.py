# -*- coding: utf-8 -*-
"""密钥自动提取（:mod:`atuowexin.keyfinder`）的离线测试。

**不碰任何真实进程**：内存用 :class:`FakeMemory` 注入，库第 1 页在测试里现造
（salt + 随机密文/IV + 用真 key 算出来的 HMAC）。于是可以精确断言：

1. 密钥出现在**数据库路径附近**时必须找得到（真机实测：路径离密钥 344 B ~ 635 KB）；
2. 密钥**离 salt 很远**时也要找得到（真机实测：salt 离密钥 5.8 万 ~ 292 MB——
   所以"按 salt 找邻居"是错的假设，4.1.7.30 上因此扫了 7 分钟一无所获）；
3. 旁边只有路径、没有真密钥时**必须找不到**；
4. 随机字节、别的库的密钥**必须被 HMAC 拒掉**。

第 2、3、4 条比第 1 条重要：一个"经常给出错密钥"的提取器，比没有提取器更危险。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from atuowexin import keyfinder  # noqa: E402

KEY = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90" * 2)
KEY2 = bytes.fromhex("0f1e2d3c4b5a69788796a5b4c3d2e1f0" * 2)
SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
SALT2 = bytes.fromhex("ffeeddccbbaa99887766554433221100")
PAGE = 4096
RESERVE = 80
REGION_BASE = 0x10000000


def make_db(path: Path, salt: bytes, key: bytes) -> None:
    """造一个"看起来像" SQLCipher v4 库的文件：第 1 页是真 HMAC，其余是填充。"""
    ciphertext = os.urandom(PAGE - RESERVE - len(salt))
    iv = os.urandom(16)
    masked = bytes(byte ^ 0x3A for byte in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, masked, 2, 32)
    mac = hmac.new(mac_key, ciphertext + iv + (1).to_bytes(4, "little"), "sha512").digest()
    page1 = salt + ciphertext + iv + mac
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(page1 + bytes(PAGE - len(page1)) + bytes(PAGE))


class FakeMemory(keyfinder.MemoryReader):
    """把"进程内存"变成一个可控的字节块。"""

    def __init__(self, regions: dict[int, bytes], pid: int = 4242) -> None:
        self.pid = pid
        self._regions = dict(regions)
        self.reads = 0

    def regions(self) -> list[tuple[int, int]]:
        return [(base, len(data)) for base, data in sorted(self._regions.items())]

    def read(self, address: int, size: int) -> bytes:
        self.reads += 1
        for base, data in self._regions.items():
            if base <= address and address + size <= base + len(data):
                offset = address - base
                return data[offset : offset + size]
        return b""


def build_region(size: int, blobs: dict[int, bytes]) -> bytes:
    """造一块内存：``{偏移: 内容}``，其余填随机字节（模拟真实堆）。"""
    buffer = bytearray(os.urandom(size))
    for offset, blob in blobs.items():
        buffer[offset : offset + len(blob)] = blob
    return bytes(buffer)


class KeyfinderTests(unittest.TestCase):
    """查找逻辑本身。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-keyfinder-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, name: str = "contact.db") -> Path:
        target = self.tmp / name
        make_db(target, SALT, KEY)
        return target

    # -- 正例 --------------------------------------------------------------- #

    def test_key_near_database_path_is_found(self):
        """真机布局：库路径字符串离密钥只有几 KB → 必须找到（这是主路径）。"""
        self._db()
        region = build_region(
            4 << 20,
            {
                0x1000: b"C:\\wechat\\db_storage\\contact\\contact.db",
                0x1000 + 4096: KEY,          # 密钥在路径后 4 KB（真机实测 344 B ~ 635 KB）
            },
        )
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.keymap[SALT.hex()], KEY.hex())
        self.assertEqual(result.found[0].pid, memory.pid)

    def test_key_far_from_salt_is_still_found(self):
        """真机回归：salt 与密钥相距很远（几 MB）也必须找到 —— 锚点是路径不是 salt。"""
        self._db()
        region = build_region(
            6 << 20,
            {
                0x1000: b"C:\\wechat\\db_storage\\contact\\contact.db",
                0x1000 + 512: KEY,
                (5 << 20) + 12345: SALT,     # salt 在 5 MB 之外
            },
        )
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.keymap[SALT.hex()], KEY.hex())

    def test_multiple_databases_with_different_keys(self):
        """每库一把 key（微信 4.x 的真实形态）：两把都要找出来，各配各的 salt。"""
        make_db(self.tmp / "a.db", SALT, KEY)
        make_db(self.tmp / "b.db", SALT2, KEY2)
        region = build_region(
            4 << 20,
            {
                0x1000: b"C:\\wechat\\db_storage\\x\\a.db",
                0x1000 + 2048: KEY,
                0x100000: b"C:\\wechat\\db_storage\\x\\b.db",
                0x100000 + 2048: KEY2,
            },
        )
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.keymap[SALT.hex()], KEY.hex())
        self.assertEqual(result.keymap[SALT2.hex()], KEY2.hex())
        self.assertEqual(result.missing, [])

    def test_seed_keys_locate_the_neighbourhood_for_a_new_database(self):
        """路标快路径：拿**已知密钥**当路标，把旁边新库的密钥一起找出来。

        真机依据：同一批库的密钥在内存里是聚簇的（实测同一库的 6 次出现只落在 3 个区域，
        彼此相隔 1.6 KB），所以用已有密钥定位邻域比按路径锚点扫几十 MB 快一两个数量级。
        """
        make_db(self.tmp / "a.db", SALT, KEY)        # 已知库（路标）
        make_db(self.tmp / "b.db", SALT2, KEY2)      # 新库（要自动找出来）
        # 内存里只有两把密钥挨在一起，**没有任何路径字符串**
        region = build_region(4 << 20, {0x200000: KEY, 0x200000 + 4096: KEY2})
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(
            self.tmp,
            reader=memory,
            pids=[memory.pid],
            seed_keys={SALT.hex(): KEY.hex()},     # 只给 a.db 的密钥当路标
            use_anchors=False,                     # 故意关掉通用锚点，只验快路径
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.keymap[SALT.hex()], KEY.hex())
        self.assertEqual(result.keymap[SALT2.hex()], KEY2.hex())
        self.assertTrue(all(item.source == "seed" for item in result.found), [i.source for i in result.found])

    def test_seed_key_that_is_absent_from_memory_finds_nothing(self):
        """路标本身不在内存里（该库没被打开）时，快路径必须安静地什么也不给。"""
        make_db(self.tmp / "a.db", SALT, KEY)
        make_db(self.tmp / "b.db", SALT2, KEY2)
        region = build_region(4 << 20, {0x200000: KEY2})   # 只有 b 的密钥，没有 a 的
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(
            self.tmp, reader=memory, pids=[memory.pid], seed_keys={SALT.hex(): KEY.hex()}, use_anchors=False
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.found, [])

    def test_window_bounds_what_is_scanned(self):
        """窗口之外找不到；把 window 放大就能找到（说明边界是"扫描范围"而不是"没这个能力"）。"""
        self._db()
        # 密钥在路径之后 3 MB，而默认窗口是 2 MB
        region = build_region(8 << 20, {0x1000: b"contact.db", 0x1000 + (3 << 20): KEY})
        memory = FakeMemory({REGION_BASE: region})
        narrow = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid], window=1 << 20)
        self.assertFalse(narrow.ok)
        wide = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid], window=5 << 20)
        self.assertTrue(wide.ok, wide.error)
        self.assertEqual(wide.keymap[SALT.hex()], KEY.hex())

    def test_max_scan_caps_the_work(self):
        """锚点命中过多时必须被 max_scan 截断：截断后找不到，放开就找得到。"""
        self._db()
        region = build_region(8 << 20, {0x1000: b"contact.db", 0x1000 + (6 << 20): KEY})
        memory = FakeMemory({REGION_BASE: region})
        capped = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid], window=8 << 20, max_scan=256 << 10)
        self.assertFalse(capped.ok)
        generous = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid], window=8 << 20, max_scan=32 << 20)
        self.assertTrue(generous.ok, generous.error)

    # -- 反例（比正例更重要） ------------------------------------------------- #

    def test_anchor_without_key_finds_nothing(self):
        """内存里只有路径、没有密钥 → 找不到，并说清两种可能。"""
        self._db()
        region = build_region(4 << 20, {0x1000: b"contact.db"})
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertFalse(result.ok)
        self.assertEqual(result.found, [])
        self.assertIn("没有找到", result.error)
        self.assertIn("4.1.10", result.hint)

    def test_random_bytes_near_anchor_are_rejected(self):
        """路径旁边是随机字节时绝不能"猜一把"：HMAC 校验必须拒掉。"""
        self._db()
        region = build_region(4 << 20, {0x1000: b"contact.db", 0x1000 + 1024: os.urandom(32)})
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertFalse(result.ok)
        self.assertEqual(result.found, [])

    def test_another_databases_key_is_rejected(self):
        """别的库的密钥出现在这个库的路径旁边 → 必须拒绝（salt 才是配对依据）。"""
        make_db(self.tmp / "a.db", SALT, KEY)
        make_db(self.tmp / "b.db", SALT2, KEY2)
        region = build_region(4 << 20, {0x1000: b"a.db", 0x1000 + 1024: KEY2})
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        self.assertFalse(result.ok)
        self.assertEqual(result.found, [])

    def test_empty_memory_does_not_crash(self):
        """读不到任何东西（区域被回收）时不能崩，只能报找不到。"""
        self._db()

        class Empty(keyfinder.MemoryReader):
            pid = 999

            def regions(self):
                return [(0x70000000, 1 << 20)]

            def read(self, address, size):
                return b""

        result = keyfinder.find_keys(self.tmp, reader=Empty(), pids=[999])
        self.assertFalse(result.ok)
        self.assertEqual(result.found, [])

    # -- 参数与边界 --------------------------------------------------------- #

    def test_passphrase_mode_is_refused_explicitly(self):
        self._db()
        result = keyfinder.find_keys(self.tmp, reader=FakeMemory({}), key_type="passphrase")
        self.assertFalse(result.ok)
        self.assertIn("raw", result.error)
        self.assertIn("passphrase", result.hint)

    def test_missing_directory_reports_cleanly(self):
        result = keyfinder.find_keys(self.tmp / "does-not-exist", reader=FakeMemory({}))
        self.assertFalse(result.ok)
        self.assertIn("没有找到任何 .db", result.error)

    def test_no_wechat_process_reports_cleanly(self):
        self._db()
        original = keyfinder.list_wechat_processes
        keyfinder.list_wechat_processes = lambda names=None: []
        try:
            result = keyfinder.find_keys(self.tmp)
        finally:
            keyfinder.list_wechat_processes = original
        self.assertFalse(result.ok)
        self.assertIn("没有找到正在运行的微信进程", result.error)

    def test_result_to_dict_is_json_serialisable(self):
        self._db()
        region = build_region(4 << 20, {0x1000: b"contact.db", 0x1000 + 2048: KEY})
        memory = FakeMemory({REGION_BASE: region})
        result = keyfinder.find_keys(self.tmp, reader=memory, pids=[memory.pid])
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("found", payload)
        self.assertEqual(json.loads(payload)["found"][0]["salt"], SALT.hex())

    def test_anchors_include_path_name_stem_and_directory(self):
        """锚点要同时给出完整路径/文件名/词干/上级目录 —— 真机上哪个离得近用哪个。"""
        page = keyfinder._read_page_one(self._db(), 4)
        anchors = keyfinder._anchor_bytes(page)
        self.assertIn("contact.db", anchors)
        self.assertIn("contact", anchors)
        self.assertTrue(any("contact.db" in item and "\\" in item for item in anchors) or len(anchors) >= 3)

    def test_coalesce_merges_overlapping_windows(self):
        spans = keyfinder._coalesce([1000, 1100, 10000], window=200)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0][0], 800)
        self.assertEqual(spans[1][0], 9800)

    # -- keymap 读写 --------------------------------------------------------- #

    def test_keymap_merge_and_load(self):
        target = self.tmp / "keymap.json"
        keyfinder.save_keymap(target, {SALT.hex(): KEY.hex()})
        keyfinder.save_keymap(target, {SALT2.hex(): KEY2.hex()})
        table = keyfinder.load_keymap(target)
        self.assertEqual(set(table), {SALT.hex(), SALT2.hex()})
        keyfinder.save_keymap(target, {SALT.hex(): KEY2.hex()})
        self.assertEqual(keyfinder.load_keymap(target)[SALT.hex()], KEY2.hex())

    def test_keymap_load_tolerates_garbage(self):
        target = self.tmp / "broken.json"
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(keyfinder.load_keymap(target), {})
        self.assertEqual(keyfinder.load_keymap(self.tmp / "missing.json"), {})

    def test_save_keymap_without_merge_replaces(self):
        target = self.tmp / "keymap.json"
        keyfinder.save_keymap(target, {SALT.hex(): KEY.hex()})
        keyfinder.save_keymap(target, {SALT2.hex(): KEY2.hex()}, merge=False)
        self.assertEqual(set(keyfinder.load_keymap(target)), {SALT2.hex()})


class EnsureTests(unittest.TestCase):
    """``ensure_keys``：先看有没有 key，缺了才去读。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-ensure-"))
        self.keymap = self.tmp / "keymap.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_covered_returns_immediately_without_touching_memory(self):
        """密钥齐备时必须**不碰内存**（正常使用时的开销要可以忽略）。"""
        make_db(self.tmp / "contact.db", SALT, KEY)
        keyfinder.save_keymap(self.keymap, {SALT.hex(): KEY.hex()})

        class Exploding(keyfinder.MemoryReader):
            pid = 1

            def regions(self):
                raise AssertionError("密钥已齐备时不该扫描内存")

        result = keyfinder.ensure_keys(self.tmp, keymap_path=self.keymap, reader=Exploding())
        self.assertTrue(result.ok)
        self.assertEqual(result.databases, 1)
        self.assertEqual(result.covered, 1)
        self.assertEqual(result.extracted, 0)
        self.assertEqual(result.missing, [])
        self.assertFalse(result.written)

    def test_missing_key_is_extracted_and_written(self):
        """缺密钥时去内存里读，并把合并后的 keymap 写回。"""
        make_db(self.tmp / "a.db", SALT, KEY)
        make_db(self.tmp / "b.db", SALT2, KEY2)
        keyfinder.save_keymap(self.keymap, {SALT.hex(): KEY.hex()})   # 只有 a
        region = build_region(4 << 20, {0x200000: KEY, 0x200000 + 4096: KEY2})
        memory = FakeMemory({REGION_BASE: region})

        result = keyfinder.ensure_keys(
            self.tmp, keymap_path=self.keymap, reader=memory, pids=[memory.pid], use_anchors=False
        )
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.covered, 2)
        self.assertEqual(result.extracted, 1)
        self.assertTrue(result.written)
        table = keyfinder.load_keymap(self.keymap)
        self.assertEqual(table[SALT2.hex()], KEY2.hex())

    def test_check_only_does_not_extract_or_write(self):
        make_db(self.tmp / "a.db", SALT, KEY)
        result = keyfinder.ensure_keys(self.tmp, keymap_path=self.keymap, extract=False)
        self.assertFalse(result.ok)
        self.assertIn("只检查不提取", result.detail)
        self.assertFalse(self.keymap.exists())

    def test_seed_paths_are_merged(self):
        """多个 keymap 来源（--out / 环境 / 工作目录）要合并，别互相覆盖。"""
        make_db(self.tmp / "a.db", SALT, KEY)
        make_db(self.tmp / "b.db", SALT2, KEY2)
        keyfinder.save_keymap(self.keymap, {SALT.hex(): KEY.hex()})
        extra = self.tmp / "other.json"
        keyfinder.save_keymap(extra, {SALT2.hex(): KEY2.hex()})
        result = keyfinder.ensure_keys(self.tmp, keymap_path=self.keymap, seed_paths=[extra], extract=False)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.covered, 2)

    def test_empty_root_reports_cleanly(self):
        result = keyfinder.ensure_keys(self.tmp / "nope", keymap_path=self.keymap)
        self.assertFalse(result.ok)
        self.assertIn("没有找到任何 .db", result.detail)


class VerifyTests(unittest.TestCase):
    """校验函数本身（算法正确性的根）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atuowexin-verify-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verify_accepts_the_true_key_and_rejects_others(self):
        db = self.tmp / "x.db"
        make_db(db, SALT, KEY)
        page = keyfinder._read_page_one(db, 4)
        self.assertTrue(keyfinder._verify(page, KEY, 4))
        self.assertFalse(keyfinder._verify(page, KEY2, 4))
        self.assertFalse(keyfinder._verify(page, KEY[:-1], 4))

    def test_page_one_geometry_matches_sqlcipher_v4(self):
        db = self.tmp / "y.db"
        make_db(db, SALT, KEY)
        page = keyfinder._read_page_one(db, 4)
        self.assertEqual(len(page.ciphertext), PAGE - RESERVE - 16)
        self.assertEqual(len(page.iv), 16)
        self.assertEqual(len(page.hmac), 64)
        self.assertEqual(page.salt, SALT)


if __name__ == "__main__":
    unittest.main()
