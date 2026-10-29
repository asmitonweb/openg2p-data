#!/usr/bin/env python3
"""Render themes/oan_theme/theme.json into registry db-seed SQL for one registry.

    build_theme_sql.py <registry>            # SQL on stdout
    build_theme_sql.py --check               # validate every registry entry

The SQL upserts the theme and its values (the background image is stored as the
theme's `dashboard_image` data URI, the only image attribute the registry
platform's theme model has) and points the registry's configuration row at it.
A registry entry with a `logo` also sets the configuration row's
`registry_logo` to that image's data URI.
Ids are derived from the theme id, so re-running is idempotent. Pure standard
library: it runs inside the registry db-seed image.
"""
import base64
import json
import sys
import uuid
from pathlib import Path

THEME_DIR = Path(__file__).resolve().parents[2] / "themes" / "oan_theme"
MAX_IMAGE_BYTES = 300 * 1024
COLOUR_KEYS = {
    "primary_color_1", "primary_color_2", "secondary_color_1", "secondary_color_2",
    "secondary_color_3", "neutral_color_1", "neutral_color_2",
}


def load():
    return json.loads((THEME_DIR / "theme.json").read_text(encoding="utf-8"))


def lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def data_uri(path):
    raw = path.read_bytes()
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    else:
        raise SystemExit(f"{path}: not a JPEG or PNG")
    if len(raw) > MAX_IMAGE_BYTES:
        raise SystemExit(f"{path}: {len(raw)} bytes exceeds {MAX_IMAGE_BYTES}")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def values(theme, registry):
    entry = theme["registries"][registry]
    if set(entry["colours"]) != COLOUR_KEYS:
        raise SystemExit(f"{registry}: colours must be exactly {sorted(COLOUR_KEYS)}")
    rows = {"font_family": "", "font_url": "", **entry["colours"],
            "dashboard_image": data_uri(THEME_DIR / entry["image"])}
    theme_id = uuid.UUID(theme["theme_id"])
    return [(str(uuid.uuid5(theme_id, f"{registry}/{name}")), name, value)
            for name, value in sorted(rows.items())]


def render(theme, registry):
    theme_id = theme["theme_id"]
    rows = ",\n".join(f"({lit(i)},{lit(theme_id)},{lit(n)},{lit(v)})" for i, n, v in values(theme, registry))
    return (
        f'INSERT INTO "public"."registry_themes" ("theme_id","theme_mnemonic","is_factory_shipped") '
        f"VALUES ({lit(theme_id)},{lit(theme['mnemonic'])},'FALSE')\n"
        'ON CONFLICT ("theme_id") DO UPDATE SET "theme_mnemonic" = EXCLUDED."theme_mnemonic";\n\n'
        'INSERT INTO "public"."registry_theme_values" ("theme_value_id","theme_id","attribute_name","attribute_value") VALUES\n'
        f"{rows}\n"
        'ON CONFLICT ("theme_value_id") DO UPDATE SET "attribute_value" = EXCLUDED."attribute_value";\n\n'
        f'UPDATE "public"."g2p_registry_configuration" SET "registry_theme_id" = {lit(theme_id)};\n'
        + logo(theme, registry)
    )


def logo(theme, registry):
    path = theme["registries"][registry].get("logo")
    if not path:
        return ""
    return f'UPDATE "public"."g2p_registry_configuration" SET "registry_logo" = {lit(data_uri(THEME_DIR / path))};\n'


def main(argv):
    theme = load()
    if argv == ["--check"]:
        for registry in theme["registries"]:
            render(theme, registry)
        print(f"ok: {', '.join(theme['registries'])}")
    elif len(argv) == 1 and argv[0] in theme["registries"]:
        sys.stdout.write(render(theme, argv[0]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
