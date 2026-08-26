"""Ask-user elicitation: Kimi q0 mapping + Grok _x.ai/ask_user_question.

Invariants: Kimi handleQuestion only accepts /^q0_opt_N$/; listed
Q1 goes through elicitation accept. Other/freetext is a second
session/prompt after end_turn — not cancel+reprompt. Grok answers
are {outcome:accepted, answers:{q:[labels]}} (§9.25).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, Optional

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

from rpc_helpers import send_notification  # noqa: E402


class AskUserMixin:
    def _is_ask_user_permission(
            self, tool_name: str, options: list,
            tool_call: Optional[dict] = None) -> bool:
        """Kimi encodes AskUserQuestion choices as permission optionIds."""
        if tool_name in (
                "AskUserQuestion", "ask_user", "ask_user_question",
                "AskUser"):
            return True
        title = ((tool_call or {}).get("title") or "")
        if isinstance(title, str) and "question" in title.lower():
            return True
        for o in options or []:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("optionId") or "")
            # q0_opt_0 / q1_opt_2 / q0_skip
            if re.match(r"^q\d+_opt_\d+$", oid) or re.match(
                    r"^q\d+_skip$", oid):
                return True
        return False

    async def _acp_elicitation_create(self, params: dict) -> dict:
        """kimi-code AskUser: full question set via elicitation/create form."""
        questions, keys = self._questions_from_elicitation(params)
        if not questions:
            self.file_log("elicitation/create: empty schema; cancel")
            return {"action": "cancel"}
        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"elicitation/create cancelled: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)
        if answers is None:
            return {"action": "cancel"}
        content = self._elicitation_content_from_answers(
            questions, keys, answers)
        if not content:
            return {"action": "cancel"}
        extra = self._kimi_followup_answers(questions, answers)
        if extra and self._kimi_answers_dropped(questions, answers, content, keys):
            # Other cannot enter the tool result (enum filter). Chain a
            # second session/prompt AFTER this turn end_turn — not cancel.
            self._pending_ask_followup = extra
        self.file_log(
            f"elicitation/create accept keys={list(content.keys())}"
            f" freetext_followup={bool(self._pending_ask_followup)}")
        return {"action": "accept", "content": content}

    @staticmethod
    def _questions_from_elicitation(params: dict):
        """Map kimi requestedSchema (q0/q1…) into plugin question rows."""
        schema = (params or {}).get("requestedSchema") or {}
        props = schema.get("properties") or {}
        if not isinstance(props, dict) or not props:
            return [], []
        required = schema.get("required") or []
        keys = [k for k in required if k in props]
        for k in sorted(props.keys()):
            if k not in keys:
                keys.append(k)
        msg_lines = [
            ln for ln in str((params or {}).get("message") or "").split("\n")
            if ln.strip()
        ]
        questions = []
        for i, key in enumerate(keys):
            prop = props.get(key) or {}
            if not isinstance(prop, dict):
                continue
            if prop.get("type") == "array":
                items = prop.get("items") or {}
                enums = items.get("anyOf") or items.get("oneOf") or []
                multi = True
            else:
                enums = prop.get("oneOf") or prop.get("anyOf") or []
                multi = False
            options = []
            for e in enums:
                if not isinstance(e, dict):
                    continue
                label = e.get("const") or e.get("title") or ""
                if label == "" and e.get("const") is not None:
                    label = str(e.get("const"))
                options.append({
                    "label": str(label),
                    "description": str(e.get("description") or ""),
                })
            qtext = msg_lines[i] if i < len(msg_lines) else ""
            if not qtext:
                qtext = str(prop.get("title") or "Question?")
            questions.append({
                "question": qtext,
                "header": str(prop.get("title") or ""),
                "options": options,
                "multiSelect": multi,
            })
        return questions, keys

    @staticmethod
    def _elicitation_content_from_answers(
            questions: list, keys: list, answers: dict) -> dict:
        """Plugin answers → {q0: label, q1: [labels]} for kimi form accept."""
        if not isinstance(answers, dict):
            return {}
        content: Dict[str, Any] = {}
        for i, q in enumerate(questions or []):
            if not isinstance(q, dict):
                continue
            key = keys[i] if i < len(keys) else f"q{i}"
            val = None
            for k in (q.get("question") or "", q.get("header") or ""):
                if k and k in answers:
                    val = answers[k]
                    break
            if val is None:
                continue
            allowed = [
                str(o.get("label") or "")
                for o in (q.get("options") or [])
                if isinstance(o, dict)
            ]
            if q.get("multiSelect"):
                raw = list(val) if isinstance(val, (list, tuple)) else [val]
                picked = []
                for item in raw:
                    label = str(item or "")
                    if label in allowed:
                        picked.append(label)
                        continue
                    for ol in allowed:
                        if AskUserMixin._labels_match(label, ol):
                            picked.append(ol)
                            break
                # declared order
                picked = [ol for ol in allowed if ol in picked]
                if picked:
                    content[key] = picked
                continue
            label = (
                str(val[0]) if isinstance(val, (list, tuple)) and val
                else str(val or "")
            )
            if label in allowed:
                content[key] = label
                continue
            for ol in allowed:
                if AskUserMixin._labels_match(label, ol):
                    content[key] = ol
                    break
        return content

    async def _handle_acp_ask_user_permission(
            self, tool_call: dict, options: list,
            tool_input: dict) -> dict:
        """Show question UI; return selected optionId (Kimi q0_opt_N / skip)."""
        raw = tool_call.get("rawInput") or {}
        if not isinstance(raw, dict):
            raw = {}
        questions = (
            raw.get("questions")
            or (tool_input or {}).get("questions")
            or []
        )
        questions = self._normalize_questions(questions)

        # Fallback: synthesize one question from permission options
        if not questions:
            choices = [
                o for o in (options or [])
                if isinstance(o, dict)
                and str(o.get("kind") or "").startswith("allow")
                and not str(o.get("optionId") or "").startswith("approve")
            ]
            q_text = ""
            for c in (tool_call.get("content") or []):
                if not isinstance(c, dict):
                    continue
                body = c.get("content")
                if isinstance(body, dict) and body.get("text"):
                    q_text = str(body["text"])
                    break
                if c.get("type") == "text" and c.get("text"):
                    q_text = str(c["text"])
                    break
            questions = [{
                "question": q_text or "Choose an option:",
                "header": "",
                "options": [
                    {"label": o.get("name") or o.get("optionId") or "?",
                     "description": ""}
                    for o in choices
                ],
                "multiSelect": False,
            }]

        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"ask_user permission cancelled: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)

        skip_id = next(
            (o.get("optionId") for o in (options or [])
             if isinstance(o, dict) and (
                 "skip" in str(o.get("optionId") or "").lower()
                 or str(o.get("kind") or "").startswith("reject"))),
            None,
        )
        if answers is None:
            if skip_id:
                return {"outcome": {
                    "outcome": "selected", "optionId": skip_id,
                }}
            return {"outcome": {"outcome": "cancelled"}}

        # Kimi ACP handleQuestion only accepts /^q0_opt_(\d+)$/.
        # Anything else (Skip, freeform, q1_opt_*) → tool result
        # "User dismissed the question without answering."
        label = self._first_answer_label(answers, questions)
        oid = self._kimi_q0_option_id(options, questions, label)
        extra = self._kimi_followup_answers(questions, answers)
        if extra:
            self._pending_ask_followup = extra
        if oid:
            self.file_log(
                f"ask_user permission selected label={label!r} optionId={oid!r}")
            return {"outcome": {"outcome": "selected", "optionId": oid}}

        # Other / freeform is not a q0_opt. A fake first-option lied.
        # outcomeToQuestionAnswer returns null for unknown ids (dismissed).
        # fallback = self._kimi_first_q0_option_id(options)
        # if fallback:
        #     return {"outcome": {"outcome": "selected", "optionId": fallback}}
        other = self._kimi_other_followup(questions, answers, label)
        if other:
            self._pending_ask_followup = other
        self.file_log(f"ask_user unmatched label={label!r} → skip/cancel")
        if skip_id:
            return {"outcome": {
                "outcome": "selected", "optionId": skip_id,
            }}
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _answer_as_label(v) -> str:
        if isinstance(v, (list, tuple)) and v:
            return str(v[0])
        return str(v) if v is not None and str(v) != "" else ""

    @staticmethod
    def _first_answer_label(answers: dict, questions: list) -> str:
        if not isinstance(answers, dict) or not answers:
            return ""
        if questions:
            q0 = questions[0] if isinstance(questions[0], dict) else {}
            for key in (q0.get("question") or "", q0.get("header") or ""):
                if key and key in answers:
                    got = AskUserMixin._answer_as_label(answers[key])
                    if got:
                        return got
        for v in answers.values():
            got = AskUserMixin._answer_as_label(v)
            if got:
                return got
        return ""

    @staticmethod
    def _is_kimi_q0_opt(option_id: str) -> bool:
        return bool(re.match(r"^q0_opt_\d+$", option_id or ""))

    @staticmethod
    def _labels_match(a: str, b: str) -> bool:
        a = (a or "").strip().lower()
        b = (b or "").strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        a2 = re.sub(r"\s*\(recommended\)\s*$", "", a).strip()
        b2 = re.sub(r"\s*\(recommended\)\s*$", "", b).strip()
        if a2 and b2 and a2 == b2:
            return True
        # Description / Other text often contains or is contained by the label.
        if len(a2) >= 8 and (a2 in b2 or b2 in a2):
            return True
        return False

    @staticmethod
    def _kimi_q0_option_id(options: list, questions: list, label: str) -> str:
        """Map a UI answer onto Kimi's /^q0_opt_N$/ permission id.

        Kimi outcomeToQuestionAnswer returns null (dismissed) for any other id.
        """
        label = (label or "").strip()
        if not label:
            return ""
        named = AskUserMixin._match_option_id_for_label(options, label)
        if AskUserMixin._is_kimi_q0_opt(named):
            return named
        q0 = questions[0] if questions and isinstance(questions[0], dict) else {}
        qopts = q0.get("options") or []
        for i, opt in enumerate(qopts):
            if isinstance(opt, dict):
                olabel = str(opt.get("label") or opt.get("name") or "")
                odesc = str(opt.get("description") or "")
            else:
                olabel, odesc = str(opt), ""
            if (AskUserMixin._labels_match(label, olabel)
                    or AskUserMixin._labels_match(label, odesc)):
                cand = f"q0_opt_{i}"
                if any(
                    isinstance(o, dict) and o.get("optionId") == cand
                    for o in (options or [])
                ):
                    return cand
        return ""

    @staticmethod
    def _kimi_first_q0_option_id(options: list) -> str:
        for o in options or []:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("optionId") or "")
            if AskUserMixin._is_kimi_q0_opt(oid):
                return oid
        return ""

    @staticmethod
    def _kimi_followup_answers(questions: list, answers: dict) -> str:
        """Text for Q1+ (Kimi ACP drops them) or a full recap when useful."""
        if not isinstance(answers, dict) or not answers:
            return ""
        if not questions or len(questions) < 2:
            return ""
        lines = [
            "The user answered AskUserQuestion. ACP only forwards the "
            "first question — do NOT treat this as dismissed. Honor every "
            "answer below:",
        ]
        for q in questions:
            if not isinstance(q, dict):
                continue
            header = q.get("header") or ""
            qtext = q.get("question") or header or "Question"
            val = ""
            for key in (q.get("question") or "", header):
                if key and key in answers:
                    val = AskUserMixin._answer_as_label(answers[key])
                    if val:
                        break
            if not val:
                continue
            prefix = f"{header}: " if header and header != qtext else ""
            lines.append(f"- {prefix}{qtext}: {val}" if prefix else f"- {qtext}: {val}")
        if len(lines) <= 1:
            return ""
        lines.append(
            "If a tool result said the user dismissed or only includes "
            "the first choice, ignore that and use this list.")
        return "\n".join(lines)

    @staticmethod
    def _kimi_other_followup(questions: list, answers: dict, label: str) -> str:
        q0 = ""
        if questions and isinstance(questions[0], dict):
            q0 = questions[0].get("question") or questions[0].get("header") or ""
        return (
            "The user answered AskUserQuestion with custom text "
            f"(not a listed option){': ' + q0 if q0 else ''}: {label}. "
            "Do NOT treat this as dismissed."
        )

    def _flush_ask_followup(self) -> None:
        # Dead: interrupt+followup was the anti-pattern. Keep the helper
        # for tests that still parse the source.
        text = getattr(self, "_pending_ask_followup", None)
        self._pending_ask_followup = None
        if text:
            # self._inject_ask_user_followup(text)
            self.file_log(
                f"ask_user follow-up dropped ({len(text)} chars); "
                "elicitation listed labels only")

    @staticmethod
    def _kimi_answers_dropped(
            questions: list, answers: dict, content: dict, keys: list) -> bool:
        """True when the UI answered something elicitation content omitted.

        Kimi elicitationResponseToQuestionAnswers keeps only values that
        match a declared option label. Other/freeform is dropped.
        """
        if not isinstance(answers, dict) or not answers:
            return False
        content = content or {}
        for i, q in enumerate(questions or []):
            if not isinstance(q, dict):
                continue
            val = ""
            for key in (q.get("question") or "", q.get("header") or ""):
                if key and key in answers:
                    val = AskUserMixin._answer_as_label(answers[key])
                    if val:
                        break
            if not val:
                continue
            ck = keys[i] if i < len(keys) else f"q{i}"
            got = content.get(ck)
            if got is None:
                return True
            if isinstance(got, list):
                if val not in [str(x) for x in got]:
                    return True
            elif str(got) != val:
                return True
        return False

    def _inject_ask_user_followup(self, text: str) -> None:
        # Anti-pattern: session/cancel then a prose recap. listed Q1 is
        # elicitation/create content {q0, q1, ...}. Other is unsupported.
        if not text or not str(text).strip():
            return
        # send_notification("notification_wake", {
        #     "wake_prompt": str(text).strip(),
        #     "display_message": "AskUserQuestion answers",
        #     "interrupt": True,
        # })
        self.file_log(
            f"ask_user follow-up NOT injected ({len(text)} chars)")

    @staticmethod
    def _match_option_id_for_label(options: list, label: str) -> str:
        label = (label or "").strip()
        if not label:
            return ""
        # Exact name match first
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip()
            if name == label:
                return str(o.get("optionId") or "")
        # Case-insensitive exact / Recommended suffix
        low = label.lower()
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip()
            nlow = name.lower()
            if nlow == low:
                return str(o.get("optionId") or "")
            # Strip "(Recommended)" noise on either side
            n_clean = re.sub(r"\s*\(recommended\)\s*$", "", nlow).strip()
            l_clean = re.sub(r"\s*\(recommended\)\s*$", "", low).strip()
            if n_clean and n_clean == l_clean:
                return str(o.get("optionId") or "")
        return ""

    @staticmethod
    def _find_other_option_id(options: list) -> str:
        """Locate system-added Other / freeform option (Kimi adds one)."""
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip().lower()
            oid = str(o.get("optionId") or "")
            if name in (
                "other", "other...", "other…", "custom",
                "type your own", "something else",
            ):
                return oid
            if name.startswith("other"):
                return oid
            if re.search(r"(^|_)(other|freeform|custom)(_|$)", oid.lower()):
                return oid
        return ""

    def _normalize_questions(self, questions: list) -> list:
        """Normalize Grok/Claude question payloads for the plugin UI.

        Plugin expects: [{question, options:[{label, description?}], multiSelect}]
        Grok may send multi_select or multiSelect.
        """
        out = []
        for q in questions or []:
            if not isinstance(q, dict):
                continue
            opts_in = q.get("options") or []
            opts = []
            for o in opts_in:
                if isinstance(o, dict):
                    opts.append({
                        "label": o.get("label") or o.get("name") or str(o),
                        "description": o.get("description") or "",
                    })
                else:
                    opts.append({"label": str(o), "description": ""})
            out.append({
                "question": q.get("question") or q.get("header") or "Question?",
                "header": q.get("header") or "",
                "options": opts,
                "multiSelect": bool(
                    q.get("multiSelect", q.get("multi_select", False))),
            })
        return out

    def _format_ask_user_answers(self, answers: dict) -> dict:
        """Normalize plugin answers → Grok AskUserQuestionExtResponse shape.

        Grok expects (internally-tagged, snake_case outcomes):
          {outcome: "accepted", answers: {q: [label, ...]}, partial_answers: {}}

        Answer values are ALWAYS lists of strings (single-select has one element;
        multi-select has many; freeform-only uses ["Other"] / free text).
        """
        norm: Dict[str, list] = {}
        if not isinstance(answers, dict):
            return norm
        for k, v in answers.items():
            key = str(k)
            if isinstance(v, (list, tuple)):
                labels = [str(x) for x in v if x is not None and str(x) != ""]
            elif v is None:
                labels = []
            else:
                labels = [str(v)]
            if labels:
                norm[key] = labels
        return norm

    async def _acp_ask_user_question(self, params: dict) -> dict:
        """Handle Grok `_x.ai/ask_user_question` → plugin question UI.

        Request shape (observed):
          {sessionId, toolCallId, questions:[{question, options, multiSelect}], mode}

        Response outcomes (Grok AskUserQuestionExtResponse):
          accepted | chat_about_this | skip_interview | cancelled
        Accepted payload:
          {outcome: "accepted",
           answers: {questionText: [selectedLabel, ...]},
           partial_answers: {}}
        """
        questions = self._normalize_questions(params.get("questions") or [])
        self.file_log(
            f"ask_user_question: {len(questions)} question(s) "
            f"mode={params.get('mode')!r} toolCallId={params.get('toolCallId')}")
        if not questions:
            return {
                "outcome": "accepted",
                "answers": {},
                "partial_answers": {},
            }

        # session/update already opened this id — do not paint a second ☐.
        tool_call_id = params.get("toolCallId") or f"ask_{self.permission_id + 1}"
        if tool_call_id not in self._tool_ids_emitted:
            self._tool_ids_emitted.add(tool_call_id)
            self._tool_names_by_id[tool_call_id] = "ask_user"
            send_notification("message", {
                "type": "tool_use",
                "id": tool_call_id,
                "name": "ask_user",
                "input": {"questions": questions},
            })

        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"ask_user_question cancelled/error: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)

        if answers is None:
            # User cancelled / interrupted the question UI.
            if tool_call_id not in self._tool_results_sent:
                self._tool_results_sent.add(tool_call_id)
                send_notification("message", {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": "User cancelled",
                    "is_error": True,
                })
            return {"outcome": "cancelled"}

        # Plugin returns {question_text: answer_label_or_list}.
        norm = self._format_ask_user_answers(answers)

        summary = "; ".join(
            f"{k}: {', '.join(v)}" for k, v in norm.items())
        if tool_call_id not in self._tool_results_sent:
            self._tool_results_sent.add(tool_call_id)
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": summary or "answered",
                "is_error": False,
            })
        self.file_log(f"ask_user_question answers: {json.dumps(norm)[:400]}")
        return {
            "outcome": "accepted",
            "answers": norm,
            "partial_answers": {},
        }
