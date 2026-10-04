from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


def choose(prompt: str, options: Sequence[str]) -> int:
    if not options:
        raise ValueError("No options available.")
    for idx, option in enumerate(options):
        print(f"[{idx}] {option}")
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("Enter one of the listed numbers.")
            continue
        if 0 <= value < len(options):
            return value
        print("Enter one of the listed numbers.")


def choose_file(directory: Path, suffixes: tuple[str, ...], label: str) -> Path:
    files = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in suffixes],
        key=lambda p: p.name.lower(),
    )
    if not files:
        raise FileNotFoundError(f"No {label} found in '{directory.name}/'.")
    if len(files) == 1:
        print(f"Using {label}: {files[0].name}")
        return files[0]
    return files[choose(f"Select {label}", [p.name for p in files])]


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").lower()
    return value or "operation"


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
