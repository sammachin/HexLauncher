"""Hexpansion launcher for Tildagon.

Scans all 6 hexpansion ports for EEPROM headers, mounts their
filesystems, and launches apps — even if the hexpansion booted
too slowly to be detected at insertion time.
"""

import app
import os
import sys
from app_components import clear_background
from system.eventbus import eventbus
from events.input import Buttons, BUTTON_TYPES, ButtonDownEvent
from system.scheduler.events import RequestForegroundPushEvent, RequestStartAppEvent
from system.hexpansion.header import read_header
from system.hexpansion.config import HexpansionConfig
from system.hexpansion.app import detect_eeprom_addr, get_hexpansion_block_devices


class HexLauncherApp(app.App):
    def __init__(self):
        super().__init__()
        self.buttons = Buttons(self)
        self.hexes = []  # [{port, name, has_app, header}]
        self.idx = 0
        self.scan_timer = 0
        self.toast = ""
        self.toast_t = 0
        self._fg = False
        self._active = False
        self.launched = {}  # port -> app instance
        self._pending = None  # deferred launch (waits for toast to render)
        self._pending_drawn = False

    # ---- Scanning ----

    def _scan(self):
        found = []
        for port in range(1, 7):
            try:
                h = read_header(port)
                if h:
                    entry = {
                        "port": port,
                        "name": getattr(h, "friendly_name", "") or "?",
                        "header": h,
                        "has_app": False,
                    }
                    # Check if already mounted and has app.py
                    mp = "/hexpansion_%d" % port
                    try:
                        entry["has_app"] = "app.py" in os.listdir(mp)
                    except OSError:
                        # Not mounted — try mounting to check
                        if self._mount(port, h):
                            try:
                                entry["has_app"] = "app.py" in os.listdir(mp)
                            except OSError:
                                pass
                    found.append(entry)
            except Exception:
                pass
        self.hexes = found

    def _mount(self, port, header):
        """Mount hexpansion EEPROM filesystem. Returns True on success."""
        mp = "/hexpansion_%d" % port
        try:
            config = HexpansionConfig(port)
            addr, addr_len = detect_eeprom_addr(config.i2c)
            eep, partition = get_hexpansion_block_devices(
                config.i2c, header, addr, addr_len
            )
            try:
                os.umount(mp)
            except Exception:
                pass
            os.mount(os.VfsLfs2(partition), mp)
            return True
        except Exception as e:
            self._show_toast("Mount: " + str(e)[:20])
            return False

    def _launch(self, entry):
        """Mount (if needed) and launch a hexpansion app."""
        port = entry["port"]

        # If already launched, bring back to foreground
        if port in self.launched:
            try:
                eventbus.emit(RequestForegroundPushEvent(self.launched[port]))
                self.toast_t = 0
                return
            except Exception:
                del self.launched[port]

        mp = "/hexpansion_%d" % port

        # Ensure mounted
        try:
            os.listdir(mp)
        except OSError:
            if not self._mount(port, entry["header"]):
                return

        # Check for app
        try:
            if "app.py" not in os.listdir(mp):
                self._show_toast("No app.py")
                return
        except OSError:
            self._show_toast("Read failed")
            return

        # Import the app module
        try:
            pkg_name = "hexpansion_%d" % port
            mod_name = pkg_name + ".app"

            # Clear any cached modules for this package
            for name in list(sys.modules.keys()):
                if name.startswith(pkg_name):
                    del sys.modules[name]

            # __import__ returns the top-level package, not the submodule
            __import__(mod_name)
            mod = sys.modules[mod_name]

            app_class = getattr(mod, "__app_export__", None)
            if not app_class:
                self._show_toast("No __app_export__")
                return

            config = HexpansionConfig(port)
            instance = app_class(config=config)
            self.launched[port] = instance
            eventbus.emit(RequestStartAppEvent(instance))
            self.toast_t = 0
        except Exception as e:
            self._show_toast("ERR: " + str(e)[:20])

    def _show_toast(self, text):
        self.toast = text
        self.toast_t = 3000

    # ---- Input ----

    def _on_button(self, event):
        b = event.button
        if BUTTON_TYPES["CANCEL"] in b:
            eventbus.remove(ButtonDownEvent, self._on_button, self)
            self._active = False
            self.minimise()
        elif BUTTON_TYPES["UP"] in b:
            self.idx = max(0, self.idx - 1)
        elif BUTTON_TYPES["DOWN"] in b:
            if self.hexes:
                self.idx = min(len(self.hexes) - 1, self.idx + 1)
        elif BUTTON_TYPES["CONFIRM"] in b:
            if self.hexes and self.idx < len(self.hexes):
                entry = self.hexes[self.idx]
                if entry["has_app"]:
                    self._show_toast("Launching P%d..." % entry["port"])
                    self._pending = entry
                else:
                    self._show_toast("No app on P%d" % entry["port"])
            else:
                self._scan()

    # ---- Lifecycle ----

    def update(self, delta):
        if not self._fg:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._fg = True
            self._scan()

        # Deferred launch — only after draw() has rendered the toast
        if self._pending and self._pending_drawn:
            entry = self._pending
            self._pending = None
            self._pending_drawn = False
            self._launch(entry)
            return

        self.scan_timer += delta
        if self.scan_timer >= 3000:
            self.scan_timer = 0
            self._scan()

        if self.toast_t > 0:
            self.toast_t -= delta

    def draw(self, ctx):
        # Re-register button handler when we're foreground again
        # (draw is only called for the foreground app)
        if not self._active:
            eventbus.on(ButtonDownEvent, self._on_button, self)
            self._active = True
            self.toast_t = 0

        if self._pending:
            self._pending_drawn = True

        ctx.save()
        clear_background(ctx)
        ctx.text_align = ctx.CENTER
        ctx.font_size = 20
        ctx.rgb(0.2, 0.7, 1.0).move_to(0, -80).text("HEX LAUNCHER")

        if not self.hexes:
            ctx.font_size = 16
            ctx.rgb(0.5, 0.5, 0.5).move_to(0, -10).text("Scanning...")
            ctx.font_size = 12
            ctx.rgb(0.3, 0.3, 0.3).move_to(0, 15).text("Insert a hexpansion")
        else:
            self.idx = min(self.idx, len(self.hexes) - 1)
            ctx.font_size = 18
            y = -40
            for i, entry in enumerate(self.hexes):
                label = "P%d %s" % (entry["port"], entry["name"])
                if entry["has_app"]:
                    label += " [app]"
                if i == self.idx:
                    ctx.rgb(1, 1, 0).move_to(0, y).text("> " + label)
                else:
                    ctx.rgb(0.6, 0.6, 0.6).move_to(0, y).text(label)
                y += 28

            ctx.font_size = 10
            ctx.rgb(0.3, 0.3, 0.3).move_to(0, 78).text("[OK] launch")

        if self.toast_t > 0 and self.toast:
            ctx.font_size = 12
            ctx.rgb(0, 1, 0.3).move_to(0, 90).text(self.toast[:32])

        ctx.restore()


__app_export__ = HexLauncherApp