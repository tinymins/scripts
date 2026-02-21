from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
)

TARGET_EXTENSIONS = {".heic", ".jpg"}
EXTRA_EXTENSIONS = [".mov", ".aae"]
HASH_CHUNK_SIZE = 1 << 20  # 1 MiB
CACHE_DB_FILENAME = "remove_duplicate_photos.index.db"
CACHE_VERSION = 1
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR.parent / "cache"


class SignatureCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    root TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (root, path)
                )
                """
            )

        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'version'"
        ).fetchone()
        if not row or row[0] != CACHE_VERSION:
            logging.info(
                "Resetting cache %s due to version mismatch", self.path
            )
            with self.connection:
                self.connection.execute("DELETE FROM files")
                self.connection.execute("DELETE FROM metadata")
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('version', ?)",
                    (CACHE_VERSION,),
                )

    def load_files(self, root: Path) -> Dict[str, Dict[str, Any]]:
        cursor = self.connection.execute(
            "SELECT path, size, mtime_ns, digest FROM files WHERE root = ?",
            (str(root),),
        )
        return {
            row[0]: {"size": row[1], "mtime_ns": row[2], "digest": row[3]}
            for row in cursor
        }

    def store_files(
        self,
        root: Path,
        files: Dict[str, Dict[str, Any]],
        previous: Dict[str, Dict[str, Any]],
        complete: bool,
    ) -> None:
        root_key = str(root)
        to_delete = set()
        if complete:
            to_delete = set(previous) - set(files)

        rows = [
            (
                root_key,
                path_str,
                metadata["size"],
                metadata["mtime_ns"],
                metadata["digest"],
            )
            for path_str, metadata in files.items()
        ]

        with self.connection:
            if to_delete:
                self.connection.executemany(
                    "DELETE FROM files WHERE root = ? AND path = ?",
                    ((root_key, path_str) for path_str in to_delete),
                )
            if rows:
                self.connection.executemany(
                    """
                    INSERT INTO files (root, path, size, mtime_ns, digest)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(root, path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        digest = excluded.digest
                    """,
                    rows,
                )

    def close(self) -> None:
        self.connection.close()


def iter_media_files(root: Path, extensions: Sequence[str]) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in extensions:
            yield path


def file_size(path: Path) -> int:
    return path.stat().st_size


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_signature_index(
    root: Path, cache: SignatureCache
) -> Dict[int, Dict[str, List[Path]]]:
    cached_files = cache.load_files(root)
    updated_cache: Dict[str, Dict[str, Any]] = {}

    signatures: Dict[int, Dict[str, List[Path]]] = {}
    grouped: Dict[int, Dict[str, List[Path]]] = defaultdict(
        lambda: defaultdict(list)
    )

    processed_all = False
    try:
        for path in iter_media_files(root, TARGET_EXTENSIONS):
            resolved = str(path.resolve())
            try:
                stat_result = path.stat()
            except OSError as exc:
                logging.warning("Skip %s: %s", path, exc)
                continue

            size = stat_result.st_size
            mtime_ns = getattr(stat_result, "st_mtime_ns", None)
            if mtime_ns is None:
                mtime_ns = int(stat_result.st_mtime * 1_000_000_000)

            cached = cached_files.get(resolved)
            digest: Optional[str] = None
            if cached:
                if (
                    cached.get("size") == size
                    and cached.get("mtime_ns") == mtime_ns
                ):
                    cached_digest = cached.get("digest")
                    if isinstance(cached_digest, str):
                        digest = cached_digest
                        logging.debug("Cache hit for %s", path)

            if not digest:
                try:
                    digest = hash_file(path)
                except OSError as exc:
                    logging.warning("Hash failed for %s: %s", path, exc)
                    continue

            grouped[size][digest].append(path)
            updated_cache[resolved] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "digest": digest,
            }
        processed_all = True
    finally:
        cache.store_files(root, updated_cache, cached_files, processed_all)

    for size, digest_map in grouped.items():
        if digest_map:
            signatures[size] = dict(digest_map)

    return signatures


def matching_extra_files(path: Path) -> Iterable[Path]:
    stem = path.with_suffix("")
    for ext in EXTRA_EXTENSIONS:
        for variant in {ext, ext.upper()}:
            extra = stem.with_suffix(variant)
            if extra.exists():
                yield extra


def delete_path(path: Path, dry_run: bool) -> None:
    if not path.exists():
        logging.debug("Already gone: %s", path)
        return
    logging.info("Deleting %s", path)
    if dry_run:
        return
    try:
        path.unlink()
    except OSError as exc:
        logging.error("Unable to delete %s: %s", path, exc)


def process(
    source: Path, target: Path, dry_run: bool, cache: SignatureCache
) -> None:
    logging.info("Indexing source %s", source)
    signatures = build_signature_index(source, cache)
    logging.info("Indexed %d unique file sizes", len(signatures))

    removed_count = 0
    scanned_count = 0

    for file_path in iter_media_files(target, TARGET_EXTENSIONS):
        scanned_count += 1
        try:
            size = file_size(file_path)
        except OSError as exc:
            logging.warning("Skip %s: %s", file_path, exc)
            continue

        digest_map: Optional[Mapping[str, List[Path]]] = signatures.get(size)
        if not digest_map:
            continue

        try:
            digest = hash_file(file_path)
        except OSError as exc:
            logging.warning("Hash failed for %s: %s", file_path, exc)
            continue

        source_paths = digest_map.get(digest)
        if source_paths:
            logging.info(
                "Duplicate found: %s matches %s",
                file_path,
                source_paths[0],
            )
            delete_path(file_path, dry_run)
            for extra in matching_extra_files(file_path):
                delete_path(extra, dry_run)
            removed_count += 1

    logging.info("Scanned %d candidate files in target", scanned_count)
    if dry_run:
        logging.info("Dry run enabled, no files were deleted")
    else:
        logging.info("Deleted %d duplicate files", removed_count)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Delete HEIC/JPG files from the target directory when contents "
            "match files stored in the source directory."
        )
    )
    parser.add_argument(
        "--source",
        default=r"\\\\10.8.28.10\\home\\PhotoSync\\iPhone",
        type=Path,
        help="Directory to keep (default: network share path).",
    )
    parser.add_argument(
        "--target",
        default=r"\\\\10.8.28.10\\home\\PhotoSync\\iOS1",
        type=Path,
        help="Directory to clean (default: network share path).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log deletions without removing files.",
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

    source: Path = args.source
    target: Path = args.target

    if not source.exists():
        parser.error(f"Source path does not exist: {source}")
    if not target.exists():
        parser.error(f"Target path does not exist: {target}")

    source = source.resolve()
    target = target.resolve()

    logging.info("Using source: %s", source)
    logging.info("Using target: %s", target)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = SignatureCache(CACHE_DIR / CACHE_DB_FILENAME)

    try:
        process(source, target, args.dry_run, cache)
    finally:
        cache.close()


if __name__ == "__main__":
    main()
