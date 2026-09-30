// Every tunable the firmware has, in one place.
//
// The rule for this file: a constant only lives here if changing it changes
// behaviour a human would care about. Register addresses and protocol magic
// stay next to the code that speaks them, because a value you cannot change
// without reading a datasheet is not a setting.
//
// Numbers that also exist on the server (battery thresholds, panel geometry)
// are duplicated here on purpose -- the board must still make correct
// decisions when the server is unreachable, which is exactly the case where
// it cannot ask. Keep them in step with backend/app/config.py by hand.

#pragma once

#include <stdint.h>

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

#define FW_VERSION "1.0.1"

//: Sent as the MODEL header. The server keys panel geometry off this string,
//: so it tracks the backend's model row, not the M5Stack SKU.
#define DEVICE_MODEL "m5_papercolor"

//: Shown as the captive-portal SSID when no credentials are stored.
#define SETUP_AP_SSID "inkwake-setup"

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

//: The fallback, not the setting. Whatever the setup portal stored in NVS wins
//: over this (net::serverBase), so a board that has been through setup ignores
//: it entirely -- it is what a factory-fresh unit uses until somebody types
//: something better, and what a nonsense stored value falls back to.
//:
//: Still worth setting properly with -DEINK_SERVER_URL in platformio.ini: it
//: is what the board tries before anyone has touched it. The value here exists
//: so a fresh checkout compiles, not so it works.
#ifndef EINK_SERVER_URL
#define EINK_SERVER_URL "https://eink.example.com"
#endif

//: There is no enrolment path. The device never asks the server for a key --
//: the operator creates the row with `python -m app.cli device add`, the token
//: is shown once, and it is typed into the setup portal.
#define API_DISPLAY_PATH "/api/display"
#define API_LOG_PATH "/api/log"

//: Per-request ceiling, and it is also a term in the wake budget below -- see
//: there for why it came down from 15 s. A server that has sent no header
//: after eight seconds is not about to send one, and a half-open socket holds
//: the radio up at ~120 mA, the dominant energy cost (9.1).
//:
//: HTTPClient::setTimeout takes a uint16_t. Anything past 65535 wraps
//: silently.
static constexpr uint32_t HTTP_TIMEOUT_MS = 8000;

//: Ceiling on one TLS handshake. This is a separate number because
//: HTTP_TIMEOUT_MS does not reach the handshake at all: it binds the select()
//: around lwip_connect and SO_RCVTIMEO, and ssl_client.cpp puts the socket in
//: O_NONBLOCK before either matters. The handshake loop
//: (ssl_client.cpp:270-278) runs against handshake_timeout alone and does not
//: feed the task watchdog.
//:
//: The arduino-esp32 default is 120 000 ms (WiFiClientSecure.cpp:40), set per
//: WiFiClientSecure instance. The longest path carries four handshakes --
//: enrolment, the display call, and two image hops under setRedirectLimit(1)
//: -- so the stock default exposed 480 s of hang against a watchdog that was
//: 300 s and whose budget table did not mention TLS at all.
//:
//: In SECONDS: setHandshakeTimeout multiplies by 1000 internally
//: (WiFiClientSecure.cpp:369-371). Do not write HTTP_TIMEOUT_MS / 1000 here --
//: should that constant ever drop below 1000 the integer division yields 0,
//: and a zero timeout aborts every handshake immediately, which is a failure
//: nobody would see until the board is on the wall.
//:
//: Twenty, and deliberately generous, because the two failure directions are
//: not symmetric. Too long is bounded by WAKE_WATCHDOG_S, which is sized to
//: contain it and costs about 2 mAh of 1250 on an over-run. Too short fails
//: *every* https request: fetchPlan gives up, giveUpForNow runs, and the
//: board climbs the offline ladder showing nothing but a notice - a brick
//: produced by a tuning constant, on hardware where a handshake against a VPS
//: with full CA-bundle chain verification has never been measured.
//: Measurement M4 is what should tighten this, not a guess.
static constexpr uint32_t TLS_HANDSHAKE_TIMEOUT_S = 20;

//: Ceiling on one whole wake. Every hang this firmware can suffer -- a
//: half-open socket, a panel that never releases BUSY, an I2C bus wedged by
//: the PMIC -- costs the same thing: the radio or the ~15 V panel rail stays
//: powered until the cell is flat, which on 1250 mAh is a matter of hours.
//: A reboot is strictly better than draining.
//:
//: Read this as a hard deadline on a *healthy* wake, not as a hang detector.
//: Nothing in this firmware calls esp_task_wdt_reset() outside the setup
//: portal, and neither delay() nor yield() feeds the task watchdog, so the
//: work simply has to finish inside this number. That makes a value too close
//: to the real worst case actively harmful: an over-run reboots, the next
//: boot sees the watchdog reset reason and skips its slot, and a board on a
//: merely slow network would never show a picture while draining a full
//: budget three times a day.
//:
//: The sum of the documented ceilings on the longest path -- first wake, no
//: API key yet, one redirect on the image path -- is what this has to clear:
//:
//:     M5.begin() panel init, 2 x BUSY wait      up to  40 s
//:     connect(), WIFI_MAX_ATTEMPTS x timeout           30 s
//:     DNS, up to 4 uncached lookups x 31 s            124 s
//:     fetchPlan(), connect 8 + TLS 20 + hdr 8          36 s
//:     fetchImage(), 2 hops x 36 + body 30             102 s
//:     showImage() + present(), 4 x BUSY wait     up to 80 s
//:     panel::finish(), BUSY wait + settle        up to 23 s
//:     armWake(), up to 2 x pm1.begin()                  2 s
//:     NVS, battery median, SHT40, WiFi driver           2 s
//:     runOta(), reconnect WIFI_MAX_ATTEMPTS x 6        30 s
//:     runOta(), connect 8 + TLS 20 + hdr 8             36 s
//:     runOta(), OTA_TRANSFER_TIMEOUT_S                180 s
//:                                                    ------
//:                                                     685 s
//:
//: Two movements since the last revision, and they pull in opposite
//: directions. Enrolment is gone -- the device is given its token by hand
//: rather than asking the server for one -- which took 36 s out. The firmware
//: update put 246 s back in.
//:
//: Each term is a real ceiling rather than a rounding: the TLS handshake is
//: not covered by HTTP_TIMEOUT_MS (see TLS_HANDSHAKE_TIMEOUT_S), the redirect
//: hop that setRedirectLimit(1) permits is counted twice, and armWake retries
//: pm1.begin(), whose internal delays run to about 0.83 s a time.
//:
//: DNS is the biggest single term. WiFiClientSecure resolves through
//: WiFi.hostByName() *before* start_ssl_client, so neither setConnectTimeout
//: nor the handshake timeout binds it, and WiFiGeneric::hostByName waits
//: WIFI_DNS_IDLE_BIT for 16 s and then WIFI_DNS_DONE_BIT for 15 s. The idle
//: half only blocks behind another in-flight resolution, which this firmware
//: never has -- but this table is built from ceilings, not from guesses about
//: overlap, so it is counted.
//:
//: FOUR uncached lookups, not three. In practice the display call, the image
//: and the firmware all live on one host and only the first resolution costs
//: anything; the table counts what is possible. The previous version counted
//: three and left the reserve thinner than the prose claimed -- that was a
//: known open finding, and this is where it is closed.
//:
//: 267 s of the sum is transfer and 143 s is worst-case panel BUSY. Nothing
//: here says the two cannot peak together -- a cold panel is slow to release
//: BUSY and a congested AP is slow to answer, and a winter morning can serve
//: both -- so the ceiling goes above the whole sum rather than above a guess
//: about which terms overlap. At ~120 mA an over-run costs about 2 mAh of
//: 1250, so the number is energetically free and must be set from the
//: false-alarm side alone: every second shaved off it buys nothing and risks
//: rebooting a wake that was going to succeed.
//:
//: The captive portal is exempt in a different way: it feeds the watchdog
//: itself and carries its own deadline (see net::runSetupPortal).
//: 780 rather than 685 exactly: the same 14 % head-room the previous pair
//: carried, and for the same reason. At ~120 mA an over-run costs about 2 mAh
//: of 1250, so the number is energetically free and must be set from the
//: false-alarm side alone. Every second shaved off it buys nothing and risks
//: rebooting a wake that was going to succeed -- and a reboot here costs the
//: NEXT slot too, because the fault-skip sees the watchdog reset reason.
static constexpr uint32_t WAKE_WATCHDOG_S = 780;

//: Ceiling on the image body transfer, measured from the first body byte.
//: HTTPClient's own timeout covers only the header phase; the body loop is
//: `while (connected() && len)` with a bare delay(1), and WiFiClient reports a
//: half-open socket as connected forever (EWOULDBLOCK). Without an explicit
//: deadline a stalled AP mid-download hangs setup() indefinitely, leaving the
//: radio up and the panel rail powered until the cell is flat -- the one
//: failure mode this firmware genuinely cannot afford.
static constexpr uint32_t IMAGE_BODY_TIMEOUT_MS = 30000;

//: Second, tighter bound: abort when no byte has arrived for this long, even
//: if the overall deadline has not expired. A connection that has gone quiet
//: is not going to recover within a wake, and waiting costs radio-on time.
static constexpr uint32_t IMAGE_STALL_TIMEOUT_MS = 5000;

//: A 400x600 six-colour PNG is 5-30 kB. Anything past this is a redirect to an
//: error page, a captive portal, or a bug -- refuse it before allocating,
//: rather than discovering it when PSRAM runs out mid-download.
static constexpr size_t MAX_IMAGE_BYTES = 256 * 1024;

//: Same reasoning for the JSON metadata, which is a few hundred bytes.
static constexpr size_t MAX_JSON_BYTES = 8 * 1024;

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------

//: TRMNL's figure, and the right shape of answer: bounded retries, then sleep.
//: An unbounded retry loop is the classic way to flatten a battery overnight
//: (9.7). The wake interval is the backoff; there is no other one.
//:
//: Five attempts, not three: a router that is itself rebooting comes back
//: within 30-60 s, and five chances at the scan are worth more than three.
//: The per-attempt bound carries the saving instead. Do not go below ~6 s --
//: WL_CONNECTED includes DHCP, and a slow lease would start failing.
static constexpr uint8_t WIFI_MAX_ATTEMPTS = 5;
static constexpr uint32_t WIFI_ATTEMPT_TIMEOUT_MS = 6000;

//: How long the captive portal stays up before we give up and power off. A
//: portal left open forever is a flat battery by morning.
//:
//: This became a real bound only once runSetupPortal() stopped trusting
//: WiFiManager to enforce it: its timer restarts on every page hit, and a
//: phone parked on the setup AP polls it forever. Fifteen minutes rather than
//: ten because it is now a wall, and somebody may have to walk to the router
//: to read the password off the label.
static constexpr uint32_t WIFI_PORTAL_TIMEOUT_S = 900;

// ---------------------------------------------------------------------------
// Battery
// ---------------------------------------------------------------------------

//: WiFi TX spikes depress the reading, so this many samples get taken in the
//: quiet window before the radio comes up, and the median wins (9.6).
static constexpr uint8_t BATTERY_SAMPLES = 20;
static constexpr uint32_t BATTERY_SAMPLE_GAP_MS = 5;

//: Below this we stop fetching and show a charge prompt. The factory firmware
//: hard-shutdowns at 3100 mV, so this leaves room for several more wakes.
static constexpr uint16_t BATTERY_LOW_MV = 3300;

//: Community discharge-study calibration points (6.2). Not a datasheet curve;
//: good enough to drive a two-digit badge, not good enough to trust near 0 %.
static constexpr uint16_t BATTERY_EMPTY_MV = 3350;
static constexpr uint16_t BATTERY_FULL_MV = 4160;

// ---------------------------------------------------------------------------
// Sleep
// ---------------------------------------------------------------------------

//: Used when the server never answered and NVS holds nothing. One hour, not
//: one minute: a failed wake costs only the WiFi attempt because no repaint
//: happens, but a tight retry loop still adds up over a week offline.
static constexpr uint32_t SLEEP_FALLBACK_S = 3600;

//: After a failed cycle. Deliberately shorter than a scheduled slot so a
//: transient outage self-heals before the next real slot, and deliberately
//: long enough that a week-long outage costs single-digit mAh.
static constexpr uint32_t SLEEP_RETRY_S = 3600;

//: On low battery, back off hard. There is nothing to fetch and nothing to
//: paint; the only job left is to survive until someone plugs in a cable.
static constexpr uint32_t SLEEP_LOW_BATTERY_S = 6 * 3600;

//: The emergency nap, taken only when the PMIC will not arm its wake timer.
//: This one really is an ESP32 deep sleep, with everything 6.3 says about it:
//: the rails keep running, and the droop measurement there puts the cell at
//: well under two days -- plan against that number, not against the optimistic
//: 5-10 mA in the same table. Its one virtue is that the timer lives inside
//: the S3 and needs no PMIC, so the board does come back. Hence short: the
//: point is a fresh boot with a fresh chance at the bus, not keeping a
//: schedule.
static constexpr uint32_t SLEEP_PMIC_RETRY_S = 900;

//: How many wakes in a row may end in that nap before the board switches off
//: dark instead. Roughly two hours of trying. A permanently dead PMIC would
//: otherwise flatten and then deep-discharge the cell in under two days, and a
//: ruined cell is worse than a dark panel -- which keeps its last frame either
//: way, and comes back the moment somebody plugs in USB.
static constexpr uint16_t PMIC_FALLBACK_MAX = 8;

//: Floor. Vendor guidance is >= 180 s between refreshes and the documented
//: failure is permanent panel damage (4.4 rule 1). The server enforces this
//: too; the board enforces it again because a server bug must not be able to
//: destroy hardware.
static constexpr uint32_t SLEEP_MIN_S = 300;

//: Ceiling. The panel must be refreshed at least once every 24 h or it starts
//: down the documented burn-in path (4.4 rule 2), so we refuse to sleep
//: through a whole day no matter what the server says.
static constexpr uint32_t SLEEP_MAX_S = 23 * 3600;

//: What the stored ETag is set to when the panel is showing one of our own
//: notice screens rather than a server frame. The ETag store means "what is on
//: the glass", so these belong in it -- and because a real ETag is always a
//: quoted string, an unquoted sentinel can never collide with one. Sending it
//: back as If-None-Match simply fails to match and yields a fresh 200.
#define ETAG_LOCAL_LOW_BATTERY "local:low-battery"
#define ETAG_LOCAL_OFFLINE "local:offline"
//: Painted once each, never per wake. Both mark states that need a human, and
//: a board that repainted the same instruction screen three times a day would
//: spend about 0.8 mAh a time saying something nobody had come to read.
#define ETAG_LOCAL_SETUP "local:setup"
#define ETAG_LOCAL_AUTH "local:auth"

//: Backoff ladder for consecutive failed wakes, in seconds. Index is
//: failureStreak - 1; past the end the last rung repeats.
//:
//: A flat SLEEP_RETRY_S meant 24 failed wakes a day against a dead router,
//: each spending WIFI_MAX_ATTEMPTS x WIFI_ATTEMPT_TIMEOUT_MS with the radio
//: up at WIFI_PS_NONE -- roughly 25 mAh on an offline day against 6.7 mAh on
//: a working one. A fortnight with the router unplugged cost more than a
//: month of normal service.
//:
//: Cumulative time from the first failed wake to the fifth, which is where
//: OFFLINE_NOTICE_AFTER fires: 1 + 2 + 4 + 4 = 11 h. Against a dead router
//: that is eight wakes a day instead of twenty-four, and the notice arrives
//: in eleven hours instead of twenty.
//:
//: What this does NOT do, despite being tempting to claim: guarantee panel
//: rule 2 offline. The ladder is indexed by failureStreak, and that counter
//: measures wakes since the last successful *fetch*, not since the last
//: *paint*. A 304 resets it (main.cpp, Fetch::NotModified) without touching
//: the glass -- correctly, since a 304 means the server decided no repaint
//: was due. So the age of the image when connectivity dies is not something
//: this counter can see, and there is no bound on it at all:
//:
//:   * benign case -- unchanged content under the server's max_image_age_s:
//:     the image can be 23 h old when the router dies, plus 11 h of ladder,
//:     so 34 h to the notice;
//:   * unbounded case -- should_repaint also refuses below min_refresh_temp_c
//:     (~15 C, the panel takes a colour cast otherwise). An unheated room in
//:     January produces no-paint successes for *weeks*, every one of them
//:     resetting the streak, and the ladder then runs its 11 h from a last
//:     paint that is arbitrarily old.
//:
//: While the server is reachable rule 2 is its job and it does it as far as
//: the cold-panel rule allows. This ladder only covers the window after that,
//: and what it actually buys is a sooner notice (11 h instead of 20) and a
//: third of the wakes -- not a 24 h guarantee. Do not restate it as one.
//:
//: Closing it needs a clock, not a counter: store the epoch of the last paint
//: in NVS and compare it against the RTC, which is already set from the
//: server's Date header on every successful fetch. That is a change to the
//: wake path and is deliberately not made on firmware that has never booted;
//: see the README's open points.
static constexpr uint32_t SLEEP_OFFLINE_BACKOFF_S[] = {3600, 7200, 14400, 14400};

//: How many consecutive failed wakes before we paint an offline notice.
//: A best-effort stand-in for rule 2, not an implementation of it: when the
//: server is unreachable it cannot force the daily refresh for us, and the
//: board has no trustworthy wall-clock time after a cold boot. See the ladder
//: above for exactly how far short of 24 h this falls and why. Tied to the
//: ladder -- change one, recompute the other.
static constexpr uint16_t OFFLINE_NOTICE_AFTER = 5;


// ---------------------------------------------------------------------------
// Firmware update
// ---------------------------------------------------------------------------

//: Ceiling on the whole firmware transfer, measured from the first body byte.
//: The image is 1.35 MB where every other transfer this board makes is 5-30 kB,
//: so IMAGE_BODY_TIMEOUT_MS is two orders of magnitude too small and reusing it
//: would mean the update silently never lands.
//:
//: 180 s is what the sibling firmware carries in production for a 1.9 MB image,
//: so it is generous here on purpose -- the two failure directions are not
//: symmetric. Too long is bounded by WAKE_WATCHDOG_S and costs a wake that was
//: already spent; too short means a board that reports an old version forever
//: with nothing in the log to say why.
static constexpr uint32_t OTA_TRANSFER_TIMEOUT_S = 180;

//: Second, tighter bound, exactly as IMAGE_STALL_TIMEOUT_MS is for the image:
//: abort when no byte has arrived for this long. A peer that has gone quiet is
//: not coming back inside a wake, and waiting costs radio-on time at ~120 mA.
//: Ten rather than five seconds because a 1.35 MB transfer competes with the
//: server's own disk, and a brief gap there is normal in a way it is not for a
//: 5 kB PNG.
static constexpr uint32_t OTA_STALL_TIMEOUT_MS = 10000;

//: Below this, no update is attempted at all. Deliberately far above
//: BATTERY_LOW_MV: that threshold gates *content*, and content is the thing
//: this device exists to show. An update is the one piece of work that can
//: always wait for the next charge, and it is also the most expensive single
//: act a wake can perform. Above the server's battery_recover_mv as well, so a
//: board that has just climbed out of the charge prompt does not spend its
//: first healthy wake on a minute of radio.
static constexpr uint16_t OTA_MIN_BATTERY_MV = 3600;

//: Sanity window on the advertised Content-Length, checked before
//: Update.begin() erases anything. The running image is ~1.35 MB and an OTA
//: slot is 6400 kB (default_16MB.csv), so a length outside this window is an
//: HTML error page, a captive-portal redirect, or -- the expensive one -- a
//: MERGED flash image that starts at 0x0 with the bootloader and would brick
//: every board it reached. Refusing before the erase is what makes a wrong URL
//: a logged non-event instead of a wiped partition.
static constexpr uint32_t OTA_MIN_IMAGE_BYTES = 512 * 1024;
static constexpr uint32_t OTA_MAX_IMAGE_BYTES = 4 * 1024 * 1024;

//: Consecutive attempts on one and the same image digest before it is written
//: off. Counted in NVS, because on this board every attempt is a separate cold
//: boot and a RAM counter would always read one. The sibling device keeps its
//: equivalent in RAM and can, because it never powers down.
//:
//: Three, and the two failure kinds are deliberately unequal: a truncated
//: transfer says nothing about the image and gets its three tries, a checksum
//: mismatch says everything and burns the whole budget at once. Without that
//: distinction a weak signal would permanently lock out a perfectly good
//: release; without the cap, an image that can never finish inside
//: OTA_TRANSFER_TIMEOUT_S would spend ~0.7 mAh of radio three times a day
//: forever.
static constexpr uint16_t OTA_ATTEMPT_MAX = 3;

// ---------------------------------------------------------------------------
// Provisioning
// ---------------------------------------------------------------------------

//: Consecutive 401/403 answers before the setup portal is offered again. The
//: server answered, so the network is fine and the offline ladder is the wrong
//: medicine -- this is an identity problem and only a human at the server
//: can fix it.
//:
//: Two rather than one, because a server redeploy can reject a valid token for
//: a few minutes, and one false alarm costs a 900 s portal (~25 mAh) plus a
//: notice screen on the glass.
static constexpr uint16_t AUTH_FAIL_BEFORE_PORTAL = 2;

//: Consecutive failed wakes before the portal is offered as well. This is the
//: router-was-replaced case: the stored SSID no longer exists, so the board can
//: never reach anyone and no amount of backoff helps. Eight rungs of
//: SLEEP_OFFLINE_BACKOFF_S is about a day and a half -- long enough that a
//: weekend outage does not put a setup screen on a working dashboard.
static constexpr uint16_t PORTAL_AFTER_FAILURES = 8;

//: How many times the portal opens on a plain timer wake before it becomes
//: button-only.
//:
//: This is the whole of the old finding: an unconfigured board used to paint
//: the instructions and run a 900 s portal on EVERY wake, and because that path
//: slept SLEEP_RETRY_S the result was 24 sessions a day -- roughly 620 mAh,
//: half a charge, daily, on a board nobody had set up yet.
//:
//: Two automatic tries cover the person who walked off to read the router
//: label. After that the board waits to be asked: the PMIC wakes on PWRBTN and
//: on VIN, so pressing the power button or plugging in USB opens the portal
//: immediately, and power::wakeReason() already knows which happened.
static constexpr uint16_t SETUP_PORTAL_AUTO_TRIES = 2;

//: Between the automatic portal tries. One hour, not SLEEP_MIN_S: a portal is
//: 25 mAh, and whoever is going to set this up is either there now or will be
//: there later today.
static constexpr uint32_t SLEEP_SETUP_RETRY_S = 3600;

//: Once the automatic tries are spent. Six hours -- the same figure as
//: SLEEP_LOW_BATTERY_S and for the same reason: there is nothing to fetch and
//: nothing to paint, and the only job left is to still be here when somebody
//: finally presses the button. At this rate an unconfigured board in a drawer
//: lives out its full idle life instead of flattening itself in two days.
//:
//: Written as a plain decimal literal, not 6 * 3600: test_deploy.py reads these
//: values with a regex that only matches one.
static constexpr uint32_t SLEEP_UNCONFIGURED_S = 21600;

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

//: Native geometry, portrait. Not a preference: the framebuffer is 400x600
//: and the server's packer refuses anything else (4.1).
static constexpr int32_t PANEL_WIDTH = 400;
static constexpr int32_t PANEL_HEIGHT = 600;

//: The panel needs >= 2 s after the deep-sleep command before its rail may be
//: cut, or it is left in a high-voltage state (4.3 / 4.4 rule 3).
static constexpr uint32_t PANEL_SLEEP_SETTLE_MS = 2500;

//: The six inks as M5GFX renders them, mirroring backend palette.py
//: DEVICE_RGB. Status screens draw in these exact values so that
//: setEpdMode(epd_fastest) -- which means *no* dithering -- maps every pixel
//: to its ink with no rounding at all.
struct Ink {
  uint8_t r, g, b;
};
static constexpr Ink INK_BLACK{0, 0, 0};
static constexpr Ink INK_WHITE{255, 255, 255};
static constexpr Ink INK_YELLOW{255, 243, 56};
static constexpr Ink INK_RED{191, 0, 0};
static constexpr Ink INK_BLUE{100, 64, 255};
static constexpr Ink INK_GREEN{67, 138, 28};
