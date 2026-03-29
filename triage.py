#!/usr/bin/env python3
"""RAW+JPEG Photo Triage Tool — quickly sort through paired RAW/JPEG files."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from flask import Flask, render_template, jsonify, send_file, request, abort
from PIL import Image

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TRASH_DIR_NAME = "_triage_trash"
RAW_EXTS = {".raf"}
JPEG_EXTS = {".jpg", ".jpeg", ".hif", ".heif", ".heic"}
AUDIO_EXTS = {".wav"}

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
source_dir: Path = Path()
archive_dir: Path | None = None
photos: list = []          # ordered list of PhotoGroup
photo_map: dict = {}       # id -> PhotoGroup
decisions: dict = {}       # id -> "delete" | "keep_jpeg" | "keep_both"
cache: dict = {}           # arbitrary cache keyed by string

HAS_HEIF = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except ImportError:
    pass

HAS_RAWPY = False
try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    pass

HAS_EXIFTOOL = False
EXIFTOOL_CMD = "exiftool"
for _candidate in ["exiftool", "/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"]:
    try:
        subprocess.run([_candidate, "-ver"], capture_output=True, timeout=3)
        EXIFTOOL_CMD = _candidate
        HAS_EXIFTOOL = True
        break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        continue


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class PhotoGroup:
    def __init__(self, stem: str, raw_path: Path | None, jpeg_path: Path | None,
                 audio_path: Path | None = None):
        self.id = stem
        self.stem = stem
        self.raw_path = raw_path
        self.jpeg_path = jpeg_path
        self.audio_path = audio_path

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stem": self.stem,
            "hasRaw": self.raw_path is not None,
            "hasJpeg": self.jpeg_path is not None,
            "hasAudio": self.audio_path is not None,
            "rawExt": self.raw_path.suffix.upper() if self.raw_path else None,
            "jpegExt": self.jpeg_path.suffix.upper() if self.jpeg_path else None,
            "decision": decisions.get(self.id),
        }


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def scan(directory: Path) -> None:
    global photos, photo_map, source_dir
    source_dir = directory

    stems: dict[str, dict] = {}
    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        stem = f.stem
        if stem not in stems:
            stems[stem] = {"raw": None, "jpeg": None, "audio": None}
        if ext in RAW_EXTS:
            stems[stem]["raw"] = f
        elif ext in JPEG_EXTS:
            stems[stem]["jpeg"] = f
        elif ext in AUDIO_EXTS:
            stems[stem]["audio"] = f

    photos = []
    photo_map = {}
    for stem in sorted(stems):
        r, j, a = stems[stem]["raw"], stems[stem]["jpeg"], stems[stem]["audio"]
        if r is None and j is None:
            continue
        g = PhotoGroup(stem, r, j, a)
        photos.append(g)
        photo_map[stem] = g

    print(f"Found {len(photos)} photo(s) in {directory}")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def _jpeg_bytes_for(path: Path) -> bytes | None:
    """Return JPEG bytes for a JPEG/HEIF file, converting if needed."""
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return path.read_bytes()
    # HEIF → JPEG conversion
    ck = f"heif:{path}"
    if ck in cache:
        return cache[ck]
    data = _convert_heif(path)
    if data:
        cache[ck] = data
    return data


def _convert_heif(path: Path) -> bytes | None:
    if HAS_HEIF:
        try:
            img = Image.open(path)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue()
        except Exception:
            pass
    # Fallback: macOS sips
    try:
        import tempfile
        tmp = tempfile.mktemp(suffix=".jpg")
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(path), "--out", tmp],
            capture_output=True, check=True, timeout=15,
        )
        data = Path(tmp).read_bytes()
        os.unlink(tmp)
        return data
    except Exception:
        return None


def _extract_raf_thumb(path: Path) -> bytes | None:
    """Extract the embedded JPEG preview (has film simulation applied)."""
    ck = f"raf:{path}"
    if ck in cache:
        return cache[ck]
    if not HAS_RAWPY:
        return None
    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format is rawpy.ThumbFormat.JPEG:
                cache[ck] = thumb.data
                return thumb.data
            img = Image.fromarray(thumb.data)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            data = buf.getvalue()
            cache[ck] = data
            return data
    except Exception as e:
        print(f"Warning: RAF thumb extraction failed for {path}: {e}")
        return None


def _develop_raw(path: Path) -> bytes | None:
    """Develop RAW with neutral/default settings (no film simulation).

    Uses half_size=True for speed (~6 MP on a 26 MP Fuji sensor).
    """
    ck = f"dev:{path}"
    if ck in cache:
        return cache[ck]
    if not HAS_RAWPY:
        return None
    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=True,
                no_auto_bright=False,
                output_bps=8,
            )
        img = Image.fromarray(rgb)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        cache[ck] = data
        return data
    except Exception as e:
        print(f"Warning: RAW development failed for {path}: {e}")
        return None


def _make_thumbnail(data: bytes, size: int = 200) -> bytes:
    ck = f"thumb:{hash(data)}:{size}"
    if ck in cache:
        return cache[ck]
    img = Image.open(io.BytesIO(data))
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    out = buf.getvalue()
    cache[ck] = out
    return out


def _best_image(group: PhotoGroup) -> bytes | None:
    """Return best available image bytes (JPEG preferred)."""
    if group.jpeg_path:
        return _jpeg_bytes_for(group.jpeg_path)
    if group.raw_path:
        return _extract_raf_thumb(group.raw_path)
    return None


# ---------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------
def _get_exif(photo_id: str) -> dict:
    ck = f"exif:{photo_id}"
    if ck in cache:
        return cache[ck]
    group = photo_map.get(photo_id)
    if not group:
        return {}
    path = group.jpeg_path or group.raw_path
    exif = _exif_exiftool(path) if HAS_EXIFTOOL else {}
    if not exif:
        exif = _exif_pillow(path)
    cache[ck] = exif
    return exif


def _parse_exiftool_gps(val, ref: str = "") -> str:
    """Parse GPS value from exiftool output (e.g. '52.517389 N' or numeric)."""
    if not val:
        return ""
    s = str(val).strip()
    try:
        # Handle "52.517389 N" format from -c flag
        parts = s.split()
        num = float(parts[0])
        direction = parts[1] if len(parts) > 1 else ref
        if direction in ("S", "W"):
            num = -abs(num)
        return str(round(num, 6))
    except (ValueError, IndexError):
        return ""


def _exif_exiftool(path: Path) -> dict:
    try:
        r = subprocess.run(
            [EXIFTOOL_CMD, "-json", "-c", "%.6f", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        d = json.loads(r.stdout)[0]
        return {
            "camera": str(d.get("Model", "")),
            "lens": str(d.get("LensModel", "")),
            "focalLength": str(d.get("FocalLength", "")),
            "aperture": str(d.get("FNumber", "")),
            "shutterSpeed": str(
                d.get("ShutterSpeed", d.get("ExposureTime", ""))
            ),
            "iso": str(d.get("ISO", "")),
            "filmSimulation": str(d.get("FilmMode", "")),
            "whiteBalance": str(d.get("WhiteBalance", "")),
            "exposureComp": str(d.get("ExposureCompensation", "")),
            "date": str(d.get("DateTimeOriginal", "")),
            "resolution": str(d.get("ImageSize", "")),
            "meteringMode": str(d.get("MeteringMode", "")),
            "focusMode": str(d.get("FocusMode", "")),
            "dynamicRange": str(d.get("DynamicRange", "")),
            "gpsLatitude": _parse_exiftool_gps(d.get("GPSLatitude", ""), d.get("GPSLatitudeRef", "")),
            "gpsLongitude": _parse_exiftool_gps(d.get("GPSLongitude", ""), d.get("GPSLongitudeRef", "")),
        }
    except Exception:
        return {}


def _gps_dms_to_decimal(dms, ref: str) -> float | None:
    """Convert GPS DMS (degrees/minutes/seconds) to signed decimal degrees."""
    try:
        if isinstance(dms, (tuple, list)) and len(dms) == 3:
            d = float(dms[0])
            m = float(dms[1])
            s = float(dms[2])
        else:
            return None
        dec = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            dec = -dec
        return round(dec, 6)
    except Exception:
        return None


def _exif_pillow(path: Path) -> dict:
    from PIL.ExifTags import TAGS

    try:
        ext = path.suffix.lower()
        if ext in RAW_EXTS:
            # Pillow can't open RAF — read EXIF from the embedded thumbnail
            thumb_data = _extract_raf_thumb(path)
            if not thumb_data:
                return {}
            img = Image.open(io.BytesIO(thumb_data))
        else:
            img = Image.open(path)

        # Use getexif() (modern API, works with pillow-heif)
        exif_data = img.getexif()
        if not exif_data:
            return {}

        # Also grab IFD EXIF sub-tags (shutter, ISO, etc.)
        from PIL.ExifTags import IFD
        ifd_exif = {}
        try:
            ifd_exif = exif_data.get_ifd(IFD.Exif)
        except Exception:
            pass

        # Parse GPS IFD
        gps_lat, gps_lon = "", ""
        try:
            gps_ifd = exif_data.get_ifd(IFD.GPSInfo)
            if gps_ifd:
                lat = _gps_dms_to_decimal(gps_ifd.get(2), gps_ifd.get(1, "N"))
                lon = _gps_dms_to_decimal(gps_ifd.get(4), gps_ifd.get(3, "E"))
                if lat is not None:
                    gps_lat = str(lat)
                if lon is not None:
                    gps_lon = str(lon)
        except Exception:
            pass

        # Merge: main EXIF + IFD sub-tags, resolving tag names
        tags = {}
        for k, v in exif_data.items():
            tags[TAGS.get(k, k)] = v
        for k, v in ifd_exif.items():
            tags[TAGS.get(k, k)] = v

        focal = tags.get("FocalLength", "")
        if hasattr(focal, "numerator"):
            focal = f"{float(focal):.0f} mm"
        elif isinstance(focal, (int, float)):
            focal = f"{focal:.0f} mm"
        aperture = tags.get("FNumber", "")
        if hasattr(aperture, "numerator"):
            aperture = f"{float(aperture):.1f}"
        elif isinstance(aperture, (int, float)):
            aperture = f"{aperture:.1f}"
        exposure = tags.get("ExposureTime", "")
        if hasattr(exposure, "numerator") and exposure.numerator:
            val = float(exposure)
            exposure = f"1/{int(1/val)}s" if val < 1 else f"{val}s"
        elif isinstance(exposure, (int, float)) and exposure > 0:
            exposure = f"1/{int(1/exposure)}s" if exposure < 1 else f"{exposure}s"
        return {
            "camera": str(tags.get("Model", "")),
            "lens": str(tags.get("LensModel", "")),
            "focalLength": str(focal),
            "aperture": str(aperture),
            "shutterSpeed": str(exposure),
            "iso": str(tags.get("ISOSpeedRatings", tags.get("PhotographicSensitivity", ""))),
            "filmSimulation": "",
            "whiteBalance": str(tags.get("WhiteBalance", "")),
            "exposureComp": str(tags.get("ExposureCompensation", "")),
            "date": str(tags.get("DateTimeOriginal", "")),
            "resolution": f'{tags.get("ImageWidth", tags.get("PixelXDimension", ""))}x{tags.get("ImageLength", tags.get("PixelYDimension", ""))}',
            "meteringMode": str(tags.get("MeteringMode", "")),
            "focusMode": "",
            "dynamicRange": "",
            "gpsLatitude": gps_lat,
            "gpsLongitude": gps_lon,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/capabilities")
def api_capabilities():
    return jsonify({"exiftool": HAS_EXIFTOOL, "rawpy": HAS_RAWPY, "heif": HAS_HEIF})


@app.route("/api/photos")
def api_photos():
    return jsonify([p.to_dict() for p in photos])


@app.route("/api/image/<photo_id>/<source>")
def api_image(photo_id, source):
    group = photo_map.get(photo_id)
    if not group:
        abort(404)

    data = None
    if source == "jpeg" and group.jpeg_path:
        data = _jpeg_bytes_for(group.jpeg_path)
    elif source == "raw" and group.raw_path:
        # Develop RAW with neutral settings (no film sim)
        data = _develop_raw(group.raw_path)
    elif source == "jpeg" and group.raw_path:
        # No JPEG file — fall back to embedded RAF thumbnail
        data = _extract_raf_thumb(group.raw_path)
    elif source == "raw" and group.jpeg_path:
        # No RAW file — fall back to JPEG
        data = _jpeg_bytes_for(group.jpeg_path)

    if not data:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/thumb/<photo_id>")
def api_thumb(photo_id):
    group = photo_map.get(photo_id)
    if not group:
        abort(404)
    data = _best_image(group)
    if not data:
        abort(404)
    thumb = _make_thumbnail(data)
    return send_file(io.BytesIO(thumb), mimetype="image/jpeg")


@app.route("/api/audio/<photo_id>")
def api_audio(photo_id):
    group = photo_map.get(photo_id)
    if not group or not group.audio_path or not group.audio_path.exists():
        abort(404)
    return send_file(group.audio_path, mimetype="audio/wav")


@app.route("/api/exif/<photo_id>")
def api_exif(photo_id):
    return jsonify(_get_exif(photo_id))


@app.route("/api/decide", methods=["POST"])
def api_decide():
    body = request.json
    photo_id = body.get("id")
    decision = body.get("decision")  # "delete" | "keep_jpeg" | "keep_both" | None
    if photo_id not in photo_map:
        abort(404)
    if decision is None:
        decisions.pop(photo_id, None)
    else:
        decisions[photo_id] = decision
    return jsonify({"ok": True})


@app.route("/api/summary")
def api_summary():
    delete = keep_jpeg = keep_both = undecided = 0
    for p in photos:
        d = decisions.get(p.id)
        if d == "delete":
            delete += 1
        elif d == "keep_jpeg":
            keep_jpeg += 1
        elif d == "keep_both":
            keep_both += 1
        else:
            undecided += 1
    return jsonify({
        "delete": delete,
        "keepJpeg": keep_jpeg,
        "keepBoth": keep_both,
        "undecided": undecided,
        "total": len(photos),
    })


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "Empty query"}), 400
    try:
        from geopy.geocoders import Nominatim
        geo = Nominatim(user_agent="raw-triage/1.0")
        loc = geo.geocode(query, timeout=10)
        if not loc:
            return jsonify({"error": "No results found"}), 404
        return jsonify({
            "lat": round(loc.latitude, 6),
            "lon": round(loc.longitude, 6),
            "displayName": loc.address,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/geotag", methods=["POST"])
def api_geotag():
    if not HAS_EXIFTOOL:
        return jsonify({"error": "exiftool is required for writing GPS data"}), 500
    body = request.json or {}
    photo_id = body.get("id")
    lat = body.get("lat")
    lon = body.get("lon")
    if not photo_id or lat is None or lon is None:
        return jsonify({"error": "Missing id, lat, or lon"}), 400
    group = photo_map.get(photo_id)
    if not group:
        abort(404)

    lat_ref = "N" if lat >= 0 else "S"
    lon_ref = "E" if lon >= 0 else "W"
    tagged = 0
    for path in [group.raw_path, group.jpeg_path]:
        if not path or not path.exists():
            continue
        try:
            subprocess.run(
                [
                    EXIFTOOL_CMD, "-overwrite_original", "-n",
                    f"-GPSLatitude={abs(lat):.6f}",
                    f"-GPSLatitudeRef={lat_ref}",
                    f"-GPSLongitude={abs(lon):.6f}",
                    f"-GPSLongitudeRef={lon_ref}",
                    str(path),
                ],
                capture_output=True, check=True, timeout=15,
            )
            tagged += 1
        except Exception as e:
            print(f"Warning: failed to geotag {path}: {e}")

    # Invalidate EXIF cache
    cache.pop(f"exif:{photo_id}", None)

    return jsonify({"ok": True, "tagged": tagged})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    trash = source_dir / TRASH_DIR_NAME
    trash.mkdir(exist_ok=True)
    trashed = archived = 0

    for p in photos:
        d = decisions.get(p.id)
        if d == "delete":
            # Move all files (raw, jpeg, audio) to trash
            for path in [p.raw_path, p.jpeg_path, p.audio_path]:
                if path and path.exists():
                    shutil.move(str(path), str(trash / path.name))
                    trashed += 1
        elif d == "keep_jpeg":
            # Discard RAW, keep JPEG + WAV in import folder
            if p.raw_path and p.raw_path.exists():
                shutil.move(str(p.raw_path), str(trash / p.raw_path.name))
                trashed += 1
        elif d == "keep_both":
            # Move RAW to archive, keep JPEG + WAV in import folder
            if archive_dir and p.raw_path and p.raw_path.exists():
                shutil.move(str(p.raw_path), str(archive_dir / p.raw_path.name))
                archived += 1

    result = {
        "ok": True,
        "trashed": trashed,
        "archived": archived,
        "trashDir": str(trash),
    }
    if archive_dir:
        result["archiveDir"] = str(archive_dir)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="RAW+JPEG Photo Triage Tool")
    parser.add_argument("directory", type=Path, help="Folder containing RAW+JPEG pairs (import folder)")
    parser.add_argument("--archive", type=Path, default=None,
                        help="Archive folder for kept RAWs (default: sibling 'archive' folder)")
    parser.add_argument("--port", type=int, default=8080, help="Server port (default 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"Error: {args.directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    global archive_dir
    if args.archive:
        archive_dir = args.archive.resolve()
    else:
        # Default: sibling "archive" folder next to import folder
        archive_dir = args.directory.resolve().parent / "archive"

    archive_dir.mkdir(parents=True, exist_ok=True)
    print(f"Archive folder: {archive_dir}")

    scan(args.directory.resolve())

    if not photos:
        print("No supported photos found. Exiting.")
        sys.exit(0)

    caps = []
    if HAS_RAWPY:
        caps.append("rawpy")
    if HAS_HEIF:
        caps.append("pillow-heif")
    if HAS_EXIFTOOL:
        caps.append("exiftool")
    print(f"Capabilities: {', '.join(caps) or 'basic only'}")

    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}")

    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
