#!/usr/bin/env node
// dsh-plugin-atuowexin 安装脚本（macOS / Linux，或任何有 Node 的环境）
//
// 作用：按本机目录生成 cordis.patch.yml（DSH 的 bundle 补丁层需要绝对路径），
//       并在需要时从 config.example.json 准备一份 config.json。
//
// 用法（在本目录执行）：
//     node install.mjs

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)));
const lib = join(root, "lib/index.js");
const tpl = join(root, "cordis.patch.yml.tpl");
const out = join(root, "cordis.patch.yml");

if (!existsSync(lib)) throw new Error(`找不到实现文件：${lib}`);
if (!existsSync(tpl)) throw new Error(`找不到模板：${tpl}`);

const workdir = join(root, "data");
const config = join(root, "config.json");
mkdirSync(workdir, { recursive: true });
if (!existsSync(config)) {
  const example = join(root, "config.example.json");
  const cfg = JSON.parse(readFileSync(example, "utf8"));
  cfg.workdir = workdir;
  writeFileSync(config, JSON.stringify(cfg, null, 2) + "\n", "utf8");
}

let text = readFileSync(tpl, "utf8");
text = text.replaceAll("{{LIB}}", () => lib);
text = text.replaceAll("{{WORKDIR}}", () => join(root, "data"));
text = text.replaceAll("{{CONFIG}}", () => join(root, "config.json"));
writeFileSync(out, text, "utf8");

console.log(`[ok] 已生成：${out}`);
console.log("");
console.log("下一步：");
console.log(`  1) 让 DSH 里的 agent 执行：plugin_manager install_bundle  file:${root}`);
console.log(`  2) 或在 GUI「设置 → 插件 → 安装 → 本地路径」填：${root}`);
