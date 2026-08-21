"""PasteMixin — paste sanitization, confirmation, mc subshell detection."""
import os
import re
from gi.repository import Gtk, Gdk, GLib
from .config import _MC_SUBSHELL_DANGEROUS_CMDS


class PasteMixin:
    # ----------------------------------------------------------- Paste security
    @staticmethod
    def _sanitize_paste(text):
        """Sanitize pasted text: strip dangerous escape sequences, control
        chars, and invisible Unicode characters.

        Protects against:
          - Embedded ESC sequences that could manipulate terminal state
          - OSC sequences (title change, clipboard write via OSC 52, etc.)
          - C0/C1 control characters (except \\t and \\n which are legitimate)
          - Zero-width and invisible Unicode chars used in copy-paste attacks
          - Trailing newlines that would auto-execute commands
        """
        if not text:
            return text
        # 1. Remove ANSI escape sequences
        text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text)       # CSI
        text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)  # OSC
        text = re.sub(r'\x1b[()][A-Za-z0-9]', '', text)          # charset
        text = re.sub(r'\x1b[#=><NOM78]', '', text)               # misc ESC
        text = re.sub(r'\x1b.', '', text)                          # any remaining
        # 2. Remove C0 control characters except TAB, LF, CR
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        # 3. Remove C1 control characters (0x80-0x9F)
        text = re.sub(r'[\x80-\x9f]', '', text)
        # 4. Remove zero-width and invisible Unicode characters
        _invisible = (
            '\u200b\u200c\u200d\u200e\u200f'
            '\u2028\u2029\u202a\u202b\u202c\u202d\u202e'
            '\u2060\u2061\u2062\u2063\u2064'
            '\u2066\u2067\u2068\u2069'
            '\ufeff\ufff9\ufffa\ufffb'
        )
        text = re.sub(f'[{_invisible}]', '', text)
        # 5. Strip trailing newlines/carriage returns
        text = text.rstrip('\n\r')
        return text

    @staticmethod
    def _paste_looks_suspicious(text):
        """Heuristic check: does pasted text look like binary / garbage?"""
        length = len(text)
        if length == 0:
            return ""
        _normal = 0
        for ch in text:
            if ch.isalnum() or ch in ' \t\n\r_-./|&;:="\'`${}()[]<>@#%^*+,!?~\\':
                _normal += 1
        if _normal / length < 0.6:
            return "binary-like content"
        _lines = text.split('\n')
        _long_lines = sum(1 for ln in _lines if len(ln) > 500)
        if _long_lines >= 2:
            return "very long lines"
        if _long_lines == 1 and length > 2000:
            return "very long line"
        _non_print = sum(1 for ch in text if not ch.isprintable() and ch not in '\t\n\r')
        if length > 100 and _non_print / length > 0.05:
            return "non-printable characters"
        return ""

    def _is_foreground_shell(self):
        """Check if the PTY foreground process is an interactive shell or REPL.

        Returns True when paste confirmation should be shown:
          - Foreground process is a known shell (bash, zsh, fish, etc.)
          - Foreground process is a shell proxy (ssh, sudo, su, mosh, etc.)
          - Foreground process is an interactive REPL (psql, python, node, etc.)
          - mc subshell (Ctrl+O) — user is in shell prompt inside mc

        Returns False for TUI apps (mcedit, vim, htop — alt screen) and
        non-interactive programs (cat, grep, make, gcc).
        """
        # Alt screen = full-screen TUI app — never show multiline confirmation
        if self._is_alt_screen():
            return False
        # mc subshell (Ctrl+O): user is at a shell prompt even though
        # mc is still running in the background.  Treat as shell.
        if self._is_in_mc_subshell():
            return True
        # Check foreground process name
        pid = self._get_foreground_process_pid()
        if not pid:
            return True  # can't determine — be safe, show confirmation
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (OSError, IOError):
            return True  # can't read — be safe
        return comm in self._SHELL_NAMES or comm in self._SHELL_PROXY_NAMES or comm in self._REPL_NAMES

    def _paste_from_clipboard(self):
        """Paste text from system clipboard with security checks."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.read_text_async(None, self._on_clipboard_text_ready)

    def _on_clipboard_text_ready(self, clipboard, result):
        """Callback when clipboard text is available — apply paste security."""
        try:
            text = clipboard.read_text_finish(result)
        except Exception:
            return
        if not text or self._child_exited:
            return
        text = self._sanitize_paste(text)
        if not text:
            return
        data = text.encode("utf-8")
        is_shell = self._is_foreground_shell()
        char_count = len(text)

        # --- Determine if confirmation is needed ---
        # Confirmation is shown only for large pastes (>= threshold chars),
        # regardless of line count or shell/TUI context.
        confirm_msg = ""
        if char_count >= self._PASTE_CONFIRM_THRESHOLD:
            reason = self._paste_looks_suspicious(text) if is_shell else ""
            if reason:
                confirm_msg = (f"⚠  Suspicious paste ({char_count} chars, "
                               f"{reason})?  [Enter = paste, Esc = cancel]")
            else:
                confirm_msg = (f"⚠  Large paste ({char_count} chars)?  "
                               f"[Enter = paste, Esc = cancel]")


        if confirm_msg:
            self._paste_pending = True
            self._paste_data = data
            self._confirm_pending = True
            self._confirm_cmd = ""
            self._confirm_msg = confirm_msg
            self._confirm_bar.set_label(confirm_msg)
            self._sync_overlay_margins()  # ensure margin is up-to-date before showing
            self._confirm_bar.set_visible(True)
            self.terminal.grab_focus()  # keep focus on terminal for Esc
            return
        # No confirmation needed — paste immediately via VTE
        self._do_paste(data)

    def _do_paste(self, data):
        """Send paste data to the PTY.

        Wraps data in bracketed paste markers (\\e[200~ ... \\e[201~) ONLY when
        the foreground application has actually enabled bracketed paste mode
        (DECSET ?2004).  Wrapping unconditionally was wrong: while a command is
        producing output (readline / bracketed paste inactive) the markers are
        not understood by the receiving program — ESC[200 is swallowed and the
        trailing '~' leaks in, so pasted text showed up as '~text~'.  Real
        terminals gate the markers on ?2004 for exactly this reason.
        """
        pty = self.terminal.get_pty()
        if not pty:
            return
        fd = pty.get_fd()
        if fd < 0:
            return
        wrap = self.terminal.is_bracketed_paste()
        try:
            if wrap:
                # Bash/zsh/readline treat content between these markers as
                # pasted text — newlines are inserted literally, not executed.
                os.write(fd, b"\x1b[200~" + data + b"\x1b[201~")
            else:
                os.write(fd, data)
        except OSError:
            return
        # Scroll to the input cursor so the pasted text is visible even if the
        # user had scrolled up into the scrollback (both idle and while output
        # is streaming).
        try:
            self.terminal.scroll_to_bottom()
        except Exception:
            pass

    def _show_confirm(self, msg, action=None):
        """Raise the confirmation bar.

        `action` is a callable to run if the user accepts.  Without one the bar
        keeps its original meaning — it is guarding a command line the user has
        already typed, so Enter forwards Return to the PTY and Escape sends
        Ctrl+C to discard the line.
        """
        self._confirm_pending = True
        self._confirm_action = action
        self._confirm_msg = msg
        self._confirm_cmd = ""
        self._confirm_bar.set_label(msg)
        self._sync_overlay_margins()  # margin must be right before it shows
        self._confirm_bar.set_visible(True)
        self.terminal.grab_focus()    # keep the focus here so Esc lands

    def _clear_confirm(self):
        self._confirm_pending = False
        self._confirm_action = None
        self._confirm_msg = ""
        self._confirm_cmd = ""
        self._confirm_bar.set_visible(False)

    def _confirm_accept(self):
        """User confirmed paste/command — execute it."""
        if self._confirm_action is not None:
            action = self._confirm_action
            self._clear_confirm()
            action()
            return
        if self._paste_pending:
            data = self._paste_data
            self._paste_pending = False
            self._paste_data = b""
            self._confirm_pending = False
            self._confirm_msg = ""
            self._confirm_cmd = ""
            self._confirm_bar.set_visible(False)
            self._do_paste(data)
        else:
            # Command confirmation — send Enter to PTY
            self._confirm_pending = False
            self._confirm_msg = ""
            self._confirm_cmd = ""
            self._confirm_bar.set_visible(False)
            pty = self.terminal.get_pty()
            if pty:
                fd = pty.get_fd()
                if fd >= 0:
                    try:
                        os.write(fd, b"\r")
                    except OSError:
                        pass

    def _confirm_cancel(self):
        """User cancelled paste/command."""
        if self._confirm_action is not None:
            # A queued UI action, not a half-typed command line: there is
            # nothing at the prompt to discard, so no Ctrl+C down the PTY.
            self._paste_pending = False
            self._paste_data = b""
            self._clear_confirm()
            return
        was_paste = self._paste_pending
        self._paste_pending = False
        self._paste_data = b""
        self._confirm_pending = False
        self._confirm_msg = ""
        self._confirm_cmd = ""
        self._confirm_bar.set_visible(False)
        if not was_paste:
            # Send Ctrl+C to discard pending command line
            pty = self.terminal.get_pty()
            if pty:
                fd = pty.get_fd()
                if fd >= 0:
                    try:
                        os.write(fd, b"\x03")
                    except OSError:
                        pass

    # ------------------------------------------------- MC subshell detection
    def _is_mc_running(self):
        """Check if Midnight Commander is running as a child process."""
        if self._child_pid <= 0:
            return False
        try:
            children_path = f"/proc/{self._child_pid}/task/{self._child_pid}/children"
            if not os.path.exists(children_path):
                return False
            with open(children_path) as f:
                child_pids = f.read().split()
            to_check = [int(p) for p in child_pids]
            while to_check:
                cpid = to_check.pop(0)
                try:
                    comm_path = f"/proc/{cpid}/comm"
                    with open(comm_path) as f:
                        comm = f.read().strip()
                    if comm == "mc":
                        return True
                    # Check grandchildren
                    gc_path = f"/proc/{cpid}/task/{cpid}/children"
                    if os.path.exists(gc_path):
                        with open(gc_path) as f:
                            for gp in f.read().split():
                                to_check.append(int(gp))
                except (OSError, IOError, ValueError):
                    pass
        except (OSError, IOError):
            pass
        return False

    def _get_cursor_line_text(self):
        """Read text from VTE terminal at cursor position.
        VTE doesn't expose buffer directly, so we use get_text_range_format."""
        try:
            col, row = self.terminal.get_cursor_position()
            ncols = self.terminal.get_column_count()
            result = self.terminal.get_text_range_format(
                None, row, 0, row, ncols - 1)
            if result is None:
                return ""
            # VTE returns (text, length) tuple or just a string depending on version
            if isinstance(result, tuple):
                text = result[0] if result[0] else ""
            elif isinstance(result, str):
                text = result
            else:
                text = str(result) if result else ""
            return text.rstrip("\n")
        except Exception as e:
            return ""

    def _extract_command_from_line(self, line_text):
        """Extract command from terminal line (handles prompts, reverse-i-search)."""
        m = re.search(r"\((?:failed )?reverse-i-search\)[`'][^'`]*[`']:\s*(.*)", line_text)
        if m:
            return m.group(1).strip()
        for sep in ["$ ", "# ", "> "]:
            idx = line_text.rfind(sep)
            if idx >= 0:
                return line_text[idx + len(sep):].strip()
        return line_text.strip()

    def _get_base_command(self, cmd):
        """Strip sudo, env, and variable prefixes to get the actual command name."""
        parts = cmd.split()
        i = 0
        while i < len(parts):
            token = parts[i]
            if token == "sudo":
                i += 1
                while i < len(parts) and parts[i].startswith("-"):
                    flag = parts[i]
                    i += 1
                    if flag in ("-u", "-g", "-C", "-D", "-p", "-r", "-t") and i < len(parts):
                        i += 1
                continue
            if token == "env":
                i += 1
                while i < len(parts) and parts[i].startswith("-"):
                    i += 1
                continue
            if "=" in token and not token.startswith("="):
                i += 1
                continue
            return token
        return ""

    def _is_in_mc_subshell(self):
        """Check if we're in mc's Ctrl+O subshell (mc running but shell is foreground).

        When mc is in full-screen mode, mc itself is the foreground process.
        When user presses Ctrl+O, the shell becomes the foreground process
        while mc is still running in the background.

        Strategy: mc is running AND the mc process has a child shell process
        (the subshell).  When in Ctrl+O, that subshell is the foreground
        process group leader.  We walk mc's children looking for a shell.
        If TIOCGPGRP points to mc itself, we fall back to checking whether
        mc has a child shell — if it does, the user is in the subshell
        (mc panels would be fullscreen otherwise and Enter wouldn't reach us
        through the normal key handler for command interception).
        """
        if not self._is_mc_running():
            return False
        # Simple heuristic: if mc is running and we got here (key press
        # intercepted on Enter with a command line), we must be in the
        # subshell.  When mc is in fullscreen panel mode, the terminal
        # shows mc's TUI — the user can't type shell commands, so
        # _check_dangerous_command wouldn't find a base command.
        # Therefore: mc_running + base command detected = subshell.
        return True

    def _is_child_process_running(self, name):
        """Check if a process with given name is running as a child of the shell."""
        if self._child_pid <= 0:
            return False
        try:
            children_path = f"/proc/{self._child_pid}/task/{self._child_pid}/children"
            if not os.path.exists(children_path):
                return False
            with open(children_path) as f:
                child_pids = f.read().split()
            to_check = [int(p) for p in child_pids]
            while to_check:
                cpid = to_check.pop(0)
                try:
                    with open(f"/proc/{cpid}/comm") as f:
                        comm = f.read().strip()
                    if comm == name:
                        return True
                    gc_path = f"/proc/{cpid}/task/{cpid}/children"
                    if os.path.exists(gc_path):
                        with open(gc_path) as f:
                            for gp in f.read().split():
                                to_check.append(int(gp))
                except (OSError, IOError, ValueError):
                    pass
        except (OSError, IOError):
            pass
        return False

    def _check_dangerous_command(self):
        """Check if the command being entered is dangerous.

        Detects:
          - Launching mc when mc is already running (in Ctrl+O subshell)
          - Launching ssh when mc is open (may break Ctrl+O return)
          - Launching TUI programs in mc subshell
          - Launching mc when mc is already running (from normal shell)
          - Launching ssh when ssh session is already active
        Returns confirmation message or empty string.
        """
        line_text = self._get_cursor_line_text()
        cmd = self._extract_command_from_line(line_text)
        base = self._get_base_command(cmd)
        if not base:
            return ""

        # --- MC subshell context (Ctrl+O) ---
        in_mc_subshell = self._is_in_mc_subshell()
        if in_mc_subshell:
            if base == "ssh":
                return f"⚠  mc is open — confirm SSH: {cmd}  [Enter = run, Esc = cancel]"
            if base == "mc":
                return f"⚠  mc is already running — open another mc?  [Enter = yes, Esc = cancel]"
            if base in _MC_SUBSHELL_DANGEROUS_CMDS:
                return f"⚠  mc subshell — {base} may break Ctrl+O return  [Enter = run, Esc = cancel]"

        # --- Normal shell: detect duplicate mc/ssh launches ---
        if base == "mc" and not in_mc_subshell and self._is_mc_running():
            return "⚠  mc is already running — open another mc?  [Enter = yes, Esc = cancel]"
        if base == "ssh" and self._is_child_process_running("ssh"):
            return f"⚠  SSH session already open — confirm: {cmd}  [Enter = run, Esc = cancel]"

        return ""

    def _check_dangerous_picker(self):
        """Check whether raising the session picker (Ctrl+E) needs confirming.

        The picker types its choice into whatever holds the foreground, so the
        situations that make it a bad idea are the ones _check_dangerous_
        command() already guards against.  The difference is that there is no
        command line to read here — only the process that would receive it.

        Note this does NOT use _is_in_mc_subshell(): that helper infers the
        subshell from its caller ("we were reached from Enter on a typed
        command line, so mc cannot be showing its panels"), and Ctrl+E can be
        pressed with mc fullscreen, where the premise is false.

        Returns the confirmation message, or "" when there is nothing to warn
        about — including a tab with no child yet, where the picker is the
        only thing there is.
        """
        if self._child_pid <= 0:
            return ""
        if self._is_mc_running():
            return ("⚠  mc is open — open the session picker?  "
                    "[Enter = yes, Esc = cancel]")
        if self._is_child_process_running("ssh"):
            return ("⚠  SSH session already open — open the session picker?  "
                    "[Enter = yes, Esc = cancel]")
        return ""

