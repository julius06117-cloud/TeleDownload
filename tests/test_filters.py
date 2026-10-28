import re
from datetime import datetime, timezone

from tgdl.filters import FileQuery, extension_matches, sender_matches, size_matches
from tgdl.models import MediaItem


def _item(**kwargs) -> MediaItem:
    base = dict(
        chat_id=1,
        chat_title="c",
        message_id=1,
        date=datetime(2024, 5, 2, tzinfo=timezone.utc),
        sender_id=9,
        sender_username="ada",
        sender_name="Ada Lovelace",
        file_name="Quarterly.pdf",
        file_size=1500,
        mime_type="application/pdf",
        file_unique_id="1",
        kind="document",
        extension=".pdf",
    )
    base.update(kwargs)
    return MediaItem(**base)


def test_size_and_extension_helpers():
    assert size_matches(10, None, None)
    assert not size_matches(None, 1, None)
    assert size_matches(10, 10, 10)
    assert not size_matches(9, 10, None)
    assert extension_matches("A.PDF", frozenset({".pdf"}))
    assert extension_matches("bundle.tar.gz", frozenset({".tar.gz"}))
    assert not extension_matches("notes.txt", frozenset({".pdf"}))


def test_sender_matches_id_username_or_display_name():
    assert sender_matches(9, "ada", "Ada Lovelace", "9")
    assert sender_matches(9, "ada", "Ada Lovelace", "@Ada")
    assert sender_matches(9, "ada", "Ada Lovelace", "lovelace")
    assert not sender_matches(9, "ada", "Ada Lovelace", "grace")


def test_query_matches_combined_filters():
    query = FileQuery(
        exts=frozenset({".pdf"}),
        min_size=1000,
        max_size=2000,
        since=datetime(2024, 5, 1, tzinfo=timezone.utc),
        until=datetime(2024, 5, 3, tzinfo=timezone.utc),
        sender="ada",
        name="quarter",
        name_regex=re.compile(r"pdf$", re.I),
        photos=False,
        message_ids=None,
    )
    assert query.matches(_item())
    assert not query.matches(_item(kind="photo"))
    assert not query.matches(_item(file_name="notes.txt", extension=".txt"))
    assert not query.matches(_item(file_size=10))
    assert not query.matches(_item(date=datetime(2024, 4, 1, tzinfo=timezone.utc)))
    assert query.matches(_item())


def test_message_id_allow_list():
    query = FileQuery(
        exts=frozenset(),
        min_size=None,
        max_size=None,
        since=None,
        until=None,
        sender=None,
        name=None,
        name_regex=None,
        photos=True,
        message_ids=frozenset({"5"}),
    )
    assert query.matches(_item(message_id=5, kind="photo"))
    assert not query.matches(_item(message_id=6, kind="photo"))
