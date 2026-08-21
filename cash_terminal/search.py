"""SearchMixin — incremental search + Konsole-style highlight overlay.

Matching is done by the Rust core (alacritty's RegexSearch) rather than in
Python.  The core scans the grid directly, so there is no full-scrollback text
extraction and no Python-side match cache: highlights are re-derived from the
live grid on every frame (see core_terminal._draw), and Enter / F3 walk the
buffer via PtyTerm.search_next().  This module is left with the UI and the
scroll placement policy.
"""
from gi.repository import Gtk, Gdk, GLib


# Regex metacharacters, per Rust's regex-syntax.  The search field holds
# literal text, so these must be escaped before the query reaches the core.
_RE_META = frozenset(r"\.+*?()[]{}|^$")


def _escape_literal(text):
    """Escape `text` so the core matches it literally.

    Deliberately narrower than re.escape(): Python also escapes characters like
    space, '#' and '~', which Rust's regex parser rejects as unknown escape
    sequences.  Letter case is preserved untouched — it is what drives the
    core's smart-case rule (all-lowercase query => case-insensitive).
    """
    return "".join("\\" + c if c in _RE_META else c for c in text)


class SearchMixin:
    # ---------------------------------------------------------------- Search
    def _build_search_bar(self):
        """Build compact search bar widget (top-right, Chrome-style)."""
        self._search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._search_box.add_css_class("search-bar")
        self._search_box.set_halign(Gtk.Align.END)
        self._search_box.set_valign(Gtk.Align.START)
        self._search_box.set_visible(False)
        # margin-end is set dynamically by _sync_overlay_margins()
        self._search_box.set_margin_top(2)

        self._search_entry = Gtk.Entry()
        self._search_entry.set_width_chars(30)
        self._search_entry.add_css_class("search-entry")
        self._search_entry.set_placeholder_text("Search…")
        # Disable cursor blinking in search entry (#1)
        try:
            settings = Gtk.Settings.get_default()
            if settings:
                settings.set_property("gtk-cursor-blink", False)
        except Exception:
            pass
        self._search_entry.connect("changed", self._on_search_changed)
        
        # Status label (e.g. 1/10)
        self._search_status_label = Gtk.Label(label="")
        self._search_status_label.set_margin_start(4)
        self._search_status_label.set_margin_end(4)
        self._search_status_label.add_css_class("search-status")
        
        # Key handler for Escape, Enter, Shift+Enter, F3
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_search_key)
        self._search_entry.add_controller(key_ctrl)
        
        # Focus handler
        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("leave", self._on_search_focus_leave)
        self._search_entry.add_controller(focus_ctrl)

        self._search_box.append(self._search_entry)
        self._search_box.append(self._search_status_label)

        # Navigation buttons
        prev_btn = Gtk.Button(label="▲")
        prev_btn.set_has_frame(False)
        prev_btn.add_css_class("search-nav-btn")
        prev_btn.set_tooltip_text("Previous (Enter / F3)")
        prev_btn.connect("clicked", lambda b: self._search_find(forward=False))
        self._search_box.append(prev_btn)

        next_btn = Gtk.Button(label="▼")
        next_btn.set_has_frame(False)
        next_btn.add_css_class("search-nav-btn")
        next_btn.set_tooltip_text("Next (Shift+Enter)")
        next_btn.connect("clicked", lambda b: self._search_find(forward=True))
        self._search_box.append(next_btn)

        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.set_has_frame(False)
        close_btn.add_css_class("search-close-btn")
        close_btn.connect("clicked", lambda b: self._close_search())
        self._search_box.append(close_btn)

    def _update_search_status(self, found=None):
        """Update the search entry border.  No match count is displayed.

        Counting every occurrence meant scanning the whole scrollback, which
        does not scale to a million-line journal.  Like Konsole we only signal
        whether the query matches.

        `found` carries the verdict of a real search (Enter / F3 / the typing
        jump).  Pass None when no search was run — then we fall back to "is
        anything highlighted on screen right now", and stay neutral rather than
        claiming "No match" for something that may well exist off-screen.
        """
        if not getattr(self, '_search_active', False) or not self._search_entry:
            return
        if self._search_status_label is not None:
            self._search_status_label.set_text("")

        if not self._search_entry.get_text():
            self._set_search_status("")
            return
        if found is None:
            self._set_search_status(
                "found" if self.terminal.has_visible_match() else "")
            return
        self._set_search_status("found" if found else "No match")

    def _set_search_status(self, text):
        """Update search entry visual feedback: green border on match, red on no match.

        Empty text = neutral (default border).
        """
        entry = self._search_entry
        entry.remove_css_class("search-match")
        entry.remove_css_class("search-no-match")
        if text == "No match":
            entry.add_css_class("search-no-match")
        elif text == "":
            pass  # neutral — default border
        else:
            # "Wrapped" or any other status — matches exist
            entry.add_css_class("search-match")

    def _on_search_focus_leave(self, *_args):
        """Clear match/no-match border when search entry loses focus."""
        self._search_entry.remove_css_class("search-match")
        self._search_entry.remove_css_class("search-no-match")

    def _toggle_search(self):
        """Toggle search bar visibility."""
        if self._search_active:
            self._close_search()
        else:
            self._open_search()

    def _open_search(self):
        """Show search bar and focus entry."""
        self._search_active = True
        self._search_box.set_visible(True)
        self._search_entry.grab_focus()
        # Select all text in entry for easy replacement
        self._search_entry.select_region(0, -1)

    def _close_search(self):
        """Hide search bar, clear highlights, clear field, reset scroll."""
        self._search_active = False
        self._search_box.set_visible(False)
        self._set_search_status("")
        self._cancel_search_debounce()
        # Drop the pattern in the core — that is what removes the highlights,
        # since they are re-derived from the core on every frame.
        try:
            self.terminal.clear_search()
        except Exception:
            pass
        self._search_installed_query = None
        self._search_current_match = None
        self._search_anchor_row = None
        self._search_view_row = None
        self._push_highlights()
        # Clear search field so next open starts fresh (#5)
        self._search_entry.set_text("")
        # Return focus to the terminal.
        self.terminal.grab_focus()

    # ----------------------------------------- Konsole-style search highlights

    def _get_scroll_row(self):
        """Get the first visible absolute row index.
        In VTE, the absolute row index 0 is always the start of the scrollback buffer.
        """
        vadj = self.terminal.get_vadjustment()
        if vadj is None:
            return 0
        val = vadj.get_value()
        upper = vadj.get_upper()
        page = vadj.get_page_size()
        
        # Clamp value to [0, upper - page] just in case VTE state is inconsistent
        scroll_row = int(max(0, min(val, upper - page)))
        
        
        return scroll_row

    def _push_highlights(self):
        """Repaint so the core is re-queried for the matches on screen.

        There is no match list to push any more: core_terminal._draw calls
        PtyTerm.search_visible() while painting, so the highlights are computed
        from the very grid being drawn and cannot fall out of sync with it.
        """
        self.terminal.queue_draw()

    def _on_search_scroll(self, *_args):
        """Redraw highlights when terminal scrolls."""
        if self._search_active:
            self._push_highlights()

    def _on_vte_reset(self, *_args):
        """Handle terminal reset (clear buffer).

        The buffer the core's search focus points into is gone, so forget the
        installed query too: the next find reinstalls the pattern, which resets
        the focus, instead of resuming from a position that no longer exists.
        """
        self._search_current_match = None
        self._search_anchor_row = None
        self._search_view_row = None
        self._search_installed_query = None
        self._push_highlights()

    def _paste_into_search_entry(self):
        """Paste clipboard text into the search entry at the cursor.

        The window-level key handler (CAPTURE phase) intercepts Ctrl+V /
        Ctrl+Insert / Ctrl+Shift+V before the entry can act on them (it blocks
        VTE's native paste and routes secure pastes to the terminal), so the
        search field never received a paste.  When the search entry is focused
        we call this instead: read the clipboard and splice it in.  Search is a
        single-line field, so newlines are collapsed to spaces.
        """
        entry = self._search_entry
        if entry is None:
            return
        clipboard = entry.get_clipboard()

        def _on_text(cb, res):
            try:
                text = cb.read_text_finish(res)
            except Exception:
                text = None
            if not text:
                return
            text = text.replace("\r", " ").replace("\n", " ")
            entry.delete_selection()  # replace selection if any (no-op otherwise)
            pos = entry.get_position()
            old = entry.get_text()
            entry.set_text(old[:pos] + text + old[pos:])
            entry.set_position(pos + len(text))

        clipboard.read_text_async(None, _on_text)

    def _on_search_key(self, controller, keyval, keycode, state):
        """Handle special keys in search entry."""
        mods = state & Gtk.accelerator_get_default_mod_mask()
        shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)

        if keyval == Gdk.KEY_Escape:
            self._close_search()
            return True
            
        # Enter: search NEXT (Older/Upwards by default in terminal)
        # Shift+Enter: search PREVIOUS (Newer/Downwards)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            # _search_find() already handles the no-current-match case: the
            # core starts from the viewport edge.  Do NOT pre-select here —
            # that caused a double-step that skipped the nearest match and
            # jumped a screen.
            self._search_find(forward=shift)
            return True
            
        if keyval == Gdk.KEY_F3:
            self._search_find(forward=shift)
            return True
            
        return False

    def _on_search_changed(self, entry):
        """Text changed in search entry — debounced auto-search."""
        self._set_search_status("")
        if self._search_status_label:
            self._search_status_label.set_text("")

        # Cancel previous debounce timer
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None
        
        query = entry.get_text()

        if not query:
            # Text cleared — drop the search in the core (which removes the
            # highlights) and forget the anchor so the next search re-captures
            # the current position.
            self._search_current_match = None
            self._search_anchor_row = None
            self._search_view_row = None
            self._apply_search_regex()  # clears the core's pattern
            self._push_highlights()
            return

        # Schedule debounce — the callback jumps to the nearest match upward
        # from the fixed anchor captured on the first keystroke.
        self._search_debounce_id = GLib.timeout_add(
            200, self._do_smart_search)

    def _do_smart_search(self):
        """Debounce callback — auto-jump to the nearest match upward.

        On every keystroke we jump to the nearest match UP from a fixed anchor
        (the viewport position captured when the search began).  Using a fixed
        anchor — instead of the live, already-scrolled position — is what stops
        the search from climbing one screen further up on each keystroke.
        """
        self._search_debounce_id = None
        return self._do_highlight_only_impl()

    def _do_highlight_only_impl(self):
        """Auto-jump to the nearest match upward from the search anchor.

        The anchor is the viewport-bottom row captured when the search began,
        so typing more characters re-evaluates from the SAME reference instead
        of climbing upward.  It has to be passed explicitly to the core: every new
        query reinstalls the pattern, which clears the core's focus, and its
        fallback origin is the current viewport — already scrolled by the
        previous keystroke's jump.

        The jump happens whether or not the console is streaming output.  It
        used to be suppressed while output flowed, which left the viewport
        pinned to the bottom watching the output instead of moving to the hit.
        Scrolling away from the bottom is exactly what freezes the view: the
        core keeps a scrolled-back viewport on the same content as new lines
        arrive, so the match stays put once we land on it.
        """
        if not self._search_active:
            return False
        if not self._apply_search_regex():
            self._search_current_match = None
            self._push_highlights()
            self._update_search_status()
            return False

        # (Re)capture the anchor from the current viewport bottom whenever the
        # view is not where our own last jump left it — the user scrolled, or
        # the view is riding live output.  A search that stays open across a
        # burst of output must start from what the user is looking at NOW, not
        # from wherever the bar happened to be opened.
        #
        # When the view has not moved, the anchor is kept: that is the whole
        # point of it.  Each extra character would otherwise re-evaluate from
        # the position the previous character's jump scrolled to, so the search
        # would climb a screen further up on every keystroke.
        if self._search_anchor_row is None or self._view_moved_since_jump():
            nrows = self.terminal.get_row_count()
            self._search_anchor_row = self._get_scroll_row() + nrows - 1

        pos = self.terminal.search_next(
            reverse=True, origin_row=self._search_anchor_row)
        if pos is None:
            self._search_current_match = None
            self._push_highlights()
            self._update_search_status(found=False)
            return False

        self._search_current_match = pos
        self._scroll_to_match(pos[0])
        self._remember_view()
        self._push_highlights()
        self._update_search_status(found=True)
        return False  # don't repeat

    def _cancel_search_debounce(self):
        """Cancel any pending debounce timers."""
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None

    def _apply_search_regex(self):
        """Install the current query in the core's search engine.

        Returns True when the core holds a usable pattern afterwards.

        The pattern is only (re)installed when the text actually changed:
        PtyTerm.set_search() resets the core's search focus, so calling it on
        every Enter/F3 would restart navigation from the viewport each time.
        The query is escaped to be matched literally (see _escape_literal); the
        core applies smart-case on top of it (all-lowercase query =>
        case-insensitive).
        """
        query = self._search_entry.get_text() if self._search_entry else ""

        if not query:
            if getattr(self, '_search_installed_query', None) is not None:
                self.terminal.clear_search()
                self._search_installed_query = None
            return False

        if getattr(self, '_search_installed_query', None) == query:
            return True  # already installed — keep the core's focus intact

        ok = self.terminal.set_search(_escape_literal(query))
        self._search_installed_query = query if ok else None
        return bool(ok)

    def _remember_view(self):
        """Record where a jump left the viewport (see _view_moved_since_jump)."""
        self._search_view_row = self._get_scroll_row()

    def _view_moved_since_jump(self):
        """True when the viewport is no longer where our last jump left it.

        Two things move it: the user scrolling, and live output while the view
        rides the bottom.  Both mean the user is now looking somewhere else, so
        the search must re-orient itself instead of continuing from a reference
        that has gone stale.  A jump of our own always calls _remember_view(),
        so our own scrolling never counts as movement.
        """
        last = getattr(self, '_search_view_row', None)
        return last is None or last != self._get_scroll_row()

    def _is_view_at_bottom(self):
        """True when the viewport is parked at the end of the buffer.

        In that state the view follows live output: the core keeps the display
        offset at 0, so everything on screen marches upward as new lines
        arrive.  Scrolled back even by one line, the core pins the view to the
        content instead (it grows the display offset in step with the history)
        and what is on screen stays put however much output follows.
        """
        vadj = self.terminal.get_vadjustment()
        if not vadj:
            return True
        return vadj.get_value() >= (vadj.get_upper() - vadj.get_page_size()) - 0.5

    def _scroll_to_match(self, target_abs_row):
        """Scroll the viewport onto `target_abs_row`.

        Browser-like behaviour:
        - If the row is already visible on screen → do NOT scroll at all.
        - If it is off-screen → scroll so the row lands at ~2/3 of the
          viewport height (slightly below centre), matching Chrome/Firefox.

        Exception: while the view is still parked at the bottom we scroll even
        for an already-visible row.  "Visible" means nothing there — the view
        is riding live output, so the match would drift off the top a moment
        later.  Scrolling away from the bottom is precisely what pins the view
        to the content, so this is what keeps the found match under the user's
        eyes instead of letting the output carry it away.
        """
        vadj = self.terminal.get_vadjustment()
        if not vadj:
            return
        upper = vadj.get_upper()
        page = vadj.get_page_size()
        if upper <= page:
            return

        scroll_row = self._get_scroll_row()
        nrows = self.terminal.get_row_count()
        viewport_bottom = scroll_row + nrows - 1

        # Row already visible in an already-pinned view — leave the scroll alone
        if (scroll_row <= target_abs_row <= viewport_bottom
                and not self._is_view_at_bottom()):
            return

        # Place target at ~2/3 down the viewport (slightly below centre)
        max_scroll = float(max(0, upper - page))
        new_scroll = float(max(0, target_abs_row - int(nrows * 2 / 3)))

        # Keep at least one line below the viewport.  max_scroll is exactly the
        # "display offset 0" position, i.e. the follow-the-output state, so a
        # value clamped to it drops us straight back to riding the output and
        # the match scrolls away within a moment.  This bites whenever the
        # match sits in the lower third of the screen — its ideal 2/3 placement
        # then lies past the end of the buffer — which is the normal case when
        # output arrives at a human pace (an ssh login, a build log) rather
        # than in a flood.
        new_scroll = min(new_scroll, max(0.0, max_scroll - 1.0))

        # ...but never so far up that the match itself leaves the screen. Only
        # reachable for a match on the last line or two, where staying pinned
        # and staying visible genuinely cannot both hold.
        new_scroll = max(new_scroll, float(max(0, target_abs_row - nrows + 1)))

        vadj.set_value(min(new_scroll, max_scroll))

    def _search_find(self, forward=True):
        """Jump to the next/previous match, one step at a time.

        Navigation is delegated to the core's search engine, which walks the
        grid itself: no full-scrollback rescan, and the returned position always
        refers to the grid as it is right now, so it cannot drift when the
        scrollback evicts lines.

        forward=False (Enter)        -> older / upward
        forward=True  (Shift+Enter)  -> newer / downward

        Scrolling stays here (_scroll_to_match) so the browser-like rule holds:
        an already-visible match does not move the viewport.
        """
        if not self._apply_search_regex():
            self._search_current_match = None
            self._push_highlights()
            self._update_search_status(found=False)
            return

        # Step from the last match we landed on, handed back in ABSOLUTE
        # coordinates.  The core also keeps its own focus, but that one is a
        # grid-space Point: while output streams, every new line shifts the
        # content under it, so within a second it points near the bottom of the
        # buffer and each Enter restarts the search from there.  Absolute rows
        # do not move while history grows, so stepping stays correct no matter
        # how much output arrives between two presses.
        cur = self._search_current_match
        if cur is not None and self._view_moved_since_jump():
            # The user scrolled away, or the view rode live output down: the
            # previous match is no longer what is being looked at, so step from
            # the viewport instead of continuing an abandoned walk.
            cur = None
            self._search_current_match = None

        if cur is not None:
            pos = self.terminal.search_next(
                reverse=not forward, origin_row=cur[0], origin_col=cur[1])
        else:
            # First step of this search — let the core start from the viewport
            # edge (bottom when going up, top when going down).
            pos = self.terminal.search_next(reverse=not forward)

        if pos is None:
            self._search_current_match = None
            self._push_highlights()
            self._update_search_status(found=False)
            return

        self._search_current_match = pos
        # Keep the anchor on the match we landed on, so refining the query
        # afterwards re-evaluates from here rather than from where the search
        # originally started.
        self._search_anchor_row = pos[0]
        self._scroll_to_match(pos[0])
        self._remember_view()
        self._push_highlights()
        self._update_search_status(found=True)

