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
