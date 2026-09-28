#!/usr/bin/env python3
"""
ExifTool Web UI
===============
A tiny local web interface for ExifTool.

Run:
    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5000 in your browser.

Requires the `exiftool` command-line tool to be installed and on your PATH:
  - macOS:   brew install exiftool
  - Windows: download from https://exiftool.org and add it to PATH
  - Linux:   sudo apt install libimage-exiftool-perl

Or grab the standalone Windows .exe from the GitHub Releases page -
it bundles Python, this app, and ExifTool: just double-click it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

try:
    from version import APP_VERSION
except ImportError:
    APP_VERSION = "dev"

RELEASES_URL = "https://github.com/kappytappy/exiftool-web/releases"


def resource_path(rel):
    """Absolute path to a bundled resource (dev and PyInstaller-frozen)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


app = Flask(__name__, template_folder=resource_path("templates"))
# Refuse uploads larger than 200 MB.
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def find_exiftool():
    # Bundled exe (PyInstaller --add-binary "exiftool.exe;.") takes priority.
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "exiftool.exe")
        if os.path.isfile(bundled):
            return bundled
    # Otherwise rely on PATH (manual installs).
    return shutil.which("exiftool")


EXIFTOOL = find_exiftool()

# Friendly field name -> (exiftool tag, input type, placeholder)
EDITABLE_FIELDS = [
    ("title", "XMP:Title", "text", "Title"),
    ("description", "EXIF:ImageDescription", "text", "Description / caption"),
    ("author", "EXIF:Artist", "text", "Author / artist"),
    ("device", "EXIF:Model", "text", "Device (camera / phone model)"),
    ("copyright", "EXIF:Copyright", "text", "Copyright notice"),
    ("keywords", "IPTC:Keywords", "text", "Keywords (comma separated)"),
    ("date_taken", "EXIF:DateTimeOriginal", "datetime-local", "Date taken"),
    ("latitude", "EXIF:GPSLatitude", "text", "Latitude"),
    ("longitude", "EXIF:GPSLongitude", "text", "Longitude"),
]


def require_exiftool():
    if not EXIFTOOL:
        return jsonify(
            {"error": "exiftool not found. Install it and restart this app."}
        ), 500
    return None


def exiftool_cwd():
    """Working directory for exiftool runs.

    The bundled Windows exe needs its exiftool_files/ support folder,
    which it looks up relative to the working directory. In frozen mode
    both live in sys._MEIPASS; otherwise the working dir doesn't matter.
    """
    if getattr(sys, "frozen", False) and EXIFTOOL:
        return os.path.dirname(EXIFTOOL)
    return None


def run_exiftool(*args):
    """Run exiftool, return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [EXIFTOOL, *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=exiftool_cwd(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def save_upload(file_storage):
    """Save an uploaded file to a temp dir. Returns (tmpdir, filepath)."""
    tmpdir = tempfile.mkdtemp(prefix="exifweb_")
    filename = Path(file_storage.filename or "upload").name or "upload"
    filepath = os.path.join(tmpdir, filename)
    file_storage.save(filepath)
    return tmpdir, filepath


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"exiftool": bool(EXIFTOOL), "version": exiftool_version(),
                    "app_version": APP_VERSION, "releases_url": RELEASES_URL})


def exiftool_version():
    if not EXIFTOOL:
        return None
    rc, out, _ = run_exiftool("-ver")
    return out.strip() if rc == 0 else None


@app.route("/api/metadata", methods=["POST"])
def metadata():
    err = require_exiftool()
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    tmpdir, filepath = save_upload(request.files["file"])
    try:
        rc, out, serr = run_exiftool("-j", "-G", "-s", "-s", "-s", filepath)
        if rc != 0:
            return jsonify({"error": f"exiftool failed: {serr.strip()}"}), 500
        data = json.loads(out)[0]
        # Drop the SourceFile entry; keep everything else grouped like "EXIF:Tag".
        data.pop("SourceFile", None)
        # Split off exiftool's own pseudo-tags (never embedded in the file):
        # ExifTool:* = the tool's own version, File:* = filesystem details,
        # Composite:* = computed values. Shown only via the "file details" toggle.
        SYSTEM_GROUPS = ("ExifTool", "File", "Composite")
        tags, system_tags = {}, {}
        for k, v in data.items():
            grp = k.split(":")[0] if ":" in k else ""
            (system_tags if grp in SYSTEM_GROUPS else tags)[k] = v
        return jsonify({"tags": tags, "system_tags": system_tags,
                        "count": len(tags), "system_count": len(system_tags)})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def build_update_args(fields):
    """Turn the edit form into exiftool args. Returns (args, error)."""
    args = ["-overwrite_original"]
    gps_refs = []  # N/S/E/W refs must come after ALL coordinate tags (exiftool ordering quirk)
    tag_map = {name: tag for name, tag, _, _ in EDITABLE_FIELDS}
    for name, value in fields.items():
        tag = tag_map.get(name)
        if not tag:
            continue
        value = (value or "").strip()
        if name == "keywords":
            # Replace the whole keyword list.
            args.append(f"-{tag}=")
            for kw in [k.strip() for k in value.split(",") if k.strip()]:
                args.append(f"-{tag}={kw}")
        elif name in ("latitude", "longitude"):
            # Decimal degrees, e.g. 41.8781 / -87.6298. ExifTool silently
            # drops the sign of negative decimals, so write the unsigned
            # coordinate plus an explicit N/S/E/W ref tag.
            is_lat = name == "latitude"
            coord_tag = "EXIF:GPSLatitude" if is_lat else "EXIF:GPSLongitude"
            ref_tag = "EXIF:GPSLatitudeRef" if is_lat else "EXIF:GPSLongitudeRef"
            if not value:
                args.append(f"-{coord_tag}=")
                args.append(f"-{ref_tag}=")
                continue
            try:
                num = float(value)
            except ValueError:
                return None, f"Invalid {name}: enter a number like 41.8781."
            lo, hi = (-90, 90) if is_lat else (-180, 180)
            if not lo <= num <= hi:
                return None, f"{name.capitalize()} must be between {lo} and {hi}."
            args.append(f"-{coord_tag}={abs(num)}")
            hemi = ("N" if num >= 0 else "S") if is_lat else ("E" if num >= 0 else "W")
            gps_refs.append(f"-{ref_tag}={hemi}")
        elif name == "date_taken" and value:
            # datetime-local -> "YYYY:MM:DD HH:MM:SS"
            value = value.replace("T", " ").replace("-", ":")
            if len(value) == 16:  # no seconds given
                value += ":00"
            args.append(f"-{tag}={value}")
        else:
            args.append(f"-{tag}={value}")
    args.extend(gps_refs)
    return args, None


def apply_updates(filepath, fields):
    """Run exiftool over filepath. Returns an error string or None."""
    args, err = build_update_args(fields)
    if err:
        return err
    args.append(filepath)
    rc, _, serr = run_exiftool(*args)
    if rc != 0:
        return f"exiftool failed: {serr.strip()}"
    return None


@app.route("/api/update", methods=["POST"])
def update():
    """Apply edited tags and return the modified file for download."""
    err = require_exiftool()
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        fields = json.loads(request.form.get("fields", "{}"))
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid fields JSON."}), 400

    tmpdir, filepath = save_upload(request.files["file"])
    err = apply_updates(filepath, fields)
    if err:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": err}), 500
    response = send_file(filepath, as_attachment=True,
                         download_name=Path(filepath).name)
    delete_later(tmpdir)
    return response


@app.route("/api/save_to_pc", methods=["POST"])
def save_to_pc():
    """Apply edited tags, save straight to Downloads, stamp Windows dates.

    Returns JSON instead of a download: a browser download always stamps
    "now" as the file's Created date, but writing the file ourselves lets
    us stamp the date she chose, so Explorer Properties shows it.
    """
    err = require_exiftool()
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        fields = json.loads(request.form.get("fields", "{}"))
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid fields JSON."}), 400

    tmpdir, filepath = save_upload(request.files["file"])
    try:
        err = apply_updates(filepath, fields)
        if err:
            return jsonify({"error": err}), 500
        # Her date always wins; a fresh file would otherwise say "now".
        now = datetime.now().astimezone()
        created = parse_optional_dt(fields.get("date_taken")) or now
        dest = unique_download_path(Path(filepath).name)
        shutil.copy2(filepath, dest)
        stamp_err = set_windows_file_times(dest, created, created)
        if stamp_err:
            return jsonify({"error": stamp_err}), 500
        return jsonify({"ok": True, "filename": os.path.basename(dest),
                        "created": created.strftime("%Y-%m-%d %H:%M")})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def delete_later(tmpdir, delay=30):
    """Remove a temp dir after the response has (presumably) been sent."""

    def _delete():
        time.sleep(delay)
        shutil.rmtree(tmpdir, ignore_errors=True)

    threading.Thread(target=_delete, daemon=True).start()


# ----------------------------------------------------------------------------
# "Save to PC": stamp Windows filesystem dates
# ----------------------------------------------------------------------------
# Explorer's Properties reads Created/Modified from the file itself, not from
# embedded metadata — so a browser download can never carry her dates over
# (the browser always stamps "now"). Writing the finished file to her
# Downloads folder ourselves lets us stamp the dates she chose.
def _dt_to_filetime(dt):
    """datetime -> Windows FILETIME (100ns ticks since 1601-01-01 UTC)."""
    aware = dt.astimezone() if dt.tzinfo is None else dt
    utc = aware.astimezone(timezone.utc)
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    ft = int((utc - epoch).total_seconds() * 10_000_000)
    return ft


def set_windows_file_times(path, created=None, modified=None):
    """Stamp a file's Created / Last-modified dates (Windows only).

    created/modified: datetimes (naive = this computer's local time).
    Her date always wins: callers pass her chosen date, never the original.
    Returns an error string, or None on success / non-Windows.
    """
    if os.name != "nt":
        return None  # not Windows: nothing to stamp
    if created is None and modified is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        FILE_WRITE_ATTRIBUTES = 0x100
        OPEN_EXISTING = 3
        h = kernel32.CreateFileW(os.fspath(path), FILE_WRITE_ATTRIBUTES, 0,
                                 None, OPEN_EXISTING, 0, None)
        if h == wintypes.HANDLE(-1).value:  # INVALID_HANDLE_VALUE
            return "Could not open the file to stamp its dates."
        try:
            c = wintypes.FILETIME.from_buffer_copy(
                _dt_to_filetime(created).to_bytes(8, "little")) if created else None
            m = wintypes.FILETIME.from_buffer_copy(
                _dt_to_filetime(modified).to_bytes(8, "little")) if modified else None
            ok = kernel32.SetFileTime(h,
                                      ctypes.byref(c) if c else None,
                                      None,  # leave last-access alone
                                      ctypes.byref(m) if m else None)
            if not ok:
                return "Windows refused to set the file dates."
        finally:
            kernel32.CloseHandle(h)
    except Exception as e:
        return f"Could not stamp the Windows file dates: {e}"
    return None


def unique_download_path(filename):
    """A non-clobbering path inside the user's Downloads folder."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(downloads, exist_ok=True)
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(downloads, filename)
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = os.path.join(downloads, f"{base} ({n}){ext}")
    return candidate


def parse_optional_dt(raw):
    """datetime-local-ish value -> aware local datetime, or None if empty."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).astimezone()
        except ValueError:
            pass
    return None


@app.route("/api/strip", methods=["POST"])
def strip():
    """Remove ALL metadata and return the cleaned file for download."""
    err = require_exiftool()
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    tmpdir, filepath = save_upload(request.files["file"])
    rc, _, serr = run_exiftool("-overwrite_original", "-all=", filepath)
    if rc != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": f"exiftool failed: {serr.strip()}"}), 500

    response = send_file(filepath, as_attachment=True,
                         download_name=Path(filepath).name)
    delete_later(tmpdir)
    return response


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        # Double-clicked standalone exe: open the browser automatically.
        threading.Timer(
            1.5, lambda: webbrowser.open("http://127.0.0.1:5000")
        ).start()
    # Localhost only by default - this is a personal tool, not a public server.
    app.run(host="127.0.0.1", port=5000, debug=False)
