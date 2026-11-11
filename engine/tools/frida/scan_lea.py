#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PE 静态定位器：找字符串 → 找引用它的 RIP-relative lea → 用 .pdata 反查函数入口。

跑在哪：任意一台 Linux / Windows 的 python3（纯标准库，不装依赖）。
解决什么：微信每升一个小版本，偏移就变。真实流程是
  「特征字符串 → 引用指令 → 函数入口 → 喂 Frida 下钩子」，
本脚本把这三步压成一条命令，省得每版重跑 find_mmv1.py + wx_func.py。

用法：
  # 找 MMV1 引用（ascii + utf16le 都扫），输出候选函数表
  python3 scan_lea.py --dll Weixin.dll --pattern MMV1

  # 多特征一起扫，出 JSON 给下一步用
  python3 scan_lea.py --dll Weixin.dll --pattern MMV1 --pattern Config.Cipher --json rva.json

  # 已知 RVA，要函数边界 + 入口字节
  python3 scan_lea.py --dll Weixin.dll --func 0x35D6319

  # 直接给字节特征（十六进制），不依赖可打印串
  python3 scan_lea.py --dll Weixin.dll --pattern-hex 4d4d5631

  # 近邻上下文（判断这个函数大概在干什么）
  python3 scan_lea.py --dll Weixin.dll --func 0x35D6319 --ctx 64

依赖：无。x64 才做 lea 扫描；x86 只出字符串命中 RVA。
部署：不部署，仓内 `wx/` 直接跑；稳定后在生活档脚本库加一行。
"""
import argparse
import json
import re
import struct
import sys
from pathlib import Path


# ---------- PE 基础 ----------

class PE:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 0x40:
            raise ValueError("文件太小，不是 PE")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("NOT_PE：e_lfanew 处没有 PE 签名")
        self.pe = pe
        coff = pe + 4
        self.nsec = struct.unpack_from("<H", data, coff + 2)[0]
        optsz = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        self.magic = struct.unpack_from("<H", data, opt)[0]
        if self.magic == 0x20B:          # PE32+
            self.is64 = True
            self.image_base = struct.unpack_from("<Q", data, opt + 24)[0]
            dd = opt + 112
        elif self.magic == 0x10B:        # PE32
            self.is64 = False
            self.image_base = struct.unpack_from("<I", data, opt + 28)[0]
            dd = opt + 96
        else:
            raise ValueError("未知 optional header magic %#x" % self.magic)
        self.exc_rva, self.exc_size = struct.unpack_from("<II", data, dd + 3 * 8)
        self.secs = []
        sec_off = opt + optsz
        for i in range(self.nsec):
            o = sec_off + i * 40
            name = data[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
            vs, va, rs, rp = struct.unpack_from("<IIII", data, o + 8)
            self.secs.append({"name": name, "va": va, "vs": vs, "rp": rp, "rs": rs})

    def off2rva(self, off):
        for s in self.secs:
            if s["rp"] <= off < s["rp"] + s["rs"]:
                return s["va"] + (off - s["rp"])
        return None

    def rva2off(self, rva):
        for s in self.secs:
            if s["va"] <= rva < s["va"] + max(s["vs"], s["rs"]):
                return s["rp"] + (rva - s["va"])
        return None

    def section(self, name):
        return next((s for s in self.secs if s["name"] == name), None)

    # ---- .pdata：函数边界 ----
    def pdata(self):
        """返回 [(begin_rva, end_rva, unwind_rva)]，按 begin 升序；无表则空。"""
        if not self.exc_rva or not self.exc_size:
            return []
        off = self.rva2off(self.exc_rva)
        if off is None:
            return []
        out = []
        for i in range(self.exc_size // 12):
            b, e, u = struct.unpack_from("<III", self.data, off + i * 12)
            if b == 0:
                continue
            out.append((b, e, u))
        out.sort()
        return out

    def func_of(self, rva, table=None):
        """二分找包含 rva 的函数；返回 (begin, end) 或 None。"""
        table = table if table is not None else self.pdata()
        if not table:
            return None
        lo, hi = 0, len(table) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            b, e, _ = table[mid]
            if rva < b:
                hi = mid - 1
            elif rva >= e:
                lo = mid + 1
            else:
                return b, e
        return None

    def bytes_at_rva(self, rva, n):
        off = self.rva2off(rva)
        if off is None:
            return b""
        return self.data[off:off + n]


# ---------- 扫描 ----------

def find_patterns(pe: PE, ascii_pat=None, raw_pat=None):
    """返回 [(file_off, rva, tag)]。"""
    hits = []
    pats = []
    if ascii_pat:
        pats.append((ascii_pat.encode("utf-8"), "ascii"))
        pats.append((ascii_pat.encode("utf-16le"), "utf16le"))
    if raw_pat:
        pats.append((raw_pat, "hex"))
    data = pe.data
    for pat, tag in pats:
        if not pat:
            continue
        i = 0
        while True:
            i = data.find(pat, i)
            if i < 0:
                break
            hits.append((i, pe.off2rva(i), tag))
            i += 1
    seen, uniq = set(), []
    for off, rva, tag in hits:
        if off in seen:
            continue
        seen.add(off)
        uniq.append((off, rva, tag))
    return uniq


def find_lea_refs(pe: PE, target_rvas):
    """在 .text 里找 RIP-relative lea（x64: 48/4C 8d /r，modrm&0xC7==0x05）。"""
    text = pe.section(".text")
    if not text:
        return []
    buf = pe.data[text["rp"]:text["rp"] + text["rs"]]
    va = text["va"]
    refs = []
    i = 0
    n = len(buf)
    while i + 7 <= n:
        # REX.W 8d /r  → modrm 的 mod=00, rm=101 即 RIP-relative
        if buf[i] in (0x48, 0x4C) and buf[i + 1] == 0x8D:
            modrm = buf[i + 2]
            if (modrm & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", buf, i + 3)[0]
                instr_rva = va + i
                tgt = instr_rva + 7 + disp
                if tgt in target_rvas:
                    refs.append({"lea_rva": instr_rva, "target_rva": tgt,
                                 "target_off": pe.rva2off(tgt)})
                i += 7
                continue
        i += 1
    return refs


def describe(pe: PE, rva, ctx=0):
    b = pe.bytes_at_rva(rva, 16 + ctx)
    return " ".join("%02x" % x for x in b)


def fmt_rva(v):
    return "None" if v is None else "0x%X" % v


# ---------- 子命令 ----------

def cmd_func(pe: PE, rva: int, ctx: int):
    table = pe.pdata()
    if not table:
        print("没有 .pdata（不是 x64 或已剥离）")
        return 2
    got = pe.func_of(rva, table)
    if not got:
        print("RVA 0x%X 不在任何 .pdata 函数里" % rva)
        return 1
    b, e = got
    print("RVA      %s" % fmt_rva(rva))
    print("FUNC     %s - %s  (%d 字节)" % (fmt_rva(b), fmt_rva(e), e - b))
    print("VA       %#x" % (pe.image_base + b))
    print("BYTES    %s" % describe(pe, b, ctx))
    idx = next(i for i, (bb, _, _) in enumerate(table) if bb == b)
    for k in range(max(0, idx - 2), min(len(table), idx + 3)):
        bb, ee, _ = table[k]
        mark = " <==" if k == idx else ""
        print("  [%d] %s - %s (%d)%s" % (k, fmt_rva(bb), fmt_rva(ee), ee - bb, mark))
    return 0


def cmd_scan(pe: PE, args):
    raw = bytes.fromhex(args.pattern_hex.replace(" ", "")) if args.pattern_hex else None
    hits = []
    for p in args.pattern or []:
        hits.extend(find_patterns(pe, ascii_pat=p))
    if raw:
        hits.extend(find_patterns(pe, raw_pat=raw))
    # 去重
    seen, uniq = set(), []
    for off, rva, tag in hits:
        if off in seen:
            continue
        seen.add(off)
        uniq.append((off, rva, tag))

    print("文件     %s" % args.dll)
    print("架构     %s   image_base=%#x   节=%d"
          % ("x64" if pe.is64 else "x86", pe.image_base, pe.nsec))
    print("特征     文件内命中 %d 处" % len(uniq))
    for off, rva, tag in uniq[:40]:
        print("  off=%#x rva=%s tag=%s" % (off, fmt_rva(rva), tag))
    if len(uniq) > 40:
        print("  … 其余 %d 处省略" % (len(uniq) - 40))

    target_rvas = {rva for _, rva, _ in uniq if rva}
    if not target_rvas:
        print("\n没有可用 RVA（特征可能落在无节区/重叠区），停止")
        return 2

    if not pe.is64:
        print("\nx86 不做 lea 扫描（无 RIP-relative 寻址）；命中 RVA 已给，自己按 32 位绝对寻址找引用")
        return 0

    refs = find_lea_refs(pe, target_rvas)
    print("\nlea 引用 %d 处" % len(refs))
    table = pe.pdata()
    rows = []
    for r in refs:
        fn = pe.func_of(r["lea_rva"], table) if table else None
        r["func"] = fn[0] if fn else None
        r["func_end"] = fn[1] if fn else None
        r["bytes"] = describe(pe, r["func"]) if fn else ""
        rows.append(r)
        print("  lea %s -> %s   func=%s%s"
              % (fmt_rva(r["lea_rva"]), fmt_rva(r["target_rva"]),
                 fmt_rva(r["func"]),
                 "  [%s]" % r["bytes"][:47] if r["bytes"] else ""))

    funcs = []
    seenf = set()
    for r in rows:
        if r["func"] is None or r["func"] in seenf:
            continue
        seenf.add(r["func"])
        funcs.append({"func_rva": r["func"], "func_va": pe.image_base + r["func"],
                      "func_end": r["func_end"], "hits": sum(
                          1 for x in rows if x["func"] == r["func"]),
                      "bytes": r["bytes"]})
    funcs.sort(key=lambda x: -x["hits"])
    print("\n候选函数 %d 个（按引用数排）" % len(funcs))
    for f in funcs:
        print("  %s  VA=%#x  refs=%d  %s"
              % (fmt_rva(f["func_rva"]), f["func_va"], f["hits"], f["bytes"][:47]))

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"dll": str(args.dll), "image_base": pe.image_base,
             "patterns": list(args.pattern or []) + ([args.pattern_hex] if args.pattern_hex else []),
             "hits": [{"off": o, "rva": r, "tag": t} for o, r, t in uniq],
             "refs": rows, "funcs": funcs}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nJSON: %s" % args.json)
    return 0


def main():
    ap = argparse.ArgumentParser(description="PE 字符串→lea→函数入口 定位器")
    ap.add_argument("--dll", required=True, help="PE 文件路径（.dll/.exe）")
    ap.add_argument("--pattern", action="append", help="ASCII 特征串，可多次给；自动同时扫 utf16le")
    ap.add_argument("--pattern-hex", help="十六进制字节特征，如 4d4d5631")
    ap.add_argument("--func", help="已知 RVA（0x…），只查函数边界")
    ap.add_argument("--ctx", type=int, default=32, help="--func 时打印多少字节入口（默认 32）")
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args()

    path = Path(args.dll).expanduser()
    if not path.is_file():
        sys.exit("文件不存在: %s" % path)
    pe = PE(path.read_bytes())
    if args.func:
        return cmd_func(pe, int(args.func, 16), args.ctx)
    if not args.pattern and not args.pattern_hex:
        sys.exit("要么给 --pattern / --pattern-hex，要么用 --func 查函数")
    return cmd_scan(pe, args)


if __name__ == "__main__":
    sys.exit(main())
