// WiFi, provisioning, and the two HTTP requests that make up a wake.
//
// The protocol is TRMNL BYOS (HARDWARE.md 9.2): telemetry rides in request
// headers, so the server can render the battery percentage and the SHT40
// reading into the very frame it returns in that same response. That is why
// every sensor is read before this module is ever called -- there is no second
// round-trip to send them in.
//
// Nothing in here throws or reboots. Every failure path returns false and
// leaves the caller to sleep, because on a bistable panel the last good frame
// is still on the glass and doing nothing is a valid outcome (9.7).

#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace net {

//: What the board says about itself. Sentinels mean "unknown", and unknown
//: fields are omitted from the request entirely rather than sent as zero -- a
//: missing battery reading must reach the server as absent, not as a flat
//: battery (see backend models.Telemetry, every field Optional).
struct Telemetry {
  String device_id;
  uint16_t battery_mv = 0;    //: 0 = unknown
  uint8_t battery_pct = 255;  //: 255 = unknown
  bool charging = false;
  bool charging_known = false;
  float temperature_c = NAN;
  float humidity_pct = NAN;
  int32_t rssi = 0;  //: 0 = unknown (a real reading is negative)
  const char *wake_reason = "unknown";
};

//: The server's answer to GET /api/display.
struct DisplayPlan {
  int status = 0;
  String image_url;
  String filename;
  uint32_t refresh_rate = 0;  //: 0 = server said nothing; caller keeps its own
  bool update_firmware = false;
  String firmware_url;
  //: Not BYOS. The device has to know the digest and the length BEFORE it
  //: opens the stream: it checks the digest before it writes the OTA partition,
  //: it has neither Range nor resume, and Update.begin() needs the length up
  //: front. A header would arrive only once the download had already started.
  String firmware_version;
  String firmware_sha256;
  uint32_t firmware_size = 0;
  String special_function;
  //: Parsed from the HTTP Date header, 0 when absent or unparseable. This is
  //: how the RTC stays roughly right without ever paying for NTP, which costs
  //: up to 4 s of radio-on -- comparable to the whole rest of a wake (9.6).
  time_t server_epoch = 0;
};

//: One firmware image, as offered by the server and already vetted by the
//: caller. Passed to finishWake(), which runs it after the wake timer is armed.
struct OtaJob {
  String url;
  String sha256;
  String version;
  uint32_t size = 0;
};

//: Why an update run ended. The distinction between the last two is the whole
//: reason the attempt counter exists: a truncated transfer says nothing about
//: the image and deserves another try, a checksum mismatch says everything and
//: retires the digest on the spot.
enum class Ota : uint8_t {
  Installed,
  Skipped,         //: refused before anything was written
  TransferFailed,  //: the bytes did not all arrive
  BadImage,        //: they arrived and were wrong
};

enum class Fetch : uint8_t {
  Ok,           //: 200, buffer filled
  NotModified,  //: 304 -- skip the download AND the repaint (9.5)
  Failed,
};

//: MAC-derived, uppercase with colons. Stable across reflashes, which is what
//: makes it usable as the device identity.
String deviceId();

bool hasCredentials();

//: Block on the captive portal until someone configures WiFi or the timeout
//: expires. Only ever called when there are no stored credentials.
bool runSetupPortal();

//: Bounded retries, then give up. Never loops until the battery dies (9.7).
bool connect();

//: Radio off. Must happen before the panel is driven: panel drive current plus
//: TX spikes on a 1250 mAh cell is a real brownout risk (9.7).
void disconnect();

bool fetchPlan(const Telemetry &telemetry, DisplayPlan *plan);

//: Download, verify and write one firmware image. Brings the radio back up
//: itself, because it runs after the panel rail has been cut and the radio
//: switched off.
//:
//: Does NOT restart. The new image boots at the next cold wake, which is the
//: whole difference from how a mains-powered device would do this: restarting
//: here would skip finishWake() -- the only place that arms the wake timer --
//: and would also be a zero-second sleep, side-stepping the panel-care floor
//: that armWake() enforces.
Ota runOta(const OtaJob &job);

//: On Fetch::Ok, `*data` points at a PSRAM buffer the caller must hand to
//: releaseImage(). On any other result it is left untouched.
Fetch fetchImage(const String &url, const String &etag_in, uint8_t **data, size_t *length,
                 String *etag_out);
void releaseImage(uint8_t *data);

}  // namespace net
