import io

import pytest
from PIL import Image

from services.playbook_images import MAX_IMAGE_BYTES, asset_path, validate_image
from services.playbook_service import PlaybookConflict, PlaybookDataError, PlaybookService


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (40, 50, 60)).save(output, "PNG")
    return output.getvalue()


def test_image_is_saved_with_strategy_and_preserved_through_edits(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    data = png_bytes()
    strategy = service.create({"name": "Execute", "map": "Haven", "side": "attack"}, data)
    asset = strategy["diagram"]["image_asset"]
    assert service.image_path(asset).read_bytes() == data
    updated = service.update(strategy["id"], 1, {**strategy, "objective": "Take A"})
    assert updated["diagram"]["image_asset"] == asset
    removed = service.update(strategy["id"], 2, {**updated, "diagram": None})
    assert removed["diagram"] is None
    assert service.image_path(asset).exists()


def test_stale_image_update_does_not_write_file(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    strategy = service.create({"name": "Execute", "map": "Haven", "side": "attack"})
    service.update(strategy["id"], 1, {**strategy, "objective": "Changed"})
    with pytest.raises(PlaybookConflict):
        service.update(strategy["id"], 1, strategy, png_bytes())
    assert not (tmp_path / "playbook_images").exists()


@pytest.mark.parametrize("data", [b"not an image", b"x" * (MAX_IMAGE_BYTES + 1), b""])
def test_bad_or_oversized_image_is_rejected(data):
    with pytest.raises(PlaybookDataError):
        validate_image(data)


def test_image_path_cannot_escape_storage_directory(tmp_path):
    with pytest.raises(PlaybookDataError):
        asset_path(tmp_path / "playbooks.json", "../../secret.png")
