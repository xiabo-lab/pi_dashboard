#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
On-screen WiFi setup for the touchscreen dashboard.

Entered by a 5-second press on the screen (see main.py). Because the Pi has no
keyboard, this draws a network list and an on-screen keyboard with Pillow - the
same rendering pipeline the dashboard uses - and connects via nmcli, so the
change persists in NetworkManager and reconnects on boot. The service runs as
root, so nmcli needs no sudo.

The UI is a small state machine:
    SCANNING -> LIST -> PASSWORD -> CONNECTING -> RESULT
Taps are hit-tested against rectangles rebuilt each render; coordinates arrive
from the display backend in the 1920x440 design space.
"""
import logging
import subprocess
import threading
import time

from PIL import Image, ImageDraw

# UI states
SCANNING, LIST, PASSWORD, CONNECTING, RESULT = 'scan', 'list', 'pw', 'connecting', 'result'

WIFI_DEVICE = 'wlan0'


# --- nmcli helpers -----------------------------------------------------------

def _split_terse(line):
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes ':' and '\\')."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == '\\' and i + 1 < len(line):
            cur.append(line[i + 1]); i += 2; continue
        if c == ':':
            out.append(''.join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    out.append(''.join(cur))
    return out


def scan_networks(rescan=False):
    """Return [{'ssid','signal','secure'}], strongest first, deduped."""
    if rescan:
        subprocess.run(['nmcli', 'device', 'wifi', 'rescan'],
                       capture_output=True, timeout=20)
        time.sleep(2)
    try:
        out = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list'],
            capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        logging.error(f"wifi scan failed: {e}")
        return []

    best = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = _split_terse(line)
        ssid = parts[0].strip()
        if not ssid:  # hidden network, no usable name
            continue
        try:
            signal = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        except ValueError:
            signal = 0
        secure = bool(parts[2].strip()) if len(parts) > 2 else True
        if ssid not in best or signal > best[ssid]['signal']:
            best[ssid] = {'ssid': ssid, 'signal': signal, 'secure': secure}
    return sorted(best.values(), key=lambda n: n['signal'], reverse=True)


def current_ssid():
    try:
        out = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'],
                             capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[0] == 'yes':
                return parts[1]
    except Exception:
        pass
    return None


def _delete_profiles_for_ssid(ssid):
    """Delete every saved connection bound to this SSID, whatever its name.

    A stale/incomplete profile for the target network makes `nmcli dev wifi
    connect` reuse it and fail with "802-11-wireless-security.key-mgmt: property
    is missing". These profiles are often not named after the SSID (e.g.
    netplan-created `netplan-wlan0-<ssid>`), so we match on the actual SSID
    field, not the connection name.
    """
    try:
        out = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 2 or parts[1] != '802-11-wireless':
            continue
        name = parts[0]
        try:
            info = subprocess.run(
                ['nmcli', '-t', '-f', '802-11-wireless.ssid', 'con', 'show', name],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            continue
        info_parts = _split_terse(info)  # ['802-11-wireless.ssid', '<ssid>']
        prof_ssid = info_parts[1] if len(info_parts) >= 2 else ''
        if prof_ssid == ssid or name == ssid:
            subprocess.run(['nmcli', 'con', 'delete', name],
                           capture_output=True, timeout=10)


def connect(ssid, password, secure):
    """Blocking nmcli connect. Returns (ok, message)."""
    # Clear any stale profile for this SSID first so we always build a fresh,
    # complete one. (The other saved networks remain, so this can't strand the
    # Pi offline if the new password is wrong.)
    _delete_profiles_for_ssid(ssid)
    cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
    if secure and password:
        cmd += ['password', password]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    if r.returncode == 0:
        return True, "Connected"
    err = (r.stderr or r.stdout or "Failed").strip().splitlines()
    msg = err[-1] if err else "Failed"
    return False, msg.replace('Error: ', '')[:60]


# --- keyboard layout ---------------------------------------------------------

_ROWS_ABC = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
_ROWS_SYM = ("1234567890", "!@#$%^&*()", "-_=+[]{};:", "'\",.?/\\|~")


class WifiSetup:
    """Full-screen WiFi setup overlay, rendered to a PIL image."""

    def __init__(self, fonts, theme, width, height):
        self.f = fonts
        self.t = theme                     # live reference to main.THEME
        self.w, self.h = width, height
        self.state = SCANNING
        self.networks = []
        self.selected = None               # chosen network dict
        self.password = ''
        self.show_pw = False
        self.shift = False
        self.symbols = False
        self.result_ok = False
        self.result_msg = ''
        self.current = current_ssid()
        self._hits = []                    # [(x0,y0,x1,y1,action)]
        self._lock = threading.Lock()
        self.dirty = True
        self._spin = 0
        # Fresh rescan on entry so the list is complete, not just NM's cache.
        threading.Thread(target=self._scan_thread, args=(True,), daemon=True).start()

    # --- background work ---
    def _scan_thread(self, rescan):
        nets = scan_networks(rescan)
        with self._lock:
            self.networks = nets
            self.current = current_ssid()
            if self.state == SCANNING:
                self.state = LIST
            self.dirty = True

    def _connect_thread(self):
        ok, msg = connect(self.selected['ssid'], self.password,
                          self.selected['secure'])
        with self._lock:
            self.result_ok, self.result_msg = ok, msg
            self.current = current_ssid()
            self.state = RESULT
            self.dirty = True

    @property
    def animating(self):
        """True while a background scan/connect is running (needs periodic redraw)."""
        return self.state in (SCANNING, CONNECTING)

    # --- input ---
    def handle_tap(self, x, y):
        """Process a tap. Returns 'back' to leave WiFi setup, else None."""
        action = None
        for x0, y0, x1, y1, act in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                action = act
                break
        if action is None:
            return None
        self.dirty = True

        if action == 'cancel':
            return 'back'
        if action == 'rescan':
            self.state = SCANNING
            threading.Thread(target=self._scan_thread, args=(True,), daemon=True).start()
            return None
        if action.startswith('ssid:'):
            idx = int(action[5:])
            self.selected = self.networks[idx]
            if self.selected['secure']:
                self.password, self.shift, self.symbols = '', False, False
                self.state = PASSWORD
            else:
                self._begin_connect()
            return None

        if self.state == PASSWORD:
            return self._password_key(action)
        if self.state == RESULT:
            if action in ('ok', 'done'):
                return 'back' if self.result_ok else None
            if action == 'retry':
                self.state = PASSWORD
        return None

    def _password_key(self, action):
        if action == 'back':
            self.state = LIST
        elif action == 'connect':
            if self.password:
                self._begin_connect()
        elif action == 'shift':
            self.shift = not self.shift
        elif action == 'sym':
            self.symbols = not self.symbols
        elif action == 'space':
            self.password += ' '
        elif action == 'del':
            self.password = self.password[:-1]
        elif action == 'show':
            self.show_pw = not self.show_pw
        elif len(action) == 1:
            ch = action.upper() if (self.shift and not self.symbols) else action
            self.password += ch
            self.shift = False   # one-shot shift, like a phone keyboard
        return None

    def _begin_connect(self):
        self.state = CONNECTING
        threading.Thread(target=self._connect_thread, daemon=True).start()

    # --- drawing helpers ---
    def _key(self, draw, x0, y0, x1, y1, label, action, fill=None, fg=None):
        t = self.t
        draw.rounded_rectangle((x0, y0, x1, y1), radius=8,
                               fill=fill or t['line'], outline=t['muted'])
        fnt = self.f['28']
        bb = draw.textbbox((0, 0), label, font=fnt)
        draw.text((((x0 + x1) - (bb[2] - bb[0])) / 2,
                   ((y0 + y1) - (bb[3] + bb[1])) / 2), label,
                  font=fnt, fill=fg or t['fg'])
        self._hits.append((x0, y0, x1, y1, action))

    def _button(self, draw, x0, y0, x1, y1, label, action, kind='normal'):
        t = self.t
        fill = {'ok': t['ok'], 'alert': t['alert'], 'accent': t['accent']}.get(kind, t['line'])
        fg = t['bg'] if kind != 'normal' else t['fg']
        self._key(draw, x0, y0, x1, y1, label, action, fill=fill, fg=fg)

    # --- render ---
    def render(self):
        with self._lock:
            state = self.state
            self._hits = []
        img = Image.new('RGB', (self.w, self.h), self.t['bg'])
        draw = ImageDraw.Draw(img)
        draw.text((24, 12), "WiFi Setup", font=self.f['35'], fill=self.t['fg'])
        cur = f"Connected: {self.current}" if self.current else "Not connected"
        bb = draw.textbbox((0, 0), cur, font=self.f['20'])
        draw.text((self.w - 24 - (bb[2] - bb[0]), 24), cur, font=self.f['20'],
                  fill=self.t['ok'] if self.current else self.t['muted'])

        if state == SCANNING:
            self._center(draw, "Scanning for networks" + "." * (self._spin % 4))
        elif state == LIST:
            self._render_list(draw)
        elif state == PASSWORD:
            self._render_password(draw)
        elif state == CONNECTING:
            self._center(draw, f"Connecting to {self.selected['ssid']}"
                               + "." * (self._spin % 4))
        elif state == RESULT:
            self._render_result(draw)
        self._spin += 1
        return img

    def _center(self, draw, text):
        bb = draw.textbbox((0, 0), text, font=self.f['40'])
        draw.text(((self.w - (bb[2] - bb[0])) / 2, (self.h - 40) / 2), text,
                  font=self.f['40'], fill=self.t['fg'])

    def _draw_lock(self, draw, cx, cy, color):
        """Small padlock drawn with primitives (the font lacks a lock glyph)."""
        bw, bh = 16, 12
        x0, y0 = cx - bw // 2, cy - 2
        draw.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=2, fill=color)
        sr = 5
        draw.arc((cx - sr, y0 - sr - 3, cx + sr, y0 + sr - 1),
                 start=180, end=360, fill=color, width=2)

    def _render_list(self, draw):
        t = self.t
        top, rowh, gap = 64, 44, 6
        pad = 24
        # Footer buttons first (fixed position).
        by = self.h - 56
        self._button(draw, pad, by, pad + 220, by + 44, "Rescan", 'rescan', 'accent')
        self._button(draw, self.w - pad - 220, by, self.w - pad, by + 44, "Back", 'cancel', 'normal')
        # Network rows.
        avail = by - gap - top
        maxrows = max(1, avail // (rowh + gap))
        for i, net in enumerate(self.networks[:maxrows]):
            y0 = top + i * (rowh + gap)
            y1 = y0 + rowh
            draw.rounded_rectangle((pad, y0, self.w - pad, y1), radius=8,
                                   fill=t['line'])
            draw.text((pad + 16, y0 + 8), net['ssid'], font=self.f['28'], fill=t['fg'])
            sig = f"{net['signal']}%"
            bb = draw.textbbox((0, 0), sig, font=self.f['24'])
            sx = self.w - pad - 20 - (bb[2] - bb[0])
            draw.text((sx, y0 + 10), sig, font=self.f['24'], fill=t['muted'])
            if net['secure']:
                self._draw_lock(draw, sx - 22, y0 + rowh // 2, t['muted'])
            self._hits.append((pad, y0, self.w - pad, y1, f"ssid:{i}"))
        if not self.networks:
            self._center(draw, "No networks found - tap Rescan")

    def _render_password(self, draw):
        t = self.t
        pad = 24
        draw.text((pad, 60), f"Password for  {self.selected['ssid']}",
                  font=self.f['28'], fill=t['fg'])
        # Password field.
        fy0, fy1 = 96, 140
        draw.rounded_rectangle((pad, fy0, self.w - pad - 360, fy1), radius=8,
                               outline=t['accent'], width=2)
        shown = self.password if self.show_pw else '•' * len(self.password)
        draw.text((pad + 12, fy0 + 8), shown or " ", font=self.f['28'], fill=t['fg'])
        self._button(draw, self.w - pad - 344, fy0, self.w - pad - 180, fy1,
                     "Hide" if self.show_pw else "Show", 'show')
        self._button(draw, self.w - pad - 168, fy0, self.w - pad, fy1, "Back", 'back')
        # Keyboard.
        self._render_keyboard(draw, top=150, bottom=self.h - 8)

    def _render_keyboard(self, draw, top, bottom):
        pad = 16
        rows = _ROWS_SYM if self.symbols else _ROWS_ABC
        n_rows = len(rows) + 1
        gy = 8
        kh = (bottom - top - (n_rows - 1) * gy) // n_rows
        y = top
        for row in rows:
            n = len(row)
            gx = 8
            kw = (self.w - 2 * pad - (n - 1) * gx) // n
            row_w = n * kw + (n - 1) * gx
            x = (self.w - row_w) // 2
            for ch in row:
                label = ch.upper() if (self.shift and not self.symbols and ch.isalpha()) else ch
                self._key(draw, x, y, x + kw, y + kh, label, ch)
                x += kw + gx
            y += kh + gy
        # Bottom function row: proportional widths.
        specs = [("ABC" if self.symbols else "?123", 'sym', 1.4),
                 ("Shift", 'shift', 1.4),
                 ("Space", 'space', 3.0),
                 ("Del", 'del', 1.4),
                 ("Connect", 'connect', 2.2)]
        gx = 8
        total_u = sum(u for _l, _a, u in specs)
        avail = self.w - 2 * pad - (len(specs) - 1) * gx
        x = pad
        for label, action, u in specs:
            kw = int(avail * (u / total_u))
            kind = 'ok' if action == 'connect' else \
                   'accent' if (action == 'shift' and self.shift) else 'normal'
            self._button(draw, x, y, x + kw, y + kh, label, action, kind)
            x += kw + gx

    def _render_result(self, draw):
        ok = self.result_ok
        msg = ("Connected!" if ok else "Could not connect")
        self._center_at(draw, msg, 120, self.f['40'],
                        self.t['ok'] if ok else self.t['alert'])
        detail = (f"{self.selected['ssid']}  -  {self.current}" if ok
                  else self.result_msg)
        self._center_at(draw, detail, 190, self.f['24'], self.t['muted'])
        pad = 24
        by = self.h - 60
        if ok:
            self._button(draw, self.w // 2 - 110, by, self.w // 2 + 110, by + 48,
                         "Done", 'done', 'ok')
        else:
            self._button(draw, self.w // 2 - 240, by, self.w // 2 - 10, by + 48,
                         "Retry", 'retry', 'accent')
            self._button(draw, self.w // 2 + 10, by, self.w // 2 + 240, by + 48,
                         "Back", 'cancel', 'normal')

    def _center_at(self, draw, text, y, font, color):
        bb = draw.textbbox((0, 0), text, font=font)
        draw.text(((self.w - (bb[2] - bb[0])) / 2, y), text, font=font, fill=color)
