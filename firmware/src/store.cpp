#include "store.h"

#include <Preferences.h>

namespace store {
namespace {

//: Kept distinct from the factory firmware's "papercolor" namespace so a
//: reflash in either direction cannot read the other's keys as its own. The
//: name predates the project's current one and stays: renaming it would make
//: every board that is already on a wall forget its token and server address.
constexpr const char *NAMESPACE = "einkmax";

constexpr const char *KEY_ETAG = "etag";
constexpr const char *KEY_SLEEP = "sleep_s";
//: Not "api_key". That key still holds a server-minted value on boards coming
//: from an earlier firmware, and reusing it would turn "never configured" into
//: a 401 nobody can explain. NVS keys are capped at 15 characters, which is why
//: all of these are short rather than descriptive.
constexpr const char *KEY_TOKEN = "tok";
constexpr const char *KEY_SERVER = "srv";
constexpr const char *KEY_STREAK = "fail_n";
constexpr const char *KEY_PMIC_NAPS = "pmic_n";
constexpr const char *KEY_OTA_SHA = "ota_sha";
constexpr const char *KEY_OTA_TRIES = "ota_n";
constexpr const char *KEY_AUTH_FAILS = "auth_n";
constexpr const char *KEY_SETUP_TRIES = "setup_n";

Preferences prefs;
bool open = false;

}  // namespace

bool begin() {
  open = prefs.begin(NAMESPACE, false);
  return open;
}

void end() {
  if (open) {
    prefs.end();
    open = false;
  }
}

String etag() { return open ? prefs.getString(KEY_ETAG, "") : String(); }

void setEtag(const String &value) {
  if (!open || prefs.getString(KEY_ETAG, "") == value) {
    return;
  }
  prefs.putString(KEY_ETAG, value);
}

uint32_t sleepSeconds(uint32_t fallback) {
  return open ? prefs.getUInt(KEY_SLEEP, fallback) : fallback;
}

void setSleepSeconds(uint32_t value) {
  if (!open || prefs.getUInt(KEY_SLEEP, 0) == value) {
    return;
  }
  prefs.putUInt(KEY_SLEEP, value);
}

String token() { return open ? prefs.getString(KEY_TOKEN, "") : String(); }

void setToken(const String &value) {
  if (!open || prefs.getString(KEY_TOKEN, "") == value) {
    return;
  }
  prefs.putString(KEY_TOKEN, value);
}

String serverUrl() { return open ? prefs.getString(KEY_SERVER, "") : String(); }

void setServerUrl(const String &value) {
  if (!open || prefs.getString(KEY_SERVER, "") == value) {
    return;
  }
  prefs.putString(KEY_SERVER, value);
}

uint16_t failureStreak() { return open ? prefs.getUShort(KEY_STREAK, 0) : 0; }

void setFailureStreak(uint16_t value) {
  if (!open || prefs.getUShort(KEY_STREAK, 0) == value) {
    return;
  }
  prefs.putUShort(KEY_STREAK, value);
}

uint16_t pmicNaps() { return open ? prefs.getUShort(KEY_PMIC_NAPS, 0) : 0; }

void setPmicNaps(uint16_t value) {
  if (!open || prefs.getUShort(KEY_PMIC_NAPS, 0) == value) {
    return;
  }
  prefs.putUShort(KEY_PMIC_NAPS, value);
}

String otaSha() { return open ? prefs.getString(KEY_OTA_SHA, "") : String(); }

void setOtaSha(const String &value) {
  if (!open || prefs.getString(KEY_OTA_SHA, "") == value) {
    return;
  }
  prefs.putString(KEY_OTA_SHA, value);
}

uint16_t otaAttempts() { return open ? prefs.getUShort(KEY_OTA_TRIES, 0) : 0; }

void setOtaAttempts(uint16_t value) {
  if (!open || prefs.getUShort(KEY_OTA_TRIES, 0) == value) {
    return;
  }
  prefs.putUShort(KEY_OTA_TRIES, value);
}

uint16_t authFailures() { return open ? prefs.getUShort(KEY_AUTH_FAILS, 0) : 0; }

void setAuthFailures(uint16_t value) {
  if (!open || prefs.getUShort(KEY_AUTH_FAILS, 0) == value) {
    return;
  }
  prefs.putUShort(KEY_AUTH_FAILS, value);
}

uint16_t setupTries() { return open ? prefs.getUShort(KEY_SETUP_TRIES, 0) : 0; }

void setSetupTries(uint16_t value) {
  if (!open || prefs.getUShort(KEY_SETUP_TRIES, 0) == value) {
    return;
  }
  prefs.putUShort(KEY_SETUP_TRIES, value);
}

}  // namespace store
