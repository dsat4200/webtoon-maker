"""Retired snapshot IO never delays input or releases surviving source pins."""
from pathlib import Path
import subprocess
import sys
from threading import Event, get_ident
import weakref

from comic_editor.core import tile_backing
from comic_editor.core.tile_backing import SnapshotBacking


def test_last_pin_release_is_detached_and_keeps_directory_until_io_finishes(tmp_path, monkeypatch):
    source = tmp_path / 'original.png'
    source.write_bytes(b'original native bytes')
    backing = SnapshotBacking()
    pinned = backing.pin(source)
    surviving = backing.pin(source)
    retired_path, surviving_path, root = pinned.path, surviving.path, backing.root
    retained = weakref.ref(backing)
    owner_thread = get_ident()
    entered, release, directory_removed = Event(), Event(), Event()
    unlink = Path.unlink
    cleanup = backing.workspace.cleanup

    def slow_unlink(path, *args, **kwargs):
        if path == retired_path:
            assert get_ident() != owner_thread, 'Retirement ran on the input thread'
            entered.set()
            assert release.wait(5.)
        return unlink(path, *args, **kwargs)

    def removed():
        assert get_ident() != owner_thread, 'Directory cleanup ran on the input thread'
        cleanup()
        directory_removed.set()

    monkeypatch.setattr(Path, 'unlink', slow_unlink)
    monkeypatch.setattr(backing.workspace, 'cleanup', removed)
    try:
        del pinned
        assert entered.wait(2.)
        del backing
        # A later snapshot still reads its exact original while earlier IO is
        # blocked; replacing the authored filename cannot replace those bytes.
        replacement = tmp_path / 'replacement.png'
        replacement.write_bytes(b'new source revision')
        replacement.replace(source)
        assert surviving_path.read_bytes() == b'original native bytes'
        del surviving
        assert retained() is not None
        assert root.exists() and retired_path.exists()
    finally:
        release.set()
    assert directory_removed.wait(3.)
    assert not root.exists()
    assert source.read_bytes() == b'new source revision'


def test_empty_snapshot_directory_cleanup_is_detached(monkeypatch):
    backing = SnapshotBacking()
    root = backing.root
    owner_thread = get_ident()
    entered, release, finished = Event(), Event(), Event()
    cleanup = backing.workspace.cleanup

    def held():
        assert get_ident() != owner_thread
        entered.set()
        assert release.wait(5.)
        cleanup()
        finished.set()

    monkeypatch.setattr(backing.workspace, 'cleanup', held)
    try:
        del backing
        assert entered.wait(2.)
        assert root.exists(), 'Cleanup did not retain its private directory'
    finally:
        release.set()
    assert finished.wait(3.)
    assert not root.exists()


def test_failed_pin_delete_does_not_block_later_retirement(tmp_path, monkeypatch):
    source = tmp_path / 'source.png'
    source.write_bytes(b'unchanged original')
    backing = SnapshotBacking()
    pin = backing.pin(source)
    path = pin.path
    failed, continued = Event(), Event()
    unlink = Path.unlink

    def failure(target, *args, **kwargs):
        if target == path:
            failed.set()
            raise OSError('injected retired pin failure')
        return unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', failure)
    del pin
    tile_backing._queue_retirement(continued.set)
    assert failed.wait(2.) and continued.wait(2.)
    assert source.read_bytes() == b'unchanged original'
    # The backing owner retains the ordinary final directory cleanup fallback.
    monkeypatch.setattr(Path, 'unlink', unlink)
    root = backing.root
    cleanup = backing.workspace.cleanup
    directory_removed = Event()
    def removed():
        cleanup()
        directory_removed.set()
    monkeypatch.setattr(backing.workspace, 'cleanup', removed)
    del backing
    assert directory_removed.wait(3.)
    assert not root.exists()


def test_same_thread_pin_finalizer_under_executor_shutdown_locks_does_not_submit(tmp_path):
    # A real ThreadPoolExecutor can invoke GC while constructing a WorkItem,
    # with both non-reentrant locks held on the finalizer's own thread. Use an
    # isolated process so the former deadlock fails with a bounded timeout
    # instead of retaining a locked pytest process or unrelated executor.
    code = r'''
from concurrent.futures import ThreadPoolExecutor, thread
from pathlib import Path
from threading import Event
import sys
from comic_editor.core.tile_backing import SnapshotBacking, _queue_retirement

source = Path(sys.argv[1]) / 'source.png'
source.write_bytes(b'original native pixels')
backing = SnapshotBacking()
pin = backing.pin(source)
retired, root = pin.path, backing.root
executor = ThreadPoolExecutor(max_workers=1)
with executor._shutdown_lock, thread._global_shutdown_lock:
    del pin
    completed = Event()
    _queue_retirement(completed.set)
    assert completed.wait(3.), 'Finalizer could not enqueue while executor locks were held'
assert not retired.exists()
del backing
completed = Event()
_queue_retirement(completed.set)
assert completed.wait(3.)
assert not root.exists()
assert source.read_bytes() == b'original native pixels'
executor.shutdown()
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10.)
    assert result.returncode == 0, result.stdout + result.stderr
