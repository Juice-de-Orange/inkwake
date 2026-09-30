// The only things that survive a wake.
//
// A PMIC shutdown is a real power cut: RAM, PSRAM and even the ESP32's own RTC
// slow memory are gone by the next boot (HARDWARE.md 6.4). NVS in flash is the
// primary store, and this module is all of it.
//
// Every setter here writes only when the value actually changed. Not
// micro-optimisation: NVS is flash, the device writes three times a day for
// years, and an unconditional write of an unchanged ETag would spend erase
// cycles to store the same bytes.
//
// ONE RULE ABOUT CHANGING THIS SCHEMA, and it arrived with the firmware
// update: two consecutive firmware versions must be able to share it. The old
// image keeps running for the rest of the wake in which it writes the new one
// -- it writes the ETag, the sleep duration and the failure streak on its way
// out -- and the new image reads all of that on the next cold boot. A key
// renamed between two releases is therefore a value silently lost across every
// update, exactly once, in a way that looks like nothing at all.

#pragma once

#include <Arduino.h>
#include <stdint.h>

namespace store {

bool begin();

//: Commit and close. Call once, immediately before powering off.
void end();

//: The ETag of the image currently on glass. Empty when nothing was ever
//: painted, which is also what we send when we want an unconditional GET.
String etag();
void setEtag(const String &value);

//: Seconds to sleep, as last dictated by the server. Survives a server outage
//: so a board that cannot reach anyone still wakes on roughly the right
//: rhythm instead of falling back to a guess (9.2).
uint32_t sleepSeconds(uint32_t fallback);
void setSleepSeconds(uint32_t value);

//: The device token, `<id>.<secret>`, as typed into the setup portal. Sent as
//: `Authorization: Bearer`. Empty means this board has never been told who it
//: is, and it will not even bring the radio up.
//:
//: A NEW KEY, not a reuse of the old `api_key`. A board coming from an earlier
//: firmware carries a 30-character server-minted key there, and that would look
//: like a perfectly plausible bearer token -- it would produce a 401 and a
//: puzzled operator instead of the honest "not configured" state. The old key
//: is left where it lies as dead bytes; the migration is a non-event.
String token();
void setToken(const String &value);

//: The server this board talks to, as typed into the setup portal. Empty means
//: "use the compiled-in default", which is what a fresh unit does.
//:
//: This lives in NVS because the alternative is a compile-time constant, and a
//: compile-time constant means that moving the server involves taking the
//: board off the wall and finding a USB cable. There is no other input on this
//: device.
String serverUrl();
void setServerUrl(const String &value);

//: Cheap health signal: how many wakes in a row failed to reach the server.
//: The panel shows a staleness hint once this gets embarrassing.
uint16_t failureStreak();
void setFailureStreak(uint16_t value);

//: How many wakes in a row ended in the deep-sleep fallback because the PMIC
//: would not arm its wake timer. Has to survive the wake it describes, which
//: is the whole reason it lives in NVS: every one of those naps is a cold
//: boot, so a counter in RAM would always read one.
uint16_t pmicNaps();
void setPmicNaps(uint16_t value);

//: The SHA-256 of the last firmware image this board acted on -- installed or
//: written off. Compared before anything is downloaded, and it is the single
//: most important value in this file.
//:
//: Without it, a registered version string that does not match the one
//: compiled into the image produces an endless loop: the server offers, the
//: board flashes, the board still reports the old version, the server offers
//: again. That has actually happened on the sibling device, where it cost five
//: minutes of notice screen per round. Here it would be three downloads a day,
//: about 730 mAh a year -- a third of the budget. With this key the board
//: fetches any given image exactly once and ignores the offer for ever after.
String otaSha();
void setOtaSha(const String &value);

//: Consecutive attempts on the digest above. In NVS rather than RAM because
//: every attempt on this board is a separate cold boot, so a RAM counter would
//: always read one.
uint16_t otaAttempts();
void setOtaAttempts(uint16_t value);

//: Consecutive 401/403 answers from the server. Distinct from failureStreak on
//: purpose: the server ANSWERED, so the network is fine and the offline ladder
//: is the wrong medicine. This is an identity problem, and only a human at
//: the server can fix it.
uint16_t authFailures();
void setAuthFailures(uint16_t value);

//: How many times the setup portal has already been opened automatically. The
//: counter that turns "portal on every wake" into "portal twice, then only when
//: somebody presses the button".
uint16_t setupTries();
void setSetupTries(uint16_t value);

}  // namespace store
