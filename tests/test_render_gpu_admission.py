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
