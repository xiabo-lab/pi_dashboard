#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Display backend for the GeeekPi 11.26" 1920x440 HDMI LCD (capacitive touch).

This replaces the old Waveshare 10.85" SPI e-paper driver. The panel is an
ordinary HDMI screen, so frames are pushed with pygame/SDL.

Deployment target is a Pi Zero 2W on Raspberry Pi OS *Lite* (console only),
where the app runs under `cage`, a single-app Wayland kiosk compositor, with
`seatd` providing the seat. Under cage, SDL auto-selects its wayland driver.

Hard-won constraints, measured on this exact hardware (Pi Zero 2W, Pi OS Lite
trixie, cage 0.2, SDL 2.32.4) - see also the sibling `carlyrics` project:

  * Do NOT force SDL_VIDEODRIVER at all. `kmsdrm` fails to start, and `wayland`
    is worse: pygame.init() succeeds but set_mode() blocks forever. Leaving it
    unset makes SDL pick x11 through cage's Xwayland, which works and still
    enumerates the touchscreen as a real SDL touch device.
  * Do NOT force an HDMI mode in cmdline.txt/config.txt - the panel advertises
    its native 1920x440 over EDID, and overriding it gives a black screen.

Touch arrives as SDL finger events. SDL also synthesises a mouse event from the
same touch, so a tap can be reported twice; we filter on `event.touch` and also
time-debounce, because the `touch` attribute is not reliable on every backend.
"""
import glob
import logging
import os
import shutil
import struct
import subprocess
import threading
import time

# pygame would otherwise spew ALSA warnings; the dashboard has no audio.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Design resolution. Frames are composed at this size and scaled if the panel
# comes up at anything else.
SCREEN_W = 1920
SCREEN_H = 440

# Event kinds returned by Display.poll()
TAP = 'tap'
LONG_PRESS = 'long'
SETTINGS = 'settings'
QUIT = 'quit'

LONG_PRESS_SEC = 1.2
# A longer hold opens the settings menu. Fires the instant the threshold is
# crossed (while still holding), so it isn't confused with the theme-toggle
# long-press.
SETTINGS_HOLD_SEC = 5.0

# A touch fires FINGERDOWN *and* a synthesised MOUSEBUTTONDOWN. Ignore a second
# press arriving within this window.
TOUCH_DEBOUNCE_SEC = 0.4

# Only probed if SDL's own auto-detection fails. `wayland` is deliberately absent:
# on the target hardware its set_mode() hangs forever, which is worse than a
# clean failure. kmsdrm is last because it fails there too.
_FALLBACK_DRIVERS = ('x11', 'fbcon', 'kmsdrm')


class DisplayUnavailable(RuntimeError):
    """No usable video device (no HDMI, no SDL driver, pygame missing)."""


# input_event struct on a 64-bit kernel: struct timeval (2x long) + type + code
# + value = 24 bytes. Verified on this Pi's aarch64 trixie kernel.
_EV_FORMAT = 'llHHi'
_EV_SIZE = struct.calcsize(_EV_FORMAT)
_EV_SYN = 0  # end-of-packet marker; every real touch also emits non-SYN events


class ScreenPower:
    """Turn the HDMI panel physically off/on, and wake it on touch.

    This GeeekPi panel exposes no backlight to the Pi, so there is nothing in
    /sys/class/backlight to dim - the only way to save the backlight's life is
    to drop the HDMI signal so the panel enters standby. cage advertises the
    wlr-output-management protocol, so `wlr-randr --output <name> --off` disables
    the connector (real signal-off, backlight off) and `--on` restores it; the
    pygame app keeps running throughout (verified on-device).

    Waking reads the kernel touch device directly rather than waiting for a
    pygame event: while the output is disabled the compositor has no surface to
    route touches to, but the USB touch digitizer still emits evdev events
    regardless of HDMI state, so reading it is the reliable wake source.

    Degrades safely: if wlr-randr, the output, or the touch device can't be
    found, `available` is False and the screen simply stays on.
    """

    # Prefer the stable by-id symlink; fall back to scanning device names.
    _TOUCH_BY_ID = '/dev/input/by-id/usb-ILITEK_ILITEK-TOUCH-event-if00'

    def __init__(self, on_activity=None):
        # on_activity() is called (from the touch thread) on any touch, whether
        # the screen is on or off - the caller uses it to reset its idle timer.
        self.on_activity = on_activity
        self._is_on = True
        self._lock = threading.Lock()
        self._env = self._wayland_env()
        self._output = self._detect_output()
        self._touch_path = self._find_touch_device()
        self.available = bool(
            shutil.which('wlr-randr') and self._output and self._touch_path)

        if self.available:
            logging.info("ScreenPower ready: output=%s touch=%s",
                         self._output, self._touch_path)
        else:
            logging.warning(
                "ScreenPower disabled (wlr-randr=%s output=%s touch=%s); "
                "screen will stay on",
                bool(shutil.which('wlr-randr')), self._output, self._touch_path)

    def _wayland_env(self):
        env = dict(os.environ)
        env.setdefault('XDG_RUNTIME_DIR', '/tmp')
        if not env.get('WAYLAND_DISPLAY'):
            runtime = env['XDG_RUNTIME_DIR']
            socks = [s for s in glob.glob(os.path.join(runtime, 'wayland-*'))
                     if not s.endswith('.lock')]
            if socks:
                env['WAYLAND_DISPLAY'] = os.path.basename(socks[0])
        return env

    def _wlr_randr(self, *args):
        return subprocess.run(('wlr-randr',) + args, env=self._env,
                              capture_output=True, text=True, timeout=10)

    def _detect_output(self):
        try:
            out = self._wlr_randr().stdout
        except Exception as e:
            logging.warning("wlr-randr probe failed: %s", e)
            return None
        for line in out.splitlines():
            # Output headers start in column 0 (e.g. "HDMI-A-1 ..."); their
            # detail lines are indented.
            if line and not line[0].isspace():
                return line.split()[0]
        return None

    def _find_touch_device(self):
        if os.path.exists(self._TOUCH_BY_ID):
            return self._TOUCH_BY_ID
        for name_path in glob.glob('/sys/class/input/event*/device/name'):
            try:
                with open(name_path) as f:
                    if 'TOUCH' in f.read().upper():
                        ev = name_path.split('/')[4]  # .../input/eventN/device/name
                        return f'/dev/input/{ev}'
            except OSError:
                continue
        return None

    @property
    def is_on(self):
        with self._lock:
            return self._is_on

    def screen_off(self):
        if not self.available:
            return
        with self._lock:
            if not self._is_on:
                return
            r = self._wlr_randr('--output', self._output, '--off')
            if r.returncode == 0:
                self._is_on = False
                logging.info("Screen off (idle)")
            else:
                logging.warning("wlr-randr --off failed: %s", r.stderr.strip())

    def screen_on(self):
        if not self.available:
            return
        with self._lock:
            if self._is_on:
                return
            r = self._wlr_randr('--output', self._output, '--on')
            if r.returncode == 0:
                self._is_on = True
                logging.info("Screen on (touch)")
            else:
                logging.warning("wlr-randr --on failed: %s", r.stderr.strip())

    def start(self):
        """Begin watching the touch device in a background thread."""
        if not self.available:
            return
        threading.Thread(target=self._touch_loop, daemon=True).start()

    def _touch_loop(self):
        last_fire = 0.0
        while True:
            try:
                with open(self._touch_path, 'rb') as dev:
                    while True:
                        data = dev.read(_EV_SIZE)  # blocks until an event
                        if not data or len(data) < _EV_SIZE:
                            break
                        _, _, etype, _, _ = struct.unpack(_EV_FORMAT, data)
                        if etype == _EV_SYN:
                            continue
                        # A single touch bursts many events; coalesce so we act
                        # (and wake) at most a few times a second.
                        now = time.monotonic()
                        if now - last_fire < 0.3:
                            continue
                        last_fire = now
                        self.screen_on()          # no-op if already on
                        if self.on_activity:
                            self.on_activity()
            except OSError as e:
                logging.warning("touch device read error: %s; retrying in 2s", e)
                time.sleep(2)


class Display:
    """Owns the SDL window/framebuffer and turns SDL input into touch events."""

    def __init__(self, width=SCREEN_W, height=SCREEN_H, fullscreen=True):
        try:
            import pygame
        except ImportError as e:
            raise DisplayUnavailable("pygame is not installed") from e

        self.pygame = pygame
        self.width = width
        self.height = height
        self._init_video()

        if fullscreen:
            # Ask for the compositor's own size rather than dictating one:
            # forcing a mode on this panel is what produces a black screen.
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption('Pi Dashboard')
        pygame.mouse.set_visible(False)

        self.driver = pygame.display.get_driver()
        self.surface_size = self.screen.get_size()
        self._needs_scale = self.surface_size != (width, height)
        if self._needs_scale:
            logging.warning(
                "Panel came up at %sx%s, scaling frames from the %sx%s design size",
                self.surface_size[0], self.surface_size[1], width, height)

        self.clock = pygame.time.Clock()
        self._press_start = None
        self._press_pos = (0, 0)
        self._press_consumed = False
        self._last_press = 0.0

        logging.info("Display ready: %sx%s via SDL driver '%s'",
                     self.surface_size[0], self.surface_size[1], self.driver)

    def _init_video(self):
        """Let SDL choose. Only probe explicitly if its own detection fails."""
        pygame = self.pygame
        pygame.init()

        if pygame.display.get_init():
            return

        # pygame.init() swallows display errors; nothing came up, so probe.
        forced = os.environ.get('SDL_VIDEODRIVER')
        if forced:
            pygame.display.init()  # let the error surface - the user asked for this one
            return

        for driver in _FALLBACK_DRIVERS:
            os.environ['SDL_VIDEODRIVER'] = driver
            try:
                pygame.display.init()
                return
            except pygame.error:
                pygame.display.quit()

        os.environ.pop('SDL_VIDEODRIVER', None)
        raise DisplayUnavailable(
            "SDL could not open a video device (auto-detect failed; tried: %s). "
            "On a Lite image the dashboard must run under `cage` - it cannot be "
            "started from a plain SSH session." % ', '.join(_FALLBACK_DRIVERS))

    def show(self, pil_image):
        """Blit a PIL RGB image to the panel."""
        pygame = self.pygame
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')

        frombytes = getattr(pygame.image, 'frombytes', pygame.image.fromstring)
        surface = frombytes(pil_image.tobytes(), pil_image.size, 'RGB')
        if self._needs_scale:
            surface = pygame.transform.smoothscale(surface, self.surface_size)
        self.screen.blit(surface, (0, 0))
        pygame.display.flip()

    def poll(self):
        """Drain SDL's queue and return a list of (kind, (x, y)) touch events."""
        pygame = self.pygame
        events = []

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                events.append((QUIT, (0, 0)))

            elif e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE, pygame.K_q):
                    events.append((QUIT, (0, 0)))
                elif e.key == pygame.K_r:
                    events.append((TAP, (0, 0)))
                elif e.key == pygame.K_t:
                    events.append((LONG_PRESS, (0, 0)))
                elif e.key == pygame.K_s:
                    events.append((SETTINGS, (0, 0)))

            elif e.type == pygame.FINGERDOWN:
                self._begin_press((e.x * self.width, e.y * self.height))
            elif e.type == pygame.FINGERUP:
                events.extend(self._end_press((e.x * self.width, e.y * self.height)))

            # SDL synthesises mouse events from touch; `e.touch` marks those so
            # a single tap is not counted twice.
            elif e.type == pygame.MOUSEBUTTONDOWN and not getattr(e, 'touch', False):
                self._begin_press(e.pos)
            elif e.type == pygame.MOUSEBUTTONUP and not getattr(e, 'touch', False):
                events.extend(self._end_press(e.pos))

        # Open settings the moment the hold crosses the threshold, while the
        # finger is still down, and mark the press consumed so the eventual
        # release doesn't also emit a tap/long-press.
        if self._press_start is not None and not self._press_consumed:
            held = self.pygame.time.get_ticks() / 1000.0 - self._press_start
            if held >= SETTINGS_HOLD_SEC:
                self._press_consumed = True
                events.append((SETTINGS, (int(self._press_pos[0]), int(self._press_pos[1]))))

        return events

    def hold_seconds(self):
        """Seconds the current press has been held, or 0 if no press in progress."""
        if self._press_start is None or self._press_consumed:
            return 0.0
        return self.pygame.time.get_ticks() / 1000.0 - self._press_start

    def _begin_press(self, pos):
        now = self.pygame.time.get_ticks() / 1000.0
        if now - self._last_press < TOUCH_DEBOUNCE_SEC:
            return  # synthesised duplicate of the press we just took
        self._last_press = now
        self._press_start = now
        self._press_pos = pos
        self._press_consumed = False

    def _end_press(self, pos):
        if self._press_start is None:
            return []
        held = (self.pygame.time.get_ticks() / 1000.0) - self._press_start
        consumed = self._press_consumed
        self._press_start = None
        self._press_consumed = False
        if consumed:
            return []  # already emitted WIFI on the threshold crossing
        kind = LONG_PRESS if held >= LONG_PRESS_SEC else TAP
        return [(kind, (int(pos[0]), int(pos[1])))]

    def tick(self, fps=30):
        """Cap the event loop so touch stays responsive without busy-waiting."""
        self.clock.tick(fps)

    def close(self):
        try:
            self.pygame.display.quit()
            self.pygame.quit()
        except Exception:
            pass
