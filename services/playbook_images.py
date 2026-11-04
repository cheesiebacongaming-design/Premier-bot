"""Validate and persist uploaded playbook images outside Discord's CDN."""

import hashlib
import io
import os
import re
import tempfile
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from services.playbook_store import PlaybookDataError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 8_000_000
_ASSET_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|webp)$")


def validate_image(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise PlaybookDataError("Upload a PNG, JPEG or WebP image of at most 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
                if not extension:
                    raise PlaybookDataError("Upload a PNG, JPEG or WebP image")
                if image.width < 1 or image.height < 1 or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise PlaybookDataError("Image must be no larger than 8 megapixels")
                if getattr(image, "n_frames", 1) != 1:
                    raise PlaybookDataError("Animated images are not supported")
                image.verify()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError) as exc:
        raise PlaybookDataError("The uploaded image is invalid or too large") from exc
    return f"{hashlib.sha256(data).hexdigest()}.{extension}"


def asset_path(store_path, asset_name):
    if not isinstance(asset_name, str) or not _ASSET_NAME.fullmatch(asset_name):
        raise PlaybookDataError("Invalid playbook image reference")
    return Path(store_path).parent / "playbook_images" / asset_name


def save_image(store_path, data):
    name = validate_image(data)
    path = asset_path(store_path, name)
    if path.is_file():
        return name
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temporary)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise PlaybookDataError("Could not save the uploaded image") from exc
    return name
