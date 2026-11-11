#!/usr/bin/python3
"""GTK4/libadwaita front-end: an interactive world map for CS2 POPs."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

if (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) < (1, 4):
    sys.stderr.write(
        "cs2blocker requires libadwaita 1.4 or newer "
        f"(found {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION})\n"
    )
    raise SystemExit(1)

from .. import __version__
from ..core.config import default_config, load_config
from ..core.model import Mode, Region, SdrSnapshot, Selection
from ..core.ping import PingResult, ping_median, pop_ping_target
from ..core.presets import resolve_preset
from ..core.regions import REGION_ORDER
from ..core.ruleset import build_plan
from ..core.sdr import load_bundled, load_snapshot_file
from ..core.state import Paths, read_optional_json
from ..core.util import Cs2BlockerError, format_ranges, human_age, merge_ranges, which_fixed
from .locations import normalize_geo, parse_location
from .worldmap import MapEntry, WorldMap

INSTALLED_HELPER = Path("/usr/lib/cs2blocker/cs2blocker-helper")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSS = b"""
.map-shell { background-color: #08121b; border: 1px solid #22313d; border-radius: 12px; }
.map-control { min-width: 30px; min-height: 30px; padding: 0; border-radius: 6px; }
.map-legend { background-color: rgba(5, 13, 19, 0.78); border: 1px solid rgba(91, 122, 141, 0.28); border-radius: 6px; padding: 5px 9px; color: #b7c6d0; font-size: 11px; }
.detail-card { background-color: #0f1821; border: 1px solid #22313d; border-radius: 10px; padding: 11px 14px; }
.detail-title { font-size: 19px; font-weight: 600; }
.detail-subtitle { color: #9aacb8; font-size: 12px; }
.detail-meta { color: #8497a4; font-size: 11px; }
.ping-big { font-size: 20px; font-weight: 600; color: #d5e5ed; }
.ping-caption { color: #7f93a1; font-size: 10.5px; }
.ping-label { font-size: 11px; }
.drop-label { font-size: 11px; color: #7f93a1; }
.state-label { font-size: 10.5px; }

.ping-good { color: #43b98b; }
.ping-fair { color: #d2a24a; }
.ping-bad { color: #e05d5d; }
.state-blocked { color: #e05d5d; font-weight: 600; }
.state-allowed { color: #43b98b; font-weight: 600; }
.stat-dirty { color: #d2a24a; font-weight: 600; }
.region-chip { padding: 3px 9px; border-radius: 6px; background-color: rgba(255,255,255,0.035); border: 1px solid rgba(120,145,160,0.20); font-size: 11px; }
.region-chip-active { background-color: rgba(111,168,220,0.10); border-color: rgba(111,168,220,0.34); }
.footer { color: #8fa3b0; font-size: 11.5px; }
.section-title { font-size: 13px; font-weight: 600; }
.dim { color: #95a7b3; }
.warning-line { color: #d2a24a; font-size: 11.5px; padding: 0 2px; }
.sidebar-shell { background-color: #0c141c; border-left: 1px solid #1e2b35; }
.sidebar-heading { font-size: 17px; font-weight: 600; }
.column-header { color: #7d909d; font-size: 10.5px; font-weight: 600; padding: 4px 8px; border-bottom: 1px solid #1d2a34; }
.dc-country { padding: 2px 7px; border-radius: 5px; min-height: 34px; }
.dc-country:hover { background-color: rgba(255,255,255,0.035); }
.dc-child { padding: 2px 7px 2px 30px; border-radius: 5px; min-height: 30px; }
.dc-child:hover { background-color: rgba(255,255,255,0.030); }
.code-badge { font-family: monospace; font-size: 9.5px; color: #a8bfce; background-color: rgba(140,170,190,0.10); border-radius: 3px; padding: 1px 4px; }
.sidebar-bulk { padding: 1px 5px; font-size: 10.5px; min-height: 20px; }
.sort-toggle:checked { background-color: rgba(111,168,220,0.14); color: #dce8ef; }
.dc-country:focus-visible, .dc-child:focus-visible { background-color: rgba(111,168,220,0.10); outline: 1px solid rgba(111,168,220,0.55); outline-offset: -2px; }
.latency-row { padding: 2px 0; }
.pending-bar { background-color: #111b24; border: 1px solid #344755; border-radius: 8px; padding: 5px 7px; }
.pending-label { color: #d8e5ed; font-size: 12px; font-weight: 600; }
.status-pill { background-color: rgba(143,170,186,0.08); border: 1px solid #263744; border-radius: 6px; padding: 2px 9px; color: #9fb2bf; font-size: 11.5px; }
.status-popover-title { color: #d7e5ed; font-size: 12px; font-weight: 600; }
.status-popover-value { color: #93a7b4; font-size: 11.5px; }
.sidebar-search { font-size: 12px; }
.header-counts { font-size: 11.5px; }
"""



@dataclass
class CountrySidebar:
    country: str
    entries: tuple[MapEntry, ...]
    row: Gtk.Widget
    check: Gtk.CheckButton
    count_label: Gtk.Label | None = None
    expander: Adw.ExpanderRow | None = None
    server_checks: dict[str, Gtk.CheckButton] = field(default_factory=dict)
    server_pings: dict[str, Gtk.Label] = field(default_factory=dict)
    server_drops: dict[str, Gtk.Label] = field(default_factory=dict)
    server_states: dict[str, Gtk.Label] = field(default_factory=dict)


@dataclass
class ContinentSidebar:
    region: Region
    entries: tuple[MapEntry, ...]
    check: Gtk.CheckButton
    count_label: Gtk.Label


class Cs2BlockerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, *, smoke_test: bool = False) -> None:
        super().__init__(application=application)
        self.set_icon_name("cs2blocker")
        self.set_title("CS2 Server Blocker")
        self.set_default_size(1380, 860)
        self.set_size_request(900, 620)
        self.smoke_test = smoke_test
        self.paths = Paths.default()
        self.config = default_config()
        self.snapshot: SdrSnapshot
        self.origin = "bundled"
        self.mode = Mode.BLOCK
        self.selected: set[str] = set()
        self.applied_mode = Mode.BLOCK
        self.applied_selection: set[str] = set()
        self.entries: tuple[MapEntry, ...] = ()
        self.entry_by_code: dict[str, MapEntry] = {}
        self.focus_code: str | None = None
        self.pings: dict[str, float | None] = {}
        self.counters: dict[str, int] = {}
        self._loading = True
        self._busy = False
        self._live: dict[str, Any] | None = None
        self._state: dict[str, Any] = {}
        self._active = False
        self._last_counters: dict[str, int] = {}
        self._ping_generation = 0
        self._ping_outstanding = 0
        self._ping_flush_scheduled = False
        self._executor: ThreadPoolExecutor | None = None
        self._unit_cache: tuple[float, bool | None] | None = None
        self._search_source = 0
        self._close_dialog_open = False
        self._closing = False
        self._undo_state: tuple[Mode, set[str]] | None = None
        self._apply_undo_snapshot: tuple[Mode, set[str]] | None = None
        self._persistence_loading = False
        self._seen_refresh: str | None = None
        self._region_chips: dict[Region, tuple[Gtk.Button, Gtk.Label, Gtk.Label]] = {}
        self._preset_keys: list[str | None] = [None]
        self.search_query = ""
        self.sidebar_page: Adw.PreferencesPage | None = None
        self.country_widgets: dict[str, CountrySidebar] = {}
        self.continent_widgets: dict[Region, ContinentSidebar] = {}
        self.continent_rows: dict[Region, list[Gtk.Widget]] = {}
        self.continent_chevrons: dict[Region, Gtk.Button] = {}
        self.collapsed_regions: set[Region] = set(REGION_ORDER)
        self._updating_sidebar = False
        self._block_new_loading = False
        self.ping_cap: float | None = 80.0
        self.sort_by_ping = False
        self._sidebar_toggle_sync = False

        self._install_css()
        self._load_data()
        self._build_ui()
        self._refresh_ui_from_data()
        self._loading = False
        GLib.idle_add(self._start_ping)
        GLib.timeout_add_seconds(2, self._poll_status)
        GLib.timeout_add_seconds(60, self._periodic_ping)
        if smoke_test:
            GLib.timeout_add(1600, self._smoke_done)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _install_css() -> None:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                provider = Gtk.CssProvider()
                provider.load_from_data(CSS)
                Gtk.StyleContext.add_provider_for_display(
                    display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
        except Exception:
            pass
        try:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        except Exception:
            pass

    def _installed(self) -> bool:
        return INSTALLED_HELPER.is_file()

    # ------------------------------------------------------------------ data
    def _load_data(self) -> None:
        try:
            config = load_config(self.paths, require_root=False, missing_ok=True)
        except Cs2BlockerError:
            config = None
        self.config = config or default_config()
        self.mode = self.config.selection.mode
        self.selected = set(self.config.selection.pops)
        self.applied_mode = self.config.selection.mode
        self.applied_selection = set(self.config.selection.pops)
        cache = self.paths.cache_file(self.config.appid)
        if cache.is_file():
            try:
                self.snapshot = load_snapshot_file(cache, appid=self.config.appid, require_root=False)
                self.origin = "cache"
                self._build_entries()
                return
            except Cs2BlockerError:
                pass
        self.snapshot = load_bundled(self.config.appid)
        self.origin = "bundled"
        self._build_entries()

    def _build_entries(self) -> None:
        preliminary: list[tuple[Any, Any, str, tuple[float, float]]] = []
        primary_by_country: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for pop in self.snapshot.pops.values():
            if not pop.relays or not pop.geo:
                continue
            location = parse_location(pop)
            country = location.country or "Unknown"
            raw = (float(pop.geo[0]), float(pop.geo[1]))
            preliminary.append((pop, location, country, raw))
            # Most Valve POPs publish (longitude, latitude), but some partner
            # POPs publish the pair reversed. Build a median from the normal
            # POPs in the same country so we can pick the sane orientation.
            if not pop.name.startswith("Datapacket"):
                primary_by_country[country].append(raw)
        references = {
            country: (median(x for x, _ in coords), median(y for _, y in coords))
            for country, coords in primary_by_country.items()
            if coords
        }

        entries: list[MapEntry] = []
        for pop, location, country, raw in preliminary:
            lon, lat = normalize_geo(raw, references.get(country))
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                continue
            ports = format_ranges(merge_ranges((r.port_start, r.port_end) for r in pop.relays))
            entries.append(
                MapEntry(
                    code=pop.code,
                    title=location.title,
                    subtitle=location.subtitle,
                    region=pop.region,
                    lon=lon,
                    lat=lat,
                    relays=pop.relay_count,
                    ports=ports,
                    country=country,
                    city=location.city,
                )
            )
        entries.sort(key=lambda e: (REGION_ORDER.index(e.region) if e.region in REGION_ORDER else 99, e.code))
        self.entries = tuple(entries)
        self.entry_by_code = {entry.code: entry for entry in entries}
        if self.focus_code not in self.entry_by_code:
            selected_entry = next((e.code for e in entries if e.code in self.selected), None)
            self.focus_code = selected_entry

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title="CS2 Server Blocker",
                subtitle="Linux · nftables",
            )
        )
        self.sidebar_toggle = Gtk.ToggleButton()
        self.sidebar_toggle.set_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle.set_active(True)
        self.sidebar_toggle.set_tooltip_text("Show or hide the datacenter panel")
        self.sidebar_toggle.connect("toggled", self._sidebar_toggled)
        header.pack_start(self.sidebar_toggle)

        self.header_refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.header_refresh_button.set_tooltip_text("Refresh the Valve relay list")
        self.header_refresh_button.connect("clicked", lambda *_: self._refresh_clicked())
        header.pack_end(self.header_refresh_button)
        self.settings_button = Gtk.MenuButton()
        self.settings_button.set_icon_name("open-menu-symbolic")
        self.settings_button.set_tooltip_text("Settings")
        self.settings_button.set_popover(self._build_settings_popover())
        header.pack_end(self.settings_button)
        root.append(header)

        self.banner_revealer = Gtk.Revealer()
        self.banner_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner_box.set_margin_top(8)
        banner_box.set_margin_bottom(4)
        banner_box.set_margin_start(16)
        banner_box.set_margin_end(16)
        self.banner_label = Gtk.Label(xalign=0.0, hexpand=True, wrap=True)
        self.banner_button = Gtk.Button()
        self.banner_button.add_css_class("flat")
        self.banner_button.connect("clicked", lambda *_: self._banner_clicked())
        banner_box.append(self.banner_label)
        banner_box.append(self.banner_button)
        self.banner_revealer.set_child(banner_box)
        root.append(self.banner_revealer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(10)
        content.set_margin_bottom(8)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.set_vexpand(True)

        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        presets_label = Gtk.Label(label="Presets")
        presets_label.add_css_class("dim")
        presets_label.add_css_class("footer")
        topbar.append(presets_label)
        self.preset_dropdown = Gtk.DropDown.new_from_strings(["Apply preset…"])
        self.preset_dropdown.set_valign(Gtk.Align.CENTER)
        self.preset_dropdown.connect("notify::selected", self._preset_changed)
        topbar.append(self.preset_dropdown)
        self.save_preset_button = Gtk.Button(label="Save preset")
        self.save_preset_button.add_css_class("flat")
        self.save_preset_button.set_tooltip_text("Save the current allowed/blocked state as a preset")
        self.save_preset_button.connect("clicked", lambda *_: self._save_preset())
        topbar.append(self.save_preset_button)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        topbar.append(spacer)
        self.header_counts = Gtk.Label(label="0 blocked · 0 allowed")
        self.header_counts.add_css_class("header-counts")
        self.header_counts.set_tooltip_text("Counts of datacenters, not relays.")
        topbar.append(self.header_counts)
        self.unblock_button = Gtk.Button(label="Unblock all")
        self.unblock_button.add_css_class("flat")
        self.unblock_button.set_tooltip_text("Remove every block created by this application")
        self.unblock_button.connect("clicked", lambda *_: self._unblock_clicked())
        topbar.append(self.unblock_button)
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.add_css_class("flat")
        self.pause_button.set_tooltip_text("Remove the rules temporarily; keep the saved datacenter selection")
        self.pause_button.connect("clicked", lambda *_: self._pause_clicked())
        topbar.append(self.pause_button)
        content.append(topbar)

        map_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        map_shell.add_css_class("map-shell")
        map_shell.set_vexpand(True)
        map_shell.set_overflow(Gtk.Overflow.HIDDEN)
        self.map = WorldMap()
        try:
            self.map.update_property(
                [Gtk.AccessibleProperty.LABEL],
                ["CS2 datacenter world map. Use arrow keys to focus a datacenter, "
                 "Enter or Space to toggle it."],
            )
        except Exception:
            pass
        self.map.on_toggle = self._toggle_pop
        self.map.on_focus = self._focus_pop
        self.map.on_cluster = self._show_cluster_popover
        overlay = Gtk.Overlay()
        overlay.set_child(self.map)
        overlay.set_vexpand(True)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        controls.set_halign(Gtk.Align.END)
        controls.set_valign(Gtk.Align.START)
        controls.set_margin_top(10)
        controls.set_margin_end(10)
        for label, tooltip, callback in (
            ("+", "Zoom in", lambda *_: self.map.zoom_in()),
            ("−", "Zoom out", lambda *_: self.map.zoom_out()),
            ("⟲", "Reset view", lambda *_: self.map.reset_view()),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("map-control")
            button.set_tooltip_text(tooltip)
            button.connect("clicked", callback)
            controls.append(button)
        overlay.add_overlay(controls)

        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        legend.set_halign(Gtk.Align.START)
        legend.set_valign(Gtk.Align.START)
        legend.set_margin_start(10)
        legend.set_margin_top(10)
        legend_label = Gtk.Label(xalign=0.0)
        legend_label.set_markup(
            "<span foreground='#43b98b'>●</span> Allowed   "
            "<span foreground='#e05d5d'>⊘</span> Blocked   "
            "<span foreground='#d2a24a'>◐</span> Mixed   "
            "<span foreground='#d2a24a'>◌</span> Pending"
        )
        legend_label.add_css_class("map-legend")
        legend.append(legend_label)
        overlay.add_overlay(legend)

        self.pending_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pending_bar.add_css_class("pending-bar")
        self.pending_bar.set_halign(Gtk.Align.CENTER)
        self.pending_bar.set_valign(Gtk.Align.END)
        self.pending_bar.set_margin_bottom(10)
        self.pending_bar.set_visible(False)
        self.pending_label = Gtk.Label(label="")
        self.pending_label.add_css_class("pending-label")
        self.revert_button = Gtk.Button(label="Revert")
        self.revert_button.add_css_class("flat")
        self.revert_button.set_tooltip_text("Discard pending changes")
        self.revert_button.connect("clicked", lambda *_: self._revert_clicked())
        self.spinner = Gtk.Spinner()
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply_clicked())
        for widget in (
            self.pending_label,
            self.revert_button,
            self.spinner,
            self.apply_button,
        ):
            self.pending_bar.append(widget)
        overlay.add_overlay(self.pending_bar)
        map_shell.append(overlay)
        content.append(map_shell)

        self.warning_label = Gtk.Label(label="", xalign=0.0, wrap=True)
        self.warning_label.add_css_class("warning-line")
        self.warning_label.set_visible(False)
        content.append(self.warning_label)

        self.region_chip_box = Gtk.FlowBox()
        self.region_chip_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.region_chip_box.set_row_spacing(5)
        self.region_chip_box.set_column_spacing(5)
        self.region_chip_box.set_max_children_per_line(10)
        self.region_chip_box.set_halign(Gtk.Align.FILL)
        content.append(self.region_chip_box)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        detail.add_css_class("detail-card")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        detail_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        detail_left.set_hexpand(True)
        self.detail_title = Gtk.Label(label="Choose a datacenter", xalign=0.0)
        self.detail_title.add_css_class("detail-title")
        self.detail_title.set_ellipsize(3)
        self.detail_subtitle = Gtk.Label(
            label="Select a dot on the map or a row in the datacenter panel",
            xalign=0.0,
        )
        self.detail_subtitle.add_css_class("detail-subtitle")
        self.detail_subtitle.set_ellipsize(3)
        self.details_expander = Gtk.Expander(label="Details")
        self.details_expander.set_visible(False)
        self.detail_meta = Gtk.Label(label="", xalign=0.0, wrap=True)
        self.detail_meta.add_css_class("detail-meta")
        self.details_expander.set_child(self.detail_meta)
        detail_left.append(self.detail_title)
        detail_left.append(self.detail_subtitle)
        detail_left.append(self.details_expander)
        top.append(detail_left)

        ping_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        ping_box.set_valign(Gtk.Align.CENTER)
        self.detail_ping = Gtk.Label(label="—")
        self.detail_ping.add_css_class("ping-big")
        ping_caption = Gtk.Label(label="ICMP ping")
        ping_caption.add_css_class("ping-caption")
        ping_box.append(self.detail_ping)
        ping_box.append(ping_caption)
        top.append(ping_box)

        self.drops_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.drops_box.set_valign(Gtk.Align.CENTER)
        self.detail_drops = Gtk.Label(label="0")
        self.detail_drops.add_css_class("ping-big")
        drops_caption = Gtk.Label(label="drops")
        drops_caption.add_css_class("ping-caption")
        self.drops_box.append(self.detail_drops)
        self.drops_box.append(drops_caption)
        top.append(self.drops_box)

        action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        action_box.set_valign(Gtk.Align.CENTER)
        self.detail_state = Gtk.Label(label="")
        self.detail_toggle = Gtk.Button(label="Block datacenter")
        self.detail_toggle.connect("clicked", lambda *_: self._toggle_focus())
        self.detail_toggle.set_sensitive(False)
        action_box.append(self.detail_state)
        action_box.append(self.detail_toggle)
        top.append(action_box)
        detail.append(top)
        content.append(detail)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_pill = Gtk.MenuButton()
        self.status_pill.add_css_class("status-pill")
        self.status_pill.set_tooltip_text("Backend, relay list and persistence details")
        self.status_label = Gtk.Label(label="Starting…")
        self.status_pill.set_child(self.status_label)
        self.status_pill.set_popover(self._build_status_popover())
        status_row.append(self.status_pill)
        content.append(status_row)

        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_content(content)
        self.split_view.set_sidebar(self._build_sidebar())
        self.split_view.set_sidebar_position(Gtk.PackType.END)
        self.split_view.set_show_sidebar(True)
        self.split_view.set_min_sidebar_width(330)
        self.split_view.set_max_sidebar_width(430)
        self.split_view.set_sidebar_width_fraction(0.30)
        self.split_view.connect("notify::show-sidebar", self._sidebar_visibility_changed)
        root.append(self.split_view)

        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1120px")
        )
        breakpoint.add_setter(self.split_view, "collapsed", True)
        breakpoint.add_setter(self.split_view, "show-sidebar", False)
        self.add_breakpoint(breakpoint)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

    def _build_sidebar(self) -> Gtk.Widget:
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        shell.add_css_class("sidebar-shell")
        shell.set_margin_top(10)
        shell.set_margin_bottom(8)
        shell.set_margin_start(10)
        shell.set_margin_end(10)
        shell.set_size_request(340, -1)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Datacenters", xalign=0.0)
        title.add_css_class("sidebar-heading")
        title.set_hexpand(True)
        self.sidebar_count_label = Gtk.Label(label="")
        self.sidebar_count_label.add_css_class("footer")
        title_row.append(title)
        title_row.append(self.sidebar_count_label)
        shell.append(title_row)

        self.sidebar_search = Gtk.SearchEntry()
        self.sidebar_search.add_css_class("sidebar-search")
        self.sidebar_search.set_placeholder_text("Search country, city, or code")
        self.sidebar_search.connect("search-changed", self._search_changed)
        shell.append(self.sidebar_search)
        self.search_count_label = Gtk.Label(label="", xalign=0.0)
        self.search_count_label.add_css_class("footer")
        self.search_count_label.set_visible(False)
        shell.append(self.search_count_label)

        self.sidebar_quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        allow_all = Gtk.Button(label="Allow all")
        allow_all.add_css_class("flat")
        allow_all.set_tooltip_text("Allow every known datacenter")
        allow_all.connect("clicked", lambda *_: self._allow_all())
        block_all = Gtk.Button(label="Block all")
        block_all.add_css_class("flat")
        block_all.set_tooltip_text("Block every known datacenter")
        block_all.connect("clicked", lambda *_: self._block_all())
        self.sort_button = Gtk.ToggleButton(label="Sort: A–Z")
        self.sort_button.add_css_class("flat")
        self.sort_button.add_css_class("sort-toggle")
        self.sort_button.set_tooltip_text("Sort datacenters by ping instead of name")
        self.sort_button.connect("toggled", self._sort_toggled)
        self.sidebar_quick_box.append(allow_all)
        self.sidebar_quick_box.append(block_all)
        self.sidebar_quick_box.append(self.sort_button)
        shell.append(self.sidebar_quick_box)

        cap_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cap_row.add_css_class("latency-row")
        cap_label = Gtk.Label(label="Allow under")
        cap_label.add_css_class("footer")
        self.ping_cap_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 30, 150, 5
        )
        self.ping_cap_scale.set_draw_value(False)
        self.ping_cap_scale.set_hexpand(True)
        self.ping_cap_scale.set_value(self.ping_cap or 80)
        self.ping_cap_scale.set_tooltip_text(
            "Preview datacenters at or below this ping; Apply makes it pending"
        )
        self.ping_cap_scale.connect("value-changed", self._ping_cap_changed)
        self.ping_cap_label = Gtk.Label(label=f"{int(self.ping_cap or 80)} ms", width_chars=6)
        self.ping_cap_label.add_css_class("footer")
        self.ping_cap_apply = Gtk.Button(label="Apply cap")
        self.ping_cap_apply.add_css_class("flat")
        self.ping_cap_apply.set_tooltip_text("Allow datacenters at or below the chosen ping")
        self.ping_cap_apply.connect("clicked", lambda *_: self._apply_ping_cap())
        cap_row.append(cap_label)
        cap_row.append(self.ping_cap_scale)
        cap_row.append(self.ping_cap_label)
        cap_row.append(self.ping_cap_apply)
        shell.append(cap_row)

        column_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        column_header.add_css_class("column-header")
        column_header.set_margin_end(38)
        dc_header = Gtk.Label(label="Datacenter", xalign=0.0, hexpand=True)
        state_header = Gtk.Label(label="State", xalign=1.0, width_chars=8)
        ping_header = Gtk.Label(label="Ping", xalign=1.0, width_chars=7)
        drops_header = Gtk.Label(label="Drops", xalign=1.0, width_chars=5)
        column_header.append(dc_header)
        column_header.append(state_header)
        column_header.append(ping_header)
        column_header.append(drops_header)
        shell.append(column_header)

        self.sidebar_scroll = Gtk.ScrolledWindow()
        self.sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar_scroll.set_vexpand(True)
        placeholder = Gtk.Label(label="Loading datacenters…")
        placeholder.add_css_class("dim")
        self.sidebar_scroll.set_child(placeholder)
        shell.append(self.sidebar_scroll)
        return shell

    def _sidebar_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._sidebar_toggle_sync:
            return
        self.split_view.set_show_sidebar(button.get_active())

    def _sidebar_visibility_changed(self, split, _pspec) -> None:
        self._sidebar_toggle_sync = True
        self.sidebar_toggle.set_active(split.get_show_sidebar())
        self._sidebar_toggle_sync = False

    def _build_status_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        title = Gtk.Label(label="Runtime status", xalign=0.0)
        title.add_css_class("status-popover-title")
        box.append(title)
        self.status_backend = Gtk.Label(xalign=0.0)
        self.status_backend.add_css_class("status-popover-value")
        self.status_revision = Gtk.Label(xalign=0.0)
        self.status_revision.add_css_class("status-popover-value")
        self.status_cache = Gtk.Label(xalign=0.0)
        self.status_cache.add_css_class("status-popover-value")
        for label in (self.status_backend, self.status_revision, self.status_cache):
            box.append(label)
        note = Gtk.Label(
            label="Only inet cs2blocker is modified.",
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("status-popover-value")
        box.append(note)
        popover.set_child(box)
        return popover

    def _build_settings_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_size_request(280, -1)
        title = Gtk.Label(label="Settings", xalign=0.0)
        title.add_css_class("section-title")
        box.append(title)

        policy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        policy_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        policy_text.set_hexpand(True)
        policy_label = Gtk.Label(
            label="Block newly added datacenters automatically",
            xalign=0.0,
            wrap=True,
        )
        policy_label.set_max_width_chars(28)
        self.block_new_label = Gtk.Label(label="Checking…", xalign=0.0, wrap=True)
        self.block_new_label.add_css_class("dim")
        self.block_new_label.add_css_class("footer")
        policy_text.append(policy_label)
        policy_text.append(self.block_new_label)
        self.block_new_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.block_new_switch.set_tooltip_text(
            "On: newly added Valve datacenters start blocked. "
            "Off: they start allowed."
        )
        self.block_new_switch.connect("notify::active", self._block_new_toggled)
        policy_row.append(policy_text)
        policy_row.append(self.block_new_switch)
        box.append(policy_row)
        box.append(Gtk.Separator())

        persistence_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        persistence_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        persistence_text.set_hexpand(True)
        label = Gtk.Label(label="Boot persistence", xalign=0.0)
        self.persistence_label = Gtk.Label(label="Checking…", xalign=0.0)
        self.persistence_label.add_css_class("dim")
        self.persistence_label.add_css_class("footer")
        persistence_text.append(label)
        persistence_text.append(self.persistence_label)
        self.persistence_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.persistence_switch.connect("notify::active", self._persistence_toggled)
        persistence_row.append(persistence_text)
        persistence_row.append(self.persistence_switch)
        box.append(persistence_row)

        separator = Gtk.Separator()
        box.append(separator)
        self.menu_refresh_button = Gtk.Button(label="Refresh relay list")
        self.menu_refresh_button.set_halign(Gtk.Align.START)
        self.menu_refresh_button.connect("clicked", lambda *_: self._refresh_clicked())
        box.append(self.menu_refresh_button)
        self.menu_ping_button = Gtk.Button(label="Ping all datacenters")
        self.menu_ping_button.set_halign(Gtk.Align.START)
        self.menu_ping_button.connect("clicked", lambda *_: self._start_ping())
        box.append(self.menu_ping_button)
        self.menu_undo_button = Gtk.Button(label="Undo last apply")
        self.menu_undo_button.set_halign(Gtk.Align.START)
        self.menu_undo_button.set_tooltip_text("Restore and apply the rules from before the last apply")
        self.menu_undo_button.connect("clicked", lambda *_: self._undo_apply())
        box.append(self.menu_undo_button)
        note = Gtk.Label(
            label="Only inet cs2blocker is modified. ICMP is never blocked.",
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim")
        note.add_css_class("footer")
        box.append(note)
        popover.set_child(box)
        return popover

    # --------------------------------------------------------------- rendering
    def _dirty(self) -> bool:
        return self.mode is not self.applied_mode or self.selected != self.applied_selection

    def _is_blocked(self, code: str) -> bool:
        return code in self.selected if self.mode is Mode.BLOCK else code not in self.selected

    def _is_paused(self) -> bool:
        return bool(self._state.get("paused", False))

    def _applied_blocked(self, code: str) -> bool:
        return (
            code in self.applied_selection
            if self.applied_mode is Mode.BLOCK
            else code not in self.applied_selection
        )

    def _pending_codes(self) -> set[str]:
        return {
            entry.code
            for entry in self.entries
            if self._is_blocked(entry.code) != self._applied_blocked(entry.code)
        }

    def _set_codes_allowed(self, codes, allowed: bool) -> None:
        """Set explicit allowed/blocked state while preserving the internal mode model."""
        codes = set(codes)
        if self.mode is Mode.BLOCK:
            if allowed:
                self.selected.difference_update(codes)
            else:
                self.selected.update(codes)
        else:
            if allowed:
                self.selected.update(codes)
            else:
                self.selected.difference_update(codes)

    def _refresh_ui_from_data(self) -> None:
        self._loading = True
        self._build_preset_model()
        self._build_region_chips()
        self._update_block_new_view()
        self._loading = False
        self.map.set_ping_cap(self.ping_cap)
        self._expand_region_for(self.focus_code)
        self.map.set_data(
            self.entries,
            selected=self.selected,
            mode=self.mode,
            pings=self.pings,
            counters=self.counters,
            focus=self.focus_code,
            pending=self._pending_codes(),
        )
        self._rebuild_sidebar()
        self._update_stats()
        self._update_detail()
        self._update_selection_warnings()
        self._update_buttons()
        self._update_status_view()

    def _update_block_new_view(self) -> None:
        if not hasattr(self, "block_new_switch"):
            return
        self._block_new_loading = True
        enabled = self.mode is Mode.ONLY
        self.block_new_switch.set_active(enabled)
        self.block_new_switch.set_sensitive(not self._busy)
        self.block_new_label.set_text(
            "On — new datacenters are blocked"
            if enabled
            else "Off — new datacenters are allowed"
        )
        self._block_new_loading = False

    def _build_preset_model(self) -> None:
        names = ["Apply preset…"]
        self._preset_keys = [None]
        for key, preset in sorted(self.config.presets.items()):
            names.append(preset.name)
            self._preset_keys.append(key)
        self._loading = True
        self.preset_dropdown.set_model(Gtk.StringList.new(names))
        self.preset_dropdown.set_selected(0)
        self._loading = False

    def _build_region_chips(self) -> None:
        while (child := self.region_chip_box.get_first_child()) is not None:
            self.region_chip_box.remove(child)
        self._region_chips.clear()
        for region in REGION_ORDER:
            total = sum(1 for entry in self.entries if entry.region is region)
            if not total:
                continue
            button = Gtk.Button()
            button.add_css_class("region-chip")
            button.set_valign(Gtk.Align.CENTER)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            dot = Gtk.Label()
            dot.set_markup("<span foreground='#8fa3b0'>●</span>")
            text = Gtk.Label(label=f"{region.value} 0 of {total} allowed")
            row.append(dot)
            row.append(text)
            button.set_child(row)
            button.connect("clicked", lambda _b, r=region: self._toggle_region(r))
            self.region_chip_box.append(button)
            self._region_chips[region] = (button, text, dot)
        self._update_region_chips()

    def _update_region_chips(self) -> None:
        for region, (_button, label, _dot) in self._region_chips.items():
            codes = [entry.code for entry in self.entries if entry.region is region]
            allowed = sum(
                1
                for entry in self.entries
                if entry.region is region and not self._is_blocked(entry.code)
            )
            label.set_text(f"{region.value} {allowed} of {len(codes)} allowed")
            button = _button
            if allowed < len(codes):
                button.add_css_class("region-chip-active")
            else:
                button.remove_css_class("region-chip-active")
            all_allowed = allowed == len(codes)
            button.set_tooltip_text(
                f"Block all {region.value} datacenters"
                if all_allowed
                else f"Allow all {region.value} datacenters"
            )

    @staticmethod
    def _ping_class(ping: float | None) -> str:
        if ping is None:
            return ""
        if ping <= 50:
            return "ping-good"
        if ping <= 100:
            return "ping-fair"
        return "ping-bad"

    @staticmethod
    def _code_badge(code: str) -> Gtk.Label:
        label = Gtk.Label(label=code)
        label.add_css_class("code-badge")
        return label

    def _sort_toggled(self, button: Gtk.ToggleButton) -> None:
        self.sort_by_ping = button.get_active()
        button.set_label("Sort: ping" if self.sort_by_ping else "Sort: A–Z")
        self._rebuild_sidebar()

    def _ping_cap_changed(self, scale: Gtk.Scale) -> None:
        self.ping_cap = scale.get_value()
        self.ping_cap_label.set_text(f"{int(self.ping_cap)} ms")
        if hasattr(self, "map"):
            self.map.set_ping_cap(self.ping_cap)

    def _apply_ping_cap(self) -> None:
        if self._busy:
            return
        cap = self.ping_cap
        if cap is None:
            return
        known = {
            entry.code: self.pings[entry.code]
            for entry in self.entries
            if self.pings.get(entry.code) is not None
        }
        if not known:
            self._toast("No ping data yet; wait for the probe to finish")
            return
        # POPs whose ICMP probe failed are not "slow": keep their current
        # state instead of silently blocking every one of them.
        preserve_unknown = {
            entry.code
            for entry in self.entries
            if entry.code not in known and not self._is_blocked(entry.code)
        }
        eligible = {code for code, rtt in known.items() if rtt <= cap}
        self._set_codes_allowed([entry.code for entry in self.entries], False)
        self._set_codes_allowed(eligible | preserve_unknown, True)
        self.focus_code = next(
            (entry.code for entry in self.entries if entry.code in eligible),
            next(iter(preserve_unknown), None),
        )
        self._pending_changed()
        self._toast(
            f"Allow under {int(cap)} ms added as pending"
            + (f" ({len(preserve_unknown)} unknown ping kept as-is)" if preserve_unknown else "")
        )

    def _open_except_menu(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        region: Region,
        countries: list[tuple[str, list[MapEntry]]],
    ) -> None:
        row = gesture.get_widget()
        popover = self._build_except_popover(region, countries)
        popover.set_parent(row)
        try:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
        except Exception:
            pass
        popover.popup()

    def _build_except_popover(
        self, region: Region, countries: list[tuple[str, list[MapEntry]]]
    ) -> Gtk.Popover:
        popover = Gtk.Popover()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        title = Gtk.Label(label=f"Block every {region.value} datacenter except…", xalign=0.0)
        title.add_css_class("section-title")
        outer.append(title)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(150)
        scrolled.set_max_content_height(300)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for country, _entries in sorted(countries, key=lambda item: item[0]):
            button = Gtk.Button(label=country)
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            button.connect(
                "clicked",
                lambda _button, c=country: (
                    self._block_all_except(region, c),
                    popover.popdown(),
                ),
            )
            inner.append(button)
        scrolled.set_child(inner)
        outer.append(scrolled)
        popover.set_child(outer)
        return popover

    def _block_all_except(self, region: Region, country: str) -> None:
        region_codes = {entry.code for entry in self.entries if entry.region is region}
        except_codes = {
            entry.code
            for entry in self.entries
            if entry.region is region and entry.country == country
        }
        self._set_codes_allowed(region_codes, False)
        self._set_codes_allowed(except_codes, True)
        self.focus_code = next(
            (entry.code for entry in self.entries if entry.code in region_codes - except_codes),
            None,
        )
        self.map.zoom_to_region(region)
        self._pending_changed()

    def _set_region_allowed(self, region: Region, allowed: bool) -> None:
        codes = [entry.code for entry in self.entries if entry.region is region]
        self._set_codes_allowed(codes, allowed)
        self._pending_changed()

    def _rebuild_sidebar(self) -> None:
        if not hasattr(self, "sidebar_scroll"):
            return
        query = self.search_query.casefold().strip()
        page = Adw.PreferencesPage()
        self.country_widgets.clear()
        self.continent_widgets.clear()
        self.continent_rows.clear()
        self.continent_chevrons.clear()
        any_group = False

        for region in REGION_ORDER:
            region_entries = [entry for entry in self.entries if entry.region is region]
            if not region_entries:
                continue
            by_country: dict[str, list[MapEntry]] = defaultdict(list)
            for entry in region_entries:
                by_country[entry.country or "Unknown"].append(entry)
            filtered: list[tuple[str, list[MapEntry]]] = []
            for country in sorted(by_country):
                entries = by_country[country]
                if not query or query in country.casefold() or query in region.value.casefold():
                    filtered.append((country, entries))
                    continue
                matches = [
                    entry
                    for entry in entries
                    if query in entry.title.casefold()
                    or query in entry.subtitle.casefold()
                    or query in entry.code.casefold()
                ]
                if matches:
                    filtered.append((country, matches))
            if not filtered:
                continue

            def ping_key(item: tuple[str, list[MapEntry]]) -> tuple[float, str]:
                pings = [
                    self.pings.get(entry.code)
                    for entry in item[1]
                    if self.pings.get(entry.code) is not None
                ]
                best = min(pings) if pings else float("inf")
                return (best, item[0].casefold())

            if self.sort_by_ping:
                filtered.sort(key=ping_key)
            else:
                filtered.sort(key=lambda item: item[0].casefold())

            any_group = True
            group = Adw.PreferencesGroup()
            continent_row = Adw.ActionRow(title=region.value)
            continent_row.add_css_class("dc-continent")
            continent_check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            continent_check.set_tooltip_text(
                f"Check to allow every {region.value} datacenter; uncheck to block them"
            )
            continent_check.connect("toggled", self._continent_toggled, region)
            collapse_button = Gtk.Button.new_from_icon_name("pan-down-symbolic")
            collapse_button.add_css_class("flat")
            collapse_button.set_valign(Gtk.Align.CENTER)
            collapse_button.set_tooltip_text(f"Collapse {region.value}")
            collapse_button.connect(
                "clicked", self._continent_collapse_clicked, region
            )
            continent_count = Gtk.Label(label="")
            continent_count.add_css_class("footer")
            allow_button = Gtk.Button(label="Allow all")
            allow_button.add_css_class("sidebar-bulk")
            allow_button.add_css_class("flat")
            allow_button.set_tooltip_text(f"Allow every {region.value} datacenter")
            allow_button.connect("clicked", lambda _b, r=region: self._set_region_allowed(r, True))
            block_button = Gtk.Button(label="Block all")
            block_button.add_css_class("sidebar-bulk")
            block_button.add_css_class("flat")
            block_button.set_tooltip_text(f"Block every {region.value} datacenter")
            block_button.connect("clicked", lambda _b, r=region: self._set_region_allowed(r, False))
            continent_row.add_prefix(continent_check)
            continent_row.add_prefix(collapse_button)
            continent_row.add_suffix(continent_count)
            continent_row.add_suffix(allow_button)
            continent_row.add_suffix(block_button)
            continent_row.set_tooltip_text(
                "Right-click for Block all except…"
            )
            except_click = Gtk.GestureClick()
            except_click.set_button(3)
            except_click.connect(
                "pressed", self._open_except_menu, region, filtered
            )
            continent_row.add_controller(except_click)
            group.add(continent_row)
            self.continent_widgets[region] = ContinentSidebar(
                region=region,
                entries=tuple(region_entries),
                check=continent_check,
                count_label=continent_count,
            )
            self.continent_chevrons[region] = collapse_button
            self.continent_rows[region] = []

            for country, country_entries in filtered:
                if self.sort_by_ping:
                    country_entries.sort(
                        key=lambda entry: (
                            self.pings.get(entry.code) is None,
                            self.pings.get(entry.code) or 9999,
                            entry.city,
                        )
                    )
                else:
                    country_entries.sort(key=lambda entry: entry.city.casefold())

                if len(country_entries) == 1:
                    entry = country_entries[0]
                    row = Adw.ActionRow(
                        title=entry.city or entry.title,
                        subtitle=entry.country,
                    )
                    row.add_css_class("dc-country")
                    row.set_activatable(True)
                    row.set_focusable(True)
                    row.connect("activated", lambda _r, code=entry.code: self._toggle_pop(code))
                    check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                    check.set_tooltip_text("Check to allow this datacenter; uncheck to block it")
                    check.connect("toggled", self._code_toggled, entry.code)
                    state_label = Gtk.Label(label="", xalign=1.0, width_chars=8)
                    state_label.add_css_class("state-label")
                    ping_label = Gtk.Label(label="—", width_chars=7, xalign=1.0)
                    ping_label.add_css_class("ping-label")
                    drop_label = Gtk.Label(label="", width_chars=5, xalign=1.0)
                    drop_label.add_css_class("drop-label")
                    row.add_prefix(self._code_badge(entry.code))
                    row.add_prefix(check)
                    row.add_suffix(state_label)
                    row.add_suffix(ping_label)
                    row.add_suffix(drop_label)
                    group.add(row)
                    self.continent_rows[region].append(row)
                    widget = CountrySidebar(
                        country=country,
                        entries=(entry,),
                        row=row,
                        check=check,
                        count_label=None,
                        expander=None,
                    )
                    widget.server_checks[entry.code] = check
                    widget.server_pings[entry.code] = ping_label
                    widget.server_drops[entry.code] = drop_label
                    widget.server_states[entry.code] = state_label
                    self.country_widgets[country] = widget
                    continue

                expander = Adw.ExpanderRow(title=country)
                expander.add_css_class("dc-country")
                expander.set_focusable(True)
                expander.set_subtitle("")
                country_check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                country_check.set_tooltip_text(
                    f"Check to allow every datacenter in {country}; uncheck to block them"
                )
                country_check.connect("toggled", self._country_toggled, country)
                country_count = Gtk.Label(label="")
                country_count.add_css_class("footer")
                expander.add_prefix(country_check)
                expander.add_suffix(country_count)
                if query:
                    expander.set_expanded(True)
                widget = CountrySidebar(
                    country=country,
                    entries=tuple(country_entries),
                    row=expander,
                    check=country_check,
                    count_label=country_count,
                    expander=expander,
                )
                for entry in country_entries:
                    row = Adw.ActionRow(
                        title=entry.city or entry.title,
                        subtitle="",
                    )
                    row.add_css_class("dc-child")
                    row.set_activatable(True)
                    row.connect("activated", lambda _row, code=entry.code: self._toggle_pop(code))
                    row.set_focusable(True)
                    check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                    check.set_tooltip_text("Check to allow this datacenter; uncheck to block it")
                    check.connect("toggled", self._code_toggled, entry.code)
                    state_label = Gtk.Label(label="", xalign=1.0, width_chars=8)
                    state_label.add_css_class("state-label")
                    ping_label = Gtk.Label(label="—", width_chars=7, xalign=1.0)
                    ping_label.add_css_class("ping-label")
                    drop_label = Gtk.Label(label="", width_chars=5, xalign=1.0)
                    drop_label.add_css_class("drop-label")
                    row.add_prefix(self._code_badge(entry.code))
                    row.add_prefix(check)
                    row.add_suffix(state_label)
                    row.add_suffix(ping_label)
                    row.add_suffix(drop_label)
                    expander.add_row(row)
                    widget.server_checks[entry.code] = check
                    widget.server_pings[entry.code] = ping_label
                    widget.server_drops[entry.code] = drop_label
                    widget.server_states[entry.code] = state_label
                group.add(expander)
                self.continent_rows[region].append(expander)
                self.country_widgets[country] = widget
            self._apply_continent_visibility(region, force=bool(query))
            page.add(group)

        if not any_group:
            group = Adw.PreferencesGroup()
            row = Adw.ActionRow(
                title="No matching datacenters",
                subtitle="Try a country, city, or POP code.",
            )
            group.add(row)
            page.add(group)
        self.sidebar_page = page
        self.sidebar_scroll.set_child(page)
        if query:
            matched = sum(
                len(entries)
                for group in (self.country_widgets.values())
                for entries in [group.entries]
            )
            self.search_count_label.set_text(f"{matched} matching datacenters")
            self.search_count_label.set_visible(True)
        else:
            self.search_count_label.set_text("")
            self.search_count_label.set_visible(False)
        self._update_sidebar_selection()

    def _update_sidebar_selection(self) -> None:
        if self._updating_sidebar or self.sidebar_page is None:
            return
        self._updating_sidebar = True
        try:
            for widget in self.country_widgets.values():
                total = len(widget.entries)
                allowed = sum(
                    1 for entry in widget.entries if not self._is_blocked(entry.code)
                )
                widget.check.set_active(allowed == total and total > 0)
                widget.check.set_inconsistent(0 < allowed < total)
                color = (
                    "#36d399"
                    if allowed == total and total > 0
                    else ("#ff6b6b" if allowed == 0 else "#f6c177")
                )
                if widget.count_label is not None:
                    widget.count_label.set_markup(
                        f"<span foreground='{color}'>{allowed} of {total} allowed</span>"
                    )
                    widget.count_label.set_tooltip_text(
                        f"{allowed} of {total} datacenters allowed"
                    )
                if widget.expander is not None:
                    widget.expander.set_subtitle("")
                for code, check in widget.server_checks.items():
                    blocked = self._is_blocked(code)
                    check.set_active(not blocked)
                    state = widget.server_states.get(code)
                    if state is not None:
                        state.set_text("Blocked" if blocked else "Allowed")
                        state.remove_css_class("state-blocked")
                        state.remove_css_class("state-allowed")
                        state.add_css_class(
                            "state-blocked" if blocked else "state-allowed"
                        )
                    ping = self.pings.get(code)
                    ping_label = widget.server_pings[code]
                    ping_label.set_text("—" if ping is None else f"{ping:.0f} ms")
                    for cls in ("ping-good", "ping-fair", "ping-bad"):
                        ping_label.remove_css_class(cls)
                    ping_class = self._ping_class(ping)
                    if ping_class:
                        ping_label.add_css_class(ping_class)
                    drops = self.counters.get(code, 0)
                    widget.server_drops[code].set_text(str(drops) if drops > 0 else "")
            for widget in self.continent_widgets.values():
                total = len(widget.entries)
                allowed = sum(
                    1 for entry in widget.entries if not self._is_blocked(entry.code)
                )
                widget.check.set_active(allowed == total and total > 0)
                widget.check.set_inconsistent(0 < allowed < total)
                color = (
                    "#36d399"
                    if allowed == total and total > 0
                    else ("#ff6b6b" if allowed == 0 else "#f6c177")
                )
                widget.count_label.set_markup(
                    f"<span foreground='{color}'>{allowed} of {total} allowed</span>"
                )
                widget.count_label.set_tooltip_text(
                    f"{allowed} of {total} datacenters allowed"
                )
            all_codes = [entry.code for entry in self.entries]
            allowed = sum(1 for entry in self.entries if not self._is_blocked(entry.code))
            self.sidebar_count_label.set_text(
                f"{allowed} of {len(all_codes)} allowed"
            )
        finally:
            self._updating_sidebar = False

    def _continent_collapse_clicked(
        self, _button: Gtk.Button, region: Region
    ) -> None:
        if region in self.collapsed_regions:
            self.collapsed_regions.remove(region)
        else:
            self.collapsed_regions.add(region)
        self._apply_continent_visibility(region)

    def _apply_continent_visibility(self, region: Region, *, force: bool = False) -> None:
        collapsed = region in self.collapsed_regions and not force
        for row in self.continent_rows.get(region, []):
            row.set_visible(not collapsed)
        button = self.continent_chevrons.get(region)
        if button is not None:
            button.set_icon_name(
                "pan-end-symbolic" if collapsed else "pan-down-symbolic"
            )
            button.set_tooltip_text(
                f"Expand {region.value}" if collapsed else f"Collapse {region.value}"
            )

    def _expand_region_for(self, code: str | None) -> None:
        if not code:
            return
        entry = self.entry_by_code.get(code)
        if entry is None or entry.region not in self.collapsed_regions:
            return
        self.collapsed_regions.discard(entry.region)
        self._apply_continent_visibility(entry.region)

    def _search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.search_query = entry.get_text().strip()
        if self._search_source:
            GLib.source_remove(self._search_source)
        self._search_source = GLib.timeout_add(150, self._apply_search)

    def _apply_search(self) -> bool:
        self._search_source = 0
        self._rebuild_sidebar()
        return False

    def _continent_toggled(self, check: Gtk.CheckButton, region: Region) -> None:
        if self._updating_sidebar:
            return
        codes = [entry.code for entry in self.entries if entry.region is region]
        self._set_codes_allowed(codes, check.get_active())
        self._pending_changed()

    def _country_toggled(self, check: Gtk.CheckButton, country: str) -> None:
        if self._updating_sidebar:
            return
        widget = self.country_widgets.get(country)
        if widget is None:
            return
        codes = [entry.code for entry in widget.entries]
        self._set_codes_allowed(codes, check.get_active())
        self._pending_changed()

    def _code_toggled(self, check: Gtk.CheckButton, code: str) -> None:
        if self._updating_sidebar:
            return
        self._set_codes_allowed([code], check.get_active())
        self.focus_code = code
        self._pending_changed()

    def _allow_all(self) -> None:
        self._set_codes_allowed(
            [entry.code for entry in self.entries], True
        )
        self._pending_changed()

    def _block_all(self) -> None:
        self._set_codes_allowed(
            [entry.code for entry in self.entries], False
        )
        self._pending_changed()

    def _allowed_count(self) -> int:
        return sum(1 for entry in self.entries if not self._is_blocked(entry.code))

    def _update_stats(self) -> None:
        codes = [entry.code for entry in self.entries]
        blocked = [code for code in codes if self._is_blocked(code)]
        allowed = [code for code in codes if not self._is_blocked(code)]
        self.header_counts.set_markup(
            f"<span foreground='#ff6b6b'><b>{len(blocked)}</b> blocked</span>"
            f" · <span foreground='#36d399'><b>{len(allowed)}</b> allowed</span>"
        )
        self._update_region_chips()
        self._update_pending_bar()

    def _update_detail(self) -> None:
        entry = self.entry_by_code.get(self.focus_code or "")
        if entry is None:
            self.detail_title.set_text("Choose a datacenter")
            self.detail_subtitle.set_text("Select a dot or a row in the datacenter panel")
            self.detail_meta.set_text("")
            self.details_expander.set_visible(False)
            self.detail_ping.set_text("—")
            for cls in ("ping-good", "ping-fair", "ping-bad"):
                self.detail_ping.remove_css_class(cls)
            self.detail_drops.set_text("—")
            self.drops_box.set_visible(False)
            self.detail_state.set_text("")
            self.detail_toggle.set_label("Block datacenter")
            self.detail_toggle.set_sensitive(False)
            return
        blocked = self._is_blocked(entry.code)
        self.detail_title.set_text(entry.title)
        self.detail_subtitle.set_text(f"{entry.country}  ·  {entry.code}")
        self.detail_meta.set_text(
            f"{entry.region.value}  ·  {entry.relays} relays  ·  UDP {entry.ports}"
        )
        self.details_expander.set_visible(True)
        ping = self.pings.get(entry.code)
        self.detail_ping.set_text("—" if ping is None else f"{ping:.0f} ms")
        for cls in ("ping-good", "ping-fair", "ping-bad"):
            self.detail_ping.remove_css_class(cls)
        ping_class = self._ping_class(ping)
        if ping_class:
            self.detail_ping.add_css_class(ping_class)
        self.detail_drops.set_text(f"{self.counters.get(entry.code, 0):,}")
        self.drops_box.set_visible(blocked)
        self.detail_state.set_text("BLOCKED" if blocked else "ALLOWED")
        self.detail_state.remove_css_class("state-blocked")
        self.detail_state.remove_css_class("state-allowed")
        self.detail_state.add_css_class("state-blocked" if blocked else "state-allowed")
        self.detail_toggle.set_label("Allow datacenter" if blocked else "Block datacenter")
        self.detail_toggle.set_sensitive(self._installed() and not self._busy)
        self.map.set_focus(entry.code)

    @staticmethod
    def _friendly_warning(warning: str) -> str:
        text = warning
        replacements = (
            ("selection contains unknown POP(s):", "Unknown datacenter code(s):"),
            ("selected POP ", "Datacenter "),
            ("no POPs selected; the firewall table will be cleared",
             "No datacenters are blocked; the firewall table will be cleared"),
            ("selection allows every known POP; nothing will be blocked",
             "All datacenters are allowed; nothing will be blocked"),
            ("selection blocks every known POP; matchmaking is likely to fail",
             "All datacenters are blocked; matchmaking is likely to fail"),
            ("selection leaves only ", "Only "),
            (" POP(s) allowed; ", " datacenter(s) allowed; "),
            ("all POPs with relay endpoints are blocked; matchmaking may fail",
             "All datacenters with relay endpoints are blocked; matchmaking may fail"),
            ("shared with a selected POP", "shared with an allowed datacenter"),
            ("keep the selected POP working", "keep the allowed datacenter working"),
            ("those POPs too", "those datacenters too"),
            ("known POP", "known datacenter"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    def _update_selection_warnings(self) -> None:
        try:
            plan = build_plan(
                self.snapshot,
                Selection(mode=self.mode, pops=frozenset(self.selected)),
            )
            text = "; ".join(self._friendly_warning(warning) for warning in plan.warnings)
        except Exception as exc:
            text = str(exc)
        allowed = self._allowed_count()
        if allowed < 3:
            low = (
                f"Only {allowed} of {len(self.entries)} datacenters allowed. "
                "Matchmaking may fail."
            )
            text = f"{low} {text}".strip()
        if text:
            self.warning_label.set_text(text)
            self.warning_label.set_visible(True)
        else:
            self.warning_label.set_text("")
            self.warning_label.set_visible(False)

    def _update_buttons(self) -> None:
        installed = self._installed()
        for button in (
            self.header_refresh_button,
            self.preset_dropdown,
            self.save_preset_button,
            self.unblock_button,
            self.menu_refresh_button,
            self.menu_ping_button,
            self.menu_undo_button,
        ):
            button.set_sensitive(installed and not self._busy)
        self.menu_undo_button.set_sensitive(
            installed and not self._busy and self._undo_state is not None
        )
        paused = self._is_paused()
        self.pause_button.set_label("Resume" if paused else "Pause")
        self.pause_button.set_visible(installed and (paused or self._active))
        self.pause_button.set_sensitive(installed and not self._busy)
        self.detail_toggle.set_sensitive(
            installed and not self._busy and self.focus_code in self.entry_by_code
        )
        for button, _label, _dot in self._region_chips.values():
            button.set_sensitive(not self._busy)
        self.map.set_sensitive(not self._busy)
        self.sidebar_search.set_sensitive(not self._busy)
        self.sidebar_quick_box.set_sensitive(not self._busy)
        self.ping_cap_scale.set_sensitive(not self._busy)
        self.ping_cap_apply.set_sensitive(not self._busy)
        if self.sidebar_page is not None:
            self.sidebar_page.set_sensitive(not self._busy)
        self.persistence_switch.set_sensitive(installed and not self._busy)
        self.block_new_switch.set_sensitive(not self._busy)
        self._update_pending_bar()

    def _update_pending_bar(self) -> None:
        if not hasattr(self, "pending_bar"):
            return
        dirty = self._dirty()
        pending = len(self._pending_codes())
        self.pending_bar.set_visible(dirty or self._busy)
        self.spinner.set_visible(self._busy)
        if self._busy:
            self.pending_label.set_text("Applying…")
        elif pending:
            self.pending_label.set_text(f"{pending} pending")
        elif dirty:
            self.pending_label.set_text("New-POP policy pending")
        else:
            self.pending_label.set_text("")
        installed = self._installed()
        self.apply_button.set_sensitive(installed and dirty and not self._busy)
        self.apply_button.set_label("Apply")
        self.revert_button.set_sensitive(installed and dirty and not self._busy)

    # --------------------------------------------------------------- actions
    def _toggle_pop(self, code: str) -> None:
        blocked = self._is_blocked(code)
        self._set_codes_allowed([code], blocked)
        self.focus_code = code
        self._pending_changed()

    def _focus_pop(self, code: str | None) -> None:
        self.focus_code = code
        self._expand_region_for(code)
        self.map.set_focus(code)
        self._update_detail()
        self._update_buttons()

    def _show_cluster_popover(self, entries: tuple[MapEntry, ...], x: float, y: float) -> None:
        popover = Gtk.Popover()
        popover.set_parent(self.map)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        title = Gtk.Label(label="Datacenters in this cluster", xalign=0.0)
        title.add_css_class("section-title")
        box.append(title)
        hint = Gtk.Label(label="Click one to toggle it and show its ping.", xalign=0.0)
        hint.add_css_class("dim")
        hint.add_css_class("footer")
        box.append(hint)
        for entry in sorted(entries, key=lambda item: item.title):
            button = Gtk.Button()
            button.add_css_class("flat")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            text.set_hexpand(True)
            city = Gtk.Label(label=entry.title, xalign=0.0)
            city.add_css_class("section-title")
            ping = self.pings.get(entry.code)
            ping_text = "ping …" if ping is None else f"{ping:.0f} ms"
            sub = Gtk.Label(
                label=f"{entry.code} · {ping_text} · {entry.subtitle}",
                xalign=0.0,
            )
            sub.add_css_class("dim")
            sub.add_css_class("footer")
            text.append(city)
            text.append(sub)
            state = Gtk.Label(label="BLOCKED" if self._is_blocked(entry.code) else "ALLOWED", xalign=1.0)
            state.add_css_class("state-blocked" if self._is_blocked(entry.code) else "state-allowed")
            row.append(text)
            row.append(state)
            button.set_child(row)
            button.connect(
                "clicked",
                lambda _button, code=entry.code: (
                    self._toggle_pop(code),
                    self._focus_pop(code),
                    popover.popdown(),
                ),
            )
            box.append(button)
        popover.set_child(box)
        try:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
        except Exception:
            pass
        popover.popup()

    def _toggle_focus(self) -> None:
        if self.focus_code:
            self._toggle_pop(self.focus_code)

    def _toggle_region(self, region: Region) -> None:
        codes = [entry.code for entry in self.entries if entry.region is region]
        if not codes:
            return
        all_allowed = all(not self._is_blocked(code) for code in codes)
        self._set_codes_allowed(codes, not all_allowed)
        if not all_allowed:
            self.focus_code = codes[0]
            self._expand_region_for(self.focus_code)
        self._pending_changed()

    def _pending_changed(self) -> None:
        self.map.set_selection(self.selected, self.mode)
        self.map.set_pending(self._pending_codes())
        self.map.set_focus(self.focus_code)
        self._update_stats()
        self._update_sidebar_selection()
        self._update_detail()
        self._update_selection_warnings()
        self._update_buttons()

    def _block_new_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._loading or self._block_new_loading or self._busy:
            return
        desired = Mode.ONLY if switch.get_active() else Mode.BLOCK
        if desired is self.mode:
            return
        # Only visible datacenters participate in the explicit state; POPs
        # without relay endpoints cannot be shown or toggled in the GUI.
        known = {entry.code for entry in self.entries}
        blocked = {code for code in known if self._is_blocked(code)}
        allowed = known - blocked
        self.mode = desired
        self.selected = set(allowed) if desired is Mode.ONLY else set(blocked)
        self._update_block_new_view()
        self._pending_changed()
        self._toast(
            "New datacenters will be blocked after Apply"
            if desired is Mode.ONLY
            else "New datacenters will be allowed after Apply"
        )

    def _preset_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._loading:
            return
        index = dropdown.get_selected()
        if index <= 0:
            return
        key = self._preset_keys[index] if index < len(self._preset_keys) else None
        preset = self.config.presets.get(key) if key else None
        if preset is None:
            return
        codes, warnings = resolve_preset(preset, self.snapshot)
        self.mode = preset.mode
        self.selected = set(codes)
        self._update_block_new_view()
        self.focus_code = next((code for code in sorted(codes) if code in self.entry_by_code), None)
        self._expand_region_for(self.focus_code)
        if preset.regions:
            self.map.zoom_to_region(preset.regions[0])
        elif codes:
            self.map.zoom_to_codes(codes)
        self._loading = True
        self.preset_dropdown.set_selected(0)
        self._loading = False
        self._pending_changed()
        self._toast(f"Loaded preset “{preset.name}” — press Apply")
        for warning in warnings:
            self._toast(warning)

    def _apply_clicked(self) -> None:
        if self._busy or not self._dirty():
            return
        pops = sorted(self.selected)
        allowed = self._allowed_count()
        total = len(self.entries)
        if allowed < 3:
            self._confirm(
                "Few datacenters allowed",
                f"Only {allowed} of {total} datacenters will remain allowed. "
                "Matchmaking may fail or take much longer. Apply anyway?",
                lambda: self._do_apply(pops),
            )
            return
        self._do_apply(pops)

    def _do_apply(self, pops: list[str]) -> None:
        self._apply_undo_snapshot = (self.applied_mode, set(self.applied_selection))
        self._run_privileged(
            ["apply", "--mode", self.mode.value, "--pops", ",".join(pops)],
            self._after_apply,
        )

    def _after_apply(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            if self._apply_undo_snapshot is not None:
                self._undo_state = self._apply_undo_snapshot
                self._apply_undo_snapshot = None
            self.applied_mode = self.mode
            self.applied_selection = set(self.selected)
            self.map.set_pending(self._pending_codes())
            self._update_stats()
            self._update_status_view()
            warnings = [
                line.removeprefix("warning: ").strip()
                for line in stderr.splitlines()
                if line.startswith("warning: ")
            ]
            self._toast("Rules applied")
            for warning in warnings[:3]:
                self._toast(warning)
        else:
            self._apply_undo_snapshot = None
            self._show_error(
                "Apply failed",
                (stderr or stdout).strip() + f"\n\nCommand:\n{command}",
            )

    def _undo_apply(self) -> None:
        if self._busy or self._undo_state is None:
            return
        mode, pops = self._undo_state
        self.mode = mode
        self.selected = set(pops)
        self.focus_code = next((code for code in self.selected if code in self.entry_by_code), None)
        self._update_block_new_view()
        self.map.set_selection(self.selected, self.mode)
        self.map.set_pending(self._pending_codes())
        self._update_stats()
        self._update_sidebar_selection()
        self._update_detail()
        self._update_buttons()
        self._do_apply(sorted(self.selected))

    def _revert_clicked(self) -> None:
        self.mode = self.applied_mode
        self.selected = set(self.applied_selection)
        self._update_block_new_view()
        self.map.set_selection(self.selected, self.mode)
        self.map.set_pending(self._pending_codes())
        self._update_stats()
        self._update_sidebar_selection()
        self._update_detail()
        self._update_selection_warnings()
        self._update_buttons()
        self._toast("Reverted to the applied state")

    def _unblock_clicked(self) -> None:
        self._confirm(
            "Unblock everything?",
            "This removes the cs2blocker table and clears the saved datacenter state. "
            "Other firewall tables are untouched.",
            lambda: self._run_privileged(["clear"], self._after_clear),
        )

    def _after_clear(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self.mode = Mode.BLOCK
            self.selected = set()
            self.applied_mode = Mode.BLOCK
            self.applied_selection = set()
            self._undo_state = None
            self.focus_code = None
            self._update_block_new_view()
            self.map.set_data(
                self.entries,
                selected=self.selected,
                mode=self.mode,
                pings=self.pings,
                counters=self.counters,
                focus=None,
                pending=set(),
            )
            self._pending_changed()
            self._update_status_view()
            self._toast("All CS2 blocks removed")
        else:
            self._show_error("Unblock failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _pause_clicked(self) -> None:
        if self._busy:
            return
        if self._is_paused():
            self._run_privileged(["resume"], self._after_resume)
            return
        if self._dirty():
            self._toast("Apply or discard your pending changes before pausing")
            return
        self._confirm(
            "Pause blocking?",
            "This removes every cs2blocker rule but keeps your saved datacenter "
            "selection. Other firewall tables are untouched; resume with one click.",
            lambda: self._run_privileged(["pause"], self._after_pause),
        )

    def _after_pause(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._undo_state = None
            self._update_status_view()
            self._toast("Blocking paused — selection kept")
        else:
            self._show_error("Pause failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _after_resume(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._update_status_view()
            self._toast("Blocking resumed")
        else:
            self._show_error("Resume failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _refresh_clicked(self) -> None:
        if self._busy:
            return
        if self._dirty():
            self._toast("Apply or discard your pending changes before refreshing")
            return
        self._run_privileged(["refresh"], self._after_refresh)

    def _after_refresh(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            focus = self.focus_code
            self._load_data()
            self.focus_code = focus if focus in self.entry_by_code else None
            self._refresh_ui_from_data()
            GLib.idle_add(self._start_ping)
            self._toast("Relay list refreshed")
        else:
            self._show_error("Refresh failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _save_preset(self) -> None:
        self._ask_text(
            "Save preset",
            "Preset name",
            lambda name: self._run_privileged(
                [
                    "preset-save",
                    "--name",
                    name,
                    "--mode",
                    self.mode.value,
                    "--pops",
                    ",".join(sorted(self.selected)),
                ],
                self._after_save_preset,
            ),
        )

    def _after_save_preset(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            pending_mode, pending_selection = self.mode, set(self.selected)
            try:
                config = load_config(self.paths, require_root=False, missing_ok=True)
            except Cs2BlockerError:
                config = None
            if config is not None:
                self.config = config
                self.applied_mode = config.selection.mode
                self.applied_selection = set(config.selection.pops)
            self.mode, self.selected = pending_mode, pending_selection
            self._build_preset_model()
            self._update_stats()
            self._update_buttons()
            self._toast("Preset saved")
        else:
            self._show_error("Preset save failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _persistence_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._persistence_loading or self._loading:
            return
        action = "enable" if switch.get_active() else "disable"
        self._run_privileged(["persistence", action], self._after_persistence)

    def _after_persistence(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._toast(f"Boot persistence {stdout.strip() or 'updated'}")
        else:
            self._show_error(
                "Persistence change failed",
                (stderr or stdout).strip() + f"\n\nCommand:\n{command}",
            )
        self._invalid_unit_cache()
        self._update_persistence_view()

    # ------------------------------------------------------------------- ping
    def _start_ping(self) -> bool:
        if self.smoke_test:
            return False
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=16)
        self._ping_generation += 1
        generation = self._ping_generation
        self._ping_outstanding = sum(
            1
            for entry in self.entries
            if entry.code in self.snapshot.pops
            and pop_ping_target(self.snapshot.pops[entry.code].relays) is not None
        )
        for entry in self.entries:
            pop = self.snapshot.pops.get(entry.code)
            target = pop_ping_target(pop.relays) if pop else None
            if target is None:
                self.pings[entry.code] = None
                continue
            future = self._executor.submit(ping_median, target, samples=3, timeout=1.0)
            future.add_done_callback(
                lambda fut, code=entry.code, gen=generation: GLib.idle_add(
                    self._ping_done, gen, code, fut
                )
            )
        return False

    def _periodic_ping(self) -> bool:
        if self.smoke_test:
            return False
        if self._ping_outstanding == 0 and not self._busy:
            self._start_ping()
        return True

    def _ping_done(self, generation: int, code: str, future) -> bool:
        if generation != self._ping_generation:
            return False
        try:
            result: PingResult = future.result()
            self.pings[code] = result.rtt_ms
        except Exception:
            self.pings[code] = None
        self.map.set_pings(self.pings)
        self._ping_outstanding = max(0, self._ping_outstanding - 1)
        if code == self.focus_code:
            self._update_detail()
        if self._ping_outstanding == 0:
            self._flush_pings()
        elif not self._ping_flush_scheduled:
            self._ping_flush_scheduled = True
            GLib.timeout_add(120, self._flush_pings)
        return False

    def _flush_pings(self) -> bool:
        self._ping_flush_scheduled = False
        self._update_sidebar_selection()
        if self.sort_by_ping and self._ping_outstanding == 0:
            self._rebuild_sidebar()
        return False

    # ---------------------------------------------------------- status/privil
    def _read_status(self) -> dict[str, Any] | None:
        try:
            document = read_optional_json(self.paths.status_file, require_root=False)
        except Cs2BlockerError:
            return None
        return document if isinstance(document, dict) else None

    def _read_state(self) -> dict[str, Any]:
        try:
            document = read_optional_json(self.paths.state_file, require_root=False)
        except Cs2BlockerError:
            return {}
        return document if isinstance(document, dict) else {}

    def _invalid_unit_cache(self) -> None:
        self._unit_cache = None

    def _unit_enabled(self, *, force: bool = False) -> bool | None:
        if not force and self._unit_cache is not None:
            cached_at, cached = self._unit_cache
            if time.monotonic() - cached_at < 60.0:
                return cached
        if not which_fixed("systemctl"):
            self._unit_cache = (time.monotonic(), None)
            return None
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", "cs2blocker.service"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError:
            self._unit_cache = (time.monotonic(), None)
            return None
        if result.returncode == 0:
            enabled: bool | None = True
        elif result.returncode == 1:
            enabled = False
        else:
            enabled = None
        self._unit_cache = (time.monotonic(), enabled)
        return enabled

    def _update_status_view(self) -> None:
        self._live = self._read_status()
        self._state = self._read_state()
        live = self._live or {}
        state = self._state
        paused = self._is_paused()
        active = bool(live.get("active", state.get("active", False))) and not paused
        backend = live.get("backend") or state.get("backend") or "?"
        counters = {
            code: int(value.get("packets", 0))
            for code, value in (live.get("counters") or {}).items()
            if isinstance(value, dict)
        }
        if paused:
            counters = {}
        counters_changed = counters != self.counters
        self.counters = counters
        self._active = active
        self.map.set_opacity(0.55 if paused else 1.0)
        if counters_changed:
            self.map.set_counters(counters)
            self._update_sidebar_selection()
        if self.focus_code and counters_changed:
            self._update_detail()
        cache = state.get("cache") or {}
        enabled = self._unit_enabled()
        blocked_count = len(live.get("blocked") or state.get("blocked") or [])
        if not self._installed():
            status = "Not installed"
        elif paused:
            status = "Paused · rules removed"
        elif active:
            status = f"Active · {blocked_count} blocked"
        else:
            status = "Inactive"
        status += " · starts at boot" if enabled else " · boot off"
        if cache.get("age_human"):
            status += f" · relay list updated {cache['age_human']}"
        self.status_label.set_text(status)
        self.status_backend.set_text(f"Backend: {backend}")
        self.status_revision.set_text(
            f"Relay revision: {cache.get('revision', 'unknown')}"
        )
        self.status_cache.set_text(
            f"Relay list: {cache.get('origin', 'unknown')} · {cache.get('age_human', 'unknown')}"
        )
        self._update_persistence_view()
        self._update_banner()
        self._update_buttons()
        summary = state.get("relay_list_summary")
        last_refresh = state.get("last_refresh_at")
        if state.get("relay_list_changed") and last_refresh and last_refresh != self._seen_refresh:
            self._seen_refresh = last_refresh
            if not self.smoke_test:
                self._toast(f"Relay list changed: {summary or 'new Valve data'}")

    def _update_persistence_view(self) -> None:
        enabled = self._unit_enabled()
        self._persistence_loading = True
        if enabled is None:
            self.persistence_switch.set_active(False)
            self.persistence_switch.set_sensitive(False)
            self.persistence_label.set_text("systemd unavailable")
        else:
            self.persistence_switch.set_active(enabled)
            self.persistence_switch.set_sensitive(self._installed() and not self._busy)
            self.persistence_label.set_text("Enabled at boot" if enabled else "Disabled")
        self._persistence_loading = False

    def _update_banner(self) -> None:
        state = self._state
        if state.get("error"):
            self._banner_show(f"Last apply failed: {state['error']}", "Retry apply")
            return
        if not self._installed():
            self._banner_show("Not installed system-wide. Run: sudo ./packaging/install.sh", "")
            return
        if self._is_paused():
            self._banner_show(
                "Blocking is paused. Your saved datacenter selection is kept; "
                "press Resume to restore the same rules.",
                "Resume",
            )
            return
        cache = state.get("cache") or {}
        if cache.get("stale") or self.origin == "bundled":
            self._banner_show(
                "Relay list is bundled/stale. Refresh to fetch the current Valve data.",
                "Refresh list",
            )
            return
        if state.get("refresh_error"):
            self._banner_show(f"Last relay refresh failed: {state['refresh_error']}", "Refresh list")
            return
        self._banner_hide()

    def _banner_show(self, title: str, button_label: str) -> None:
        self.banner_label.set_text(title)
        self.banner_button.set_label(button_label)
        self.banner_button.set_visible(bool(button_label))
        self.banner_revealer.set_reveal_child(True)

    def _banner_hide(self) -> None:
        self.banner_revealer.set_reveal_child(False)

    def _banner_clicked(self) -> None:
        if self._state.get("error"):
            self._apply_clicked()
        elif self._is_paused():
            self._pause_clicked()
        else:
            self._refresh_clicked()

    def _poll_status(self) -> bool:
        if self.smoke_test:
            return True
        self._update_status_view()
        self._update_stats()
        return True

    def _run_privileged(
        self, args: list[str], callback: Callable[[tuple[int, str, str, str]], None]
    ) -> None:
        self._busy = True
        self.spinner.start()
        self._update_buttons()

        def worker() -> None:
            result = self._privileged_call(args)
            GLib.idle_add(self._privileged_done, callback, result)

        threading.Thread(target=worker, daemon=True).start()

    def _privileged_call(self, args: list[str]) -> tuple[int, str, str, str]:
        if not self._installed():
            return (
                127,
                "",
                "cs2blocker is not installed system-wide. Run:\n  sudo ./packaging/install.sh",
                "",
            )
        if os.geteuid() == 0:
            command = [str(INSTALLED_HELPER), *args]
        else:
            pkexec = which_fixed("pkexec")
            if not pkexec:
                return (127, "", "pkexec is not installed; use sudo cs2blockctl", "")
            command = [pkexec, str(INSTALLED_HELPER), *args]
        display = shlex.join(command)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (124, "", "helper timed out after 180 seconds", display)
        except OSError as exc:
            return (127, "", str(exc), display)
        stderr = result.stderr or ""
        if result.returncode in (126, 127) and "pkexec" in command[0]:
            lowered = stderr.lower()
            if "agent" in lowered or "not authorized" in lowered or "dismissed" in lowered:
                stderr += (
                    "\nNo polkit agent or authentication was cancelled. Run:\n  "
                    + shlex.join(["sudo", str(INSTALLED_HELPER), *args])
                )
        return (result.returncode, result.stdout or "", stderr, display)

    def _privileged_done(
        self,
        callback: Callable[[tuple[int, str, str, str]], None],
        result: tuple[int, str, str, str],
    ) -> bool:
        self._busy = False
        self.spinner.stop()
        self._update_buttons()
        self._update_persistence_view()
        callback(result)
        return False

    # ------------------------------------------------------------------ dialogs
    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def _show_error(self, heading: str, body: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=heading,
        )
        dialog.set_property("secondary-text", body[:8000])
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.present()

    def _confirm(
        self,
        heading: str,
        body: str,
        on_ok: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=heading,
        )
        dialog.set_property("secondary-text", body)

        def response(d, response_id):
            d.destroy()
            if response_id == Gtk.ResponseType.OK:
                on_ok()
            elif on_cancel is not None:
                on_cancel()

        dialog.connect("response", response)
        dialog.present()

    def _ask_text(self, heading: str, label: str, on_ok: Callable[[str], None]) -> None:
        dialog = Gtk.Dialog(transient_for=self, modal=True, title=heading)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text=label, activates_default=True)
        entry.set_margin_top(12)
        entry.set_margin_bottom(12)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        dialog.get_content_area().append(entry)

        def response(d, response_id):
            text = entry.get_text().strip()
            d.destroy()
            if response_id == Gtk.ResponseType.OK and text:
                on_ok(text)

        dialog.connect("response", response)
        dialog.present()

    def _on_close(self, *_args) -> bool:
        if self.smoke_test or self._closing:
            self._shutdown()
            return False
        if self._dirty() and not self._close_dialog_open:
            self._close_dialog_open = True
            pending = len(self._pending_codes())
            self._confirm(
                "Discard unsaved changes?",
                f"You have {pending or 'unapplied'} changes. Closing without applying "
                "will leave the current firewall state unchanged.",
                self._close_after_confirm,
                self._cancel_close,
            )
            return True
        if self._close_dialog_open:
            return True
        self._shutdown()
        return False

    def _close_after_confirm(self) -> None:
        self._close_dialog_open = False
        self._closing = True
        self.close()

    def _cancel_close(self) -> None:
        self._close_dialog_open = False

    def _shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _smoke_done(self) -> bool:
        print("GUI smoke test: window constructed and shown")
        self.get_application().quit()
        return False


class Cs2BlockerApp(Adw.Application):
    def __init__(self, *, smoke_test: bool = False) -> None:
        app_id = (
            f"org.cs2blocker.Gui.Smoke{os.getpid()}"
            if smoke_test
            else "org.cs2blocker.Gui"
        )
        flags = (
            Gio.ApplicationFlags.NON_UNIQUE
            if smoke_test
            else Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        super().__init__(application_id=app_id, flags=flags)
        Gtk.Window.set_default_icon_name("cs2blocker")
        self.smoke_test = smoke_test
        self.window: Cs2BlockerWindow | None = None
        self.connect("activate", self._activate)

    def _activate(self, _app) -> None:
        if self.window is None:
            self.window = Cs2BlockerWindow(self, smoke_test=self.smoke_test)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    smoke = "--smoke-test" in args
    args = [a for a in args if a != "--smoke-test"]
    app = Cs2BlockerApp(smoke_test=smoke)
    return app.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
