"""Message dates survive replay and are rendered only for model requests."""
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.providers.base import LLMProvider
from nanobot.providers.openai_responses.converters import convert_user_message
from nanobot.session.manager import Session


class MessageTimestampTests(unittest.TestCase):
    def test_history_preserves_each_date_without_changing_stored_text(self):
        session = Session(key="cli:dates")
        session.add_message("user", "Hola", timestamp="2026-11-05T10:00:00-06:00")
        session.add_message("assistant", "Hola también", timestamp="2026-11-05T10:00:03-06:00")
        original = deepcopy(session.messages)
        history = session.get_history()
        outgoing = LLMProvider._sanitize_empty_content(history)
        for stored, rendered in zip(original, outgoing):
            self.assertIn(stored["timestamp"], rendered["content"])
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
        self.assertIn(stamp, outgoing[-1]["content"])
        self.assertEqual(messages[-1]["content"], "Continuemos")
        pending = {"role": "user", "content": "Otra cosa", "_meta": {"timestamp": stamp}}
        self.assertIn(stamp, LLMProvider._sanitize_empty_content([pending])[0]["content"])
        self.assertEqual(pending["content"], "Otra cosa")

    def test_multimodal_content_and_tool_call_identity_survive(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        messages = [
            {"role": "user", "content": [image], "timestamp": "2026-11-06T14:30:00"},
            {"role": "tool", "content": "done", "tool_call_id": "call-1", "timestamp": "2026-11-06T14:31:00"},
        ]
        original = deepcopy(messages)
        outgoing = LLMProvider._sanitize_empty_content(messages)
        converted = convert_user_message(outgoing[0]["content"])
        self.assertEqual(converted["content"][0]["type"], "input_image")
        self.assertIn(messages[0]["timestamp"], converted["content"][-1]["text"])
        self.assertEqual(outgoing[1]["tool_call_id"], "call-1")
        self.assertIn(messages[1]["timestamp"], outgoing[1]["content"])
        self.assertEqual(messages, original)

    def test_legacy_message_without_date_is_not_given_an_invented_date(self):
        message = {"role": "user", "content": "Old message"}
        self.assertEqual(LLMProvider._sanitize_empty_content([message]), [message])
