# RAW Triage

A local web-based tool for quickly triaging standalone or paired RAW & JPEG/HEIF photo files. Built (by and) for Fujifilm shooters who shoot RAW+Lossy and want a fast way to decide what to keep.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

Point it at a folder of photos and it opens a browser-based image viewer. For each photo you can mark it one of:

- **Delete** — all associated files (RAW + JPEG/HEIF + WAV memo) go to a `_triage_trash/` subfolder
- **Keep JPEG/HEIF** — trash the RAW, keep the JPEG/HEIF and any WAV memo
- **Keep Both** — move the RAW to an archive folder, keep the JPEG/HEIF in place

You can also mark photos as **favourites** (`F`).

Once you've triaged everything, hit **Apply** to execute all decisions at once and, on macOS, optionally import the kept JPEG/HEIFs straight into Apple Photos, with your favourites flagged as Favorites (see [below](#apple-photos-import-and-favourites)). Decisions and favourites are saved as you go (in a hidden `.raw-triage.json` in the photo folder), so you can quit and pick up where you left off.

![Example Screenshot](screenshot.jpeg)

## Features

- Side-by-side RAW vs JPEG comparison (toggle with `T`)
- Click-to-zoom at 100% for checking focus, with drag-to-pan
- **Focus peaking** overlay (`P`) with three sensitivity levels (`Shift+P`)
- **Camera AF point** marker for Fujifilm files (`A`); `Z` zooms straight into it. Fuji's own in-camera "Focus Check" verdict is shown in the EXIF panel (both need `exiftool`)
- **Clipping warnings** (`C`) and a **RAW headroom** readout: does the RAW hold highlight detail the JPEG lost? (see [below](#clipping-and-raw-headroom))
- Favourites, tagged with a `Favorite` keyword on Apply and flagged as Favorites when imported into Apple Photos
- Direct import into Apple Photos on Apply (macOS)
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
| `F` | Toggle favourite |
| `C` | Toggle clipping warnings |
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
--photos-album NAME  Default Apple Photos album for imports (editable in the Apply dialog)
--no-browser       Don't auto-open the browser
```

Example with explicit archive:

```bash
python triage.py ~/Photos/RAWs/Import --archive ~/Photos/RAWs/Archive
```

## Apple Photos import and favourites

On **Apply** (macOS only), tick *Import … into Apple Photos* to import every kept JPEG/HEIF (Keep JPEG/HEIF or Keep Both) into your Photos library, optionally into an album you name in the dialog. Photos you marked as favourites are flagged as **Favorites** in Photos, so they sync to your iPhone like any other Favorite.

- The first time, macOS asks whether your terminal may control Photos. Allow it (or later under *System Settings → Privacy & Security → Automation*). If you denied it, the import stops with an error: fix the permission and hit Apply again.
- Photos copies the files into its library; the originals stay in the import folder.
- RAW Triage remembers what it imported, so applying again never imports a photo twice.
- RAW-only photos are archived but not imported.

Independently of the import, favourites get a single `Favorite` keyword written into the file (and into an XMP sidecar next to archived RAWs), so other apps can find them too.

## Clipping and RAW headroom

The point of keeping a RAW is often that it still holds highlight detail the JPEG has lost. Press `C` to see clipping:

- **JPEG view:** red = blown (all channels at 252+), orange = some channels clipped (e.g. saturated colours, often recoverable), blue = crushed shadows (all channels at 3 or below).
- **RAW view (`T`):** red = clipped **on the sensor**, measured directly on the raw data before white balance and tone curve. That detail is gone for good. Toggle `T` while clipping is on: whatever is red in the JPEG but not in the RAW can be recovered from the RAW.

The *Exposure* section of the side panel puts numbers on it: how much of the JPEG is clipped, how much of the RAW is clipped at the sensor, and the RAW's **headroom** (how many stops its brightest 0.1% sits below saturation). A one-line verdict says whether the RAW recovers most of the clipped highlights. The thresholds are heuristics, so treat the verdict as a hint, not a ruling. Fuji's DR200/DR400 modes underexpose the RAW on purpose, so expect extra headroom on those shots.

## Focus peaking notes

Peaking highlights fine, high-contrast detail (a difference-of-Gaussians band-pass filter on the full-resolution image), drawn in red over a dimmed greyscale copy. Like in-camera peaking it depends on contrast: a sharp but low-contrast subject may barely light up, so use the sensitivity levels. A soft, defocused frame stays mostly dark. The developed RAW (half size, no in-camera sharpening) lights up less than the JPEG does. The thresholds (`PEAKING_THRESHOLDS` in `triage.py`) were calibrated on synthetic images, so adjust them if they don't suit your files.

The AF point comes from Fujifilm's `FocusPixel` maker-note tag. Manual-focus shots don't have one.

## Archiving

Kept RAWs are moved into one flat archive folder, and Apply **never overwrites** a file there. Fujifilm file numbers repeat (after DSCF9999, or across camera bodies), so a name can already be taken:

- If the archive already holds a **byte-identical** copy (e.g. you re-ran Apply after copying files by hand), the import-folder copy goes to `_triage_trash/` and counts as archived.
- If it holds a **different** file with the same name, the new RAW stays in the import folder untouched, and the Apply summary lists it so you can deal with it by hand.

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
