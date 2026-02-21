import json
import sys

import piexif  # type: ignore

try:  # Prefer pillow-heif because it ships libheif binaries on Windows.
    from pillow_heif import read_heif  # type: ignore
except ImportError:
    read_heif = None

try:
    import pyheif  # type: ignore
except ImportError:
    pyheif = None


def _iter_exif_payloads(path):
    if read_heif is not None:
        heif = read_heif(path)
        if hasattr(heif, "info"):
            exif_blob = heif.info.get("exif")
            if exif_blob:
                yield bytes(exif_blob)
        metadata = getattr(heif, "metadata", None)
        if metadata is None:
            metadata = None
            if hasattr(heif, "info"):
                metadata = heif.info.get("metadata")
        metadata = metadata or []
    elif pyheif is not None:
        metadata = pyheif.read(path).metadata or []
    else:
        raise RuntimeError(
            "Install pillow-heif or pyheif to read HEIF metadata."
        )

    for meta in metadata:
        if meta.get("type") != "Exif":
            continue
        raw = meta.get("data") or meta.get("payload")
        if raw is None:
            continue
        yield bytes(raw)


def _jsonify(value):
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def dump_exif(path):
    for payload in _iter_exif_payloads(path):
        exif = piexif.load(payload)
        print(json.dumps(_jsonify(exif), indent=2, ensure_ascii=False))
        return
    print("No Exif block found.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <image.heic>")
        sys.exit(1)
    try:
        dump_exif(sys.argv[1])
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
