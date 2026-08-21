"""TerminalTab — a single terminal tab (core: lifecycle, layout, title)."""
import os
import re
import time
import shlex
import signal
from gi.repository import Gtk, Gdk, GLib, Gio, Pango
from . import config
from . import connections
from .config import _hex_to_rgba
from .core_terminal import CashTerminal
from .search import SearchMixin
from .paste import PasteMixin
from .interaction import InteractionMixin
from .session_picker import SessionPicker


class TerminalTab(Gtk.Box, SearchMixin, PasteMixin, InteractionMixin):
    """A single terminal tab containing a CashTerminal widget."""

    # Title shown while a tab is still asking which session to start.  A tab
    # with no child has nothing to read from /proc, so without an explicit
    # title it would keep whatever placeholder it was created with.
    PICKER_TITLE = "Выбор сессии"

    # Paste confirmation threshold — confirmation is shown only when the
    # pasted text is this many characters or more (regardless of line count
    # or shell/TUI context).  Smaller pastes go through directly.
    _PASTE_CONFIRM_THRESHOLD = 10000

    # Shell process names — foreground process with one of these comm values
    # is considered an interactive shell prompt (multiline paste confirmation
    # is shown).  Any other foreground process (mcedit, vim, htop, etc.)
    # gets paste data directly without multiline confirmation.
    _SHELL_NAMES = frozenset({"bash", "zsh", "fish", "sh", "dash", "ksh", "csh", "tcsh"})

    # Processes that proxy to a remote/nested shell — we can't inspect the
    # remote side (ssh, mosh) or the child shell may share the same process
    # group as the proxy (sudo, su).  Treat as shell context for paste safety.
    _SHELL_PROXY_NAMES = frozenset({"ssh", "mosh", "telnet", "rsh", "sudo", "su", "pkexec", "doas"})

    # Interactive REPL programs where multiline paste is dangerous
    # (e.g. accidental SQL execution).
    _REPL_NAMES = frozenset({"psql", "mysql", "sqlite3", "mongosh", "redis-cli",
                             "python", "python3", "ipython", "node", "irb", "lua"})

    def __init__(self, start_dir=None, show_picker=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._child_exited = False
        self._child_pid = -1
        self._tab_label = None
        self._title_timer_id = 0
        self._last_tab_title = ""

        # Session picker overlay, or None.  On a brand-new tab it means no
        # shell has been spawned yet; raised by Ctrl+E it is simply floating
        # over a working terminal.  self._child_pid tells the two apart.
        self._picker = None
        self._start_dir = start_dir
        # Last working directory seen in this tab, refreshed by the title poll.
        # A new tab opened with startup.follow_cwd copies it, so it has to
        # survive the moment the child is already gone (a tab that is closing
        # is still the visible one while the new tab is being built).
        self._last_cwd = None
        # Last SSH command line recorded into the history, as (pid, argv).
        # The title poll runs every 700 ms and would otherwise record the same
        # live connection over and over.
        self._last_ssh_key = None

        # Paste/confirm state
        self._confirm_pending = False
        self._confirm_msg = ""
        self._confirm_cmd = ""
        self._paste_pending = False
        self._paste_data = b""
        # Callable to run if the confirmation bar is accepted.  None = the
        # classic behaviour, where the bar is guarding a command line the user
        # typed and Enter/Esc just forward Return/Ctrl+C to the PTY.
        self._confirm_action = None

        # MC subshell tracking
        self._alt_screen_seen = False
        self._mc_subshell = False

        # Search state
        self._search_active = False
        self._search_bar = None
        self._search_entry = None
        self._search_status_label = None
        # Matching lives in the Rust core (alacritty RegexSearch); there is no
        # Python-side match list or cache any more.  Highlights are re-derived
        # from the live grid on every frame by core_terminal._draw.
        self._search_current_match = None     # (abs_row, col) of the focused match
        self._search_anchor_row = None        # viewport-bottom row a typed query searches up from
        self._search_view_row = None          # scroll row our own last jump left behind
        self._search_installed_query = None   # query currently installed in the core
        self._search_debounce_id = None       # GLib.timeout for auto-search

        # Reset detection state.  When the terminal receives RIS (ESC c)
        # via the `reset` command, VTE clears the screen visually but
        # get_text_range_format still returns stale pre-reset content.
        # We detect this and recreate the VTE widget to get a clean buffer.
        self._prev_vadj_upper = None          # track upper to detect reset
        self._prev_cursor_row = 0             # track cursor row to detect reset
        self._recreating = False              # guard against re-entrant recreate

        # Cursor hide on Enter: suppress the brief cursor flash at col 0
        # between pressing Enter and the shell drawing the new prompt.
        self._cursor_hidden_for_enter = False
        self._cursor_restore_timer = None

        # Create the terminal widget — Rust core (PTY + alacritty grid) with a
        # Cairo renderer, a drop-in replacement for the old Vte.Terminal subclass.
        self.terminal = CashTerminal()
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)

        # Font
        font_desc = Pango.FontDescription(f"{config.FONT_FAMILY} {config.FONT_SIZE_PT}")
        self.terminal.set_font(font_desc)

        # Scrollback / scroll behavior.  These are no-ops on the core widget
        # (it owns its own scrollback + scroll policy) but are kept for parity
        # with the previous VTE configuration.
        self.terminal.set_scrollback_lines(config.SCROLLBACK_LINES)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_bold_is_bright(True)
        self.terminal.set_mouse_autohide(True)

        # Colors
        self._apply_colors()
        self.terminal.connect("realize", lambda w: self._apply_colors())

        # Signals
        self.terminal.connect("child-exited", self._on_child_exited)
        self.terminal.connect("window-title-changed", self._on_title_changed)
        self.terminal.connect("cursor-moved", self._on_vte_cursor_moved)

        # NOTE: "contents-changed" is deliberately NOT connected.  Search used
        # to listen to it to rebuild its match list and to detect streaming
        # output; both are gone now (the core matches, and the highlights are
        # re-derived while drawing), so the handler was pure per-chunk overhead
        # on the hottest path there is.

        # Right-click context menu (Copy / Paste / Select All / Clear /
        # New Tab / Close Tab / Search / Settings).  CashTerminal emits
        # "context-menu" on a non-app-mouse right click.
        self._build_context_menu()
        self.terminal.connect("context-menu", self._on_context_menu)

        # NOTE: the core widget has no native clipboard paste and no OSC-52
        # emission, so the old "paste-clipboard"/"commit" blocking is gone —
        # paste only flows through our secure Shift+Insert / Ctrl+Shift+V path.
        # The alacritty grid is always truthful after a reset (RIS / clear), so
        # the VTE buffer-recreation dance is no longer needed either.

        # Mouse click → move readline cursor.
        # In normal shell mode (not alt-screen), a left-click should move
        # the readline input cursor to the clicked column.  VTE doesn't do
        # this automatically because readline doesn't enable mouse tracking.
        # We intercept the click, compute the delta between the current
        # cursor column and the clicked column, and send the appropriate
        # number of Left/Right arrow escape sequences to readline.
        _click_ctrl = Gtk.GestureClick()
        _click_ctrl.set_button(1)  # left button only
        # BUBBLE phase: VTE processes the click first (selection, focus),
        # then we move the readline cursor.  We never claim the sequence
        # so VTE's own gesture handling is not disrupted.
        _click_ctrl.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        _click_ctrl.connect("pressed", self._on_terminal_click)
        self.terminal.add_controller(_click_ctrl)
        self._click_ctrl = _click_ctrl

        # Build the layout: overlay for search/confirm bars on top of terminal
        self._overlay = Gtk.Overlay()
        overlay = self._overlay
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)

        # Lay out the terminal beside a manual vertical scrollbar.  The core
        # widget renders its own scrollback (driven by display_offset), so it
        # must NOT be wrapped in a ScrolledWindow viewport — that would clip it
        # and double-handle scrolling.  We bind a plain Gtk.Scrollbar to the
        # widget's absolute-row adjustment instead.
        term_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._term_box = term_box
        term_box.set_hexpand(True)
        term_box.set_vexpand(True)
        term_box.append(self.terminal)
        self._vscrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=self.terminal.get_vadjustment())
        term_box.append(self._vscrollbar)
        overlay.set_child(term_box)

        # Confirmation bar (hidden by default) — overlay at bottom,
        # stops before the scrollbar so it doesn't cover or shift it.
        self._confirm_bar = Gtk.Label()
        self._confirm_bar.add_css_class("confirm-bar")
        self._confirm_bar.set_halign(Gtk.Align.FILL)
        self._confirm_bar.set_valign(Gtk.Align.END)
        self._confirm_bar.set_focusable(False)  # never steal focus from terminal
        self._confirm_bar.set_visible(False)
        overlay.add_overlay(self._confirm_bar)

        # Search bar (hidden by default) — overlay on top of terminal
        self._build_search_bar()
        overlay.add_overlay(self._search_box)

        # Search highlights are rendered directly by CashTerminal in its own
        # Cairo draw pass — it queries the core (search_visible) while painting,
        # so there is no separate overlay and no match list to keep in sync.

        # Dynamically measure the vertical scrollbar width and set
        # margin-end on overlays so they stop before the scrollbar.
        # We use GLib.idle_add so the read happens after GTK finishes layout.
        def _schedule_margin_sync(*_a):
            GLib.idle_add(self._sync_overlay_margins)
        self._vscrollbar.connect("realize", _schedule_margin_sync)
        self._vscrollbar.connect("notify::allocation", _schedule_margin_sync)

        # Redraw search highlights on scroll and new output.
        # We connect to both vadjustment value-changed (fires when scroll
        # position changes) and to the terminal's own draw signal in AFTER
        # phase so the highlight overlay is queued for redraw in the same
        # paint cycle as VTE — eliminating the one-frame lag that causes
        # visible jitter during smooth scrolling.
        vadj = self.terminal.get_vadjustment()
        if vadj:
            vadj.connect("value-changed", self._on_search_scroll)
        # NOTE: search does not listen to "contents-changed" at all — see the
        # comment next to the other terminal signal connections above.

        # Wrap the inner overlay in an outer overlay so that the
        # cursor-cover DrawingArea is rendered ABOVE VTE.
        # In GTK4, Overlay.set_child() draws the child on top of
        # add_overlay() widgets.  By using a second overlay level,
        # the cursor cover (added via add_overlay on the outer one)
        # paints after the inner overlay (which contains VTE).
        self._outer_overlay = Gtk.Overlay()
        self._outer_overlay.set_hexpand(True)
        self._outer_overlay.set_vexpand(True)
        self._outer_overlay.set_child(overlay)

        # Cursor-cover overlay: small bg-colored rectangle shown briefly
        # after Enter to hide the cursor flash at col 0.
        self._build_cursor_cover()

        self.append(self._outer_overlay)

        if show_picker:
            # Nothing is spawned yet — the picker decides what runs.  It is an
            # overlay on the outer level so it paints above the terminal.
            self._show_picker()
        else:
            self._spawn_shell(start_dir, config.NEW_TAB_COMMAND or None)

    # ------------------------------------------------------- Session picker
    def _show_picker(self):
        """Put the session picker over the terminal."""
        if self._picker is not None:
            return
        # With a shell running the picker is just an overlay over a working
        # tab, so Escape only dismisses it; on a tab that has nothing running
        # there is nothing to dismiss back to, and Escape drops the tab.
        #
        # That same shell is also why "Локальная оболочка" is dropped from the
        # list here: choosing it would type nothing into the local shell this
        # tab is already sitting at.  On a tab with no child it is the whole
        # point of the row, so there it stays.
        live = self._child_pid > 0
        self._picker = SessionPicker(
            self._on_picker_choice,
            on_edit=self._on_picker_edit,
            on_cancel=self._on_picker_cancel,
            cancel_hint="закрыть меню" if live else "закрыть вкладку",
            include_local=not live)
        self._outer_overlay.add_overlay(self._picker)
        GLib.idle_add(self._picker.grab_picker_focus)

    def _hide_picker(self):
        """Take the picker down and hand the keyboard back to the terminal."""
        picker, self._picker = self._picker, None
        if picker is not None:
            self._outer_overlay.remove_overlay(picker)
        self.terminal.grab_focus()

    def toggle_session_picker(self):
        """Ctrl+E — raise the session picker, or take it back down.

        Toggling it closed is only offered once a shell is running: on a tab
        that is still waiting to be told what to start, dismissing the picker
        would leave neither something to look at nor something to type into.
        """
        if self._picker is not None:
            if self._child_pid > 0:
                self._hide_picker()
            return
        # Opening the picker over a busy terminal is as risky as launching a
        # second mc/ssh from the prompt, and warns the same way: whatever is
        # chosen gets typed into that running program.
        msg = self._check_dangerous_picker()
        if msg:
            self._show_confirm(msg, self._show_picker)
            return
        self._show_picker()

    def _on_picker_edit(self):
        """"Настроить список…" chosen — open the editor, keep the picker up."""
        self._ctx_app_call("open_connections")

    def _on_picker_cancel(self):
        """Escape in the picker.

        With a shell running this is a plain dismiss.  Without one the tab was
        opened by mistake and has no process to warn about, so it goes down
        the same path as Ctrl+W — which means Escape on the last remaining tab
        quits the app.
        """
        if self._child_pid > 0:
            self._hide_picker()
        else:
            self._ctx_app_call("close_tab", self)
        return GLib.SOURCE_REMOVE

    def reload_picker(self):
        """Refresh the picker list (called after the editor is saved)."""
        if self._picker is not None:
            self._picker.reload()

    def _on_picker_choice(self, command):
        """Picker callback: run what was chosen and drop the picker."""
        if self._picker is None:
            return GLib.SOURCE_REMOVE  # tab closed before the idle ran
        # Decide before hiding: _hide_picker() grabs the terminal focus, and
        # the branch below depends only on whether this tab already has a
        # child, which nothing here changes.
        live = self._child_pid > 0
        self._hide_picker()
        if live:
            self._run_in_shell(command)
        else:
            self._spawn_shell(self._start_dir, command)
            # The title poll only fires 700 ms from now; /proc is readable at
            # once, so replace PICKER_TITLE immediately instead of leaving it.
            GLib.idle_add(self.sync_window_title)
        return GLib.SOURCE_REMOVE

    def _run_in_shell(self, command):
        """Type `command` into the shell already running in this tab.

        Deliberately not a spawn: the connection becomes a child of the shell,
        so quitting ssh drops back to that shell instead of ending the tab.
        It is also exactly what the user would have typed, which means the
        shell's own history, aliases and job control all apply.

        `command` None means the local shell, and this tab is already running
        one — there is nothing to do.

        No check that a shell is what's actually in the foreground: "run it as
        if typed" is the whole point, and typing into mc or vim is the user's
        call, the same as it would be from the keyboard.
        """
        if not command:
            return
        self._discard_input_line()
        # \r, not \n — that is the byte the terminal sends for Enter, and the
        # program on the other end is reading keystrokes, not lines.
        self.terminal.feed_child(command + "\r")

    def _discard_input_line(self):
        """Throw away whatever is half-typed at the prompt.

        Without this the chosen command is appended to the text already on the
        line and the two run as one — "cat fi" + "ssh host" executes
        "cat fissh host".  The user asked for this session, not for whatever
        that concatenation happens to mean.

        Ctrl+E then Ctrl+U, in that order: bash binds Ctrl+U to
        unix-line-discard, which kills *backwards* from the cursor, so the
        cursor has to be moved to the end of the line first or anything to the
        right of it would survive.  (zsh's kill-whole-line and fish's
        backward-kill-line are both fine with the same sequence.)

        Only sent to something that is actually reading a command line.  Into a
        full-screen program those two bytes are ordinary keystrokes and could
        mean anything at all, so an unrecognised foreground process is left
        alone — a stray command appended to a line is a much smaller problem
        than Ctrl+U delivered to an editor.
        """
        if not self._is_command_line_prompt():
            return
        self.terminal.feed_child("\x05\x15")

    def _is_command_line_prompt(self):
        """True when the foreground process is reading a command line.

        Deliberately narrower than paste.py's _is_foreground_shell(), which
        answers "should a paste be confirmed?" and therefore says yes whenever
        it cannot tell.  Here an unknown process must mean no: this decides
        whether to *send* control characters, so uncertainty has to fall on the
        side of sending nothing.
        """
        if self._child_pid <= 0:
            return False
        if self._is_alt_screen():
            return False
        pid = self._get_foreground_process_pid()
        if not pid:
            return False
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (OSError, IOError):
            return False
        # Shells, plus the proxies that stand in front of a remote shell (ssh,
        # sudo…) — the far side of those is reading a command line too.
        return comm in self._SHELL_NAMES or comm in self._SHELL_PROXY_NAMES

    # ------------------------------------------------- Overlay margin sync
    def _sync_overlay_margins(self, *_args):
        """Set margin-end on overlay widgets to match the actual scrollbar width.

        Called (via GLib.idle_add) after the vertical scrollbar is realized
        or re-allocated, so the confirm bar and search bar never overlap
        the scrollbar regardless of GTK theme.
        """
        vscrollbar = getattr(self, "_vscrollbar", None)
        if not vscrollbar:
            return
        sb_width = vscrollbar.get_width()  # allocated pixel width
        if sb_width <= 0:
            # Widget not yet laid out — ask GTK for its preferred width so
            # we still get a non-zero margin on the very first show.
            # The scrollbar is always visible (PolicyType.ALWAYS) even when
            # there is no scrollback content (inactive/greyed out), so its
            # preferred width is the correct value to use here.
            min_size, _ = vscrollbar.get_preferred_size()
            sb_width = min_size.width if min_size else 0
        if sb_width <= 0:
            return  # truly unknown yet — keep previous margin
        # Avoid re-layout loop: only update if width actually changed
        if getattr(self, '_last_sb_width', -1) == sb_width:
            return
        self._last_sb_width = sb_width
        self._confirm_bar.set_margin_end(sb_width)
        self._search_box.set_margin_end(sb_width)

    # ----------------------------------------------------------- Environment
    def _build_env(self):
        """Build clean environment for child shell."""
        env = os.environ.copy()

        # Clean AppImage/Qt/IDE variables
        for var in config._APPIMAGE_CLEAN_VARS:
            env.pop(var, None)
        # Remove tainted prefixes
        for var in list(env):
            if var.startswith(config._APPIMAGE_TAINT_PREFIXES):
                env.pop(var, None)
            elif "/tmp/.mount_" in env.get(var, ""):
                env.pop(var, None)
        # Clean PATH
        _path = env.get("PATH", "")
        _clean_parts = [p for p in _path.split(":") if "/tmp/.mount_" not in p]
        env["PATH"] = ":".join(_clean_parts)

        # Apply config env vars
        for k, v in config.CHILD_ENV.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        # Ensure TERM is set
        if "TERM" not in env and "TERM" not in config.CHILD_ENV:
            env["TERM"] = "xterm-256color"
        if "COLORTERM" not in env and "COLORTERM" not in config.CHILD_ENV:
            env["COLORTERM"] = "truecolor"

        # Apply readline inputrc overrides
        if config.INPUTRC_OVERRIDES is not False and config.INPUTRC_OVERRIDES:
            _ct_dir = os.path.join(os.path.expanduser("~"), ".cash-terminal")
            _ct_inputrc = os.path.join(_ct_dir, "inputrc")
            try:
                os.makedirs(_ct_dir, exist_ok=True)
                _user_inputrc = os.path.join(os.path.expanduser("~"), ".inputrc")
                _lines = []
                if os.path.isfile("/etc/inputrc"):
                    _lines.append("$include /etc/inputrc")
                if os.path.isfile(_user_inputrc):
                    _lines.append(f"$include {_user_inputrc}")
                for _rc_key, _rc_val in config.INPUTRC_OVERRIDES.items():
                    _lines.append(f"set {_rc_key} {_rc_val}")
                _lines.append("")
                with open(_ct_inputrc, "w") as _f:
                    _f.write("\n".join(_lines))
                env["INPUTRC"] = _ct_inputrc
            except OSError:
                pass

        return [f"{k}={v}" for k, v in env.items()]

    def _spawn_shell(self, start_dir=None, command=None):
        """Spawn the tab's child process in the core terminal widget.

        `command` is a full command line ("ssh -p 2222 user@host") coming from
        the session picker or from `new_tab.command`.  It is split with shlex
        and exec'd directly — no shell in between — so the connection is the
        tab's own child and closing it closes the tab.  None = login shell.
        """
        work_dir = start_dir or config.STARTUP_DIRECTORY
        env = self._build_env()

        argv = []
        if command:
            try:
                argv = shlex.split(command)
            except ValueError:  # unbalanced quotes in the config/favourite
                argv = []
        if argv:
            program, args = argv[0], argv[1:]
        else:
            # Spawn as login shell: argv = [shell, "-l"] so ~/.bash_profile /
            # ~/.profile are sourced — same as Konsole.
            program, args = os.environ.get("SHELL", "/bin/bash"), ["-l"]

        # The Rust core spawns the PTY synchronously and returns the child PID.
        try:
            pid = self.terminal.spawn(program, args, work_dir, env)
        except Exception as exc:
            print(f"Core spawn error: {exc}")
            return
        if pid is None or pid < 0:
            print("Core spawn error: invalid PID")
            return
        self._child_pid = pid
        # Start periodic title refresh (like terminal.py's 700ms timer)
        self._title_timer_id = GLib.timeout_add(700, self._refresh_tab_title)

    def _on_child_exited(self, terminal, status):
        """Shell exited — notify app to close tab."""
        self._child_exited = True
        app = self.get_root()
        if app and hasattr(app, 'get_application'):
            application = app.get_application()
            if application and hasattr(application, 'close_tab'):
                GLib.idle_add(application.close_tab, self)

    def _on_title_changed(self, terminal):
        """VTE title changed — trigger a title refresh."""
        # We use /proc-based title building (like terminal.py) instead of
        # relying on VTE's window-title-changed, but we still use this
        # signal as a trigger to refresh immediately.
        self._refresh_tab_title()

    def _get_foreground_process_pid(self):
        """Get the foreground process PID of this terminal's PTY."""
        if self._child_pid <= 0:
            return None
        try:
            pty = self.terminal.get_pty()
            if pty:
                fd = pty.get_fd()
                if fd >= 0:
                    import fcntl, termios, struct
                    # TIOCGPGRP returns the foreground process group ID
                    buf = fcntl.ioctl(fd, termios.TIOCGPGRP,
                                      struct.pack('i', 0))
                    pgid = struct.unpack('i', buf)[0]
                    if pgid > 0:
                        return pgid
        except Exception:
            pass
        return self._child_pid

    def get_cwd(self):
        """The tab's current local working directory, or None.

        Read from /proc rather than tracked by parsing what the user typed:
        `cd` is only one of the ways a shell changes directory (pushd, a
        subshell, a script that cds and execs another shell), and the kernel
        knows the answer to all of them.

        The foreground process is asked first because that is what the user is
        looking at — `cd /tmp && vim` leaves vim in /tmp, and so should a new
        tab.  Over an ssh connection the foreground process is the local ssh
        client, so what comes back is the local directory it was started from,
        which is the only thing a local shell could use anyway.
        """
        for pid in (self._get_foreground_process_pid(), self._child_pid):
            if not pid or pid <= 0:
                continue
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
            # A deleted directory readlinks as "/some/path (deleted)", and
            # handing that to chdir() would fail the spawn.
            if cwd and os.path.isdir(cwd):
                self._last_cwd = cwd
                return cwd
        # The child may already be gone (a closing tab); the last directory it
        # was seen in is still a better answer than none.
        if self._last_cwd and os.path.isdir(self._last_cwd):
            return self._last_cwd
        return None

    def _format_ssh_target(self, argv):
        """Format SSH target from argv for tab title."""
        target = ""
        login_user = ""
        expect_login_user = False
        skip_next = False
        opts_with_value = {"-b", "-c", "-D", "-E", "-F", "-I", "-i", "-J",
                           "-L", "-m", "-O", "-o", "-p", "-Q", "-R", "-S",
                           "-W", "-w"}
        for arg in argv[1:]:
            if expect_login_user:
                expect_login_user = False
                login_user = arg
                continue
            if skip_next:
                skip_next = False
                continue
            if not arg or arg == "--":
                continue
            if arg == "-l":
                expect_login_user = True
                continue
            if arg in opts_with_value:
                skip_next = True
                continue
            if arg.startswith("-l") and len(arg) > 2:
                login_user = arg[2:]
                continue
            if arg.startswith("-"):
                continue
            target = arg
            break
        if not target:
            return "ssh"
        if "@" in target:
            user, host = target.split("@", 1)
            return f"({user}) {host}"
        if login_user:
            return f"({login_user}) {target}"
        return target

    def _build_tab_title(self):
        """Build tab title from /proc: 'dirname : program'."""
        if self._child_pid <= 0 and self._last_tab_title:
            return self._last_tab_title
        pid = self._get_foreground_process_pid()
        if not pid:
            return "shell : shell"
        comm = ""
        argv = []
        cwd = ""
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (OSError, IOError):
            pass
        if not comm:
            comm = "shell"
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [p.decode("utf-8", errors="replace")
                        for p in f.read().split(b"\0") if p]
        except (OSError, IOError):
            pass
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        if cwd:
            self._last_cwd = cwd
        dirname = os.path.basename(cwd.rstrip("/")) if cwd else ""
        if cwd == "/":
            dirname = "/"
        if not dirname:
            dirname = "shell"
        if comm == "ssh":
            program = self._format_ssh_target(argv)
            dirname = "ssh"
            self._note_ssh_session(pid, argv, program)
        else:
            program = os.path.basename(argv[0]) if argv else comm
            if comm.startswith(("python", "perl", "ruby")) and argv:
                script_name = os.path.basename(argv[0])
                if script_name == comm or script_name.startswith(
                        ("python", "perl", "ruby")):
                    if len(argv) > 1 and not argv[1].startswith("-"):
                        program = os.path.basename(argv[1])
                else:
                    program = script_name
        return f"{dirname} : {program}"

    def _note_ssh_session(self, pid, argv, target):
        """Record an SSH connection in the history, once per connection.

        The title poll sees the same live ssh process every 700 ms, so the
        (pid, argv) pair is remembered and a repeat is ignored.  A genuinely
        new connection always has a different pid (or at least a different
        command line), and re-connecting later legitimately bumps the counter.
        """
        if not argv:
            return
        key = (pid, tuple(argv))
        if key == self._last_ssh_key:
            return
        self._last_ssh_key = key
        try:
            connections.record(argv, target)
        except Exception:
            pass  # history is a convenience, never worth breaking a tab over

    def _refresh_tab_title(self):
        """Refresh tab title from /proc data (timer callback)."""
        if self._tab_label is None:
            return GLib.SOURCE_CONTINUE
        if self._child_pid <= 0:
            return GLib.SOURCE_CONTINUE
        # Skip background tabs
        app = self.get_root().get_application() if self.get_root() else None
        if app and app._get_visible_terminal() is not self:
            return GLib.SOURCE_CONTINUE
        title = self._build_tab_title()
        if title != self._last_tab_title:
            self._last_tab_title = title
            self._tab_label.set_label(title)
            if app and app._win:
                full = f"Cash Terminal \u2014 {title}"
                app._win.set_title(full)
        return GLib.SOURCE_CONTINUE

    def sync_window_title(self):
        """Force this tab's title onto the tab label and the window title.

        `_refresh_tab_title` only touches the window title when the computed
        title *changed* since the last tick, so after switching tabs
        (Ctrl+Tab, Ctrl+Left/Right, tab list) the window kept showing the
        previously active tab's title until this tab's title happened to
        change.  This variant always applies it and is called on tab switch.

        Returns False so it can be used directly as a GLib.idle_add callback.
        """
        if self._child_pid <= 0:
            # No child yet — a tab still showing the session picker.  There is
            # nothing to read from /proc, but we must NOT bail out: otherwise
            # the window keeps advertising the previously active tab's title.
            title = self._last_tab_title or self.PICKER_TITLE
        else:
            try:
                title = self._build_tab_title()
            except Exception:
                return False
        self._last_tab_title = title
        if self._tab_label is not None:
            self._tab_label.set_label(title)
        root = self.get_root()
        app = root.get_application() if root else None
        if app is not None and getattr(app, "_win", None):
            app._win.set_title(f"Cash Terminal \u2014 {title}")
        return False

    def _apply_colors(self):
        """Apply fg/bg/palette/cursor colors and transparency to the terminal."""
        fg_rgba = _hex_to_rgba(config.FG_COLOR)
        bg_rgba = _hex_to_rgba(config.BG_COLOR, alpha=config.BG_ALPHA)
        palette = [_hex_to_rgba(h) for h in config.PALETTE_HEX]
        palette = [c for c in palette if c is not None]

        if fg_rgba and bg_rgba and len(palette) == 16:
            self.terminal.set_colors(fg_rgba, bg_rgba, palette)

        if fg_rgba:
            self.terminal.set_color_foreground(fg_rgba)
        if bg_rgba:
            self.terminal.set_color_background(bg_rgba)

        cursor_fg_rgba = _hex_to_rgba(config.CURSOR_FG)
        cursor_bg_rgba = _hex_to_rgba(config.CURSOR_BG)
        if cursor_bg_rgba:
            self.terminal.set_color_cursor(cursor_bg_rgba)
        if cursor_fg_rgba:
            self.terminal.set_color_cursor_foreground(cursor_fg_rgba)

        if config.BG_ALPHA < 1.0:
            self.terminal.set_clear_background(False)

    def apply_font(self):
        """Re-apply the font from config to the terminal (live preview)."""
        font_desc = Pango.FontDescription(
            f"{config.FONT_FAMILY} {config.FONT_SIZE_PT}")
        self.terminal.set_font(font_desc)

    # ------------------------------------------------- Right-click menu
    def _build_context_menu(self):
        """Create the plain Gtk.Popover used on right-click.

        A Gtk.PopoverMenu (model-based) trips GTK's "Broken accounting of
        active state" warning when popped up from a click gesture, and its
        item icons depend on the icon theme.  A plain Gtk.Popover holding flat
        Gtk.Button rows avoids both problems."""
        self._ctx_copy_btn = None
        # Pending id of the deferred popup(), 0 when none is queued.  See
        # _on_context_menu() for why more than one must never be in flight.
        self._ctx_popup_id = 0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("context-menu")

        def _row(label, callback):
            btn = Gtk.Button()
            btn.set_has_frame(False)
            btn.add_css_class("flat")
            btn.add_css_class("context-menu-item")
            lbl = Gtk.Label(label=label, xalign=0.0)
            lbl.set_hexpand(True)
            btn.set_child(lbl)
            btn.connect("clicked", self._on_ctx_item, callback)
            box.append(btn)
            return btn

        def _sep():
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._ctx_copy_btn = _row("Копировать",
                                  lambda: self.terminal.copy_clipboard_format(None))
        _row("Вставить", lambda: self._paste_from_clipboard())
        _row("Выделить всё", lambda: self.terminal.select_all())
        _sep()
        _row("Очистить", lambda: self.terminal.clear_screen())
        _row("Поиск", lambda: self._open_search())
        _sep()
        _row("Новая вкладка", lambda: self._ctx_app_call("_add_tab"))
        _row("Закрыть вкладку", lambda: self._ctx_app_call("close_tab", self))
        _sep()
        _row("Подключения…", lambda: self._ctx_app_call("open_connections"))
        _row("Настройки…", lambda: self._ctx_app_call("open_settings"))

        self._ctx_popover = Gtk.Popover()
        self._ctx_popover.set_child(box)
        self._ctx_popover.set_parent(self.terminal)
        self._ctx_popover.set_has_arrow(False)
        self._ctx_popover.set_halign(Gtk.Align.START)
        self._ctx_popover.set_position(Gtk.PositionType.BOTTOM)

    def _on_ctx_item(self, _btn, callback):
        """Pop the menu down, then run the selected action — from idle.

        Running it inline is what trips GTK's "Broken accounting of active
        state for widget …(GtkPopover)".  We are inside the click gesture of a
        button that lives *inside* this popover, and some of the actions tear
        the popover down while that gesture is still being dispatched:
        "Закрыть вкладку" reaches cleanup(), which unparents it, and
        "Настройки…" opens a modal window that takes the focus out from under
        it.  Either way the press is never matched by its release, GTK's active
        counter is left non-zero, and the implicit pointer grab the popup took
        is never released cleanly.

        Deferring to idle lets the gesture finish and the popover finish
        popping down before anything else happens — the same reasoning that
        already applies to popup() in _on_context_menu().
        """
        self._ctx_popover.popdown()
        GLib.idle_add(self._run_ctx_action, callback)

    @staticmethod
    def _run_ctx_action(callback):
        callback()
        return GLib.SOURCE_REMOVE

    def _ctx_app_call(self, method, *args):
        """Invoke a method on the TerminalApp (if available)."""
        root = self.get_root()
        app = root.get_application() if root else None
        if app is None:
            from gi.repository import Gio as _Gio
            app = _Gio.Application.get_default()
        if app is not None and hasattr(app, method):
            getattr(app, method)(*args)

    def _on_context_menu(self, terminal, x, y):
        """Show the right-click popover at the pointer position."""
        # Enable/disable items based on current state.
        has_sel = False
        try:
            has_sel = terminal.get_has_selection()
        except Exception:
            has_sel = False
        if self._ctx_copy_btn is not None:
            self._ctx_copy_btn.set_sensitive(has_sel)

        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._ctx_popover.set_pointing_to(rect)

        # Already on screen: just move it.  Repeated right-clicks must not turn
        # into popup() on a popover that is up — that is a second attempt to
        # take a grab GTK thinks it already holds, and it is what produces
        # "Broken accounting of active state for widget (GtkPopover)".
        # Repositioning is also what the user means by right-clicking again.
        if self._ctx_popover.get_visible():
            return

        # Defer popup() to idle: showing the popover synchronously from inside
        # the click-gesture handler leaves the gesture grab active and trips the
        # same warning.  Popping up after the gesture settles avoids it.
        #
        # Only ever one deferred popup in flight.  Clicking fast enough to be
        # read as a double/triple click still fires the press handler once per
        # press, so the signal arrives several times before the first idle
        # callback runs; without this guard each would queue its own popup() and
        # all but the first would fire against an already-visible popover.  The
        # rect set above belongs to the newest click, so dropping the extra
        # callbacks loses nothing.
        if self._ctx_popup_id:
            return
        self._ctx_popup_id = GLib.idle_add(self._ctx_popup_idle)

    def _ctx_popup_idle(self):
        self._ctx_popup_id = 0
        popover = getattr(self, "_ctx_popover", None)
        if popover is not None and not popover.get_visible():
            popover.popup()
        return GLib.SOURCE_REMOVE

    def bind_tab_label(self, label):
        """Bind a Gtk.Label to this tab for title updates."""
        self._tab_label = label

    def grab_terminal_focus(self):
        """Give keyboard focus to the terminal — or to the picker if it is up.

        The app focuses the "terminal" on tab switch and on window activation;
        while a tab is still asking which session to start, the keyboard has
        to go to the picker instead or the arrow keys do nothing.
        """
        if self._picker is not None:
            self._picker.grab_picker_focus()
            return
        self.terminal.grab_focus()

    def cleanup(self):
        """Clean up resources and kill child process."""
        self._cancel_search_debounce()
        # Closing a tab that is still showing the picker: drop it first so a
        # queued choice callback becomes a no-op instead of spawning a shell
        # into a terminal that is about to be shut down.
        if self._picker is not None:
            try:
                self._outer_overlay.remove_overlay(self._picker)
            except Exception:
                pass
            self._picker = None
        if self._title_timer_id:
            GLib.source_remove(self._title_timer_id)
            self._title_timer_id = 0

        # Drop a deferred popup before tearing the popover down, or it fires
        # afterwards and pops up a menu belonging to a closed tab.
        if getattr(self, "_ctx_popup_id", 0):
            GLib.source_remove(self._ctx_popup_id)
            self._ctx_popup_id = 0

        # Tear down the right-click popover (parented on the terminal widget)
        # so GTK doesn't warn about finalizing a widget with children.
        popover = getattr(self, "_ctx_popover", None)
        if popover is not None:
            try:
                # Down first, then unparent.  Unparenting a popup that is still
                # mapped leaves its surface (and the grab that goes with it)
                # for GTK to clean up implicitly, which is where the "Broken
                # accounting of active state" warnings come from.
                popover.popdown()
            except Exception:
                pass
            try:
                popover.unparent()
            except Exception:
                pass
            self._ctx_popover = None

        # Stop the core widget's render/poll tick.
        try:
            self.terminal.shutdown()
        except Exception:
            pass

        # Send SIGHUP to the child process group to ensure clean exit
        if self._child_pid > 0 and not self._child_exited:
            try:
                # Use process group ID to kill all descendants (e.g. nested shells)
                os.killpg(os.getpgid(self._child_pid), signal.SIGHUP)
            except Exception:
                # Fallback to direct kill if pgid fails
                try:
                    os.kill(self._child_pid, signal.SIGHUP)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# TerminalApp — GTK4 Application with tabbed VTE terminals
# ---------------------------------------------------------------------------

