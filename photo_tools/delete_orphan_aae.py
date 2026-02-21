"""Delete orphaned .AAE sidecar files when no matching original exists.

Scan all subdirectories under a given root, look for .AAE files, and delete any
that do not have a sibling JPG, PNG, or HEIC file sharing the same stem. The
script can run in dry-run mode to preview actions before deletion.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Iterable, Iterator


ORIGINAL_EXTENSIONS = (".jpg", ".png", ".heic")


def iter_orphan_sidecars(directory: Path) -> Iterator[Path]:
    """Yield .AAE files under *directory* that lack matching originals."""
    for dirpath, _, filenames in os.walk(directory):
        if not filenames:
            continue

        lowercase_lookup = {name.lower(): name for name in filenames}
        for name in filenames:
            if not name.lower().endswith(".aae"):
                continue

            stem = name[:-4]  # remove .AAE
            match = _find_matching_original(stem, lowercase_lookup)
            if match is not None:
                logging.debug(
                    "Keeping %s; found matching original %s",
                    Path(dirpath, name),
                    Path(dirpath, match),
                )
                continue

            yield Path(dirpath, name)


def _find_matching_original(
    stem: str, lowercase_lookup: dict[str, str]
) -> str | None:
    """Return a matching original filename for *stem*, or ``None``."""
    for extension in ORIGINAL_EXTENSIONS:
        candidate = f"{stem}{extension}"
        actual_name = lowercase_lookup.get(candidate.lower())
        if actual_name:
            return actual_name
    return None


def delete_files(files: Iterable[Path], dry_run: bool) -> int:
    """Delete *files*, respecting dry-run mode, and return delete count."""
    removed = 0
    for path in files:
        logging.info("Deleting %s", path)
        if dry_run:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logging.error("Failed to delete %s: %s", path, exc)
    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        type=Path,
        help="Root directory to scan for orphan .AAE files.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete files. Default is dry run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Increase logging verbosity.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    root = args.root
    if not root.exists():
        raise SystemExit(f"Root directory not found: {root}")
    if not root.is_dir():
        raise SystemExit(f"Root path is not a directory: {root}")

    dry_run = not args.execute
    if dry_run:
        logging.info("Dry run enabled; no files will be deleted")

    orphans = list(iter_orphan_sidecars(root))
    if not orphans:
        logging.info("No orphan .AAE files found under %s", root)
        return

    logging.info("Found %d orphan .AAE file(s)", len(orphans))
    deleted = delete_files(orphans, dry_run=dry_run)
    if dry_run:
        logging.info(
            "Preview complete. Re-run with --execute to delete %d file(s).",
            len(orphans),
        )
    else:
        logging.info("Deleted %d orphan .AAE file(s)", deleted)


if __name__ == "__main__":
    main()
