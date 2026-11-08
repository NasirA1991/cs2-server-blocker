"""A custom-drawn, zoomable equirectangular world map of CS2 POPs.

No map service or image asset is needed at runtime: Natural Earth's public
domain 1:110m land rings are checked into ``map_data.py`` and drawn with Cairo
on a Gtk.DrawingArea.  Dots are clickable; nearby dots collapse into a bubble
that zooms in when clicked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

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
REGION_ANCHORS: dict[Region, tuple[float, float]] = {
    Region.EUROPE: (15.0, 53.0),
    Region.NORTH_AMERICA: (-101.0, 44.0),
    Region.SOUTH_AMERICA: (-60.0, -16.0),
    Region.ASIA: (94.0, 33.0),
    Region.OCEANIA: (140.0, -28.0),
    Region.AFRICA: (19.0, 2.0),
    Region.MIDDLE_EAST: (46.0, 25.0),
}


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
    ) -> None:
        self.entries = entries
        self.selected = set(selected)
        self.mode = mode
        self.pings = dict(pings)
        self.counters = dict(counters)
        self.focus_code = focus
        self.queue_draw()

    def set_selection(self, selected: set[str], mode: Mode) -> None:
        self.selected = set(selected)
        self.mode = mode
        self.queue_draw()

    def set_pings(self, pings: dict[str, float | None]) -> None:
        self.pings = dict(pings)
        self.queue_draw()

    def set_counters(self, counters: dict[str, int]) -> None:
        self.counters = dict(counters)
        self.queue_draw()

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
            if target is None:
                self.set_tooltip_text(None)
            elif target.cluster:
                self.set_tooltip_text(
                    f"{len(target.entries)} datacenters · click to zoom in"
                )
            else:
                entry = target.entries[0]
                ping = self.pings.get(entry.code)
                ping_text = "ping …" if ping is None else f"{ping:.0f} ms"
                self.set_tooltip_text(
                    f"{entry.title}\n{entry.region.value} · {entry.relays} relays · {ping_text}"
                )
            self.queue_draw()

    def _on_leave(self, _controller) -> None:
        self.hover_code = None
        self._hover_target = None
        self.queue_draw()

    def _on_scroll(self, _controller, _dx: float, dy: float) -> bool:
        factor = 1.18 ** (-dy)
        self._zoom_around(self._pointer_x, self._pointer_y, factor)
        return True

    def _on_drag_begin(self, _gesture, start_x: float, start_y: float) -> None:
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
                if (
                    len(target.entries) <= 4
                    and self.zoom >= 2.5
                    and self.on_cluster is not None
                ):
                    self.on_cluster(target.entries, target.x, target.y)
                else:
                    self._zoom_to_entries(list(target.entries))
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
        self._draw_region_labels(cr)
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

    def _draw_region_labels(self, cr: cairo.Context) -> None:
        if self.zoom > 3.2:
            return
        cr.save()
        for region, (lon, lat) in REGION_ANCHORS.items():
            points = [entry for entry in self.entries if entry.region is region]
            if not points:
                continue
            blocked = sum(1 for entry in points if self._is_blocked(entry.code))
            allowed = len(points) - blocked
            color = ALLOWED if blocked == 0 else (BLOCKED if blocked == len(points) else AMBER)
            name = region.value.upper()
            count = f"{allowed} of {len(points)} allowed"
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(10.0)
            name_width = cr.text_extents(name).width
            cr.set_font_size(9.0)
            count_width = cr.text_extents(count).width
            width = max(name_width, count_width) + 14
            height = 30.0
            x, y = self._project(lon, lat)
            x0 = max(
                self._map_rect[0] + 4,
                min(self._map_rect[0] + self._map_rect[2] - width - 4, x - width / 2),
            )
            y0 = max(
                self._map_rect[1] + 4,
                min(self._map_rect[1] + self._map_rect[3] - height - 4, y - height / 2),
            )
            self._rounded_rect(cr, x0, y0, width, height, 6)
            cr.set_source_rgba(0.015, 0.06, 0.10, 0.62)
            cr.fill()
            cr.set_source_rgba(*color, 0.55)
            cr.set_line_width(0.6)
            cr.stroke()
            cr.set_source_rgba(0.78, 0.88, 0.94, 0.72)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(10.0)
            cr.move_to(x0 + 7, y0 + 12)
            cr.show_text(name)
            cr.set_source_rgba(*color, 0.95)
            cr.set_font_size(9.0)
            cr.move_to(x0 + 7, y0 + 23)
            cr.show_text(count)
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

    def _draw_entries(self, cr: cairo.Context) -> None:
        self._targets = []
        visible: list[tuple[MapEntry, float, float]] = []
        x0, y0, w, h = self._map_rect
        for entry in self.entries:
            x, y = self._project(entry.lon, entry.lat)
            if x0 - 30 <= x <= x0 + w + 30 and y0 - 30 <= y <= y0 + h + 30:
                visible.append((entry, x, y))
        clusters: list[list[tuple[MapEntry, float, float]]] = []
        threshold = 24.0
        for item in sorted(visible, key=lambda item: item[0].code):
            for cluster in clusters:
                cx = sum(p[1] for p in cluster) / len(cluster)
                cy = sum(p[2] for p in cluster) / len(cluster)
                if math.hypot(item[1] - cx, item[2] - cy) <= threshold:
                    cluster.append(item)
                    break
            else:
                clusters.append([item])

        for cluster in clusters:
            entries = tuple(item[0] for item in cluster)
            cx = sum(item[1] for item in cluster) / len(cluster)
            cy = sum(item[2] for item in cluster) / len(cluster)
            color = self._state_color(entries)
            is_cluster = len(entries) > 1
            focused = any(e.code == self.focus_code for e in entries) and not is_cluster
            hovered = self._hover_target is not None and entries[0] in self._hover_target.entries
            radius = (11.0 + min(8.0, math.sqrt(len(entries)) * 3.0)) if is_cluster else (7.2 if focused else 5.8)
            if hovered:
                radius += 1.5
            glow = cairo.RadialGradient(cx, cy, radius * 0.4, cx, cy, radius * 2.6)
            glow.add_color_stop_rgba(0, *color, 0.30)
            glow.add_color_stop_rgba(1, *color, 0.0)
            cr.set_source(glow)
            cr.arc(cx, cy, radius * 2.6, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgb(*color)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(0.95, 0.99, 1.0, 0.92 if focused or hovered else 0.55)
            cr.set_line_width(1.2 if focused else 0.8)
            cr.arc(cx, cy, radius - 0.6, 0, 2 * math.pi)
            cr.stroke()
            if is_cluster:
                cr.set_source_rgb(0.02, 0.08, 0.12)
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(10.0 + min(3.0, len(entries) * 0.2))
                text = str(len(entries))
                ext = cr.text_extents(text)
                cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
                cr.show_text(text)
            target = _Target(cx, cy, radius, entries, is_cluster)
            self._targets.append(target)
            if focused or (hovered and not is_cluster):
                self._draw_callout(cr, entries[0], cx, cy, radius)

        focus = next((entry for entry in self.entries if entry.code == self.focus_code), None)
        if focus is not None:
            x, y = self._project(focus.lon, focus.lat)
            if x0 - 20 <= x <= x0 + w + 20 and y0 - 20 <= y <= y0 + h + 20:
                self._draw_ping_pill(cr, x, y + 15)

    def _draw_ping_pill(self, cr: cairo.Context, x: float, y: float) -> None:
        ping = self.pings.get(self.focus_code or "")
        text = "ping …" if ping is None else f"{ping:.0f} ms"
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10.5)
        ext = cr.text_extents(text)
        width = ext.width + 16
        height = 20
        x = max(self._map_rect[0] + 4, min(self._map_rect[0] + self._map_rect[2] - width - 4, x - width / 2))
        y = min(self._map_rect[1] + self._map_rect[3] - height - 4, y)
        self._rounded_rect(cr, x, y, width, height, 9)
        cr.set_source_rgba(0.02, 0.07, 0.11, 0.88)
        cr.fill()
        cr.set_source_rgba(0.55, 0.78, 0.90, 0.35)
        cr.set_line_width(0.8)
        cr.stroke()
        cr.set_source_rgb(0.80, 0.92, 0.98)
        cr.move_to(x + 8, y + 14)
        cr.show_text(text)
        cr.restore()

    def _draw_callout(self, cr: cairo.Context, entry: MapEntry, x: float, y: float, radius: float) -> None:
        blocked = self._is_blocked(entry.code)
        state = "Blocked" if blocked else "Allowed"
        ping = self.pings.get(entry.code)
        ping_text = "ping …" if ping is None else f"{ping:.0f} ms"
        count = self.counters.get(entry.code, 0)
        lines = [
            (entry.title, 14.0, True, TEXT),
            (entry.subtitle, 10.5, False, DIM),
            (f"{entry.region.value}  ·  {entry.relays} relays  ·  {ping_text}  ·  {count:,} drops", 9.5, False, DIM),
        ]
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10.5)
        width = max(238.0, cr.text_extents(lines[2][0]).width + 24)
        height = 66.0
        x0 = x - width / 2
        y0 = y - radius - height - 12
        if y0 < self._map_rect[1] + 6:
            y0 = y + radius + 12
        x0 = max(self._map_rect[0] + 6, min(self._map_rect[0] + self._map_rect[2] - width - 6, x0))
        y0 = max(self._map_rect[1] + 6, min(self._map_rect[1] + self._map_rect[3] - height - 6, y0))
        self._rounded_rect(cr, x0, y0, width, height, 12)
        cr.set_source_rgba(0.015, 0.06, 0.10, 0.94)
        cr.fill_preserve()
        cr.set_source_rgba(0.38, 0.66, 0.80, 0.40)
        cr.set_line_width(0.9)
        cr.stroke()
        ty = y0 + 20
        for text, size, bold, color in lines:
            cr.set_source_rgb(*color)
            cr.select_font_face(
                "Sans",
                cairo.FONT_SLANT_NORMAL,
                cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL,
            )
            cr.set_font_size(size)
            max_width = width - 24
            while cr.text_extents(text).width > max_width and len(text) > 7:
                text = text[:-2] + "…"
            cr.move_to(x0 + 12, ty)
            cr.show_text(text)
            ty += size + 7
        pill_w = 58.0
        pill_x = x0 + width - pill_w - 10
        pill_y = y0 + 10
        self._rounded_rect(cr, pill_x, pill_y, pill_w, 18, 9)
        cr.set_source_rgba(*(BLOCKED if blocked else ALLOWED), 0.18)
        cr.fill()
        cr.set_source_rgb(*(BLOCKED if blocked else ALLOWED))
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(9.0)
        ext = cr.text_extents(state)
        cr.move_to(pill_x + (pill_w - ext.width) / 2, pill_y + 13)
        cr.show_text(state)
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
