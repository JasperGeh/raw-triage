#!/usr/bin/env python3
"""RAW+JPEG Photo Triage Tool — quickly sort through paired RAW/JPEG files."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import OrderedDict
from pathlib import Path

import numpy as np
from flask import Flask, render_template, jsonify, send_file, request, abort
from PIL import Image, ImageChops, ImageFilter, ImageOps

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TRASH_DIR_NAME = "_triage_trash"
STATE_FILE_NAME = ".raw-triage.json"   # dotfile, so scan() ignores it
RAW_EXTS = {".raf"}
JPEG_EXTS = {".jpg", ".jpeg", ".hif", ".heif", ".heic"}
AUDIO_EXTS = {".wav"}
DECISIONS = {"delete", "keep_jpeg", "keep_both"}

# Focus peaking: band-pass threshold (0–255) per sensitivity level (1 = low, 3 = high).
# Calibrated on synthetic images; tune against your own files if needed.
PEAKING_THRESHOLDS = {1: 30, 2: 20, 3: 12}

# exiftool's printed Orientation values -> EXIF orientation numbers
ORIENTATIONS = {"Horizontal (normal)": 1, "Rotate 180": 3, "Rotate 90 CW": 6, "Rotate 270 CW": 8}


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
class ByteCache:
    """Thread-safe LRU cache for encoded images, bounded by total size."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = value
            self._size += len(value)
            while self._size > self.max_bytes and len(self._items) > 1:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
source_dir: Path = Path()
archive_dir: Path | None = None
photos: list = []          # ordered list of PhotoGroup
photo_map: dict = {}       # id -> PhotoGroup
decisions: dict = {}       # id -> "delete" | "keep_jpeg" | "keep_both"
ratings: dict = {}         # id -> 1..5 stars
image_cache = ByteCache(1024 * 1024 * 1024)  # large previews (HEIF, RAW, peaking)
thumb_cache: dict = {}     # id -> thumbnail bytes
exif_cache: dict = {}      # id -> parsed EXIF dict
state_lock = threading.Lock()

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
            "rating": ratings.get(self.id, 0),
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
            if stems[stem]["jpeg"] is not None:
                print(f"Warning: several JPEG/HEIF files for {stem}; only {f.name} will be triaged")
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
# Persistent state (decisions + ratings survive restarts)
# ---------------------------------------------------------------------------
def load_state() -> None:
    path = source_dir / STATE_FILE_NAME
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"Warning: could not read {path}: {e}")
        return
    for pid, d in data.get("decisions", {}).items():
        if pid in photo_map and d in DECISIONS:
            decisions[pid] = d
    for pid, r in data.get("ratings", {}).items():
        if pid in photo_map and isinstance(r, int) and 1 <= r <= 5:
            ratings[pid] = r
    if decisions or ratings:
        print(f"Resumed {len(decisions)} decision(s) and {len(ratings)} rating(s) from {path.name}")


def save_state() -> None:
    path = source_dir / STATE_FILE_NAME
    with state_lock:
        data = {
            "version": 1,
            "decisions": {k: v for k, v in decisions.items() if k in photo_map},
            "ratings": {k: v for k, v in ratings.items() if k in photo_map},
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError as e:
            print(f"Warning: could not save triage state to {path}: {e}")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def _encode_jpeg(img: Image.Image, quality: int, icc: bytes | None = None) -> bytes:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, icc_profile=icc)
    return buf.getvalue()


def _convert_heif(path: Path) -> bytes | None:
    ck = f"heif:{path}"
    data = image_cache.get(ck)
    if data:
        return data
    data = None
    if HAS_HEIF:
        try:
            img = Image.open(path)
            data = _encode_jpeg(img, 95, img.info.get("icc_profile"))
        except Exception:
            data = None
    if not data:
        # Fallback: macOS sips
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp_path)],
                    capture_output=True, check=True, timeout=15,
                )
                data = tmp_path.read_bytes()
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            return None
    image_cache.put(ck, data)
    return data


def _extract_raf_thumb(path: Path) -> bytes | None:
    """Extract the embedded JPEG preview (has film simulation applied)."""
    ck = f"raf:{path}"
    data = image_cache.get(ck)
    if data:
        return data
    if not HAS_RAWPY:
        return None
    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format is rawpy.ThumbFormat.JPEG:
                data = thumb.data
            else:
                data = _encode_jpeg(Image.fromarray(thumb.data), 90)
        image_cache.put(ck, data)
        return data
    except Exception as e:
        print(f"Warning: RAF thumb extraction failed for {path}: {e}")
        return None


def _develop_raw(path: Path) -> bytes | None:
    """Develop RAW with neutral/default settings (no film simulation).

    Uses half_size=True for speed (~6 MP on a 26 MP Fuji sensor).
    """
    ck = f"dev:{path}"
    data = image_cache.get(ck)
    if data:
        return data
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
        data = _encode_jpeg(Image.fromarray(rgb), 90)
        image_cache.put(ck, data)
        return data
    except Exception as e:
        print(f"Warning: RAW development failed for {path}: {e}")
        return None


def _source_image(group: PhotoGroup, source: str) -> Path | bytes | None:
    """What the viewer shows for `source` ("jpeg" | "raw"): a JPEG file path or JPEG bytes."""
    if source == "raw" and group.raw_path:
        # Develop RAW with neutral settings (no film sim)
        return _develop_raw(group.raw_path)
    if group.jpeg_path:
        if group.jpeg_path.suffix.lower() in {".jpg", ".jpeg"}:
            return group.jpeg_path if group.jpeg_path.exists() else None
        return _convert_heif(group.jpeg_path)
    if group.raw_path:
        # No JPEG file — fall back to embedded RAF thumbnail
        return _extract_raf_thumb(group.raw_path)
    return None


def _open_image(src: Path | bytes) -> Image.Image:
    return Image.open(src if isinstance(src, Path) else io.BytesIO(src))


def _make_thumbnail(group: PhotoGroup, size: int = 200) -> bytes | None:
    if group.id in thumb_cache:
        return thumb_cache[group.id]
    # Open JPEG/HEIF straight from disk rather than via a full-size conversion
    src = group.jpeg_path if group.jpeg_path and group.jpeg_path.exists() else _source_image(group, "jpeg")
    if src is None:
        return None
    img = _open_image(src)
    img.draft("RGB", (size, size))   # JPEG only: decode at 1/2–1/8 scale, much faster
    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img)  # otherwise portrait shots appear sideways
    img.thumbnail((size, size))
    out = _encode_jpeg(img, 70, icc)
    thumb_cache[group.id] = out
    return out


def _peaking_image(group: PhotoGroup, source: str, level: int) -> bytes | None:
    """Render the image as dimmed greyscale with in-focus detail highlighted in red."""
    ck = f"peak:{group.id}:{source}:{level}"
    data = image_cache.get(ck)
    if data:
        return data
    src = _source_image(group, source)
    if src is None:
        return None
    img = ImageOps.exif_transpose(_open_image(src))
    lum = img.convert("L")

    # Band-pass (difference of Gaussians): the small blur suppresses pixel noise, the large one
    # removes everything but fine detail, so soft (defocused) edges respond only weakly.
    band = ImageChops.difference(lum.filter(ImageFilter.GaussianBlur(0.7)),
                                 lum.filter(ImageFilter.GaussianBlur(1.8)))
    thr = PEAKING_THRESHOLDS[level]
    mask = np.array(band.point([0] * (thr + 1) + [255] * (255 - thr)))

    # 3x3 dilation so highlights stay visible when the browser scales the image down to fit
    np.maximum(mask[1:], mask[:-1], out=mask[1:])
    np.maximum(mask[:-1], mask[1:], out=mask[:-1])
    np.maximum(mask[:, 1:], mask[:, :-1], out=mask[:, 1:])
    np.maximum(mask[:, :-1], mask[:, 1:], out=mask[:, :-1])

    grey = np.asarray(lum.point([v * 45 // 100 for v in range(256)]))
    red = Image.fromarray(np.maximum(grey, mask))
    green_blue = Image.fromarray(np.minimum(grey, ~mask))
    data = _encode_jpeg(Image.merge("RGB", (red, green_blue, green_blue)), 85)
    image_cache.put(ck, data)
    return data


# ---------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------
def _get_exif(photo_id: str) -> dict:
    if photo_id in exif_cache:
        return exif_cache[photo_id]
    group = photo_map.get(photo_id)
    if not group:
        return {}
    path = group.jpeg_path or group.raw_path
    exif = _exif_exiftool(path) if HAS_EXIFTOOL else {}
    if not exif:
        exif = _exif_pillow(path)
    exif_cache[photo_id] = exif
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


def _af_point(d: dict) -> list | None:
    """Fujifilm AF point as fractions [x, y] of the displayed (orientation-corrected) image.

    FocusPixel is stored in unrotated pixel coordinates of the image described by
    ExifImageWidth/Height (the full-size JPEG, or the RAF's embedded preview).
    """
    try:
        x, y = (float(v) for v in str(d["FocusPixel"]).split())
        u, v = x / float(d["ExifImageWidth"]), y / float(d["ExifImageHeight"])
    except (KeyError, ValueError, ZeroDivisionError):
        return None
    if not (0 < u <= 1 and 0 < v <= 1):
        return None
    orientation = ORIENTATIONS.get(d.get("Orientation"), 1)
    if orientation == 3:
        u, v = 1 - u, 1 - v
    elif orientation == 6:
        u, v = 1 - v, u
    elif orientation == 8:
        u, v = v, 1 - u
    return [round(u, 4), round(v, 4)]


def _parse_exiftool(d: dict) -> dict:
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
        "focusWarning": str(d.get("FocusWarning", "")),
        "afPoint": _af_point(d),
        "dynamicRange": str(d.get("DynamicRange", "")),
        "gpsLatitude": _parse_exiftool_gps(d.get("GPSLatitude", ""), d.get("GPSLatitudeRef", "")),
        "gpsLongitude": _parse_exiftool_gps(d.get("GPSLongitude", ""), d.get("GPSLongitudeRef", "")),
    }


def _exif_exiftool(path: Path) -> dict:
    try:
        r = subprocess.run(
            [EXIFTOOL_CMD, "-json", "-c", "%.6f", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        return _parse_exiftool(json.loads(r.stdout)[0])
    except Exception:
        return {}


def _prefetch_exif(batch_size: int = 50) -> None:
    """Read EXIF for all photos in a few batched exiftool calls (one process per call is slow)."""
    items = [(g.id, g.jpeg_path or g.raw_path) for g in photos]
    for i in range(0, len(items), batch_size):
        chunk = [(pid, path) for pid, path in items[i:i + batch_size] if pid not in exif_cache]
        if not chunk:
            continue
        try:
            r = subprocess.run(
                [EXIFTOOL_CMD, "-json", "-c", "%.6f", *(str(p) for _, p in chunk)],
                capture_output=True, text=True, timeout=120,
            )
            records = {d.get("SourceFile"): d for d in json.loads(r.stdout or "[]")}
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        for pid, path in chunk:
            if str(path) in records:
                exif_cache.setdefault(pid, _parse_exiftool(records[str(path)]))


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
            "focusWarning": "",
            "afPoint": None,
            "dynamicRange": "",
            "gpsLatitude": gps_lat,
            "gpsLongitude": gps_lon,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Ratings → metadata
# ---------------------------------------------------------------------------
def _rating_keyword(stars: int) -> str:
    return "★" * stars


def _write_rating(path: Path, stars: int, sidecar: bool = False) -> bool:
    """Write XMP:Rating plus a ★-keyword (which Apple Photos imports) to a file.

    With sidecar=True, `path` is a RAW and the metadata goes to an XMP sidecar next to it
    (the convention Lightroom, Capture One and darktable read).
    """
    target = path.with_suffix(".xmp") if sidecar else path
    args = [EXIFTOOL_CMD, "-q", "-P", f"-XMP-xmp:Rating={stars}"]
    # Drop ★-keywords from an earlier rating, then add the current one (no duplicates)
    for n in range(1, 6):
        args.append(f"-XMP-dc:Subject-={_rating_keyword(n)}")
    args.append(f"-XMP-dc:Subject+={_rating_keyword(stars)}")
    if target.suffix.lower() in {".jpg", ".jpeg"}:
        args.append("-IPTC:CodedCharacterSet=UTF8")
        for n in range(1, 6):
            args.append(f"-IPTC:Keywords-={_rating_keyword(n)}")
        args.append(f"-IPTC:Keywords+={_rating_keyword(stars)}")
    if sidecar and not target.exists():
        args += ["-o", str(target), str(path)]   # create the sidecar from the RAW's metadata
    else:
        args += ["-overwrite_original", str(target)]
    try:
        subprocess.run(args, capture_output=True, check=True, timeout=30)
        return True
    except Exception as e:
        print(f"Warning: failed to write rating to {target}: {e}")
        return False


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
    if not group or source not in ("jpeg", "raw"):
        abort(404)
    data = _source_image(group, source)
    if not data:
        abort(404)
    if isinstance(data, Path):
        return send_file(data, mimetype="image/jpeg")
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/peaking/<photo_id>/<source>")
def api_peaking(photo_id, source):
    group = photo_map.get(photo_id)
    level = request.args.get("level", 2, type=int)
    if not group or source not in ("jpeg", "raw") or level not in PEAKING_THRESHOLDS:
        abort(404)
    data = _peaking_image(group, source, level)
    if not data:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/thumb/<photo_id>")
def api_thumb(photo_id):
    group = photo_map.get(photo_id)
    if not group:
        abort(404)
    thumb = _make_thumbnail(group)
    if not thumb:
        abort(404)
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
    body = request.get_json(silent=True) or {}
    photo_id = body.get("id")
    decision = body.get("decision")  # "delete" | "keep_jpeg" | "keep_both" | None
    if photo_id not in photo_map:
        abort(404)
    if decision is None:
        decisions.pop(photo_id, None)
    elif decision in DECISIONS:
        decisions[photo_id] = decision
    else:
        return jsonify({"error": f"Unknown decision {decision!r}"}), 400
    save_state()
    return jsonify({"ok": True})


@app.route("/api/rate", methods=["POST"])
def api_rate():
    body = request.get_json(silent=True) or {}
    photo_id = body.get("id")
    rating = body.get("rating")
    if photo_id not in photo_map:
        abort(404)
    if not isinstance(rating, int) or not 0 <= rating <= 5:
        return jsonify({"error": "Rating must be 0–5"}), 400
    if rating:
        ratings[photo_id] = rating
    else:
        ratings.pop(photo_id, None)
    save_state()
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
    rated = sum(1 for p in photos if ratings.get(p.id) and decisions.get(p.id) != "delete")
    return jsonify({
        "delete": delete,
        "keepJpeg": keep_jpeg,
        "keepBoth": keep_both,
        "undecided": undecided,
        "rated": rated,
        "total": len(photos),
        "exiftool": HAS_EXIFTOOL,
    })


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    query = (request.get_json(silent=True) or {}).get("query", "").strip()
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
    body = request.get_json(silent=True) or {}
    photo_id = body.get("id")
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid id, lat, or lon"}), 400
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
    exif_cache.pop(photo_id, None)

    return jsonify({"ok": True, "tagged": tagged})


def _move(src: Path, dest_dir: Path, rename_on_conflict: bool) -> Path | None:
    """Move src into dest_dir without ever overwriting an existing file.

    On a name clash, either pick a free name (stem_1.ext, …) or return None and leave src alone.
    """
    dest = dest_dir / src.name
    if dest.exists():
        if not rename_on_conflict:
            return None
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
    shutil.move(str(src), str(dest))
    return dest


@app.route("/api/apply", methods=["POST"])
def api_apply():
    trash = source_dir / TRASH_DIR_NAME
    trash.mkdir(exist_ok=True)
    trashed = archived = rated = 0
    conflicts = []
    rating_errors = []

    for p in photos:
        d = decisions.get(p.id)
        stars = ratings.get(p.id, 0)
        if d == "delete":
            # Move all files (raw, jpeg, audio) to trash
            for path in [p.raw_path, p.jpeg_path, p.audio_path]:
                if path and path.exists():
                    _move(path, trash, rename_on_conflict=True)
                    trashed += 1
            continue

        if stars and HAS_EXIFTOOL and p.jpeg_path and p.jpeg_path.exists():
            if _write_rating(p.jpeg_path, stars):
                rated += 1
            else:
                rating_errors.append(p.jpeg_path.name)

        if d == "keep_jpeg":
            # Discard RAW, keep JPEG + WAV in import folder
            if p.raw_path and p.raw_path.exists():
                _move(p.raw_path, trash, rename_on_conflict=True)
                trashed += 1
        elif d == "keep_both":
            # Move RAW to archive, keep JPEG + WAV in import folder.
            # Never overwrite: Fuji file numbers repeat after DSCF9999 or across bodies.
            if archive_dir and p.raw_path and p.raw_path.exists():
                dest = _move(p.raw_path, archive_dir, rename_on_conflict=False)
                if dest is None:
                    conflicts.append(p.raw_path.name)
                    continue
                archived += 1
                if stars and HAS_EXIFTOOL and not _write_rating(dest, stars, sidecar=True):
                    rating_errors.append(dest.with_suffix(".xmp").name)

    # Pick up the new state of the folder; thumbnails/EXIF of remaining files stay valid
    scan(source_dir)
    save_state()

    result = {
        "ok": True,
        "trashed": trashed,
        "archived": archived,
        "rated": rated,
        "conflicts": conflicts,
        "ratingErrors": rating_errors,
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
    parser.add_argument("--cache-mb", type=int, default=1024,
                        help="Memory budget for converted previews in MB (default 1024)")
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
    image_cache.max_bytes = args.cache_mb * 1024 * 1024

    scan(args.directory.resolve())

    if not photos:
        print("No supported photos found. Exiting.")
        sys.exit(0)

    load_state()

    caps = []
    if HAS_RAWPY:
        caps.append("rawpy")
    if HAS_HEIF:
        caps.append("pillow-heif")
    if HAS_EXIFTOOL:
        caps.append("exiftool")
        threading.Thread(target=_prefetch_exif, daemon=True).start()
    print(f"Capabilities: {', '.join(caps) or 'basic only'}")

    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}")

    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
