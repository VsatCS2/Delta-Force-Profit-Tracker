"""
Delta Force GUI — small reusable tkinter widgets.

Split out of delta_force_gui.py: the gradient background, rounded "Card"
container, sidebar NavItem (with hand-drawn vector icons, not font
glyphs — see _draw_nav_icon), a debounce helper, and the sortable-
Treeview-header helper. None of this knows about match data; it only
knows how to draw itself given a palette dict from delta_force_theme.
"""

import math
import tkinter as tk


# =====================================================================
# Debounce helper (used for the matches search box)
# =====================================================================
class _Debouncer:
    def __init__(self, widget, delay_ms, callback):
        self._widget = widget
        self._delay = delay_ms
        self._cb = callback
        self._after_id = None

    def trigger(self, *_args):
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self._after_id = self._widget.after(self._delay, self._fire)

    def _fire(self):
        self._after_id = None
        self._cb()

    def cancel(self):
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None


# =====================================================================
# Mousewheel scrolling for a scrollable Canvas
# =====================================================================
def bind_mousewheel_to_canvas(canvas):
    """Lets the mouse wheel scroll a canvas while the pointer is over it,
    without permanently stealing wheel events from the rest of the app.
    Binds on <Enter> and unbinds on <Leave> rather than binding once and
    leaving it - Tkinter's bind_all wheel binding is global to the whole
    application, so a permanent binding would scroll whichever canvas
    last called this, regardless of where the mouse actually is.

    Canvas is the one Tk scrollable container that needs this manually -
    Treeview, Listbox, and Text already get mousewheel scrolling for
    free from Tk's own default bindings.
    """
    def _on_wheel(event):
        canvas.yview_scroll(
            -1 * (event.delta // 120 or (1 if event.delta > 0 else -1)), "units")
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))


# =====================================================================
# Sortable Treeview headers
# =====================================================================
def make_sortable(tree, columns, on_sort,
                  initial_col=None, initial_reverse=True):
    state = {"col": initial_col, "reverse": initial_reverse}

    def apply_arrows():
        for cid, label in columns:
            arrow = ""
            if cid == state["col"]:
                arrow = "  ▼" if state["reverse"] else "  ▲"
            tree.heading(cid, text=label + arrow)

    def header_click(col_id):
        if state["col"] == col_id:
            state["reverse"] = not state["reverse"]
        else:
            state["col"] = col_id
            state["reverse"] = True
        apply_arrows()
        on_sort(state["col"], state["reverse"])

    for cid, label in columns:
        tree.heading(cid, text=label,
                     command=lambda c=cid: header_click(c))
    apply_arrows()


# =====================================================================
# Color + shape helpers
# =====================================================================
def _hex_blend(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kwargs)


class GradientFrame(tk.Canvas):
    def __init__(self, master, color_top, color_bottom, **kwargs):
        super().__init__(master, highlightthickness=0, bd=0, **kwargs)
        self._top = color_top
        self._bottom = color_bottom
        self.bind("<Configure>", self._redraw)
        self.body = tk.Frame(self, bg=color_top)

    def _redraw(self, _event=None):
        self.delete("grad")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return
        steps = 48
        band_h = h / steps
        for i in range(steps):
            t = i / (steps - 1) if steps > 1 else 0
            color = _hex_blend(self._top, self._bottom, t)
            y0 = i * band_h
            self.create_rectangle(0, y0, w, y0 + band_h + 1,
                                  fill=color, outline="", tags="grad")
        self.create_window(0, 0, window=self.body, anchor="nw",
                           width=w, height=h)


class ToggleSwitch(tk.Canvas):
    """A pill-shaped on/off switch, matching how `tk.Checkbutton` is
    already used everywhere in this app: pass a BooleanVar and an
    optional command, and it stays in sync the same way a real
    Checkbutton would (including external var.set() calls updating the
    drawn state, via the var's own trace).

    Deliberately NOT the reference UI's card-corner placement - this app
    uses row-based settings (checkbox-then-label), so this widget is
    just the switch itself; callers place a Label next to it the same
    way they already place one next to a Checkbutton, preserving the
    existing reading order instead of restyling the whole layout.
    """
    WIDTH, HEIGHT = 40, 22

    def __init__(self, master, palette, variable, command=None, **kwargs):
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT,
                         highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", master.cget("bg")), **kwargs)
        self.palette = palette
        self.var = variable
        self.command = command
        self._hover = False
        self._enabled = True
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._trace_id = self.var.trace_add("write", lambda *a: self._redraw())
        self.bind("<Destroy>", self._on_destroy)
        self._redraw()

    def set_state(self, state: str):
        """"normal" or "disabled" - same two values and the same method
        name _pill_button's returned widget already uses elsewhere in
        this app, for the same reason: a plain tk widget has no built-in
        concept of "disabled" that a hand-drawn Canvas widget inherits
        for free, so this app's custom widgets all expose their own
        small, consistent state API instead of the real tk `state=`
        option (which doesn't apply to Canvas in a way that would help
        here anyway)."""
        self._enabled = (state != "disabled")
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._redraw()

    def _on_destroy(self, _event=None):
        try:
            self.var.trace_remove("write", self._trace_id)
        except (tk.TclError, ValueError):
            pass  # var already gone, or trace already cleared - either way, nothing left to clean up

    def _set_hover(self, on: bool):
        self._hover = on
        self._redraw()

    def _on_click(self, _event=None):
        if not self._enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _redraw(self):
        self.delete("all")
        c = self.palette
        on = bool(self.var.get())
        if not self._enabled:
            track = c["SURFACE_ALT"]
        else:
            track = c["ACCENT"] if on else c["SURFACE_ACTIVE"]
            if self._hover:
                track = c["ACCENT_HI"] if on else c["SURFACE_HOVER"]
        h = self.HEIGHT
        round_rect(self, 1, 1, self.WIDTH - 1, h - 1, h / 2 - 1,
                  fill=track, outline="")
        knob_d = h - 8
        knob_x = (self.WIDTH - 4 - knob_d) if on else 4
        knob_color = c["FG_MUTED"] if not self._enabled else (c["FG"] if on else c["FG_DIM"])
        self.create_oval(knob_x, 4, knob_x + knob_d, 4 + knob_d,
                         fill=knob_color, outline="")


class InfoIcon(tk.Canvas):
    """A small "i" glyph that shows an explanatory tooltip on hover -
    for the handful of numbers in this app whose meaning genuinely isn't
    obvious from their label alone (see delta_force_gui.py's
    _info_icon() for the specific, researched cases this is actually
    used for; this class only knows how to draw itself and show
    whatever text it's given).

    Deliberately muted and small (14px) rather than a bright attention-
    grabbing icon - the point is an available-if-wanted affordance next
    to the few numbers that need one, not a UI element that competes
    for attention with the number it's explaining.
    """
    SIZE = 14

    def __init__(self, master, palette, text: str, **kwargs):
        super().__init__(master, width=self.SIZE, height=self.SIZE,
                         highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", master.cget("bg")), **kwargs)
        self.palette = palette
        self.text = text
        self._tooltip = None
        self.configure(cursor="hand2")
        self._draw(hover=False)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Destroy>", lambda e: self._hide())

    def _draw(self, hover: bool):
        self.delete("all")
        c = self.palette
        color = c["FG_DIM"] if hover else c["FG_MUTED"]
        r = self.SIZE / 2
        self.create_oval(1, 1, self.SIZE - 1, self.SIZE - 1,
                         outline=color, width=1.2)
        self.create_text(r, r - 1, text="i", fill=color,
                         font=("Georgia", 9, "italic"))

    def _on_enter(self, _event=None):
        self._draw(hover=True)
        self._show()

    def _on_leave(self, _event=None):
        self._draw(hover=False)
        self._hide()

    def _show(self):
        if self._tooltip is not None:
            return
        try:
            c = self.palette
            win = tk.Toplevel(self.winfo_toplevel())
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=c["BORDER_SOFT"])
            inner = tk.Frame(win, bg=c["SURFACE"])
            inner.pack(padx=1, pady=1)
            tk.Label(inner, text=self.text, bg=c["SURFACE"], fg=c["FG_DIM"],
                     font=("Segoe UI", 9), justify="left", wraplength=260,
                     padx=10, pady=7).pack()
            win.update_idletasks()
            x = self.winfo_rootx() + self.SIZE + 6
            y = self.winfo_rooty() - (win.winfo_reqheight() // 2) + (self.SIZE // 2)
            win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
            self._tooltip = win
        except tk.TclError:
            self._tooltip = None

    def _hide(self):
        if self._tooltip is not None:
            try:
                self._tooltip.destroy()
            except tk.TclError:
                pass
            self._tooltip = None


class Card(tk.Frame):
    def __init__(self, master, palette, padding=(22, 20), radius=14,
                 shadow=True, border=True):
        super().__init__(master, bg=palette["BG_TOP"], bd=0, highlightthickness=0)
        self._pal = palette
        self._radius = radius
        self._shadow = shadow
        self._border = border

        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                 bg=palette["BG_TOP"])
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        inset = max(5, int(radius * 0.4))
        self.body = tk.Frame(self, bg=palette["SURFACE"],
                             padx=padding[0], pady=padding[1])
        self.body.pack(fill="both", expand=True, padx=inset, pady=inset)

        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        c = self._canvas
        c.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 6 or h <= 6:
            return
        pal = self._pal
        r = self._radius

        if self._shadow:
            round_rect(c, 2, 5, w - 2, h - 1, r,
                       fill=pal["SHADOW"], outline="")

        outline = pal["BORDER_SOFT"] if self._border else ""
        round_rect(c, 2, 2, w - 2, h - 3, r,
                   fill=pal["SURFACE"], outline=outline, width=1)


# =====================================================================
# Sidebar nav icons — hand-drawn vector shapes, not font glyphs.
#
# Font-rendered symbols (▦ ◍ ◎ ≡ ⚙) look inconsistent across platforms:
# some fall back to a different font at a different size/baseline than
# the rest (the gear "⚙" is the usual offender — it visibly renders
# larger and sits off the baseline vs. its neighbors). Drawing each icon
# ourselves inside a fixed box guarantees every icon in the list is the
# same size and sits on the same baseline, regardless of what fonts are
# installed.
# =====================================================================
def _draw_nav_icon(canvas: tk.Canvas, key: str, cx: float, cy: float,
                   size: float, color: str) -> None:
    s = size

    if key == "overview":
        # Ascending bar-chart glyph.
        bw = s * 0.46
        heights = (s * 0.55, s * 1.05, s * 0.8)
        xs = (cx - s * 1.05, cx, cx + s * 1.05)
        base = cy + s * 0.85
        for x, h in zip(xs, heights):
            canvas.create_rectangle(x - bw / 2, base - h, x + bw / 2, base,
                                    fill=color, outline="")

    elif key == "profile":
        # Head + shoulders.
        r = s * 0.42
        canvas.create_oval(cx - r, cy - s * 0.95, cx + r,
                           cy - s * 0.95 + 2 * r, fill=color, outline="")
        canvas.create_arc(cx - s * 1.05, cy - s * 0.05, cx + s * 1.05,
                          cy + s * 1.55, start=20, extent=140,
                          style="chord", fill=color, outline="")

    elif key == "maps":
        # Compass: outlined ring + a small diamond needle.
        r = s * 0.95
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=color, width=2)
        nr = s * 0.42
        canvas.create_polygon(cx, cy - nr, cx + nr * 0.5, cy,
                              cx, cy + nr, cx - nr * 0.5, cy,
                              fill=color, outline="")

    elif key == "matches":
        # List: three left-aligned bars, decreasing length.
        widths = (s * 1.7, s * 1.7, s * 1.05)
        y0 = cy - s * 0.65
        for i, wd in enumerate(widths):
            y = y0 + i * s * 0.65
            canvas.create_line(cx - s * 1.05, y, cx - s * 1.05 + wd, y,
                               fill=color, width=2.2, capstyle="round")

    elif key == "settings":
        # Sliders: two horizontal tracks with an offset knob each —
        # reads as "settings" without needing a punched-out gear hole,
        # and is built from the exact same primitives (lines + a dot)
        # as the "matches" icon, so it lines up at an identical size.
        track_w = s * 1.9
        y1 = cy - s * 0.55
        y2 = cy + s * 0.55
        x0 = cx - track_w / 2
        x1 = cx + track_w / 2
        canvas.create_line(x0, y1, x1, y1, fill=color, width=2,
                           capstyle="round")
        canvas.create_line(x0, y2, x1, y2, fill=color, width=2,
                           capstyle="round")
        knob_r = s * 0.24
        knob1_x = cx - track_w * 0.18
        knob2_x = cx + track_w * 0.22
        canvas.create_oval(knob1_x - knob_r, y1 - knob_r,
                           knob1_x + knob_r, y1 + knob_r,
                           fill=color, outline="")
        canvas.create_oval(knob2_x - knob_r, y2 - knob_r,
                           knob2_x + knob_r, y2 + knob_r,
                           fill=color, outline="")

    elif key == "community":
        # Two overlapping "person" dots - a simple people/group glyph.
        # Smaller circle drawn first so the larger one overlaps it, the
        # usual visual convention for a two-person icon.
        r_back, r_front = s * 0.42, s * 0.55
        cx_back, cx_front = cx + s * 0.32, cx - s * 0.22
        canvas.create_oval(cx_back - r_back, cy - r_back,
                           cx_back + r_back, cy + r_back,
                           fill=color, outline="")
        canvas.create_oval(cx_front - r_front, cy - r_front,
                           cx_front + r_front, cy + r_front,
                           fill=color, outline="")

    # ---- Settings-card header badges (see delta_force_gui.py's
    # section_title) - same primitives/stroke-width conventions as the
    # nav icons above, just a wider vocabulary since there are more
    # distinct card concepts than nav destinations. "profile_match"
    # deliberately isn't here - that card reuses the "profile" icon
    # above directly, since it's the same underlying concept (profile
    # data), not a new glyph.
    elif key == "auto_refresh":
        # A single incomplete ring with one arrowhead at its leading
        # edge - the standard single-arrow refresh glyph. A two-arrow
        # "chasing" version was tried first and rendered as a near-solid
        # blob at real badge size (tested at 40/80/160px) - this one
        # holds up much better small.
        import math
        r = s * 0.9
        start_deg = 35
        canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                          start=start_deg, extent=280, style="arc",
                          outline=color, width=2.3)
        ang = math.radians(start_deg)
        tip_x = cx + r * math.cos(ang)
        tip_y = cy - r * math.sin(ang)
        # arrowhead tangent to the circle at that point, pointing in the
        # arc's direction of travel
        tang = math.radians(start_deg + 90)
        dx, dy = math.cos(tang), -math.sin(tang)
        px, py = -dy, dx
        head = s * 0.42
        canvas.create_polygon(
            tip_x + dx * head, tip_y + dy * head,
            tip_x - dx * head * 0.35 + px * head * 0.55,
            tip_y - dy * head * 0.35 + py * head * 0.55,
            tip_x - dx * head * 0.35 - px * head * 0.55,
            tip_y - dy * head * 0.35 - py * head * 0.55,
            fill=color, outline="")

    elif key == "notifications":
        # Bell: a rounded triangle-ish body plus a small base line and clapper dot.
        canvas.create_arc(cx - s * 0.8, cy - s * 0.9, cx + s * 0.8, cy + s * 0.5,
                          start=20, extent=140, style="chord",
                          fill=color, outline="")
        canvas.create_line(cx - s * 0.95, cy + s * 0.5, cx + s * 0.95, cy + s * 0.5,
                           fill=color, width=2, capstyle="round")
        canvas.create_oval(cx - s * 0.18, cy + s * 0.75, cx + s * 0.18, cy + s * 1.1,
                           fill=color, outline="")

    elif key == "discord":
        # A speech bubble, not the Discord brand mark itself (that's a
        # trademarked logo) - this card is about broadcasting your
        # status, which a generic chat bubble conveys without
        # reproducing anyone's actual logo.
        canvas.create_oval(cx - s * 1.0, cy - s * 0.85, cx + s * 1.0, cy + s * 0.55,
                           fill=color, outline="")
        canvas.create_polygon(
            cx - s * 0.5, cy + s * 0.45, cx - s * 0.15, cy + s * 0.45,
            cx - s * 0.55, cy + s * 1.05, fill=color, outline="")

    elif key == "overlay":
        # A window frame with a smaller window overlapping its corner -
        # reads as "something floating on top of something else".
        canvas.create_rectangle(cx - s * 1.05, cy - s * 0.75,
                                cx + s * 0.55, cy + s * 0.75,
                                outline=color, width=1.8)
        canvas.create_rectangle(cx - s * 0.15, cy - s * 0.15,
                                cx + s * 1.05, cy + s * 0.85,
                                fill=color, outline=color, width=1.8)

    elif key == "startup":
        # The standard power-button glyph: a ring with a gap at the top,
        # broken by a vertical line through it.
        r = s * 0.85
        canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                          start=55, extent=250, style="arc",
                          outline=color, width=2.2)
        canvas.create_line(cx, cy - r * 1.15, cx, cy - r * 0.1,
                           fill=color, width=2.2, capstyle="round")

    elif key == "updates":
        # Download glyph: an arrow shaft pointing down into a tray.
        canvas.create_line(cx, cy - s * 1.0, cx, cy + s * 0.3,
                           fill=color, width=2.2, capstyle="round")
        canvas.create_polygon(
            cx - s * 0.5, cy - s * 0.15, cx + s * 0.5, cy - s * 0.15,
            cx, cy + s * 0.45, fill=color, outline="")
        canvas.create_line(cx - s * 0.95, cy + s * 0.95, cx + s * 0.95, cy + s * 0.95,
                           fill=color, width=2, capstyle="round")

    elif key == "about_legal":
        # A simple page: a rectangle with two short text-line strokes -
        # deliberately not a circled "i" (that's InfoIcon's glyph
        # elsewhere in this app, for a different purpose - a tooltip
        # trigger, not a section header - and reusing it here would
        # blur that distinction).
        canvas.create_rectangle(cx - s * 0.7, cy - s * 1.0, cx + s * 0.7, cy + s * 1.0,
                                outline=color, width=1.8)
        canvas.create_line(cx - s * 0.4, cy - s * 0.35, cx + s * 0.4, cy - s * 0.35,
                           fill=color, width=1.6, capstyle="round")
        canvas.create_line(cx - s * 0.4, cy + s * 0.1, cx + s * 0.4, cy + s * 0.1,
                           fill=color, width=1.6, capstyle="round")
        canvas.create_line(cx - s * 0.4, cy + s * 0.55, cx + s * 0.05, cy + s * 0.55,
                           fill=color, width=1.6, capstyle="round")

    elif key == "data_export":
        # Same tray primitive as "updates", arrow pointing the opposite
        # way (up, out of the tray) - export/save, not download.
        canvas.create_line(cx, cy + s * 0.3, cx, cy - s * 1.0,
                           fill=color, width=2.2, capstyle="round")
        canvas.create_polygon(
            cx - s * 0.5, cy - s * 0.15, cx + s * 0.5, cy - s * 0.15,
            cx, cy - s * 0.75, fill=color, outline="")
        canvas.create_line(cx - s * 0.95, cy + s * 0.95, cx + s * 0.95, cy + s * 0.95,
                           fill=color, width=2, capstyle="round")


def icon_badge(parent, palette, icon_key: str, size: int = 30) -> tk.Canvas:
    """A small rounded-square badge with one of _draw_nav_icon's glyphs
    centered in it - the card-header icon treatment from the reference
    UI (a muted icon in a soft rounded square, sitting to the left of
    a card's title), built from the exact same icon vocabulary the nav
    rail already uses rather than a second icon system."""
    cvs = tk.Canvas(parent, width=size, height=size,
                    highlightthickness=0, bd=0, bg=parent.cget("bg"))
    round_rect(cvs, 1, 1, size - 1, size - 1, size * 0.28,
              fill=palette["SURFACE_ALT"], outline="")
    _draw_nav_icon(cvs, icon_key, size / 2, size / 2, size * 0.24,
                   palette["FG_DIM"])
    return cvs


def status_pill(parent, palette, text: str, kind: str = "neutral") -> tk.Canvas:
    """A small rounded status chip - text on a softly-tinted background,
    matching the reference UI's compact status indicators ("Supported",
    "1 WARNING") rather than this app's usual full-sentence status
    labels. kind picks the color pairing:
        "good"    - POSITIVE/ACCENT_SOFT  (connected, supported, active)
        "warning" - WARNING/WARNING_SOFT  (needs attention, but not broken)
        "bad"     - NEGATIVE (text) on a dim SURFACE_ALT fill (blocked, unavailable)
        "neutral" - FG_DIM on SURFACE_ALT (inactive, informational)
    Sized to its own text (not a fixed width), since chip length varies
    a lot ("On" vs "Not Installed").
    """
    fg_by_kind = {
        "good": palette["POSITIVE"], "warning": palette["WARNING"],
        "bad": palette["NEGATIVE"], "neutral": palette["FG_DIM"],
    }
    bg_by_kind = {
        "good": palette["ACCENT_SOFT"], "warning": palette["WARNING_SOFT"],
        "bad": palette["SURFACE_ALT"], "neutral": palette["SURFACE_ALT"],
    }
    fg = fg_by_kind.get(kind, palette["FG_DIM"])
    bg = bg_by_kind.get(kind, palette["SURFACE_ALT"])

    probe = tk.Label(parent, text=text, font=("Segoe UI", 8, "bold"))
    probe.update_idletasks()
    text_w = probe.winfo_reqwidth()
    text_h = probe.winfo_reqheight()
    probe.destroy()

    pad_x, pad_y = 9, 5
    w, h = text_w + pad_x * 2, text_h + pad_y * 2
    cvs = tk.Canvas(parent, width=w, height=h, highlightthickness=0, bd=0,
                    bg=parent.cget("bg"))
    round_rect(cvs, 0, 0, w, h, h / 2, fill=bg, outline="")
    cvs.create_text(w / 2, h / 2, text=text, fill=fg,
                    font=("Segoe UI", 8, "bold"))
    return cvs


class NavItem(tk.Canvas):
    ICON_CX = 26
    ICON_SIZE = 7
    LABEL_X = 52

    def __init__(self, master, palette, icon_key, label, command,
                 height=46):
        super().__init__(master, height=height,
                         bg=palette["SURFACE"],
                         highlightthickness=0, bd=0)
        self._pal = palette
        self._icon_key = icon_key
        self._label = label
        self._command = command
        self._active = False
        self._hover = False

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", lambda e: self._command())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _):
        self._hover = True
        self.configure(cursor="hand2")
        self._redraw()

    def _leave(self, _):
        self._hover = False
        self._redraw()

    def set_active(self, active):
        self._active = active
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 4 or h <= 4:
            return

        pal = self._pal

        if self._active:
            bg = pal["SURFACE_ACTIVE"]
            fg = pal["FG"]
            icon_color = pal["ACCENT"]
            label_font = ("Segoe UI Semibold", 11)
        elif self._hover:
            bg = pal["SURFACE_HOVER"]
            fg = pal["FG"]
            icon_color = pal["FG_DIM"]
            label_font = ("Segoe UI", 11)
        else:
            bg = None
            fg = pal["FG_DIM"]
            icon_color = pal["FG_MUTED"]
            label_font = ("Segoe UI", 11)

        if bg:
            round_rect(self, 4, 4, w - 4, h - 4, 10, fill=bg, outline="")

        if self._active:
            round_rect(self, 4, h // 2 - 10, 7, h // 2 + 10, 2,
                       fill=pal["ACCENT"], outline="")

        _draw_nav_icon(self, self._icon_key, self.ICON_CX, h / 2,
                       self.ICON_SIZE, icon_color)
        self.create_text(self.LABEL_X, h / 2, text=self._label,
                         fill=fg, font=label_font, anchor="w")
