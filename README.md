# RAW Triage

A local web-based tool for quickly triaging standalone or paired RAW & JPEG/HEIF photo files. Built (by and) for Fujifilm shooters who shoot RAW+Lossy and want a fast way to decide what to keep.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

Point it at a folder of photos and it opens a browser-based image viewer. For each photo you can mark it one of:

- **Delete** — all associated files (RAW + JPEG/HEIF + WAV memo) go to a `_triage_trash/` subfolder
- **Keep JPEG/HEIF** — trash the RAW, keep the JPEG/HEIF and any WAV memo
- **Keep Both** — move the RAW to an archive folder, keep the JPEG/HEIF in place

You can also give photos a **star rating**, or just mark favourites, to find them again after importing into Apple Photos (see [below](#getting-favourites-into-apple-photos)).

Once you've triaged everything, hit **Apply** to execute all decisions at once. Decisions and ratings are saved as you go (in a hidden `.raw-triage.json` in the photo folder), so you can quit and pick up where you left off.

![Example Screenshot](screenshot.jpeg)

## Features

- Side-by-side RAW vs JPEG comparison (toggle with `T`)
- Click-to-zoom at 100% for checking focus, with drag-to-pan
- **Focus peaking** overlay (`P`) with three sensitivity levels (`Shift+P`)
- **Camera AF point** marker for Fujifilm files (`A`); `Z` zooms straight into it. Fuji's own in-camera "Focus Check" verdict is shown in the EXIF panel (both need `exiftool`)
- Star ratings / favourites, written to the kept JPEG/HEIFs (and an XMP sidecar for archived RAWs) on Apply
- EXIF display (camera, lens, aperture, shutter speed, ISO, film simulation, etc.)
- GPS coordinates with Apple Maps links
- Address-based geotagging (requires `exiftool`)
- Audio memo playback for voice memos (`.wav`)
- Filmstrip thumbnail strip with lazy loading
- Full keyboard-driven workflow
- Undo support
- Image and voice memo files are grouped by filename

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `1` | Delete |
| `2` | Keep JPEG/HEIF |
| `3` | Keep Both |
| `4` | Undo |
| `F` | Toggle favourite (5 stars) |
| `Shift` + `1`–`5` | Set star rating |
| `0` | Clear rating |
| `P` | Toggle focus peaking |
| `Shift` + `P` | Cycle peaking sensitivity (low / med / high) |
| `A` | Show/hide the camera's AF point |
| `T` | Toggle RAW / JPEG view |
| `Z` | Toggle 100% zoom (centred on the AF point if known) |
| `I` | Toggle EXIF panel |
| `M` | Play/stop audio memo |
| `←` `→` `Space` | Navigate |
| `Enter` | Apply decisions |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional: install [exiftool](https://exiftool.org/) for richer EXIF data and geotagging support.

## Usage

```bash
python triage.py /path/to/photos/Import
```

Options:

```
--archive FOLDER   Archive folder for kept RAWs (default: sibling 'archive' folder)
--port PORT        Server port (default: 8080)
--cache-mb MB      Memory budget for converted previews (default: 1024)
--no-browser       Don't auto-open the browser
```

Example with explicit archive:

```bash
python triage.py ~/Photos/RAWs/Import --archive ~/Photos/RAWs/Archive
```

## Getting favourites into Apple Photos

Apple Photos does not read star ratings from files, and it has no way to set the Favourite flag from file metadata. So on **Apply**, for every rated photo that isn't deleted, RAW Triage uses `exiftool` to write:

- `XMP:Rating` = 1–5, the standard rating field (Lightroom, Capture One, darktable, digiKam and `osxphotos` read it)
- a keyword made of stars, e.g. `★★★★★` (IPTC + XMP), which Photos **does** import as a keyword

Archived RAWs get the same data in an XMP sidecar (`DSCF1234.xmp`) next to them.

Then choose one of these routes:

1. **Plain Photos import:** after importing, create a Smart Album (*File → New Smart Album*) with *Keyword is ★★★★★*. To turn those photos into real Favourites (which sync to your iPhone), select everything in that album and press `.` (*Image → Favourite*).
2. **Import with [osxphotos](https://github.com/RhetTbull/osxphotos)**, which can set the Favourite flag directly from the rating:

   ```bash
   osxphotos import ~/Photos/RAWs/Import --favorite-rating 5
   ```

   (`--favorite-rating N` marks everything rated N stars or more as a Favourite; see `osxphotos help import` for albums, keywords, etc.)

Try this with a couple of photos first, especially if you shoot HEIF: the keyword route depends on Photos reading the embedded keywords, which hasn't been verified for every format.

## Focus peaking notes

Peaking highlights fine, high-contrast detail (a difference-of-Gaussians band-pass filter on the full-resolution image), drawn in red over a dimmed greyscale copy. Like in-camera peaking it depends on contrast: a sharp but low-contrast subject may barely light up, so use the sensitivity levels. A soft, defocused frame stays mostly dark. The developed RAW (half size, no in-camera sharpening) lights up less than the JPEG does. The thresholds (`PEAKING_THRESHOLDS` in `triage.py`) were calibrated on synthetic images, so adjust them if they don't suit your files.

The AF point comes from Fujifilm's `FocusPixel` maker-note tag. Manual-focus shots don't have one.

## Supported formats

- **RAW:** `.raf` (Fujifilm)
- **Lossy:** `.jpg`, `.jpeg`, `.hif`, `.heif`, `.heic`
- **Audio:** `.wav` (voice memos)

## Dependencies

- [Flask](https://flask.palletsprojects.com/) — web server
- [rawpy](https://github.com/letmaik/rawpy) — RAW file processing
- [Pillow](https://python-pillow.org/) — image manipulation
- [pillow-heif](https://github.com/bigcat88/pillow_heif) — HEIC/HEIF support
- [geopy](https://github.com/geopy/geopy) — address geocoding
- [NumPy](https://numpy.org/) — focus peaking

## License

MIT
