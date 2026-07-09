#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
On-screen settings menu for the touchscreen dashboard.

Opened by a 5-second press (see main.py). Presents a menu of sub-screens:

    WiFi      - scan/connect to a network (reuses wifi_setup.WifiSetup)
    Zip Code  - set the ZIP used for weather, via a numeric keypad
    Account   - show the connected Claude and Google account names
    Firmware  - app version + system info

Everything is drawn with Pillow (same pipeline as the dashboard) and driven by
taps hit-tested against rectangles rebuilt each render, in the 1920x440 design
space. Each sub-screen's handle_tap() returns 'back' to return to the menu;
the menu's Close returns 'exit' to leave settings entirely.

`ctx` carries the hooks into main.py:
    ctx['current_zip']   -> str            current ZIP
    ctx['apply_zip'](s)                     persist + re-resolve a new ZIP
    ctx['app_version']   -> str
    ctx['fetch_claude']  -> {name,email,plan} | None
    ctx['fetch_google']  -> email str | None
    ctx['current_ssid']  -> str | None
"""
import platform
import socket
import threading
import time

from PIL import Image, ImageDraw

import wifi_setup


# --- shared drawing helpers -------------------------------------------------

def _text_center(draw, cx, y, text, font, color):
    bb = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (bb[2] - bb[0]) / 2, y), text, font=font, fill=color)


def draw_button(draw, fonts, theme, rect, label, action, hits, kind='normal'):
    x0, y0, x1, y1 = rect
    fill = {'ok': theme['ok'], 'alert': theme['alert'],
            'accent': theme['accent']}.get(kind, theme['line'])
    fg = theme['bg'] if kind != 'normal' else theme['fg']
    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=fill, outline=theme['muted'])
    _text_center(draw, (x0 + x1) / 2, (y0 + y1) / 2 - fonts['28'].size / 2,
                 label, fonts['28'], fg)
    hits.append((x0, y0, x1, y1, action))


# --- sub-screens ------------------------------------------------------------

class ZipEntry:
    """Numeric keypad to set the weather ZIP code."""

    def __init__(self, fonts, theme, w, h, ctx):
        self.f, self.t, self.w, self.h, self.ctx = fonts, theme, w, h, ctx
        self.value = str(ctx['current_zip']() or '')
        self.saved = False
        self.dirty = True
        self.animating = False
        self._hits = []

    def handle_tap(self, x, y):
        act = self._hit(x, y)
        if act is None:
            return None
        self.dirty = True
        if act == 'back':
            return 'back'
        if act == 'save':
            if len(self.value) == 5:
                self.ctx['apply_zip'](self.value)
                self.saved = True
            return None
        if act == 'del':
            self.value = self.value[:-1]
        elif act == 'clear':
            self.value = ''
        elif act.isdigit() and len(self.value) < 5:
            self.value += act
        self.saved = False
        return None

    def _hit(self, x, y):
        for x0, y0, x1, y1, a in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return a
        return None

    def render(self):
        self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        d = ImageDraw.Draw(img)
        d.text((24, 12), "Weather ZIP code", font=self.f['35'], fill=self.t['fg'])
        # Field
        d.rounded_rectangle((24, 70, 520, 130), radius=8, outline=self.t['accent'], width=2)
        d.text((40, 80), self.value or "-----", font=self.f['60'], fill=self.t['fg'])
        if self.saved:
            d.text((540, 90), "Saved", font=self.f['28'], fill=self.t['ok'])
        elif len(self.value) != 5:
            d.text((540, 90), "5 digits", font=self.f['24'], fill=self.t['muted'])

        # Keypad 1-9,0 + Clear/Del, laid out on the right half.
        keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'clear', '0', 'del']
        labels = {'clear': 'Clr', 'del': 'Del'}
        kx, ky = 700, 40
        kw, kh, g = 180, 84, 12
        for i, k in enumerate(keys):
            col, row = i % 3, i // 3
            x0 = kx + col * (kw + g)
            y0 = ky + row * (kh + g)
            kind = 'accent' if k in ('clear', 'del') else 'normal'
            draw_button(d, self.f, self.t, (x0, y0, x0 + kw, y0 + kh),
                        labels.get(k, k), k, self._hits, kind)
        # Save / Back at bottom-left under the field.
        draw_button(d, self.f, self.t, (24, 150, 260, 210), "Save", 'save', self._hits, 'ok')
        draw_button(d, self.f, self.t, (284, 150, 520, 210), "Back", 'back', self._hits)
        return img


class AccountInfo:
    """Read-only screen showing the connected Claude and Google accounts."""

    def __init__(self, fonts, theme, w, h, ctx):
        self.f, self.t, self.w, self.h, self.ctx = fonts, theme, w, h, ctx
        self.claude = None
        self.google = None
        self.loading = True
        self.dirty = True
        self._hits = []
        threading.Thread(target=self._load, daemon=True).start()

    @property
    def animating(self):
        return self.loading

    def _load(self):
        claude = self.ctx['fetch_claude']()
        google = self.ctx['fetch_google']()
        self.claude, self.google, self.loading = claude, google, False
        self.dirty = True

    def handle_tap(self, x, y):
        for x0, y0, x1, y1, a in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a == 'back':
                return 'back'
        return None

    def render(self):
        self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        d = ImageDraw.Draw(img)
        d.text((24, 12), "Accounts", font=self.f['35'], fill=self.t['fg'])

        # Claude block
        d.text((40, 90), "Claude", font=self.f['28'], fill=self.t['accent'])
        if self.loading:
            d.text((260, 92), "Loading" + "." * (int(time.time() * 2) % 4),
                   font=self.f['24'], fill=self.t['muted'])
        elif self.claude:
            d.text((260, 84), f"{self.claude['name']}   ({self.claude['plan']})",
                   font=self.f['32'], fill=self.t['fg'])
            d.text((260, 126), self.claude['email'], font=self.f['24'], fill=self.t['muted'])
        else:
            d.text((260, 92), "Not connected", font=self.f['28'], fill=self.t['muted'])

        # Google block
        d.text((40, 210), "Google", font=self.f['28'], fill=self.t['accent'])
        if self.loading:
            d.text((260, 212), "...", font=self.f['24'], fill=self.t['muted'])
        elif self.google:
            d.text((260, 204), self.google, font=self.f['32'], fill=self.t['fg'])
        else:
            d.text((260, 212), "Not connected", font=self.f['28'], fill=self.t['muted'])

        draw_button(d, self.f, self.t, (self.w // 2 - 120, self.h - 62,
                                        self.w // 2 + 120, self.h - 14),
                    "Back", 'back', self._hits)
        return img


class BluetoothScreen:
    """Pair a phone / list & forget paired phones. Drives bluetooth_music.BtMusic."""

    def __init__(self, fonts, theme, w, h, ctx):
        self.f, self.t, self.w, self.h, self.ctx = fonts, theme, w, h, ctx
        self.bt = ctx['bt']()
        self.dirty = True
        self.animating = True   # keep repainting so pairing status/list update live
        self._hits = []
        if self.bt:
            self.bt.refresh()

    def handle_tap(self, x, y):
        act = None
        for x0, y0, x1, y1, a in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                act = a
                break
        if act is None:
            return None
        self.dirty = True
        if act == 'back':
            if self.bt and self.bt.pairing:
                self.bt.stop_pairing()
            return 'back'
        if not self.bt:
            return None
        if act == 'pair':
            self.bt.start_pairing()
        elif act == 'stoppair':
            self.bt.stop_pairing()
        elif act.startswith('forget:'):
            self.bt.forget(act[7:])
            self.bt.refresh()
        return None

    def render(self):
        self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        d = ImageDraw.Draw(img)
        d.text((24, 12), "Bluetooth", font=self.f['35'], fill=self.t['fg'])

        if not self.bt or not getattr(self.bt, 'available', False):
            _text_center(d, self.w // 2, self.h // 2 - 20,
                         "Bluetooth unavailable (dbus-next missing)", self.f['24'], self.t['alert'])
            draw_button(d, self.f, self.t, (self.w // 2 - 120, self.h - 62,
                                            self.w // 2 + 120, self.h - 14), "Back", 'back', self._hits)
            return img

        pairing = self.bt.pairing
        status = self.bt.pair_status
        if status:
            d.text((300, 22), status, font=self.f['20'],
                   fill=self.t['accent'] if pairing else self.t['ok'])

        # Pair / Stop button top-right.
        if pairing:
            draw_button(d, self.f, self.t, (self.w - 300, 14, self.w - 24, 62),
                        "Stop Pairing", 'stoppair', self._hits, 'alert')
        else:
            draw_button(d, self.f, self.t, (self.w - 300, 14, self.w - 24, 62),
                        "Pair New Phone", 'pair', self._hits, 'accent')

        # Paired list.
        d.text((24, 74), "Paired phones:", font=self.f['24'], fill=self.t['muted'])
        y = 112
        paired = list(self.bt.paired)
        if not paired:
            d.text((40, y), "none yet", font=self.f['24'], fill=self.t['muted'])
        for path, name, connected in paired[:3]:
            d.rounded_rectangle((24, y, self.w - 24, y + 56), radius=8, fill=self.t['line'])
            dot = self.t['ok'] if connected else self.t['muted']
            d.ellipse((40, y + 22, 56, y + 38), fill=dot)
            d.text((72, y + 12), name[:30], font=self.f['28'], fill=self.t['fg'])
            draw_button(d, self.f, self.t, (self.w - 190, y + 8, self.w - 34, y + 48),
                        "Forget", f'forget:{path}', self._hits, 'alert')
            y += 64

        draw_button(d, self.f, self.t, (24, self.h - 62, 200, self.h - 14),
                    "Back", 'back', self._hits)
        return img


class FirmwareInfo:
    """App version and system info."""

    def __init__(self, fonts, theme, w, h, ctx):
        self.f, self.t, self.w, self.h, self.ctx = fonts, theme, w, h, ctx
        self.dirty = True
        self.animating = False
        self._hits = []
        self.rows = self._gather()

    def _gather(self):
        try:
            with open('/proc/uptime') as fp:
                secs = int(float(fp.read().split()[0]))
            up = f"{secs // 3600}h {secs % 3600 // 60}m"
        except Exception:
            up = "-"
        ip = "-"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1)
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
        except Exception:
            pass
        return [
            ("Version", self.ctx['app_version']),
            ("Host", socket.gethostname()),
            ("IP", ip),
            ("WiFi", self.ctx['current_ssid']() or "-"),
            ("Python", platform.python_version()),
            ("Uptime", up),
        ]

    def handle_tap(self, x, y):
        for x0, y0, x1, y1, a in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a == 'back':
                return 'back'
        return None

    def render(self):
        self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        d = ImageDraw.Draw(img)
        d.text((24, 12), "Firmware", font=self.f['35'], fill=self.t['fg'])
        d.text((40, 70), "Pi Dashboard", font=self.f['32'], fill=self.t['fg'])
        y = 120
        for k, v in self.rows:
            d.text((40, y), f"{k}:", font=self.f['24'], fill=self.t['muted'])
            d.text((240, y), str(v), font=self.f['24'], fill=self.t['fg'])
            y += 42
        draw_button(d, self.f, self.t, (self.w - 260, self.h - 62,
                                        self.w - 24, self.h - 14),
                    "Back", 'back', self._hits)
        return img


# --- top-level menu ---------------------------------------------------------

class SettingsUI:
    _ITEMS = [("WiFi", 'wifi'), ("Bluetooth", 'bluetooth'), ("Zip Code", 'zip'),
              ("Account", 'account'), ("Firmware", 'firmware')]

    def __init__(self, fonts, theme, w, h, ctx):
        self.f, self.t, self.w, self.h, self.ctx = fonts, theme, w, h, ctx
        self.sub = None
        self._dirty = True
        self._hits = []

    # dirty / animating delegate to the active sub-screen when there is one.
    @property
    def dirty(self):
        return self.sub.dirty if self.sub else self._dirty

    @dirty.setter
    def dirty(self, v):
        if self.sub:
            self.sub.dirty = v
        else:
            self._dirty = v

    @property
    def animating(self):
        return bool(self.sub and getattr(self.sub, 'animating', False))

    def _make(self, key):
        if key == 'wifi':
            return wifi_setup.WifiSetup(self.f, self.t, self.w, self.h)
        if key == 'bluetooth':
            return BluetoothScreen(self.f, self.t, self.w, self.h, self.ctx)
        if key == 'zip':
            return ZipEntry(self.f, self.t, self.w, self.h, self.ctx)
        if key == 'account':
            return AccountInfo(self.f, self.t, self.w, self.h, self.ctx)
        if key == 'firmware':
            return FirmwareInfo(self.f, self.t, self.w, self.h, self.ctx)
        return None

    def handle_tap(self, x, y):
        """Returns 'exit' to close settings, else None."""
        if self.sub is not None:
            if self.sub.handle_tap(x, y) == 'back':
                self.sub = None
                self._dirty = True
            return None
        for x0, y0, x1, y1, act in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                self._dirty = True
                if act == 'close':
                    return 'exit'
                self.sub = self._make(act)
                return None
        return None

    def render(self):
        if self.sub is not None:
            return self.sub.render()
        return self._render_menu()

    def _render_menu(self):
        self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        d = ImageDraw.Draw(img)
        d.text((24, 12), "Settings", font=self.f['35'], fill=self.t['fg'])
        draw_button(d, self.f, self.t, (self.w - 180, 16, self.w - 24, 64),
                    "Close", 'close', self._hits, 'alert')

        # Subtitle hints per tile.
        btobj = self.ctx['bt']()
        bt_hint = "unavailable"
        if btobj:
            connected = next((n for _p, n, c in btobj.paired if c), None)
            bt_hint = connected or ("paired" if btobj.paired else "pair a phone")
        hints = {
            'wifi': self.ctx['current_ssid']() or "not connected",
            'bluetooth': bt_hint,
            'zip': str(self.ctx['current_zip']() or "-"),
            'account': "Claude / Google",
            'firmware': f"v{self.ctx['app_version']}",
        }
        pad, top, gap = 24, 90, 16
        tiles = len(self._ITEMS)
        tw = (self.w - 2 * pad - (tiles - 1) * gap) // tiles
        th = self.h - top - 24
        for i, (label, key) in enumerate(self._ITEMS):
            x0 = pad + i * (tw + gap)
            x1 = x0 + tw
            y0, y1 = top, top + th
            d.rounded_rectangle((x0, y0, x1, y1), radius=14,
                                fill=self.t['line'], outline=self.t['muted'])
            _text_center(d, (x0 + x1) / 2, y0 + th / 2 - 40, label, self.f['35'], self.t['fg'])
            _text_center(d, (x0 + x1) / 2, y0 + th / 2 + 16,
                         hints[key][:22], self.f['20'], self.t['muted'])
            self._hits.append((x0, y0, x1, y1, key))
        return img
