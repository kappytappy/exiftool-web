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
        return jsonify({"tags": data, "count": len(data)})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    args = ["-overwrite_original"]
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
        elif name in ("latitude", "longitude") and value:
            # Decimal degrees, e.g. 41.8781 / -87.6298.
            try:
                num = float(value)
            except ValueError:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return jsonify({"error": f"Invalid {name}: enter a number like 41.8781."}), 400
            lo, hi = (-90, 90) if name == "latitude" else (-180, 180)
            if not lo <= num <= hi:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return jsonify({"error": f"{name.capitalize()} must be between {lo} and {hi}."}), 400
            args.append(f"-{tag}={value}")
        elif name == "date_taken" and value:
            # datetime-local -> "YYYY:MM:DD HH:MM:SS"
            value = value.replace("T", " ").replace("-", ":")
            if len(value) == 16:  # no seconds given
                value += ":00"
            args.append(f"-{tag}={value}")
        else:
            args.append(f"-{tag}={value}")
    args.append(filepath)
    rc, _, serr = run_exiftool(*args)
    if rc != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": f"exiftool failed: {serr.strip()}"}), 500
    response = send_file(filepath, as_attachment=True,
                         download_name=Path(filepath).name)
    delete_later(tmpdir)
    return response


def delete_later(tmpdir, delay=30):
    """Remove a temp dir after the response has (presumably) been sent."""

    def _delete():
        time.sleep(delay)
        shutil.rmtree(tmpdir, ignore_errors=True)

    threading.Thread(target=_delete, daemon=True).start()


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
