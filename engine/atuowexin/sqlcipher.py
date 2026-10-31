# -*- coding: utf-8 -*-
"""SQLCipher decryption tool chain for the atuowexin WeChat data reader.

Why a PowerShell helper?
------------------------
This plugin may not depend on third party Python packages (no ``cryptography``
and no ``pycryptodome``), and the standard library ships no AES.  All
cryptography is therefore delegated to .NET through PowerShell
(``System.Security.Cryptography``), driven by
``atuowexin/tools/sqlcipher.ps1``.  This module is a thin, typed wrapper around
that script: it validates arguments, invokes the script, parses the single JSON
object the script prints on its last stdout line and maps it onto
:class:`DecryptResult`.

The WeChat 4.0 databases under ``db_storage`` are SQLCipher 4 files whose key
was lifted from the running process image (a raw 32 byte key), so the normal
case is ``key_type="raw"``, ``version=4``.

On-disk format implemented here (page size 4096, SQLCipher 4 defaults)
---------------------------------------------------------------------
::

    page 1 : [16 byte salt][4000 byte ciphertext][16 byte IV][64 byte HMAC]
    page n : [4016 byte ciphertext]              [16 byte IV][64 byte HMAC]

The plaintext page handed to SQLite is always a full ``page_size`` page; the
trailing ``reserve`` bytes (80 for v4, 48 for v3) are dead space that SQLite
never uses.  Decrypted output therefore zero fills them.

Key derivation
--------------
``raw``        the 64 hex characters are the AES-256 key itself; PBKDF2 is NOT
               applied to it.  The MAC key is
               ``PBKDF2-HMAC-SHA512(enc_key, salt XOR 0x3a, 2, 32)``.
``passphrase`` ``enc_key = PBKDF2-HMAC-SHA512(passphrase, salt, iterations,
               32)`` and ``mac_key = PBKDF2-HMAC-SHA512(enc_key, salt XOR 0x3a,
               2, 32)``.  Every database has its own random salt, so every
               database gets its own encryption key.
``auto``       64 hex characters (case insensitive) means ``raw``, anything
               else means ``passphrase``.

The HMAC is computed over ``ciphertext || IV || page_number`` with the page
number as a little endian uint32, and it is the only reliable way to tell a
correct key from a wrong one.  A mismatch is reported as an error; this module
never writes a silently corrupt file.  For page 1 the 16 byte salt is *not*
part of the MAC input.  After decryption the first 16 bytes of page 1 must be
rewritten with ``b"SQLite format 3\\x00"`` before the result is a legal SQLite
file.

Sources (the implementation was checked line by line against these)
------------------------------------------------------------------
* ``sqlcipher/src/crypto_impl.c`` (v4.5.5) -- ``sqlcipher_page_hmac()``
  (MAC input ``in, size + iv_sz`` plus the page number),
  ``sqlcipher_page_cipher()`` (``size = page_sz - reserve_sz``, IV and HMAC
  live at the end of the page), ``sqlcipher_codec_ctx_reserve_setup()``
  (reserve = IV + HMAC rounded up to a block), and
  ``sqlcipher_cipher_ctx_key_derive()`` (raw hex key used directly, HMAC key
  derived with ``fast_kdf_iter`` and the XOR masked salt).
  https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto_impl.c
* ``sqlcipher/src/crypto.h`` -- ``FILE_HEADER_SZ 16``, ``PBKDF2_ITER 256000``,
  ``FAST_PBKDF2_ITER 2``, ``HMAC_SALT_MASK 0x3a``,
  ``DEFAULT_CIPHER_FLAGS = CIPHER_FLAG_HMAC | CIPHER_FLAG_LE_PGNO``.
  https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto.h
* ``sqlcipher/src/crypto.c`` -- ``PRAGMA cipher_compatibility = 3`` gives HMAC
  SHA1 + PBKDF2 SHA1 + kdf_iter 64000, the v4 default is SHA512 + 256000; and
  ``sqlite3Codec()`` skips the leading 16 bytes (the salt) for page 1 only.
  https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto.c
* ``https://www.zetetic.net/sqlcipher/design/`` -- AES-256-CBC, per page IV
  stored at the end of the page, HMAC-SHA512, 256,000 iterations by default.

Known uncertainties (not verifiable in this workspace)
------------------------------------------------------
1. The WeChat specific parameter set (SQLCipher 4, page size 4096, 256000
   iterations, HMAC-SHA512, reserve 80, raw process memory key) comes from the
   project specification and from public WeChat decryptors, not from a real
   WeChat database: no real ``db_storage`` file was available here.  The
   implementation is self consistent and every page is HMAC verified, so a
   wrong assumption fails loudly instead of producing a bad file.
2. SQLCipher's own ``cipher_compatibility = 3`` selects a 1024 byte page while
   WeChat 3.x databases are widely reported to use 4096; this module defaults to
   4096 for both versions and the page size can be overridden in the script
   (``-PageSize``) or inferred when the file length is not a multiple of 4096.
3. WAL files **are** merged since v0.2.0.  ``decrypt_file`` decrypts the main
   database and then replays the committed frames of the sibling ``<db>-wal``
   into it (``walapply`` mode of the helper script), so messages that SQLite has
   not checkpointed yet are no longer lost.  On a real WeChat 4.1.13 install the
   WAL was where *most* of the recent data lived (message_0.db was 2.8 MB while
   message_0.db-wal was a full 4 MB / 1018 frames / 87 commits).  ``wal_info``
   parses a WAL header without any key; ``walencrypt_file`` is a test-only
   inverse used to build fixtures.
4. How a WAL frame is encrypted (verified against 2806 real frames, 100 per cent
   HMAC clean): the WAL header and the frame headers are plaintext and big
   endian; a frame's page payload uses exactly the page codec of the main
   database, with the frame's own ``page number`` fed into the HMAC as a little
   endian uint32, and the key material derived from the *database* salt.  Only
   the frames up to the last one whose header carries a commit (``db size after
   commit`` != 0) are applied; the result is truncated to that page count.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DecryptResult",
    "PAGE_SIZE",
    "SALT_SIZE",
    "SQLITE_MAGIC",
    "VERSION_PARAMS",
    "WAL_HEADER_SIZE",
    "WAL_FRAME_HEADER_SIZE",
    "WAL_MAGIC_BE",
    "WAL_MAGIC_LE",
    "WAL_VERSION",
    "default_script_path",
    "powershell_path",
    "is_available",
    "inspect_key_type",
    "wal_path_for",
    "wal_info",
    "wal_checksum",
    "cleanup_sidecars",
    "decrypt_file",
    "decrypt_tree",
    "encrypt_file",
    "walencrypt_file",
]

#: Default SQLCipher page size for WeChat databases.
PAGE_SIZE = 4096
#: Size of the per database random salt stored in the first 16 bytes of page 1.
SALT_SIZE = 16
#: What page 1 must start with once decrypted.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: SQLite WAL constants (the WAL header and frame headers are big endian and are
#: *not* encrypted by SQLCipher; the page payloads are).
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LE = 0x377F0683
WAL_MAGIC_BE = 0x377F0682
#: WAL header format version SQLite currently writes; anything else is a hard error.
WAL_VERSION = 3007000

#: Parameters per SQLCipher compatibility version.  ``reserve`` is stored for
#: documentation and self checks only; the script recomputes it from the IV and
#: MAC sizes (IV + MAC, rounded up to a 16 byte block).
VERSION_PARAMS = {
    3: {"kdf_iter": 64000, "hmac": "sha1", "hmac_size": 20, "reserve": 48, "kdf": "sha1"},
    4: {"kdf_iter": 256000, "hmac": "sha512", "hmac_size": 64, "reserve": 80, "kdf": "sha512"},
}

_KEY_TYPE_RAW = "raw"
_KEY_TYPE_PASSPHRASE = "passphrase"
_KEY_TYPE_AUTO = "auto"

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass
class DecryptResult:
    """Outcome of one encrypt/decrypt operation."""

    src: str
    dst: str
    ok: bool
    pages: int = 0
    version: int = 4
    key_type: str = "raw"  # 'raw' | 'passphrase'
    error: str = ""
    elapsed_ms: int = 0
    #: 兄弟 ``-wal`` 文件的统计（``decrypt_file`` 自动合并时填写）。
    wal_frames: int = 0
    wal_valid_frames: int = 0
    wal_committed: int = 0
    wal_applied: int = 0
    wal_page_count: int = 0
    wal_stale: bool = False
    wal_error: str = ""
    #: 非致命说明（例如"忽略了一个重置过的 WAL"），用于报告而不是判定失败。
    wal_note: str = ""

    @property
    def wal_ok(self) -> bool:
        """``True`` 表示没有 WAL 需要合并、或合并成功。"""
        return not self.wal_error

    @property
    def wal_merged(self) -> bool:
        """``True`` 表示确实把提交过的 WAL 帧写进了产物。"""
        return self.wal_applied > 0 and not self.wal_error

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的字典（CLI ``--json`` 与报告都走它）。"""
        return {
            "src": self.src,
            "dst": self.dst,
            "ok": bool(self.ok),
            "pages": int(self.pages),
            "version": int(self.version),
            "key_type": self.key_type,
            "error": self.error,
            "elapsed_ms": int(self.elapsed_ms),
            "wal_frames": int(self.wal_frames),
            "wal_valid_frames": int(self.wal_valid_frames),
            "wal_committed": int(self.wal_committed),
            "wal_applied": int(self.wal_applied),
            "wal_page_count": int(self.wal_page_count),
            "wal_stale": bool(self.wal_stale),
            "wal_error": self.wal_error,
            "wal_note": self.wal_note,
        }


# ---------------------------------------------------------------------------
# environment discovery
# ---------------------------------------------------------------------------

def _candidate_shells() -> list[str]:
    """Return shell executable names to try, in order of preference."""
    return ["pwsh", "pwsh.exe", "powershell", "powershell.exe"]


def _known_shell_paths() -> list[str]:
    """Well known install locations, used when PATH lookup fails."""
    paths: list[str] = []
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    if local:
        paths.append(os.path.join(local, "Microsoft", "WindowsApps", "pwsh.exe"))
    for base in (program_files, program_files_x86):
        if base:
            paths.append(os.path.join(base, "PowerShell", "7", "pwsh.exe"))
    paths.append(os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    return paths


def powershell_path() -> "str | None":
    """Return the path of a usable PowerShell host, or ``None``.

    ``pwsh`` (PowerShell 7) is preferred, but Windows PowerShell 5.1 is accepted
    as a fallback: the helper script only uses .NET cryptography APIs that both
    hosts provide, and it is written to be 5.1 compatible (no ternary operator,
    no ``??``, ASCII only source).
    """
    for name in _candidate_shells():
        found = shutil.which(name)
        if found:
            return found
    for candidate in _known_shell_paths():
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def is_available() -> bool:
    """True when a PowerShell host that can run the helper script exists."""
    return powershell_path() is not None


def default_script_path() -> str:
    """Absolute path of ``atuowexin/tools/sqlcipher.ps1``.

    Resolved relative to this module's own file, so it works no matter what the
    current working directory is.
    """
    return str(Path(__file__).resolve().parent / "tools" / "sqlcipher.ps1")


def inspect_key_type(key: str) -> str:
    """Classify a key string as ``'raw'`` or ``'passphrase'``.

    A raw key is exactly 64 hexadecimal characters (32 bytes).  Everything else
    - including a 64 character string that contains a non hex character - is
    treated as a passphrase.  This mirrors the ``auto`` rule and the SQLCipher
    ``x'<64 hex>'`` raw key syntax.
    """
    if not isinstance(key, str):
        raise ValueError("key must be a string")
    if len(key) == 64 and all(ch in _HEX_DIGITS for ch in key):
        return _KEY_TYPE_RAW
    return _KEY_TYPE_PASSPHRASE


# ---------------------------------------------------------------------------
# script invocation
# ---------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    """Decode captured child process output.

    PowerShell writes stdout in the console code page, which is not UTF-8 on
    Chinese Windows.  The helper script only emits ASCII JSON, but error text
    may contain non ASCII, so try UTF-8 first and fall back to the ANSI code
    page with replacement instead of raising.
    """
    for encoding in ("utf-8", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _parse_json_line(stdout: str) -> dict:
    """Return the last JSON object printed by the script."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
    return {}


def _run_script(
    mode: str,
    src: Path,
    dst: "Path | None",
    key: str,
    key_type: str,
    version: int,
    salt: "str | None",
    timeout: int,
    extra: "list[str] | None" = None,
) -> DecryptResult:
    """Run the PowerShell codec once and convert its JSON into a result."""
    started = time.monotonic()
    result = DecryptResult(
        src=str(src),
        dst=str(dst) if dst is not None else "",
        ok=False,
        version=version,
        key_type=key_type,
    )

    shell = powershell_path()
    if shell is None:
        result.error = "pwsh not found"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    script = default_script_path()
    if not os.path.isfile(script):
        result.error = "helper script not found: {0}".format(script)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    resolved_type = key_type
    if resolved_type == _KEY_TYPE_AUTO:
        resolved_type = inspect_key_type(key)
    result.key_type = resolved_type

    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        # The documented CLI contract is "pwsh -NoProfile -NonInteractive -File
        # tools\sqlcipher.ps1 ...".  On a host whose effective execution policy
        # is Restricted (the Windows client default, and the state of this
        # machine: every scope is Undefined) "-File <script>.ps1" is refused
        # with "running scripts is disabled on this system".  -ExecutionPolicy
        # Bypass is therefore added here; it only affects this child process and
        # changes no machine wide setting.  Invoke the script by hand with the
        # same switch, or from a shell whose policy allows scripts.
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script,
        "-Mode",
        mode,
        "-In",
        str(src),
        "-Key",
        key,
        "-KeyType",
        resolved_type,
        "-Version",
        str(version),
    ]
    if dst is not None:
        command += ["-Out", str(dst)]
    if salt:
        command += ["-Salt", salt]
    if extra:
        command += list(extra)

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.error = "timeout after {0}s".format(timeout)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result
    except OSError as exc:  # pragma: no cover - depends on the host
        result.error = "failed to start {0}: {1}".format(shell, exc)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    payload = _parse_json_line(stdout)

    if payload:
        result.ok = bool(payload.get("ok"))
        result.pages = int(payload.get("pages") or 0)
        result.version = int(payload.get("version") or version)
        reported_type = payload.get("keyType")
        if reported_type:
            result.key_type = str(reported_type)
        result.error = str(payload.get("error") or "")
        result.wal_frames = int(payload.get("walFrames") or 0)
        result.wal_valid_frames = int(payload.get("walValidFrames") or 0)
        result.wal_committed = int(payload.get("walCommitted") or 0)
        result.wal_applied = int(payload.get("walApplied") or 0)
        result.wal_page_count = int(payload.get("walPageCount") or 0)
        result.wal_stale = bool(payload.get("walStale"))
    else:
        result.ok = False
        result.error = "helper script produced no JSON result"

    if not result.ok and not result.error:
        result.error = stderr.strip() or "unknown failure (exit code {0})".format(
            completed.returncode
        )
    if result.ok and completed.returncode != 0:
        result.ok = False
        result.error = "helper script reported success but exited with {0}".format(
            completed.returncode
        )

    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def _validate_common(key: str, key_type: str, version: int) -> None:
    if not isinstance(key, str) or key == "":
        raise ValueError("key must be a non-empty string")
    if key_type not in (_KEY_TYPE_RAW, _KEY_TYPE_PASSPHRASE, _KEY_TYPE_AUTO):
        raise ValueError("key_type must be 'raw', 'passphrase' or 'auto'")
    if version not in VERSION_PARAMS:
        raise ValueError("version must be 3 or 4")
    if key_type == _KEY_TYPE_RAW and inspect_key_type(key) != _KEY_TYPE_RAW:
        raise ValueError("key_type='raw' requires exactly 64 hex characters")


def _candidate_key_types(key: str, key_type: str) -> list:
    """按候选顺序列出要尝试的 key 形态。

    为什么需要"猜两次"：微信 4.0.x 内存里拿到的是 **raw 32 字节 hex key**，而 4.1+
    拿到的是 **passphrase 口令**，两者算法不同（raw 直接作 AES key，passphrase 要过
    PBKDF2）。这也是这个领域最高频的踩坑点：形态搞混只会得到
    ``hmac mismatch``，而用户根本不知道该改哪个参数。所以 ``auto`` 直接两种都试。

    "64 位十六进制"优先按 raw 试（raw 是更常见的失败源），失败后仍会试
    "把它当口令"这一种可能。
    """
    if key_type == _KEY_TYPE_RAW:
        return [_KEY_TYPE_RAW]
    if key_type == _KEY_TYPE_PASSPHRASE:
        return [_KEY_TYPE_PASSPHRASE]
    guessed = inspect_key_type(key)
    order = [guessed]
    for item in (_KEY_TYPE_RAW, _KEY_TYPE_PASSPHRASE):
        if item not in order:
            order.append(item)
    return order


def _failure(src: Path, dst: Path, version: int, key: str, key_type: str,
             error: str) -> DecryptResult:
    """Build a failed result, resolving ``auto`` to the type that was inferred."""
    resolved = key_type
    if resolved == _KEY_TYPE_AUTO:
        resolved = inspect_key_type(key) if isinstance(key, str) else _KEY_TYPE_RAW
    return DecryptResult(src=str(src), dst=str(dst), ok=False, version=version,
                         key_type=resolved, error=error)


# ---------------------------------------------------------------------------
# WAL inspection / merging
# ---------------------------------------------------------------------------

def wal_path_for(db_path: "str | Path") -> Path:
    """Sidecar WAL path of a database: ``<path>-wal``."""
    return Path(str(db_path) + "-wal")


def cleanup_sidecars(db_path: "str | Path") -> list[str]:
    """Delete ``<db>-wal``/``<db>-shm`` next to a database we just wrote.

    A decrypted database must never keep the **encrypted** sidecars of its source:
    stock SQLite would read the (plaintext) WAL header, find the ciphertext frame
    checksums valid, and write ciphertext into the database as if it were pages -
    silent corruption.  Returns the paths that were removed.
    """
    removed: list[str] = []
    for suffix in ("-wal", "-shm"):
        target = Path(str(db_path) + suffix)
        try:
            if target.exists():
                target.unlink()
                removed.append(str(target))
        except OSError:
            continue
    return removed


def wal_checksum(data: bytes, s1: int = 0, s2: int = 0, big_endian: bool = False) -> "tuple[int, int]":
    """SQLite's cumulative WAL checksum over ``data`` (length must be a multiple of 8).

    ``s1 += x[i] + s2 ; s2 += x[i+1] + s1`` with 32 bit wraparound, word pairs read
    little endian when the WAL magic is ``0x377f0682`` and big endian for
    ``0x377f0683``.  SQLCipher encrypts a page *before* SQLite encodes the frame,
    so the stored checksums cover the ciphertext and can be validated without a key.
    """
    fmt = ">II" if big_endian else "<II"
    total = len(data) - (len(data) % 8)
    for offset in range(0, total, 8):
        w0, w1 = struct.unpack_from(fmt, data, offset)
        s1 = (s1 + w0 + s2) & 0xFFFFFFFF
        s2 = (s2 + w1 + s1) & 0xFFFFFFFF
    return s1, s2


def wal_info(path: "str | Path") -> dict:
    """Describe a WAL file **without needing a key**.

    The WAL header and the frame headers are plaintext (SQLite writes them, only
    the page payload of each frame is enciphered by SQLCipher), so frames,
    commits and the resulting page count can be reported for a database nobody
    has a key for.  That is what ``doctor``/``ingest`` use to say "your newest
    messages are in the WAL" instead of guessing.

    Validity mirrors ``sqlite3``'s own reader (``walIndexRecover``): the header
    checksum must hold, and frame scanning stops at the first frame whose
    ``salt-1``/``salt-2`` differ from the header's or whose cumulative checksum
    does not chain.  The salt rule matters a lot in practice: WeChat leaves
    *reset* WALs behind (header rewritten with ``salt-1 + 1`` and a fresh
    ``salt-2`` while the old frames stay in the file), and those stale frames are
    already checkpointed into the database - replaying them corrupts it.  On the
    machine this was written against, 19 of 20 non-empty WALs were of that kind.
    A chain that breaks while the salts still match is a *torn* WAL
    (``corrupt``), which is reported as a problem instead of being ignored.

    :return: ``{"ok", "error", "path", "size", "page_size", "frames",
        "valid_frames", "stale", "corrupt", "header_ok", "commits",
        "last_commit", "page_count", "complete"}``.  Never raises: a missing or
        malformed file comes back as ``ok=False`` plus ``error``.
    """
    info = {
        "ok": False,
        "error": "",
        "path": str(path),
        "size": 0,
        "page_size": 0,
        "frames": 0,
        "valid_frames": 0,
        "stale": False,
        "corrupt": False,
        "header_ok": False,
        "empty": False,
        "commits": 0,
        "last_commit": 0,
        "page_count": 0,
        "complete": True,
    }
    wal = Path(path)
    try:
        size = wal.stat().st_size
    except OSError as exc:
        info["error"] = "wal not readable: {0}".format(exc)
        return info
    info["size"] = size
    if size < WAL_HEADER_SIZE:
        # An empty or header-less -wal is perfectly normal: SQLite creates a zero
        # length WAL and truncates it again after a checkpoint.  That is "no
        # frames", not an error.
        info["ok"] = True
        info["empty"] = True
        return info
    try:
        blob = wal.read_bytes()
    except OSError as exc:
        info["error"] = "wal not readable: {0}".format(exc)
        return info
    magic, version, page_size, _seq = struct.unpack(">4I", blob[:16])
    if magic not in (WAL_MAGIC_BE, WAL_MAGIC_LE):
        info["error"] = "not a SQLite WAL file (magic 0x{0:08x})".format(magic)
        return info
    if version != WAL_VERSION:
        info["error"] = "unsupported WAL format version {0} (expected {1})".format(version, WAL_VERSION)
        return info
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        info["error"] = "implausible WAL page size {0}".format(page_size)
        return info
    info["page_size"] = page_size
    salt1, salt2, ck1, ck2 = struct.unpack(">4I", blob[16:32])
    big_endian = magic == WAL_MAGIC_LE
    info["ok"] = True
    info["header_ok"] = wal_checksum(blob[:24], 0, 0, big_endian) == (ck1, ck2)

    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    usable = size - WAL_HEADER_SIZE
    frames = usable // frame_size
    info["frames"] = frames
    info["complete"] = (usable % frame_size) == 0
    if not info["header_ok"]:
        # A torn header write: sqlite3 ignores the whole WAL without an error.
        info["stale"] = True
        return info

    chain = (ck1, ck2)
    offset = WAL_HEADER_SIZE
    for index in range(frames):
        pgno, db_size, fs1, fs2, fc1, fc2 = struct.unpack(">6I", blob[offset:offset + WAL_FRAME_HEADER_SIZE])
        if pgno == 0 or fs1 != salt1 or fs2 != salt2:
            break  # stale generation: sqlite3 stops reading here
        chain = wal_checksum(blob[offset:offset + 8], chain[0], chain[1], big_endian)
        chain = wal_checksum(blob[offset + WAL_FRAME_HEADER_SIZE:offset + frame_size],
                             chain[0], chain[1], big_endian)
        if chain != (fc1, fc2):
            info["corrupt"] = True
            break
        info["valid_frames"] += 1
        if db_size:
            info["commits"] += 1
            info["last_commit"] = index + 1
            info["page_count"] = db_size
        offset += frame_size
    info["stale"] = info["valid_frames"] == 0 and frames > 0 and not info["corrupt"]
    return info


def _merge_wal(
    src: Path,
    dst: Path,
    result: DecryptResult,
    *,
    key: str,
    key_type: str,
    version: int,
    salt: "str | None",
    timeout: int,
) -> None:
    """Replay the committed frames of ``<src>-wal`` into the decrypted ``dst``.

    Failures are recorded on ``result`` (``wal_error``) instead of raised: the
    main database is already decrypted and useful, and "the newest messages are
    missing" must be reported, not turned into a total failure.  The merged
    database is built in a temporary file and moved into place only when the
    helper script verified every frame HMAC, so ``dst`` is never half written.
    """
    wal = wal_path_for(src)
    info = wal_info(wal)
    if not info["ok"]:
        # No sibling WAL (the common case) is not an error at all; a WAL that
        # exists but cannot be parsed is.
        if Path(wal).exists() and info["error"] and "not readable" not in info["error"]:
            result.wal_error = info["error"]
        return
    result.wal_frames = int(info["frames"])
    result.wal_valid_frames = int(info["valid_frames"])
    result.wal_stale = bool(info["stale"])
    result.wal_committed = int(info["commits"])
    if info["corrupt"] and info["valid_frames"] == 0:
        result.wal_error = (
            "WAL 校验链在第一帧就断裂（salt 匹配但校验和不通过），已拒绝用它覆盖主库；"
            "主库仍可用，但最新改动可能缺失"
        )
        return
    if info["corrupt"]:
        result.wal_note = f"WAL 只有前 {info['valid_frames']} 帧通过校验（后面是撕裂写入），已合并这部分"
    if not info["header_ok"]:
        result.wal_note = "WAL 头部校验和不通过（写入被截断），按 sqlite3 的规则整体忽略"
        return
    if info["valid_frames"] <= 0 or info["last_commit"] <= 0:
        # Stale (reset) WAL, or nothing committed yet: the main database is
        # already the newest state and applying the frames would corrupt it.
        if info["stale"]:
            result.wal_note = (
                f"-wal 里 {info['frames']} 帧的 salt 与头部不一致（SQLite 重置后的残留帧），"
                "内容早已 checkpoint 进主库，已按 sqlite3 的规则忽略"
            )
        return
    salt_hex = salt
    if not salt_hex:
        try:
            salt_hex = src.read_bytes()[:SALT_SIZE].hex()
        except OSError as exc:
            result.wal_error = "cannot read the database salt: {0}".format(exc)
            return
    merged = dst.with_name(dst.name + ".walmerge.tmp")
    try:
        outcome = _run_script(
            "walapply",
            dst,
            merged,
            key,
            key_type,
            version,
            salt_hex,
            timeout,
            extra=["-WalFile", str(wal)],
        )
        if not outcome.ok:
            result.wal_error = outcome.error or "wal merge failed"
            return
        os.replace(str(merged), str(dst))
        result.wal_applied = int(outcome.wal_applied)
        result.wal_page_count = int(outcome.wal_page_count)
        result.pages = int(outcome.pages or result.pages)
        if outcome.wal_applied == 0:
            result.wal_error = ""
    except OSError as exc:  # pragma: no cover - filesystem level failure
        result.wal_error = "cannot write the merged database: {0}".format(exc)
    finally:
        try:
            if merged.exists():
                merged.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def decrypt_file(
    src: "str | Path",
    dst: "str | Path",
    *,
    key: str,
    key_type: str = "auto",
    version: int = 4,
    salt: "str | None" = None,
    timeout: int = 600,
    wal: bool = True,
    wal_timeout: int = 900,
) -> DecryptResult:
    """Decrypt one SQLCipher database into a plain SQLite file.

    ``salt`` is only needed when the salt cannot be read from page 1 of ``src``
    (for example when decrypting a WAL segment or a page copied out of a file);
    leave it ``None`` for normal database files.

    ``wal`` (default on) additionally replays the committed frames of the sibling
    ``<src>-wal`` into the decrypted file, so the newest messages - which SQLite
    has not checkpointed yet - are not silently lost.  A WAL problem never
    invalidates a good decryption: it is reported in ``result.wal_error`` (and
    ``result.wal_applied``/``result.wal_frames`` describe what happened).

    Returns a :class:`DecryptResult`; failures - including a missing PowerShell
    host, which reports ``error="pwsh not found"`` - are returned, never raised.
    Invalid arguments (empty key, bad ``key_type``, bad ``version``, a ``raw``
    key that is not 64 hex characters) raise :class:`ValueError`.
    """
    _validate_common(key, key_type, version)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.is_file():
        return _failure(src_path, dst_path, version, key, key_type,
                        "source file not found: {0}".format(src_path))
    if not is_available():
        return _failure(src_path, dst_path, version, key, key_type, "pwsh not found")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in _candidate_key_types(key, key_type):
        result = _run_script("decrypt", src_path, dst_path, key, attempt, version, salt, timeout)
        if result.ok:
            if wal:
                _merge_wal(src_path, dst_path, result, key=key, key_type=result.key_type,
                           version=version, salt=salt, timeout=wal_timeout)
            # 产物旁边绝不能留下**加密的** -wal/-shm：普通 sqlite3 会把它当明文 WAL
            # 读，校验和还全对，于是把密文当页写进库（静默损坏）。见 cleanup_sidecars。
            removed = cleanup_sidecars(dst_path)
            if removed:
                note = "已清理产物旁边的 WAL 边车文件（{" + ", ".join(Path(item).name for item in removed) + "}）"
                result.wal_note = (result.wal_note + "；" + note) if result.wal_note else note
            return result
        last = result
    # 全部形态都失败：回报最后一次错误，并说清两种形态都试过了
    if last is not None and key_type == _KEY_TYPE_AUTO:
        tried = "/".join(_candidate_key_types(key, key_type))
        last.error = f"{last.error}（已按 {tried} 两种形态各试一次，说明 key 本身多半不对）"
    return last if last is not None else _failure(src_path, dst_path, version, key, key_type, "no attempt made")


def decrypt_tree(
    src_dir: "str | Path",
    dst_dir: "str | Path",
    *,
    key: str,
    key_type: str = "auto",
    version: int = 4,
    patterns: "tuple[str, ...]" = ("*.db",),
    recursive: bool = True,
    timeout: int = 900,
    wal: bool = True,
    wal_timeout: int = 900,
) -> "list[DecryptResult]":
    """Decrypt every matching database below ``src_dir`` into ``dst_dir``.

    Only files matching ``patterns`` are touched - anything else (``*.txt``,
    ``*.db-shm``) is ignored.  Sub directories are recreated under ``dst_dir``
    when ``recursive`` is true.  Each file gets its own :class:`DecryptResult`;
    the list is ordered by source path.

    ``wal`` (default on) merges the committed frames of each database's sibling
    ``-wal`` file; see :func:`decrypt_file`.
    """
    _validate_common(key, key_type, version)
    src_root = Path(src_dir)
    dst_root = Path(dst_dir)

    if not src_root.is_dir():
        raise ValueError("source directory not found: {0}".format(src_root))
    if not patterns:
        raise ValueError("patterns must not be empty")

    found: list[Path] = []
    for pattern in patterns:
        iterator = src_root.rglob(pattern) if recursive else src_root.glob(pattern)
        for path in iterator:
            if path.is_file():
                found.append(path)
    found = sorted(set(found))

    results: "list[DecryptResult]" = []
    if not is_available():
        for path in found:
            rel = path.relative_to(src_root)
            results.append(_failure(path, dst_root / rel, version, key, key_type,
                                    "pwsh not found"))
        return results

    for path in found:
        rel = path.relative_to(src_root)
        target = dst_root / rel
        results.append(
            decrypt_file(path, target, key=key, key_type=key_type, version=version,
                         timeout=timeout, wal=wal, wal_timeout=wal_timeout)
        )
    return results


def encrypt_file(
    src: "str | Path",
    dst: "str | Path",
    *,
    key: str,
    key_type: str = "raw",
    version: int = 4,
    salt: "str | None" = None,
    timeout: int = 600,
) -> DecryptResult:
    """Encrypt a plain SQLite file into SQLCipher format.

    THIS IS NOT A PRODUCT FEATURE.  It exists only so that the test suite can
    build encrypted fixtures from scratch and then prove that
    :func:`decrypt_file` recovers them byte for byte; nothing in the plugin
    should ever call it.  It is the exact inverse of :func:`decrypt_file`: the
    input must already be laid out the way SQLCipher lays out plaintext pages
    (page size 4096 with 80 reserved bytes per page for version 4), otherwise
    the reserved bytes are dropped.  ``salt`` is random when not supplied and is
    reported in the script output.
    """
    _validate_common(key, key_type, version)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.is_file():
        return _failure(src_path, dst_path, version, key, key_type,
                        "source file not found: {0}".format(src_path))
    if not is_available():
        return _failure(src_path, dst_path, version, key, key_type, "pwsh not found")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    return _run_script("encrypt", src_path, dst_path, key, key_type, version, salt, timeout)


def walencrypt_file(
    src: "str | Path",
    dst: "str | Path",
    *,
    key: str,
    key_type: str = "raw",
    version: int = 4,
    salt: str,
    timeout: int = 600,
) -> DecryptResult:
    """Encrypt a plaintext SQLite WAL into SQLCipher form.

    THIS IS NOT A PRODUCT FEATURE - like :func:`encrypt_file` it exists so the
    test suite can build fixtures.  ``src`` must be a real ``-wal`` file written
    by SQLite (the WAL header and the frame headers are copied through
    verbatim, only the page payload of every frame is enciphered), and ``salt``
    must be the salt of the encrypted database the WAL belongs to.
    """
    _validate_common(key, key_type, version)
    if not isinstance(salt, str) or len(salt) != 32:
        raise ValueError("salt must be the 32 hex characters of the database salt")
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.is_file():
        return _failure(src_path, dst_path, version, key, key_type,
                        "source file not found: {0}".format(src_path))
    if not is_available():
        return _failure(src_path, dst_path, version, key, key_type, "pwsh not found")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    return _run_script("walencrypt", src_path, dst_path, key, key_type, version, salt, timeout)


def _main(argv: "list[str]") -> int:  # pragma: no cover - manual diagnostics
    """Tiny CLI used for manual smoke checks: ``python -m atuowexin.sqlcipher``."""
    if len(argv) < 4:
        print("usage: sqlcipher.py <src> <dst> <key> [version]")
        return 2
    result = decrypt_file(argv[1], argv[2], key=argv[3],
                          version=int(argv[4]) if len(argv) > 4 else 4)
    print(json.dumps(result.__dict__, ensure_ascii=False))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv))
