#!/usr/bin/python3
# -*- coding:utf-8 -*-
import sys
import os
import time
import logging
import threading
import requests
import io
import gc
import socket
import json
import asyncio
import pickle
import argparse
import subprocess
import math
import random
import calendar
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps
from logging.handlers import RotatingFileHandler

# --- GMAIL IMPORTS ---
# Optional, like the bambu/roborock imports below: the Gmail widget degrades to
# "Unread Inbox: 0" if the google libraries are absent. A hard import here would
# crash-loop the systemd service on a Pi that never had them installed.
try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

# --- SYSTEM LIMITS (POSIX only; skipped when previewing on a dev box) ---
try:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
except Exception as e:
    print(f"Failed to set rlimit: {e}")

# --- VERSION ---
APP_VERSION = '1.2.0'  # 1.2: Bluetooth music widget + Bluetooth settings

# --- PATHS ---
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
LIB_DIR = os.path.join(BASE_DIR, 'lib')
FONT_DIR = os.path.join(BASE_DIR, 'fnt')
ICON_DIR = os.path.join(BASE_DIR, 'icons')
LOG_FILE = os.path.join(BASE_DIR, 'dashboard.log')
# Runtime settings editable from the on-screen menu (currently just the ZIP),
# kept separate from the code defaults so an on-screen change survives restarts.
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')

# ######################
# --- WIDGET TOGGLES ---
# ######################
ENABLE_STRAVA = False
ENABLE_BAMBU = True
ENABLE_ROBOROCK = False
ENABLE_ANTIGRAVITY = False
ENABLE_CLAUDE = True
ENABLE_SPOTIFY = False

# ######################
# --- DISPLAY CONFIG ---
# ######################
# GeeekPi 11.26" HDMI LCD, 1920x440, capacitive touch.
SCREEN_W = 1920
SCREEN_H = 440

# The LCD redraws instantly, so we can re-render as soon as anything changes
# instead of the e-paper's 60s partial-refresh cadence. The loop spins at
# EVENT_POLL_FPS to keep touch responsive, but a frame is only *rendered* when
# the clock minute, the data revision, or the theme actually moves.
EVENT_POLL_FPS = 30

# Minimum seconds between touch-triggered data refetches.
TAP_REFRESH_COOLDOWN = 15

# After this many seconds with no touch, show a moving-clock screensaver (a
# time/day/date block drifting on black) instead of the dashboard; a touch
# returns to the dashboard. This panel can't be powered off in software - it has
# no backlight control and no CEC, and cutting the HDMI signal just makes it show
# a "No Signal" OSD - so we keep the signal on and drift the clock to avoid
# burn-in. The dashboard keeps running underneath. Set 0 to disable. This is the
# code default; the Firmware settings screen can override it (persisted to
# settings.json, applied live) within the minute bounds below.
SCREENSAVER_SECONDS = 600    # 10 minutes
SCREENSAVER_FRAME_S = 0.1    # screensaver redraw interval (~10 fps drift)
SCREENSAVER_MIN_MINUTES = 1
SCREENSAVER_MAX_MINUTES = 60
SCREENSAVER_STEP_MINUTES = 1

# Automatic daily restart. This panel's Pi has been seen to drop its Wi-Fi after
# many days of uptime; a scheduled reboot at a quiet hour clears it before anyone
# notices. On by default at 06:00. Both the on/off and the hour are overridable
# on the Firmware settings screen (persisted to settings.json, applied live).
AUTO_RESTART_ENABLED = True
AUTO_RESTART_HOUR = 6          # 24h clock; the reboot fires at HH:00
AUTO_RESTART_MIN_HOUR = 0
AUTO_RESTART_MAX_HOUR = 23

# Data-thread watchdog. update_data_thread fetches every widget sequentially in
# one thread, and some calls (Bambu MQTT, Gmail httplib2) have no hard timeout -
# after a Wi-Fi flap a socket can be left half-open and block forever, freezing
# *all* widgets at a fixed time while the clock keeps ticking. The thread bumps a
# heartbeat once per loop; if that goes stale past this many seconds the process
# is re-exec'd to recover, instead of sitting frozen for hours. Must comfortably
# exceed the slowest healthy iteration (claude.py subprocess is capped at 30s).
DATA_WATCHDOG_TIMEOUT_S = 180

# After waking, ignore tap/hold *actions* for this long so the touch that woke
# the screen doesn't also refresh data or flip the theme.
WAKE_GUARD_SECONDS = 1.5

# Optional HC-SR04 ultrasonic sensor: a second wake source alongside touch, so
# walking up to the panel dismisses the screensaver without reaching for it. It
# fires on *entering* range, so a wall or a chair parked inside the threshold
# neither wakes the screen nor holds the screensaver off. Pins are BCM numbers;
# see proximity.py for the wiring, including the mandatory ECHO voltage divider.
# With the sensor unplugged (or gpiozero absent) the dashboard is unchanged.
PROXIMITY_ENABLED = True
PROXIMITY_TRIGGER_PIN = 23   # header pin 16
PROXIMITY_ECHO_PIN = 24      # header pin 18, via a 1k/2k divider
# 20cm, not 100: with nobody in front, this sensor's noise floor scatters around
# 30-50cm, so a 100cm threshold woke on clutter. A real approach reads <15cm,
# well clear of the noise, so 20cm fires only on a genuine close approach. This
# is the code default; the Firmware settings screen can override it (persisted
# to settings.json, applied live) within the bounds below, in 5cm steps.
PROXIMITY_WAKE_CM = 20
PROXIMITY_WAKE_MIN_CM = 5
PROXIMITY_WAKE_MAX_CM = 200
PROXIMITY_WAKE_STEP_CM = 5
# How the sensor decides to wake:
#   'level' - wake on ANY reading within the distance, even a stationary target
#             that walks up and stops. More sensitive; may re-wake while present.
#   'edge'  - wake only on a clean far->near approach (ignores a target already
#             in range). Needs a clear approach path to the sensor.
# 'level' suits a sensor glued to the panel where people stop right in front.
PROXIMITY_TRIGGER_MODE = 'level'
# Seconds between measurements. Also the worst-case wake latency in level mode.
# 3s (not 1s): each ping busy-waits on the echo line holding the GIL, so polling
# less often frees CPU for the render/data threads at the cost of ~2s more wake
# latency - fine for a screensaver dismiss.
PROXIMITY_POLL_INTERVAL_S = 3.0

# --- API ENDPOINTS ---
API_ENDPOINTS = {
    'weather': 'https://api.open-meteo.com/v1/forecast',
    'aqi': 'https://air-quality-api.open-meteo.com/v1/air-quality',
    'geo_city': 'https://geocoding-api.open-meteo.com/v1/search',
    'geo_zip': 'https://api.zippopotam.us',  # /<country>/<zip> -> lat/lon + place
    'geo_ip': 'http://ip-api.com/json/?fields=status,lat,lon,city,regionName,countryCode',
    'strava_token': 'https://www.strava.com/oauth/token',
    'strava_auth': 'https://www.strava.com/oauth/authorize',
    'strava_activities': 'https://www.strava.com/api/v3/athlete/activities',
    'yahoo_chart': 'https://query1.finance.yahoo.com/v8/finance/chart/',
    'lastfm': 'http://ws.audioscrobbler.com/2.0/'
}

# --- CONFIGURATION ---
# Weather location, resolved in this priority order (first one that succeeds
# wins; re-checked about once a day):
#
#   1. LOCATION_ZIP    - a postal code, resolved to coordinates via zippopotam.us.
#                        Most precise for a fixed spot. Open-Meteo's own geocoder
#                        does NOT understand ZIP codes, hence the separate lookup.
#   2. LOCATION_CITY   - a place name geocoded via Open-Meteo. Add a region hint
#                        after a comma to disambiguate, e.g. 'Santa Clara, CA'.
#   3. USE_IP_LOCATION - public-IP geolocation (ip-api.com). Follows the Pi to a
#                        new network automatically, but a VPN or unusual ISP
#                        routing can place you in the wrong city.
#   4. LOCATION_LAT/LON - hardcoded fallback, used only if all the above are off
#                        or fail (no internet yet, API down).
#
# Ships unpinned: no ZIP, so the weather follows the Pi via IP geolocation. To
# pin it to a fixed spot, either set LOCATION_ZIP here or — better, since it
# stays out of the repo — set it on-screen (hold 5s -> Settings -> Zip Code),
# which saves to the gitignored settings.json and overrides this default.
LOCATION_ZIP = ''
LOCATION_ZIP_COUNTRY = 'us'
LOCATION_CITY = ''
USE_IP_LOCATION = True
LOCATION_LAT = 51.4779   # Greenwich - placeholder, used only if everything else fails
LOCATION_LON = -0.0015
LOCATION_LABEL = 'Greenwich'

# Per-device credentials (printer serial / LAN access code, Roborock account)
# are read from device_conf.json, which is gitignored so they never reach the
# repo. Copy device_conf.example.json to device_conf.json and fill it in; the
# defaults below leave the relevant widget disabled if the file is absent.
DEVICE_CONF_FILE = os.path.join(BASE_DIR, 'device_conf.json')

PRINTER_CONF = {
    'IP': '',
    'SERIAL': '',
    'ACCESS_CODE': ''
}

ROBOROCK_CONF = {
    'EMAIL': ''
}

try:
    with open(DEVICE_CONF_FILE) as _f:
        _device_conf = json.load(_f)
    PRINTER_CONF.update(_device_conf.get('printer', {}))
    ROBOROCK_CONF.update(_device_conf.get('roborock', {}))
except FileNotFoundError:
    pass
except (OSError, ValueError) as _e:
    print(f"Warning: could not read {DEVICE_CONF_FILE}: {_e}", file=sys.stderr)

LASTFM_CONF = {
    'API_KEY': '',
    'USERNAME': ''
}

STRAVA_CONF = {
    'TOKEN_FILE': os.path.join(BASE_DIR, 'strava_token.json')
}

# --- FILES & SCOPES ---
GMAIL_TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')
ROBOROCK_TOKEN_FILE = os.path.join(BASE_DIR, 'roborock_session.pkl')
ROBOROCK_STATS_FILE = os.path.join(BASE_DIR, 'roborock_stats.json')
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

if os.path.exists(LIB_DIR):
    sys.path.append(LIB_DIR)

import display as display_backend
import wifi_setup
import settings as settings_ui
import bluetooth_music
import proximity

try:
    import bambulabs_api as bl
    from roborock.web_api import RoborockApiClient
    from roborock.devices.device_manager import create_device_manager, UserParams
except ImportError:
    pass

# --- LOGGING ---
logging.getLogger("bambulabs_api").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("roborock").setLevel(logging.CRITICAL)
logging.getLogger("aiomqtt").setLevel(logging.CRITICAL)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=1)
file_handler.setFormatter(formatter)

logger.handlers.clear()
logger.addHandler(console_handler)
logger.addHandler(file_handler)

icon_cache = {}
global_printer = None
# Set when the settings menu rewrites PRINTER_CONF; update_data_thread owns the
# Printer object, so it does the disconnect/rebuild rather than the UI thread.
printer_reinit = threading.Event()
global_sensor = None   # proximity.ProximitySensor once main() creates it
bt = None  # bluetooth_music.BtMusic instance (created in main())
# Set by the data watchdog when update_data_thread wedges; the main loop sees it
# and re-execs from the main thread (a clean spot to tear down SDL/display).
data_restart_request = threading.Event()


# ##############
# --- THEME ---
# ##############
# The e-paper panel was 1-bit, so everything was black-on-white. The LCD is
# full colour: text/graph colours come from THEME so the palette can be swapped
# at runtime (long-press the screen).
DARK_THEME = {
    'bg': (11, 13, 17),
    'fg': (233, 237, 243),
    'muted': (146, 155, 170),
    'line': (44, 50, 61),
    'accent': (64, 196, 255),
    'warn': (255, 183, 77),
    'alert': (255, 95, 95),
    'ok': (94, 222, 142),
    'gold': (240, 200, 90),
}

LIGHT_THEME = {
    'bg': (250, 250, 248),
    'fg': (20, 22, 26),
    'muted': (110, 116, 126),
    'line': (208, 212, 219),
    'accent': (0, 122, 204),
    'warn': (198, 118, 0),
    'alert': (198, 40, 40),
    'ok': (22, 140, 72),
    'gold': (176, 132, 0),
}

THEME = dict(DARK_THEME)
THEME_NAME = 'dark'


def toggle_theme():
    global THEME_NAME
    THEME_NAME = 'light' if THEME_NAME == 'dark' else 'dark'
    THEME.clear()
    THEME.update(LIGHT_THEME if THEME_NAME == 'light' else DARK_THEME)
    logging.info(f"Theme -> {THEME_NAME}")


def pct_color(pct):
    if pct >= 90:
        return THEME['alert']
    if pct >= 75:
        return THEME['warn']
    return THEME['accent']


# --- ROBUST NETWORK MANAGER ---
class NetworkManager:
    def __init__(self):
        self.session = None
        self.create_session()

    def create_session(self):
        if self.session:
            try:
                self.session.close()
            except:
                pass
        gc.collect()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5, pool_maxsize=10,
            max_retries=requests.adapters.Retry(total=1, backoff_factor=0.5)
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def get_json(self, url, headers=None, data=None, method='GET', timeout=10):
        try:
            if self.session is None: self.create_session()
            if method == 'POST':
                resp = self.session.post(url, headers=headers, data=data, timeout=timeout)
            else:
                resp = self.session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self.create_session()
            return None

    def get_image(self, url, timeout=15):
        try:
            if self.session is None: self.create_session()
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            self.create_session()
            return None


net = NetworkManager()


# --- GLOBAL DATA STORE ---
class DataStore:
    def __init__(self):
        # RLock so bump() can be called from inside a `with data_store.lock` block.
        self.lock = threading.RLock()
        self.rev = 0
        self.updated_at = 0
        # Bumped at the top of every update_data_thread iteration; the watchdog
        # re-execs if it stops advancing (a wedged fetch froze the whole thread).
        self.heartbeat = time.monotonic()
        self.weather = {}
        self.aqi = 0
        # Resolved weather location. Seeded from the configured fallback and
        # overwritten once IP geolocation succeeds (if USE_IP_LOCATION).
        self.location = {'lat': LOCATION_LAT, 'lon': LOCATION_LON, 'label': LOCATION_LABEL}
        self.strava = {
            'rides': 0, 'total_distance': 0,
            'rides_curr': 0, 'distance_curr': 0,
            'rides_prev': 0, 'distance_prev': 0,
            'bike_total': 0, 'hike_total': 0
        }
        self.printer = {'status': 'OFFLINE'}
        self.gmail_unread = 0
        self.spotify = {'status': 'PAUSED', 'text': '', 'cover': None}
        self.claude = {'error': False, 'five_hour': {}, 'seven_day': {}}
        self.antigravity = {'error': False, 'models': []}
        self.roborock = {
            'status': 'OFFLINE', 'battery': 0, 'is_cleaning': False,
            'current_area': 0.0, 'ref_area': 0.0, 'pct': 0.0, 'last_date': '-'
        }
        self.sysload = {'cpu': 0, 'ram_free': 0, 'history': deque(maxlen=30)}
        # Markets shown in the finance widget: each value is {'price', 'pct'}.
        self.market = {'btc': {}, 'sp500': {}, 'gold': {}}
        self.ping = {'current': 0, 'history': deque(maxlen=50)}

        self.last_update = {
            'weather': 0, 'strava': 0, 'printer': 0, 'gmail': 0,
            'spotify': 0, 'market': 0, 'sysload': 0, 'ping': 0,
            'claude': 0, 'antigravity': 0, 'geo': 0
        }

    def bump(self):
        """Mark data as changed so the render loop knows to redraw."""
        with self.lock:
            self.rev += 1
            self.updated_at = time.time()

    def force_refresh(self):
        with self.lock:
            for key in self.last_update:
                self.last_update[key] = 0


data_store = DataStore()


# --- HELPERS ---
def ping_printer(ip):
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    except:
        return False


_local_ip_cache = {'ip': '-', 'ts': 0}


def get_local_ip():
    if time.time() - _local_ip_cache['ts'] < 300:
        return _local_ip_cache['ip']
    ip = '-'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
    except Exception:
        pass
    _local_ip_cache.update({'ip': ip, 'ts': time.time()})
    return ip


def geolocate_by_ip():
    """Resolve (lat, lon, label) from the public IP, or None on failure.

    Uses ip-api.com (free, no key, HTTP-only on the free tier, ~45 req/min).
    We query it at most a few times a day, so the rate limit is irrelevant.
    """
    data = net.get_json(API_ENDPOINTS['geo_ip'], timeout=8)
    if not data or data.get('status') != 'success':
        return None
    try:
        lat = float(data['lat'])
        lon = float(data['lon'])
    except (KeyError, TypeError, ValueError):
        return None
    parts = [p for p in (data.get('city'), data.get('regionName')) if p]
    label = ', '.join(parts) or data.get('countryCode') or 'Unknown'
    return lat, lon, label


# US state abbreviations, so a hint like "CA" in LOCATION_CITY disambiguates to
# California rather than matching some other place's substring.
_US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
}


def _pick_geocode(results, hint):
    """Choose the geocoding result best matching the region/country hint."""
    if not hint:
        return results[0]
    h = hint.lower()
    state = _US_STATES.get(hint.upper(), '').lower()

    def score(r):
        admin1 = (r.get('admin1') or '').lower()
        cc = (r.get('country_code') or '').lower()
        country = (r.get('country') or '').lower()
        s = 0
        if state and admin1 == state:
            s += 5
        if h == admin1:
            s += 4
        if h == cc:
            s += 3
        if h == country:
            s += 3
        return s

    # Open-Meteo returns results ranked by population/relevance; max() keeps the
    # first (highest-ranked) on a score tie.
    return max(results, key=score)


def geocode_city(query):
    """Resolve a place name like 'Santa Clara, CA' to (lat, lon, label).

    Uses Open-Meteo's geocoding API (free, no key). The part before the first
    comma is the city; anything after is a region/country hint used to pick the
    right match when the name is ambiguous. Returns None on failure.
    """
    query = (query or '').strip()
    if not query:
        return None
    name, _, hint = query.partition(',')
    name, hint = name.strip(), hint.strip()

    url = (f"{API_ENDPOINTS['geo_city']}?name={urllib.parse.quote(name)}"
           f"&count=10&language=en&format=json")
    data = net.get_json(url, timeout=8)
    results = (data or {}).get('results') or []
    if not results:
        return None

    best = _pick_geocode(results, hint)
    try:
        lat = float(best['latitude'])
        lon = float(best['longitude'])
    except (KeyError, TypeError, ValueError):
        return None
    label = ', '.join(p for p in (best.get('name'), best.get('admin1')) if p) or name
    return lat, lon, label


def geocode_zip(zipcode, country='us'):
    """Resolve a postal code to (lat, lon, label) via zippopotam.us (free, no key).

    Open-Meteo's geocoder ignores ZIP codes, so this is the only way to pin a
    specific postal area rather than a whole city. Returns None on failure.
    """
    zipcode = (zipcode or '').strip()
    if not zipcode:
        return None
    url = f"{API_ENDPOINTS['geo_zip']}/{country}/{urllib.parse.quote(zipcode)}"
    data = net.get_json(url, timeout=8)
    places = (data or {}).get('places') or []
    if not places:
        return None
    p = places[0]
    try:
        lat = float(p['latitude'])
        lon = float(p['longitude'])
    except (KeyError, TypeError, ValueError):
        return None
    label = p.get('place name') or zipcode
    return lat, lon, label


# --- RUNTIME SETTINGS (on-screen editable, persisted to settings.json) ---
def _save_settings(patch):
    """Merge `patch` into settings.json, preserving the other keys. -> bool.

    Read-modify-write rather than a bare dump so saving one setting (e.g. the
    ZIP) never wipes another (e.g. the sensor distance)."""
    conf = {}
    try:
        with open(SETTINGS_FILE) as f:
            conf = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Failed to read settings.json: {e}")
    if not isinstance(conf, dict):
        conf = {}
    conf.update(patch)
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(conf, f)
        return True
    except Exception as e:
        logging.error(f"Failed to write settings.json: {e}")
        return False


def load_runtime_settings():
    """Apply any settings saved from the on-screen menu over the code defaults."""
    global LOCATION_ZIP, PROXIMITY_WAKE_CM, SCREENSAVER_SECONDS
    global AUTO_RESTART_ENABLED, AUTO_RESTART_HOUR
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        z = s.get('zip')
        if isinstance(z, str) and z.strip():
            LOCATION_ZIP = z.strip()
            logging.info(f"Loaded ZIP from settings: {LOCATION_ZIP}")
        cm = s.get('sensor_wake_cm')
        if isinstance(cm, (int, float)):
            PROXIMITY_WAKE_CM = _clamp_wake_cm(cm)
            logging.info(f"Loaded sensor wake distance from settings: {PROXIMITY_WAKE_CM}cm")
        mins = s.get('screensaver_minutes')
        if isinstance(mins, (int, float)):
            SCREENSAVER_SECONDS = _clamp_screensaver_min(mins) * 60
            logging.info(f"Loaded screensaver timeout from settings: {SCREENSAVER_SECONDS // 60}min")
        en = s.get('restart_enabled')
        if isinstance(en, bool):
            AUTO_RESTART_ENABLED = en
        hr = s.get('restart_hour')
        if isinstance(hr, (int, float)):
            AUTO_RESTART_HOUR = _clamp_restart_hour(hr)
        logging.info(f"Auto-restart: {'on' if AUTO_RESTART_ENABLED else 'off'} "
                     f"at {AUTO_RESTART_HOUR:02d}:00")
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Failed to read settings.json: {e}")


def apply_zip(new_zip):
    """Set the weather ZIP from the on-screen menu, persist it, and re-resolve."""
    global LOCATION_ZIP
    LOCATION_ZIP = str(new_zip).strip()
    _save_settings({'zip': LOCATION_ZIP})
    with data_store.lock:
        data_store.last_update['geo'] = 0      # force location re-resolve
        data_store.last_update['weather'] = 0  # and an immediate weather refetch
    logging.info(f"ZIP set to {LOCATION_ZIP}")


def _clamp_wake_cm(cm):
    """Round to the nearest 5cm step and clamp to the allowed range. -> int."""
    cm = int(round(float(cm) / PROXIMITY_WAKE_STEP_CM) * PROXIMITY_WAKE_STEP_CM)
    return max(PROXIMITY_WAKE_MIN_CM, min(PROXIMITY_WAKE_MAX_CM, cm))


def _clamp_screensaver_min(mins):
    """Clamp the screensaver timeout to the allowed minute range. -> int."""
    return max(SCREENSAVER_MIN_MINUTES, min(SCREENSAVER_MAX_MINUTES, int(round(float(mins)))))


def _clamp_restart_hour(hour):
    """Clamp the auto-restart hour to 0-23. -> int."""
    return max(AUTO_RESTART_MIN_HOUR, min(AUTO_RESTART_MAX_HOUR, int(round(float(hour)))))


def apply_restart_enabled(enabled):
    """Turn the daily auto-restart on/off from the menu, persist it. -> bool."""
    global AUTO_RESTART_ENABLED
    AUTO_RESTART_ENABLED = bool(enabled)
    _save_settings({'restart_enabled': AUTO_RESTART_ENABLED})
    logging.info(f"Auto-restart {'enabled' if AUTO_RESTART_ENABLED else 'disabled'}")
    return AUTO_RESTART_ENABLED


def apply_restart_hour(new_hour):
    """Set the daily auto-restart hour from the menu, persist it. -> int."""
    global AUTO_RESTART_HOUR
    AUTO_RESTART_HOUR = _clamp_restart_hour(new_hour)
    _save_settings({'restart_hour': AUTO_RESTART_HOUR})
    logging.info(f"Auto-restart time set to {AUTO_RESTART_HOUR:02d}:00")
    return AUTO_RESTART_HOUR


def reboot_pi():
    """Reboot the Pi now (from the Firmware menu or the daily schedule).

    The service runs as root, so a plain reboot suffices; `sudo` is kept only so
    this still works if the app is ever run under a non-root user with the usual
    passwordless-sudo the Pi ships with."""
    logging.warning("Rebooting the Pi")
    try:
        subprocess.Popen(['sudo', 'reboot'])
    except Exception as e:
        logging.error(f"Reboot failed: {e}")


def auto_restart_thread():
    """Reboot the Pi at AUTO_RESTART_HOUR:00 on each day it's enabled.

    Polls once every 20s and fires at most once per calendar day, so the reboot
    lands in the target minute without repeating. Reads the globals live, so a
    change from the settings menu takes effect without a restart of this thread."""
    last_fire_day = None
    while True:
        time.sleep(20)
        if not AUTO_RESTART_ENABLED:
            continue
        now = datetime.now()
        if (now.hour == AUTO_RESTART_HOUR and now.minute == 0
                and last_fire_day != now.date()):
            last_fire_day = now.date()
            logging.warning(f"Scheduled daily restart at {AUTO_RESTART_HOUR:02d}:00")
            reboot_pi()


def _reexec(reason):
    """Replace this process with a fresh copy of itself. Used to recover from an
    unrecoverable in-process wedge (FD exhaustion, a stuck fetch loop). Keeps the
    same PID, so the cage compositor keeps managing us and the display returns."""
    logging.critical(f"Re-executing dashboard: {reason}")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def data_watchdog_thread():
    """Recover the dashboard if update_data_thread stops making progress.

    A single wedged network call (half-open socket after a Wi-Fi flap, an
    unresponsive printer MQTT broker) freezes every widget's data at a fixed
    time. The render thread keeps drawing, so nothing looks crashed - it just
    goes stale for hours. This catches that: if the fetch loop's heartbeat is
    older than the timeout, ask the main loop to re-exec."""
    while True:
        time.sleep(30)
        if data_restart_request.is_set():
            continue
        stalled = time.monotonic() - data_store.heartbeat
        if stalled > DATA_WATCHDOG_TIMEOUT_S:
            logging.critical(
                f"Data thread stalled {stalled:.0f}s (>{DATA_WATCHDOG_TIMEOUT_S}s); "
                "requesting restart")
            data_restart_request.set()


def apply_screensaver_min(new_min):
    """Set the screensaver idle timeout from the menu, persist it, apply it live."""
    global SCREENSAVER_SECONDS
    mins = _clamp_screensaver_min(new_min)
    SCREENSAVER_SECONDS = mins * 60   # the main loop reads this global each frame
    _save_settings({'screensaver_minutes': mins})
    logging.info(f"Screensaver timeout set to {mins}min")
    return mins


def apply_sensor_cm(new_cm):
    """Set the proximity wake distance from the menu, persist it, apply it live."""
    global PROXIMITY_WAKE_CM
    PROXIMITY_WAKE_CM = _clamp_wake_cm(new_cm)
    _save_settings({'sensor_wake_cm': PROXIMITY_WAKE_CM})
    if global_sensor is not None:
        global_sensor.set_threshold(PROXIMITY_WAKE_CM)
    logging.info(f"Sensor wake distance set to {PROXIMITY_WAKE_CM}cm")
    return PROXIMITY_WAKE_CM


def apply_printer_conf(ip, serial, access_code):
    """Persist Bambu credentials from the on-screen menu and reconnect. -> bool"""
    PRINTER_CONF.update({'IP': ip.strip(), 'SERIAL': serial.strip(),
                         'ACCESS_CODE': access_code.strip()})
    conf = {}
    try:
        with open(DEVICE_CONF_FILE) as f:
            conf = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Failed to read {DEVICE_CONF_FILE}: {e}")
    if not isinstance(conf, dict):
        conf = {}
    conf['printer'] = dict(PRINTER_CONF)   # leaves roborock and friends intact
    try:
        with open(DEVICE_CONF_FILE, 'w') as f:
            json.dump(conf, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to write {DEVICE_CONF_FILE}: {e}")
        return False
    printer_reinit.set()
    logging.info(f"Printer set to {PRINTER_CONF['IP']}")
    return True


def fetch_claude_profile():
    """{'name','email','plan'} for the connected Claude account, or None."""
    try:
        import claude
        return claude.fetch_profile()
    except Exception:
        return None


def build_settings_ctx():
    """Hooks the on-screen settings menu uses to read/change dashboard state."""
    return {
        'current_zip': lambda: LOCATION_ZIP,
        'apply_zip': apply_zip,
        'current_printer': lambda: dict(PRINTER_CONF),
        'apply_printer': apply_printer_conf,
        'current_sensor_cm': lambda: PROXIMITY_WAKE_CM,
        'apply_sensor_cm': apply_sensor_cm,
        'sensor_available': lambda: bool(global_sensor and global_sensor.available),
        'sensor_bounds': (PROXIMITY_WAKE_MIN_CM, PROXIMITY_WAKE_MAX_CM,
                          PROXIMITY_WAKE_STEP_CM),
        'sensor_reading': lambda: (global_sensor.last_cm if global_sensor else None),
        'current_screensaver_min': lambda: SCREENSAVER_SECONDS // 60,
        'apply_screensaver_min': apply_screensaver_min,
        'screensaver_bounds': (SCREENSAVER_MIN_MINUTES, SCREENSAVER_MAX_MINUTES,
                               SCREENSAVER_STEP_MINUTES),
        'restart_enabled': lambda: AUTO_RESTART_ENABLED,
        'apply_restart_enabled': apply_restart_enabled,
        'current_restart_hour': lambda: AUTO_RESTART_HOUR,
        'apply_restart_hour': apply_restart_hour,
        'restart_bounds': (AUTO_RESTART_MIN_HOUR, AUTO_RESTART_MAX_HOUR, 1),
        'reboot_now': reboot_pi,
        'app_version': APP_VERSION,
        'fetch_claude': fetch_claude_profile,
        'fetch_google': fetch_google_email,
        'current_ssid': wifi_setup.current_ssid,
        'bt': lambda: bt,   # deferred: the BtMusic instance (created in start_threads)
    }


def fetch_google_email():
    """Email of the connected Gmail account, or None. For the Account screen."""
    if not GMAIL_AVAILABLE or not os.path.exists(GMAIL_TOKEN_PATH):
        return None
    try:
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            return None
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        return service.users().getProfile(userId='me').execute().get('emailAddress')
    except Exception:
        return None


def _has_light_background(gray):
    """True if the icon is a dark shape on a light field (the common case)."""
    w, h = gray.size
    corners = (gray.getpixel((0, 0)), gray.getpixel((w - 1, 0)),
               gray.getpixel((0, h - 1)), gray.getpixel((w - 1, h - 1)))
    return sum(corners) / 4.0 >= 128


def get_cached_icon(name, size):
    """Return the icon as an 'L' alpha mask so it can be painted in any colour.

    Most source bitmaps are dark shapes on a light background, so they need
    inverting to make the shape opaque and the background transparent. A few
    (icon_wifi) ship the other way round; inverting those would paint the
    background and knock the glyph out, so detect polarity per icon.
    """
    key = f"{name}_{size[0]}x{size[1]}"
    if key not in icon_cache:
        path = os.path.join(ICON_DIR, f"{name}.bmp")
        if os.path.exists(path):
            try:
                with Image.open(path) as f_img:
                    mask = f_img.convert("L").resize(size, Image.LANCZOS)
                    if _has_light_background(mask):
                        mask = ImageOps.invert(mask)
                    icon_cache[key] = mask
            except Exception:
                icon_cache[key] = None
        else:
            icon_cache[key] = None
    return icon_cache.get(key)


def time_until(iso_str):
    if not iso_str: return "N/A"
    try:
        # Handling the explicit +00:00 timezone format
        target = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        diff = target - now
        if diff.total_seconds() < 0: return "Resetting..."
        hours, rem = divmod(diff.total_seconds(), 3600)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{int(days)}d {int(hours)}h"
        else:
            minutes = rem // 60
            return f"{int(hours)}h {int(minutes)}m"
    except Exception:
        return "N/A"


# --- AUTH & FETCH THREADS ---

def auth_claude():
    global ENABLE_CLAUDE
    if not ENABLE_CLAUDE: return
    try:
        import claude
        success = claude.interactive_auth()
        if not success:
            ENABLE_CLAUDE = False
            print("Claude widget is disabled.")
    except ImportError:
        print("claude.py not found. Claude widget disabled.")
        ENABLE_CLAUDE = False


def auth_antigravity():
    global ENABLE_ANTIGRAVITY
    if not ENABLE_ANTIGRAVITY: return
    try:
        import antigravity
        success = antigravity.interactive_auth()
        if not success:
            ENABLE_ANTIGRAVITY = False
            print("Antigravity widget is disabled.")
    except ImportError:
        print("antigravity.py not found. Antigravity widget disabled.")
        ENABLE_ANTIGRAVITY = False


def auth_strava():
    global ENABLE_STRAVA
    if not ENABLE_STRAVA: return

    if os.path.exists(STRAVA_CONF['TOKEN_FILE']):
        return

    print("\n--- STRAVA CONFIGURATION REQUIRED ---")
    c_id = input("Enter Strava Client ID (or press Enter to disable): ").strip()
    if not c_id:
        print("Strava is disabled. Fallback widget (System Load) will be used.\n")
        ENABLE_STRAVA = False
        return

    c_secret = input("Enter Strava Client Secret: ").strip()

    auth_url = (
        f"{API_ENDPOINTS['strava_auth']}?"
        f"client_id={c_id}&"
        f"response_type=code&"
        f"redirect_uri=http://localhost&"
        f"approval_prompt=force&"
        f"scope=activity:read_all"
    )

    print("\n[!] To get a token with the correct permissions, open this link in your browser:\n")
    print(f"--> {auth_url} <--\n")
    print("Click 'Authorize'. You will be redirected to an empty/error page (localhost).")
    print("Look at the address bar. Copy the 'code' parameter.")

    code_input = input("Enter the 'code' from the URL (or paste the full URL): ").strip()

    if not code_input:
        print("Authorization cancelled. Strava is disabled.\n")
        ENABLE_STRAVA = False
        return

    if 'code=' in code_input:
        try:
            parsed = urllib.parse.urlparse(code_input)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get('code', [code_input])[0]
        except:
            code = code_input.split('code=')[1].split('&')[0]
    else:
        code = code_input

    print("Fetching Access Token...")
    data = {'client_id': c_id, 'client_secret': c_secret, 'code': code, 'grant_type': 'authorization_code'}

    try:
        resp = requests.post(API_ENDPOINTS['strava_token'], data=data)
        resp.raise_for_status()
        token_data = resp.json()
        token_data['client_id'] = c_id
        token_data['client_secret'] = c_secret

        with open(STRAVA_CONF['TOKEN_FILE'], 'w') as f:
            json.dump(token_data, f, indent=4)
        print("Strava Authorization Successful!\n")
    except Exception as e:
        print(f"Failed to fetch Strava tokens: {e}")
        ENABLE_STRAVA = False


def fetch_strava_data():
    if not os.path.exists(STRAVA_CONF['TOKEN_FILE']): return None
    with open(STRAVA_CONF['TOKEN_FILE'], 'r') as f:
        token_data = json.load(f)

    c_id = token_data.get('client_id')
    c_secret = token_data.get('client_secret')

    if time.time() > token_data.get('expires_at', 0):
        data = {'client_id': c_id, 'client_secret': c_secret, 'grant_type': 'refresh_token',
                'refresh_token': token_data.get('refresh_token')}
        new_token = net.get_json(API_ENDPOINTS['strava_token'], data=data, method='POST')
        if new_token and 'access_token' in new_token:
            new_token['client_id'] = c_id
            new_token['client_secret'] = c_secret
            token_data = new_token
            with open(STRAVA_CONF['TOKEN_FILE'], 'w') as f:
                json.dump(token_data, f, indent=4)
        else:
            return None

    access_token = token_data['access_token']

    now_year = datetime.now().year
    start_curr_ts = datetime(now_year, 1, 1).timestamp()
    start_prev_ts = datetime(now_year - 1, 1, 1).timestamp()
    end_prev_ts = datetime(now_year - 1, 12, 31, 23, 59, 59).timestamp()

    page = 1
    total_rides, total_dist = 0, 0
    rides_curr, dist_curr = 0, 0
    rides_prev, dist_prev = 0, 0
    bike_total, hike_total = 0, 0

    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        url = f"{API_ENDPOINTS['strava_activities']}?page={page}&per_page=100"
        activities = net.get_json(url, headers=headers)
        if not activities: break

        for act in activities:
            t = act.get('type')
            d = act.get('distance', 0)
            act_time = datetime.strptime(act['start_date'], "%Y-%m-%dT%H:%M:%SZ").timestamp()

            if t in ['Ride', 'VirtualRide', 'EBikeRide', 'GravelRide', 'MountainBikeRide']:
                total_rides += 1
                total_dist += d
                bike_total += d
                if act_time >= start_curr_ts:
                    rides_curr += 1
                    dist_curr += d
                elif start_prev_ts <= act_time <= end_prev_ts:
                    rides_prev += 1
                    dist_prev += d
            elif t in ['Hike', 'Walk']:
                hike_total += d

        if len(activities) < 100: break
        page += 1

    return {
        "rides": total_rides,
        "total_distance": round(total_dist / 1000, 1),
        "rides_curr": rides_curr,
        "distance_curr": round(dist_curr / 1000, 1),
        "rides_prev": rides_prev,
        "distance_prev": round(dist_prev / 1000, 1),
        "bike_total": round(bike_total / 1000, 1),
        "hike_total": round(hike_total / 1000, 1)
    }


def auth_roborock(email):
    global ENABLE_ROBOROCK
    if not ENABLE_ROBOROCK: return None

    if os.path.exists(ROBOROCK_TOKEN_FILE):
        try:
            with open(ROBOROCK_TOKEN_FILE, "rb") as f:
                return pickle.load(f)
        except:
            pass

    print("\n--- ROBOROCK AUTHORIZATION REQUIRED ---")

    async def _do_auth():
        web_api = RoborockApiClient(username=email)
        await web_api.request_code()
        code = input(f"Enter 6-digit Roborock auth code sent to {email} (or press Enter to disable): ").strip()
        if not code: return None
        user_data = await web_api.code_login(code)
        with open(ROBOROCK_TOKEN_FILE, "wb") as f: pickle.dump(user_data, f)
        print("Roborock Authorization Successful!\n")
        return user_data

    if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        user_data = asyncio.run(_do_auth())
        if not user_data:
            print("Roborock is disabled. Fallback widget (Ping) will be used.\n")
            ENABLE_ROBOROCK = False
        return user_data
    except Exception as e:
        print(f"Failed to auth Roborock: {e}")
        ENABLE_ROBOROCK = False
        return None


def roborock_update_thread(user_data, email):
    if not ENABLE_ROBOROCK or not user_data: return

    async def _loop():
        ref_area, last_date = 0.0, "-"
        if os.path.exists(ROBOROCK_STATS_FILE):
            try:
                with open(ROBOROCK_STATS_FILE, "r") as f:
                    stats = json.load(f)
                    ref_area, last_date = stats.get("ref_area", 0.0), stats.get("last_date", "-")
            except:
                pass

        user_params = UserParams(username=email, user_data=user_data)
        device_manager = await create_device_manager(user_params)

        short_states = {
            5: "Clean", 6: "Return", 8: "Charge", 10: "Pause",
            17: "Spot", 18: "Room", 22: "Empty", 23: "Wash",
            26: "ToWash", 29: "Map"
        }

        while True:
            try:
                devices = await device_manager.get_devices()
                if devices and devices[0].v1_properties:
                    device = devices[0]
                    status_trait = device.v1_properties.status
                    await status_trait.refresh()
                    current_area = (status_trait.clean_area / 1000000) if status_trait.clean_area else 0

                    is_cleaning = status_trait.state in [5, 6, 10, 17, 18, 22, 23, 26, 29]
                    status_str = short_states.get(status_trait.state, f"S:{status_trait.state}")

                    if not is_cleaning and current_area > 0 and current_area != ref_area:
                        ref_area = current_area
                        last_date = datetime.now().strftime("%d %b %H:%M")
                        with open(ROBOROCK_STATS_FILE, "w") as f: json.dump(
                            {"ref_area": ref_area, "last_date": last_date}, f)

                    pct = (current_area / ref_area) * 100 if is_cleaning and ref_area > 0 else 0.0

                    with data_store.lock:
                        data_store.roborock = {
                            'status': status_str, 'battery': status_trait.battery,
                            'is_cleaning': is_cleaning, 'current_area': current_area,
                            'ref_area': ref_area, 'pct': pct, 'last_date': last_date
                        }
                        data_store.bump()
            except Exception as e:
                logging.error(f"Roborock error: {e}")
            await asyncio.sleep(60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_loop())


def update_data_thread():
    global global_printer

    def _init_printer():
        global global_printer
        try:
            global_printer = bl.Printer(PRINTER_CONF['IP'], PRINTER_CONF['ACCESS_CODE'], PRINTER_CONF['SERIAL'])
        except Exception as e:
            logging.error(f"Bambu init error: {e}")
            global_printer = None

    if ENABLE_BAMBU:
        _init_printer()

    is_connected = False

    while True:
        # Heartbeat for the watchdog: proof this loop is still turning over. A
        # fetch that wedges below never lets us come back here, so the heartbeat
        # goes stale and the watchdog re-execs us.
        data_store.heartbeat = time.monotonic()
        now = time.time()

        # Resolve the weather location before the weather fetch, so the first
        # pass resolves the location and then fetches for that spot; re-checked
        # once a day. Priority: ZIP -> city name -> public IP -> lat/lon fallback.
        # A failure leaves the previous location in place, so a brief outage or a
        # bad lookup never blanks the weather.
        if now - data_store.last_update['geo'] > 86400:
            geo = geocode_zip(LOCATION_ZIP, LOCATION_ZIP_COUNTRY) if LOCATION_ZIP else None
            if not geo and LOCATION_CITY:
                geo = geocode_city(LOCATION_CITY)
            if not geo and USE_IP_LOCATION:
                geo = geolocate_by_ip()
            if geo:
                lat, lon, label = geo
                with data_store.lock:
                    moved = (lat, lon) != (data_store.location['lat'], data_store.location['lon'])
                    data_store.location = {'lat': lat, 'lon': lon, 'label': label}
                # Force an immediate weather refresh when the location changes.
                if moved:
                    logging.info(f"Weather location: {label} ({lat:.3f}, {lon:.3f})")
                    data_store.last_update['weather'] = 0
                data_store.last_update['geo'] = now
            else:
                # Nothing resolved yet - retry in ~10 min rather than a full day.
                data_store.last_update['geo'] = now - 86400 + 600

        if now - data_store.last_update['weather'] > 600:
            loc = data_store.location
            lat, lon = loc['lat'], loc['lon']
            weather_url = f"{API_ENDPOINTS['weather']}?latitude={lat}&longitude={lon}&current=temperature_2m,wind_speed_10m,wind_direction_10m,weather_code,is_day,uv_index&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=4"
            aqi_url = f"{API_ENDPOINTS['aqi']}?latitude={lat}&longitude={lon}&current=european_aqi&timezone=auto"
            w_data = net.get_json(weather_url)
            a_data = net.get_json(aqi_url)
            with data_store.lock:
                if w_data: data_store.weather = w_data
                if a_data and 'current' in a_data: data_store.aqi = a_data['current'].get('european_aqi', 0)
                data_store.bump()
            data_store.last_update['weather'] = now

        if ENABLE_STRAVA:
            if now - data_store.last_update['strava'] > 900:
                s_data = fetch_strava_data()
                if s_data:
                    with data_store.lock:
                        data_store.strava = s_data
                        data_store.bump()
                data_store.last_update['strava'] = now
        else:
            if now - data_store.last_update['sysload'] > 30:
                try:
                    with open('/proc/loadavg', 'r') as f:
                        cpu = float(f.read().split()[0]) * 10
                    with open('/proc/meminfo', 'r') as f:
                        lines = f.readlines()
                        free = int(lines[1].split()[1]) // 1024
                    with data_store.lock:
                        data_store.sysload['cpu'] = min(int(cpu), 100)
                        data_store.sysload['ram_free'] = free
                        data_store.sysload['history'].append(min(int(cpu), 100))
                        data_store.bump()
                except:
                    pass
                data_store.last_update['sysload'] = now

        if ENABLE_BAMBU:
            # Credentials changed in Settings -> Account -> Bambu Printer.
            if printer_reinit.is_set():
                printer_reinit.clear()
                if global_printer:
                    try:
                        global_printer.disconnect()
                    except Exception:
                        pass
                _init_printer()
                is_connected = False
                data_store.last_update['printer'] = 0

            update_interval = 5 if is_connected else 15
            if now - data_store.last_update['printer'] > update_interval:
                is_alive = ping_printer(PRINTER_CONF['IP'])
                if is_alive:
                    try:
                        if not is_connected and global_printer:
                            global_printer.connect()
                            time.sleep(1)
                            is_connected = True
                        if global_printer:
                            # An idle printer reports UNKNOWN gcode state but still
                            # pushes temps over MQTT, so treat reachable+UNKNOWN as
                            # IDLE rather than dropping it to OFFLINE.
                            raw = global_printer.get_state()
                            state = str(raw).upper() if raw else 'IDLE'
                            if state in ('UNKNOWN', 'NONE', ''):
                                state = 'IDLE'
                            try:
                                nozzle = global_printer.get_nozzle_temperature()
                                bed = global_printer.get_bed_temperature()
                            except Exception:
                                nozzle = bed = None
                            with data_store.lock:
                                data_store.printer = {
                                    'status': state,
                                    'percentage': global_printer.get_percentage(),
                                    'remaining_time': global_printer.get_time(),
                                    'layers': f"{global_printer.current_layer_num()}/{global_printer.total_layer_num()}",
                                    'nozzle': nozzle,
                                    'bed': bed,
                                }
                                data_store.bump()
                    except Exception as e:
                        is_connected = False
                        with data_store.lock:
                            data_store.printer = {'status': 'OFFLINE'}
                            data_store.bump()
                        try:
                            if global_printer: global_printer.disconnect()
                        except:
                            pass
                else:
                    if is_connected:
                        is_connected = False
                        try:
                            global_printer.disconnect()
                        except:
                            pass
                    with data_store.lock:
                        data_store.printer = {'status': 'OFFLINE'}
                        data_store.bump()
                data_store.last_update['printer'] = now

        # Markets (BTC / S&P 500 / gold) - always fetched; shown in column 1's
        # lower slot regardless of the printer.
        if now - data_store.last_update['market'] > 600:
            m_data = fetch_markets()
            if m_data:
                with data_store.lock:
                    data_store.market.update(m_data)
                    data_store.bump()
            data_store.last_update['market'] = now

        if not ENABLE_ROBOROCK and not ENABLE_ANTIGRAVITY:
            if now - data_store.last_update['ping'] > 20:
                try:
                    out = subprocess.check_output(['ping', '-c', '1', '-W', '1', '8.8.8.8']).decode('utf-8')
                    ms = float(out.split('time=')[1].split(' ms')[0])
                except:
                    ms = 0
                with data_store.lock:
                    data_store.ping['current'] = int(ms)
                    data_store.ping['history'].append(int(ms))
                    data_store.bump()
                data_store.last_update['ping'] = now

        if GMAIL_AVAILABLE and now - data_store.last_update['gmail'] > 300:
            try:
                creds = None
                if os.path.exists(GMAIL_TOKEN_PATH):
                    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        with open(GMAIL_TOKEN_PATH, 'w') as t: t.write(creds.to_json())
                if creds and creds.valid:
                    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
                    label_info = service.users().labels().get(userId='me', id='INBOX').execute()
                    with data_store.lock:
                        data_store.gmail_unread = label_info.get('messagesUnread', 0)
                        data_store.bump()
            except:
                pass
            data_store.last_update['gmail'] = now

        # Claude Data Fetching (Run external script every 10 min)
        if ENABLE_CLAUDE and now - data_store.last_update['claude'] > 600:
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, 'claude.py')], capture_output=True, timeout=30)
                usage_path = os.path.join(BASE_DIR, 'usage.json')
                if os.path.exists(usage_path):
                    with open(usage_path, 'r') as f:
                        usage_data = json.load(f)
                    with data_store.lock:
                        data_store.claude = usage_data
                        # claude.py reports 'reauth_required' (dead token, needs a
                        # browser login) or 'transient' (network/rate limit, last
                        # good numbers kept and flagged stale).
                        kind = usage_data.get('error') if 'five_hour' not in usage_data else None
                        data_store.claude['error'] = bool(kind)
                        data_store.claude['error_kind'] = kind
                        data_store.bump()
                else:
                    with data_store.lock:
                        data_store.claude['error'] = True
                        data_store.bump()
            except Exception as e:
                logging.error(f"Claude update error: {e}")
                with data_store.lock:
                    data_store.claude['error'] = True
                    data_store.bump()
            data_store.last_update['claude'] = now

        if ENABLE_ANTIGRAVITY and now - data_store.last_update['antigravity'] > 60:
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, 'antigravity.py')], capture_output=True, timeout=30)
                limits_path = os.path.join(BASE_DIR, 'limits.json')
                if os.path.exists(limits_path):
                    with open(limits_path, 'r', encoding='utf-8') as f:
                        limits_data = json.load(f)
                    with data_store.lock:
                        data_store.antigravity = limits_data
                        if "error" in limits_data:
                            data_store.antigravity['error'] = True
                        else:
                            data_store.antigravity['error'] = False
                        data_store.bump()
                else:
                    with data_store.lock:
                        data_store.antigravity['error'] = True
                        data_store.bump()
            except Exception as e:
                logging.error(f"Antigravity update error: {e}")
                with data_store.lock:
                    data_store.antigravity['error'] = True
                    data_store.bump()
            data_store.last_update['antigravity'] = now

        if ENABLE_SPOTIFY and now - data_store.last_update['spotify'] > 20:
            url = f"{API_ENDPOINTS['lastfm']}?method=user.getrecenttracks&user={LASTFM_CONF['USERNAME']}&api_key={LASTFM_CONF['API_KEY']}&format=json&limit=2&rnd={int(now)}"
            s_data = net.get_json(url, timeout=5)
            if s_data:
                try:
                    tracks = s_data.get('recenttracks', {}).get('track', [])
                    if isinstance(tracks, dict): tracks = [tracks]
                    if tracks:
                        current_track = tracks[0]
                        is_playing = current_track.get('@attr', {}).get('nowplaying') == 'true'
                        if is_playing:
                            track_name = current_track.get('name', 'Unknown')
                            artist = current_track.get('artist', {}).get('#text', 'Unknown')
                            img_url = ""
                            for img in current_track.get('image', []):
                                if img.get('size') == 'extralarge': img_url = img.get('#text', '')
                            # The LCD is full colour, so album art is kept as RGB
                            # instead of the old 1-bit dither.
                            cover = None
                            if img_url:
                                img_bytes = net.get_image(img_url)
                                if img_bytes:
                                    cover = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize(
                                        (COVER_SIZE, COVER_SIZE), Image.LANCZOS)
                            with data_store.lock:
                                data_store.spotify = {'status': 'PLAYING', 'text': f"{artist} - {track_name}",
                                                      'cover': cover}
                                data_store.bump()
                        else:
                            with data_store.lock:
                                data_store.spotify = {'status': 'PAUSED', 'text': '', 'cover': None}
                                data_store.bump()
                except:
                    pass
            data_store.last_update['spotify'] = now

        gc.collect()
        time.sleep(1)


# Finance widget symbols (Yahoo Finance chart API). GC=F is COMEX gold futures
# in USD/oz; ^GSPC is the S&P 500 index; BTC-USD is bitcoin. Yahoo needs a
# browser-like User-Agent or it returns 429/empty.
MARKET_SYMBOLS = [('btc', 'BTC-USD'), ('sp500', '^GSPC'), ('gold', 'GC=F')]
_YAHOO_HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64)'}


def fetch_markets():
    """Fetch current price + daily % change for each market symbol.

    Returns {'btc': {'price', 'pct'}, ...} for whatever resolved, or None.
    """
    out = {}
    for key, sym in MARKET_SYMBOLS:
        url = (f"{API_ENDPOINTS['yahoo_chart']}{urllib.parse.quote(sym)}"
               f"?interval=1d&range=1d")
        data = net.get_json(url, headers=_YAHOO_HEADERS)
        try:
            meta = data['chart']['result'][0]['meta']
            price = meta.get('regularMarketPrice')
            if price is None:
                continue
            prev = meta.get('chartPreviousClose') or meta.get('previousClose')
            pct = ((price - prev) / prev * 100.0) if prev else None
            out[key] = {'price': float(price), 'pct': pct}
        except (KeyError, IndexError, TypeError):
            continue
    return out or None


# ###########################
# --- LAYOUT (1920 x 440) ---
# ###########################
PAD = 24
COL_W = SCREEN_W // 4  # 480 - four columns instead of three; the panel is
                       # 560px wider but 40px shorter than the old e-paper.
COL_X = [0, COL_W, COL_W * 2, COL_W * 3]

ROW1_Y = 16
ROW_RULE_Y = 218
ROW2_Y = 236
BOTTOM_Y = 424

COVER_SIZE = 120


def inner_x(col):
    return COL_X[col] + PAD


def inner_right(col):
    return COL_X[col] + COL_W - PAD


# --- GRAPHICS FUNCTIONS ---
def draw_icon(img, x, y, name, size=(40, 40), color=None):
    color = color or THEME['fg']
    mask = get_cached_icon(name, size)
    if mask:
        img.paste(color, (int(x), int(y), int(x) + size[0], int(y) + size[1]), mask)
    else:
        ImageDraw.Draw(img).rectangle((x, y, x + size[0], y + size[1]), outline=THEME['line'])


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_text_right(draw, right_x, y, text, font, color):
    """Align the glyphs' right ink edge to right_x (bbox[2] includes bearing)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((right_x - bbox[2], y), text, font=font, fill=color)


def draw_text_center(draw, center_x, y, text, font, color):
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (bbox[0] + bbox[2]) / 2, y), text, font=font, fill=color)


def hline(draw, x0, x1, y):
    draw.line((x0, y, x1, y), fill=THEME['line'], width=2)


def draw_bar(draw, x, y, w, h, pct, color=None):
    """Rounded-ish progress bar. `pct` is 0-100."""
    fill = color or pct_color(pct)
    draw.rectangle((x, y, x + w, y + h), outline=THEME['line'], width=2)
    fill_w = int((w - 4) * min(max(pct, 0) / 100.0, 1.0))
    if fill_w > 0:
        draw.rectangle((x + 2, y + 2, x + 2 + fill_w, y + h - 2), fill=fill)


def draw_badge(draw, x, y, text, font, bg_color):
    """Filled pill used to call out an alerting value (high AQI / UV).

    Measures at the real draw origin so the fill hugs the glyphs regardless of
    the font's ascender/bearing, rather than guessing with fixed offsets.
    """
    pad_x, pad_y = 14, 8
    left, top, right, bottom = draw.textbbox((x, y), text, font=font)
    draw.rectangle((left - pad_x, top - pad_y, right + pad_x, bottom + pad_y), fill=bg_color)
    draw.text((x, y), text, font=font, fill=THEME['bg'])


def draw_sparkline(draw, x, y, data, max_items=50, width=400, height=60,
                   color=None, style="bar", zero_based=True):
    if not data: return
    color = color or THEME['accent']

    lo = 0 if zero_based else min(data)
    hi = max(data)
    span = (hi - lo) or 1.0
    step = width / max(max_items - 1, 1)

    if style == "line":
        points = []
        for i, val in enumerate(data):
            px = x + i * step
            py = y + height - ((val - lo) / span) * height
            points.append((px, py))
        if len(points) > 1:
            draw.line(points, fill=color, width=2, joint="curve")
        # Faint baseline so a flat series still reads as a chart.
        hline(draw, x, x + width, y + height)
    else:
        bar_w = max(int(step) - 1, 1)
        for i, val in enumerate(data):
            bh = int(((val - lo) / span) * height)
            bx = x + i * step
            by = y + height - bh
            draw.rectangle((bx, by, bx + bar_w, y + height), fill=color)


def get_weather_icon(code, is_day=1):
    if code == 0:
        return "icon_sun" if is_day else "icon_moon"
    elif code in [1, 2]:
        return "icon_partly-cloudy-day"
    elif code == 3:
        return "icon_clouds"
    elif code in [45, 48]:
        return "icon_wind"
    elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
        return "icon_rain"
    elif code in [71, 73, 75, 85, 86]:
        return "icon_snow"
    elif code in [95, 96, 99]:
        return "icon_cloud-lightning"
    return "icon_sun"


# --- WIDGETS ---
def widget_strava(img, draw, x, y, strava):
    draw_icon(img, x, y, "icon_strava", (56, 56), THEME['accent'])
    draw.text((x + 70, y + 4), "STRAVA STATS", font=FONTS['28'], fill=THEME['fg'])

    now_y = datetime.now().year
    draw.text((x + 70, y + 44),
              f"{now_y}: {strava.get('distance_curr', 0)} km | {now_y - 1}: {strava.get('distance_prev', 0)} km",
              font=FONTS['20'], fill=THEME['muted'])
    draw.text((x + 70, y + 70),
              f"Total: {strava.get('total_distance', 0)} km | {strava.get('rides', 0)} acts",
              font=FONTS['20'], fill=THEME['muted'])

    draw_icon(img, x + 70, y + 100, "icon_bike", (28, 28), THEME['fg'])
    draw.text((x + 104, y + 104), f"{strava.get('bike_total', 0)} km", font=FONTS['20'], fill=THEME['fg'])

    draw_icon(img, x + 240, y + 100, "icon_hike", (28, 28), THEME['fg'])
    draw.text((x + 274, y + 104), f"{strava.get('hike_total', 0)} km", font=FONTS['20'], fill=THEME['fg'])


def widget_sysload(img, draw, x, y, sysload):
    cpu = sysload['cpu']
    draw_icon(img, x, y, "icon_cpu", (50, 50), pct_color(cpu))
    draw.text((x + 64, y + 4), f"SYSTEM LOAD: {cpu}%", font=FONTS['28'], fill=THEME['fg'])
    draw.text((x + 64, y + 42), f"RAM Free: {sysload['ram_free']} MB", font=FONTS['20'], fill=THEME['muted'])
    draw_sparkline(draw, x + 64, y + 72, list(sysload['history']), max_items=30,
                   width=360, height=70, color=pct_color(cpu), style="bar")


def widget_bambu(img, draw, x, y, printer):
    p_status = str(printer.get('status', 'OFFLINE')).upper()
    offline = p_status in ("OFFLINE", "UNKNOWN")
    printing = p_status in ("RUNNING", "PREPARE", "PAUSE", "SLICING")
    icon_color = THEME['ok'] if printing else THEME['muted'] if offline else THEME['accent']
    draw_icon(img, x, y, "icon_3d", (56, 56), icon_color)
    draw.text((x + 70, y + 4), f"PRINTER: {p_status}", font=FONTS['28'], fill=THEME['fg'])

    if printing:
        percent = printer.get('percentage') or 0
        draw_bar(draw, x + 70, y + 46, 340, 22, percent, THEME['ok'])
        draw.text((x + 70, y + 78),
                  f"{percent}% | Rem: {printer.get('remaining_time', '0')}m | {printer.get('layers', '0/0')} L",
                  font=FONTS['20'], fill=THEME['muted'])
    elif not offline:
        # Idle / finished: show nozzle + bed temperatures instead of a progress bar.
        nozzle, bed = printer.get('nozzle'), printer.get('bed')
        if nozzle is not None:
            draw_icon(img, x + 70, y + 48, "icon_temp", (26, 26), THEME['warn'])
            draw.text((x + 104, y + 50), f"Nozzle {nozzle:.0f}°C", font=FONTS['20'], fill=THEME['muted'])
        if bed is not None:
            draw_icon(img, x + 250, y + 48, "icon_temp", (26, 26), THEME['accent'])
            draw.text((x + 284, y + 50), f"Bed {bed:.0f}°C", font=FONTS['20'], fill=THEME['muted'])


def widget_markets(img, draw, x, y, market):
    # (key, label, colour, price prefix). S&P is an index, so no '$'.
    rows = [
        ('btc', 'BTC', THEME['warn'], '$'),
        ('sp500', 'S&P 500', THEME['accent'], ''),
        ('gold', 'GOLD', THEME['gold'], '$'),
    ]
    right = inner_right(0)
    row_h = 62
    for i, (key, label, color, prefix) in enumerate(rows):
        ry = y + 4 + i * row_h
        draw.text((x, ry), label, font=FONTS['32'], fill=color)

        info = market.get(key) or {}
        price = info.get('price')
        if price is None:
            draw.text((x + 175, ry + 2), "...", font=FONTS['32'], fill=THEME['muted'])
            continue
        price_str = f"{prefix}{price:,.0f}"
        draw.text((x + 175, ry), price_str, font=FONTS['32'], fill=THEME['fg'])

        pct = info.get('pct')
        if pct is not None:
            pc = THEME['ok'] if pct >= 0 else THEME['alert']
            pct_str = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
            draw_text_right(draw, right, ry + 6, pct_str, FONTS['20'], pc)


def widget_roborock(img, draw, x, y, rob):
    battery = rob['battery']
    draw_icon(img, x, y, "icon_roborock", (50, 50),
              THEME['ok'] if battery > 25 else THEME['alert'])
    draw.text((x + 64, y + 2), f"Bat: {battery}% | {rob['status']}", font=FONTS['28'], fill=THEME['fg'])

    if rob['is_cleaning']:
        draw.text((x + 64, y + 42), f"Clean: {rob['current_area']:.1f} m2 ({rob['pct']:.0f}%)",
                  font=FONTS['24'], fill=THEME['muted'])
        draw_bar(draw, x + 64, y + 78, 340, 22, min(rob['pct'], 100), THEME['accent'])
    else:
        draw.text((x + 64, y + 42), f"Last: {rob['last_date']} | {rob['ref_area']:.1f} m2",
                  font=FONTS['24'], fill=THEME['muted'])


def widget_antigravity(img, draw, x, y, antigravity):
    draw_icon(img, x, y, "icon_cpu", (50, 50), THEME['accent'])
    draw.text((x + 64, y + 2), "ANTIGRAVITY USAGE", font=FONTS['28'], fill=THEME['fg'])

    if antigravity.get('error'):
        draw.text((x + 64, y + 44), "Error loading data", font=FONTS['20'], fill=THEME['alert'])
        return

    models = antigravity.get('models', [])
    opus = next((m for m in models if m.get('modelId') == 'claude-opus-4-6-thinking'), None)
    gemini = next((m for m in models if m.get('modelId') == 'gemini-3-pro-high'), None)

    y_off = y + 42
    for m_data in (opus, gemini):
        if not m_data:
            continue
        label = "Opus 4.6" if m_data.get('modelId') == 'claude-opus-4-6-thinking' else "Gemini 3Pro"
        pct = m_data.get('usedPercentage', 0)
        rem_time = time_until(m_data.get('resetDate'))

        draw.text((x + 64, y_off), f"{label} {pct}% | In {rem_time}", font=FONTS['20'], fill=THEME['muted'])
        draw_bar(draw, x + 64, y_off + 24, 340, 16, pct)
        y_off += 52


def widget_ping(img, draw, x, y, ping):
    ms = ping['current']
    color = THEME['ok'] if 0 < ms < 50 else THEME['warn'] if ms < 120 else THEME['alert']
    draw_icon(img, x, y, "icon_wifi", (50, 50), color)
    draw.text((x + 64, y + 2), f"Internet Quality: {ms} ms", font=FONTS['28'], fill=THEME['fg'])
    draw_sparkline(draw, x, y + 62, list(ping['history']), max_items=50, width=420,
                   height=60, color=color, style="bar")


def _fmt_ms(ms):
    s = max(0, int(ms)) // 1000
    return f"{s // 60}:{s % 60:02d}"


# Tap targets for the music transport controls, rebuilt each render:
# [(x0, y0, x1, y1, 'prev'|'playpause'|'next')]. Empty when no track is shown.
MUSIC_HITS = []


def widget_music(img, draw, x, y, music):
    global MUSIC_HITS
    MUSIC_HITS = []
    right = inner_right(1)
    width = right - x
    music = music or {}
    title = music.get('title', '')
    playing = music.get('status') == 'playing'
    has_track = bool(title)

    # Header: a small Bluetooth glyph + status.
    draw_icon(img, x, y, "icon_spotify", (30, 30),
              THEME['ok'] if has_track else THEME['muted'])
    hdr = "NOW PLAYING" if has_track else (
        "Bluetooth connected" if music.get('connected') else "Bluetooth: pair a phone")
    draw.text((x + 40, y + 2), hdr, font=FONTS['20'], fill=THEME['muted'])

    if not has_track:
        draw.text((x, y + 48), "No music playing", font=FONTS['28'], fill=THEME['muted'])
        draw.text((x, y + 84), "Hold 5s -> Settings -> Bluetooth", font=FONTS['20'], fill=THEME['line'])
        return

    # Song + artist (CJK-capable font so Chinese/Japanese/Korean names render).
    draw.text((x, y + 36), title[:24], font=FONTS['cjk'], fill=THEME['fg'])
    draw.text((x, y + 76), music.get('artist', '')[:30], font=FONTS['cjk_sm'], fill=THEME['muted'])

    # Progress bar with position dot.
    bar_y = y + 116
    dur = music.get('duration_ms', 0)
    now = music.get('now_ms', 0)
    frac = min(max(now / dur, 0.0), 1.0) if dur else 0.0
    draw.line((x, bar_y, right, bar_y), fill=THEME['line'], width=3)
    if dur:
        fill_x = x + int(width * frac)
        draw.line((x, bar_y, fill_x, bar_y), fill=THEME['accent'], width=3)
        draw.ellipse((fill_x - 8, bar_y - 8, fill_x + 8, bar_y + 8), fill=THEME['accent'])
    draw.text((x, bar_y + 12), _fmt_ms(now), font=FONTS['20'], fill=THEME['muted'])
    if dur:
        draw_text_right(draw, right, bar_y + 12, _fmt_ms(dur), FONTS['20'], THEME['muted'])

    # Playback controls: prev | play/pause | next (tappable).
    cy = y + 170
    cx = (x + right) // 2
    ctrl = THEME['fg']
    # prev
    draw.polygon([(cx - 74, cy - 12), (cx - 74, cy + 12), (cx - 90, cy)], fill=ctrl)
    draw.rectangle((cx - 94, cy - 12, cx - 90, cy + 12), fill=ctrl)
    # next
    draw.polygon([(cx + 74, cy - 12), (cx + 74, cy + 12), (cx + 90, cy)], fill=ctrl)
    draw.rectangle((cx + 90, cy - 12, cx + 94, cy + 12), fill=ctrl)
    # play / pause in a circle
    draw.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), outline=THEME['accent'], width=3)
    if playing:
        draw.rectangle((cx - 10, cy - 12, cx - 3, cy + 12), fill=THEME['accent'])
        draw.rectangle((cx + 3, cy - 12, cx + 10, cy + 12), fill=THEME['accent'])
    else:
        draw.polygon([(cx - 8, cy - 13), (cx - 8, cy + 13), (cx + 14, cy)], fill=THEME['accent'])

    # Generous tap targets around each control.
    MUSIC_HITS = [
        (cx - 116, cy - 34, cx - 52, cy + 34, 'prev'),
        (cx - 40, cy - 34, cx + 40, cy + 34, 'playpause'),
        (cx + 52, cy - 34, cx + 116, cy + 34, 'next'),
    ]


def widget_claude(img, draw, x, y, claude):
    draw.text((x, y), "CLAUDE AI USAGE", font=FONTS['28'], fill=THEME['fg'])
    if claude.get('stale'):
        draw.text((x + 260, y + 6), "stale", font=FONTS['20'], fill=THEME['muted'])

    if claude.get('error'):
        if claude.get('error_kind') == 'reauth_required':
            draw.text((x, y + 44), "Sign-in expired", font=FONTS['24'], fill=THEME['alert'])
            draw.text((x, y + 76), "run: claude.py --reauth", font=FONTS['20'], fill=THEME['muted'])
        else:
            draw.text((x, y + 50), "Claude Usage Error", font=FONTS['24'], fill=THEME['alert'])
        return

    pct_5h = claude.get('five_hour', {}).get('utilization', 0)
    rem_5h = time_until(claude.get('five_hour', {}).get('resets_at'))
    draw.text((x, y + 40), f"5-Hour Limit: {pct_5h}% (Resets in {rem_5h})", font=FONTS['20'], fill=THEME['muted'])
    draw_bar(draw, x, y + 66, 400, 16, pct_5h)

    pct_7d = claude.get('seven_day', {}).get('utilization', 0)
    rem_7d = time_until(claude.get('seven_day', {}).get('resets_at'))
    draw.text((x, y + 94), f"7-Day Limit: {pct_7d}% (Resets in {rem_7d})", font=FONTS['20'], fill=THEME['muted'])
    draw_bar(draw, x, y + 120, 400, 16, pct_7d)


def widget_spotify(img, draw, x, y, spotify):
    if spotify['cover']:
        img.paste(spotify['cover'], (x, y))
    else:
        draw_icon(img, x, y, "icon_spotify", (COVER_SIZE, COVER_SIZE), THEME['ok'])

    playing = spotify['status'] == 'PLAYING'
    draw_icon(img, x + COVER_SIZE + 20, y + 10, "icon_play" if playing else "icon_pause",
              (30, 30), THEME['ok'] if playing else THEME['muted'])

    if playing:
        words = spotify['text'].split(' - ')
        artist = words[0] if words else "Unknown"
        track = words[1] if len(words) > 1 else ""
        draw.text((x + COVER_SIZE + 60, y + 10), artist[:20], font=FONTS['28'], fill=THEME['fg'])
        draw.text((x + COVER_SIZE + 60, y + 54), track[:26], font=FONTS['24'], fill=THEME['muted'])
    else:
        draw.text((x + COVER_SIZE + 60, y + 10), "Paused", font=FONTS['28'], fill=THEME['muted'])


def widget_time_progress(img, draw, x, y, dt):
    draw.text((x, y), "TIME PROGRESS", font=FONTS['28'], fill=THEME['fg'])

    day_pct = (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400.0
    days_in_m = calendar.monthrange(dt.year, dt.month)[1]
    month_pct = (dt.day - 1 + (dt.hour / 24.0)) / days_in_m
    days_in_y = 366 if calendar.isleap(dt.year) else 365
    year_pct = (dt.timetuple().tm_yday - 1 + (dt.hour / 24.0)) / days_in_y

    def draw_prog(y_offset, label, pct):
        draw.text((x, y + y_offset), label, font=FONTS['24'], fill=THEME['muted'])
        bx, bw, bh = x + 130, 220, 20
        draw_bar(draw, bx, y + y_offset + 2, bw, bh, pct * 100, THEME['accent'])
        draw.text((bx + bw + 16, y + y_offset), f"{int(pct * 100)}%", font=FONTS['24'], fill=THEME['fg'])

    draw_prog(42, "DAY", day_pct)
    draw_prog(80, "MONTH", month_pct)
    draw_prog(118, "YEAR", year_pct)


def widget_weather(img, draw, col, weather, aqi, location_label=''):
    x = inner_x(col)
    right = inner_right(col)

    # On-screen we show just the city; the full "City, Region" stays in logs.
    city = location_label.split(',')[0].strip()

    if 'current' not in weather:
        draw.text((x, ROW1_Y + 60), "Waiting for weather data...", font=FONTS['24'], fill=THEME['muted'])
        if city:
            draw.text((x, ROW1_Y + 92), city, font=FONTS['20'], fill=THEME['line'])
        return

    cur = weather['current']
    temp = cur.get('temperature_2m', 0)
    w_code = cur.get('weather_code', 0)
    wind_dir = cur.get('wind_direction_10m', 0)
    wind_spd = cur.get('wind_speed_10m', 0)
    is_day = cur.get('is_day', 1)
    uv_index = cur.get('uv_index', 0.0)

    # --- Row A: current conditions ---
    draw_icon(img, x, 20, get_weather_icon(w_code, is_day), (84, 84), THEME['accent'])
    draw.text((x + 96, 24), f"{math.floor(temp + 0.5)}°C", font=FONTS['96'], fill=THEME['fg'])

    # UV stacked at the right edge: the label sits above the number rather than
    # beside it, so a two-digit UV can't crowd the (now much wider) temperature.
    uv_rounded = math.floor(uv_index + 0.5)
    uv_str = str(uv_rounded)
    draw_text_right(draw, right, 4, "UV", FONTS['20'], THEME['muted'])
    uv_w, _ = text_size(draw, uv_str, FONTS['60'])
    uv_x = right - uv_w
    if uv_rounded >= 6:
        draw_badge(draw, uv_x, 32, uv_str, FONTS['60'], THEME['warn'])
    else:
        draw.text((uv_x, 32), uv_str, font=FONTS['60'], fill=THEME['fg'])

    # City name, right-aligned below the UV reading and clear of the
    # temperature's descender band. Kept to the city alone so it never runs in.
    if city:
        draw_text_right(draw, right, 102, city[:16], FONTS['20'], THEME['muted'])

    hline(draw, x, right, 130)

    # --- Row B: wind compass + air quality ---
    draw_icon(img, x + 2, 146, "icon_wind", (26, 26), THEME['muted'])

    cx, cy, r = x + 80, 228, 58
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=THEME['line'], width=2)

    for angle in range(0, 360, 45):
        rad_tick = math.radians(angle)
        inner_r = r - 8 if angle % 90 == 0 else r - 4
        tx1, ty1 = cx + inner_r * math.cos(rad_tick), cy + inner_r * math.sin(rad_tick)
        tx2, ty2 = cx + r * math.cos(rad_tick), cy + r * math.sin(rad_tick)
        draw.line((tx1, ty1, tx2, ty2), fill=THEME['line'], width=2)

    draw.text((cx - 8, cy - r - 22), "N", font=FONTS['20'], fill=THEME['muted'])
    draw.text((cx - 8, cy + r + 2), "S", font=FONTS['20'], fill=THEME['muted'])
    draw.text((cx + r + 6, cy - 10), "E", font=FONTS['20'], fill=THEME['muted'])
    draw.text((cx - r - 24, cy - 10), "W", font=FONTS['20'], fill=THEME['muted'])

    rad_arrow = math.radians(wind_dir - 90)
    tip_x = cx + (r - 12) * math.cos(rad_arrow)
    tip_y = cy + (r - 12) * math.sin(rad_arrow)
    base_angle = math.radians(150)
    left_x = cx + 20 * math.cos(rad_arrow + base_angle)
    left_y = cy + 20 * math.sin(rad_arrow + base_angle)
    right_x = cx + 20 * math.cos(rad_arrow - base_angle)
    right_y = cy + 20 * math.sin(rad_arrow - base_angle)
    draw.polygon([(tip_x, tip_y), (left_x, left_y), (right_x, right_y)], fill=THEME['accent'])
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=THEME['accent'])
    draw_text_center(draw, cx, cy + 26, f"{wind_spd} km/h", FONTS['20'], THEME['fg'])

    aqi_x = x + 206
    draw.text((aqi_x, 152), "AIR QUALITY", font=FONTS['20'], fill=THEME['muted'])
    draw.text((aqi_x, 196), "AQI:", font=FONTS['28'], fill=THEME['fg'])

    aqi_str = str(aqi)
    aqi_w, _ = text_size(draw, aqi_str, FONTS['80'])
    aqi_val_x = right - aqi_w
    if aqi >= 50:
        draw_badge(draw, aqi_val_x, 186, aqi_str, FONTS['80'], THEME['alert'])
    else:
        draw.text((aqi_val_x, 186), aqi_str, font=FONTS['80'],
                  fill=THEME['ok'] if aqi <= 20 else THEME['fg'])

    hline(draw, x, right, 306)

    # --- Row C: 4-day forecast ---
    daily = weather.get('daily', {})
    days = daily.get('time', [])
    d_codes = daily.get('weather_code', [])
    d_max = daily.get('temperature_2m_max', [])
    d_min = daily.get('temperature_2m_min', [])

    # Card fill for "today": halfway between the background and the rule colour,
    # so it reads as a raised tile in both the dark and light themes.
    card = tuple((b + l) // 2 for b, l in zip(THEME['bg'], THEME['line']))
    today = datetime.now().strftime("%Y-%m-%d")

    slot_w = (right - x) // 4
    for i in range(min(4, len(days), len(d_codes), len(d_max), len(d_min))):
        off_x = x + i * slot_w
        mid_x = off_x + slot_w // 2

        if days[i] == today:
            draw.rounded_rectangle((off_x + 2, 314, off_x + slot_w - 2, 422),
                                   radius=10, fill=card)

        name = datetime.strptime(days[i], "%Y-%m-%d").strftime("%a")
        draw_text_center(draw, mid_x, 320, name, FONTS['24'],
                         THEME['fg'] if i == 0 else THEME['muted'])
        draw_icon(img, mid_x - 24, 350, get_weather_icon(d_codes[i], 1), (48, 48), THEME['fg'])

        hi = f"{math.floor(d_max[i] + 0.5)}°"
        lo = f"{math.floor(d_min[i] + 0.5)}°"
        hi_w, _ = text_size(draw, hi, FONTS['24'])
        lo_w, _ = text_size(draw, lo, FONTS['20'])
        gap = 8
        t_x = mid_x - (hi_w + gap + lo_w) // 2
        draw.text((t_x, 398), hi, font=FONTS['24'], fill=THEME['fg'])
        draw.text((t_x + hi_w + gap, 402), lo, font=FONTS['20'], fill=THEME['muted'])


def widget_clock_panel(img, draw, col, dt, gmail_unread, updated_at):
    x = inner_x(col)
    right = inner_right(col)

    draw_text_center(draw, (x + right) // 2, 6, dt.strftime("%H:%M"), FONTS['clock'], THEME['accent'])

    draw.text((x, 168), dt.strftime("%d %B %Y"), font=FONTS['32'], fill=THEME['fg'])
    draw_text_right(draw, right, 168, dt.strftime("%a").upper(), FONTS['32'], THEME['muted'])

    hline(draw, x, right, 214)

    # Gmail
    has_mail = gmail_unread > 0
    draw_icon(img, x, 230, "icon_mail", (60, 60), THEME['accent'] if has_mail else THEME['muted'])
    draw.text((x + 80, 242), f"Unread Inbox: {gmail_unread}", font=FONTS['35'],
              fill=THEME['fg'] if has_mail else THEME['muted'])

    hline(draw, x, right, 316)

    # Status footer: "Updated" + IP on one row, gesture hint on its own line
    # below so they can't overlap.
    stamp = datetime.fromtimestamp(updated_at).strftime("%H:%M") if updated_at else "--:--"
    draw.text((x, 332), f"Updated {stamp}", font=FONTS['24'], fill=THEME['muted'])
    draw_icon(img, x, 366, "icon_wifi", (24, 24), THEME['muted'])
    draw.text((x + 34, 368), get_local_ip(), font=FONTS['20'], fill=THEME['muted'])
    draw_text_right(draw, right, 402, "tap: refresh · hold: theme · 5s: settings", FONTS['16'], THEME['line'])


# --- Screensaver (moving clock on black; prevents burn-in without an OSD) ---
# The panel has no backlight control, so "lowest brightness" is done in software:
# very dim text on black. It stays readable up close but emits little light. The
# dashboard renders at full brightness, so touching the screen restores normal.
SS_TIME_COLOR = (46, 66, 96)
SS_DATE_COLOR = (40, 46, 58)
SS_SPEED = 55.0  # drift speed, px/sec


def render_screensaver(pos):
    """Black frame with a time/day/date block at pos. Returns (img, w, h)."""
    dt = datetime.now()
    img = Image.new('RGB', (SCREEN_W, SCREEN_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    t_str = dt.strftime("%H:%M:%S")
    d_str = dt.strftime("%A  %d %B %Y").upper()
    tf, df = FONTS['ss_clock'], FONTS['32']
    tb = draw.textbbox((0, 0), t_str, font=tf)
    db = draw.textbbox((0, 0), d_str, font=df)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    dw, dh = db[2] - db[0], db[3] - db[1]
    gw, gap = max(tw, dw), 18
    gh = th + gap + dh
    x, y = int(pos[0]), int(pos[1])
    draw.text((x + (gw - tw) // 2 - tb[0], y - tb[1]), t_str, font=tf, fill=SS_TIME_COLOR)
    draw.text((x + (gw - dw) // 2 - db[0], y + th + gap - db[1]), d_str, font=df, fill=SS_DATE_COLOR)
    return img, gw, gh


def screensaver_start():
    gw, gh = 480, 150  # rough size; refined by the first frame's bounce
    pos = [float(random.randint(0, max(1, SCREEN_W - gw))),
           float(random.randint(0, max(1, SCREEN_H - gh)))]
    ang = random.uniform(0, 2 * math.pi)
    return pos, [SS_SPEED * math.cos(ang), SS_SPEED * math.sin(ang)]


def screensaver_step(pos, vel, gw, gh, dt_s):
    """Advance and bounce the block; a small random turn on each bounce makes
    the path wander so it covers more of the panel over time."""
    pos[0] += vel[0] * dt_s
    pos[1] += vel[1] * dt_s
    bounced = False
    if pos[0] < 0:
        pos[0], vel[0], bounced = 0.0, abs(vel[0]), True
    elif pos[0] + gw > SCREEN_W:
        pos[0], vel[0], bounced = float(SCREEN_W - gw), -abs(vel[0]), True
    if pos[1] < 0:
        pos[1], vel[1], bounced = 0.0, abs(vel[1]), True
    elif pos[1] + gh > SCREEN_H:
        pos[1], vel[1], bounced = float(SCREEN_H - gh), -abs(vel[1]), True
    if bounced:
        a = random.uniform(-0.35, 0.35)
        vx, vy = vel
        vel[0] = vx * math.cos(a) - vy * math.sin(a)
        vel[1] = vx * math.sin(a) + vy * math.cos(a)


def _music_hit(pos):
    """Return the music transport control at pos ('prev'/'playpause'/'next'), or None."""
    px, py = pos
    for x0, y0, x1, y1, action in MUSIC_HITS:
        if x0 <= px <= x1 and y0 <= py <= y1:
            return action
    return None


def draw_hold_hint(img, frac):
    """Overlay a progress banner while the user holds toward the 5s settings gesture."""
    draw = ImageDraw.Draw(img)
    bw, bh = 560, 88
    x0 = (SCREEN_W - bw) // 2
    y0 = (SCREEN_H - bh) // 2
    draw.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=14,
                           fill=THEME['bg'], outline=THEME['accent'], width=3)
    draw_text_center(draw, SCREEN_W // 2, y0 + 12, "Keep holding for Settings",
                     FONTS['28'], THEME['fg'])
    bx, by, bwid = x0 + 30, y0 + 56, bw - 60
    draw.rounded_rectangle((bx, by, bx + bwid, by + 16), radius=8, outline=THEME['line'])
    fillw = int(bwid * min(max(frac, 0.0), 1.0))
    if fillw > 4:
        draw.rounded_rectangle((bx, by, bx + fillw, by + 16), radius=8, fill=THEME['accent'])


# --- SCREEN COMPOSITION ---
def render_screen():
    img = Image.new('RGB', (SCREEN_W, SCREEN_H), THEME['bg'])
    draw = ImageDraw.Draw(img)

    if not data_store.lock.acquire(timeout=2.0):
        return img
    try:
        weather = data_store.weather.copy()
        aqi = data_store.aqi
        strava = data_store.strava.copy()
        printer = data_store.printer.copy()
        rob = data_store.roborock.copy()
        gmail_unread = data_store.gmail_unread
        spotify = data_store.spotify.copy()
        claude = data_store.claude.copy()
        antigravity = data_store.antigravity.copy()
        sysload = dict(data_store.sysload, history=list(data_store.sysload['history']))
        market = {k: dict(v) for k, v in data_store.market.items()}
        ping = dict(data_store.ping, history=list(data_store.ping['history']))
        location = data_store.location.copy()
        updated_at = data_store.updated_at
    finally:
        data_store.lock.release()

    dt = datetime.now()

    # Column dividers
    for col_x in COL_X[1:]:
        draw.line((col_x, 12, col_x, BOTTOM_Y + 4), fill=THEME['line'], width=2)

    # --- COLUMN 1: 3D printer (top) + markets (bottom) ---
    c1 = inner_x(0)
    if ENABLE_STRAVA:
        widget_strava(img, draw, c1, ROW1_Y, strava)
    else:
        widget_bambu(img, draw, c1, ROW1_Y, printer)
    hline(draw, c1, inner_right(0), ROW_RULE_Y)
    widget_markets(img, draw, c1, ROW2_Y, market)

    # --- COLUMN 2: home / AI usage ---
    c2 = inner_x(1)
    if ENABLE_ROBOROCK:
        widget_roborock(img, draw, c2, ROW1_Y, rob)
    elif ENABLE_ANTIGRAVITY:
        widget_antigravity(img, draw, c2, ROW1_Y, antigravity)
    else:
        widget_music(img, draw, c2, ROW1_Y, bt.music_snapshot() if bt else None)
    hline(draw, c2, inner_right(1), ROW_RULE_Y)
    if ENABLE_CLAUDE:
        widget_claude(img, draw, c2, ROW2_Y, claude)
    elif ENABLE_SPOTIFY:
        widget_spotify(img, draw, c2, ROW2_Y, spotify)
    else:
        widget_time_progress(img, draw, c2, ROW2_Y, dt)

    # --- COLUMN 3: weather (full height) ---
    widget_weather(img, draw, 2, weather, aqi, location.get('label', ''))

    # --- COLUMN 4: clock, mail, status ---
    widget_clock_panel(img, draw, 3, dt, gmail_unread, updated_at)

    return img


# Noto Sans CJK, for song/artist names that contain Chinese/Japanese/Korean
# (the Aldrich display font is Latin-only). First existing path wins; falls
# back to Aldrich if the CJK font isn't installed (e.g. on a dev box).
_CJK_FONT_PATHS = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
]


def load_fonts():
    def load(name, size):
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)

    def load_cjk(size):
        for p in _CJK_FONT_PATHS:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        return load('Aldrich-Regular.ttc', size)

    return {
        '16': load('Aldrich-Regular.ttc', 16),
        '20': load('Aldrich-Regular.ttc', 20),
        '24': load('Aldrich-Regular.ttc', 24),
        '28': load('Aldrich-Regular.ttc', 28),
        '32': load('Aldrich-Regular.ttc', 32),
        '35': load('Aldrich-Regular.ttc', 35),
        '40': load('Aldrich-Regular.ttc', 40),
        '60': load('Aldrich-Regular.ttc', 60),
        '80': load('Aldrich-Regular.ttc', 80),
        '96': load('Aldrich-Regular.ttc', 96),
        'clock': load('advanced_led_board-7.ttc', 170),
        'ss_clock': load('advanced_led_board-7.ttc', 192),  # screensaver clock (2x)
        'cjk': load_cjk(30),
        'cjk_sm': load_cjk(23),
    }


FONTS = {}


def start_threads(roborock_user_data):
    global bt
    bt = bluetooth_music.BtMusic("Pi_Dashboard")
    bt.start()

    t_data = threading.Thread(target=update_data_thread, daemon=True)
    t_data.start()

    # Watchdog: re-exec if the data thread wedges on a stuck network call so the
    # dashboard recovers on its own instead of freezing stale for hours.
    threading.Thread(target=data_watchdog_thread, daemon=True).start()

    # Daily auto-restart to shake off the Wi-Fi drop seen after long uptimes.
    threading.Thread(target=auto_restart_thread, daemon=True).start()

    if ENABLE_ROBOROCK:
        t_robo = threading.Thread(target=roborock_update_thread,
                                  args=(roborock_user_data, ROBOROCK_CONF['EMAIL']),
                                  daemon=True)
        t_robo.start()


def parse_args():
    p = argparse.ArgumentParser(description="Pi Dashboard for the GeeekPi 11.26\" 1920x440 HDMI LCD")
    p.add_argument('--preview', metavar='PNG', nargs='?', const='preview.png',
                   help="Render a single frame to a PNG and exit (no display needed)")
    p.add_argument('--windowed', action='store_true',
                   help="Run in a window instead of fullscreen (for desktop testing)")
    p.add_argument('--theme', choices=('dark', 'light'), default='dark')
    return p.parse_args()


# --- MAIN LOOP ---
def main():
    global FONTS

    args = parse_args()
    if args.theme != THEME_NAME:
        toggle_theme()

    load_runtime_settings()  # apply on-screen-saved ZIP over the code default
    FONTS = load_fonts()

    if args.preview:
        out = args.preview if os.path.isabs(args.preview) else os.path.join(BASE_DIR, args.preview)
        render_screen().save(out)
        print(f"Wrote {out}")
        return

    auth_strava()
    auth_claude()
    auth_antigravity()
    roborock_user_data = auth_roborock(ROBOROCK_CONF['EMAIL'])

    try:
        screen = display_backend.Display(SCREEN_W, SCREEN_H, fullscreen=not args.windowed)
    except display_backend.DisplayUnavailable as e:
        logging.critical(f"Cannot open the LCD: {e}")
        logging.critical("Try `python3 main.py --preview` to check rendering without a display.")
        return

    # Idle -> moving-clock screensaver (see SCREENSAVER_SECONDS). `activity['t']`
    # (monotonic) is bumped by touches; ScreenPower is kept only for its kernel
    # touch reader, which feeds note_activity as a backup to pygame's events.
    # The proximity sensor is a third feed: bumping the timer is all it takes to
    # wake, since the loop leaves the screensaver as soon as it stops being idle.
    # `by_touch` records what bumped it last, so the wake guard below can tell a
    # touch-wake (which must swallow the touch that caused it) from a proximity
    # wake (which has no touch to swallow).
    activity = {'t': time.monotonic(), 'by_touch': True}

    def note_activity():
        activity['t'] = time.monotonic()
        activity['by_touch'] = True

    def note_proximity():
        activity['t'] = time.monotonic()
        activity['by_touch'] = False

    power = display_backend.ScreenPower(on_activity=note_activity)
    power.start()

    global global_sensor
    sensor = None
    if PROXIMITY_ENABLED:
        sensor = proximity.ProximitySensor(
            PROXIMITY_TRIGGER_PIN, PROXIMITY_ECHO_PIN,
            threshold_cm=PROXIMITY_WAKE_CM, trigger_mode=PROXIMITY_TRIGGER_MODE,
            poll_interval_s=PROXIMITY_POLL_INTERVAL_S, on_detect=note_proximity)
        global_sensor = sensor   # lets the settings menu retune it live
        sensor.start()

    start_threads(roborock_user_data)

    settings_ctx = build_settings_ctx()
    last_key = None
    last_forced = 0.0
    wake_time = 0.0
    frames = 0
    menu = None            # settings_ui.SettingsUI instance while open, else None
    last_menu_draw = 0.0
    saver = False          # screensaver active?
    ss_pos, ss_vel = [0.0, 0.0], [0.0, 0.0]
    ss_last = 0.0

    try:
        while True:
            force = False
            now_m = time.monotonic()

            # The data watchdog asks for a restart here rather than re-execing
            # from its own thread, so the display/SDL teardown happens on the
            # main thread where it started.
            if data_restart_request.is_set():
                if sensor is not None:
                    sensor.close()
                screen.close()
                _reexec("data thread watchdog")

            for kind, pos in screen.poll():
                if kind == display_backend.QUIT:
                    raise KeyboardInterrupt
                note_activity()

                # A touch during the screensaver only wakes it; swallow the
                # action so it doesn't also refresh / toggle / hit a control.
                if saver:
                    saver = False
                    wake_time = now_m
                    last_key = None
                    force = True
                    continue
                if now_m - wake_time < WAKE_GUARD_SECONDS:
                    force = True
                    continue

                if menu is not None:
                    # In settings, every touch is routed to the on-screen menu.
                    if kind in (display_backend.TAP, display_backend.LONG_PRESS,
                                display_backend.SETTINGS):
                        if menu.handle_tap(pos[0], pos[1]) == 'exit':
                            menu = None
                            last_key = None  # repaint the dashboard
                    force = True
                    continue

                if kind == display_backend.SETTINGS:
                    logging.info("Entering settings")
                    menu = settings_ui.SettingsUI(FONTS, THEME, SCREEN_W, SCREEN_H,
                                                  settings_ctx)
                    force = True
                elif kind == display_backend.LONG_PRESS:
                    toggle_theme()  # icon cache holds colour-independent masks, no need to clear
                elif kind == display_backend.TAP and (music_action := _music_hit(pos)):
                    # Tap landed on a music transport control -> drive the phone.
                    if bt:
                        if music_action == 'playpause':
                            bt.play_pause()
                        elif music_action == 'next':
                            bt.next()
                        elif music_action == 'prev':
                            bt.previous()
                elif time.time() - last_forced > TAP_REFRESH_COOLDOWN:
                    # Rate-limited so repeated taps cannot hammer the upstream APIs.
                    logging.info("Touch: forcing data refresh")
                    data_store.force_refresh()
                    last_forced = time.time()
                force = True

            # --- Settings mode: render the menu/sub-screen ---
            if menu is not None:
                if menu.dirty or (menu.animating and now_m - last_menu_draw > 0.25):
                    try:
                        screen.show(menu.render())
                        menu.dirty = False
                        last_menu_draw = now_m
                    except Exception as e:
                        logging.error(f"Settings UI render error: {e}")
                screen.tick(EVENT_POLL_FPS)
                continue

            # --- Enter / exit the screensaver ---
            idle = SCREENSAVER_SECONDS and (now_m - activity['t'] > SCREENSAVER_SECONDS)
            if idle and not saver:
                saver = True
                ss_pos, ss_vel = screensaver_start()
                ss_last = 0.0
            elif not idle and saver:
                saver = False
                # Guard the pygame touch that follows a touch-wake. A proximity
                # wake has no such touch, so don't make the user tap twice.
                wake_time = now_m if activity['by_touch'] else 0.0
                last_key = None        # repaint the dashboard

            if saver:
                if now_m - ss_last >= SCREENSAVER_FRAME_S:
                    dt_s = (now_m - ss_last) if ss_last else SCREENSAVER_FRAME_S
                    ss_last = now_m
                    try:
                        img, gw, gh = render_screensaver(ss_pos)
                        screen.show(img)
                        del img
                        screensaver_step(ss_pos, ss_vel, gw, gh, dt_s)
                    except Exception as e:
                        logging.error(f"Screensaver error: {e}")
                screen.tick(EVENT_POLL_FPS)
                continue

            # A hold heading toward the 5s settings gesture: show a progress hint.
            hold = screen.hold_seconds()
            hinting = hold >= 2.5
            if hinting:
                force = True

            # Re-render only when something visible changed: the minute, the data
            # revision, the theme, or a touch. While music is playing, add a
            # per-second tick so the progress bar advances.
            if bt:
                _snap = bt.music_snapshot()
                music_key = (_snap['status'], _snap['title'],
                             int(now_m) if _snap['status'] == 'playing' else 0)
            else:
                music_key = None
            key = (datetime.now().strftime('%H:%M'), data_store.rev, THEME_NAME, music_key)
            if force or key != last_key:
                try:
                    image = render_screen()
                    if hinting:
                        draw_hold_hint(image, hold / display_backend.SETTINGS_HOLD_SEC)
                    screen.show(image)
                    del image
                    last_key = None if hinting else key
                    frames += 1
                    if frames % 60 == 0:
                        gc.collect()
                except OSError as e:
                    if e.errno == 24:  # too many open fds - restart clean
                        _reexec("FD exhaustion")
                    logging.error(f"Render error: {e}")
                except Exception as e:
                    logging.error(f"Unexpected error in main: {e}")

            screen.tick(EVENT_POLL_FPS)

    except KeyboardInterrupt:
        pass
    finally:
        if sensor is not None:
            sensor.close()
        screen.close()


if __name__ == '__main__':
    main()
