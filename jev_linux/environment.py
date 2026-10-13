"""Keep Snap editor runtime paths out of native Ubuntu desktop launches."""

import os


def native_environment(source):
    result = dict(source)
    for key in ("GTK_PATH", "GIO_MODULE_DIR", "GDK_PIXBUF_MODULE_FILE", "GSETTINGS_SCHEMA_DIR"):
        if "/snap/" in result.get(key, ""):
            result.pop(key, None)
    if "XDG_DATA_DIRS" in result:
        paths = [path for path in result["XDG_DATA_DIRS"].split(":") if path and "/snap/" not in path]
        result["XDG_DATA_DIRS"] = ":".join(paths) or "/usr/local/share:/usr/share"
    return result


def clean_editor_environment():
    cleaned = native_environment(os.environ)
    for key in set(os.environ) - set(cleaned):
        os.environ.pop(key, None)
    os.environ.update(cleaned)
