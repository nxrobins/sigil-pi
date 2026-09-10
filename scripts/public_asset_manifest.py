"""Build-time public-file inventory only; no request or application decisions."""
import hashlib
import json
from pathlib import Path
import sys


PUBLIC_FILES = (
    ("/", "index.html", "text/html"),
    ("/_assets/app.mjs", "app.mjs", "text/javascript"),
    ("/_assets/api.mjs", "api.mjs", "text/javascript"),
    ("/_assets/styles.css", "styles.css", "text/css"),
)


def manifest(web_root):
    root = Path(web_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("public asset root must be a directory")
    assets = []
    for route, name, content_type in PUBLIC_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("public asset must be a regular non-symlink file")
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if not raw or len(raw) > 65536:
            raise ValueError("public asset must contain 1 through 65536 bytes")
        raw.decode("utf-8", errors="strict")
        assets.append({"route": route, "path": str(path),
                       "sha256": hashlib.sha256(raw).hexdigest(), "content_type": content_type})
    return {"version": 1, "assets": assets}


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: public_asset_manifest.py WEB_DIRECTORY")
    print(json.dumps(manifest(sys.argv[1]), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
