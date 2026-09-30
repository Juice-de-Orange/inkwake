#include "power.h"

// M5Unified must come first. On IDF >= 5.3 the reverse order makes i2c_bus.h
// and driver/i2c.h collide over i2c_config_t (HARDWARE.md 7.3).
#include <M5Unified.h>
#include <M5PM1.h>

#include <Arduino.h>
#include <esp_sleep.h>
#include <esp_task_wdt.h>

#include <algorithm>

#include "config.h"

namespace power {
namespace {

constexpr uint8_t PM1_ADDR = 0x6E;
constexpr uint8_t SHT40_ADDR = 0x44;

//: The PMIC defaults to 100 kHz. It tolerates 400 kHz, but this bus is shared
//: with four other devices and the register writes below are the ones that
//: must not fail, so they run at the conservative speed.
constexpr uint32_t PM1_I2C_HZ = 100000;
constexpr uint32_t SHT40_I2C_HZ = 100000;

//: PYG0 gates the panel rail, PYG3 the microSD rail, PYG2 carries the RTC's
//: interrupt into the PMIC's wake logic (HARDWARE.md 3.2).
constexpr m5pm1_gpio_num_t PIN_EPD = M5PM1_GPIO_NUM_0;
constexpr m5pm1_gpio_num_t PIN_SD = M5PM1_GPIO_NUM_3;
constexpr m5pm1_gpio_num_t PIN_RTC_IRQ = M5PM1_GPIO_NUM_2;

M5PM1 pm1;
bool pm1_ready = false;

//: PWR_CFG bit 3. HARDWARE.md contradicts itself here: 3.2 maps
//: BOOST5V_EN_PP to the Grove port's 5 V output, while 10.2 calls the same bit
//: "the ~15 V e-ink rail" -- and the M5PM1 library's own register doc agrees
//: with 3.2. Since this panel carries its own driver and PMIC behind the glass
//: (4.1), it almost certainly makes its own high voltage and this bit is just
//: Grove power.
//:
//: We set it anyway, because the risk is lopsided: leaving it off to save a
//: few hundred microamps costs a blank screen if 10.2 turns out to be right,
//: and the one community configuration known to work on this board sets it.
//: Build with -DEINK_PMIC_ENABLE_BOOST=0 once somebody can actually measure
//: the idle current -- that is the first thing to try if 92 uA is not reached.
#ifndef EINK_PMIC_ENABLE_BOOST
#define EINK_PMIC_ENABLE_BOOST 1
#endif

//: I2C_CFG. Only the low nibble (idle-sleep timeout) is ours to change; the
//: speed bit above it belongs to whoever configured the bus.
constexpr uint8_t REG_I2C_CFG = 0x09;
constexpr uint8_t I2C_CFG_SLEEP_TIMEOUT = 0x0F;

//: Explicit read-modify-write rather than I2C_Class::bitOn, whose "bit"
//: argument is a mask despite the name.
bool clearRegBits(uint8_t reg, uint8_t mask) {
  uint8_t value = 0;
  if (!M5.In_I2C.readRegister(PM1_ADDR, reg, &value, 1, PM1_I2C_HZ)) {
    return false;
  }
  const uint8_t updated = value & static_cast<uint8_t>(~mask);
  if (updated == value) {
    return true;
  }
  return M5.In_I2C.writeRegister8(PM1_ADDR, reg, updated, PM1_I2C_HZ);
}

//: Drive one of the PMIC's rails from a cold, unknown register state.
bool configureRail(m5pm1_gpio_num_t pin, bool on) {
  bool ok = pm1.gpioSetFunc(pin, M5PM1_GPIO_FUNC_GPIO) == M5PM1_OK;
  ok = (pm1.gpioSetMode(pin, M5PM1_GPIO_MODE_OUTPUT) == M5PM1_OK) && ok;
  ok = (pm1.gpioSetDrive(pin, M5PM1_GPIO_DRIVE_PUSHPULL) == M5PM1_OK) && ok;
  ok = (pm1.gpioSetOutput(pin, on ? 1 : 0) == M5PM1_OK) && ok;
  return ok;
}

//: Timer registers, read back after arming. M5PM1.h documents TIM_CNT
//: 0x38..0x3B as a 31-bit second count, LSB first, and TIM_CFG 0x3C as
//: [3] ARM plus [2:0] ACTION.
//:
//: They are read back because timerSet() reports M5PM1_OK as soon as three
//: I2C writes were acknowledged -- that proves the PMIC accepted the bytes and
//: says nothing about whether the countdown is running. This PMIC is
//: documented to go unresponsive partway through a sequence when idle-sleep is
//: enabled (10.13), which is exactly the shape of failure that would leave the
//: board switched off with no way back.
constexpr uint8_t REG_TIM_CNT = 0x38;
constexpr uint8_t REG_TIM_CFG = 0x3C;
constexpr uint8_t TIM_CFG_ARM = 0x08;
constexpr uint8_t TIM_CFG_MASK = 0x0F;

//: Worth a register dump on the first real board (11): M5PM1.h calls TIM_CFG
//: bit 3 auto-reload ("0=one-shot, 1=auto") while timerSet()'s own comment
//: calls it "start timer". timerSet always sets it, so the check below is
//: right either way -- but if the header is the accurate one, a timer armed
//: once keeps firing on its own, and most of this module's caution about
//: never reaching the arming code stops being necessary.
bool timerIsArmed(uint32_t requested_s) {
  uint8_t cfg = 0;
  if (!M5.In_I2C.readRegister(PM1_ADDR, REG_TIM_CFG, &cfg, 1, PM1_I2C_HZ)) {
    log_e("timer config could not be read back");
    return false;
  }
  const uint8_t want = TIM_CFG_ARM | static_cast<uint8_t>(M5PM1_TIM_ACTION_POWERON);
  if ((cfg & TIM_CFG_MASK) != want) {
    log_e("timer config reads back 0x%02X, wanted 0x%02X", cfg & TIM_CFG_MASK, want);
    return false;
  }

  // The count is logged, never judged. Which direction this register runs is
  // undocumented -- "Timer counter byte 0..3" is all M5PM1.h says -- and if it
  // counts up rather than down it reads 0 immediately after timerSet(). A
  // check on that value would then declare a perfectly healthy PMIC broken on
  // every single wake, send the board down the deep-sleep fallback, and
  // switch it off dark after PMIC_FALLBACK_MAX. That is a worse failure than
  // the one this whole function is guarding against, and it would happen on
  // working hardware.
  //
  // The config byte is enough to do the job: timerSet() writes it last but
  // one, so reading back ARM plus the POWERON action proves the write
  // sequence reached the PMIC and was not swallowed by an I2C bus that
  // stopped answering partway through (10.13). Dump the count on the first
  // real board and tighten this if it turns out to be a countdown (11).
  uint8_t raw[4] = {0};
  if (M5.In_I2C.readRegister(PM1_ADDR, REG_TIM_CNT, raw, sizeof(raw), PM1_I2C_HZ)) {
    const uint32_t counted = static_cast<uint32_t>(raw[0]) |
                             (static_cast<uint32_t>(raw[1]) << 8) |
                             (static_cast<uint32_t>(raw[2]) << 16) |
                             (static_cast<uint32_t>(raw[3] & 0x7F) << 24);
    log_i("wake timer armed, count register reads %u s (asked for %u)",
          static_cast<unsigned>(counted), static_cast<unsigned>(requested_s));
  } else {
    log_w("wake timer armed, but the count register could not be read back");
  }
  return true;
}

//: Primary wake path: the PMIC's own countdown. A plain timer, so it works on
//: a board whose RTC has never been set -- which is every factory-fresh unit,
//: and any unit whose backup cell ran flat. An RTC alarm on a wrong clock is a
//: device that never wakes again.
bool armPmicTimer(uint32_t delay_s) {
  if (pm1.timerClear() != M5PM1_OK) {
    // Not fatal by itself: timerSet overwrites the same registers, and the
    // read-back is the only opinion that counts.
    log_w("timerClear failed");
  }
  if (pm1.timerSet(delay_s, M5PM1_TIM_ACTION_POWERON) != M5PM1_OK) {
    log_w("timerSet failed");
    return false;
  }
  return timerIsArmed(delay_s);
}

//: Released immediately before each call that does not return -- the PMIC
//: shutdown, the deep sleep, and the parking loop -- and not one line
//: earlier. Everything before that point, the I2C exchange that arms the timer
//: very much included, is what the wake watchdog exists to catch: a bus wedged
//: halfway through arming is one of the ways this board goes dark for good,
//: and a reboot retries the whole sequence from a clean start.
void releaseWatchdog() { esp_task_wdt_delete(nullptr); }

[[noreturn]] void park() {
  while (true) {
    delay(1000);
  }
}

uint8_t sht40Crc(const uint8_t *data, size_t len) {
  // Sensirion's CRC-8: polynomial 0x31, init 0xFF, no reflection.
  uint8_t crc = 0xFF;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x31) : static_cast<uint8_t>(crc << 1);
    }
  }
  return crc;
}

}  // namespace

bool wakeWasHuman(WakeReason reason) {
  // UsbPower and FiveVolt count: plugging in a cable is somebody standing
  // there just as much as pressing the button is, and it is the natural way to
  // approach a board that has gone quiet. ResetButton counts because it is the
  // only button some enclosures leave reachable.
  //
  // Timer and Rtc emphatically do not -- they are the normal case, and treating
  // them as human would restore exactly the behaviour this exists to end.
  return reason == WakeReason::PowerButton || reason == WakeReason::ResetButton ||
         reason == WakeReason::UsbPower || reason == WakeReason::FiveVolt;
}

const char *wakeReasonName(WakeReason reason) {
  switch (reason) {
    case WakeReason::Timer:
      return "timer";
    case WakeReason::Rtc:
      return "rtc";
    case WakeReason::PowerButton:
      return "power_button";
    case WakeReason::ResetButton:
      return "reset_button";
    case WakeReason::CommandReset:
      return "command_reset";
    case WakeReason::UsbPower:
      return "usb";
    case WakeReason::FiveVolt:
      return "five_volt";
    default:
      return "unknown";
  }
}

bool begin() {
  // Borrow the bus M5.begin() already brought up rather than opening a second
  // driver on the same two pins.
  pm1_ready = (pm1.begin(&M5.In_I2C, PM1_ADDR, PM1_I2C_HZ) == M5PM1_OK);
  if (!pm1_ready) {
    return false;
  }

  // Every write below is idempotent, so running this again after a
  // half-finished boot is safe.

  // The PMIC watchdog would reset us in the middle of a 15-30 s refresh. This
  // firmware is awake for well under a minute and has nothing to supervise.
  bool ok = pm1.wdtSet(0) == M5PM1_OK;

  // The panel rail is OFF at cold boot and hangs off the PMIC, not an ESP32
  // pin (10.2). M5Unified already raised it during M5.begin(); repeating it
  // costs four I2C writes and makes this module correct on its own.
  ok = configureRail(PIN_EPD, true) && ok;
  ok = configureRail(PIN_SD, true) && ok;

  // Charge, DCDC, LDO, and the boost bit discussed above. PWR_CFG auto-clears
  // on EVERY reset and every wake here IS a reset, so this is not a one-time
  // setup step (10.2).
  constexpr uint8_t pwr_mask = EINK_PMIC_ENABLE_BOOST ? 0x0F : 0x07;
  ok = (pm1.setPowerConfig(pwr_mask, pwr_mask) == M5PM1_OK) && ok;

  // HOLD_CFG. Without it the device freezes the moment the USB cable comes out
  // and never charges again (10.10). The three bits HARDWARE.md names -- 0, 3
  // and 5 -- are exactly the panel rail, the microSD rail and the 3V3 LDO.
  ok = (pm1.gpioSetPowerHold(PIN_EPD, true) == M5PM1_OK) && ok;
  ok = (pm1.gpioSetPowerHold(PIN_SD, true) == M5PM1_OK) && ok;
  ok = (pm1.ldoSetPowerHold(true) == M5PM1_OK) && ok;

  // Last, because it changes whether the PMIC answers at all: with idle-sleep
  // enabled it goes unresponsive partway through a sequence like this one
  // (10.13). Only the timeout nibble is cleared -- the speed bit beside it
  // belongs to whoever configured the bus.
  ok = clearRegBits(REG_I2C_CFG, I2C_CFG_SLEEP_TIMEOUT) && ok;

  return ok;
}

WakeReason wakeReason() {
  if (!pm1_ready) {
    return WakeReason::Unknown;
  }
  uint8_t src = 0;
  // Reading with CLEAN_ALL also clears the latch, so a stale flag cannot make
  // the next boot look like a button press.
  if (pm1.getWakeSource(&src, M5PM1_CLEAN_ALL) != M5PM1_OK) {
    return WakeReason::Unknown;
  }
  if (src & M5PM1_WAKE_SRC_TIM) return WakeReason::Timer;
  if (src & M5PM1_WAKE_SRC_EXT_WAKE) return WakeReason::Rtc;
  if (src & M5PM1_WAKE_SRC_PWRBTN) return WakeReason::PowerButton;
  if (src & M5PM1_WAKE_SRC_RSTBTN) return WakeReason::ResetButton;
  if (src & M5PM1_WAKE_SRC_CMD_RST) return WakeReason::CommandReset;
  if (src & M5PM1_WAKE_SRC_VIN) return WakeReason::UsbPower;
  if (src & M5PM1_WAKE_SRC_5VINOUT) return WakeReason::FiveVolt;
  return WakeReason::Unknown;
}

uint16_t batteryMilliVolts() {
  uint16_t samples[BATTERY_SAMPLES];
  uint8_t count = 0;

  for (uint8_t i = 0; i < BATTERY_SAMPLES; ++i) {
    const int32_t mv = M5.Power.getBatteryVoltage();
    // A plausibility window, not a clamp: the PMIC returns 0 when the read
    // failed, and a single bad transaction must not drag the median down into
    // the low-battery branch and blank the dashboard for six hours.
    if (mv > 2500 && mv < 5000) {
      samples[count++] = static_cast<uint16_t>(mv);
    }
    delay(BATTERY_SAMPLE_GAP_MS);
  }

  if (count == 0) {
    return 0;
  }
  std::sort(samples, samples + count);
  return samples[count / 2];
}

uint8_t batteryPercent(uint16_t millivolts) {
  if (millivolts == 0) {
    return 255;
  }
  if (millivolts <= BATTERY_EMPTY_MV) {
    return 0;
  }
  if (millivolts >= BATTERY_FULL_MV) {
    return 100;
  }
  const uint32_t span = BATTERY_FULL_MV - BATTERY_EMPTY_MV;
  return static_cast<uint8_t>(((millivolts - BATTERY_EMPTY_MV) * 100UL) / span);
}

bool externalPower(bool *known) {
  if (known != nullptr) {
    *known = false;
  }
  if (!pm1_ready) {
    return false;
  }

  // Register 0x04: which supply the PMIC is running the board from. This is
  // the question the hardware can actually answer -- see the header for why
  // "charging" is not.
  //
  // Whitelisted, not blacklisted: getPowerSource() masks the register with
  // 0x07 while only 0..3 are defined, so a reserved bit or a widened field
  // would otherwise arrive as a confident "running on battery" and paint the
  // charge prompt on a board sitting on a cable -- the exact bug this
  // function was written to remove.
  m5pm1_pwr_src_t src = M5PM1_PWR_SRC_UNKNOWN;
  if (pm1.getPowerSource(&src) == M5PM1_OK &&
      (src == M5PM1_PWR_SRC_5VIN || src == M5PM1_PWR_SRC_5VINOUT ||
       src == M5PM1_PWR_SRC_BAT)) {
    if (known != nullptr) {
      *known = true;
    }
    return src == M5PM1_PWR_SRC_5VIN || src == M5PM1_PWR_SRC_5VINOUT;
  }

  // Fallback, and M5's own criterion elsewhere: a VIN reading above a volt
  // means something is feeding the barrel, whatever the source register says.
  uint16_t vin_mv = 0;
  if (pm1.readVin(&vin_mv) == M5PM1_OK) {
    if (known != nullptr) {
      *known = true;
    }
    return vin_mv > 1000;
  }

  return false;
}

bool readSht40(float *temperature_c, float *humidity_pct) {
  // 0xFD: measure once, highest repeatability. ~8.3 ms typical, 10 ms max.
  if (!M5.In_I2C.start(SHT40_ADDR, false, SHT40_I2C_HZ)) {
    return false;
  }
  const uint8_t cmd = 0xFD;
  const bool sent = M5.In_I2C.write(&cmd, 1);
  M5.In_I2C.stop();
  if (!sent) {
    return false;
  }

  delay(12);

  uint8_t raw[6] = {0};
  if (!M5.In_I2C.start(SHT40_ADDR, true, SHT40_I2C_HZ)) {
    return false;
  }
  const bool got = M5.In_I2C.read(raw, sizeof(raw));
  M5.In_I2C.stop();
  if (!got) {
    return false;
  }

  // Both CRCs, both checked. A half-corrupted reading is worse than no
  // reading: it goes into the frame and nothing downstream can tell.
  if (sht40Crc(raw, 2) != raw[2] || sht40Crc(raw + 3, 2) != raw[5]) {
    return false;
  }

  const uint16_t t_ticks = static_cast<uint16_t>((raw[0] << 8) | raw[1]);
  const uint16_t h_ticks = static_cast<uint16_t>((raw[3] << 8) | raw[4]);

  const float t = -45.0f + 175.0f * (static_cast<float>(t_ticks) / 65535.0f);
  float h = -6.0f + 125.0f * (static_cast<float>(h_ticks) / 65535.0f);
  // The datasheet's transfer function overshoots at the ends; clamping is the
  // documented remedy, not a fudge.
  h = std::min(100.0f, std::max(0.0f, h));

  if (temperature_c != nullptr) *temperature_c = t;
  if (humidity_pct != nullptr) *humidity_pct = h;
  return true;
}

void setEpdRail(bool on) {
  if (pm1_ready) {
    pm1.gpioSetOutput(PIN_EPD, on ? 1 : 0);
  }
}

bool armWake(uint32_t seconds) {
  const uint32_t delay_s = std::min(SLEEP_MAX_S, std::max(SLEEP_MIN_S, seconds));
  log_i("arming wake in %u s", delay_s);

  // Kill any alarm we or a previous firmware left behind. An RX8130CE alarm
  // matches on hour:minute and repeats daily; forgetting one costs an extra
  // wake every day forever.
  M5.Rtc.clearIRQ();
  M5.Rtc.disableIRQ();

  if (!pm1_ready) {
    // One more go at the bus before writing the PMIC off. M5PM1::begin() is an
    // escalation ladder in its own right -- wake signal, 10 ms, retry, 800 ms,
    // retry, then drop to 400 kHz -- so this is a different attempt rather
    // than a louder one. Retrying the register write instead would add
    // nothing: _writeReg already retries twice internally.
    pm1_ready = (pm1.begin(&M5.In_I2C, PM1_ADDR, PM1_I2C_HZ) == M5PM1_OK);
  }
  if (!pm1_ready) {
    log_e("PMIC will not answer -- no wake can be armed");
    return false;
  }

  bool armed = armPmicTimer(delay_s);
  if (!armed) {
    log_w("wake timer did not take -- reopening the PMIC and trying once more");
    pm1_ready = (pm1.begin(&M5.In_I2C, PM1_ADDR, PM1_I2C_HZ) == M5PM1_OK);
    armed = pm1_ready && armPmicTimer(delay_s);
  }

  // Backup path: if the RTC does hold a plausible time, set the alarm too and
  // let it reach the PMIC over PYG2. Both firing is harmless -- the second one
  // lands while we are already awake and gets cleared above.
  //
  // It stays a backup and never counts as "armed" on its own. It needs a
  // plausible clock *and* the PMIC's GPIO wake logic, so when the timer above
  // could not be verified there is no reason left to trust this either -- and
  // the two failure costs are wildly different: a fallback nap is fifteen
  // minutes of expensive sleep, a shutdown on an unverified wake path is
  // forever.
  //
  // Read straight out of time()/gmtime_r rather than through getLocalTime():
  // that helper loops while (millis() - start) <= ms, so with ms = 0 a single
  // tick between the two calls makes it return false and silently drop this
  // path. mktime is avoided for the same class of reason -- it interprets its
  // argument in the current timezone, and everything here is UTC.
  const time_t now_epoch = time(nullptr);
  struct tm now {};
  gmtime_r(&now_epoch, &now);
  if (now.tm_year >= 126 /* 2026 */) {
    const time_t target = now_epoch + static_cast<time_t>(delay_s);
    struct tm wake {};
    gmtime_r(&target, &wake);
    M5.Rtc.setAlarmIRQ(&wake);

    pm1.gpioSetFunc(PIN_RTC_IRQ, M5PM1_GPIO_FUNC_WAKE);
    pm1.gpioSetPull(PIN_RTC_IRQ, M5PM1_GPIO_PULL_UP);
    pm1.gpioSetWakeEdge(PIN_RTC_IRQ, M5PM1_GPIO_WAKE_FALLING);
    pm1.gpioSetWakeEnable(PIN_RTC_IRQ, true);
  }

  return armed;
}

[[noreturn]] void powerOff(bool wake_armed, bool give_up, uint32_t nap_s) {
  // No Serial.flush() here. With ARDUINO_USB_MODE=1 `Serial` is the HWCDC
  // object, HWCDC::begin() is never called (M5Unified only calls it when
  // cfg.serial_baudrate is non-zero, and it is not), so tx_ring_buf stays null
  // and HWCDC::flush() returns on its first line. It looked like a guarantee
  // that the last log line got out, in the one function that must reach
  // sysCmd(OFF) on every path, and it never was one.
  //
  // Nothing replaces it: log_printfv already blocks on uart_ll_is_tx_idle
  // after every line it writes, so UART0 is drained by the time we get here.

#ifdef EINK_DEV_NO_SHUTDOWN
  // Bench builds: staying alive keeps the USB CDC port enumerated so the next
  // upload does not need the download-mode button dance. The watchdog has to
  // be released first, or the bench reboots every WAKE_WATCHDOG_S.
  log_w("EINK_DEV_NO_SHUTDOWN set -- idling instead of powering off");
  (void)wake_armed;
  (void)give_up;
  (void)nap_s;
  releaseWatchdog();
  park();
#else
  if (wake_armed || give_up) {
    if (!wake_armed) {
      log_e("giving up on the PMIC -- switching off dark");
    }
    if (pm1_ready) {
      // The watchdog stays armed across this write, and that is the whole
      // point of it: sysCmd() returns M5PM1_ERR_I2C_COMM when the PMIC has
      // gone quiet since begin() -- the idle-sleep lapse of 10.13 that
      // clearRegBits(REG_I2C_CFG, ...) in begin() exists to head off, not
      // something we can rule out here. Dropping the watchdog first and then
      // parking on a write that never landed is the one combination with no
      // way out: park() spins at run current, nothing is left to interrupt
      // it, and the cell is flat long before anyone notices a dark panel.
      const m5pm1_err_t rc = pm1.sysCmd(M5PM1_SYS_CMD_OFF);
      if (rc == M5PM1_OK) {
        releaseWatchdog();
        park();  // the rail drops out from under us within milliseconds
      }
      log_e("PMIC refused the OFF command (%d) -- falling through to M5Unified",
            static_cast<int>(rc));
    }
    // A wake was armed and the PMIC will not switch us off. Do NOT fall into
    // M5Unified's path here: on this board Power_Class::_powerOff() ends in
    // esp_deep_sleep_start() with *no wake source* -- the ext1 rescue beneath
    // it is compiled out for anything but C5/C61, and board_M5PaperColor has
    // no POWER_HOLD pin to pulse. The armed PMIC timer cannot save us either,
    // because its action is POWERON and the system never powered off. That
    // leaves the board at the 5-10 mA of 6.3 with nothing to end it: flat cell
    // in about two days, dark wall, recovery only over USB.
    //
    // The S3's own timer costs the same milliamps but ends. Take it directly
    // rather than through M5.Power.deepSleep(), for the reason spelled out in
    // the branch below: that one first runs M5.Display.sleep() + waitDisplay(),
    // up to 20 s of BUSY polling against a rail panel::finish() already cut --
    // and the watchdog would be gone by then.
    if (wake_armed && !give_up) {
      log_e("PMIC unreachable but a wake was armed -- S3 deep sleep for %u s",
            static_cast<unsigned>(nap_s));
      esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(nap_s) * 1000000ULL);
      releaseWatchdog();
      esp_deep_sleep_start();
      park();
    }

    releaseWatchdog();

    // give_up: going dark is what this branch decided, for the cell's sake, so
    // a milliamp nap would work against its own purpose. M5Unified's path is
    // all that is left. One correction to what this comment used to claim: it
    // is *not* an independent second attempt. M5PM1_Class defaults to
    // &In_I2C (M5PM1_Class.hpp:16), which is the very object power.cpp hands
    // our own instance -- same bus, same address 0x6E, same register 0x0C, and
    // without our retry count. It is worth trying because it is free, not
    // because it is independent. If it fails, the deep sleep it falls into has
    // no wake source, and at single-digit milliamps that still beats park() at
    // run current by an order of magnitude.
    M5.Power.powerOff();
    park();
  }

  // Nothing above could arm a wake, so switching off would be permanent. Take
  // the S3's own deep-sleep timer instead: it costs what 6.3 says an ESP32
  // deep sleep costs on this board, but it does not go through the PMIC, so
  // it works when the PMIC is exactly what is broken.
  //
  // The raw IDF call rather than M5.Power.deepSleep(), which first runs
  // M5.Display.sleep() and waitDisplay() -- up to 20 s of BUSY polling
  // against a panel rail that panel::finish() has already cut.
  log_e("no wake armed -- deep sleeping %u s to try again", static_cast<unsigned>(nap_s));
  esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(nap_s) * 1000000ULL);
  releaseWatchdog();
  esp_deep_sleep_start();
  park();
#endif
}

}  // namespace power
