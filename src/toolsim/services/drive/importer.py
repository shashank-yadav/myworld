"""Google Drive from a folder: a Drive Takeout (Takeout/Drive) or any directory tree.

Folders stay folders. Text-like files keep their content; .docx text is extracted; other files
keep name, type and size. Everything is owned by the importing user (Takeout has no sharing data).
"""

from __future__ import annotations

import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any

from ...importers.common import ImportOptions

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".htm", ".xml", ".log", ".py", ".js",
            ".ts", ".sql", ".ini", ".cfg", ".toml"}
GOOGLE_TYPES = {".docx": "doc", ".gdoc": "doc", ".xlsx": "sheet", ".gsheet": "sheet", ".pptx": "slides", ".gslides": "slides"}
MAX_BYTES = 200_000
MAX_FILES = 1000


def _docx_text(p: Path) -> str | None:
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml).strip()


def import_folder(path: str | Path, opts: ImportOptions | None = None, *, owner: str = "me@example.com") -> dict[str, Any]:
    opts = opts or ImportOptions()
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    opts.home = opts.home or owner.split("@")[-1]
    email, _ = opts.person(owner)
    folders, files = [], []
    keys: dict[Path, str] = {}
    for d in sorted(p for p in root.rglob("*") if p.is_dir() and not p.name.startswith(".")):
        key = f"f{len(keys) + 1}"
        keys[d] = key
        entry: dict[str, Any] = {"key": key, "name": opts.text(d.name)}
        if d.parent != root:
            entry["parent"] = keys[d.parent]
        folders.append(entry)
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))[:MAX_FILES]:
        ext = f.suffix.lower()
        entry = {"name": opts.text(f.stem if ext in GOOGLE_TYPES else f.name)}
        if f.parent != root:
            entry["parent"] = keys[f.parent]
        if ext in GOOGLE_TYPES:
            entry["type"] = GOOGLE_TYPES[ext]
            text = _docx_text(f) if ext == ".docx" else None
            if text:
                entry["content"] = opts.text(text[:MAX_BYTES])
            else:
                entry["size"] = f.stat().st_size
        elif ext in TEXT_EXT and f.stat().st_size <= MAX_BYTES:
            entry["mimeType"] = mimetypes.guess_type(f.name)[0] or "text/plain"
            entry["content"] = opts.text(f.read_text(errors="replace"))
        else:
            entry["mimeType"] = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            entry["size"] = f.stat().st_size
        files.append(entry)
    opts.save_map()
    return {"user": {"email": email}, "domain": email.split("@")[-1], "folders": folders, "files": files}
