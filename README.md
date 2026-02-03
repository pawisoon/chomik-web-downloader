Chomik Downloader (Python)
===========================

NAS web implementation to download lots of files at once from chomikuj.pl to your NAS. Paste a folder URL, choose options, and download entire folders (including subfolders) with progress shown in the browser.

Python 3 with a web UI; tested on macOS with Python 3.11+ and runs on any recent Python 3 with `requests` and `Flask`.


## Web UI (Flask)

Run the web interface (login, URL input, progress list):

```bash
# Install dependencies
pip install -r requirements.txt

# Set Chomikuj credentials (and optional panel password)
export CHOMIK_USERNAME=your_login
export CHOMIK_PASSWORD=your_password
export PANEL_PASSWORD=optional_web_panel_password   # omit to disable login
export DOWNLOAD_FOLDER=./downloads                 # default: /app/downloads in Docker

# Run
python app.py
```

Then open http://localhost:5000, paste a chomikuj.pl folder URL (e.g. `https://chomikuj.pl/Username/FolderName`), optionally set a subfolder and options (recursive, keep structure, overwrite), and click **Rozpocznij pobieranie**. Files are saved under `DOWNLOAD_FOLDER` on your NAS.

**Docker:**

```bash
docker compose up --build
# Set CHOMIK_USERNAME, CHOMIK_PASSWORD (and optionally PANEL_PASSWORD, SECRET_KEY) in .env or environment.
# Web UI: http://localhost:8001   (downloads go to ./downloads)
```


## Requirements

- Python 3.8 or newer
- `requests` and `Flask` (for CLI only: `requests`):

```bash
pip install -r requirements.txt
# or for CLI only:  pip install requests
```


## CLI usage

From the project root:

```bash
python3 chomik.py \
  --user YOUR_LOGIN \
  --password YOUR_PASSWORD \
  --url "https://chomikuj.pl/Username/FolderName" \
  -r -s downloads
```

### Required arguments

- `--user`, `-u` – your `chomikuj.pl` username.
- `--password`, `-p` – your plaintext password **or**:
- `--hash` – MD5 hash of your password (alternative to `--password`).
- `--url` – folder or file URL on `chomikuj.pl`, e.g.
  `https://chomikuj.pl/Username/Some+Folder`.

You must provide **either** `--password` or `--hash`.


### Optional arguments

- `destination` (positional, default: `./`)
  - Root folder where files will be saved.
- `--recursive`, `-r`
  - Recursively traverse subfolders of the given URL.
- `--structure`, `-s`
  - Preserve the folder structure from `chomikuj.pl`, e.g.
    `downloads/Username/Folder/...`.
- `--overwrite`, `-o`
  - Overwrite already downloaded files.
- `--noprogress`, `-n`
  - Suppress progress logs.
- `--ext`
  - Comma‑separated list of allowed extensions, e.g. `--ext "pdf,epub,mobi"`.
- `--max-limit`
  - Do not download files larger than the specified size (bytes).

Examples:

```bash
# Download a single folder with full structure into ./downloads
python3 chomik.py -u USER -p PASS \
  --url "https://chomikuj.pl/Username/FolderName" -r -s downloads

# Download only PDFs (no recursion) into current directory
python3 chomik.py -u USER -p PASS \
  --url "https://chomikuj.pl/Username/FolderName" --ext "pdf"
```


## Output layout

With `-s/--structure` enabled, files are typically stored as:

```text
<destination>/
  Username/
    Folder/
      file1.pdf
      file2.epub
```

Without `--structure`, files are downloaded directly into the
`destination` folder.


## Debugging

For troubleshooting, the script may create the following files in the
project root:

- `debug_download_info_request.xml` – last SOAP request used to fetch
  file metadata.
- `debug_download_info.xml` – last SOAP response with file metadata.
- `debug_download_files_request.xml` – last SOAP request used to get
  download URLs.
- `debug_download_files.xml` – last SOAP response containing actual
  download URLs.

These can be safely deleted at any time; they are only for debugging.

