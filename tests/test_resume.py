from pathlib import Path

from tgdl.contract import ALIGNMENT, REQUEST_SIZE
from tgdl.resume import (
    aligned_resume_offset,
    build_sidecar,
    decide_slot,
    prepare_part_file,
    read_sidecar,
    write_sidecar,
)


def test_request_size_is_legal_for_telegram():
    assert ALIGNMENT == 4096
    assert REQUEST_SIZE == 512 * 1024
    assert REQUEST_SIZE % ALIGNMENT == 0
    assert (1024 * 1024) % REQUEST_SIZE == 0


def test_aligned_resume_offset_discards_only_a_torn_tail():
    assert aligned_resume_offset(0) == 0
    assert aligned_resume_offset(4095) == 0
    assert aligned_resume_offset(4096) == 4096
    assert aligned_resume_offset(5000) == 4096
    assert aligned_resume_offset(REQUEST_SIZE + 10) == REQUEST_SIZE


def test_prepare_part_truncates_to_alignment(tmp_path: Path):
    part = tmp_path / "file.bin.part"
    part.write_bytes(b"x" * 5000)
    offset = prepare_part_file(part, expected_size=9000)
    assert offset == 4096
    assert part.stat().st_size == 4096
    assert part.read_bytes() == b"x" * 4096


def test_prepare_part_keeps_exact_expected_size(tmp_path: Path):
    part = tmp_path / "file.bin.part"
    part.write_bytes(b"x" * 100)
    assert prepare_part_file(part, expected_size=100) == 100
    assert part.stat().st_size == 100


def test_sidecar_roundtrip(tmp_path: Path):
    path = tmp_path / "a.pdf.tgdl.json"
    payload = build_sidecar(
        message_id="42",
        chat_id="-1001",
        file_size=10,
        file_unique_id="999",
        file_name="a.pdf",
        bytes_written=4,
        aligned_offset=0,
        complete=False,
        part_path=tmp_path / "a.pdf.part",
        final_path=tmp_path / "a.pdf",
    )
    write_sidecar(path, payload)
    state, loaded = read_sidecar(path)
    assert state == "ok"
    assert loaded is not None
    assert loaded["fileUniqueId"] == "999"
    assert loaded["messageId"] == "42"
    assert loaded["fileSize"] == 10
    assert loaded["v"] == 1


def test_corrupt_sidecar(tmp_path: Path):
    path = tmp_path / "a.pdf.tgdl.json"
    path.write_text("{", encoding="utf-8")
    assert read_sidecar(path) == ("corrupt", None)


def test_decide_slot_table():
    cases = [
        ("empty", dict(final_exists=False, final_size=None, part_exists=False, part_size=None, meta_state="missing", meta=None), "take"),
        ("size heuristic skip", dict(final_exists=True, final_size=100, part_exists=False, part_size=None, meta_state="missing", meta=None), "skip"),
        ("different size collides", dict(final_exists=True, final_size=50, part_exists=False, part_size=None, meta_state="missing", meta=None), "collide"),
        ("unknown size without meta collides", dict(final_exists=True, final_size=50, part_exists=False, part_size=None, meta_state="missing", meta=None, expected=None), "collide"),
        ("sidecar identity skip", dict(final_exists=True, final_size=100, part_exists=False, part_size=None, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 100, "complete": True}), "skip"),
        ("sidecar wins over same size", dict(final_exists=True, final_size=100, part_exists=False, part_size=None, meta_state="ok", meta={"fileUniqueId": "other", "fileSize": 100}), "collide"),
        ("resume partial", dict(final_exists=False, final_size=None, part_exists=True, part_size=5000, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 9000}, expected=9000), "resume"),
        ("finish exact part", dict(final_exists=False, final_size=None, part_exists=True, part_size=100, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 100}), "finish"),
        ("orphan part resumes", dict(final_exists=False, final_size=None, part_exists=True, part_size=40, meta_state="missing", meta=None), "resume"),
        ("orphan part larger than expected collides", dict(final_exists=False, final_size=None, part_exists=True, part_size=400, meta_state="missing", meta=None), "collide"),
        ("same id different size collides", dict(final_exists=False, final_size=None, part_exists=True, part_size=10, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 200}), "collide"),
        ("ours but final short resumes", dict(final_exists=True, final_size=40, part_exists=False, part_size=None, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 100, "complete": False}), "resume"),
        ("stale complete flag still skips when sizes match", dict(final_exists=True, final_size=100, part_exists=False, part_size=None, meta_state="ok", meta={"fileUniqueId": "u", "fileSize": 100, "complete": False}), "skip"),
        ("corrupt sidecar collides when a file exists", dict(final_exists=True, final_size=100, part_exists=False, part_size=None, meta_state="corrupt", meta=None), "collide"),
        ("corrupt sidecar on an empty slot is free", dict(final_exists=False, final_size=None, part_exists=False, part_size=None, meta_state="corrupt", meta=None), "take"),
    ]
    for name, view, expected in cases:
        expected_size = view.pop("expected", 100)
        got = decide_slot(unique_id="u", expected_size=expected_size, **view)
        assert got == expected, name
