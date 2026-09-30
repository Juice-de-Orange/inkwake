#include "panel.h"

#include <M5Unified.h>

#include <Arduino.h>

#include "power.h"

namespace panel {
namespace {

//: Heap-allocated rather than a file-scope object: an M5Canvas built before
//: M5.begin() would capture a display that has not been configured yet, and
//: static init order across translation units is not something to bet a
//: 400 km-away device on.
M5Canvas *canvas = nullptr;
bool refreshed = false;
bool parked = false;

uint32_t ink(const Ink &c) {
  return canvas->color888(c.r, c.g, c.b);
}

//: One refresh, start to finish. Called exactly once per drawing operation,
//: which is the whole point of routing everything through the canvas.
void present() {
  canvas->pushSprite(0, 0);
  const uint32_t started = millis();
  M5.Display.display();
  M5.Display.waitDisplay();  // M5GFX polls BUSY every 10 ms with a 20 s cap
  const uint32_t took = millis() - started;

  // Stays true even when the wait timed out, and that is on purpose: this
  // flag gates the sleep command in finish(), and a panel left *mid-refresh*
  // needs that sequence more than a finished one does, not less (4.4 rule 3).
  refreshed = true;

  // waitDisplay() returns void and M5GFX's _wait_busy() swallows its own
  // timeout (Panel_ED2208.cpp:275-289 just returns false), so a panel that
  // never releases BUSY is indistinguishable from a completed refresh. Ask
  // the panel itself. This is the one failure a first flash is actually
  // likely to produce -- rail not up, RST or BUSY miswired, panel still
  // asleep -- and it would otherwise look like success in the log.
  if (M5.Display.displayBusy()) {
    log_e("panel still BUSY after %u ms -- refresh did NOT complete",
          static_cast<unsigned>(took));
  } else {
    log_i("panel refresh took %u ms", static_cast<unsigned>(took));
  }
}

}  // namespace

bool begin() {
  if (parked) {
    // The panel has been slept and its rail cut. Waking it for a second
    // refresh inside one cycle would need a full re-init and would break the
    // 180 s minimum gap anyway, whose failure mode is permanent (4.4 rule 1).
    log_e("refusing a second refresh in one wake");
    return false;
  }
  if (canvas != nullptr) {
    return true;
  }

  // M5Unified already brought this rail up during M5.begin(), and power::begin()
  // re-asserted it. Doing it a third time here costs one I2C write and means
  // this module is correct even if the call order above ever changes.
  power::setEpdRail(true);

  // epd_fastest means *no* dithering. The names mislead -- epd_text dithers
  // harder than epd_fastest (4.7 / 10.7). The server already quantised against
  // the measured palette, so any further dithering here would re-mangle
  // exactly the error diffusion it computed, and shred text edges doing it.
  M5.Display.setEpdMode(m5gfx::epd_mode_t::epd_fastest);

  // Native portrait. The server renders 400x600 and the packer refuses
  // anything else, so there is nothing to rotate.
  M5.Display.setRotation(0);

  // Belt and braces against the auto-display trap: even a stray draw outside
  // this module now cannot trigger a refresh on its own.
  M5.Display.setAutoDisplay(false);

  canvas = new M5Canvas(&M5.Display);
  if (canvas == nullptr) {
    return false;
  }
  canvas->setColorDepth(24);  // forced to rgb888 on this panel anyway (8)
  canvas->setPsram(true);     // 400*600*3 = 720 kB; internal SRAM has no chance
  if (!canvas->createSprite(PANEL_WIDTH, PANEL_HEIGHT)) {
    delete canvas;
    canvas = nullptr;
    log_e("canvas allocation failed -- is OPI PSRAM enabled?");
    return false;
  }
  return true;
}

bool showImage(const uint8_t *png, size_t length) {
  if (!begin() || png == nullptr || length == 0) {
    return false;
  }

  canvas->fillSprite(ink(INK_WHITE));
  if (!canvas->drawPng(png, length, 0, 0)) {
    log_e("PNG decode failed (%u bytes)", static_cast<unsigned>(length));
    return false;
  }

  present();
  return true;
}

void showNotice(const char *title, const char *const *lines, size_t line_count, Ink accent) {
  if (!begin()) {
    return;
  }

  const uint32_t fg = ink(INK_BLACK);
  const uint32_t bg = ink(INK_WHITE);

  canvas->fillSprite(bg);

  // A band in the accent colour, so the screen is identifiable from across the
  // room without reading it.
  canvas->fillRect(0, 0, PANEL_WIDTH, 8, ink(accent));

  canvas->setTextDatum(textdatum_t::top_center);
  canvas->setTextColor(fg, bg);
  // 18 pt, not 24: at 24 pt "WLAN einrichten" is about 360 px wide on a 400 px
  // panel, and a title that clips is a title nobody can read.
  canvas->setFont(&fonts::FreeSansBold18pt7b);
  canvas->drawString(title, PANEL_WIDTH / 2, 90);

  canvas->setFont(&fonts::FreeSans12pt7b);
  int32_t y = 200;
  for (size_t i = 0; i < line_count; ++i) {
    if (lines[i] == nullptr) {
      continue;
    }
    canvas->drawString(lines[i], PANEL_WIDTH / 2, y);
    y += 34;
  }

  canvas->setFont(&fonts::FreeSans9pt7b);
  canvas->setTextDatum(textdatum_t::bottom_center);
  canvas->drawString("inkwake " FW_VERSION, PANEL_WIDTH / 2, PANEL_HEIGHT - 16);

  present();
}

void finish() {
  // Idempotent, because the caller parks the panel as soon as it is done with
  // it -- which for the setup screen is fifteen minutes before the wake ends --
  // and then again on the way out.
  if (parked) {
    return;
  }
  parked = true;

  if (refreshed) {
    M5.Display.waitDisplay();
    M5.Display.sleep();  // 0x07 0xA5

    // The vendor requires >= 2 s in deep sleep before the rail may be cut, or
    // the panel is left in a high-voltage state (4.3 / 4.4 rule 3).
    delay(PANEL_SLEEP_SETTLE_MS);
  }
  // If nothing was drawn the panel was never driven into that state, so a
  // plain power cut is enough -- and rule 3 names it as the alternative to
  // the sleep command anyway.
  power::setEpdRail(false);

  if (canvas != nullptr) {
    canvas->deleteSprite();
    delete canvas;
    canvas = nullptr;
  }
}

}  // namespace panel
