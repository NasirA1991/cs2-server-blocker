#!/usr/bin/python3
"""GTK4/libadwaita front-end for cs2blocker."""

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

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import __version__
from ..core.config import default_config, load_config
from ..core.model import Mode, SdrSnapshot, Selection
from ..core.ruleset import build_plan
from ..core.ping import PingResult, ping, pop_ping_target, rtt_text
from ..core.presets import resolve_preset
from ..core.regions import REGION_ORDER, group_pops
from ..core.sdr import load_bundled, load_snapshot_file
from ..core.state import Paths, read_optional_json
from ..core.util import Cs2BlockerError, human_age, which_fixed

INSTALLED_HELPER = Path("/usr/lib/cs2blocker/cs2blocker-helper")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODE_LABELS = ["Block selected", "Allow only selected"]


class Cs2BlockerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, *, smoke_test: bool = False) -> None:
        super().__init__(application=application)
        self.set_title("CS2 Server Blocker")
        self.set_default_size(980, 760)
        self.set_size_request(720, 520)
        self.smoke_test = smoke_test
        self.paths = Paths.default()
        self.config = default_config()
        self.snapshot: SdrSnapshot
        self.origin = "bundled"
        self.mode = Mode.BLOCK
        self.selected: set[str] = set()
        self.pop_rows: dict[str, tuple[Gtk.CheckButton, Gtk.Label, Gtk.Label, Adw.ActionRow]] = {}
        self.region_expanders: dict[str, Adw.ExpanderRow] = {}
        self._loading = True
        self._loading_presets = False
        self._persistence_loading = False
        self._live: dict[str, Any] | None = None
        self._state: dict[str, Any] = {}
        self._last_counters: dict[str, int] = {}
        self._ping_generation = 0
        self._executor: ThreadPoolExecutor | None = None
        self._busy = False

        self._load_data()
        self._build_ui()
        self._refresh_ui_from_data()
        self._loading = False
        GLib.idle_add(self._start_ping)
        GLib.timeout_add_seconds(2, self._poll_status)
        if smoke_test:
            GLib.timeout_add(1500, self._smoke_done)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ data
    def _load_data(self) -> None:
        try:
            config = load_config(self.paths, require_root=False, missing_ok=True)
        except Cs2BlockerError:
            config = None
        self.config = config or default_config()
        self.mode = self.config.selection.mode
        self.selected = set(self.config.selection.pops)
        cache = self.paths.cache_file(self.config.appid)
        if cache.is_file():
            try:
                self.snapshot = load_snapshot_file(cache, appid=self.config.appid, require_root=False)
                self.origin = "cache"
                return
            except Cs2BlockerError:
                pass
        self.snapshot = load_bundled(self.config.appid)
        self.origin = "bundled"

    def _installed(self) -> bool:
        return INSTALLED_HELPER.is_file()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="CS2 Server Blocker", subtitle="SDR datacenter firewall rules")
        )
        root.append(header)

        # A plain Gtk.Revealer keeps the GUI compatible with libadwaita 1.1/1.2,
        # where Adw.Banner and Adw.ToolbarView do not exist yet.
        self.banner_revealer = Gtk.Revealer()
        self.banner_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner_box.set_margin_top(8)
        banner_box.set_margin_bottom(8)
        banner_box.set_margin_start(12)
        banner_box.set_margin_end(12)
        self.banner_label = Gtk.Label(xalign=0.0, hexpand=True, wrap=True)
        self.banner_button = Gtk.Button()
        self.banner_button.add_css_class("flat")
        self.banner_button.connect("clicked", lambda *_: self._banner_clicked())
        banner_box.append(self.banner_label)
        banner_box.append(self.banner_button)
        self.banner_revealer.set_child(banner_box)
        root.append(self.banner_revealer)

        self.preferences = Adw.PreferencesPage()
        status_group = Adw.PreferencesGroup(title="Status")
        self.rules_row = Adw.ActionRow(title="Firewall rules", subtitle="checking…")
        self.cache_row = Adw.ActionRow(title="Relay list", subtitle="checking…")
        self.monitor_row = Adw.ActionRow(title="Live counters", subtitle="checking…")
        self.warning_row = Adw.ActionRow(title="Selection check", subtitle="")
        self.persistence_row = Adw.ActionRow(
            title="Boot persistence",
            subtitle="Apply the saved selection at boot",
        )
        self.persistence_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.persistence_switch.connect("notify::active", self._persistence_toggled)
        self.persistence_row.add_suffix(self.persistence_switch)
        status_group.add(self.rules_row)
        status_group.add(self.cache_row)
        status_group.add(self.monitor_row)
        status_group.add(self.warning_row)
        status_group.add(self.persistence_row)
        self.preferences.add(status_group)

        selection_group = Adw.PreferencesGroup(
            title="Selection",
            description="Apply uses pkexec and only changes inet cs2blocker.",
        )
        self.mode_row = Adw.ActionRow(title="Mode", subtitle="Block the selected POPs")
        self.mode_dropdown = Gtk.DropDown.new_from_strings(MODE_LABELS)
        self.mode_dropdown.set_valign(Gtk.Align.CENTER)
        self.mode_dropdown.connect("notify::selected", self._mode_changed)
        self.mode_row.add_suffix(self.mode_dropdown)
        selection_group.add(self.mode_row)

        self.preset_row = Adw.ActionRow(title="Presets", subtitle="Load a preset into the checkboxes")
        self.preset_dropdown = Gtk.DropDown.new_from_strings(["Apply preset…"])
        self.preset_dropdown.set_valign(Gtk.Align.CENTER)
        self.preset_dropdown.connect("notify::selected", self._preset_changed)
        self.preset_row.add_suffix(self.preset_dropdown)
        self.save_preset_button = Gtk.Button(label="Save current…", valign=Gtk.Align.CENTER)
        self.save_preset_button.add_css_class("flat")
        self.save_preset_button.connect("clicked", lambda *_: self._save_preset())
        self.preset_row.add_suffix(self.save_preset_button)
        selection_group.add(self.preset_row)
        self.preferences.add(selection_group)

        self.regions_group = Adw.PreferencesGroup(title="Datacenters")
        self.preferences.add(self.regions_group)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.preferences)

        root.append(scrolled)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        self.status_label = Gtk.Label(label="", xalign=0, hexpand=True)
        self.status_label.add_css_class("dim-label")
        bar.append(self.status_label)
        self.ping_button = Gtk.Button(label="Ping all")
        self.ping_button.set_tooltip_text(
            "ICMP is intentionally not blocked; use the drop counters to verify SDR UDP blocking."
        )
        self.ping_button.connect("clicked", lambda *_: self._start_ping())
        bar.append(self.ping_button)
        self.refresh_button = Gtk.Button(label="Refresh list")
        self.refresh_button.connect("clicked", lambda *_: self._refresh_clicked())
        bar.append(self.refresh_button)
        self.unblock_button = Gtk.Button(label="Unblock all")
        self.unblock_button.add_css_class("destructive-action")
        self.unblock_button.connect("clicked", lambda *_: self._unblock_clicked())
        bar.append(self.unblock_button)
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply_clicked())
        bar.append(self.apply_button)
        root.append(bar)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

    def _rebuild_regions(self) -> None:
        self._loading = True
        for expander in list(self.region_expanders.values()):
            try:
                self.regions_group.remove(expander)
            except Exception:
                pass
        self.pop_rows.clear()
        self.region_expanders.clear()
        grouped = group_pops(self.snapshot.sorted_pops())
        for region in REGION_ORDER:
            pops = grouped.get(region, [])
            if not pops:
                continue
            expander = Adw.ExpanderRow(title=region.value, subtitle="")
            expander.set_expanded(True)
            self.region_expanders[region.value] = expander
            for pop in pops:
                row = Adw.ActionRow(
                    title=f"{pop.code}  ·  {pop.name}",
                    subtitle=(
                        f"{pop.relay_count} relay endpoint(s)"
                        if pop.relays
                        else "no relay endpoints (cannot be blocked)"
                    ),
                )
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                check.set_active(pop.code in self.selected)
                check.set_sensitive(bool(pop.relays))
                check.connect("toggled", self._selection_changed, pop.code)
                row.add_prefix(check)
                ping_label = Gtk.Label(label="—", width_chars=8, xalign=1.0)
                ping_label.add_css_class("dim-label")
                counter_label = Gtk.Label(label="0", width_chars=9, xalign=1.0)
                counter_label.add_css_class("dim-label")
                suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                suffix.append(ping_label)
                suffix.append(counter_label)
                drops = Gtk.Label(label="drops", xalign=0.0)
                drops.add_css_class("dim-label")
                suffix.append(drops)
                row.add_suffix(suffix)
                expander.add_row(row)
                self.pop_rows[pop.code] = (check, ping_label, counter_label, row)
            self.regions_group.add(expander)
        for region in REGION_ORDER:
            expander = self.region_expanders.get(region.value)
            if expander is not None:
                expander.set_subtitle(self._region_subtitle(region))
        self._loading = False

    def _region_subtitle(self, region) -> str:
        selected = sum(
            1
            for code, (check, *_rest) in self.pop_rows.items()
            if check.get_active() and self.snapshot.pops[code].region is region
        )
        total = sum(1 for p in self.snapshot.pops.values() if p.region is region)
        return f"{selected} / {total} selected"

    def _refresh_ui_from_data(self) -> None:
        self._rebuild_regions()
        self._build_preset_model()
        self._loading = True
        self.mode_dropdown.set_selected(1 if self.mode is Mode.ONLY else 0)
        self.mode_row.set_subtitle(
            "Allow only the selected POPs; all other known POPs are blocked"
            if self.mode is Mode.ONLY
            else "Block the selected POPs"
        )
        self._loading = False
        self._update_status_view()
        self._update_buttons()

    def _build_preset_model(self) -> None:
        names = ["Apply preset…"]
        self._preset_keys: list[str | None] = [None]
        for key, preset in sorted(self.config.presets.items()):
            names.append(preset.name)
            self._preset_keys.append(key)
        self._loading_presets = True
        self.preset_dropdown.set_model(Gtk.StringList.new(names))
        self.preset_dropdown.set_selected(0)
        self._loading_presets = False

    def _update_buttons(self) -> None:
        installed = self._installed()
        for button in (self.apply_button, self.unblock_button, self.refresh_button, self.save_preset_button):
            button.set_sensitive(installed and not self._busy)
        self.ping_button.set_sensitive(not self._busy)

    # ---------------------------------------------------------- status/counters
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
        if active:
            blocked = live.get("blocked") or state.get("blocked") or []
            backend = live.get("backend") or state.get("backend") or "?"
            self.rules_row.set_subtitle(f"Active · {len(blocked)} POP(s) blocked · backend={backend}")
        else:
            self.rules_row.set_subtitle("Inactive · no cs2blocker table")
        cache = state.get("cache") or {}
        if cache:
            cache_text = (
                f"revision {cache.get('revision', '?')} · {cache.get('origin', '?')} · "
                f"{cache.get('age_human', '?')}"
            )
            if cache.get("stale"):
                cache_text += " · STALE"
            last_refresh = state.get("last_refresh_at")
            if last_refresh:
                cache_text += f" · refreshed {human_age(last_refresh)}"
            self.cache_row.set_subtitle(cache_text)
        else:
            self.cache_row.set_subtitle(
                f"revision {self.snapshot.revision} · {self.origin} · {human_age(self.snapshot.fetched_at)}"
            )
        if live:
            counters = live.get("counters") or {}
            self.monitor_row.set_subtitle(
                f"Live · {len(counters)} POP(s) reporting drops · {human_age(live.get('updated_at'))}"
                if active
                else "Monitor offline or rules inactive"
            )
        else:
            self.monitor_row.set_subtitle("No live status yet (monitor not running)")
        self._update_counters()
        self._update_selection_warnings()
        self._update_persistence_view()
        self._update_banner()
        self._update_buttons()
        summary = state.get("relay_list_summary")
        last_refresh = state.get("last_refresh_at")
        if state.get("relay_list_changed") and last_refresh and last_refresh != getattr(self, "_seen_refresh", None):
            self._seen_refresh = last_refresh
            if not self.smoke_test:
                self._toast(f"Relay list changed: {summary or 'new Valve data'}")

    def _update_banner(self) -> None:
        state = self._state
        live = self._live or {}
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

    def _update_selection_warnings(self) -> None:
        try:
            selection = Selection(
                mode=self.mode,
                pops=frozenset(self._selected_from_checks()),
            )
            plan = build_plan(self.snapshot, selection)
        except Exception as exc:
            self.warning_row.set_subtitle(str(exc))
            return
        if plan.warnings:
            self.warning_row.set_subtitle("; ".join(plan.warnings))
            self.warning_row.add_css_class("warning")
        else:
            self.warning_row.set_subtitle("No selection warnings")
            self.warning_row.remove_css_class("warning")

    def _update_counters(self) -> None:
        live = self._live or {}
        counters = live.get("counters") or {}
        for code, (_, _, counter_label, _) in self.pop_rows.items():
            value = counters.get(code)
            packets = 0
            if isinstance(value, dict):
                packets = int(value.get("packets", 0))
            counter_label.set_text(f"{packets:,}")
            if packets:
                counter_label.remove_css_class("dim-label")
                counter_label.add_css_class("accent")
            else:
                counter_label.remove_css_class("accent")
                counter_label.add_css_class("dim-label")
            counter_label.set_tooltip_text(
                f"{packets} packets"
                + (f", {int(value.get('bytes', 0)):,} bytes" if isinstance(value, dict) else "")
            )
            previous = self._last_counters.get(code, 0)
            if packets > previous:
                counter_label.add_css_class("warning")
            else:
                counter_label.remove_css_class("warning")
        self._last_counters = {
            code: int(value.get("packets", 0))
            for code, value in counters.items()
            if isinstance(value, dict)
        }

    def _poll_status(self) -> bool:
        if self.smoke_test:
            return True
        self._update_status_view()
        return True

    def _update_persistence_view(self) -> None:
        enabled = self._unit_enabled()
        self._persistence_loading = True
        if enabled is None:
            self.persistence_switch.set_active(False)
            self.persistence_switch.set_sensitive(False)
            self.persistence_row.set_subtitle("systemd unit not installed or systemctl unavailable")
        else:
            self.persistence_switch.set_active(enabled)
            self.persistence_switch.set_sensitive(self._installed() and not self._busy)
            self.persistence_row.set_subtitle(
                "Enabled · rules are applied at boot" if enabled else "Disabled"
            )
        self._persistence_loading = False

    # ---------------------------------------------------------------- actions
    def _selected_from_checks(self) -> set[str]:
        return {
            code
            for code, (check, *_rest) in self.pop_rows.items()
            if check.get_active() and self.snapshot.pops[code].relays
        }

    def _selection_changed(self, check: Gtk.CheckButton, code: str) -> None:
        if self._loading:
            return
        self.selected = self._selected_from_checks()
        for region in REGION_ORDER:
            expander = self.region_expanders.get(region.value)
            if expander is not None:
                expander.set_subtitle(self._region_subtitle(region))
        self._update_selection_warnings()
        self._update_buttons()

    def _mode_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._loading:
            return
        self.mode = Mode.ONLY if dropdown.get_selected() == 1 else Mode.BLOCK
        self.mode_row.set_subtitle(
            "Allow only the selected POPs; all other known POPs are blocked"
            if self.mode is Mode.ONLY
            else "Block the selected POPs"
        )
        self._update_selection_warnings()

    def _preset_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._loading or self._loading_presets:
            return
        index = dropdown.get_selected()
        if index <= 0:
            return
        key = self._preset_keys[index]
        preset = self.config.presets.get(key) if key else None
        if preset is None:
            return
        self._loading = True
        codes, warnings = resolve_preset(preset, self.snapshot)
        for code, (check, *_rest) in self.pop_rows.items():
            check.set_active(code in codes)
        self.mode = preset.mode
        self.mode_dropdown.set_selected(1 if preset.mode is Mode.ONLY else 0)
        self.mode_row.set_subtitle(
            "Allow only the selected POPs; all other known POPs are blocked"
            if preset.mode is Mode.ONLY
            else "Block the selected POPs"
        )
        self.selected = set(codes)
        self._loading = False
        self._update_selection_warnings()
        dropdown.set_selected(0)
        self.status_label.set_text(f"Loaded preset “{preset.name}” — press Apply")
        for warning in warnings:
            self._toast(warning)

    def _apply_clicked(self) -> None:
        if self._busy:
            return
        selected = sorted(self._selected_from_checks())
        if self.mode is Mode.ONLY and not selected:
            self._confirm(
                "Block every POP?",
                "The allow-only selection is empty, so every known POP will be blocked. "
                "Matchmaking is likely to fail.",
                lambda: self._do_apply(selected),
            )
            return
        self._do_apply(selected)

    def _do_apply(self, selected: list[str]) -> None:
        args = ["apply", "--mode", self.mode.value, "--pops", ",".join(selected)]
        self._run_privileged(args, self._after_apply)

    def _after_apply(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._toast("Rules applied")
            self.status_label.set_text("Rules applied")
            self._reload_after_change()
        else:
            self._show_error(
                "Apply failed",
                (stderr or stdout).strip()
                + f"\n\nCommand:\n{command}",
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
            self._toast("All CS2 blocks removed")
            self.status_label.set_text("Unblocked")
            self._reload_after_change()
        else:
            self._show_error("Unblock failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _refresh_clicked(self) -> None:
        if self._busy:
            return
        self._run_privileged(["refresh"], self._after_refresh)

    def _after_refresh(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._toast("Relay list refreshed")
            self.status_label.set_text("Relay list refreshed")
            self._reload_after_change()
        else:
            self._show_error("Refresh failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _persistence_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._persistence_loading or self._loading:
            return
        action = "enable" if switch.get_active() else "disable"
        self._run_privileged(["persistence", action], self._after_persistence)

    def _after_persistence(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._toast(f"Boot persistence {stdout.strip() or 'updated'}")
            self._update_persistence_view()
        else:
            self._toast("Could not change boot persistence")
            self._show_error(
                "Persistence change failed",
                (stderr or stdout).strip() + f"\n\nCommand:\n{command}",
            )
            self._update_persistence_view()

    def _save_preset(self) -> None:
        selected = sorted(self._selected_from_checks())
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
                    ",".join(selected),
                ],
                self._after_save_preset,
            ),
        )

    def _after_save_preset(self, result: tuple[int, str, str, str]) -> None:
        code, stdout, stderr, command = result
        if code == 0:
            self._toast(stdout.strip() or "Preset saved")
            self._reload_after_change()
        else:
            self._show_error("Preset save failed", (stderr or stdout).strip() + f"\n\nCommand:\n{command}")

    def _reload_after_change(self) -> None:
        self._load_data()
        self._refresh_ui_from_data()
        GLib.idle_add(self._start_ping)

    # ------------------------------------------------------------------- ping
    def _start_ping(self) -> bool:
        if self.smoke_test:
            return False
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=16)
        self._ping_generation += 1
        generation = self._ping_generation
        for code, (_, label, _c, _row) in self.pop_rows.items():
            pop = self.snapshot.pops[code]
            target = pop_ping_target(pop.relays)
            if target is None:
                label.set_text("—")
                continue
            label.set_text("…")
            label.remove_css_class("dim-label")
            future = self._executor.submit(ping, target, 1.0)
            future.add_done_callback(
                lambda fut, code=code, gen=generation: GLib.idle_add(
                    self._ping_done, gen, code, fut
                )
            )
        return False

    def _ping_done(self, generation: int, code: str, future) -> bool:
        if generation != self._ping_generation or code not in self.pop_rows:
            return False
        try:
            result: PingResult = future.result()
        except Exception:
            result = None
        label = self.pop_rows[code][1]
        label.remove_css_class("success")
        label.remove_css_class("warning")
        label.remove_css_class("error")
        label.remove_css_class("dim-label")
        if result is None or result.rtt_ms is None:
            label.set_text("n/a")
            label.add_css_class("dim-label")
            label.set_tooltip_text(result.error if result else "ping failed")
        else:
            label.set_text(rtt_text(result))
            if result.rtt_ms < 80:
                label.add_css_class("success")
            elif result.rtt_ms < 180:
                label.add_css_class("warning")
            else:
                label.add_css_class("error")
            label.set_tooltip_text(f"{result.method}: {result.rtt_ms:.1f} ms")
        return False

    def _on_close(self, *_args) -> bool:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        return False

    def _smoke_done(self) -> bool:
        print("GUI smoke test: window constructed and shown")
        self.get_application().quit()
        return False

    # -------------------------------------------------------------- privileges
    def _run_privileged(
        self, args: list[str], callback: Callable[[tuple[int, str, str, str]], None]
    ) -> None:
        self._busy = True
        self.spinner.start()
        self.persistence_switch.set_sensitive(False)
        self._update_buttons()
        self.status_label.set_text("Working…")

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
                return (127, "", "pkexec is not installed; run:\n  sudo cs2blockctl " + " ".join(args), "")
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
        if result.returncode in (126, 127) and "pkexec" in command[0]:
            lowered = (result.stderr or "").lower()
            if "agent" in lowered or "not authorized" in lowered or "dismissed" in lowered:
                result = subprocess.CompletedProcess(
                    args=result.args,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=(
                        (result.stderr or "")
                        + "\nNo polkit agent or authentication was cancelled. Run:\n  "
                        + shlex.join(["sudo", str(INSTALLED_HELPER), *args])
                    ),
                )
        return (result.returncode, result.stdout, result.stderr, display)

    def _privileged_done(
        self,
        callback: Callable[[tuple[int, str, str, str]], None],
        result: tuple[int, str, str, str],
    ) -> bool:
        self._busy = False
        self.spinner.stop()
        self._update_buttons()
        self._update_persistence_view()
        self.status_label.set_text("Ready")
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
        dialog = Gtk.Dialog(
            transient_for=self,
            modal=True,
            title=heading,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="e.g. EU only", activates_default=True)
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
