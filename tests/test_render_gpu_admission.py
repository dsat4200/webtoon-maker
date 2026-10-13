"""Aggregate queued copies and owner-only graphics residency admission."""
from comic_editor.render.admission import WorkAdmission
from comic_editor.render.gpu.residency import GraphicsResidency


def test_unadmitted_copies_are_nonblocking_and_share_one_budget():
    admission = WorkAdmission(100)
    first = admission.reserve_copies(60)
    assert first is not None and admission.reserve_copies(60) is None
    assert admission.copied_bytes == 60
    with admission.reserve('graphics',60,copies=first) as ticket:
        assert ticket.estimated_bytes == 60 and admission.peak_bytes == 60
        assert admission.reserve_copies(50) is None
    first.release()
    first.release()
    assert admission.copied_bytes == 0 and not admission.running


def test_only_admitted_parent_borrows_an_oversized_copy_workspace():
    admission = WorkAdmission(100)
    assert admission.reserve_copies(101) is None
    with admission.reserve('scene',80) as ticket:
        copies = admission.reserve_copies(70)
        assert copies is not None and admission.peak_bytes == 150
        with admission.reserve('nested-graphics',70,copies=copies) as borrowed:
            assert borrowed is ticket and len(admission.running) == 1
        copies.release()
    assert admission.copied_bytes == 0


def test_bounded_capture_parent_can_queue_graphics_with_its_own_credited_copies():
    admission = WorkAdmission(100)
    capture = admission.reserve_copies(60)
    with admission.reserve('canvas',160,copies=capture,own_copies=True) as ticket:
        assert ticket.oversized
        nested = admission.reserve_copies(70)
        assert nested is not None
        assert admission.peak_bytes == 230
        assert admission.copied_bytes == 130
        assert len(admission.running) == 1
        nested.release()
    assert not admission.owned_copy_markers and not admission.copy_credits
    capture.release()
    assert admission.copied_bytes == 0


def test_owned_capture_credit_does_not_adopt_a_foreign_pending_capture():
    admission = WorkAdmission(100)
    own = admission.reserve_copies(40)
    foreign = admission.reserve_copies(40)
    with admission.reserve('canvas',160,copies=own,own_copies=True):
        assert admission.reserve_copies(1) is None
        foreign.release()
        nested = admission.reserve_copies(20)
        assert nested is not None
        nested.release()
    own.release()


def test_owned_capture_lease_must_belong_to_the_same_admission():
    import pytest
    admission = WorkAdmission(100)
    foreign = WorkAdmission(100).reserve_copies(10)
    with pytest.raises(ValueError):
        with admission.reserve('canvas',20,copies=foreign,own_copies=True):
            pass
    foreign.release()


def test_empty_snapshot_leases_do_not_block_a_sole_graphics_parent():
    admission = WorkAdmission(100)
    metadata_only = [admission.reserve_copies(0) for _ in range(2)]
    with admission.reserve('canvas',160,copies=metadata_only[0],own_copies=True):
        nested = admission.reserve_copies(70)
        assert nested is not None
        assert admission.peak_bytes == 230
        nested.release()
    for lease in metadata_only:
        lease.release()
    assert not admission.copies and not admission.owned_copy_markers


def test_active_and_pending_copies_of_one_actual_interaction_do_not_deadlock():
    admission = WorkAdmission(100)
    scope = object()
    active = admission.reserve_copies(40, scope=scope)
    pending = admission.reserve_copies(40, scope=scope)
    with admission.reserve('canvas', 160, copies=active, own_copies=True):
        nested = admission.reserve_copies(20)
        assert nested is not None
        # Pending capture storage is charged in full, without a second credit.
        assert admission.peak_bytes == 220
        assert admission.copied_bytes == 100
        assert admission._occupied() == 220
        nested.release()
    active.release()
    pending.release()
    assert not admission.copies and not admission.copy_scopes


def test_equal_but_differently_owned_scopes_remain_foreign():
    admission = WorkAdmission(100)
    active_scope, foreign_scope = ['same'], ['same']
    active = admission.reserve_copies(40, scope=active_scope)
    foreign = admission.reserve_copies(40, scope=foreign_scope)
    with admission.reserve('canvas', 160, copies=active, own_copies=True):
        assert admission.reserve_copies(1) is None
    active.release()
    foreign.release()


def test_cancelled_parent_stops_a_graphics_copy_wait(monkeypatch):
    from threading import Condition, Event
    from types import SimpleNamespace
    import pytest
    import comic_editor.render.admission as module
    from comic_editor.render.gpu.worker import GpuWorker
    admission = WorkAdmission(100)
    monkeypatch.setattr(module, 'RENDER_ADMISSION', admission)
    foreign = admission.reserve_copies(40)
    cancelled = Event()
    worker = SimpleNamespace(closed=False, condition=Condition())
    with admission.reserve('canvas', 160, cancelled=cancelled.is_set):
        cancelled.set()
        with worker.condition, pytest.raises(module.WorkCancelled):
            GpuWorker._consumer_copies(worker, 20)
    foreign.release()


def test_gui_metadata_only_capture_can_advance_during_oversized_worker():
    from concurrent.futures import ThreadPoolExecutor
    admission = WorkAdmission(100)
    with admission.reserve('canvas', 160):
        # A separate context has no parent-ticket privilege. Zero bytes may
        # advance; a nonempty capture still declines rather than overallocating.
        with ThreadPoolExecutor(max_workers=1) as executor:
            empty = executor.submit(admission.reserve_copies, 0).result(timeout=2)
            nonempty = executor.submit(admission.reserve_copies, 1).result(timeout=2)
        assert empty is not None and nonempty is None
        assert admission._occupied() == 160 and admission.peak_bytes == 160
        empty.release()


def test_graphics_residency_is_aggregate_and_oversized_storage_is_transient():
    pool = GraphicsResidency(100)
    first,second,legacy = pool.token(),pool.token(),pool.token()
    assert pool.change(first,70)
    assert not pool.change(second,70) and pool.bytes == 70
    assert pool.change(first,0) and pool.change(second,70)
    assert pool.change(legacy,120,allow_exclusive=True)
    assert pool.bytes == 190 and not pool.change(second,80)
    assert pool.change(legacy,0) and pool.change(second,80)
    assert pool.change(second,0) and pool.bytes == 0
