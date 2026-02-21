#!/usr/bin/env python3
"""Scan QuickTime videos for incorrect creation metadata and optionally fix it.

The script looks for cases where QuickTime CreateDate/MediaCreateDate drift
from the true capture timestamp (preferring CreationDate/ContentCreateDate).
When "--apply" is supplied, it copies the trusted source timestamp back into
all relevant QuickTime time fields using exiftool.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# QuickTime date fields that influence downstream tools (e.g. mtphoto).
QT_TARGET_TAGS = [
    "QuickTime:CreateDate",
    "QuickTime:ModifyDate",
    "QuickTime:TrackCreateDate",
    "QuickTime:TrackModifyDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:MediaModifyDate",
]

# Date tags that usually keep the original capture moment even after edits.
PREFERRED_SOURCE_TAGS = [
    "Keys:CreationDate",
    "QuickTime:ContentCreateDate",
    "Composite:DateTimeOriginal",
    "EXIF:DateTimeOriginal",
    "File:FileModifyDate",
    "System:FileModifyDate",
    "FileModifyDate",
    "CreationDate",
    "MediaCreateDate",  # final fallback if nothing else is present
]

# File extensions we care about (case-insensitive).
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".hevc"}


class MetadataRecord:
    """Container for the parsed metadata of a single media file."""

    def __init__(self, path: Path, tags: Dict[str, str]):
        self.path = path
        self.tags = tags

    def get_datetime(self, tag: str) -> Optional[dt.datetime]:
        value = self.tags.get(tag)
        if value is None and ":" in tag:
            group, bare = tag.split(":", 1)
            value = self.tags.get(bare)
        return parse_exif_datetime(value)

    def primary_source(self) -> Optional[Tuple[str, dt.datetime]]:
        for tag in PREFERRED_SOURCE_TAGS:
            source_dt = self.get_datetime(tag)
            if source_dt is not None:
                return tag, source_dt
        return None

    def primary_targets(self) -> List[Tuple[str, dt.datetime]]:
        targets: List[Tuple[str, dt.datetime]] = []
        for tag in QT_TARGET_TAGS:
            parsed = self.get_datetime(tag)
            if parsed is not None:
                targets.append((tag, parsed))
        return targets


class RepairCandidate:
    """Represents a file whose QuickTime timestamps should be repaired."""

    def __init__(
        self,
        record: MetadataRecord,
        source_tag: str,
        source_value: dt.datetime,
        mismatched_targets: List[Tuple[str, dt.datetime]],
    ):
        self.record = record
        self.source_tag = source_tag
        self.source_value = source_value
        self.mismatched_targets = mismatched_targets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan QuickTime videos for incorrect creation dates "
            "and optionally fix them"
        ),
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root folder to scan (UNC paths supported)",
    )
    parser.add_argument(
        "--exiftool",
        default="exiftool",
        help="Path to exiftool executable (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply fixes in-place (default is dry-run)",
    )
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=24.0,
        help=(
            "How far targets may drift from the source timestamp before "
            "flagging (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--extensions",
        nargs="*",
        default=sorted(VIDEO_EXTENSIONS),
        help=(
            "Custom list of file extensions to process (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Reserved for future parallelism (currently unused)",
    )
    parser.add_argument(
        "--all",
        dest="slowmo_only",
        action="store_false",
        help="Process every matching file instead of only *_slowmo clips",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging for troubleshooting",
    )
    parser.set_defaults(slowmo_only=True)
    return parser.parse_args()


def parse_exif_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    value = value.strip()
    # Replace the first two colons with dashes to form an ISO-style date.
    if len(value) >= 10 and value[4] == ":" and value[7] == ":":
        iso_date = value[:10].replace(":", "-", 2)
        remainder = value[10:]
        candidate = f"{iso_date}{remainder}".strip()
    else:
        candidate = value

    # Ensure we have a "T" separator when a time part exists.
    if " " in candidate and "T" not in candidate:
        candidate = candidate.replace(" ", "T", 1)

    # Handle UTC shorthand.
    if candidate.endswith("Z") and "+" not in candidate:
        candidate = candidate[:-1] + "+00:00"

    try:
        parsed = dt.datetime.fromisoformat(candidate)
        return parsed
    except ValueError:
        pass

    # Fallback to common patterns without timezone info.
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def to_naive_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def load_metadata(exiftool: str, path: Path) -> Optional[MetadataRecord]:
    tag_args = [f"-{tag}" for tag in (*QT_TARGET_TAGS, *PREFERRED_SOURCE_TAGS)]
    command = [
        exiftool,
        "-json",
        "-G1",
        "-api",
        "QuickTimeUTC",
        *tag_args,
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print(
            "exiftool not found. Install exiftool or point --exiftool to its "
            "executable.",
            file=sys.stderr,
        )
        raise
    except subprocess.CalledProcessError as exc:
        print(f"exiftool failed on {path}: {exc}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"Failed to parse exiftool output for {path}: {exc}",
            file=sys.stderr,
        )
        return None

    if not data:
        return None
    tags = data[0]
    return MetadataRecord(path, tags)


def dump_debug(record: MetadataRecord) -> None:
    print(f"DEBUG {record.path}")
    seen: set[str] = set()
    for tag in PREFERRED_SOURCE_TAGS:
        value = record.tags.get(tag)
        if value is not None:
            print(f"  source[{tag}] = {value}")
            seen.add(tag)
    for tag in QT_TARGET_TAGS:
        raw = record.tags.get(tag)
        if raw is None and ":" in tag:
            _group, bare = tag.split(":", 1)
            raw = record.tags.get(bare)
            if raw is not None:
                seen.add(bare)
        if raw is None:
            continue
        seen.add(tag)
        parsed = record.get_datetime(tag)
        suffix = f" -> {parsed.isoformat()}" if parsed else ""
        print(f"  target[{tag}] = {raw}{suffix}")
    computed = record.primary_targets()
    if computed:
        for tag, parsed in computed:
            print(f"  computed[{tag}] = {parsed.isoformat()}")
    else:
        print("  computed targets: <empty>")
    extra = sorted(set(record.tags) - seen)
    if extra:
        sample = ", ".join(extra[:10])
        print(f"  ... additional tags: {sample}")


def find_mismatches(
    record: MetadataRecord,
    threshold: dt.timedelta,
    debug: bool = False,
) -> Optional[RepairCandidate]:
    source = record.primary_source()
    if source is None:
        return None

    source_tag, source_value = source
    targets = record.primary_targets()

    mismatched: List[Tuple[str, dt.datetime]] = []
    for tag, value in targets:
        src = to_naive_utc(source_value)
        tgt = to_naive_utc(value)
        delta = abs(src - tgt)
        if debug:
            print(
                "    delta {} vs {} = {}".format(
                    tag,
                    source_tag,
                    delta,
                )
            )
        if delta > threshold:
            mismatched.append((tag, value))

    if mismatched:
        return RepairCandidate(record, source_tag, source_value, mismatched)
    return None


def apply_fix(candidate: RepairCandidate, exiftool: str) -> bool:
    source_tag = candidate.source_tag
    path = candidate.record.path

    arguments = [
        exiftool,
        "-overwrite_original",
        "-P",
        "-api",
        "QuickTimeUTC",
    ]
    for tag in QT_TARGET_TAGS:
        arguments.append(f"-{tag}<{source_tag}")
    arguments.append(str(path))

    try:
        subprocess.run(arguments, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"Failed to repair {path}: {exc}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return False


def should_process(
    path: Path, extensions: Iterable[str], slowmo_only: bool
) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in {ext.lower() for ext in extensions}:
        return False
    if slowmo_only and "_slowmo" not in path.stem.lower():
        return False
    return True


def walk_media_files(
    root: Path, extensions: Iterable[str], slowmo_only: bool
) -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath, name)
            if should_process(candidate, extensions, slowmo_only):
                yield candidate


def resolve_exiftool(path_str: str) -> Optional[str]:
    candidate = Path(path_str)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(path_str)
    if resolved:
        return resolved
    exts = {".exe", ".bat", ".cmd"}
    for ext in exts:
        resolved = shutil.which(path_str + ext)
        if resolved:
            return resolved
    return None


def main() -> int:
    args = parse_args()
    root: Path = args.root

    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 1

    exiftool_path = resolve_exiftool(args.exiftool)
    if exiftool_path is None:
        print(
            "Cannot find exiftool executable. Install exiftool or provide "
            "--exiftool with the full path.",
            file=sys.stderr,
        )
        return 1

    threshold = dt.timedelta(hours=args.threshold_hours)
    candidates: List[RepairCandidate] = []

    print(f"Scanning {root} for QuickTime date inconsistencies...")
    if root.is_file():
        paths: Iterable[Path] = [root]
    else:
        paths = walk_media_files(root, args.extensions, args.slowmo_only)

    for path in paths:
        if not root.is_file() and not should_process(
            path, args.extensions, args.slowmo_only
        ):
            continue
        try:
            record = load_metadata(exiftool_path, path)
        except FileNotFoundError:
            return 1
        if record is None:
            continue
        if args.debug:
            dump_debug(record)
        mismatch = find_mismatches(record, threshold, args.debug)
        if mismatch is not None:
            candidates.append(mismatch)
            source_tag = mismatch.source_tag
            source_value = mismatch.source_value.isoformat()
            target_details = ", ".join(
                f"{tag}={value.isoformat()}"
                for tag, value in mismatch.mismatched_targets
            )
            print(
                "! {}: source {}={} | {}".format(
                    path, source_tag, source_value, target_details
                )
            )

    if not candidates:
        print("No inconsistencies detected. Nothing to do.")
        return 0

    print(
        "\nFound {} file(s) with mismatched QuickTime timestamps.".format(
            len(candidates)
        )
    )

    if not args.apply:
        print(
            "Dry-run completed. Re-run with --apply to repair the timestamps."
        )
        return 0

    print("Applying repairs...")
    failures = 0
    for candidate in candidates:
        if not apply_fix(candidate, exiftool_path):
            failures += 1

    if failures:
        print(f"Finished with {failures} failure(s). See logs above.")
        return 1

    print("Repairs completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
