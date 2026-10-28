from datetime import datetime, timezone
from pathlib import Path

from tgdl.models import MediaItem
from tgdl.naming import (
    allocate_slot,
    destination_directory,
    plan_items,
    sanitize_filename,
    with_collision_index,
)
from tgdl.resume import build_sidecar, write_sidecar


def _item(name: str, unique: str, size: int | None = 100, day: int = 2) -> MediaItem:
    return MediaItem(
        chat_id=-1001,
        chat_title="Reports / Q1",
        message_id=1,
        date=datetime(2024, 3, day, tzinfo=timezone.utc),
        sender_id=7,
        sender_username="ada",
        sender_name="Ada",
        file_name=name,
        file_size=size,
        mime_type="application/pdf",
        file_unique_id=unique,
        kind="document",
        extension=".pdf",
    )


def test_sanitize_strips_path_and_reserved_names():
    assert sanitize_filename("../secret.pdf") == "secret.pdf"
    assert sanitize_filename("a<b>:c") == "a_b__c"
    assert sanitize_filename("CON.txt").startswith("CON_")
    assert sanitize_filename("") == "file"
    assert sanitize_filename("..") == "file"


def test_collision_index_keeps_extension():
    assert with_collision_index("report.pdf", 0) == "report.pdf"
    assert with_collision_index("report.pdf", 1) == "report (1).pdf"
    assert with_collision_index("report.pdf", 2) == "report (2).pdf"


def test_month_layout_uses_utc():
    when = datetime(2024, 3, 1, 23, 30, tzinfo=timezone.utc)
    path = destination_directory(Path("/tmp/out"), "Reports / Q1", when, True)
    assert path.as_posix().endswith("/out/Reports _ Q1/2024-03") or path.as_posix().endswith(
        "/out/Reports _ Q1/2024-03"
    )
    flat = destination_directory(Path("/tmp/out"), "Reports", when, False)
    assert flat == Path("/tmp/out")


def test_allocate_skips_same_size_and_collides_on_other_size(tmp_path: Path):
    (tmp_path / "report.pdf").write_bytes(b"x" * 100)
    skip = allocate_slot(tmp_path, "report.pdf", unique_id="u", expected_size=100, resume=True, force=False)
    assert skip.action == "skip"
    other = allocate_slot(tmp_path, "report.pdf", unique_id="v", expected_size=80, resume=True, force=False)
    assert other.action == "download"
    assert other.final_path.name == "report (1).pdf"


def test_sidecar_identity_beats_equal_size(tmp_path: Path):
    final = tmp_path / "report.pdf"
    final.write_bytes(b"x" * 100)
    write_sidecar(
        Path(str(final) + ".tgdl.json"),
        build_sidecar(
            message_id="1",
            chat_id="-100",
            file_size=100,
            file_unique_id="owner",
            file_name="report.pdf",
            bytes_written=100,
            aligned_offset=100,
            complete=True,
            part_path=Path(str(final) + ".part"),
            final_path=final,
        ),
    )
    planned = allocate_slot(tmp_path, "report.pdf", unique_id="other", expected_size=100, resume=True, force=False)
    assert planned.final_path.name == "report (1).pdf"


def test_plan_items_dedupes_and_can_stay_read_only(tmp_path: Path):
    items = [_item("report.pdf", "same", day=2), _item("report.pdf", "same", day=2)]
    items[1] = MediaItem(**{**items[1].__dict__, "message_id": 2})
    planned = plan_items(items, output=tmp_path, group_by_month=True, resume=True, force=False, write=False)
    assert planned[0].duplicate is False
    assert planned[1].duplicate is True
    assert planned[0].final_path == planned[1].final_path
    assert list(tmp_path.rglob("*")) == []


def test_plan_items_writes_sidecar_for_download(tmp_path: Path):
    planned = plan_items(
        [_item("report.pdf", "u")],
        output=tmp_path,
        group_by_month=False,
        resume=True,
        force=False,
        write=True,
    )
    assert planned[0].action == "download"
    assert planned[0].sidecar_path.exists()
    assert not planned[0].final_path.exists()
