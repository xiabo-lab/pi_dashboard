#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Bluetooth AVRCP "now playing" for the dashboard music widget.

A phone pairs with the Pi (whose Bluetooth adapter is named "Pi_Dashboard") and
this reads the currently playing track over BlueZ MediaPlayer1 (AVRCP): title,
artist, play/pause, position and duration. No audio backend is needed - the
metadata rides the AVRCP control channel, which we connect explicitly (a plain
Device1.Connect on a dual-mode phone can bring up only BLE and expose no
MediaPlayer1). Adapted from the sibling `carlyrics` project.

Runs its own asyncio loop in a background thread. Thread-safe surface:
  * music_snapshot() -> dict, read by the render thread each frame.
  * start_pairing() / stop_pairing() / forget(path) / refresh() - scheduled
    onto the BT loop from the main thread via run_coroutine_threadsafe.
  * paired / pairing / pair_status - simple fields read by the settings UI.
"""
import asyncio
import glob
import logging
import os
import threading
import time

try:
    from dbus_next.aio import MessageBus
    from dbus_next import BusType, Variant
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

BLUEZ = "org.bluez"
MP_IFACE = "org.bluez.MediaPlayer1"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
# "A/V Remote Control" - connected explicitly to force the AVRCP control channel
# BlueZ surfaces as MediaPlayer1.
AVRCP_UUID = "0000110e-0000-1000-8000-00805f9b34fb"


def _unwrap(d):
    return {k: (v.value if hasattr(v, "value") else v) for k, v in (d or {}).items()}


class BtMusic:
    AUTOCONNECT_INTERVAL_S = 4
    POSITION_POLL_S = 2.0

    def __init__(self, adapter_name="Pi_Dashboard"):
        self.adapter_name = adapter_name
        self.available = DBUS_AVAILABLE
        self.loop = None
        self.bus = None
        self._om = None
        self._adapter_path = None
        self._adapter_props = None
        self._player_path = None
        self._player_props = None
        self._player_iface = None
        self._last_polled = None

        self._lock = threading.Lock()
        self._m = {'connected': False, 'status': 'stopped', 'title': '', 'artist': '',
                   'position_ms': 0, 'duration_ms': 0, 'at': time.monotonic()}

        # Admin state (read by the settings UI; written on the BT loop thread).
        self.paired = []          # [(path, name, connected)]
        self.pairing = False
        self.pair_status = ''
        self._baseline = set()
        self._pair_task = None

    # ---------------- lifecycle ----------------
    def start(self):
        if not self.available:
            logging.warning("dbus-next not installed; Bluetooth music disabled")
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            logging.error(f"Bluetooth loop crashed: {e}")

    async def _main(self):
        self._unblock_rfkill()
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as e:
            logging.error(f"Bluetooth D-Bus connect failed: {e}")
            return
        intro = await self.bus.introspect(BLUEZ, "/")
        root = self.bus.get_proxy_object(BLUEZ, "/", intro)
        self._om = root.get_interface(OM_IFACE)
        await self._find_adapter()
        await self._setup_adapter()

        self._om.on_interfaces_added(self._on_added)
        self._om.on_interfaces_removed(self._on_removed)
        await self._attach_existing_player()
        await self.refresh_paired()

        asyncio.create_task(self._auto_connect_loop())
        asyncio.create_task(self._position_poller())
        logging.info("Bluetooth music ready (adapter '%s')", self.adapter_name)
        while True:
            await asyncio.sleep(3600)

    @staticmethod
    def _unblock_rfkill():
        """Clear a soft rfkill block on the BT radio (survives reboots)."""
        for r in glob.glob('/sys/class/rfkill/rfkill*'):
            try:
                with open(os.path.join(r, 'type')) as f:
                    if f.read().strip() != 'bluetooth':
                        continue
                with open(os.path.join(r, 'soft'), 'w') as f:
                    f.write('0')
            except Exception:
                pass

    async def _find_adapter(self):
        objs = await self._om.call_get_managed_objects()
        for path, ifaces in objs.items():
            if ADAPTER_IFACE in ifaces:
                self._adapter_path = path
                intro = await self.bus.introspect(BLUEZ, path)
                obj = self.bus.get_proxy_object(BLUEZ, path, intro)
                self._adapter_props = obj.get_interface(PROPS_IFACE)
                return

    async def _adapter_set(self, prop, variant):
        if self._adapter_props:
            await self._adapter_props.call_set(ADAPTER_IFACE, prop, variant)

    async def _setup_adapter(self):
        try:
            await self._adapter_set("Powered", Variant("b", True))
            await self._adapter_set("Alias", Variant("s", self.adapter_name))
        except Exception as e:
            logging.error(f"BT adapter setup: {e}")

    # ---------------- player attach / metadata ----------------
    async def _attach_existing_player(self):
        objs = await self._om.call_get_managed_objects()
        for path, ifaces in objs.items():
            if MP_IFACE in ifaces:
                await self._attach(path, _unwrap(ifaces[MP_IFACE]))
                return

    def _on_added(self, path, ifaces):
        if MP_IFACE in ifaces and self._player_path is None:
            asyncio.create_task(self._attach(path, _unwrap(ifaces[MP_IFACE])))

    def _on_removed(self, path, ifaces):
        if path == self._player_path and MP_IFACE in ifaces:
            self._player_path = None
            self._player_props = None
            self._player_iface = None
            self._last_polled = None
            with self._lock:
                self._m.update({'connected': False, 'status': 'stopped',
                                'title': '', 'artist': '', 'position_ms': 0,
                                'duration_ms': 0, 'at': time.monotonic()})

    async def _attach(self, path, initial):
        logging.info("Bluetooth player appeared: %s", path)
        self._player_path = path
        intro = await self.bus.introspect(BLUEZ, path)
        obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        self._player_props = obj.get_interface(PROPS_IFACE)
        self._player_iface = obj.get_interface(MP_IFACE)  # Play/Pause/Next/Previous
        with self._lock:
            self._m['connected'] = True
        self._handle(initial)

        def on_changed(iface, changed, invalidated):
            if iface == MP_IFACE:
                self._handle(_unwrap(changed))
        self._player_props.on_properties_changed(on_changed)

    def _handle(self, changed):
        with self._lock:
            if "Status" in changed:
                self._m['status'] = str(changed["Status"]).lower()
            if "Track" in changed:
                track = changed["Track"] or {}
                track = _unwrap(track) if hasattr(track, 'items') else track
                self._m['title'] = (track.get("Title") or "").strip()
                self._m['artist'] = (track.get("Artist") or "").strip()
                dur = track.get("Duration")
                self._m['duration_ms'] = int(dur) if dur else 0
                # New track: reset the position anchor.
                self._m['position_ms'] = 0
                self._m['at'] = time.monotonic()
                self._last_polled = None
            if "Position" in changed:
                self._set_position(int(changed["Position"]))

    def _set_position(self, pos_ms):
        # caller holds the lock (or is single-threaded on the BT loop)
        self._m['position_ms'] = pos_ms
        self._m['at'] = time.monotonic()

    async def _position_poller(self):
        """Re-anchor position only when BlueZ reports a *changed* value, so a
        stale cached Position can't freeze or jerk the progress bar."""
        while True:
            await asyncio.sleep(self.POSITION_POLL_S)
            if not self._player_props:
                continue
            with self._lock:
                playing = self._m['status'] == 'playing'
            if not playing:
                continue
            try:
                var = await self._player_props.call_get(MP_IFACE, "Position")
                pos = int(var.value)
            except Exception:
                continue
            if pos != self._last_polled:
                self._last_polled = pos
                with self._lock:
                    self._set_position(pos)

    async def _auto_connect_loop(self):
        """Keep a player attached by polling, not just relying on the
        InterfacesAdded/Removed signals (which don't fire reliably from a
        background-thread event loop). Also brings up AVRCP to a paired phone
        so the user needn't touch their phone's Bluetooth settings again."""
        await asyncio.sleep(3)
        while True:
            if self._om is not None:
                try:
                    objs = await self._om.call_get_managed_objects()
                except Exception:
                    objs = {}

                # Detach if the player we were showing has vanished.
                if self._player_path and self._player_path not in objs:
                    self._on_removed(self._player_path, {MP_IFACE: {}})

                # Attach any live player if we have none.
                if self._player_path is None:
                    for path, ifaces in objs.items():
                        if MP_IFACE in ifaces:
                            await self._attach(path, _unwrap(ifaces[MP_IFACE]))
                            break

                # Still nothing playing: force AVRCP up on a paired phone.
                if self._player_path is None:
                    for path, ifaces in objs.items():
                        dev = ifaces.get(DEVICE_IFACE)
                        if not dev or not _unwrap(dev).get("Paired"):
                            continue
                        try:
                            intro = await self.bus.introspect(BLUEZ, path)
                            obj = self.bus.get_proxy_object(BLUEZ, path, intro)
                            await obj.get_interface(DEVICE_IFACE).call_connect_profile(AVRCP_UUID)
                            break
                        except Exception:
                            continue
            await asyncio.sleep(self.AUTOCONNECT_INTERVAL_S)

    # ---------------- thread-safe read surface ----------------
    def music_snapshot(self):
        with self._lock:
            m = dict(self._m)
        if m['status'] == 'playing':
            m['now_ms'] = m['position_ms'] + int((time.monotonic() - m['at']) * 1000)
        else:
            m['now_ms'] = m['position_ms']
        if m['duration_ms']:
            m['now_ms'] = max(0, min(m['now_ms'], m['duration_ms']))
        return m

    # ---------------- playback control (called from main thread) ----------------
    def play_pause(self):
        self._schedule(self._play_pause())

    def next(self):
        self._schedule(self._transport('call_next'))

    def previous(self):
        self._schedule(self._transport('call_previous'))

    async def _play_pause(self):
        if not self._player_iface:
            return
        with self._lock:
            playing = self._m['status'] == 'playing'
        try:
            if playing:
                await self._player_iface.call_pause()
            else:
                await self._player_iface.call_play()
        except Exception as e:
            logging.error(f"BT play/pause: {e}")

    async def _transport(self, method):
        if not self._player_iface:
            return
        try:
            await getattr(self._player_iface, method)()
        except Exception as e:
            logging.error(f"BT {method}: {e}")

    # ---------------- admin (called from main thread) ----------------
    def _schedule(self, coro):
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def refresh(self):
        self._schedule(self.refresh_paired())

    def start_pairing(self):
        self._schedule(self._start_pairing())

    def stop_pairing(self):
        self._schedule(self._stop_pairing())

    def forget(self, path):
        self._schedule(self._forget(path))

    async def refresh_paired(self):
        if self._om is None:
            return
        try:
            objs = await self._om.call_get_managed_objects()
        except Exception:
            return
        out = []
        for path, ifaces in objs.items():
            dev = ifaces.get(DEVICE_IFACE)
            if not dev:
                continue
            p = _unwrap(dev)
            if not p.get("Paired"):
                continue
            name = p.get("Alias") or p.get("Name") or path.rsplit("/", 1)[-1]
            out.append((path, name, bool(p.get("Connected"))))
        out.sort(key=lambda t: t[1].lower())
        self.paired = out

    async def _start_pairing(self):
        if self._adapter_props is None:
            self.pair_status = "No Bluetooth adapter"
            return
        await self.refresh_paired()
        self._baseline = {p for p, _n, _c in self.paired}
        try:
            await self._adapter_set("Pairable", Variant("b", True))
            await self._adapter_set("DiscoverableTimeout", Variant("u", 0))
            await self._adapter_set("Discoverable", Variant("b", True))
        except Exception as e:
            self.pair_status = "Could not enter pairing mode"
            logging.error(f"BT pairing: {e}")
            return
        self.pairing = True
        self.pair_status = f'On your phone, pick "{self.adapter_name}"'
        if self._pair_task is None or self._pair_task.done():
            self._pair_task = asyncio.create_task(self._await_new_device())

    async def _stop_pairing(self):
        self.pairing = False
        if self._pair_task and not self._pair_task.done():
            self._pair_task.cancel()
        try:
            await self._adapter_set("Discoverable", Variant("b", False))
        except Exception:
            pass

    async def _await_new_device(self):
        try:
            while self.pairing:
                await asyncio.sleep(2.0)
                await self.refresh_paired()
                new = {p for p, _n, _c in self.paired} - self._baseline
                if new:
                    await self._adopt(sorted(new)[0])
                    return
        except asyncio.CancelledError:
            raise

    async def _adopt(self, path):
        name = next((n for p, n, _c in self.paired if p == path), path)
        logging.info("Bluetooth phone paired: %s", name)
        try:
            intro = await self.bus.introspect(BLUEZ, path)
            obj = self.bus.get_proxy_object(BLUEZ, path, intro)
            await obj.get_interface(PROPS_IFACE).call_set(
                DEVICE_IFACE, "Trusted", Variant("b", True))
            try:
                await obj.get_interface(DEVICE_IFACE).call_connect_profile(AVRCP_UUID)
            except Exception:
                pass  # phone may not expose AVRCP until it plays; autoconnect retries
        except Exception as e:
            logging.error(f"BT adopt: {e}")
        self.pair_status = f"Paired: {name}"
        await self._stop_pairing()
        await self.refresh_paired()

    async def _forget(self, path):
        if not self._adapter_path:
            return
        try:
            intro = await self.bus.introspect(BLUEZ, self._adapter_path)
            obj = self.bus.get_proxy_object(BLUEZ, self._adapter_path, intro)
            await obj.get_interface(ADAPTER_IFACE).call_remove_device(path)
        except Exception as e:
            logging.error(f"BT forget: {e}")
        await self.refresh_paired()
