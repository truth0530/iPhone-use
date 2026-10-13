"""Prepare a WDA screenshot for a model: bounded pixel size, JPEG, exact point scale.

WDA always answers /screenshot with a native-resolution PNG, often several megabytes.
Vision models downscale large images themselves, and each retained screenshot is sent
again with every later request. Scaling once here, to a size the common model limits
accept unchanged, keeps coordinates exact and removes most of the transferred bytes.
"""
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Common vision limits: the short side is kept at 768 px and the long side at 1568 px.
MAX_SHORT_SIDE = 768
MAX_LONG_SIDE = 1568
JPEG_QUALITY = 80
SIPS = "/usr/bin/sips"


def png_size(data):
    """Width and height from the PNG header, or None when it is not a complete PNG header."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if 0 < width <= 20000 and 0 < height <= 20000 else None


def fit_size(width, height, max_short=MAX_SHORT_SIDE, max_long=MAX_LONG_SIDE):
    """Largest size no bigger than the source that fits both model limits."""
    short, long_side = sorted((width, height))
    scale = min(1.0, max_short / short, max_long / long_side)
    return max(1, round(width * scale)), max(1, round(height * scale))


def enabled():
    """WDA_IMAGE=original returns the native PNG, for debugging legibility."""
    return os.environ.get("WDA_IMAGE", "").strip().lower() != "original"


def _read_exclusive(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return stream.read(limit)
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def model_image(source, quality=JPEG_QUALITY, timeout=10):
    """Write a downscaled JPEG next to the PNG at `source`.

    Returns (path, mime type, width, height). Any failure returns the original PNG:
    a screenshot is never lost because the local conversion was unavailable.
    """
    source = Path(source)
    header = _read_exclusive(source, 32)
    if header is None:
        raise OSError("Screenshot is not a regular unlinked file.")
    size = png_size(header)
    if size is None:
        return source, "image/png", None, None
    width, height = size
    target = fit_size(width, height)
    # An image already inside the limits stays the lossless original.
    if target == (width, height) or not enabled() or not os.access(SIPS, os.X_OK):
        return source, "image/png", width, height
    destination = source.with_suffix(".jpg")
    temporary = None
    try:
        # sips takes the resampled height before the width. Write our own inode, then rename.
        descriptor, temporary = tempfile.mkstemp(prefix=".image-", suffix=".jpg", dir=source.parent)
        os.close(descriptor)
        completed = subprocess.run(
            [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
             "-z", str(target[1]), str(target[0]), str(source), "--out", temporary],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
        produced = _read_exclusive(temporary, 4)
        if completed.returncode != 0 or produced is None or len(produced) < 4 or produced[:2] != b"\xff\xd8":
            raise OSError("sips did not produce an image")
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("sips output is aliased")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        temporary = None
        return destination, "image/jpeg", target[0], target[1]
    except (OSError, subprocess.SubprocessError):
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return source, "image/png", width, height
