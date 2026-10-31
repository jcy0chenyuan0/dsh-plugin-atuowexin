# -*- coding: utf-8 -*-
"""数据模型测试：类型映射、时间归一化、脱敏、转录渲染、消息去重键。"""

from __future__ import annotations

import unittest

from atuowexin.model import (
	Conversation,
	Message,
	classify,
	conversation_kind,
	conversation_slug,
	format_transcript,
	normalize_ts,
	parse_time_text,
	redact,
	strip_markup,
	xml_fields,
)


class ClassifyTests(unittest.TestCase):
	"""消息类型 → 归一化 kind。"""

	def test_text_and_media(self):
		self.assertEqual(classify(1, 0), "text")
		self.assertEqual(classify(3, 0), "image")
		self.assertEqual(classify(34, 0), "voice")
		self.assertEqual(classify(43, 0), "video")
		self.assertEqual(classify(47, 0), "sticker")

	def test_app_subtypes(self):
		self.assertEqual(classify(49, 6), "file")
		self.assertEqual(classify(49, 57), "quote")
		self.assertEqual(classify(49, 2000), "transfer")
		self.assertEqual(classify(49, 2001), "red_packet")
		self.assertEqual(classify(49, 9999), "app")

	def test_system_subtypes(self):
		self.assertEqual(classify(10000, 0), "system")
		self.assertEqual(classify(10000, 4), "pat")

	def test_unknown_is_not_guessed(self):
		self.assertEqual(classify(123456, 0), "unknown")
		self.assertEqual(classify(None, None), "unknown")


class TimeTests(unittest.TestCase):
	"""时间归一化：秒/毫秒/字符串都要落到秒。"""

	def test_seconds_and_milliseconds(self):
		self.assertEqual(normalize_ts(1_760_000_000), 1_760_000_000)
		self.assertEqual(normalize_ts(1_760_000_000_000), 1_760_000_000)
		self.assertEqual(normalize_ts("1760000000"), 1_760_000_000)
		self.assertEqual(normalize_ts(None), 0)

	def test_rfc3339(self):
		# 带 +08:00 偏移的时间字符串应换算成正确的 Unix 秒
		self.assertEqual(parse_time_text("2026-01-01T08:00:00+08:00"), 1767225600)
		self.assertEqual(normalize_ts("2026-01-01T08:00:00+08:00"), 1767225600)


class RedactTests(unittest.TestCase):
	"""脱敏：手机号/身份证/银行卡。"""

	def test_masks(self):
		text = "我的手机 13812345678，身份证 110101199001011234，卡号 6222021234567890123"
		masked = redact(text)
		self.assertNotIn("13812345678", masked)
		self.assertNotIn("110101199001011234", masked)
		self.assertNotIn("6222021234567890123", masked)
		self.assertIn("138****5678", masked)

	def test_can_disable(self):
		text = "13812345678"
		self.assertEqual(redact(text, phone=False), text)


class ContentTests(unittest.TestCase):
	"""XML 摘要与标签剥离。"""

	def test_xml_fields(self):
		xml = '<msg><appmsg><title><![CDATA[需求说明书]]></title><des>请查收</des><type>6</type><filename>需求.docx</filename></appmsg></msg>'
		fields = xml_fields(xml)
		self.assertEqual(fields["title"], "需求说明书")
		self.assertEqual(fields["des"], "请查收")
		self.assertEqual(fields["filename"], "需求.docx")

	def test_strip_markup(self):
		self.assertEqual(strip_markup("<a>你好</a>\n<b>世界</b>"), "你好 世界")


class ConversationTests(unittest.TestCase):
	"""会话身份：类型判定与稳定 slug。"""

	def test_kind(self):
		self.assertEqual(conversation_kind("45678901234@chatroom"), "group")
		self.assertEqual(conversation_kind("gh_abc123"), "official")
		self.assertEqual(conversation_kind("wxid_abc"), "friend")
		self.assertEqual(conversation_kind("filehelper"), "service")

	def test_slug_is_stable_and_distinct(self):
		first = conversation_slug("产品群", "45678901234@chatroom")
		second = conversation_slug("产品群", "45678901234@chatroom")
		other = conversation_slug("产品群", "99999999999@chatroom")
		self.assertEqual(first, second)
		self.assertNotEqual(first, other)
		self.assertNotIn("/", first)

	def test_slug_survives_illegal_names(self):
		slug = conversation_slug("../../坏名字:群*聊", "wxid_x")
		self.assertNotIn("/", slug)
		self.assertNotIn("..", slug)


class MessageTests(unittest.TestCase):
	"""消息 uid / 渲染 / 序列化往返。"""

	def _message(self, **kwargs):
		payload = {
			"conversation_id": "wxid_a",
			"seq": 3,
			"server_id": 0,
			"sender_id": "wxid_b",
			"sender_name": "小林",
			"is_self": False,
			"ts": 1_760_000_000,
			"type": 1,
			"kind": "text",
			"text": "在吗",
		}
		payload.update(kwargs)
		return Message(**payload)

	def test_uid_prefers_server_id(self):
		self.assertEqual(self._message(server_id=99).uid, "wxid_a|s99")
		self.assertEqual(self._message().uid, "wxid_a|l3")

	def test_uid_falls_back_to_hash(self):
		message = self._message(seq=0)
		self.assertTrue(message.uid.startswith("wxid_a|h"))

	def test_display_line_handles_media(self):
		message = self._message(kind="image", text="")
		self.assertIn("[图片]", message.display_line())
		self.assertIn("小林", message.display_line())

	def test_roundtrip(self):
		original = self._message(media=[{"kind": "image", "md5": "a" * 32}], raw={"x": 1})
		restored = Message.from_row(original.to_row())
		self.assertEqual(restored.uid, original.uid)
		self.assertEqual(restored.media, original.media)
		self.assertEqual(restored.raw, original.raw)
		again = Message.from_dict(original.to_dict())
		self.assertEqual(again.text, original.text)

	def test_transcript_marks_self_and_ids(self):
		messages = [self._message(is_self=True, sender_name="我"), self._message()]
		text = format_transcript(messages)
		self.assertIn("我:", text)
		self.assertIn("小林(wxid_b)", text)

	def test_transcript_truncates_from_oldest(self):
		messages = [self._message(seq=index, server_id=index, text=f"消息{index}" * 20) for index in range(10)]
		text = format_transcript(messages, max_chars=200)
		self.assertNotIn("消息0消息0", text)
		self.assertIn("消息9", text)

	def test_conversation_to_dict(self):
		conversation = Conversation(id="wxid_a", name="小林", kind="friend")
		restored = Conversation.from_dict(conversation.to_dict())
		self.assertEqual(restored.id, conversation.id)
		self.assertEqual(restored.display_name, "小林")


if __name__ == "__main__":
	unittest.main()
