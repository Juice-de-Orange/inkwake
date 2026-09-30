#include "net.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <Update.h>
#include <esp_task_wdt.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <mbedtls/sha256.h>
#include <string.h>

#include "config.h"
#include "store.h"

namespace net {
namespace {

WiFiClient plain_client;
WiFiClientSecure secure_client;

#ifdef EINK_TLS_CA_BUNDLE
// arduino-esp32 embeds Mozilla's root set when CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
// is on, which it is by default. Opt in with -DEINK_TLS_CA_BUNDLE.
extern "C" const uint8_t rootca_crt_bundle_start[] asm("_binary_x509_crt_bundle_start");
#endif

WiFiClient &clientFor(const String &url) {
  if (!url.startsWith("https:")) {
    return plain_client;
  }
#ifdef EINK_TLS_CA_BUNDLE
  secure_client.setCACertBundle(rootca_crt_bundle_start);
#else
  // Deliberate and loud: unverified TLS is acceptable on a LAN and is not
  // acceptable on the open internet. Anything internet-exposed must build with
  // -DEINK_TLS_CA_BUNDLE (9.7).
  log_w("https without a CA bundle -- certificate NOT verified");
  secure_client.setInsecure();
#endif
  // Outside the #if on purpose: both branches need it, and the stock default
  // is 120 s per handshake (WiFiClientSecure.cpp:40). The longest path carries
  // four handshakes -- enrolment, the display call and two image hops under
  // setRedirectLimit(1) -- so the stock default put 480 s of potential hang
  // inside a 480 s wake watchdog, on a path that feeds no watchdog of its own.
  // Argument is in seconds; the setter multiplies by 1000.
  //
  // Safe to repeat: clientFor() runs before every request, and
  // stop_ssl_socket() preserves this field across the memset it does.
  secure_client.setHandshakeTimeout(TLS_HANDSHAKE_TIMEOUT_S);
  return secure_client;
}

//: Where this board's server lives. What the setup portal stored wins over the
//: compile-time default, so moving the server does not mean taking the board
//: off the wall and finding a USB cable.
//:
//: A stored value that cannot possibly work is ignored rather than obeyed.
//: Locking the board out of every server it knows because somebody typed a
//: bare hostname is the exact failure this is supposed to prevent, and the
//: compiled-in default is at least a value somebody chose deliberately.
String serverBase() {
  String stored = store::serverUrl();
  // Normalise first, then judge. The other order accepts a bare "http://",
  // strips both slashes off it, and hands back "http:" -- which is exactly the
  // permanent lock-out this guard exists to prevent, with the compiled-in
  // default never reached.
  while (stored.endsWith("/")) {
    stored.remove(stored.length() - 1);
  }
  if (stored.startsWith("http://") || stored.startsWith("https://")) {
    return stored;
  }
  if (stored.length() > 0) {
    log_w("stored server URL %s has no scheme -- falling back to the built-in default",
          stored.c_str());
  }
  return String(EINK_SERVER_URL);
}

//: BYOS returns absolute URLs, but a server behind a reverse proxy often has
//: no idea what its own public host is and emits a path instead. Accept both
//: rather than failing on the more sensible of the two.
String absolutise(const String &url) {
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  String out(serverBase());
  if (!url.startsWith("/")) {
    out += '/';
  }
  out += url;
  return out;
}

//: Every telemetry name is HYPHENATED, and that is a deliberate change from
//: the spelling the BYOS document uses.
//:
//: Underscores in a header name are legal HTTP and are what the spec writes,
//: but they are the one shape proxies quietly drop: nginx discards them unless
//: `underscores_in_headers` is on, and Cloudflare's own documentation says it
//: may remove header names "considered invalid according to NGINX". Every
//: request from this board now travels through at least one of those.
//:
//: The failure that avoids is silent, which is why it is worth a rename rather
//: than a proxy directive. The server answers 200, the frame renders, and the
//: battery reading is simply absent -- so the deep-discharge gate never closes
//: and the panel-care temperature guard, which reads a column that is now NULL,
//: stops applying. A board that looks like it is working.
//:
//: The server has accepted both spellings from the beginning (main.py
//: _ALIASES), so this costs nothing and removes the whole class.
//: The device token, on every request that carries one. `Authorization` is a
//: name no proxy on earth mangles, which is exactly why the credential moved
//: out of ACCESS_TOKEN: a dropped telemetry header costs a reading, a dropped
//: credential costs the whole wake.
void addAuth(HTTPClient &http) {
  const String tok = store::token();
  if (tok.length() > 0) {
    http.addHeader("Authorization", "Bearer " + tok);
  }
}

void addTelemetry(HTTPClient &http, const Telemetry &t) {
  http.addHeader("ID", t.device_id);
  http.addHeader("Model", DEVICE_MODEL);
  http.addHeader("FW-Version", FW_VERSION);
  http.addHeader("Width", String(PANEL_WIDTH));
  http.addHeader("Height", String(PANEL_HEIGHT));
  http.addHeader("Wake-Reason", t.wake_reason);

  addAuth(http);

  if (t.battery_mv != 0) {
    http.addHeader("Battery-Voltage", String(t.battery_mv));
  }
  if (t.battery_pct != 255) {
    http.addHeader("Percent-Charged", String(t.battery_pct));
  }
  if (t.charging_known) {
    http.addHeader("Battery-Charging", t.charging ? "1" : "0");
  }
  if (t.rssi != 0) {
    http.addHeader("Rssi", String(t.rssi));
  }
  if (!isnan(t.temperature_c)) {
    http.addHeader("Temperature", String(t.temperature_c, 1));
  }
  if (!isnan(t.humidity_pct)) {
    http.addHeader("Humidity", String(t.humidity_pct, 1));
  }
}

//: Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
//: days_from_civil). Done by hand because timegm is not in the newlib this
//: toolchain ships, and mktime would apply whatever timezone the RTC layer
//: last set -- silently shifting the clock by an hour twice a year.
long daysFromCivil(int year, unsigned month, unsigned day) {
  year -= month <= 2;
  const int era = (year >= 0 ? year : year - 399) / 400;
  const unsigned yoe = static_cast<unsigned>(year - era * 400);
  const unsigned doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return static_cast<long>(era) * 146097 + static_cast<long>(doe) - 719468;
}

//: RFC 7231 IMF-fixdate: "Sun, 06 Nov 1994 08:49:37 GMT". Hand-parsed because
//: the format is rigidly fixed and strptime's behaviour depends on a locale an
//: embedded newlib does not really have.
time_t parseHttpDate(const String &value) {
  static const char *kMonths = "JanFebMarAprMayJunJulAugSepOctNovDec";
  if (value.length() < 29) {
    return 0;
  }
  char month[4] = {0};
  int day = 0, year = 0, hour = 0, minute = 0, second = 0;
  if (sscanf(value.c_str() + 5, "%2d %3s %4d %2d:%2d:%2d", &day, month, &year, &hour, &minute,
             &second) != 6) {
    return 0;
  }
  const char *found = strstr(kMonths, month);
  if (found == nullptr || (found - kMonths) % 3 != 0) {
    return 0;
  }
  const unsigned mon = static_cast<unsigned>((found - kMonths) / 3) + 1;

  // Computed in int64 on purpose. time_t is 32 bits in this toolchain -- newlib
  // defines _USE_LONG_TIME_T and __LONG_MAX__ is 0x7fffffff -- so the seconds
  // since the epoch stop fitting on 2038-01-19. Doing the multiply in time_t
  // would be signed overflow, which is undefined behaviour, and the compiler
  // is then entitled to delete the caller's `server_epoch > 0` check outright.
  //
  // Refusing the value is the honest outcome: the caller treats 0 as "no
  // server time", skips setting the RTC, and everything else still works.
  // Returning a negative timestamp would instead set the clock to 1901.
  constexpr int64_t kTimeTMax =
      sizeof(time_t) >= 8 ? INT64_MAX
                          : ((static_cast<int64_t>(1) << (8 * sizeof(time_t) - 1)) - 1);

  const int64_t days = daysFromCivil(year, mon, static_cast<unsigned>(day));
  const int64_t epoch =
      days * 86400 + static_cast<int64_t>(hour) * 3600 + minute * 60 + second;
  if (epoch <= 0 || epoch > kTimeTMax) {
    log_w("Date header out of range for time_t: %s", value.c_str());
    return 0;
  }
  return static_cast<time_t>(epoch);
}

//: A growable PSRAM buffer that HTTPClient can write into.
//:
//: This exists because getStreamPtr() hands back the raw socket, and a raw
//: socket on a chunked response still carries the chunk-length lines. Reading
//: it directly puts "1a3f\r\n" in the middle of the PNG. writeToStream() is
//: the only path in HTTPClient that de-chunks, and it wants a Stream.
//:
//: The read half is stubbed: nothing ever reads from a sink.
class PsramSink : public Stream {
 public:
  explicit PsramSink(size_t initial) {
    const size_t wanted = initial > 0 ? initial : 8192;
    data_ = static_cast<uint8_t *>(heap_caps_malloc(wanted, MALLOC_CAP_SPIRAM));
    // Only claim the capacity the allocation actually delivered. Setting it
    // first would leave a failed sink advertising room it does not have, and
    // reserve() would then memcpy into a null pointer. valid() catches that
    // today, but only because every caller remembers to ask.
    capacity_ = data_ != nullptr ? wanted : 0;
  }
  ~PsramSink() override { reset(); }

  bool valid() const { return data_ != nullptr; }
  size_t size() const { return used_; }

  //: Hands the buffer to the caller, who becomes responsible for freeing it.
  uint8_t *release() {
    uint8_t *out = data_;
    data_ = nullptr;
    capacity_ = 0;
    used_ = 0;
    return out;
  }

  void reset() {
    if (data_ != nullptr) {
      heap_caps_free(data_);
      data_ = nullptr;
    }
    capacity_ = 0;
    used_ = 0;
  }

  //: Wall clock for the chunked path, as an absolute millis() stamp. Zero
  //: means unbounded, which is what the Content-Length path uses because it
  //: times the raw socket itself.
  void setDeadline(uint32_t at_ms) { deadline_ms_ = (at_ms == 0) ? 1 : at_ms; }

  bool deadlineHit() const { return deadline_hit_; }

  size_t write(uint8_t byte) override { return write(&byte, 1); }

  size_t write(const uint8_t *buffer, size_t length) override {
    // Signed difference, so the millis() rollover at 49 days does not read as
    // a deadline that expired long ago.
    if (deadline_ms_ != 0 && static_cast<int32_t>(millis() - deadline_ms_) >= 0) {
      deadline_hit_ = true;
      return 0;
    }
    if (!reserve(used_ + length)) {
      // A short write is how writeToStream is told to stop, which is exactly
      // what should happen when a response outgrows the cap.
      return 0;
    }
    memcpy(data_ + used_, buffer, length);
    used_ += length;
    return length;
  }

  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}

 private:
  bool reserve(size_t wanted) {
    if (wanted <= capacity_) {
      return true;
    }
    if (wanted > MAX_IMAGE_BYTES) {
      return false;
    }
    size_t grown = capacity_ * 2;
    if (grown < wanted) grown = wanted;
    if (grown > MAX_IMAGE_BYTES) grown = MAX_IMAGE_BYTES;
    uint8_t *moved = static_cast<uint8_t *>(heap_caps_realloc(data_, grown, MALLOC_CAP_SPIRAM));
    if (moved == nullptr) {
      return false;
    }
    data_ = moved;
    capacity_ = grown;
    return true;
  }

  uint8_t *data_ = nullptr;
  size_t capacity_ = 0;
  size_t used_ = 0;
  uint32_t deadline_ms_ = 0;
  bool deadline_hit_ = false;
};

void configure(HTTPClient &http) {
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setReuse(false);
  // A redirect chain is how a captive portal or a misconfigured proxy turns a
  // 5 kB PNG into an HTML page. Follow one, then stop: each hop is a whole
  // extra request against HTTP_TIMEOUT_MS, and those seconds come out of the
  // wake budget the watchdog is measured against. One covers the legitimate
  // case -- a server that moved -- and two only ever bought a longer walk into
  // the same error page.
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.setRedirectLimit(1);
  http.setUserAgent("inkwake/" FW_VERSION);
}

}  // namespace

String deviceId() {
  uint8_t mac[6] = {0};
  // esp_read_mac reads the eFuse directly, so this works before the radio is
  // up -- WiFi.macAddress() needs an initialised driver.
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char buf[18];
  snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3],
           mac[4], mac[5]);
  return String(buf);
}

bool hasCredentials() {
  WiFi.mode(WIFI_STA);
  wifi_config_t conf = {};
  if (esp_wifi_get_config(WIFI_IF_STA, &conf) != ESP_OK) {
    return false;
  }
  return conf.sta.ssid[0] != 0;
}

bool runSetupPortal() {
  // The one place in a wake that legitimately takes minutes, because a person
  // is typing a WiFi password on a phone. It still has to be bounded -- a
  // portal left up runs the radio at ~100 mA until the cell is flat -- and
  // neither of the two obvious bounds actually worked.
  //
  // WiFiManager's own timeout is not a deadline. configPortalHasTimeout()
  // pushes _configPortalStart forward to the last web request whenever
  // _webClientCheck is set, which it is by default, and handleNotFound() feeds
  // it too -- so the connectivity probes a phone fires at a captive portal
  // restart the clock indefinitely. WIFI_PORTAL_TIMEOUT_S was decorative.
  //
  // Stretching the wake watchdog over the portal instead would be worse.
  // Nothing else in this firmware calls esp_task_wdt_reset(), so the watchdog
  // is a hard limit on elapsed time rather than a hang detector: a longer one
  // would simply reboot a perfectly healthy portal out from under somebody
  // mid-password, and the reset-reason check in setup() would then skip the
  // next slot as well.
  //
  // So the portal runs non-blocking, this loop owns the deadline, and it feeds
  // the watchdog every pass -- which is also the one place in this firmware
  // where the watchdog gets to mean what it says: it now fires only if this
  // loop itself stops turning.
  WiFiManager wm;
  wm.setDebugOutput(false);
  wm.setConfigPortalBlocking(false);
  wm.setConfigPortalTimeout(0);  // the deadline below is the only one
  wm.setBreakAfterConfig(true);

  // The server address, so a board can be pointed somewhere without a rebuild.
  // It has to ride on the WiFi page rather than the separate parameters page,
  // because setBreakAfterConfig(true) ends the portal at the WiFi save and
  // nobody would ever reach a second page. _paramsInWifi defaults to true,
  // which is exactly that, and handleWifiSave() calls doParamSave() before the
  // portal closes.
  const String stored_url = store::serverUrl();
  WiFiManagerParameter server_param("srv", "Server (http://host:port)", stored_url.c_str(), 96);
  wm.addParameter(&server_param);

  // The device token, created with `app.cli device add` and shown there exactly once.
  //
  // Pre-filled EMPTY, never with the stored value: a secret the portal page
  // serves is a secret every phone on the setup network can read, and the setup
  // network has no password.
  WiFiManagerParameter token_param("tok", "Geraete-Token (id.secret)", "", 128);
  wm.addParameter(&token_param);

  // Returns false in non-blocking mode as a matter of course -- `result` is
  // still the initial false when it skips the blocking loop -- so the portal
  // has to be asked whether it came up.
  wm.startConfigPortal(SETUP_AP_SSID);
  if (!wm.getConfigPortalActive()) {
    log_e("config portal would not start");
    esp_task_wdt_reset();
    return false;
  }

  const uint32_t started = millis();
  bool ok = false;
  while (true) {
    esp_task_wdt_reset();
    if (wm.process()) {
      ok = true;
      break;
    }
    // WiFiManager tears the portal down itself once it has a verdict:
    // processConfigPortal() ends both the success path and the
    // break-after-config failure path with `if(_disableConfigPortal)
    // shutdownConfigPortal()`, and _disableConfigPortal defaults to true.
    // After that every process() returns false at its first guard, so without
    // this the loop would spin out the whole deadline against a portal that
    // no longer exists -- fifteen minutes of a fully awake board while the
    // phone has already lost the AP.
    if (!wm.getConfigPortalActive()) {
      break;
    }
    // Unsigned subtraction, so the millis() rollover at 49 days yields a small
    // elapsed value rather than an enormous one.
    if (millis() - started > WIFI_PORTAL_TIMEOUT_S * 1000UL) {
      log_w("config portal deadline reached after %u s",
            static_cast<unsigned>(WIFI_PORTAL_TIMEOUT_S));
      break;
    }
    delay(10);
  }

  // Deliberately not inside `if (ok)`. WiFiManager writes the custom
  // parameters in handleWifiSave() before it ever tries the credentials, so a
  // typed server address survives a mistyped WiFi password -- and the next
  // wake reopens the portal with the field already filled in.
  //
  // Read before the parameter object dies with this stack frame: it owns the
  // buffer getValue() hands back.
  String typed(server_param.getValue());
  typed.trim();
  while (typed.endsWith("/")) {
    typed.remove(typed.length() - 1);
  }

  if (typed.length() == 0) {
    // Cleared on purpose: fall back to whatever was compiled in.
    store::setServerUrl("");
  } else if (typed.startsWith("http://") || typed.startsWith("https://")) {
    if (typed != store::serverUrl()) {
      store::setServerUrl(typed);
      // A different server has never heard of the token the old one issued, so
      // keeping it would mean presenting a stranger's credential for ever. The
      // ETag goes too: it names a frame on the old host.
      //
      // ORDER MATTERS: this runs BEFORE the token field is applied below, so
      // somebody who types a new server AND a new token in one sitting keeps
      // the token they just typed. Doing it the other way round would wipe it.
      store::setToken("");
      store::setEtag("");
      log_i("server set to %s (token cleared)", typed.c_str());
    }
  } else {
    // Refused rather than stored. serverBase() would reject it on every wake
    // anyway, and a value that is quietly ignored forever is worse than one
    // that was never accepted.
    log_w("ignoring server address without http:// or https://: %s", typed.c_str());
  }

  // Same reasoning as the server address: applied outside `if (ok)`, so a
  // pasted token survives a mistyped WiFi password.
  //
  // Checked rather than stored blindly. A trailing newline out of a clipboard
  // is the likeliest real mistake here, and unchecked it would be a 401 with no
  // hint anywhere -- the board would sit there reporting an identity problem
  // that is really a whitespace problem.
  String typed_token(token_param.getValue());
  typed_token.trim();
  if (typed_token.length() > 0) {
    const int dot = typed_token.indexOf('.');
    const bool shaped = dot > 0 && dot < static_cast<int>(typed_token.length()) - 1 &&
                        typed_token.length() >= 16 && typed_token.length() <= 128 &&
                        typed_token.indexOf(' ') < 0;
    if (shaped) {
      store::setToken(typed_token);
      store::setAuthFailures(0);
      log_i("device token stored (%u chars)", static_cast<unsigned>(typed_token.length()));
    } else {
      log_w("ignoring token that is not shaped like id.secret");
    }
  }

  // Only if it is still up. stopConfigPortal() forwards to
  // shutdownConfigPortal(), which does `server->handleClient()` at its top --
  // above its own `if(!configPortalActive) return false` guard -- and
  // server.reset() a few lines later. Calling it after WiFiManager has already
  // shut down dereferences a null unique_ptr and panics the board.
  if (wm.getConfigPortalActive()) {
    wm.stopConfigPortal();
  }
  esp_task_wdt_reset();
  log_i("config portal finished: %s", ok ? "configured" : "not configured");
  return ok;
}

bool connect() {
  WiFi.persistent(true);
  WiFi.mode(WIFI_STA);
  // Modem sleep saves nothing here: the radio is up for a handful of seconds
  // and then the whole board switches off.
  WiFi.setSleep(WIFI_PS_NONE);
  WiFi.setAutoReconnect(false);

  for (uint8_t attempt = 1; attempt <= WIFI_MAX_ATTEMPTS; ++attempt) {
    // No arguments: reuse the credentials esp_wifi already holds in NVS. That
    // path uses WIFI_FAST_SCAN, which stops at the first matching AP instead
    // of sweeping every channel -- 1-3 s saved on every cold-boot wake (9.8).
    WiFi.begin();

    const uint32_t deadline = millis() + WIFI_ATTEMPT_TIMEOUT_MS;
    while (millis() < deadline) {
      if (WiFi.status() == WL_CONNECTED) {
        log_i("wifi up after %u attempt(s), rssi %d", attempt, WiFi.RSSI());
        return true;
      }
      delay(50);
    }
    log_w("wifi attempt %u/%u timed out", attempt, WIFI_MAX_ATTEMPTS);
    WiFi.disconnect(false, false);
  }

  // Do not retry until the battery is flat. The wake interval is the backoff.
  return false;
}

void disconnect() {
  WiFi.disconnect(true, false);  // radio off, credentials kept
  WiFi.mode(WIFI_OFF);
  delay(20);
}

bool fetchPlan(const Telemetry &telemetry, DisplayPlan *plan) {
  if (plan == nullptr) {
    return false;
  }

  const String url = serverBase() + API_DISPLAY_PATH;
  HTTPClient http;
  if (!http.begin(clientFor(url), url)) {
    log_e("cannot open %s", url.c_str());
    return false;
  }
  configure(http);
  addTelemetry(http, telemetry);

  static const char *kCollect[] = {"Date"};
  http.collectHeaders(kCollect, 1);

  plan->status = http.GET();
  if (plan->status != HTTP_CODE_OK) {
    log_e("GET %s -> %d", API_DISPLAY_PATH, plan->status);
    http.end();
    return false;
  }

  plan->server_epoch = parseHttpDate(http.header("Date"));

  // The plan is a few hundred bytes. Anything larger is an error page or a
  // captive portal, and ArduinoJson 7 grows its document to fit whatever it is
  // handed -- which on a device with no supervisor means an out-of-memory
  // reboot instead of a skipped cycle.
  if (http.getSize() > static_cast<int>(MAX_JSON_BYTES)) {
    log_e("display json is %d bytes, cap is %u", http.getSize(),
          static_cast<unsigned>(MAX_JSON_BYTES));
    http.end();
    return false;
  }

  JsonDocument doc;
  const DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    log_e("display json: %s", err.c_str());
    return false;
  }

  plan->image_url = absolutise(String(doc["image_url"] | ""));
  // The hosted API says image_name where the BYOS spec says filename. Neither
  // is load-bearing for us, but guessing one and logging the wrong thing wastes
  // an afternoon later (9.2).
  const char *name = doc["filename"] | doc["image_name"] | "";
  plan->filename = String(name);
  plan->refresh_rate = doc["refresh_rate"] | 0U;
  plan->update_firmware = doc["update_firmware"] | false;
  // Absolute already -- unlike image_url this one is NOT run through
  // absolutise(), because the server builds it from PUBLIC_BASE_URL for exactly
  // that reason. A relative value here would resolve against nothing and the
  // download would never start.
  plan->firmware_url = String(doc["firmware_url"] | "");
  plan->firmware_version = String(doc["firmware_version"] | "");
  plan->firmware_sha256 = String(doc["firmware_sha256"] | "");
  plan->firmware_size = doc["firmware_size"] | 0U;
  plan->special_function = String(doc["special_function"] | "");

  log_i("plan: %s refresh=%u", plan->image_url.c_str(), plan->refresh_rate);
  return plan->image_url.length() > 0;
}

Fetch fetchImage(const String &url, const String &etag_in, uint8_t **data, size_t *length,
                 String *etag_out) {
  if (data == nullptr || length == nullptr || url.length() == 0) {
    return Fetch::Failed;
  }

  HTTPClient http;
  if (!http.begin(clientFor(url), url)) {
    return Fetch::Failed;
  }
  configure(http);
  http.addHeader("ID", deviceId());
  addAuth(http);
  if (etag_in.length() > 0) {
    // The single biggest battery lever there is: a 304 skips the download and
    // the 15-30 s repaint, which together are most of a wake (9.5).
    http.addHeader("If-None-Match", etag_in);
  }

  static const char *kCollect[] = {"ETag"};
  http.collectHeaders(kCollect, 1);

  const int status = http.GET();

  if (status == HTTP_CODE_NOT_MODIFIED) {
    http.end();
    log_i("304 -- panel already shows the current frame");
    return Fetch::NotModified;
  }
  if (status != HTTP_CODE_OK) {
    http.end();
    log_e("image GET -> %d", status);
    return Fetch::Failed;
  }

  const int declared = http.getSize();
  if (declared > static_cast<int>(MAX_IMAGE_BYTES)) {
    // Refuse before allocating. An oversized body means a redirect to an HTML
    // page or a broken render, and neither is worth blowing the heap over.
    http.end();
    log_e("image is %d bytes, cap is %u", declared, static_cast<unsigned>(MAX_IMAGE_BYTES));
    return Fetch::Failed;
  }

  // Read the ETag before the body, simply because the buffer below can fail
  // and return early. (An earlier comment here claimed the collected headers
  // are freed by the transfer -- they are not: HTTPClient clears
  // _currentHeaders only on the next sendRequest.)
  if (etag_out != nullptr) {
    *etag_out = http.header("ETag");
  }

  PsramSink sink(declared > 0 ? static_cast<size_t>(declared) : 32 * 1024);
  if (!sink.valid()) {
    http.end();
    log_e("no PSRAM for the image buffer");
    return Fetch::Failed;
  }

  // Two body paths, and the difference is not cosmetic.
  //
  // With a Content-Length the transfer-encoding is identity, so the socket
  // carries nothing but payload and we can read it ourselves -- which is the
  // only way to put a wall clock on it. http.writeToStream() has no bound on
  // the body phase: its loop is `while (connected() && len)` with a bare
  // delay(1), _tcpTimeout covers only the header phase, and WiFiClient reports
  // a half-open socket as connected indefinitely. An AP dropping mid-body
  // would hang here forever with the radio up and the panel rail powered,
  // which flattens the cell in hours.
  //
  // Without a Content-Length the response is chunked, and then the raw socket
  // still carries the "1a3f\r\n" length lines. Reading it by hand would splice
  // those into the middle of the PNG. writeToStream() is the only path in
  // HTTPClient that de-chunks, so that case has to use it. Its deadline comes
  // from the sink instead: PsramSink carries one and short-writes past it,
  // which is the only way to make writeToStream() stop. What that cannot cover
  // is a stall -- a sink only ever sees bytes that actually arrive -- so a peer
  // that goes silent mid-chunk is still the wake watchdog's problem.
  bool transfer_failed = false;

  if (declared > 0) {
    WiFiClient *stream = http.getStreamPtr();
    if (stream == nullptr) {
      http.end();
      log_e("no body stream for the image");
      return Fetch::Failed;
    }

    const size_t expected = static_cast<size_t>(declared);
    const uint32_t started = millis();
    uint32_t last_progress = started;
    uint8_t chunk[1024];

    // Unsigned subtraction, so the millis() rollover at 49 days yields a small
    // elapsed value rather than an enormous one.
    while (sink.size() < expected) {
      const uint32_t now = millis();
      if (now - started > IMAGE_BODY_TIMEOUT_MS) {
        log_e("image body exceeded %u ms", static_cast<unsigned>(IMAGE_BODY_TIMEOUT_MS));
        transfer_failed = true;
        break;
      }
      if (now - last_progress > IMAGE_STALL_TIMEOUT_MS) {
        log_e("image body stalled %u ms at %u of %u bytes",
              static_cast<unsigned>(IMAGE_STALL_TIMEOUT_MS),
              static_cast<unsigned>(sink.size()), static_cast<unsigned>(expected));
        transfer_failed = true;
        break;
      }

      const int available = stream->available();
      if (available <= 0) {
        if (!http.connected()) {
          // Peer closed before Content-Length was satisfied: truncated.
          break;
        }
        delay(10);
        continue;
      }

      size_t want = static_cast<size_t>(available);
      const size_t missing = expected - sink.size();
      if (want > missing) {
        want = missing;
      }
      if (want > sizeof(chunk)) {
        want = sizeof(chunk);
      }

      const int got = stream->readBytes(chunk, want);
      if (got <= 0) {
        delay(10);
        continue;
      }
      if (sink.write(chunk, static_cast<size_t>(got)) != static_cast<size_t>(got)) {
        log_e("image buffer refused %d bytes", got);
        transfer_failed = true;
        break;
      }
      last_progress = now;
    }
  } else {
    // writeToStream() has no bound of its own -- its body loop is the same
    // `while (connected() && len)` the branch above exists to avoid -- so the
    // only lever left is the sink, where a short write ends the transfer.
    // Without this a server that omits Content-Length can spend the entire
    // wake budget on its own, and the watchdog would be the first thing to
    // notice.
    sink.setDeadline(millis() + IMAGE_BODY_TIMEOUT_MS);
    const int written = http.writeToStream(&sink);
    if (sink.deadlineHit()) {
      log_e("chunked image body exceeded %u ms",
            static_cast<unsigned>(IMAGE_BODY_TIMEOUT_MS));
      transfer_failed = true;
    } else if (written <= 0) {
      log_e("chunked image transfer failed: %d", written);
      transfer_failed = true;
    }
  }

  http.end();

  if (transfer_failed) {
    return Fetch::Failed;
  }

  // A truncated PNG decodes to a half-drawn frame, and a half-drawn frame
  // would sit on the glass until the next slot. Throw it away instead.
  if (sink.size() == 0) {
    log_e("image transfer produced no bytes");
    return Fetch::Failed;
  }
  if (declared > 0 && sink.size() != static_cast<size_t>(declared)) {
    log_e("image incomplete: %u of %d bytes", static_cast<unsigned>(sink.size()), declared);
    return Fetch::Failed;
  }

  *length = sink.size();
  *data = sink.release();
  log_i("image %u bytes, etag %s", static_cast<unsigned>(*length),
        etag_out != nullptr ? etag_out->c_str() : "-");
  return Fetch::Ok;
}

void releaseImage(uint8_t *data) {
  if (data != nullptr) {
    heap_caps_free(data);
  }
}


// ---------------------------------------------------------------------------
// Firmware update
// ---------------------------------------------------------------------------
//
// Written by hand rather than with HTTPUpdate or esp_https_ota, and for one
// reason: the digest has to be computed while the bytes go past. This device
// has no room to buffer 1.35 MB and check afterwards, and it has no second
// chance either -- once Update.end(true) has moved the boot partition there is
// no rollback in an Arduino build.
//
// The header phase still belongs to HTTPClient: it already handles redirects,
// TLS and the CA bundle here, and rewriting that by hand -- as the sibling
// firmware had to, having no equivalent -- would be a second HTTP parser to get
// wrong. Only the body is read directly, exactly as fetchImage does, and for
// the same reason: it is the only way to put a wall clock on it.

namespace {

//: The first bytes of any ESP32 application image, checked before Update.begin()
//: erases a single sector.
//:
//: This is the cheapest filter that exists and it catches the expensive
//: mistakes: an HTML error page that arrived with a Content-Length, a
//: firmware.elf uploaded instead of firmware.bin, an image built for another
//: chip -- and the one that would be unrecoverable, a MERGED flash image that
//: starts at 0x0 with the bootloader. Flashing that at the OTA offset produces
//: a partition that cannot boot and a board that needs the BOOT button held
//: down at a cable.
constexpr uint8_t ESP_IMAGE_MAGIC = 0xE9;
constexpr uint32_t ESP_APP_DESC_MAGIC = 0xABCD5432;
constexpr size_t ESP_APP_DESC_OFFSET = 32;  // sizeof(esp_image_header_t) + segment header
constexpr size_t OTA_PREVIEW_BYTES = 256;

bool looksLikeAppImage(const uint8_t *head, size_t len) {
  if (len < ESP_APP_DESC_OFFSET + sizeof(uint32_t) || head[0] != ESP_IMAGE_MAGIC) {
    return false;
  }
  uint32_t magic = 0;
  memcpy(&magic, head + ESP_APP_DESC_OFFSET, sizeof(magic));
  return magic == ESP_APP_DESC_MAGIC;
}

String hexDigest(const uint8_t digest[32]) {
  static const char *kHex = "0123456789abcdef";
  String out;
  out.reserve(64);
  for (size_t i = 0; i < 32; ++i) {
    out += kHex[digest[i] >> 4];
    out += kHex[digest[i] & 0x0F];
  }
  return out;
}

}  // namespace

Ota runOta(const OtaJob &job) {
  if (job.url.length() == 0 || job.sha256.length() != 64) {
    log_w("ota: nothing usable to install");
    return Ota::Skipped;
  }

  // The radio was switched off before the panel was driven, and the panel rail
  // was cut in panel::finish(). Bring it back up here: this is the point in the
  // wake with the lowest baseline draw, which is why the update runs here at
  // all.
  if (WiFi.status() != WL_CONNECTED && !connect()) {
    log_e("ota: no network");
    return Ota::TransferFailed;
  }

  HTTPClient http;
  if (!http.begin(clientFor(job.url), job.url)) {
    log_e("ota: cannot open %s", job.url.c_str());
    return Ota::TransferFailed;
  }
  configure(http);
  http.addHeader("ID", deviceId());
  addAuth(http);

  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    log_e("ota GET -> %d", status);
    http.end();
    return Ota::TransferFailed;
  }

  const int declared = http.getSize();
  // Chunked is refused rather than handled. Update.begin() wants the length up
  // front so it can erase exactly the right number of sectors; handing it
  // UPDATE_SIZE_UNKNOWN gives that boundary up, and the whole point of this
  // path is to know what is being written before writing it.
  if (declared <= 0) {
    log_e("ota: no Content-Length, refusing a chunked image");
    http.end();
    return Ota::TransferFailed;
  }
  const uint32_t expected = static_cast<uint32_t>(declared);
  if (expected < OTA_MIN_IMAGE_BYTES || expected > OTA_MAX_IMAGE_BYTES) {
    log_e("ota: %u bytes is outside [%u, %u]", static_cast<unsigned>(expected),
          static_cast<unsigned>(OTA_MIN_IMAGE_BYTES), static_cast<unsigned>(OTA_MAX_IMAGE_BYTES));
    http.end();
    return Ota::BadImage;
  }
  if (job.size != 0 && job.size != expected) {
    // The server told us the size in the display response and the download
    // disagrees. One of the two is stale -- most likely a re-upload under the
    // same row -- and neither is worth a flash write.
    log_e("ota: announced %u bytes, got %u", static_cast<unsigned>(job.size),
          static_cast<unsigned>(expected));
    http.end();
    return Ota::BadImage;
  }

  WiFiClient *stream = http.getStreamPtr();
  if (stream == nullptr) {
    http.end();
    log_e("ota: no body stream");
    return Ota::TransferFailed;
  }

  // Read the first block into RAM and inspect it BEFORE anything is erased.
  // Once Update.begin() has run, the previous contents of the inactive slot are
  // gone -- which is harmless in itself, but it means the decision to write has
  // to be made while it is still free.
  uint8_t head[OTA_PREVIEW_BYTES];
  size_t head_len = 0;
  {
    const uint32_t started = millis();
    while (head_len < sizeof(head)) {
      if (millis() - started > OTA_STALL_TIMEOUT_MS) {
        break;
      }
      const int got = stream->readBytes(head + head_len, sizeof(head) - head_len);
      if (got > 0) {
        head_len += static_cast<size_t>(got);
        continue;
      }
      if (!http.connected()) {
        break;
      }
      delay(10);
    }
  }
  if (!looksLikeAppImage(head, head_len)) {
    log_e("ota: not an ESP32 application image (first byte 0x%02X, %u bytes read)",
          head_len > 0 ? head[0] : 0, static_cast<unsigned>(head_len));
    http.end();
    return Ota::BadImage;
  }

  if (!Update.begin(expected)) {
    log_e("ota: Update.begin(%u) refused: %s", static_cast<unsigned>(expected),
          Update.errorString());
    http.end();
    return Ota::TransferFailed;
  }

  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts(&sha, 0);  // 0 = SHA-256, not SHA-224

  bool failed = false;
  uint32_t written = 0;

  auto consume = [&](const uint8_t *buf, size_t len) -> bool {
    mbedtls_sha256_update(&sha, buf, len);
    if (Update.write(const_cast<uint8_t *>(buf), len) != len) {
      log_e("ota: flash write refused at %u bytes: %s", static_cast<unsigned>(written),
            Update.errorString());
      return false;
    }
    written += static_cast<uint32_t>(len);
    return true;
  };

  if (!consume(head, head_len)) {
    failed = true;
  }

  const uint32_t started = millis();
  uint32_t last_progress = started;
  uint8_t chunk[1024];

  // Unsigned subtraction, so the millis() rollover at 49 days yields a small
  // elapsed value rather than an enormous one.
  //
  // Deliberately NO esp_task_wdt_reset() in this loop. The sibling firmware
  // feeds the watchdog here because it runs continuously; this one would be
  // creating a second exemption beside the setup portal, and the invariant that
  // makes WAKE_WATCHDOG_S checkable at all -- nothing outside the portal feeds
  // it, so the number is a deadline on elapsed time -- is worth more than the
  // ten milliamp-hours it would save on a wake that has already failed.
  while (!failed && written < expected) {
    const uint32_t now = millis();
    if (now - started > OTA_TRANSFER_TIMEOUT_S * 1000UL) {
      log_e("ota: body exceeded %u s at %u of %u bytes",
            static_cast<unsigned>(OTA_TRANSFER_TIMEOUT_S), static_cast<unsigned>(written),
            static_cast<unsigned>(expected));
      failed = true;
      break;
    }
    if (now - last_progress > OTA_STALL_TIMEOUT_MS) {
      log_e("ota: stalled %u ms at %u of %u bytes",
            static_cast<unsigned>(OTA_STALL_TIMEOUT_MS), static_cast<unsigned>(written),
            static_cast<unsigned>(expected));
      failed = true;
      break;
    }

    const int available = stream->available();
    if (available <= 0) {
      if (!http.connected()) {
        log_e("ota: peer closed at %u of %u bytes", static_cast<unsigned>(written),
              static_cast<unsigned>(expected));
        failed = true;
        break;
      }
      delay(10);
      continue;
    }

    size_t want = static_cast<size_t>(available);
    const uint32_t missing = expected - written;
    if (want > missing) {
      want = missing;
    }
    if (want > sizeof(chunk)) {
      want = sizeof(chunk);
    }

    const int got = stream->readBytes(chunk, want);
    if (got <= 0) {
      delay(10);
      continue;
    }
    if (!consume(chunk, static_cast<size_t>(got))) {
      failed = true;
      break;
    }
    last_progress = now;
  }

  http.end();

  uint8_t digest[32];
  mbedtls_sha256_finish(&sha, digest);
  mbedtls_sha256_free(&sha);
  const String got_sha = hexDigest(digest);

  if (failed || written != expected) {
    Update.abort();
    // Remembered, but only as one strike. A transfer that broke says nothing
    // about the image -- a weak signal at 1.35 MB is an ordinary event -- so
    // burning the whole budget here would permanently lock out a perfectly good
    // release over one bad afternoon.
    store::setOtaSha(job.sha256);
    store::setOtaAttempts(store::otaAttempts() + 1);
    log_e("ota: transfer failed, attempt %u of %u", static_cast<unsigned>(store::otaAttempts()),
          static_cast<unsigned>(OTA_ATTEMPT_MAX));
    return Ota::TransferFailed;
  }

  if (!got_sha.equalsIgnoreCase(job.sha256)) {
    Update.abort();
    // The other kind of failure, and it retires the digest at once. The bytes
    // all arrived and they are the wrong bytes; trying again would fetch the
    // same wrong bytes.
    store::setOtaSha(job.sha256);
    store::setOtaAttempts(OTA_ATTEMPT_MAX);
    log_e("ota: digest mismatch, wanted %s got %s", job.sha256.c_str(), got_sha.c_str());
    return Ota::BadImage;
  }

  if (!Update.end(true)) {
    Update.abort();
    store::setOtaSha(job.sha256);
    store::setOtaAttempts(store::otaAttempts() + 1);
    log_e("ota: Update.end refused: %s", Update.errorString());
    return Ota::TransferFailed;
  }

  // Installed. Recorded so the next wake -- which still runs the OLD image,
  // because this function deliberately does not restart -- refuses the same
  // offer instead of downloading it all over again.
  store::setOtaSha(job.sha256);
  store::setOtaAttempts(0);
  log_i("ota: installed %s (%u bytes), boots on the next cold wake",
        job.version.c_str(), static_cast<unsigned>(written));
  return Ota::Installed;
}

}  // namespace net
