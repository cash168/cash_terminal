# Cash Terminal

Terminal emulator built on a **Rust core** (`cashterm_core`, a pyo3 extension
wrapping [`alacritty_terminal`](https://crates.io/crates/alacritty_terminal)
0.24) with a **Python / GTK4** front-end (PyGObject). The Rust core owns the
PTY, the VT/ANSI parser and the terminal grid; the Python side renders the
visible grid with **Cairo + Pango**, handles keyboard/mouse input, and adds a
tabbed UI, search overlay, paste-security pipeline, right-click menu, a live
settings dialog, and desktop integration.

> Earlier versions embedded `Vte.Terminal`. VTE has been fully replaced by the
> Rust core; there is no longer any VTE dependency.

## Architecture

- **Rust core (`cashterm_core.PtyTerm`)** — spawns the PTY, runs the
  `alacritty_terminal` parser/grid, and exposes a snapshot of the visible
  cells (glyphs, colors, SGR attributes, cursor, scrollback offset) to Python
  over a compact binary wire format (see the snapshot decoder in
  `core_terminal.py`, which must match `cashterm_core/src/lib.rs`).
- **Cairo/Pango renderer** — `CashTerminal` subclasses `Gtk.DrawingArea` and
  paints each frame in `set_draw_func`: a Pango layout per glyph run, ANSI/
  true-color fills, reverse/bold/italic/underline attributes, the cursor, the
  selection, and the search highlights — all in one Cairo pass.
- **Vte-compatible widget API** — `CashTerminal` re-implements the subset of
  the old `Vte.Terminal` API the app already consumed (`spawn`, `feed_child`,
  `get_cursor_position`, `get_text_range`/`text_abs`, `get_has_selection`,
  `copy_clipboard_format`, signals like `contents-changed`,
  `child-exited`, …), so the rest of the package needed no rewrite.
- **Mixin-based tab class** — `TerminalTab` (a `Gtk.Box`) composes
  `SearchMixin`, `PasteMixin`, and `InteractionMixin`, each in its own module.

## Features

### Rendering & Display

- **Full ANSI/SGR support** — 16-color palette, 256-color indexed, true-color
  (24-bit RGB), and SGR attributes (bold, italic, faint, underline,
  strikethrough, reverse), parsed by `alacritty_terminal` and drawn by the
  Cairo renderer.
- **Configurable color scheme** — palette, foreground/background, and cursor
  colors loaded from `linux.yaml` and editable live in the Settings dialog.
- **Background opacity** — configurable alpha (`0.0`–`1.0`) for compositor
  transparency.
- **Cursor hiding on empty prompt line** — when the cursor lands at column 0
  (blank line before the prompt) its color is set to the background so it
  doesn't flash; restored as soon as the prompt or input appears.

### Tabs

- **Tabbed interface** — `Gtk.Notebook` with a per-tab `CashTerminal`; each tab
  runs its own login shell.
- **Single-instance via D-Bus** — the app uses
  `Gio.ApplicationFlags.HANDLES_COMMAND_LINE`; a second launch opens a new tab
  in the existing window instead of starting a new process.
- **Tab shortcuts**:
  - `Ctrl+T` — new tab (inherits cwd from current tab)
  - `Ctrl+W` — close current tab (last tab quits app)
  - `Ctrl+Tab` — MRU (most recently used) switch
  - `Ctrl+Left` / `Ctrl+Right` — sequential tab navigation
- **Drag-to-reorder** — drag tabs with the mouse to rearrange them; MRU history
  and close-button behavior are preserved.
- **Opera-like close** — both `Ctrl+W` and clicking × switch to the adjacent
  tab so the × stays under the cursor for rapid sequential closes.
- **Tab list overlay** — `Ctrl+Up` / `Ctrl+Down` or the ▼ button opens a popup
  list with live preview, keyboard navigation, and click support.
- **Smart tab titles** — format `dir : process`; SSH tabs show `(user) host`.

### Search

- **Search UI** — `Ctrl+F` opens a Chrome/VSCode-style search bar in the
  top-right corner.
- **Incremental search** — results update as you type (debounced).
- **Custom highlight overlay** — all visible matches are highlighted and the
  current match emphasized, painted directly by the Cairo renderer
  (`set_highlight_data` → `CashTerminal` draw pass) so highlights and text are
  always composited in the same frame with no jitter or one-frame lag.
- **Navigation** — `Enter` / `Shift+Enter` / `F3` cycle through matches with
  wrap-around at top/bottom.
- **Esc** — close the search bar and return focus to the terminal.

### Text Selection & Clipboard

- **Mouse selection** — click-and-drag selection, with copy via the right-click
  menu or `Ctrl+Shift+C`.
- **Click-to-position** — in normal (non-alt-screen) shell mode, a left-click
  on the cursor row sends the right number of arrow-key sequences so readline
  repositions its cursor to the clicked column.

### Right-Click Context Menu

- **Plain `Gtk.Popover` menu** (icon-theme-independent) with: Copy, Paste,
  Select All, Clear, Search, New Tab, Close Tab, and Settings…
- **Copy** is enabled only when there is an active selection.
- Right-click is suppressed when a TUI app has mouse tracking on (the event is
  forwarded to the program instead).

### Settings Dialog

- **Live-preview appearance editor** — opened from the context menu; changes
  apply immediately to every open tab.
- **Color editing** — primary foreground/background, cursor, and all 16 ANSI
  palette entries via `Gtk.ColorDialogButton`.
- **Presets** — Konsole Linux Colors, Solarized Dark/Light, Dracula, Gruvbox,
  One Dark; the dropdown auto-syncs to "(custom)" when colors are hand-edited.
- **Font & opacity** — monospace family picker, size stepper, and a background
  opacity slider. The picker/stepper are built from plain widgets so they
  render correctly even without `adwaita-icon-theme`.
- **Environment variables** — add/edit/remove the `env:` name/value pairs passed
  to newly spawned shells. An empty value removes the variable from the child
  environment. (Env changes apply to new tabs, not already-running shells.)
- **Save / Cancel** — Save writes back to `~/.cash-terminal/linux.yaml`
  (preserving unrelated sections); Cancel (or Esc / window close) restores the
  snapshot taken when the dialog opened.

### Paste Security

- **Sanitized paste pipeline** — `Ctrl+Shift+V` (and the menu's Paste) is
  routed through a security filter that strips ANSI/OSC escape sequences,
  C0/C1 control characters (except Tab and LF), and invisible Unicode
  (zero-width spaces, directional overrides, BOM).
- **Trailing newline stripping** — removes trailing `\n`/`\r` to prevent
  auto-execution of pasted commands.
- **Large-paste confirmation** — a paste of **10 000 characters or more** shows
  a `⚠ Large paste (N chars)?` confirmation bar (binary-like content is flagged
  as "suspicious"); smaller pastes go through directly. The threshold is
  `TerminalTab._PASTE_CONFIRM_THRESHOLD`.
- **Bracketed paste mode** — honored when the shell requested mode 2004, so the
  whole paste is treated as a single input block (nothing runs until Enter).

### Confirmation Bar

- **Dangerous command interception** — when `mc` is running in a background
  (`Ctrl+O`) subshell, launching alt-screen programs (`vim`, `htop`, `tmux`,
  `ssh`, etc.) shows a confirmation bar first, to avoid alt-screen conflicts.
- **Multiline paste confirmation** reuses the same bar UI.
- **Enter** confirms, **Esc** / **Ctrl+C** cancels.

### Alt-Screen & Mouse Handling

- **Alt-screen detection** — the foreground process group is read via
  `TIOCGPGRP` and checked against a list of known line-oriented programs;
  a scroll-state heuristic is used as a fallback (e.g. for ssh/mosh).
- **Mouse in TUI apps** — when an app enables mouse tracking, mouse events are
  forwarded to the PTY; Cash Terminal only adds click-to-position in normal
  shell mode.

### Keyboard

- **Non-Latin layout support** — control shortcuts (`Ctrl+R`, `Ctrl+O`, …) work
  even on a Russian/non-Latin keyboard layout: the keycode is translated to its
  Latin equivalent before deriving the control byte, so `Ctrl`-combos send the
  correct C0 code instead of a Cyrillic character.

### Desktop Integration

- **Icon installation** — on first run, installs the SVG plus rasterized PNGs
  into `~/.local/share/icons/hicolor/` and creates a `.desktop` file.
- **Maximized on launch** — the window starts maximized (configurable).

### Configuration

All settings live in `~/.cash-terminal/linux.yaml`, auto-created on first run
from the bundled default (`cash_terminal/resources/linux.yaml`):

| Section | Keys |
|---------|------|
| `colors.primary` | `background`, `foreground`, `background_opacity` |
| `colors.cursor` | `text`, `cursor` |
| `colors.normal` / `colors.bright` | 8 ANSI color names each |
| `colors.tabs` | gradient colors, `active_bar_color` |
| `font` | `normal.family`, `size` |
| `scrollback_lines` | max scrollback buffer size |
| `background_opacity` | `0.0`–`1.0` (also accepted under `colors.primary`) |
| `startup` | `directory`, `maximized`, `window_size` |
| `tab_list` | `preview`, `activate_on_select`, `show_on_ctrl_tab`, `show_on_ctrl_tab_delay` |
| `env` | environment variables for the child shell (`TERM`, `COLORTERM`, …) |
| `inputrc` | readline overrides (`enable-active-region`, …); set to `false` to disable |
| `debug` | `true`/`false` — verbose logging |

> The default file also contains `colors.dim` and `colors.scrollbar` sections
> (alacritty-style); these are currently not consumed by the renderer.

#### Environment Variables (`env`)

The `env` section sets environment variables for every spawned shell. The
application hardcodes none — `CHILD_ENV` defaults to empty, and all values come
exclusively from `env:` in `linux.yaml`. If the section is missing, the child
shell inherits the (cleaned) parent environment as-is.

- Override a variable: `TERM: xterm-kitty`
- Add a variable: `MY_VAR: my_value`
- Remove an inherited variable: `MY_VAR: null`

Before applying these, the spawner also scrubs AppImage/Qt/IDE leak variables
(`APPDIR`, `LD_LIBRARY_PATH`, `QT_QPA_*`, `/tmp/.mount_*` PATH entries, …) so a
shell launched from a bundled environment behaves like a normal login shell.

#### Readline / inputrc Overrides (`inputrc`)

Cash Terminal creates `~/.cash-terminal/inputrc` that `$include`s
`/etc/inputrc` and `~/.inputrc`, then appends overrides from this section and
points the child shell's `INPUTRC` at it:

```yaml
inputrc:
  enable-active-region: "off"
```

`enable-active-region` defaults to `off` to suppress the reverse-video
highlight readline 8.1+ applies to bracketed-paste text. Set `inputrc: false`
to disable all overrides (use system/user inputrc as-is).

### CLI Options

```
cash-terminal [OPTIONS]

  --scheme FILE        Color scheme YAML file
                       (default: ~/.cash-terminal/linux.yaml)
  --directory, -d DIR  Start in this directory
```

## Dependencies

| Package | Ubuntu/Debian install |
|---------|----------------------|
| Python 3 | `sudo apt install python3` |
| PyGObject (+ Cairo) | `sudo apt install python3-gi python3-gi-cairo` |
| GTK4 GI bindings | `sudo apt install gir1.2-gtk-4.0` |
| Pango / Cairo (GI) | included with the GTK4 / PyGObject packages above |
| Rust toolchain | `rustup` from <https://rustup.rs> (provides `cargo`) |
| maturin, PyYAML | installed automatically into the build venv by `build.sh` |

`cashterm_core` is a compiled pyo3 extension; building it requires the Rust
toolchain (`cargo`). There is **no** VTE dependency.

## Build & Install

```bash
# Build the Rust core + Python front-end and install to
# /usr/local/sbin/cash-terminal
./build.sh

# Run
cash-terminal

# Uninstall
./build.sh remove
```

`build.sh` is self-contained and does everything itself:

1. **Checks dependencies** — `python3`, PyGObject, GTK4, and `cargo` (it sources
   `~/.cargo/env` / adds `~/.cargo/bin` to PATH if needed).
2. **Creates an isolated venv** at
   `~/.local/share/cash-terminal/venv` with `--system-site-packages` (so it
   sees the distro's PyGObject/Cairo), and installs `maturin` + `pyyaml` into
   it.
3. **Builds the Rust core** — `maturin develop --release` compiles
   `cashterm_core/` and installs the native extension into the venv (run inside
   a scoped subshell so the parent shell keeps no venv activated).
4. **Packages the Python front-end** into a self-contained
   [zipapp](https://docs.python.org/3/library/zipapp.html) (`.pyz`) at
   `~/.local/share/cash-terminal/cash-terminal.pyz`. The bundled icon and
   default config live in `cash_terminal/resources/` and load at runtime via
   `importlib.resources`.
5. **Installs a launcher** at `/usr/local/sbin/cash-terminal` — a tiny shim that
   runs the zipapp under the venv's Python (which holds the compiled
   `cashterm_core`; a native `.so` can't live inside a zipapp).

Uninstall (`./build.sh remove`) removes the launcher, the runtime data
directory (venv + zipapp), and the installed icons / `.desktop` file.

## Project Files

| Path | Description |
|------|-------------|
| `cash_terminal/` | Python application package (front-end) |
| `cash_terminal/app.py` | `TerminalApp` (Gtk.Application), window/notebook, CSS, `main()` |
| `cash_terminal/core_terminal.py` | `CashTerminal` — `Gtk.DrawingArea` widget backed by `cashterm_core`; Cairo/Pango renderer + input/mouse, Vte-compatible API |
| `cash_terminal/tab.py` | `TerminalTab` — per-tab widget, shell spawn/env, right-click menu, composing the mixins |
| `cash_terminal/search.py` | `SearchMixin` — search bar, match finding, navigation, highlight push |
| `cash_terminal/paste.py` | `PasteMixin` — paste sanitization and confirmation |
| `cash_terminal/interaction.py` | `InteractionMixin` — alt-screen detection, cursor hiding, click-to-position |
| `cash_terminal/session_picker.py` | `SessionPicker` — searchable list of what a tab can run (local shell, favourites, recent SSH); used for `new_tab.action: picker` and Ctrl+E |
| `cash_terminal/connections.py` | Favourites + SSH history store, kept in `~/.cash-terminal/connections.yaml` (rewritten by the app, separate from `linux.yaml`) |
| `cash_terminal/connections_dialog.py` | `ConnectionsDialog` — editor for those favourites; promotes history entries into them |
| `cash_terminal/settings.py` | `SettingsDialog` — live-preview editor (colors/presets/font/opacity, startup, behaviour, tab list, env) |
| `cash_terminal/presets.py` | Built-in color-scheme presets + current-scheme matching |
| `cash_terminal/config.py` | Constants + `linux.yaml` loader/saver |
| `cash_terminal/util.py` | Small helpers (keyval→Latin translation, OSC 52 regex) |
| `cash_terminal/resources/` | Bundled icon (`cash-terminal.svg`) and default `linux.yaml` — the copies that actually ship, loaded via `importlib.resources` |
| `cash_terminal/__init__.py` | Package init — pins the GTK/GDK versions before any `gi.repository` import |
| `cash_terminal/__main__.py` | Package entry point (`from .app import main`) |
| `cashterm_core/` | Rust crate — pyo3 extension wrapping `alacritty_terminal` |
| `cashterm_core/src/lib.rs` | `PtyTerm` core: PTY + parser + grid + snapshot wire format |
| `cashterm_core/Cargo.toml` | Rust crate manifest (pyo3 0.22, alacritty_terminal 0.24) |
| `cashterm_core/Cargo.lock` | Pinned Rust dependency versions — committed so a build is reproducible |
| `cashterm_core/pyproject.toml` | maturin build config (builds the crate as the `cashterm_core` extension module) |
| `build.sh` | Build/install script — venv + maturin core build + zipapp + launcher |
| `check_deps.py` | Optional diagnostic — reports which dependencies are present and what to install (not required for build) |
| `LICENSE` | Apache License 2.0 |

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE).

Copyright 2026 Aleksandr Popov.

### Third-party licenses

Nothing in the dependency tree is copyleft, so building or redistributing this
project carries no share-alike obligation.

The Rust core links `alacritty_terminal` (Apache-2.0), `pyo3`, `portable-pty`
and `rustix` (all MIT/Apache-2.0 dual-licensed), plus their transitive
dependencies — MIT or Apache-2.0 throughout. Apache-2.0 was chosen for this
project so that a distributed binary, which statically links all of them, ends
up under a single license.

GTK4, PyGObject and cairo (LGPL-2.1+) are *not* redistributed here: they are
loaded dynamically from the distribution's own packages, which is why
`build.sh` creates the venv with `--system-site-packages`.
