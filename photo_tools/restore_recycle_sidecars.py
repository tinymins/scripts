#!/usr/bin/env python3
"""Restore sidecar files (.MOV/.AAE) from the recycle tree to the live library.

The script scans a primary media library for JPG/PNG/HEIC originals. For each
original, it checks whether matching MOV or AAE sidecars exist in a recycle
location using the same relative path. When a match is found, the sidecar is
moved back alongside the original.

By default the script runs in dry-run mode and only logs the actions that would
be taken. Pass ``--execute`` to perform the file moves.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, Iterator, List

ORIGINAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}
SIDECAR_EXTENSIONS = (".MOV", ".AAE")


def _iter_originals(primary_root: Path, verbose: bool) -> Iterator[Path]:
    """Yield original media files relative to *primary_root*."""
    for dirpath, dirnames, filenames in os.walk(primary_root):
        dirnames.sort()
        filenames.sort()
        current_dir = Path(dirpath)
        try:
            relative_dir = current_dir.relative_to(primary_root)
        except ValueError:
            continue
        if verbose:
            if relative_dir == Path("."):
                display_dir = "."
            else:
                display_dir = str(relative_dir)
            print(f"[ENTER] {display_dir}")
        for filename in filenames:
            entry = current_dir / filename
            if entry.suffix.lower() not in ORIGINAL_EXTENSIONS:
                continue
            try:
                yield entry.relative_to(primary_root)
            except ValueError:
                continue
        if verbose:
            print(f"[LEAVE] {display_dir}")


def _case_insensitive_lookup(
    directory: Path,
    target_name: str,
    cache: Dict[Path, Dict[str, List[Path]]],
) -> List[Path]:
    """Return case-insensitive matches in *directory* for *target_name*."""
    target_key = target_name.casefold()

    if directory in cache:
        return cache[directory].get(target_key, [])

    mapping: Dict[str, List[Path]] = {}
    if directory.exists() and directory.is_dir():
        try:
            for candidate in directory.iterdir():
                if not candidate.is_file():
                    continue
                key = candidate.name.casefold()
                mapping.setdefault(key, []).append(candidate)
        except OSError as exc:  # pragma: no cover - defensive guard
            print(
                f"[WARN] Unable to read directory {directory}: {exc}",
                file=sys.stderr,
            )
    cache[directory] = mapping
    return mapping.get(target_key, [])


def move_sidecars(
    primary_root: Path,
    recycle_root: Path,
    execute: bool,
    verbose: bool,
) -> None:

    originals_processed = 0
    sidecars_found = 0
    sidecars_moved = 0
    sidecars_skipped = 0
    cache: Dict[Path, Dict[str, List[Path]]] = {}

    print(f"[INFO] primary-root={primary_root}")
    print(f"[INFO] recycle-root={recycle_root}")
    print(f"[INFO] execute={execute}")

    for relative_original in _iter_originals(primary_root, verbose=verbose):
        originals_processed += 1
        original_path = primary_root / relative_original
        recycle_dir = recycle_root / relative_original.parent
        stem = original_path.stem
        if verbose:
            print(f"[ORIGINAL] {relative_original}")

        for suffix in SIDECAR_EXTENSIONS:
            candidate_name = f"{stem}{suffix}"
            matches = _case_insensitive_lookup(
                recycle_dir,
                candidate_name,
                cache,
            )
            if not matches:
                continue

            sidecars_found += len(matches)
            dest = original_path.with_name(candidate_name)
            for source in matches:
                if dest.exists():
                    print(f"[SKIP-EXISTS] {dest}")
                    sidecars_skipped += 1
                    continue

                action = "MOVE" if execute else "FOUND"
                print(f"[{action}] {source} -> {dest}")
                if not execute:
                    continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(dest))
                    sidecars_moved += 1
                except OSError as exc:
                    sidecars_skipped += 1
                    print(
                        f"[ERROR] Failed to move {source} -> {dest}: {exc}",
                        file=sys.stderr,
                    )

        if originals_processed % 500 == 0:
            print(
                f"[INFO] Processed {originals_processed} originals "
                f"(last {relative_original})"
            )

    print(
        "[INFO] originals={orig} sidecars-found={found} moved={moved} "
        "skipped={skipped} execute={exe}".format(
            orig=originals_processed,
            found=sidecars_found,
            moved=sidecars_moved if execute else 0,
            skipped=sidecars_skipped,
            exe=execute,
        )
    )


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan the live media library for JPG/PNG/HEIC originals and move"
            " matching MOV/AAE sidecar files from the recycle tree back into"
            " place."
        )
    )
    parser.add_argument(
        "--primary-root",
        default=r"\\\\10.8.28.10\home\PhotoSync\iOS1",
        help="Root directory of the live media library (default: %(default)s)",
    )
    parser.add_argument(
        "--recycle-root",
        default=r"\\\\10.8.28.10\home\#recycle\PhotoSync\iOS1",
        help="Root directory containing recycled media (default: %(default)s)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move the matched sidecar files (defaults to dry-run)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-directory and per-file tracing while scanning",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    primary_root = Path(args.primary_root)
    recycle_root = Path(args.recycle_root)

    if not primary_root.exists():
        print(
            f"[ERROR] primary root not found: {primary_root}",
            file=sys.stderr,
        )
        return 1
    if not recycle_root.exists():
        print(
            f"[ERROR] recycle root not found: {recycle_root}",
            file=sys.stderr,
        )
        return 1

    move_sidecars(
        primary_root=primary_root,
        recycle_root=recycle_root,
        execute=args.execute,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
