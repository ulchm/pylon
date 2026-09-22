# Circuit outlines

**Copied, not drawn here. Do not edit these files.**

    uv run tools/sync_league_art.py

They come from the Global Open Wheel repository's `assets/tracks/*-outline.svg`,
which builds them from OpenStreetMap geometry and checks each one against the
circuit's published length. The tool strips the opaque background rectangle so a
circuit composites onto the holding card's own backdrop, and changes nothing
else: the stroke stays the league's bone `#F3F4F6`, which is `--ink` in this
package too.

The filename stem is the league's `art` slug. It is not derived here and must not
be: the broadcaster holds iRacing's `TrackDisplayShortName`, which is a different
string from the league calendar's track name, so the league resolves the stem and
ships it in the broadcast feed (`round.art`). Nothing in this repository should
ever try to work out which picture a track is.

## Attribution, which is not optional

The geometry is OpenStreetMap data, used under the Open Database Licence. **Any
surface that shows one of these owes the credit:**

    © OpenStreetMap contributors

`cards.html` prints it whenever it draws a circuit. If you put one of these
anywhere else, print it there too.
