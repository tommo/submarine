"""Grant or revoke a browser's web UI access from the command palette."""
from __future__ import annotations

import time

import sublime
import sublime_plugin

from features.web_access import WebAccessError, default_store


def _ago(ts: object, now: float) -> str:
    try:
        secs = max(0, int(now - float(ts)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


class SubmarineWebAccessCommand(sublime_plugin.WindowCommand):
    """Pending requests and granted devices. Grant, deny, or revoke."""

    def run(self):
        try:
            data = default_store().list_devices()
        except WebAccessError as e:
            sublime.status_message("Web access: %s" % e.message)
            return
        except Exception as e:
            sublime.status_message("Web access: %s" % e)
            return
        now = time.time()
        items = []
        rows = []  # type: list
        for row in data.get("pending") or []:
            ip = row.get("ip") or "unknown address"
            items.append([
                "Pending  %s" % (row.get("name") or "unnamed"),
                "%s · %s ago" % (ip, _ago(row.get("created"), now)),
            ])
            rows.append(("pending", row.get("id")))
        for row in data.get("granted") or []:
            ip = row.get("ip") or "unknown address"
            seen = row.get("last_seen") or row.get("created")
            items.append([
                "Granted  %s" % (row.get("name") or "unnamed"),
                "%s · last seen %s ago" % (ip, _ago(seen, now)),
            ])
            rows.append(("granted", row.get("id")))
        if not items:
            items = [["Nothing pending or granted",
                      "A browser's request shows up here"]]
            rows.append(("empty", ""))

        def on_pick(idx):
            if idx < 0 or rows[idx][0] == "empty":
                return
            kind, ident = rows[idx]
            if kind == "pending":
                self._decide(ident)
            else:
                self._revoke(ident)

        self.window.show_quick_panel(items, on_pick)

    def _decide(self, request_id):
        items = [
            ["Grant", "Let this device use the web UI"],
            ["Deny", "Refuse this request"],
        ]

        def on_pick(idx):
            if idx < 0:
                return
            store = default_store()
            try:
                if idx == 0:
                    store.grant(request_id)
                    sublime.status_message("Web access granted")
                else:
                    store.deny(request_id)
                    sublime.status_message("Web access denied")
            except WebAccessError as e:
                sublime.status_message("Web access: %s" % e.message)
                return
            self.run()

        self.window.show_quick_panel(items, on_pick)

    def _revoke(self, device_id):
        items = [["Revoke", "This device must request access again"]]

        def on_pick(idx):
            if idx < 0:
                return
            try:
                default_store().revoke(device_id)
            except WebAccessError as e:
                sublime.status_message("Web access: %s" % e.message)
                return
            sublime.status_message("Web access revoked")
            self.run()

        self.window.show_quick_panel(items, on_pick)
