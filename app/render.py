"""Calendar rendering — composes views to PIL images for the e-ink screen.

All rendering targets the configured SCREEN_W x SCREEN_H (default 800x480,
SUPERSAMPLE=1 = native). Layout constants are expressed in a 1440-tall reference
space and scaled to the actual screen via _SCALE = SCREEN_H/1440, so the same
code renders cleanly at native (1px is literally 1px) or at a 3x supersample for
debugging. Supports 5-days / week / 7-days / month / 35-days views with a
current-time indicator line.
"""
import datetime
import logging
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import config

logger = logging.getLogger("eink.render")

# Screen dimensions
W = config.SCREEN_W
H = config.SCREEN_H

# Supersample factor (render is composed at W/H = OUTPUT * SUPERSAMPLE, then
# downscaled). Snapping thin dotted grid lines to a multiple of this keeps them
# a uniform 1px after downscale (an unaligned line aliases to 1px or 2px).
_S = max(1, getattr(config, "SUPERSAMPLE", 1))

# Native-render scale: the layout was originally tuned for a 1440-tall canvas
# (OUTPUT 480 x SUPERSAMPLE 3 = 1440). All hardcoded pixel constants are
# expressed in that tuned space and are multiplied by _SCALE so they map to the
# actual SCREEN_H. Native (SUPERSAMPLE=1, SCREEN_H=480) -> _SCALE=1/3, so e.g. a
# 78px margin becomes 26px, a 32px font becomes ~11px, and 1px is literally 1px.
# SUPERSAMPLE=3 (legacy) -> _SCALE=1, original sizes unchanged.
_SCALE = H / 1440.0


def _sz(v) -> int:
    """Scale a tuned-canvas pixel value to the actual screen size (rounded).

    Use for margins, font sizes, offsets, radii — any constant that was sized
    for the 1440-tall reference canvas. Non-integer results are rounded so 1px
    features (borders, dotted lines) land on whole pixels.
    """
    return int(round(v * _SCALE))


def _snap(v) -> int:
    """Round a coordinate to the nearest multiple of the supersample factor so
    thin lines downscale to a consistent 1px. When _S=1 (native) this is a
    no-op round to the nearest integer."""
    return int(round(v / _S) * _S)


def _hdots(draw, x0, x1, y, on=None, off=None):
    """Horizontal dotted line, drawn as top-aligned _S-thick dashes starting at a
    snapped coordinate → a uniform 1px dotted line after downscale. on/off are in
    supersample px (default 1px-on/2px-off at output = '100100')."""
    on = _S if on is None else on
    off = 2 * _S if off is None else off
    x, x1, y = int(x0), int(x1), int(y)
    while x <= x1:
        draw.rectangle([x, y, min(x + on - 1, x1), y + _S - 1], fill=BLACK)
        x += on + off


def _vdots(draw, x, y0, y1, on=None, off=None):
    """Vertical dotted line (top-aligned _S-thick dashes). Default 1px-on/1px-off
    at output = '1010' (denser than the hour lines)."""
    on = _S if on is None else on
    off = _S if off is None else off
    y, y1, x = int(y0), int(y1), int(x)
    while y <= y1:
        draw.rectangle([x, y, x + _S - 1, min(y + on - 1, y1)], fill=BLACK)
        y += on + off

# Margins / layout (in tuned-canvas px; scaled to screen via _sz where used).
# These are the 1440-tall reference values — _SCALE maps them to native.
MARGIN = 78   # left margin for hour labels
RIGHT_PAD = 10  # right edge padding
HEADER_H = 120
FOOTER_H = 30

# Fonts — prefer a bundled DejaVuSans.ttf, fall back through macOS system TTFs
# (see config.FONT_CANDIDATES). The first candidate that loads is used for all
# sizes and cached.
_FONT_PATHS = list(getattr(config, "FONT_CANDIDATES", [])) or [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_RESOLVED_FONT_PATH: Optional[str] = None  # first candidate that actually loads
_SIZE_MODIFIER = 0  # global font size adjustment, set before each render


def _resolve_font_path() -> Optional[str]:
    """Return the first font candidate that PIL can open (cached)."""
    global _RESOLVED_FONT_PATH
    if _RESOLVED_FONT_PATH is not None:
        return _RESOLVED_FONT_PATH
    for p in _FONT_PATHS:
        try:
            ImageFont.truetype(p, 20)
            _RESOLVED_FONT_PATH = p
            logger.info("Using font: %s", p)
            return p
        except Exception:
            continue
    _RESOLVED_FONT_PATH = ""  # sentinel: none found, use PIL default
    logger.warning("No TrueType font found in candidates; using PIL default")
    return _RESOLVED_FONT_PATH


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Get a cached font instance, adjusted by global size modifier.

    `size` is in tuned-canvas pixels (the 1440-tall reference). It is scaled by
    _SCALE to the actual screen size, then the text_size_modifier is ADDED IN
    NATIVE pixels (so the user-visible +/- adjustment is consistent regardless
    of SUPERSAMPLE; e.g. modifier +13 in the old 3x space == ~+4 native px). A
    requested bold weight adds +1 tuned-px to partially compensate.

    Always uses the regular (non-bold) font — bold fonts produce 3-5px wide
    strokes that the IT8951 GC16 waveform doubles/splits. The regular font
    produces 2px strokes that render cleanly.
    """
    native_size = _sz(size) + _SIZE_MODIFIER + (1 if bold else 0)
    size = max(4, native_size)
    path = _resolve_font_path()
    key = (path, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_h(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _font_size(font) -> int:
    """The font's actual rendered pixel size (the size it was created with)."""
    try:
        return font.size
    except AttributeError:  # PIL default font
        return 10


def _font_line_h(font) -> int:
    """Line height derived from the font's actual metrics, so text stays readable
    across text_size_modifier 0-10. Uses ascent+descent + 1px leading (min 9px)
    — instead of a fixed tuned-canvas constant that doesn't scale with the
    modifier. At modifier 0 this gives comfortable leading for ~8-11px text; at
    modifier 10 it grows with the ~18-21px glyphs so lines never overlap."""
    try:
        asc, desc = font.getmetrics()
        return max(9, asc + desc + 1)
    except Exception:
        return max(9, _font_size(font) + 2)


def _font_stroke(font, base: int = 2) -> int:
    """Stroke width scaled to the font's actual size so white-on-black text stays
    bold enough to survive 1-bit at large modifiers, and doesn't blob at small
    ones. base is the tuned-canvas px reference (~2)."""
    sz = _font_size(font)
    # ~sz/8, clamped to >=1; at native base sizes (8-21px) this is 1-2px.
    return max(1, max(base, sz // 8))


# ---- Exact (non-antialiased) drawing helpers for b/w mode ----
def _hline(draw, x0: int, y: int, x1: int, fill, width: int = 1):
    """Draw a horizontal line using rectangle fill (no PIL line AA)."""
    if width == 1:
        draw.rectangle([(x0, y), (x1, y)], fill=fill)
    else:
        half = width // 2
        draw.rectangle([(x0, y - half), (x1, y + width - half - 1)], fill=fill)


def _vline(draw, x: int, y0: int, y1: int, fill, width: int = 1):
    """Draw a vertical line using rectangle fill (no PIL line AA)."""
    if width == 1:
        draw.rectangle([(x, y0), (x, y1)], fill=fill)
    else:
        half = width // 2
        draw.rectangle([(x - half, y0), (x + width - half - 1, y1)], fill=fill)


def _hsegments(draw, x0: int, x1: int, y: int, fill,
               step: int, seg_len: int, width: int = 1):
    """Draw a dotted/dashed horizontal line with exact rectangle segments."""
    for sx in range(x0, x1 + 1, step):
        ex = min(sx + seg_len - 1, x1)
        _hline(draw, sx, y, ex, fill, width)


def _vsegments(draw, x: int, y0: int, y1: int, fill,
               step: int, seg_len: int, width: int = 1):
    """Draw a dotted/dashed vertical line with exact rectangle segments."""
    for sy in range(y0, y1 + 1, step):
        ey = min(sy + seg_len - 1, y1)
        _vline(draw, x, sy, ey, fill, width)


# ---- Color helpers (grayscale for e-ink: (0,0,0)=black, (255,255,255)=white) ----
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY_DARK = (60, 60, 60)
GRAY_MID = (120, 120, 120)
GRAY_LIGHT = (200, 200, 200)
GRAY_VLIGHT = (239, 239, 239)
GRAY_DIM = (153, 153, 153)
GRAY_HOUR_LINE = (170, 170, 170)


def render_calendar(view_mode: str, events: list[dict],
                    day_start: str, day_end: str,
                    max_full_day: int, time_format: str = "24h",
                    date_format: str = "",
                    settings_url: str = "",
                    crossed_event_dim: bool = False,
                    dim_past_events: bool = False,
                    text_size_modifier: int = 0,
                    show_time_line: bool = True,
                    time_line_style: str = "dotted",
                    bw_mode: bool = False,
                    dim_style: str = "normal",
                    now: Optional[datetime.datetime] = None) -> Image.Image:
    """Render the full calendar view to a PIL Image.

    Returns an RGB image (will be converted to grayscale by the C driver).
    bw_mode: when True, render in 1-bit (black/white only) and threshold the
             final image so DU mode can be used without ghosting accumulation.
    dim_style: "normal" (white fill + dotted border) or "checkerboard" (1px
               alternating B/W pattern fill for dimmed events).
    """
    global _SIZE_MODIFIER
    _SIZE_MODIFIER = text_size_modifier

    if now is None:
        now = datetime.datetime.now()

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))

    if view_mode == "month":
        _render_month(draw, events, now, max_full_day, date_format=date_format, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    elif view_mode == "35days":
        _render_35days(draw, events, now, max_full_day, date_format=date_format, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    elif view_mode == "7days":
        week_num = now.isocalendar()[1]
        _render_7days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format, week_num=week_num,
                      crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    elif view_mode == "5days":
        _render_5days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format,
                      crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    else:  # week (default)
        _render_week(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)

    # Draw current-time line on week/7days/5days views
    if view_mode in ("week", "7days", "5days") and show_time_line:
        _draw_time_line(draw, now, view_mode, day_start, day_end, events, time_format,
                        style=time_line_style, max_full_day=max_full_day, bw_mode=bw_mode)

    # Settings URL (centered between title and subtitle on header line)
    if settings_url:
        url_font = _font(28)
        uw = _text_w(draw, settings_url, url_font)
        # Compute center between title right edge and subtitle left edge
        font_title = _font(64, bold=True)
        title_str = now.strftime(date_format) if date_format else (
            now.strftime("%B %Y") if view_mode in ("month", "35days") else (
            now.strftime("%B %d, %Y") if view_mode == "week" else "Next 7 Days"))
        tw = _text_w(draw, title_str, font_title)
        title_right = _sz(MARGIN) + tw
        font_sub = _font(36)
        sub_str = f"Week {now.isocalendar()[1]}" if view_mode in ("week", "7days") else (
            f"Week {now.isocalendar()[1]} — 35 days" if view_mode == "35days" else "")
        if sub_str:
            sw = _text_w(draw, sub_str, font_sub)
            sub_left = W - _sz(MARGIN) - sw
        else:
            sub_left = W - _sz(MARGIN)
        ip_center = (title_right + sub_left) // 2
        draw.text((ip_center - uw // 2, _sz(32)), settings_url, fill=GRAY_MID, font=url_font)

    # Grayscale mode: threshold the entire image to pure B/W to eliminate
    # anti-aliasing artifacts. The IT8951 GL16 waveform ghosts gray AA edge
    # pixels, creating a visible doubling/shift effect on text and thin lines.
    # By making everything pure B/W (no gray), there are no gray edge pixels
    # to ghost. Gray grid lines (GRAY_LIGHT=200) become white after threshold,
    # so they are drawn as BLACK in this mode to survive the <128 threshold.
    if bw_mode:
        gray = img.convert("L")
        if dim_past_events or crossed_event_dim:
            # Hard threshold (NOT Floyd-Steinberg): the finished-event 1px
            # checkerboard is drawn as alternating WHITE/BLACK pixels, so a hard
            # threshold preserves it exactly as a true 1px B/W checker. FS
            # dithering would average the fine checker to grey and re-dither it
            # into solid black or a coarse mess (Task 5). Gray AA edge pixels on
            # text/strokes still get pulled to B/W; the white text halo keeps
            # dimmed text readable over the checker.
            bw = gray.point(lambda x: 0 if x < 128 else 255, "L").convert("1")
        else:
            bw = gray.point(lambda x: 0 if x < 128 else 255, "L")
        img = bw.convert("RGB")
    else:
        # Grayscale mode: hard threshold to eliminate ALL gray AA pixels that
        # cause GL16 ghosting/doubling. Threshold at 201 keeps grid lines
        # (GRAY_LIGHT=200, GRAY_HOUR_LINE=170, GRAY_DIM=153) as black, while
        # event fills (GRAY_VLIGHT=239) and background (255) stay white.
        gray = img.convert("L")
        bw = gray.point(lambda x: 0 if x < 201 else 255, "L")
        img = bw.convert("RGB")

    return img


# ---- Header ----
def _draw_header(draw: ImageDraw.ImageDraw, title: str, subtitle: str = ""):
    """Draw the page header with title (left) and subtitle (right)."""
    # Title
    font_title = _font(64, bold=True)
    draw.text((_sz(MARGIN), _sz(20)), title, fill=BLACK, font=font_title)

    # Subtitle (e.g. week number or date range)
    if subtitle:
        font_sub = _font(36)
        sw = _text_w(draw, subtitle, font_sub)
        draw.text((W - _sz(MARGIN) - sw, _sz(30)), subtitle, fill=GRAY_DARK, font=font_sub)

    # Header separator line
    y = _sz(HEADER_H) - _sz(10)
    _hline(draw, MARGIN, y, W - MARGIN, GRAY_MID, width=2)


# ---- Month view ----
def _render_month(draw, events, now, max_full_day, date_format="", dim_past_events=False, bw_mode=False, dim_style="normal"):
    """Month grid view — weeks as rows, days as columns."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %Y")
    _draw_header(draw, title)

    today = now.date()
    # Find first day of month and the starting grid cell
    first = today.replace(day=1)
    # Monday=0
    start_weekday = first.weekday()
    grid_start = first - datetime.timedelta(days=start_weekday)

    # Number of weeks to show (usually 5-6)
    next_month = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    last_day = (next_month - datetime.timedelta(days=1)).day
    total_days = start_weekday + last_day
    num_weeks = math.ceil(total_days / 7)

    grid_x = 19  # moved left by 11mm total
    grid_y = HEADER_H + 10
    grid_w = W - grid_x - RIGHT_PAD
    grid_h = H - grid_y - FOOTER_H
    col_w = grid_w // 7
    row_h = grid_h // num_weeks

    # Day-of-week headers
    dow_font = _font(28, bold=True)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, dow in enumerate(dows):
        cx = grid_x + i * col_w + col_w // 2
        tw = _text_w(draw, dow, dow_font)
        draw.text((cx - tw // 2, grid_y), dow, fill=GRAY_DARK, font=dow_font)
    grid_y += 36

    # Events indexed by date
    events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            events_by_date.setdefault(d, []).append(ev)
        else:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            events_by_date.setdefault(d, []).append(ev)

    # Grid cells
    cell_font = _font(32, bold=True)
    event_font = _font(22)
    event_bold = _font(22, bold=True)
    # Outer left + top border (cells only draw right+bottom edges)
    _vline(draw, grid_x, grid_y, grid_y + num_weeks * row_h - 1, GRAY_LIGHT, width=1)
    _hline(draw, grid_x, grid_y, grid_x + 7 * col_w - 1, GRAY_LIGHT, width=1)
    day_num = grid_start
    for week in range(num_weeks):
        for col in range(7):
            x = grid_x + col * col_w
            y = grid_y + week * row_h

            # Cell border — draw only RIGHT and BOTTOM edges to avoid doubling
            # (a full rectangle per cell draws shared edges twice, 1px apart).
            # Strong (thick 3px) at month boundaries; thin otherwise.
            next_day = day_num + datetime.timedelta(days=1)
            next_week = day_num + datetime.timedelta(days=7)
            right_strong = col < 6 and next_day.month != day_num.month
            bottom_strong = week < num_weeks - 1 and next_week.month != day_num.month
            if right_strong or bottom_strong:
                if right_strong:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, BLACK, width=3)
                if bottom_strong:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, BLACK, width=3)
                # Non-strong edges as thin lines
                if not right_strong and col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if not bottom_strong and week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)
            elif bw_mode:
                # Dotted cell border in b/w mode (2px dot, 6px gap).
                # Draw ONLY the dotted segments — no solid rectangle underneath.
                # Right edge (dotted vertical)
                if col < 6 and not right_strong:
                    _vsegments(draw, x + col_w - 1, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                # Bottom edge (dotted horizontal)
                if week < num_weeks - 1 and not bottom_strong:
                    _hsegments(draw, x, x + col_w - 1, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                # Left + top edges: only draw if this is the first cell (col=0/week=0)
                if col == 0:
                    _vsegments(draw, x, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week == 0:
                    _hsegments(draw, x, x + col_w - 1, y, BLACK, step=8, seg_len=2, width=1)
            else:
                # Non-bw, non-month-boundary: draw only RIGHT + BOTTOM edges
                if col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)

            # Day number
            in_month = day_num.month == today.month
            is_today = day_num == today
            if dim_past_events and day_num < today:
                color = GRAY_DIM
            else:
                color = BLACK if in_month else GRAY_MID
            day_str = str(day_num.day)
            if is_today:
                # Highlight today — full cell width, compact height around text
                bb = draw.textbbox((0, 0), day_str, font=cell_font)
                tw = bb[2] - bb[0]
                th = bb[3] - bb[1]
                pad = 3
                rect_top = y + 6 + bb[1] - pad
                rect_bot = y + 6 + bb[3] + pad
                draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
                draw.text((x + 10, y + 6), day_str, fill=(255, 255, 255), font=cell_font)
                # Dotted cell border — skip edges that already have strong month-boundary line
                prev_day = day_num - datetime.timedelta(days=1)
                prev_week = day_num - datetime.timedelta(days=7)
                left_strong = col > 0 and prev_day.month != day_num.month
                top_strong = week > 0 and prev_week.month != day_num.month
                dot_step = 27
                dot_r = 4
                if bw_mode:
                    # Square dots (no AA) in b/w mode
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
                else:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
            else:
                draw.text((x + 10, y + 6), day_str, fill=color, font=cell_font)

            # Events for this day — sort all-day events by span (longer first)
            # so multi-day all-day events appear above shorter ones in each cell.
            day_events = events_by_date.get(day_num, [])
            cell_avail_w = col_w - 16
            visible_events = [ev for ev in day_events if ev.get("summary", "") not in ("", "(No title)")]
            visible_events.sort(key=lambda e: (-_all_day_span_days(e) if e.get("all_day") else 0,
                                               str(e["start"])))
            ey = y + 48
            ev_fill = GRAY_DIM if (dim_past_events and day_num < today) else GRAY_DARK
            ev_idx = 0
            while ev_idx < len(visible_events) and ey + 26 <= y + row_h - 4:
                ev = visible_events[ev_idx]
                ev_time = _ev_time_str(ev, now)
                if ev_time:
                    time_w = _text_w(draw, ev_time + " ", event_bold)
                    draw.text((x + 8, ey), ev_time + " ", fill=ev_fill, font=event_bold)
                    name = ev["summary"]
                    if time_w + _text_w(draw, name, event_font) > cell_avail_w:
                        while len(name) > 2 and time_w + _text_w(draw, name + "…", event_font) > cell_avail_w:
                            name = name[:-1]
                        name += "…"
                    draw.text((x + 8 + time_w, ey), name, fill=ev_fill, font=event_font)
                else:
                    # All-day event — syllable-wrap to fit cell width
                    display = _fit_fd_text(draw, ev["summary"], event_font, cell_avail_w)
                    if display:
                        draw.text((x + 8, ey), display, fill=ev_fill, font=event_font)
                ey += 26
                ev_idx += 1
            if ev_idx < len(visible_events) and ey + 26 > y + row_h - 4:
                remaining = len(visible_events) - ev_idx
                draw.text((x + 8, ey), f"+{remaining}", fill=ev_fill, font=_font(18))

            day_num += datetime.timedelta(days=1)


def _render_35days(draw, events, now, max_full_day, date_format="", dim_past_events=False, bw_mode=False, dim_style="normal"):
    """35-days view — 5 weeks starting from current week's Monday.

    Shows a month-like grid with the current week as the top row.
    Includes month separator lines between months.
    """
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %Y")
    week_num = now.isocalendar()[1]
    _draw_header(draw, title, f"Week {week_num} — 35 days")

    today = now.date()
    # Start from Monday of current week
    start_date = today - datetime.timedelta(days=today.weekday())
    num_weeks = 5

    grid_x = 19  # moved left by 11mm total
    grid_y = HEADER_H + 10
    grid_w = W - grid_x - RIGHT_PAD
    grid_h = H - grid_y - FOOTER_H
    col_w = grid_w // 7
    row_h = grid_h // num_weeks

    # Day-of-week headers
    dow_font = _font(28, bold=True)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, dow in enumerate(dows):
        cx = grid_x + i * col_w + col_w // 2
        tw = _text_w(draw, dow, dow_font)
        draw.text((cx - tw // 2, grid_y), dow, fill=GRAY_DARK, font=dow_font)
    grid_y += 36

    # Events indexed by date
    events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        events_by_date.setdefault(d, []).append(ev)

    # Grid cells
    cell_font = _font(32, bold=True)
    event_font = _font(22)
    event_bold = _font(22, bold=True)
    # Outer left + top border (cells only draw right+bottom edges)
    _vline(draw, grid_x, grid_y, grid_y + num_weeks * row_h - 1, GRAY_LIGHT, width=1)
    _hline(draw, grid_x, grid_y, grid_x + 7 * col_w - 1, GRAY_LIGHT, width=1)
    day_num = start_date
    for week in range(num_weeks):
        for col in range(7):
            x = grid_x + col * col_w
            y = grid_y + week * row_h

            # Cell border — draw only RIGHT and BOTTOM edges to avoid doubling
            next_day = day_num + datetime.timedelta(days=1)
            next_week = day_num + datetime.timedelta(days=7)
            right_strong = col < 6 and next_day.month != day_num.month
            bottom_strong = week < num_weeks - 1 and next_week.month != day_num.month
            if right_strong or bottom_strong:
                if right_strong:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, BLACK, width=3)
                if bottom_strong:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, BLACK, width=3)
                if not right_strong and col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if not bottom_strong and week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)
            elif bw_mode:
                # Dotted cell border — draw ONLY dots, no solid rectangle
                if col < 6 and not right_strong:
                    _vsegments(draw, x + col_w - 1, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week < num_weeks - 1 and not bottom_strong:
                    _hsegments(draw, x, x + col_w - 1, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if col == 0:
                    _vsegments(draw, x, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week == 0:
                    _hsegments(draw, x, x + col_w - 1, y, BLACK, step=8, seg_len=2, width=1)
            else:
                # Non-bw, non-month-boundary: draw only RIGHT + BOTTOM edges
                if col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)

            # Day number
            is_today = day_num == today
            color = GRAY_DIM if (dim_past_events and day_num < today and not is_today) else BLACK
            day_str = str(day_num.day)
            if is_today:
                # Highlight today — full cell width, compact height
                bb = draw.textbbox((0, 0), day_str, font=cell_font)
                th = bb[3] - bb[1]
                pad = 3
                rect_top = y + 6 + bb[1] - pad
                rect_bot = y + 6 + bb[3] + pad
                draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
                draw.text((x + 10, y + 6), day_str, fill=(255, 255, 255), font=cell_font)
                # Dotted cell border — skip edges with strong month-boundary lines
                prev_day = day_num - datetime.timedelta(days=1)
                prev_week = day_num - datetime.timedelta(days=7)
                left_strong = col > 0 and prev_day.month != day_num.month
                top_strong = week > 0 and prev_week.month != day_num.month
                dot_step = 27
                dot_r = 4
                if bw_mode:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
                else:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
            else:
                draw.text((x + 10, y + 6), day_str, fill=color, font=cell_font)

            # Events for this day — sort all-day events by span (longer first)
            # so multi-day all-day events appear above shorter ones in each cell.
            day_events = events_by_date.get(day_num, [])
            cell_avail_w = col_w - 16
            visible_events = [ev for ev in day_events if ev.get("summary", "") not in ("", "(No title)")]
            visible_events.sort(key=lambda e: (-_all_day_span_days(e) if e.get("all_day") else 0,
                                               str(e["start"])))
            ey = y + 48
            ev_fill = GRAY_DIM if (dim_past_events and day_num < today) else GRAY_DARK
            ev_idx = 0
            while ev_idx < len(visible_events) and ey + 26 <= y + row_h - 4:
                ev = visible_events[ev_idx]
                ev_time = _ev_time_str(ev, now)
                if ev_time:
                    time_w = _text_w(draw, ev_time + " ", event_bold)
                    draw.text((x + 8, ey), ev_time + " ", fill=ev_fill, font=event_bold)
                    name = ev["summary"]
                    if time_w + _text_w(draw, name, event_font) > cell_avail_w:
                        while len(name) > 2 and time_w + _text_w(draw, name + "…", event_font) > cell_avail_w:
                            name = name[:-1]
                        name += "…"
                    draw.text((x + 8 + time_w, ey), name, fill=ev_fill, font=event_font)
                else:
                    display = _fit_fd_text(draw, ev["summary"], event_font, cell_avail_w)
                    if display:
                        draw.text((x + 8, ey), display, fill=ev_fill, font=event_font)
                ey += 26
                ev_idx += 1
            remaining = len(visible_events) - ev_idx
            if remaining > 0 and ey + 26 > y + row_h - 4:
                draw.text((x + 8, ey), f"+{remaining}", fill=ev_fill, font=_font(18))

            day_num += datetime.timedelta(days=1)


def _render_week(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal"):
    """Week view — 7 day columns with timed events stacked vertically."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %d, %Y")
    week_num = now.isocalendar()[1]
    _draw_header(draw, title, f"Week {week_num}")

    _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=7, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)


# ---- 7-days view (next 7 days starting today) ----
def _render_7days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, week_num=None, bw_mode=False, dim_style="normal"):
    """7-days view — starting from today, 7 consecutive day columns."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = "Next 7 Days"
    _draw_header(draw, title, f"Week {week_num}" if week_num else now.strftime("%a %b %d"))

    _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=7, start_today=True, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)


# ---- 5-days view (next 5 days starting today; wider columns / larger text) ----
def _render_5days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal"):
    """5-days view — starting from today, 5 consecutive day columns.

    Same card/text rules as the 7-days view (shares _render_day_grid) but with
    fewer, wider columns — better legibility on the small 800x480 panel.
    """
    # No page title/date header — the day grid fills the whole vertical area,
    # with the day labels (Thu 6 …) at the very top.
    _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=5, start_today=True, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style, compact_top=True)


def _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", days=7, start_today=False, date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal", compact_top=False):
    """Shared day-grid renderer for week and 7-days views."""
    today = now.date()

    if start_today:
        start_date = today
    else:
        # Start from Monday of current week
        start_date = today - datetime.timedelta(days=today.weekday())

    # Hour lines + labels — measure widest label first for dynamic left margin
    hour_font = _font(26, bold=True)
    max_label_w = 0
    for h in range(ds_h, de_h + 1):
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        lw = _text_w(draw, label, hour_font)
        if lw > max_label_w:
            max_label_w = lw

    left_margin = max(_sz(60), max_label_w + _sz(14))  # dynamic left margin for hour labels
    label_rpad = _sz(6)  # gap between label right edge and grid
    grid_x = left_margin
    grid_w = W - left_margin - _sz(RIGHT_PAD)
    # Vertical layout. compact_top (5-day view): no page title — day labels sit at
    # the very top, all-day events stack below them, and the hour grid fills the
    # rest of the height. Otherwise use the classic header-band layout.
    fd_h = max(_sz(34) if compact_top else _sz(30), _font_line_h(_font(24)) + _sz(2))   # all-day bar height (grows with text size so it never clips)
    if compact_top:
        label_top = _sz(6)
        label_h = _sz(52)
        allday_top = label_top + label_h + _sz(4)   # all-day bars start below the labels
        reserve = (max_full_day * fd_h) if max_full_day > 0 else 0
        grid_y = allday_top + reserve + _sz(6)
    else:
        label_top = None
        allday_top = _sz(HEADER_H) - _sz(8)
        grid_y = _sz(HEADER_H) + _sz(50)
    grid_h = H - grid_y - _sz(FOOTER_H) + _sz(20)  # +~2mm bottom expansion

    col_w = grid_w // days
    if bw_mode:
        # Snap grid geometry to the supersample grid so dotted hour/day lines
        # and edges all downscale to a uniform 1px (no 1px/2px deviations).
        grid_x = _snap(grid_x)
        grid_y = _snap(grid_y)
        grid_h = _snap(grid_h)
        col_w = _snap(col_w)
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60  # fallback 16h
    minute_h = grid_h / span_min  # pixels per minute

    # Full-day events — build data index early. Multi-day all-day events are
    # registered on EVERY date they span (start .. end-1, Google convention) so
    # they can be rendered as a single bar stretching across all their days.
    fd_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            end_d = ev.get("end")
            if isinstance(end_d, datetime.datetime):
                end_d = end_d.date()
            # Expand to all dates in [start, end). end is exclusive (Google Cal).
            cur = d
            while end_d is not None and cur < end_d:
                fd_events_by_date.setdefault(cur, []).append(ev)
                cur += datetime.timedelta(days=1)
            if end_d is None:
                fd_events_by_date.setdefault(d, []).append(ev)

    # Grid border. In b/w mode the dotted hour/day lines define the grid, so we
    # skip the solid perimeter outline — it drew a solid horizontal line across
    # all days at grid_y (= day_start, e.g. 10:30) that looked out of place.
    if not bw_mode:
        draw.rectangle([grid_x, grid_y, grid_x + days * col_w - 1, grid_y + grid_h - 1],
                       outline=GRAY_LIGHT, width=1)
    else:
        # Dotted top + bottom edges (same 100100 style as hour lines) so the
        # vertical day separators connect to a horizontal line at the grid top
        # and bottom — no solid perimeter.
        for _edge_y in (grid_y, grid_y + grid_h):
            _hdots(draw, grid_x, grid_x + days * col_w, _edge_y)

    # Hour lines + labels
    for h in range(ds_h, de_h + 1):
        y = int(grid_y + (h * 60 - ds_min) * minute_h)
        if bw_mode:
            y = _snap(y)  # align to supersample grid → uniform 1px dotted line
        if y > grid_y + grid_h:
            break
        if y < grid_y:
            continue  # hour before day_start (e.g. 10:00 when day starts 10:30) — off-grid
        if bw_mode:
            # Hour lines as "100100" dots (1px on / 2px off at output), snapped so
            # every line is a uniform 1px.
            _hdots(draw, grid_x, grid_x + days * col_w, y)
        else:
            _hline(draw, grid_x, y, grid_x + days * col_w, GRAY_HOUR_LINE, width=1)
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        draw.text((grid_x - max_label_w - label_rpad, y - _sz(14)), label, fill=BLACK, font=hour_font)

    # Column separators — thicker where month changes, extending up to header line
    sep_top = grid_y if compact_top else _sz(HEADER_H) - _sz(10)
    for i in range(1, days):
        x = grid_x + i * col_w
        prev_d = start_date + datetime.timedelta(days=i - 1)
        curr_d = start_date + datetime.timedelta(days=i)
        if prev_d.month != curr_d.month and not bw_mode:
            # Thick line at month boundary (grayscale mode only)
            _vline(draw, x, sep_top, grid_y + grid_h, BLACK, width=3)
        elif bw_mode:
            # Day separators as "1010" dots (1px on / 1px off at output), snapped
            # so every line is a uniform 1px.
            _vdots(draw, _snap(x), grid_y, grid_y + grid_h)
        else:
            _vline(draw, x, grid_y, grid_y + grid_h, GRAY_LIGHT, width=1)

    # Day headers — drawn AFTER full-day events so dates stay on top of bars
    dow_font = _font(40, bold=True)
    date_font = _font(40, bold=True)
    # Baseline: center the label text in the label row (compact) or the classic
    # header band (non-compact).
    _bb = draw.textbbox((0, 0), "Mon", font=dow_font)
    if compact_top:
        _line_y = label_top + label_h // 2 - (_bb[1] + _bb[3]) // 2
    else:
        _line_y = _sz(140) - (_bb[1] + _bb[3]) // 2
    now_min_total = now.hour * 60 + now.minute
    is_before_day = now_min_total < ds_min
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        cx = x + col_w // 2

        dow = d.strftime("%a")
        date_str = str(d.day)

        dw = _text_w(draw, dow, dow_font)
        dw2 = _text_w(draw, date_str, date_font)
        gap = _sz(6)
        combined_w = dw + gap + dw2
        line_x = cx - combined_w // 2
        line_y = _line_y

        if d == today:
            # Today: solid black label cell, white text. No stroke — the render
            # pipeline hard-thresholds to pure B/W, so AA edges are eliminated
            # and the text is clean white on black without thickening.
            rect_top = label_top if compact_top else _sz(110)
            rect_bot = (label_top + label_h) if compact_top else grid_y
            draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
            draw.text((line_x, line_y), dow, fill=WHITE, font=dow_font)
            draw.text((line_x + dw + gap, line_y), date_str, fill=WHITE, font=date_font)
        else:
            draw.text((line_x, line_y), dow, fill=GRAY_DARK, font=dow_font)
            draw.text((line_x + dw + gap, line_y), date_str, fill=BLACK, font=date_font)

    # Timed events
    timed_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            continue
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        timed_events_by_date.setdefault(d, []).append(ev)

    event_font = _font(32)
    event_font_sm = _font(24)
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        day_events = sorted(timed_events_by_date.get(d, []), key=lambda e: _ev_minutes(e, now))
        if not day_events:
            continue

        # Pre-compute positions for overlap detection
        ev_infos = []
        for ev in day_events:
            ev_start_min = _ev_minutes(ev, now, start=True)
            ev_end_min = _ev_minutes(ev, now, start=False)
            ev_start_min = max(ev_start_min, ds_min)
            ev_end_min = min(ev_end_min, de_min)
            if ev_end_min <= ev_start_min:
                continue
            ey_top = grid_y + (ev_start_min - ds_min) * minute_h
            ey_bot = grid_y + (ev_end_min - ds_min) * minute_h
            eh = max(ey_bot - ey_top, _sz(18))
            ev_infos.append((ev, ey_top, ey_bot, eh, ev_end_min - ev_start_min, ev_start_min, ev_end_min))

        # Calculate horizontal splits for all events — checks ALL overlaps, not just neighbors.
        # SHRINK is the per-rank horizontal indent (tuned-canvas px → scaled). The
        # longest/base overlapping event keeps the full column left edge (Task 2);
        # only the shorter, on-top (rank > 0) event is inset to the right.
        SHRINK = _sz(6)  # ~1mm at 150 DPI
        OVERLAP_INSET = _sz(12) + 3  # On-top card left edge inset from the base's
        # left edge (+3 raw px for a clearly visible gap). The base (longest)
        # event stays at the column edge.
        draw_infos = []  # (ev, ey_top, ey_bot, eh, duration, xl, xr, start_min, end_min, rank)
        for idx, (ev, ey_top, ey_bot, eh, duration, s_min, e_min) in enumerate(ev_infos):
            # Find ALL overlapping events
            overlap_idxs = []
            for j, (_, j_top, j_bot, _, _, _, _) in enumerate(ev_infos):
                if j != idx and ey_top < j_bot and ey_bot > j_top:
                    overlap_idxs.append(j)

            if overlap_idxs:
                # Rank this event's duration among all overlapping events (0 = longest)
                all_durs = sorted([ev_infos[k][4] for k in overlap_idxs] + [duration], reverse=True)
                rank = all_durs.index(duration)

                if rank == 0:
                    # Longest/base event: full column left edge, right edge inset
                    # so the on-top card has room. Left edge STAYS PUT (Task 2).
                    xl, xr = x + _sz(6), x + col_w - _sz(6) - SHRINK
                elif rank == 1 and len(all_durs) >= 3:
                    # Middle event (3-way overlap): medium indent
                    xl, xr = x + _sz(6) + SHRINK * 3, x + col_w - _sz(4)
                elif len(overlap_idxs) >= 2:
                    # Shortest in 3+ overlap: render on top, 2x indent
                    xl, xr = x + _sz(6) + SHRINK * 6, x + col_w - _sz(4) - SHRINK * 3
                else:
                    # 2-event overlap: the on-top (shorter) card. Task 2 — shift
                    # ONLY this card's left edge +4px (OVERLAP_INSET) from the
                    # base's left edge; the base stays at the column edge.
                    xl, xr = x + _sz(6) + OVERLAP_INSET, x + col_w - _sz(4)
            else:
                rank = 0
                xl, xr = x + _sz(4), x + col_w - _sz(4)
            draw_infos.append((ev, ey_top, ey_bot, eh, duration, xl, xr, s_min, e_min, rank))

        # Task 3: draw boxes AND text in ONE loop, longest-first, so the on-top
        # card's box is painted before the longer event's text — the box then
        # blocks (reflows) the covered text below it instead of bleeding through.
        now_min_total = now.hour * 60 + now.minute
        # Line heights derived from the ACTUAL font metrics so text stays readable
        # across text_size_modifier 0-10 (fixed _sz() constants left zero leading
        # at 0 and overlapping lines at 10).
        line_h = _font_line_h(event_font)
        line_h_sm = _font_line_h(event_font_sm)
        GROUP_GAP = max(4, line_h_sm // 2)  # blank-spacer scales with text size
        TEXT_PAD = max(2, line_h // 4)      # top padding inside card scales too
        for info in sorted(draw_infos, key=lambda e: -e[4]):
            ev, ey_top, ey_bot, eh, duration, xl, xr, s_min, e_min, rank = info
            is_crossed = crossed_event_dim and (s_min <= now_min_total < e_min)
            is_past = dim_past_events and (d < today or (d == today and e_min <= now_min_total))
            is_dimmed = is_crossed or is_past
            if bw_mode:
                # Black card with a ROUNDED white 1px border on ALL four sides
                # (Task 4). Under native rendering (_S=1) this is literally
                # rounded_rectangle(width=1); no supersample snapping needed.
                fx0, fy0 = _snap(int(xl)), _snap(int(ey_top))
                fx1, fy1 = _snap(int(xr)), _snap(int(ey_top + eh - 1))
                border_w = max(1, _S)
                if is_dimmed:
                    # Task 5: true 1px black/white checkerboard for finished/past
                    # events — block=1 at native draws alternating B/W pixels that
                    # read as ~50% grey and survive the hard threshold exactly.
                    # Draw black fill + checker interior, then a SOLID white
                    # rectangle border on top (not rounded — the rounded corners
                    # were clipping the border on the top and left edges).
                    draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=_sz(12),
                                           fill=BLACK, outline=BLACK, width=border_w)
                    _draw_checker(draw, fx0 + border_w, fy0 + border_w,
                                  fx1 - border_w, fy1 - border_w, block=max(1, _sz(1)))
                    # Solid white rounded border on all four sides — matches
                    # the rounded fill so corners stay rounded. The earlier plain
                    # rectangle left sharp corners that looked wrong next to the
                    # rounded non-dimmed cards.
                    draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=_sz(12),
                                           outline=WHITE, width=border_w)
                else:
                    draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=_sz(12),
                                           fill=BLACK, outline=WHITE, width=border_w)
            elif is_crossed:
                draw.rounded_rectangle([xl, ey_top, xr, ey_top + eh - 1], radius=6,
                                       fill=WHITE, outline=GRAY_DIM, width=2)
            elif is_past:
                draw.rounded_rectangle([xl, ey_top, xr, ey_top + eh - 1], radius=6,
                                       fill=WHITE, outline=GRAY_LIGHT, width=2)
            else:
                draw.rounded_rectangle([xl, ey_top, xr, ey_top + eh - 1], radius=6,
                                       fill=GRAY_VLIGHT, outline=BLACK, width=2)

            # ---- Text for this event (drawn right after its box) ----
            summary = ev.get("summary", "")
            time_str = _ev_time_str(ev, now, time_format)
            avail_w = xr - xl - _sz(8)
            txt_x = xl + _sz(10) + _S  # +1px right (+_S below for +1px down)

            is_overlap = rank > 0
            location = (ev.get("location") or "").strip()
            description = (ev.get("description") or "").strip()
            render_lines = []
            if summary and summary != "(No title)":
                for line in _wrap_text_lines(draw, summary, event_font, avail_w):
                    render_lines.append((line, event_font))
            if time_str:
                if render_lines:
                    render_lines.append(("", None, -3))  # spacing from title (−3px: text block moved up)
                render_lines.append((time_str, event_font))
            if location and not is_overlap:
                if render_lines:
                    render_lines.append(("", None, 0))
                for line in _wrap_text_lines(draw, "@ " + location, event_font_sm, avail_w):
                    render_lines.append((line, event_font_sm))
            if description and not is_overlap:
                if render_lines:
                    render_lines.append(("", None, 0))
                for line in _wrap_text_lines(draw, description, event_font_sm, avail_w):
                    render_lines.append((line, event_font_sm))

            if not render_lines:
                continue

            # Overlap ranges from SHORTER (on-top) events — these cards block this
            # event's text and force it to reflow below them (Task 3).
            overlap_ranges = []
            for o_ev, o_top, o_bot, o_eh, o_dur, o_xl, o_xr, _, _, o_rank in draw_infos:
                if o_dur < duration and o_top < ey_bot and o_bot > ey_top:
                    overlap_ranges.append((o_top, o_bot))

            if bw_mode:
                text_fill = BLACK if is_dimmed else WHITE
            else:
                text_fill = GRAY_MID if is_dimmed else BLACK

            # Per-card text padding: shrink when the card is short so a single
            # title line can still fit (touching the borders) instead of clipping
            # or overflowing into the next cell. Keeps text readable for short
            # (e.g. 30-min) events across the whole modifier 0-10 range.
            card_text_h = ey_bot - ey_top - 2 * _S  # interior between borders
            pad = TEXT_PAD if line_h + 2 * TEXT_PAD <= card_text_h else max(1, (card_text_h - line_h) // 2)
            pad = max(0, pad)
            y = ey_top + pad + _S  # +1px down (text nudged right+down inside the card)
            for item in render_lines:
                text, fnt = item[0], item[1]
                gap_adj = item[2] if len(item) > 2 else 0
                if not text:
                    y += GROUP_GAP + gap_adj
                    continue
                lh = line_h if fnt is event_font else line_h_sm
                glyph_h = _font_line_h(fnt)  # ascent+descent+leading of THIS font
                # Task 3: push text lines that fall inside an on-top card's
                # vertical range down to just below that card (if room remains).
                while True:
                    blocked = False
                    for o_top, o_bot in overlap_ranges:
                        if y >= o_top and y + lh <= o_bot:
                            y = o_bot + _sz(4)
                            blocked = True
                            break
                    if not blocked:
                        break
                    if y + lh > ey_bot - _sz(4):
                        break
                # Fit check: only draw if the glyph fits within the card. Use the
                # glyph height (not the full line_h) so a tight card can still show
                # one title line; if even the glyph doesn't fit, stop (the card is
                # too short for text at this size — the grid still shows the event).
                if y + glyph_h > ey_bot - _S:
                    break  # No room below the overlapping card / card too short
                if bw_mode and not is_dimmed:
                    # White text on the black card — no stroke. The render
                    # pipeline hard-thresholds to pure B/W so AA edges are
                    # eliminated; stroke would just blob the glyphs.
                    draw.text((txt_x, y), text, fill=WHITE, font=fnt)
                elif bw_mode and is_dimmed:
                    # Finished/checkerboard event: black text with a WIDE white
                    # halo so it reads clearly over the 1px B/W checker fill.
                    # Halo scales with the font so it stays proportional at large
                    # modifiers and doesn't swallow small glyphs at modifier 0.
                    draw.text((txt_x, y), text, fill=BLACK, font=fnt,
                              stroke_width=_font_stroke(fnt, base=3) + 2 * _S,
                              stroke_fill=WHITE)
                else:
                    draw.text((txt_x, y), text, fill=text_fill, font=fnt)
                y += lh

            # Redraw the white border ON TOP of the text halo for checkerboard
            # dimmed events — the wide white text stroke (needed for readability
            # over the B/W checker) eats the border on the top and left edges.
            if bw_mode and is_dimmed and dim_style == "checkerboard":
                draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=_sz(12),
                                       outline=WHITE, width=border_w)

    # Full-day events — drawn LAST so they cover everything (day headers, timed events).
    # Multi-day all-day events are expanded to span all their day columns as a
    # single horizontal bar on the same row.
    fd_font = _font(24)
    fd_step = fd_h  # no gap between stacked events

    # Collect unique events + their date span within the visible range.
    visible_dates = [start_date + datetime.timedelta(days=i) for i in range(days)]
    date_to_col = {d: i for i, d in enumerate(visible_dates)}

    # Build unique event list with start/end column indices within the visible range.
    seen_ids = set()
    fd_spans = []  # (ev, col_start, col_end_inclusive)
    for ev in events:
        if not ev["all_day"]:
            continue
        ev_id = ev.get("id", id(ev))
        if ev_id in seen_ids:
            continue
        seen_ids.add(ev_id)
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        end_d = ev.get("end")
        if isinstance(end_d, datetime.datetime):
            end_d = end_d.date()
        if end_d is None:
            end_d = d + datetime.timedelta(days=1)
        # Last day the event covers (end is exclusive)
        last_d = end_d - datetime.timedelta(days=1)
        # Clamp to visible range
        vis_start = max(d, visible_dates[0])
        vis_end = min(last_d, visible_dates[-1])
        if vis_start > visible_dates[-1] or vis_end < visible_dates[0]:
            continue  # entirely outside visible range
        col_start = date_to_col[vis_start]
        col_end = date_to_col[vis_end]
        fd_spans.append((ev, col_start, col_end))

    # Sort by span length (longer first so they get higher row slots), then by
    # start column — so longer all-day events are displayed above shorter ones.
    fd_spans.sort(key=lambda s: (-(s[2] - s[1]), s[1]))

    # Assign row slots greedily — each event needs the same row across all its columns.
    # row_occupied[row] = set of columns already taken
    row_occupied: list[set[int]] = []
    fd_rows: list[int] = []  # row index per fd_span entry
    for ev, col_start, col_end in fd_spans:
        cols_needed = set(range(col_start, col_end + 1))
        assigned = False
        for row_idx, occupied in enumerate(row_occupied):
            if not cols_needed & occupied:
                occupied.update(cols_needed)
                fd_rows.append(row_idx)
                assigned = True
                break
        if not assigned:
            row_occupied.append(cols_needed.copy())
            fd_rows.append(len(row_occupied) - 1)

    # Draw each event as a single bar spanning its day columns.
    max_row = max(fd_rows) if fd_rows else -1
    for (ev, col_start, col_end), row_idx in zip(fd_spans, fd_rows):
        if row_idx >= max_full_day:
            continue  # skip rows beyond the visible limit
        label = ev.get("summary", "")
        if not label or label == "(No title)":
            continue

        fd_count = max_row + 1  # total rows used (for classic-band layout logic)
        if compact_top:
            # Stack straight down, just below the day labels.
            ey = allday_top + row_idx * fd_step
        else:
            # Classic band: single event above header line, 2 below, 3+ overshoot
            if fd_count == 1:
                ey = allday_top - fd_step
            elif row_idx < 2:
                ey = allday_top + row_idx * fd_step
            else:
                ey = allday_top - (row_idx - 1) * fd_step

        # Span from first day's left edge to last day's right edge
        x_start = grid_x + col_start * col_w
        x_end_col = grid_x + (col_end + 1) * col_w
        xl, xr = x_start + _sz(4), x_end_col - _sz(4)
        avail_fd_w = xr - xl - _sz(8)
        display = _fit_fd_text(draw, label, fd_font, avail_fd_w)
        if bw_mode:
            # Black bar with a 1px white border + white text (matches cards).
            draw.rectangle([xl, ey, xr, ey + fd_h - _sz(2)], fill=BLACK,
                           outline=WHITE, width=max(1, _S))
            draw.text((xl + _sz(8), ey + _sz(3)), display, fill=WHITE, font=fd_font)
        else:
            draw.rounded_rectangle([xl, ey, xr, ey + fd_h - _sz(2)], radius=6,
                                   fill=GRAY_VLIGHT, outline=BLACK, width=2)
            draw.text((xl + _sz(8), ey + _sz(3)), display, fill=BLACK, font=fd_font)


def _wrap_text_lines(draw, text, font, max_w):
    """Wrap text to fit max_w pixels, hyphenating long words.

    Returns a list of lines.
    """
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = current + (" " if current else "") + word
        if _text_w(draw, test, font) <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            # Check if word itself overflows — hyphenate
            if _text_w(draw, word, font) > max_w:
                while word:
                    for i in range(len(word), 0, -1):
                        frag = word[:i] + ("-" if i < len(word) else "")
                        if _text_w(draw, frag, font) <= max_w or i == 1:
                            lines.append(frag)
                            word = word[i:]
                            break
                current = ""
            else:
                current = word
    if current:
        lines.append(current)
    return lines


def _fit_fd_text(draw, text, font, max_w):
    """Fit text to one line of max_w, greedily filling with syllable fragments.

    Uses _wrap_text_lines (word + syllable hyphenation) for the first pass,
    then tries to append characters from the next word (with a hyphen) so
    as much of the title as possible is shown. Appends '…' if truncated.
    """
    if not text:
        return ""
    full = text.strip()
    if _text_w(draw, full, font) <= max_w:
        return full
    wrapped = _wrap_text_lines(draw, full, font, max_w)
    if not wrapped:
        return full[:14] + "…"
    display = wrapped[0]
    if len(wrapped) == 1:
        return display
    # There is more text — greedily append chars from the next word with a
    # hyphen so the line shows as much info as possible.  We reserve room
    # for the ellipsis upfront so the final string never overflows.
    ELLIPSIS_W = _text_w(draw, "…", font)
    avail = max_w - ELLIPSIS_W
    next_word = wrapped[1].lstrip()
    if display.endswith("-"):
        display = display[:-1]  # drop trailing hyphen, we'll re-add
    # Try fitting display + hyphen + chars from next_word (without ellipsis)
    while next_word and _text_w(draw, display + "-" + next_word[0], font) <= avail:
        display += "-" + next_word[0]
        next_word = next_word[1:]
    if not next_word:
        # Consumed the entire next word — check if there are more lines
        if len(wrapped) > 2:
            # Make sure ellipsis fits
            while display and _text_w(draw, display + "…", font) > max_w:
                if display.endswith("-"):
                    display = display[:-1]
                else:
                    display = display[:-1]
                    if display.endswith("-"):
                        display = display[:-1]
            display += "…"
        return display
    # Trim display back so display + ellipsis fits in max_w
    while display and _text_w(draw, display + "…", font) > max_w:
        if display.endswith("-"):
            display = display[:-1]
        else:
            display = display[:-1]
            if display.endswith("-"):
                display = display[:-1]
    display += "…"
    return display


def _all_day_span_days(ev: dict) -> int:
    """Number of days an all-day event spans (1 for a single-day event)."""
    d = ev["start"]
    if isinstance(d, datetime.datetime):
        d = d.date()
    end_d = ev.get("end")
    if isinstance(end_d, datetime.datetime):
        end_d = end_d.date()
    if end_d is None:
        return 1
    return max(1, (end_d - d).days)


def _ev_minutes(ev: dict, now: datetime.datetime, start: bool = True) -> int:
    """Get event start/end time in minutes from midnight (local)."""
    dt = ev["start"] if start else ev["end"]
    if isinstance(dt, datetime.datetime):
        # Convert to local if needed
        if dt.tzinfo:
            dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
        return dt.hour * 60 + dt.minute
    if isinstance(dt, datetime.date):
        return 0 if start else 24 * 60
    return 0


def _ev_time_str(ev: dict, now: datetime.datetime, time_format: str = "24h") -> str:
    """Format event time range as 'HH:MM–HH:MM' (start–end).
    Falls back to just the start time if no end time is available."""
    start = ev["start"]
    end = ev.get("end")
    fmt = "%-I:%M" if time_format == "12h" else "%H:%M"

    def _fmt(dt):
        if isinstance(dt, datetime.datetime):
            if dt.tzinfo:
                dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
            return dt.strftime(fmt)
        return ""

    s = _fmt(start)
    e = _fmt(end) if end is not None else ""
    if s and e:
        return f"{s}–{e}"
    return s


def _ev_end_time_str(ev: dict, now: datetime.datetime) -> str:
    """Format event end time as HH:MM."""
    dt = ev["end"]
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo:
            dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%H:%M")
    return ""


def _draw_checker(draw, x0, y0, x1, y1, block=6):
    """Fill a rect with a coarse black/white checkerboard (white blocks over a
    black card). Coarse blocks survive the 1-bit downscale (1px would mush)."""
    if x1 <= x0 or y1 <= y0:
        return
    row = 0
    y = y0
    while y < y1:
        col = 0
        x = x0
        while x < x1:
            if (row + col) % 2 == 0:
                draw.rectangle([x, y, min(x + block - 1, x1), min(y + block - 1, y1)], fill=WHITE)
            x += block
            col += 1
        y += block
        row += 1


def _draw_time_pill(draw, x_right, y_center, text, font):
    """Draw a time label in a white pill sized to fit the text.

    The box is measured from the text's actual bounding box (+ padding) and the
    text is centered inside it, so the pill always contains the text regardless
    of font size. Right edge is anchored at x_right, vertically centered on
    y_center.
    """
    bb = draw.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    th = bb[3] - bb[1]
    pad_x, pad_y = _sz(8), _sz(6)
    box_w = tw + pad_x * 2
    box_h = th + pad_y * 2
    bx1 = int(x_right)
    bx0 = bx1 - box_w
    by0 = int(y_center - box_h // 2)
    by1 = by0 + box_h
    ring = _sz(5)  # black border thickness (scaled; ~1.7px at 3x, 1px native)
    ring = max(1, ring)
    # Solid black border rectangle (all four edges, incl. top), then a white
    # interior with black text: visible on both the white grid and black cards.
    draw.rectangle([bx0 - ring, by0 - ring, bx1 + ring, by1 + ring], fill=BLACK)
    draw.rectangle([bx0, by0, bx1, by1], fill=WHITE)
    draw.text((bx0 + pad_x - bb[0], by0 + pad_y - bb[1]), text, fill=BLACK, font=font)


# ---- Current-time line ----
def _draw_time_line(draw, now, view_mode, day_start, day_end, events, time_format="24h",
                     style="dotted", max_full_day=0, bw_mode=False):
    """Draw a horizontal line at the current time position.

    style: "solid" (thick 4px line), "dotted" (striped, default), "wavy"
    """
    if view_mode == "week":
        today = now.date()
        start_date = today - datetime.timedelta(days=today.weekday())
        col_index = today.weekday()  # 0=Mon
        days = 7
    elif view_mode == "5days":
        start_date = now.date()
        col_index = 0
        days = 5
    else:  # 7days
        start_date = now.date()
        col_index = 0
        days = 7

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m

    now_min = now.hour * 60 + now.minute

    # `days` is set above per view_mode (week=7, 5days=5, 7days=7)

    # Replicate the scaled geometry from _render_day_grid EXACTLY so the time
    # line lands in the right hour cell. compact_top applies to 5days/7days
    # (start_today); the classic header band applies to the week view.
    hour_font = _font(26, bold=True)
    max_label_w = 0
    for h in range(ds_h, de_h + 1):
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        lw = _text_w(draw, label, hour_font)
        if lw > max_label_w:
            max_label_w = lw
    grid_x = max(_sz(60), max_label_w + _sz(14))
    grid_w = W - grid_x - _sz(RIGHT_PAD)
    compact_top = view_mode in ("5days", "7days")  # start_today views
    fd_h = max(_sz(34) if compact_top else _sz(30), _font_line_h(_font(24)) + _sz(2))
    if compact_top:
        label_top = _sz(6)
        label_h = _sz(52)
        allday_top = label_top + label_h + _sz(4)
        reserve = (max_full_day * fd_h) if max_full_day > 0 else 0
        grid_y = allday_top + reserve + _sz(6)
    else:
        grid_y = _sz(HEADER_H) + _sz(50)
    # MUST match _render_day_grid's grid_h exactly — the +20 bottom expansion
    # affects minute_h, which determines the time-line Y position.
    grid_h = H - grid_y - _sz(FOOTER_H) + _sz(20)
    col_w = grid_w // days
    if bw_mode:
        grid_x = _snap(grid_x)
        grid_y = _snap(grid_y)
        grid_h = _snap(grid_h)
        col_w = _snap(col_w)
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60
    minute_h = grid_h / span_min

    x_start = grid_x + col_index * col_w
    x_end = x_start + col_w

    if now_min < ds_min:
        # Before visible range — striped indicator at 15-min mark
        y = int(grid_y + 15 * minute_h)
        # Striped pattern: alternating black/white vertical stripes across column width
        stripe_w = _sz(6)
        for sx in range(x_start, x_end, stripe_w * 2):
            draw.rectangle([sx, y - _sz(4), sx + stripe_w, y + _sz(4)], fill=BLACK)
        # Time label pill
        _draw_time_pill(draw, x_end, y, now.strftime("%H:%M"), _font(28, bold=True))
        return

    if now_min > de_min:
        # After visible range — striped indicator at 45-min mark
        y = int(grid_y + grid_h - 15 * minute_h)
        stripe_w = _sz(6)
        for sx in range(x_start, x_end, stripe_w * 2):
            draw.rectangle([sx, y - _sz(4), sx + stripe_w, y + _sz(4)], fill=BLACK)
        # Time label pill
        _draw_time_pill(draw, x_end, y, now.strftime("%H:%M"), _font(28, bold=True))
        return

    y = grid_y + (now_min - ds_min) * minute_h

    # Draw the time line in the selected style
    y = int(y)
    if style == "solid":
        # Thick solid line with white outline (exact rectangles, no AA)
        _hline(draw, x_start, y, x_end, BLACK, width=_sz(5))
        _hline(draw, x_start, y - _sz(4), x_end, WHITE, width=1)
        _hline(draw, x_start, y + _sz(4), x_end, WHITE, width=1)
    elif style == "wavy":
        # Wavy line: sine-like pattern using small rectangles
        import math
        wave_amp = _sz(4)
        wave_period = _sz(12)
        for sx in range(int(x_start), int(x_end)):
            offset = int(wave_amp * math.sin((sx - x_start) / wave_period * 2 * math.pi))
            draw.point((sx, y + offset), fill=BLACK)
            draw.point((sx, y + offset - 1), fill=BLACK)
        # White outline above/below
        for sx in range(int(x_start), int(x_end), 2):
            offset_top = int(wave_amp * math.sin((sx - x_start) / wave_period * 2 * math.pi))
            draw.point((sx, y + offset_top - _sz(3)), fill=WHITE)
            draw.point((sx, y + offset_top + _sz(3)), fill=WHITE)
    else:
        # Dotted/striped (default). Draw a CONTINUOUS white band first so the
        # line stays visible where it crosses a black event card, then black
        # dashes on top (visible on the white grid).
        draw.rectangle([x_start, y - _sz(5), x_end, y + _sz(5)], fill=WHITE)
        stripe_w = _sz(9)
        for sx in range(int(x_start), int(x_end), stripe_w * 2):
            x2 = min(sx + stripe_w, x_end)
            draw.rectangle([sx, y - _sz(3), x2, y + _sz(3)], fill=BLACK)

    # Small time label at the right edge of the line
    _draw_time_pill(draw, x_end, y, now.strftime("%H:%M"), _font(28, bold=True))


# ---- QR code screen (initial setup) ----
def render_qr_setup(qr_url: str, scheme: str, lan_ip: str, port: int) -> Image.Image:
    """Render the initial-setup screen: QR code + LAN IP:port below it."""
    import qrcode

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Title
    title_font = _font(48, bold=True)
    title = "E-Ink Calendar Setup"
    tw = _text_w(draw, title, title_font)
    draw.text(((W - tw) // 2, 80), title, fill=BLACK, font=title_font,
              stroke_width=2, stroke_fill=BLACK)

    subtitle_font = _font(28)
    subtitle = "Scan QR code or visit the URL below"
    sw = _text_w(draw, subtitle, subtitle_font)
    draw.text(((W - sw) // 2, 150), subtitle, fill=GRAY_DARK, font=subtitle_font,
              stroke_width=2, stroke_fill=GRAY_DARK)

    # QR code (centered)
    qr = qrcode.QRCode(version=1, box_size=12, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 600
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    qr_x = (W - qr_size) // 2
    qr_y = 220
    img.paste(qr_img, (qr_x, qr_y))

    # LAN IP + port below QR code
    ip_font = _font(56, bold=True)
    ip_text = f"{lan_ip}:{port}"
    iw = _text_w(draw, ip_text, ip_font)
    draw.text(((W - iw) // 2, qr_y + qr_size + 40), ip_text, fill=BLACK, font=ip_font,
              stroke_width=2, stroke_fill=BLACK)

    url_font = _font(28)
    url_text = f"{scheme}://{lan_ip}:{port}/settings"
    uw = _text_w(draw, url_text, url_font)
    draw.text(((W - uw) // 2, qr_y + qr_size + 110), url_text, fill=GRAY_DARK, font=url_font,
              stroke_width=2, stroke_fill=GRAY_DARK)

    return img


def render_status(message: str, submessage: str = "") -> Image.Image:
    """Render a simple status/error message screen."""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    font = _font(48, bold=True)
    mw = _text_w(draw, message, font)
    draw.text(((W - mw) // 2, H // 2 - 40), message, fill=BLACK, font=font,
              stroke_width=2, stroke_fill=BLACK)

    if submessage:
        sub_font = _font(28)
        sw = _text_w(draw, submessage, sub_font)
        draw.text(((W - sw) // 2, H // 2 + 30), submessage, fill=GRAY_DARK, font=sub_font,
                  stroke_width=2, stroke_fill=GRAY_DARK)

    return img


def render_setup_required(lan_ip: str, port: int, ssl: bool = False) -> Image.Image:
    """Render the 'Setup Required' screen with Google OAuth instructions.

    Uses 3x supersampling + LANCZOS downscale for smooth antialiased text,
    matching the C IT8951 driver's text rendering technique.
    """
    redirect_uri = "http://localhost:8889/auth/callback"
    scheme = "https" if ssl else "http"
    scale = 3
    sw, sh = W * scale, H * scale

    canvas = Image.new("RGB", (sw, sh), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    x = MARGIN * scale
    y = 36 * scale

    # Title
    title_font = _font(64 * scale, bold=True)
    draw.text((x, y), "Setup Required", fill=BLACK, font=title_font)
    y += 80 * scale

    # Separator
    _hline(draw, x, y, W * scale - MARGIN * scale, GRAY_MID, width=2 * scale)
    y += 36 * scale

    step_font = _font(56 * scale, bold=True)
    text_font = _font(44 * scale)
    code_font = _font(38 * scale)
    indent = x + 24 * scale

    steps = [
        ("step", "1. Create Google OAuth Credentials"),
        ("text", "Go to console.cloud.google.com"),
        ("text", "Create or select a project"),
        ("text", "Enable Google Calendar API"),
        ("text", "Credentials > Create > OAuth client ID"),
        ("text", "Type: Web application"),
        ("text", f"Redirect URI: {redirect_uri}"),
        ("text", "Download client_secret.json"),
        ("blank", ""),
        ("step", "2. Upload to Pi"),
        ("code", f"scp client_secret.json root@{lan_ip}:/opt/eink-calendar/config/"),
        ("blank", ""),
        ("step", "3. Restart the app"),
        ("code", "ssh root@192.168.0.199 'systemctl restart eink-calendar'"),
        ("blank", ""),
        ("step", "After restart:"),
        ("text", "E-ink will show a QR code"),
        ("text", f"Open {scheme}://{lan_ip}:{port}/settings"),
        ("text", "Login with Google, select calendars"),
    ]

    for kind, line in steps:
        if kind == "blank":
            y += 16 * scale
        elif kind == "step":
            draw.text((x, y), line, fill=BLACK, font=step_font)
            y += 64 * scale
        elif kind == "text":
            draw.text((indent, y), line, fill=GRAY_DARK, font=text_font)
            y += 54 * scale
        elif kind == "code":
            tw = _text_w(draw, line, code_font)
            box_w = min(tw + 24 * scale, W * scale - MARGIN * scale - indent + 8 * scale)
            box_h = 48 * scale
            draw.rectangle([indent - 8 * scale, y - 2 * scale,
                           indent - 8 * scale + box_w + 2 * scale, y + box_h + 2 * scale],
                           outline=GRAY_MID, width=2 * scale)
            display = line
            while _text_w(draw, display, code_font) > W * scale - MARGIN * scale - indent - 16 * scale and len(display) > 3:
                display = display[:-1]
            if display != line:
                display = display[:-1] + "…"
            draw.text((indent, y + 4 * scale), display, fill=BLACK, font=code_font)
            y += 56 * scale

    # Downscale with LANCZOS (high-quality cubic filter) — matches C driver's
    # bilinear downscale but produces slightly sharper results at same quality.
    return canvas.resize((W, H), Image.LANCZOS)