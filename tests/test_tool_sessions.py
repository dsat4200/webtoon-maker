from types import SimpleNamespace

import pytest

from comic_editor.ui.tool_sessions import InputAdapter, Interruption, LatestValueInput, ToolSession


def packet(timestamp):
    return SimpleNamespace(timestamp=lambda: timestamp, xTilt=lambda: 20, yTilt=lambda: -8, rotation=lambda: 45)


def test_device_time_survives_dispatch_stall_and_clock_reset():
    now = [100.]
    adapter = InputAdapter(lambda: now[0])
    first = adapter.capture(packet(1000), tablet=True)
    now[0] += 5
    second = adapter.capture(packet(1008), tablet=True)
    assert second.timestamp-first.timestamp == pytest.approx(.008)
    assert (second.tilt_x, second.tilt_y, second.rotation) == (20, -8, 45)
    assert adapter.capture(packet(3)).timestamp == 105
    assert adapter.capture(packet(0)).timestamp is None


def test_ordered_session_consumes_every_sample_and_commits_once():
    samples, terminal = [], []
    session = ToolSession(samples.append, samples.append, lambda: terminal.append("commit"),
                          lambda: terminal.append("cancel"), interruption=Interruption.COMMIT)
    session.begin((1, .1))
    for number in range(2, 802):
        session.update((number, number/802))
    session.commit((802, .25))
    session.commit()
    session.cancel()
    assert len(samples) == 802
    assert samples[-1] == (802, .25)
    assert terminal == ["commit"]
    assert session.samples == 802


def test_interrupt_policy_is_per_tool():
    terminals = []
    for policy in Interruption:
        session = ToolSession(lambda sample: None, lambda sample: None,
                              lambda: terminals.append("commit"), lambda: terminals.append("cancel"), interruption=policy)
        session.begin(None)
        session.interrupt()
    assert terminals == ["commit", "cancel"]


def test_absolute_input_coalesces_and_release_flushes_final_position():
    positions = []
    latest = LatestValueInput(positions.append)
    for position in range(800):
        latest.queue(position)
    latest.flush()
    latest.finish(900)
    latest.queue(1000)
    latest.cancel()
    latest.flush()
    assert positions == [799, 900]
