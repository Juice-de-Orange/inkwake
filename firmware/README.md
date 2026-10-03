# inkwake — Firmware for the M5Stack PaperColor (C151)

The board is a *thin client*: it switches itself on three times a day, fetches
a fully rendered 400×600 PNG from the server, paints it onto the glass and then
switches itself off completely. It computes nothing, knows no time zone and
carries no fonts — the server does all of that.

Between two wake times the device is **genuinely off** (around 92 µA), not in
deep sleep. The image stays up anyway, because e-ink is bistable.

> **This firmware first ran on real hardware on 2026-09-09** and worked end to
> end: Wi-Fi, plan, image, panel refresh, wake timer including the read-back
> check. Measured on that run: refresh 35.3 s cold / 16.1 s on the second
> wake, full cycle 21 s.
>
> What that run did **not** show: a real PMIC cold start (the build was
> flashed with `-DEINK_DEV_NO_SHUTDOWN`, so the board parks afterwards), TLS on
> this hardware, and an over-the-air OTA update. Before the first flash, please
> read [Before the very first flash](#part-3--before-the-very-first-flash).

Everything here is grounded in `HARDWARE.md`. Numbers in parentheses —
(4.4 rule 1), (10.2) — refer to the sections there.

---

## Part 1 — Operation

### What you need

* **PlatformIO Core** (the command line; VS Code is not required).
  Install it with `pip install platformio`, ideally into the project venv. On
  Windows the venv's executables live in `.venv\Scripts\`; every command below
  can equally be run as `python -m platformio …` instead of `pio …`.
* A **USB-C data cable**. Many bundled cables can only charge — that is behind
  most "board not detected" cases.
* The PaperColor. No driver is needed; the ESP32-S3 enumerates via native USB
  (VID `0x303A`).

The first build downloads the ESP32-S3 toolchain and the libraries, several
hundred MB. That takes a while once; after that it is a matter of seconds.

### Putting the board into download mode

**Hold the side button down while** plugging in the USB-C cable, then release
it (10.16). Only then does the serial port appear.

Once the firmware is running, the device is switched off most of the time and
has no serial port at all — download mode is then the only way in.

### Setting the server URL

This is the address a **factory-fresh** board tries. After setup it is no
longer the authoritative one: the setup portal has a *Server* field, and
whatever is entered there is stored in NVS and takes precedence over this value
(`net::serverBase()`). That is the difference between "the server moved" and
"take the board down and go hunting for a USB cable" — the device has no other
means of input.

Set it correctly anyway. It lives in `platformio.ini`, not in `config.h`, so a
workbench build cannot be committed by accident:

```ini
build_flags =
    ...
    -DEINK_SERVER_URL='"https://eink.example.com"'
    -DEINK_TLS_CA_BUNDLE
```

The double quoting is intentional and required: the inner quotes belong to the
C string, the outer ones protect them from the shell. A
`-DEINK_SERVER_URL=http://…` without quotes does not compile.

Use the **public** hostname, not a LAN address. If your server runs on a VPS,
there is no local route to it anyway. The name only resolves once the DNS record
exists — until then every wake runs into the void, which on bistable glass
costs nothing but freshness.

No trailing slash. The firmware appends the paths (`/api/display` etc.) itself
— `serverBase()` strips an accidental trailing slash from the stored value, and
an address without `http://` or `https://` is discarded rather than obeyed, so
that a typo cannot lock the board out of every server for good.

`EINK_TLS_CA_BUNDLE` is mandatory as soon as the URL is `https://`: without the
flag the firmware accepts any certificate and only writes a warning to the log
— and nobody reads the log of a device that works. See
[Build flags](#build-flags).

### Building and flashing

Run all commands from the `firmware/` directory.

```bash
cd firmware

# build only
pio run

# build and flash (put the board into download mode first)
pio run -t upload

# follow the serial log (115200 baud, stack traces are decoded)
pio device monitor

# pick the port by hand if several devices are attached
pio run -t upload --upload-port COM5        # e.g. /dev/ttyACM0 on Linux

# throw away the build directory
pio run -t clean

# erase the entire flash — including Wi-Fi credentials, device token and ETag
pio run -t erase
```

A successful build ends like this — measured with
`pio run -e papercolor`, platform-espressif32 7.0.1, arduino-esp32 2.0.17,
M5Unified 0.2.20 / M5GFX 0.2.28 / M5PM1 1.0.7. The library versions in `platformio.ini` are `^`
ranges, so a later build resolves newer releases and the byte counts move a little (1 378 817 bytes
of flash with M5Unified 0.2.25 / M5GFX 0.2.32 in October 2026):

```
RAM:   [==        ]  15.4% (used 50384 bytes from 327680 bytes)
Flash: [==        ]  20.8% (used 1362941 bytes from 6553600 bytes)
========================= [SUCCESS] Took 40.60 seconds =========================
```

The seconds figure is a full rebuild of the project's own sources with the
toolchain already downloaded; if nothing changed, it is around 11 seconds. The
two memory lines are the part that stays comparable — a large difference there
means the build used different flags; a small one comes from newer library releases.

#### Why the monitor usually stays empty

After flashing, one wake cycle runs and then the board switches itself off. The
USB CDC port disappears in the middle of the log. For work at the desk, build
with `-DEINK_DEV_NO_SHUTDOWN` (see below) — the device then stays awake after
the cycle, the port stays open, and the next upload no longer needs the button
acrobatics.

### Setting up Wi-Fi (first start)

On the very first start no credentials are stored. Then the following happens,
in this order:

1. **Instructions appear on the display** ("set up Wi-Fi") with the name of the
   setup network. This takes about 20 seconds — e-ink is slow, the device has
   not crashed.
2. The board opens an open Wi-Fi network called **`inkwake-setup`**
   (WiFiManager; confirmed working on this board, 9.8).
3. Select this network on a phone or laptop. The sign-in window (captive portal)
   usually opens by itself; if not, open **http://192.168.4.1** in a browser.
4. There, choose "Configure WiFi", select your network, enter the password and
   save. The same page has the *Server* field and a *device token* field
   (`<id>.<secret>`); the token comes from
   `python -m app.cli device add --label "<name>" --mac <AA:BB:CC:DD:EE:FF>`
   on the server, which prints it exactly once. Everything is stored in NVS.
5. The board switches itself off.
6. **Five minutes later** it wakes up again and fetches the first real image.

Step 6 is where people get impatient. It is deliberate: the instruction image
has only just been painted, and the panel manufacturer requires at least 180
seconds between two refreshes. According to the datasheet, the damage from
ignoring this is permanent (4.4 rule 1), so the firmware prefers to wait.

The portal stays open for **15 minutes**, and that deadline is also shown on
the instruction image. If nothing is configured within that time, the device
switches off and tries again in an hour — a permanently open access point would
mean an empty battery by morning. The deadline has only recently become a real
one: before, every page request extended it, and a phone that stays attached to
the setup network polls every second.

#### Changing Wi-Fi later

There is currently **no button** for this (the three buttons are unused, see
[Known gaps](#known-gaps)). The only way is `pio run -t erase` followed by a
fresh `-t upload`. That also erases the device token and the server URL, so
the setup portal asks for them again (`device list` shows the id; a lost token
means `device remove` and `device add`). The ETag is simply fetched anew.

### What happens at power-on

A normal wake cycle, in the order it shows up in the log:

| Step | Duration | Log line |
|---|---|---|
| Record reset reason, arm the wake watchdog | <0.1 s | (no output) |
| `M5.begin()`, board detection, panel rail on | ~1 s | `[Autodetect] board_M5PaperColor` |
| Re-apply PMIC registers, read wake reason | <0.1 s | `wake: timer` |
| Read battery (20 samples) and SHT40 — **before** the radio | ~0.2 s | `battery 4021 mV (82 %), external power no, …` |
| Was the last cycle a failure reboot? Then stop here | <0.1 s | only on failure: `previous wake failed …` |
| Connect Wi-Fi | 1–3 s | `wifi up after 1 attempt(s), rssi -58` |
| `GET /api/display` | ~1.5 s | `plan: http://…/img/… refresh=28800` |
| Download image, then radio off | 1–5 s | `image 18342 bytes, etag "…"` |
| **Panel refresh** | **15–30 s** | (no output, the device blocks) |
| Park panel and cut the rail, set and verify the alarm, switch off | ~3 s | `arming wake in 28800 s` |

As a rule of thumb: **about 19–25 seconds from `M5.begin()` to the first image
on the glass** (10.8); including Wi-Fi association and the download before it,
a complete cycle is more like 25–40 seconds in practice. The lion's share is
the panel itself, and there is nothing to optimise there.

The refresh is a **visible flicker** through several colour passes. That is
normal for Spectra 6 panels and not a defect.

If the image has not changed, the server answers with `304` and the firmware
skips the download **and** the refresh:

```
304 -- panel already shows the current frame
```

Such a cycle costs about a fifth of a full one (9.5) — the biggest battery
lever in the whole system.

#### The wake watchdog

The entire cycle sits under a hard upper limit: **780 seconds**
(`WAKE_WATCHDOG_S` in `config.h`). If it is exceeded, the task watchdog fires
and the device reboots.

The reason is not tidiness but the cell. A hung cycle does not cost one pass,
it costs the whole device: `setup()` then never reaches `finishWake()`, and
that is the **only** place that arms the wake timer in the PMIC. The board
would therefore stay awake — radio on, panel rail powered, around 120 mA —
until the 1250 mAh cell is empty after about ten hours, and then stay dark
until someone plugs in USB. No other guard catches this: the Arduino core sets
`loopTaskWDTEnabled = false`, the bootloader RTC watchdog is disabled during
IDF startup, and `power::begin()` deliberately disables the PMIC watchdog so it
does not reset in the middle of a 30-second refresh.

**Important for anyone tuning this number:** outside the setup portal, nothing
in this firmware calls `esp_task_wdt_reset()`, and neither `delay()` nor
`yield()` feeds the task watchdog. `WAKE_WATCHDOG_S` is therefore not hang
detection but a deadline on the **elapsed time of a perfectly healthy pass**. A
value too close to the real duration is actively harmful: it reboots a working
cycle, the next boot skips its slot because of the reset reason, and a board on
a merely slow network never shows an image while burning its full budget three
times a day. The sum of the documented upper bounds on the longest path — the
first wake after setup — is worked out in the comment above the
constant. In energy terms an overrun costs around 2 mAh out of 1250; the number
should therefore be sized purely from the false-alarm side.

Two things are exempt:

* **The captive portal.** It runs while a human types a Wi-Fi password, and it
  is the one place where the watchdog does what its name promises:
  `net::runSetupPortal()` drives WiFiManager non-blocking and feeds the
  watchdog on every loop pass, but keeps its own absolute deadline
  (`WIFI_PORTAL_TIMEOUT_S`, 15 minutes). Both together, because either one
  alone would be worse — see "The portal now really closes" below.
* **The shutdown sequence.** `power::powerOff()` releases the watchdog
  immediately before each call that does not return: the PMIC shutdown, the
  deep-sleep fallback path and the park loop. Not earlier — arming the wake
  timer is an I²C exchange, and a bus jammed there is exactly the case a
  reboot gets you out of. This is also why workbench builds with
  `-DEINK_DEV_NO_SHUTDOWN` may sit around awake instead of rebooting every four
  minutes.

#### The portal now really closes

Both obvious limits for the captive portal were ineffective, each in its own
way:

* WiFiManager's own timeout is not a deadline. `configPortalHasTimeout()`
  pushes the start time forward on every web request as long as
  `_webClientCheck` is set — which is the default, and `handleNotFound()`
  counts too. The connectivity checks a phone fires at a captive portal every
  second therefore extend it indefinitely. A phone that stayed attached to the
  setup AP kept the board at around 100 mA until the cell was empty.
* Stretching the wake watchdog over the portal instead would have been worse:
  it is not fed, so after expiry it would have force-rebooted a perfectly
  healthy process — in the middle of password entry, and the input would be
  lost.

That is why the portal runs non-blocking, the loop in `net::runSetupPortal()`
owns the deadline itself and calls `esp_task_wdt_reset()` on every pass. The 15
minutes are shown on the instruction screen the device paints before the portal
— a hard limit that nobody announces would be unreasonable.

What happens after a watchdog reboot is described under
[Device reboots instead of sleeping](#device-reboots-instead-of-sleeping).

#### When the server does not answer

Then **nothing is repainted**. The old image stays up, which is free on
bistable glass and still shows more information than an error screen (9.7).
The device retries on a stretched ladder: 1 h, 2 h, then every 4 h
(`SLEEP_OFFLINE_BACKOFF_S`), i.e. eight wakes a day instead of twenty-four.
After 5 failed attempts — around 11 h on this ladder — an "offline" notice
appears — not as an error message, but because the manufacturer requires the
panel to be driven at least once every 24 hours (4.4 rule 2).

The nastier case is a server that **answers and then stalls**: headers
present, image half delivered, connection never closed. For this the image
download has its own clock — 30 seconds for the whole body
(`IMAGE_BODY_TIMEOUT_MS`) and 5 seconds without a single new byte
(`IMAGE_STALL_TIMEOUT_MS`). Whichever limit breaks first applies; the log then
shows one of these two lines:

```
image body stalled 5000 ms at 4096 of 18342 bytes
image body exceeded 30000 ms
```

After that it is an entirely ordinary failed cycle: the old image stays up, the
failure counter goes up, and another attempt follows on the backoff ladder (in
one hour after the first failure, every four hours from the third onwards). The
same applies to an image that does arrive but is shorter than announced —
`image incomplete: 4096 of 18342 bytes`. Half a PNG would be half an image on
the glass, and it would stay there until the next wake time.

Below 3.3 V a one-off "battery empty" screen appears; after that the device
sleeps in 6-hour steps until someone plugs in a cable.

---

## Part 2 — Troubleshooting

### Black, white or unchanged old screen

**First suspect: PSRAM.** `platformio.ini` must contain:

```ini
board_build.arduino.memory_type = qio_opi
```

If this is missing or set to something else, the board is **still detected
correctly** and `M5.Display` then silently does nothing at all. This is by far
the most common cause of a dead display on this device (10.1).

You can spot it in the log by one of these lines:

```
M5PaperColor need OPI-PSRAM enabled                  <- from M5GFX
canvas allocation failed -- is OPI PSRAM enabled?    <- from this firmware
```

Cross-check: with the correct setting, `[Autodetect] board_M5PaperColor`
appears early in the log and neither of the two lines follows.

Second suspect, if the PSRAM line is clean: the server is not delivering a
valid PNG. The log then shows `PNG decode failed (… bytes)` and the old image
deliberately stays up.

### Device hangs, log stops in the middle of the refresh

That is the **BUSY line**, and behind it is almost always the panel's power
supply. The panel rail is not driven by an ESP32 pin but by the M5PM1 PMIC
(its GPIO0), and the `PWR_CFG` register (0x06) **is cleared automatically on
every reset** — and every wake is a reset here (10.2).

The firmware re-applies it in `power::begin()` on every boot. If the PMIC does
not answer, however, the log says so:

```
PMIC did not answer -- the panel will not come up
```

Then only the hardware route helps: **unplug the cable, hold the power button
for about 10 seconds, plug it back in** (10.15). That brings a wedged PMIC
back.

M5GFX polls BUSY every 10 ms with a 20 s cap and carries on afterwards — so a
permanent hang points to a crash rather than the panel. The
`esp32_exception_decoder` in the monitor then resolves the stack trace into
file names.

**Since the wake watchdog, the device can no longer hang permanently.** After
780 seconds at the latest, the cycle aborts with a reboot. So if the log breaks
off in the middle of the refresh and a fresh boot starts some minutes later,
that is not the next wake time but the watchdog — the rescue, not the fault.
How to tell the two apart in the log is covered in the next section.

### Device reboots instead of sleeping

The telltale sign is a boot that does **not** follow `arming wake in … s` but
comes out of the middle of a cycle. Two places in the log prove it. First, the
IDF writes this when it fires:

```
E (183xxx) task_wdt: Task watchdog got triggered. The following tasks did not reset the watchdog in time:
E (183xxx) task_wdt:  - loopTask (CPU 1)
E (183xxx) task_wdt: Aborting.
```

(`loopTask` is the Arduino task in which `setup()` runs;
`CONFIG_ARDUINO_RUNNING_CORE` is 1, hence CPU 1.)

and after the reboot this firmware itself writes:

```
previous wake failed (reset reason 6) -- skipping this slot, back in 3600 s
arming wake in 3600 s
```

The number is `esp_reset_reason()`. The slot is skipped for **6**
(`ESP_RST_TASK_WDT`, the normal case), **5** (`ESP_RST_INT_WDT`), **7**
(`ESP_RST_WDT`), **4** (`ESP_RST_PANIC`) and **9** (`ESP_RST_BROWNOUT`).

What is **not** on this list matters more than what is. **1**
(`ESP_RST_POWERON`) is the reset reason of *every normal wake* — the PMIC cuts
the rails and brings them back up — so skipping on it would mean never showing
an image again. **3** (`ESP_RST_SW`) is produced by no path in this firmware.
And the reset reasons esptool uses to reset after flashing do not exist yet in
this IDF version; should they arrive with an upgrade, they must stay off the
list, otherwise the board on the workbench switches itself off after every
upload before a single log line appears.

**The skipped pass is intentional.** After a failure reboot the cycle does
*not* run again: the device only re-applies the PMIC registers, measures the
battery, parks the panel, sets the alarm and switches off. A reproducible fault
— a server that always stalls at the same point, a jammed I²C bus, a crash in
the PNG decoder — would otherwise turn into a reboot loop, and that drains the
cell just as reliably as the fault itself. The price is an image that is one
interval old; on bistable glass that costs nothing.

**On brownout, the battery decides.** A brownout means the 3V3 rail collapsed
— that can be a weak cell or a load step on a full one (driving the panel alone
exceeds 200 mA, and TX peaks come on top). That is why the battery is measured
**before** the reset-reason check, which costs around 150 ms with the radio
idle: below `BATTERY_LOW_MV` the device sleeps for `SLEEP_LOW_BATTERY_S`,
otherwise only for `SLEEP_RETRY_S`. Six hours of darkness for a load step would
be six hours for nothing.

**When it is the rescue:** once or occasionally, with a plausible cause in the
log just before (download hangs, Wi-Fi driver stuck, PMIC not answering). Then
the watchdog did exactly what it is there for, and the next regular pass runs
through normally.

**When it is a symptom:**

* **Every** pass ends this way. Then something hangs systematically — usually
  right before the point where the log breaks off. For debugging, build with
  `-DEINK_DEV_NO_SHUTDOWN`: the device stays awake and the port stays open.
* The reboot comes **well before** the 780 seconds. Then it was not the
  watchdog: a crash announces itself with `Guru Meditation Error` and a stack
  trace, a brownout with `Brownout detector was triggered`. With a brownout,
  suspect the cell or the panel supply, not the software. Both cases are now
  also caught by the lock described above (reset reasons 4 and 9), so here too
  you get a `previous wake failed` and a skipped slot instead of a reboot loop.
* It happens **while setting up Wi-Fi**. That must not happen: the loop in
  `net::runSetupPortal()` calls `esp_task_wdt_reset()` on every pass. The
  watchdog can therefore only strike there if the loop itself stalls — and then
  the actual fault is further up in the log. On its own, the loop ends after
  `WIFI_PORTAL_TIMEOUT_S` at the latest.

A watchdog reboot erases nothing. Wi-Fi credentials, device token, ETag and failure
counter live in NVS and survive it, just as they survive every normal wake.

### Does not charge / freezes when the USB cable is unplugged

That is `HOLD_CFG` (register 0x07). Without bits 0, 3 and 5 the device does not
charge and stops dead the moment USB power goes away (10.10).

The register **resets itself to 0x00 on every reset, in download mode and on
shutdown**. It is therefore not a one-time setup step but has to be rewritten
on every boot — which is exactly what `power::begin()` does.

If this symptom appears, either the PMIC is not reachable (see above) or
`power::begin()` was never reached.

### Board does not show up as a serial port at all

* Data cable, not just a charging cable.
* Download mode: hold the side button **while** plugging in.
* If the firmware is already running, the device is off between wake times.
  That is not a fault — download mode is then the only option.

---

## Part 3 — Before the very first flash

The following points come from `HARDWARE.md` chapter 11 ("Open questions") and
should be checked on the first device. The run of 2026-09-09 closed some of
them; what remains open is listed under [Known gaps](#known-gaps).

**Two things up front that apply since device identity moved to per-device
tokens:**

**An erase is mandatory when upgrading.** The NVS key for the identity is now
called `tok` rather than `api_key`; a board from an older firmware has a
server-issued key stored there that it will never use again. That is harmless,
but the old server-URL entry and the old ETag are not — so run
`pio run -t erase` once, and afterwards enter everything again through the
portal (Wi-Fi, server URL, and the device token printed by
`python -m app.cli device add --label "<name>" --mac <AA:BB:CC:DD:EE:FF>`).
After that, erasing is no longer the way to reach these values: from now on the
setup portal reopens even with stored credentials when it is needed (token
expired, router replaced) or when someone presses the button.

**After an OTA update, a USB flash writes to the wrong slot.** Once the board
has been updated over the air, `otadata` points to `app1`. A cable upload from
PlatformIO, however, writes `app0` **and leaves `otadata` untouched** — the
board then keeps booting the old OTA image, and you spend hours hunting for a
bug in code that is not even running. Fix before uploading:

```bash
# erase otadata, keep NVS (Wi-Fi and token survive)
python -m esptool --chip esp32s3 --port <port> erase-region 0xe000 0x2000
```

`pio run -t erase` would also do it, but takes NVS with it.


1. **Button-to-GPIO mapping.** According to the docs `BtnA` = G10, `BtnB` = G9,
   `BtnC` = G1, all active-low — but the source explicitly advises measuring
   this on your own device (5.4). For this firmware it makes no difference,
   because it reads no button. Anyone adding controls must look here first.
2. **Quiescent current.** The target is the datasheet value of ~92 µA in the
   switched-off state. If it is well above that, `-DEINK_PMIC_ENABLE_BOOST=0` is
   the first thing to try.
3. **PWR_CFG bit 3.** See [Contradictions in the
   manual](#contradictions-in-the-manual-and-what-the-firmware-does-about-them).
4. **Wake reason.** The log shows `wake: timer`, `wake: rtc`,
   `wake: power_button` etc. On the first real cycle, check whether the PMIC
   timer really comes back as `timer` — the whole wake strategy depends on it.
5. **Refresh duration.** 15–30 s is expected. Significantly more points to
   temperature (below ~15 °C there are colour casts and longer cycles, 4.4
   rule 4) or to a supply problem.
6. **Charging current.** Not published for this model. Do not calculate with
   the values of other M5PM1 boards.
7. **Cycle duration versus the watchdog.** `WAKE_WATCHDOG_S` is set to 780 s;
   the expected duration of a pass is 25–40 s. On the first device, work the
   log timestamps against it: is there a comfortable margin between the last
   step and the 780 s, or does it get tight? Too tight means reboots in normal
   operation — the opposite of what the watchdog is for, and because nothing
   feeds the watchdog, it also hits perfectly healthy passes.
8. **Wake-timer read-back test.** On every shutdown the log shows
   `arming wake in … s`. If you get `timer config reads back …` or
   `no wake armed` instead, the PMIC did not accept the registers — the
   deep-sleep fallback path then kicks in, and that is exactly the case it was
   built for. While at it, settle `TIM_CFG` bit 3: `M5PM1.h` calls it
   auto-reload ("0=one-shot, 1=auto"), while `timerSet()` in the same package
   calls it "start timer". If the header is right, the wake timer reloads
   itself — then a board that misses `finishWake()` once is not lost at all,
   and a good part of the caution in `power.cpp` would be belt and braces.

---

## Part 4 — How the firmware is built

This part is for whoever changes the code.

### The one decision everything else follows from

**No ESP32 deep sleep.** On this board deep sleep measures 5–10 mA because the
rails keep running; that is around 1.8 days from the 1250 mAh cell. A full
M5PM1 shutdown (`SYS_CMD_OFF`) measures 92 µA — a factor of 70 (6.3).

So the device does not sleep, it switches off, and every wake is a **cold
start**. Nothing survives that except:

| Storage | Survives | Used for |
|---|---|---|
| NVS (flash) | yes | ETag, sleep duration, device token, failure counter, server URL, count of PMIC fallback sleeps |
| M5PM1 RTC RAM, 32 B | yes | *(unused — see Known gaps)* |
| The panel image | yes | bistable; an old image costs nothing |
| RAM / PSRAM / `RTC_DATA_ATTR` | **no** | — |

That is why `store.cpp` exists, and why `setup()` never returns.

### One wake cycle, in order

`src/main.cpp` is the entire state machine. The order is not a matter of
style:

1. **Read `esp_reset_reason()`, then arm the wake watchdog.**
   Arming it does not change what the last reset reports; collecting it first
   anyway makes the two things verifiably independent.
   `WAKE_WATCHDOG_S` = 780 s, panic → reboot.
2. `M5.begin()` with `clear_display = false` — otherwise every wake spends
   20 s clearing the panel (10.8).
3. `store::begin()` opens NVS, then `power::begin()` — re-applying the PMIC
   registers that the reset cleared. **`PWR_CFG` clears itself on every reset,
   and every wake is a reset here** (10.2).
4. Read the wake reason from the PMIC, then `M5.Rtc.setSystemTimeFromRtc()` —
   before the radio, because the radio is what should stay short. Reading also
   clears the wake-reason latch in the PMIC, which is why it comes before the
   abort below and not after it: a stale latch would make the *next* boot
   report this one's wake reason (6.4).
5. **Battery and SHT40 before the radio.** Wi-Fi transmit peaks pull the
   voltage down, so 20 samples and the median, taken in the quiet window (9.6).
6. **If the last reset was a failure, the cycle ends here** — watchdog, panic
   or brownout. Only at this point, not earlier: the PMIC registers must be set
   on this path too, otherwise the device stops charging and freezes when the
   cable is unplugged, and it is the measurement from step 5 that tells a weak
   cell apart from a mere load step on brownout. Then
   `finishWake(SLEEP_RETRY_S)`, or `SLEEP_LOW_BATTERY_S` for a brownout on an
   empty cell. Without this lock a reproducible fault becomes a reboot loop that
   drains the cell just as surely as the fault itself.
7. Below 3300 mV and not demonstrably on a cable → paint "battery empty",
   sleep 6 h, switch off.
8. Wi-Fi: at most 5 attempts, then give up. **The wake interval is the
   backoff** — an unbounded retry loop is the classic way to a battery that is
   empty overnight (9.7).
9. `GET /api/display`, telemetry in the headers. The server renders these
   readings into exactly the image it returns in the same response — which is
   why they have to go along with the very first request (9.2).
10. The RTC is set from the `Date` header of that same response — free, because
    the header arrived anyway, and the prerequisite for the RTC alarm to be of
    any use as a second wake path. `net::parseHttpDate()` computes in `int64_t`
    for this and rejects anything that cannot be represented as a positive
    `time_t` value; see
    [The year 2038 in parseHttpDate](#the-year-2038-in-parsehttpdate).
11. Image via `GET` with `If-None-Match`. **304 → no download and no repaint**,
    around 20 % of the energy of a full cycle (9.5). The body is read against
    two clocks, see [Two paths through the image
    body](#two-paths-through-the-image-body).
12. **Radio off, only then the panel.** Panel driver current plus transmit
    peaks on a 1250 mAh cell is a real brownout risk (9.7).
13. Decode the PNG into a canvas, one `pushSprite`, one explicit `display()`,
    wait, then `0x07 0xA5` and cut the rail (4.4 rule 3).
14. ETag and interval into NVS, set the alarm and **verify it**, close NVS,
    `sysCmd(SYS_CMD_OFF)`. The watchdog stays armed until immediately before the
    shutdown command: arming the wake timer is an I²C exchange, and a bus jammed
    there is exactly how this board goes permanently dark. It is only released
    in `power::powerOff()`, before each call that does not return.

### Two paths through the image body

`net::fetchImage()` reads the response body in two different ways, and the
difference is not cosmetic.

**With `Content-Length`** — the normal case — the transfer encoding is
`identity`, so the socket carries only payload. The firmware then reads it
itself, `stream->readBytes()` in 1 kB chunks, and can attach a wall clock:
`IMAGE_BODY_TIMEOUT_MS` (30 s for the whole body) and
`IMAGE_STALL_TIMEOUT_MS` (5 s without a single new byte). Both differences are
computed unsigned, so that the `millis()` overflow after 49 days yields a small
rather than a huge time span.

This is the point of it all: `http.writeToStream()` has **no limit on the body
phase**. Its loop is `while (connected() && len)` with a bare `delay(1)`,
`_tcpTimeout` covers only the header phase, and `WiFiClient::connected()`
reports a half-open socket (`EWOULDBLOCK`) as connected indefinitely. A server
that stalls in the middle of the body would thus have held the device
**indefinitely** — radio on, panel rail powered, around 120 mA, until the cell
is empty after about ten hours.

**Without `Content-Length`** the response is chunked, and then the chunk
lengths (`1a3f\r\n`) are part of the data stream. `writeToStream()` is the only
path in `HTTPClient` that strips them; reading the raw socket would write them
into the middle of the PNG. This path therefore still uses `writeToStream()`,
but gets its deadline through the sink: `PsramSink` carries a deadline and
returns a short write after it, which forces `writeToStream()` to abort. What
is missing here is only the stall deadline — the sink sees only what actually
arrives. If the other end goes silent in the middle of a chunk, the wake
watchdog is still what binds, and that costs a reboot instead of a clean abort.

Both paths are followed by a cross-check: an empty buffer or fewer bytes than
announced means the image is discarded. And the ETag is only stored after the
image is actually on the glass — storing it earlier would make the server
answer `304` on the next wake, and the old image would stay up forever.

### The year 2038 in `parseHttpDate`

`time_t` is **32 bits** in this toolchain: newlib defines `_USE_LONG_TIME_T`,
and `long` is 32 bits on Xtensa — see
`toolchain-xtensa-esp32s3/xtensa-esp32s3-elf/include/sys/_types.h`. Seconds
since 1970 fit into it until 19 January 2038.

The calculation used to run in `time_t` and would have been a signed overflow
from then on. That is not merely a wrong value but undefined behaviour — which
allows the compiler to optimise away the caller's `plan.server_epoch > 0`
check, because from its point of view no overflow *can* happen. What would be
left is a negative timestamp that sets the clock to 1901.

It is now computed in `int64_t`, and the result is checked against the actual
`time_t` range before returning — at the top against the 2038 limit, at the
bottom against anything ≤ 0. If it does not fit, `0` is returned and a warning
goes to the log:

```
Date header out of range for time_t: ...
```

For the caller, `0` means "no server time": the RTC is not set, everything
else carries on normally. That is the honest answer — a device without a set
clock keeps waking via the PMIC countdown, a device whose clock says 1901 does
unpredictable things.

### Files

| File | Contents |
|---|---|
| `platformio.ini` | Board settings. Every line is load-bearing, see below. |
| `src/config.h` | Every tunable, with the reasoning for its value. |
| `src/main.cpp` | The wake cycle above, the wake watchdog and the three notice screens. |
| `src/power.cpp/.h` | M5PM1 bring-up, battery median, SHT40, alarm, shutdown. |
| `src/panel.cpp/.h` | Canvas, PNG blit, notice screens, panel parking. |
| `src/net.cpp/.h` | Wi-Fi, captive portal, `/api/display`, image download with its own time limits. |
| `src/store.cpp/.h` | The NVS keys — the only state that survives a wake. |

### Settings that are not optional

| Setting | Why |
|---|---|
| `board_build.arduino.memory_type = qio_opi` | **OPI PSRAM.** Without it the board is still detected and `M5.Display` silently does nothing. Most common cause of black screens (10.1). |
| `board = esp32s3box` | Neither of the two platform forks has a PaperColor board definition (7.2). M5Stack recommends this substitute in its own docs. |
| `board_build.partitions = default_16MB.csv` | The factory table has no OTA slots (10.20). |
| `M5Unified ≥ 0.2.14`, `M5GFX ≥ 0.2.20` | That is where PaperColor support was added (10.22). |
| `#include <M5Unified.h>` before `<M5PM1.h>` | From IDF 5.3 onwards, `i2c_bus.h` and `driver/i2c.h` otherwise collide over `i2c_config_t` (7.3). |
| `WAKE_WATCHDOG_S = 780` (`config.h`) | Not hang detection but a deadline on the elapsed time of **every** pass — nothing outside the setup portal feeds the task watchdog. It must therefore be above the sum of all documented partial timeouts (the calculation is in the comment above the constant), not merely above the expected duration. Too tight means reboots on healthy hardware; disabling it means, in the hang case, an empty cell and a device that never wakes up again. |
| `IMAGE_BODY_TIMEOUT_MS = 30000`, `IMAGE_STALL_TIMEOUT_MS = 5000` | The only wall clock over the body phase of the image download. `HTTPClient` does not bring one, and a half-open socket reports itself as connected for as long as it likes. |
| Portal loop in `net::runSetupPortal()` | Drives WiFiManager non-blocking, feeds the watchdog on every pass and keeps its own absolute deadline. Turning it back into a blocking `startConfigPortal()` gets you one of two things back: a portal without an effective time limit (WiFiManager's timeout is extended by every page request) or a reboot in the middle of password entry. |

### Build flags

| Flag | Default | Effect |
|---|---|---|
| `EINK_SERVER_URL` | `https://eink.example.com` | Base URL for a factory-fresh board. The setup portal overrides it in NVS; set it correctly anyway. |
| `EINK_TLS_CA_BUNDLE` | **on** | HTTPS with real certificate verification against the Mozilla root store built into arduino-esp32. **Mandatory as soon as the server is reachable from the internet.** Without this flag the firmware accepts *any* certificate for `https://` and writes a warning to the log. Costs around 62 kB of flash (measured against the same baseline build). |
| `EINK_PMIC_ENABLE_BOOST` | `1` | Sets `PWR_CFG` bit 3. See "Contradictions in the manual". |
| `EINK_DEV_NO_SHUTDOWN` | off | Workbench builds: stay awake after the cycle instead of switching off, so the USB port stays enumerated for the next upload. Costs milliamps — never ship it like this. |

All three optional flags have been built individually and together and
compile. `EINK_TLS_CA_BUNDLE` takes flash from 1,299,017 to 1,362,925 bytes —
the roughly 62 kB mentioned; since it is on by default, the second value is the
normal one. `EINK_DEV_NO_SHUTDOWN` saves about 9 kB because the shutdown path
drops out; `EINK_PMIC_ENABLE_BOOST=0` changes only a constant and therefore
nothing at all about the size.

### Panel care rules and where each is enforced

| Rule (4.4) | Enforced by |
|---|---|
| ≥ 180 s between two refreshes | `SLEEP_MIN_S` clamp in `power::armWake`, plus `panel::begin()`, which refuses a second refresh in the same cycle |
| At least one refresh every 24 h | The server, via the ETag. Offline: `OFFLINE_NOTICE_AFTER` counts wakes instead of hours, because the board has no reliable time after a cold start. |
| Put the panel to sleep or cut the rail after every refresh | `panel::finish()`, called before the blocking portal and once more on the way out |
| No refresh below ~15 °C | **The server**, from the `TEMPERATURE` header. See Known gaps. |

`setEpdMode(epd_fastest)` means *no* dithering — the mode names are misleading;
`epd_text` dithers more than `epd_fastest` (4.7 / 10.7). The server has already
quantised against the measured palette; anything further would destroy exactly
its error diffusion and grind up the text edges in the process.

### Contradictions in the manual, and what the firmware does about them

**`PWR_CFG` bit 3.** Section 3.2 assigns `BOOST5V_EN_PP` to the 5 V output of
the Grove port. Section 10.2 calls the same bit "the ~15 V e-ink rail". The
M5PM1 library itself sides with 3.2 — `M5PM1.h` line 123 ff. documents bit 3
as `BOOST_EN – BOOST/GROVE(5VINOUT) power enable (hardware dependent)`. Since
this panel brings its own driver and PMIC behind the glass (4.1), it almost
certainly generates its high voltage itself.

The firmware **sets the bit anyway**, because the risk is one-sided: a few
hundred microamps are nothing compared to a black screen, and the one community
configuration known to work on this board sets it. Build with
`-DEINK_PMIC_ENABLE_BOOST=0` as soon as someone can measure the quiescent
current — that is the first thing to try if the 92 µA are not reached.

**Bit layout of register `0x16`.** The ESPHome recipe in 7.5 says "clear bits
0 & 3" of `GPIO_FUNC0`, but this register holds **two bits per GPIO** —
clearing bit 3 hits GPIO1, not GPIO3. See M5GFX 0.2.28, `src/M5GFX.cpp`
(PaperColor branch), which correctly masks
`0b11<<(0*2) | 0b11<<(3*2)` for this. Instead of raw masks, this firmware uses
the typed `gpioSetFunc()` of the M5PM1 library and thus sidesteps the question
entirely.

---

## Known gaps

An honest list. What is here is either not implemented or not proven — and
none of it pretends otherwise.

* **The PMIC cold start is still unproven.** The run of 2026-09-09 used
  `-DEINK_DEV_NO_SHUTDOWN`; the board parks afterwards instead of switching
  off. A complete cycle with a real shutdown and a wake reason of `timer` on the
  second wake is still outstanding — and that is the run that decides whether
  the board is ready for unattended, permanent mounting.
* **TLS is untested on this hardware.** The only hardware run went over HTTP
  against a workbench server. The production path has up to four handshakes,
  and `TLS_HANDSHAKE_TIMEOUT_S = 20` is a derivation, not a measurement.
* **The wake watchdog and the download time limits are the least-tested part.**
  They are built against failure cases that have never actually occurred: a
  server that stalls in the middle of the body, a cycle that never ends. Neither
  path ever triggers on a perfectly working device — so they remain unproven
  even when a board boots cleanly. The numbers (780 s, 180 s, 30 s, 10 s, 5 s)
  are added up from the documented partial timeouts, not measured;
  `test_wake_watchdog_clears_the_sum_of_its_own_timeouts` recomputes the sum and
  arrives at 685 s. What has been measured so far is one healthy cycle: 21 s.
* **The chunked path lacks the stall deadline.** It does have an overall
  deadline now: `PsramSink` carries one, and a short write aborts
  `writeToStream()`. But that only works as long as bytes are arriving at all —
  a sink sees nothing else. If the other end goes silent in the middle of a
  chunk, only the wake watchdog binds, and that costs a reboot instead of a
  clean abort. Closing this completely would mean reimplementing the chunk
  parsing.
* **From 19 January 2038, `parseHttpDate` no longer returns a time.** This is
  not fixed, only made honest: `time_t` is 32 bits here, and instead of an
  overflow it now returns `0`. In practice this means the RTC is no longer set
  from then on and the RTC alarm as a second wake path silently drops out. The
  PMIC countdown, the main path, is not affected. A real fix would need a
  toolchain with a 64-bit `time_t`.
* **OTA is built but has never run over the air.** Server and device sides are
  in place, the offer logic is covered by the server tests, and the
  refusals (wrong class, no reported current version, too little charge, digest
  already seen) work. No image has reached a board yet. On the server, firmware
  is registered with
  `python -m app.cli firmware add <path/to/firmware.bin> --version <x.y.z>` and
  assigned to a device with
  `python -m app.cli device set <id> --firmware <firmware-id>`.

  The design is deliberately conservative: the update runs **after**
  `power::armWake()` and does **not** reboot. From that point on, every failure
  is therefore without consequence — the watchdog ends a hung transfer, the
  reset reason makes the next slot drop out, and `otadata` stays untouched if
  power fails in the middle of writing. The new image boots at the next slot, or
  immediately at the press of a button.

  What this does **not** cover: an image that passes the checksum and still
  crashes before the reset-reason check. Only a PMIC dead-man switch would help
  against that, and it is not built. A self-flash that goes wrong would be
  unrecoverable on a device without a reachable console, and OTA is at odds with
  the PMIC shutdown anyway (9.7). The partition table would have the slots for
  it.
* **`POST /api/log` is not implemented.** Logs only go over USB CDC.
* **The cold rule is the server's job.** The board sends `TEMPERATURE` and does
  what it is told; it does not refuse a refresh below 15 °C on its own. Should
  the server stop delivering images on cold days, this check has to move here.
* **The 32 bytes of RTC RAM in the PMIC are unused.** The failure counter lives
  in NVS. That costs one flash write per failed wake — bounded, because `store`
  only writes values that have actually changed.
* **`special_function` is read and ignored.** The enum is undocumented
  (11.13).
* **The three buttons do nothing.** A/B/C are never polled. Adding support
  would only help so much anyway: while the board is switched off, **no** ESP32
  GPIO wakes it, only the PMIC (10.12).
* **The RTC alarm as a second wake path is untested and mostly idle.** The main
  path is the PMIC's own countdown, which also works on a board whose clock has
  never been set. The RTC alarm is only set when the clock looks plausible —
  which is first the case after a successful `Date` header.
* **The SHT40 readout is hand-built on the I²C bus**, including the CRC check,
  but has never run against a real sensor. If it does not answer or the CRC
  does not match, the firmware reports no reading at all rather than a wrong
  one (`SHT40 did not answer`).
* **There is no partial refresh.** Every update is a full, flickering pass
  (4.5). A clock with a seconds display is not possible on this panel.
