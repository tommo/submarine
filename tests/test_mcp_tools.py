"""MCP catalog + router — plain pytest, no sublime."""
from __future__ import annotations

import ast

from mcp.tools import (
    CALLER_INJECT_TOOLS,
    DEBUG_OPS,
    TOOL_CODEGEN,
    TOOL_SCHEMAS,
    TOOL_TABLE,
    catalog_names,
    create_router,
    exec_global_names,
    has_tool,
    is_gated_tool,
    is_local_tool,
    list_tool_descriptors,
    normalize_mcp_tool_name,
    parse_tool_call,
    route,
)


KEEP = (
    "get_window_summary",
    "find_file",
    "get_symbols",
    "goto_symbol",
    "read_view",
    "read_image",
    "list_backends",
    "spawn_session",
    "send_to_session",
    "list_sessions",
    "read_session_output",
    "read_session_edits",
    "lsp",
    "sublime_eval",
    "sublime_tool",
    "list_tools",
    "quick_done",
    "update_goal",
    "goal_verdict",
    "session_info",
    "signal_complete",
    "wait_for_subsession",
    "list_profiles",
    "list_profile_docs",
    "read_profile_doc",
    "set_timer",
    "cancel_timer",
    "write_artifact",
    "edit_artifact",
    "read_artifact",
    "list_artifacts",
)

DROP = (
    "terminal_list",
    "terminal_run",
    "terminal_read",
    "terminal_close",
    "terminal_send",
    "list_personas",
    "order",
    "subscribe",
    "discover_services",
    "list_notifications",
    "unregister_notification",
    "register_notification",
    "chatroom",
    "garage_search",
)


# ─── Normalization ────────────────────────────────────────────────────────────

def test_normalize_prefix_table():
    cases = (
        ("goal_verdict", "goal_verdict"),
        ("mcp__submarine__goal_verdict", "goal_verdict"),
        ("submarine__goal_verdict", "goal_verdict"),
        ("mcp__sublime__goal_verdict", "goal_verdict"),
        ("sublime__goal_verdict", "goal_verdict"),
        ("mcp__submarine__update_goal", "update_goal"),
        ("submarine__spawn_session", "spawn_session"),
        ("mcp__sublime__wait_for_subsession", "wait_for_subsession"),
        ("sublime__signal_complete", "signal_complete"),
        ("", ""),
        ("  update_goal  ", "update_goal"),
    )
    for raw, expected in cases:
        assert normalize_mcp_tool_name(raw) == expected, raw


def test_parse_tool_call_normalizes_and_reads_arguments():
    name, args = parse_tool_call("tools/call", {
        "name": "mcp__submarine__update_goal",
        "arguments": {"message": "hi", "completed": True},
    })
    assert name == "update_goal"
    assert args["message"] == "hi"
    assert args["completed"] is True


def test_parse_tool_call_accepts_toolName_and_input():
    name, args = parse_tool_call("tools/call", {
        "toolName": "sublime__find_file",
        "input": {"query": "foo"},
    })
    assert name == "find_file"
    assert args == {"query": "foo"}


# ─── Catalog / exec_globals parity ────────────────────────────────────────────

def test_schema_and_codegen_tables_cover_the_same_names():
    assert set(TOOL_SCHEMAS) == set(TOOL_CODEGEN)
    assert set(TOOL_TABLE) == set(TOOL_SCHEMAS)
    assert set(TOOL_TABLE) == set(catalog_names())


def test_exec_globals_match_catalog_minus_local():
    local = {n for n, spec in TOOL_TABLE.items() if spec.get("local")}
    assert local == {"read_image"}
    assert exec_global_names() == set(TOOL_TABLE) - local
    assert exec_global_names() <= set(TOOL_SCHEMAS)
    assert exec_global_names() <= set(TOOL_CODEGEN)


def test_keep_list_is_exactly_the_catalog():
    assert set(TOOL_TABLE) == set(KEEP)
    for name in KEEP:
        assert name in TOOL_SCHEMAS
        assert name in TOOL_CODEGEN
        assert has_tool(name)
        assert has_tool("mcp__submarine__%s" % name)


def test_read_image_is_gated_and_local():
    assert is_local_tool("read_image")
    assert is_gated_tool("mcp__sublime__read_image")
    advertised = {t["name"] for t in list_tool_descriptors(enable_read_image=False)}
    assert "read_image" not in advertised
    advertised_on = {t["name"] for t in list_tool_descriptors(enable_read_image=True)}
    assert "read_image" in advertised_on
    assert advertised_on == set(KEEP)
    assert advertised == set(KEEP) - {"read_image"}


def test_debug_ops_are_not_in_the_catalog():
    for op in DEBUG_OPS:
        assert op not in TOOL_TABLE
        assert "debug_%s" % op not in TOOL_TABLE
    for desc in list_tool_descriptors(enable_read_image=True):
        assert not desc["name"].startswith("debug_")


def test_send_and_read_schemas_drop_view_id():
    for name in ("send_to_session", "read_session_output"):
        props = TOOL_SCHEMAS[name]["properties"]
        assert "view_id" not in props
        assert "_caller_view_id" not in props
        assert "fork_from_view_id" not in props


def test_spawn_schema_has_no_persona_or_checkpoint():
    props = TOOL_SCHEMAS["spawn_session"]["properties"]
    assert "persona_id" not in props
    assert "checkpoint" not in props
    assert set(props) == {
        "prompt", "name", "profile", "backend", "model",
        "fork_current", "fork_from_agent_id",
        "wait_for_completion",
    }


def test_caller_inject_set():
    assert CALLER_INJECT_TOOLS == frozenset((
        "spawn_session", "send_to_session", "signal_complete",
        "write_artifact", "edit_artifact", "read_artifact", "list_artifacts",
    ))


# ─── Removed-tool guard ───────────────────────────────────────────────────────

def test_dropped_tools_are_absent_from_catalog():
    for name in DROP:
        assert name not in TOOL_TABLE, name
        assert name not in TOOL_SCHEMAS, name
        assert name not in TOOL_CODEGEN, name
        assert name not in catalog_names()
        assert name not in exec_global_names()
        assert not has_tool(name)
        advertised = {t["name"] for t in list_tool_descriptors(True)}
        assert name not in advertised


# ─── Router codegen smoke ─────────────────────────────────────────────────────

def _assert_call(src: str, func: str) -> ast.Call:
    tree = ast.parse(src)
    assert len(tree.body) == 1
    stmt = tree.body[0]
    assert isinstance(stmt, ast.Return)
    call = stmt.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == func
    return call


def _kw(call: ast.Call) -> dict:
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}


def test_router_codegen_wait_for_subsession():
    src = route("mcp__submarine__wait_for_subsession", {
        "agent_id": "abc123",
        "wake_prompt": "Architect done — review.",
    })
    call = _assert_call(src, "wait_for_subsession")
    kw = _kw(call)
    assert kw["agent_id"] == "abc123"
    assert kw["wake_prompt"] == "Architect done — review."
    assert kw["subsession_id"] is None


def test_router_codegen_signal_complete():
    src = route("sublime__signal_complete", {
        "session_id": 42,
        "result_summary": "Task done. Files: a.py",
    })
    call = _assert_call(src, "signal_complete")
    kw = _kw(call)
    assert kw["session_id"] == 42
    assert kw["result_summary"] == "Task done. Files: a.py"


def test_router_codegen_update_goal():
    src = route("update_goal", {
        "message": "slice landed",
        "completed": True,
        "blocked_reason": "",
    })
    call = _assert_call(src, "update_goal")
    kw = _kw(call)
    assert kw["message"] == "slice landed"
    assert kw["completed"] is True
    assert kw["blocked_reason"] == ""


def test_router_codegen_spawn_session_no_removed_args():
    src = route("spawn_session", {
        "prompt": "explore",
        "name": "explorer",
        "backend": "grok",
        "fork_from_agent_id": "aa11",
        "wait_for_completion": True,
        "_caller_agent_id": "agent-abc",
    })
    call = _assert_call(src, "spawn_session")
    kw = _kw(call)
    assert kw["prompt"] == "explore"
    assert kw["name"] == "explorer"
    assert kw["backend"] == "grok"
    assert kw["fork_from_agent_id"] == "aa11"
    assert kw["wait_for_completion"] is True
    assert kw["_caller_agent_id"] == "agent-abc"
    assert "persona_id" not in kw
    assert "checkpoint" not in kw
    assert "fork_from_view_id" not in kw
    assert "_caller_view_id" not in kw


def test_router_codegen_set_and_cancel_timer():
    src = route("set_timer", {"seconds": 300, "wake_prompt": "⏰ up"})
    kw = _kw(_assert_call(src, "set_timer"))
    assert kw["seconds"] == 300
    assert kw["wake_prompt"] == "⏰ up"
    src = route("cancel_timer", {"timer_id": "tmr-abc"})
    kw = _kw(_assert_call(src, "cancel_timer"))
    assert kw["timer_id"] == "tmr-abc"
    src = route("cancel_timer", {})
    assert src == "return cancel_timer()"


def test_router_codegen_write_artifact_injects_caller():
    src = route("write_artifact", {
        "name": "auth-analysis",
        "content": "# hi",
        "summary": "auth flow",
        "_caller_agent_id": "agent-abc",
        "auto_open": False,
    })
    call = _assert_call(src, "write_artifact")
    kw = _kw(call)
    assert kw["name"] == "auth-analysis"
    assert kw["content"] == "# hi"
    assert kw["summary"] == "auth flow"
    assert kw["_caller_agent_id"] == "agent-abc"
    assert kw["auto_open"] is False
    assert "view_id" not in kw
    assert "_caller_view_id" not in kw


def test_router_codegen_edit_and_read_artifact():
    src = route("edit_artifact", {
        "path": "/tmp/a.md",
        "op": "replace_text",
        "old": "a",
        "new": "b",
        "note": "fix typo",
        "_caller_agent_id": "agent-z",
    })
    kw = _kw(_assert_call(src, "edit_artifact"))
    assert kw["path"] == "/tmp/a.md"
    assert kw["op"] == "replace_text"
    assert kw["old"] == "a"
    assert kw["new"] == "b"
    assert kw["note"] == "fix typo"
    assert kw["_caller_agent_id"] == "agent-z"
    src = route("read_artifact", {"path": "/tmp/a.md", "offset": 10, "limit": 5})
    kw = _kw(_assert_call(src, "read_artifact"))
    assert kw["path"] == "/tmp/a.md"
    assert kw["offset"] == 10
    assert kw["limit"] == 5
    src = route("list_artifacts", {"scope": "all", "_caller_agent_id": "agent-z"})
    kw = _kw(_assert_call(src, "list_artifacts"))
    assert kw["scope"] == "all"
    assert kw["_caller_agent_id"] == "agent-z"


def test_artifact_tool_docs_steer_away_from_transcript_dump():
    write_desc = TOOL_TABLE["write_artifact"]["description"].lower()
    assert "is the output" in write_desc
    assert "do not also print" in write_desc
    assert "native" in write_desc and "edit" in write_desc
    read_desc = TOOL_TABLE["read_artifact"]["description"].lower()
    assert "read_session_output" in read_desc
    assert "journal_tail" in read_desc
    assert "external" in read_desc


def test_router_unknown_tool_raises():
    try:
        route("list_personas", {})
    except ValueError as e:
        assert "Unknown tool" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_sublime_eval_and_tool_pass_through():
    assert route("sublime_eval", {"code": "return 1 + 1"}) == "return 1 + 1"
    assert route("sublime_tool", {"name": "my_tool"}) == "my_tool"


def test_create_router_matches_module_route():
    router = create_router()
    args = {"message": "x", "completed": False, "blocked_reason": "stuck"}
    assert router.route("update_goal", args) == route("update_goal", args)
    assert router.has_tool("mcp__submarine__goal_verdict")
    assert not router.has_tool("chatroom")
