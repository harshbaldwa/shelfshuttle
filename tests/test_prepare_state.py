import os
import stat

import pytest

from app.core.config import private_key
from app.core.prepare_state import prepare_state
from app.models.database import Job, database


def test_prepared_state_keeps_the_key_and_queue(tmp_path):
    state = tmp_path / "data"
    key = private_key(state)
    engine, factory = database(state)
    with factory() as session:
        session.add(Job(id="1", author="Author", title="Book", format="ebook", destinations={}))
        session.commit()
    engine.dispose()
    state.chmod(0o775)
    (state / ".key").chmod(0o644)
    (state / "shelfshuttle.sqlite3").chmod(0o664)
    covers = state / "covers"
    covers.mkdir(0o755)
    cover = covers / f"{'1' * 32}.jpg"
    cover.write_bytes(b"cover")
    unrelated = state / "other-file"
    unrelated.touch(mode=0o644)
    unrelated_mode = unrelated.stat().st_mode

    for _ in range(2):
        prepare_state(state, os.geteuid(), os.getegid())

    assert private_key(state) == key
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(covers.stat().st_mode) == 0o700
    assert stat.S_IMODE(cover.stat().st_mode) == 0o600
    assert cover.read_bytes() == b"cover"
    assert unrelated.stat().st_mode == unrelated_mode
    engine, factory = database(state)
    try:
        with factory() as session:
            assert session.get(Job, "1").title == "Book"
    finally:
        engine.dispose()


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_preparation_rejects_links_to_other_files(tmp_path, link):
    state = tmp_path / "data"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.touch()
    outside.chmod(0o644)
    if link == "symbolic":
        (state / ".key").symlink_to(outside)
    else:
        os.link(outside, state / ".key")

    with pytest.raises(ValueError, match="Unsafe ShelfShuttle state entry"):
        prepare_state(state, os.geteuid(), os.getegid())

    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
