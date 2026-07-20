#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""HC-SR501 PIR motion sensor -> "someone walked up" wake source.

The dashboard's screensaver is normally dismissed by a touch (see ScreenPower in
display.py). This module adds a second wake source: an HC-SR501 pointed out from
the panel, which fires when its passive-infrared field sees a person move.

Wiring (BCM numbering):

    HC-SR501 VCC  -> 5V      (header pin 2 or 4)
    HC-SR501 GND  -> GND     (header pin 6)
    HC-SR501 OUT  -> GPIO23  (header pin 16)

The HC-SR501's OUT idles at 0V and pulses to 3.3V while it sees motion, so it
connects straight to a Pi GPIO with no divider (unlike the HC-SR04 this replaced,
whose 5V ECHO needed one). Its two on-board pots set sensitivity and how long OUT
stays high after a trigger; its jumper picks retrigger mode. We only read OUT.

Design notes:

* Fires on the *rising edge* of OUT (idle -> motion), so a single detection wakes
  the screen once. While OUT stays high (the module's own hold time) we don't
  re-fire; a fresh rising edge is needed. Bumping the activity timer on each wake
  is all it takes - the main loop leaves the screensaver as soon as it stops
  being idle, so one wake per approach is plenty.
* Degrades safely: if gpiozero or the GPIO chip is missing (e.g. running
  `main.py --preview` on a laptop), `available` is False and nothing else in the
  dashboard changes - the screensaver still wakes on touch.
"""
import time
import logging
import threading


class ProximitySensor:
    """Watches an HC-SR501's OUT line and calls on_detect() when motion starts.

    Kept the name ProximitySensor (rather than MotionSensor) so the rest of the
    dashboard - main.py's global_sensor, the settings hooks - needs no renaming.
    """

    def __init__(self, out_pin, poll_interval_s=0.2, on_detect=None,
                 enabled=True):
        self.out_pin = out_pin
        self.poll_interval_s = poll_interval_s
        self.on_detect = on_detect
        # User-facing on/off from the settings menu. When False the OUT line is
        # still read (so the live readout works) but motion never wakes the screen.
        self.enabled = bool(enabled)

        self._prev_high = False   # last OUT level, to catch the rising edge
        # Most recent OUT level, for the on-screen readout. None until first read.
        self.motion = None
        self.last_reading_t = 0.0
        self._stop = threading.Event()
        self._out = None

        try:
            from gpiozero import DigitalInputDevice
            self._out = DigitalInputDevice(out_pin)
            self.available = True
        except Exception as e:
            self.available = False
            logging.warning(
                "Motion sensor disabled (out=%s): %s; "
                "the screensaver still wakes on touch", out_pin, e)
            return

        logging.info("Motion sensor ready: out=GPIO%s (%s)",
                     out_pin, "on" if self.enabled else "off")

    def set_enabled(self, enabled):
        """Turn motion-wake on or off while running (from the settings menu)."""
        self.enabled = bool(enabled)
        # Drop any in-progress edge so re-enabling doesn't fire on stale motion.
        self._prev_high = False
        logging.info("Motion wake %s", "on" if self.enabled else "off")

    def start(self):
        if self.available:
            threading.Thread(target=self._loop, daemon=True).start()

    def close(self):
        self._stop.set()
        if self._out is not None:
            try:
                self._out.close()
            except Exception:
                pass

    def _update(self, high):
        """Fold one OUT reading into the edge state. Returns True if it fired."""
        rising = high and not self._prev_high
        self._prev_high = high
        if rising and self.enabled:
            logging.info("Motion detected, waking screen")
            if self.on_detect:
                self.on_detect()
            return True
        return False

    def _loop(self):
        while not self._stop.is_set():
            try:
                high = bool(self._out.value)
            except Exception as e:
                logging.warning("Motion read error: %s; retrying in 2s", e)
                self._stop.wait(2)
                continue

            self.motion = high                      # for the on-screen readout
            self.last_reading_t = time.monotonic()
            self._update(high)
            self._stop.wait(self.poll_interval_s)


if __name__ == '__main__':
    # Wiring check: `python3 proximity.py` prints OUT transitions and says WAKE
    # whenever the dashboard's screensaver would be dismissed. Wave a hand in
    # front of the sensor. Note the HC-SR501 wants ~60s to settle after power-up.
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', type=int, default=23, help='BCM pin (default 23)')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    s = ProximitySensor(args.out)
    if not s.available:
        raise SystemExit('sensor unavailable - see the warning above')

    print(f'Reading HC-SR501 OUT on GPIO{args.out}. Wave to trigger; Ctrl-C to stop.')
    try:
        last = None
        while True:
            high = bool(s._out.value)
            fired = s._update(high)
            if high != last:
                print(f'{"MOTION" if high else "idle  "}'
                      f'{"   *** WAKE ***" if fired else ""}')
                last = high
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
