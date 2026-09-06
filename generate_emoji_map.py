"""Regenerate web/static/vendor/emoji-map.json from web/static/vendor/emoji-data.json.

emoji-data.json is the iamcal/emoji-data dataset (https://github.com/iamcal/emoji-data,
emoji.json) committed as-is. This script flattens it into a plain {name: "<unicode>"}
map — every short_name and short_names alias for every entry — which is what
web/static/app.js actually fetches at runtime. Re-run this after pulling a newer
emoji-data.json.
"""
import json
from pathlib import Path

SRC = Path(__file__).resolve().parent / "web" / "static" / "vendor" / "emoji-data.json"
DEST = Path(__file__).resolve().parent / "web" / "static" / "vendor" / "emoji-map.json"


def _unicode_for(unified: str) -> str:
    return "".join(chr(int(cp, 16)) for cp in unified.split("-"))


def main() -> None:
    data = json.loads(SRC.read_text())
    result: dict[str, str] = {}
    for entry in data:
        glyph = _unicode_for(entry["unified"])
        names = set(entry.get("short_names") or [])
        if entry.get("short_name"):
            names.add(entry["short_name"])
        for name in names:
            result[name] = glyph
    DEST.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"Wrote {len(result)} emoji names to {DEST}")


if __name__ == "__main__":
    main()
