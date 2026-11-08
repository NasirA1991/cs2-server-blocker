#!/usr/bin/python3
"""GTK4/libadwaita front-end: an interactive world map for CS2 POPs."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __version__
from ..core.config import default_config, load_config
from ..core.model import Mode, Region, SdrSnapshot, Selection
from ..core.ping import PingResult, ping, pop_ping_target
from ..core.presets import resolve_preset
from ..core.regions import REGION_ORDER
from ..core.ruleset import build_plan
from ..core.sdr import load_bundled, load_snapshot_file
from ..core.state import Paths, read_optional_json
from ..core.util import Cs2BlockerError, format_ranges, human_age, merge_ranges, which_fixed
from .locations import parse_location
from .worldmap import MapEntry, WorldMap

INSTALLED_HELPER = Path("/usr/lib/cs2blocker/cs2blocker-helper")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGION_COLORS = {
    Region.EUROPE: "#5db7ff",
    Region.NORTH_AMERICA: "#f4a261",
    Region.SOUTH_AMERICA: "#f28ab2",
    Region.ASIA: "#7ddc98",
    Region.OCEANIA: "#f2ce5c",
    Region.AFRICA: "#d59bf6",
    Region.MIDDLE_EAST: "#5de0d0",
    Region.UNKNOWN: "#aab8c2",
}
CSS = b"""
.map-shell { background-color: #07131f; border-radius: 18px; }
.map-control { min-width: 32px; min-height: 32px; padding: 0; }
.detail-card { background-color: rgba(255,255,255,0.045); border-radius: 16px; padding: 14px; }
.detail-title { font-size: 23px; font-weight: 800; }
.detail-subtitle { color: #8fa7b6; }
.detail-meta { color: #9db0bd; font-size: 11px; }
.ping-big { font-size: 26px; font-weight: 800; color: #8bd3ff; }
.ping-caption { color: #7f97a8; font-size: 10px; }
.state-blocked { color: #ff6b6b; font-weight: 800; }
.state-allowed { color: #36d399; font-weight: 800; }
.stat-blocked { color: #ff6b6b; font-weight: 700; }
.stat-allowed { color: #36d399; font-weight: 700; }
.stat-relay { color: #8bd3ff; font-weight: 600; }
.stat-dirty { color: #f6c177; font-weight: 700; }
.region-chip { padding: 2px 10px; border-radius: 999px; }
.region-chip-active { background-color: rgba(255,255,255,0.10); }
.footer { color: #7f97a8; font-size: 11px; }
.section-title { font-size: 15px; font-weight: 800; }
.dim { color: #8fa7b6; }
"""
MODE_LABELS = ["Block selected", "Allow only selected"]


class Cs2BlockerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, *, smoke_test: bool = False) -> None:
        super().__init__(application=application)
        self.set_title("CS2 Server Blocker")
        self.set_default_size(1220, 840)
        self.set_size_request(900, 650)
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
        self._last_counters: dict[str, int] = {}
        self._ping_generation = 0
        self._executor: ThreadPoolExecutor | None = None
        self._persistence_loading = False
        self._seen_refresh: str | None = None
        self._region_chips: dict[Region, tuple[Gtk.Button, Gtk.Label, Gtk.Label]] = {}
        self._preset_keys: list[str | None] = [None]

        self._install_css()
        self._load_data()
        self._build_ui()
        self._refresh_ui_from_data()
        self._loading = False
        GLib.idle_add(self._start_ping)
        GLib.timeout_add_seconds(2, self._poll_status)
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
        entries: list[MapEntry] = []
        for pop in self.snapshot.pops.values():
            if not pop.relays or not pop.geo:
                continue
            location = parse_location(pop)
            ports = format_ranges(merge_ranges((r.port_start, r.port_end) for r in pop.relays))
            entries.append(
                MapEntry(
                    code=pop.code,
                    title=location.title,
                    subtitle=location.subtitle,
                    region=pop.region,
                    lon=float(pop.geo[0]),
                    lat=float(pop.geo[1]),
                    relays=pop.relay_count,
                    ports=ports,
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
                subtitle="Click a datacenter dot to block or allow it",
            )
        )
        self.header_refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.header_refresh_button.set_tooltip_text("Refresh the Valve relay list")
        self.header_refresh_button.connect("clicked", lambda *_: self._refresh_clicked())
        header.pack_end(self.header_refresh_button)
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_tooltip_text("Settings")
        menu_button.set_popover(self._build_settings_popover())
        header.pack_end(menu_button)
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

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_vexpand(True)

        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_box.add_css_class("linked")
        self.block_button = Gtk.ToggleButton(label="Block selected")
        self.only_button = Gtk.ToggleButton(label="Allow only selected")
        self.only_button.set_group(self.block_button)
        self.block_button.set_active(True)
        self.block_button.connect("clicked", self._mode_clicked)
        self.only_button.connect("clicked", self._mode_clicked)
        mode_box.append(self.block_button)
        mode_box.append(self.only_button)
        topbar.append(mode_box)

        self.preset_dropdown = Gtk.DropDown.new_from_strings(["Apply preset…"])
        self.preset_dropdown.set_valign(Gtk.Align.CENTER)
        self.preset_dropdown.connect("notify::selected", self._preset_changed)
        topbar.append(self.preset_dropdown)
        self.save_preset_button = Gtk.Button(label="Save preset")
        self.save_preset_button.add_css_class("flat")
        self.save_preset_button.set_tooltip_text("Save the current selection as a preset")
        self.save_preset_button.connect("clicked", lambda *_: self._save_preset())
        topbar.append(self.save_preset_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        topbar.append(spacer)
        self.blocked_stat = Gtk.Label(label="0 blocked")
        self.blocked_stat.add_css_class("stat-blocked")
        self.allowed_stat = Gtk.Label(label="0 allowed")
        self.allowed_stat.add_css_class("stat-allowed")
        self.relay_stat = Gtk.Label(label="0 relays")
        self.relay_stat.add_css_class("stat-relay")
        self.dirty_stat = Gtk.Label(label="Unsaved changes")
        self.dirty_stat.add_css_class("stat-dirty")
        self.dirty_stat.set_visible(False)
        for widget in (self.blocked_stat, self.allowed_stat, self.relay_stat, self.dirty_stat):
            topbar.append(widget)
        content.append(topbar)

        map_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        map_shell.add_css_class("map-shell")
        map_shell.set_vexpand(True)
        map_shell.set_overflow(Gtk.Overflow.HIDDEN)
        self.map = WorldMap()
        self.map.on_toggle = self._toggle_pop
        self.map.on_focus = self._focus_pop
        overlay = Gtk.Overlay()
        overlay.set_child(self.map)
        overlay.set_vexpand(True)
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        controls.set_halign(Gtk.Align.END)
        controls.set_valign(Gtk.Align.START)
        controls.set_margin_top(12)
        controls.set_margin_end(12)
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

        legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        legend.set_halign(Gtk.Align.START)
        legend.set_valign(Gtk.Align.END)
        legend.set_margin_start(14)
        legend.set_margin_bottom(12)
        legend_label = Gtk.Label(xalign=0.0)
        legend_label.set_markup(
            "<span foreground='#ff6b6b'>●</span> Blocked    "
            "<span foreground='#36d399'>●</span> Allowed"
        )
        hint_label = Gtk.Label(
            label="Click a dot to toggle · click a bubble to zoom · drag to pan",
            xalign=0.0,
        )
        hint_label.add_css_class("dim")
        hint_label.add_css_class("footer")
        legend.append(legend_label)
        legend.append(hint_label)
        overlay.add_overlay(legend)
        map_shell.append(overlay)
        content.append(map_shell)

        self.region_scroll = Gtk.ScrolledWindow()
        self.region_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.region_scroll.set_min_content_height(40)
        self.region_scroll.set_max_content_height(48)
        self.region_scroll.set_vexpand(False)
        self.region_chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.region_scroll.set_child(self.region_chip_box)
        content.append(self.region_scroll)

        detail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        detail.add_css_class("detail-card")
        detail_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        detail_left.set_hexpand(True)
        self.detail_title = Gtk.Label(label="Select a datacenter", xalign=0.0)
        self.detail_title.add_css_class("detail-title")
        self.detail_title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.detail_subtitle = Gtk.Label(label="Click a dot on the map or a region chip", xalign=0.0)
        self.detail_subtitle.add_css_class("detail-subtitle")
        self.detail_subtitle.set_ellipsize(3)
        self.detail_meta = Gtk.Label(label="", xalign=0.0)
        self.detail_meta.add_css_class("detail-meta")
        self.detail_meta.set_wrap(True)
        detail_left.append(self.detail_title)
        detail_left.append(self.detail_subtitle)
        detail_left.append(self.detail_meta)
        detail.append(detail_left)

        ping_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        ping_box.set_valign(Gtk.Align.CENTER)
        self.detail_ping = Gtk.Label(label="—")
        self.detail_ping.add_css_class("ping-big")
        ping_caption = Gtk.Label(label="ICMP ping")
        ping_caption.add_css_class("ping-caption")
        ping_box.append(self.detail_ping)
        ping_box.append(ping_caption)
        detail.append(ping_box)

        drops_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        drops_box.set_valign(Gtk.Align.CENTER)
        self.detail_drops = Gtk.Label(label="0")
        self.detail_drops.add_css_class("ping-big")
        drops_caption = Gtk.Label(label="drops since apply")
        drops_caption.add_css_class("ping-caption")
        drops_box.append(self.detail_drops)
        drops_box.append(drops_caption)
        detail.append(drops_box)

        action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        action_box.set_valign(Gtk.Align.CENTER)
        self.detail_state = Gtk.Label(label="—")
        self.detail_toggle = Gtk.Button(label="Block this POP")
        self.detail_toggle.connect("clicked", lambda *_: self._toggle_focus())
        self.detail_toggle.set_sensitive(False)
        action_box.append(self.detail_state)
        action_box.append(self.detail_toggle)
        detail.append(action_box)
        content.append(detail)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.warning_label = Gtk.Label(label="", xalign=0.0, hexpand=True, wrap=True)
        self.warning_label.add_css_class("dim")
        self.warning_label.add_css_class("footer")
        self.unblock_button = Gtk.Button(label="Unblock all")
        self.unblock_button.connect("clicked", lambda *_: self._unblock_clicked())
        self.spinner = Gtk.Spinner()
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply_clicked())
        action_row.append(self.warning_label)
        action_row.append(self.unblock_button)
        action_row.append(self.spinner)
        action_row.append(self.apply_button)
        content.append(action_row)

        self.footer_label = Gtk.Label(label="", xalign=0.0, wrap=True)
        self.footer_label.add_css_class("footer")
        content.append(self.footer_label)
        root.append(content)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

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

    def _refresh_ui_from_data(self) -> None:
        self._loading = True
        self._build_preset_model()
        self._build_region_chips()
        self._update_mode_buttons()
        self._loading = False
        self.map.set_data(
            self.entries,
            selected=self.selected,
            mode=self.mode,
            pings=self.pings,
            counters=self.counters,
            focus=self.focus_code,
        )
        self._update_stats()
        self._update_detail()
        self._update_selection_warnings()
        self._update_buttons()
        self._update_status_view()

    def _update_mode_buttons(self) -> None:
        self._loading = True
        self.block_button.set_active(self.mode is Mode.BLOCK)
        self.only_button.set_active(self.mode is Mode.ONLY)
        self._loading = False

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
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            dot = Gtk.Label()
            dot.set_markup(f"<span foreground='{REGION_COLORS.get(region, '#aab8c2')}'>●</span>")
            text = Gtk.Label(label=f"{region.value} 0/{total}")
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
            selected = sum(1 for code in codes if code in self.selected)
            label.set_text(f"{region.value} {selected}/{len(codes)}")
            button = _button
            if selected:
                button.add_css_class("region-chip-active")
            else:
                button.remove_css_class("region-chip-active")
            action = "select" if self.mode is Mode.BLOCK else "allow"
            button.set_tooltip_text(f"{action.title()} all {region.value} POPs")

    def _update_stats(self) -> None:
        codes = [entry.code for entry in self.entries]
        blocked = [code for code in codes if self._is_blocked(code)]
        allowed = [code for code in codes if not self._is_blocked(code)]
        relays = sum(self.entry_by_code[code].relays for code in blocked)
        self.blocked_stat.set_markup(f"<b>{len(blocked)}</b> blocked")
        self.allowed_stat.set_markup(f"<b>{len(allowed)}</b> allowed")
        self.relay_stat.set_markup(f"<b>{relays}</b> relays")
        self.dirty_stat.set_visible(self._dirty())
        self._update_region_chips()

    def _update_detail(self) -> None:
        entry = self.entry_by_code.get(self.focus_code or "")
        if entry is None:
            self.detail_title.set_text("Select a datacenter")
            self.detail_subtitle.set_text("Click a dot on the map or a region chip")
            self.detail_meta.set_text("")
            self.detail_ping.set_text("—")
            self.detail_drops.set_text("—")
            self.detail_state.set_text("")
            self.detail_toggle.set_label("Block this POP")
            self.detail_toggle.set_sensitive(False)
            return
        blocked = self._is_blocked(entry.code)
        self.detail_title.set_text(entry.title)
        self.detail_subtitle.set_text(f"{entry.subtitle}  ·  {entry.code}")
        self.detail_meta.set_text(
            f"{entry.region.value}  ·  {entry.relays} relays  ·  UDP {entry.ports}"
        )
        ping = self.pings.get(entry.code)
        self.detail_ping.set_text("—" if ping is None else f"{ping:.0f} ms")
        self.detail_drops.set_text(f"{self.counters.get(entry.code, 0):,}")
        self.detail_state.set_text("BLOCKED" if blocked else "ALLOWED")
        self.detail_state.remove_css_class("state-blocked")
        self.detail_state.remove_css_class("state-allowed")
        self.detail_state.add_css_class("state-blocked" if blocked else "state-allowed")
        self.detail_toggle.set_label("Allow this POP" if blocked else "Block this POP")
        self.detail_toggle.set_sensitive(self._installed() and not self._busy)
        self.map.set_focus(entry.code)

    def _update_selection_warnings(self) -> None:
        try:
            plan = build_plan(
                self.snapshot,
                Selection(mode=self.mode, pops=frozenset(self.selected)),
            )
            text = "; ".join(plan.warnings)
        except Exception as exc:
            text = str(exc)
        if text:
            self.warning_label.set_text(text)
        else:
            self.warning_label.set_text(
                "Click a dot to toggle it, then press Apply. ICMP stays reachable; "
                "drops are counted per POP."
            )

    def _update_buttons(self) -> None:
        installed = self._installed()
        dirty = self._dirty()
        for button in (
            self.header_refresh_button,
            self.preset_dropdown,
            self.save_preset_button,
            self.unblock_button,
            self.menu_refresh_button,
            self.menu_ping_button,
        ):
            button.set_sensitive(installed and not self._busy)
        self.apply_button.set_sensitive(installed and dirty and not self._busy)
        self.apply_button.set_label("Apply changes" if dirty else "Applied")
        self.apply_button.set_tooltip_text(
            "Apply these changes through the privileged helper"
            if dirty
            else "No unsaved changes"
        )
        self.detail_toggle.set_sensitive(
            installed and not self._busy and self.focus_code in self.entry_by_code
        )
        self.dirty_stat.set_visible(dirty)
        for button, _label, _dot in self._region_chips.values():
            button.set_sensitive(not self._busy)
        self.persistence_switch.set_sensitive(installed and not self._busy)

    # --------------------------------------------------------------- actions
    def _toggle_pop(self, code: str) -> None:
        if code in self.selected:
            self.selected.remove(code)
        else:
            self.selected.add(code)
        self.focus_code = code
        self._pending_changed()

    def _focus_pop(self, code: str | None) -> None:
        self.focus_code = code
        self.map.set_focus(code)
        self._update_detail()
        self._update_buttons()

    def _toggle_focus(self) -> None:
        if self.focus_code:
            self._toggle_pop(self.focus_code)

    def _toggle_region(self, region: Region) -> None:
        codes = [entry.code for entry in self.entries if entry.region is region]
        if not codes:
            return
        if all(code in self.selected for code in codes):
            self.selected.difference_update(codes)
        else:
            self.selected.update(codes)
            self.focus_code = codes[0]
            self.map.zoom_to_region(region)
        self._pending_changed()

    def _pending_changed(self) -> None:
        self.map.set_selection(self.selected, self.mode)
        self.map.set_focus(self.focus_code)
        self._update_stats()
        self._update_detail()
        self._update_selection_warnings()
        self._update_buttons()

    def _mode_clicked(self, button: Gtk.ToggleButton) -> None:
        if self._loading:
            return
        if not button.get_active():
            # Clicking the already-active half should not leave both empty.
            button.set_active(True)
            return
        mode = Mode.BLOCK if button is self.block_button else Mode.ONLY
        if mode is self.mode:
            return
        self.mode = mode
        self._pending_changed()

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
        self._update_mode_buttons()
        self.focus_code = next((code for code in sorted(codes) if code in self.entry_by_code), None)
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
        if self.mode is Mode.ONLY and not pops:
            self._confirm(
                "Block every POP?",
                "The allow-only selection is empty, so every known POP will be blocked. "
                "Matchmaking is likely to fail.",
                lambda: self._do_apply(pops),
            )
            return
        self._do_apply(pops)

    def _do_apply(self, pops: list[str]) -> None:
        self._run_privileged(
            ["apply", "--mode", self.mode.value, "--pops", ",".join(pops)],
            self._after_apply,
        )

    def _after_apply(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self.applied_mode = self.mode
            self.applied_selection = set(self.selected)
            self._update_stats()
            self._update_buttons()
            self._update_status_view()
            self._toast("Rules applied")
        else:
            self._show_error(
                "Apply failed",
                (stderr or stdout).strip() + f"\n\nCommand:\n{command}",
            )

    def _unblock_clicked(self) -> None:
        self._confirm(
            "Unblock everything?",
            "This removes the cs2blocker table and empties the saved selection. "
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
            self.focus_code = None
            self._update_mode_buttons()
            self.map.set_data(
                self.entries,
                selected=self.selected,
                mode=self.mode,
                pings=self.pings,
                counters=self.counters,
                focus=None,
            )
            self._pending_changed()
            self._update_status_view()
            self._toast("All CS2 blocks removed")
        else:
            self._show_error("Unblock failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

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
            self._toast(stdout.strip() or "Preset saved")
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
        self._update_persistence_view()

    # ------------------------------------------------------------------- ping
    def _start_ping(self) -> bool:
        if self.smoke_test:
            return False
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=16)
        self._ping_generation += 1
        generation = self._ping_generation
        for entry in self.entries:
            pop = self.snapshot.pops.get(entry.code)
            target = pop_ping_target(pop.relays) if pop else None
            if target is None:
                self.pings[entry.code] = None
                continue
            future = self._executor.submit(ping, target, 1.0)
            future.add_done_callback(
                lambda fut, code=entry.code, gen=generation: GLib.idle_add(
                    self._ping_done, gen, code, fut
                )
            )
        return False

    def _ping_done(self, generation: int, code: str, future) -> bool:
        if generation != self._ping_generation:
            return False
        try:
            result: PingResult = future.result()
            self.pings[code] = result.rtt_ms
        except Exception:
            self.pings[code] = None
        self.map.set_pings(self.pings)
        if code == self.focus_code:
            self._update_detail()
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

    def _unit_enabled(self) -> bool | None:
        if not which_fixed("systemctl"):
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
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def _update_status_view(self) -> None:
        self._live = self._read_status()
        self._state = self._read_state()
        live = self._live or {}
        state = self._state
        active = bool(live.get("active", state.get("active", False)))
        backend = live.get("backend") or state.get("backend") or "?"
        counters = {
            code: int(value.get("packets", 0))
            for code, value in (live.get("counters") or {}).items()
            if isinstance(value, dict)
        }
        self.counters = counters
        self.map.set_counters(counters)
        if self.focus_code:
            self._update_detail()
        cache = state.get("cache") or {}
        cache_text = ""
        if cache:
            cache_text = (
                f"relay list revision {cache.get('revision', '?')}, "
                f"{cache.get('origin', '?')}, {cache.get('age_human', '?')}"
            )
        persistence = "on" if self._unit_enabled() else "off"
        rules_text = (
            f"{len(live.get('blocked') or state.get('blocked') or [])} POPs blocked"
            if active
            else "rules inactive"
        )
        self.footer_label.set_text(
            f"Rules: {rules_text}  ·  backend {backend}  ·  {cache_text}  ·  "
            f"boot persistence {persistence}  ·  only inet cs2blocker is modified"
        )
        self._update_persistence_view()
        self._update_banner()
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
        self.footer_label.set_text("Working…")

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

    def _confirm(self, heading: str, body: str, on_ok: Callable[[], None]) -> None:
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
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        return False

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
