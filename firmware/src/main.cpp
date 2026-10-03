// One wake, start to finish.
//
// There is no loop. The device cold-boots, does the sequence below once, and
// switches itself off with the PMIC -- 92 uA instead of the 5-10 mA an ESP32
// deep sleep costs on this board (HARDWARE.md 6.3). setup() therefore never
// returns; every exit from it goes through finishWake().
//
// The order of the first four steps is not stylistic:
//
//   * clear_display = false, or M5.begin() spends 20 s wiping the panel on
//     every single wake (8);
//   * RTC before WiFi, because the radio is what we are trying to keep short;
//   * battery before the radio, because TX spikes depress the reading (9.6);
//   * WiFi off before the panel is driven, because panel drive current plus
//     TX spikes on a 1250 mAh cell is a real brownout risk (9.7).

// M5Unified first: on IDF >= 5.3 the other order makes i2c_bus.h collide with
// driver/i2c.h over i2c_config_t (7.3).
#include <M5Unified.h>

#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

#include "config.h"
#include "net.h"
#include "panel.h"
#include "power.h"
#include "store.h"

namespace {

//: A findable copy of the version string, so tools/verify-image.py can read the
//: version OUT of a built image rather than trusting what somebody typed.
//:
//: Needed because the natural place -- esp_app_desc_t.version at offset 0x30 --
//: is filled by the build system, not from FW_VERSION, and on an Arduino build
//: it does not reliably carry our number. Searching the binary for the bare
//: string is no good either: "1.0.0" occurs in half the libraries linked in.
//:
//: `used` keeps the linker from discarding it; it is never read at runtime.
const char kFwMarker[] __attribute__((used)) = "INKWAKE-FW-VERSION:" FW_VERSION;


//: The backstop for every hang this firmware can still suffer.
//:
//: The Arduino core disables the loop watchdog (loopTaskWDTEnabled = false),
//: the bootloader RTC watchdog is switched off during IDF startup, and
//: power::begin() deliberately disables the PMIC watchdog. That left nothing
//: at all watching a wake -- and a wake that hangs does not merely lose one
//: cycle: setup() never reaches finishWake(), which is the only place
//: that arms the PMIC wake timer. The device would sit at ~120 mA until the
//: cell is flat and then stay dark until somebody plugs in USB.
//:
//: A reboot is strictly better than that. Combined with the reset-reason check
//: in setup(), a reproducible hang costs one wake, not the battery.
void armWakeWatchdog() {
  const esp_err_t err = esp_task_wdt_init(WAKE_WATCHDOG_S, true);  // true: panic -> reboot
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    log_e("watchdog init failed: %d -- this wake runs unguarded", err);
    return;
  }
  if (esp_task_wdt_add(nullptr) != ESP_OK) {
    log_e("watchdog could not watch this task");
  }
}

//: Last stop for every path through setup(). Parks the panel, arms the wake,
//: commits NVS, cuts power. The panel goes first, because a panel left awake
//: after a refresh takes permanent damage (4.4 rule 3).
//:
//: The wake is armed here rather than inside the shutdown because whether the
//: PMIC actually took the timer is a fact that has to outlive this wake. If it
//: did not, power::powerOff() takes an expensive deep-sleep nap instead of a
//: shutdown nothing would ever wake from -- and something has to remember how
//: many of those naps have already been spent, which means NVS, which means
//: before store::end().
//:
//: Note what is *not* here any more: the watchdog stays armed straight through
//: the arming sequence and is released inside power::powerOff(), immediately
//: before each call that does not return. Arming the timer is an I2C exchange,
//: and a wedged bus in the middle of it is precisely the failure worth
//: rebooting out of.
//: `conserve` says the cell is already too low to spend anything on retrying a
//: broken PMIC: the fallback nap runs the rails at milliamps rather than
//: microamps, and eight of them on a flat battery would finish it off. Then
//: going dark immediately is the kinder outcome -- the panel keeps its frame,
//: and USB brings the board back either way.
//: `ota`, when present, is a firmware image the server offered and setup()
//: already vetted. It runs HERE, and the placement is the single most
//: considered decision in this file.
//:
//: After armWake() and after panel::finish(), which means: the wake timer is
//: already set, and the panel rail is already cut. Every failure past this
//: point is therefore free. A hung download hits the wake watchdog, the reboot
//: sees the fault reason, and the next boot skips its slot and sleeps -- the
//: board is dark for one cycle instead of dark for ever. A power cut mid-write
//: leaves otadata untouched, so the old image still boots.
//:
//: And it does NOT restart afterwards. The obvious thing -- write, reboot into
//: the new image -- is what a mains-powered device does, and it is wrong twice
//: over here. It would skip finishWake(), the only place that arms the wake
//: timer, so a new image that crashed before reaching it would leave a board
//: that never wakes again. And a restart is a zero-second sleep: it walks
//: straight past the SLEEP_MIN_S clamp in armWake() that exists so a server bug
//: cannot violate the 180 s panel-care floor. The new image boots at the next
//: scheduled wake, or immediately if somebody presses the power button.
[[noreturn]] void finishWake(uint32_t sleep_s, bool conserve = false,
                             const net::OtaJob *ota = nullptr) {
  panel::finish();

  const bool armed = power::armWake(sleep_s);
  const uint16_t naps = armed ? 0 : static_cast<uint16_t>(store::pmicNaps() + 1);
  store::setPmicNaps(naps);

  // Only with the timer actually armed. A board whose PMIC is not answering is
  // one step from the rescue-nap path and its counter; that is the worst
  // imaginable moment to spend a minute of radio and write 1.35 MB to flash.
  if (ota != nullptr && armed) {
    net::runOta(*ota);
    net::disconnect();
  }

  store::end();

  // Never longer than asked for: a fifteen-minute cap is there to get another
  // shot at the PMIC soon, not to override a deliberate back-off.
  //
  // Floored as well as capped, and the floor is the load-bearing half.
  // power::armWake clamps its own argument to [SLEEP_MIN_S, SLEEP_MAX_S]
  // precisely so a server bug cannot destroy the panel -- but this path does
  // not go through armWake, it is the path taken *because* armWake failed.
  // The server may legitimately hand out a very short refresh_rate (its own
  // floor is 60 s, not 300), and without a floor here the eight rescue naps
  // PMIC_FALLBACK_MAX allows would collapse into eight minutes of trying.
  // Note what the floor does and does not restore: eight naps at 300 s is
  // forty minutes, not the "roughly two hours" PMIC_FALLBACK_MAX describes.
  // Two hours only holds when the countdown already exceeds
  // SLEEP_PMIC_RETRY_S, which is the normal case; the floor bounds the
  // pathological one.
  const uint32_t nap_s = max(SLEEP_MIN_S, min(sleep_s, SLEEP_PMIC_RETRY_S));
  power::powerOff(armed, conserve || naps >= PMIC_FALLBACK_MAX, nap_s);
}

//: Notice screens are painted once, not once per wake. A charge prompt that
//: repaints every six hours spends 15-30 s of panel drive to tell the same
//: person the same thing out of a battery that is already flat. The stored
//: ETag doubles as "what is on the glass", so it answers the question.
bool alreadyShowing(const char *sentinel) { return store::etag() == sentinel; }

void showLowBattery(uint16_t millivolts, uint8_t percent) {
  char volts[24];
  snprintf(volts, sizeof(volts), "%u.%02u V", millivolts / 1000, (millivolts % 1000) / 10);

  char level[24];
  if (percent == 255) {
    snprintf(level, sizeof(level), "Ladestand unbekannt");
  } else {
    snprintf(level, sizeof(level), "noch %u %%", percent);
  }

  // ASCII only: the GFX free fonts carry no umlauts, and a missing glyph is
  // drawn as a blank box that nobody can debug from across a room.
  const char *lines[] = {"Bitte laden.", volts, level, "", "Das Bild bleibt stehen,",
                         "bis Strom da ist."};
  panel::showNotice("Akku leer", lines, sizeof(lines) / sizeof(lines[0]), INK_RED);
  store::setEtag(ETAG_LOCAL_LOW_BATTERY);
}

void showPortalInstructions() {
  // The portal now really does close on time, so the deadline has to be on the
  // glass. This screen is the only warning anyone gets, and it is still
  // showing after the board has switched itself off.
  char window[44];
  snprintf(window, sizeof(window), "Zeit dafuer: %u Minuten.",
           static_cast<unsigned>(WIFI_PORTAL_TIMEOUT_S / 60));

  // The device id belongs on this screen, and it was missing.
  //
  // Somebody standing in front of an unconfigured board has to create its row
  // on the server before the token exists, and the row wants this identifier.
  // Until now it appeared nowhere but the USB log of a device that, by
  // definition, has no USB cable attached.
  char ident[40];
  snprintf(ident, sizeof(ident), "Geraet: %s", net::deviceId().c_str());

  // Die Adresse steht hier, weil das Anmeldefenster NICHT zuverlaessig von
  // selbst aufgeht -- am 10.09.2026 an einem echten Handy zweimal nicht. Die
  // Captive-Portal-Erkennung haengt am Geraet, am Browser und daran, ob mobile
  // Daten noch laufen; nichts davon kann diese Firmware beeinflussen. Was sie
  // kann, ist die Adresse hinschreiben, unter der das Portal in jedem Fall
  // erreichbar ist. Ohne sie steht jemand vor einem Board, das laut Anleitung
  // etwas tut, was es nicht tut.
  const char *lines[] = {"Am Handy dieses WLAN waehlen:",
                         SETUP_AP_SSID,
                         "",
                         "Oeffnet sich kein Fenster:",
                         "http://192.168.4.1 aufrufen",
                         "(mobile Daten ausschalten).",
                         "",
                         "Heimnetz auswaehlen und das",
                         "Geraete-Token eintragen.",
                         window,
                         "",
                         ident,
                         "",
                         "Token am Server erzeugen:",
                         "app.cli device add"};
  panel::showNotice("WLAN einrichten", lines, sizeof(lines) / sizeof(lines[0]), INK_BLUE);
}

//: The server answered, and it does not know us. A different problem from
//: being offline, and it needs a different screen: no amount of retrying fixes
//: an identity, only a person at the server does.
void showAuthProblem() {
  char ident[40];
  snprintf(ident, sizeof(ident), "Geraet: %s", net::deviceId().c_str());

  const char *lines[] = {"Der Server kennt dieses",
                         "Geraet nicht (oder es wurde",
                         "abgeschaltet).",
                         "",
                         ident,
                         "",
                         "Am Server ein Token erzeugen",
                         "(app.cli device add), dann",
                         "hier die Ein-Aus-Taste",
                         "druecken."};
  panel::showNotice("Nicht angemeldet", lines, sizeof(lines) / sizeof(lines[0]), INK_RED);
}

void showOffline(uint16_t streak) {
  char detail[40];
  snprintf(detail, sizeof(detail), "%u Versuche ohne Antwort", streak);

  const char *lines[] = {"Kein Kontakt zum Server.", detail, "",
                         "Der Bildschirm zeigt weiter",
                         "den letzten bekannten Stand."};
  panel::showNotice("Offline", lines, sizeof(lines) / sizeof(lines[0]), INK_YELLOW);
  store::setEtag(ETAG_LOCAL_OFFLINE);
}

//: A failed cycle repaints nothing. On bistable glass the last good frame is
//: still there for free, and a fresh error screen would replace real
//: information with none (9.7).
[[noreturn]] void giveUpForNow() {
  // Unconditionally, because one caller reaches here with a radio that is up
  // but never associated, and the offline notice below drives the panel.
  net::disconnect();

  const uint16_t streak = static_cast<uint16_t>(store::failureStreak() + 1);

  if (streak >= OFFLINE_NOTICE_AFTER) {
    // Except here: the panel has now held one image for the better part of a
    // day with nobody able to tell us to refresh it (4.4 rule 2). Resetting
    // the streak afterwards is what makes this repeat on a roughly daily
    // cadence -- the rule is about exercising the pixels, so painting the same
    // notice again still satisfies it.
    showOffline(streak);
    store::setFailureStreak(0);
  } else {
    store::setFailureStreak(streak);
  }

  // A ladder, not a flat hour. The stored sleep value is the slot gap (5-11 h)
  // so the old min(stored, SLEEP_RETRY_S) was always exactly SLEEP_RETRY_S,
  // and against a dead router that is 24 failed wakes a day, each holding the
  // radio up for WIFI_MAX_ATTEMPTS attempts. The ladder is indexed by the
  // streak we just recorded and reaches the offline notice after 11 h.
  // What that does and does not guarantee is set out at
  // SLEEP_OFFLINE_BACKOFF_S -- read it before quoting a number here.
  //
  // streak is >= 1 on every path that reaches this (it was just incremented
  // from an unsigned counter), so streak - 1 does not wrap in practice; the
  // clamp below makes the pathological uint16_t wrap harmless anyway.
  constexpr size_t kRungs = sizeof(SLEEP_OFFLINE_BACKOFF_S) / sizeof(SLEEP_OFFLINE_BACKOFF_S[0]);
  const size_t rung = min(static_cast<size_t>(streak - 1), kRungs - 1);
  finishWake(SLEEP_OFFLINE_BACKOFF_S[rung]);
}

}  // namespace

void setup() {
  // Read first: esp_reset_reason() describes the reset we just came out of,
  // and arming the watchdog below does not disturb it.
  const esp_reset_reason_t reset_reason = esp_reset_reason();
  armWakeWatchdog();

#ifdef EINK_DEV_NO_SHUTDOWN
  // Bench builds only, and it is not optional there: without this the log is
  // unreachable. The IDF console is UART0 (CONFIG_ESP_CONSOLE_UART_DEFAULT,
  // CONFIG_ESP_CONSOLE_UART_NUM 0), whose default S3 pins are G43/G44 -- and
  // on this board those are EINK_DC and EINK_CS. Every log_x() therefore goes
  // out on the panel's own control lines. CONFIG_ESP_CONSOLE_SECONDARY_USB_
  // SERIAL_JTAG carries only esp_rom_printf, not esp_log, so the USB port
  // stays silent; measured on hardware 2026-09-09: 0 bytes over 120 s.
  //
  // ARDUINO_USB_MODE=1 also stops the Arduino core from calling begin() for
  // us (cores/esp32/main.cpp). setDebugOutput() is the part that matters --
  // it installs the CDC putc1 hook, which is what actually redirects
  // ets_printf, and with it every log_x(), onto USB.
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  delay(300);  // let the host re-enumerate before the first line is written
  log_i("bench build: serial log redirected to USB CDC");
#endif

  auto cfg = M5.config();
  cfg.clear_display = false;  // 20 s of nothing, on every wake, otherwise
  cfg.internal_spk = false;   // the codec rail stays dark; this is a dashboard
  cfg.internal_mic = false;
  cfg.output_power = false;  // no Grove 5 V boost
  // This board carries no IMU (HARDWARE.md 3.2) and nothing here reads M5.Imu,
  // but IMU_Class::begin() still probes six parts on every wake -- and the
  // AK8963 probe spends three unconditional vTaskDelay(10) *before* it checks
  // WhoAmI, so a board with none of them pays the delay in full.
  // internal_rtc stays on: M5.Rtc is used, on this same bus.
  cfg.internal_imu = false;
  M5.begin(cfg);

  store::begin();

  // M5.begin() already raised the panel rail, but M5GFX never touches PWR_CFG
  // or HOLD_CFG -- verified in M5GFX 0.2.28 M5GFX.cpp, which writes only 0x0A,
  // 0x16, 0x10, 0x13, 0x11 and 0x09. Both of those registers clear on every
  // reset, and every wake here IS a reset, so this runs each time (10.2).
  if (!power::begin()) {
    log_e("PMIC did not answer -- the panel will not come up");
  }

  // Reading the wake source also clears the PMIC's latch, so it has to happen
  // on every path out of setup() including the skip below -- a latch left
  // standing makes the *next* boot report this wake's reason instead of its
  // own (6.4).
  const power::WakeReason reason = power::wakeReason();
  M5.Rtc.setSystemTimeFromRtc();
  // kFwMarker is printed rather than merely defined, and that is the point:
  // `used` stops the compiler discarding it but not the linker's
  // --gc-sections, which did exactly that on the first build. One reference is
  // what keeps the version findable from outside the binary.
  log_i("wake: %s, %s", power::wakeReasonName(reason), kFwMarker);

  net::Telemetry telemetry;
  telemetry.device_id = net::deviceId();
  telemetry.wake_reason = power::wakeReasonName(reason);

  // --- the quiet window: everything on the internal I2C bus, radio still off
  //
  // This runs before the fault check below, not after, because the brownout
  // case needs the battery reading to decide anything sensible. It is 150 ms
  // of I2C with the radio off, so a skipped wake pays almost nothing for it.
  telemetry.battery_mv = power::batteryMilliVolts();
  telemetry.battery_pct = power::batteryPercent(telemetry.battery_mv);
  telemetry.charging = power::externalPower(&telemetry.charging_known);
  if (!power::readSht40(&telemetry.temperature_c, &telemetry.humidity_pct)) {
    // Stays NAN, which net omits from the headers entirely. The server renders
    // "--" rather than believing a wrong temperature.
    log_w("SHT40 did not answer");
  }
  log_i("battery %u mV (%u %%), external power %s, %.1f C / %.1f %%RH", telemetry.battery_mv,
        telemetry.battery_pct,
        telemetry.charging_known ? (telemetry.charging ? "yes" : "no") : "unknown",
        telemetry.temperature_c, telemetry.humidity_pct);

  // A fault reboot means the previous wake never finished. Running the same
  // sequence again straight away is how a reproducible fault becomes a reboot
  // loop that empties the cell just as surely as the fault would have. Skip
  // this wake and let the next slot try again -- by then the server, the AP,
  // or whatever else broke has had hours to recover. The panel keeps its last
  // good frame for free, so the cost of skipping is nothing but staleness.
  //
  // Only genuine faults belong in this list, and the omissions matter more
  // than the entries. ESP_RST_POWERON is what *every* normal wake looks like,
  // because the PMIC cuts the rails and puts them back -- skipping on it would
  // mean the panel is never updated again. ESP_RST_SW is not produced by any
  // path in this firmware. (ESP_RST_USB and ESP_RST_JTAG, the ones esptool
  // uses to reset the board after a flash, do not exist in this IDF version;
  // if they ever arrive with an upgrade, they must stay out of here or the
  // board will switch off before a single log line reaches the bench.)
  if (reset_reason == ESP_RST_TASK_WDT || reset_reason == ESP_RST_WDT ||
      reset_reason == ESP_RST_INT_WDT || reset_reason == ESP_RST_PANIC ||
      reset_reason == ESP_RST_BROWNOUT) {
    // A brownout says the 3V3 rail collapsed, and that is either a weak cell
    // or a load transient on a perfectly good one -- panel drive alone reaches
    // over 200 mA (11.7), and TX spikes land on top of it. Backing off six
    // hours on the second case would be six hours of darkness for nothing, so
    // ask the battery instead of assuming the expensive answer.
    const bool flat = telemetry.battery_mv != 0 && telemetry.battery_mv < BATTERY_LOW_MV;
    const uint32_t back_off =
        (reset_reason == ESP_RST_BROWNOUT && flat) ? SLEEP_LOW_BATTERY_S : SLEEP_RETRY_S;
    log_e("previous wake failed (reset reason %d) -- skipping this slot, back in %u s",
          static_cast<int>(reset_reason), static_cast<unsigned>(back_off));
    finishWake(back_off);
  }

  // A reading of 0 means the PMIC gave no usable answer, which is not the same
  // as a flat battery and must not trigger the charge prompt. Nor may an
  // unknown supply state: `charging` is only trustworthy when charging_known
  // says so, and a board on a cable must not be told to go and find a cable.
  if (telemetry.battery_mv != 0 && telemetry.battery_mv < BATTERY_LOW_MV &&
      !(telemetry.charging_known && telemetry.charging)) {
    log_w("battery below %u mV, skipping the whole cycle", BATTERY_LOW_MV);
    // Painted once and then left alone. Here the daily-refresh rule loses to
    // the flat battery it would be spending: at SLEEP_LOW_BATTERY_S an
    // ungated prompt would drive the panel four times a day to say the same
    // sentence to the same person.
    if (!alreadyShowing(ETAG_LOCAL_LOW_BATTERY)) {
      showLowBattery(telemetry.battery_mv, telemetry.battery_pct);
    }
    finishWake(SLEEP_LOW_BATTERY_S, /*conserve=*/true);
  }

  // Four states, one condition. They are the same problem wearing different
  // hats: the board needs a human, and until now it either asked on every
  // single wake or -- once configured -- could never ask again.
  //
  //   no WiFi credentials      first boot out of the box
  //   no device token          the row exists on the server, the board does not
  //                            know its half of it yet
  //   repeated 401/403         the operator rotated the token
  //   a long failure streak    the router was replaced and the stored SSID is
  //                            gone, so no backoff will ever help
  //
  // The last two are what closes "the portal never opens again": before this,
  // the only check was whether any SSID was stored, so a board past setup could
  // not be reconfigured without erasing NVS over USB.
  const bool wants_provisioning = !net::hasCredentials() || store::token().length() == 0 ||
                                  store::authFailures() >= AUTH_FAIL_BEFORE_PORTAL ||
                                  store::failureStreak() >= PORTAL_AFTER_FAILURES;

  if (wants_provisioning) {
    // hasCredentials() had to start the WiFi driver to ask. Shut it down again
    // before driving the panel: radio and panel never run together (9.7).
    net::disconnect();

    // Whether the portal actually opens is a separate question from whether it
    // is wanted, and this is where the old 620 mAh-a-day finding is closed.
    //
    // An unconfigured board used to paint the instructions and run a 900 s
    // portal on EVERY wake, sleeping SLEEP_RETRY_S in between: 24 sessions a
    // day, half a charge, on a board nobody had set up yet.
    //
    // Two automatic tries cover the person who walked off to read the router
    // label. After that it waits to be asked -- and it can be asked, because
    // the PMIC wakes on the power button and on USB power, and wakeReason()
    // has known which all along.
    //
    // Note the condition is `wants_provisioning && human`, not `human` alone: a
    // button press on a HEALTHY board must be an ordinary wake ("refresh now"),
    // not fifteen minutes of captive portal because somebody knocked the wall.
    const bool human = power::wakeWasHuman(reason);
    const uint16_t tries = store::setupTries();
    const bool open_portal = human || tries < SETUP_PORTAL_AUTO_TRIES;

    if (!open_portal) {
      // Nothing to fetch and nothing to paint. Do not even bring the radio up:
      // at six-hourly intervals an unconfigured board in a drawer lives out its
      // full idle life instead of flattening itself in two days.
      log_i("provisioning wanted, %u automatic tries spent -- waiting for a button",
            static_cast<unsigned>(tries));
      finishWake(SLEEP_UNCONFIGURED_S);
    }

    // Sentinel-gated, so this is painted once per state and not once per wake.
    // Decide once and derive the sentinel from it: comparing the two string literals by pointer
    // only works while the compiler happens to merge identical literals.
    const bool auth_problem = store::authFailures() >= AUTH_FAIL_BEFORE_PORTAL;
    const char *sentinel = auth_problem ? ETAG_LOCAL_AUTH : ETAG_LOCAL_SETUP;
    if (!alreadyShowing(sentinel)) {
      if (auth_problem) {
        showAuthProblem();
      } else {
        showPortalInstructions();
      }
      store::setEtag(sentinel);
    }
    // Park the panel before the portal, not after. The portal blocks for up to
    // fifteen minutes, and a panel left awake after a refresh is the case rule 3
    // calls permanent damage.
    panel::finish();

    const bool configured = net::runSetupPortal();
    net::disconnect();
    if (!configured) {
      store::setSetupTries(static_cast<uint16_t>(tries + 1));
      finishWake(tries + 1 < SETUP_PORTAL_AUTO_TRIES ? SLEEP_SETUP_RETRY_S
                                                     : SLEEP_UNCONFIGURED_S);
    }
    // Fresh credentials mean every ladder on this board is about a world that
    // no longer exists: a network that was replaced, or a token that was
    // rotated. Reset all three, or commissioning starts on the four-hour rung --
    // which is exactly when the operator wants fast feedback.
    store::setFailureStreak(0);
    store::setAuthFailures(0);
    store::setSetupTries(0);
    // The instructions screen was painted moments ago. Painting again now
    // would break the 180 s minimum gap (4.4 rule 1), so the first real frame
    // waits for the next wake.
    finishWake(SLEEP_MIN_S);
  }

  if (!net::connect()) {
    giveUpForNow();
  }
  telemetry.rssi = WiFi.RSSI();

  net::DisplayPlan plan;
  if (!net::fetchPlan(telemetry, &plan)) {
    // 401 and 403 are not network failures and must not go down the offline
    // ladder. The server answered; the radio, the router and DNS are all fine.
    // What is wrong is who we say we are, and the only cure is a person with
    // access to the server -- so this counts towards the portal instead.
    if (plan.status == 401 || plan.status == 403) {
      const uint16_t fails = static_cast<uint16_t>(store::authFailures() + 1);
      store::setAuthFailures(fails);
      log_e("server refused this device (%d), auth failure %u of %u", plan.status,
            static_cast<unsigned>(fails), static_cast<unsigned>(AUTH_FAIL_BEFORE_PORTAL));
      net::disconnect();
      // Deliberately SLEEP_RETRY_S and not the offline ladder: a server
      // redeploy can refuse a valid token for a couple of minutes, and the next
      // wake is where the portal decision gets made anyway.
      finishWake(SLEEP_RETRY_S);
    }
    giveUpForNow();  // turns the radio off on its way past
  }
  // Reaching here means the token was accepted.
  store::setAuthFailures(0);

  if (plan.server_epoch > 0) {
    // Keeps the RX8130CE roughly right for free, off a header we were already
    // receiving. It is what makes the RTC alarm usable as a backup wake path.
    //
    // Everything on the device is UTC and stays UTC: TZ is never set, so
    // localtime_r degenerates to gmtime_r and the RTC, the system clock and
    // the alarm all agree. Local time exists only in the pixels the server
    // renders -- which is the entire point of pushing timezones server-side
    // (9.6), and the reason no DST bug can live in this firmware.
    struct tm utc = {};
    gmtime_r(&plan.server_epoch, &utc);
    M5.Rtc.setDateTime(&utc);
  }

  // Decide about a firmware update here, but do not act on it here. The job
  // is carried to finishWake(), which runs it after the wake timer is armed and
  // the panel rail is cut.
  //
  // The ladder below runs cheapest-first, and the third rung is the one that
  // matters most. Without it, a registered version string that does not match
  // the one compiled into the image is an endless loop: offer, flash, still
  // report the old version, offer again. That has happened in production on the
  // sibling device; there it cost five minutes of notice screen a round, here it
  // would be three downloads a day and about 730 mAh a year. With the digest in
  // NVS the board fetches any given image exactly once.
  net::OtaJob ota;
  bool have_ota = false;
  if (plan.update_firmware) {
    const bool usable = plan.firmware_url.length() > 0 && plan.firmware_sha256.length() == 64;
    const bool is_new_version = plan.firmware_version != FW_VERSION;
    const bool seen_before = plan.firmware_sha256.equalsIgnoreCase(store::otaSha());
    const bool budget_left = !seen_before || store::otaAttempts() < OTA_ATTEMPT_MAX;
    // Measured in the quiet window before the radio came up -- the only
    // trustworthy reading this board takes.
    const bool charged = telemetry.battery_mv == 0 || telemetry.battery_mv >= OTA_MIN_BATTERY_MV;

    if (!usable) {
      log_w("ota offer incomplete, ignoring");
    } else if (!is_new_version) {
      log_i("ota: server offered %s, which is what we are running",
            plan.firmware_version.c_str());
    } else if (seen_before && !budget_left) {
      log_w("ota: %s was already tried %u times, giving up on it",
            plan.firmware_sha256.substring(0, 12).c_str(),
            static_cast<unsigned>(store::otaAttempts()));
    } else if (seen_before && store::otaAttempts() == 0) {
      // Installed on an earlier wake and waiting to boot. The server still
      // offers it because we still report the old version -- which we will,
      // right up until the new image runs.
      log_i("ota: %s is already written and boots on the next cold wake",
            plan.firmware_version.c_str());
    } else if (!charged) {
      log_w("ota: %u mV is below %u mV, deferring the update", telemetry.battery_mv,
            static_cast<unsigned>(OTA_MIN_BATTERY_MV));
    } else {
      ota.url = plan.firmware_url;
      ota.sha256 = plan.firmware_sha256;
      ota.version = plan.firmware_version;
      ota.size = plan.firmware_size;
      have_ota = true;
      log_i("ota: will install %s after this wake is armed", plan.firmware_version.c_str());
    }
  }

  uint8_t *image = nullptr;
  size_t image_len = 0;
  String etag;
  const net::Fetch result =
      net::fetchImage(plan.image_url, store::etag(), &image, &image_len, &etag);

  // Radio off before a single milliamp goes into the panel.
  net::disconnect();

  uint32_t next_sleep = plan.refresh_rate;
  if (next_sleep == 0) {
    next_sleep = store::sleepSeconds(SLEEP_FALLBACK_S);
  }

  switch (result) {
    case net::Fetch::NotModified:
      // The cheapest possible wake: no download, no repaint, roughly 20 % of a
      // full cycle (9.5). The server is responsible for changing the ETag once
      // a day so the panel still gets its mandatory refresh (4.4 rule 2).
      store::setFailureStreak(0);
      break;

    case net::Fetch::Ok: {
      const bool painted = panel::showImage(image, image_len);
      net::releaseImage(image);
      if (painted) {
        // Only now. Storing the ETag for a frame that failed to decode would
        // make the next wake answer 304 and leave the old image up forever.
        store::setEtag(etag);
        store::setFailureStreak(0);
      } else {
        store::setFailureStreak(static_cast<uint16_t>(store::failureStreak() + 1));
      }
      break;
    }

    case net::Fetch::Failed:
    default:
      giveUpForNow();
  }

  store::setSleepSeconds(next_sleep);
  finishWake(next_sleep, /*conserve=*/false, have_ota ? &ota : nullptr);
}

void loop() {
  // Unreachable: setup() always ends in power::powerOff(), which does not
  // return. Arduino needs the symbol to exist.
}
