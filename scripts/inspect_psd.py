#!/usr/bin/env python3
"""
Walk a PSD/PSDT file and print its layer tree with bboxes.

Used to explore Kim's template files so we know what layer names to
target when building the extractor.

Usage:
    python scripts/inspect_psd.py <path-to-psd>
"""

import sys
from pathlib import Path

from psd_tools import PSDImage
from psd_tools.constants import Resource


def walk(layer, depth=0):
    indent = "  " * depth
    bbox = getattr(layer, "bbox", None)
    kind = layer.kind
    name = layer.name
    visible = "v" if layer.visible else "-"
    extras = []
    if hasattr(layer, "smart_object") and layer.smart_object:
        extras.append(f"smart_object={layer.smart_object.filename or '?'}")
    if kind == "type":
        try:
            text = (layer.text or "").replace("\n", "\\n")
            extras.append(f'text="{text}"')
        except Exception:
            pass
    extras_s = "  " + " ".join(extras) if extras else ""
    print(f"{indent}[{visible}] {kind:10s} {name!r:45s} bbox={bbox}{extras_s}")

    if hasattr(layer, "__iter__"):
        try:
            for child in layer:
                walk(child, depth + 1)
        except TypeError:
            pass


def main():
    if len(sys.argv) < 2:
        print("usage: inspect_psd.py <path>")
        sys.exit(1)
    path = Path(sys.argv[1]).expanduser()
    psd = PSDImage.open(path)
    print(f"File:   {path}")
    print(f"Size:   {psd.width} x {psd.height}  ({psd.color_mode})")
    print(f"DPI:    ?")
    print()
    print("Layer tree (visible-flag, kind, name, bbox):")
    print("-" * 80)
    for layer in psd:
        walk(layer)


if __name__ == "__main__":
    main()
