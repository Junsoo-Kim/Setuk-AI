"""Create a release ZIP with portable forward-slash entry names."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def create_release_zip(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    create_release_zip(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
