# M5Stack PaperColor (C151) — Technical Reference Manual

**Device:** M5Stack PaperColor · SKU **C151** · ESP32-S3R8 · 4" E Ink Spectra 6 (E6) · 400×600 native / 600×400 landscape
**Document type:** Technical Reference Manual (TRM) — consolidated hardware, firmware and system-architecture reference
**Purpose:** Ground-truth context document for AI-assisted development (Claude Code) of firmware + server backend
**Compiled:** 2026-08-24 · **Sources:** M5Stack official docs, M5GFX/M5Unified/M5PM1 source, ESP-IDF docs, E Ink/Good Display/Waveshare panel docs, ESPHome source, community projects

---

## 0. How to use this document

Place this file in your project repository. For Claude Code, either:

- save it as `CLAUDE.md` in the repo root (auto-loaded as project context), or
- keep it as `HARDWARE.md` beside `CLAUDE.md` (what this repository does).

**Confidence markers used throughout:**

| Marker | Meaning |
|---|---|
| ✅ **VERIFIED** | Confirmed on an official datasheet, official doc page, or vendor source code |
| ⚠️ **DERIVED** | Logically follows from verified facts, or measured by a credible third party — not vendor-stated |
| ❌ **UNVERIFIED** | Could not be confirmed. Do not rely on it; measure or read the schematic |

**Rule for the AI agent:** never invent GPIO numbers, register addresses, or API names. If something is marked ❌, treat it as an open question and ask rather than guess.

---

## 1. Device identification — read this first

M5Stack sells **four** similar-looking e-paper boards. They are mutually incompatible in software. The AliExpress listing "M5Paper Color ESP32-S3 Development Kit, 4-Inch SPECTRA 6, 600×400" is **PaperColor / C151**.

| | **PaperColor (C151)** ← this device | M5PaperS3 (C139) | M5Paper (K049) | PaperMono (C153) |
|---|---|---|---|---|
| MCU | ESP32-S3R8 | ESP32-S3R8 | ESP32-D0WDQ6-V3 (LX6) | ESP32-S3R8 |
| Panel | ED2208-DOA / **EL040EF1**, 4.0" | ED047TC1, 4.7" | ED047TC1, 4.7" | SSD1677, 3.97" |
| Resolution | **400×600** native | 960×540 | 540×960 | 480×800 |
| Colour | **Spectra 6, 6 colours, no greyscale** | 16-level grey | 16-level grey | 4-level grey |
| Touch | **NONE** | GT911 | GT911 | FT6336G |
| RTC | **RX8130CE @ 0x32** | BM8563 @ 0x51 | BM8563 @ 0x51 | — |
| PMIC | **M5PM1 @ 0x6E** | PMS150G + LGS4056H | discrete | — |
| Panel bus | **SPI** | 8-bit parallel | parallel | — |
| Library | **M5Unified + M5GFX + M5PM1** | M5Unified (+epdiy ≤0.2.6) | M5EPD | M5Unified |
| Status | current | **EOL** | legacy | new |

✅ VERIFIED — [docs.m5stack.com/en/core/PaperColor](https://docs.m5stack.com/en/core/PaperColor), [PaperS3](https://docs.m5stack.com/en/core/PaperS3), [M5Paper](https://docs.m5stack.com/en/core/m5paper), [PaperMono](https://docs.m5stack.com/en/core/PaperMono)

> **Consequence:** any tutorial, library or snippet written for "M5Paper" or "M5PaperS3" is wrong for this board. `M5EPD` does not apply. `epdiy` does not apply. GT911 touch code does not apply.

**Identity constants**

| Item | Value |
|---|---|
| Official name | PaperColor |
| SKU | C151 |
| EAN | 6972934176486 |
| MSRP | 75.00 USD (shop.m5stack.com) |
| Docs | https://docs.m5stack.com/en/core/PaperColor · https://docs.m5stack.com/en/products/sku/C151 |
| Factory firmware | https://github.com/m5stack/M5PaperColor-UserDemo (MIT) |
| Board enum (M5GFX) | `board_M5PaperColor = 28` |
| Arduino board name | `M5PaperColor` |
| Zephyr board | `m5stack_paper_color` |
| MicroPython board | `M5STACK_PaperColor`, USB VID `0x303A` / PID `0x816B` |
| Release | ⚠️ DERIVED ≈ mid-May 2026 (schematic rev V0.5 dated 2026-04-24; press 2026-05-15) |

---

## 2. Specifications

✅ VERIFIED verbatim from [docs.m5stack.com/en/core/PaperColor](https://docs.m5stack.com/en/core/PaperColor)

| Spec | Value |
|---|---|
| SoC | **ESP32-S3R8**, Xtensa LX7 dual-core, 240 MHz |
| Flash | 16 MB (external; the R8 die has no embedded flash) |
| PSRAM | **8 MB Octal (OPI)** — in-package die |
| SRAM | 512 KB internal (Espressif SoC spec) |
| Wi-Fi | 2.4 GHz 802.11 b/g/n |
| Bluetooth | ⚠️ DERIVED: BLE 5.0 (silicon supports it; **M5 docs never mention Bluetooth**) |
| Display | 4" E-Paper E6 full-colour, **ED2208-DOA (EL040EF1)**, 400×600 |
| Input power | USB Type-C, DC 5 V |
| Battery | **1250 mAh** (⚠️ chemistry/nominal voltage not stated; Li-Po assumed) |
| Audio codec | ES8311 |
| Microphone | MEMS mic + **ES7210** audio ADC with integrated AEC |
| Speaker | 1 W @ 8 Ω, "2520", **AW8737A** amplifier |
| Temp/Humidity | **SHT40** |
| Storage | microSD (SPI mode) |
| RTC | **RX8130CE** |
| Buttons | 3× user + 1× power |
| Power consumption | **Standby 92.53 µA · Full load 211.97 mA** |
| Product size / weight | 70.8 × 103.9 × 8.5 mm · 73.3 g |
| Package contents | 1× PaperColor **only** — no cable, no case, no stand |

**Not present on this board:** touchscreen, IMU, IR receiver, LoRa, NFC, Ethernet, Port B, Port C, battery-ADC GPIO, power-hold GPIO, mounting holes (❌ undocumented).

---

## 3. Pinout and bus topology

✅ VERIFIED — official PinMap cross-checked against `M5GFX/src/M5GFX.cpp` (`board_M5PaperColor` branch) and `M5Unified/src/M5Unified.cpp`.

### 3.1 ESP32-S3 GPIO map

| GPIO | Net | Function |
|---|---|---|
| **G1** | USER_KEY1 | **Button C** (active-low) |
| **G2** | SYS_SCL / AUDIO_I2C_SCL | **I²C clock — single shared internal bus** |
| **G3** | SYS_SDA / AUDIO_I2C_SDA | **I²C data** — ⚠️ also an ESP32-S3 strapping pin |
| **G4** | PORT.A yellow | Grove GPIO / factory UART console **RX** |
| **G5** | PORT.A white | Grove GPIO / factory UART console **TX** |
| **G7** | RTC_IRQ | RX8130CE interrupt → ESP32 (ext0 wake capable) |
| **G9** | USER_KEY2 | **Button B** |
| **G10** | USER_KEY3 | **Button A** |
| **G11** | EINK_BUSY | EPD busy, **active-low** (0 = busy) |
| **G12** | EINK_RST | EPD reset |
| **G13** | SPI_MOSI | shared: EPD + microSD |
| **G14** | SPI_MISO | microSD only (EPD is write-only) |
| **G15** | SPI_CLK | shared: EPD + microSD |
| **G19 / G20** | USB D− / D+ | fixed by ESP32-S3 silicon (not in M5's table) |
| **G21** | RGB | 2× addressable RGB LED, single data line |
| **G38** | I2S_DSDIN | ES8311 audio out (speaker path) |
| **G39** | I2S_SDOUT | ES7210 audio in (mic path) |
| **G40** | I2S_BCLK | I²S bit clock (shared) |
| **G41** | I2S_LRCK | I²S word clock (shared) |
| **G42** | I2S_MCLK | I²S master clock (shared) |
| **G43** | EINK_DC | EPD data/command |
| **G44** | EINK_CS | EPD chip select |
| **G45** | AUDIO_PWR_EN | enables `CODEC_3V3_L3B` rail (ES8311 + ES7210) |
| **G46** | SPK_EN | AW8737A amplifier enable |
| **G47** | SD_CS | microSD chip select |
| **G48** | IR_TX | infrared transmit (TX only) |

### 3.2 M5PM1 PMIC GPIOs — separate pin domain

⚠️ These are **PMIC pins, not ESP32 GPIOs**. They are only reachable over I²C.

| PM1 pin | Net | Function |
|---|---|---|
| **PYG0** | PY_EPD_EN | **E-paper panel power enable** (off at cold boot) |
| **PYG1** | CARD_DEC | microSD card detect |
| **PYG2** | RTC_IRQ | RX8130CE IRQ → PMIC — **the wake source** |
| **PYG3** | PY_SD_PWR_EN | microSD power enable |
| **PYG4** | PY_SD_DET_EN | microSD detect-circuit enable |
| DCDC3V3_EN_PP | PY_MPWR_EN | `3V3_L2` main rail |
| LDO3V3_EN_PP | PY_RGB_PWR_EN | RGB LED rail |
| BOOST5V_EN_PP | PY_GROVE_OUT_EN | Grove Port A 5 V output |

### 3.3 Buses

**One I²C bus** on **SCL = G2, SDA = G3**, five devices:

| Device | Address |
|---|---|
| ES8311 audio codec | `0x18` |
| RX8130CE RTC | `0x32` |
| ES7210 mic ADC | `0x40` |
| SHT40 temp/humidity | `0x44` |
| M5PM1 PMIC | `0x6E` |

M5's docs split this into "SYS" and "AUDIO" tables, but both use G2/G3 — physically one bus. Exposed as `M5.In_I2C`. PMIC default 100 kHz (400 kHz supported); M5Unified instantiates the RTC at 400 kHz.

**One SPI bus** (`SPI2_HOST`, mode 0, `spi_3wire = true`, **4 MHz write clock**) shared by EPD and microSD:

```
CLK  G15  ──┬── EPD  (CS G44, DC G43, BUSY G11, RST G12)
MOSI G13  ──┤
MISO G14  ──┴── microSD (CS G47)
```

> ⚠️ **Contention:** refreshing the panel while streaming from SD on the same bus will conflict. Serialise them.

**Grove PORT.A** — the only expansion connector (HY2.0-4P):

| Wire | Signal |
|---|---|
| Black | GND |
| Red | **5 V**, software-gated (`PY_GROVE_OUT_EN` boost) |
| Yellow | **G4** |
| White | **G5** |

> ⚠️ **Two traps on Port A.** (1) M5Unified's external-I²C table maps `scl = G5, sda = G4`, i.e. **white = SCL, yellow = SDA** — inverted relative to the usual M5 Grove-A convention. Verify before wiring an I²C unit. (2) The factory firmware routes its **UART console** to `TX = G5, RX = G4`; a Grove device there will see console traffic unless you disable it.

---

## 4. Display — E Ink Spectra 6 (E6)

This is the component that dictates the entire system architecture. Read all of §4 before designing anything.

### 4.1 Panel facts

| Property | Value | Status |
|---|---|---|
| M5 designation | ED2208-DOA (module), **EL040EF1** (E Ink glass) | ✅ |
| Native geometry | **400 wide × 600 tall (portrait)** | ✅ |
| Landscape use | `setRotation(1)` or `(3)` → 600×400 | ✅ |
| Active area | 84.6 × 56.4 mm | ⚠️ vendor (Good Display / E Ink) |
| Resolution density | ~180 PPI | ⚠️ vendor |
| Colours | **Black, White, Yellow, Red, Blue, Green** — exactly 6 | ✅ |
| Greyscale | **None.** Tones only via dithering | ✅ |
| Controller | All-in-one driver + TCON + PMIC + temp sensor **inside the panel**; part number **not published** | ✅ presence / ❌ part |
| Interface | SPI, write-only + DC/BUSY/RST | ✅ |
| **Full refresh** | **15–30 s** ("depending on the complexity of the colour distribution" — M5). **Measured 35.3 s** on unit `28:84:85:43:e3:08`, 2026-09-09, room temperature, six-colour setup screen — i.e. *above* the vendor range, so budget for 40 s, not 30 | ✅ vendor / ✅ measured |
| Partial refresh | **Assume NONE.** See §4.5 | ⚠️ |
| Operating temp | **0–50 °C** (storage −25 to 60 °C) | ⚠️ vendor |
| Bistable | Image persists at zero power; panel standby < 0.01 µA | ✅ |
| Refresh-cycle lifetime | **No published figure** | ❌ |

### 4.2 Colour codes — the single most error-prone detail

Framebuffer is **4 bpp, 2 pixels per byte, high nibble = left (even-x) pixel**, row-major, top row first, no padding.

```
0x0 = Black
0x1 = White
0x2 = Yellow
0x3 = Red
0x4 = (unused / undefined — never emit)
0x5 = Blue
0x6 = Green
0x7 = (unused)
```

✅ VERIFIED identically in three independent implementations: Waveshare's official `epd4in0e.py` (branch **`Development`**, not `master`), M5GFX `Panel_ED2208.cpp`, and ESPHome `epaper_spi_spectra_e6.cpp`.

> ❗ **Known-bad source:** the Waveshare *wiki* page for the 4" E lists `green=0x2, blue=0x3, red=0x4, yellow=0x5`. That is the **ACeP / 7-colour (E7)** ordering, copy-pasted in error. Trust the driver source, not the wiki. Likewise, ACeP palettes (which include **orange**) must never be reused here — E6 has no orange ink.

**Frame size at 400×600:** `400 × 600 / 2 = 120 000 bytes` exactly.

### 4.3 Command set

Classic UC81xx-family opcodes with an E6-specific `0xAA` unlock prefix. ✅ VERIFIED across M5GFX, Waveshare and ESPHome (byte-identical init sequences).

| Cmd | Name | Cmd | Name |
|---|---|---|---|
| `0xAA` | CMDH unlock (`49 55 20 08 09 18`) | `0x30` | PLL |
| `0x00` | PSR — panel setting | `0x50` | CDI — VCOM/data interval |
| `0x01` | PWRR — power setting | `0x60` | TCON |
| `0x02` | POF — power off | `0x61` | TRES — resolution |
| `0x03` | POFS — power-off sequence | `0x70` | REV |
| `0x04` | PON — power on | `0x82`/`0x84` | VDCS / T_VDCS |
| `0x05`/`0x06`/`0x08` | BTST1/2/3 booster | `0x83` | partial window (see §4.5) |
| `0x07` | DSLP — deep sleep (`0xA5`) | `0xE3` | PWS — power saving |
| `0x10` | DTM — data start | | |
| `0x12` | DRF — display refresh (`0x00`) | | |

**Init sequence** (verbatim from Waveshare `epd4in0e.py::init()`, mirrored by `Panel_ED2208::getInitCommands()`):

```
HW reset: RST=1 (20 ms) → RST=0 (2 ms) → RST=1 (20 ms)
wait BUSY high; delay 30 ms
0xAA 49 55 20 08 09 18   # CMDH unlock
0x01 3F                  # PWRR
0x00 5F 69               # PSR
0x05 40 1F 1F 2C         # BTST1
0x08 6F 1F 1F 22         # BTST3
0x06 6F 1F 17 17         # BTST2
0x03 00 54 00 44         # POFS
0x60 02 00               # TCON
0x30 08                  # PLL
0x50 3F                  # CDI
0x61 01 90 02 58         # TRES = 400 x 600
0xE3 2F                  # PWS
0x84 01                  # T_VDCS
wait BUSY high
```

**Frame + refresh:**

```
0x10, <120000 bytes>      # DTM
0x04                      # PON
wait BUSY; delay 200 ms
0x06 6F 1F 17 27          # BTST2 re-tune — note 0x27 (the 4" needs this, the 7.3" does not)
delay 200 ms
0x12 00                   # DRF → BUSY low for ~15-20 s
wait BUSY
0x02 00                   # POF
wait BUSY; delay 200 ms
```

**Sleep:** `0x07 0xA5`, then ≥ 2 s before cutting power. After deep sleep the panel ignores data until fully re-initialised.

**BUSY polling:** active-low (`0` = busy). M5GFX: `input_pullup`, poll every 10 ms, **20 s timeout**, then a fixed 200 ms settle delay.

**Waveform/LUT:** **OTP only** — there is no LUT upload command. The waveform is baked into the panel and selected via PSR/CDI/BTST.

### 4.4 Panel-care rules — enforce these in firmware

⚠️ Vendor guidance (Waveshare 4"/7.3" manuals), **not restated by M5**, but the failure mode is described as permanent:

1. **≥ 180 s between refreshes.** (ESPHome's validator only enforces 30 s — that is not conservative enough.)
2. **Refresh at least once every 24 h.** Leaving one image up indefinitely is the documented burn-in path.
3. **Deep-sleep (`0x07 0xA5`) or power-cut the panel immediately after every refresh.** "Otherwise the screen may be damaged permanently by staying in a high-voltage state for a long time." On PaperColor the panel rail is switchable via M5PM1 `PYG0`, so a true power-down is available — use it.
4. **Below ~15 °C expect colour cast.** Waveshare's remedy is 6 h at 25 °C. Gate refreshes on the on-board SHT40 if the device lives somewhere cold.
5. Bend the FPC along the horizontal axis only.

### 4.5 Partial refresh — treat as unavailable

- Historically, 6/7-colour panels have **no** partial addressing and no partial waveform (GxEPD2 maintainer, [discussion #161](https://github.com/ZinggJM/GxEPD2/discussions/161)).
- In March 2026 an undocumented **`0x83` partial-window command** was disclosed for Good Display's 7.3" **GDEP073E01** and implemented in GxEPD2 v1.6.8. The maintainer notes: *"My other 6 or 7 colour displays don't know the 0x83 command, it is simply ignored."*
- **For the 4" ED2208-DOA: ❌ UNTESTED.** M5GFX never emits `0x83`; it always transfers the full frame.
- Even where it works, the **refresh duration is unchanged** (GxEPD2 sets `partial_refresh_time = full_refresh_time = 15000 ms`).

**Design assumption: every update is a full, flashing, 15–30 s refresh.** Never put a seconds clock on this panel.

### 4.6 Palettes

Three palettes matter; use the right one for the right job.

**(a) Saturated / "ideal"** — what Waveshare's driver ships, index order matches the native nibble codes:

```python
PAL = (  0,  0,  0,    # 0 black
       255,255,255,    # 1 white
       255,255,  0,    # 2 yellow
       255,  0,  0,    # 3 red
         0,  0,  0,    # 4 UNUSED filler (duplicate of black)
         0,  0,255,    # 5 blue
         0,255,  0)    # 6 green
```

**(b) M5GFX on-device palette** — use this if server output must match what the device itself renders:

`black (0,0,0)` · `white (255,255,255)` · `yellow (255,243,56)` · `red (191,0,0)` · `blue (100,64,255)` · `green (67,138,28)`

**(c) Measured / reflective** — what the panel actually looks like. **Use this for photographic work.** From the `epaper-dithering` package (`SPECTRA_7_3_6COLOR_V2`):

| Colour | RGB | Hex |
|---|---|---|
| black | (31, 24, 41) | `#1F1829` |
| white | (168, 180, 182) | `#A8B4B6` |
| yellow | (180, 173, 0) | `#B4AD00` |
| red | (113, 24, 19) | `#711813` |
| blue | (36, 70, 139) | `#24468B` |
| green | (50, 84, 60) | `#32543C` |

> Note how far "white" is from `#FFFFFF` (~72 % luminance) and "black" from `#000000`. Quantising against idealised RGB is the #1 cause of muddy output. Independent confirmation from `epdoptimize`: calibrated black ≈ `#1F2226`, white ≈ `#B9C7C9`.

**Workflow:** dither against palette (c) with a **6-entry** palette in order `[black, white, yellow, red, blue, green]`, then remap the index through `[0,1,2,3,5,6]` before nibble-packing. Match in **Lab**, not RGB — RGB distance collapses dark green and dark blue into black.

### 4.7 Dithering — content-dependent, not one-size-fits-all

| Content | Method | Why |
|---|---|---|
| **Text, thin lines, icons** | **No dithering.** Hard-quantise to an exact palette entry | At ~180 PPI, dithered glyph edges become unreadable noise |
| **Solid UI fills, chart areas, logos** | **50 % checkerboard** on a colour pair | A study of 8 methods found the plain checkerboard beat everything for graphical primitives; error diffusion and rotated Bayer "struggle with small-scale graphical primitives" |
| **Photos, gradients** | **Floyd–Steinberg** (default) or **Atkinson** (portraits/faces — higher contrast, less mud) | |
| **Smoothest gradients** | Jarvis–Judice–Ninke / Stucki | Heaviest |
| **Flat UI, deterministic output** | Ordered / Bayer 8×8 | No edge smear, but visible geometric artefacts |

**Region-tagged dithering is the correct pattern for a dashboard:** dither photo regions, hard-quantise text/UI regions, composite text *after* the error-diffusion pass. A single global Floyd–Steinberg over a dashboard smears every text edge.

**Extending the palette:** two-ink checkerboards reliably produce **orange, purple, brown, cyan, lime, light pink, light yellow**. The eye needs a **14×14 to 20×20 px** block for a blend to read as a solid colour — use these for backgrounds and fills, never for small icons or text.

**Poor-contrast pairs (against the measured palette):** yellow-on-white (near-identical luminance, effectively unreadable), green-on-black, blue-on-black, green-vs-blue at small sizes. **Safe pairs:** black/white, red/white, blue/white, green/white, black/yellow.

> ✅ **blue-on-white confirmed on hardware, 2026-09-09, down to 14 px** — but only
> as an *undithered* solid. The distinction is the whole point: rendered through
> a ditherer that matches against the measured palette, the same DEVICE_RGB blue
> `(100,64,255)` breaks into a ~59/41 blue-white checkerboard, because its
> lightness sits outside the measured gamut and the diffuser has to mix in white
> to reach it. Photographed side by side: via the factory demo's own quantiser
> the 18 px heading was barely legible; via our firmware with
> `setEpdMode(epd_fastest)` (→ `_dither_row_none`, bias-free nearest colour) the
> 14 px metadata line reads cleanly. One line of firmware separates the two.

**M5GFX's on-device dither modes** (`epd_mode_t` — note: these change *dithering only*, never refresh speed):

| Mode | Algorithm | Strength |
|---|---|---|
| `epd_fastest` | none (nearest colour) | — |
| `epd_fast` | 16×16 Bayer, per-pixel-pair | 70 |
| `epd_text` | pseudo-random per-channel RGB, pair-optimised | 70 |
| `epd_quality` *(default)* | same as `epd_text` | 140 |

> The names are misleading: `epd_text` applies **more** dithering than `epd_fastest`. **For text use `epd_fastest`.** M5's own factory firmware toggles to `epd_quality` only around photo slideshows.

---

## 5. Peripherals

### 5.1 RTC — Epson RX8130CE (I²C `0x32`)

The RTC is the linchpin of the low-power design. ✅ VERIFIED via M5 docs, M5GFX autodetect probe list, and `M5Unified/src/utility/rtc/RX8130_Class.hpp` (`DEFAULT_ADDRESS = 0x32`).

**Chip capabilities** ([Epson](https://www.epsondevice.com/crystal/en/products/rtc/rx8130ce.html)):

- Alarm on day/date/hour/minute/second
- Wake-up timer, 16-bit, **244 µs to 7.5 years**
- Time-update interrupt: **every second or every minute**
- **Auto power switching** (VDD/VBAT/VIO) + **backup-battery charge control**; backup current 300 nA typ @ 3 V
- I²C Fast-Mode 400 kHz, timekeeping down to 1.1 V

**M5Unified wrapper** (`m5::RX8130_Class`, selected automatically for this board — there is **no** `RTC8130` class and **no** `RTC8563` on this board):

```cpp
bool     M5.Rtc.isEnabled();
bool     M5.Rtc.getDateTime(rtc_date_t*, rtc_time_t*);
void     M5.Rtc.setDateTime(const tm*);
uint32_t M5.Rtc.setTimerIRQ(uint32_t timer_msec);
int      M5.Rtc.setAlarmIRQ(const tm*);          // also (date&,time&) overloads
void     M5.Rtc.setSystemTimeFromRtc(struct timezone* tz = nullptr);
bool     M5.Rtc.getIRQstatus();  void clearIRQ();  void disableIRQ();
bool     M5.Rtc.getVoltLow();
```

Timer ranges implemented by `setTimerIRQ`: ~16 s (4096 Hz) · ~17 min (64 Hz) · ~18 h (1 Hz) · ~45 days (1/60 Hz) · ~1 y 3 m (1/3600 Hz).
Registers touched: time `0x10–0x16` (BCD, week one-hot), alarm `0x17–0x19`, timer `0x1A–0x1C`, flags `0x1D`, enables `0x1E`.
The per-second/per-minute *time-update* interrupt is **not** wrapped — drive `0x1E` UIE / `0x1C` USEL yourself if needed.

**Wiring — two independent paths, and the difference matters:**

| Path | Destination | Effect |
|---|---|---|
| 1 | **ESP32 G7** (`_rtcIntPin`) | `esp_sleep_enable_ext0_wakeup(G7, false)` → wakes the ESP32 from **deep sleep**. 3V3 rail stays up. This is what `M5.Power.timerSleep()` uses. |
| 2 | **M5PM1 PYG2**, configured `GPIO_FUNC_WAKE`, pull-up, falling edge | RTC pulls nIRQ low → **PMIC restores power to a fully powered-off board** → ESP32 **cold-boots**. |

> ❗ Unlike the old M5Paper (where the BM8563 latched its own power off), **PaperColor's power gating lives in the M5PM1, not the RTC.** Path 2 is the one you want (see §6).

### 5.2 SHT40 (I²C `0x44`)

Temperature + humidity, on the shared bus. Practical use here: gate panel refreshes below ~15 °C (§4.4).

### 5.3 Audio

- **ES8311** codec (`0x18`) — playback. I²S: MCLK G42, LRCK G41, BCLK G40, DSDIN G38.
- **ES7210** ADC (`0x40`) — 4-channel with **hardware AEC**. I²S SDOUT G39 (shares MCLK/LRCK/BCLK).
- **AW8737A** amp, enable **G46**. Speaker 1 W @ 8 Ω.
- **G45** gates the `CODEC_3V3_L3B` rail for both codecs — leave it off when audio is unused.
- MEMS mic part number ❌ not published.

### 5.4 IR, RGB, microSD, buttons

| Item | Detail |
|---|---|
| **IR** | Transmit only, **G48**. No receiver. LED part ❌ unpublished. |
| **RGB LED** | **2 LEDs, one data line G21**, rail gated by `PY_RGB_PWR_EN`. Addressable (M5PM1's NeoPixel engine supports 1–32). Exact type (WS2812 vs SK6812) ❌ unpublished — do not assume. |
| **microSD** | **SPI mode only** (not SDMMC). CS G47, bus shared with EPD. Power/detect via PM1 PYG1/PYG3/PYG4. Max capacity ❌ undocumented. |
| **Buttons** | `M5.BtnA` = **G10**, `M5.BtnB` = **G9**, `M5.BtnC` = **G1**, all **active-low**, `INPUT` mode, polled via `M5.update()`. ⚠️ Community reports advise verifying the physical↔GPIO mapping on your own unit. Button presses **during a refresh are dropped** (polling). |
| **Power button** | ON / OFF / RESET / BOOT, on the **left side**. Wired to the **M5PM1**, not to an ESP32 GPIO. Single press = on/restart, double press = off, hold during USB connect = download mode. Exact durations ❌ unpublished. |

---

## 6. Power architecture — the most important chapter for battery life

### 6.1 M5PM1 PMIC (I²C `0x6E`)

M5Stack's own PMIC. It owns charging, every power rail, the power button, a wake timer, and 32 bytes of retained RAM.

**Rail hierarchy:** `L0/L1` (PMIC + RTC, battery-only) → `L2/L3A` (ESP32-S3 + sensors) → `L3B` (peripherals). Individually switchable: **EPD, microSD, RGB rail, Grove 5 V, audio codec, speaker amp.**

**Standalone `M5PM1` library API** (✅ verified in `M5PM1.h`):

```cpp
m5pm1_err_t timerSet(uint32_t seconds, m5pm1_tim_action_t action);  // max 214 748 364 s
// actions: STOP=0b000, FLAG=0b001, REBOOT=0b010, POWERON=0b011, POWEROFF=0b100
m5pm1_err_t timerClear();
m5pm1_err_t sysCmd(m5pm1_sys_cmd_t);   // OFF=0x01, RESET=0x02, DL=0x03 (download mode)
m5pm1_err_t shutdown();  reboot();  enterDownloadMode();
m5pm1_err_t getWakeSource(uint8_t* src, m5pm1_clean_t);
m5pm1_err_t readVbat(uint16_t* mv);
m5pm1_err_t writeRtcRAM(...); readRtcRAM(...);   // 32 bytes, survives power-off
```

**Wake-source flags:** `TIM 0x01` · `VIN 0x02` · `PWRBTN 0x04` · `RSTBTN 0x08` · `CMD_RST 0x10` · `EXT_WAKE 0x20` · `5VINOUT 0x40`.

**Raw registers** (needed for ESPHome/Zephyr/bare IDF, ✅ verified from working community configs):

| Reg | Purpose |
|---|---|
| `0x06` | **PWR_CFG** — bits: charge / DCDC / LDO / **boost** (the ~15 V e-ink rail). **Auto-clears on every reset — must be re-set each boot.** |
| `0x07` | HOLD_CFG — set bits 0, 3, 5 to survive USB unplug |
| `0x09` | set to `0x00` to disable PMIC I²C idle-sleep (else the PMIC goes unresponsive mid-sequence) |
| `0x0A` | watchdog (`0x00` = off) |
| `0x0C` | `0xA1` = SYS_CMD_OFF |
| `0x10/0x11/0x13/0x16` | GPIO mode / output / push-pull / function |
| `0x38–0x3D` | wake timer |
| `0x49` bit0 / `0x4A` bit0 | single-press-reset / double-press-off behaviour |

**M5Unified's `M5PM1_Class`** exposes `setExtOutput`, `setLDOOutput`, `setDCDCOutput`, GPIO control, `setBatteryCharge/getBatteryCharge/setChargeCurrent/setChargeVoltage/isCharging`, `getVBUSVoltage`, `getBatteryVoltage`, `powerOff()` — **but not the wake timer.** For `timerSet()` you need the standalone M5PM1 library.

### 6.2 Battery API (M5Unified)

```cpp
int32_t  M5.Power.getBatteryLevel();     // %
int16_t  M5.Power.getBatteryVoltage();   // mV, via M5PM1 over I2C (no ADC pin)
is_charging_t M5.Power.isCharging();
void     M5.Power.powerOff();
void     M5.Power.timerSleep(int seconds);            // RTC alarm + ESP32 deep sleep
void     M5.Power.deepSleep(uint64_t us, bool touch_wakeup = true);
void     M5.Power.lightSleep(uint64_t us, bool touch_wakeup = true);
void     M5.Power.setLed(uint8_t brightness = 255);
```

Charging detection in M5's own example: `const bool hasVbus = (vbusVoltage > 1000);`
⚠️ **`M5.Power.setBatteryCharge()` is a silent no-op on this board** — explicit early return with the comment *"M5PaperColor does not support charge control"*. Use the M5PM1 `PWR_CFG 0x06 bit0` path instead.
❌ Charge current for C151 is **not published**. Do not quote the 650/200 mA figures from the Stamp-S3Bat M5PM1 page.

Calibration points from a community discharge study: **3350 mV = 0 %, 4160 mV = 100 %**. Factory firmware hard-shutdowns below **3100 mV**.

### 6.3 Sleep strategies — measured, and the conclusion is decisive

| Strategy | Current | Battery life (1250 mAh) |
|---|---|---|
| ESP32 deep sleep (`M5.Power.deepSleep` / `timerSleep`) | **~5–10 mA** measured on this board (~17 mV/h droop) | **~1.8 days** |
| **M5PM1 full shutdown** (`SYS_CMD_OFF`) | **~92 µA** — matches the datasheet standby figure (~2.15 mV/h droop) | ~560 days idle floor |
| ESP32-S3-WROOM module alone, deep sleep | 8.14 µA (Espressif, bare module — not this board) | — |

**Conclusion: do NOT use ESP32 deep sleep on this board.** The always-on rails (EPD boost, LDO, SD, LEDs, PMIC housekeeping) leak milliamps regardless of how long you sleep. Both `M5.Power.deepSleep()` and `timerSleep()` end at `esp_deep_sleep_start()` and inherit that leak.

**Measured real-world battery life with PMIC shutdown** (community, ESPHome, 3 discharge runs):

| Wake interval | Life per charge |
|---|---|
| 30 min | **~20–22 days** (19.97 d and 22.25 d measured) |
| 30 min + overnight skip (01:00–06:00) | **~26.3 days** (+18 %) |
| 15 min | ~1.5–2 weeks (≈ 8× better than ESP32 deep sleep) |

### 6.4 The canonical low-power cycle

M5Stack's own factory firmware pattern:

```cpp
// 1. schedule the wake
struct tm wake = now_plus_minutes(N);
M5.Rtc.setAlarmIRQ(&wake);

// 2. arm the PMIC to accept the RTC's nIRQ as a wake edge (once, at boot)
pm1.gpioSetFunc(M5PM1_GPIO_NUM_2, M5PM1_GPIO_FUNC_WAKE);
pm1.gpioSetPull(M5PM1_GPIO_NUM_2, M5PM1_GPIO_PULL_UP);
pm1.gpioSetWakeEdge(M5PM1_GPIO_NUM_2, M5PM1_GPIO_WAKE_FALLING);
pm1.gpioSetWakeEnable(M5PM1_GPIO_NUM_2, true);

// 3. clear stale flags, persist state, cut all power
pm1.getWakeSource(&src, M5PM1_CLEAN_ALL);
M5.Rtc.clearIRQ();
pm1.timerClear();
nvs_commit(...);                 // NOTHING else survives
pm1.sysCmd(M5PM1_SYS_CMD_OFF);   // all rails dead
while (true) { delay(1000); }    // never reached
```

**Alternative without the RTC** — the PMIC's own timer:

```cpp
pm1.timerSet(seconds, M5PM1_TIM_ACTION_POWERON);
pm1.shutdown();
```

On the next boot, branch on the wake source:

```cpp
pm1.getWakeSource(&src, M5PM1_CLEAN_ALL);
bool rtc_wake = (src & M5PM1_WAKE_SRC_EXT_WAKE) != 0;
bool timer_wake = (src & M5PM1_WAKE_SRC_TIM) != 0;
bool button_wake = (src & M5PM1_WAKE_SRC_PWRBTN) != 0;
```

**State persistence across a PMIC shutdown:**

| Store | Survives? |
|---|---|
| RAM / PSRAM | ❌ no — cold boot |
| `RTC_DATA_ATTR` (ESP32 RTC slow memory) | ❌ no — the ESP32 is unpowered |
| **NVS (flash)** | ✅ yes — the primary store |
| **M5PM1 RTC RAM (32 bytes)** | ✅ yes — cheap counters/flags |
| **The panel image itself** | ✅ yes — bistable, zero power |

> ❗ **While PMIC-shutdown, no ESP32 GPIO can wake the device** — the ESP32 has no power. Only the PMIC power button or a PMIC wake source works. Buttons B/C will *not* wake it. Draw a meaningful "sleeping" screen before shutting down; the panel keeps showing it for free.

---

## 7. Firmware stacks

| Environment | Status | Key identifier |
|---|---|---|
| Arduino IDE (M5Stack core) | ✅ full | Board `M5PaperColor` |
| Espressif `arduino-esp32` core | ❌ **no board entry** | verified absent from `boards.txt` |
| PlatformIO | ⚠️ **no board definition** | use `board = esp32s3box` |
| ESP-IDF | ✅ full | v5.5.1 |
| UIFlow2 / MicroPython | ✅ full | board `M5STACK_PaperColor` |
| ESPHome | ⚠️ works, **no device preset** | `epaper_spi` / `Spectra-E6` |
| Zephyr | ✅ | `m5stack_paper_color` |

### 7.1 Arduino

Board Manager URL:
```
https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
```
Install the **M5Stack** package (M5Stack's own ESP32 core fork, *not* Espressif's), then `Tools → Board → M5Stack → M5PaperColor`.

Libraries: `M5Unified` **≥ 0.2.14** (PaperColor support added there; latest 0.2.20), `M5GFX` **≥ 0.2.20** (tested 0.2.27), optionally `M5PM1` v1.0.6.

**Mandatory board settings:**

| Setting | Value |
|---|---|
| PSRAM | **OPI PSRAM (Enabled)** — non-negotiable, see below |
| Flash size | 16 MB |
| Flash mode | DIO |
| Partition scheme | a 16 MB layout (`default_16MB.csv` for OTA) |
| USB CDC on boot | Enabled ⚠️ recommended, not documented |

> ❗ **The #1 "my screen is blank" cause.** M5GFX contains:
> ```cpp
> #elif !defined (CONFIG_SPIRAM_MODE_OCT)
>       ESP_LOGE(LIBRARY_NAME, "M5PaperColor need OPI-PSRAM enabled");
> ```
> With the wrong PSRAM mode the board is still detected correctly, but `M5.Display` silently does nothing.

### 7.2 PlatformIO

❌ **No `m5stack-papercolor` board exists** in either `platformio/platform-espressif32` (242 boards) or `pioarduino/platform-espressif32` (299 boards). The only `m5stack_paper` entry is the 2020 **ESP32** M5Paper. Use a generic S3 board — M5Stack's own docs recommend `esp32s3box`.

```ini
[env:papercolor]
platform  = espressif32
board     = esp32s3box            ; per M5Stack docs; esp32-s3-devkitc-1 also works
framework = arduino

board_build.partitions          = default_16MB.csv
board_build.arduino.memory_type = qio_opi     ; QIO flash + OPI PSRAM — REQUIRED
board_upload.flash_size         = 16MB
board_build.flash_mode          = dio

build_flags =
    -DBOARD_HAS_PSRAM
    -DARDUINO_USB_CDC_ON_BOOT=1
    -DARDUINO_USB_MODE=1

lib_deps =
    m5stack/M5Unified @ ^0.2.20
    m5stack/M5GFX @ ^0.2.27
```

⚠️ Every individual flag above is sourced, but this exact combination is **untested** — validate on first build.

### 7.3 ESP-IDF

**ESP-IDF v5.5.1** (stated by the factory firmware). There is **no formal M5Stack BSP** — the "BSP" is the trio `M5GFX` + `M5Unified` + `M5PM1`, each shipping `idf_component.yml`. Registry deps used by the factory build: `espressif/esp_tinyusb` 2.1.1, `espressif/mdns` 1.11.1, `espressif/qrcode`, `espressif/i2c_bus` 1.5.1.

Required sdkconfig:
```
CONFIG_ESPTOOLPY_FLASHMODE_DIO=y
CONFIG_ESPTOOLPY_FLASHFREQ_80M=y
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_40M=y
CONFIG_LWIP_MAX_SOCKETS=16     # if you run the captive-portal httpd
```

Factory partition table — note **no OTA slots**:
```
nvs,      data, nvs,     0x9000,   0x6000,
phy_init, data, phy,     0xf000,   0x1000,
factory,  app,  factory, 0x10000,  0x9F0000,   # ~9.94 MB
storage,  data, fat,     0xA00000, 0x600000,   # 6 MB, exposed over USB MSC
```

> ❗ **Include order on IDF ≥ 5.3:** `#include <M5Unified.h>` **before** `#include <M5PM1.h>`, else `i2c_bus.h` and `driver/i2c.h` collide over `i2c_config_t`. (Or enable `CONFIG_I2C_BUS_BACKWARD_CONFIG`.)

### 7.4 UIFlow2 / MicroPython

Board `M5STACK_PaperColor` exists in [m5stack/uiflow-micropython](https://github.com/m5stack/uiflow-micropython) (`mpconfigboard.h`: `MICROPY_HW_I2C0_SCL (2)`, `SDA (3)`, MCU `ESP32-S3R8`). Firmware built with ESP-IDF **v5.5.4** (`make BOARD=M5STACK_PaperColor pack_all`); end users flash via **M5Burner**.

```python
import M5
from M5 import *
M5.begin({"clear_display": False})
Widgets.setRotation(1)
M5.Lcd.setEpdMode(M5.Lcd.EPDMode.EPD_FASTEST)
```

### 7.5 ESPHome — works, with one critical caveat

Mainline ESPHome has the **`epaper_spi`** component with a generic **`Spectra-E6`** model (`minimum_update_interval = 30s`). There is **no PaperColor preset** — supply `dimensions` explicitly. (A preset was proposed in [esphome PR #16031](https://github.com/esphome/esphome/pull/16031); merge status ❌ unconfirmed.) ESPHome's init sequence is byte-identical to M5GFX's, confirming the same controller.

```yaml
esp32:
  board: esp32-s3-devkitc-1
  variant: esp32s3
  framework: {type: arduino}
psram:
  mode: octal
  speed: 80MHz
logger:
  hardware_uart: USB_SERIAL_JTAG
i2c:
  sda: GPIO3
  scl: GPIO2
  frequency: 100000
spi:
  clk_pin: GPIO15
  mosi_pin: GPIO13
display:
  - platform: epaper_spi
    model: Spectra-E6
    cs_pin: GPIO44
    dc_pin: GPIO43
    busy_pin: {number: GPIO11, inverted: true, mode: {input: true, pullup: true}}
    reset_pin: GPIO12
    dimensions: {width: 400, height: 600}
    rotation: 270°
wifi:
  fast_connect: true      # skips the AP scan — saves 1-3 s per cold-boot wake
```

> ❗ **The EPD rail is off at cold boot and lives on the PMIC.** You must write M5PM1 registers at `on_boot: priority: 800` (after I²C at 900, before display at 500) or the display component hangs on BUSY forever: `0x0A=0x00` (WDT off) → `0x16` clear bits 0&3 → `0x10` set bits 0&3 → `0x13` clear bits 0&3 → `0x11` set bits 0&3 (EPD + SD rails on) → `0x06` set bits 0–3 (**charge + DCDC + LDO + boost**) → `0x07` set bits 0,3,5 (HOLD_CFG) → `0x09=0x00`.

Working references: [h0verin/ESPHome-m5stackPaperColor](https://github.com/h0verin/ESPHome-m5stackPaperColor), [PFalko/m5stack-papercolor-esphome](https://github.com/PFalko/m5stack-papercolor-esphome) (ships an `m5pm1_power` external component).

### 7.6 Zephyr

`west build -b m5stack_paper_color`. Requires `west blobs fetch hal_espressif`. Display, SHT4x and RX8130CE drivers present. Simple boot provides no OTA — select MCUboot if needed.

---

## 8. M5Unified / M5GFX API reference

All names below are ✅ verified in source (M5Unified 0.2.20, M5GFX 0.2.27).

**Detection:** runtime, no board macro. M5GFX probes I²C on G3/G2 for `0x32` (RX8130) + `0x44` (SHT40), then `_check_m5pm1()`, and logs `[Autodetect] board_M5PaperColor`. Check with `M5.getBoard() == m5::board_t::board_M5PaperColor`.

**Init (from the factory firmware `hal.cpp`):**

```cpp
#include <M5Unified.h>

auto cfg = M5.config();
cfg.clear_display = false;              // avoid a 20 s clear on every boot
M5.begin(cfg);

M5.Display.setEpdMode(epd_mode_t::epd_quality);   // epd_fastest for text-only UIs
M5.Display.setRotation(3);                        // 600x400 landscape

M5Canvas* canvas = new M5Canvas(&M5.Display);
canvas->createSprite(M5.Display.width(), M5.Display.height());
```

**Display API:**

```cpp
M5.Display.display();                  // full refresh (15-30 s)
M5.Display.display(x, y, w, h);        // dirty-rect tracked, but STILL a full refresh
M5.Display.waitDisplay();              // blocks on BUSY (20 s timeout)
bool busy = M5.Display.displayBusy();
M5.Display.setAutoDisplay(false);      // see the trap below
M5.Display.clearDisplay();             // inverse-fill + fill de-ghost cycle
M5.Display.sleep();                    // panel 0x07 0xA5
canvas->pushSprite(0, 0);
```

> ❗ **The auto-display trap.** `Panel_FrameBufferBase::init()` sets `_auto_display = true` on ESP32-S3 with PSRAM, and `Panel::endWrite()` then calls `display(0,0,0,0)`. **Every un-bracketed draw call can kick off a 15–30 s refresh.** Correct patterns: (a) draw into an `M5Canvas` and `pushSprite()` once — what M5's own firmware does; (b) wrap drawing in `startWrite()`/`endWrite()`; (c) `setAutoDisplay(false)` and call `display()` explicitly.

**Colour depth is forced.** `setColorDepth()` always yields `rgb888_3Byte`. You draw in full RGB888 into a **720 KB PSRAM framebuffer** (`400×600×3`); quantisation + dithering happen at transfer time, row by row. Budget PSRAM accordingly — a full-size sprite costs another 720 KB.

**Buttons:**

```cpp
M5.update();                       // call every loop
M5.BtnA.wasClicked();  M5.BtnA.wasHold();  M5.BtnA.wasPressed();
M5.BtnA.wasReleased(); M5.BtnA.getClickCount();
```

---

## 9. System architecture — device + backend

### 9.1 Thin client is the correct pattern

Every mature battery-powered e-ink dashboard (TRMNL, MagInkDash, Tesserae, PicPak) uses **thin client**: the device wakes, fetches a **pre-rendered image**, blits it, and sleeps. It does no layout, holds no fonts, and runs no template engine.

For a **Spectra 6** panel this is even more strongly indicated than usual, because correct quantisation needs Lab-space matching against *measured* panel colours plus whole-frame error diffusion (§4.6/4.7) — work you do not want on an ESP32 at every wake.

**Energy reality:** radio-on time and panel refresh time dominate; compute is noise. TRMNL's measurements per wake: WiFi connect > 1 s, NTP **up to 4 s or more**, API call ~1.5 s, image download 1–5 s. A fat client pays all of that *plus* local rendering.

The one thing a thin client cannot do is degrade gracefully offline — but on e-ink that is nearly free: the panel is bistable, so **doing nothing is a valid, zero-cost failure mode**. Show a small "stale since HH:MM" badge rather than a full error screen.

### 9.2 Recommended protocol: speak TRMNL BYOS

TRMNL's "Bring Your Own Server" protocol is the best-documented open contract in this space, it costs nothing to adopt, and it gives you seven ready-made server implementations. TRMNL's own firmware already contains a `GetSpectraPixel()` mapper for 6-colour panels.

**Three endpoints:** `/api/setup`, `/api/display`, `/api/log`. Requests carry `ID: <device_mac>` and `Content-Type: application/json`.

`GET /api/display` response (✅ from [byos_hanami api.adoc](https://github.com/usetrmnl/byos_hanami/blob/main/doc/api.adoc); host replaced with a documentation address):

```json
{
  "filename": "demo.png",
  "firmware_url": null,
  "firmware_version": null,
  "image_url": "http://192.0.2.13:2300/uploads/6658....png",
  "image_url_timeout": 0,
  "maximum_compatibility": false,
  "refresh_rate": 900,
  "reset_firmware": false,
  "special_function": "none",
  "temperature_profile": "default",
  "touchbar_mode": "tap",
  "update_firmware": false
}
```

`GET /api/setup` response (key replaced with a placeholder; inkwake has no setup endpoint -- see `app/store.py`):

```json
{ "api_key": "<device-api-key>",
  "image_url": "https://host/assets/setup.bmp",
  "message": "Welcome to Terminus!",
  "status": 200 }
```

**Device telemetry travels as request headers** — elegant, because it costs zero extra round-trips and the server can react in the same response:

`ACCESS_TOKEN`, `BATTERY_VOLTAGE`, `PERCENT_CHARGED`, `BATTERY_CHARGING`, `BATTERY_HEALTH`, `BATTERY_TEMP`, `BATTERY_CAPACITY`, `BATTERY_COUNT`, `BATTERY_CURRENT`, `FW_VERSION`, `HEIGHT`, `WIDTH`, `HOST`, `ID`, `IMAGE_CACHED`, `MODEL`, `REFRESH_RATE`, `RSSI`, `SENSORS`, `TEMPERATURE_PROFILE`, `UPDATE_SOURCE`, `USB_CONNECTED`, `WAKE_TIME`.

⚠️ Two spellings exist in the wild (`ACCESS_TOKEN` in the spec, `Access-Token` in captured traffic). HTTP headers are case-insensitive but the underscore/hyphen difference is real — **parse permissively**.

`POST /api/log` → **HTTP 204**. Observed fields: `message`, `level`, `wifi_status`, `firmware_version`, `battery_voltage`, `wifi_rssi_level`, `free_heap_size`, `wakeup_reason`.

**Semantics ❌ unverified:** the `special_function` enum, `image_url_timeout`, `maximum_compatibility`, `temperature_profile`, `touchbar_mode`. Also note the hosted API returns `image_name` where the BYOS spec returns `filename` — do not assume one name.

**Server implementations:** [terminus / byos_hanami](https://github.com/usetrmnl/byos_hanami) (Ruby, the reference + spec), [byos_fastapi](https://github.com/usetrmnl/byos_fastapi) (Python), [byos_django](https://github.com/usetrmnl/byos_django), [byos_next](https://github.com/usetrmnl/byos_next) (Next.js), [byos_laravel](https://github.com/usetrmnl/byos_laravel), Phoenix, Inker. Index: [awesome-TRMNL](https://github.com/eindpunt/awesome-TRMNL).

**Terminus data model is worth copying:** `models` (name, width, height, **colors**, **bit_depth**, rotation, scale_factor, css) decoupled from `devices` (model_id, playlist_id, mac_address, api_key, refresh_rate, battery_charge, **sleep_start_at**, **sleep_stop_at**), `playlists` (mode automatic/manual, ordered items), `screens` (filename, mime_type, bit_depth, width, height, uri). Adding PaperColor is a row insert: `width: 400, height: 600, colors: 6, bit_depth: 4`.

**Recommended extension** (from the PicPak/Tesserae design): split the sleep value in two — a **persisted default** `sleep_interval_s` (written to NVS, survives a server outage) plus a **per-cycle override** `next_poll_s` (lets the server say "sleep 43 s to hit the next quarter hour" or "content is static overnight, sleep 6 h").

### 9.3 Server-side rendering

| Technique | Fidelity | Cost | Use when |
|---|---|---|---|
| **Headless Chromium + Playwright/Puppeteer** | Highest — full CSS, webfonts, grid, SVG, JS charts | ~300–500 MB RSS, 0.5–2 s/render | Charts, arbitrary HTML, pixel-perfect layouts |
| **Satori/Takumi (HTML/JSX→SVG) + resvg** | Good — **flexbox subset only**, no grid/floats; fonts must be passed as buffers | tens of ms, no browser | The 95 % of screens that are text/number layouts |
| **PIL/Pillow direct drawing** | Full pixel control, manual layout arithmetic | lowest | Simple, fixed layouts; runs anywhere |
| wkhtmltoimage | ancient WebKit, no flexbox | low | ❌ deprecated — no current project uses it |

**A two-tier hybrid is the pragmatic answer:** Satori for most screens, Chromium only where it's needed.

**Font discipline is the #1 source of silent layout bugs.** Chromium substitutes silently when a font is missing from the container; Satori/resvg hard-error instead (arguably safer). At 600×400 with no antialiasing surviving quantisation, prefer heavily-hinted or pixel fonts, ≥16 px body text.

**Supersample then downsample** (render at 1200×800, scale to 600×400, then dither) smooths glyph edges before quantisation — Terminus exposes a per-model `scale_factor` field for exactly this.

### 9.4 Image pipeline

Reference implementation, Pillow → packed 4 bpp:

```python
from PIL import Image

W, H = 400, 600                       # NATIVE panel geometry (portrait)

# Measured Spectra 6 palette, in the order [K, W, Y, R, B, G]
MEASURED = [(31,24,41), (168,180,182), (180,173,0),
            (113,24,19), (36,70,139), (50,84,60)]
NATIVE   = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]      # index -> panel nibble code

pal_img = Image.new("P", (1, 1))
flat = [c for rgb in MEASURED for c in rgb]
pal_img.putpalette(flat + [0, 0, 0] * (256 - len(MEASURED)))

img = Image.open(src).convert("RGB")
if img.size == (H, W):                 # landscape source -> rotate to native
    img = img.rotate(90, expand=True)
assert img.size == (W, H)

idx = img.quantize(palette=pal_img)    # dither=FLOYDSTEINBERG by default
raw = idx.tobytes("raw")               # 1 byte per pixel, row-major

buf = bytearray(W * H // 2)
for i in range(0, len(raw), 2):
    buf[i >> 1] = (NATIVE[raw[i]] << 4) | NATIVE[raw[i + 1]]   # HIGH nibble = LEFT pixel

assert len(buf) == 120_000
```

**ImageMagick equivalent:**

```bash
# build the 6-swatch remap image once
magick -size 1x1 xc:'#1F1829' xc:'#A8B4B6' xc:'#B4AD00' \
                 xc:'#711813' xc:'#24468B' xc:'#32543C' +append e6_palette.png

magick input.jpg -resize 400x600^ -gravity center -extent 400x600 \
       -dither FloydSteinberg -remap e6_palette.png out.png

magick input.png ... +dither -remap e6_palette.png out.png    # text/UI: NO dithering
```
ImageMagick will not emit packed nibbles — do the packing in a small script.

**Ready-made libraries:** [`epaper-dithering`](https://pypi.org/project/epaper-dithering/) (Python; ships measured Spectra 6 palettes and 10 dither modes), [`epdoptimize`](https://github.com/paperlesspaper/epdoptimize) (JS; Lab matching, chroma-aware mode, lightness-range compression).

**Wire format — raw packed vs PNG:**

| | Raw packed 4 bpp | PNG |
|---|---|---|
| Size @ 600×400 | exactly 120 000 B | typically 5–15 KB |
| Device cost | zero decode, stream socket → framebuffer | inflate window + scanline buffer + palette map |
| Determinism | perfect (fixed radio-on window) | variable |

⚠️ **For this board, PNG is probably the better default.** With 8 MB PSRAM, decode is cheap, and the ~105 KB saved is roughly 1–2 s less radio-on time — which is the dominant energy cost. **Support both and negotiate via the `MODEL`/`WIDTH`/`HEIGHT` headers**, then measure. Raw packed is the right choice on PSRAM-less hardware.

### 9.5 Caching — the biggest battery lever

TRMNL's own measurement: a full wake costs **32.8 mA over 24 s ≈ 0.219 mAh**; a wake where nothing changed and no repaint happens costs **~20 % of that**. On Spectra 6, whose refresh is 8–15× slower than TRMNL's mono panel, the ratio is even more favourable.

**Skip the *repaint*, not just the download.** Three patterns, ascending:

1. **Filename-as-cache-key** (TRMNL). `filename` must change when content changes; the device compares against its stored value and skips both download and repaint. Still costs a JSON round-trip.
2. **ETag / `If-None-Match` → 304** (Tesserae). One request, no body, standard HTTP, works through any proxy. **Recommended.**
3. **Content hash in the metadata response** — decide before opening the second connection.

Compute a **strong ETag over the final packed pixel bytes**, not the source data, so an identical re-render still yields 304. Pair with `Cache-Control: no-cache` (always revalidate) rather than `max-age`.

### 9.6 Scheduling

- **Typical intervals:** TRMNL default 15 min (floor 15 min free / 5 min paid); MagInkDash hourly; PicPak 900 s.
- **Clock alignment:** TRMNL deliberately does *not* align to boundaries (fleet-wide thundering-herd avoidance). For a single self-hosted device you want the opposite — a clock reading `14:45` should update *at* 14:45. **Let the server compute it:** return `refresh_rate = seconds_until_next_boundary + small_jitter`.
- **Quiet windows:** Terminus's per-device `sleep_start_at` / `sleep_stop_at`. Measured on this hardware: a 01:00–06:00 skip window bought **+18 % battery life**.
- **Time/timezone/DST: push it all server-side.** NTP costs up to 4 s of radio-on per wake — comparable to the entire rest of the cycle. A thin client needs no tz database at all; the server renders local time into the pixels. If the device must know the time to align its own wake, have the server return an epoch and let the **RX8130CE** hold it across power-off. Re-sync NTP rarely (daily), and write it back with `M5.Rtc.setDateTime()`; on every cold boot restore with `M5.Rtc.setSystemTimeFromRtc()` **before** bringing up WiFi.
- **Battery telemetry:** sample **once early in the wake, before radio and panel load** (WiFi TX spikes depress the reading), 20-sample median, report both mV and %. Gate at < 3300 mV → "charge me" splash + long low-power poll until recovery above ~3500 mV. Factory firmware hard-shutdowns at 3100 mV.

### 9.7 Reliability

| Pattern | Detail |
|---|---|
| **Bounded WiFi retries** | TRMNL retries 5×, then shows an error and sleeps. An unbounded retry loop is the classic way to flatten a battery overnight. |
| **Sleep-as-backoff** | On any failure, don't busy-wait — go back to sleep and retry next wake. The wake interval *is* the backoff. |
| **Last-known-good** | Don't repaint on failure. The bistable panel keeps the last good frame for free. Add a small staleness badge. |
| **Radio off during refresh** | Keep WiFi off during the 15–30 s panel drive — panel drive current plus TX spikes is a real brownout risk on a 1250 mAh cell. Initialise the panel lazily (only power it when a frame actually arrived). |
| **Server-triggered OTA** | `update_firmware: true` + `firmware_url` in the display response; `reset_firmware: true` as a separate factory-reset flag. ⚠️ OTA and PMIC shutdown are in tension — the device is fully offline between wakes, so any push must land inside a wake window. |
| **Pairing** | Boot unclaimed → `GET /api/setup` with `ID: <MAC>` → server mints `api_key` → device stores it in NVS and sends it as `Access-Token` thereafter. |
| **Auth** | Bearer-style API key in a header is what everyone actually uses. ❌ No project found using HMAC signing or mTLS on the device path — HMAC needs synced time (the NTP call you're trying to avoid) and mTLS costs handshake CPU on a device that must never brick. |
| **Transport** | Plain HTTP on-LAN is common and saves a TLS handshake per wake. For anything internet-exposed use HTTPS with the IDF certificate bundle (`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE`, or `WiFiClientSecure::setCACertBundle()` on Arduino) and push mbedTLS buffers to PSRAM (`CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC`). |

### 9.8 WiFi provisioning

- **Factory firmware:** SoftAP + **captive portal** (ESP-IDF `httpd` + DNS redirect + mDNS, APSTA mode, credentials in NVS under namespace `papercolor`). It sets `ESP_NETIF_CAPTIVEPORTAL_URI` via DHCP option and serves a redirect *with a body*, because iOS needs content to detect a portal. Gotcha they documented: `max_open_sockets = 13` requires `CONFIG_LWIP_MAX_SOCKETS=16`, else `httpd_start()` fails with `ESP_ERR_INVALID_ARG` and the portal never binds port 80.
- **Arduino:** `WiFiManager` (tzapu) is confirmed working on this board.
- **ESPHome:** `wifi:` + `ap:` fallback + `captive_portal:`. Use `fast_connect: true` — with PMIC shutdown every wake is a cold boot, so connect time is a direct battery cost.

### 9.9 Battery-life budget (⚠️ DERIVED — arithmetic, verify by measurement)

Based on 1250 mAh, 92.53 µA floor, a full wake ≈ 0.3–0.5 mAh (WiFi ~5 s at ~120 mA + a slow colour refresh), and TRMNL's 20 % figure for cache-hit wakes:

| Interval | All wakes full | With ~70 % 304 cache hits |
|---|---|---|
| 15 min | ~30–40 days | ~75–95 days |
| 30 min | ~55–75 days | ~130–160 days |
| 60 min | ~100–130 days | ~200+ days |
| idle floor only | ~560 days | — |

Community-measured reality with PMIC shutdown at 30 min: **~20–22 days**, **~26 days** with an overnight skip. Treat the table above as an upper bound and the measured figures as the realistic baseline.

---

## 10. Gotcha checklist

Work through this list before debugging anything.

**Display**

1. **OPI PSRAM not enabled** → board detected, `M5.Display` silently does nothing. The #1 blank-screen cause.
2. **EPD rail off at cold boot** — it's on M5PM1 `PYG0`, not a GPIO. Non-M5GFX stacks must set it *and* the `PWR_CFG 0x06` boost bit (the ~15 V rail) before touching SPI, or hang on BUSY forever. **`PWR_CFG` auto-clears on every reset.**
3. **BUSY is inverted** (0 = busy). Backwards = instant timeout or instant false-ready.
4. **Auto-display trap** — every un-bracketed draw can trigger a 15–30 s refresh. Use `M5Canvas` + `pushSprite()`.
5. **6 colours, no orange.** Orange quantises to red or yellow; simulate it with a 1 px red/yellow checkerboard.
6. **Never use ACeP/7-colour palettes or the Waveshare wiki colour table** — wrong index order, swapped colours.
7. **Text must not be dithered.** `epd_fastest` on-device, or hard-quantise server-side.
8. ~19–25 s from `M5.begin()` to the first frame on glass. Community configs wait 25 s at boot.

**Power**

9. **ESP32 deep sleep is useless here** (~5–10 mA). Use M5PM1 `SYS_CMD_OFF`.
10. **`HOLD_CFG (0x07)` must be set** or the device won't charge and freezes the moment USB is unplugged.
11. **`M5.Power.setBatteryCharge()` is a silent no-op.**
12. **Nothing wakes the device via ESP32 GPIO during PMIC shutdown** — only the PMIC power button or a PMIC wake source.
13. **Disable PMIC I²C idle-sleep** (`0x09 = 0x00`) or the PMIC goes unresponsive mid-sequence.
14. Consider disabling PMIC single-press-reset / double-press-off (`0x49` bit0, `0x4A` bit0) while your firmware runs, so hardware button behaviour doesn't fight it.
15. Recovery from a wedged PMIC: unplug, hold power ~10 s, replug.

**Build / boot**

16. **Download mode:** hold the side reset button while connecting USB-C. Or programmatically `pm1.enterDownloadMode()`.
17. **G3 is a strapping pin** and is the internal I²C SDA — ESPHome warns about it; safe to ignore on this hardware.
18. **Grove Port A (G4/G5) doubles as the factory UART console**, and its SCL/SDA mapping is inverted from the usual M5 convention.
19. **IDF ≥ 5.3:** `M5Unified.h` before `M5PM1.h`.
20. **Factory partition table has no OTA slots.** Switch to `default_16MB.csv` (or a custom `factory + ota_0 + ota_1 + otadata`) if you want OTA.
21. **EPD and microSD share the SPI bus** — serialise access.
22. Pin library versions. M5Unified needs **≥ 0.2.14** for this board; M5GFX **≥ 0.2.20**.
23. **The application log has nowhere to go by default — you get silence, not an error.** Arduino's prebuilt SDK sets `CONFIG_ESP_CONSOLE_UART_DEFAULT` with `CONFIG_ESP_CONSOLE_UART_NUM 0`, and UART0's default S3 pins are **G43/G44 — which on this board are `EINK_DC` and `EINK_CS`**. So every `log_x()` is clocked out onto the panel's control lines. `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` does *not* save you: it carries `esp_rom_printf` only (ROM banner, panic handler), not `esp_log`. Measured 2026-09-09: **0 bytes over 120 s** from a healthy, running app. `ARDUINO_USB_MODE=1` additionally stops the core from calling `Serial.begin()` for you (`cores/esp32/main.cpp`). Fix, and it needs both halves: `Serial.begin(115200); Serial.setDebugOutput(true);` — `setDebugOutput` is the one that matters, because it installs the CDC `putc1` hook that redirects `ets_printf`.
24. **Never touch DTR on the serial port — it is IO0, and you will land in download mode.** On USB-Serial/JTAG, RTS drives EN and **DTR drives IO0**. A hand-rolled reset that toggles DTR (or a library that asserts it on open) boots the chip to `boot:0x23 (DOWNLOAD(USB/UART0))`, where it sits silently: no app, no panel refresh, no SoftAP — a failure that looks exactly like dead firmware. esptool's own `HardReset(uses_usb=True)` is **RTS only, 200 ms either side, DTR untouched**; copy that. When opening the port merely to *read*, construct the port unopened and set `dtr = False`/`rts = False` before `open()`.
25. **The ROM log does reach USB — but only if the port is already open across the reset.** Anything that closes the port to reset (esptool, then reopening) loses the banner, because it is emitted within ~50 ms. Keeping one handle open through an RTS-only reset yields `ESP-ROM:esp32s3-20210327 … rst:0x… boot:0x…`, which is the cheapest way to tell "app not running" from "app crashed".

---

## 11. Open questions — verify before relying on these

| # | Item | How to close it |
|---|---|---|
| 1 | ESP32-S3R8 as bare chip or WROOM-1 module (affects FCC/CE, antenna) | Read `C151-SCH_PaperColor_V0.5` schematic PDF |
| 2 | ~~USB-UART bridge chip present or native USB only~~ **CLOSED 2026-09-09: native USB, no bridge.** Download mode enumerates as `303A:1001` — a composite of `MI_00` (CDC, becomes a COM port via the Windows inbox `usbser.sys`) and `MI_02` (USB JTAG/serial debug unit). esptool reports `USB mode: USB-Serial/JTAG`. The running app enumerates under the same `303A:1001`. | done |
| 3 | Charge current for C151 | Schematic / M5PM1 datasheet. **Do not quote the Stamp-S3Bat 650/200 mA figures.** |
| 4 | Button pull-up / active-level details, power-button press durations | Schematic; measure |
| 5 | RGB LED exact part (WS2812 vs SK6812 vs other), mic part, IR LED part | Schematic / BOM |
| 6 | Max microSD capacity | Test |
| 7 | Per-mode power figures (WiFi TX, during refresh) | Measure — only 92.53 µA / 211.97 mA are published |
| 8 | Battery chemistry / nominal voltage | Schematic |
| 9 | Mounting holes / stand / magnet | [M5_Hardware C151 Structures](https://github.com/m5stack/M5_Hardware/tree/master/Products/C151_PaperColor/Structures) |
| 10 | Panel controller IC part number | Not published anywhere. **Do not assert JD796xx.** |
| 11 | Whether `0x83` partial-window works on ED2208-DOA | Test on hardware; M5GFX never sends it |
| 12 | Panel refresh-cycle lifetime | No published figure from any vendor |
| 13 | TRMNL `special_function` enum, `image_url_timeout`, `maximum_compatibility`, `touchbar_mode` semantics | Read byos server source |
| 14 | Whether ESPHome PR #16031 (PaperColor preset) merged | Check ESPHome master |

**Unreachable documents** (blocked by network policy during compilation — fetch these manually, they close items 1–5 and 8):

```
https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/1239/C151-SCH_PaperColor_V0.5_SCH_PDF_20260424_EN_2026_04_24_11_01_17.pdf
https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/1239/C151PaperColor_model_size.pdf
https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/1207/M5PM1_Datasheet_EN.pdf
```

---

## 12. Recommended system shape

A concrete starting point that follows from everything above:

**Device (Arduino + M5Unified, or ESP-IDF):**

1. Cold boot → read PM1 wake source → `M5.Rtc.setSystemTimeFromRtc()`
2. Read battery **before** radio comes up (20-sample median)
3. If < 3300 mV → draw "charge me", schedule a long wake, PM1 shutdown
4. WiFi connect (`fast_connect`, max 5 attempts) → `GET /api/display` with telemetry headers
5. Conditional `GET` on `image_url` with `If-None-Match`
   - **304** → skip download **and** repaint (~20 % of a full wake's energy)
   - **200** → download to PSRAM, **turn WiFi off**, power the panel, blit, `0x07 0xA5`, cut the panel rail
6. Persist ETag + next interval to NVS; set the RTC alarm; `pm1.sysCmd(SYS_CMD_OFF)`

**Server (Python/FastAPI or whatever you like):**

1. TRMNL-BYOS-compatible `/api/setup`, `/api/display`, `/api/log`
2. Device/model/playlist tables; register PaperColor as `400×600, colors 6, bit_depth 4`
3. Render at 600×400 (or 1200×800 supersampled) with Satori for text screens, Playwright for chart screens
4. Quantise in Lab against the **measured** palette; region-tagged dithering (no dither on text, checkerboard on UI fills, Floyd–Steinberg on photos)
5. Emit PNG by default with a raw-packed-4bpp fallback; strong ETag over the final pixel bytes
6. Return `refresh_rate` computed to the next clock boundary + jitter; honour a nightly quiet window
7. Enforce the panel-care rules server-side too: never instruct a repaint less than 180 s after the last one; force one repaint per 24 h

---

## 13. Sources

**Official M5Stack**
[PaperColor docs](https://docs.m5stack.com/en/core/PaperColor) · [C151 datasheet page](https://docs.m5stack.com/en/products/sku/C151) · [shop page](https://shop.m5stack.com/products/m5paper-color-esp32s3-dev-kit) · [Arduino quickstart](https://docs.m5stack.com/en/arduino/papercolor/program) · [battery example](https://docs.m5stack.com/en/arduino/papercolor/battery) · [M5PM1 page](https://docs.m5stack.com/en/arduino/papercolor/m5pm1) · [UIFlow2 flashing](https://docs.m5stack.com/en/uiflow2/papercolor/program) · [factory firmware usage](https://docs.m5stack.com/en/guide/display_device/papercolor/usage) · [Arduino board manager URL](https://docs.m5stack.com/en/arduino/arduino_board)

**M5Stack source**
[M5PaperColor-UserDemo](https://github.com/m5stack/M5PaperColor-UserDemo) · [M5Unified](https://github.com/m5stack/M5Unified) · [M5GFX](https://github.com/m5stack/M5GFX) (`Panel_ED2208.cpp`, `M5GFX.cpp`, `lgfx/boards.hpp`) · [M5PM1](https://github.com/m5stack/M5PM1) ([function reference](https://github.com/m5stack/M5PM1/blob/main/README_FUNCTION_EN.md)) · [uiflow-micropython](https://github.com/m5stack/uiflow-micropython) · [M5_Hardware](https://github.com/m5stack/M5_Hardware)

**Panel**
[E Ink EL040EF1](https://www.eink.com/product/detail/EL040EF1) · [E Ink Spectra 6](https://www.eink.com/brand/detail/Spectra6) · [E Ink shop ED2208-DOA](https://shopkits.eink.com/en/product/detail/4''Spectra6ePaperDisplay) · [Good Display GDEP040E01](https://www.good-display.com/product/532.html) · [Waveshare 4" HAT+ (E)](https://www.waveshare.com/4inch-e-paper-hat-plus-e.htm) · [Waveshare 4" manual](https://www.waveshare.com/wiki/4inch_e-Paper_HAT%2B_(E)_Manual) · [waveshareteam/e-Paper (branch `Development`)](https://github.com/waveshareteam/e-Paper)

**Libraries / stacks**
[LovyanGFX](https://github.com/lovyan03/LovyanGFX) · [GxEPD2](https://github.com/ZinggJM/GxEPD2) + [discussion #161](https://github.com/ZinggJM/GxEPD2/discussions/161) · [ESPHome epaper_spi](https://esphome.io/components/display/epaper_spi/) · [esphome source](https://github.com/esphome/esphome/tree/dev/esphome/components/epaper_spi) · [PR #16031](https://github.com/esphome/esphome/pull/16031) · [h0verin/ESPHome-m5stackPaperColor](https://github.com/h0verin/ESPHome-m5stackPaperColor) · [PFalko/m5stack-papercolor-esphome](https://github.com/PFalko/m5stack-papercolor-esphome) · [Zephyr board](https://docs.zephyrproject.org/latest/boards/m5stack/m5stack_paper_color/doc/index.html) · [platformio/platform-espressif32](https://github.com/platformio/platform-espressif32) · [pioarduino/platform-espressif32](https://github.com/pioarduino/platform-espressif32) · [espressif/arduino-esp32 boards.txt](https://github.com/espressif/arduino-esp32/blob/master/boards.txt)

**Silicon**
[Espressif ESP32-S3](https://www.espressif.com/en/products/socs/esp32-s3) · [ESP-IDF sleep modes (S3)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/sleep_modes.html) · [ESP-IDF module current consumption (S3)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/current-consumption-measurement-modules.html) · [Epson RX8130CE](https://www.epsondevice.com/crystal/en/products/rtc/rx8130ce.html) · [RX8130CE brief PDF](https://download.epsondevice.com/td/pdf/brief/RX8130CE_en.pdf) · [espp/rx8130ce component](https://components.espressif.com/components/espp/rx8130ce/versions/1.0.32/readme)

**Architecture / backend**
[TRMNL BYOS docs](https://docs.trmnl.com/go/diy/byos) · [byos_hanami api.adoc](https://github.com/usetrmnl/byos_hanami/blob/main/doc/api.adoc) · [TRMNL ImageMagick guide](https://docs.trmnl.com/go/diy/imagemagick-guide) · [trmnl-firmware](https://github.com/usetrmnl/trmnl-firmware) · [byos_fastapi](https://github.com/usetrmnl/byos_fastapi) · [byos_next](https://github.com/usetrmnl/byos_next) · [awesome-TRMNL](https://github.com/eindpunt/awesome-TRMNL) · [TRMNL refresh rates](https://help.trmnl.com/en/articles/10113695-how-refresh-rates-work) · [TRMNL battery FAQ](https://help.trmnl.com/en/articles/10556850-device-battery-faq) · [TRMNL power efficiency](https://trmnl.com/blog/power-efficiency) · [MagInkDash](https://github.com/speedyg0nz/MagInkDash) · [InkyPi](https://github.com/fatihak/InkyPi) · [Tesserae](https://tesserae.ink/) · [picpak-tesserae-client](https://github.com/varanu5/picpak-tesserae-client) · [shi-314/esp32-spectra-e6](https://github.com/shi-314/esp32-spectra-e6) · [prstoetzer/PaperSatColor](https://github.com/prstoetzer/PaperSatColor)

**Image pipeline**
[epaper-dithering (PyPI)](https://pypi.org/project/epaper-dithering/) · [epdoptimize](https://github.com/paperlesspaper/epdoptimize) · [PhotoPainter Spectra 6 converter](https://github.com/Toon-nooT/PhotoPainter-E-Ink-Spectra-6-image-converter) · [Spectra 6 dithering study](https://myembeddedstuff.com/e-ink-spectra-6-color) · [Adafruit coverage](https://blog.adafruit.com/2026/01/05/beyond-6-colors-exploring-dithering-on-spectra-6-color-e-ink-displays/) · [einkframe colour gamut](https://www.einkframe.com/2025/11/26/spectra-6-color-gamut-part1/)

**Press**
[CNX Software](https://www.cnx-software.com/2026/05/15/m5stack-papercolor-esp32-s3-devkit-features-4-inch-e-ink-spectra-6-color-display/) · [LinuxGizmos](https://linuxgizmos.com/m5stack-papercolor-is-an-esp32-s3-dev-kit-with-spectra-6-e-paper-panel/) · [Hackster.io](https://www.hackster.io/news/m5stack-s-new-papercolor-pairs-an-e-ink-spectra-6-epaper-display-with-an-espressif-esp32-s3-e9519416106f)
