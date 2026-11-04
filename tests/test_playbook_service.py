import pytest
from concurrent.futures import ThreadPoolExecutor

from services.playbook_service import PlaybookConflict, PlaybookDataError, PlaybookService, validate_draft


def draft(**changes):
    return {"name": "A Execute", "map": "Ascent", "side": "attack", "objective": "Take site",
            "steps": ["Smoke heaven", "Enter"], "assignments": [], "utility": "", "links": [],
            "diagram": None, **changes}


def test_create_filter_update_and_delete(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    first = service.create(draft())
    second = service.create(draft(name="Retake", side="defence"))
    assert [s["id"] for s in service.list("ASCENT", "attack")] == [first["id"]]
    assert service.get(second["id"])["name"] == "Retake"
    revised = service.update(first["id"], first["revision"], draft(steps=["Enter", "Smoke heaven"]))
    assert revised["steps"] == ["Enter", "Smoke heaven"]
    assert revised["revision"] == 2
    service.delete(first["id"], 2)
    assert service.get(first["id"]) is None


def test_duplicate_names_are_rejected_per_map_and_side(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    service.create(draft())
    with pytest.raises(PlaybookDataError, match="already exists"):
        service.create(draft(name="a execute", map="ASCENT"))
    service.create(draft(name="A Execute", side="defence"))


def test_stale_update_and_delete_do_not_overwrite(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    initial = service.create(draft())
    service.update(initial["id"], 1, draft(objective="New plan"))
    with pytest.raises(PlaybookConflict):
        service.update(initial["id"], 1, draft(objective="Old plan"))
    with pytest.raises(PlaybookConflict):
        service.delete(initial["id"], 1)
    assert service.get(initial["id"])["objective"] == "New plan"


def test_concurrent_updates_allow_only_one_revision(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    initial = service.create(draft())

    def save(value):
        try:
            return service.update(initial["id"], 1, draft(objective=value))["objective"]
        except PlaybookConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["First", "Second"]))
    assert results.count("conflict") == 1
    assert service.get(initial["id"])["objective"] in ("First", "Second")


@pytest.mark.parametrize("changes", [
    {"side": "unknown"}, {"name": " "}, {"links": ["javascript:alert(1)"]},
    {"links": ["https://example.com/>@everyone"]}, {"assignments": [{}] * 6},
    {"diagram": {"markers": [{"kind": "smoke", "x": 1.1, "y": 0.2}]}},
    {"diagram": {"markers": [{"kind": "path", "x": 0, "y": 0, "points": [[0, 0]]}]}},
])
def test_invalid_drafts_are_rejected(changes):
    with pytest.raises(PlaybookDataError):
        validate_draft(draft(**changes))


def test_valid_tactical_coordinates_and_assignments():
    clean = validate_draft(draft(assignments=[{"role": "Entry", "agent": "Jett"}], diagram={"markers": [
        {"kind": "path", "x": 0, "y": 0, "points": [[0, 0], [1, 1]]}
    ]}))
    assert clean["assignments"][0]["role"] == "Entry"
    assert clean["diagram"]["markers"][0]["points"] == [[0, 0], [1, 1]]
