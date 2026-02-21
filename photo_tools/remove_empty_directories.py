import argparse
import os
import sys
from pathlib import Path


def remove_empty_directories(
    root: Path, dry_run: bool = False, verbose: bool = False
) -> list[Path]:
    """Remove every empty directory inside root (depth-first)."""
    removed: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(dirpath)

        if dirnames or filenames:
            # Directory is not empty, skip.
            continue

        if dry_run:
            removed.append(current)
            if verbose:
                print(f"DRY-RUN would remove: {current}")
            continue

        try:
            current.rmdir()
            removed.append(current)
            if verbose:
                print(f"Removed: {current}")
        except OSError as exc:
            if verbose:
                print(
                    f"Skip (error removing {current}): {exc}",
                    file=sys.stderr,
                )

    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete empty subdirectories below the provided root path."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=r"\\\\10.8.28.10\\home\\PhotoSync\\iOS1",
        help="Root directory to scan (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List directories that would be deleted without removing them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each directory as it is processed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root)

    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 1

    removed = remove_empty_directories(
        root, dry_run=args.dry_run, verbose=args.verbose
    )
    status = "found" if args.dry_run else "removed"
    print(f"Total empty directories {status}: {len(removed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
