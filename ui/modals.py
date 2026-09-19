"""Permission, plan-approval, and question UIs. Key handlers return bool."""
from __future__ import annotations

import os
import time
from typing import Callable, List, Optional

from plat.settings import load_project_settings

from . import keys
from .models import (
    PERM_ALLOW,
    PERM_ALLOW_ALL,
    PERM_ALLOW_SESSION,
    PERM_DENY,
    PLAN_APPROVE,
    PLAN_REJECT,
    PLAN_VIEW,
    PermissionRequest,
    PlanApproval,
    QuestionRequest,
)
from .pending import clear_pending_block

try:
    import sublime
except ImportError:
    sublime = None  # type: ignore

PERM_TIMEOUT_S = 30
SESSION_ALLOW_S = 30
_DANGEROUS_BASH = (
    "rm ", "rm\t",
    "git checkout", "git reset",
    "git clean", "git stash drop",
)


class ModalUI:
    """Turn-modal permission / plan / question blocks.

    Viewless convention: `_has_view()` is False when detached. Pending
    requests stay as live objects (callback + payload); `descriptors()`
    serializes them. Regions/phantoms are never carried across views —
    `drop_view_chrome()` nulls spans; `rerender_pending()` rebuilds them
    on the bound view. Answering/dismissing clears the live object, so
    the descriptor list empties.
    """

    def __init__(self, owner):
        self.owner = owner
        self.pending_permission = None  # type: Optional[PermissionRequest]
        self._permission_queue = []  # type: List[PermissionRequest]
        self.pending_plan = None  # type: Optional[PlanApproval]
        self.pending_question = None  # type: Optional[QuestionRequest]
        self.auto_allow_tools = set()
        self._last_allowed_tool = None
        self._last_allowed_time = 0.0
        self._perm_timeout_token = 0
        self._region_stash = None  # type: Optional[dict]

    def _has_view(self) -> bool:
        view = self.owner.view
        if not view:
            return False
        try:
            return bool(view.is_valid())
        except Exception:
            return True

    def _notify_detached(self, kind: str) -> None:
        """One-shot status bar for a request that arrived while viewless."""
        if self._has_view() or sublime is None:
            return
        name = "session"
        try:
            name = getattr(self.owner.sheet, "_name", None) or name
        except Exception:
            pass
        labels = {
            "permission": "needs permission",
            "plan": "needs plan approval",
            "question": "has a question",
        }
        try:
            sublime.status_message("Submarine: %s %s" % (
                name, labels.get(kind, kind)))
        except Exception:
            pass

    def descriptors(self) -> List[dict]:
        """Serializable pending-modal list (kind + payload). Callbacks omitted."""
        out = []  # type: List[dict]
        if self.pending_permission:
            out.append(self.pending_permission.descriptor())
        for perm in self._permission_queue:
            out.append(perm.descriptor())
        if self.pending_plan:
            out.append(self.pending_plan.descriptor())
        if self.pending_question:
            out.append(self.pending_question.descriptor())
        return out

    def _bump_dirty(self) -> None:
        """Mark the session dirty so a later attach does not take the clean path."""
        r = getattr(self.owner, "renderer", None)
        if r is not None:
            r.mark_chrome_dirty()

    def drop_view_chrome(self) -> None:
        """Erase named regions/phantoms on the bound view, then null offsets."""
        self._stash_regions()
        view = self.owner.view
        if view:
            for key in (
                keys.PERM_BLOCK, keys.PLAN_BLOCK, keys.QUESTION_BLOCK,
                keys.QUESTION_KEYS, keys.QUESTION_INPUT_MARKER,
            ):
                try:
                    view.erase_regions(key)
                except Exception:
                    pass
            perm = self.pending_permission
            if perm:
                for btn_type in list(perm.button_regions):
                    try:
                        view.erase_regions("%s%s" % (keys.PERM_BTN_PREFIX, btn_type))
                    except Exception:
                        pass
            plan = self.pending_plan
            if plan:
                for btn_type in list(plan.button_regions):
                    try:
                        view.erase_regions("%s%s" % (keys.PLAN_BTN_PREFIX, btn_type))
                    except Exception:
                        pass
        self.drop_regions()

    def _stash_regions(self) -> None:
        stash = {}  # type: dict
        if self.pending_permission:
            stash["permission"] = (
                self.pending_permission.region,
                dict(self.pending_permission.button_regions),
            )
        if self.pending_plan:
            stash["plan"] = (
                self.pending_plan.region,
                dict(self.pending_plan.button_regions),
            )
        if self.pending_question:
            stash["question"] = (
                self.pending_question.region,
                dict(self.pending_question.button_regions),
            )
        self._region_stash = stash or None

    def restore_stashed_regions(self) -> None:
        """Re-apply modal region tuples after an exact buffer restore."""
        stash = self._region_stash
        if not stash:
            return
        view = self.owner.view
        perm = self.pending_permission
        if perm and "permission" in stash:
            region, buttons = stash["permission"]
            perm.region = region
            perm.button_regions = dict(buttons or {})
            if view and region and sublime is not None:
                try:
                    self.owner.sheet.set_hidden_region(
                        keys.PERM_BLOCK, region[0], region[1])
                    for btn_type, (bs, be) in perm.button_regions.items():
                        view.add_regions(
                            "%s%s" % (keys.PERM_BTN_PREFIX, btn_type),
                            [sublime.Region(bs, be)], "", "",
                            getattr(sublime, "DRAW_NO_OUTLINE", 0),
                        )
                except Exception:
                    pass
        plan = self.pending_plan
        if plan and "plan" in stash:
            region, buttons = stash["plan"]
            plan.region = region
            plan.button_regions = dict(buttons or {})
            if view and region and sublime is not None:
                try:
                    self.owner.sheet.set_hidden_region(
                        keys.PLAN_BLOCK, region[0], region[1])
                except Exception:
                    pass
        q = self.pending_question
        if q and "question" in stash:
            region, buttons = stash["question"]
            q.region = region
            q.button_regions = dict(buttons or {})
            if view and region and sublime is not None:
                try:
                    self.owner.sheet.set_hidden_region(
                        keys.QUESTION_BLOCK, region[0], region[1])
                except Exception:
                    pass
        self._region_stash = None

    def drop_regions(self) -> None:
        """Invalidate stored region tuples. Live request objects are kept."""
        if self.pending_permission:
            self.pending_permission.region = None
            self.pending_permission.button_regions = {}
        for perm in self._permission_queue:
            perm.region = None
            perm.button_regions = {}
        if self.pending_plan:
            self.pending_plan.region = None
            self.pending_plan.button_regions = {}
        if self.pending_question:
            self.pending_question.region = None
            self.pending_question.button_regions = {}

    def rerender_pending(self) -> None:
        """Rebuild modal chrome from live pending objects. Viewless: no-op."""
        if not self._has_view():
            return
        if self.pending_permission:
            self._render_permission()
        if self.pending_plan:
            self._render_plan_approval()
        if self.pending_question:
            self.render_question()

    def clear_asking_state(self) -> None:
        """Drop leftover question / permission / plan UI.

        Resume after interrupt must not restore asking. Does not fire
        callbacks — the old turn's waiters are gone.
        """
        if (self.pending_question or self.pending_permission
                or self.pending_plan or self._permission_queue):
            self._bump_dirty()
        if self.pending_question:
            try:
                self.clear_question()
            except Exception:
                pass
            self.pending_question = None
        if self.pending_permission:
            try:
                self.remove_permission_block()
            except Exception:
                pass
            self.pending_permission = None
        self._permission_queue.clear()
        if self.pending_plan:
            try:
                self.clear_plan_approval()
            except Exception:
                pass
            self.pending_plan = None
        try:
            self.owner.composer._question_input_mode = False
        except Exception:
            pass
        view = getattr(self.owner, "view", None)
        if not view:
            try:
                surf = getattr(self.owner, "_surface", None)
                if isinstance(surf, dict):
                    surf["modals"] = []
            except Exception:
                pass
            return
        try:
            if hasattr(view, "is_valid") and not view.is_valid():
                return
        except Exception:
            pass
        try:
            keys.write_setting(view.settings(), keys.QUESTION_INPUT_MODE, False)
        except Exception:
            pass
        try:
            for key in (keys.QUESTION_BLOCK, keys.PERM_BLOCK, keys.PLAN_BLOCK):
                regs = view.get_regions(key) if hasattr(view, "get_regions") else []
                if regs:
                    clear_pending_block(
                        view,
                        block_region_key=key,
                        button_prefix="",
                        button_keys={},
                        extra_region_keys=(
                            keys.QUESTION_KEYS,
                            keys.QUESTION_INPUT_MARKER,
                        ),
                    )
            view.erase_regions(keys.QUESTION_KEYS)
            view.erase_regions(keys.QUESTION_INPUT_MARKER)
        except Exception:
            pass
        try:
            surf = getattr(self.owner, "_surface", None)
            if isinstance(surf, dict):
                surf["modals"] = []
        except Exception:
            pass

    def reset_all(self, keep_auto=False):
        self.pending_permission = None
        self._permission_queue.clear()
        self.pending_plan = None
        self.pending_question = None
        if not keep_auto:
            self.auto_allow_tools.clear()

    def load_persisted_auto_allow(self):
        try:
            folders = self.owner.window.folders() if self.owner.window else None
            project_dir = folders[0] if folders else None
            settings = load_project_settings(project_dir)
            self.auto_allow_tools = set(settings.get("autoAllowedMcpTools", []))
        except Exception:
            self.auto_allow_tools = set()

    def trailing_ui_start(self):
        view = self.owner.view
        if not self._has_view():
            return None
        starts = []
        for key in (
            keys.QUESTION_BLOCK, keys.PERM_BLOCK, keys.PLAN_BLOCK,
            keys.QUESTION_INPUT_MARKER,
        ):
            regs = view.get_regions(key)
            if regs and regs[0].size() > 0:
                starts.append(regs[0].begin())
        q = self.pending_question
        if q and getattr(q, "region", None):
            a, b = q.region
            if b > a:
                starts.append(a)
        c = self.owner.composer
        if c._question_input_mode:
            qs = c._question_input_start
            if qs is not None:
                starts.append(max(0, int(qs) - len("\n    ▸ ")))
        return min(starts) if starts else None

    # --- permission --------------------------------------------------------

    def permission_request(self, pid, tool, tool_input, callback):
        """Queue/show a permission modal. Viewless: records request, no chrome."""
        if self._has_view():
            self.owner.show(focus=False)
        for pattern in self.auto_allow_tools:
            if self._match_auto_allow_pattern(tool, tool_input, pattern):
                callback(PERM_ALLOW)
                return
        now = time.time()
        if self._last_allowed_tool == tool and (now - self._last_allowed_time) < SESSION_ALLOW_S:
            callback(PERM_ALLOW)
            return
        perm = PermissionRequest(
            id=pid, tool=tool, tool_input=tool_input, callback=callback,
            created_at=now,
        )
        if self.pending_permission and self.pending_permission.callback:
            self._permission_queue.append(perm)
            self._bump_dirty()
            self._notify_detached("permission")
            return
        self.pending_permission = perm
        self._bump_dirty()
        self.owner.composer.hide_composer_for_modal()
        self._render_permission()
        self.owner.composer.scroll_to_end()
        self._arm_perm_timeout(perm)
        self._notify_detached("permission")

    def _arm_perm_timeout(self, perm):
        self._perm_timeout_token += 1
        tok = self._perm_timeout_token
        if sublime is None:
            return

        def _expire(t=tok, p=perm):
            if self._perm_timeout_token != t:
                return
            if self.pending_permission is not p:
                return
            if not p.callback:
                return
            cb = p.callback
            p.callback = None
            self._respond_permission_with_callback(
                PERM_DENY, cb, p.tool, p.tool_input)

        sublime.set_timeout(_expire, PERM_TIMEOUT_S * 1000)

    def _render_permission(self):
        if not self.pending_permission or not self._has_view():
            return
        perm = self.pending_permission
        tool = perm.tool
        tool_input = perm.tool_input
        detail = ""
        display_tool = tool
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            if len(cmd) > 80:
                cmd = cmd[:80] + "..."
            detail = cmd
        elif tool in ("Read", "Edit", "Write"):
            detail = tool_input.get("file_path") or tool_input.get("description") or ""
        elif tool == "Glob" and "pattern" in tool_input:
            detail = tool_input["pattern"]
        elif tool == "Grep" and "pattern" in tool_input:
            detail = tool_input["pattern"]
        elif tool == "Skill" and "skill" in tool_input:
            display_tool = "Skill: %s" % tool_input["skill"]
            if tool_input.get("args"):
                detail = tool_input["args"]
        else:
            for k, v in list(tool_input.items())[:1]:
                detail = "%s: %s" % (k, str(v)[:60])
        lines = ["\n", "  ⚠ Allow %s" % display_tool]
        if detail:
            lines.append(": %s" % detail)
        lines.append("?\n")
        lines.append("    ")
        text_before_buttons = "".join(lines)
        hide_always = False
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            if any(p in cmd for p in _DANGEROUS_BASH):
                hide_always = True
        btn_y, btn_n, btn_s = "[Y] Allow", "[N] Deny", "[S] Allow 30s"
        always_hint = ""
        if tool == "Bash" and "command" in tool_input:
            pattern = self._make_auto_allow_pattern(tool, tool_input)
            if pattern != tool and "(" in pattern:
                always_hint = " `%s`" % pattern[pattern.index("(") + 1:-1]
        elif tool in ("Read", "Write", "Edit") and "file_path" in tool_input:
            dir_path = os.path.dirname(tool_input["file_path"])
            if dir_path:
                if len(dir_path) > 25:
                    dir_path = "..." + dir_path[-22:]
                always_hint = " in `%s/`" % dir_path
        btn_a = "[A] Always%s" % always_hint
        lines.append(btn_y)
        lines.append("  ")
        lines.append(btn_n)
        lines.append("  ")
        lines.append(btn_s)
        if not hide_always:
            lines.append("  ")
            lines.append(btn_a)
        else:
            lines.append("  (Always disabled for safety)")
        lines.append("\n")
        text = "".join(lines)
        start = self.owner.view.size()
        end = self.owner._write(text)
        perm.region = (start, end)
        self.owner.sheet.set_hidden_region(keys.PERM_BLOCK, start, end)
        btn_start = start + len(text_before_buttons)
        perm.button_regions[PERM_ALLOW] = (btn_start, btn_start + len(btn_y))
        btn_start += len(btn_y) + 2
        perm.button_regions[PERM_DENY] = (btn_start, btn_start + len(btn_n))
        btn_start += len(btn_n) + 2
        perm.button_regions[PERM_ALLOW_SESSION] = (btn_start, btn_start + len(btn_s))
        btn_start += len(btn_s) + 2
        if not hide_always:
            perm.button_regions[PERM_ALLOW_ALL] = (btn_start, btn_start + len(btn_a))
        self._add_button_regions()

    def _add_button_regions(self):
        if not self.pending_permission or not self._has_view() or sublime is None:
            return
        perm = self.pending_permission
        for btn_type, (start, end) in perm.button_regions.items():
            self.owner.view.add_regions(
                "%s%s" % (keys.PERM_BTN_PREFIX, btn_type),
                [sublime.Region(start, end)],
                "submarine.permission.button.%s" % btn_type,
                "",
                sublime.DRAW_NO_OUTLINE,
            )

    def remove_permission_block(self):
        if not self.pending_permission:
            return
        self._bump_dirty()
        if not self._has_view():
            self.pending_permission.region = None
            self.pending_permission.button_regions = {}
            return
        perm = self.pending_permission
        for btn_type in perm.button_regions:
            self.owner.view.erase_regions("%s%s" % (keys.PERM_BTN_PREFIX, btn_type))
        regions = self.owner.view.get_regions(keys.PERM_BLOCK)
        if regions and regions[0].size() > 0:
            r = regions[0]
            self.owner._replace(r.begin(), r.end(), "")
        else:
            conv_end = _conv_end(self.owner)
            if conv_end is not None and self.owner.view.size() > conv_end:
                self.owner._replace(conv_end, self.owner.view.size(), "")
        self.owner.view.erase_regions(keys.PERM_BLOCK)
        perm.region = None
        perm.button_regions = {}

    def _clear_permission(self):
        if not self.pending_permission:
            return
        if self._has_view():
            clear_pending_block(
                self.owner.view,
                block_region_key=keys.PERM_BLOCK,
                button_prefix=keys.PERM_BTN_PREFIX,
                button_keys=self.pending_permission.button_regions,
                fallback_region_end=_conv_end(self.owner),
            )
            cur = self.owner.current
            if cur and cur.region:
                cur.region = (cur.region[0], self.owner.view.size())
        self.pending_permission.region = None
        self.pending_permission.button_regions = {}

    def clear_stale_permission(self, current_pid):
        if not self.pending_permission:
            return
        if self.pending_permission.id < current_pid:
            self._clear_permission()
            self.pending_permission = None
            self._permission_queue.clear()

    def clear_all_permissions(self):
        if self.pending_permission:
            self._clear_permission()
            self.pending_permission = None
        self._permission_queue.clear()

    @staticmethod
    def _extract_bash_subcommands(command):
        import re
        parts = re.split(r'\s*(?:&&|\|\||\|&|[;&|\n])\s*', command)
        wrappers = {"timeout", "time", "nice", "nohup", "stdbuf"}
        result = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            words = part.split()
            idx = 0
            while idx < len(words) and '=' in words[idx] and not words[idx].startswith('-'):
                idx += 1
            if idx >= len(words):
                continue
            while idx < len(words) and words[idx] in wrappers:
                idx += 1
                while idx < len(words) and (
                        words[idx].startswith('-') or words[idx].replace('.', '').isdigit()):
                    idx += 1
            if idx >= len(words):
                continue
            if words[idx] == "xargs" and (idx + 1 >= len(words) or not words[idx + 1].startswith('-')):
                idx += 1
            if idx >= len(words):
                continue
            word = words[idx]
            if '/' in word:
                word = word.split('/')[-1]
            result.append((word, part))
        return result

    def _make_auto_allow_pattern(self, tool, tool_input):
        if not tool_input:
            return tool
        if tool == "Bash":
            command = tool_input.get("command", "")
            if command:
                trivial = {
                    "cd", "pushd", "popd", "export", "set", "unset",
                    "source", ".", "true", "false",
                }
                subcmds = self._extract_bash_subcommands(command)
                best = None
                for word, _ in subcmds:
                    if word not in trivial:
                        best = word
                        break
                if not best and subcmds:
                    best = subcmds[0][0]
                if best:
                    return "Bash(%s:*)" % best
        elif tool in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path", "")
            if file_path:
                dir_path = os.path.dirname(file_path)
                if dir_path:
                    return "%s(%s/)" % (tool, dir_path)
        elif tool == "Skill":
            skill_name = tool_input.get("skill", "")
            if skill_name:
                return "Skill(%s)" % skill_name
        return tool

    def _match_auto_allow_pattern(self, tool, tool_input, pattern):
        import fnmatch
        if '(' in pattern and pattern.endswith(')'):
            paren_idx = pattern.index('(')
            parsed_tool = pattern[:paren_idx]
            specifier = pattern[paren_idx + 1:-1]
        else:
            parsed_tool = pattern
            specifier = None
        if not fnmatch.fnmatch(tool, parsed_tool):
            return False
        if specifier is None:
            return True
        if tool == "Bash":
            command = tool_input.get("command", "")
            if not command:
                return False
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                subcmds = self._extract_bash_subcommands(command)
                return any(word.startswith(prefix) for word, _ in subcmds)
            return command == specifier
        if tool in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path", "")
            if not file_path:
                return False
            if specifier.endswith('/'):
                return (file_path.startswith(specifier)
                        or os.path.dirname(file_path) + '/' == specifier)
            if any(c in specifier for c in ['*', '?', '[']):
                return fnmatch.fnmatch(file_path, specifier)
            return file_path == specifier
        if tool == "Skill":
            return tool_input.get("skill", "") == specifier
        return False

    def _respond_permission_with_callback(self, response, callback, tool, tool_input=None):
        if response == PERM_ALLOW_ALL:
            pattern = self._make_auto_allow_pattern(tool, tool_input)
            self.auto_allow_tools.add(pattern)
            self._save_auto_allowed_tool(pattern)
        if response == PERM_ALLOW_SESSION:
            self._last_allowed_tool = tool
            self._last_allowed_time = time.time()
            response = PERM_ALLOW
        self._clear_permission()
        self.pending_permission = None
        callback(response)
        self._process_permission_queue()
        self._restore_composer_after_modal()

    def _save_auto_allowed_tool(self, tool):
        import json
        folders = self.owner.window.folders() if self.owner.window else None
        if not folders:
            print("[Submarine] Cannot save auto-allowed tool: no project folder")
            return
        project_dir = folders[0]
        settings_dir = os.path.join(project_dir, ".claude")
        settings_path = os.path.join(settings_dir, "settings.json")
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except Exception as e:
                print("[Submarine] Error loading settings: %s" % e)
                return
        auto_allowed = settings.get("autoAllowedMcpTools", [])
        if tool not in auto_allowed:
            auto_allowed.append(tool)
            settings["autoAllowedMcpTools"] = auto_allowed
            os.makedirs(settings_dir, exist_ok=True)
            try:
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=2)
                if sublime is not None:
                    sublime.status_message("Auto-allowed: %s" % tool)
            except Exception as e:
                print("[Submarine] Failed to save settings: %s" % e)

    def _process_permission_queue(self):
        while self._permission_queue:
            perm = self._permission_queue.pop(0)
            auto_allowed = False
            for pattern in self.auto_allow_tools:
                if self._match_auto_allow_pattern(perm.tool, perm.tool_input, pattern):
                    perm.callback(PERM_ALLOW)
                    auto_allowed = True
                    break
            if auto_allowed:
                continue
            now = time.time()
            if self._last_allowed_tool == perm.tool and (now - self._last_allowed_time) < SESSION_ALLOW_S:
                perm.callback(PERM_ALLOW)
                continue
            self.pending_permission = perm
            self._render_permission()
            self.owner.composer.scroll_to_end()
            self._arm_perm_timeout(perm)
            break

    def handle_permission_key(self, key):
        """Answer the visible permission. Viewless: still resolves the callback."""
        if not self.pending_permission:
            return False
        if self.pending_permission.callback is None:
            return False
        if self.owner.composer.is_input_mode() and not self.owner.composer._question_input_mode:
            return False
        perm = self.pending_permission
        key = key.lower()
        response = {
            "y": PERM_ALLOW, "n": PERM_DENY,
            "s": PERM_ALLOW_SESSION, "a": PERM_ALLOW_ALL,
        }.get(key)
        if response:
            callback = perm.callback
            perm.callback = None
            self._respond_permission_with_callback(
                response, callback, perm.tool, perm.tool_input)
            return True
        return False

    # --- plan --------------------------------------------------------------

    def plan_approval_request(self, plan_id, plan_file, allowed_prompts, callback):
        """Show a plan-approval modal. Viewless: records request, no chrome."""
        if self._has_view():
            self.owner.show(focus=False)
        self.pending_plan = PlanApproval(
            id=plan_id, plan_file=plan_file,
            allowed_prompts=allowed_prompts, callback=callback,
        )
        self._bump_dirty()
        self.owner.composer.hide_composer_for_modal()
        self._render_plan_approval()
        self.owner.composer.scroll_to_end()
        self._notify_detached("plan")

    def _render_plan_approval(self):
        if not self.pending_plan or not self._has_view():
            return
        plan = self.pending_plan
        lines = ["\n", "  ⚙ Plan complete — approve to start implementation\n"]
        if plan.plan_file:
            lines.append("    plan: %s\n" % os.path.basename(plan.plan_file))
        if plan.allowed_prompts:
            lines.append("    permissions: %d\n" % len(plan.allowed_prompts))
            for p in plan.allowed_prompts[:3]:
                lines.append("      • %s: %s\n" % (p.get("tool", "?"), p.get("prompt", "")))
            if len(plan.allowed_prompts) > 3:
                lines.append("      ... and %d more\n" % (len(plan.allowed_prompts) - 3))
        lines.append("    ")
        text_before_buttons = "".join(lines)
        btn_y, btn_n, btn_v = "[Y] Approve", "[N] Reject", "[V] View Plan"
        lines.extend([btn_y, "  ", btn_n, "  ", btn_v, "\n"])
        text = "".join(lines)
        start = self.owner.view.size()
        end = self.owner._write(text)
        plan.region = (start, end)
        self.owner.sheet.set_hidden_region(keys.PLAN_BLOCK, start, end)
        btn_start = start + len(text_before_buttons)
        plan.button_regions[PLAN_APPROVE] = (btn_start, btn_start + len(btn_y))
        btn_start += len(btn_y) + 2
        plan.button_regions[PLAN_REJECT] = (btn_start, btn_start + len(btn_n))
        btn_start += len(btn_n) + 2
        plan.button_regions[PLAN_VIEW] = (btn_start, btn_start + len(btn_v))
        if sublime is None:
            return
        scope_map = {
            PLAN_APPROVE: "submarine.permission.button.allow",
            PLAN_REJECT: "submarine.permission.button.deny",
            PLAN_VIEW: "submarine.permission.button.allow_session",
        }
        for btn_type, (bs, be) in plan.button_regions.items():
            self.owner.view.add_regions(
                "%s%s" % (keys.PLAN_BTN_PREFIX, btn_type),
                [sublime.Region(bs, be)],
                scope_map.get(btn_type, ""),
                "", sublime.DRAW_NO_OUTLINE,
            )

    def clear_plan_approval(self):
        if not self.pending_plan:
            return
        self._bump_dirty()
        if self._has_view():
            clear_pending_block(
                self.owner.view,
                block_region_key=keys.PLAN_BLOCK,
                button_prefix=keys.PLAN_BTN_PREFIX,
                button_keys=self.pending_plan.button_regions,
                fallback_region_end=_conv_end(self.owner),
            )
        self.pending_plan.region = None
        self.pending_plan.button_regions = {}

    def handle_plan_key(self, key):
        """Answer the visible plan. Viewless: still resolves the callback."""
        if not self.pending_plan or self.pending_plan.callback is None:
            return False
        if self.owner.composer.is_input_mode() and not self.owner.composer._question_input_mode:
            return False
        plan = self.pending_plan
        key = key.lower()
        if key == "v":
            self._open_plan_file(plan.plan_file)
            return True
        if key == "y":
            response = PLAN_APPROVE
        elif key == "n":
            response = PLAN_REJECT
        else:
            return False
        callback = plan.callback
        plan.callback = None
        self.clear_plan_approval()
        self.pending_plan = None  # descriptor clears with the live object
        if callback:
            callback(response)
        self._restore_composer_after_modal()
        return True

    def _open_plan_file(self, path):
        """Read the plan from the SAVED file and open it in its remembered slot."""
        if not path:
            return
        # Prefer the on-disk saved file (not any unsaved buffer).
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.read(1)
            except Exception:
                pass
        win = None
        if sublime is not None:
            try:
                win = sublime.active_window()
            except Exception:
                win = None
        if not win:
            win = getattr(self.owner, "window", None)
        if not win:
            return
        from core.placement import open_plan_file
        open_plan_file(win, path)

    # --- question ----------------------------------------------------------

    def question_request(self, qid, questions, callback):
        """Show a question modal. Viewless: records request, no chrome."""
        if self._has_view():
            self.owner.show(focus=False)
        self.pending_question = QuestionRequest(
            qid=qid, questions=questions, callback=callback)
        self._bump_dirty()
        self.owner.composer.hide_composer_for_modal()
        self.render_question()
        self.owner.composer.scroll_to_end(force=True)
        self._notify_detached("question")

    def _question_block_text(self):
        q_req = self.pending_question
        if not q_req or q_req.current_idx >= len(q_req.questions):
            return ""
        q = q_req.questions[q_req.current_idx]
        question_text = (q.get("question") or "").replace("\n", " ").strip()
        options = q.get("options") or []
        multi = q.get("multiSelect", False)
        lines = ["\n"]
        if multi:
            lines.append("  ❓ %s (Enter to confirm)\n" % question_text)
        else:
            lines.append("  ❓ %s\n" % question_text)
        for i, opt in enumerate(options):
            label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            label = str(label).replace("\n", " ").strip()
            desc = str(desc).replace("\n", " ").strip()
            num = i + 1
            if multi:
                check = "✓" if i in q_req.selected else " "
                line = "    [%d] %s %s" % (num, check, label)
            else:
                line = "    [%d] %s" % (num, label)
            if desc:
                line += " — %s" % desc
            lines.append(line + "\n")
        if multi:
            lines.append("    [O] Other...  [⏎] Confirm\n")
        else:
            lines.append("    [O] Other...\n")
        return "".join(lines)

    def render_question(self):
        if not self.pending_question or not self._has_view():
            return
        q_req = self.pending_question
        if q_req.current_idx >= len(q_req.questions):
            return
        text = self._question_block_text()
        if not text:
            return
        view = self.owner.view
        c = self.owner.composer
        old = view.get_regions(keys.QUESTION_BLOCK)
        if old and old[0].size() > 0:
            write_at = old[0].begin()
            self.owner._replace(old[0].begin(), old[0].end(), "")
        elif q_req.region and q_req.region[1] > q_req.region[0]:
            a, b = q_req.region
            a = max(0, min(a, view.size()))
            b = max(a, min(b, view.size()))
            write_at = a
            if b > a:
                self.owner._replace(a, b, "")
        else:
            conv = view.get_regions(keys.CONV_REGION)
            if conv and conv[0].size() > 0:
                write_at = conv[0].end()
            elif self.owner.current and self.owner.current.region:
                write_at = min(self.owner.current.region[1], view.size())
            else:
                write_at = view.size()
        free_text = None
        free_marker = None
        if c._question_input_mode:
            regs = view.get_regions(keys.QUESTION_INPUT_MARKER)
            if regs:
                free_marker = "\n    ▸ "
                free_text = view.substr(_R(regs[0].end(), view.size()))
                self.owner._replace(regs[0].begin(), view.size(), "")
            else:
                q_start = c._question_input_start
                if q_start is not None and q_start <= view.size():
                    free_marker = "\n    ▸ "
                    free_text = view.substr(_R(q_start, view.size()))
                    erase_from = max(0, q_start - len(free_marker))
                    self.owner._replace(erase_from, view.size(), "")
        if free_text is None:
            tail = view.substr(_R(write_at, view.size()))
            if "❓" in tail:
                orphan = tail.find("\n  ❓ ")
                if orphan < 0:
                    orphan = tail.find("  ❓ ")
                if orphan >= 0:
                    self.owner._replace(write_at + orphan, view.size(), "")
        end = self.owner._write(text, pos=write_at)
        q_req.region = (write_at, end)
        self.owner.sheet.set_hidden_region(keys.QUESTION_BLOCK, write_at, end)
        if free_marker is not None:
            view.set_read_only(False)
            marker_start = view.size()
            view.run_command("append", {
                "characters": free_marker + (free_text or ""),
            })
            marker_end = marker_start + len(free_marker)
            c._question_input_start = marker_end
            c._input_start = marker_end
            self.owner.sheet.set_hidden_region(
                keys.QUESTION_INPUT_MARKER, marker_start, marker_end)
            if sublime is not None:
                caret = view.size()
                view.sel().clear()
                view.sel().add(sublime.Region(caret, caret))
            view.set_read_only(False)
        import re
        key_regions = []
        for m in re.finditer(r'\[\d+\]|\[O\]|\[⏎\]', text):
            if sublime is not None:
                key_regions.append(sublime.Region(write_at + m.start(), write_at + m.end()))
        if key_regions and sublime is not None:
            view.add_regions(
                keys.QUESTION_KEYS, key_regions,
                "submarine.permission.button.allow",
                "", sublime.DRAW_NO_OUTLINE,
            )
        else:
            view.erase_regions(keys.QUESTION_KEYS)

    def clear_question(self, summary=""):
        if not self.pending_question:
            return
        self._bump_dirty()
        if not self._has_view():
            self.pending_question.region = None
            self.pending_question.button_regions = {}
            c = self.owner.composer
            c._question_input_mode = False
            if summary and self.owner.current is not None:
                self.owner.current.events.append("  ☑ %s\n" % summary)
            return
        view = self.owner.view
        c = self.owner.composer
        if summary and self.owner.current is not None:
            self.owner.current.events.append("  ☑ %s\n" % summary)
        try:
            regs = view.get_regions(keys.QUESTION_INPUT_MARKER)
            if regs:
                view.set_read_only(False)
                self.owner._replace(regs[0].begin(), view.size(), "")
            elif c._question_input_mode:
                q_start = c._question_input_start
                if q_start is not None and q_start <= view.size():
                    erase_from = max(0, q_start - len("\n    ▸ "))
                    view.set_read_only(False)
                    self.owner._replace(erase_from, view.size(), "")
        except Exception:
            pass
        was_free = bool(c._question_input_mode)
        c._question_input_mode = False
        try:
            keys.write_setting(view.settings(), keys.QUESTION_INPUT_MODE, False)
            view.erase_regions(keys.QUESTION_INPUT_MARKER)
        except Exception:
            pass
        if was_free or c.is_input_mode():
            try:
                tail = view.substr(_R(max(0, view.size() - 120), view.size()))
                if "◎ " not in tail:
                    c._input_mode = False
                    keys.write_setting(view.settings(), keys.INPUT_MODE, False)
            except Exception:
                if was_free:
                    c._input_mode = False
        clear_pending_block(
            view,
            block_region_key=keys.QUESTION_BLOCK,
            button_prefix="submarine_question_btn_",
            button_keys={},
            fallback_region_end=_conv_end(self.owner),
            extra_region_keys=(keys.QUESTION_KEYS, keys.QUESTION_INPUT_MARKER),
        )
        self.pending_question.region = None
        self.pending_question.button_regions = {}
        if summary:
            self.owner.renderer._struct_dirty = True
            self.owner.renderer._render_current()

    def _advance_question(self):
        q_req = self.pending_question
        if not q_req:
            return
        q_req.current_idx += 1
        q_req.selected = set()
        if q_req.current_idx >= len(q_req.questions):
            callback = q_req.callback
            answers = q_req.answers
            self.pending_question = None
            if callback:
                callback(answers)
            self._restore_composer_after_modal()
        else:
            self.render_question()
            self.owner.composer.scroll_to_end()

    def _restore_composer_after_modal(self):
        """After plan / question / permission, put the caret back in ◎ — not EOF."""
        def _go():
            try:
                if self.owner.has_turn_modal_ui():
                    return
            except Exception:
                if (
                    (self.pending_question and self.pending_question.callback)
                    or (self.pending_permission and self.pending_permission.callback)
                    or (self.pending_plan and self.pending_plan.callback)
                ):
                    return
            view = getattr(self.owner, "view", None)
            session = None
            try:
                from .session_api import get_session_for_view
                session = get_session_for_view(view)
            except Exception:
                session = None
            entered = False
            if session is not None:
                enter = getattr(session, "_enter_input_with_draft", None)
                if callable(enter):
                    try:
                        enter()
                        entered = True
                    except Exception:
                        entered = False
            if not entered:
                try:
                    self.owner.composer.enter_input_mode()
                except Exception:
                    pass
            try:
                self.owner.composer.focus(
                    force_show=True, steal_focus=True, park_at_end=True)
            except Exception:
                pass
            try:
                self.owner._move_cursor_to_end()
            except Exception:
                pass

        if sublime is not None:
            sublime.set_timeout(_go, 30)
        else:
            _go()

    def handle_question_key(self, key):
        """Answer the visible question. Viewless: updates answers, no chrome."""
        if not self.pending_question or self.pending_question.callback is None:
            return False
        if (self.owner.composer.is_input_mode()
                and not self.owner.composer._question_input_mode):
            return False
        q_req = self.pending_question
        q = q_req.questions[q_req.current_idx]
        options = q.get("options", [])
        multi = q.get("multiSelect", False)
        header = q.get("header", "Q%d" % (q_req.current_idx + 1))
        question_text = q.get("question", str(q_req.current_idx))
        key = key.lower()
        if key in ("1", "2", "3", "4"):
            idx = int(key) - 1
            if idx >= len(options):
                return True
            opt = options[idx]
            label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            if multi:
                if idx in q_req.selected:
                    q_req.selected.discard(idx)
                else:
                    q_req.selected.add(idx)
                self.clear_question()
                self.render_question()
                self.owner.composer.scroll_to_end()
            else:
                q_req.answers[question_text] = label
                self.clear_question("%s → %s" % (header, label))
                self._advance_question()
            return True
        if key == "o":
            self._question_enter_input_mode()
            return True
        if key == "enter":
            if multi:
                selected_labels = []
                for idx in sorted(q_req.selected):
                    opt = options[idx]
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    selected_labels.append(label)
                q_req.answers[question_text] = selected_labels
                summary = ", ".join(selected_labels) if selected_labels else "(none)"
                self.clear_question("%s → %s" % (header, summary))
                self._advance_question()
                return True
            return False
        return False

    def _question_enter_input_mode(self):
        view = self.owner.view
        c = self.owner.composer
        if not self._has_view() or not self.pending_question:
            return
        if c._question_input_mode:
            view.set_read_only(False)
            end = view.size()
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(end, end))
            view.show(end)
            return
        view.set_read_only(False)
        try:
            if c._pad_phantom_set is not None:
                c._pad_phantom_set.update([])
        except Exception:
            pass
        marker_text = "\n    ▸ "
        marker_start = view.size()
        view.run_command("append", {"characters": marker_text})
        marker_end = view.size()
        c._question_input_start = marker_end
        c._question_input_mode = True
        self.owner.sheet.set_hidden_region(
            keys.QUESTION_INPUT_MARKER, marker_start, marker_end)
        c._input_start = c._question_input_start
        c._input_mode = True
        keys.write_setting(view.settings(), keys.INPUT_MODE, True)
        keys.write_setting(view.settings(), keys.QUESTION_INPUT_MODE, True)
        if sublime is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(c._question_input_start, c._question_input_start))
        view.set_read_only(False)
        try:
            view.show(c._question_input_start)
        except Exception:
            pass

    def submit_question_input(self):
        c = self.owner.composer
        view = self.owner.view
        if not c._question_input_mode:
            return False
        if not self.pending_question or not self._has_view():
            c._question_input_mode = False
            try:
                keys.write_setting(view.settings(), keys.QUESTION_INPUT_MODE, False)
            except Exception:
                pass
            return False
        regions = view.get_regions(keys.QUESTION_INPUT_MARKER)
        if regions:
            text_start = regions[0].end()
            erase_start = regions[0].begin()
        else:
            text_start = c._question_input_start
            erase_start = max(0, c._question_input_start - len("\n    ▸ "))
        text = view.substr(_R(text_start, view.size())).strip()
        if not text:
            view.set_read_only(False)
            end = view.size()
            if sublime is not None:
                view.sel().clear()
                view.sel().add(sublime.Region(end, end))
            return True
        c._question_input_mode = False
        c._input_mode = False
        keys.write_setting(view.settings(), keys.INPUT_MODE, False)
        keys.write_setting(view.settings(), keys.QUESTION_INPUT_MODE, False)
        view.set_read_only(False)
        view.run_command(keys.CMD_REPLACE, {
            "start": erase_start, "end": view.size(), "text": "",
        })
        view.set_read_only(True)
        view.erase_regions(keys.QUESTION_INPUT_MARKER)
        q_req = self.pending_question
        q = q_req.questions[q_req.current_idx]
        header = q.get("header", "Q%d" % (q_req.current_idx + 1))
        q_req.answers[q.get("question", str(q_req.current_idx))] = text
        self.clear_question("%s → %s" % (header, text))
        self._advance_question()
        return True


def _conv_end(owner):
    cur = getattr(owner, "current", None)
    if not cur or not cur.region:
        return None
    try:
        return cur.region[1]
    except Exception:
        return None


def _R(a, b):
    if sublime is not None:
        return sublime.Region(a, b)
    return type("R", (), {"a": a, "b": b})()
