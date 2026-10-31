# -*- coding: utf-8 -*-
"""Tests for :mod:`atuowexin.sqlcipher` - the PowerShell backed SQLCipher codec.

Run from the ``engine`` directory (the interpreter is the one shipped with the
workspace)::

    set PYTHONUTF8=1
    <python.exe> -m unittest discover -s tests -p "test_*.py" -v

The tests that actually exercise the codec need a PowerShell host.  They are
skipped, not failed, with :func:`unittest.skipUnless` when neither ``pwsh`` nor
``powershell`` can be found.

Fixture note
------------
The round trip can only be byte exact when the plaintext database is laid out
the way SQLCipher lays out plaintext pages, that is a page size of 4096 with a
reserved area at the end of every page (80 bytes for SQLCipher 4, 48 for
SQLCipher 3) which SQLite never writes data into.  ``make_plain_sqlite`` builds
exactly such a database by seeding page 1 with a valid header whose "reserved
space per page" byte (offset 20) is set before SQLite writes anything.  This is
what a real decrypted SQLCipher database looks like.
"""

import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - very old interpreters
        pass

_ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from atuowexin import sqlcipher  # noqa: E402

# 64 hex characters -> a raw 32 byte key, the shape of a key lifted from the
# WeChat process memory.
RAW_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90" * 2
WRONG_RAW_KEY = "ffeeddccbbaa99887766554433221100" * 2
PASSPHRASE = "correct horse battery staple 微信"
PASSPHRASE_SALT = "00112233445566778899aabbccddeeff"

ROWS = 400
PAD = "x" * 200


def make_plain_sqlite(path, rows=ROWS, reserve=80, page_size=4096):
    """Create a multi page SQLite database with ``reserve`` reserved bytes.

    The reserved byte count is written into the page 1 header before SQLite
    opens the file, so SQLite honours it for every page it writes afterwards.
    """
    header = bytearray(page_size)
    header[0:16] = b"SQLite format 3\x00"
    header[16:18] = struct.pack(">H", page_size)   # page size
    header[18] = 1                                  # write format
    header[19] = 1                                  # read format
    header[20] = reserve                            # reserved bytes per page
    header[21] = 64                                 # max embedded payload
    header[22] = 32                                 # min embedded payload
    header[23] = 32                                 # leaf payload
    header[24:28] = struct.pack(">I", 1)            # change counter
    header[28:32] = struct.pack(">I", 1)            # database size in pages
    header[44:48] = struct.pack(">I", 4)            # schema format
    header[56:60] = struct.pack(">I", 1)            # UTF-8
    header[92:96] = struct.pack(">I", 1)            # version valid for
    header[96:100] = struct.pack(">I", 3045000)     # sqlite version number
    header[100] = 0x0D                              # leaf table b-tree page
    header[101:103] = struct.pack(">H", 0)          # no free blocks
    header[103:105] = struct.pack(">H", 0)          # no cells
    header[105:107] = struct.pack(">H", page_size - reserve)  # content start
    header[107] = 0                                 # fragmented free bytes

    path = Path(path)
    path.write_bytes(bytes(header))

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE msg(id INTEGER PRIMARY KEY, body TEXT)")
        for index in range(rows):
            connection.execute(
                "INSERT INTO msg(id, body) VALUES(?, ?)",
                (index, "第{0}行 中文内容 {1}{0}".format(index, PAD)),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def read_rows(path):
    """Return ``(row_count, ordered_rows)`` for the ``msg`` table."""
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT id, body FROM msg ORDER BY id").fetchall()
    finally:
        connection.close()
    return len(rows), rows


def count_pages(path, page_size=4096):
    return Path(path).stat().st_size // page_size


@unittest.skipUnless(sqlcipher.is_available(), "no PowerShell host (pwsh/powershell) available")
class CodecTestCase(unittest.TestCase):
    """Shared temporary directory and plaintext fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atuowexin_sqlcipher_"))
        cls.plain_v4 = make_plain_sqlite(cls.tmp / "plain_v4.db", reserve=80)
        cls.plain_v3 = make_plain_sqlite(cls.tmp / "plain_v3.db", reserve=48)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.tmp), ignore_errors=True)


class TestRoundTrip(CodecTestCase):
    def test_roundtrip_v4_raw_key(self):
        """Encrypt then decrypt a multi page database and compare byte for byte."""
        encrypted = self.tmp / "enc_v4.db"
        decrypted = self.tmp / "dec_v4.db"
        for stale in (encrypted, decrypted):
            if stale.exists():
                stale.unlink()

        self.assertGreater(count_pages(self.plain_v4), 2, "fixture must span >2 pages")

        result = sqlcipher.encrypt_file(self.plain_v4, encrypted, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.key_type, "raw")
        self.assertEqual(result.version, 4)
        self.assertGreater(result.pages, 2)

        blob = encrypted.read_bytes()
        self.assertEqual(len(blob), self.plain_v4.stat().st_size)
        self.assertNotIn(b"SQLite format 3", blob, "encrypted file must not expose the header")

        result = sqlcipher.decrypt_file(encrypted, decrypted, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.pages, count_pages(self.plain_v4))
        self.assertGreater(result.elapsed_ms, -1)

        self.assertEqual(decrypted.read_bytes(), self.plain_v4.read_bytes())

        expected = read_rows(self.plain_v4)
        self.assertEqual(expected[0], ROWS)
        self.assertEqual(read_rows(decrypted), expected)

        connection = sqlite3.connect(str(decrypted))
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_roundtrip_v3_raw_key(self):
        """Same round trip with the SQLCipher 3 parameter set (SHA1, reserve 48)."""
        encrypted = self.tmp / "enc_v3.db"
        decrypted = self.tmp / "dec_v3.db"
        for stale in (encrypted, decrypted):
            if stale.exists():
                stale.unlink()

        self.assertGreater(count_pages(self.plain_v3), 2)

        result = sqlcipher.encrypt_file(self.plain_v3, encrypted, key=RAW_KEY,
                                        key_type="raw", version=3)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version, 3)
        self.assertNotIn(b"SQLite format 3", encrypted.read_bytes())

        result = sqlcipher.decrypt_file(encrypted, decrypted, key=RAW_KEY,
                                        key_type="raw", version=3)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(decrypted.read_bytes(), self.plain_v3.read_bytes())
        self.assertEqual(read_rows(decrypted), read_rows(self.plain_v3))

    def test_roundtrip_passphrase(self):
        """A passphrase plus an explicit salt must survive the round trip."""
        encrypted = self.tmp / "enc_pass.db"
        decrypted = self.tmp / "dec_pass.db"
        for stale in (encrypted, decrypted):
            if stale.exists():
                stale.unlink()

        result = sqlcipher.encrypt_file(self.plain_v4, encrypted, key=PASSPHRASE,
                                        key_type="passphrase", version=4,
                                        salt=PASSPHRASE_SALT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.key_type, "passphrase")
        self.assertEqual(encrypted.read_bytes()[:16], bytes.fromhex(PASSPHRASE_SALT))
        self.assertNotIn(b"SQLite format 3", encrypted.read_bytes())

        result = sqlcipher.decrypt_file(encrypted, decrypted, key=PASSPHRASE,
                                        key_type="passphrase", version=4,
                                        salt=PASSPHRASE_SALT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(read_rows(decrypted), read_rows(self.plain_v4))
        self.assertEqual(decrypted.read_bytes(), self.plain_v4.read_bytes())

    def test_auto_key_type_is_used(self):
        """The default key_type='auto' must pick raw for a 64 hex character key."""
        encrypted = self.tmp / "enc_auto.db"
        decrypted = self.tmp / "dec_auto.db"
        for stale in (encrypted, decrypted):
            if stale.exists():
                stale.unlink()

        result = sqlcipher.encrypt_file(self.plain_v4, encrypted, key=RAW_KEY, version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.key_type, "raw")

        result = sqlcipher.decrypt_file(encrypted, decrypted, key=RAW_KEY, version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.key_type, "raw")
        self.assertEqual(read_rows(decrypted), read_rows(self.plain_v4))


class TestFailures(CodecTestCase):
    def _encrypt_fixture(self, name="enc_fail.db", key=RAW_KEY, salt=None, version=4):
        target = self.tmp / name
        result = sqlcipher.encrypt_file(self.plain_v4, target, key=key, key_type="raw",
                                        version=version, salt=salt)
        self.assertTrue(result.ok, result.error)
        return target

    def test_wrong_key_fails(self):
        """A wrong key must fail on the page HMAC, never produce a broken file."""
        encrypted = self._encrypt_fixture()
        target = self.tmp / "dec_wrong_key.db"
        if target.exists():
            target.unlink()

        result = sqlcipher.decrypt_file(encrypted, target, key=WRONG_RAW_KEY,
                                        key_type="raw", version=4)
        self.assertFalse(result.ok)
        lowered = result.error.lower()
        self.assertTrue("hmac" in lowered or "key" in lowered,
                        "unexpected error text: {0!r}".format(result.error))
        self.assertIn("hmac", lowered)

    def test_wrong_salt_fails(self):
        """Passing the wrong salt must fail as well (it changes both keys)."""
        encrypted = self._encrypt_fixture(name="enc_salt.db", salt=PASSPHRASE_SALT)
        target = self.tmp / "dec_wrong_salt.db"
        if target.exists():
            target.unlink()

        result = sqlcipher.decrypt_file(encrypted, target, key=RAW_KEY, key_type="raw",
                                        version=4, salt="ffeeddccbbaa99887766554433221100")
        self.assertFalse(result.ok)
        self.assertIn("hmac", result.error.lower())

    def test_tampered_ciphertext_fails(self):
        """Flipping one ciphertext bit must be detected by the HMAC check."""
        encrypted = self._encrypt_fixture(name="enc_tamper.db")
        blob = bytearray(encrypted.read_bytes())
        blob[4096 + 64] ^= 0x01  # inside page 2 ciphertext
        tampered = self.tmp / "tampered.db"
        tampered.write_bytes(bytes(blob))

        target = self.tmp / "dec_tampered.db"
        if target.exists():
            target.unlink()

        result = sqlcipher.decrypt_file(tampered, target, key=RAW_KEY, key_type="raw",
                                        version=4)
        self.assertFalse(result.ok)
        self.assertIn("hmac", result.error.lower())

    def test_invalid_arguments_raise(self):
        with self.assertRaises(ValueError):
            sqlcipher.decrypt_file(self.plain_v4, self.tmp / "x.db", key="")
        with self.assertRaises(ValueError):
            sqlcipher.decrypt_file(self.plain_v4, self.tmp / "x.db", key=RAW_KEY,
                                   key_type="nonsense")
        with self.assertRaises(ValueError):
            sqlcipher.decrypt_file(self.plain_v4, self.tmp / "x.db", key=RAW_KEY,
                                   version=5)
        with self.assertRaises(ValueError):
            # key_type='raw' demands exactly 64 hex characters
            sqlcipher.decrypt_file(self.plain_v4, self.tmp / "x.db", key="not-a-key",
                                   key_type="raw")

    def test_missing_source_reports_failure(self):
        result = sqlcipher.decrypt_file(self.tmp / "does_not_exist.db", self.tmp / "y.db",
                                        key=RAW_KEY, key_type="raw")
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)


class TestDecryptTree(CodecTestCase):
    def test_decrypt_tree_only_touches_db_files(self):
        """decrypt_tree walks *.db only and mirrors the directory layout."""
        src = self.tmp / "tree_src"
        dst = self.tmp / "tree_dst"
        shutil.rmtree(str(src), ignore_errors=True)
        shutil.rmtree(str(dst), ignore_errors=True)
        (src / "nested").mkdir(parents=True)

        expected = {}
        for relative in ("a.db", "b.db", "nested/c.db"):
            target = src / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            result = sqlcipher.encrypt_file(self.plain_v4, target, key=RAW_KEY,
                                            key_type="raw", version=4)
            self.assertTrue(result.ok, result.error)
            expected[relative] = count_pages(target)

        # decoys that must be ignored
        (src / "notes.txt").write_text("not a database", encoding="utf-8")
        (src / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (src / "a.db-wal").write_bytes(b"\x00" * 4096)

        results = sqlcipher.decrypt_tree(src, dst, key=RAW_KEY, key_type="raw", version=4)

        self.assertEqual(len(results), 3)
        self.assertEqual([Path(item.src).name for item in results],
                         ["a.db", "b.db", "c.db"])
        for item in results:
            self.assertTrue(item.ok, item.error)
        self.assertEqual((dst / "notes.txt").exists(), False)
        self.assertEqual((dst / "image.png").exists(), False)
        self.assertEqual((dst / "a.db-wal").exists(), False)

        self.assertEqual(read_rows(dst / "a.db"), read_rows(self.plain_v4))
        self.assertEqual(read_rows(dst / "nested" / "c.db"), read_rows(self.plain_v4))
        for relative, pages in expected.items():
            self.assertTrue((dst / relative).is_file())
            self.assertEqual((dst / relative).stat().st_size, pages * 4096)

    def test_decrypt_tree_recursive_false(self):
        """recursive=False must not descend into sub directories."""
        src = self.tmp / "flat_src"
        dst = self.tmp / "flat_dst"
        shutil.rmtree(str(src), ignore_errors=True)
        shutil.rmtree(str(dst), ignore_errors=True)
        (src / "nested").mkdir(parents=True)

        for relative in ("top.db", "nested/deep.db"):
            result = sqlcipher.encrypt_file(self.plain_v4, src / relative, key=RAW_KEY,
                                            key_type="raw", version=4)
            self.assertTrue(result.ok, result.error)

        results = sqlcipher.decrypt_tree(src, dst, key=RAW_KEY, key_type="raw",
                                         version=4, recursive=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(Path(results[0].src).name, "top.db")
        self.assertFalse((dst / "nested").exists())


class TestWalMerge(CodecTestCase):
    """WAL handling: plaintext headers, checksum chain, committed prefix, staleness.

    The fixture is a *real* SQLite WAL: a database is opened in WAL mode, rows are
    committed, and ``snap.db``/``snap.db-wal`` are copied while a second
    connection keeps the WAL alive.  Everything the engine sees is therefore
    exactly what SQLite writes.
    """

    @classmethod
    def make_wal_fixture(cls):
        """Return ``(workspace, snapshot_db, snapshot_wal, wal_only_rows)``.

        ``snapshot_*`` are copies taken **while** two connections keep the WAL
        alive: closing them would checkpoint the WAL into the main file and
        delete it, which is exactly the state we do *not* want to test.
        """
        workspace = Path(tempfile.mkdtemp(prefix="atuowexin_wal_"))
        db = workspace / "live.db"
        header = bytearray(4096)
        header[0:16] = b"SQLite format 3\x00"
        header[16:18] = struct.pack(">H", 4096)
        header[18] = 2                                   # write format: WAL
        header[19] = 2                                   # read format: WAL
        header[20] = 80                                  # reserved bytes per page
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
        header[105:107] = struct.pack(">H", 4096 - 80)
        db.write_bytes(bytes(header))

        connection = sqlite3.connect(str(db))
        keeper = None
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE msg(id INTEGER PRIMARY KEY, body TEXT)")
            for index in range(50):
                connection.execute("INSERT INTO msg(id, body) VALUES(?, ?)",
                                   (index, "early {0} {1}".format(index, "y" * 300)))
            connection.commit()
            # A second live connection stops the first close from checkpointing.
            keeper = sqlite3.connect(str(db))
            keeper.execute("SELECT count(*) FROM msg").fetchone()
            for index in range(50, 90):
                connection.execute("INSERT INTO msg(id, body) VALUES(?, ?)",
                                   (index, "late {0} {1}".format(index, "z" * 300)))
            connection.commit()
            wal = Path(str(db) + "-wal")
            if wal.stat().st_size <= 32:
                return None
            snapshot_db = workspace / "snap.db"
            snapshot_wal = workspace / "snap.db-wal"
            shutil.copy2(db, snapshot_db)
            shutil.copy2(wal, snapshot_wal)
            return workspace, snapshot_db, snapshot_wal, 40
        finally:
            connection.close()
            if keeper is not None:
                keeper.close()

    def setUp(self):
        fixture = self.make_wal_fixture()
        if fixture is None:  # pragma: no cover - the host's sqlite3 refused WAL mode
            self.skipTest("could not build a WAL fixture with this sqlite3")
        self.wal_tmp, self.plain_db, self.plain_wal, self.wal_rows = fixture
        self.enc_db = self.wal_tmp / "enc.db"
        self.enc_wal = self.wal_tmp / "enc.db-wal"
        result = sqlcipher.encrypt_file(self.plain_db, self.enc_db, key=RAW_KEY,
                                        key_type="raw", version=4, salt=PASSPHRASE_SALT)
        self.assertTrue(result.ok, result.error)
        result = sqlcipher.walencrypt_file(self.plain_wal, self.enc_wal, key=RAW_KEY,
                                           version=4, salt=PASSPHRASE_SALT)
        self.assertTrue(result.ok, result.error)

    def tearDown(self):
        shutil.rmtree(str(self.wal_tmp), ignore_errors=True)

    def _rows(self, path):
        connection = sqlite3.connect(str(path))
        try:
            return connection.execute("SELECT count(*) FROM msg").fetchone()[0]
        finally:
            connection.close()

    def _rows_or_none(self, path):
        """Row count, or ``None`` when the schema itself is not in the file yet."""
        try:
            return self._rows(path)
        except sqlite3.OperationalError:
            return None

    def test_wal_info_parses_plaintext_headers_without_a_key(self):
        """Frames, commits and page count come from plaintext big endian headers."""
        info = sqlcipher.wal_info(self.plain_wal)
        self.assertTrue(info["ok"], info["error"])
        self.assertTrue(info["header_ok"])
        self.assertEqual(info["page_size"], 4096)
        self.assertGreater(info["frames"], 0)
        self.assertEqual(info["frames"], info["valid_frames"])
        self.assertGreater(info["commits"], 0)
        self.assertFalse(info["stale"])
        self.assertFalse(info["corrupt"])
        self.assertGreater(info["page_count"], 0)

    def test_encrypted_wal_keeps_a_valid_checksum_chain(self):
        """walencrypt must chain the checksum over the ciphertext, like SQLite does."""
        info = sqlcipher.wal_info(self.enc_wal)
        self.assertTrue(info["ok"], info["error"])
        self.assertEqual(info["frames"], sqlcipher.wal_info(self.plain_wal)["frames"])
        self.assertEqual(info["valid_frames"], info["frames"])
        self.assertFalse(info["corrupt"])

    def test_decrypt_merges_the_committed_wal(self):
        """Without the merge the newest messages are simply not there."""
        plain_out = self.wal_tmp / "dec_plain.db"
        result = sqlcipher.decrypt_file(self.enc_db, plain_out, key=RAW_KEY,
                                        key_type="raw", version=4, wal=False)
        self.assertTrue(result.ok, result.error)
        unmerged = self._rows_or_none(plain_out)
        self.assertTrue(unmerged is None or unmerged < 90,
                        "the fixture must keep newest rows in the WAL, got {0}".format(unmerged))

        merged_out = self.wal_tmp / "dec_merged.db"
        result = sqlcipher.decrypt_file(self.enc_db, merged_out, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.wal_error, "", result.wal_error)
        self.assertGreater(result.wal_applied, 0)
        self.assertEqual(result.wal_applied, result.wal_valid_frames)
        self.assertEqual(self._rows(merged_out), 90)

        connection = sqlite3.connect(str(merged_out))
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()
        # page 1's in-header page count must agree with the file, or SQLite reports
        # SQLITE_CORRUPT once the header count exceeds the file size.
        blob = merged_out.read_bytes()
        self.assertEqual(struct.unpack(">I", blob[28:32])[0], merged_out.stat().st_size // 4096)

    def test_decrypt_removes_encrypted_sidecars_next_to_the_output(self):
        """A leftover encrypted -wal would be replayed by stock sqlite3 as pages."""
        target = self.wal_tmp / "dec_side.db"
        Path(str(target) + "-wal").write_bytes(b"\x00" * 64)
        Path(str(target) + "-shm").write_bytes(b"\x00" * 64)
        result = sqlcipher.decrypt_file(self.enc_db, target, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(Path(str(target) + "-wal").exists())
        self.assertFalse(Path(str(target) + "-shm").exists())
        self.assertIn("WAL", result.wal_note)

    def test_stale_wal_frames_are_ignored(self):
        """A reset WAL (header salt rotated) must never be replayed into the DB."""
        blob = bytearray(self.enc_wal.read_bytes())
        magic = struct.unpack(">I", bytes(blob[0:4]))[0]
        big = magic == sqlcipher.WAL_MAGIC_LE
        salt1 = struct.unpack(">I", bytes(blob[16:20]))[0]
        blob[16:20] = struct.pack(">I", (salt1 + 1) & 0xFFFFFFFF)   # exactly what walRestartHdr does
        ck1, ck2 = sqlcipher.wal_checksum(bytes(blob[:24]), 0, 0, big)   # a real reset rewrites these
        blob[24:28] = struct.pack(">I", ck1)
        blob[28:32] = struct.pack(">I", ck2)
        stale = self.wal_tmp / "stale.db-wal"
        stale.write_bytes(bytes(blob))
        shutil.copy2(self.enc_db, self.wal_tmp / "stale.db")

        info = sqlcipher.wal_info(stale)
        self.assertTrue(info["ok"], info["error"])
        self.assertTrue(info["header_ok"])
        self.assertTrue(info["stale"])
        self.assertEqual(info["valid_frames"], 0)

        target = self.wal_tmp / "dec_stale.db"
        result = sqlcipher.decrypt_file(self.wal_tmp / "stale.db", target, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.wal_error, "", result.wal_error)
        self.assertEqual(result.wal_applied, 0)
        self.assertTrue(result.wal_stale)
        self.assertIn("salt", result.wal_note)
        # the main database is the authority: the result must equal a WAL-less decrypt
        reference = self.wal_tmp / "dec_stale_reference.db"
        plain = sqlcipher.decrypt_file(self.wal_tmp / "stale.db", reference, key=RAW_KEY,
                                       key_type="raw", version=4, wal=False)
        self.assertTrue(plain.ok, plain.error)
        self.assertEqual(self._rows_or_none(target), self._rows_or_none(reference))

    def test_torn_frame_chain_is_reported_not_ignored(self):
        """Salts that match but a broken checksum chain means a damaged WAL."""
        blob = bytearray(self.enc_wal.read_bytes())
        blob[32 + 24 + 100] ^= 0x01          # flip one ciphertext byte of frame 1
        torn = self.wal_tmp / "torn.db-wal"
        torn.write_bytes(bytes(blob))
        info = sqlcipher.wal_info(torn)
        self.assertTrue(info["ok"], info["error"])
        self.assertTrue(info["corrupt"])
        self.assertEqual(info["valid_frames"], 0)

        shutil.copy2(self.enc_db, self.wal_tmp / "torn.db")
        target = self.wal_tmp / "dec_torn.db"
        result = sqlcipher.decrypt_file(self.wal_tmp / "torn.db", target, key=RAW_KEY,
                                        key_type="raw", version=4)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.wal_applied, 0)
        self.assertIn("校验", result.wal_error)

    def test_wal_checksum_matches_a_hand_computed_chain(self):
        """The pure Python checksum must agree with what SQLite stored."""
        blob = self.plain_wal.read_bytes()
        magic, _version, _page_size = struct.unpack(">3I", blob[:12])
        big = magic == sqlcipher.WAL_MAGIC_LE
        ck1, ck2 = struct.unpack(">2I", blob[24:32])
        self.assertEqual(sqlcipher.wal_checksum(blob[:24], 0, 0, big), (ck1, ck2))

    def test_wal_rejects_a_foreign_file(self):
        """A file that is not a WAL reports an error instead of pretending."""
        decoy = self.wal_tmp / "decoy.db-wal"
        decoy.write_bytes(b"\x00" * 4096)
        info = sqlcipher.wal_info(decoy)
        self.assertFalse(info["ok"])
        self.assertIn("not a SQLite WAL", info["error"])
        self.assertTrue(sqlcipher.wal_info(self.wal_tmp / "missing.db-wal")["frames"] == 0)


class TestPurePythonBehaviour(unittest.TestCase):
    """Behaviour that must hold even without a PowerShell host."""

    def test_inspect_key_type(self):
        self.assertEqual(sqlcipher.inspect_key_type("a" * 64), "raw")
        self.assertEqual(sqlcipher.inspect_key_type("A1B2C3D4" * 8), "raw")
        self.assertEqual(sqlcipher.inspect_key_type("my pass phrase"), "passphrase")
        self.assertEqual(sqlcipher.inspect_key_type("a" * 63), "passphrase")
        self.assertEqual(sqlcipher.inspect_key_type("g" * 64), "passphrase")
        self.assertEqual(sqlcipher.inspect_key_type(""), "passphrase")

    def test_default_script_path_points_at_the_helper(self):
        script = Path(sqlcipher.default_script_path())
        self.assertTrue(script.is_absolute())
        self.assertEqual(script.name, "sqlcipher.ps1")
        self.assertEqual(script.parent.name, "tools")
        self.assertTrue(script.is_file(), "tools/sqlcipher.ps1 must exist")

    def test_version_parameters_match_sqlcipher_defaults(self):
        self.assertEqual(sqlcipher.VERSION_PARAMS[4]["kdf_iter"], 256000)
        self.assertEqual(sqlcipher.VERSION_PARAMS[4]["hmac_size"], 64)
        self.assertEqual(sqlcipher.VERSION_PARAMS[4]["reserve"], 80)
        self.assertEqual(sqlcipher.VERSION_PARAMS[3]["kdf_iter"], 64000)
        self.assertEqual(sqlcipher.VERSION_PARAMS[3]["hmac_size"], 20)
        self.assertEqual(sqlcipher.VERSION_PARAMS[3]["reserve"], 48)

    def test_missing_powershell_returns_failure_instead_of_raising(self):
        """With no PowerShell host both entry points report ok=False."""
        original = sqlcipher.powershell_path
        sqlcipher.powershell_path = lambda: None
        try:
            self.assertFalse(sqlcipher.is_available())

            workspace = Path(tempfile.mkdtemp(prefix="atuowexin_nopwsh_"))
            try:
                source = workspace / "sample.db"
                source.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4080)

                result = sqlcipher.decrypt_file(source, workspace / "out.db", key=RAW_KEY,
                                                key_type="raw", version=4)
                self.assertIsInstance(result, sqlcipher.DecryptResult)
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "pwsh not found")
                self.assertEqual(result.pages, 0)

                tree = workspace / "tree"
                tree.mkdir()
                (tree / "one.db").write_bytes(b"encrypted")
                (tree / "two.db").write_bytes(b"encrypted")
                (tree / "readme.txt").write_text("skip me", encoding="utf-8")

                results = sqlcipher.decrypt_tree(tree, workspace / "out", key=RAW_KEY,
                                                 key_type="raw", version=4)
                self.assertEqual(len(results), 2)
                for item in results:
                    self.assertFalse(item.ok)
                    self.assertEqual(item.error, "pwsh not found")

                encrypted = sqlcipher.encrypt_file(source, workspace / "enc.db",
                                                   key=RAW_KEY, key_type="raw", version=4)
                self.assertFalse(encrypted.ok)
                self.assertEqual(encrypted.error, "pwsh not found")
            finally:
                shutil.rmtree(str(workspace), ignore_errors=True)
        finally:
            sqlcipher.powershell_path = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
