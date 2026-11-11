# -*- coding: utf-8 -*-
"""工具 7：解密本地数据。

微信 4.1.15 的密钥不在内存里（x'<64hex><32hex>' 那套已失效），也不在 SetDBKey
的一次调用里，所以改成参考实现的做法：

1. Frida **spawn** 微信，在 Weixin.dll 的 codec 配置函数入口下钩子（4.1.15.56 RVA=0x35D62E0）；
2. 命中时 dump rcx/rdx/r8/r9 指向的 64 字节，得到候选集；
3. 用 SQLCipher4 首页 HMAC 判定谁是 enc_key / 谁是 passphrase；
4. 有 passphrase 就逐库派生 enc_key（PBKDF2-SHA512, 256000），然后 AES-256-CBC 整库解密。

加密参数（微信 4.1.x）：
    page 4096 / salt(16) / ciphertext / IV(16)@4016 / HMAC-SHA512(64)@4032 / reserve 80
    enc_key = PBKDF2-SHA512(passphrase, salt, 256000, 32)
    mac_key = PBKDF2-SHA512(enc_key, salt^0x3a, 2, 32)
    校验     = HMAC-SHA512(mac_key, page1[16:4032] + uint32le(1)) == page1[4032:4096]

只读：Frida 只读寄存器指向的内存，不写目标进程；解密只读原库、另存明文副本。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

#: 插件自带的第三方库（pycryptodome / frida）
_PYLIBS = Path(__file__).resolve().parents[1] / "pylibs"
if _PYLIBS.is_dir() and str(_PYLIBS) not in sys.path:
    sys.path.insert(0, str(_PYLIBS))

PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE = 80
SQLITE_HDR = b"SQLite format 3\x00"

#: 4.1.15.56 的 codec 配置函数入口（参考实现实测）；换版本用 scan_lea 现算。
KNOWN_RVA = {"4.1.15.56": 0x35D62E0}

JS_TEMPLATE = r"""
var RVA = __RVA__;
var DLL = "__DLL__";
var MAXHITS = __MAXHITS__;
var hooked = false, hits = 0;
function hex(buf) {
  var a = new Uint8Array(buf), s = "";
  for (var i = 0; i < a.length; i++) s += ("0" + a[i].toString(16)).slice(-2);
  return s;
}
function tryHook() {
  if (hooked) return;
  var m = null;
  try { m = Process.getModuleByName(DLL); } catch (e) { }
  if (m === null) { setTimeout(tryHook, 50); return; }
  var target = m.base.add(RVA);
  try {
    Interceptor.attach(target, {
      onEnter: function (a) {
        if (hits >= MAXHITS) return;
        hits++;
        var out = {n: hits, args: []};
        for (var i = 0; i < 4; i++) {
          var h = null, d = null;
          try { h = hex(a[i].readByteArray(64)); } catch (e) { }
          try { d = hex(a[i].readPointer().readByteArray(64)); } catch (e) { }
          out.args.push({hex: h, deep: d});
        }
        send(out);
      }
    });
    hooked = true;
    send({log: "HOOKED base=" + m.base + " target=" + target + " rva=" + RVA});
  } catch (e) {
    send({log: "attach failed: " + e + ", retry"});
    setTimeout(tryHook, 100);
  }
}
send({log: "script loaded, waiting for " + DLL});
setTimeout(tryHook, 10);
"""


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #


def weixin_exe() -> str:
    for candidate in (
        Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
        Path(r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


def weixin_dll() -> Path | None:
    """找最新版本目录下的 Weixin.dll。"""
    root = Path(r"C:\Program Files\Tencent\Weixin")
    candidates = sorted(root.glob("*/Weixin.dll"), key=lambda p: p.parent.name, reverse=True)
    return candidates[0] if candidates else None


def version_of(dll: Path | None) -> str:
    return dll.parent.name if dll else ""


def resolve_rva(dll: Path, pattern: str = "MMV1") -> tuple[int, str]:
    """优先用已知 RVA；否则调用 scan_lea.py 现算。"""
    version = version_of(dll)
    if version in KNOWN_RVA:
        return KNOWN_RVA[version], "known:" + version
    scanner = Path(__file__).resolve().parents[1] / "tools" / "frida" / "scan_lea.py"
    if not scanner.is_file():
        raise RuntimeError("没有 %s 的已知 RVA，且找不到 scan_lea.py" % version)
    out = Path(os.environ.get("TEMP", ".")) / "rva_scan.json"
    subprocess.run(
        [sys.executable, str(scanner), "--dll", str(dll), "--pattern", pattern, "--json", str(out)],
        check=True, timeout=600,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    rva = int(payload.get("rva") or payload.get("function_rva"), 0)
    return rva, "scanned:" + pattern


# --------------------------------------------------------------------------- #
# Frida 捕获
# --------------------------------------------------------------------------- #


def capture(seconds: float = 180.0, *, rva: int | None = None, out_dir=None, on_log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """spawn 微信并在 codec 配置函数下钩子，收集候选到 hits.jsonl。"""
    try:
        import frida
    except ImportError:
        return {"ok": False, "error": "没有 frida：pip install frida（插件 engine/pylibs 里未找到）"}

    exe = weixin_exe()
    if not exe:
        return {"ok": False, "error": "找不到 Weixin.exe"}
    dll = weixin_dll()
    if dll is None:
        return {"ok": False, "error": "找不到 Weixin.dll"}
    if rva is None:
        rva, how = resolve_rva(dll)
    else:
        how = "explicit"

    out = Path(out_dir) if out_dir else Path(os.environ.get("TEMP", ".")) / "wx_keys"
    out.mkdir(parents=True, exist_ok=True)
    hits_path = out / "hits.jsonl"
    log = on_log or (lambda m: None)

    js = (JS_TEMPLATE
          .replace("__RVA__", hex(rva))
          .replace("__DLL__", "Weixin.dll")
          .replace("__MAXHITS__", "800"))

    handle = hits_path.open("w", encoding="utf-8")

    def on_message(msg, _data):
        if msg.get("type") != "send":
            log("[frida:error] " + json.dumps(msg)[:200])
            return
        payload = msg["payload"]
        if "log" in payload:
            log("[frida] " + str(payload["log"]))
            return
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()

    log("[*] spawn %s, rva=0x%X (%s)" % (exe, rva, how))
    pid = frida.spawn(exe)
    session = frida.attach(pid)
    script = session.create_script(js)
    script.on("message", on_message)
    script.load()
    frida.resume(pid)
    log("[*] 已挂起钩子并恢复进程：请在微信窗口登录（点「进入微信」），等待 %gs" % seconds)

    deadline = time.time() + max(10.0, float(seconds))
    try:
        while time.time() < deadline:
            time.sleep(1.0)
    finally:
        handle.close()
        try:
            script.unload()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": hits_path.stat().st_size > 0, "hits": str(hits_path), "rva": hex(rva), "how": how}


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #


def _hmac_ok(enc_key: bytes, page: bytes) -> bool:
    import hashlib
    import hmac as hmac_mod

    salt = page[:SALT_SZ]
    body = page[SALT_SZ:PAGE_SZ - RESERVE + IV_SZ]
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3A for b in salt), 2, 32)
    calc = hmac_mod.new(mac_key, body + struct.pack("<I", 1), hashlib.sha512).digest()
    return calc == page[PAGE_SZ - HMAC_SZ:PAGE_SZ]


def _derive_enc(passphrase: bytes, salt: bytes, iters: int = 256000) -> bytes:
    import hashlib

    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, iters, 32)


def _databases(db_root: Path) -> list[Path]:
    out = []
    for path in sorted(db_root.rglob("*.db")):
        try:
            head = path.read_bytes()[:PAGE_SZ]
        except OSError:
            continue
        if len(head) >= PAGE_SZ and head[:SALT_SZ] != SQLITE_HDR[:SALT_SZ]:
            out.append(path)
    return out


def recover(hits_path: str | Path, db_root: str | Path, *, iters: int = 256000) -> dict[str, Any]:
    """从候选里判定 passphrase 与各库 enc_key。"""
    databases = _databases(Path(db_root))
    if not databases:
        return {"ok": False, "error": "没有加密库：%s" % db_root, "keys": {}, "passphrase": ""}

    # 候选 = 每个参数直接 64 字节 + 解引用后的 64 字节；截 32 字节当 key 用
    candidates: dict[bytes, None] = {}
    for line in Path(hits_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        for arg in payload.get("args") or []:
            for field in ("hex", "deep"):
                value = arg.get(field)
                if isinstance(value, str) and len(value) >= 64:
                    try:
                        candidates[bytes.fromhex(value[:64])] = None
                    except ValueError:
                        pass

    pages = {}
    for db in databases:
        try:
            pages[db] = db.read_bytes()[:PAGE_SZ]
        except OSError:
            continue

    keys: dict[str, str] = {}
    passphrase = b""
    for cand in candidates:
        hit = [db for db, page in pages.items() if _hmac_ok(cand, page)]
        if hit:
            for db in hit:
                keys[str(db)] = cand.hex()
            continue
        # 当作 passphrase 试试：能派生出一个库的 enc_key 就算
        for db, page in pages.items():
            enc = _derive_enc(cand, page[:SALT_SZ], iters)
            if _hmac_ok(enc, page):
                passphrase = cand
                break
        if passphrase:
            break

    if passphrase:
        keys = {}
        for db, page in pages.items():
            keys[str(db)] = _derive_enc(passphrase, page[:SALT_SZ], iters).hex()

    return {
        "ok": bool(keys),
        "candidates": len(candidates),
        "databases": len(databases),
        "matched": len(keys),
        "passphrase": passphrase.hex(),
        "iters": iters,
        "keys": keys,
    }


# --------------------------------------------------------------------------- #
# 解密
# --------------------------------------------------------------------------- #


def decrypt_db(db_path: str | Path, enc_key_hex: str, out_path: str | Path) -> dict[str, Any]:
    """整库 AES-256-CBC 解密；先验 HMAC，解完用 sqlite3 打开确认。"""
    from Crypto.Cipher import AES

    db_path = Path(db_path)
    out_path = Path(out_path)
    data = db_path.read_bytes()
    enc_key = bytes.fromhex(enc_key_hex)

    page1 = data[:PAGE_SZ]
    if not _hmac_ok(enc_key, page1):
        return {"ok": False, "error": "page1 HMAC 不匹配", "db": str(db_path)}

    total = len(data) // PAGE_SZ
    out = bytearray()
    for index in range(total):
        page = data[index * PAGE_SZ:(index + 1) * PAGE_SZ]
        iv = page[PAGE_SZ - RESERVE:PAGE_SZ - RESERVE + IV_SZ]
        if len(page) < PAGE_SZ:
            break
        body = page[SALT_SZ:PAGE_SZ - RESERVE] if index == 0 else page[:PAGE_SZ - RESERVE]
        plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(body)
        if index == 0:
            out += SQLITE_HDR + plain + b"\x00" * RESERVE
        else:
            out += plain + b"\x00" * RESERVE

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))

    import sqlite3

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % out_path.as_posix(), uri=True)
        tables = [row[0] for row in conn.execute("select name from sqlite_master where type='table'").fetchall()]
        conn.close()
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": "解密后打不开：%s" % error, "db": str(db_path), "out": str(out_path)}

    return {"ok": True, "db": str(db_path), "out": str(out_path), "tables": len(tables)}


def decrypt_all(db_root: str | Path, keys: dict[str, str], out_dir: str | Path) -> dict[str, Any]:
    """按 {库路径: enc_key} 批量解密。"""
    root = Path(db_root)
    out_dir = Path(out_dir)
    results = []
    for db, key in keys.items():
        target = out_dir / Path(db).relative_to(root) if str(db).startswith(str(root)) else out_dir / Path(db).name
        results.append(decrypt_db(db, key, target))
    ok = sum(1 for item in results if item.get("ok"))
    return {"ok": ok > 0, "decrypted": ok, "total": len(results), "results": results, "out_dir": str(out_dir)}


__all__ = [
    "KNOWN_RVA",
    "capture",
    "decrypt_all",
    "decrypt_db",
    "recover",
    "resolve_rva",
    "weixin_dll",
    "weixin_exe",
]
