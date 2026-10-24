"""Pictures and files you send Lago from the Talk to Lago page.

Each one is saved to a file and named at the top of your message as "[Image #N: /path]"
or "[File #N: /path]"; Lago opens it with his Read tool, which shows him a picture or a
PDF and reads a text file, as Claude Code does with what you paste into its terminal.

The files are short-lived. Once Lago has opened one it's in his conversation transcript
(as a pasted image is in Claude Code's), so the file only serves the chat and the work
sessions he hands it to. A sweep deletes files older than KEEP_SECONDS, then the oldest
ones while the folder is over MAX_FOLDER_BYTES; it runs when the app starts, every
SWEEP_SECONDS while it's open, and before each new file is saved. Every save, refusal
and deletion goes to attachments.log in the state folder.
"""

import base64
import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path

from config_store import STATE_DIR

FOLDER = STATE_DIR / "attachments"
LOG_PATH = STATE_DIR / "attachments.log"
IMAGES = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "gif": (b"GIF87a", b"GIF89a")}
LIMITS = {"image": 10 * 1024 * 1024, "pdf": 20 * 1024 * 1024, "text": 5 * 1024 * 1024}
MAX_FILES = 10
KEEP_SECONDS = 3 * 86400
MAX_FOLDER_BYTES = 200 * 1024 * 1024
SWEEP_SECONDS = 3600
# The files save() writes: stamp, unique part, number, then the original name (or, for
# the first pictures, just an extension).
NAME = re.compile(r"^\d{8}-\d{6}-\d{6}-\d+[-.][A-Za-z0-9._-]{1,90}$")
LINE = re.compile(r"^\[(?:Image|File) #\d+: (" + re.escape(str(FOLDER)) + r"/[^\]\n/]+)\]\n?", re.M)
REFUSED_KIND = "Lago can read pictures (PNG, JPEG, GIF, WebP), PDFs and text files"

log = logging.getLogger("lago.attachments")
_lock = threading.Lock()


class AttachmentError(Exception):
    pass


def _setup_log():
    if log.handlers:
        return
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=1,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_setup_log()


def save(files):
    """Save data-URL files ({"data": "data:...;base64,...", "name": "x.pdf"}) and return
    their paths. Nothing is saved unless every file is acceptable."""
    if len(files) > MAX_FILES:
        _refuse(f"Send at most {MAX_FILES} files at a time")
    decoded = [_decode(item) for item in files]
    with _lock:
        FOLDER.mkdir(parents=True, exist_ok=True)
        _sweep()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        unique = f"{time.time_ns() // 1000 % 10**6:06d}"
        paths = []
        for n, (kind, name, data) in enumerate(decoded, 1):
            path = FOLDER / f"{stamp}-{unique}-{n}-{name}"
            partial = path.with_name(f".{path.name}.partial")
            try:
                partial.write_bytes(data)
                os.replace(partial, path)  # never a half-written file under its real name
            except OSError as error:
                partial.unlink(missing_ok=True)
                for done in paths:
                    os.unlink(done)
                log.error("couldn't save a file: %r", error)
                raise AttachmentError("Couldn't save the file; see attachments.log") from error
            paths.append(str(path))
            log.info("saved %s (%s, %d bytes)", path.name, kind, len(data))
    return paths


def _decode(item):
    item = item or {}
    header, _, encoded = str(item.get("data", "")).partition(",")
    if not re.fullmatch(r"data:[\w/+.-]*;base64", header):
        _refuse("That file couldn't be read", header[:40])
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        _refuse("That file couldn't be read", "bad base64")
    kind, extension = _kind(data)
    if not kind:
        _refuse(REFUSED_KIND, str(item.get("name", ""))[:60])
    if len(data) > LIMITS[kind]:
        _refuse(f"{_label(kind)} can be at most {LIMITS[kind] // 1024 // 1024} MB", f"{len(data)} bytes")
    return kind, _safe_name(item.get("name"), extension, kind), data


def _kind(data):
    """("image"|"pdf"|"text", extension) going by what the bytes are, or (None, None)."""
    for extension, magic in IMAGES.items():
        if data.startswith(magic):
            return "image", extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp"
    if data.startswith(b"%PDF-"):
        return "pdf", "pdf"
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text", None
        except UnicodeDecodeError:
            pass
    return None, None


def _label(kind):
    return {"image": "A picture", "pdf": "A PDF", "text": "A text file"}[kind]


def _safe_name(name, extension, kind):
    """The original file name, kept readable but safe, with the extension the content has."""
    name = Path(str(name or "")).name
    found = re.search(r"\.([A-Za-z0-9]{1,8})$", name)
    base = name[:found.start()] if found else name
    extension = extension or (found[1] if found else "txt")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")[:70]
    return f"{base or ('picture' if kind == 'image' else 'file')}.{extension.lower()}"


def _refuse(message, detail=""):
    log.warning("refused a file: %s%s", message, f" ({detail})" if detail else "")
    raise AttachmentError(message)


def is_image(path):
    return Path(path).suffix.lower().lstrip(".") in ("png", "jpg", "jpeg", "gif", "webp")


def display_name(path):
    """The name you gave the file, without the stamp save() put in front."""
    match = re.match(r"^\d{8}-\d{6}-\d{6}-\d+-(.+)$", Path(path).name)
    return match[1] if match else Path(path).name


def message(text, paths):
    """The text Lago receives: one line per file, then what you wrote."""
    counts = {"Image": 0, "File": 0}
    lines = []
    for path in paths:
        label = "Image" if is_image(path) else "File"
        counts[label] += 1
        lines.append(f"[{label} #{counts[label]}: {path}]")
    return "\n".join(lines + ([""] if lines and text else []) + ([text] if text else []))


def split(text):
    """(what you wrote, [file paths]) from a message `message` made."""
    return LINE.sub("", text).strip(), LINE.findall(text)


def kept(path):
    """`path` is a file in the attachments folder that still exists (it may be resent)."""
    path = Path(str(path))
    return path.parent == FOLDER and bool(NAME.match(path.name)) and path.is_file()


def sweep():
    """Delete files past their time, then the oldest while the folder is too big."""
    with _lock:
        _sweep()


def _sweep():
    try:
        entries = [(path, path.stat()) for path in FOLDER.iterdir()]
    except FileNotFoundError:
        return
    except OSError as error:
        log.error("couldn't list %s: %r", FOLDER, error)
        return
    now = time.time()
    keep = []
    for path, stat in entries:
        if path.name.endswith(".partial") and now - stat.st_mtime > 60:
            _delete(path, "left over from a failed save")
        elif not NAME.match(path.name):
            continue  # not ours: leave it
        elif now - stat.st_mtime > KEEP_SECONDS:
            _delete(path, f"older than {KEEP_SECONDS // 86400} days")
        else:
            keep.append((path, stat))
    total = sum(stat.st_size for _, stat in keep)
    for path, stat in sorted(keep, key=lambda entry: entry[1].st_mtime):
        if total <= MAX_FOLDER_BYTES:
            break
        if _delete(path, f"folder over {MAX_FOLDER_BYTES // 1024 // 1024} MB"):
            total -= stat.st_size


def _delete(path, reason):
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError as error:
        log.error("couldn't delete %s: %r", path.name, error)
        return False
    log.info("deleted %s: %s", path.name, reason)
    return True


def start_sweeping():
    """Sweep now and every SWEEP_SECONDS, in the background, while the app runs."""
    def loop():
        while True:
            try:
                sweep()
            except Exception:  # a failed sweep must not end the loop
                log.exception("sweep failed")
            time.sleep(SWEEP_SECONDS)
    threading.Thread(target=loop, name="attachment-sweep", daemon=True).start()
