from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable


def find_dotfiles(root: Path) -> Iterable[Path]:
    """Yield files under *root* whose basename starts with a dot."""
    for path in root.rglob('*'):
        if path.is_file() and path.name.startswith('.'):
            yield path


def delete_paths(paths: Iterable[Path], dry_run: bool) -> int:
    """Delete each path yielded from *paths*; return count of deleted files."""
    deleted = 0
    for path in paths:
        if dry_run:
            print(f"[DRY-RUN] Would delete: {path}")
            deleted += 1
            continue
        try:
            path.unlink()
            print(f"Deleted: {path}")
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to delete {path}: {exc}", file=sys.stderr)
    return deleted


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete files whose names start with a dot (.) recursively under "
            "the given root."
        )
    )
    parser.add_argument(
        "root",
        type=Path,
        help=(
            "Root directory to scan. UNC paths are supported (e.g. "
            "\\\\server\\\\share\\\\path)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be deleted without removing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = args.root

    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"Root path is not a directory: {root}", file=sys.stderr)
        return 1

    dot_files = list(find_dotfiles(root))
    if not dot_files:
        print("No dot-prefixed files found.")
        return 0

    deleted = delete_paths(dot_files, dry_run=args.dry_run)
    if args.dry_run:
        print(f"Dry run finished; {deleted} files would be deleted.")
    else:
        print(f"Deleted {deleted} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
