"""What Ctrl+V on the Talk to Lago page attaches, read from the system clipboard.

WebKitGTK hands the page little of the clipboard: a screenshot or an image copied in a
browser, or files copied in the file manager, often arrive as nothing or as a path. So,
as Claude Code does, the app reads the clipboard itself (GTK, so it works on Wayland
and X11), in this order: image data; copied files; text that is only paths to existing
pictures. Must run on the GTK main thread. The files are checked again when sent
(attachments.save), so this only gathers them.
"""

import base64
import mimetypes
import urllib.parse
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def files():
    """[{"name", "data": data URL}] from the clipboard, or [] when it holds none."""
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    ok, atoms = clipboard.wait_for_targets()
    targets = [atom.name() for atom in atoms] if ok and atoms else []
    image = _image(clipboard, targets)
    if image:
        return [image]
    paths = _copied_paths(clipboard, targets)
    if not paths and ("text/plain" in targets or "UTF8_STRING" in targets):
        paths = _image_paths(clipboard.wait_for_text() or "")
    return [item for item in (_read(path) for path in paths[:MAX_FILES]) if item]


def _image(clipboard, targets):
    """A picture copied as data (a screenshot, an image copied from a page)."""
    for kind in IMAGE_TYPES:
        if kind in targets:
            data = clipboard.wait_for_contents(Gdk.Atom.intern(kind, False))
            raw = data.get_data() if data else None
            if raw and len(raw) <= MAX_BYTES:
                return {"name": f"pasted-image.{kind.split('/')[1].replace('jpeg', 'jpg')}",
                        "data": _data_url(kind, raw)}
    if any(t.startswith("image/") for t in targets):  # another format: GTK converts it
        pixbuf = clipboard.wait_for_image()
        if pixbuf:
            ok, raw = pixbuf.save_to_bufferv("png", [], [])
            if ok and len(raw) <= MAX_BYTES:
                return {"name": "pasted-image.png", "data": _data_url("image/png", raw)}
    return None


def _copied_paths(clipboard, targets):
    """Files copied in a file manager (GNOME's own list first, then a URI list)."""
    text = ""
    if "x-special/gnome-copied-files" in targets:
        data = clipboard.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
        text = "\n".join((data.get_data() or b"").decode("utf-8", "replace").splitlines()[1:]) if data else ""
    elif "text/uri-list" in targets:
        text = "\n".join(clipboard.wait_for_uris() or [])
    return [p for p in (_path(line) for line in text.splitlines()) if p]


def _image_paths(text):
    """Text that is nothing but paths (or file:// links) to pictures that exist."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    paths = [_path(line) for line in lines]
    if not lines or not all(paths) or not all(p.suffix.lower() in IMAGE_SUFFIXES for p in paths):
        return []
    return paths


def _path(text):
    text = text.strip()
    if text.startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    if not text.startswith("/"):
        return None
    path = Path(text)
    return path if path.is_file() else None


def _read(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {"name": path.name, "data": _data_url(kind, raw)}


def _data_url(kind, raw):
    return f"data:{kind};base64,{base64.b64encode(raw).decode()}"
