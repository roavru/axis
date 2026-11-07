"""Message dates survive replay and are rendered only for model requests."""
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.providers.base import LLMProvider
from nanobot.providers.openai_responses.converters import convert_user_message
from nanobot.session.manager import Session


class MessageTimestampTests(unittest.TestCase):
    def test_history_preserves_user_dates_without_annotating_assistant_replies(self):
        session = Session(key="cli:dates")
        session.add_message("user", "Hola", timestamp="2026-11-05T10:00:00-06:00")
        session.add_message("assistant", "Hola también", timestamp="2026-11-05T10:00:03-06:00")
        original = deepcopy(session.messages)
        history = session.get_history()
        outgoing = LLMProvider._sanitize_empty_content(history)
        self.assertEqual(outgoing[0]["content"], 'Hola\n\n<metadata>\ntimestamp: "2026-11-05 10:00:00"\n</metadata>')
        self.assertEqual(outgoing[1]["content"], "Hola también")
        for rendered in outgoing:
            self.assertNotIn("timestamp", rendered)
        self.assertEqual(session.messages, original)
        self.assertEqual(LLMProvider._sanitize_empty_content(history), outgoing)
        self.assertEqual(LLMProvider._sanitize_empty_content(outgoing), outgoing)

    def test_current_message_uses_receipt_time_and_pending_input_uses_metadata(self):
        stamp = "2026-11-06T14:30:00-06:00"
        with TemporaryDirectory() as directory:
            builder = ContextBuilder(Path(directory))
            messages = builder.build_transcript(TranscriptInput(
                history=[], current_message="Continuemos", timestamp=stamp,
            ))
        outgoing = LLMProvider._sanitize_empty_content(messages)
        self.assertIn('timestamp: "2026-11-06 14:30:00"', outgoing[-1]["content"])
        self.assertEqual(messages[-1]["content"], "Continuemos")
        pending = {"role": "user", "content": "Otra cosa", "_meta": {"timestamp": stamp}}
        self.assertIn('timestamp: "2026-11-06 14:30:00"', LLMProvider._sanitize_empty_content([pending])[0]["content"])
        self.assertEqual(pending["content"], "Otra cosa")

    def test_multimodal_content_and_tool_call_identity_survive(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        messages = [
            {"role": "user", "content": [image], "timestamp": "2026-11-06T20:30:00+00:00"},
            {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
                "_meta": {"timestamp": "2026-11-06T20:30:30+00:00"},
            },
            {"role": "tool", "content": "done", "tool_call_id": "call-1", "timestamp": "2026-11-06T20:31:00+00:00"},
        ]
        original = deepcopy(messages)
        outgoing = LLMProvider._sanitize_empty_content(messages)
        converted = convert_user_message(outgoing[0]["content"])
        self.assertEqual(converted["content"][0]["type"], "input_image")
        self.assertEqual(converted["content"][-1]["text"], '<metadata>\ntimestamp: "2026-11-06 14:30:00"\n</metadata>')
        self.assertIsNone(outgoing[1]["content"])
        self.assertEqual(outgoing[1]["tool_calls"], messages[1]["tool_calls"])
        self.assertEqual(outgoing[2], {"role": "tool", "content": "done", "tool_call_id": "call-1"})
        self.assertEqual(messages, original)

    def test_conversion_changes_calendar_date_and_drops_offset_and_microseconds(self):
        for timestamp, expected in (
            ("2026-11-07T01:30:45.123456Z", "2026-11-06 19:30:45"),
            ("2026-11-06T06:00:00+02:00", "2026-11-05 22:00:00"),
        ):
            with self.subTest(timestamp=timestamp):
                outgoing = LLMProvider._sanitize_empty_content([
                    {"role": "user", "content": "Hola", "timestamp": timestamp},
                ])
                self.assertEqual(outgoing[0]["content"], f'Hola\n\n<metadata>\ntimestamp: "{expected}"\n</metadata>')

    def test_naive_server_timestamp_is_interpreted_in_host_local_time(self):
        instant = datetime(2026, 11, 6, 20, 30, tzinfo=timezone.utc)
        timestamp = datetime.fromtimestamp(instant.timestamp()).isoformat()
        outgoing = LLMProvider._sanitize_empty_content([
            {"role": "user", "content": "Hola", "timestamp": timestamp},
        ])
        self.assertEqual(outgoing[0]["content"], 'Hola\n\n<metadata>\ntimestamp: "2026-11-06 14:30:00"\n</metadata>')

    def test_invalid_imported_timestamp_does_not_break_model_request_or_invent_date(self):
        session = Session(key="cli:imported")
        session.add_message("user", "Old message", timestamp="unknown")
        self.assertEqual(LLMProvider._sanitize_empty_content(session.get_history()), [
            {"role": "user", "content": "Old message"},
        ])

    def test_legacy_message_without_date_is_not_given_an_invented_date(self):
        message = {"role": "user", "content": "Old message"}
        self.assertEqual(LLMProvider._sanitize_empty_content([message]), [message])
