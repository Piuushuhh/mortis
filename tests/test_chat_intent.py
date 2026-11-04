import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mortis_core as core
from desktop_spider import TypingAnimation


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": "A friendly local reply."}}


class _Scheduler:
    def __init__(self):
        self.pending = {}
        self.next_id = 0

    def after(self, _delay, callback):
        self.next_id += 1
        self.pending[self.next_id] = callback
        return self.next_id

    def after_cancel(self, callback_id):
        self.pending.pop(callback_id, None)

    def tick(self):
        callback_id, callback = next(iter(self.pending.items()))
        self.pending.pop(callback_id)
        callback()


class ChatIntentTests(unittest.TestCase):
    def test_intent_router_sends_greetings_to_chat_and_page_questions_to_retrieval(self):
        self.assertEqual(core.classify_intent("heyy"), "conversation")
        self.assertEqual(core.classify_intent("Hey Mortis!"), "conversation")
        self.assertEqual(core.classify_intent("How are you doing?"), "conversation")
        self.assertEqual(core.classify_intent("Hey Mortis, how's your day?"), "conversation")
        self.assertEqual(core.classify_intent("What did the captured Python page say about decorators?"), "knowledge")

    def test_conversational_response_uses_local_chat_and_recent_context(self):
        captured = {}

        def fake_post(endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured["payload"] = kwargs["json"]
            captured["timeout"] = kwargs["timeout"]
            return _Response()

        history = [
            {"role": "user", "content": "I finally finished my project."},
            {"role": "assistant", "content": "You sounded relieved."},
        ]
        with patch.object(core.requests, "post", fake_post):
            answer = core.conversational_response("Hey Mortis", "llama3.2", history)

        self.assertEqual(answer, "A friendly local reply.")
        self.assertTrue(captured["endpoint"].endswith("/api/chat"))
        self.assertEqual(captured["payload"]["model"], "llama3.2")
        self.assertEqual([m["role"] for m in captured["payload"]["messages"]],
                         ["system", "user", "assistant", "user"])
        self.assertEqual(captured["payload"]["messages"][-2]["content"],
                         "You sounded relieved.")
        personality = captured["payload"]["messages"][0]["content"]
        self.assertIn("AI research assistant", personality)
        self.assertIn("Sound natural", personality)
        self.assertIn("do not end every reply with a question", personality)
        self.assertIn("For programming", personality)
        self.assertLessEqual(captured["timeout"], 90)

    def test_retrieval_prompt_keeps_recent_context_and_real_source(self):
        with tempfile.TemporaryDirectory(prefix="mortis-chat-") as temporary:
            db = str(Path(temporary) / "knowledge.db")
            connection = core.conn(db)
            connection.execute(
                "INSERT INTO chunks(url,title,body,vec) VALUES(?,?,?,?)",
                ("https://example.test/python", "Python Guide",
                 "Decorators wrap a callable and return a replacement callable.",
                 json.dumps([1.0, 0.0, 0.0])),
            )
            connection.commit()
            connection.close()
            captured = {}

            class RetrievalResponse(_Response):
                def json(self):
                    return {"message": {"content": "It wraps the callable [Source 1]."}}

            def fake_post(_endpoint, **kwargs):
                captured["prompt"] = kwargs["json"]["messages"][0]["content"]
                return RetrievalResponse()

            with patch.object(core, "embed", return_value=[1.0, 0.0, 0.0]), \
                 patch.object(core.requests, "post", fake_post):
                answer, sources = core.ask(
                    "How does it work?", "llama3.2", "nomic-embed-text", db,
                    history=[
                        {"role": "user", "content": "What are Python decorators?"},
                        {"role": "assistant", "content": "They wrap functions."},
                    ],
                )

        self.assertIn("[Source 1]", answer)
        self.assertEqual(sources[0][1:3], ("https://example.test/python", "Python Guide"))
        self.assertIn("Recent conversation (context only)", captured["prompt"])
        self.assertIn("They wrap functions.", captured["prompt"])
        self.assertIn("https://example.test/python", captured["prompt"])

    def test_typing_animation_updates_and_stops_without_rescheduling(self):
        scheduler = _Scheduler()
        frames = []
        animation = TypingAnimation(scheduler.after, scheduler.after_cancel, frames.append)
        animation.start()
        self.assertEqual(frames, [TypingAnimation.FRAMES[0]])
        scheduler.tick()
        self.assertEqual(frames[-1], TypingAnimation.FRAMES[1])
        animation.stop()
        self.assertFalse(animation.active)
        self.assertEqual(scheduler.pending, {})
        count = len(frames)
        animation._tick()
        self.assertEqual(len(frames), count)


if __name__ == "__main__":
    unittest.main()
