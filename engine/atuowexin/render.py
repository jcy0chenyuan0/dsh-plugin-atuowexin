# -*- coding: utf-8 -*-
"""多格式渲染：把归一化消息渲染成 txt / html / xlsx / pdf。

来源说明：这几个渲染器的**版式**参考了 WeChat-Export-Tool v1.2.1 的 exporters/ 目录
（Telegram 风格气泡 HTML、带样式的 Excel 表、分页 PDF），但**数据模型换成本仓库的**
Conversation / Message，不再依赖 WCDB 与微信原始字段；因此本模块是纯函数式的：
给它消息列表 + 输出路径，它只写文件。

依赖策略（与引擎其余部分一致：默认只用标准库）：

* txt / html：**零依赖**，永远可用。
* xlsx：需要 openpyxl（DSH 自带运行时已含）；缺了就抛 RenderError 并给安装提示。
* pdf：需要 fpdf2；缺了同样抛 RenderError，绝不静默降级成别的格式。

为什么不做静默降级：上层（CLI / 插件工具）要能如实告诉用户「你要的 PDF 没生成」，
而不是给一个后缀不对的文件。
"""

from __future__ import annotations

import base64
import datetime
import html as _html
from pathlib import Path
from typing import Sequence

from .model import Conversation, Message, format_ts

#: 本模块负责的格式（其余格式仍由 atuowexin.extract 自己的 jsonl/csv/md 实现）。
SUPPORTED = ("txt", "html", "xlsx", "pdf")

#: 可选依赖缺失时给用户的提示。
_DEPS_HINT = {
	"xlsx": "pip install openpyxl（或用 DSH 自带运行时，它已内置 openpyxl）",
	"pdf": "pip install fpdf2（或用 python -m atuowexin deps install fpdf2）",
}

#: 非文本消息的占位标签。
_PLACEHOLDER = {
	"image": "[图片]",
	"sticker": "[表情]",
	"emoji": "[表情]",
	"voice": "[语音]",
	"video": "[视频]",
	"file": "[文件]",
	"link": "[链接]",
	"card": "[名片]",
	"location": "[位置]",
	"call": "[通话]",
	"redpacket": "[红包]",
	"transfer": "[转账]",
	"system": "[系统消息]",
}

#: 媒体字典里可能存放本机文件路径的键名（不同后端用词不同，按序尝试）。
_MEDIA_PATH_KEYS = ("path", "local_path", "file", "file_path", "thumb", "thumb_path")


class RenderError(RuntimeError):
	"""渲染失败（多为缺少可选依赖）。"""


# --------------------------------------------------------------------------- #
# 公共小工具
# --------------------------------------------------------------------------- #


def _who(message: Message, self_name: str = "我") -> str:
	"""显示名：本人用 self_name，对方优先备注名，退化到 id。"""
	if message.is_self:
		return self_name
	return message.sender_name or message.sender_id or "未知"


def _body(message: Message) -> str:
	"""正文：没有正文的非文本消息用 [图片] 这类占位。"""
	text = (message.text or "").strip()
	if text:
		return text
	return _PLACEHOLDER.get(message.kind, "[%s]" % message.label)


def _local_media_path(message: Message) -> str:
	"""从 media 里挑出一个真实存在的本机文件路径；没有就返回空串。"""
	for item in message.media or []:
		if not isinstance(item, dict):
			continue
		for key in _MEDIA_PATH_KEYS:
			value = item.get(key)
			if isinstance(value, str) and value and Path(value).is_file():
				return value
	return ""


def _timestamp(message: Message):
	"""秒级时间戳 -> 本地 datetime；非法值返回 None。"""
	try:
		return datetime.datetime.fromtimestamp(int(message.ts))
	except (TypeError, ValueError, OSError):
		return None


def _clean(message: Message, redacted: bool) -> str:
	"""正文 + 可选脱敏。"""
	body = _body(message)
	if redacted:
		from .model import redact as _redact

		body = _redact(body)
	return body


# --------------------------------------------------------------------------- #
# txt
# --------------------------------------------------------------------------- #


def export_txt(
	messages: Sequence[Message],
	conversation: Conversation,
	path,
	*,
	redacted: bool = False,
	self_name: str = "我",
) -> str:
	"""导出成纯文本：头部元信息 + 每条消息一行 [时间] 说话人: 内容。"""
	target = Path(path)
	lines = [
		"会话：%s" % conversation.display_name,
		"会话 id：%s" % conversation.id,
		"类型：%s" % conversation.kind,
		"消息数：%d" % len(messages),
		"",
	]
	for message in messages:
		lines.append(
			"[%s] %s: %s"
			% (format_ts(message.ts, "%Y-%m-%d %H:%M:%S"), _who(message, self_name), _clean(message, redacted))
		)
	target.write_text("\n".join(lines) + "\n", encoding="utf-8")
	return str(target)


# --------------------------------------------------------------------------- #
# html（版式参考 WeChat-Export-Tool/exporters/html_exporter.py）
# --------------------------------------------------------------------------- #

_HTML_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;background:#e8ecef;color:#1f2a37}
.page{max-width:860px;margin:0 auto;padding:12px 16px;min-height:100vh}
.header{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:14px 20px;margin-bottom:12px;position:sticky;top:12px;z-index:10}
.title{font-size:16px;font-weight:600;display:inline}
.meta{color:#6b7280;font-size:13px;display:inline;margin-left:12px}
.controls{margin-top:8px}
.controls input[type="search"]{width:240px;border-radius:8px;border:1px solid #e5e7eb;padding:6px 10px;font-size:13px;font-family:inherit}
.date-sep{text-align:center;margin:16px 0 12px}
.date-sep span{display:inline-block;background:rgba(0,0,0,.06);color:#6b7280;font-size:12px;padding:4px 14px;border-radius:12px}
.msg{display:flex;flex-direction:column;margin-bottom:10px;max-width:80%}
.msg.left{align-items:flex-start}
.msg.right{align-items:flex-end;margin-left:auto}
.msg .sender{font-size:12px;color:#6b7280;margin-bottom:3px;padding:0 4px}
.msg .sender .time{font-size:11px;opacity:.7}
.msg .bubble{padding:10px 14px;font-size:14px;line-height:1.6;word-break:break-word;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.msg.left .bubble{background:#fff;border-radius:12px;border-bottom-left-radius:4px}
.msg.right .bubble{background:#6ab5ff;color:#fff;border-radius:12px;border-bottom-right-radius:4px}
.msg.right .sender{text-align:right}
.msg .bubble img{max-width:300px;max-height:300px;border-radius:4px;display:block}
.msg .bubble .ph{opacity:.75;font-style:italic}
.msg.hidden{display:none}
@media(max-width:600px){.page{padding:8px}.msg{max-width:90%}.controls input[type="search"]{width:160px}}
"""

_HTML_JS = """
function filterMessages(){
  var kw=document.getElementById('searchInput').value.trim().toLowerCase();
  var msgs=document.querySelectorAll('.msg');
  for(var i=0;i<msgs.length;i++){
    var t=msgs[i].textContent.toLowerCase();
    msgs[i].classList.toggle('hidden', kw && t.indexOf(kw)<0);
  }
  var dates=document.querySelectorAll('.date-sep');
  for(var i=0;i<dates.length;i++){
    var n=dates[i].nextElementSibling, visible=false;
    while(n && !n.classList.contains('date-sep')){
      if(n.classList.contains('msg') && !n.classList.contains('hidden')){visible=true;break;}
      n=n.nextElementSibling;
    }
    dates[i].style.display=visible?'':'none';
  }
}
"""


def _image_tag(message: Message, image_dir) -> str:
	"""图片消息 -> img 标签；本机没有原图就退回占位文字。"""
	local = _local_media_path(message)
	if not local:
		return '<span class="ph">[图片]</span>'
	try:
		data = Path(local).read_bytes()
	except OSError:
		return '<span class="ph">[图片]</span>'
	suffix = Path(local).suffix.lower().lstrip(".") or "png"
	if suffix not in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
		return '<span class="ph">[图片]</span>'
	# 落一份到 <会话目录>/图片/，让 HTML 自包含、便于整包拷走
	if image_dir is not None:
		try:
			image_dir.mkdir(parents=True, exist_ok=True)
			name = "img_%s.%s" % (message.seq or message.server_id or abs(hash(local)), suffix)
			(Path(image_dir) / name).write_bytes(data)
			return '<img src="图片/%s" alt="图片" loading="lazy">' % _html.escape(name)
		except OSError:
			pass
	encoded = base64.b64encode(data).decode("ascii")
	return '<img src="data:image/%s;base64,%s" alt="图片" loading="lazy">' % (suffix, encoded)


def export_html(
	messages: Sequence[Message],
	conversation: Conversation,
	path,
	*,
	redacted: bool = False,
	self_name: str = "我",
) -> str:
	"""导出成可搜索的单文件 HTML 聊天记录（左右气泡 + 日期分隔 + 图片内嵌）。"""
	target = Path(path)
	image_dir = target.parent / "图片"
	blocks = []
	last_date = ""

	for message in messages:
		moment = _timestamp(message)
		date_text = moment.strftime("%Y-%m-%d") if moment else ""
		clock = moment.strftime("%H:%M:%S") if moment else ""
		if date_text and date_text != last_date:
			blocks.append('<div class="date-sep"><span>%s</span></div>' % date_text)
			last_date = date_text
		side = "right" if message.is_self else "left"
		if message.kind in ("image", "sticker", "emoji"):
			if message.kind == "image":
				body = _image_tag(message, image_dir)
			else:
				body = '<span class="ph">%s</span>' % _PLACEHOLDER.get(message.kind, "[图片]")
		else:
			body = _html.escape(_clean(message, redacted)).replace("\n", "<br>")
		blocks.append(
			'<div class="msg %s"><div class="sender">%s  <span class="time">%s</span></div>'
			'<div class="bubble">%s</div></div>'
			% (side, _html.escape(_who(message, self_name)), clock, body)
		)

	title = _html.escape(conversation.display_name)
	meta = "%d 条消息 · %s" % (len(messages), conversation.kind)
	document = (
		'<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
		'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
		"<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n<div class=\"page\">\n"
		'<div class="header"><div><h1 class="title">%s</h1><span class="meta">%s</span></div>'
		'<div class="controls"><input id="searchInput" type="search" placeholder="搜索消息..." oninput="filterMessages()"></div></div>\n'
		'<div class="msg-list" id="msgList">\n%s\n</div>\n</div>\n'
		"<script>%s</script>\n</body>\n</html>\n"
	) % (title, _HTML_CSS, title, meta, "\n".join(blocks), _HTML_JS)
	target.write_text(document, encoding="utf-8")
	return str(target)


# --------------------------------------------------------------------------- #
# xlsx（样式参考 WeChat-Export-Tool/exporters/excel_exporter.py）
# --------------------------------------------------------------------------- #


def export_xlsx(
	messages: Sequence[Message],
	conversation: Conversation,
	path,
	*,
	redacted: bool = False,
	self_name: str = "我",
) -> str:
	"""导出成 Excel：冻结首行 + 自动筛选 + 正文自动换行。"""
	try:
		from openpyxl import Workbook
		from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
	except ImportError as error:  # pragma: no cover - 取决于环境
		raise RenderError("导出 xlsx 需要 openpyxl：%s" % _DEPS_HINT["xlsx"]) from error

	workbook = Workbook()
	sheet = workbook.active
	sheet.title = "聊天记录"
	for column, width in (("A", 20), ("B", 18), ("C", 10), ("D", 70)):
		sheet.column_dimensions[column].width = width

	header_font = Font(bold=True, color="FFFFFF", size=11)
	header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
	header_align = Alignment(horizontal="center", vertical="center")
	border = Border(
		left=Side(style="thin", color="E5E7EB"),
		right=Side(style="thin", color="E5E7EB"),
		top=Side(style="thin", color="E5E7EB"),
		bottom=Side(style="thin", color="E5E7EB"),
	)
	sheet.append(["时间", "发送者", "类型", "消息内容"])
	for cell in sheet[1]:
		cell.font = header_font
		cell.fill = header_fill
		cell.alignment = header_align
		cell.border = border

	for message in messages:
		sheet.append(
			[format_ts(message.ts, "%Y-%m-%d %H:%M:%S"), _who(message, self_name), message.label, _clean(message, redacted)]
		)
		row = sheet.max_row
		for index in range(1, 5):
			cell = sheet.cell(row=row, column=index)
			cell.alignment = Alignment(vertical="top", wrap_text=(index == 4))
			cell.border = border

	sheet.freeze_panes = "A2"
	sheet.auto_filter.ref = "A1:D%d" % sheet.max_row
	workbook.save(str(path))
	return str(path)


# --------------------------------------------------------------------------- #
# pdf（版式参考 WeChat-Export-Tool/exporters/pdf_exporter.py）
# --------------------------------------------------------------------------- #


def export_pdf(
	messages: Sequence[Message],
	conversation: Conversation,
	path,
	*,
	redacted: bool = False,
	self_name: str = "我",
) -> str:
	"""导出成 PDF 聊天记录（需要 fpdf2；Windows 下用微软雅黑，缺字体则用内置字体）。"""
	try:
		from fpdf import FPDF
	except ImportError as error:  # pragma: no cover - 取决于环境
		raise RenderError("导出 pdf 需要 fpdf2：%s" % _DEPS_HINT["pdf"]) from error

	pdf = FPDF()
	pdf.set_auto_page_break(auto=True, margin=15)
	pdf.add_page()

	font_family = "Helvetica"
	for candidate, candidate_bold in (
		("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
		("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
	):
		try:
			pdf.add_font("zh", "", candidate)
			pdf.add_font("zh", "B", candidate_bold)
			font_family = "zh"
			break
		except Exception:  # noqa: BLE001 - 字体不可用就换下一个候选
			continue

	page_width = pdf.w - 2 * pdf.l_margin
	pdf.set_font(font_family, "B", 13)
	pdf.cell(page_width, 9, conversation.display_name, align="C")
	pdf.ln(9)
	pdf.set_font(font_family, "", 9)
	pdf.set_text_color(120, 128, 140)
	pdf.cell(page_width, 6, "%s · %d 条消息" % (conversation.id, len(messages)), align="C")
	pdf.ln(8)
	pdf.set_text_color(31, 42, 55)

	last_date = ""
	for message in messages:
		moment = _timestamp(message)
		if moment and moment.strftime("%Y-%m-%d") != last_date:
			last_date = moment.strftime("%Y-%m-%d")
			pdf.set_font(font_family, "B", 9)
			pdf.set_text_color(120, 128, 140)
			pdf.cell(page_width, 6, last_date, align="C")
			pdf.ln(7)
			pdf.set_text_color(31, 42, 55)

		pdf.set_font(font_family, "B", 8)
		pdf.cell(page_width, 5, "%s · %s" % (_who(message, self_name), format_ts(message.ts, "%H:%M")))
		pdf.ln(5)
		pdf.set_font(font_family, "", 10)
		pdf.multi_cell(page_width, 6, _clean(message, redacted))
		pdf.ln(2)

	pdf.output(str(path))
	return str(path)


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

_RENDERERS = {
	"txt": export_txt,
	"html": export_html,
	"xlsx": export_xlsx,
	"pdf": export_pdf,
}

_FILENAMES = {
	"txt": "messages.txt",
	"html": "messages.html",
	"xlsx": "messages.xlsx",
	"pdf": "messages.pdf",
}


def export_format(
	fmt: str,
	messages: Sequence[Message],
	conversation: Conversation,
	out_dir,
	*,
	redacted: bool = False,
	self_name: str = "我",
) -> str:
	"""按格式导出到 <out_dir>/messages.<ext>，返回生成的文件路径。"""
	renderer = _RENDERERS.get(fmt)
	if renderer is None:
		raise RenderError("不支持的格式：%s（可选：%s）" % (fmt, ", ".join(SUPPORTED)))
	target = Path(out_dir) / _FILENAMES[fmt]
	return renderer(messages, conversation, target, redacted=redacted, self_name=self_name)


__all__ = [
	"SUPPORTED",
	"RenderError",
	"export_format",
	"export_html",
	"export_pdf",
	"export_txt",
	"export_xlsx",
]
