#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""frida_hook.py — 从微信 4.x（Windows）进程里抓数据库密钥材料。

做什么
  spawn 微信（必须在进程启动早期挂钩，attach 会错过初始化），
  等 Weixin.dll 载入后在 codec 配置函数入口下钩子，
  每次命中把 rcx/rdx/r8/r9 指向的内存各 dump 64 字节，写成 JSONL。

配合
  scan_lea.py   先算出函数 RVA（"MMV1" → lea 引用 → .pdata 函数入口）
  validate.py   从 dump 出来的候选里判定 passphrase / enc_key

用法
  python frida_hook.py --rva 0x35D62E0 --out hits.jsonl
  python frida_hook.py --exe "C:\\Program Files\\Tencent\\Weixin\\Weixin.exe" --rva 0x35D62E0 --seconds 600

依赖
  pip install frida
"""
from __future__ import annotations

import argparse
import json
import sys
import time

DEFAULT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
DEFAULT_DLL = "Weixin.dll"

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=DEFAULT_EXE, help="Weixin.exe 路径")
    ap.add_argument("--dll", default=DEFAULT_DLL, help="主模块名，默认 Weixin.dll")
    ap.add_argument("--rva", default="0x35D62E0", help="codec 配置函数 RVA（scan_lea.py 输出）")
    ap.add_argument("--seconds", type=int, default=300, help="等待登录/开库的秒数")
    ap.add_argument("--max-hits", type=int, default=400, help="最多记录多少次命中")
    ap.add_argument("--out", default="hits.jsonl", help="候选输出文件")
    args = ap.parse_args()

    try:
        import frida
    except ImportError:
        sys.exit("缺 frida：pip install frida")

    js = (JS_TEMPLATE
          .replace("__RVA__", args.rva)
          .replace("__DLL__", args.dll)
          .replace("__MAXHITS__", str(args.max_hits)))

    fh = open(args.out, "w", encoding="utf-8")

    def on_message(msg, data):
        if msg.get("type") == "send":
            p = msg["payload"]
            if "log" in p:
                print("[frida]", p["log"], flush=True)
                return
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
            fh.flush()
            print("[hit]", p.get("n"), flush=True)
        else:
            print("[frida:error]", json.dumps(msg)[:300], flush=True)

    print("[*] spawn", args.exe, flush=True)
    pid = frida.spawn(args.exe)
    session = frida.attach(pid)
    script = session.create_script(js)
    script.on("message", on_message)
    script.load()
    frida.resume(pid)
    print(f"[*] resumed pid={pid}, 请在微信里登录（或点「进入微信」），等待 {args.seconds}s", flush=True)

    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
    print(f"[*] 结束，候选写在 {args.out}", flush=True)


if __name__ == "__main__":
    main()
