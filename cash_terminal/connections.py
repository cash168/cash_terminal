"""Session list for the new-tab picker: favourites + SSH connection history.

Stored in its own file (~/.cash-terminal/connections.yaml) rather than in
linux.yaml.  Two reasons: history is rewritten every time a connection is made,
while linux.yaml is hand-edited; and _save_yaml_config() rewrites linux.yaml
wholesale (dropping comments), which is fine for a settings dialog but not for
something touched in the background.

Entry shapes:
    favourite  {"name": str, "command": str}
    group      {"type": "group", "name": str}   — titled separator in the list
    history    {"command": str, "target": str, "count": int, "last": float}

A group carries no command: it only splits the favourites into named sections
in the picker.  load_favorites() returns entries with an explicit "type"
("item" / "group"); an entry stored without one is an item, so files written
before groups existed keep working.

`command` is always a full command line (e.g. "ssh -p 2222 user@host").  It is
split with shlex and executed directly — there is no shell in between, so the
connection is the tab's own child process and closing it closes the tab.
"""
import os
import shlex
import time

import yaml


PATH = os.path.join(os.path.expanduser("~"), ".cash-terminal", "connections.yaml")

# How much history to keep on disk...
HISTORY_LIMIT = 100
# ...and how much of it the picker shows under the favourites.
PICKER_HISTORY = 5


def _read():
    """Load the whole file as a dict; never raises."""
    try:
        with open(PATH, "r") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data):
    """Write the file atomically.  Returns True on success.

    tmp + os.replace so a crash mid-write cannot leave a truncated file that
    would lose the user's favourites.
    """
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, PATH)
        return True
    except (OSError, yaml.YAMLError):
        return False


def load_favorites():
    """Favourites in the user's own order.  Malformed entries are skipped.

    Every returned entry has a "type": "item" (name + command) or "group"
    (name only).  A nameless group or a commandless item is dropped — neither
    can be displayed sensibly.
    """
    out = []
    for item in _read().get("favorites") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if str(item.get("type") or "").strip().lower() == "group":
            if name:
                out.append({"type": "group", "name": name})
            continue
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        out.append({"type": "item", "name": name or command, "command": command})
    return out


def save_favorites(items):
    """Replace the favourites list, leaving history untouched.

    Items are written without a "type" key — the plain {name, command} shape
    stays the common case and the file stays easy to read by hand.
    """
    out = []
    for i in items:
        name = str(i.get("name") or "").strip()
        if str(i.get("type") or "").strip().lower() == "group":
            if name:
                out.append({"type": "group", "name": name})
            continue
        command = str(i.get("command") or "").strip()
        if not command:
            continue
        out.append({"name": name or command, "command": command})
    data = _read()
    data["favorites"] = out
    return _write(data)


def load_history(limit=None):
    """History, most recently used first."""
    out = []
    for item in _read().get("history") or []:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        try:
            count = int(item.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        try:
            last = float(item.get("last") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        out.append({
            "command": command,
            "target": str(item.get("target") or "").strip() or command,
            "count": count,
            "last": last,
        })
    out.sort(key=lambda e: e["last"], reverse=True)
    return out[:limit] if limit else out


def clear_history():
    data = _read()
    data["history"] = []
    return _write(data)


def record(argv, target=""):
    """Record one connection, identified by its argv as read from /proc.

    Called from the tab-title poll, which already knows when the foreground
    process is ssh.  Deduplicated on the full command line: `ssh host` and
    `ssh -p 2222 host` are genuinely different connections and both are worth
    keeping, while a repeat of the same one only bumps its counter.
    """
    if not argv:
        return False
    try:
        command = shlex.join(argv)
    except (AttributeError, TypeError):  # shlex.join is 3.8+
        command = " ".join(shlex.quote(a) for a in argv)
    command = command.strip()
    if not command:
        return False

    data = _read()
    hist = data.get("history")
    if not isinstance(hist, list):
        hist = []

    now = time.time()
    for item in hist:
        if isinstance(item, dict) and str(item.get("command") or "").strip() == command:
            try:
                item["count"] = int(item.get("count") or 0) + 1
            except (TypeError, ValueError):
                item["count"] = 1
            item["last"] = now
            if target:
                item["target"] = target
            break
    else:
        hist.append({
            "command": command,
            "target": target or command,
            "count": 1,
            "last": now,
        })

    # Keep the most recent HISTORY_LIMIT entries.
    hist.sort(key=lambda e: (e.get("last") or 0) if isinstance(e, dict) else 0,
              reverse=True)
    data["history"] = hist[:HISTORY_LIMIT]
    return _write(data)
