#!/usr/bin/env python3
"""Delete AAE and MOV sidecars for recycled JPG/HEIC originals."""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List

ORIGINAL_EXTENSIONS = {".jpg", ".jpeg", ".heic"}
SIDECAR_EXTENSIONS = (".AAE", ".MOV")


def _iter_originals(recycle_root: Path, verbose: bool) -> Iterable[Path]:
    """Yield original media files relative to the recycle root."""
    for dirpath, dirnames, filenames in os.walk(recycle_root):
        dirnames.sort()
        filenames.sort()
        current_dir = Path(dirpath)
        try:
            relative_dir = current_dir.relative_to(recycle_root)
        except ValueError:
            continue
        if verbose:
            display_dir = (
                "." if relative_dir == Path(".") else str(relative_dir)
            )
            print(f"[ENTER] {display_dir}")
        for filename in filenames:
            entry = current_dir / filename
            if entry.suffix.lower() not in ORIGINAL_EXTENSIONS:
                continue
            try:
                yield entry.relative_to(recycle_root)
            except ValueError:
                continue
        if verbose:
            print(f"[LEAVE] {display_dir}")


def _case_insensitive_matches(
    target: Path,
    cache: Dict[Path, Dict[str, List[Path]]],
) -> List[Path]:
    """Return all files that match the target name, ignoring case."""
    parent = target.parent
    expected = target.name.casefold()

    if parent in cache:
        return cache[parent].get(expected, [])

    mapping: Dict[str, List[Path]] = {}
    if parent.exists() and parent.is_dir():
        try:
            for candidate in parent.iterdir():
                if not candidate.is_file():
                    continue
                key = candidate.name.casefold()
                mapping.setdefault(key, []).append(candidate)
        except OSError as exc:  # pragma: no cover - defensive guard
            print(
                f"[WARN] Unable to read directory {parent}: {exc}",
                file=sys.stderr,
            )
    cache[parent] = mapping
    return mapping.get(expected, [])


def delete_sidecars(
    recycle_root: Path,
    primary_root: Path,
    execute: bool,
    verbose: bool,
) -> None:
    originals_found = 0
    sidecars_found = 0
    sidecars_deleted = 0
    cache: Dict[Path, Dict[str, List[Path]]] = {}
    print(f"[INFO] recycle-root={recycle_root}")
    print(f"[INFO] primary-root={primary_root}")
    print(f"[INFO] execute={execute}")
    for relative_path in _iter_originals(recycle_root, verbose=verbose):
        originals_found += 1
        source_hint = recycle_root / relative_path
        if verbose:
            print(f"[ORIGINAL] {relative_path}")
        for suffix in SIDECAR_EXTENSIONS:
            candidate = (primary_root / relative_path).with_suffix(suffix)
            matches = _case_insensitive_matches(candidate, cache)
            if not matches:
                continue
            sidecars_found += len(matches)
            for match in matches:
                action = "DELETE" if execute else "FOUND"
                print(f"[{action}] {match}")
                if execute:
                    try:
                        match.unlink()
                        sidecars_deleted += 1
                    except OSError as exc:
                        print(
                            f"[ERROR] Failed to delete {match}: {exc}",
                            file=sys.stderr,
                        )
        if originals_found % 500 == 0:
            print(
                f"[INFO] Processed {originals_found} originals "
                f"(last {source_hint})"
            )
    print(
        f"[INFO] originals={originals_found} sidecars={sidecars_found} "
        f"deleted={sidecars_deleted if execute else 0} execute={execute}"
    )


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan recycled JPG/HEIC originals and delete matching AAE/MOV "
            "files from the live library."
        )
    )
    parser.add_argument(
        "--recycle-root",
        default=r"\\\\10.8.28.10\home\#recycle\PhotoSync\iOS1",
        help="Root directory containing recycled media (default: %(default)s)",
    )
    parser.add_argument(
        "--primary-root",
        default=r"\\\\10.8.28.10\home\PhotoSync\iOS1",
        help="Root directory of the live media library (default: %(default)s)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete the matched sidecar files (defaults to dry-run)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-directory and per-file tracing while scanning",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    recycle_root = Path(args.recycle_root)
    primary_root = Path(args.primary_root)

    if not recycle_root.exists():
        print(
            f"[ERROR] recycle root not found: {recycle_root}",
            file=sys.stderr,
        )
        return 1
    if not primary_root.exists():
        print(
            f"[ERROR] primary root not found: {primary_root}",
            file=sys.stderr,
        )
        return 1

    delete_sidecars(
        recycle_root,
        primary_root,
        execute=args.execute,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
