#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""HC-SR04 ultrasonic sensor -> "someone walked up" wake source.

The dashboard's screensaver is normally dismissed by a touch (see ScreenPower in
display.py). This module adds a second wake source: an HC-SR04 pointed out from
the panel, which fires when a person crosses into range.

Wiring (BCM numbering; the ECHO divider is not optional - see below):

    HC-SR04 VCC  -> 5V      (header pin 2 or 4)  the sensor will not fire at 3.3V
    HC-SR04 GND  -> GND     (header pin 6)
    HC-SR04 TRIG -> GPIO23  (header pin 16)
    HC-SR04 ECHO -> 1k in series, junction to GPIO24 (pin 18), 2k from junction
                    to GND. ECHO idles at 5V and the Pi's GPIOs are 3.3V-only,
                    so a bare connection eventually kills the pin. The 1k/2k
                    divider lands at 5 * 2/(1+2) = 3.33V.

Design notes:

* Fires on the *transition* into range, not on being in range. A wall, a chair,
  or a monitor parked 40cm away therefore never fires, and - just as important -
  never holds the screensaver off forever. A person approaching fires once.
* Hysteresis (PROXIMITY_HYSTERESIS_CM) keeps a target hovering at exactly the
  threshold from rattling the wake.
* The very first measurement only primes the state; it never fires. Otherwise
  every restart would wake the screen off whatever is already in front.
* Degrades safely: if gpiozero or the GPIO chip is missing (e.g. running
  `main.py --preview` on a laptop), `available` is False and nothing else in the
  dashboard changes.
"""
import time
import logging
import threading

# Round-trip: sound covers 2x the distance, hence the /2 at the call site.
_SPEED_OF_SOUND_CM_S = 34300.0

# The echo line should rise within a fraction of a ms of the trigger. If it
# never does, the sensor is unplugged or wired wrong.
_ECHO_START_TIMEOUT_S = 0.05
# An HC-SR04 tops out around 4m -> 8m of travel -> ~23ms of echo. Anything
# longer is a stuck line, not a distance.
_ECHO_PULSE_TIMEOUT_S = 0.04

# Datasheet asks for >=60ms between pings so the previous burst has decayed.
_PING_GAP_S = 0.06
# Median of this many pings per measurement, to drop the odd wild reading.
_SAMPLES = 3

# Readings outside this window are physically impossible for an HC-SR04.
_MIN_VALID_CM = 2.0
_MAX_VALID_CM = 400.0

# A dead sensor (never raises ECHO) costs a full _ECHO_START_TIMEOUT_S busy-wait
# per ping, which at the normal poll rate is a third of a core spent on nothing.
# After this many measurements in a row come back empty, poll far more slowly.
_FAILURES_BEFORE_BACKOFF = 10
_BACKOFF_INTERVAL_S = 10.0


class ProximitySensor:
    """Polls an HC-SR04 in a background thread and calls on_detect() on approach."""

    def __init__(self, trigger_pin, echo_pin, threshold_cm=100.0,
                 hysteresis_cm=20.0, poll_interval_s=0.5, refire_gap_s=2.0,
                 confirm_samples=3, trigger_mode='edge', on_detect=None):
        self.threshold_cm = float(threshold_cm)
        self.hysteresis_cm = float(hysteresis_cm)
        self.release_cm = self.threshold_cm + self.hysteresis_cm
        self.poll_interval_s = poll_interval_s
        self.refire_gap_s = refire_gap_s
        self.on_detect = on_detect
        # 'edge'  - fire once when the target crosses from far to near (needs a
        #           clean far->near transition; ignores a target that's already
        #           there). Best when the sensor has a clear approach path.
        # 'level' - fire on any single reading within threshold, rate-limited by
        #           refire_gap_s. Catches someone who stops in front and reads as
        #           a stationary object, at the cost of firing repeatedly while
        #           they stay (harmless - it just keeps the screen awake).
        self.trigger_mode = trigger_mode
        # A state change must be seen this many measurements in a row before it
        # commits. The HC-SR04 drops the odd burst - if your hand is angled the
        # echo scatters and the sensor reports whatever fixed object is behind
        # it instead. A single such reading would otherwise flip near->far and
        # re-arm a wake on the very next good reading (seen in the field as a
        # burst of wakes while a hand sat still), so we make a flip earn it.
        self.confirm_samples = max(1, int(confirm_samples))

        self._near = None      # None until the first confirmed measurement
        self._candidate = None # a proposed flip, not yet confirmed
        self._streak = 0       # measurements in a row supporting _candidate
        self._last_fire = 0.0
        # Most recent measurement, for a live on-screen readout. last_cm is None
        # when the last ping got no echo (out of range / nothing in the cone).
        self.last_cm = None
        self.last_reading_t = 0.0
        self._stop = threading.Event()
        self._trig = self._echo = None

        try:
            from gpiozero import DigitalInputDevice, DigitalOutputDevice
            self._trig = DigitalOutputDevice(trigger_pin, initial_value=False)
            self._echo = DigitalInputDevice(echo_pin)
            self.available = True
        except Exception as e:
            self.available = False
            logging.warning(
                "Proximity sensor disabled (trigger=%s echo=%s): %s; "
                "the screensaver still wakes on touch", trigger_pin, echo_pin, e)
            return

        logging.info("Proximity sensor ready: trigger=GPIO%s echo=GPIO%s wake<%.0fcm",
                     trigger_pin, echo_pin, self.threshold_cm)

    def set_threshold(self, threshold_cm):
        """Change the wake distance while running (from the settings menu).

        The release distance follows so the hysteresis gap is preserved, and the
        near/far state is reset to unknown so the new threshold decides the next
        transition from scratch rather than inheriting a stale near/far.
        """
        self.threshold_cm = float(threshold_cm)
        self.release_cm = self.threshold_cm + self.hysteresis_cm
        self._near = None
        self._candidate = None
        self._streak = 0
        logging.info("Proximity wake distance set to <%.0fcm", self.threshold_cm)

    def start(self):
        if self.available:
            threading.Thread(target=self._loop, daemon=True).start()

    def close(self):
        self._stop.set()
        for dev in (self._trig, self._echo):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass

    def _ping(self):
        """One trigger/echo round trip. Returns cm, or None if it timed out."""
        self._trig.on()
        time.sleep(10e-6)   # datasheet wants >=10us; Linux will oversleep, fine
        self._trig.off()

        # Busy-wait rather than gpiozero's edge callbacks: a ~6ms spin at 2Hz is
        # cheap, and callback dispatch latency would show up directly as cm.
        deadline = time.monotonic() + _ECHO_START_TIMEOUT_S
        while not self._echo.value:
            if time.monotonic() > deadline:
                return None
        t0 = time.monotonic()
        while self._echo.value:
            if time.monotonic() - t0 > _ECHO_PULSE_TIMEOUT_S:
                return None
        cm = (time.monotonic() - t0) * _SPEED_OF_SOUND_CM_S / 2
        return cm if _MIN_VALID_CM <= cm <= _MAX_VALID_CM else None

    def _measure(self):
        """Median of _SAMPLES pings. Returns cm, or None if too few landed."""
        reads = []
        for i in range(_SAMPLES):
            if i:
                time.sleep(_PING_GAP_S)
            cm = self._ping()
            if cm is not None:
                reads.append(cm)
        if len(reads) < 2:
            return None
        reads.sort()
        return reads[len(reads) // 2]

    def _fire(self, cm):
        """Fire on_detect if the refire gap has elapsed. Returns True if it did."""
        now = time.monotonic()
        if now - self._last_fire < self.refire_gap_s:
            return False
        self._last_fire = now
        logging.info("Proximity: object at %.0fcm, waking screen", cm)
        if self.on_detect:
            self.on_detect()
        return True

    def _update(self, cm):
        """Fold one distance into the near/far state. Returns True if it fired."""
        if self.trigger_mode == 'level':
            # Any reading within threshold wakes, rate-limited by refire_gap_s.
            self._near = cm <= self.threshold_cm
            return self._fire(cm) if self._near else False

        # --- 'edge' mode: fire only on a confirmed far->near transition ---
        # Between threshold and release the reading is ambiguous; keep whatever
        # the committed state is so a target hovering on the line doesn't rattle.
        if cm <= self.threshold_cm:
            reading = True
        elif cm >= self.release_cm:
            reading = False
        else:
            reading = self._near

        # Debounce: a reading that disagrees with the committed state has to
        # repeat confirm_samples times before it commits. A lone dropout resets
        # the streak and is forgotten.
        if reading == self._near:
            self._candidate = None
            self._streak = 0
            return False

        if reading == self._candidate:
            self._streak += 1
        else:
            self._candidate = reading
            self._streak = 1
        if self._streak < self.confirm_samples:
            return False

        was, self._near = self._near, reading
        self._candidate = None
        self._streak = 0

        # `was is False` (not `not was`) so the first confirmed state, flipping
        # from the initial None, can never fire.
        if was is False and reading:
            return self._fire(cm)
        return False

    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            try:
                cm = self._measure()
            except Exception as e:
                logging.warning("Proximity read error: %s; retrying in 2s", e)
                self._stop.wait(2)
                continue

            self.last_cm = cm                       # for the on-screen readout
            self.last_reading_t = time.monotonic()

            if cm is None:
                failures += 1
                if failures == _FAILURES_BEFORE_BACKOFF:
                    logging.warning(
                        "Proximity sensor not responding; polling every %.0fs "
                        "until it comes back", _BACKOFF_INTERVAL_S)
            else:
                if failures >= _FAILURES_BEFORE_BACKOFF:
                    logging.info("Proximity sensor responding again")
                failures = 0
                self._update(cm)

            self._stop.wait(_BACKOFF_INTERVAL_S
                            if failures >= _FAILURES_BEFORE_BACKOFF
                            else self.poll_interval_s)


if __name__ == '__main__':
    # Wiring check: `python3 proximity.py` prints a live distance readout and
    # says WAKE whenever the dashboard's screensaver would be dismissed.
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trigger', type=int, default=23, help='BCM pin (default 23)')
    ap.add_argument('--echo', type=int, default=24, help='BCM pin (default 24)')
    ap.add_argument('--cm', type=float, default=20.0, help='wake threshold')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    s = ProximitySensor(args.trigger, args.echo, threshold_cm=args.cm)
    if not s.available:
        raise SystemExit('sensor unavailable - see the warning above')

    # Drive the poll from here rather than start()ing the thread: two threads
    # triggering one sensor would interleave their bursts and read noise.
    try:
        while True:
            cm = s._measure()
            if cm is None:
                print('no echo (out of range, or check the wiring)')
            else:
                fired = s._update(cm)
                print(f'{cm:6.1f} cm   {"near" if s._near else "far "}'
                      f'{"   *** WAKE ***" if fired else ""}')
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
