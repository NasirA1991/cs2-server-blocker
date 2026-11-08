"""A custom-drawn, zoomable equirectangular world map of CS2 POPs.

No map service or image asset is needed at runtime: Natural Earth's public
domain 1:110m land rings are checked into ``map_data.py`` and drawn with Cairo
on a Gtk.DrawingArea. Dots are clickable; nearby dots collapse into a bubble
that opens a per-POP popover when clicked.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from ..core.model import Mode, Region
from .map_data import WORLD_LAND_RINGS

LAT_MIN = -58.0
LAT_MAX = 84.0
ZOOM_MIN = 1.0
ZOOM_MAX = 14.0
MAP_PAD = 14.0
OCEAN_TOP = (0.017, 0.055, 0.095)
OCEAN_BOTTOM = (0.018, 0.105, 0.160)
LAND_FILL = (0.070, 0.155, 0.205)
LAND_STROKE = (0.23, 0.46, 0.56, 0.55)
GRID = (0.55, 0.78, 0.90, 0.055)
BLOCKED = (0.94, 0.30, 0.32)
ALLOWED = (0.20, 0.82, 0.58)
AMBER = (0.98, 0.72, 0.28)
TEXT = (0.90, 0.95, 1.0)
DIM = (0.63, 0.74, 0.82)
PENDING = (0.86, 0.70, 0.38)
ACCENT = (0.48, 0.72, 0.96)


@dataclass(frozen=True, slots=True)
class MapEntry:
    code: str
    title: str
    subtitle: str
    region: Region
    lon: float
    lat: float
    relays: int
    ports: str
    country: str = ""
    city: str = ""


@dataclass
class _Target:
    x: float
    y: float
    radius: float
    entries: tuple[MapEntry, ...]
    cluster: bool


class WorldMap(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(760, 380)
        self.set_focusable(True)
        self.entries: tuple[MapEntry, ...] = ()
        self.selected: set[str] = set()
        self.mode = Mode.BLOCK
        self.pings: dict[str, float | None] = {}
        self.counters: dict[str, int] = {}
        self.pending: set[str] = set()
        self.ping_cap: float | None = None
        self._pulses: dict[str, float] = {}
        self._pulse_tick_id = 0
        self.focus_code: str | None = None
        self.hover_code: str | None = None
        self.zoom = 1.0
        self.center_lon = 0.0
        self.center_lat = (LAT_MIN + LAT_MAX) / 2.0
        self.on_toggle: Callable[[str], None] | None = None
        self.on_focus: Callable[[str | None], None] | None = None
        self.on_cluster: Callable[[tuple[MapEntry, ...], float, float], None] | None = None
        self._targets: list[_Target] = []
        self._hover_target: _Target | None = None
        self._map_rect = (0.0, 0.0, 1.0, 1.0)
        self._drag_start: tuple[float, float] | None = None
        self._drag_last: tuple[float, float] | None = None
        self._dragged = False
        self._press_target: _Target | None = None
        self._width = 1
        self._height = 1
        self._land_bboxes = [
            (
                min(p[0] for p in ring),
                max(p[0] for p in ring),
                min(p[1] for p in ring),
                max(p[1] for p in ring),
                ring,
            )
            for ring in WORLD_LAND_RINGS
        ]

        self.set_draw_func(self._draw)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # ------------------------------------------------------------------ data
    def set_data(
        self,
        entries: tuple[MapEntry, ...],
        *,
        selected: set[str],
        mode: Mode,
        pings: dict[str, float | None],
        counters: dict[str, int],
        focus: str | None = None,
        pending: set[str] | None = None,
    ) -> None:
        self.entries = entries
        self.selected = set(selected)
        self.mode = mode
        self.pings = dict(pings)
        self.counters = dict(counters)
        self.pending = set(pending or ())
        self.focus_code = focus
        self.queue_draw()

    def set_selection(self, selected: set[str], mode: Mode) -> None:
        self.selected = set(selected)
        self.mode = mode
        self.queue_draw()

    def set_pings(self, pings: dict[str, float | None]) -> None:
        self.pings = dict(pings)
        self.queue_draw()

    def set_pending(self, pending: set[str]) -> None:
        self.pending = set(pending)
        self.queue_draw()

    def set_ping_cap(self, cap: float | None) -> None:
        self.ping_cap = cap
        self.queue_draw()

    def set_counters(self, counters: dict[str, int]) -> None:
        now = time.monotonic()
        for code, value in counters.items():
            if value > self.counters.get(code, 0) and self._is_blocked(code):
                self._pulses[code] = now + 1.15
        self.counters = dict(counters)
        if self._pulses and not self._pulse_tick_id:
            self._pulse_tick_id = GLib.timeout_add(55, self._tick_pulses)
        self.queue_draw()

    def _tick_pulses(self) -> bool:
        now = time.monotonic()
        self._pulses = {code: end for code, end in self._pulses.items() if end > now}
        self.queue_draw()
        if self._pulses:
            return True
        self._pulse_tick_id = 0
        return False

    def set_focus(self, code: str | None) -> None:
        self.focus_code = code
        self.queue_draw()

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.center_lon = 0.0
        self.center_lat = (LAT_MIN + LAT_MAX) / 2.0
        self.queue_draw()

    def zoom_in(self) -> None:
        self._zoom_around(self._width / 2, self._height / 2, 1.35)

    def zoom_out(self) -> None:
        self._zoom_around(self._width / 2, self._height / 2, 1 / 1.35)

    def zoom_to_region(self, region: Region) -> None:
        points = [e for e in self.entries if e.region is region]
        self._zoom_to_entries(points)

    def zoom_to_codes(self, codes: set[str]) -> None:
        points = [e for e in self.entries if e.code in codes]
        self._zoom_to_entries(points)

    def _zoom_to_entries(self, entries: list[MapEntry]) -> None:
        if not entries:
            return
        lons = [e.lon for e in entries]
        lats = [e.lat for e in entries]
        span_lon = max(12.0, max(lons) - min(lons) + 14.0)
        span_lat = max(8.0, max(lats) - min(lats) + 10.0)
        self.zoom = max(1.0, min(ZOOM_MAX, min(360.0 / span_lon, (LAT_MAX - LAT_MIN) / span_lat)))
        self.center_lon = (min(lons) + max(lons)) / 2.0
        self.center_lat = (min(lats) + max(lats)) / 2.0
        self._clamp_view()
        self.queue_draw()

    # -------------------------------------------------------------- projection
    def _spans(self) -> tuple[float, float]:
        return 360.0 / self.zoom, (LAT_MAX - LAT_MIN) / self.zoom

    def _clamp_view(self) -> None:
        lon_span, lat_span = self._spans()
        half_lon = lon_span / 2.0
        half_lat = lat_span / 2.0
        if lon_span < 360.0:
            self.center_lon = max(-180.0 + half_lon, min(180.0 - half_lon, self.center_lon))
        else:
            self.center_lon = 0.0
        if lat_span < LAT_MAX - LAT_MIN:
            self.center_lat = max(LAT_MIN + half_lat, min(LAT_MAX - half_lat, self.center_lat))
        else:
            self.center_lat = (LAT_MIN + LAT_MAX) / 2.0

    def _project(self, lon: float, lat: float) -> tuple[float, float]:
        x0, y0, w, h = self._map_rect
        lon_span, lat_span = self._spans()
        x = x0 + ((lon - (self.center_lon - lon_span / 2.0)) / lon_span) * w
        y = y0 + (((self.center_lat + lat_span / 2.0) - lat) / lat_span) * h
        return x, y

    def _unproject(self, x: float, y: float) -> tuple[float, float]:
        x0, y0, w, h = self._map_rect
        lon_span, lat_span = self._spans()
        lon = (self.center_lon - lon_span / 2.0) + ((x - x0) / w) * lon_span
        lat = (self.center_lat + lat_span / 2.0) - ((y - y0) / h) * lat_span
        return lon, lat

    # ------------------------------------------------------------------ events
    def _on_motion(self, _controller, x: float, y: float) -> None:
        self._last_x, self._last_y = x, y
        target = self._hit(x, y)
        code = target.entries[0].code if target and len(target.entries) == 1 else None
        if code != self.hover_code or target is not self._hover_target:
            self.hover_code = code
            self._hover_target = target
            self.set_tooltip_text(None)
            self.queue_draw()

    def _on_leave(self, _controller) -> None:
        self.hover_code = None
        self._hover_target = None
        self.queue_draw()

    def _on_scroll(self, _controller, _dx: float, dy: float) -> bool:
        factor = 1.18 ** (-dy)
        self._zoom_around(self._pointer_x, self._pointer_y, factor)
        return True

    def _on_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal):
            self.zoom_in()
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_underscore):
            self.zoom_out()
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_Home):
            self.reset_view()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.on_focus:
                self.on_focus(None)
            return True
        order = [entry for entry in self.entries if entry.code in self._visible_codes()]
        if not order:
            return False
        codes = [entry.code for entry in order]
        if self.focus_code in codes:
            index = codes.index(self.focus_code)
        else:
            index = -1
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Tab):
            index = (index + 1) % len(codes)
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            index = (index - 1) % len(codes)
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            if self.focus_code and self.on_toggle:
                self.on_toggle(self.focus_code)
            return True
        else:
            return False
        code = codes[index]
        self.focus_code = code
        if self.on_focus:
            self.on_focus(code)
        self.queue_draw()
        return True

    def _visible_codes(self) -> set[str]:
        if not self._targets:
            return {entry.code for entry in self.entries}
        return {entry.code for target in self._targets for entry in target.entries}

    def _on_drag_begin(self, _gesture, start_x: float, start_y: float) -> None:
        self.grab_focus()
        self._drag_start = (start_x, start_y)
        self._drag_last = (start_x, start_y)
        self._dragged = False
        self._press_target = self._hit(start_x, start_y)

    def _on_drag_update(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._drag_start is None or self._drag_last is None:
            return
        if abs(offset_x) + abs(offset_y) > 5:
            self._dragged = True
        dx = offset_x - (self._drag_last[0] - self._drag_start[0])
        dy = offset_y - (self._drag_last[1] - self._drag_start[1])
        self._drag_last = (self._drag_start[0] + offset_x, self._drag_start[1] + offset_y)
        if self._dragged:
            lon_span, lat_span = self._spans()
            self.center_lon -= dx / self._map_rect[2] * lon_span
            self.center_lat += dy / self._map_rect[3] * lat_span
            self._clamp_view()
            self.queue_draw()

    def _on_drag_end(self, _gesture, _offset_x: float, _offset_y: float) -> None:
        if not self._dragged and self._press_target is not None:
            target = self._press_target
            if target.cluster:
                if self.on_cluster is not None:
                    self.on_cluster(target.entries, target.x, target.y)
            elif target.entries:
                entry = target.entries[0]
                if self.on_toggle:
                    self.on_toggle(entry.code)
                if self.on_focus:
                    self.on_focus(entry.code)
        elif not self._dragged and self._press_target is None:
            if self.on_focus:
                self.on_focus(None)
        self._drag_start = None
        self._drag_last = None
        self._press_target = None
        self._dragged = False
        self.queue_draw()

    @property
    def _pointer_x(self) -> float:
        return getattr(self, "_last_x", self._width / 2)

    @property
    def _pointer_y(self) -> float:
        return getattr(self, "_last_y", self._height / 2)

    def _hit(self, x: float, y: float) -> _Target | None:
        self._last_x, self._last_y = x, y
        for target in reversed(self._targets):
            if math.hypot(x - target.x, y - target.y) <= target.radius + 5:
                return target
        return None

    def _zoom_around(self, x: float, y: float, factor: float) -> None:
        if self._map_rect[2] <= 0:
            return
        lon, lat = self._unproject(x, y)
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        if abs(new_zoom - self.zoom) < 1e-6:
            return
        self.zoom = new_zoom
        lon_span, lat_span = self._spans()
        x0, y0, w, h = self._map_rect
        self.center_lon = lon + lon_span / 2.0 - ((x - x0) / w) * lon_span
        self.center_lat = lat - lat_span / 2.0 + ((y - y0) / h) * lat_span
        self._clamp_view()
        self.queue_draw()

    # ------------------------------------------------------------------ paint
    def _draw(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        self._width, self._height = max(1, width), max(1, height)
        aspect = 360.0 / (LAT_MAX - LAT_MIN)
        available_w = max(1.0, width - 2 * MAP_PAD)
        available_h = max(1.0, height - 2 * MAP_PAD)
        if available_w / available_h > aspect:
            map_h = available_h
            map_w = map_h * aspect
        else:
            map_w = available_w
            map_h = map_w / aspect
        x0 = (width - map_w) / 2.0
        y0 = (height - map_h) / 2.0
        self._map_rect = (x0, y0, map_w, map_h)
        self._clamp_view()

        cr.save()
        self._rounded_rect(cr, x0, y0, map_w, map_h, 16)
        cr.clip()
        gradient = cairo.LinearGradient(0, y0, 0, y0 + map_h)
        gradient.add_color_stop_rgb(0, *OCEAN_TOP)
        gradient.add_color_stop_rgb(1, *OCEAN_BOTTOM)
        cr.set_source(gradient)
        cr.paint()
        self._draw_grid(cr)
        self._draw_land(cr)
        self._draw_entries(cr)
        self._draw_empty_state(cr, width, height)
        cr.restore()
        self._draw_border(cr, x0, y0, map_w, map_h)

    @staticmethod
    def _rounded_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw_border(self, cr: cairo.Context, x: float, y: float, w: float, h: float) -> None:
        cr.save()
        self._rounded_rect(cr, x, y, w, h, 16)
        cr.set_source_rgba(0.45, 0.70, 0.82, 0.20)
        cr.set_line_width(1.0)
        cr.stroke()
        cr.restore()

    def _draw_grid(self, cr: cairo.Context) -> None:
        cr.save()
        cr.set_line_width(1.0)
        cr.set_source_rgba(*GRID)
        lon_span, lat_span = self._spans()
        lon_start = max(-180, math.floor((self.center_lon - lon_span / 2) / 30) * 30)
        lon_end = min(180, math.ceil((self.center_lon + lon_span / 2) / 30) * 30)
        for lon in range(int(lon_start), int(lon_end) + 1, 30):
            x, _ = self._project(lon, 0)
            cr.move_to(x, self._map_rect[1])
            cr.line_to(x, self._map_rect[1] + self._map_rect[3])
        lat_start = max(LAT_MIN, math.floor((self.center_lat - lat_span / 2) / 20) * 20)
        lat_end = min(LAT_MAX, math.ceil((self.center_lat + lat_span / 2) / 20) * 20)
        for lat in range(int(lat_start), int(lat_end) + 1, 20):
            _, y = self._project(0, lat)
            cr.move_to(self._map_rect[0], y)
            cr.line_to(self._map_rect[0] + self._map_rect[2], y)
        cr.stroke()
        cr.restore()

    def _draw_land(self, cr: cairo.Context) -> None:
        lon_span, lat_span = self._spans()
        visible_lon = (self.center_lon - lon_span / 2, self.center_lon + lon_span / 2)
        visible_lat = (self.center_lat - lat_span / 2, self.center_lat + lat_span / 2)
        cr.save()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.new_path()
        for min_lon, max_lon, min_lat, max_lat, ring in self._land_bboxes:
            if max_lon < visible_lon[0] - 10 or min_lon > visible_lon[1] + 10:
                continue
            if max_lat < visible_lat[0] - 10 or min_lat > visible_lat[1] + 10:
                continue
            first = True
            for lon, lat in ring:
                x, y = self._project(lon, lat)
                if first:
                    cr.move_to(x, y)
                    first = False
                else:
                    cr.line_to(x, y)
            cr.close_path()
        cr.set_source_rgb(*LAND_FILL)
        cr.fill_preserve()
        cr.set_source_rgba(*LAND_STROKE)
        cr.set_line_width(0.75)
        cr.stroke()
        cr.restore()

    def _is_blocked(self, code: str) -> bool:
        if self.mode is Mode.BLOCK:
            return code in self.selected
        return code not in self.selected

    def _state_color(self, entries: tuple[MapEntry, ...]) -> tuple[float, float, float]:
        blocked = sum(1 for entry in entries if self._is_blocked(entry.code))
        if blocked == 0:
            return ALLOWED
        if blocked == len(entries):
            return BLOCKED
        return AMBER

    @staticmethod
    def _ping_color(ping: float | None) -> tuple[float, float, float]:
        if ping is None:
            return DIM
        if ping <= 50.0:
            return (0.30, 0.82, 0.62)
        if ping <= 100.0:
            return (0.92, 0.70, 0.34)
        return (0.88, 0.40, 0.42)

    def _draw_entries(self, cr: cairo.Context) -> None:
        self._targets = []
        visible: list[tuple[MapEntry, float, float]] = []
        x0, y0, w, h = self._map_rect
        for entry in self.entries:
            x, y = self._project(entry.lon, entry.lat)
            if x0 - 30 <= x <= x0 + w + 30 and y0 - 30 <= y <= y0 + h + 30:
                visible.append((entry, x, y))

        clusters: list[list[tuple[MapEntry, float, float]]] = []
        threshold = 22.0
        for item in sorted(visible, key=lambda item: item[0].code):
            for cluster in clusters:
                cx = sum(p[1] for p in cluster) / len(cluster)
                cy = sum(p[2] for p in cluster) / len(cluster)
                if math.hypot(item[1] - cx, item[2] - cy) <= threshold:
                    cluster.append(item)
                    break
            else:
                clusters.append([item])

        now = time.monotonic()
        for cluster in clusters:
            entries = tuple(item[0] for item in cluster)
            cx = sum(item[1] for item in cluster) / len(cluster)
            cy = sum(item[2] for item in cluster) / len(cluster)
            color = self._state_color(entries)
            is_cluster = len(entries) > 1
            focused = any(entry.code == self.focus_code for entry in entries) and not is_cluster
            hovered = self._hover_target is not None and entries[0] in self._hover_target.entries
            pending = any(entry.code in self.pending for entry in entries)

            if is_cluster:
                radius = 9.0 + min(6.0, math.sqrt(len(entries)) * 2.0)
                cr.set_source_rgba(0.025, 0.07, 0.10, 0.96)
                cr.arc(cx, cy, radius, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(*color)
                cr.set_line_width(1.5)
                cr.arc(cx, cy, radius, 0, 2 * math.pi)
                cr.stroke()
                cr.set_source_rgb(0.86, 0.92, 0.96)
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(9.5 + min(2.0, len(entries) * 0.12))
                text = str(len(entries))
                ext = cr.text_extents(text)
                cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
                cr.show_text(text)
                if pending:
                    self._draw_dashed_ring(cr, cx, cy, radius + 4.0, PENDING, 1.1)
                target = _Target(cx, cy, radius + 2, entries, True)
                self._targets.append(target)
                if hovered:
                    self._draw_cluster_hint(cr, cx, cy, radius, len(entries), pending)
                continue

            entry = entries[0]
            blocked = self._is_blocked(entry.code)
            pending = entry.code in self.pending
            ping = self.pings.get(entry.code)
            base_radius = 4.7 if (focused or hovered) else 4.1
            if hovered:
                base_radius += 0.5

            if focused or hovered:
                glow = cairo.RadialGradient(cx, cy, base_radius, cx, cy, base_radius * 3.2)
                glow.add_color_stop_rgba(0, *(BLOCKED if blocked else ALLOWED), 0.18)
                glow.add_color_stop_rgba(1, *(BLOCKED if blocked else ALLOWED), 0.0)
                cr.set_source(glow)
                cr.arc(cx, cy, base_radius * 3.2, 0, 2 * math.pi)
                cr.fill()

            if blocked:
                # Blocked is a hollow ring plus a slash, not a red filled dot.
                cr.set_source_rgba(0.025, 0.07, 0.10, 0.96)
                cr.arc(cx, cy, base_radius, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(*BLOCKED)
                cr.set_line_width(1.8)
                cr.arc(cx, cy, base_radius, 0, 2 * math.pi)
                cr.stroke()
                cr.set_line_width(1.4)
                cr.move_to(cx - base_radius * 0.62, cy + base_radius * 0.62)
                cr.line_to(cx + base_radius * 0.62, cy - base_radius * 0.62)
                cr.stroke()
            else:
                cr.set_source_rgb(*ALLOWED)
                cr.arc(cx, cy, base_radius, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.03, 0.10, 0.09, 0.85)
                cr.set_line_width(0.9)
                cr.arc(cx, cy, base_radius - 0.5, 0, 2 * math.pi)
                cr.stroke()

            if ping is not None:
                cr.set_source_rgba(*self._ping_color(ping), 0.62)
                cr.set_line_width(1.05)
                cr.arc(cx, cy, base_radius + 2.0, 0, 2 * math.pi)
                cr.stroke()

            if self.ping_cap is not None and ping is not None and ping <= self.ping_cap:
                cr.set_source_rgba(*ACCENT, 0.86)
                cr.set_line_width(1.2)
                cr.arc(cx, cy, base_radius + 3.8, 0, 2 * math.pi)
                cr.stroke()

            if focused:
                cr.set_source_rgba(0.92, 0.97, 1.0, 0.95)
                cr.set_line_width(1.2)
                cr.arc(cx, cy, base_radius + 2.8 if ping is None else base_radius + 5.2, 0, 2 * math.pi)
                cr.stroke()

            if pending:
                self._draw_dashed_ring(cr, cx, cy, base_radius + 4.2, PENDING, 1.0)

            pulse_until = self._pulses.get(entry.code)
            if pulse_until is not None and pulse_until > now and blocked:
                remaining = max(0.0, pulse_until - now)
                progress = 1.0 - remaining / 1.15
                cr.set_source_rgba(*BLOCKED, max(0.0, 0.65 * (1.0 - progress)))
                cr.set_line_width(1.8)
                cr.arc(cx, cy, base_radius + 3.0 + progress * 9.0, 0, 2 * math.pi)
                cr.stroke()

            target = _Target(cx, cy, max(8.0, base_radius + 4.0), entries, False)
            self._targets.append(target)
            if focused or hovered:
                self._draw_callout(cr, entry, cx, cy, base_radius, pending)

    def _draw_dashed_ring(
        self, cr: cairo.Context, x: float, y: float, radius: float,
        color: tuple[float, float, float], width: float,
    ) -> None:
        cr.save()
        cr.set_dash([2.6, 2.6])
        cr.set_source_rgba(*color, 0.95)
        cr.set_line_width(width)
        cr.arc(x, y, radius, 0, 2 * math.pi)
        cr.stroke()
        cr.restore()

    def _draw_cluster_hint(
        self, cr: cairo.Context, x: float, y: float, radius: float,
        count: int, pending: bool,
    ) -> None:
        text = f"{count} datacenters" + (" · pending" if pending else "")
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9.5)
        ext = cr.text_extents(text)
        width = ext.width + 12
        height = 18.0
        left = max(self._map_rect[0] + 3, min(self._map_rect[0] + self._map_rect[2] - width - 3, x - width / 2))
        top = y - radius - height - 6
        if top < self._map_rect[1] + 3:
            top = y + radius + 6
        self._rounded_rect(cr, left, top, width, height, 5)
        cr.set_source_rgba(0.015, 0.045, 0.07, 0.95)
        cr.fill()
        cr.set_source_rgba(0.42, 0.58, 0.70, 0.55)
        cr.set_line_width(0.7)
        cr.stroke()
        cr.set_source_rgba(0.82, 0.90, 0.95, 0.95)
        cr.move_to(left + 6, top + 12)
        cr.show_text(text)
        cr.restore()

    def _draw_callout(
        self, cr: cairo.Context, entry: MapEntry, x: float, y: float,
        radius: float, pending: bool,
    ) -> None:
        blocked = self._is_blocked(entry.code)
        state = "Blocked" if blocked else "Allowed"
        state_color = BLOCKED if blocked else ALLOWED
        ping = self.pings.get(entry.code)
        ping_text = "ping …" if ping is None else f"{ping:.0f} ms"
        pending_text = "  ·  pending" if pending else ""
        lines = [
            (entry.title, 12.5, True, TEXT),
            (entry.subtitle, 9.5, False, DIM),
            (f"{state}  ·  {ping_text}{pending_text}", 9.5, True, state_color),
        ]
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        width = 178.0
        for text, size, _bold, _color in lines:
            cr.set_font_size(size)
            width = max(width, cr.text_extents(text).width + 22)
        width = min(width, 300.0)
        height = 52.0
        x0 = x - width / 2
        y0 = y - radius - height - 8
        if y0 < self._map_rect[1] + 5:
            y0 = y + radius + 8
        x0 = max(self._map_rect[0] + 5, min(self._map_rect[0] + self._map_rect[2] - width - 5, x0))
        y0 = max(self._map_rect[1] + 5, min(self._map_rect[1] + self._map_rect[3] - height - 5, y0))
        self._rounded_rect(cr, x0, y0, width, height, 7)
        cr.set_source_rgba(0.015, 0.045, 0.07, 0.96)
        cr.fill_preserve()
        cr.set_source_rgba(0.32, 0.46, 0.56, 0.55)
        cr.set_line_width(0.8)
        cr.stroke()
        ty = y0 + 16
        for text, size, bold, color in lines:
            cr.set_source_rgb(*color)
            cr.select_font_face(
                "Sans",
                cairo.FONT_SLANT_NORMAL,
                cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL,
            )
            cr.set_font_size(size)
            while cr.text_extents(text).width > width - 20 and len(text) > 5:
                text = text[:-2] + "…"
            cr.move_to(x0 + 10, ty)
            cr.show_text(text)
            ty += size + 5
        cr.restore()

    def _draw_empty_state(self, cr: cairo.Context, width: int, height: int) -> None:
        if self.entries:
            return
        cr.save()
        cr.set_source_rgba(*DIM)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(14)
        text = "No relay data loaded"
        ext = cr.text_extents(text)
        cr.move_to((width - ext.width) / 2, height / 2)
        cr.show_text(text)
        cr.restore()
