# Root edits for web access

Applied in `submarine.py`. Saving that file reloads the plugin.

`commands/__init__.py` re-exports the class. Sublime only registers command
classes imported from a root module, and it often reloads `submarine.py`
before the commands package. The import reloads `commands.web_access_cmds`
and falls back if the name is missing, same as the cycle-session command.
An unconditional name in `from commands import (` would abort plugin load.

```python
import commands.web_access_cmds as _web_access_cmds
try:
    importlib.reload(_web_access_cmds)
except Exception:
    pass
try:
    from commands.web_access_cmds import SubmarineWebAccessCommand  # noqa: F401
except ImportError:
    SubmarineWebAccessCommand = getattr(
        _web_access_cmds, "SubmarineWebAccessCommand", None)
```

Palette entry: `Submarine: Web Access…` (`submarine_web_access`).
