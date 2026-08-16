# Profiles

Profiles configure model, system prompt, and related session options for different
tasks. Named saved `session_id` snapshots are not supported — use a profile with
`system_prompt` / `preload_docs`, or **live-fork** an open session.

## File locations

Cascade loading (project overrides user when names conflict):

| Level | Path | Purpose |
|-------|------|---------|
| User | `~/.submarine/profiles.json` | Global profiles across projects |
| Project | `{project}/.claude/profiles.json` | Project-specific profiles |

Only the `"profiles"` map is loaded; other top-level keys are ignored.

The settings comment in `Submarine.sublime-settings` still mentions
`~/.claude-sublime/profiles.json`. The loader reads `~/.submarine/profiles.json`
(`plat.constants.USER_PROFILES_DIR`). Copy or symlink old files if you still
have them under `~/.claude-sublime/`.

## Profile options

```json
{
  "profiles": {
    "default": {
      "model": "opus",
      "description": "Default Opus for general work"
    },
    "design": {
      "model": "sonnet",
      "betas": ["context-1m-2025-08-07"],
      "system_prompt": "You are a software architect. Focus on design patterns and architecture.",
      "preload_docs": ["docs/**/*.md"],
      "effort": "high",
      "description": "Sonnet 1M for design with docs preloaded"
    },
    "quick": {
      "model": "haiku",
      "description": "Fast Haiku for simple tasks"
    }
  }
}
```

| Option | Description |
|--------|-------------|
| `model` | Model id or alias (`opus`, `sonnet`, `haiku`, or a backend-specific id) |
| `betas` | Beta feature flags (passed on Claude-bridge init) |
| `system_prompt` | Replaces the session system prompt |
| `append_system_prompt` | Appended instead of replacing (used by Quick Agent) |
| `preload_docs` | Glob patterns advertised as the session docset (`list_profile_docs` / `read_profile_doc`) |
| `effort` | Reasoning override (`low` / `medium` / `high` / `max`); blank → provider then global `effort` |
| `description` | Shown in the New Session / Switch pickers |

`model`, `betas`, `system_prompt`, `append_system_prompt`, and `effort` are
applied at session start (`core/session.py` init params).

## Usage

- **New Session** — if any profiles exist, a picker lists `🆕 New Session` plus
  each `📋 {name}`.
- **Switch Session** (`Cmd+\`) — same profiles as start rows.
- **Restart Session** — restart the current view with a chosen profile.
- **MCP** — `list_profiles()` then `spawn_session(prompt=…, profile="design")`.

### Specialized roles

```json
"profiles": {
    "reviewer": {
        "model": "opus",
        "system_prompt": "You are a code reviewer. Focus on bugs, security issues, and maintainability."
    },
    "documenter": {
        "model": "sonnet",
        "system_prompt": "You write clear, concise documentation."
    }
}
```

### Agent-spawned sessions

```python
list_profiles()
spawn_session(
    prompt="Analyze the authentication flow",
    profile="design",
    name="auth-analysis"
)
```

## MCP tools

| Tool | Description |
|------|-------------|
| `list_profiles` | Returns available profiles |
| `spawn_session` | Create a session with optional `profile` (and live fork args) |
| `list_profile_docs` | List this session's profile docset |
| `read_profile_doc` | Read one doc by relative path |

## Tips

- Keep `preload_docs` focused — load only what the session needs.
- Store cross-project roles in `~/.submarine/profiles.json`.
- Want warm conversation history? Live-fork an open explorer session
  (`fork_current` / `fork_from_agent_id`).
