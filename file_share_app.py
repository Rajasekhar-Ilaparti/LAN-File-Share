"""
LAN File Share  —  v5
======================
A Windows desktop app that shares a folder over your local network. Any phone
or PC on the same Wi-Fi opens a link (or scans a QR code) and gets a modern
file browser: previews, video streaming, multi-select ZIP downloads, and
drag-and-drop uploads back to the PC. An optional PIN keeps others out.

Requirements:
    - Python 3.8+ (from python.org, check "Add to PATH" during install)
    - pip install pillow qrcode   (Pillow draws the window; qrcode adds the QR code)

Run:
    python file_share_app.py

Build a standalone .exe (on Windows, same folder as this file + app_icon.ico):
    pip install pyinstaller
    pyinstaller --onefile --windowed --icon=app_icon.ico --name "LAN File Share" file_share_app.py
"""

import base64
import email.utils
import errno
import hashlib
import hmac
import html
import http.server
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import OrderedDict
from datetime import datetime
from http.cookies import SimpleCookie
from tkinter import filedialog, messagebox

try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


APP_NAME = "LAN File Share"
APP_VERSION = "5.0"
AUTHOR_NAME = "Viraj"
AUTHOR_URL = "https://github.com/Rajasekhar-Ilaparti"
UPDATE_API = "https://api.github.com/repos/Rajasekhar-Ilaparti/LAN-File-Share/releases/latest"
SETUP_ASSET = "LAN-File-Share-Setup.exe"  # installer file name attached to each GitHub release
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".lan_file_share_config.json")
FIREWALL_RULE = "LAN File Share"
IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console flash from the .exe


# =========================================================================
#  Config persistence
# =========================================================================

DEFAULT_CONFIG = {
    "recent_folders": [],
    "port": 8000,
    "allow_uploads": False,
    "require_pin": False,
    "pin": "",
    "auto_start": False,
    "check_updates": True,
    "adapter_ip": "",
    "secret": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(16)
    if not re.fullmatch(r"\d{6}", str(cfg.get("pin", ""))):
        cfg["pin"] = new_pin()
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def new_pin():
    return f"{secrets.randbelow(10 ** 6):06d}"


# =========================================================================
#  File helpers
# =========================================================================

KIND_BY_EXT = {}
for _kind, _exts in {
    "image": "jpg jpeg png gif webp bmp svg heic heif avif ico tif tiff",
    "video": "mp4 m4v mov mkv avi webm 3gp wmv flv ts",
    "audio": "mp3 wav flac m4a aac ogg opus wma",
    "pdf": "pdf",
    "doc": "doc docx odt rtf",
    "sheet": "xls xlsx ods csv",
    "slide": "ppt pptx odp key",
    "text": "txt md log ini cfg srt vtt",
    "code": "py js ts html htm css json xml yml yaml c cpp h java cs go rs sh bat ps1 sql",
    "archive": "zip rar 7z tar gz bz2 xz",
    "app": "exe msi apk dmg iso",
}.items():
    for _e in _exts.split():
        KIND_BY_EXT["." + _e] = _kind

# Windows' registry-based MIME table is often wrong or missing for media.
for _type, _exts in {
    "video/mp4": ".mp4 .m4v", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/quicktime": ".mov", "video/3gpp": ".3gp", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/wav": ".wav", "audio/flac": ".flac", "audio/ogg": ".ogg .opus", "audio/aac": ".aac",
    "image/jpeg": ".jpg .jpeg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/avif": ".avif", "image/heic": ".heic", "application/pdf": ".pdf",
    "text/plain": ".txt .log .md .ini .cfg .srt", "text/vtt": ".vtt", "application/json": ".json",
    "text/javascript": ".js", "text/css": ".css",
    "application/vnd.android.package-archive": ".apk",
}.items():
    for _e in _exts.split():
        mimetypes.add_type(_type, _e)

# Already-compressed formats: deflating them again burns CPU for ~0% gain.
STORED_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".3gp",
    ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz",
    ".docx", ".xlsx", ".pptx", ".pdf",
}

# Served with a sandbox CSP so an uploaded page can't run scripts on this origin.
ACTIVE_CONTENT_EXTS = {".html", ".htm", ".xhtml", ".svg", ".xml"}

PART_SUFFIX = ".lfs-part"  # in-progress uploads; hidden from listings and ZIPs
IO_CHUNK = 1024 * 1024
HIDDEN_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def kind_of(name):
    return KIND_BY_EXT.get(os.path.splitext(name)[1].lower(), "other")


def human_size(num_bytes):
    if num_bytes is None:
        return ""
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def is_hidden_name(name):
    return name.startswith(".") or name.endswith(PART_SUFFIX) or name.lower() in HIDDEN_NAMES


def is_hidden_entry(entry):
    if is_hidden_name(entry.name):
        return True
    if IS_WINDOWS:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & 0x6)  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        except (OSError, AttributeError):
            pass
    return False


def sanitize_filename(name):
    """Make a client-supplied name safe to create on Windows."""
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        return ""
    base, ext = os.path.splitext(name)
    if base.lower() in _WIN_RESERVED:
        base = "_" + base
    return base[:180] + ext[:20]


def safe_join(base, rel_path):
    """Join an already-decoded relative path onto base, refusing to escape it."""
    rel_path = (rel_path or "").replace("\\", "/").lstrip("/")
    full = os.path.normpath(os.path.join(base, rel_path))
    base_norm = os.path.normpath(base)
    if full != base_norm and not full.startswith(base_norm.rstrip(os.sep) + os.sep):
        raise ValueError("Blocked path traversal attempt")
    return full


def content_disposition(disposition, filename):
    ascii_name = re.sub(r'[^A-Za-z0-9 ._()-]', "_", filename) or "download"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"


def device_name(ua):
    ua = ua or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua or ("Macintosh" in ua and "Mobile" in ua):
        return "iPad"
    if "Android" in ua:
        m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build|\))", ua)
        model = m.group(1).strip() if m else ""
        return model if model and model != "K" else "Android phone"
    if "Windows" in ua:
        return "Windows PC"
    if "Macintosh" in ua:
        return "Mac"
    if "CrOS" in ua:
        return "Chromebook"
    if "Linux" in ua:
        return "Linux PC"
    return "Device"


# =========================================================================
#  Streaming multipart upload (no dependency on the deprecated cgi module)
# =========================================================================

def move_into_place(tmp_path, directory, name):
    """Rename tmp_path to directory/name without overwriting anything —
    "clip.mp4" becomes "clip (1).mp4", "clip (2).mp4", ... if taken."""
    base, ext = os.path.splitext(name)
    n = 0
    while True:
        candidate = os.path.join(directory, name if n == 0 else f"{base} ({n}){ext}")
        if not os.path.exists(candidate):
            try:
                os.rename(tmp_path, candidate)
                return candidate
            except FileExistsError:
                pass
        n += 1


def save_multipart_files(rfile, length, boundary: bytes, target_dir):
    """Stream a multipart/form-data body straight to disk.

    Reads at most `length` bytes from `rfile` in fixed-size chunks, so memory
    use stays flat however large the upload is. Each file is written to a
    temporary .lfs-part file and only renamed into place once it has arrived
    completely; an interrupted upload leaves nothing behind.
    Returns the final names of the files that were saved."""
    delim = b"\r\n--" + boundary
    keep = len(delim) - 1
    remaining = length
    buf = b"\r\n"  # lets the first boundary match the same pattern as the rest
    saved = []

    def fill():
        nonlocal buf, remaining
        if remaining <= 0:
            return False
        chunk = rfile.read(min(IO_CHUNK, remaining))
        if not chunk:
            raise ConnectionAbortedError("client disconnected mid-upload")
        remaining -= len(chunk)
        buf += chunk
        return True

    # Skip any preamble before the first boundary
    while True:
        i = buf.find(delim)
        if i != -1:
            buf = buf[i + len(delim):]
            break
        buf = buf[-keep:]
        if not fill():
            return saved

    while True:
        # After a boundary: "--" ends the body, CRLF starts another part
        while len(buf) < 2:
            if not fill():
                return saved
        if buf.startswith(b"--"):
            return saved
        buf = buf[2:]

        while True:
            h = buf.find(b"\r\n\r\n")
            if h != -1:
                break
            if len(buf) > 64 * 1024 or not fill():
                return saved
        headers_raw = buf[:h].decode("utf-8", errors="replace")
        buf = buf[h + 4:]

        m = re.search(r'filename="([^"]*)"', headers_raw, re.IGNORECASE)
        name = sanitize_filename(m.group(1)) if m else ""
        out = tmp_path = None
        if name:
            fd, tmp_path = tempfile.mkstemp(prefix=".", suffix=PART_SUFFIX, dir=target_dir)
            out = os.fdopen(fd, "wb")

        complete = False
        try:
            while True:
                i = buf.find(delim)
                if i != -1:
                    if out:
                        out.write(buf[:i])
                    buf = buf[i + len(delim):]
                    complete = True
                    break
                # Write everything except a tail that could be the start of a
                # boundary split across two reads.
                if len(buf) > keep:
                    if out:
                        out.write(buf[:-keep])
                    buf = buf[-keep:]
                if not fill():
                    break
        finally:
            if out:
                out.close()
                try:
                    if complete:
                        saved.append(os.path.basename(move_into_place(tmp_path, target_dir, name)))
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

        if not complete:
            return saved


# =========================================================================
#  Photo thumbnails (only when Pillow is installed)
# =========================================================================

_thumb_cache = OrderedDict()
_thumb_lock = threading.Lock()
THUMB_CACHE_MAX = 600


def make_thumbnail(path, mtime):
    key = (path, mtime)
    with _thumb_lock:
        if key in _thumb_cache:
            _thumb_cache.move_to_end(key)
            return _thumb_cache[key]
    with Image.open(path) as im:
        try:
            im.draft("RGB", (720, 720))  # JPEG: decode at reduced size, much faster
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        im.thumbnail((360, 360))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=80, optimize=True)
        data = out.getvalue()
    with _thumb_lock:
        _thumb_cache[key] = data
        while len(_thumb_cache) > THUMB_CACHE_MAX:
            _thumb_cache.popitem(last=False)
    return data


# =========================================================================
#  Web pages
# =========================================================================

BASE_CSS = r"""
:root{--bg:#f3f4f9;--surface:#fff;--surface-2:#f1f2f8;--border:#e3e5ef;--border-strong:#c9cdda;--text:#141824;--muted:#6a7186;
--accent:#5850ec;--accent-2:#4a42d9;--accent-soft:#efeefe;--ring:rgba(88,80,236,.25);--danger:#e0364d;--ok:#0f9f55;
--shadow:0 1px 2px rgba(16,24,40,.05),0 4px 14px rgba(16,24,40,.06);--radius:16px;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1016;--surface:#171a23;--surface-2:#20232e;--border:#262a36;
--border-strong:#383d4c;--text:#eceef4;--muted:#959db0;--accent:#7d79ff;--accent-2:#a29fff;--accent-soft:#25264a;--ring:rgba(125,121,255,.32);
--danger:#ff5d73;--ok:#3ddc84;--shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.25);color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1016;--surface:#171a23;--surface-2:#20232e;--border:#262a36;--border-strong:#383d4c;--text:#eceef4;
--muted:#959db0;--accent:#7d79ff;--accent-2:#a29fff;--accent-soft:#25264a;--ring:rgba(125,121,255,.32);--danger:#ff5d73;--ok:#3ddc84;
--shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.25);color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-tap-highlight-color:transparent}
a{color:inherit;text-decoration:none}
button,input,select{font:inherit;color:inherit}
.i{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:40px;padding:0 14px;border-radius:10px;border:1px solid var(--border);
background:var(--surface);cursor:pointer;font-weight:600;white-space:nowrap;transition:background .15s,border-color .15s}
.btn:hover{background:var(--surface-2)}
.btn:focus-visible,.icon-btn:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:var(--accent-2);border-color:var(--accent-2)}
.btn .i{width:18px;height:18px}
.logo{width:34px;height:34px;border-radius:10px;background:var(--accent);display:grid;place-items:center;color:#fff;flex:none}
.logo .i{width:19px;height:19px;stroke-width:2}
.credit{text-align:center;color:var(--muted);font-size:12px;margin-top:6px}
.credit .heart{color:var(--danger)}
.credit a{color:var(--accent);font-weight:600}
.credit a:hover{text-decoration:underline}
"""

ICON_SPRITE = r"""<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></symbol>
<symbol id="i-file" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></symbol>
<symbol id="i-file-text" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></symbol>
<symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></symbol>
<symbol id="i-video" viewBox="0 0 24 24"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></symbol>
<symbol id="i-music" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></symbol>
<symbol id="i-sheet" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/></symbol>
<symbol id="i-slides" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="13" rx="2"/><path d="M12 16v5M8 21h8"/></symbol>
<symbol id="i-code" viewBox="0 0 24 24"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></symbol>
<symbol id="i-archive" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/></symbol>
<symbol id="i-box" viewBox="0 0 24 24"><path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/></symbol>
<symbol id="i-download" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></symbol>
<symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 21V9M7 14l5-5 5 5M5 3h14"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></symbol>
<symbol id="i-list" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></symbol>
<symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></symbol>
<symbol id="i-left" viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></symbol>
<symbol id="i-right" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></symbol>
<symbol id="i-folder-plus" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10v6M9 13h6"/></symbol>
<symbol id="i-folder-up" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 16v-6M9 13l3-3 3 3"/></symbol>
<symbol id="i-wifi-off" viewBox="0 0 24 24"><path d="M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 5.2-2.8M19 12.9a10 10 0 0 0-2.2-1.6M2 8.8a15 15 0 0 1 4.2-2.6M22 8.8A15 15 0 0 0 11 5M12 20h.01"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></symbol>
<symbol id="i-share" viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></symbol>
</svg>"""

THEME_BOOT = """<script>try{var t=localStorage.getItem('lfs-theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>"""
CREDIT_HTML = (f'<div class="credit">Made with <span class="heart">\u2665</span> by {AUTHOR_NAME} \u00b7 '
               f'<a href="{AUTHOR_URL}" target="_blank" rel="noopener">GitHub</a></div>')

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#5850ec">
<title>{{TITLE}}</title>
<link rel="icon" href="{{PIXEL_LOGO}}">
{{THEME_BOOT}}
<style>
{{BASE_CSS}}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:10px 24px;background:var(--surface);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.pixlogo{width:32px;height:32px;image-rendering:pixelated;flex:none;filter:drop-shadow(0 2px 4px rgba(40,30,120,.25))}
.brand b{display:block;font-size:15px;white-space:nowrap}
.brand small{display:block;color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
.search{flex:1;max-width:560px;margin:0 auto;position:relative}
.search .i{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);width:18px;height:18px;pointer-events:none}
.search input{width:100%;height:40px;border-radius:10px;border:1px solid var(--border);background:var(--surface-2);padding:0 12px 0 38px;outline:none;font-size:15px}
.search input:focus{border-color:var(--accent);background:var(--surface);box-shadow:0 0 0 3px var(--ring)}
.icon-btn{width:40px;height:40px;border-radius:10px;border:1px solid var(--border);background:var(--surface);display:grid;place-items:center;cursor:pointer;flex:none}
.icon-btn:hover{background:var(--surface-2)}
.wrap{max-width:1120px;margin:0 auto;padding:22px 24px 140px}
.crumbs{display:flex;align-items:center;gap:2px;font-size:13px;color:var(--muted);overflow-x:auto;white-space:nowrap;scrollbar-width:none;margin-bottom:4px}
.crumbs::-webkit-scrollbar{display:none}
.crumbs a{padding:3px 6px;border-radius:6px}
.crumbs a:hover{background:var(--surface-2);color:var(--text)}
.crumbs .sep{opacity:.5;padding:0 2px}
.crumbs .cur{padding:3px 6px;color:var(--text);font-weight:600}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:4px 0 18px}
.head h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.015em;word-break:break-word}
.meta{color:var(--muted);font-size:13px;margin-top:2px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.bar{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.bar .grow{flex:1}
.bar .count{color:var(--muted);font-size:13px}
select{height:36px;border-radius:9px;border:1px solid var(--border);background:var(--surface);padding:0 10px;cursor:pointer;outline:none}
select:focus-visible{box-shadow:0 0 0 3px var(--ring)}
.seg{display:flex;border:1px solid var(--border);border-radius:9px;overflow:hidden;background:var(--surface)}
.seg button{border:0;background:none;width:38px;height:34px;display:grid;place-items:center;cursor:pointer;color:var(--muted)}
.seg button.on{background:var(--accent-soft);color:var(--accent)}
.seg .i{width:18px;height:18px}

.items.list{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.list .lh,.list .it{display:grid;grid-template-columns:48px minmax(0,1fr) 110px 160px 56px;align-items:center}
.list .lh{font-size:12px;color:var(--muted);font-weight:600;height:40px;border-bottom:1px solid var(--border);background:var(--surface-2);user-select:none}
.list .lh [data-sort]{cursor:pointer;display:inline-flex;align-items:center;gap:4px}
.list .lh [data-sort]:hover{color:var(--text)}
.list .lh .i{width:14px;height:14px}
.list .it{border-bottom:1px solid var(--border);min-height:58px;transition:background .1s}
.list .it:last-child{border-bottom:0}
.list .it:hover{background:var(--surface-2)}
.it.sel,.list .it.sel:hover{background:var(--accent-soft)}
.mn{display:flex;align-items:center;gap:12px;min-width:0;padding:9px 0;-webkit-touch-callout:none;outline:none}
.mn:focus-visible .nm{text-decoration:underline}
.tx{min-width:0;display:flex;flex-direction:column}
.nm{font-weight:550;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm mark{background:rgba(250,204,21,.4);color:inherit;border-radius:3px}
.sb{font-size:12px;color:var(--muted);display:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c-size,.c-date{color:var(--muted);font-size:13px;white-space:nowrap}
.ab{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;color:var(--muted)}
.ab:hover{background:var(--accent-soft);color:var(--accent)}
.ck{position:relative;display:grid;place-items:center;align-self:stretch;cursor:pointer}
.ck input{position:absolute;opacity:0;pointer-events:none}
.ck span{width:18px;height:18px;border-radius:5px;border:1.5px solid var(--border-strong);display:grid;place-items:center;background:var(--surface)}
.ck input:checked+span,.ck input:indeterminate+span{background:var(--accent);border-color:var(--accent)}
.ck input:checked+span::after{content:"";width:9px;height:5px;border:2px solid #fff;border-top:0;border-right:0;transform:translateY(-1px) rotate(-45deg)}
.ck input:indeterminate+span::after{content:"";width:8px;height:2px;background:#fff;border-radius:2px}
.ck input:focus-visible+span{box-shadow:0 0 0 3px var(--ring)}

.tile{position:relative;width:42px;height:42px;border-radius:11px;display:grid;place-items:center;flex:none;background:var(--cs);color:var(--c);overflow:hidden}
.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.k-folder{--c:#d97706;--cs:rgba(245,158,11,.16)}
.k-folder .i{fill:currentColor;fill-opacity:.22}
.k-image{--c:#059669;--cs:rgba(16,185,129,.14)}
.k-video{--c:#db2777;--cs:rgba(236,72,153,.14)}
.k-audio{--c:#7c3aed;--cs:rgba(139,92,246,.15)}
.k-pdf{--c:#dc2626;--cs:rgba(239,68,68,.13)}
.k-doc,.k-text{--c:#2563eb;--cs:rgba(59,130,246,.14)}
.k-sheet{--c:#16a34a;--cs:rgba(34,197,94,.14)}
.k-slide{--c:#ea580c;--cs:rgba(249,115,22,.15)}
.k-code{--c:#0891b2;--cs:rgba(6,182,212,.14)}
.k-archive{--c:#a16207;--cs:rgba(202,138,4,.15)}
.k-app,.k-other{--c:#64748b;--cs:rgba(100,116,139,.15)}
:root[data-theme="dark"] .tile{--c:color-mix(in srgb,var(--c) 70%,white)}

.items.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:14px}
.grid .it{position:relative;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);transition:transform .12s,box-shadow .12s,border-color .12s}
.grid .it:hover{transform:translateY(-2px);box-shadow:0 10px 26px rgba(16,24,40,.12)}
.grid .it.sel{border-color:var(--accent)}
.grid .mn{flex-direction:column;align-items:stretch;gap:0;padding:0}
.grid .th{aspect-ratio:4/3;display:block;background:var(--surface-2)}
.grid .tile.big{width:100%;height:100%;border-radius:0}
.grid .tile.big .i{width:40px;height:40px;stroke-width:1.5}
.grid .tx{padding:10px 12px 12px}
.grid .sb{display:block}
.grid .ck{position:absolute;top:8px;left:8px;width:30px;height:30px;z-index:2;opacity:0;transition:opacity .12s}
.grid .it:hover .ck,.grid.selecting .ck,.grid .it.sel .ck{opacity:1}
.grid .ab{position:absolute;top:8px;right:8px;width:34px;height:34px;background:var(--surface);box-shadow:var(--shadow);opacity:0;transition:opacity .12s}
.grid .it:hover .ab{opacity:1}
.grid .lh,.grid .c-size,.grid .c-date{display:none}
@media (hover:none){.grid .ab{opacity:1}}

.empty{text-align:center;padding:64px 20px;color:var(--muted);background:var(--surface);border:1px dashed var(--border-strong);border-radius:var(--radius)}
.empty .tile{margin:0 auto 14px;width:56px;height:56px;border-radius:16px}
.empty .tile .i{width:26px;height:26px}
.empty b{display:block;color:var(--text);font-size:16px;margin-bottom:4px}
.foot{text-align:center;color:var(--muted);font-size:12px;margin-top:28px}

.selbar{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translate(-50%,180%);transition:transform .22s cubic-bezier(.2,.8,.2,1);z-index:30;
display:flex;align-items:center;gap:8px;background:#151a26;color:#fff;padding:8px 8px 8px 10px;border-radius:14px;box-shadow:0 12px 32px rgba(0,0,0,.28);max-width:calc(100% - 32px)}
:root[data-theme="dark"] .selbar{background:#2a2f3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .selbar{background:#2a2f3b}}
.selbar.show{transform:translate(-50%,0)}
.selbar .lbl{font-weight:600;padding:0 6px;white-space:nowrap}
.selbar .btn{height:36px;background:transparent;border-color:rgba(255,255,255,.22);color:#fff}
.selbar .btn:hover{background:rgba(255,255,255,.1)}
.selbar .btn.primary{background:#5850ec;border-color:#5850ec}
.selbar .xb{width:34px;height:34px;border:0;border-radius:9px;background:none;color:#fff;display:grid;place-items:center;cursor:pointer}
.selbar .xb:hover{background:rgba(255,255,255,.1)}

.drop{position:fixed;inset:0;z-index:40;background:rgba(79,70,229,.14);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);display:none;place-items:center;padding:24px}
.drop.show{display:grid}
.drop>div{border:2px dashed var(--accent);border-radius:22px;padding:44px 52px;background:var(--surface);text-align:center;box-shadow:0 20px 50px rgba(0,0,0,.2);pointer-events:none}
.drop .tile{margin:0 auto 12px;width:56px;height:56px;--c:var(--accent);--cs:var(--accent-soft)}
.drop b{display:block;font-size:18px}
.drop span{color:var(--muted)}

.up{position:fixed;right:20px;bottom:calc(20px + env(safe-area-inset-bottom));width:380px;max-width:calc(100% - 32px);background:var(--surface);border:1px solid var(--border);
border-radius:16px;box-shadow:0 18px 44px rgba(16,24,40,.2);z-index:35;overflow:hidden}
.uph{display:flex;align-items:center;gap:6px;padding:12px 10px 12px 16px}
.uph .t{flex:1;min-width:0}
.uph b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.uph small{color:var(--muted);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.upbar{height:3px;background:var(--surface-2)}
.upbar i{display:block;height:100%;background:var(--accent);width:0;transition:width .25s}
.upl{max-height:300px;overflow:auto;border-top:1px solid var(--border)}
.up.min .upl{display:none}
.up.min #upMin{transform:rotate(180deg)}
.ui{display:flex;align-items:center;gap:10px;padding:10px 12px 10px 16px;border-bottom:1px solid var(--border)}
.ui:last-child{border-bottom:0}
.ui .tx{flex:1}
.ui .nm{font-size:13px}
.ui .sb{display:block}
.ui .pb{height:4px;border-radius:4px;background:var(--surface-2);margin-top:6px;overflow:hidden}
.ui .pb i{display:block;height:100%;background:var(--accent);width:0;transition:width .2s}
.ui.done .pb i{background:var(--ok)}
.ui.fail .sb{color:var(--danger)}
.ui.cancel{opacity:.55}
.ui .ok{color:var(--ok);display:grid;place-items:center;width:30px}
.sbtn{border:0;background:none;color:var(--muted);cursor:pointer;width:32px;height:32px;border-radius:8px;display:grid;place-items:center;flex:none}
.sbtn:hover{background:var(--surface-2);color:var(--text)}
.sbtn .i{width:18px;height:18px;transition:transform .2s}

.viewer{position:fixed;inset:0;z-index:50;background:rgba(6,8,14,.95);display:flex;flex-direction:column;color:#fff}
.vh{display:flex;align-items:center;gap:10px;padding:10px 12px;padding-top:calc(10px + env(safe-area-inset-top))}
.vh .tx{flex:1}
.vh .nm{color:#fff}
.vh .sb{display:block;color:rgba(255,255,255,.6)}
.vbtn{width:42px;height:42px;border-radius:11px;border:0;background:rgba(255,255,255,.1);color:#fff;display:grid;place-items:center;cursor:pointer;flex:none}
.vbtn:hover{background:rgba(255,255,255,.18)}
.vh .btn{background:rgba(255,255,255,.1);border-color:transparent;color:#fff}
.vh .btn:hover{background:rgba(255,255,255,.18)}
.vb{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:0 12px 12px;position:relative;touch-action:pan-y}
.vb img,.vb video{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px;background:#000}
.vb iframe{width:100%;height:100%;border:0;border-radius:8px;background:#fff}
.vb pre{width:100%;max-width:980px;height:100%;overflow:auto;margin:0;padding:18px;background:#11141b;border:1px solid #232838;border-radius:12px;
font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word;color:#e5e7eb}
.vb .aud{text-align:center;width:100%;max-width:420px}
.vb .aud .tile{width:120px;height:120px;border-radius:28px;margin:0 auto 22px}
.vb .aud .i{width:52px;height:52px}
.vb audio{width:100%}
.vmsg{text-align:center;color:rgba(255,255,255,.75);max-width:340px}
.vmsg .i{width:36px;height:36px;margin-bottom:6px}
.vnav{position:absolute;top:50%;transform:translateY(-50%);width:46px;height:46px;border-radius:50%;border:0;background:rgba(255,255,255,.12);color:#fff;display:grid;place-items:center;cursor:pointer}
.vnav:hover{background:rgba(255,255,255,.22)}
.vnav.prev{left:16px}.vnav.next{right:16px}
@media (hover:none){.vnav{display:none}}

.offline{position:fixed;top:calc(12px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);z-index:60;background:#b91c1c;color:#fff;padding:10px 16px;border-radius:12px;
display:flex;gap:10px;align-items:center;box-shadow:0 10px 30px rgba(0,0,0,.25);font-size:13px;width:max-content;max-width:calc(100% - 32px)}
.offline .i{width:18px;height:18px}
.spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
#toast{position:fixed;left:50%;bottom:calc(28px + env(safe-area-inset-bottom));transform:translate(-50%,16px);background:#151a26;color:#fff;padding:11px 18px;border-radius:12px;
font-size:13px;box-shadow:0 10px 30px rgba(0,0,0,.25);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:70;max-width:calc(100% - 32px);text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}
body.has-sel #toast{bottom:calc(90px + env(safe-area-inset-bottom))}

@media (max-width:760px){
  .topbar{padding:10px 16px;gap:10px}
  .brand .bt{display:none}
  .wrap{padding:16px 16px 140px}
  .head{margin-bottom:14px}
  .head h1{font-size:21px}
  .actions{width:100%}
  .actions .btn{flex:1}
  .list .lh{display:none}
  .list .it{grid-template-columns:44px minmax(0,1fr) 50px}
  .list .c-size,.list .c-date{display:none}
  .list .sb{display:block}
  .items.grid{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
  .hide-sm{display:none!important}
  .up{right:16px;left:16px;width:auto;max-width:none}
}
</style>
</head>
<body>
{{ICONS}}
<header class="topbar">
  <a class="brand" href="/" title="Home">
    <img class="pixlogo" src="{{PIXEL_LOGO}}" alt="" width="32" height="32">
    <span class="bt"><b>LAN File Share</b><small id="hostLbl"></small></span>
  </a>
  <label class="search"><svg class="i"><use href="#i-search"/></svg><input id="q" type="search" placeholder="Search this folder" autocomplete="off" aria-label="Search this folder"></label>
  <button class="icon-btn" id="themeBtn" title="Switch light/dark" aria-label="Switch light or dark theme"><svg class="i"><use id="themeIc" href="#i-moon"/></svg></button>
</header>
<main class="wrap">
  <nav class="crumbs" id="crumbs" aria-label="Folder path"></nav>
  <div class="head">
    <div><h1 id="title"></h1><div class="meta" id="meta"></div></div>
    <div class="actions">
      <button class="btn primary" id="upBtn" hidden><svg class="i"><use href="#i-upload"/></svg>Upload</button>
      <button class="btn hide-sm" id="dirBtn" hidden><svg class="i"><use href="#i-folder-up"/></svg>Upload folder</button>
      <button class="btn" id="mkBtn" hidden><svg class="i"><use href="#i-folder-plus"/></svg><span>New folder</span></button>
      <a class="btn" id="zipAll"><svg class="i"><use href="#i-download"/></svg>Download all</a>
    </div>
  </div>
  <div class="bar">
    <span class="count" id="count"></span>
    <span class="grow"></span>
    <select id="sort" aria-label="Sort by">
      <option value="name-asc">Name A–Z</option><option value="name-desc">Name Z–A</option>
      <option value="date-desc">Newest first</option><option value="date-asc">Oldest first</option>
      <option value="size-desc">Largest first</option><option value="size-asc">Smallest first</option>
    </select>
    <div class="seg" role="group" aria-label="View">
      <button data-view="list" title="List view" aria-label="List view"><svg class="i"><use href="#i-list"/></svg></button>
      <button data-view="grid" title="Grid view" aria-label="Grid view"><svg class="i"><use href="#i-grid"/></svg></button>
    </div>
  </div>
  <section id="items" class="items list"></section>
  <div id="empty" class="empty" hidden></div>
  <div class="foot" id="foot"></div>
  {{CREDIT}}
</main>

<div class="selbar" id="selbar" role="toolbar" aria-label="Selection">
  <button class="xb" id="selClear" title="Clear selection" aria-label="Clear selection"><svg class="i"><use href="#i-x"/></svg></button>
  <span class="lbl" id="selCount"></span>
  <button class="btn hide-sm" id="selAllBtn">Select all</button>
  <button class="btn primary" id="selDl"><svg class="i"><use href="#i-download"/></svg>Download</button>
</div>

<div class="drop" id="drop"><div><span class="tile"><svg class="i"><use href="#i-upload"/></svg></span><b>Drop to upload</b><span id="dropTo"></span></div></div>

<div class="up" id="up" hidden>
  <div class="uph">
    <div class="t"><b id="upTitle"></b><small id="upSub"></small></div>
    <button class="sbtn" id="upMin" title="Collapse" aria-label="Collapse"><svg class="i"><use href="#i-down"/></svg></button>
    <button class="sbtn" id="upClose" title="Close" aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button>
  </div>
  <div class="upbar"><i id="upBar"></i></div>
  <div class="upl" id="upList"></div>
</div>

<div class="viewer" id="viewer" hidden role="dialog" aria-modal="true">
  <div class="vh">
    <button class="vbtn" id="vClose" title="Close (Esc)" aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button>
    <div class="tx"><span class="nm" id="vName"></span><span class="sb" id="vSub"></span></div>
    <a class="btn" id="vDl"><svg class="i"><use href="#i-download"/></svg><span class="hide-sm">Download</span></a>
  </div>
  <div class="vb" id="vBody"></div>
  <button class="vnav prev" id="vPrev" aria-label="Previous"><svg class="i"><use href="#i-left"/></svg></button>
  <button class="vnav next" id="vNext" aria-label="Next"><svg class="i"><use href="#i-right"/></svg></button>
</div>

<div class="offline" id="offline" hidden><span class="spin"></span><span>Can't reach the PC. Check it's still sharing and you're on the same Wi-Fi. Retrying…</span></div>
<div id="toast" role="status" aria-live="polite"></div>
<input type="file" id="fileIn" multiple hidden>
<input type="file" id="dirIn" webkitdirectory multiple hidden>

<script id="lfs-data" type="application/json">{{DATA}}</script>
<script>
(function(){
'use strict';
var D=JSON.parse(document.getElementById('lfs-data').textContent);
function $(s){return document.querySelector(s)}
var LS={get:function(k,d){try{var v=localStorage.getItem('lfs-'+k);return v==null?d:v}catch(e){return d}},set:function(k,v){try{localStorage.setItem('lfs-'+k,v)}catch(e){}}};
var touch=!!(window.matchMedia&&matchMedia('(hover: none)').matches);
var S={entries:D.entries,q:'',sort:LS.get('sort','name-asc'),view:LS.get('view','list'),sel:new Set(),uploads:D.uploads,list:[]};
var collator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});

function esc(s){return String(s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function ic(n){return '<svg class="i" aria-hidden="true"><use href="#i-'+n+'"/></svg>'}
function encPath(p){return p?p.split('/').map(encodeURIComponent).join('/'):''}
var BASE='/'+(D.path?encPath(D.path)+'/':'');
function urlOf(e){return BASE+encodeURIComponent(e.n)+(e.d?'/':'')}
function relOf(n){return (D.path?D.path+'/':'')+n}
function ext(n){var i=n.lastIndexOf('.');return i<1?'':n.slice(i+1).toLowerCase()}
function fmtSize(b){if(b==null)return '';var u=['B','KB','MB','GB','TB'],i=0;while(b>=1024&&i<4){b/=1024;i++}return (i?(b<10?b.toFixed(1):Math.round(b)):b)+' '+u[i]}
function fmtDate(s){var d=new Date(s*1000),n=new Date();
  if(d.toDateString()===n.toDateString())return 'Today, '+d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
  var o={month:'short',day:'numeric'};if(d.getFullYear()!==n.getFullYear())o.year='numeric';return d.toLocaleDateString([],o)}
function zipUrl(names){var u='/__zip?path='+encodeURIComponent(D.path);(names||[]).forEach(function(n){u+='&items='+encodeURIComponent(n)});return u}
var KI={folder:'folder',image:'image',video:'video',audio:'music',pdf:'file-text',doc:'file-text',text:'file-text',sheet:'sheet',slide:'slides',code:'code',archive:'archive',app:'box',other:'file'};
var IMG={jpg:1,jpeg:1,png:1,gif:1,webp:1,bmp:1,svg:1,avif:1,ico:1};
function pv(e){if(e.d)return null;var x=ext(e.n);
  if(e.k==='image')return IMG[x]?'image':null;
  if(e.k==='video')return 'video';
  if(e.k==='audio')return 'audio';
  if(e.k==='pdf')return touch?null:'pdf';
  if((e.k==='text'||e.k==='code')&&e.s<2e6)return 'text';
  return null}
function tile(e,big){var h='<span class="tile k-'+e.k+(big?' big':'')+'">'+ic(KI[e.k]||'file');
  if(e.k==='image')h+='<img loading="lazy" decoding="async" alt="" src="/__thumb?path='+encodeURIComponent(relOf(e.n))+'&v='+e.m+'" onerror="this.remove()">';
  return h+'</span>'}
function hl(n){var q=S.q.trim().toLowerCase();if(!q)return esc(n);var i=n.toLowerCase().indexOf(q);if(i<0)return esc(n);
  return esc(n.slice(0,i))+'<mark>'+esc(n.slice(i,i+q.length))+'</mark>'+esc(n.slice(i+q.length))}

/* ---------- theme ---------- */
function isDark(){var t=document.documentElement.getAttribute('data-theme');return t?t==='dark':!!(window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)}
function themeIcon(){$('#themeIc').setAttribute('href',isDark()?'#i-sun':'#i-moon')}
$('#themeBtn').onclick=function(){var t=isDark()?'light':'dark';document.documentElement.setAttribute('data-theme',t);LS.set('theme',t);themeIcon()};
themeIcon();

/* ---------- listing ---------- */
function visible(){var q=S.q.trim().toLowerCase();
  var list=S.entries.filter(function(e){return !q||e.n.toLowerCase().indexOf(q)>=0});
  var p=S.sort.split('-'),key=p[0],dir=p[1]==='desc'?-1:1;
  list.sort(function(a,b){if(a.d!==b.d)return a.d?-1:1;var r=0;
    if(key==='size')r=((a.s||0)-(b.s||0))*dir;else if(key==='date')r=(a.m-b.m)*dir;else r=collator.compare(a.n,b.n)*dir;
    return r||collator.compare(a.n,b.n)});
  return list}
function hrefOf(e){return e.d||pv(e)?urlOf(e):urlOf(e)+'?dl=1'}
function dlBtn(e){return '<a class="ab" href="'+esc(e.d?zipUrl([e.n]):urlOf(e)+'?dl=1')+'" title="'+(e.d?'Download folder as ZIP':'Download')+'" aria-label="Download '+esc(e.n)+'">'+ic('download')+'</a>'}
function ck(e){var on=S.sel.has(e.n);return '<label class="ck"><input type="checkbox"'+(on?' checked':'')+' aria-label="Select '+esc(e.n)+'"><span></span></label>'}
function sub(e){return (e.d?'Folder':fmtSize(e.s))+' · '+fmtDate(e.m)}
function rowHtml(e,i){return '<div class="it'+(S.sel.has(e.n)?' sel':'')+'" data-i="'+i+'">'+ck(e)+
  '<a class="mn" href="'+esc(hrefOf(e))+'">'+tile(e)+'<span class="tx"><span class="nm" title="'+esc(e.n)+'">'+hl(e.n)+'</span><span class="sb">'+sub(e)+'</span></span></a>'+
  '<span class="c-size">'+(e.d?'—':fmtSize(e.s))+'</span><span class="c-date">'+fmtDate(e.m)+'</span>'+dlBtn(e)+'</div>'}
function cardHtml(e,i){return '<div class="it'+(S.sel.has(e.n)?' sel':'')+'" data-i="'+i+'">'+ck(e)+
  '<a class="mn" href="'+esc(hrefOf(e))+'"><span class="th">'+tile(e,true)+'</span><span class="tx"><span class="nm" title="'+esc(e.n)+'">'+hl(e.n)+'</span><span class="sb">'+sub(e)+'</span></span></a>'+dlBtn(e)+'</div>'}
function sortHead(key,label){var p=S.sort.split('-'),arrow=p[0]===key?ic(p[1]==='asc'?'down':'down').replace('class="i"','class="i" style="transform:rotate('+(p[1]==='asc'?180:0)+'deg)"'):'';
  return '<span data-sort="'+key+'">'+label+arrow+'</span>'}
function render(){
  var list=visible();S.list=list;
  var box=$('#items'),empty=$('#empty');box.className='items '+S.view+(S.sel.size?' selecting':'');
  if(!list.length){box.hidden=true;empty.hidden=false;
    empty.innerHTML=S.entries.length?'<b>No matches</b>Nothing in this folder matches “'+esc(S.q.trim())+'”.':
      '<span class="tile k-folder">'+ic('folder')+'</span><b>This folder is empty</b>'+(S.uploads?'Drop files here or press Upload to add some.':'Files added on the PC will appear here.');
  }else{box.hidden=false;empty.hidden=true;
    var h=S.view==='list'?'<div class="lh"><label class="ck"><input type="checkbox" id="selAll" aria-label="Select all"><span></span></label>'+sortHead('name','Name')+'<span>'+sortHead('size','Size')+'</span><span>'+sortHead('date','Modified')+'</span><span></span></div>':'';
    for(var i=0;i<list.length;i++)h+=S.view==='list'?rowHtml(list[i],i):cardHtml(list[i],i);
    box.innerHTML=h}
  var f=0,d=0,sz=0;S.entries.forEach(function(e){if(e.d)d++;else{f++;sz+=e.s||0}});
  var parts=[];if(d)parts.push(d+(d>1?' folders':' folder'));if(f)parts.push(f+(f>1?' files':' file'));if(f)parts.push(fmtSize(sz));
  $('#meta').textContent=parts.join(' · ')||'Empty folder';
  $('#count').textContent=S.q.trim()?list.length+' of '+S.entries.length+' shown':'';
  $('#zipAll').hidden=!S.entries.length;
  updateSel()}
function renderCrumbs(){var parts=D.path?D.path.split('/'):[];var c=$('#crumbs');
  if(!parts.length){c.hidden=true;return}
  var h='<a href="/">'+esc(D.root)+'</a>',acc='';
  parts.forEach(function(p,i){acc+='/'+encodeURIComponent(p);h+='<span class="sep">/</span>'+(i===parts.length-1?'<span class="cur">'+esc(p)+'</span>':'<a href="'+acc+'/">'+esc(p)+'</a>')});
  c.innerHTML=h;c.scrollLeft=c.scrollWidth}

/* ---------- selection ---------- */
function toggleSel(n,on){if(on===undefined)on=!S.sel.has(n);if(on)S.sel.add(n);else S.sel.delete(n)}
function updateSel(){var n=S.sel.size;$('#selbar').classList.toggle('show',n>0);document.body.classList.toggle('has-sel',n>0);
  $('#selCount').textContent=n+' selected';$('#items').classList.toggle('selecting',n>0);
  var els=document.querySelectorAll('#items .it');for(var i=0;i<els.length;i++){var el=els[i],e=S.list[+el.getAttribute('data-i')],on=S.sel.has(e.n);
    el.classList.toggle('sel',on);var c=el.querySelector('input');if(c)c.checked=on}
  var all=$('#selAll');if(all){all.checked=n>0&&n>=S.list.length;all.indeterminate=n>0&&n<S.list.length}}
var items=$('#items'),suppress=false,lpT=null,lpX=0,lpY=0;
items.addEventListener('click',function(ev){
  var sh=ev.target.closest('[data-sort]');if(sh){var k=sh.getAttribute('data-sort'),p=S.sort.split('-');
    S.sort=k+'-'+(p[0]===k?(p[1]==='asc'?'desc':'asc'):(k==='name'?'asc':'desc'));LS.set('sort',S.sort);$('#sort').value=S.sort;render();return}
  var it=ev.target.closest('.it');if(!it||ev.target.closest('.ck'))return;
  if(suppress){ev.preventDefault();suppress=false;return}
  var e=S.list[+it.getAttribute('data-i')];
  if(ev.target.closest('.ab')){if(e.d)toast('Preparing ZIP… the download will start shortly');return}
  if(!ev.target.closest('.mn'))return;
  if(S.sel.size){ev.preventDefault();toggleSel(e.n);updateSel();return}
  if(ev.ctrlKey||ev.metaKey||ev.shiftKey)return;
  if(pv(e)){ev.preventDefault();openViewer(e)}});
items.addEventListener('change',function(ev){var t=ev.target;
  if(t.id==='selAll'){if(t.checked)S.list.forEach(function(e){S.sel.add(e.n)});else S.sel.clear();updateSel();return}
  var it=t.closest('.it');if(it){toggleSel(S.list[+it.getAttribute('data-i')].n,t.checked);updateSel()}});
items.addEventListener('touchstart',function(ev){var it=ev.target.closest('.it');if(!it||ev.target.closest('.ck,.ab'))return;
  var t=ev.touches[0];lpX=t.clientX;lpY=t.clientY;clearTimeout(lpT);
  lpT=setTimeout(function(){lpT=null;suppress=true;toggleSel(S.list[+it.getAttribute('data-i')].n);updateSel();if(navigator.vibrate)navigator.vibrate(15)},480)},{passive:true});
items.addEventListener('touchmove',function(ev){if(!lpT)return;var t=ev.touches[0];if(Math.abs(t.clientX-lpX)>10||Math.abs(t.clientY-lpY)>10){clearTimeout(lpT);lpT=null}},{passive:true});
items.addEventListener('touchend',function(){clearTimeout(lpT);lpT=null;if(suppress)setTimeout(function(){suppress=false},450)},{passive:true});
items.addEventListener('contextmenu',function(ev){if(touch&&ev.target.closest('.it'))ev.preventDefault()});
$('#selClear').onclick=function(){S.sel.clear();updateSel()};
$('#selAllBtn').onclick=function(){S.list.forEach(function(e){S.sel.add(e.n)});updateSel()};
$('#selDl').onclick=function(){var names=Array.from(S.sel);
  if(names.length===1){var e=S.entries.find(function(x){return x.n===names[0]});if(e&&!e.d){location.href=urlOf(e)+'?dl=1';return}}
  location.href=zipUrl(names);toast('Preparing ZIP of '+names.length+' item'+(names.length>1?'s':'')+'… the download will start shortly')};
$('#zipAll').onclick=function(){this.href=zipUrl();toast('Preparing ZIP… the download will start shortly')};

/* ---------- toolbar ---------- */
var qIn=$('#q');qIn.addEventListener('input',function(){S.q=qIn.value;render()});
qIn.addEventListener('keydown',function(ev){if(ev.key==='Escape'){qIn.value='';S.q='';render();qIn.blur()}});
$('#sort').value=S.sort;$('#sort').onchange=function(){S.sort=this.value;LS.set('sort',S.sort);render()};
function setView(v){S.view=v;LS.set('view',v);document.querySelectorAll('.seg button').forEach(function(b){var on=b.getAttribute('data-view')===v;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on)});render()}
document.querySelectorAll('.seg button').forEach(function(b){b.onclick=function(){setView(b.getAttribute('data-view'))}});
document.addEventListener('keydown',function(ev){if(V.open)return;
  if(ev.key==='/'&&document.activeElement!==qIn){ev.preventDefault();qIn.focus()}
  else if(ev.key==='Escape'&&S.sel.size){S.sel.clear();updateSel()}});

/* ---------- viewer ---------- */
var V={open:false,list:[],i:0};
function clearV(){var b=$('#vBody');b.querySelectorAll('video,audio').forEach(function(m){try{m.pause();m.removeAttribute('src');m.load()}catch(e){}});b.innerHTML=''}
function openViewer(e){V.list=S.list.filter(function(x){return pv(x)});V.i=V.list.indexOf(e);
  if(!V.open){V.open=true;try{history.pushState({lfsv:1},'')}catch(x){}}
  $('#viewer').hidden=false;document.body.style.overflow='hidden';showV()}
function closeViewer(fromPop){if(!V.open)return;V.open=false;clearV();$('#viewer').hidden=true;document.body.style.overflow='';if(!fromPop){try{history.back()}catch(x){}}}
window.addEventListener('popstate',function(){if(V.open)closeViewer(true)});
function showV(){clearV();var e=V.list[V.i],t=pv(e),u=urlOf(e),b=$('#vBody');
  $('#vName').textContent=e.n;$('#vSub').textContent=fmtSize(e.s)+(V.list.length>1?' · '+(V.i+1)+' of '+V.list.length:'');
  $('#vDl').href=u+'?dl=1';$('#vPrev').hidden=V.i<=0;$('#vNext').hidden=V.i>=V.list.length-1;
  var fail='<div class="vmsg">'+ic('alert')+'<p>This file can\'t be previewed in the browser, but you can download it.</p><a class="btn primary" href="'+esc(u)+'?dl=1">'+ic('download')+'Download</a></div>';
  if(t==='image')b.innerHTML='<img alt="'+esc(e.n)+'" src="'+esc(u)+'">';
  else if(t==='video')b.innerHTML='<video controls autoplay playsinline preload="metadata" src="'+esc(u)+'"></video>';
  else if(t==='audio')b.innerHTML='<div class="aud">'+tile(e,false)+'<audio controls autoplay src="'+esc(u)+'"></audio></div>';
  else if(t==='pdf')b.innerHTML='<iframe src="'+esc(u)+'" title="'+esc(e.n)+'"></iframe>';
  else if(t==='text'){b.innerHTML='<pre>Loading…</pre>';
    fetch(u).then(function(r){return r.ok?r.text():Promise.reject()}).then(function(tx){if(V.list[V.i]===e&&V.open)b.querySelector('pre').textContent=tx})
      .catch(function(){if(V.list[V.i]===e&&V.open)b.innerHTML=fail})}
  var m=b.querySelector('img,video,audio');if(m)m.addEventListener('error',function(){b.innerHTML=fail})}
function navV(d){var j=V.i+d;if(j<0||j>=V.list.length)return;V.i=j;showV()}
$('#vClose').onclick=function(){closeViewer(false)};
$('#vPrev').onclick=function(){navV(-1)};$('#vNext').onclick=function(){navV(1)};
document.addEventListener('keydown',function(ev){if(!V.open)return;
  if(ev.key==='Escape')closeViewer(false);else if(ev.key==='ArrowLeft')navV(-1);else if(ev.key==='ArrowRight')navV(1)});
var sx=0,sy=0;$('#vBody').addEventListener('touchstart',function(ev){sx=ev.touches[0].clientX;sy=ev.touches[0].clientY},{passive:true});
$('#vBody').addEventListener('touchend',function(ev){var t=ev.changedTouches[0],dx=t.clientX-sx,dy=t.clientY-sy;
  if(Math.abs(dx)>60&&Math.abs(dy)<60&&pv(V.list[V.i])==='image')navV(dx<0?1:-1)},{passive:true});

/* ---------- uploads ---------- */
var U={q:[],busy:false,uid:0,speed:0,lastT:0,lastB:0,raf:0};
function applyUploads(){var on=!!S.uploads;$('#upBtn').hidden=!on;$('#mkBtn').hidden=!on;
  $('#dirBtn').hidden=!on||touch||!('webkitdirectory' in document.createElement('input'));
  if(!S.entries.length)render()}
$('#upBtn').onclick=function(){$('#fileIn').click()};
$('#dirBtn').onclick=function(){$('#dirIn').click()};
$('#fileIn').onchange=function(){var l=Array.from(this.files).map(function(f){return{file:f,dir:''}});this.value='';if(l.length)enqueue(l)};
$('#dirIn').onchange=function(){var l=Array.from(this.files).map(function(f){var p=(f.webkitRelativePath||f.name).split('/');p.pop();return{file:f,dir:p.join('/')}});this.value='';if(l.length)enqueue(l)};
function enqueue(list){list.forEach(function(it){U.q.push({id:++U.uid,f:it.file,dir:it.dir||'',st:'wait',ld:0,tries:0,err:''})});
  $('#up').hidden=false;$('#up').classList.remove('min');renderUp();pump()}
function pump(){if(U.busy)return;var u=U.q.find(function(x){return x.st==='wait'});
  if(!u){renderUp();finishBatch();return}U.busy=true;send(u)}
function finishBatch(){var n=0;U.q.forEach(function(x){if(x.st==='done'&&!x.counted){x.counted=true;n++}});
  if(n){toast(n+(n>1?' files':' file')+' uploaded');refreshList()}}
function failU(u,m){if(++u.tries<3){u.st='wait';u.err=m+' — retrying…'}else{u.st='fail';u.err=m}}
function send(u){u.st='up';u.ld=0;U.lastT=performance.now();U.lastB=0;
  var fd=new FormData();fd.append('files',u.f,u.f.name);
  var x=new XMLHttpRequest();u.x=x;
  x.open('POST','/__upload?path='+encodeURIComponent(D.path)+'&dir='+encodeURIComponent(u.dir));
  x.upload.onprogress=function(ev){u.ld=ev.loaded;var now=performance.now(),dt=now-U.lastT;
    if(dt>600){var sp=(ev.loaded-U.lastB)/(dt/1000);U.speed=U.speed?U.speed*.6+sp*.4:sp;U.lastT=now;U.lastB=ev.loaded}tickUp()};
  x.onload=function(){U.busy=false;
    if(x.status>=200&&x.status<300){u.st='done';u.ld=u.f.size;u.err=''}
    else if(x.status===401){location.reload();return}
    else if(x.status===403){u.st='fail';u.err='Uploads are turned off on the PC'}
    else if(x.status===507){u.st='fail';u.err='The PC is out of disk space'}
    else failU(u,'Server error ('+x.status+')');
    renderUp();pump()};
  x.onerror=function(){U.busy=false;failU(u,'Connection lost');renderUp();setTimeout(pump,u.st==='wait'?2500:0)};
  x.onabort=function(){U.busy=false;if(u.st==='up')u.st='cancel';renderUp();pump()};
  x.send(fd)}
function upStats(){var s={tot:0,ld:0,done:0,fail:0,act:0,n:0};U.q.forEach(function(u){if(u.st==='cancel')return;s.n++;s.tot+=u.f.size;
  s.ld+=u.st==='done'?u.f.size:u.ld;if(u.st==='done')s.done++;else if(u.st==='fail')s.fail++;else s.act++});return s}
function fmtEta(sec){if(!isFinite(sec)||sec<=0)return '';if(sec<60)return Math.ceil(sec)+'s left';if(sec<3600)return Math.ceil(sec/60)+' min left';return (sec/3600).toFixed(1)+' h left'}
function upHead(){var s=upStats();
  if(s.act){$('#upTitle').textContent='Uploading '+Math.min(s.done+1,s.n)+' of '+s.n;
    $('#upSub').textContent=fmtSize(s.ld)+' of '+fmtSize(s.tot)+(U.speed?' · '+fmtSize(U.speed)+'/s · '+fmtEta((s.tot-s.ld)/U.speed):'')}
  else{$('#upTitle').textContent=s.fail?(s.done+' uploaded, '+s.fail+' failed'):(s.done+(s.done===1?' file':' files')+' uploaded');
    $('#upSub').textContent=s.fail?'Press retry to try the failed files again':'All done'}
  $('#upBar').style.width=(s.tot?s.ld/s.tot*100:100)+'%'}
function upItem(u){var pct=u.st==='done'?100:(u.f.size?Math.floor(u.ld/u.f.size*100):0);
  var txt={wait:u.err||'Waiting',up:pct+'%',done:'Uploaded',fail:u.err||'Failed',cancel:'Cancelled'}[u.st];
  var b=(u.st==='wait'||u.st==='up')?'<button class="sbtn" data-act="cancel" title="Cancel">'+ic('x')+'</button>':
    u.st==='fail'?'<button class="sbtn" data-act="retry" title="Retry">'+ic('refresh')+'</button>':u.st==='done'?'<span class="ok">'+ic('check')+'</span>':'';
  return '<div class="ui '+u.st+'" data-id="'+u.id+'"><div class="tx"><span class="nm" title="'+esc(u.f.name)+'">'+esc((u.dir?u.dir+'/':'')+u.f.name)+'</span>'+
    '<span class="sb">'+fmtSize(u.f.size)+' · <span class="pc">'+esc(txt)+'</span></span><div class="pb"><i style="width:'+pct+'%"></i></div></div>'+b+'</div>'}
function renderUp(){$('#upList').innerHTML=U.q.map(upItem).join('');upHead()}
function tickUp(){if(U.raf)return;U.raf=requestAnimationFrame(function(){U.raf=0;upHead();
  U.q.forEach(function(u){if(u.st!=='up')return;var el=document.querySelector('.ui[data-id="'+u.id+'"]');if(!el)return;
    var pct=u.f.size?Math.floor(u.ld/u.f.size*100):0;el.querySelector('.pb i').style.width=pct+'%';el.querySelector('.pc').textContent=pct+'%'})})}
$('#upList').addEventListener('click',function(ev){var b=ev.target.closest('[data-act]');if(!b)return;
  var id=+b.closest('.ui').getAttribute('data-id'),u=U.q.find(function(x){return x.id===id});if(!u)return;
  if(b.getAttribute('data-act')==='cancel'){if(u.st==='up'&&u.x)u.x.abort();else{u.st='cancel';renderUp()}}
  else{u.st='wait';u.tries=0;u.err='';renderUp();pump()}});
$('#upMin').onclick=function(){$('#up').classList.toggle('min')};
$('#upClose').onclick=function(){var s=upStats();if(s.act&&!confirm('Cancel the remaining uploads?'))return;
  U.q.forEach(function(u){if(u.st==='wait')u.st='cancel';else if(u.st==='up'&&u.x)u.x.abort()});U.q=[];$('#up').hidden=true};
window.addEventListener('beforeunload',function(ev){if(upStats().act){ev.preventDefault();ev.returnValue=''}});

function filesFromDrop(dt){return new Promise(function(res){
  var its=dt.items;if(!its||!its.length||!its[0].webkitGetAsEntry){res(Array.from(dt.files).map(function(f){return{file:f,dir:''}}));return}
  var out=[],pending=0,entries=Array.from(its).map(function(it){return it.webkitGetAsEntry()}).filter(Boolean);
  function done(){if(--pending===0)res(out)}
  function walk(en,dir){pending++;
    if(en.isFile)en.file(function(f){out.push({file:f,dir:dir});done()},done);
    else if(en.isDirectory){var r=en.createReader(),sub=dir?dir+'/'+en.name:en.name;
      (function batch(){r.readEntries(function(l){if(l.length){l.forEach(function(x){walk(x,sub)});batch()}else done()},done)})()}
    else done()}
  if(!entries.length){res([]);return}entries.forEach(function(en){walk(en,'')})})}
var depth=0;
function hasFiles(ev){var t=ev.dataTransfer&&ev.dataTransfer.types;return !!t&&Array.prototype.indexOf.call(t,'Files')>=0}
document.addEventListener('dragenter',function(ev){if(!hasFiles(ev))return;ev.preventDefault();if(!S.uploads)return;depth++;$('#drop').classList.add('show')});
document.addEventListener('dragover',function(ev){if(!hasFiles(ev))return;ev.preventDefault();ev.dataTransfer.dropEffect=S.uploads?'copy':'none'});
document.addEventListener('dragleave',function(ev){if(!hasFiles(ev))return;if(--depth<=0){depth=0;$('#drop').classList.remove('show')}});
document.addEventListener('drop',function(ev){if(!hasFiles(ev))return;ev.preventDefault();depth=0;$('#drop').classList.remove('show');
  if(!S.uploads){toast('Uploads are turned off on the PC');return}
  filesFromDrop(ev.dataTransfer).then(function(l){if(l.length)enqueue(l)})});

$('#mkBtn').onclick=function(){var n=prompt('Name for the new folder:');if(!n||!n.trim())return;
  fetch('/__mkdir?path='+encodeURIComponent(D.path)+'&name='+encodeURIComponent(n.trim()),{method:'POST'}).then(function(r){
    if(r.ok){toast('Folder created');refreshList()}else if(r.status===409)toast('A folder with that name already exists');
    else if(r.status===403)toast('Uploads are turned off on the PC');else toast('Could not create the folder')}).catch(function(){toast('Connection problem — try again')})};

/* ---------- live connection ---------- */
function refreshList(){return fetch('/__api/list?path='+encodeURIComponent(D.path),{cache:'no-store'}).then(function(r){
    if(r.status===401){location.reload();return}if(r.status===404){location.href='/';return}return r.json()}).then(function(j){if(!j)return;
    S.entries=j.entries;if(j.uploads!==S.uploads){S.uploads=j.uploads;applyUploads()}
    var names=new Set(S.entries.map(function(e){return e.n}));S.sel.forEach(function(n){if(!names.has(n))S.sel.delete(n)});render()}).catch(function(){})}
var fails=0;
function ping(){var ctl=window.AbortController?new AbortController():null,t=setTimeout(function(){if(ctl)ctl.abort()},6000);
  fetch('/__ping',{cache:'no-store',signal:ctl?ctl.signal:undefined}).then(function(r){clearTimeout(t);if(r.status===401){location.reload();return}return r.json()})
  .then(function(j){if(!j)return;if(fails>=2){$('#offline').hidden=true;toast('Reconnected');refreshList()}fails=0;
    if(j.uploads!==S.uploads){S.uploads=j.uploads;applyUploads()}})
  .catch(function(){clearTimeout(t);if(++fails>=2)$('#offline').hidden=false})}
setInterval(ping,8000);
document.addEventListener('visibilitychange',function(){if(!document.hidden){ping();refreshList()}});
window.addEventListener('online',ping);

var tT=null;function toast(m,ms){var t=$('#toast');t.textContent=m;t.classList.add('show');clearTimeout(tT);tT=setTimeout(function(){t.classList.remove('show')},ms||3200)}

/* ---------- init ---------- */
document.title=(D.path?D.name+' — ':'')+D.root+' · LAN File Share';
$('#title').textContent=D.name;$('#hostLbl').textContent='Shared from '+D.host;
$('#dropTo').textContent='Files will be saved to “'+D.name+'” on '+D.host;
$('#foot').textContent='Shared from '+D.host+' with LAN File Share';
renderCrumbs();applyUploads();setView(S.view==='grid'?'grid':'list');
})();
</script>
</body>
</html>
"""

LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enter PIN · LAN File Share</title>
{{THEME_BOOT}}
<style>
{{BASE_CSS}}
.login{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:16px}
.card{width:100%;max-width:370px;background:var(--surface);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow);padding:30px 26px;text-align:center}
.card .logo{margin:0 auto;width:52px;height:52px;border-radius:15px}
.card .logo .i{width:26px;height:26px}
h1{font-size:21px;margin:16px 0 6px}
p{color:var(--muted);margin:0 0 20px}
.pin{width:100%;height:56px;text-align:center;font-size:26px;letter-spacing:.45em;padding-left:.45em;border-radius:12px;border:1px solid var(--border);background:var(--surface-2);outline:none;font-variant-numeric:tabular-nums}
.pin:focus{border-color:var(--accent);background:var(--surface);box-shadow:0 0 0 3px var(--ring)}
.err{color:var(--danger);font-size:13px;margin-top:10px;min-height:19px}
.card .btn{width:100%;height:48px;margin-top:8px}
</style>
</head>
<body>
{{ICONS}}
<div class="login">
  <form class="card" method="post" action="/__login">
    <div class="logo"><svg class="i"><use href="#i-lock"/></svg></div>
    <h1>Enter PIN</h1>
    <p>You'll find the 6-digit PIN in LAN File Share on <b>{{HOST}}</b>.</p>
    <input class="pin" name="pin" inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code" maxlength="6" autofocus required aria-label="PIN">
    <input type="hidden" name="next" value="{{NEXT}}">
    <div class="err" role="alert">{{ERROR}}</div>
    <button class="btn primary" type="submit">Open shared files</button>
  </form>
  {{CREDIT}}
</div>
<script>var i=document.querySelector('.pin');i.addEventListener('input',function(){i.value=i.value.replace(/\D/g,'').slice(0,6);if(i.value.length===6)i.form.submit()});</script>
</body>
</html>
"""

ERROR_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(code)d %(message)s</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;
font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#f6f7fb;color:#151a26}
@media (prefers-color-scheme:dark){body{background:#0f1117;color:#e8eaf0}}div{text-align:center;max-width:420px}
h1{font-size:48px;margin:0;color:#5850ec}h2{margin:6px 0}p{color:#8a93a6}a{display:inline-block;margin-top:10px;background:#5850ec;color:#fff;
padding:11px 18px;border-radius:10px;text-decoration:none;font-weight:600}</style></head><body><div><h1>%(code)d</h1><h2>%(message)s</h2>
<p>%(explain)s</p><a href="/">Back to shared files</a></div></body></html>"""


def fill_template(template, **values):
    out = template.replace("{{PIXEL_LOGO}}", PIXEL_LOGO_URI).replace("{{BASE_CSS}}", BASE_CSS).replace("{{THEME_BOOT}}", THEME_BOOT).replace("{{ICONS}}", ICON_SPRITE).replace("{{CREDIT}}", CREDIT_HTML)
    # DATA goes last so text inside file names can never be treated as a placeholder
    for key in sorted(values, key=lambda k: k == "DATA"):
        out = out.replace("{{" + key + "}}", values[key])
    return out


# =========================================================================
#  Updates (GitHub releases)
# =========================================================================

def version_tuple(text):
    """'v5.1' -> (5, 1, 0, 0) so versions compare as numbers, not strings."""
    nums = [int(n) for n in re.findall(r"\d+", text)[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def fetch_latest_release(timeout=10):
    """Return {'version', 'setup_url', 'page_url'} for the newest published release.
    Only the version number is requested; nothing about this PC is sent."""
    req = urllib.request.Request(UPDATE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"LAN-File-Share/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    setup_url = next((a.get("browser_download_url") for a in data.get("assets", [])
                      if a.get("name") == SETUP_ASSET), None)
    return {"version": str(data.get("tag_name", "")).lstrip("vV"), "setup_url": setup_url,
            "page_url": data.get("html_url") or "https://github.com/Rajasekhar-Ilaparti/LAN-File-Share/releases/latest"}


def download_file(url, dest, timeout=30):
    """Download url to dest via a temporary .part file, so a broken download never looks finished."""
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": f"LAN-File-Share/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as f:
        expected = int(resp.headers.get("Content-Length") or 0)
        shutil.copyfileobj(resp, f, IO_CHUNK)
    if expected and os.path.getsize(part) != expected:
        os.remove(part)
        raise OSError("the download was incomplete")
    os.replace(part, dest)


# =========================================================================
#  Shared state between the GUI thread and the HTTP server threads
# =========================================================================

class ServerState:
    DEVICE_TTL = 30  # seconds since last request for a device to count as connected

    def __init__(self, directory, uploads_enabled, pin, secret, log, on_received=None):
        self.directory = directory
        self.uploads_enabled = uploads_enabled
        self.secret = secret
        self.log = log or (lambda msg, level="info": None)
        self.on_received = on_received
        self.host = socket.gethostname()
        self.root_name = os.path.basename(os.path.normpath(directory)) or directory
        self.lock = threading.Lock()
        self.devices = {}  # ip -> [name, last_seen]
        self.failed_logins = {}  # ip -> [count, locked_until]
        self.bytes_sent = self.bytes_received = 0
        self.files_sent = self.files_received = 0
        self.set_pin(pin)

    def set_pin(self, pin):
        self.pin = pin or ""
        # Derived from the PIN so a phone stays signed in across app restarts,
        # and is signed out automatically when the PIN changes.
        self.token = hmac.new(self.secret.encode(), self.pin.encode(), hashlib.sha256).hexdigest()

    def seen(self, ip, ua):
        now = time.time()
        with self.lock:
            dev = self.devices.get(ip)
            is_new = dev is None or now - dev[1] > 300
            name = device_name(ua) if (dev is None or ua) else dev[0]
            self.devices[ip] = [name, now]
        if is_new:
            self.log(f"{name} connected ({ip})", "success")

    def report_received(self, path, who, message):
        if self.on_received:
            self.on_received(path, who, message)
        else:
            self.log(message, "success")

    def name_of(self, ip):
        with self.lock:
            dev = self.devices.get(ip)
        return dev[0] if dev else ip

    def add(self, **counters):
        with self.lock:
            for key, value in counters.items():
                setattr(self, key, getattr(self, key) + value)

    def check_pin(self, ip, value):
        now = time.time()
        with self.lock:
            count, until = self.failed_logins.get(ip, [0, 0])
            if now < until:
                return "locked"
            if self.pin and hmac.compare_digest(value.encode(), self.pin.encode()):
                self.failed_logins.pop(ip, None)
                return "ok"
            count += 1
            locked = count >= 5
            self.failed_logins[ip] = [0, now + 60] if locked else [count, 0]
        if locked:
            self.log(f"Blocked {ip} for 1 minute after 5 wrong PINs", "warn")
            return "locked"
        return "bad"

    def snapshot(self):
        now = time.time()
        with self.lock:
            active = sorted((name, ip) for ip, (name, t) in self.devices.items() if now - t < self.DEVICE_TTL)
            return {
                "devices": active,
                "bytes_sent": self.bytes_sent, "bytes_received": self.bytes_received,
                "files_sent": self.files_sent, "files_received": self.files_received,
            }


# =========================================================================
#  HTTP handler
# =========================================================================

class ShareHandler(http.server.BaseHTTPRequestHandler):
    """Serves the shared folder as a single-page file browser plus a small
    JSON API, with Range support (video seeking, resumable downloads),
    optional PIN protection, ZIP downloads, uploads and thumbnails."""

    server_version = "LANFileShare/" + APP_VERSION
    timeout = 120  # drop stalled connections instead of tying up a thread forever
    error_message_format = ERROR_PAGE
    error_content_type = "text/html; charset=utf-8"
    _head = False

    def __init__(self, *args, state: ServerState = None, **kwargs):
        self.state = state
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        pass  # meaningful events are logged explicitly; raw access logs are noise

    @property
    def ip(self):
        return self.client_address[0]

    # ---------------- small response helpers ----------------

    def send_bytes(self, body, ctype="text/html; charset=utf-8", status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not self._head:
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status, {"Cache-Control": "no-store"})

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def auth_cookie(self):
        return f"lfs_auth={self.state.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"

    def authorized(self):
        if not self.state.pin:
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = cookie.get("lfs_auth")
        return bool(morsel) and hmac.compare_digest(morsel.value.encode(), self.state.token.encode())

    def resolve(self, rel):
        """Map a decoded URL path to a real path, or None if it's hidden or outside the share."""
        parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." or is_hidden_name(p) for p in parts):
            return None
        try:
            return safe_join(self.state.directory, "/".join(parts))
        except ValueError:
            return None

    # ---------------- routing ----------------

    def do_HEAD(self):
        self._head = True
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        route = url.path

        if route == "/favicon.ico":
            return self.send_bytes(b"", "image/x-icon", 204)
        if route == "/__login":
            if not self.state.pin:
                return self.redirect("/")
            return self.send_login(qs.get("next", ["/"])[0])

        if "pin" in qs and self.state.pin:
            # QR codes carry the PIN so scanning signs the phone straight in
            if self.state.check_pin(self.ip, qs["pin"][0]) == "ok":
                return self.redirect(urllib.parse.quote(url.path), self.auth_cookie())
            return self.redirect("/__login?e=1&next=" + urllib.parse.quote(url.path))

        if not self.authorized():
            if route.startswith("/__"):
                return self.send_json({"error": "PIN required"}, 401)
            return self.redirect("/__login?next=" + urllib.parse.quote(url.path))

        self.state.seen(self.ip, self.headers.get("User-Agent", ""))

        if route == "/__ping":
            return self.send_json({"ok": True, "uploads": self.state.uploads_enabled})
        if route == "/__api/list":
            return self.handle_api_list(qs)
        if route == "/__zip":
            return self.handle_zip_download(qs)
        if route == "/__thumb":
            return self.handle_thumb(qs)
        if route.startswith("/__"):
            return self.send_error(404, "Not found")

        rel = urllib.parse.unquote(route)
        fs = self.resolve(rel)
        if fs and os.path.isdir(fs):
            if not route.endswith("/"):
                return self.redirect(route + "/")
            return self.send_listing_page(rel.strip("/"), fs)
        if fs and os.path.isfile(fs):
            return self.serve_file(fs, "dl" in qs)
        self.send_error(404, "File not found", "It may have been moved, renamed or deleted on the PC.")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/__login":
            return self.handle_login_post()
        if not self.authorized():
            self.close_connection = True
            return self.send_json({"error": "PIN required"}, 401)
        self.state.seen(self.ip, self.headers.get("User-Agent", ""))
        if url.path == "/__upload":
            return self.handle_upload(qs)
        if url.path == "/__mkdir":
            return self.handle_mkdir(qs)
        self.send_error(404, "Not found")

    # ---------------- PIN login ----------------

    def send_login(self, next_path, error=""):
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        if not error and "e=1" in (urllib.parse.urlsplit(self.path).query or ""):
            error = "That PIN didn't work. Try again."
        page = fill_template(LOGIN_TEMPLATE, HOST=html.escape(self.state.host), NEXT=html.escape(next_path),
                             ERROR=html.escape(error))
        self.send_bytes(page.encode("utf-8"), headers={"Cache-Control": "no-store"})

    def handle_login_post(self):
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 4096)
        except ValueError:
            length = 0
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        pin = form.get("pin", [""])[0].strip()
        next_path = form.get("next", ["/"])[0]
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        result = self.state.check_pin(self.ip, pin)
        if result == "ok":
            return self.redirect(next_path, self.auth_cookie())
        message = "Too many wrong tries. Wait a minute and try again." if result == "locked" else "That PIN didn't work. Try again."
        self.send_login(next_path, message)

    # ---------------- listing ----------------

    def list_entries(self, dir_path):
        out = []
        with os.scandir(dir_path) as it:
            for entry in it:
                if is_hidden_entry(entry):
                    continue
                try:
                    is_dir = entry.is_dir()
                    st = entry.stat()
                except OSError:
                    continue
                out.append({
                    "n": entry.name, "d": is_dir,
                    "s": None if is_dir else st.st_size,
                    "m": int(st.st_mtime),
                    "k": "folder" if is_dir else kind_of(entry.name),
                })
        return out

    def send_listing_page(self, rel, fs):
        try:
            entries = self.list_entries(fs)
        except OSError:
            return self.send_error(403, "Can't open this folder", "Windows didn't allow access to it.")
        name = rel.split("/")[-1] if rel else self.state.root_name
        data = {
            "path": rel, "name": name, "root": self.state.root_name, "host": self.state.host,
            "entries": entries, "uploads": self.state.uploads_enabled,
        }
        page = fill_template(
            PAGE_TEMPLATE,
            TITLE=html.escape(name) + " · LAN File Share",
            DATA=json.dumps(data).replace("</", "<\\/"),
        )
        self.send_bytes(page.encode("utf-8"), headers={"Cache-Control": "no-store"})

    def handle_api_list(self, qs):
        fs = self.resolve(qs.get("path", [""])[0])
        if not fs or not os.path.isdir(fs):
            return self.send_json({"error": "not found"}, 404)
        try:
            entries = self.list_entries(fs)
        except OSError:
            return self.send_json({"error": "access denied"}, 403)
        self.send_json({"entries": entries, "uploads": self.state.uploads_enabled})

    # ---------------- files (with Range support) ----------------

    def serve_file(self, fs, download):
        try:
            st = os.stat(fs)
        except OSError:
            return self.send_error(404, "File not found")
        size = st.st_size
        name = os.path.basename(fs)
        ext = os.path.splitext(name)[1].lower()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"

        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and size > 0:
            m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", content_disposition("attachment" if download else "inline", name))
        if ext in ACTIVE_CONTENT_EXTS:
            self.send_header("Content-Security-Policy", "sandbox")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self._head or not length:
            return

        sent = 0
        try:
            with open(fs, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(IO_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, socket.timeout):
            if download:
                self.state.log(f"Download of '{name}' to {self.state.name_of(self.ip)} was interrupted", "warn")
            self.close_connection = True
            return
        finally:
            self.state.add(bytes_sent=sent)
        if download and start == 0 and sent == size:
            self.state.add(files_sent=1)
            self.state.log(f"↓ {self.state.name_of(self.ip)} downloaded '{name}' ({human_size(size)})")

    def handle_thumb(self, qs):
        fs = self.resolve(qs.get("path", [""])[0])
        if not fs or not os.path.isfile(fs) or kind_of(fs) != "image":
            return self.send_error(404, "Not found")
        if not PIL_AVAILABLE or fs.lower().endswith(".svg"):
            return self.serve_file(fs, False)
        try:
            data = make_thumbnail(fs, os.path.getmtime(fs))
        except Exception:
            return self.send_error(404, "No thumbnail")
        self.send_bytes(data, "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ---------------- ZIP download ----------------
    # Built into a temporary file on disk (not RAM, so huge video folders are
    # fine) and then sent with a real Content-Length header. A response with a
    # known length is much less likely to be silently dropped by browsers,
    # antivirus/proxy software, or flaky Wi-Fi than an unbounded streamed one.
    # Already-compressed media is stored as-is, which makes building fast.
    def handle_zip_download(self, qs):
        rel = qs.get("path", [""])[0].strip("/")
        target_dir = self.resolve(rel)
        if not target_dir or not os.path.isdir(target_dir):
            return self.send_error(404, "Folder not found")
        folder_label = os.path.basename(target_dir.rstrip(os.sep)) or self.state.root_name

        names = qs.get("items", [])
        if names:
            sources = []
            for item in names:
                path = self.resolve((rel + "/" if rel else "") + os.path.basename(item))
                if path and os.path.exists(path):
                    sources.append(path)
            if not sources:
                return self.send_error(404, "Nothing to download")
            label = os.path.basename(sources[0]) if len(sources) == 1 else f"{folder_label} ({len(sources)} items)"
        else:
            sources = [target_dir]
            label = folder_label

        if self._head:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.end_headers()
            return

        try:
            with tempfile.TemporaryFile() as tmp:
                file_count = 0
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for src in sources:
                        parent = os.path.dirname(src.rstrip(os.sep)) or src
                        if os.path.isfile(src):
                            walk = [(os.path.dirname(src), [], [os.path.basename(src)])]
                        else:
                            walk = os.walk(src)
                        for root, dirs, files in walk:
                            dirs[:] = [d for d in dirs if not is_hidden_name(d)]
                            files = [f for f in files if not is_hidden_name(f)]
                            if not dirs and not files and root != parent:
                                zf.writestr(os.path.relpath(root, parent).replace(os.sep, "/") + "/", b"")
                            for fname in files:
                                full_path = os.path.join(root, fname)
                                arcname = os.path.relpath(full_path, parent)
                                ext = os.path.splitext(fname)[1].lower()
                                compress = zipfile.ZIP_STORED if ext in STORED_EXTS else zipfile.ZIP_DEFLATED
                                try:
                                    zf.write(full_path, arcname, compress_type=compress)
                                    file_count += 1
                                except Exception as e:
                                    self.state.log(f"ZIP: skipped '{fname}' ({e})", "warn")
                size = tmp.tell()
                tmp.seek(0)

                self.state.log(f"↓ {self.state.name_of(self.ip)} is downloading '{label}.zip' "
                               f"({file_count} file(s), {human_size(size)})")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", content_disposition("attachment", label + ".zip"))
                self.send_header("Content-Length", str(size))
                self.end_headers()
                shutil.copyfileobj(tmp, self.wfile, IO_CHUNK)
                self.state.add(bytes_sent=size, files_sent=file_count)
        except (ConnectionError, socket.timeout):
            self.state.log(f"ZIP download '{label}.zip' was interrupted", "warn")
            self.close_connection = True
        except Exception as e:
            self.state.log(f"ZIP download failed: {e}", "error")
            try:
                self.send_error(500, "Failed to build ZIP")
            except Exception:
                pass

    # ---------------- uploads ----------------

    def upload_target(self, qs):
        if not self.state.uploads_enabled:
            self.send_json({"error": "Uploads are turned off on the PC"}, 403)
            return None
        target_dir = self.resolve(qs.get("path", [""])[0])
        if not target_dir or not os.path.isdir(target_dir):
            self.send_json({"error": "Folder not found"}, 404)
            return None
        return target_dir

    def handle_upload(self, qs):
        target_dir = self.upload_target(qs)
        if not target_dir:
            self.close_connection = True
            return

        # Folder uploads: recreate the sub-folder structure the browser sent
        sub = [sanitize_filename(p) for p in qs.get("dir", [""])[0].replace("\\", "/").split("/")]
        sub = [p for p in sub if p]
        if sub:
            try:
                target_dir = safe_join(target_dir, "/".join(sub))
                os.makedirs(target_dir, exist_ok=True)
            except (ValueError, OSError):
                self.close_connection = True
                return self.send_json({"error": "Can't create that folder"}, 400)

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self.close_connection = True
            return self.send_json({"error": "Expected multipart/form-data"}, 400)
        boundary = content_type.split("boundary=")[1].split(";")[0].strip().strip('"').encode()

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self.send_json({"error": "Content-Length required"}, 411)

        who = self.state.name_of(self.ip)
        try:
            saved = save_multipart_files(self.rfile, length, boundary, target_dir)
        except (ConnectionError, socket.timeout):
            self.state.log(f"Upload from {who} was interrupted — partial file discarded", "warn")
            self.close_connection = True
            return
        except OSError as e:
            self.close_connection = True
            if e.errno == errno.ENOSPC:
                self.state.log("Upload failed: this PC is out of disk space", "error")
                return self.send_json({"error": "Disk full"}, 507)
            self.state.log(f"Upload failed: {e}", "error")
            return self.send_json({"error": "Upload failed"}, 500)

        rel_dir = os.path.relpath(target_dir, self.state.directory).replace(os.sep, "/")
        where = "the shared folder" if rel_dir == "." else f"/{rel_dir}"
        for name in saved:
            try:
                size = os.path.getsize(os.path.join(target_dir, name))
            except OSError:
                size = None
            self.state.report_received(os.path.join(target_dir, name), who,
                                       f"↑ {who} uploaded '{name}' ({human_size(size)}) to {where}")
        self.state.add(bytes_received=length, files_received=len(saved))
        self.send_json({"saved": saved})

    def handle_mkdir(self, qs):
        target_dir = self.upload_target(qs)
        if not target_dir:
            return
        name = sanitize_filename(qs.get("name", [""])[0])
        if not name:
            return self.send_json({"error": "Invalid name"}, 400)
        try:
            os.mkdir(os.path.join(target_dir, name))
        except FileExistsError:
            return self.send_json({"error": "Already exists"}, 409)
        except OSError as e:
            return self.send_json({"error": str(e)}, 500)
        self.state.log(f"{self.state.name_of(self.ip)} created folder '{name}'", "success")
        self.send_json({"ok": True})


class ShareServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two programs bind the same port and split
    # its traffic unpredictably — use exclusive binding there instead.
    allow_reuse_address = not IS_WINDOWS
    request_queue_size = 64

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, lambda *a, **k: ShareHandler(*a, state=state, **k))

    def server_bind(self):
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind's getfqdn(), a reverse-DNS lookup that
        # can stall for many seconds on some networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name = socket.gethostname()
        self.server_port = self.server_address[1]

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            return  # phones drop connections all the time; not worth reporting
        self.state.log(f"Error while serving {client_address[0]}: {exc!r}", "error")


# =========================================================================
#  Network + Windows Firewall helpers
# =========================================================================

def run_powershell(script, timeout=20):
    """Run a PowerShell script hidden; return its stdout ('' on any failure)."""
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
        )
        return res.stdout.decode("utf-8", "replace") if res.returncode == 0 else ""
    except Exception:
        return ""


def ps_quote(s):
    return s.replace("'", "''")


def default_route_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


VIRTUAL_ADAPTER_HINTS = ("vethernet", "virtualbox", "vmware", "wsl", "hyper-v", "loopback", "bluetooth", "docker", "tailscale", "zerotier")

ADAPTERS_PS = r"""
$p=@{}
Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { $p[[int]$_.InterfaceIndex]=@{c="$($_.NetworkCategory)";n="$($_.Name)"} }
$r=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object {
  $x=$p[[int]$_.InterfaceIndex]
  [pscustomobject]@{ip=$_.IPAddress; alias=$_.InterfaceAlias; category=$(if($x){$x.c}else{''}); network=$(if($x){$x.n}else{''})}
})
ConvertTo-Json -InputObject $r -Compress
"""


def list_adapters():
    """Every usable IPv4 address on this PC, best candidate first, each as
    {"ip", "label", "category"}. Virtual adapters (WSL, Hyper-V, VPNs...)
    sort last because phones can't reach them."""
    primary = default_route_ip()
    found = []
    if IS_WINDOWS:
        try:
            for a in json.loads(run_powershell(ADAPTERS_PS) or "[]"):
                found.append({"ip": a.get("ip", ""), "alias": a.get("alias") or "",
                              "network": a.get("network") or "", "category": a.get("category") or ""})
        except Exception:
            found = []
    if not found:
        try:
            ips = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except Exception:
            ips = set()
        if primary:
            ips.add(primary)
        found = [{"ip": ip, "alias": "", "network": "", "category": ""}
                 for ip in ips if not ip.startswith(("127.", "169.254."))]

    def rank(a):
        virtual = any(h in a["alias"].lower() for h in VIRTUAL_ADAPTER_HINTS)
        return (a["ip"] != primary, virtual, a["ip"])

    out = []
    for a in sorted(found, key=rank):
        alias = a["alias"]
        if a["ip"].startswith("192.168.137."):
            alias = "Mobile hotspot"
        label = a["ip"]
        detail = " · ".join(x for x in (alias, a["network"] if a["network"] != alias else "") if x)
        if detail:
            label += "   " + detail
        out.append({"ip": a["ip"], "label": label, "category": a["category"],
                    "alias": alias, "network": a["network"]})
    return out


FIREWALL_CHECK_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$fw=@(Get-NetFirewallProfile | Where-Object { $_.Enabled -eq 'True' }).Count
$rules=@(Get-NetFirewallRule -DisplayName '__RULE__' | Where-Object { $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' })
$ports=@($rules | Get-NetFirewallPortFilter | ForEach-Object { "$($_.LocalPort)" })
$app=@(Get-NetFirewallApplicationFilter -Program '__PROG__' | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' })
$block=@($app | Where-Object { $_.Action -eq 'Block' }).Count
$allow=@($app | Where-Object { $_.Action -eq 'Allow' }).Count
ConvertTo-Json -Compress -InputObject @{fw=$fw; ports=$ports; block=$block; allow=$allow}
"""

FIREWALL_FIX_PS = r"""
$ErrorActionPreference='SilentlyContinue'
Get-NetFirewallApplicationFilter -Program '__PROG__' | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule
Remove-NetFirewallRule -DisplayName '__RULE__'
New-NetFirewallRule -DisplayName '__RULE__' -Description 'Lets phones and PCs on your local network open LAN File Share.' -Direction Inbound -Action Allow -Protocol TCP -LocalPort __PORT__ -RemoteAddress LocalSubnet -Profile Any | Out-Null
"""


def firewall_status(port, network_category=""):
    """'ok', 'missing' (no rule for this port), 'blocked' (a block rule for
    this program wins over any allow rule) or 'unknown'."""
    script = FIREWALL_CHECK_PS.replace("__RULE__", ps_quote(FIREWALL_RULE)).replace("__PROG__", ps_quote(sys.executable))
    try:
        info = json.loads(run_powershell(script, timeout=30))
    except Exception:
        return "unknown"
    if not info.get("fw"):
        return "ok"  # firewall is switched off
    if info.get("block"):
        return "blocked"
    ports = [p.lower() for p in (info.get("ports") or [])]
    if str(port) in ports or "any" in ports:
        return "ok"
    if info.get("allow") and network_category.lower() != "public":
        return "ok"  # allowed via Windows' own "allow this app" prompt
    return "missing"


def fix_firewall(port):
    """Add an allow rule for this port (local subnet only) and remove block
    rules for this program. Shows a Windows admin (UAC) prompt; returns
    False if the user declined it."""
    inner = (FIREWALL_FIX_PS.replace("__PROG__", ps_quote(sys.executable))
             .replace("__RULE__", ps_quote(FIREWALL_RULE)).replace("__PORT__", str(int(port))))
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode()
    outer = ("try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
             f"'-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}' -ErrorAction Stop; 'ok' }} catch {{ 'cancelled' }}")
    return run_powershell(outer, timeout=180).strip() == "ok"


def open_in_explorer(path):
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def enable_windows_dpi_awareness():
    """Without this, Windows bitmap-scales the whole Tk window on high-DPI
    displays, which is what makes text and shapes look blurry/soft."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def windows_prefers_dark():
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def set_dark_titlebar(root, dark):
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / older Win10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4) == 0:
                break
    except Exception:
        pass


# =========================================================================
#  Pixel-art logo — one 16x16 drawing used for the .exe icon, the window
#  icon, the in-app header and the web page's favicon/logo
# =========================================================================

PIXEL_COLORS = {
    "K": "#1c1852",  # outline
    "B": "#5850ec",  # body
    "L": "#7f79ff",  # top/left highlight
    "D": "#3f37c9",  # bottom/right shade
    "W": "#ffffff",  # node face
    "S": "#c8c6ff",  # node shade
    "l": "#a9a5ff",  # link lines
}


def _pixel_line(x0, y0, x1, y1):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        yield x0, y0
        if (x0, y0) == (x1, y1):
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def pixel_logo_grid():
    """A chunky "share" glyph (three linked nodes) on a bevelled tile,
    in the style of an old 16-bit game icon."""
    n = 16
    g = [["B"] * n for _ in range(n)]
    for y in range(n):
        for x in range(n):
            if (x in (0, n - 1) and y in (0, 1, n - 2, n - 1)) or (y in (0, n - 1) and x in (0, 1, n - 2, n - 1)):
                g[y][x] = "."
            elif x in (0, n - 1) or y in (0, n - 1) or (x, y) in {(1, 1), (n - 2, 1), (1, n - 2), (n - 2, n - 2)}:
                g[y][x] = "K"
            elif x == 1 or y == 1:
                g[y][x] = "L"
            elif x == n - 2 or y == n - 2:
                g[y][x] = "D"
    hub, top, bottom = (4, 8), (11, 4), (11, 12)
    for end in (top, bottom):
        for x, y in _pixel_line(*hub, *end):
            if g[y][x] == "B":
                g[y][x] = "l"
    for cx, cy in (hub, top, bottom):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                g[cy + dy][cx + dx] = "W" if dx < 1 and dy < 1 else "S"
    return ["".join(row) for row in g]


def pixel_logo_svg():
    rects = []
    for y, row in enumerate(pixel_logo_grid()):
        x = 0
        while x < len(row):
            ch = row[x]
            run = 1
            while x + run < len(row) and row[x + run] == ch:
                run += 1
            if ch != ".":
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{PIXEL_COLORS[ch]}"/>')
            x += run
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" shape-rendering="crispEdges">'
            + "".join(rects) + "</svg>")


PIXEL_LOGO_URI = "data:image/svg+xml," + urllib.parse.quote(pixel_logo_svg())


def pixel_logo_image(scale):
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(pixel_logo_grid()):
        for x, ch in enumerate(row):
            if ch != ".":
                c = PIXEL_COLORS[ch]
                px[x, y] = (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255)
    return img.resize((16 * scale, 16 * scale), Image.NEAREST)


def write_icon(path):
    """Write the pixel logo as a multi-size .ico (whole-pixel scales only,
    so every size stays perfectly crisp)."""
    images = [pixel_logo_image(s) for s in (1, 2, 3, 4, 8, 16)]
    images[-1].save(path, format="ICO", sizes=[im.size for im in images], append_images=images[:-1])


# =========================================================================
#  Desktop GUI — every rounded shape is rendered with Pillow at 4x and
#  downsampled, because Tk's own canvas shapes have jagged edges
# =========================================================================

LIGHT = {
    "bg": "#f3f4f9", "card": "#ffffff", "surface2": "#f1f2f8", "hover": "#e6e8f2", "border": "#e3e5ef",
    "text": "#141824", "muted": "#6a7186", "accent": "#5850ec", "accent_hover": "#4a42d9", "accent_soft": "#efeefe",
    "on_accent": "#ffffff", "success": "#0f9f55", "success_soft": "#e5f6ed", "warn": "#c27803", "danger": "#e0364d",
    "danger_hover": "#c9283f", "input": "#ffffff", "switch_off": "#d3d6e2", "folder": "#f29d16",
    "folder_soft": "#fff3df", "link": "#5850ec",
}
DARK = {
    "bg": "#0e1016", "card": "#171a23", "surface2": "#20232e", "hover": "#292d3a", "border": "#262a36",
    "text": "#eceef4", "muted": "#959db0", "accent": "#7d79ff", "accent_hover": "#8e8bff", "accent_soft": "#25264a",
    "on_accent": "#ffffff", "success": "#3ddc84", "success_soft": "#16301f", "warn": "#f5b83d", "danger": "#ff5d73",
    "danger_hover": "#ff7487", "input": "#12141b", "switch_off": "#363a49", "folder": "#f5a524",
    "folder_soft": "#33281a", "link": "#a29fff",
}

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"


def mix(a, b, t):
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def elide_middle(text, font, width):
    """Shorten long paths to 'C:\\Users\\…\\Holiday 2026' so they fit on one line."""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid // 2] + "…" + text[len(text) - (mid - mid // 2):]
        if font.measure(candidate) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo // 2] + "…" + text[len(text) - (lo - lo // 2):] if lo else "…"


def reveal_in_explorer(path):
    """Open Explorer with the file selected."""
    try:
        if IS_WINDOWS and os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            open_in_explorer(os.path.dirname(path))
    except Exception:
        pass


class Painter:
    """Renders anti-aliased shapes and caches them per size/colour."""
    S = 4  # supersampling factor

    def __init__(self):
        self.cache = {}

    def _get(self, key, make):
        img = self.cache.get(key)
        if img is None:
            img = self.cache[key] = make()
        return img

    def _done(self, im, w, h):
        return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))

    def rounded(self, w, h, r, fill, bg, outline=None):
        def make():
            S = self.S
            im = Image.new("RGB", (w * S, h * S), bg)
            ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=min(r, w / 2, h / 2) * S,
                                                 fill=fill, outline=outline, width=S if outline else 0)
            return self._done(im, w, h)
        return self._get(("rounded", w, h, r, fill, bg, outline), make)

    def corners(self, r, fill, bg, outline=None):
        def make():
            S, n = self.S, 2 * r
            im = Image.new("RGB", (n * S, n * S), bg)
            ImageDraw.Draw(im).ellipse((0, 0, n * S - 1, n * S - 1), fill=fill, outline=outline,
                                       width=S if outline else 0)
            im = im.resize((n, n), Image.LANCZOS)
            return [ImageTk.PhotoImage(im.crop(box)) for box in
                    ((0, 0, r, r), (r, 0, n, r), (0, r, r, n), (r, r, n, n))]
        return self._get(("corners", r, fill, bg, outline), make)

    def circle(self, d, fill, bg):
        def make():
            S = self.S
            im = Image.new("RGB", (d * S, d * S), bg)
            ImageDraw.Draw(im).ellipse((0, 0, d * S - 1, d * S - 1), fill=fill)
            return self._done(im, d, d)
        return self._get(("circle", d, fill, bg), make)

    def switch(self, w, h, t, off, on, bg):
        t = round(t * 10) / 10

        def make():
            S = self.S
            im = Image.new("RGB", (w * S, h * S), bg)
            d = ImageDraw.Draw(im)
            track = mix(off, on, t)
            d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S / 2, fill=track)
            pad = max(2, round(h * 0.13)) * S
            k = h * S - 2 * pad
            x = pad + (w * S - 2 * pad - k) * t
            d.ellipse((x, pad + S * 0.7, x + k, pad + k + S * 0.7), fill=mix(track, "#000000", 0.22))
            d.ellipse((x, pad, x + k, pad + k), fill="#ffffff")
            return self._done(im, w, h)
        return self._get(("switch", w, h, t, off, on, bg), make)

    def folder_tile(self, s, soft, color, bg):
        def make():
            S = self.S
            u = s * S
            im = Image.new("RGB", (u, u), bg)
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((0, 0, u - 1, u - 1), radius=u * 0.28, fill=soft)
            d.rounded_rectangle((u * .22, u * .27, u * .50, u * .42), radius=u * .05, fill=mix(color, "#000000", .12))
            d.rounded_rectangle((u * .22, u * .33, u * .78, u * .72), radius=u * .07, fill=mix(color, "#000000", .12))
            d.rounded_rectangle((u * .22, u * .40, u * .78, u * .72), radius=u * .07, fill=color)
            return self._done(im, s, s)
        return self._get(("folder", s, soft, color, bg), make)

    def pixel_logo(self, scale):
        return self._get(("pixel", scale), lambda: ImageTk.PhotoImage(pixel_logo_image(scale)))


class Card(tk.Frame):
    """A rounded panel: a normal frame (so it sizes itself to its content)
    with anti-aliased corner pieces laid over its four square corners.
    Put children in `card.body`."""

    def __init__(self, parent, app, fill=None, outline="border", radius=16, pad=(20, 18)):
        p = app.p
        self.app = app
        self.fill = fill or p["card"]
        self.page_bg = parent.cget("bg")
        self.outline = p[outline] if outline else None
        self.r = app.px(radius)
        edge = self.outline or self.fill
        super().__init__(parent, bg=self.fill, bd=0, highlightthickness=1 if self.outline else 0,
                         highlightbackground=edge, highlightcolor=edge)
        self.body = tk.Frame(self, bg=self.fill)
        self.body.pack(fill="both", expand=True, padx=app.px(pad[0]), pady=app.px(pad[1]))
        self._corners = []
        for relx, rely, anchor in ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se")):
            lbl = tk.Label(self, bd=0, highlightthickness=0, bg=self.page_bg)
            lbl.place(relx=relx, rely=rely, anchor=anchor, bordermode="outside")
            self._corners.append(lbl)
        self._paint()

    def _paint(self):
        for lbl, img in zip(self._corners, self.app.painter.corners(self.r, self.fill, self.page_bg, self.outline)):
            lbl.configure(image=img)

    def set_outline(self, key):
        self.outline = self.app.p[key] if key else None
        edge = self.outline or self.fill
        self.configure(highlightbackground=edge, highlightcolor=edge)
        self._paint()


class RoundButton(tk.Canvas):
    KINDS = {  # (fill, text, hover fill); None fill = blends into whatever it sits on
        "primary": ("accent", "on_accent", "accent_hover"),
        "secondary": ("surface2", "text", "hover"),
        "danger": ("danger", "on_accent", "danger_hover"),
        "ghost": (None, "accent", "accent_soft"),
        "subtle": (None, "muted", "surface2"),
    }

    def __init__(self, parent, app, text, command=None, kind="secondary", size=10, padx=16, pady=8, radius=10):
        self.app, self.command, self.kind, self.text = app, command, kind, text
        self.enabled, self.hover, self.pressed = True, False, False
        self.font = tkfont.Font(family=FONT_SEMI, size=size)
        self.padx, self.r = app.px(padx), app.px(radius)
        h = self.font.metrics("linespace") + 2 * app.px(pady)
        super().__init__(parent, width=self._natural(), height=h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.bind("<Enter>", lambda e: self._state(hover=True))
        self.bind("<Leave>", lambda e: self._state(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._state(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda e: self._invoke())
        self.bind("<space>", lambda e: self._invoke())
        self.bind("<Configure>", lambda e: self.draw())

    def _natural(self):
        return self.font.measure(self.text) + 2 * self.padx

    def _state(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def _release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._state(pressed=False)
        if inside:
            self._invoke()

    def _invoke(self):
        if self.enabled and self.command:
            self.command()

    def set(self, text=None, kind=None, enabled=None):
        if text is not None and text != self.text:
            self.text = text
            self.configure(width=self._natural())
        if kind is not None:
            self.kind = kind
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self.cget("width")), int(self.cget("height"))
        p, page = self.app.p, self.cget("bg")
        fill_key, text_key, hover_key = self.KINDS[self.kind]
        fill = p[fill_key] if fill_key else page
        fg = p[text_key]
        if not self.enabled:
            fill, fg = (p["surface2"] if fill_key else page), p["muted"]
        elif self.pressed:
            fill = mix(p[hover_key], "#000000", 0.10)
        elif self.hover:
            fill = p[hover_key]
        self.delete("all")
        if fill != page:
            self.create_image(0, 0, anchor="nw", image=self.app.painter.rounded(w, h, self.r, fill, page))
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class Dropdown(tk.Canvas):
    """A rounded select box that opens a native menu of options."""

    def __init__(self, parent, app, on_select):
        self.app, self.on_select = app, on_select
        self.options, self.index, self.placeholder = [], -1, ""
        self.enabled, self.hover = True, False
        self.font = tkfont.Font(family=FONT, size=10)
        h = self.font.metrics("linespace") + 2 * app.px(9)
        super().__init__(parent, height=h, width=10, bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.open())
        self.bind("<Configure>", lambda e: self.draw())

    def _hover(self, on):
        self.hover = on
        self.draw()

    def set_options(self, labels, index=0, placeholder=""):
        self.options = list(labels)
        self.index = index if labels else -1
        self.placeholder = placeholder
        self.draw()

    def current(self):
        return self.index

    def open(self):
        if not self.enabled or not self.options:
            return
        menu = tk.Menu(self, tearoff=0)
        for i, label in enumerate(self.options):
            menu.add_command(label=("✓  " if i == self.index else "     ") + label, command=lambda i=i: self._choose(i))
        menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + self.app.px(4))

    def _choose(self, i):
        self.index = i
        self.draw()
        self.on_select(i)

    def draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        p, page, px = self.app.p, self.cget("bg"), self.app.px
        outline = p["accent"] if self.hover and self.enabled else p["border"]
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self.app.painter.rounded(w, h, px(10), p["input"], page, outline))
        text = self.options[self.index] if 0 <= self.index < len(self.options) else self.placeholder
        text = elide_middle(text, self.font, w - px(44))
        self.create_text(px(12), h / 2, text=text, anchor="w", font=self.font,
                         fill=p["text"] if self.options else p["muted"])
        self.create_text(w - px(16), h / 2, text="▾", anchor="center", font=(FONT, 10), fill=p["muted"])


class Field(Card):
    """A rounded text entry."""

    def __init__(self, parent, app, textvariable, chars=8):
        super().__init__(parent, app, fill=app.p["input"], outline="border", radius=10, pad=(12, 8))
        p = app.p
        self.entry = tk.Entry(self.body, textvariable=textvariable, width=chars, relief="flat", bd=0,
                              bg=p["input"], fg=p["text"], insertbackground=p["text"], font=(FONT, 10),
                              highlightthickness=0, disabledbackground=p["input"], disabledforeground=p["muted"],
                              selectbackground=p["accent"], selectforeground="#ffffff")
        self.entry.pack()
        self.entry.bind("<FocusIn>", lambda e: self.set_outline("accent"))
        self.entry.bind("<FocusOut>", lambda e: self.set_outline("border"))


class ToggleSwitch(tk.Frame):
    def __init__(self, parent, app, variable, title, subtitle=""):
        p = app.p
        super().__init__(parent, bg=parent.cget("bg"))
        self.app, self.var, self.enabled = app, variable, True
        self.t = 1.0 if variable.get() else 0.0
        self._anim = None
        self.W, self.H = app.px(42), app.px(24)
        self.cv = tk.Canvas(self, width=self.W, height=self.H, bg=self.cget("bg"), highlightthickness=0, cursor="hand2")
        self.cv.grid(row=0, column=1, rowspan=2, sticky="e", padx=(app.px(14), 0))
        title_lbl = tk.Label(self, text=title, bg=self.cget("bg"), fg=p["text"], font=(FONT_SEMI, 10), cursor="hand2")
        title_lbl.grid(row=0, column=0, sticky="w")
        widgets = [self.cv, title_lbl]
        if subtitle:
            sub = tk.Label(self, text=subtitle, bg=self.cget("bg"), fg=p["muted"], font=(FONT, 9),
                           justify="left", anchor="w", cursor="hand2")
            sub.grid(row=1, column=0, sticky="w")
            widgets.append(sub)
        self.grid_columnconfigure(0, weight=1)
        for w in widgets:
            w.bind("<Button-1>", self.toggle)
        variable.trace_add("write", self._on_var)
        self.draw()

    def toggle(self, _=None):
        if self.enabled:
            self.var.set(not self.var.get())

    def _on_var(self, *_):
        if self._anim is None:
            self._step()

    def _step(self):
        target = 1.0 if self.var.get() else 0.0
        delta = target - self.t
        if abs(delta) < 0.01:
            self.t, self._anim = target, None
            self.draw()
            return
        self.t += max(min(delta, 0.2), -0.2)
        self.draw()
        self._anim = self.after(15, self._step)

    def draw(self):
        p = self.app.p
        on = p["accent"] if self.enabled else p["border"]
        img = self.app.painter.switch(self.W, self.H, self.t, p["switch_off"], on, self.cget("bg"))
        self.cv.delete("all")
        self.cv.create_image(0, 0, anchor="nw", image=img)


class Pill(tk.Canvas):
    """The rounded "● Sharing" badge in the header."""

    def __init__(self, parent, app):
        self.app = app
        self.font = tkfont.Font(family=FONT_SEMI, size=9)
        self.h = self.font.metrics("linespace") + 2 * app.px(7)
        super().__init__(parent, height=self.h, width=10, bg=parent.cget("bg"), highlightthickness=0, bd=0)

    def set(self, text, color, fill):
        px, h = self.app.px, self.h
        dot = px(8)
        w = px(14) + dot + px(8) + self.font.measure(text) + px(14)
        self.configure(width=w)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self.app.painter.rounded(w, h, h // 2, fill, self.cget("bg")))
        self.create_image(px(14), h / 2, anchor="w", image=self.app.painter.circle(dot, color, fill))
        self.create_text(px(14) + dot + px(8), h / 2, anchor="w", text=text, font=self.font, fill=self.app.p["text"])


class StatusLine(tk.Frame):
    COLORS = {"ok": "success", "warn": "warn", "error": "danger", "info": "muted", "busy": "accent"}

    def __init__(self, parent, app):
        p = app.p
        super().__init__(parent, bg=parent.cget("bg"))
        self.app = app
        self.dot = tk.Label(self, bg=self.cget("bg"), bd=0)
        self.dot.pack(side="left", padx=(0, app.px(10)))
        self.label = tk.Label(self, bg=self.cget("bg"), fg=p["text"], font=(FONT, 9), justify="left", anchor="w")
        self.label.pack(side="left", fill="x", expand=True)
        self.button = RoundButton(self, app, "", None, "ghost", size=9, padx=10, pady=4, radius=8)
        self.bind("<Configure>", lambda e: self.label.configure(wraplength=max(e.width - app.px(150), 120)))

    def set(self, state, text, action=None, action_text=""):
        p = self.app.p
        self.dot.configure(image=self.app.painter.circle(self.app.px(9), p[self.COLORS.get(state, "muted")], self.cget("bg")))
        self.label.configure(text=text)
        if action:
            self.button.command = action
            self.button.set(text=action_text)
            self.button.pack(side="right")
        else:
            self.button.pack_forget()


class FileShareApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        save_config(self.cfg)
        self.dark = windows_prefers_dark()
        self.p = DARK if self.dark else LIGHT
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.painter = Painter()

        self.server = None
        self.state = None
        self.active_port = None
        self.adapters = []
        self.firewall = None
        self.last_received = None
        self._link_id = 0
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()

        recents = [f for f in self.cfg.get("recent_folders", []) if os.path.isdir(f)]
        self.cfg["recent_folders"] = recents
        self.folder = tk.StringVar(value=recents[0] if recents else os.path.expanduser("~"))
        self.port = tk.StringVar(value=str(self.cfg.get("port", 8000)))
        self.uploads_enabled = tk.BooleanVar(value=self.cfg.get("allow_uploads", False))
        self.require_pin = tk.BooleanVar(value=self.cfg.get("require_pin", False))
        self.auto_start = tk.BooleanVar(value=self.cfg.get("auto_start", False))
        self.check_updates = tk.BooleanVar(value=self.cfg.get("check_updates", True))
        self.update_info = None
        self.url_var = tk.StringVar()

        root.title(APP_NAME)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(self.px(1120), sw - 40), min(self.px(840), sh - 90)
        root.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 3, 0)}")
        root.minsize(min(self.px(920), sw - 40), min(self.px(620), sh - 90))
        root.configure(bg=self.p["bg"])
        root.option_add("*Menu.font", (FONT, 10))
        self._set_icon()
        set_dark_titlebar(root, self.dark)

        self._build_ui()

        self.folder.trace_add("write", lambda *_: self._refresh_folder())
        self.uploads_enabled.trace_add("write", self._on_uploads_toggled)
        self.require_pin.trace_add("write", self._on_pin_toggled)
        self.auto_start.trace_add("write", lambda *_: self._save_settings())
        self.check_updates.trace_add("write", self._on_updates_toggled)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._refresh_folder()
        self._on_pin_toggled()
        self._set_running_ui(False)
        self._drain_queues()
        self._tick_stats()
        self.refresh_adapters(then_autostart=self.auto_start.get())
        self.log(f"{APP_NAME} {APP_VERSION} ready. Choose a folder and press Start sharing.")
        if self.check_updates.get():
            root.after(3000, self.check_for_updates)

    # ---------------- setup ----------------

    def px(self, n):
        return int(round(n * self.scale))

    def _set_icon(self):
        try:
            self._icons = [self.painter.pixel_logo(2), self.painter.pixel_logo(4)]
            self.root.iconphoto(True, *self._icons)
        except Exception:
            pass

    def _label(self, parent, text="", size=10, weight="normal", color="text", **kw):
        font = (FONT_SEMI if weight == "semi" else FONT, size)
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=self.p[color], font=font, **kw)

    def _section(self, parent, text):
        return self._label(parent, text, 8, "semi", "muted")

    # ---------------- layout ----------------

    def _build_ui(self):
        p, px = self.p, self.px

        # Header
        head = tk.Frame(self.root, bg=p["card"])
        head.pack(fill="x")
        tk.Frame(self.root, bg=p["border"], height=1).pack(fill="x")
        hin = tk.Frame(head, bg=p["card"])
        hin.pack(fill="x", padx=px(24), pady=px(12))
        logo_scale = max(2, round(px(40) / 16))
        tk.Label(hin, image=self.painter.pixel_logo(logo_scale), bg=p["card"], bd=0).pack(side="left")
        titles = tk.Frame(hin, bg=p["card"])
        titles.pack(side="left", padx=(px(14), 0))
        self._label(titles, APP_NAME, 14, "semi").pack(anchor="w")
        self._label(titles, "Share files between this PC and any phone or computer on your Wi-Fi",
                    9, color="muted").pack(anchor="w")
        right = tk.Frame(hin, bg=p["card"])
        right.pack(side="right")
        RoundButton(right, self, "Can't connect?", self.show_help, "ghost", size=9, padx=12, pady=6).pack(side="right", padx=(px(8), 0))
        self.pill = Pill(right, self)
        self.pill.pack(side="right")
        # Shown only when a newer release exists (packed by _on_update_found)
        self.update_btn = RoundButton(right, self, "Update available", self.install_update, "primary",
                                      size=9, padx=12, pady=6)

        # Footer credit (packed before the body so the expanding body can't push it off-screen)
        foot = tk.Frame(self.root, bg=p["bg"])
        foot.pack(side="bottom", fill="x", pady=(0, px(10)))
        credit = tk.Frame(foot, bg=p["bg"])
        credit.pack()
        self._label(credit, "Made with", 9, color="muted").pack(side="left")
        self._label(credit, "\u2665", 9, color="danger").pack(side="left", padx=px(3))
        self._label(credit, f"by {AUTHOR_NAME}  \u00b7", 9, color="muted").pack(side="left")
        gh = self._label(credit, "GitHub", 9, "semi", color="link", cursor="hand2")
        gh.pack(side="left", padx=(px(4), 0))
        gh.bind("<Button-1>", lambda e: webbrowser.open(AUTHOR_URL))

        # Body: settings on the left; share panel and activity log on the right
        body = tk.Frame(self.root, bg=p["bg"])
        body.pack(fill="both", expand=True, padx=px(22), pady=px(18))
        body.grid_columnconfigure(0, weight=5, uniform="col")
        body.grid_columnconfigure(1, weight=6, uniform="col")
        body.grid_rowconfigure(1, weight=1)

        left = tk.Frame(body, bg=p["bg"])
        left.grid(row=0, column=0, rowspan=2, sticky="new", padx=(0, px(9)))
        self._build_folder_card(left)
        self._build_settings_card(left)
        self.start_btn = RoundButton(left, self, "Start sharing", self.toggle_server, "primary",
                                     size=12, pady=12, radius=14)
        self.start_btn.pack(fill="x", pady=(px(16), 0))

        self.share_card = Card(body, self, pad=(22, 20))
        self.share_card.grid(row=0, column=1, sticky="new", padx=(px(9), 0))
        self._build_idle_view()
        self._build_live_view()

        logcard = Card(body, self, pad=(20, 14))
        logcard.grid(row=1, column=1, sticky="nsew", padx=(px(9), 0), pady=(px(16), 0))
        self._build_log(logcard.body)

    def _build_folder_card(self, parent):
        p, px = self.p, self.px
        card = Card(parent, self)
        card.pack(fill="x")
        inner = card.body
        self._section(inner, "SHARED FOLDER").pack(anchor="w")

        row = tk.Frame(inner, bg=p["card"])
        row.pack(fill="x", pady=(px(12), 0))
        tk.Label(row, image=self.painter.folder_tile(px(46), p["folder_soft"], p["folder"], p["card"]),
                 bg=p["card"], bd=0).pack(side="left", anchor="n")
        txt = tk.Frame(row, bg=p["card"])
        txt.pack(side="left", fill="x", expand=True, padx=(px(14), 0))
        self.folder_name_lbl = self._label(txt, "", 12, "semi", anchor="w")
        self.folder_name_lbl.pack(anchor="w", fill="x")
        self.path_font = tkfont.Font(family=FONT, size=9)
        self.folder_path_lbl = tk.Label(txt, text="", bg=p["card"], fg=p["muted"], font=self.path_font, anchor="w")
        self.folder_path_lbl.pack(anchor="w", fill="x")
        self.folder_info_lbl = self._label(txt, "", 9, color="muted", anchor="w")
        self.folder_info_lbl.pack(anchor="w", fill="x", pady=(px(2), 0))
        txt.bind("<Configure>", lambda e: self._fit_path(e.width))

        btns = tk.Frame(inner, bg=p["card"])
        btns.pack(fill="x", pady=(px(16), 0))
        self.change_btn = RoundButton(btns, self, "Change folder…", self.browse_folder, "secondary")
        self.change_btn.pack(side="left")
        self.recent_btn = RoundButton(btns, self, "Recent  ▾", self.show_recent_menu, "secondary")
        self.recent_btn.pack(side="left", padx=(px(8), 0))
        RoundButton(btns, self, "Open in Explorer", self.open_folder_in_explorer, "subtle").pack(side="right")

    def _build_settings_card(self, parent):
        p, px = self.p, self.px
        card = Card(parent, self)
        card.pack(fill="x", pady=(px(16), 0))
        inner = card.body
        self._section(inner, "SETTINGS").pack(anchor="w")

        grid = tk.Frame(inner, bg=p["card"])
        grid.pack(fill="x", pady=(px(12), 0))
        grid.grid_columnconfigure(1, weight=1)
        self._label(grid, "Network").grid(row=0, column=0, sticky="w")
        self.adapter_dd = Dropdown(grid, self, self._on_adapter_selected)
        self.adapter_dd.grid(row=0, column=1, sticky="ew", padx=(px(14), px(8)))
        self.refresh_btn = RoundButton(grid, self, "↻", lambda: self.refresh_adapters(), "secondary",
                                       size=11, padx=12, pady=5)
        self.refresh_btn.grid(row=0, column=2)
        self._label(grid, "Port").grid(row=1, column=0, sticky="w", pady=(px(12), 0))
        port_row = tk.Frame(grid, bg=p["card"])
        port_row.grid(row=1, column=1, columnspan=2, sticky="w", padx=(px(14), 0), pady=(px(12), 0))
        self.port_field = Field(port_row, self, self.port, chars=6)
        self.port_field.pack(side="left")
        self._label(port_row, "If it's busy, the next free port is used", 9, color="muted").pack(side="left", padx=(px(12), 0))

        tk.Frame(inner, bg=p["border"], height=1).pack(fill="x", pady=(px(16), px(4)))
        self.uploads_switch = ToggleSwitch(inner, self, self.uploads_enabled, "Allow uploads",
                                           "Phones and other PCs can send files into this folder")
        self.uploads_switch.pack(fill="x", pady=(px(10), 0))
        self.pin_switch = ToggleSwitch(inner, self, self.require_pin, "Require a PIN",
                                       "Only people who know the PIN can see your files")
        self.pin_switch.pack(fill="x", pady=(px(14), 0))
        self.pin_row = tk.Frame(inner, bg=p["card"])
        self.pin_value_lbl = self._label(self.pin_row, "", 10, "semi", color="accent")
        self.pin_value_lbl.pack(side="left")
        RoundButton(self.pin_row, self, "New PIN", self.new_pin, "ghost", size=9, padx=10, pady=3, radius=8).pack(side="left", padx=(px(8), 0))
        self.auto_switch = ToggleSwitch(inner, self, self.auto_start, "Start sharing when the app opens")
        self.auto_switch.pack(fill="x", pady=(px(14), 0))
        self.updates_switch = ToggleSwitch(inner, self, self.check_updates, "Check for updates",
                                           "Asks GitHub for new versions. No files are sent")
        self.updates_switch.pack(fill="x", pady=(px(14), 0))

    def _build_idle_view(self):
        p, px = self.p, self.px
        v = self.idle_view = tk.Frame(self.share_card.body, bg=p["card"])
        box = tk.Frame(v, bg=p["card"])
        box.pack(expand=True, pady=px(18))
        tk.Label(box, image=self.painter.pixel_logo(max(4, round(px(80) / 16))), bg=p["card"], bd=0).pack()
        self._label(box, "Ready when you are", 14, "semi").pack(pady=(px(16), px(4)))
        self._label(box, "Press Start sharing to get a QR code and a link\nthat opens this folder on your phone.",
                    10, color="muted", justify="center").pack()
        steps = tk.Frame(box, bg=p["card"])
        steps.pack(pady=(px(20), 0))
        d = px(24)
        for i, text in enumerate(("Connect your phone to the same Wi-Fi as this PC",
                                  "Press Start sharing",
                                  "Scan the QR code with your phone's camera"), 1):
            r = tk.Frame(steps, bg=p["card"])
            r.pack(anchor="w", pady=px(4))
            n = tk.Canvas(r, width=d, height=d, bg=p["card"], highlightthickness=0)
            n.pack(side="left")
            n.create_image(0, 0, anchor="nw", image=self.painter.circle(d, p["accent_soft"], p["card"]))
            n.create_text(d / 2, d / 2, text=str(i), fill=p["accent"], font=(FONT_SEMI, 9))
            self._label(r, text, 10).pack(side="left", padx=(px(12), 0))

    def _build_live_view(self):
        p, px = self.p, self.px
        v = self.live_view = tk.Frame(self.share_card.body, bg=p["card"])

        top = tk.Frame(v, bg=p["card"])
        top.pack(fill="x")
        qr_card = Card(top, self, fill="#ffffff", radius=16, pad=(16, 16))
        qr_card.pack(side="left", anchor="n")
        self.qr_size = px(172)
        self.qr_canvas = tk.Canvas(qr_card.body, width=self.qr_size, height=self.qr_size, bg="#ffffff", highlightthickness=0)
        self.qr_canvas.pack()

        info = tk.Frame(top, bg=p["card"])
        info.pack(side="left", fill="both", expand=True, padx=(px(22), 0))
        self._label(info, "Scan with your phone's camera", 13, "semi").pack(anchor="w")
        self._label(info, "or open this link on any device on the same Wi-Fi", 9,
                    color="muted").pack(anchor="w", pady=(px(2), px(6)))
        self.url_entry = tk.Entry(info, textvariable=self.url_var, font=(FONT_SEMI, 16), relief="flat", bd=0,
                                  readonlybackground=p["card"], fg=p["accent"], state="readonly",
                                  highlightthickness=0, cursor="xterm", selectbackground=p["accent_soft"],
                                  selectforeground=p["accent"])
        self.url_entry.pack(anchor="w", fill="x")
        btns = tk.Frame(info, bg=p["card"])
        btns.pack(anchor="w", pady=(px(12), 0))
        self.copy_btn = RoundButton(btns, self, "Copy link", self.copy_url, "primary", size=9, padx=14, pady=7)
        self.copy_btn.pack(side="left")
        RoundButton(btns, self, "Open in browser", self.open_in_browser, "secondary", size=9, padx=14, pady=7).pack(side="left", padx=(px(8), 0))
        self.pin_box = tk.Frame(info, bg=p["card"])
        self._label(self.pin_box, "PIN", 8, "semi", color="muted").pack(anchor="w")
        self.pin_big = tk.Label(self.pin_box, text="", bg=p["card"], fg=p["text"], font=("Consolas", 20, "bold"))
        self.pin_big.pack(anchor="w")

        # "Received" banner with Show in folder / Open — appears after the first upload
        self.recv_card = Card(v, self, fill=p["success_soft"], outline=None, radius=14, pad=(16, 12))
        rb = self.recv_card.body
        rtxt = tk.Frame(rb, bg=p["success_soft"])
        rtxt.pack(side="left", fill="x", expand=True)
        self.recv_title = tk.Label(rtxt, text="", bg=p["success_soft"], fg=p["success"], font=(FONT_SEMI, 8), anchor="w")
        self.recv_title.pack(anchor="w", fill="x")
        self.recv_font = tkfont.Font(family=FONT_SEMI, size=10)
        self.recv_name = tk.Label(rtxt, text="", bg=p["success_soft"], fg=p["text"], font=self.recv_font, anchor="w")
        self.recv_name.pack(anchor="w", fill="x")
        rtxt.bind("<Configure>", lambda e: self._fit_received(e.width))
        RoundButton(rb, self, "Open", self.open_received, "ghost", size=9, padx=12, pady=6).pack(side="right", padx=(px(6), 0))
        RoundButton(rb, self, "Show in folder", self.locate_received, "primary", size=9, padx=14, pady=6).pack(side="right")

        self.lines = tk.Frame(v, bg=p["card"])
        self.lines.pack(fill="x", pady=(px(16), 0))
        self.net_line = StatusLine(self.lines, self)
        self.net_line.pack(fill="x")
        self.fw_line = StatusLine(self.lines, self)
        self.fw_line.pack(fill="x", pady=(px(8), 0))

        stats = tk.Frame(v, bg=p["card"])
        stats.pack(fill="x", pady=(px(16), 0))
        self.stat_vals = {}
        for col, (key, caption) in enumerate((("devices", "Devices connected"), ("sent", "Sent to devices"),
                                              ("received", "Received from devices"))):
            stats.grid_columnconfigure(col, weight=1, uniform="stat")
            tile = Card(stats, self, fill=p["surface2"], outline=None, radius=14, pad=(14, 10))
            tile.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else px(8), 0))
            val = tk.Label(tile.body, text="0", bg=p["surface2"], fg=p["text"], font=(FONT_SEMI, 16))
            val.pack(anchor="w")
            cap = tk.Label(tile.body, text=caption, bg=p["surface2"], fg=p["muted"], font=(FONT, 8), anchor="w")
            cap.pack(anchor="w", fill="x")
            self.stat_vals[key] = val
            if key == "devices":
                self.devices_cap = cap
        self.caption_font = tkfont.Font(family=FONT, size=8)

    def _build_log(self, parent):
        p, px = self.p, self.px
        head = tk.Frame(parent, bg=p["card"])
        head.pack(fill="x", pady=(0, px(6)))
        self._section(head, "ACTIVITY").pack(side="left")
        RoundButton(head, self, "Clear", self.clear_log, "subtle", size=8, padx=10, pady=3, radius=8).pack(side="right")
        self.log_box = tk.Text(parent, height=6, bg=p["card"], fg=p["text"], relief="flat", bd=0, highlightthickness=0,
                               font=(FONT, 9), wrap="word", state="disabled", cursor="arrow",
                               spacing1=px(3), spacing3=px(3), selectbackground=p["accent_soft"],
                               selectforeground=p["text"], inactiveselectbackground=p["accent_soft"])
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("ts", foreground=p["muted"], font=("Consolas", 8))
        self.log_box.tag_configure("success", foreground=p["success"])
        self.log_box.tag_configure("warn", foreground=p["warn"])
        self.log_box.tag_configure("error", foreground=p["danger"])
        self.log_box.tag_configure("link", foreground=p["link"], font=(FONT_SEMI, 9), underline=False)
        self.log_box.tag_bind("link", "<Enter>", lambda e: self.log_box.configure(cursor="hand2"))
        self.log_box.tag_bind("link", "<Leave>", lambda e: self.log_box.configure(cursor="arrow"))

    # ---------------- thread-safe plumbing ----------------

    def log(self, message, level="info"):
        """Safe to call from any thread."""
        self.log_queue.put((datetime.now().strftime("%H:%M:%S"), message, level, None))

    def on_received(self, path, who, message):
        """Called from a server thread for every file a device uploads."""
        self.log_queue.put((datetime.now().strftime("%H:%M:%S"), message, "success", path))
        self.call_in_ui(self._show_received, path, who)

    def call_in_ui(self, fn, *args):
        """Run fn(*args) on the Tk thread (Tkinter is not thread-safe)."""
        self.ui_queue.put((fn, args))

    def _drain_queues(self):
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            box = self.log_box
            box.configure(state="normal")
            for ts, message, level, path in lines:
                box.insert("end", ts + "   ", "ts")
                box.insert("end", message, level if level != "info" else ())
                if path:
                    self._link_id += 1
                    tag = f"link{self._link_id}"
                    box.insert("end", "    Show in folder", ("link", tag))
                    box.tag_bind(tag, "<Button-1>", lambda e, p=path: reveal_in_explorer(p))
                box.insert("end", "\n")
            if int(box.index("end-1c").split(".")[0]) > 2000:
                box.delete("1.0", "500.0")
            box.see("end")
            box.configure(state="disabled")
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    self.log(f"Internal error: {e!r}", "error")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queues)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ---------------- folder ----------------

    def _fit_path(self, width=None):
        width = width or self.folder_path_lbl.winfo_width()
        if width > 20:
            self.folder_path_lbl.configure(text=elide_middle(self.folder.get().strip(), self.path_font, width - 4))

    def _refresh_folder(self):
        folder = self.folder.get().strip()
        name = os.path.basename(os.path.normpath(folder)) or folder
        self.folder_name_lbl.configure(text=name or "No folder chosen")
        self._fit_path()
        if folder and os.path.isdir(folder):
            try:
                with os.scandir(folder) as it:
                    entries = [e for e in it if not is_hidden_entry(e)]
                folders = sum(1 for e in entries if e.is_dir())
                files = len(entries) - folders
                parts = [f"{folders} folder{'s' if folders != 1 else ''}", f"{files} file{'s' if files != 1 else ''}"]
                self.folder_info_lbl.configure(text=" · ".join(parts), fg=self.p["muted"])
            except OSError:
                self.folder_info_lbl.configure(text="Windows won't let this app read that folder", fg=self.p["danger"])
        else:
            self.folder_info_lbl.configure(text="This folder doesn't exist — choose another", fg=self.p["danger"])

    def browse_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or os.path.expanduser("~"),
                                         title="Choose the folder to share")
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def show_recent_menu(self):
        if not self.recent_btn.enabled:
            return
        menu = tk.Menu(self.root, tearoff=0)
        recents = [f for f in self.cfg.get("recent_folders", []) if os.path.isdir(f)]
        if not recents:
            menu.add_command(label="No recent folders yet", state="disabled")
        for f in recents:
            menu.add_command(label=f, command=lambda f=f: self.folder.set(f))
        menu.tk_popup(self.recent_btn.winfo_rootx(),
                      self.recent_btn.winfo_rooty() + self.recent_btn.winfo_height() + self.px(4))

    def open_folder_in_explorer(self):
        folder = self.folder.get().strip()
        if folder and os.path.isdir(folder):
            open_in_explorer(folder)
        else:
            messagebox.showerror("Folder not found", "Please choose a folder that exists first.")

    # ---------------- received files ----------------

    def _show_received(self, path, who):
        self.last_received = path
        self.recv_title.configure(text=f"RECEIVED FROM {who.upper()}  ·  {datetime.now().strftime('%H:%M')}")
        self._fit_received()
        if not self.recv_card.winfo_ismapped() and self.server:
            self.recv_card.pack(fill="x", pady=(self.px(16), 0), before=self.lines)

    def _fit_received(self, width=None):
        if not self.last_received:
            return
        width = width or self.recv_name.winfo_width()
        name = os.path.basename(self.last_received)
        self.recv_name.configure(text=elide_middle(name, self.recv_font, max(width - 4, 60)))

    def locate_received(self):
        if self.last_received:
            if os.path.exists(self.last_received):
                reveal_in_explorer(self.last_received)
            else:
                self.log("That file has been moved or deleted.", "warn")

    def open_received(self):
        if self.last_received and os.path.exists(self.last_received):
            open_in_explorer(self.last_received)

    # ---------------- network ----------------

    def refresh_adapters(self, then_autostart=False):
        self.adapter_dd.set_options([], placeholder="Detecting networks…")

        def work():
            adapters = list_adapters()
            self.call_in_ui(self._set_adapters, adapters, then_autostart)
        threading.Thread(target=work, daemon=True).start()

    def _set_adapters(self, adapters, then_autostart=False):
        self.adapters = adapters
        if not adapters:
            self.adapter_dd.set_options([], placeholder="No network connection found")
        else:
            saved = self.cfg.get("adapter_ip")
            idx = next((i for i, a in enumerate(adapters) if a["ip"] == saved), 0)
            self.adapter_dd.set_options([a["label"] for a in adapters], idx)
        if self.server:
            self._refresh_share_info()
        if then_autostart and not self.server and os.path.isdir(self.folder.get().strip()):
            self.start_server()

    def current_adapter(self):
        idx = self.adapter_dd.current()
        if 0 <= idx < len(self.adapters):
            return self.adapters[idx]
        ip = default_route_ip() or "127.0.0.1"
        return {"ip": ip, "label": ip, "category": "", "alias": "", "network": ""}

    def _on_adapter_selected(self, _index=None):
        self.cfg["adapter_ip"] = self.current_adapter()["ip"]
        save_config(self.cfg)
        if self.server:
            self._refresh_share_info()
            self.check_firewall()

    # ---------------- settings ----------------

    def _save_settings(self):
        self.cfg["allow_uploads"] = bool(self.uploads_enabled.get())
        self.cfg["require_pin"] = bool(self.require_pin.get())
        self.cfg["auto_start"] = bool(self.auto_start.get())
        self.cfg["check_updates"] = bool(self.check_updates.get())
        save_config(self.cfg)

    def _on_uploads_toggled(self, *_):
        self._save_settings()
        if self.state is not None:
            self.state.uploads_enabled = self.uploads_enabled.get()
            self.log("Uploads turned " + ("on — devices can now send files to this folder." if self.uploads_enabled.get() else "off."))

    def _on_pin_toggled(self, *_):
        on = self.require_pin.get()
        self._save_settings()
        pin = self.cfg["pin"]
        self.pin_value_lbl.configure(text=f"PIN  {pin[:3]} {pin[3:]}")
        if on:
            self.pin_row.pack(fill="x", pady=(self.px(8), 0), after=self.pin_switch)
        else:
            self.pin_row.pack_forget()
        if self.state is not None:
            self.state.set_pin(pin if on else "")
            self.log("PIN protection turned " + ("on." if on else "off."))
            self._refresh_share_info()

    def new_pin(self):
        self.cfg["pin"] = new_pin()
        save_config(self.cfg)
        self._on_pin_toggled()
        if self.state is not None and self.require_pin.get():
            self.log("New PIN created. Devices will need to enter it again.")

    # ---------------- server control ----------------

    def toggle_server(self):
        if self.server:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        folder = self.folder.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Folder not found", "Please choose a folder that exists.")
            return
        try:
            port = int(self.port.get())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", "The port must be a number between 1 and 65535 (8000 is a good choice).")
            return

        state = ServerState(folder, self.uploads_enabled.get(), self.cfg["pin"] if self.require_pin.get() else "",
                            self.cfg["secret"], self.log, on_received=self.on_received)
        server, last_error = None, None
        for candidate in range(port, min(port + 20, 65536)):
            try:
                server = ShareServer(("0.0.0.0", candidate), state)
                break
            except OSError as e:
                last_error = e
        if server is None:
            messagebox.showerror("Couldn't start sharing", f"No free port was found near {port}.\n\n{last_error}")
            return
        actual = server.server_address[1]
        if actual != port:
            self.log(f"Port {port} is in use by another program, so port {actual} is being used instead.", "warn")
            self.port.set(str(actual))

        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True).start()
        self.server, self.state, self.active_port = server, state, actual

        recents = [f for f in self.cfg.get("recent_folders", []) if f != folder]
        self.cfg["recent_folders"] = [folder] + recents[:7]
        self.cfg["port"] = actual
        self.cfg["adapter_ip"] = self.current_adapter()["ip"]
        self._save_settings()

        self._set_running_ui(True)
        self._refresh_share_info()
        self.log(f"Sharing '{state.root_name}' at {self.url_var.get()}", "success")
        if not self.adapters:
            self.log("This PC doesn't seem to be connected to any network. Connect to Wi-Fi, then press ↻.", "warn")
        self.check_firewall()

    def stop_server(self):
        server = self.server
        self.server, self.state, self.active_port = None, None, None
        if server:
            server.shutdown()
            server.server_close()
        self._set_running_ui(False)
        self.log("Sharing stopped.")

    def _set_running_ui(self, running):
        p = self.p
        self.start_btn.set(text="Stop sharing" if running else "Start sharing", kind="danger" if running else "primary")
        self.change_btn.set(enabled=not running)
        self.recent_btn.set(enabled=not running)
        self.port_field.entry.configure(state="disabled" if running else "normal")
        if running:
            self.pill.set("Sharing", p["success"], p["success_soft"])
            self.idle_view.pack_forget()
            self.recv_card.pack_forget()
            self.last_received = None
            self.live_view.pack(fill="both", expand=True)
        else:
            self.pill.set("Not sharing", p["muted"], p["surface2"])
            self.live_view.pack_forget()
            self.idle_view.pack(fill="both", expand=True)

    def share_urls(self):
        adapter = self.current_adapter()
        base = f"http://{adapter['ip']}:{self.active_port}"
        pin = self.state.pin if self.state else ""
        return base, (f"{base}/?pin={pin}" if pin else base + "/")

    def _refresh_share_info(self):
        if not self.state:
            return
        base, qr_url = self.share_urls()
        self.url_var.set(base)
        self._draw_qr(qr_url)
        if self.state.pin:
            self.pin_big.configure(text=f"{self.state.pin[:3]} {self.state.pin[3:]}")
            self.pin_box.pack(anchor="w", pady=(self.px(14), 0))
        else:
            self.pin_box.pack_forget()

        a = self.current_adapter()
        if not self.adapters:
            self.net_line.set("error", "Not connected to a network. Connect this PC to Wi-Fi, then press ↻ in Settings.")
        else:
            where = " · ".join(x for x in (a.get("alias"), a.get("network")) if x) or a["ip"]
            cat = (a.get("category") or "").lower()
            if cat == "public":
                self.net_line.set("info", f"On {where} (Windows treats this as a public network)")
            else:
                self.net_line.set("ok", f"On {where}" + (" (private network)" if cat == "private" else ""))

    def _draw_qr(self, data):
        c, size = self.qr_canvas, self.qr_size
        c.delete("all")
        if not QR_AVAILABLE:
            c.create_text(size / 2, size / 2, width=size - 20, justify="center", fill="#667085", font=(FONT, 9),
                          text="Install the 'qrcode' package\n(pip install qrcode)\nto show a QR code here.")
            return
        qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)
        cell = size // n
        off = (size - cell * n) // 2
        for y, row in enumerate(matrix):
            x = 0
            while x < n:  # draw horizontal runs as single rectangles
                if row[x]:
                    start = x
                    while x < n and row[x]:
                        x += 1
                    c.create_rectangle(off + start * cell, off + y * cell, off + x * cell, off + (y + 1) * cell,
                                       fill="#141824", outline="")
                else:
                    x += 1

    # ---------------- firewall ----------------

    def check_firewall(self):
        if not IS_WINDOWS or not self.server:
            self.fw_line.pack_forget()
            return
        self.fw_line.pack(fill="x", pady=(self.px(8), 0))
        self.fw_line.set("busy", "Checking Windows Firewall…")
        port, category = self.active_port, self.current_adapter().get("category", "")

        def work():
            status = firewall_status(port, category)
            self.call_in_ui(self._show_firewall, status, port)
        threading.Thread(target=work, daemon=True).start()

    def _show_firewall(self, status, port):
        if not self.server or port != self.active_port:
            return
        self.firewall = status
        if status == "ok":
            self.fw_line.set("ok", "Windows Firewall lets other devices connect")
        elif status == "blocked":
            self.fw_line.set("error", "Windows Firewall is blocking this app, so phones can't connect",
                             self.fix_firewall_clicked, "Fix connection")
            self.log("Windows Firewall is blocking this app. Press 'Fix connection' to allow it.", "warn")
        elif status == "missing":
            self.fw_line.set("warn", "Windows Firewall may stop phones from connecting",
                             self.fix_firewall_clicked, "Fix connection")
        else:
            self.fw_line.set("info", "Couldn't check Windows Firewall. If phones can't connect, press Fix.",
                             self.fix_firewall_clicked, "Fix connection")

    def fix_firewall_clicked(self):
        port = self.active_port
        if not port:
            return
        if not messagebox.askyesno(
                "Allow phones to connect",
                f"This adds a Windows Firewall rule that lets devices on your local network "
                f"connect to port {port}, and removes any rule that blocks this app.\n\n"
                "Windows will ask for administrator permission. Continue?"):
            return
        self.fw_line.set("busy", "Waiting for administrator permission…")

        def work():
            ok = fix_firewall(port)
            self.call_in_ui(self._after_fix, ok)
        threading.Thread(target=work, daemon=True).start()

    def _after_fix(self, ok):
        if ok:
            self.log("Windows Firewall rule added — phones on your network can now connect.", "success")
        else:
            self.log("Firewall change was cancelled.", "warn")
        self.check_firewall()

    # ---------------- live stats ----------------

    def _tick_stats(self):
        if self.state is not None:
            snap = self.state.snapshot()
            self.stat_vals["devices"].configure(text=str(len(snap["devices"])))
            self.stat_vals["sent"].configure(text=human_size(snap["bytes_sent"]) or "0 B")
            self.stat_vals["received"].configure(text=human_size(snap["bytes_received"]) or "0 B")
            names = ", ".join(n for n, ip in snap["devices"]) or "Devices connected"
            width = max(self.devices_cap.winfo_width() - 4, 60)
            self.devices_cap.configure(text=elide_middle(names, self.caption_font, width))
        self.root.after(1000, self._tick_stats)

    # ---------------- actions ----------------

    def open_in_browser(self):
        if self.state:
            webbrowser.open(self.share_urls()[1])

    def copy_url(self):
        if self.url_var.get():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.url_var.get())
            self.copy_btn.set(text="Copied ✓")
            self.root.after(1500, lambda: self.copy_btn.set(text="Copy link"))

    def show_help(self):
        messagebox.showinfo(
            "If a phone can't connect",
            "1.  Put the phone on the same Wi-Fi as this PC (not mobile data).\n\n"
            "2.  Press “Fix connection” in the sharing panel so Windows Firewall lets the phone in.\n\n"
            "3.  Type the address exactly as shown, starting with http:// (not https://).\n\n"
            "4.  Turn off VPN apps on the PC and on the phone.\n\n"
            "5.  Guest and public Wi-Fi often stop devices from seeing each other. Use your home Wi-Fi, "
            "or turn on Mobile Hotspot on this PC and connect the phone to it.\n\n"
            "6.  If this PC has more than one network (Wi-Fi, Ethernet, VPN), pick the one the phone is on "
            "under Settings → Network.")

    # ---------------- updates ----------------

    def _on_updates_toggled(self, *_):
        self._save_settings()
        if self.check_updates.get():
            self.check_for_updates()
        elif self.update_info:
            self.update_btn.pack_forget()

    def check_for_updates(self):
        def work():
            try:
                info = fetch_latest_release()
            except Exception:
                return  # offline or GitHub unreachable: stay quiet, sharing works without it
            if version_tuple(info["version"]) > version_tuple(APP_VERSION):
                self.call_in_ui(self._on_update_found, info)
        threading.Thread(target=work, daemon=True).start()

    def _on_update_found(self, info):
        if not self.check_updates.get():
            return
        first = self.update_info is None
        self.update_info = info
        self.update_btn.set(text=f"Update to {info['version']}", enabled=True)
        self.update_btn.pack(side="right", padx=(0, self.px(8)))
        if first:
            self.log(f"Version {info['version']} is available. Press “Update to {info['version']}” at the top to install it.", "success")

    def install_update(self):
        info = self.update_info
        if not info:
            return
        # Running from source, or the release has no installer: just show the release page
        if not (IS_WINDOWS and getattr(sys, "frozen", False) and info["setup_url"]):
            webbrowser.open(info["page_url"])
            return
        if not messagebox.askyesno(
                f"Update to {info['version']}",
                f"Download and install {APP_NAME} {info['version']}?\n\n"
                f"The app will close while it updates and open again when it's done. "
                f"Sharing stops until then.", parent=self.root):
            return
        self.update_btn.set(text="Downloading…", enabled=False)
        self.log(f"Downloading version {info['version']}…")
        dest = os.path.join(tempfile.gettempdir(), f"LAN-File-Share-Setup-{info['version']}.exe")

        def work():
            try:
                download_file(info["setup_url"], dest)
            except Exception as e:
                self.log(f"Couldn't download the update: {e}", "error")
                self.call_in_ui(self.update_btn.set, f"Update to {info['version']}", None, True)
                return
            self.call_in_ui(self._run_installer, dest)
        threading.Thread(target=work, daemon=True).start()

    def _run_installer(self, path):
        release_single_instance()  # so setup doesn't wait for this window to close
        try:
            # /SILENT shows only a progress window; setup reuses the previous install folder and options,
            # then reopens the app (see the skipifnotsilent [Run] entry in the installer script)
            subprocess.Popen([path, "/SILENT", "/NORESTART"])
        except OSError as e:
            self.log(f"Couldn't start the installer: {e}", "error")
            self.update_btn.set(text=f"Update to {self.update_info['version']}", enabled=True)
            return
        self.on_close()

    def on_close(self):
        if self.server:
            self.stop_server()
        self.root.destroy()


SINGLE_INSTANCE_MUTEX = "LANFileShare_SingleInstance"  # also referenced by the installer (AppMutex)
_instance_mutex = None


def claim_single_instance():
    """Return False (after bringing the running window to the front) if the
    app is already open, so two copies never fight over the same port."""
    global _instance_mutex
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        _instance_mutex = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return True
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return False
    except Exception:
        return True


def release_single_instance():
    global _instance_mutex
    if _instance_mutex:
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_instance_mutex))
        except Exception:
            pass
        _instance_mutex = None


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(APP_VERSION)
        sys.exit(0)
    if "--make-icon" in sys.argv:
        write_icon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico"))
        sys.exit(0)
    if not claim_single_instance():
        sys.exit(0)
    enable_windows_dpi_awareness()
    root = tk.Tk()
    if not PIL_AVAILABLE:
        root.withdraw()
        messagebox.showerror(APP_NAME, "This app needs the Pillow package.\n\nRun:  pip install pillow qrcode")
        sys.exit(1)
    app = FileShareApp(root)
    root.mainloop()
