# ExifTool Web UI

A tiny local web app for reading, editing, and stripping file metadata with [ExifTool](https://exiftool.org). Everything runs on your own computer — files never leave your machine.

## Features

- **View** every metadata tag in a photo or file (searchable table)
- **Edit** common fields: title, description, author, copyright, keywords, date taken
- **Strip** all metadata with one click (great before sharing photos online)

## Setup

1. Install ExifTool:
   - **macOS:** `brew install exiftool`
   - **Windows:** download the Windows executable from https://exiftool.org and add it to your PATH
   - **Linux:** `sudo apt install libimage-exiftool-perl`
2. Install Python 3.10+ if you don't have it.
3. Install the one dependency:
   ```
   pip install -r requirements.txt
   ```
4. Start the app:
   ```
   python app.py
   ```
5. Open http://127.0.0.1:5000 in your browser.

The app only listens on localhost, so it's only reachable from your own computer.

## How it works

- `POST /api/metadata` — uploads a file to a temp dir, runs `exiftool -j -G`, returns the tags as JSON
- `POST /api/update` — writes edited tags with `exiftool -overwrite_original`, returns the modified file
- `POST /api/strip` — removes every tag with `exiftool -all=`, returns the cleaned file

Uploaded files live in a temp directory and are deleted automatically.
