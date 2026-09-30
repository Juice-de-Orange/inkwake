// M5PM1 PMIC, battery, the SHT40, and the shutdown that makes the whole design
// work.
//
// The central fact this module encodes: ESP32 deep sleep is useless on this
// board. It measures 5-10 mA because the rails keep running, which is ~1.8
// days on a 1250 mAh cell. A full PMIC shutdown measures 92 uA -- a factor of
// 70 (HARDWARE.md 6.3). So the device does not sleep, it switches off, and
// every wake is a cold boot. Nothing survives it except NVS, the PMIC's 32
// bytes of RTC RAM, and the panel image itself.
//
// The SHT40 lives here rather than in a sensors module because it shares the
// internal I2C bus with the PMIC and must be read in the same quiet window,
// before the radio comes up and starts moving the supply around.

#pragma once

#include <stdint.h>

namespace power {

//: Why the PMIC restored power. Reported to the server as WAKE_REASON so a
//: fleet of one can still tell "scheduled" from "someone pressed the button".
enum class WakeReason : uint8_t {
  Unknown,
  Timer,        //: our own countdown -- the normal case
  Rtc,          //: RX8130CE alarm via PYG2, the redundant backup path
  PowerButton,
  ResetButton,
  CommandReset,
  UsbPower,
  FiveVolt,
};

const char *wakeReasonName(WakeReason reason);

//: Was a person standing at the board when it woke?
//:
//: The PMIC distinguishes its wake sources and this firmware has logged them
//: since the first day without ever acting on one. It is what turns "open the
//: setup portal on every wake until somebody comes" -- half a charge a day --
//: into "open it twice, then wait to be asked".
bool wakeWasHuman(WakeReason reason);

//: Re-assert every PMIC register the reset cleared. Must run on EVERY boot,
//: immediately after M5.begin(). Returns false if the PMIC did not answer at
//: all, which means the panel will not come up either.
bool begin();

WakeReason wakeReason();

//: Median of BATTERY_SAMPLES readings. Call before the radio (9.6).
//: Returns 0 when the PMIC gave no usable answer -- 0 means "unknown", never
//: "empty", and callers must not treat it as a low-battery condition.
uint16_t batteryMilliVolts();

//: Linear interpolation between the community calibration points. Returns 255
//: for "unknown" so it can be distinguished from a real 0 %.
uint8_t batteryPercent(uint16_t millivolts);

//: Whether the board is running off external power.
//:
//: Three-valued on purpose. M5Unified's isCharging() has no branch for
//: board_M5PaperColor at all and returns charge_unknown, which the old
//: two-valued wrapper flattened into a confident "no" -- so a board sitting on
//: a USB cable below BATTERY_LOW_MV would paint the charge prompt and sleep
//: six hours. The PMIC does answer a related question directly, so this asks
//: that one instead.
//:
//: What it reports is "external supply present", not "the cell is taking
//: charge": no CHG_STAT line is wired on this board, and M5.Power's charge
//: controls are silent no-ops here (6.2). Present-and-full looks the same as
//: present-and-charging. `known` is set false only when the PMIC gave no
//: usable answer, and then the value must not be believed in either
//: direction.
bool externalPower(bool *known);

//: Reads temperature and humidity over I2C. Returns false and leaves the
//: outputs untouched on any error, including a CRC mismatch -- a wrong
//: temperature would be rendered into the frame and silently believed.
bool readSht40(float *temperature_c, float *humidity_pct);

//: Cut or restore the panel's rail (PMIC PYG0). It is off at cold boot and
//: hangs off the PMIC, not a GPIO (10.2).
void setEpdRail(bool on);

//: Arm the wake, and report whether it actually took.
//:
//: Split from the shutdown below because the answer has to reach NVS, and NVS
//: has to be closed before the power goes. It is verified rather than
//: assumed: M5PM1::timerSet() returns OK as soon as three I2C writes were
//: acknowledged, which says the PMIC took the bytes and nothing about whether
//: the countdown is running. A board that switches off with an unarmed timer
//: never comes back on its own, so the registers are read back.
//:
//: `seconds` is clamped to [SLEEP_MIN_S, SLEEP_MAX_S] before it is used --
//: both ends of that range are panel-care rules, and a server that returns 0
//: must not be able to put the board into a power-cycle loop.
bool armWake(uint32_t seconds);

//: Switch everything off. Does not return. Call armWake() first.
//:
//: `wake_armed` is what armWake() returned. When it is false there is no way
//: back from a shutdown -- the PMIC's OFF command is the very thing that is
//: not working, and M5.Power.powerOff() is worse than useless here: on this
//: chip it falls through to esp_deep_sleep_start() with no wake source
//: configured at all. So that case takes an ESP32 deep sleep with a timer
//: instead, expensive but recoverable.
//:
//: `give_up` overrides that and switches off dark anyway, for when the board
//: has already spent enough naps on the problem, or when the cell is too low
//: to afford any more, and would otherwise deep-discharge.
//:
//: `nap_s` is how long that fallback sleep lasts. It has to come from the
//: caller rather than from SLEEP_PMIC_RETRY_S alone, or a wake that asked to
//: back off for six hours on a flat battery would quietly get fifteen-minute
//: retries instead -- the one situation where backing off hardest matters most.
[[noreturn]] void powerOff(bool wake_armed, bool give_up, uint32_t nap_s);

}  // namespace power
