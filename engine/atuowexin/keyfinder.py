# -*- coding: utf-8 -*-
"""从**正在运行的微信进程内存**里找出数据库解密密钥（自动提取密钥）。

## 红线（写死在代码里，不是"建议"）

* 只调用 ``OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION)``、
  ``VirtualQueryEx``、``ReadProcessMemory``；
* **不注入** DLL/线程、**不写**目标进程内存、不改寄存器、不 hook、不碰微信网络协议；
* 每把候选密钥都必须通过目标库第 1 页的 **HMAC 本地校验**才会被采纳
  （与 :mod:`atuowexin.sqlcipher` 用的是同一套判据），验不过就丢弃——**绝不猜、绝不返回未验证的密钥**。

这三条决定了它和"外挂"是两个东西：它读的是你自己账号、自己机器上的内存，
做的事和你用调试器看一眼自己的数据没区别。

## 它怎么找到密钥（先定位，再验证）

盲目按 32 字节滑窗 + HMAC 校验是不可行的（几百万候选 × PBKDF2/HMAC，Python 里要跑几天）。
所以走"**先定位 salt，再验证邻居**"这条路：

1. 从目标库第 1 页读出 16 字节 salt（明文，SQLCipher 就是这么存的）；
2. 在微信进程内存里搜这 16 字节（``bytes.find``，几 GB 也就几秒）；
3. 对每个命中点，把前后一小段窗口里**按 8 字节对齐**的 32 字节片段当作候选密钥；
4. 用该库第 1 页的密文/IV/HMAC 做一次校验：``HMAC-SHA512(PBKDF2(key, salt^0x3a, 2), ct||iv||LE32(1))``
   等于存储的 HMAC 才算数。

微信会把每库的 raw key 缓存在内存里（v0.1.0 时代的 keymap 就是 17 把独立的 raw key），
所以"salt 的邻居"这个假设在 4.0.x 上成立。**4.1.10+ 是否仍然成立必须实测**——
本模块因此提供 :func:`find_keys` 之外的"轮询模式"（``watch``）：微信启动/登录的那几秒里
密钥可能只是短暂驻留，持续扫描有机会逮到。

## 诚实的局限

* 只支持 ``raw`` 形态（64 位十六进制）密钥。若是 passphrase，每验证一次就要过 256000 次
  PBKDF2，验证成本高到不可能逐候选试——本模块会明确拒绝而不是假装支持；
* 微信 ≥4.1.10 起密钥可能**根本不在内存里**（本项目文档记录过"零驻留"）。这种情况下
  :func:`find_keys` 会返回空并说明原因，而不是给一个看起来像成功的空结果；
* 需要与微信**同一个用户**（且同完整性级别）。微信若以管理员运行而本进程不是，
  ``OpenProcess`` 会失败，模块会如实报错并给出下一步。
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "KeyCandidate",
    "FindKeyResult",
    "EnsureResult",
    "KeyValidationResult",
    "MemoryReader",
    "Win32Memory",
    "NullMemory",
    "WECHAT_PROCESS_NAMES",
    "list_wechat_processes",
    "find_keys",
    "ensure_keys",
    "watch_keys",
    "validate_raw_key",
    "save_validated_key",
    "load_keymap",
    "save_keymap",
]

#: 微信 4.x / 3.x 的进程名（小写比较）。
WECHAT_PROCESS_NAMES = ("weixin.exe", "wechat.exe")

#: 候选密钥在 salt 命中点前后的搜索窗口（字节）与对齐粒度。
_WINDOW_BEFORE = 64
_WINDOW_AFTER = 96
_ALIGN = 8

#: 单次 ``ReadProcessMemory`` 的分块大小（1 MB：够大也够稳）。
_CHUNK = 1 << 20

# ── SQLCipher 参数（与 sqlcipher.py 保持一致） ──────────────────────────────
_SALT_SIZE = 16
_PAGE_SIZE = 4096
_RESERVE = {3: 48, 4: 80}
_HMAC_SIZE = {3: 20, 4: 64}
_HMAC_ALGO = {3: "sha1", 4: "sha512"}
_KDF_ALGO = {3: "sha1", 4: "sha512"}
_KDF_ITER = {3: 64000, 4: 256000}
_MAC_SALT_MASK = 0x3A


@dataclass
class KeyCandidate:
    """一把通过校验的密钥。"""

    salt: str          # 库 salt（hex，keymap 的键）
    key: str           # raw key（hex，keymap 的值）
    pid: int = 0
    address: int = 0
    db: str = ""
    #: 怎么找到的：``seed`` = 用已有密钥当路标；``anchor`` = 用库路径锚点
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "salt": self.salt,
            "key": self.key,
            "pid": self.pid,
            "address": self.address,
            "db": self.db,
            "source": self.source,
        }


@dataclass
class FindKeyResult:
    """一次查找的完整结果。"""

    ok: bool = False
    stage: str = ""
    error: str = ""
    hint: str = ""
    scanned_pids: list[int] = field(default_factory=list)
    scanned_bytes: int = 0
    databases: int = 0
    found: list[KeyCandidate] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "ok": self.ok,
            "stage": self.stage,
            "error": self.error,
            "hint": self.hint,
            "scanned_pids": self.scanned_pids,
            "scanned_bytes": self.scanned_bytes,
            "databases": self.databases,
            "found": [item.to_dict() for item in self.found],
            "missing": self.missing,
            "elapsed_ms": self.elapsed_ms,
        }

    @property
    def keymap(self) -> dict[str, str]:
        """``{salt_hex: key_hex}``，可直接写进 keymap.json。"""
        return {item.salt: item.key for item in self.found}


@dataclass
class KeyValidationResult:
    """一个 raw key 对目标库逐库 HMAC 校验后的结果。

    ``ok`` 表示至少命中了一个库；``all_matched`` 表示目标目录中的每个
    可读 SQLCipher 库都命中了。调用方只能使用 ``keymap`` 中的条目落盘，
    因为这里已经对每个条目执行了第 1 页 HMAC 校验。
    """

    ok: bool = False
    all_matched: bool = False
    key: str = ""
    version: int = 4
    databases: int = 0
    keymap: dict[str, str] = field(default_factory=dict)
    matched_dbs: list[str] = field(default_factory=list)
    mismatched_dbs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detail: str = ""
    written: bool = False

    def to_dict(self, *, show_key: bool = False) -> dict[str, Any]:
        """转成 JSON 安全结果，默认不回显完整密钥。"""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "all_matched": self.all_matched,
            "version": self.version,
            "databases": self.databases,
            "matched": len(self.keymap),
            "matched_dbs": self.matched_dbs[:50],
            "mismatched_dbs": self.mismatched_dbs[:50],
            "errors": self.errors[:50],
            "detail": self.detail,
            "written": self.written,
            "key_prefix": (self.key[:8] + "…") if self.key else "",
        }
        if show_key:
            payload["key"] = self.key
            payload["keymap"] = dict(self.keymap)
        else:
            payload["keymap"] = {salt: value[:8] + "…" for salt, value in self.keymap.items()}
        return payload


# --------------------------------------------------------------------------- #
# 内存读取（真实实现 + 可注入的假实现）
# --------------------------------------------------------------------------- #


class MemoryReader:
    """可读进程内存的抽象：测试里用假实现，真机上是 :class:`Win32Memory`。"""

    pid: int = 0

    def regions(self) -> list[tuple[int, int]]:
        """返回 ``[(base, size)]``（可读且已提交的区域）。"""
        return []

    def read(self, address: int, size: int) -> bytes:
        """读一段内存；读不到返回空 bytes（**不抛异常**，内存会随时变）。"""
        return b""


class NullMemory(MemoryReader):
    """永远读不到东西的实现（非 Windows 或没有目标进程时用）。"""

    def __init__(self, reason: str = "不可用") -> None:
        self.reason = reason


class Win32Memory(MemoryReader):
    """基于 ``ReadProcessMemory`` 的真实实现（只读，绝不写）。"""

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    _READABLE = (0x02, 0x04, 0x20, 0x40)  # READONLY / READWRITE / EXECUTE_READ / EXECUTE_READWRITE

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        self.pid = int(pid)
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wintypes = wintypes

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
            ]

        self._MBI = MEMORY_BASIC_INFORMATION
        self._k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
        self._k32.VirtualQueryEx.restype = ctypes.c_size_t
        self._k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self._k32.ReadProcessMemory.restype = wintypes.BOOL

        self._handle = self._k32.OpenProcess(self.PROCESS_QUERY_INFORMATION | self.PROCESS_VM_READ, False, self.pid)
        if not self._handle:
            raise OSError(
                f"打不开进程 {self.pid} 的内存（WinError {ctypes.get_last_error()}）。"
                "常见原因：微信以管理员权限运行而本进程不是——把两边权限对齐，或让微信以普通权限运行。"
            )

    def close(self) -> None:
        """释放进程句柄。"""
        if getattr(self, "_handle", None):
            self._k32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None

    def regions(self) -> list[tuple[int, int]]:
        """枚举可读、已提交的内存区域。"""
        out: list[tuple[int, int]] = []
        address = 0
        mbi = self._MBI()
        limit = 1 << 47  # 用户态地址空间上限，防止畸形区域导致死循环
        while address < limit:
            if self._k32.VirtualQueryEx(ctypes.c_void_p(self._handle), ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize)
            if size <= 0:
                break
            if mbi.State == self.MEM_COMMIT and mbi.Protect in self._READABLE:
                out.append((base, size))
            address = base + size
        return out

    def read(self, address: int, size: int) -> bytes:
        """读一段内存；失败返回空 bytes。"""
        buffer = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self._k32.ReadProcessMemory(ctypes.c_void_p(self._handle), ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
        if not ok or got.value == 0:
            return b""
        return buffer.raw[: got.value]


def list_wechat_processes(names: Sequence[str] = WECHAT_PROCESS_NAMES) -> list[dict[str, Any]]:
    """列出微信进程：``[{"pid", "name"}]``（按 pid 升序）。"""
    if os.name != "nt":
        return []
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    wanted = {str(name).lower() for name in names}
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    out: list[dict[str, Any]] = []
    ok = k32.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    while ok:
        name = bytes(entry.szExeFile).split(b"\x00")[0].decode("ascii", "replace")
        if name.lower() in wanted:
            out.append({"pid": int(entry.th32ProcessID), "name": name})
        ok = k32.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    k32.CloseHandle(ctypes.c_void_p(snapshot))
    out.sort(key=lambda item: item["pid"])
    return out


# --------------------------------------------------------------------------- #
# 校验：候选密钥 → 该库第 1 页的 HMAC 是否吻合
# --------------------------------------------------------------------------- #


@dataclass
class _PageOne:
    """目标库第 1 页的校验材料（除密钥外全是明文）。"""

    path: Path
    salt: bytes
    ciphertext: bytes
    iv: bytes
    hmac: bytes

    @property
    def salt_hex(self) -> str:
        """salt 的 hex（keymap 的键）。"""
        return self.salt.hex()


def _read_page_one(db: Path, version: int) -> _PageOne:
    """读一个 SQLCipher 库的第 1 页，切出 (salt, ciphertext, iv, hmac)。"""
    reserve = _RESERVE[version]
    hmac_size = _HMAC_SIZE[version]
    with open(db, "rb") as handle:
        page = handle.read(_PAGE_SIZE)
    if len(page) < _PAGE_SIZE:
        raise ValueError(f"库文件不足一页：{db}")
    salt = page[0:_SALT_SIZE]
    ciphertext = page[_SALT_SIZE : _PAGE_SIZE - reserve]
    iv = page[_PAGE_SIZE - reserve : _PAGE_SIZE - reserve + 16]
    hmac = page[_PAGE_SIZE - hmac_size : _PAGE_SIZE]
    return _PageOne(path=db, salt=salt, ciphertext=ciphertext, iv=iv, hmac=hmac)


def _verify(page: _PageOne, key: bytes, version: int) -> bool:
    """用候选密钥校验第 1 页的 HMAC（唯一判据，绝不靠启发式）。"""
    if len(key) != 32:
        return False
    masked = bytes(byte ^ _MAC_SALT_MASK for byte in page.salt)
    mac_key = hashlib.pbkdf2_hmac(_KDF_ALGO[version], key, masked, 2, 32)
    payload = page.ciphertext + page.iv + (1).to_bytes(4, "little")
    computed = hmac.new(mac_key, payload, _HMAC_ALGO[version]).digest()
    return hmac.compare_digest(computed, page.hmac)


# --------------------------------------------------------------------------- #
# 查找
# --------------------------------------------------------------------------- #


def _anchor_bytes(page: _PageOne) -> list[str]:
    """一个库的**内存锚点**：完整路径、文件名、词干、上级目录名。

    为什么不用 salt 当锚点：4.1.7.30 真机实测，salt 与密钥相距 **5.8 万 ~ 292 MB**
    （完全不在同一处），而数据库路径字符串离密钥只有 **344 字节 ~ 635 KB** ——
    WCDB 把「库路径 + 密钥」放在同一个 handler 结构附近，路径才是可靠的路标。
    """
    path = Path(page.path)
    out: list[str] = []
    for item in (str(path), path.name, path.stem, path.parent.name):
        text = str(item)
        if len(text) >= 4 and text not in out:
            out.append(text)
    return out


#: 锚点周围默认扫多大（字节）。实测最远 635 KB，留三倍余量。
DEFAULT_WINDOW = 2 * 1024 * 1024
#: **路标**周围扫多大。实测同一簇里密钥相隔约 1.6 KB，用不着 2 MB——
#: 窗口直接决定候选数量（=窗口/8），所以这里收窄 4 倍，速度快 4 倍。
DEFAULT_SEED_WINDOW = 512 * 1024
#: 单个库最多扫多少**去重后**的内存，防止锚点命中过多时跑飞。
DEFAULT_MAX_SCAN = 128 * 1024 * 1024
#: 候选按多少字节对齐（x64 上 32 字节的值通常 8/16 字节对齐）。
_ALIGN = 8


def _coalesce(positions: Sequence[int], window: int, gap: int = 4096) -> list[tuple[int, int]]:
    """把一堆落点按 ±window 展开成合并后的区间（重叠/相邻的并起来）。"""
    if not positions:
        return []
    spans = sorted((max(0, pos - window), pos + window) for pos in positions)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(int(a), int(b)) for a, b in merged]


def _locate_raw(
    reader: MemoryReader,
    needles: Sequence[bytes],
    *,
    stats: dict[str, int] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[bytes, list[int]]:
    """一次内存扫描，把若干**原始字节**串的位置全找出来。"""
    found: dict[bytes, list[int]] = {needle: [] for needle in needles}
    total = 0
    for base, size in reader.regions():
        offset = 0
        while offset < size:
            chunk = min(_CHUNK, size - offset)
            data = reader.read(base + offset, chunk)
            if data:
                total += len(data)
                for needle in needles:
                    start = 0
                    while True:
                        index = data.find(needle, start)
                        if index < 0:
                            break
                        found[needle].append(base + offset + index)
                        start = index + 1
                offset += len(data)
            else:
                offset += chunk
        if progress is not None:
            progress(f"定位扫描 {size // 1048576} MB @0x{base:x}")
    if stats is not None:
        stats["bytes"] = stats.get("bytes", 0) + total
    return found


def _locate_anchors(
    reader: MemoryReader,
    needles: Sequence[str],
    *,
    stats: dict[str, int] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, list[int]]:
    """一次内存扫描，把所有锚点（编码成 bytes）的位置全部找出来。"""
    encoded = {needle.encode("utf-8", "ignore"): needle for needle in needles}
    raw = _locate_raw(reader, list(encoded), stats=stats, progress=progress)
    return {original: raw.get(needle, []) for needle, original in encoded.items()}


def _scan_ranges(
    reader: MemoryReader,
    page: _PageOne,
    ranges: Sequence[tuple[int, int]],
    *,
    version: int,
    stats: dict[str, int] | None = None,
) -> KeyCandidate | None:
    """在给定内存区间里按 8 字节对齐滑窗，逐候选做 HMAC 校验。

    关键性能点：**先把区间读成大块缓冲**，再在缓冲里滑窗。早期实现是每个候选一次
    ``ReadProcessMemory``，光系统调用就比计算贵几十倍（真机上 7 分钟一无所获也有它的份）。
    """
    length = 32
    for start, end in ranges:
        offset = int(start)
        while offset < end:
            chunk = min(_CHUNK, int(end) - offset)
            data = reader.read(offset, chunk)
            if not data:
                offset += chunk
                continue
            if stats is not None:
                stats["bytes"] = stats.get("bytes", 0) + len(data)
            limit = len(data) - length
            # 对齐必须是**绝对地址**对齐，不能从块首算起：
            # 锚点命中的地址往往不是 8 的倍数，从块首按 8 步进会让整个候选网格错位，
            # 真密钥就永远落不到网格上（这一条在真机上导致过"扫了 7 分钟一无所获"）。
            first = (-offset) % _ALIGN
            for index in range(first, limit + 1, _ALIGN):
                candidate = data[index : index + length]
                if not candidate.strip(b"\x00"):  # 全零片段极多，先排除
                    continue
                if _verify(page, candidate, version):
                    return KeyCandidate(salt=page.salt_hex, key=candidate.hex(), db=str(page.path), address=offset + index)
            # 相邻块之间保留一个窗口的重叠，步长取对齐倍数，保证绝对网格一致
            advanced = ((len(data) - length) // _ALIGN) * _ALIGN
            offset += max(_ALIGN, advanced)
    return None


def _collect_databases(root: str | Path) -> list[Path]:
    """列出待找密钥的库（``*.db``，跳过 -wal/-shm 与 kvdb）。"""
    base = Path(root)
    if base.is_file():
        return [base]
    return sorted(path for path in base.rglob("*.db") if path.is_file() and not path.name.endswith(("-wal", "-shm")))


def _normalise_raw_key(value: str | bytes) -> tuple[str, bytes] | None:
    """解析一个 raw key，返回规范化 hex 与 bytes；格式不对返回 ``None``。"""
    if isinstance(value, bytes):
        raw = bytes(value)
        text = raw.hex()
    else:
        text = str(value or "").strip()
        if text.lower().startswith("0x"):
            text = text[2:]
        if len(text) != 64:
            return None
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            return None
    if len(raw) != 32:
        return None
    return raw.hex(), raw


def validate_raw_key(
    root: str | Path,
    key: str | bytes,
    *,
    version: int = 4,
) -> KeyValidationResult:
    """把一个捕获/输入的 raw key 对 ``root`` 下每个库逐一做 HMAC 校验。

    这是捕获流程唯一允许产生 keymap 条目的入口。验证采用与 SQLCipher
    解密相同的第 1 页 HMAC 判据，绝不依据 salt、文件名或长度猜测。
    ``ok`` 只表示至少一个库匹配；调用方可用 ``all_matched`` 判断是否覆盖
    了整个目录。即使只有部分库匹配，返回的 ``keymap`` 也只包含通过校验的
    条目；全不匹配时该映射为空，适合直接阻止落盘。
    """
    result = KeyValidationResult(version=version)
    normalised = _normalise_raw_key(key)
    if normalised is None:
        result.detail = "密钥必须是 64 位十六进制 raw key（32 字节）"
        return result
    key_hex, raw_key = normalised
    result.key = key_hex

    databases = _collect_databases(root)
    result.databases = len(databases)
    if not databases:
        result.detail = f"{root} 下没有找到任何 .db"
        return result

    pages: list[_PageOne] = []
    for db in databases:
        try:
            page = _read_page_one(db, version)
        except (OSError, ValueError) as error:
            result.errors.append(f"{db.name}: {error}")
            continue
        pages.append(page)
        if _verify(page, raw_key, version):
            result.keymap[page.salt_hex] = key_hex
            result.matched_dbs.append(str(page.path))
        else:
            result.mismatched_dbs.append(str(page.path))

    result.ok = bool(result.keymap)
    result.all_matched = bool(pages) and len(result.keymap) == len(pages) and not result.errors
    if not result.keymap:
        # 这是验收里明确要求的文案；不要把错误 key 当成一次成功捕获。
        names = [Path(item).name for item in result.mismatched_dbs[:10]]
        suffix = f"：{'、'.join(names)}" if names else ""
        result.detail = f"key 与这些库都不匹配{suffix}"
    elif result.all_matched:
        result.detail = f"key 通过全部 {len(pages)} 个库的 HMAC 校验"
    else:
        result.detail = (
            f"key 通过 {len(result.keymap)}/{len(pages)} 个库的 HMAC 校验，"
            f"其余库不匹配（不会写入未通过的条目）"
        )
    return result


def save_validated_key(
    root: str | Path,
    key: str | bytes,
    keymap_path: str | Path,
    *,
    version: int = 4,
    merge: bool = True,
) -> KeyValidationResult:
    """校验 raw key 并只把通过 HMAC 的 salt→key 条目写入 keymap。

    ``keymap_path`` 在全不匹配、格式错误或没有可读数据库时不会创建，
    这是防止「捕获成功但写入假 key」的持久化边界。
    """
    result = validate_raw_key(root, key, version=version)
    if result.keymap:
        save_keymap(keymap_path, result.keymap, merge=merge)
        result.written = True
    return result


def find_keys(
    root: str | Path,
    *,
    reader: MemoryReader | None = None,
    pids: Sequence[int] | None = None,
    version: int = 4,
    key_type: str = "raw",
    window: int = DEFAULT_WINDOW,
    max_scan: int = DEFAULT_MAX_SCAN,
    seed_keys: "Mapping[str, str] | None" = None,
    use_anchors: bool = True,
    progress: Callable[[str], None] | None = None,
) -> FindKeyResult:
    """从微信进程内存里找出 ``root`` 下这些库的解密密钥。

    :param root: 加密库所在目录（或单个 ``.db``）；通常是 ``db_storage``。
    :param reader: 内存读取实现；``None`` 时对每个微信进程建 :class:`Win32Memory`。
    :param pids: 指定进程；``None`` 时自动枚举微信进程。
    :param version: SQLCipher 参数集（微信 4.x = 4）。
    :param key_type: 只支持 ``raw``（passphrase 需要每次 256000 轮 PBKDF2，逐候选验证不可行）。
    :param window: 每个锚点周围扫多大，默认 2 MB。
    :param max_scan: 单个库最多扫多少去重后的内存，默认 128 MB。
    :param seed_keys: **已有的 keymap**（``{salt: key}``）。给了它就走快路径：
        已知密钥在内存里是**聚簇**的（真机实测：同一库的 6 次出现只落在 3 个区域，
        彼此相隔 1.6 KB），所以拿它们当路标，只扫这些区域就能把**新库**的密钥一起找出来，
        比按库路径锚点扫几十 MB 快一两个数量级。
    :param use_anchors: 快路径没找齐时，是否退回"按库路径锚点"的通用扫描（较慢）。
    :param progress: 进度回调（默认不打印）。
    :return: :class:`FindKeyResult`；找不到就 ``found`` 为空并给出 ``hint``。
    """
    started = time.time()
    result = FindKeyResult(stage="prepare")
    if key_type != "raw":
        result.error = "只支持 raw 密钥（64 位十六进制）：passphrase 形态每验证一次要过 256000 轮 PBKDF2，无法逐候选试。"
        result.hint = "若你手上是 passphrase，请用它直接解密（decrypt_file 支持 passphrase），不要走内存提取。"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    databases = _collect_databases(root)
    if not databases:
        result.error = f"{root} 下没有找到任何 .db"
        result.hint = "把 --root 指向 db_storage（或包含 .db 的目录）。"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    pages: list[_PageOne] = []
    for db in databases:
        try:
            pages.append(_read_page_one(db, version))
        except (OSError, ValueError) as error:
            if progress is not None:
                progress(f"跳过 {db.name}：{error}")
    result.databases = len(pages)
    if not pages:
        result.error = "所有库都读不出第 1 页（文件损坏或不是 SQLCipher 库）"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    targets = sorted(set(pids)) if pids else [item["pid"] for item in list_wechat_processes()]
    if not targets and reader is None:
        result.error = "没有找到正在运行的微信进程"
        result.hint = "先启动微信（并登录），再跑一次；密钥只存在于运行中的进程内存里。"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    readers: list[MemoryReader] = []
    if reader is not None:
        readers = [reader]
        targets = [reader.pid]
    else:
        for pid in targets:
            try:
                readers.append(Win32Memory(pid))
            except OSError as error:
                if progress is not None:
                    progress(f"跳过 pid {pid}：{error}")
        if not readers:
            result.error = "所有微信进程都打不开内存（权限不足）"
            result.hint = "让微信与 DS H 以相同权限运行（都用管理员，或都不用），再试一次。"
            result.elapsed_ms = int((time.time() - started) * 1000)
            return result

    result.stage = "scan"
    result.scanned_pids = list(targets)
    found: dict[str, KeyCandidate] = {}
    stats: dict[str, int] = {"bytes": 0}
    try:
        for current in readers:
            if len(found) == len(pages):
                break
            if progress is not None:
                progress(f"扫描 pid {current.pid} 的内存…")

            # ── 快路径：拿已有密钥当路标，只扫它们所在的那几个区域 ──────────
            if seed_keys:
                seeds: list[bytes] = []
                for value in seed_keys.values():
                    try:
                        raw = bytes.fromhex(str(value))
                    except ValueError:
                        continue
                    if len(raw) == 32 and raw not in seeds:
                        seeds.append(raw)
                if seeds:
                    positions = _locate_raw(current, seeds, stats=stats, progress=progress)
                    seeds_hits = [pos for item in positions.values() for pos in item]
                    if seeds_hits:
                        ranges = _coalesce(seeds_hits, min(window, DEFAULT_SEED_WINDOW))
                        if progress is not None:
                            span = sum(e - s for s, e in ranges) / 1048576
                            progress(f"路标命中 {len(seeds_hits)} 处 → 只扫 {span:.1f} MB")
                        for page in pages:
                            if page.salt_hex in found:
                                continue
                            hit = _scan_ranges(current, page, ranges, version=version, stats=stats)
                            if hit is not None:
                                hit.pid = current.pid
                                hit.source = "seed"
                                found[page.salt_hex] = hit
                                if progress is not None:
                                    progress(f"命中：{page.path.name}（路标邻域）")

            if not use_anchors or len(found) == len(pages):
                continue

            # ── 通用路径：按"库路径"锚点扫（实测路径离密钥 344 B ~ 635 KB）──
            needles: list[str] = []
            for page in pages:
                for anchor in _anchor_bytes(page):
                    if anchor not in needles:
                        needles.append(anchor)
            positions = _locate_anchors(current, needles, stats=stats, progress=progress)
            for page in pages:
                if page.salt_hex in found:
                    continue
                hits: list[int] = []
                for anchor in _anchor_bytes(page):
                    hits.extend(positions.get(anchor, []))
                if not hits:
                    continue
                ranges = _coalesce(hits, window)
                budget = max_scan
                trimmed: list[tuple[int, int]] = []
                for start, end in ranges:
                    size = end - start
                    if size > budget:
                        trimmed.append((start, start + budget))
                        budget = 0
                    else:
                        trimmed.append((start, end))
                        budget -= size
                    if budget <= 0:
                        break
                if progress is not None:
                    span = sum(e - s for s, e in trimmed) / 1048576
                    progress(f"{page.path.name}：{len(hits)} 个锚点命中 → 扫 {span:.1f} MB")
                hit = _scan_ranges(current, page, trimmed, version=version, stats=stats)
                if hit is not None:
                    hit.pid = current.pid
                    hit.source = "anchor"
                    found[page.salt_hex] = hit
                    if progress is not None:
                        progress(f"命中：{page.path.name}（路径锚点）")
    finally:
        for current in readers:
            close = getattr(current, "close", None)
            if callable(close) and reader is None:
                close()

    result.scanned_bytes = int(stats.get("bytes", 0))

    result.found = list(found.values())
    result.missing = [page.salt_hex for page in pages if page.salt_hex not in found]
    result.ok = bool(result.found)
    result.stage = "done"
    if not result.ok:
        scanned_mb = result.scanned_bytes / 1048576
        result.error = f"扫描了 {len(targets)} 个微信进程 / {scanned_mb:.0f} MB 内存，没有找到任何一把密钥"
        result.hint = (
            "两种可能：① 密钥确实不在内存里——微信 ≥4.1.10 起「零驻留」，"
            "本项目在 4.1.13.65 上实测 657 MB 内存里 17 把已知密钥一把都没有；"
            "② 锚点没覆盖到——密钥离数据库路径超过 "
            f"{window // 1024 // 1024} MB 时扫不到，可用 --window 放大（代价是线性变慢）。"
            "确认密钥在内存里的办法：在 ≤4.1.9（实测 4.1.7.30 可以）上登录后重跑；"
            "或退一步用 chatlog 那类外部工具、或直接用已有的 keymap.json 走 `ingest --keymap`。"
        )
    elif result.missing:
        result.hint = f"还有 {len(result.missing)} 个库没找到密钥（多半是本次没被微信打开过的库）"
    result.elapsed_ms = int((time.time() - started) * 1000)
    return result


# --------------------------------------------------------------------------- #
# 确保密钥就绪（每次用之前先看有没有 key，没有就去读）
# --------------------------------------------------------------------------- #


@dataclass
class EnsureResult:
    """一次"确保密钥就绪"的结果。"""

    ok: bool = False
    stage: str = "check"          # check | extract | done
    keymap_path: str = ""
    databases: int = 0
    covered: int = 0
    missing: list[str] = field(default_factory=list)        # 还缺的 salt
    missing_dbs: list[str] = field(default_factory=list)     # 还缺的库文件名
    extracted: int = 0
    written: bool = False
    detail: str = ""
    hint: str = ""
    elapsed_ms: int = 0
    find: dict[str, Any] = field(default_factory=dict)       # 抽取子报告

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "ok": self.ok,
            "stage": self.stage,
            "keymap_path": self.keymap_path,
            "databases": self.databases,
            "covered": self.covered,
            "missing": self.missing[:50],
            "missing_dbs": self.missing_dbs[:50],
            "extracted": self.extracted,
            "written": self.written,
            "detail": self.detail,
            "hint": self.hint,
            "elapsed_ms": self.elapsed_ms,
            "find": self.find,
        }


def ensure_keys(
    root: str | Path,
    *,
    keymap_path: str | Path,
    version: int = 4,
    extract: bool = True,
    write: bool = True,
    seed_paths: Sequence[str | Path] = (),
    reader: MemoryReader | None = None,
    pids: Sequence[int] | None = None,
    window: int = DEFAULT_WINDOW,
    watch_seconds: float = 0.0,
    use_anchors: bool = True,
    progress: Callable[[str], None] | None = None,
) -> EnsureResult:
    """**先看有没有 key，没有就去读**：检查 ``root`` 下这些库的密钥是否齐备，缺就自动提取。

    这是"每次使用之前"应该调用的那一步：

    1. 列出 ``root`` 下所有 ``.db``，算出各自的 salt；
    2. 读 ``keymap_path``（以及 ``seed_paths`` 里的其他 keymap）看覆盖了多少；
    3. **全都有 → 直接返回**（不碰内存，毫秒级，正常使用时的开销可以忽略）；
    4. 缺密钥且 ``extract=True`` → 在微信进程内存里找（优先用已有密钥当路标），
       找到的合并写回 ``keymap_path``（``write=False`` 时只报告不落盘）。

    :param watch_seconds: > 0 时用轮询模式（密钥可能只在微信登录瞬间驻留）。
    :return: :class:`EnsureResult`；``ok=True`` 表示"这些库现在都有密钥了"。
    """
    started = time.time()
    result = EnsureResult(keymap_path=str(keymap_path))
    databases = _collect_databases(root)
    result.databases = len(databases)
    if not databases:
        result.detail = f"{root} 下没有找到任何 .db"
        result.hint = "把 --root 指向 db_storage（或包含 .db 的目录）。"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    table: dict[str, str] = {}
    original = load_keymap(keymap_path)
    for path in [keymap_path, *seed_paths]:
        for salt, key in load_keymap(path).items():
            table.setdefault(salt, key)

    pages: list[_PageOne] = []
    for db in databases:
        try:
            pages.append(_read_page_one(db, version))
        except (OSError, ValueError) as error:
            if progress is not None:
                progress(f"跳过 {db.name}：{error}")

    def _missing() -> list[_PageOne]:
        return [page for page in pages if page.salt_hex not in table]

    pending = _missing()
    result.covered = len(pages) - len(pending)
    result.missing = [page.salt_hex for page in pending]
    result.missing_dbs = [Path(page.path).name for page in pending]

    if not pending:
        result.ok = True
        result.stage = "done"
        result.detail = f"密钥已就绪：{len(pages)}/{len(pages)} 个库都有 key（{keymap_path}）"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    result.stage = "extract"
    if progress is not None:
        progress(f"缺 {len(pending)} 个库的密钥（{', '.join(result.missing_dbs[:6])}…），开始从微信进程内存里找")
    if not extract:
        result.detail = f"缺 {len(pending)} 个库的密钥；本次只检查不提取"
        result.hint = f"运行 `key ensure --root <db_storage> --out {keymap_path}` 自动提取"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    if watch_seconds and watch_seconds > 0:
        found = watch_keys(root, version=version, seconds=watch_seconds, progress=progress)
    else:
        found = find_keys(
            root,
            reader=reader,
            pids=pids,
            version=version,
            window=window,
            seed_keys=table or None,
            use_anchors=use_anchors,
            progress=progress,
        )
    result.find = found.to_dict()
    before = set(table)
    for salt, key in found.keymap.items():
        table.setdefault(salt, key)
    # extracted 只统计**新**拿到的密钥（路标本身也会在扫描里被重新验证，不该算成新增）
    result.extracted = len([salt for salt in found.keymap if salt not in before])

    pending = _missing()
    result.covered = len(pages) - len(pending)
    result.missing = [page.salt_hex for page in pending]
    result.missing_dbs = [Path(page.path).name for page in pending]
    result.ok = not pending
    result.stage = "done"

    # 落盘条件：合并后的表与磁盘上的**不一样**就写（哪怕这次没新提取到密钥——
    # 从路标那儿合并进来的密钥也应该固化到标准位置，下次"看一眼"就是齐的）。
    if write and table and table != original:
        save_keymap(keymap_path, table, merge=True)
        result.written = True

    if result.ok:
        result.detail = f"密钥已齐备：{result.covered}/{len(pages)} 个库（本次新提取 {result.extracted} 把）"
    else:
        result.detail = (
            f"{result.covered}/{len(pages)} 个库有密钥，仍缺 {len(pending)} 个"
            f"（{', '.join(result.missing_dbs[:6])}）"
        )
        result.hint = (
            "缺的通常是**本次没被微信打开过**的库：在微信里点开对应功能（收藏/聊天记录/小程序）后再跑一次，"
            "或加 --watch 在微信重新登录时轮询。"
            + (f"；{found.hint}" if found.hint else "")
        )
    result.elapsed_ms = int((time.time() - started) * 1000)
    return result


def watch_keys(
    root: str | Path,
    *,
    version: int = 4,
    seconds: float = 60.0,
    interval: float = 3.0,
    progress: Callable[[str], None] | None = None,
) -> FindKeyResult:
    """在一段时间内反复扫描（用于"密钥只在启动/登录瞬间驻留"的情况）。

    每轮都重新枚举微信进程（启动过程中 pid 会变），一旦找齐就提前返回。
    """
    started = time.time()
    deadline = started + max(1.0, seconds)
    best: FindKeyResult | None = None
    while True:
        current = find_keys(root, version=version, progress=progress)
        if best is None or len(current.found) > len(best.found):
            best = current
        if current.ok:
            expected = len(_collect_databases(root))
            if len(current.found) >= expected:
                return current
        if time.time() >= deadline:
            break
        if progress is not None:
            progress(f"未找齐，{interval:.0f}s 后再扫（已找到 {len(current.found)} 把）")
        time.sleep(max(0.5, interval))
    assert best is not None
    best.elapsed_ms = int((time.time() - started) * 1000)
    if not best.ok:
        best.hint = (best.hint + "（watch 模式也没逮到）") if best.hint else "watch 模式也没逮到"
    return best


# --------------------------------------------------------------------------- #
# keymap 读写
# --------------------------------------------------------------------------- #


def load_keymap(path: str | Path) -> dict[str, str]:
    """读 keymap.json（不存在或非法则返回空表）。"""
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for salt, key in data.items():
        if isinstance(key, str) and key.strip():
            out[str(salt).strip().lower()] = key.strip()
    return out


def save_keymap(path: str | Path, mapping: dict[str, str], *, merge: bool = True) -> Path:
    """写 keymap.json；``merge=True`` 时与已有内容合并（新值覆盖同名键）。

    **这是密钥文件**：调用方有责任把它放在受控位置（不要进仓库、不要放云盘）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    table = load_keymap(target) if merge else {}
    for salt, key in mapping.items():
        table[str(salt).strip().lower()] = str(key).strip()
    target.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限
        pass
    return target
