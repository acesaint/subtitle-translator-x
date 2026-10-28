ObjC.import('Cocoa');

// Draws the Subtitle Translator app icon and writes it as a PNG.
//
//   osascript -l JavaScript tools/make_icon.js <out.png>
//
// Uses the Cocoa bridge that ships with macOS, so it needs no ffmpeg, no
// Pillow and no fonts beyond the system ones. make_icon.sh then downsamples
// this PNG into an .iconset and packs it with iconutil.

function run(argv) {
  const out = argv[0];
  if (!out) throw new Error('usage: make_icon.js <out.png>');

  // Render at 1024pt; on a Retina screen the backing store doubles it, which
  // is harmless — make_icon.sh rescales to exactly 1024 before slicing.
  const S = 1024;
  const INSET = 88;                  // macOS icons leave a margin
  const TILE = S - INSET * 2;
  const RADIUS = TILE * 0.225;

  const img = $.NSImage.alloc.initWithSize($.NSMakeSize(S, S));
  img.lockFocus;

  // ---- soft drop shadow, then the tile -------------------------------- //
  const shadow = $.NSShadow.alloc.init;
  shadow.shadowColor = $.NSColor.colorWithSRGBRedGreenBlueAlpha(0, 0, 0, 0.30);
  shadow.shadowBlurRadius = 26;
  shadow.shadowOffset = $.NSMakeSize(0, -12);
  shadow.set;

  const tile = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(
    $.NSMakeRect(INSET, INSET, TILE, TILE), RADIUS, RADIUS);
  // Teal, slightly darker at the bottom for a bit of depth.
  const grad = $.NSGradient.alloc.initWithStartingColorEndingColor(
    $.NSColor.colorWithSRGBRedGreenBlueAlpha(0.227, 0.702, 0.639, 1.0),
    $.NSColor.colorWithSRGBRedGreenBlueAlpha(0.106, 0.545, 0.494, 1.0));
  grad.drawInBezierPathAngle(tile, -90);

  // Shadow must not apply to the glyphs.
  $.NSShadow.alloc.init.set;

  // ---- glyph ----------------------------------------------------------- //
  function draw(str, size, dx, dy, alpha) {
    const font = $.NSFont.fontWithNameSize('PingFang SC', size)
              || $.NSFont.boldSystemFontOfSize(size);
    const attrs = $.NSMutableDictionary.alloc.init;
    attrs.setObjectForKey(font, $.NSFontAttributeName);
    attrs.setObjectForKey(
      $.NSColor.colorWithSRGBRedGreenBlueAlpha(1, 1, 1, alpha),
      $.NSForegroundColorAttributeName);
    const s = $.NSString.alloc.initWithUTF8String(str);
    const sz = s.sizeWithAttributes(attrs);
    s.drawAtPointWithAttributes(
      $.NSMakePoint((S - sz.width) / 2 + dx, (S - sz.height) / 2 + dy), attrs);
  }

  draw('字', 430, 0, 62, 1.0);

  // Subtitle bar underneath: reads as "captions" even at 16x16.
  const bar = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(
    $.NSMakeRect((S - 330) / 2, INSET + TILE * 0.175, 330, 40), 20, 20);
  $.NSColor.colorWithSRGBRedGreenBlueAlpha(1, 1, 1, 0.92).setFill;
  bar.fill;

  img.unlockFocus;

  // ---- PNG ------------------------------------------------------------- //
  const rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
  if (!png.writeToFileAtomically($(out), true)) {
    throw new Error('could not write ' + out);
  }
  return 'ok';
}
