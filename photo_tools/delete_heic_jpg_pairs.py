from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Iterable


def iter_heic_files(root: Path) -> Iterable[Path]:
    """Yield every *.heic file under the root directory, case-insensitively."""
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".heic":
            yield path


def remove_matching_jpg(heic_path: Path, dry_run: bool) -> bool:
    """Delete the JPG that shares the same stem as the HEIC, if present."""
    stem = heic_path.with_suffix("")
    # Cover both JPG casings for case-preserving filesystems.
    candidates = [
        stem.with_suffix(".jpg"),
        stem.with_suffix(".JPG"),
    ]

    removed_any = False
    for jpg_path in candidates:
        if jpg_path.exists():
            logging.info("Deleting %s", jpg_path)
            if not dry_run:
                jpg_path.unlink()
            removed_any = True
    return removed_any


def process(root: Path, dry_run: bool) -> None:
    heic_count = 0
    deleted_count = 0

    for heic_path in iter_heic_files(root):
        heic_count += 1
        try:
            if remove_matching_jpg(heic_path, dry_run):
                deleted_count += 1
        # Defensive guard for unexpected IO errors in edge cases.
        except Exception as exc:  # pragma: no cover
            logging.exception("Failed to handle %s: %s", heic_path, exc)

    logging.info("Processed %d HEIC files", heic_count)
    if dry_run:
        logging.info("Dry run enabled, no files were deleted")
    else:
        logging.info("Deleted JPG siblings for %d HEIC files", deleted_count)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Delete JPG files that share the same name with HEIC files."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=r"\\\\10.8.28.10\\home\\PhotoSync\\iOS1",
        type=Path,
        help="Root directory to scan (default: network share path).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the JPG files that would be deleted without removing them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Increase logging verbosity.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    root: Path = args.root
    if not root.exists():
        parser.error(f"Root path does not exist: {root}")

    # Ensure we operate on a resolved absolute path for consistent logging.
    root = root.resolve()
    logging.info("Scanning %s", root)

    process(root, args.dry_run)


if __name__ == "__main__":
    main()
