"""Legacy sublime-claude import — one-time, read-only source."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from core.migrate import (
    MARKER_NAME,
    discover_legacy_dir,
    migrate_loops,
    migrate_sessions,
    migrate_user_settings,
    normalize_record,
    run_migration,
)
from core.records import SESSIONS_CAP, SessionStore, default_sessions_path

REAL_LEGACY_SESSIONS = "/work/ai/sublime-claude/.sessions.json"

DROPPED_KEYS = (
    "persona_url",
    "pty_permission_mode",
    "pty_inject_sublime_mcp",
    "pty_auto_trust",
    "pty_busy_markers",
    "pty_busy_activity_ms",
    "claude_terminal_push_context",
)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _store(plugin_dir):
    return SessionStore(default_sessions_path(plugin_dir))


# ── normalize ────────────────────────────────────────────────────────────────

def test_normalize_terminal_becomes_sleeping():
    rec = {"session_id": "a", "state": "terminal", "mystery": 1}
    out = normalize_record(rec)
    assert out["state"] == "sleeping"
    assert out["mystery"] == 1
    assert rec["state"] == "terminal"


@pytest.mark.parametrize("state", ["open", "closed", "sleeping"])
def test_normalize_known_states_untouched(state):
    rec = {"session_id": "a", "state": state, "extra": True}
    out = normalize_record(rec)
    assert out["state"] == state
    assert out["extra"] is True
    assert out is not rec


def test_normalize_keeps_unknown_keys():
    rec = {
        "session_id": "x",
        "state": "closed",
        "weird": {"nested": 2},
    }
    out = normalize_record(rec)
    assert out["weird"] == {"nested": 2}
    assert set(out) == set(rec)


def test_normalize_drops_persona_keys():
    rec = {
        "session_id": "x",
        "state": "closed",
        "persona_id": "gone",
        "persona_session_id": "sess",
        "persona_url": "http://gone.example",
        "name": "keep",
    }
    out = normalize_record(rec)
    assert out["name"] == "keep"
    assert "persona_id" not in out
    assert "persona_session_id" not in out
    assert "persona_url" not in out
    assert rec["persona_id"] == "gone"


# ── merge ────────────────────────────────────────────────────────────────────

def test_merge_existing_session_id_not_overwritten(tmp_path):
    store = SessionStore(str(tmp_path / ".sessions.json"))
    store.save([{"session_id": "s1", "name": "ours", "last_activity": 1}])
    report = migrate_sessions(store, [
        {"session_id": "s1", "name": "theirs", "last_activity": 99},
        {"session_id": "s2", "name": "new", "last_activity": 50},
    ], source_path="/legacy/.sessions.json")
    rows = store.load()
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["s1"]["name"] == "ours"
    assert by_id["s2"]["name"] == "new"
    assert report["imported"] == 1
    assert report["skipped_existing"] == 1
    assert report["source_path"] == "/legacy/.sessions.json"


def test_merge_mru_order_by_last_activity(tmp_path):
    store = SessionStore(str(tmp_path / ".sessions.json"))
    store.save([
        {"session_id": "old", "last_activity": 10},
        {"session_id": "mid", "last_activity": 50},
    ])
    migrate_sessions(store, [
        {"session_id": "new", "last_activity": 80},
        {"session_id": "older", "last_activity": 1},
    ])
    ids = [r["session_id"] for r in store.load()]
    assert ids == ["new", "mid", "old", "older"]


def test_merge_cap_200_on_210_entries(tmp_path):
    store = SessionStore(str(tmp_path / ".sessions.json"))
    legacy = [
        {"session_id": "s%03d" % i, "last_activity": float(i), "state": "closed"}
        for i in range(210)
    ]
    report = migrate_sessions(store, legacy)
    rows = store.load()
    assert len(rows) == SESSIONS_CAP
    assert report["imported"] == SESSIONS_CAP
    ids = [r["session_id"] for r in rows]
    assert ids[0] == "s209"
    assert ids[-1] == "s010"
    assert "s000" not in ids
    assert "s009" not in ids
    assert len(set(ids)) == SESSIONS_CAP


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover_override_wins(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    sibling = tmp_path / "sublime-claude"
    sibling.mkdir()
    _write_json(str(sibling / ".sessions.json"), [])
    override = tmp_path / "override"
    override.mkdir()
    _write_json(str(override / ".sessions.json"), [{"session_id": "x"}])
    assert discover_legacy_dir(str(plugin), override=str(override)) == str(override)
    assert discover_legacy_dir(str(plugin)) == str(sibling)


def test_discover_installed_package_name(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    installed = tmp_path / "ClaudeCode"
    installed.mkdir()
    _write_json(str(installed / ".sessions.json"), [])
    assert discover_legacy_dir(str(plugin)) == str(installed)


def test_discover_missing_returns_none(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    assert discover_legacy_dir(str(plugin)) is None
    assert discover_legacy_dir(str(plugin), override=str(tmp_path / "nope")) is None


def test_discover_follows_symlink_plugin_dir(tmp_path):
    real_root = tmp_path / "real"
    plugin = real_root / "submarine"
    sibling = real_root / "sublime-claude"
    plugin.mkdir(parents=True)
    sibling.mkdir()
    _write_json(str(sibling / ".sessions.json"), [{"session_id": "x"}])
    pkgs = tmp_path / "Packages"
    pkgs.mkdir()
    link = pkgs / "Submarine"
    os.symlink(str(plugin), str(link))
    assert discover_legacy_dir(str(link)) == str(sibling)


# ── marker / run_migration ───────────────────────────────────────────────────

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_marker_second_call_imports_nothing(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_json(str(legacy / ".sessions.json"), [
        {"session_id": "a", "last_activity": 5, "state": "open"},
    ])
    first = run_migration(str(plugin), override_dir=str(legacy))
    assert first.get("already") is not True
    assert first["sessions"]["imported"] == 1
    assert (plugin / MARKER_NAME).is_file()

    store = _store(str(plugin))
    store.upsert({"session_id": "b", "name": "after", "last_activity": 9})

    second = run_migration(str(plugin), override_dir=str(legacy))
    assert second.get("already") is True
    ids = {r["session_id"] for r in store.load()}
    assert ids == {"a", "b"}
    assert store.find("a")["state"] == "open"


def test_incomplete_migration_retries_when_legacy_appears(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    first = run_migration(str(plugin), override_dir=str(tmp_path / "absent"))
    assert first.get("already") is not True
    assert first["sessions"]["imported"] == 0
    assert (plugin / MARKER_NAME).is_file()

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_json(str(legacy / ".sessions.json"), [
        {"session_id": "later", "last_activity": 5, "state": "open"},
    ])
    second = run_migration(str(plugin), override_dir=str(legacy))
    assert second.get("already") is not True
    assert second["sessions"]["imported"] == 1
    assert _store(str(plugin)).find("later")["session_id"] == "later"


def test_missing_legacy_dir_writes_marker(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    report = run_migration(str(plugin), override_dir=str(tmp_path / "absent"))
    assert report.get("already") is not True
    assert report["legacy_dir"] is None
    assert report["sessions"]["imported"] == 0
    assert report["sessions"]["skipped_existing"] == 0
    assert report["settings"] is None
    assert report["loops"] == 0
    assert (plugin / MARKER_NAME).is_file()
    marker = json.loads((plugin / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["sessions"]["imported"] == 0


def test_legacy_sessions_file_not_mutated(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    src = legacy / ".sessions.json"
    payload = [{"session_id": "t1", "state": "terminal", "last_activity": 1}]
    _write_json(str(src), payload)
    before = src.read_bytes()
    run_migration(str(plugin), override_dir=str(legacy))
    assert src.read_bytes() == before
    assert _store(str(plugin)).find("t1")["state"] == "sleeping"


# ── settings ─────────────────────────────────────────────────────────────────

def _legacy_settings_text():
    return """
{
  // user comment kept as data, not as a comment
  "custom_providers": {"foo": {"label": "Foo"}},
  "permission_mode": "acceptEdits",
  "persona_url": "http://gone.example",
  "pty_permission_mode": "bypass",
  "pty_inject_sublime_mcp": true,
  "pty_auto_trust": true,
  "pty_busy_markers": ["x"],
  "pty_busy_activity_ms": 12,
  "claude_terminal_push_context": true,
}
"""


def test_settings_copy_drops_removed_keys_keeps_providers(tmp_path):
    user = tmp_path / "User"
    user.mkdir()
    (user / "ClaudeCode.sublime-settings").write_text(
        _legacy_settings_text(), encoding="utf-8"
    )
    dest = migrate_user_settings(str(user))
    assert dest == str(user / "Submarine.sublime-settings")
    text = open(dest, encoding="utf-8").read()
    assert text.startswith("// Migrated from ClaudeCode.sublime-settings")
    data = json.loads("\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    ))
    assert data["custom_providers"] == {"foo": {"label": "Foo"}}
    assert data["permission_mode"] == "acceptEdits"
    for key in DROPPED_KEYS:
        assert key not in data


def test_settings_existing_submarine_not_overwritten(tmp_path):
    user = tmp_path / "User"
    user.mkdir()
    existing = user / "Submarine.sublime-settings"
    existing.write_text('{"custom_providers": {"keep": {}}}\n', encoding="utf-8")
    (user / "ClaudeCode.sublime-settings").write_text(
        _legacy_settings_text(), encoding="utf-8"
    )
    assert migrate_user_settings(str(user)) is None
    assert json.loads(existing.read_text(encoding="utf-8")) == {
        "custom_providers": {"keep": {}}
    }


# ── loops ────────────────────────────────────────────────────────────────────

def test_loops_copies_and_does_not_overwrite(tmp_path):
    src = tmp_path / "old_loops"
    dest = tmp_path / "new_loops"
    src.mkdir()
    (src / "a.json").write_text('{"wake": 1}', encoding="utf-8")
    (src / "b.json").write_text('{"wake": 2}', encoding="utf-8")
    (src / "skip.txt").write_text("nope", encoding="utf-8")
    dest.mkdir()
    (dest / "b.json").write_text('{"wake": "mine"}', encoding="utf-8")
    copied = migrate_loops(src_dir=str(src), dest_dir=str(dest))
    assert copied == 1
    assert json.loads((dest / "a.json").read_text(encoding="utf-8")) == {"wake": 1}
    assert json.loads((dest / "b.json").read_text(encoding="utf-8")) == {"wake": "mine"}
    assert not (dest / "skip.txt").exists()


def test_loops_via_run_migration_home(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    old = isolated_home / ".claude" / "sublime_claude_loops"
    old.mkdir(parents=True)
    (old / "sess.json").write_text('{"wake": true}', encoding="utf-8")
    report = run_migration(str(plugin))
    dest = isolated_home / ".submarine" / "loops" / "sess.json"
    assert report["loops"] == 1
    assert dest.is_file()


# ── end-to-end against the real .sessions.json ───────────────────────────────

@pytest.mark.skipif(
    not os.path.isfile(REAL_LEGACY_SESSIONS),
    reason="real sublime-claude .sessions.json not present",
)
def test_e2e_real_sessions_empty_store_and_conflict_wins(tmp_path, isolated_home):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    shutil.copy2(REAL_LEGACY_SESSIONS, str(legacy / ".sessions.json"))
    with open(REAL_LEGACY_SESSIONS, encoding="utf-8") as f:
        real = json.load(f)
    # The legacy file is live data — the row count changes over time
    # (upstream now prunes unused rows). Assert against the actual content.
    n = len(real)
    assert n > 0
    victim = real[0]
    sid = victim["session_id"]

    empty = run_migration(str(plugin), override_dir=str(legacy))
    assert empty["sessions"]["imported"] == n
    assert empty["sessions"]["skipped_existing"] == 0
    store = _store(str(plugin))
    loaded = store.load()
    assert len(loaded) == n
    assert store.find(sid)["name"] == victim.get("name")

    plugin2 = tmp_path / "plugin2"
    plugin2.mkdir()
    ours = {
        "session_id": sid,
        "name": "pre-existing-wins",
        "last_activity": 1.0,
        "state": "closed",
    }
    _store(str(plugin2)).save([ours])
    conflicted = run_migration(str(plugin2), override_dir=str(legacy))
    assert conflicted["sessions"]["skipped_existing"] == 1
    assert conflicted["sessions"]["imported"] == n - 1
    kept = _store(str(plugin2)).find(sid)
    assert kept["name"] == "pre-existing-wins"
    assert kept["state"] == "closed"
    assert len(_store(str(plugin2)).load()) == n
    # source copy must still match the original bytes
    assert (legacy / ".sessions.json").read_bytes() == open(
        REAL_LEGACY_SESSIONS, "rb"
    ).read()
