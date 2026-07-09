# Reference — archived / dead code

Files here are **not used** by the running dashboard. They are kept only for
reference and possible rollback, and are not on the Python import path.

| Item | What it is | Why it's dead |
|------|------------|---------------|
| `waveshare_epd/` | The patched Waveshare 10.85" SPI e-paper driver (`epd10in85`, `epdconfig`, `.so` blobs). | The dashboard migrated to the GeeekPi 11.26" HDMI LCD; rendering now goes through `display.py` (pygame/SDL). Nothing imports this. |
| `Font.ttc` | An unused bundled font. | Not referenced by `load_fonts()` (the app uses `Aldrich-Regular.ttc` and `advanced_led_board-7.ttc`). |

Safe to delete entirely once you're confident you won't roll back to the
e-paper panel.
