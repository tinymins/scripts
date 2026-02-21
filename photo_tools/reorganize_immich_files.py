#!/usr/bin/env python3
"""Move Immich media files from YYYY-MM folders into YYYY/MM folders."""

from __future__ import annotations

import argparse
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_MONTH_FOLDER = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")
_NAME_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(?P<rest>.+)$"
)


@dataclass
class MovePlan:
    source: Path
    destination: Path


def build_move_plan(root: Path) -> list[MovePlan]:
    plans: list[MovePlan] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        match = _MONTH_FOLDER.match(entry.name)
        if not match:
            logging.debug("Skip non YYYY-MM folder: %s", entry)
            continue
        year = match.group("year")
        month = match.group("month")
        target_dir = root / year / month
        for file in sorted(entry.iterdir()):
            if not file.is_file():
                continue
            new_name = _strip_prefix(file.name)
            destination = _unique_destination(target_dir, new_name)
            plans.append(MovePlan(source=file, destination=destination))
    return plans


def _strip_prefix(filename: str) -> str:
    match = _NAME_PREFIX.match(filename)
    if match:
        return match.group("rest")
    logging.warning(
        "Filename did not match expected pattern, keeping original: %s",
        filename,
    )
    return filename


def _unique_destination(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        new_candidate = directory / f"{stem}_{counter}{suffix}"
        if not new_candidate.exists():
            return new_candidate
        counter += 1


def execute_moves(plans: list[MovePlan], dry_run: bool) -> None:
    for plan in plans:
        logging.info("%s -> %s", plan.source, plan.destination)
        if dry_run:
            continue
        plan.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan.source), str(plan.destination))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move media files from YYYY-MM folders into YYYY/MM folders, "
            "trimming the leading timestamp prefix."
        )
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root folder containing YYYY-MM subfolders",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned moves without changing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
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
        raise SystemExit(f"Root folder does not exist: {root}")
    plans = build_move_plan(root)
    if not plans:
        logging.info("No files to move")
        return
    execute_moves(plans, args.dry_run)


if __name__ == "__main__":
    main()
