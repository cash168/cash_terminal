"""InteractionMixin — alt-screen, cursor-hide, mouse click, OSC commit."""
import os
from gi.repository import Gtk, Gdk, GLib
from . import config
from .config import _hex_to_rgba


class InteractionMixin:
    # ----------------------------------------------------------- Alt screen detection
    # Programs that use the normal screen buffer (not alt screen).
    # These are shells, interpreters, and line-oriented tools.
    # Note: ssh and mosh are REMOVED from here because they can contain TUI apps.
    _NORMAL_SCREEN_PROGRAMS = frozenset({
        "bash", "zsh", "sh", "dash", "fish", "ksh", "csh", "tcsh",
        "python", "python3", "python3.10", "python3.11", "python3.12",
        "node", "ruby", "irb", "perl", "lua",
        "sudo", "su", "login", "sshd",
        "cat", "grep", "sed", "awk", "sort", "head", "tail", "wc",
        "find", "xargs", "tee", "tr", "cut", "uniq", "diff",
        "git", "make", "cargo", "npm", "pip", "pip3",
        "psql", "mysql", "sqlite3", "redis-cli", "mongo", "mongosh",
        "gdb", "lldb", "strace", "ltrace",
        "docker", "kubectl", "terraform",
    })

    def _is_alt_screen(self):
        """Detect if VTE is in alternate screen buffer (mc, vim, htop, etc.).

        Primary method: get the foreground process via TIOCGPGRP.
        Special case for SSH/Mosh: since we can't know the remote state,
        we rely on the fallback (scrolling state).
        """
        if self._child_pid > 0:
            try:
                import struct, fcntl, termios
                pty = self.terminal.get_pty()
                if pty is not None:
                    fd = pty.get_fd()
                    buf = fcntl.ioctl(fd, termios.TIOCGPGRP,
                                      struct.pack('i', 0))
                    fg_pgid = struct.unpack('i', buf)[0]
                    if fg_pgid > 0 and fg_pgid == self._child_pid:
                        return False  # shell itself — not alt screen
                    if fg_pgid > 0 and fg_pgid != self._child_pid:
                        # Check what program is in the foreground
                        try:
                            with open(f"/proc/{fg_pgid}/comm") as f:
                                comm = f.read().strip()
                            # If it's a known CLI tool, it's NOT alt screen.
                            # If it's SSH or unknown, we MUST use the fallback check below.
                            if comm in self._NORMAL_SCREEN_PROGRAMS:
                                return False
                        except (OSError, IOError):
                            pass
            except Exception:
                pass

        # Fallback: vadjustment heuristic.
        # If scrollback is disabled (upper <= page_size), we are likely in alt-screen.
        vadj = self.terminal.get_vadjustment()
        if vadj is None:
            return False
        upper = vadj.get_upper()
        page = vadj.get_page_size()
        
        # When upper exactly equals page, we are in a non-scrollable buffer (Alt Screen)
        return upper > 0 and upper <= page

    # ------------------------------------------------ Cursor-hide on col 0
    def _build_cursor_cover(self):
        """Set up cursor-hiding via VTE color API.

        When cursor lands at col 0 (empty line before prompt), we make
        the cursor invisible by setting its color to the background.
        When the prompt appears (col > 0), we restore the normal cursor
        color.  This is the cleanest approach — no overlays, no snapshots,
        just VTE's own API.
        """
        pass  # colors are applied in _apply_colors; nothing extra needed

    def hide_cursor_for_enter(self):
        """Called on Enter — no-op.

        This was a VTE-era trick that recolored the cursor to the background
        color to hide the brief flash at col 0 before the shell redrew the
        prompt.  With the Rust-core renderer the cursor is a steadily-drawn
        block, so recoloring to the background just made it appear black on
        Enter.  We now keep the cursor a constant gray, so nothing to do here.
        """
        return

    def _hide_vte_cursor(self):
        """Make VTE cursor invisible by setting its color to background."""
        if self._cursor_hidden_for_enter:
            return
        self._cursor_hidden_for_enter = True
        bg_rgba = _hex_to_rgba(config.BG_COLOR, alpha=1.0)
        if bg_rgba:
            self.terminal.set_color_cursor(bg_rgba)
            self.terminal.set_color_cursor_foreground(bg_rgba)
        # Safety timeout: restore after 200ms
        if self._cursor_restore_timer is not None:
            GLib.source_remove(self._cursor_restore_timer)
        self._cursor_restore_timer = GLib.timeout_add(
            200, self._restore_vte_cursor_timeout)

    def _restore_vte_cursor(self):
        """Restore normal VTE cursor colors."""
        if not self._cursor_hidden_for_enter:
            return
        self._cursor_hidden_for_enter = False
        if self._cursor_restore_timer is not None:
            GLib.source_remove(self._cursor_restore_timer)
            self._cursor_restore_timer = None
        cursor_bg_rgba = _hex_to_rgba(config.CURSOR_BG)
        cursor_fg_rgba = _hex_to_rgba(config.CURSOR_FG)
        if cursor_bg_rgba:
            self.terminal.set_color_cursor(cursor_bg_rgba)
        if cursor_fg_rgba:
            self.terminal.set_color_cursor_foreground(cursor_fg_rgba)

    def _restore_vte_cursor_timeout(self):
        """Safety timeout to restore cursor if prompt never appeared."""
        self._cursor_hidden_for_enter = False
        self._cursor_restore_timer = None
        cursor_bg_rgba = _hex_to_rgba(config.CURSOR_BG)
        cursor_fg_rgba = _hex_to_rgba(config.CURSOR_FG)
        if cursor_bg_rgba:
            self.terminal.set_color_cursor(cursor_bg_rgba)
        if cursor_fg_rgba:
            self.terminal.set_color_cursor_foreground(cursor_fg_rgba)
        return False  # don't repeat

    def _on_vte_cursor_moved(self, terminal):
        """Restore cursor immediately when it moves away from col 0 or changes row after Enter."""
        col, row = self.terminal.get_cursor_position()
        if self._cursor_hidden_for_enter:
            # If the cursor moved to right (shell prompt) or to a different row, restore it.
            if col > 0 or row != self._prev_cursor_row:
                self._restore_vte_cursor()
        self._prev_cursor_row = row

    # ----------------------------------------------------------- VTE paste block
    def _on_vte_paste_blocked(self, terminal):
        """Block VTE's built-in paste and redirect through our security pipeline."""
        # VTE emits paste-clipboard on Ctrl+Shift+V.  We block it here
        # and use our own _paste_from_clipboard() which sanitizes and
        # shows confirmation for multiline/suspicious content.
        self._paste_from_clipboard()
        # Stop signal propagation — VTE must NOT paste directly
        GObject = __import__('gi.repository', fromlist=['GObject']).GObject
        terminal.stop_emission_by_name("paste-clipboard")

    # ----------------------------------------------------------- OSC 52 filtering
    def _on_commit(self, terminal, text, size):
        """Intercept VTE commit signal — used for tracking alt screen state."""
        # VTE handles OSC 52 filtering internally in newer versions,
        # but we track alt screen transitions for mc subshell detection.
        pass

    # ------------------------------------------------- Mouse click → readline
    def _on_terminal_click(self, gesture, n_press, x, y):
        """Move readline cursor on left-click in normal (non-alt-screen) mode.

        VTE doesn't forward mouse events to PTY unless the running app
        enables mouse tracking (e.g. mc, vim do; readline does not).
        In normal shell mode we compute the column delta between the
        current cursor position and the clicked column, then send the
        appropriate number of Left/Right arrow escape sequences so
        readline repositions its cursor.
        """
        if self._is_alt_screen():
            # Alt-screen app (mc, vim, htop) — let VTE handle the click
            # normally (selection, app mouse events via VTE's own tracking).
            return

        try:
            # Cell size in pixels
            char_w = self.terminal.get_char_width()
            char_h = self.terminal.get_char_height()
            if char_w <= 0 or char_h <= 0:
                return

            # Column/row of the click (0-based)
            click_col = int(x / char_w)
            click_row = int(y / char_h)

            # Current cursor position (col, row) — 0-based
            cur_col, cur_row = self.terminal.get_cursor_position()

            # Adjust for scroll offset: vadj value tells us how many rows
            # are scrolled above the viewport.
            vadj = self.terminal.get_vadjustment()
            scroll_rows = 0
            if vadj:
                scroll_rows = int(vadj.get_upper() - vadj.get_page_size()
                                  - vadj.get_value())
            # cur_row is absolute; click_row is relative to viewport top.
            # viewport_top_abs = upper - page - (upper - page - value) = value
            # absolute click row = vadj.value + click_row
            if vadj:
                abs_click_row = int(vadj.get_value()) + click_row
            else:
                abs_click_row = click_row

            # Only move cursor if click is on the same row as the cursor
            if abs_click_row != cur_row:
                return

            delta = click_col - cur_col
            if delta == 0:
                return

            pty = self.terminal.get_pty()
            if not pty:
                return
            fd = pty.get_fd()
            if fd < 0:
                return

            if delta > 0:
                # Move right: ESC[C repeated
                seq = b"\x1b[C" * delta
            else:
                # Move left: ESC[D repeated
                seq = b"\x1b[D" * (-delta)

            try:
                os.write(fd, seq)
            except OSError:
                pass
        except Exception:
            pass

