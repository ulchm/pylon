"""Offline: turn one lap of driving telemetry into a track map asset (#25).

A live broadcast knows WHERE ROUND THE LAP every car is (`CarIdxLapDistPct`, a clean
0..1 every frame) but not what that means in 2D: iRacing publishes no circuit geometry
over the live SDK. Verified against the real capture, whose 324 channels include
`LapDist`, `LapDistPct`, `Yaw` and `YawNorth` but no `Lat`, `Lon` or `Alt`. Orientation
without position does not draw a map.

Driving telemetry does have the coordinates. So the geometry is derived ONCE, offline,
per track, and shipped as an asset; at broadcast time each car's live `LapDistPct` is
looked up in it to place a dot.

## On "ibt: NOT used"

DESIGN.md section 13 rules .ibt files out, and is right: as a LIVE source. They are
driving-only, finalised on exit, and carry no `CarIdx` arrays, so they cannot drive a
spectating broadcast. This is the opposite case: offline, one-time, and a single car's
path is exactly what is wanted. Section 12 already established the reader is pure
mmap/struct with no Win32, so this runs natively on Linux.

## Why measure the line rather than trace an outline

Sampling `(Lat, Lon)` against `LapDistPct` makes the asset exact BY CONSTRUCTION, and
in the one unit that matters: the map is indexed by the same 0..1 the live feed reports,
so a dot at 0.5 is where a car at 0.5 actually is. An SVG traced by hand has to be
parameterised by geometric path length instead, which is not distance along the racing
surface, so dot spacing distorts wherever the trace is imprecise.

Validated on samples/ai_race.ibt: the derived polyline measures 4284 m against a stated
track length of 4.28 km, and the shape is unmistakably the Red Bull Ring.

Usage:
    uv run tools/track_map.py samples/ai_race.ibt
    uv run tools/track_map.py samples/spa.ibt --out overlays/maps --svg /tmp/spa.svg
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import irsdk
import yaml

# Points in the emitted polyline. 512 puts a vertex every ~8 m at Spa, which is finer
# than the map is ever drawn (a 300 px map is ~4 m per pixel), so the curve is smooth at
# any sane display size and the asset is still only ~10 KB.
POINTS = 512

# A lap must cover essentially all of 0..1 to be usable. Slack for the sample landing
# just inside either end, which it always does at 60 Hz.
MIN_SPAN = 0.98

# Below this the car is stopped or crawling (m/s), which means a spin, a tow or the
# grid. Such a lap is not a clean loop of the circuit.
MIN_SPEED = 5.0


def _parse_length_m(text: str | None) -> float | None:
    """iRacing states track length as e.g. "6.93 km" (same parse as extract_roster)."""
    if not text:
        return None
    m = re.match(r"\s*([\d.]+)\s*(km|m)\b", text)
    if not m:
        return None
    return float(m.group(1)) * (1000.0 if m.group(2) == "km" else 1.0)


def _session_info(ibt) -> dict:
    h = ibt._header
    raw = ibt._shared_mem[h.session_info_offset: h.session_info_offset + h.session_info_len]
    return yaml.safe_load(raw.split(b"\x00", 1)[0].decode("latin-1")) or {}


def _slug(name: str, config: str | None) -> str:
    parts = [p for p in (name, config) if p and p.lower() != "n/a"]
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower()).strip("-")


def _pick_lap(pct, lap, pit, speed) -> int | None:
    """The cleanest complete lap in the file.

    Wants a full 0..1 sweep, never on pit road (pit lane is a DIFFERENT path, and a lap
    containing it would bend the racing line into the pits), and never stopped. Prefers
    the one with the most samples, which is the slowest clean lap and so the best
    resolved: an out lap is fine here, because this measures the road, not a time.
    """
    laps: dict[int, list[int]] = {}
    for k, p in enumerate(pct):
        if p is not None and p >= 0.0:
            laps.setdefault(lap[k], []).append(k)

    best, best_n = None, 0
    for n, idxs in sorted(laps.items()):
        if len(idxs) < 100:
            continue
        p = [pct[k] for k in idxs]
        if max(p) - min(p) < MIN_SPAN:
            continue
        if any(pit[k] for k in idxs):
            continue
        if min(speed[k] for k in idxs) < MIN_SPEED:
            continue
        if len(idxs) > best_n:
            best, best_n = n, len(idxs)
    return best


def _to_metres(lat, lon):
    """Equirectangular projection about the centroid.

    A circuit spans a couple of km, over which the error against a proper geodesic is
    centimetres: far below the width of the road, let alone of a drawn line."""
    lat0 = sum(lat) / len(lat)
    lon0 = sum(lon) / len(lon)
    m_lat = 111_132.92 - 559.82 * math.cos(2 * math.radians(lat0))
    m_lon = 111_412.84 * math.cos(math.radians(lat0))
    return ([(lo - lon0) * m_lon for lo in lon],
            [(la - lat0) * m_lat for la in lat])


def _resample(pct, xs, ys, n):
    """Uniform in LAP DISTANCE PCT, which is the whole point: the asset is indexed by
    the same 0..1 the live feed reports, so a lookup is a straight array index."""
    order = sorted(range(len(pct)), key=lambda k: pct[k])
    p = [pct[k] for k in order]
    x = [xs[k] for k in order]
    y = [ys[k] for k in order]
    out = []
    j = 0
    for i in range(n):
        t = i / n
        while j < len(p) - 2 and p[j + 1] < t:
            j += 1
        span = p[j + 1] - p[j]
        f = 0.0 if span <= 0 else max(0.0, min(1.0, (t - p[j]) / span))
        out.append((x[j] + f * (x[j + 1] - x[j]), y[j] + f * (y[j + 1] - y[j])))
    return out


def build(ibt_path: str, points: int = POINTS) -> dict:
    ibt = irsdk.IBT()
    ibt.open(ibt_path)
    try:
        info = _session_info(ibt)
        need = ("Lat", "Lon", "LapDistPct", "Lap", "OnPitRoad", "Speed")
        missing = [c for c in need if c not in ibt.var_headers_names]
        if missing:
            raise SystemExit(f"{ibt_path}: missing channels {missing}, not a driving .ibt")
        ch = {c: ibt.get_all(c) for c in need}
    finally:
        ibt.close()

    weekend = info.get("WeekendInfo", {}) or {}
    name = weekend.get("TrackDisplayName") or "unknown"
    config = weekend.get("TrackConfigName")
    stated = _parse_length_m(weekend.get("TrackLength"))

    lap = _pick_lap(ch["LapDistPct"], ch["Lap"], ch["OnPitRoad"], ch["Speed"])
    if lap is None:
        raise SystemExit(f"{ibt_path}: no clean complete lap (needs a full 0..1 sweep, "
                         "off pit road, never stopped)")

    idxs = [k for k in range(len(ch["Lap"]))
            if ch["Lap"][k] == lap and ch["LapDistPct"][k] >= 0.0]
    pct = [ch["LapDistPct"][k] for k in idxs]
    xs, ys = _to_metres([ch["Lat"][k] for k in idxs], [ch["Lon"][k] for k in idxs])
    pts = _resample(pct, xs, ys, points)

    # Measured length of what we just built, as a check on the whole chain) projection,
    # lap choice and resampling all land in this one number.
    measured = sum(math.dist(pts[k - 1], pts[k]) for k in range(1, len(pts)))
    measured += math.dist(pts[-1], pts[0])

    # Normalise into a 0..1 box, y already flipped for screen coordinates, aspect kept.
    # The overlay then scales to whatever space it is given and never sees a projection.
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    scale = max(maxx - minx, maxy - miny)
    norm = [[round((x - minx) / scale, 5), round((maxy - y) / scale, 5)] for x, y in pts]

    return {
        "track": name,
        "config": config if config and config != "n/a" else None,
        "slug": _slug(name, config),
        "source": Path(ibt_path).name,
        "lap": lap,
        "statedLengthM": stated,
        "measuredLengthM": round(measured, 1),
        "width": round((maxx - minx) / scale, 5),
        "height": round((maxy - miny) / scale, 5),
        # points[i] is the position at lap_dist_pct == i/len(points). Closed loop: the
        # last point joins the first, so a renderer draws it with Z and never repeats one.
        "points": norm,
    }


def to_svg(asset: dict, size: int = 900) -> str:
    """A standalone look at the asset, for the one check that matters: does it look like
    the circuit? Length agreeing to a metre still would not catch a mirrored projection."""
    w, h = asset["width"], asset["height"]
    pts = [(x * size, y * size) for x, y in asset["points"]]
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"
    pad = 20
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w*size+2*pad:.0f}" '
        f'height="{h*size+2*pad:.0f}" viewBox="{-pad} {-pad} {w*size+2*pad:.0f} '
        f'{h*size+2*pad:.0f}">'
        f'<rect x="{-pad}" y="{-pad}" width="{w*size+2*pad:.0f}" '
        f'height="{h*size+2*pad:.0f}" fill="#111"/>'
        f'<path d="{d}" fill="none" stroke="#eee" stroke-width="9" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{pts[0][0]:.1f}" cy="{pts[0][1]:.1f}" r="14" fill="#e10600"/>'
        f"</svg>"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ibt", help="a driving .ibt containing at least one clean lap")
    ap.add_argument("--out", default="overlays/maps",
                    help="directory for <slug>.json (default: overlays/maps)")
    ap.add_argument("--svg", help="also write an SVG here, to eyeball the shape")
    ap.add_argument("--points", type=int, default=POINTS)
    args = ap.parse_args(argv)

    asset = build(args.ibt, args.points)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{asset['slug']}.json"
    path.write_text(json.dumps(asset, separators=(",", ":")))

    stated, measured = asset["statedLengthM"], asset["measuredLengthM"]
    print(f"{asset['track']}"
          + (f" ({asset['config']})" if asset["config"] else "")
          + f"  lap {asset['lap']}  {len(asset['points'])} points")
    if stated:
        err = abs(measured - stated) / stated * 100.0
        print(f"  length: {measured:.0f} m measured vs {stated:.0f} m stated  ({err:.2f}% off)")
        if err > 2.0:
            print("  WARNING: over 2% out. Check the lap chosen and the projection.")
    print(f"  wrote {path}")

    if args.svg:
        Path(args.svg).write_text(to_svg(asset))
        print(f"  wrote {args.svg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
