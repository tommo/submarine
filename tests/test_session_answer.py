"""`pending` / `answer` on the session-control surface: a question, permission
or plan the sheet is waiting on can be read and resolved from outside Sublime
(CLI, web UI), viewless or bound."""
from __future__ import annotations

import unittest

import features.session_control as sc
from core.registry import default_registry
from tests.test_single_view import RecordingWindow, _SingleViewCase, _session
from ui.host import HostView

QUESTIONS = [
    {"header": "Lib", "question": "Which library?",
     "options": [{"label": "requests", "description": "simple"},
                 {"label": "httpx", "description": "async"}]},
    {"header": "Feats", "question": "Which features?", "multiSelect": True,
     "options": [{"label": "cache"}, {"label": "retry"}, {"label": "tracing"}]},
]


class _AnswerCase(_SingleViewCase):
    def setUp(self):
        super().setUp()
        self.win = RecordingWindow()
        self.s = _session(self.win, "asker")
        default_registry.register_session(self.s)
        self.got = []

    def ask(self):
        self.s.output.question_request(7, [dict(q) for q in QUESTIONS],
                                       lambda answers: self.got.append(answers))


class TestPending(_AnswerCase):
    def test_nothing_pending(self):
        body, ref = sc.action_pending({"ref": self.s.agent_id})
        self.assertEqual(body["modals"], [])
        self.assertIsNone(body["waiting"])
        self.assertEqual(ref["agent_id"], self.s.agent_id)

    def test_question_is_described_with_the_current_one(self):
        self.ask()
        body, _ = sc.action_pending({"ref": self.s.agent_id})
        self.assertEqual(body["waiting"], "question")
        q = body["modals"][0]["payload"]
        self.assertEqual(q["qid"], 7)
        self.assertEqual(q["total"], 2)
        self.assertEqual(q["question"]["question"], "Which library?")
        self.assertEqual([o["label"] for o in q["question"]["options"]], ["requests", "httpx"])

    def test_permission_input_is_clipped(self):
        self.s.output.permission_request(3, "Write", {"file_path": "/x", "content": "x" * 5000},
                                         lambda r: self.got.append(r))
        body, _ = sc.action_pending({"ref": self.s.agent_id})
        self.assertEqual(body["waiting"], "permission")
        content = body["modals"][0]["payload"]["tool_input"]["content"]
        self.assertLess(len(content), 2100)
        self.assertTrue(content.endswith("…"))

    def test_list_row_reports_waiting(self):
        self.ask()
        row = sc._live_row(self.s)
        self.assertEqual(row["waiting"], "question")


class TestAnswerQuestion(_AnswerCase):
    def test_option_index_then_multi_select_completes_the_callback(self):
        self.ask()
        body, _ = sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": 2})
        self.assertEqual(body["answered"], "question")
        self.assertEqual(body["waiting"], "question")          # question 2 of 2 now
        self.assertEqual(body["modals"][0]["payload"]["current_idx"], 1)
        self.assertEqual(self.got, [])
        body, _ = sc.action_answer({"ref": self.s.agent_id, "kind": "question",
                                    "options": [1, "tracing"]})
        self.assertIsNone(body["waiting"])
        self.assertEqual(self.got, [{"Which library?": "httpx",
                                     "Which features?": ["cache", "tracing"]}])

    def test_free_text_answer(self):
        self.ask()
        sc.action_answer({"ref": self.s.agent_id, "kind": "question", "text": "urllib3"})
        self.assertEqual(self.s.output.modals.pending_question.answers, {"Which library?": "urllib3"})

    def test_answer_records_the_decision_in_the_sheet(self):
        self.s.output.prompt("pick one")   # a turn to record the ☑ line into
        self.ask()
        sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": "requests"})
        events = "".join(e for e in self.s.output.current.events if isinstance(e, str))
        self.assertIn("☑ Lib → requests", events)

    def test_out_of_range_and_stale_are_refused(self):
        self.ask()
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": 9})
        self.assertEqual(cm.exception.code, "bad_request")
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": 1, "qid": 99})
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual(self.got, [])

    def test_nothing_pending_is_not_found(self):
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": 1})
        self.assertEqual(cm.exception.code, "not_found")

    def test_works_while_bound_with_the_composer_open(self):
        hv = HostView.for_window(self.win)
        hv.attach(self.win, self.s)
        self.ask()
        try:
            self.s.output.enter_input_mode()
        except Exception:
            pass
        body, _ = sc.action_answer({"ref": self.s.agent_id, "kind": "question", "option": 1})
        self.assertEqual(body["modals"][0]["payload"]["current_idx"], 1)


class TestAnswerPermissionAndPlan(_AnswerCase):
    def test_permission_responses(self):
        self.s.output.permission_request(5, "Bash", {"command": "ls"}, lambda r: self.got.append(r))
        with self.assertRaises(sc.ControlError):
            sc.action_answer({"ref": self.s.agent_id, "kind": "permission", "response": "maybe"})
        with self.assertRaises(sc.ControlError) as cm:
            sc.action_answer({"ref": self.s.agent_id, "kind": "permission", "response": "allow", "id": 6})
        self.assertEqual(cm.exception.code, "stale")
        body, _ = sc.action_answer({"ref": self.s.agent_id, "kind": "permission",
                                    "response": "allow", "id": 5})
        self.assertEqual(self.got, ["allow"])
        self.assertIsNone(body["waiting"])

    def test_allow_session_is_delivered_as_allow(self):
        self.s.output.permission_request(5, "Bash", {"command": "ls"}, lambda r: self.got.append(r))
        sc.action_answer({"ref": self.s.agent_id, "kind": "permission", "response": "allow_session"})
        self.assertEqual(self.got, ["allow"])

    def test_plan(self):
        self.s.output.plan_approval_request(2, "/tmp/plan.md", [], lambda r: self.got.append(r))
        body, _ = sc.action_pending({"ref": self.s.agent_id})
        self.assertEqual(body["waiting"], "plan")
        self.assertEqual(body["modals"][0]["payload"]["plan_file"], "/tmp/plan.md")
        body, _ = sc.action_answer({"ref": self.s.agent_id, "kind": "plan", "response": "reject"})
        self.assertEqual(self.got, ["reject"])
        self.assertIsNone(body["waiting"])

    def test_dispatch_routes_the_new_actions(self):
        self.ask()
        env = sc.dispatch({"action": "pending", "ref": self.s.agent_id})
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["waiting"], "question")
        env = sc.dispatch({"action": "answer", "ref": self.s.agent_id, "kind": "question", "option": 1})
        self.assertTrue(env["ok"])


if __name__ == "__main__":
    unittest.main()
