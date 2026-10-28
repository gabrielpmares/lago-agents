#!/usr/bin/env python3
"""The personas' faces for the phone app, from the desktop's (OTTER_SVG, CHIP_SVG and ATLAS_SVG
in lago/ui/app.js, and Marina, Ada and Iris, the same in other colours: PERSONA_RECOLOURS), so
there is one drawing of each. Run it after changing a face:

    python3 persona-faces.py

Each face is split into the layers the app moves on its own (PersonaFace.kt): the face, its
eyes (they blink), smile, open mouth (it talks), Chip's antenna bulb and Atlas's window glow.
The ring and the asleep eyes are left out; the app draws its own rings.
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "lago" / "ui" / "app.js"
OUT = HERE / "app" / "src" / "main" / "res" / "drawable"

FACES = {"lago": "OTTER_SVG", "chip": "CHIP_SVG", "atlas": "ATLAS_SVG"}
# styles.css: the otter's fur is the accent, his eyes and nose a dark ink, his muzzle a pale fur.
FUR = "#4FB89E"
INK = "#0C1915"


def muzzle(fur):
    """A pale fur: 28% of it over white, as styles.css mixes it."""
    return "#" + "".join(f"{round(int(fur[i:i + 2], 16) * 0.28 + 255 * 0.72):02X}" for i in (1, 3, 5))


LAYERS = ("eyes", "smile", "mouth", "bulb", "glow")
DROP = {"ring", "shut"}
CLASS_LAYER = {"eyes": "eyes", "smile": "smile", "open": "mouth", "bulb": "bulb", "glow": "glow"}


def svg_text(name, source):
    found = re.search(r"const " + name + r" = `(.*?)`;", source, re.S)
    if not found:
        raise SystemExit(f"{name} isn't in app.js any more")
    return found.group(1)


def recolours(source):
    found = re.search(r"const PERSONA_RECOLOURS = (\{.*?\n\});", source, re.S)
    return json.loads(found.group(1)) if found else {}


def number(value):
    return f"{float(value):g}"


def shape(element):
    """The element as path data."""
    tag, a = element.tag, element.attrib
    if tag == "path":
        return a["d"]
    if tag == "circle":
        cx, cy, r = (float(a[k]) for k in ("cx", "cy", "r"))
        return (f"M{cx - r:g},{cy:g}a{r:g},{r:g} 0 1,0 {2 * r:g},0a{r:g},{r:g} 0 1,0 {-2 * r:g},0z")
    if tag == "rect":
        x, y, w, h = (float(a[k]) for k in ("x", "y", "width", "height"))
        r = min(float(a.get("rx", 0)), w / 2, h / 2)
        if not r:
            return f"M{x:g},{y:g}h{w:g}v{h:g}h{-w:g}z"
        return (f"M{x + r:g},{y:g}h{w - 2 * r:g}a{r:g},{r:g} 0 0 1 {r:g},{r:g}v{h - 2 * r:g}"
                f"a{r:g},{r:g} 0 0 1 {-r:g},{r:g}h{-(w - 2 * r):g}a{r:g},{r:g} 0 0 1 {-r:g},{-r:g}"
                f"v{-(h - 2 * r):g}a{r:g},{r:g} 0 0 1 {r:g},{-r:g}z")
    raise SystemExit(f"a <{tag}> in a face: teach persona-faces.py to draw it")


def style(element, inherited, fur):
    """The paint for an element: its own attributes, its classes (styles.css), then its group's."""
    paint = dict(inherited)
    classes = set(element.get("class", "").split())
    if "ink" in classes:
        paint["fill"] = INK
    if "muzzle" in classes:
        paint["fill"] = muzzle(fur)
    if "ink-line" in classes:
        paint.update(fill="none", stroke=INK, linecap="round")
    for attr, key in (("fill", "fill"), ("stroke", "stroke"), ("stroke-width", "width"),
                      ("fill-opacity", "fill_alpha"), ("stroke-opacity", "stroke_alpha"),
                      ("stroke-linecap", "linecap")):
        if element.get(attr) is not None:
            paint[key] = element.get(attr)
    if paint.get("fill") == "currentColor":
        paint["fill"] = fur
    return paint


def walk(element, paint, layer, out, fur=FUR):
    classes = set(element.get("class", "").split())
    if classes & DROP:
        return
    layer = next((CLASS_LAYER[c] for c in classes if c in CLASS_LAYER), layer)
    paint = style(element, paint, fur)
    if element.tag in ("svg", "g"):
        for child in element:
            walk(child, paint, layer, out, fur)
        return
    out.setdefault(layer, []).append((shape(element), paint))


def path_xml(data, paint):
    attrs = [f'android:pathData="{data}"']
    fill = paint.get("fill", "#000000")
    if fill != "none":
        attrs.append(f'android:fillColor="{fill.upper()}"')
        if "fill_alpha" in paint:
            attrs.append(f'android:fillAlpha="{number(paint["fill_alpha"])}"')
    if paint.get("stroke", "none") != "none":
        attrs.append(f'android:strokeColor="{paint["stroke"].upper()}"')
        attrs.append(f'android:strokeWidth="{number(paint.get("width", 1))}"')
        if "stroke_alpha" in paint:
            attrs.append(f'android:strokeAlpha="{number(paint["stroke_alpha"])}"')
        if paint.get("linecap"):
            attrs.append(f'android:strokeLineCap="{paint["linecap"]}"')
    return "    <path " + "\n        ".join(attrs) + " />"


def vector(paths):
    body = "\n".join(path_xml(d, p) for d, p in paths)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            "<!-- Made by android/persona-faces.py from lago/ui/app.js: change the face there. -->\n"
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:width="48dp" android:height="48dp"\n'
            '    android:viewportWidth="48" android:viewportHeight="48">\n'
            f"{body}\n</vector>\n")


def main():
    source = APP_JS.read_text()
    drawings = {persona: (svg_text(constant, source), FUR) for persona, constant in FACES.items()}
    for persona, recolour in recolours(source).items():
        svg = svg_text(FACES[recolour["face"]], source)
        for old, new in recolour.get("colours", {}).items():
            svg = svg.replace(old, new)
        drawings[persona] = (svg, recolour.get("fur", FUR))
    for persona, (svg, fur) in drawings.items():
        layers = {}
        walk(ET.fromstring(svg), {}, "face", layers, fur)
        for old in OUT.glob(f"persona_{persona}_*.xml"):
            old.unlink()
        for layer, paths in layers.items():
            target = OUT / f"persona_{persona}_{layer}.xml"
            target.write_text(vector(paths))
            print(target.relative_to(HERE))


if __name__ == "__main__":
    main()
