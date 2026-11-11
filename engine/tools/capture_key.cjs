// 启动期 Hook 捕获微信 4.x 的数据库密钥（移植自 WeChat-Export-Tool v1.2.1 scripts/get_key.js）
//
// 与参考实现的差别：输出目录、DLL 目录、超时都由命令行参数给（不再写死 Desktop/wx_export），
// 状态与密钥都落在 --out 指定的目录里，便于上层（atuowexin）读取并校验后写入 keymap.json。
//
// 用法：node capture_key.js --out <dir> --dll <dir> --timeout <ms>
'use strict';
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

const OUT = arg('out', path.join(process.cwd(), 'capture'));
const DLL_DIR = arg('dll', path.join(__dirname, 'dll'));
const TIMEOUT_MS = parseInt(arg('timeout', '120000'), 10) || 120000;
const STATUS_FILE = path.join(OUT, 'key_status.txt');
const KEY_FILE = path.join(OUT, 'key.txt');
fs.mkdirSync(OUT, { recursive: true });
try { fs.unlinkSync(STATUS_FILE); } catch (e) {}
try { fs.unlinkSync(KEY_FILE); } catch (e) {}

function setStatus(s) { try { fs.writeFileSync(STATUS_FILE, s); } catch (e) {} }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function decode(buf) {
  const idx = buf.indexOf(0);
  return Buffer.from(buf.slice(0, idx >= 0 ? idx : buf.length)).toString('utf-8').trim();
}

async function findWeChatPid() {
  const names = ['Weixin.exe', 'WeChat.exe'];
  for (const name of names) {
    try {
      const out = execFileSync('C:\\Windows\\System32\\tasklist.exe', ['/FI', 'IMAGENAME eq ' + name, '/FO', 'CSV', '/NH'],
                               { encoding: 'utf8', timeout: 5000 });
      for (const line of out.split('\n')) {
        if (line.toLowerCase().includes(name.replace('.exe', '').toLowerCase())) {
          const pid = parseInt((line.split('","')[1] || '').replace(/"/g, ''), 10);
          if (!isNaN(pid)) return pid;
        }
      }
    } catch (e) {}
  }
  return null;
}

async function waitWeChatExit() {
  for (let i = 0; i < 40; i++) {
    if (!(await findWeChatPid())) return true;
    await sleep(500);
  }
  return false;
}

async function waitWeChatStart() {
  for (let i = 0; i < 120; i++) {
    const pid = await findWeChatPid();
    if (pid) return pid;
    await sleep(500);
  }
  return null;
}

function loadWxKey() {
  const dllPath = path.join(DLL_DIR, 'wx_key.dll');
  if (!fs.existsSync(dllPath)) {
    console.error('[KEY] 找不到 wx_key.dll: ' + dllPath);
    setStatus('dll_not_found');
    return null;
  }
  setStatus('dll_found');
  let koffi;
  try {
    koffi = require('koffi');
  } catch (e) {
    console.error('[KEY] 加载 koffi 失败: ' + e.message);
    setStatus('koffi_missing');
    return null;
  }
  const lib = koffi.load(dllPath);
  setStatus('dll_loaded');
  return {
    lib: lib,
    initHook: lib.func('bool InitializeHook(uint32 targetPid)'),
    pollKey: lib.func('bool PollKeyData(_Out_ char* buf, int size)'),
    getStatus: lib.func('bool GetStatusMessage(_Out_ char* buf, int size, _Out_ int* level)'),
    cleanup: lib.func('bool CleanupHook()'),
    getError: lib.func('const char* GetLastErrorMsg()')
  };
}

async function main() {
  setStatus('started');
  const api = loadWxKey();
  if (!api) process.exit(1);

  const existing = await findWeChatPid();
  if (existing) {
    console.log('[KEY] 微信仍在运行，等待关闭...');
    setStatus('waiting_close');
    if (!(await waitWeChatExit())) {
      console.log('[KEY] 等待微信关闭超时');
      setStatus('timeout_close');
      process.exit(1);
    }
    console.log('[KEY] 微信已关闭');
  }

  console.log('[KEY] 请打开微信并登录');
  setStatus('waiting_start');
  const pid = await waitWeChatStart();
  if (!pid) {
    console.log('[KEY] 等待微信启动超时');
    setStatus('timeout_start');
    process.exit(1);
  }
  console.log('[KEY] 检测到微信 PID: ' + pid);

  setStatus('injecting');
  if (!api.initHook(pid)) {
    let msg = '未知错误';
    try { const err = api.getError(); if (err) msg = decode(err); } catch (e) {}
    console.log('[KEY] Hook 注入失败: ' + msg);
    setStatus('hook_failed:' + msg.substring(0, 60));
    process.exit(1);
  }
  console.log('[KEY] Hook 注入成功，等待 SetDBKey 调用...');
  setStatus('hook_ok');

  for (let i = 0; i < 10; i++) {
    const buf = Buffer.alloc(512);
    const level = [0];
    try {
      while (api.getStatus(buf, buf.length, level)) {
        const m = decode(buf);
        if (m) console.log('  [' + level[0] + '] ' + m);
      }
    } catch (e) {}
    await sleep(200);
  }

  console.log('[KEY] 等待登录过程中捕获密钥...');
  setStatus('polling');
  const buf = Buffer.alloc(128);
  const start = Date.now();
  while (Date.now() - start < TIMEOUT_MS) {
    let hit = false;
    try { hit = api.pollKey(buf, buf.length); } catch (e) {}
    if (hit) {
      const key = buf.toString('ascii').substring(0, 64).toLowerCase();
      if (/^[0-9a-f]{64}$/.test(key)) {
        fs.writeFileSync(KEY_FILE, key);
        console.log('[KEY] 成功: ' + key.substring(0, 16) + '...');
        setStatus('captured');
        try { api.cleanup(); } catch (e) {}
        process.exit(0);
      }
    }
    try {
      const sbuf = Buffer.alloc(512);
      const level = [0];
      while (api.getStatus(sbuf, sbuf.length, level)) {
        const m = decode(sbuf);
        if (m) console.log('  [' + level[0] + '] ' + m);
      }
    } catch (e) {}
    await sleep(200);
  }

  console.log('[KEY] 获取超时');
  setStatus('timeout_poll');
  try { api.cleanup(); } catch (e) {}
  process.exit(1);
}

main().catch(e => {
  console.error('[KEY] 错误: ' + (e && e.message));
  setStatus('error:' + String(e && e.message).substring(0, 80));
  process.exit(1);
});
