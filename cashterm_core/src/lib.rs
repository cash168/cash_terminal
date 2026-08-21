//! Slice-1 Rust core for Cash Terminal, exposed to Python via pyo3.
//!
//! Owns: a PTY running a shell, alacritty_terminal's parser + grid (with a
//! background reader thread that parses without the GIL), and exposes to Python:
//!   * `PtyTerm(rows, cols, shell)`  -> construct + spawn shell
//!   * `feed_input(bytes)`           -> user keystrokes to the PTY
//!   * `snapshot() -> bytes`         -> packed visible grid (16 bytes/cell)
//!   * `cursor() -> (row, col, vis)` -> cursor position
//!
//! Cell packing (little-endian u32 x4 per cell, row-major rows*cols):
//!   [0] codepoint (char as u32; 0/space allowed)
//!   [1] fg color  (tagged, see encode_color)
//!   [2] bg color  (tagged)
//!   [3] flags     (bit0 bold,1 italic,2 underline,3 inverse,4 strike,5 dim,
//!                  6 wide-char,7 wide-char-spacer)
//!
//! Color tag (high byte) + value (low 24 bits):
//!   tag 0 -> default foreground      (Python resolves from config)
//!   tag 1 -> default background
//!   tag 2 -> palette index (value 0..255; 0..15 = ANSI, rest = xterm-256)
//!   tag 3 -> direct RGB (value = 0xRRGGBB)
//!
//! Slice-1 scope: live shell + colored grid + cursor. NO scrollback/search/
//! mouse/resize yet (Slices 2-4). Grid size is fixed at construction.

use std::cell::RefCell;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use std::thread;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use alacritty_terminal::grid::{Dimensions, Scroll};
use alacritty_terminal::index::{Boundary, Column, Direction, Line, Point, Side};
use alacritty_terminal::selection::{Selection, SelectionType};
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::search::{RegexIter, RegexSearch};
use alacritty_terminal::term::{Config, Term, TermMode};
use alacritty_terminal::event::{Event, EventListener};
use alacritty_terminal::vte::ansi::{Color, NamedColor, Processor};

use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};

/// State shared between the parser thread (writes via the event listener) and
/// the Python thread (reads title / child-exit). PtyWrite bytes (terminal
/// query responses like DSR/DA) accumulate in `pty_out`; the reader thread
/// flushes them back to the child after each parse batch.
#[derive(Default)]
struct SharedState {
    title: Option<String>,
    pty_out: Vec<u8>,
    child_exited: bool,
}

/// Event sink for alacritty. Captures the window title and queues terminal
/// query responses; ignores the rest (mouse/clipboard handled in Python).
#[derive(Clone)]
struct AppListener {
    state: Arc<Mutex<SharedState>>,
}
impl EventListener for AppListener {
    fn send_event(&self, event: Event) {
        match event {
            Event::Title(t) => {
                self.state.lock().unwrap().title = Some(t);
            }
            Event::ResetTitle => {
                self.state.lock().unwrap().title = None;
            }
            // The terminal answering a query (cursor position, device
            // attributes, ...). Must be written back to the child or apps hang.
            Event::PtyWrite(text) => {
                self.state
                    .lock()
                    .unwrap()
                    .pty_out
                    .extend_from_slice(text.as_bytes());
            }
            _ => {}
        }
    }
}

/// Minimal Dimensions. total_lines = visible + scrollback history.
struct Size {
    cols: usize,
    screen_lines: usize,
    history: usize,
}
impl Dimensions for Size {
    fn total_lines(&self) -> usize {
        self.screen_lines + self.history
    }
    fn screen_lines(&self) -> usize {
        self.screen_lines
    }
    fn columns(&self) -> usize {
        self.cols
    }
}

/// Shared between the Python thread (snapshot/cursor) and the reader thread
/// (parse). Only the grid-bearing Term needs sharing; the Processor lives in
/// the reader thread.
struct Inner {
    term: Term<AppListener>,
}

fn named_index(n: NamedColor) -> Option<u32> {
    use NamedColor::*;
    Some(match n {
        Black => 0,
        Red => 1,
        Green => 2,
        Yellow => 3,
        Blue => 4,
        Magenta => 5,
        Cyan => 6,
        White => 7,
        BrightBlack => 8,
        BrightRed => 9,
        BrightGreen => 10,
        BrightYellow => 11,
        BrightBlue => 12,
        BrightMagenta => 13,
        BrightCyan => 14,
        BrightWhite => 15,
        _ => return None,
    })
}

fn encode_color(c: Color) -> u32 {
    match c {
        Color::Named(NamedColor::Foreground) => 0u32 << 24,
        Color::Named(NamedColor::Background) => 1u32 << 24,
        Color::Named(n) => match named_index(n) {
            Some(i) => (2u32 << 24) | i,
            None => 0u32 << 24, // unknown named -> default fg
        },
        Color::Indexed(i) => (2u32 << 24) | (i as u32),
        Color::Spec(rgb) => {
            (3u32 << 24) | ((rgb.r as u32) << 16) | ((rgb.g as u32) << 8) | (rgb.b as u32)
        }
    }
}

fn encode_flags(f: Flags) -> u32 {
    let mut v = 0u32;
    if f.contains(Flags::BOLD) {
        v |= 1;
    }
    if f.contains(Flags::ITALIC) {
        v |= 2;
    }
    if f.contains(Flags::UNDERLINE) {
        v |= 4;
    }
    if f.contains(Flags::INVERSE) {
        v |= 8;
    }
    if f.contains(Flags::STRIKEOUT) {
        v |= 16;
    }
    if f.contains(Flags::DIM) {
        v |= 32;
    }
    if f.contains(Flags::WIDE_CHAR) {
        v |= 64;
    }
    if f.contains(Flags::WIDE_CHAR_SPACER) {
        v |= 128;
    }
    v
}

#[pyclass(unsendable)]
struct PtyTerm {
    inner: Arc<Mutex<Inner>>,
    master: Box<dyn MasterPty + Send>,
    // Shared with the reader thread (which writes query responses), hence
    // Arc<Mutex> rather than RefCell.
    writer: Arc<Mutex<Box<dyn Write + Send>>>,
    state: Arc<Mutex<SharedState>>,
    rows: usize,
    cols: usize,
    // Search state lives on the GTK thread only (RefCell, no lock needed). The
    // compiled regex + the currently focused match.
    //
    // The focus is kept as an ABSOLUTE (row, col) -- row 0 = top of the
    // scrollback -- and NOT as a Point. A Point lives in grid space, where
    // Line(0) is the top of the current screen, so every line of new output
    // shifts the content under a stored Point one row down: with output
    // streaming, a focus saved a moment ago silently comes to mean a different
    // (lower) match. Absolute rows do not move while history grows.
    search: RefCell<Option<RegexSearch>>,
    search_focus: RefCell<Option<(i32, usize)>>,
    _child: Box<dyn portable_pty::Child + Send + Sync>,
}

#[pymethods]
impl PtyTerm {
    #[new]
    #[pyo3(signature = (rows, cols, shell, args=Vec::new(), cwd=None, env=Vec::new(), scrollback=10_000))]
    fn new(
        rows: usize,
        cols: usize,
        shell: String,
        args: Vec<String>,
        cwd: Option<String>,
        env: Vec<(String, String)>,
        scrollback: usize,
    ) -> PyResult<Self> {
        let size = Size {
            cols,
            screen_lines: rows,
            history: scrollback,
        };
        let state = Arc::new(Mutex::new(SharedState::default()));
        let listener = AppListener {
            state: Arc::clone(&state),
        };
        // `scrolling_history` is what actually caps the grid's scrollback ring
        // (Config::default() hardcodes 10_000).  The grid allocates only the
        // visible lines up front and grows lazily, so a large cap costs nothing
        // until the lines are really produced.
        let cfg = Config {
            scrolling_history: scrollback,
            ..Config::default()
        };
        let term = Term::new(cfg, &size, listener);
        let inner = Arc::new(Mutex::new(Inner { term }));

        let pty_system = native_pty_system();
        let pair = pty_system
            .openpty(PtySize {
                rows: rows as u16,
                cols: cols as u16,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| PyRuntimeError::new_err(format!("openpty: {e}")))?;

        let mut cmd = CommandBuilder::new(shell);
        // Extra argv (e.g. ["-l"] for a login shell).
        for a in &args {
            cmd.arg(a);
        }
        // Working directory for the child.
        if let Some(dir) = cwd.as_deref() {
            cmd.cwd(dir);
        }
        // Environment: when the caller supplies an explicit env list (the real
        // app builds a cleaned one), use exactly that; otherwise inherit the
        // parent environment (mini-host convenience). Either way ensure TERM.
        if env.is_empty() {
            cmd.env("TERM", "xterm-256color");
        } else {
            cmd.env_clear();
            let mut has_term = false;
            for (k, v) in &env {
                if k == "TERM" {
                    has_term = true;
                }
                cmd.env(k, v);
            }
            if !has_term {
                cmd.env("TERM", "xterm-256color");
            }
        }
        let child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| PyRuntimeError::new_err(format!("spawn: {e}")))?;
        drop(pair.slave);

        let mut reader = pair
            .master
            .try_clone_reader()
            .map_err(|e| PyRuntimeError::new_err(format!("clone reader: {e}")))?;
        let writer = pair
            .master
            .take_writer()
            .map_err(|e| PyRuntimeError::new_err(format!("take writer: {e}")))?;
        let writer = Arc::new(Mutex::new(writer));

        // background parse thread (no GIL held here)
        let inner2 = Arc::clone(&inner);
        let state2 = Arc::clone(&state);
        let writer2 = Arc::clone(&writer);
        thread::spawn(move || {
            let mut parser: Processor = Processor::new();
            let mut buf = [0u8; 65536];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break, // genuine EOF: the child closed the PTY
                    // A signal (EINTR) or a spurious WouldBlock must NOT be
                    // mistaken for the shell exiting -- retry, don't break.
                    // Otherwise e.g. exiting an ssh session could kill the
                    // parse thread while the local shell is still alive,
                    // leaving the terminal frozen.
                    Err(ref e)
                        if e.kind() == std::io::ErrorKind::Interrupted
                            || e.kind() == std::io::ErrorKind::WouldBlock =>
                    {
                        continue;
                    }
                    Ok(n) => {
                        {
                            let mut g = inner2.lock().unwrap();
                            for &b in &buf[..n] {
                                parser.advance(&mut g.term, b);
                            }
                        }
                        // Flush any query responses the terminal produced while
                        // parsing (e.g. cursor-position reports) back to the child.
                        let out = std::mem::take(&mut state2.lock().unwrap().pty_out);
                        if !out.is_empty() {
                            let mut w = writer2.lock().unwrap();
                            let _ = w.write_all(&out);
                            let _ = w.flush();
                        }
                    }
                    Err(_) => break,
                }
            }
            // Reader hit EOF / error -> the child has exited.
            state2.lock().unwrap().child_exited = true;
        });

        Ok(PtyTerm {
            inner,
            master: pair.master,
            writer,
            state,
            rows,
            cols,
            search: RefCell::new(None),
            search_focus: RefCell::new(None),
            _child: child,
        })
    }

    /// Send raw bytes (already-encoded keystrokes) to the shell.
    fn feed_input(&self, data: &[u8]) -> PyResult<()> {
        let mut w = self.writer.lock().unwrap();
        w.write_all(data)
            .map_err(|e| PyRuntimeError::new_err(format!("write: {e}")))?;
        let _ = w.flush();
        Ok(())
    }

    /// Current window title set by the app via OSC, or "" if default/unset.
    fn title(&self) -> String {
        self.state
            .lock()
            .unwrap()
            .title
            .clone()
            .unwrap_or_default()
    }

    /// True once the child process has exited (PTY reached EOF).
    fn child_exited(&self) -> bool {
        self.state.lock().unwrap().child_exited
    }

    /// PID of the spawned shell, or -1 if unknown. Used by the Python side for
    /// /proc-based title building, alt-screen detection and paste safety.
    fn pid(&self) -> i64 {
        self._child.process_id().map(|p| p as i64).unwrap_or(-1)
    }

    /// Raw fd of the PTY master, or -1. Lets Python do TIOCGPGRP (foreground
    /// process group) and direct writes the same way it did with VTE's pty.
    fn pty_fd(&self) -> i64 {
        self.master.as_raw_fd().map(|fd| fd as i64).unwrap_or(-1)
    }

    #[getter]
    fn rows(&self) -> usize {
        self.rows
    }

    #[getter]
    fn cols(&self) -> usize {
        self.cols
    }

    /// Packed visible grid: rows*cols cells, 16 bytes each (see module docs).
    fn snapshot<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let rows = self.rows;
        let cols = self.cols;
        let mut buf = vec![0u8; rows * cols * 16];

        let inner = self.inner.lock().unwrap();
        let grid = inner.term.grid();
        // When scrolled into history, shift each visible row up by the display
        // offset; negative lines resolve into the scrollback ring.
        let disp = grid.display_offset() as i32;
        let mut off = 0usize;
        for r in 0..rows {
            let line = Line(r as i32 - disp);
            for c in 0..cols {
                let cell = &grid[line][Column(c)];
                let cp = cell.c as u32;
                let fg = encode_color(cell.fg);
                let bg = encode_color(cell.bg);
                let fl = encode_flags(cell.flags);
                buf[off..off + 4].copy_from_slice(&cp.to_le_bytes());
                buf[off + 4..off + 8].copy_from_slice(&fg.to_le_bytes());
                buf[off + 8..off + 12].copy_from_slice(&bg.to_le_bytes());
                buf[off + 12..off + 16].copy_from_slice(&fl.to_le_bytes());
                off += 16;
            }
        }
        drop(inner);
        PyBytes::new_bound(py, &buf)
    }

    /// (row, col, visible) of the cursor in the visible viewport. When scrolled
    /// into history the cursor may fall outside the viewport -> visible=false.
    fn cursor(&self) -> (i32, usize, bool) {
        let inner = self.inner.lock().unwrap();
        let grid = inner.term.grid();
        let disp = grid.display_offset() as i32;
        let point = grid.cursor.point;
        let vrow = point.line.0 + disp;
        let visible = vrow >= 0 && vrow < self.rows as i32;
        (vrow, point.column.0, visible)
    }

    /// Scroll the display by `delta` lines (positive = up into history).
    fn scroll_lines(&self, delta: i32) {
        let mut g = self.inner.lock().unwrap();
        g.term.scroll_display(Scroll::Delta(delta));
    }

    /// Jump the display back to the live bottom (offset 0).
    fn scroll_to_bottom(&self) {
        let mut g = self.inner.lock().unwrap();
        g.term.scroll_display(Scroll::Bottom);
    }

    /// Current scrollback offset (0 = at the live bottom).
    fn display_offset(&self) -> usize {
        let g = self.inner.lock().unwrap();
        g.term.grid().display_offset()
    }

    /// Number of scrollback lines currently available above the viewport.
    fn history_size(&self) -> usize {
        let g = self.inner.lock().unwrap();
        let grid = g.term.grid();
        grid.total_lines().saturating_sub(grid.screen_lines())
    }

    /// Extract text from a viewport rectangle [start..=end] (row/col in current
    /// visible coordinates). Rows that soft-wrap into the next are joined with
    /// no newline; hard line breaks get '\n'. Trailing blanks per row are
    /// stripped. Wide-char spacer cells are skipped. Used for selection/copy.
    fn get_text_range(
        &self,
        start_row: i32,
        start_col: usize,
        end_row: i32,
        end_col: usize,
    ) -> String {
        let inner = self.inner.lock().unwrap();
        let grid = inner.term.grid();
        let cols = self.cols;
        let rows = self.rows as i32;
        if cols == 0 || rows == 0 {
            return String::new();
        }
        let disp = grid.display_offset() as i32;

        // normalize so (sr,sc) precedes (er,ec)
        let (sr, sc, er, ec) = if (start_row, start_col) <= (end_row, end_col) {
            (start_row, start_col, end_row, end_col)
        } else {
            (end_row, end_col, start_row, start_col)
        };
        let sr = sr.max(0);
        let er = er.min(rows - 1);

        let mut out = String::new();
        let mut r = sr;
        while r <= er {
            let row = &grid[Line(r - disp)];
            let c0 = if r == sr { sc } else { 0 };
            let c1 = if r == er { ec.min(cols - 1) } else { cols - 1 };
            let mut seg = String::new();
            let mut c = c0;
            while c <= c1 {
                let cell = &row[Column(c)];
                if cell.flags.contains(Flags::WIDE_CHAR_SPACER)
                    || cell.flags.contains(Flags::LEADING_WIDE_CHAR_SPACER)
                {
                    c += 1;
                    continue;
                }
                let ch = cell.c;
                seg.push(if ch == '\0' { ' ' } else { ch });
                c += 1;
            }
            if c1 == cols - 1 {
                while seg.ends_with(' ') {
                    seg.pop();
                }
            }
            out.push_str(&seg);
            if r < er {
                let wrapped = row[Column(cols - 1)].flags.contains(Flags::WRAPLINE);
                if !wrapped {
                    out.push('\n');
                }
            }
            r += 1;
        }
        out
    }

    /// Extract text by ABSOLUTE scrollback row index, where row 0 is the top of
    /// the scrollback buffer and the last row is the live bottom -- the same
    /// coordinate space the Python side uses for the scroll adjustment
    /// (value=top visible absolute row, upper=history+screen). Independent of
    /// the current display offset. Each row is returned on its own line; trailing
    /// blanks are stripped and wide-char spacer cells skipped. Used by search to
    /// scan the whole scrollback.
    fn text_abs(&self, start_row: i32, end_row: i32) -> String {
        let inner = self.inner.lock().unwrap();
        let grid = inner.term.grid();
        let cols = self.cols;
        if cols == 0 {
            return String::new();
        }
        let hist = grid.total_lines().saturating_sub(grid.screen_lines()) as i32;
        let total = grid.total_lines() as i32;
        let (sr, er) = if start_row <= end_row {
            (start_row, end_row)
        } else {
            (end_row, start_row)
        };
        let sr = sr.max(0);
        let er = er.min(total - 1);

        let mut out = String::new();
        let mut r = sr;
        while r <= er {
            // absolute row r -> grid Line (top of scrollback is Line(-hist))
            let line = Line(r - hist);
            let row = &grid[line];
            let mut seg = String::new();
            for c in 0..cols {
                let cell = &row[Column(c)];
                if cell.flags.contains(Flags::WIDE_CHAR_SPACER)
                    || cell.flags.contains(Flags::LEADING_WIDE_CHAR_SPACER)
                {
                    continue;
                }
                let ch = cell.c;
                seg.push(if ch == '\0' { ' ' } else { ch });
            }
            while seg.ends_with(' ') {
                seg.pop();
            }
            out.push_str(&seg);
            if r < er {
                out.push('\n');
            }
            r += 1;
        }
        out
    }

    /// Compile a search pattern. Empty pattern / invalid regex -> search off.
    /// Case-insensitive unless the pattern contains an uppercase letter
    /// (alacritty's smart-case, handled inside RegexSearch::new). Returns true
    /// when a usable regex was installed.
    fn set_search(&self, pattern: &str) -> bool {
        *self.search_focus.borrow_mut() = None;
        if pattern.is_empty() {
            *self.search.borrow_mut() = None;
            return false;
        }
        match RegexSearch::new(pattern) {
            Ok(r) => {
                *self.search.borrow_mut() = Some(r);
                true
            }
            Err(_) => {
                *self.search.borrow_mut() = None;
                false
            }
        }
    }

    /// Drop the active search (and its highlight/focus).
    fn clear_search(&self) {
        *self.search.borrow_mut() = None;
        *self.search_focus.borrow_mut() = None;
    }

    /// All match segments intersecting the visible viewport, as
    /// (row, col_start, col_end_inclusive, is_focused). A match spanning
    /// several rows yields one segment per row. Empty when no search active.
    fn search_visible(&self) -> Vec<(i32, usize, usize, bool)> {
        let mut out = Vec::new();
        let mut sb = self.search.borrow_mut();
        let regex = match sb.as_mut() {
            Some(r) => r,
            None => return out,
        };
        let inner = self.inner.lock().unwrap();
        let term = &inner.term;
        let rows = self.rows as i32;
        let cols = self.cols;
        if cols == 0 {
            return out;
        }
        let disp = term.grid().display_offset() as i32;
        let top = -disp;
        let bottom = rows - 1 - disp;
        // The focus is absolute, so the comparison below has to be too --
        // otherwise the focused match loses its darker highlight as soon as
        // output scrolls the grid under it.
        let hist = term
            .grid()
            .total_lines()
            .saturating_sub(term.grid().screen_lines()) as i32;
        let focus = *self.search_focus.borrow();
        let start = Point::new(Line(top), Column(0));
        let end = Point::new(Line(bottom), Column(cols - 1));
        let iter = RegexIter::new(start, end, Direction::Right, term, regex);
        for m in iter {
            let ms = *m.start();
            let me = *m.end();
            let focused = focus == Some((ms.line.0 + hist, ms.column.0));
            let l0 = ms.line.0.max(top);
            let l1 = me.line.0.min(bottom);
            let mut l = l0;
            while l <= l1 {
                let vrow = l + disp;
                if vrow >= 0 && vrow < rows {
                    let cs = if l == ms.line.0 { ms.column.0 } else { 0 };
                    let ce = if l == me.line.0 { me.column.0 } else { cols - 1 };
                    out.push((vrow, cs, ce, focused));
                }
                l += 1;
            }
        }
        out
    }

    /// Move to the next match (reverse=false -> down/right, true -> up/left),
    /// wrapping around the buffer ends. Returns the ABSOLUTE (row, col) of the
    /// match start, where row 0 is the top of the scrollback -- the same
    /// coordinate space as text_abs() and the Python scroll adjustment -- or
    /// None when the pattern does not match anywhere.
    ///
    /// Deliberately does NOT scroll. The Python side positions the match with
    /// browser-like rules (leave the viewport alone when the match is already
    /// on screen, otherwise put it ~2/3 down), and centring it here would
    /// override that.
    #[pyo3(signature = (reverse, origin_row=None, origin_col=None))]
    fn search_next(
        &self,
        reverse: bool,
        origin_row: Option<i32>,
        origin_col: Option<usize>,
    ) -> Option<(i32, usize)> {
        let mut sb = self.search.borrow_mut();
        let regex = match sb.as_mut() {
            Some(r) => r,
            None => return None,
        };
        // Immutable: nothing here mutates the grid any more (the scroll that
        // used to live at the end of this function moved to the Python side).
        let g = self.inner.lock().unwrap();
        let rows = self.rows as i32;
        let cols = self.cols;
        if cols == 0 {
            return None;
        }
        let disp = g.term.grid().display_offset() as i32;
        let hist = g
            .term
            .grid()
            .total_lines()
            .saturating_sub(g.term.grid().screen_lines()) as i32;
        let direction = if reverse { Direction::Left } else { Direction::Right };

        // Origin priority:
        //  1. An explicit ABSOLUTE position from the caller. This is the normal
        //     path for stepping (Enter / F3) and for the typing anchor.
        //     Absolute rows are used rather than the stored focus below because
        //     a Point is expressed in grid space, where Line(0) is the top of
        //     the *current* screen: every line of new output shifts the content
        //     under a stored Point one row further down. With output streaming,
        //     a focus captured a moment ago therefore drifts towards the bottom
        //     of the buffer, and stepping restarts from there. Absolute rows do
        //     not move while history grows, so the caller can hand back exactly
        //     the position it was given.
        //     With origin_col the origin is stepped one cell in the direction of
        //     travel, so the match we are standing on is not returned again;
        //     without it the row edge is used, which keeps a match on that very
        //     row reachable (what the typing anchor wants).
        //  2. Just past the current focus, when the caller tracks no position.
        //  3. The visible edge, for the very first jump.
        let origin = match origin_row {
            Some(r) => {
                let total = hist + rows;
                let r = r.clamp(0, (total - 1).max(0));
                let line = Line(r - hist);
                match origin_col {
                    Some(c) => {
                        let p = Point::new(line, Column(c.min(cols - 1)));
                        if reverse {
                            p.sub(&g.term, Boundary::None, 1)
                        } else {
                            p.add(&g.term, Boundary::None, 1)
                        }
                    }
                    None => {
                        if reverse {
                            Point::new(line, Column(cols - 1))
                        } else {
                            Point::new(line, Column(0))
                        }
                    }
                }
            }
            None => match *self.search_focus.borrow() {
                Some((fr, fc)) => {
                    // Stored absolute -> current grid space, then one cell on.
                    let p = Point::new(Line(fr - hist), Column(fc.min(cols - 1)));
                    if reverse {
                        p.sub(&g.term, Boundary::None, 1)
                    } else {
                        p.add(&g.term, Boundary::None, 1)
                    }
                }
                None => {
                    if reverse {
                        Point::new(Line(rows - 1 - disp), Column(cols - 1))
                    } else {
                        Point::new(Line(-disp), Column(0))
                    }
                }
            },
        };

        let mut m = g.term.search_next(regex, origin, direction, Side::Left, None);
        if m.is_none() {
            // wrap around: restart from the far end of the whole buffer
            let wrap = if reverse {
                Point::new(Line(rows - 1), Column(cols - 1))
            } else {
                Point::new(Line(-hist), Column(0))
            };
            m = g.term.search_next(regex, wrap, direction, Side::Left, None);
        }

        let m = match m {
            Some(m) => m,
            None => return None,
        };
        let start = *m.start();
        // Line(-hist) is absolute row 0, so shifting by hist converts the grid
        // line into the absolute row space the Python side scrolls in.
        let abs = (start.line.0 + hist, start.column.0);
        *self.search_focus.borrow_mut() = Some(abs);
        Some(abs)
    }

    /// True when the app has enabled DECCKM (application cursor keys). In that
    /// mode arrows must be sent as ESC O A.. instead of ESC [ A.. (curses apps
    /// like mc/vim rely on this).
    fn app_cursor(&self) -> bool {
        let inner = self.inner.lock().unwrap();
        inner.term.mode().contains(TermMode::APP_CURSOR)
    }

    /// True when the app has enabled bracketed paste mode (DECSET ?2004).
    /// Pastes should be wrapped in ESC[200~ .. ESC[201~ ONLY in this case;
    /// otherwise those markers leak to the child as literal input (e.g. the
    /// trailing '~' shows up around pasted text while a command is running and
    /// readline's bracketed paste is inactive).
    fn bracketed_paste(&self) -> bool {
        let inner = self.inner.lock().unwrap();
        inner.term.mode().contains(TermMode::BRACKETED_PASTE)
    }

    /// True when the cursor should be drawn, i.e. the app has NOT hidden it
    /// with DECTCEM (`ESC[?25l`).  Full-screen curses apps (mc, vim, less)
    /// hide the cursor while they repaint and park it in an arbitrary cell;
    /// without honouring this a stray block is painted over their output.
    /// Note this is the *mode* only — `cursor()` separately reports whether
    /// the cursor falls inside the current viewport.
    fn cursor_visible(&self) -> bool {
        let inner = self.inner.lock().unwrap();
        inner.term.mode().contains(TermMode::SHOW_CURSOR)
    }

    /// Resize the grid (alacritty reflows scrollback for us) AND inform the
    /// child via the PTY winsize. rows/cols are updated so subsequent snapshots
    /// use the new dimensions.
    fn resize(&mut self, rows: usize, cols: usize) -> PyResult<()> {
        let size = Size {
            cols,
            screen_lines: rows,
            history: 10_000,
        };
        {
            let mut g = self.inner.lock().unwrap();
            g.term.resize(size);
        }
        self.rows = rows;
        self.cols = cols;
        self.master
            .resize(PtySize {
                rows: rows as u16,
                cols: cols as u16,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| PyRuntimeError::new_err(format!("pty resize: {e}")))?;
        Ok(())
    }

    // ----- Mouse text selection (alacritty's Selection, scrollback-aware) -----

    /// Begin a selection at viewport cell (col,row). `mode`: "simple" | "word"
    /// (semantic) | "line" | "block". `side_right` = pointer is on the right
    /// half of the cell (affects which cell the edge snaps to).
    fn selection_start(&self, col: usize, row: i32, side_right: bool, mode: &str) {
        let mut g = self.inner.lock().unwrap();
        let disp = g.term.grid().display_offset() as i32;
        let ty = match mode {
            "word" | "semantic" => SelectionType::Semantic,
            "line" | "lines" => SelectionType::Lines,
            "block" => SelectionType::Block,
            _ => SelectionType::Simple,
        };
        let side = if side_right { Side::Right } else { Side::Left };
        // viewport row -> absolute grid line (negative = scrollback)
        let point = Point::new(Line(row - disp), Column(col));
        g.term.selection = Some(Selection::new(ty, point, side));
    }

    /// Extend the in-progress selection to viewport cell (col,row).
    fn selection_update(&self, col: usize, row: i32, side_right: bool) {
        let mut g = self.inner.lock().unwrap();
        let disp = g.term.grid().display_offset() as i32;
        let side = if side_right { Side::Right } else { Side::Left };
        let point = Point::new(Line(row - disp), Column(col));
        if let Some(sel) = g.term.selection.as_mut() {
            sel.update(point, side);
        }
    }

    /// Drop any active selection.
    fn selection_clear(&self) {
        let mut g = self.inner.lock().unwrap();
        g.term.selection = None;
    }

    /// Text of the current selection (empty if none).
    fn selection_text(&self) -> String {
        let g = self.inner.lock().unwrap();
        g.term.selection_to_string().unwrap_or_default()
    }

    /// Per-visible-row selected column spans: (visible_row, start_col, end_col)
    /// inclusive. Empty if nothing is selected or it's fully scrolled away.
    /// The host draws these as highlight rectangles (like search).
    fn selection_spans(&self) -> Vec<(i32, usize, usize)> {
        let g = self.inner.lock().unwrap();
        let disp = g.term.grid().display_offset() as i32;
        let rows = self.rows as i32;
        let last_col = self.cols.saturating_sub(1);
        let mut out = Vec::new();
        // Clone the (cheap) Selection so the borrow of `selection` doesn't
        // overlap the `&g.term` borrow that to_range needs.
        let sel = g.term.selection.clone();
        if let Some(range) = sel.and_then(|s| s.to_range(&g.term)) {
            let is_block = range.is_block;
            let s_line = range.start.line.0;
            let e_line = range.end.line.0;
            let s_col = range.start.column.0;
            let e_col = range.end.column.0;
            for r in 0..rows {
                let l = r - disp; // absolute grid line for this visible row
                if l < s_line || l > e_line {
                    continue;
                }
                let (cs, ce) = if is_block {
                    (s_col, e_col)
                } else {
                    let cs = if l == s_line { s_col } else { 0 };
                    let ce = if l == e_line { e_col } else { last_col };
                    (cs, ce)
                };
                if cs <= ce {
                    out.push((r, cs, ce));
                }
            }
        }
        out
    }

    // ----- Mouse reporting to the child (xterm protocols) -----

    /// Bitmask of the terminal's active mouse modes so the host knows whether to
    /// forward events to the app instead of selecting:
    ///   bit0 = report click (1000), bit1 = button-drag (1002),
    ///   bit2 = any-motion (1003), bit3 = SGR encoding (1006),
    ///   bit4 = alternate scroll (1007).
    fn mouse_mode(&self) -> u8 {
        let g = self.inner.lock().unwrap();
        let m = g.term.mode();
        let mut v = 0u8;
        if m.contains(TermMode::MOUSE_REPORT_CLICK) {
            v |= 1;
        }
        if m.contains(TermMode::MOUSE_DRAG) {
            v |= 2;
        }
        if m.contains(TermMode::MOUSE_MOTION) {
            v |= 4;
        }
        if m.contains(TermMode::SGR_MOUSE) {
            v |= 8;
        }
        if m.contains(TermMode::ALTERNATE_SCROLL) {
            v |= 16;
        }
        v
    }

    /// Encode a mouse event for the child. Returns the raw bytes to feed to the
    /// PTY, or empty if no mouse mode is active.
    ///   col,row : 0-based viewport cell
    ///   button  : 0=left 1=middle 2=right ; 64=wheel-up 65=wheel-down
    ///   action  : 0=press 1=release 2=motion
    ///   mods    : bit0 shift, bit1 alt/meta, bit2 ctrl
    fn mouse_report(&self, col: usize, row: i32, button: u8, action: u8, mods: u8) -> Vec<u8> {
        let g = self.inner.lock().unwrap();
        let m = g.term.mode();
        let report = m.contains(TermMode::MOUSE_REPORT_CLICK)
            || m.contains(TermMode::MOUSE_DRAG)
            || m.contains(TermMode::MOUSE_MOTION);
        if !report {
            return Vec::new();
        }
        let sgr = m.contains(TermMode::SGR_MOUSE);
        drop(g);

        // Build the button-state byte (without the +32 printable offset).
        let mut code: u32 = button as u32;
        if mods & 1 != 0 {
            code += 4; // shift
        }
        if mods & 2 != 0 {
            code += 8; // meta/alt
        }
        if mods & 4 != 0 {
            code += 16; // ctrl
        }
        if action == 2 {
            code += 32; // motion flag
        }

        let cx = col as u32 + 1; // protocol is 1-based
        let cy = (row.max(0) as u32) + 1;

        if sgr {
            let final_byte = if action == 1 { 'm' } else { 'M' };
            format!("\x1b[<{};{};{}{}", code, cx, cy, final_byte).into_bytes()
        } else {
            // Legacy X10 encoding: release is button 3 regardless of which
            // button; coordinates clamp at 223 (255 - 32).
            if action == 1 {
                code = (code & !0b11) | 0b11;
            }
            let pcode = (32 + code).min(255) as u8;
            let px = (32 + cx).min(255) as u8;
            let py = (32 + cy).min(255) as u8;
            vec![0x1b, b'[', b'M', pcode, px, py]
        }
    }

    /// Tell the PTY about a new size (Slice-1 keeps the grid fixed; this only
    /// informs the child so it doesn't get a stale winsize). Real reflow is
    /// Slice-2.
    fn notify_pty_size(&self, rows: usize, cols: usize) -> PyResult<()> {
        self.master
            .resize(PtySize {
                rows: rows as u16,
                cols: cols as u16,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| PyRuntimeError::new_err(format!("pty resize: {e}")))?;
        Ok(())
    }
}

#[pymodule]
fn cashterm_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PtyTerm>()?;
    Ok(())
}
