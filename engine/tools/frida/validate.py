#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate.py — 从 frida_hook.py 抓到的候选里判定 passphrase 与各库 enc_key。

核心判据（SQLCipher 4，微信 4.1.x）
  每页 4096 字节：salt(16) | 密文 | IV(16)@4016 | HMAC-SHA512(64)@4032
  mac_salt = salt ^ 0x3a
  mac_key  = PBKDF2-HMAC-SHA512(enc_key, mac_salt, 2, 32)
  HMAC-SHA512(mac_key, page1[16:4032] + uint32le(1)) == page1[4032:4096]

  enc_key 可能是
    a) 候选本身（运行时直接持有的 raw key）
    b) PBKDF2-HMAC-SHA512(passphrase, 该库 salt, 256000, 32)

用法
  python validate.py --hits hits.jsonl --db <某个 db 文件>              # 找能开这个库的 key
  python validate.py --hits hits.jsonl --db-dir <db_storage> --iters 256000
  python validate.py --passphrase <hex> --db-dir <db_storage>           # 验证一把 passphrase 能开多少库
"""
from __future__ import annotations

import argparse
import hashlib
import os
import hmac
import json
import pathlib
import struct
import sys
from concurrent.futures import ProcessPoolExecutor

PAGE = 4096


def page1(path: pathlib.Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(PAGE)


def hmac_ok(enc_key: bytes, page: bytes) -> bool:
    salt, body = page[:16], page[16:4032]
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3A for b in salt), 2, 32)
    calc = hmac.new(mac_key, body + struct.pack("<I", 1), hashlib.sha512).digest()
    return calc == page[4032:4096]


def candidates(hits_path: pathlib.Path) -> list[str]:
    seen, out = set(), []
    with open(hits_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("HIT "):
                line = line[4:]
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 32 字节密钥只可能落在结构体开头；只取每个 dump 的前 32 字节
            # （64 字节 dump 的后半是紧邻字段，会把候选集放大十几倍）
            for a in rec.get("args", []):
                for key in ("hex", "deep"):
                    h = a.get(key)
                    if not h or len(h) < 64:
                        continue
                    c = h[:64]
                    if c.strip("0") and c not in seen:
                        seen.add(c)
                        out.append(c)
    return out


def _derive(cand: str, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", bytes.fromhex(cand), salt, iters, 32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hits", help="frida_hook.py 的输出 JSONL")
    ap.add_argument("--db", action="append", default=[], help="待验证的 db 文件，可多次给")
    ap.add_argument("--db-dir", help="db_storage 目录，递归验证所有 *.db")
    ap.add_argument("--iters", type=int, default=256000, help="PBKDF2 迭代次数，4.1.x 默认 256000")
    ap.add_argument("--passphrase", help="直接验证一把 passphrase")
    args = ap.parse_args()

    dbs: list[pathlib.Path] = [pathlib.Path(p) for p in args.db]
    if args.db_dir:
        dbs += [p for p in pathlib.Path(args.db_dir).rglob("*.db") if p.is_file()]
    if not dbs:
        sys.exit("至少给 --db 或 --db-dir")

    if args.passphrase:
        pw = bytes.fromhex(args.passphrase)
        ok = 0
        for p in dbs:
            page = page1(p)
            enc = hashlib.pbkdf2_hmac("sha512", pw, page[:16], args.iters, 32)
            if hmac_ok(enc, page):
                ok += 1
        print(f"passphrase 能开 {ok}/{len(dbs)} 个库")
        return

    if not args.hits:
        sys.exit("给 --hits（或直接用 --passphrase）")

    cands = candidates(pathlib.Path(args.hits))
    print(f"候选 {len(cands)} 个，待验证库 {len(dbs)} 个")

    # 第一层：候选本身是不是某个库的 enc_key
    direct: dict[str, list[str]] = {}
    for p in dbs:
        page = page1(p)
        for c in cands:
            if hmac_ok(bytes.fromhex(c), page):
                direct.setdefault(p.name, []).append(c)

    for name, ks in direct.items():
        print(f"  ✅ enc_key 直命：{name} ← {ks[0][:16]}… ({len(ks)} 个候选)")

    # 第二层：哪个候选经 PBKDF2 能变成上面那些 enc_key → 那就是 passphrase
    pws: dict[str, set[str]] = {}
    for name, ks in direct.items():
        page = page1(next(p for p in dbs if p.name == name))
        salt = page[:16]
        # PBKDF2 很慢，按核数并行；找到就停
        workers = max(1, (os.cpu_count() or 4))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_derive, c, salt, args.iters): c for c in cands}
            for fut in futs:
                if fut.result().hex() in ks:
                    pws.setdefault(futs[fut], set()).add(name)

    if not pws:
        print("没找到 passphrase（可能只抓到 raw key，或这批候选不包含登录期计算）")
        return

    for pw, names in pws.items():
        ok = 0
        for p in dbs:
            page = page1(p)
            enc = hashlib.pbkdf2_hmac("sha512", bytes.fromhex(pw), page[:16], args.iters, 32)
            if hmac_ok(enc, page):
                ok += 1
        print(f"\n🎯 passphrase = {pw}")
        print(f"   能开 {ok}/{len(dbs)} 个库（由 {sorted(names)} 反推）")


if __name__ == "__main__":
    main()
