// Everything that reaches the glass.
//
// Two rules shape this module, both from HARDWARE.md.
//
// The auto-display trap (10.4): on an S3 with PSRAM, M5GFX sets
// _auto_display = true and every un-bracketed draw call can kick off a 15-30 s
// refresh. So nothing here draws to M5.Display directly. Everything goes into
// one PSRAM canvas, gets pushed once, and is followed by exactly one explicit
// display().
//
// Panel care (4.4 rule 3): after a refresh the panel must be put to sleep
// (0x07 0xA5) and its rail cut, or it sits in a high-voltage state and takes
// permanent damage. finish() is not optional cleanup -- it is the second half
// of a refresh.

#pragma once

#include <stddef.h>
#include <stdint.h>

#include "config.h"

namespace panel {

//: Power the rail, pick the no-dither mode, allocate the canvas. Returns false
//: if the canvas would not fit, in which case nothing may be drawn.
bool begin();

//: Decode a PNG from a memory buffer straight into the canvas and refresh.
//: Blocks for the full 15-30 s. False means the PNG was rejected -- the panel
//: keeps whatever it was already showing, which on bistable glass is a
//: perfectly good failure mode (9.1).
bool showImage(const uint8_t *png, size_t length);

//: A text-only screen for the cases where there is no frame to show: flat
//: battery, WiFi setup, nothing reachable for days. Drawn in exact palette
//: colours so epd_fastest maps every pixel with no rounding.
void showNotice(const char *title, const char *const *lines, size_t line_count, Ink accent);

//: Wait out the refresh, put the panel to sleep, cut its rail. Safe to call
//: even if nothing was drawn.
void finish();

}  // namespace panel
