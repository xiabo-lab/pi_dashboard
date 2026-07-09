# Pi Ultrawide LCD Dashboard

A fully functional dashboard running on a Raspberry Pi. Designed for the **GeeekPi 11.26" 1920x440 HDMI LCD** (capacitive touch), this project aggregates essential daily information and smart home status into a clean, minimalist interface.

> **Migrated from e-paper.** This dashboard previously drove a Waveshare 10.85" SPI e-Paper HAT (1360x480, 1-bit). It now renders in full colour to an ordinary HDMI panel via pygame/SDL. See [Migrating from the e-paper build](#migrating-from-the-e-paper-build).

## Key Features

* **Ultrawide 4-column layout:** Tuned for the panel's 1920x440 letterbox aspect.
* **Full colour + light/dark themes:** Values are colour-coded (green / amber / red) by severity instead of the old 1-bit black-on-white.
* **Capacitive touch:** Tap anywhere to force an immediate data refresh; press and hold to toggle the light/dark theme.
* **(NEW!) Antigravity usage data:** Displays usage data for Antigravity, showing the limit, and limit reset time.
* **Claude Code usage data:** Displays usage data for Claude Code, showing the daily limit, weekly limit, and limit reset time.
* **Weather & Air Quality:** Real-time temperature, wind direction/speed, UV index, 4-day forecast (weekday, condition icon, high/low — today's card highlighted), and AQI (with visual inversion for high pollution levels) using the Open-Meteo API. **Location** is resolved in priority order (first that succeeds wins): `LOCATION_ZIP` (a postal code, resolved via zippopotam.us — most precise; Open-Meteo's own geocoder can't do ZIPs), then `LOCATION_CITY` (a place name geocoded via Open-Meteo, e.g. `'Santa Clara, CA'`), then public-IP geolocation if `USE_IP_LOCATION` is on (follows the Pi to new networks automatically, but a VPN/ISP can misplace it), then the hardcoded `LOCATION_LAT/LON` fallback. Ships unpinned (`LOCATION_ZIP = ''`, `USE_IP_LOCATION = True`) so it follows the Pi; to pin it, set `LOCATION_ZIP` in `main.py`, or better, set it on-screen (hold 5s → Settings → Zip Code), which saves to the gitignored `settings.json` and keeps your location out of the repo. The resolved place is shown on the weather panel.
* **Strava Integration:** Displays total and yearly activity statistics (distance and ride counts), including specific breakdowns for biking and hiking.
* **Bambu Lab 3D Printer:** Live monitoring of print status, completion percentage, remaining time, and current layer progress.
* **Roborock Vacuum:** Live battery level, current status, and tracking for cleaned area during active cleaning.
* **Spotify:** Displays the currently playing track and artist.
* **Gmail:** Tracks the number of unread emails in your primary inbox.
* **Markets widget:** BTC, S&P 500, and gold (US $/oz), each with its daily % change, in column 1's lower slot. Data comes from Yahoo Finance (no API key). Column 1's upper slot shows the Bambu Lab 3D printer (below); other columns fall back to Ping / Time-progress demos when their integrations are off.
* **Optimized Rendering:** A frame is only drawn when something visible actually changes (the clock minute, a data update, a touch, or a theme switch). The event loop stays at 30 Hz so touch remains responsive without burning CPU on redundant redraws.

<img width="2400" height="1792" alt="dashboard_primary" src="https://github.com/user-attachments/assets/20be2eae-4a06-48e2-9ad4-efcba00dcb7f" />
<img width="2400" height="1792" alt="dashboard_fallback" src="https://github.com/user-attachments/assets/158d65ee-9a12-4f09-a9d3-ea66ca3055bc" />

---

## Prerequisites & Installation

### Hardware
* Raspberry Pi Zero 2W (tested), with a **mini-HDMI → HDMI** adapter
* [GeeekPi 11.26" 1920x440 HDMI LCD, capacitive touch](https://wiki.52pi.com/index.php/11.26-inch-1920x440-Capacitive-Touch-Screen)
* HDMI cable + USB cable (the USB link carries the touch panel, which enumerates as a standard USB HID touchscreen — no driver needed)

**Use a Raspberry Pi OS _Lite_ (64-bit, console-only) image.** A "with desktop" image runs its own Wayland compositor (`labwc`) that holds the screen and fights `cage` for it, so the dashboard never appears. If you must use a desktop image, switch the Pi to boot to console with `sudo systemctl set-default multi-user.target`.

### 1. System Setup
SPI is **no longer required** — the panel is a plain HDMI display.

> ⚠️ **Do not force an HDMI mode.** The panel advertises its native 1920x440 over EDID. Adding `hdmi_timings`/`hdmi_mode` to `config.txt`, or `video=HDMI-A-1:…` to `cmdline.txt`, gives a **black screen**. Let KMS pick the EDID mode and remove any such override you may have added.

Verify the mode the Pi actually picked:

`fbset -s` — you should see `1920x440`.

Install system dependencies. The dashboard renders through `cage`, a single-app Wayland kiosk compositor, with `seatd` supplying the seat:

```bash
sudo apt update
sudo apt install -y python3-pygame python3-pil python3-requests \
                    python3-numpy cage seatd git tmux
sudo systemctl enable --now seatd
```

`seatd` must be enabled explicitly on a Lite image, or `cage` fails with a `libseat`/seat error.

### 2. Python Dependencies
Bookworm enforces PEP 668, so system packages come from `apt` (above). The remaining pure-Python packages install into a `--user` site or a venv:

`pip3 install --break-system-packages google-api-python-client google-auth-httplib2 google-auth-oauthlib aiomqtt roborock`

*Note: `bambulabs_api` library already included in this package.*

### 3. Display Backend
Rendering lives in `display.py`. It composes each frame with Pillow and blits it through pygame/SDL. Under `cage`, SDL auto-selects its **wayland** driver — the code deliberately does not force one.

> ⚠️ **Do not set `SDL_VIDEODRIVER=kmsdrm`.** It fails to start on this Pi Zero 2W + bar-panel combination. SDL's own auto-detection is correct here; the explicit probe list in `display.py` is only a fallback for other hardware.

Because the app needs a graphical seat, **it cannot be run from a plain SSH session** — start it via systemd (below). For layout checks over SSH, use `--preview`, which needs no display at all.

If the panel comes up at a mode other than 1920x440, frames are scaled to fit and a warning is logged.

---

## Configuration & Widget Setup

All widget toggles and API configurations are located at the top of the `main.py` script. You can enable or disable specific widgets using the `ENABLE_*` boolean variables.

> **Headless note.** The Pi runs the dashboard under systemd + cage with no keyboard or browser, so the interactive OAuth flows below **cannot** be done on the Pi. Authenticate on your **desktop** (where a browser and Python are available), then copy the resulting credential file to the Pi and restart the service. This is why the `ENABLE_*` flags must be turned on and authenticated *before* the service is enabled.

### Claude Code
The Claude usage widget shows your 5-hour and 7-day limits. Auth is a browser OAuth flow that writes `claude_creds.json`.

**On your desktop:**
1. Set `ENABLE_CLAUDE = True` in `main.py`.
2. Run the interactive flow directly:
   ```bash
   python -c "import claude; claude.interactive_auth()"
   ```
3. It prints an authorization URL. Open it, log in with your Claude account, and you'll be redirected to a dead `localhost:18924/callback?code=...` page.
4. Copy the **full** URL from the address bar and paste it back at the prompt. It writes `claude_creds.json`.

**Then deploy to the Pi:**
```bash
scp claude_creds.json raspberrypi.local:~/Pi_dashboard/
ssh raspberrypi.local "sudo systemctl restart pi-dashboard"
```
The service runs `claude.py` every 10 minutes to refresh usage; the token self-renews via its refresh token, so this is a one-time step.

### Strava
1. Go to your Strava API Settings and create an API Application.
2. Note down your **Client ID** and **Client Secret**.
3. Run the `main.py` script from the terminal for the first time.
4. The script will pause, ask for your ID/Secret, and print an authorization URL in the console. 
5. Open that URL in your browser, click "Authorize", and you will be redirected to a dead `localhost` page.
6. Copy the `code=...` portion from your browser's address bar and paste it back into the terminal. The script will automatically fetch and save the required `activity:read_all` tokens to `strava_token.json`.

### Roborock
1. Open `main.py` and input your Roborock account email address in the `ROBOROCK_CONF` dictionary.
2. Run the script from the terminal.
3. The script will request an OTP (One-Time Password) which will be sent to your email.
4. Enter the 6-digit code in the terminal. The script will securely save your session data locally.

### Bambu Lab 3D Printer
**You DON'T need to enable "LAN Mode" on your Bambu Lab printer to access local data.**
1. On your printer's screen, go to **Settings -> Network**.
2. Note your printer's **IP Address**, **Serial Number**, and **Access Code**. (Force on your router to map exact IP address)
3. Copy `device_conf.example.json` to `device_conf.json` and fill in the `printer` block with these local credentials. That file is gitignored, so the credentials stay out of the repo; without it the printer widget simply stays offline.

### Spotify (via Last.fm)
Since the official Spotify API requires running a local web server for complex token renewals, this dashboard uses Last.fm to fetch the current playing track reliably form Spotify. It's is transparent and working method.
1. Connect your Spotify account to Last.fm.
2. Create a Last.fm API account to generate an **API Key**.
3. Update `LASTFM_CONF` in the script with your API Key and Last.fm Username.
   
**After configuration, you no longer need to use the Last.fm service, and a paid Last.fm account is not required. You can continue to use only the Spotify service.**

### Gmail
The Gmail widget shows your unread inbox count (read-only access). There is no `ENABLE_GMAIL` flag — the widget activates automatically once a valid `token.json` is present. `main.py` only *reads* that token; a one-time helper, `gmail_auth.py`, creates it.

**In the Google Cloud Console (one-time):**
1. Create a project and enable the **Gmail API**.
2. Configure the **OAuth consent screen** (External), and under **Test users** add your own Gmail address — otherwise Google returns `access_denied`.
3. Create an **OAuth 2.0 Client ID** of type **Desktop app**. Download the JSON, rename it to `credentials.json`, and place it next to `gmail_auth.py`.

**On your desktop:**
```bash
pip install google-auth-oauthlib google-api-python-client
python gmail_auth.py
```
A browser opens; grant read-only access. It writes `token.json` and prints your unread count to confirm it works.

**Then deploy to the Pi:**
```bash
scp token.json raspberrypi.local:~/Pi_dashboard/
ssh raspberrypi.local "sudo systemctl restart pi-dashboard"
```
The token refreshes itself from then on. `credentials.json` stays on your desktop — the Pi only needs `token.json`.

---

## Running the Dashboard

The dashboard needs a graphical seat (`cage`), so it **cannot** be launched from a plain SSH session — run it from systemd, which also starts it automatically on boot.

```bash
sudo cp ~/Pi_dashboard/pi-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-dashboard
journalctl -u pi-dashboard -f      # watch it start
```

A healthy start logs `Display ready: 1920x440 via SDL driver 'wayland'`.

> The unit's `ExecStart` hard-codes `/home/pi/Pi_dashboard`. If your Pi user differs, edit the path — otherwise the service restart-loops with `can't open file … No such file or directory`. Check with:
> `grep ExecStart /etc/systemd/system/pi-dashboard.service`

**Authenticate the integrations _before_ enabling the service.** The `auth_*` functions call `input()`, which a systemd service cannot answer — it would fail on every boot. Flip the `ENABLE_*` flags in `main.py`, run `python3 main.py --preview` once interactively to complete each OAuth flow, then enable the service. With all flags `False` (the default) the dashboard boots straight into the fallback widget set and needs no credentials.

To redeploy after a code change:

```bash
sudo systemctl restart pi-dashboard
```

### Command-line options

| Flag | Purpose |
| --- | --- |
| `--preview [file.png]` | Render one frame to a PNG and exit. Needs no display — useful for checking layout over SSH. |
| `--windowed` | Run in a window instead of fullscreen (desktop testing). |
| `--theme {dark,light}` | Starting theme. Default `dark`. |

### Touch & keyboard

| Gesture | Action |
| --- | --- |
| Tap | Force an immediate refetch of all data (rate-limited to once every 15s) |
| Press & hold (~1.2s) | Toggle light / dark theme |
| Press & hold **5s** | Open the **Settings menu** (a progress bar fills as you hold) |
| Tap during the screensaver | Return to the dashboard (this touch only wakes; it does not also refresh/toggle) |
| `R` / `T` / `S` / `Esc`,`Q` | Refresh / toggle theme / settings / quit (when a keyboard is attached) |

### Bluetooth music widget

Column 2's upper slot shows a **now-playing** widget (`bluetooth_music.py`): pair a phone (the Pi's Bluetooth name is **`Pi_Dashboard`**) and it reads the current track over BlueZ AVRCP — song, artist, play/pause, and a live progress bar — while the phone plays from YouTube Music (or any app). No audio backend is needed: the metadata rides the AVRCP control channel, which the Pi connects explicitly. Adapted from the sibling [carlyrics](https://github.com/xiabo-lab) project. Song/artist render with Noto Sans CJK (`fonts-noto-cjk`) so non-Latin names show. Tap the on-screen prev / play-pause / next controls to drive playback. Pair via **Settings → Bluetooth → Pair New Phone**. Requires `python3-dbus-next` and `bluez-tools` (bt-agent, deployed as `bt-agent.service` for headless "Just Works" pairing). A phone's AVRCP device otherwise pops a mouse cursor on screen; `99-pidash-ignore-avrcp-pointer.rules` (copy to `/etc/udev/rules.d/`) tells libinput to ignore it.

### Settings menu (on-screen, no keyboard needed)

Hold the screen for **5 seconds** to open Settings — useful for changes when you can't SSH in. It's a full-screen menu (`settings.py`, rendered with Pillow) with five sub-screens; each tile shows a live subtitle (current SSID, paired phone, ZIP, version):

* **WiFi** — scan nearby networks (signal % + lock icon for secured), tap one, type the password on an **on-screen keyboard** (letters / `?123` symbols / Shift), and **Connect**. Uses `nmcli`, so NetworkManager saves it and reconnects on boot. Before each connect it clears any stale/partial saved profile for that SSID (matched by SSID, not profile name) so a retry can't fail with `key-mgmt: property is missing`. (Lives in `wifi_setup.py`.)
* **Bluetooth** — Pair New Phone (makes the adapter discoverable as `Pi_Dashboard`), and a list of paired phones each with a Forget button.
* **Zip Code** — a numeric keypad to set the weather ZIP. Saved to `settings.json` (which overrides the code default on startup) and applied immediately — the weather re-resolves without a restart.
* **Account** — shows the connected **Claude** account (name + plan + email, via the OAuth profile endpoint) and **Google** account (Gmail address).
* **Firmware** — app version, hostname, IP, current WiFi, Python version, uptime.

`Close` returns to the dashboard. Everything runs as root under the service, so no sudo prompt; the settings menu (like WiFi) requires the app to be running under `cage` (it can't be driven over SSH).

### Screensaver (idle)

After `SCREENSAVER_SECONDS` (default **600** = 10 min) with no touch, the dashboard is replaced by a **moving-clock screensaver** — a time / day / date block drifting and bouncing on a black background — and the first touch returns to the dashboard. **The dashboard keeps running underneath**, so it repaints instantly with fresh data on touch. Set `SCREENSAVER_SECONDS = 0` to keep the dashboard on permanently.

Why a moving clock rather than actually powering the panel off: this GeeekPi LCD exposes **no backlight control** (`/sys/class/backlight` is empty), ignores `vcgencmd display_power` under KMS, and **does not support HDMI-CEC** (it NACKs CEC commands). Cutting the HDMI signal (`wlr-randr --off`) doesn't put it to sleep either — it just shows a "No Signal" OSD with the backlight still lit. So there is no software way to turn this panel's backlight off; the drifting clock is the best available option — it avoids the OSD and prevents burn-in from static content, though the backlight stays on. For true power-off you'd need a hardware switch (smart plug / GPIO relay) on the panel's power.

> This needs `wlr-randr` (`sudo apt install wlr-randr`) and the app running under `cage` as root (both already true for the systemd unit). If `wlr-randr` or the touch device isn't found, the feature disables itself and the screen simply stays on — it will never get stuck dark.

## How It Works

The dashboard is built on a robust, multi-threaded architecture designed to keep the UI responsive and prevent hardware lockups.

* **Asynchronous Data Fetching:** Instead of fetching all data sequentially, the script spawns dedicated background threads. Each service (Weather, Strava, Roborock, Bambu Lab, etc.) pulls data asynchronously at its own specific interval. This ensures that a slow API response or a temporary network drop from one service will never block the others or freeze the system.
* **Change-Driven Rendering:** The main loop polls for touch at 30 Hz but only composes and blits a frame when the visible state changes. Every data write bumps a revision counter on the shared store; the renderer compares `(clock minute, revision, theme, dim state)` against the last frame and skips the redraw when they match. On an unchanged screen the loop costs almost nothing.

**Important Notes:**

* **Initial Data Population Delay:** When you first launch the script, you will notice that the widgets may show placeholders or zeros, and the full array of data takes a few minutes to completely appear on the screen. This is an intentional design choice to stagger initial network requests. It prevents sudden spikes in CPU usage, avoids overwhelming the Raspberry Pi's network stack, and respects the rate limits of the external APIs.
* **Panel burn-in / backlight life:** LCDs do not ghost like e-paper, but a static dashboard left on around the clock can cause image retention. The idle **screensaver** described above (moving clock after 10 min, tune `SCREENSAVER_SECONDS`) prevents that. It can't reduce backlight hours, though — this panel has no software backlight/power control (see that section).

## Migrating from the e-paper build

If you are coming from the Waveshare 10.85" version:

* **`display.py` replaces the old e-paper driver.** The `epd10in85` driver, its `.so` blobs, and the SPI setup are no longer imported by anything. They've been moved to `Reference/waveshare_epd/` (off the import path) for rollback; delete `Reference/` once you are happy with the LCD.
* **Geometry changed** from 1360x480 to 1920x440. The panel is 560px wider but 40px shorter, so the layout moved from 3 columns to 4 (activity/finance · home & AI usage · weather · clock/mail/status) and each column's vertical budget was tightened.
* **Colour replaces 1-bit.** Icons are now loaded as alpha masks and painted in whatever colour the theme specifies, so the same `icons/*.bmp` files are reused unmodified. Mask polarity is detected per icon (most are dark-on-light, but `icon_wifi.bmp` ships light-on-dark). Spotify album art is kept as full-colour RGB instead of being dithered to 1-bit.
* **`signal.SIGALRM` hardware watchdog removed.** It existed to recover from the e-paper's SPI busy-wait hangs; an HDMI blit cannot hang that way.
* **Refresh cadence removed.** The old 60-second floor was an e-ink hardware constraint, not a design choice.

## The 3d printed case

The case below was designed for the Waveshare 10.85" panel and does **not** fit the GeeekPi 11.26" LCD. It is kept here for reference for anyone still running the e-paper build.

You can download the case stl files [here](https://makerworld.com/en/models/2322517-epaper-dashboard-waveshare-10-85).

## Video assembly guide

Assembly guide for the original e-paper build:

[![Video Title](https://img.youtube.com/vi/H964RpaJvu0/0.jpg)](https://youtu.be/H964RpaJvu0)
(Youtube clickable)

