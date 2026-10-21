"""
Richer themes for DF Tracker.

What changed compared to the old two-colour themes
--------------------------------------------------
* Every theme is a full tonal palette (page, cards, raised cards, hover,
  active, borders) plus an accent PAIR (ACCENT -> ACCENT_HI) so buttons can
  blend two colours instead of being flat.
* A banner across the top of the window is painted with generated artwork
  (blended colour glows, a light pattern, fine grain so gradients don't
  band) - or with the user's own picture.
* Buttons are rounded and gradient-filled.
* Themes are picked from a row of colour swatches in the banner.

Tk widgets cannot be transparent, so a picture cannot sit *behind* the cards.
The artwork therefore lives in the banner, which fades into the page colour
at its lower edge so the join is invisible.

Everything that needs Pillow degrades gracefully: without it the banner is a
plain multi-stop gradient and buttons are flat.
"""
import tkinter as tk
import tkinter.font as tkfont

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
    HAVE_PIL = True
except ImportError:                     # pragma: no cover - exercised via flag
    HAVE_PIL = False


# ======================================================================
# Colour helpers
# ======================================================================
def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def blend(a, b, t):
    """a -> b by t (0..1), as hex."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    return _hex((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


def luminance(h):
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ======================================================================
# Theme definitions
# ======================================================================
# art: stops      - horizontal colour ramp [(position, hex), ...]
#      glows      - soft colour pools [(x 0..1, y 0..1, radius px, hex, alpha)]
#      pattern    - "dots" | "diagonal" | "contour" | "none"
#      pattern_alpha - how visible the pattern is
_SPECS = {
    "Midnight": dict(
        BG_TOP="#0b1424", BG_BOTTOM="#0e192d",
        SURFACE="#142239", SURFACE_ALT="#1b2d4a",
        SURFACE_HOVER="#223759", SURFACE_ACTIVE="#28406a",
        BORDER="#2b4166", BORDER_SOFT="#213352",
        FG="#e8eef9", FG_MUTED="#9aaccc", FG_DIM="#8196b9",
        ACCENT="#4fd196", ACCENT_HI="#78e6b2", ACCENT_SOFT="#1a4a44",
        ACCENT_FG="#07241a",
        POSITIVE="#58d68d", NEGATIVE="#ff6b7a",
        WARNING="#f5b94a", WARNING_SOFT="#4a3a1a",
        SELECT_BG="#1f4a63", SELECT_FG="#ffffff", SHADOW="#070d19",
        art=dict(stops=[(0, "#0b1424"), (0.55, "#10223a"), (1, "#13233f")],
                 glows=[(0.80, 0.05, 330, "#2e6cff", 0.30),
                        (0.60, 0.95, 260, "#27d3a4", 0.24),
                        (0.97, 0.85, 220, "#6a4cff", 0.20)],
                 pattern="dots", pattern_alpha=0.07)),
    "Obsidian": dict(
        BG_TOP="#0e0f12", BG_BOTTOM="#121317",
        SURFACE="#1a1c21", SURFACE_ALT="#23262d",
        SURFACE_HOVER="#2b2f38", SURFACE_ACTIVE="#343945",
        BORDER="#3a3f4b", BORDER_SOFT="#2a2e37",
        FG="#ececf0", FG_MUTED="#a1a6b3", FG_DIM="#8c92a4",
        ACCENT="#f2ac3d", ACCENT_HI="#ffc968", ACCENT_SOFT="#3b2d12",
        ACCENT_FG="#241700",
        POSITIVE="#62d08a", NEGATIVE="#ff6477",
        WARNING="#ffd166", WARNING_SOFT="#40351a",
        SELECT_BG="#3d3320", SELECT_FG="#ffffff", SHADOW="#07080a",
        art=dict(stops=[(0, "#0e0f12"), (0.6, "#17181d"), (1, "#1c1d23")],
                 glows=[(0.85, 0.0, 320, "#f2ac3d", 0.26),
                        (0.55, 1.0, 240, "#4c6a9c", 0.20),
                        (0.12, 0.9, 200, "#f2ac3d", 0.08)],
                 pattern="diagonal", pattern_alpha=0.06)),
    "Ember": dict(
        BG_TOP="#150b0d", BG_BOTTOM="#1a0e10",
        SURFACE="#231216", SURFACE_ALT="#2d181d",
        SURFACE_HOVER="#381f25", SURFACE_ACTIVE="#44262d",
        BORDER="#53313a", BORDER_SOFT="#3a2229",
        FG="#f6ebe8", FG_MUTED="#c4a6a1", FG_DIM="#a98a85",
        ACCENT="#ff8a3d", ACCENT_HI="#ffae73", ACCENT_SOFT="#4a2a17",
        ACCENT_FG="#2a1200",
        POSITIVE="#66d68e", NEGATIVE="#ff5468",
        WARNING="#ffc857", WARNING_SOFT="#4a3a1a",
        SELECT_BG="#5a3023", SELECT_FG="#ffffff", SHADOW="#0a0506",
        art=dict(stops=[(0, "#1a0b0e"), (0.55, "#2a1214"), (1, "#321518")],
                 glows=[(0.78, 0.05, 330, "#ff7a2f", 0.32),
                        (0.97, 0.9, 260, "#d1334f", 0.30),
                        (0.50, 1.0, 200, "#ffb347", 0.14)],
                 pattern="diagonal", pattern_alpha=0.05)),
    "Glacier": dict(
        BG_TOP="#0a161d", BG_BOTTOM="#0d1d27",
        SURFACE="#12262f", SURFACE_ALT="#19333f",
        SURFACE_HOVER="#20404e", SURFACE_ACTIVE="#274e5f",
        BORDER="#2c5666", BORDER_SOFT="#1f3f4c",
        FG="#e6f4f8", FG_MUTED="#94b6c2", FG_DIM="#7fa2af",
        ACCENT="#4cc9f0", ACCENT_HI="#86dcf7", ACCENT_SOFT="#12404f",
        ACCENT_FG="#04212b",
        POSITIVE="#5bd9a4", NEGATIVE="#ff6f7d",
        WARNING="#f4c25b", WARNING_SOFT="#40381a",
        SELECT_BG="#1d5163", SELECT_FG="#ffffff", SHADOW="#050d12",
        art=dict(stops=[(0, "#0a161d"), (0.5, "#0f2a36"), (1, "#0d3040")],
                 glows=[(0.75, 0.0, 340, "#27c7d9", 0.28),
                        (0.98, 0.7, 260, "#3a7bff", 0.24),
                        (0.45, 1.0, 220, "#5fe3c0", 0.18)],
                 pattern="contour", pattern_alpha=0.10)),
    "Forest": dict(
        BG_TOP="#0b120d", BG_BOTTOM="#0f1811",
        SURFACE="#162118", SURFACE_ALT="#1e2d22",
        SURFACE_HOVER="#263829", SURFACE_ACTIVE="#2e4531",
        BORDER="#3a5440", BORDER_SOFT="#283d2d",
        FG="#e9f0e4", FG_MUTED="#a2b59f", FG_DIM="#8aa087",
        ACCENT="#c8b45a", ACCENT_HI="#e3d283", ACCENT_SOFT="#3b3716",
        ACCENT_FG="#1f1a05",
        POSITIVE="#6fd68a", NEGATIVE="#ff6a5a",
        WARNING="#e8a64a", WARNING_SOFT="#40341a",
        SELECT_BG="#3b4a24", SELECT_FG="#ffffff", SHADOW="#060a07",
        art=dict(stops=[(0, "#0b120d"), (0.55, "#14221a"), (1, "#18281c")],
                 glows=[(0.80, 0.0, 320, "#c8b45a", 0.22),
                        (0.55, 1.0, 260, "#3f8a55", 0.28),
                        (0.97, 0.85, 220, "#7a8f3a", 0.20)],
                 pattern="contour", pattern_alpha=0.11)),
    "Dusk": dict(
        BG_TOP="#120d20", BG_BOTTOM="#170f29",
        SURFACE="#1f1735", SURFACE_ALT="#2a2046",
        SURFACE_HOVER="#342858", SURFACE_ACTIVE="#3f316b",
        BORDER="#4a3b7c", BORDER_SOFT="#352a5a",
        FG="#efeafb", FG_MUTED="#b2a6d4", FG_DIM="#9a8ec2",
        ACCENT="#a281ff", ACCENT_HI="#c2a8ff", ACCENT_SOFT="#392a6b",
        ACCENT_FG="#150a35",
        POSITIVE="#62d8a0", NEGATIVE="#ff6c8a",
        WARNING="#f5b95a", WARNING_SOFT="#463a22",
        SELECT_BG="#43337a", SELECT_FG="#ffffff", SHADOW="#08050f",
        art=dict(stops=[(0, "#120d20"), (0.55, "#241a42"), (1, "#2d1d4f")],
                 glows=[(0.78, 0.05, 330, "#a281ff", 0.32),
                        (0.97, 0.9, 260, "#ff5fb2", 0.26),
                        (0.50, 1.0, 220, "#4c6bff", 0.20)],
                 pattern="dots", pattern_alpha=0.07)),
    "Daylight": dict(
        BG_TOP="#e3e8ef", BG_BOTTOM="#dbe2ec",
        SURFACE="#f3f6fa", SURFACE_ALT="#e7ecf3",
        SURFACE_HOVER="#dde4ee", SURFACE_ACTIVE="#d2dbe8",
        BORDER="#b4c1d3", BORDER_SOFT="#d0d9e5",
        FG="#1b2638", FG_MUTED="#4d5b72", FG_DIM="#66748b",
        ACCENT="#16748a", ACCENT_HI="#1a86a0", ACCENT_SOFT="#cde7ee",
        ACCENT_FG="#ffffff",
        POSITIVE="#127a45", NEGATIVE="#c2243a",
        WARNING="#9a5800", WARNING_SOFT="#f5e3c0",
        SELECT_BG="#bcd8e6", SELECT_FG="#0c1a2b", SHADOW="#b9c3d1",
        art=dict(stops=[(0, "#e9eef5"), (0.5, "#dde6f1"), (1, "#e4e9f3")],
                 glows=[(0.80, 0.0, 330, "#8cb8ff", 0.40),
                        (0.55, 1.0, 260, "#7fd6c8", 0.34),
                        (0.97, 0.85, 220, "#ffd3a8", 0.36)],
                 pattern="none", pattern_alpha=0.0)),
}

DEFAULT_THEME = "Midnight"


def _build_themes():
    # Any colour key the rest of the app (or a module I can't see) reads but
    # these palettes don't define falls back to the app's original theme, so
    # nothing can raise KeyError.
    fallback = {}
    try:
        import delta_force_theme as legacy
        fallback = dict(legacy.THEMES.get(
            getattr(legacy, "DEFAULT_THEME", ""), next(iter(legacy.THEMES.values()))))
    except Exception:
        pass
    out = {}
    for name, spec in _SPECS.items():
        pal = dict(fallback)
        pal.update({k: v for k, v in spec.items() if k != "art"})
        pal["_art"] = spec["art"]
        pal["_name"] = name
        pal["_light"] = luminance(spec["BG_TOP"]) > 0.4
        out[name] = pal
    return out


THEMES = _build_themes()


def resolve_theme_name(name):
    """Map a saved theme name onto the new set. Old names (e.g. a light
    'Day' theme) become the closest new theme instead of resetting."""
    if name in THEMES:
        return name
    try:
        import delta_force_theme as legacy
        old = legacy.THEMES.get(name)
        if old:
            return "Daylight" if luminance(old["BG_TOP"]) > 0.4 else DEFAULT_THEME
    except Exception:
        pass
    return DEFAULT_THEME


# ======================================================================
# Pillow rendering (all cached)
# ======================================================================
_cache = {}


def _ramp(stops, w):
    """Horizontal multi-stop colour ramp, w pixels wide, 1px tall."""
    img = Image.new("RGB", (w, 1))
    px = img.load()
    for x in range(w):
        t = x / max(1, w - 1)
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if t <= p1 or i == len(stops) - 2:
                u = 0 if p1 == p0 else max(0.0, min(1.0, (t - p0) / (p1 - p0)))
                px[x, 0] = _rgb(blend(c0, c1, u))
                break
    return img


def _glow(base, cx, cy, radius, color, alpha):
    """Soft radial pool of colour, composited onto base (RGB)."""
    d = max(2, int(radius * 2))
    mask = Image.radial_gradient("L").resize((d, d), Image.BICUBIC)
    # radial_gradient is 0 at the centre but only 181 at the middle of each
    # edge (255 in the corners): rescale so the pool reaches zero exactly at
    # its radius, with a smooth squared falloff - otherwise the square shows.
    mask = mask.point(lambda v: int(255 * alpha * max(0.0, 1.0 - v / 181.0) ** 2))
    base.paste(Image.new("RGB", (d, d), _rgb(color)),
               (int(cx - radius), int(cy - radius)), mask)


def _pattern(base, kind, alpha, fg):
    if kind == "none" or alpha <= 0:
        return
    w, h = base.size
    ss = 2
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    col = _rgb(fg) + (int(255 * alpha),)
    if kind == "dots":
        step = 14 * ss
        for y in range(step // 2, h * ss, step):
            for x in range(step // 2, w * ss, step):
                dr.ellipse((x - 1, y - 1, x + 1, y + 1), fill=col)
    elif kind == "diagonal":
        step = 18 * ss
        for x in range(-h * ss, w * ss, step):
            dr.line((x, h * ss, x + h * ss, 0), fill=col, width=1)
    elif kind == "contour":
        import math
        pts_per = 3 * ss
        for k in range(9):
            base_y = (k + 0.5) * (h * ss / 9.0)
            pts = []
            for x in range(0, w * ss + pts_per, pts_per):
                xx = x / ss
                y = base_y + ss * (9 * math.sin(xx / 70.0 + k * 0.9)
                                   + 5 * math.sin(xx / 29.0 + k * 1.7))
                pts.append((x, y))
            dr.line(pts, fill=col, width=1)
    layer = layer.resize((w, h), Image.LANCZOS)
    base.paste(layer, (0, 0), layer)


def render_banner(pal, w, h, custom_path=None):
    """Banner art as an RGB PIL image, fading into the page colour at the
    bottom. Cached by (theme, size, custom picture)."""
    key = ("banner", pal["_name"], w, h, custom_path)
    if key in _cache:
        return _cache[key]
    page = pal["BG_TOP"]
    art = pal["_art"]

    img = None
    if custom_path:
        try:
            src = Image.open(custom_path).convert("RGB")
            scale = max(w / src.width, h / src.height)
            src = src.resize((max(w, int(src.width * scale)),
                              max(h, int(src.height * scale))), Image.LANCZOS)
            left = (src.width - w) // 2
            top = (src.height - h) // 2
            img = src.crop((left, top, left + w, top + h))
            # darken + tint toward the theme so text stays readable
            tint = Image.new("RGB", (w, h), _rgb(page))
            img = Image.blend(img, tint, 0.45 if not pal["_light"] else 0.30)
        except Exception:
            img = None
    if img is None:
        img = _ramp(art["stops"], w).resize((w, h), Image.BILINEAR)
        for gx, gy, gr, gcol, ga in art["glows"]:
            _glow(img, gx * w, gy * h, gr, gcol, ga)
        _pattern(img, art["pattern"], art["pattern_alpha"],
                 "#000000" if pal["_light"] else "#ffffff")

    # fine grain: breaks up 8-bit banding in dark gradients
    noise = Image.effect_noise((w, h), 3).convert("RGB")
    img = ImageChops.add(img, noise, 1.0, -128)

    # fade the lower third into the page colour so there is no visible edge
    fade = Image.linear_gradient("L").resize((w, h))
    fade = fade.point(lambda v: 0 if v < 120 else int((v - 120) / 135.0 * 255))
    img.paste(Image.new("RGB", (w, h), _rgb(page)), (0, 0), fade)
    _cache[key] = img
    return img


def render_swatch(pal, size):
    """Round chip that blends the theme's surface colour into its accent."""
    key = ("sw", pal["_name"], size)
    if key in _cache:
        return _cache[key]
    ss = 4
    d = size * ss
    grad = Image.new("RGB", (d, d))
    px = grad.load()
    c0, c1, c2 = _rgb(pal["SURFACE_ALT"]), _rgb(pal["ACCENT"]), _rgb(pal["ACCENT_HI"])
    for y in range(d):
        for x in range(d):
            t = (x + (d - y)) / (2.0 * d)          # diagonal, bottom-left -> top-right
            if t < 0.55:
                u = t / 0.55
                px[x, y] = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
            else:
                u = (t - 0.55) / 0.45
                px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * u) for i in range(3))
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
    out = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    out = out.resize((size, size), Image.LANCZOS)
    _cache[key] = out
    return out


def render_button(w, h, top, bottom, radius=10, border=None):
    """Rounded vertical-gradient button as an RGBA PIL image."""
    key = ("btn", w, h, top, bottom, radius, border)
    if key in _cache:
        return _cache[key]
    ss = 3
    W, H = w * ss, h * ss
    grad = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(grad)
    for y in range(H):
        dr.line((0, y, W, y), fill=_rgb(blend(top, bottom, y / max(1, H - 1))))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius * ss, fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    if border:
        ImageDraw.Draw(out).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius * ss, outline=_rgb(border) + (255,), width=ss)
    out = out.resize((w, h), Image.LANCZOS)
    _cache[key] = out
    return out


# ======================================================================
# Widgets
# ======================================================================
def _parent_bg(parent, pal):
    try:
        return parent.cget("bg")
    except tk.TclError:
        return pal["SURFACE"]


class GradientButton(tk.Label):
    """Rounded, gradient-filled button. Drop-in for the old flat Label
    button: same set_state(), plus set_variant() to flip primary/secondary.
    Falls back to flat colours when Pillow is missing."""

    HEIGHT = 40

    def __init__(self, parent, text, command, pal, primary=False,
                 font=("Segoe UI Semibold", 10), height=None):
        if height:
            self.HEIGHT = height            # per-button override
        super().__init__(parent, text=text, font=font, bd=0,
                         highlightthickness=0, compound="center",
                         cursor="hand2", bg=_parent_bg(parent, pal))
        self._pal = pal
        self._command = command
        self._primary = primary
        self._enabled = True
        self._hover = False
        self._font = tkfont.Font(font=font)
        self._bw = 0
        self._pending = None
        self._photos = {}
        self._use_img = HAVE_PIL
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", self._on_configure)
        self._rebuild(self._natural_width())

    # ---- state -----------------------------------------------------
    def _natural_width(self):
        return max(64, self._font.measure(self.cget("text")) + 40)

    def _look(self, state):
        p = self._pal
        if state == "disabled":
            return p["SURFACE_ALT"], p["SURFACE_ALT"], p["FG_MUTED"], None
        if self._primary:
            fg = p["ACCENT_FG"] if "ACCENT_FG" in p else p["BG_BOTTOM"]
            if state == "hover":
                if p.get("_light"):          # light themes: darken on hover
                    return (p["ACCENT"], blend(p["ACCENT"], "#000000", 0.18), fg, None)
                return (blend(p["ACCENT_HI"], "#ffffff", 0.12), p["ACCENT_HI"], fg, None)
            return (blend(p["ACCENT_HI"], p["ACCENT"], 0.35), p["ACCENT"], fg, None)
        if state == "hover":
            return (p["SURFACE_ACTIVE"], p["SURFACE_HOVER"], p["FG"], p["BORDER"])
        return (blend(p["SURFACE_ALT"], p["SURFACE_HOVER"], 0.5),
                p["SURFACE_ALT"], p["FG"], p["BORDER_SOFT"])

    def _rebuild(self, w):
        self._bw = w
        if not self._use_img:
            self._apply("normal")
            return
        fresh = {}
        try:
            for state in ("normal", "hover", "disabled"):
                top, bottom, _fg, border = self._look(state)
                fresh[state] = ImageTk.PhotoImage(
                    render_button(w, self.HEIGHT, top, bottom, 10, border))
        except Exception:
            self._use_img = False
            tk.Label.configure(self, image="")
            self._photos = {}
            self._apply("normal")
            return
        # Swap images BEFORE the old ones are released: a Label that still
        # points at a freed PhotoImage raises "image doesn't exist".
        self._photos, old = fresh, self._photos
        # The requested size must depend on the TEXT only, never on the
        # image: otherwise a button inside a shrink-wrapped container
        # (fill="x") re-renders at its stretched width, which makes the
        # container grow, which stretches the button again... forever.
        tk.Label.configure(self, image=self._photos[self._state()],
                           width=self._natural_width(), height=self.HEIGHT)
        self._apply(self._state())
        del old

    def _state(self):
        if not self._enabled:
            return "disabled"
        return "hover" if self._hover else "normal"

    def _apply(self, state):
        top, bottom, fg, _border = self._look(state)
        if self._use_img and state in self._photos:
            tk.Label.configure(self, image=self._photos[state], fg=fg)
        else:
            tk.Label.configure(self, image="", bg=bottom, fg=fg,
                               padx=16, pady=10)

    # ---- events ----------------------------------------------------
    def _on_enter(self, _e):
        self._hover = True
        if self._enabled:
            self._apply("hover")

    def _on_leave(self, _e):
        self._hover = False
        self._apply(self._state())

    def _on_click(self, _e):
        if self._enabled:
            self._command()

    def _on_configure(self, e):
        # Stretched by the layout (fill="x"): re-render at the real width,
        # once the layout pass is done (not once per intermediate size).
        if (self._use_img and 8 < e.width < 3000
                and abs(e.width - self._bw) > 1 and self._pending is None):
            self._pending = self.after_idle(self._apply_pending_width)

    def _apply_pending_width(self):
        self._pending = None
        try:
            w = self.winfo_width()
        except tk.TclError:
            return
        if 8 < w < 3000 and abs(w - self._bw) > 1:
            self._rebuild(w)

    # ---- public API (matches the old flat button) ----------------------
    def set_state(self, state):
        self._enabled = (state != "disabled")
        tk.Label.configure(self, cursor="hand2" if self._enabled else "arrow")
        self._apply(self._state())

    def set_variant(self, primary):
        if primary != self._primary:
            self._primary = primary
            self._rebuild(max(self._bw, self._natural_width()))

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        super().configure(cnf, **kw) if cnf else super().configure(**kw)
        if "text" in kw and self._use_img:
            need = self._natural_width()
            tk.Label.configure(self, width=need)
            if need > self._bw:
                self._rebuild(need)

    config = configure


class HeaderBanner(tk.Canvas):
    """Full-width banner: generated art (or the user's picture), the app
    title, and a swatch picker for the themes."""

    HEIGHT = 100

    def __init__(self, parent, pal, current, on_select, pad_x=24,
                 title=("Delta Force", "  Profit Tracker"), custom_path=None):
        super().__init__(parent, height=self.HEIGHT, highlightthickness=0,
                         bd=0, bg=pal["BG_TOP"])
        self._pal = pal
        self._current = current
        self._on_select = on_select
        self._pad = pad_x
        self._title = title
        self._custom = custom_path
        self._photos = []
        self._after = None
        self._f1 = tkfont.Font(family="Segoe UI Semibold", size=24)
        self._f2 = tkfont.Font(family="Segoe UI", size=24)
        self._fn = tkfont.Font(family="Segoe UI", size=9)
        self.bind("<Configure>", self._schedule)

    def _schedule(self, _e=None):
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(30, self._paint)

    def set_custom_image(self, path):
        self._custom = path
        self._schedule()

    def _paint(self):
        self._after = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 30:
            return
        pal = self._pal
        self.delete("all")
        self._photos = []

        if HAVE_PIL:
            try:
                art = render_banner(pal, (w // 8 + 1) * 8, h, self._custom)
                ph = ImageTk.PhotoImage(art)
                self._photos.append(ph)
                self.create_image(0, 0, image=ph, anchor="nw")
            except Exception:
                self._paint_flat(w, h)
        else:
            self._paint_flat(w, h)

        cy = h // 2 - 6
        self.create_text(self._pad, cy, text=self._title[0], anchor="w",
                         fill=pal["FG"], font=self._f1)
        self.create_text(self._pad + self._f1.measure(self._title[0]), cy,
                         text=self._title[1], anchor="w",
                         fill=pal["FG_MUTED"], font=self._f2)

        self._paint_swatches(w, cy)

    def _paint_flat(self, w, h):
        stops = self._pal["_art"]["stops"]
        for i in range(48):
            t = i / 47.0
            for j in range(len(stops) - 1):
                if t <= stops[j + 1][0] or j == len(stops) - 2:
                    p0, c0 = stops[j]
                    p1, c1 = stops[j + 1]
                    u = 0 if p1 == p0 else max(0, min(1, (t - p0) / (p1 - p0)))
                    col = blend(c0, c1, u)
                    break
            self.create_rectangle(0, i * h / 48, w, (i + 1) * h / 48 + 1,
                                  fill=col, outline="")

    def _paint_swatches(self, w, cy):
        pal = self._pal
        names = list(THEMES.keys())
        size, gap = 24, 12
        total = len(names) * size + (len(names) - 1) * gap
        x0 = w - self._pad - total
        label = self.create_text(x0 - 18, cy, text="", anchor="e",
                                 fill=pal["FG_MUTED"], font=self._fn)
        for i, name in enumerate(names):
            cx = x0 + i * (size + gap) + size / 2
            tag = f"sw_{name}"
            if name == self._current:
                self.create_oval(cx - size / 2 - 5, cy - size / 2 - 5,
                                 cx + size / 2 + 5, cy + size / 2 + 5,
                                 outline=pal["FG"], width=2, tags=tag)
            drawn = False
            if HAVE_PIL:
                try:
                    ph = ImageTk.PhotoImage(render_swatch(THEMES[name], size))
                    self._photos.append(ph)
                    self.create_image(cx, cy, image=ph, tags=tag)
                    drawn = True
                except Exception:          # e.g. Pillow's Tk support missing in a packaged build
                    drawn = False
            if not drawn:
                self.create_oval(cx - size / 2, cy - size / 2,
                                 cx + size / 2, cy + size / 2,
                                 fill=THEMES[name]["ACCENT"], outline="", tags=tag)
            self.tag_bind(tag, "<Enter>",
                          lambda e, n=name: (self.itemconfigure(label, text=n),
                                             self.configure(cursor="hand2")))
            self.tag_bind(tag, "<Leave>",
                          lambda e: (self.itemconfigure(label, text=""),
                                     self.configure(cursor="")))
            self.tag_bind(tag, "<Button-1>",
                          lambda e, n=name: self._pick(n))

    def _pick(self, name):
        if name != self._current:
            # Defer: choosing rebuilds the whole UI, including this widget.
            self.after(10, lambda: self._on_select(name))
