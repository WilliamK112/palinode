"""The watcher debounce must *coalesce* a write burst, not drop its tail.

Before: an event inside ``debounce_seconds`` of the last processed event for
the same path was ignored outright, so ``save`` -> ``cross_refs`` rewrite (well
inside 1 s) left the pre-rewrite body in the index until the file was touched
again. Now each path arms a trailing-edge timer that re-reads the file when it
fires; a new event cancels and re-arms it.

Real SQLite on tmp_path, real ``PalinodeHandler``; the embedder is the only
thing faked (a constant vector at the ``embedder.embed`` seam).
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent
from watchdog.observers import Observer

from palinode.core import store
from palinode.core.config import config
from palinode.indexer import watcher

_VEC = [0.04] * 1024
_DEBOUNCE_S = 0.3


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(config.services.watcher, "debounce_seconds", _DEBOUNCE_S)
    store.init_db()
    (tmp_path / "projects").mkdir()
    return tmp_path


def _bodies() -> list[str]:
    db = store.get_db()
    rows = db.execute("SELECT content FROM chunks").fetchall()
    db.close()
    return [r["content"] for r in rows]


def _wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _doc(body: str) -> str:
    return f"---\nid: proj-x\ncategory: projects\n---\n\n# X\n\n{body}\n"


def _index_timer_threads(handler: watcher.PalinodeHandler) -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if getattr(getattr(t, "function", None), "__self__", None) is handler
        and t.function.__func__ is watcher.PalinodeHandler._fire_index
    ]


def test_second_write_inside_window_is_what_gets_indexed(tmp_store):
    """Handler-level: two events 200 ms apart -> one pass, over the second body."""
    path = tmp_store / "projects" / "x.md"
    handler = watcher.PalinodeHandler()
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC) as embed:
            path.write_text(_doc("FIRST body, must not win."))
            handler.on_created(FileCreatedEvent(str(path)))
            time.sleep(0.2)
            path.write_text(_doc("SECOND body, the trailing edge."))
            handler.on_modified(FileModifiedEvent(str(path)))

            # Still inside the window: nothing indexed yet, one timer armed.
            assert _bodies() == []
            assert list(handler._index_timers) == [str(path)]

            assert _wait_until(lambda: bool(_bodies()), timeout=5.0)
            # Let the (single) pass finish before inspecting call counts.
            assert _wait_until(lambda: not handler._index_timers, timeout=5.0)

        bodies = _bodies()
        assert any("SECOND body" in b for b in bodies), bodies
        assert not any("FIRST body" in b for b in bodies), bodies
        # Coalesced: the first write never cost an embed call.
        assert embed.call_count == 1
        # Bounded: the per-path entry is evicted on fire.
        assert handler._index_timers == {}
        # The entry is evicted from inside the timer thread, which is still
        # unwinding at that instant — give it a moment to exit.
        assert _wait_until(lambda: not _index_timer_threads(handler), timeout=2.0)
    finally:
        handler.shutdown()


def test_real_observer_indexes_final_content(tmp_store):
    """End to end through watchdog: two writes inside the window, second wins."""
    root = os.path.realpath(str(tmp_store))
    path = os.path.join(root, "projects", "obs.md")
    handler = watcher.PalinodeHandler()
    observer = Observer()
    observer.schedule(handler, root, recursive=True)
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC):
            observer.start()
            with open(path, "w") as fh:
                fh.write(_doc("FIRST via observer."))
            time.sleep(0.1)
            with open(path, "w") as fh:
                fh.write(_doc("SECOND via observer."))

            assert _wait_until(
                lambda: any("SECOND via observer" in b for b in _bodies()),
                timeout=10.0,
            ), _bodies()
            # Give any straggler event's timer a chance to fire, then re-check
            # that the final state is still the second body only.
            time.sleep(_DEBOUNCE_S * 2)
        bodies = _bodies()
        assert not any("FIRST via observer" in b for b in bodies), bodies
    finally:
        observer.stop()
        handler.shutdown()
        observer.join(5)


def test_shutdown_cancels_pending_index_timers(tmp_store):
    """No thread leak: shutdown cancels armed per-path timers and refuses new ones."""
    handler = watcher.PalinodeHandler()
    a = tmp_store / "projects" / "a.md"
    b = tmp_store / "projects" / "b.md"
    a.write_text(_doc("a"))
    b.write_text(_doc("b"))
    handler.on_created(FileCreatedEvent(str(a)))
    handler.on_created(FileCreatedEvent(str(b)))
    assert set(handler._index_timers) == {str(a), str(b)}
    assert len(_index_timer_threads(handler)) == 2

    handler.shutdown()

    assert handler._index_timers == {}
    assert _index_timer_threads(handler) == []
    # A late event on a stopped handler is a no-op.
    handler.on_modified(FileModifiedEvent(str(a)))
    assert handler._index_timers == {}
    time.sleep(_DEBOUNCE_S * 2)
    assert _bodies() == []
