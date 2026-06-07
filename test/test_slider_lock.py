
import threading
import time

import pytest

from captcha_solvers.slider_lock import SliderLockTimeout, acquire_slider_lock


def test_slider_lock_serializes_threads(tmp_path):
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        nonlocal active, max_active
        barrier.wait(timeout=5)
        with acquire_slider_lock("[测试]", lock_path=tmp_path / "slider.lock", poll_interval=0.005):
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with active_lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert max_active == 1
    assert all(not thread.is_alive() for thread in threads)


def test_slider_lock_releases_after_exception(tmp_path):
    lock_path = tmp_path / "slider.lock"

    with pytest.raises(RuntimeError):
        with acquire_slider_lock("[测试]", lock_path=lock_path, poll_interval=0.005):
            raise RuntimeError("boom")

    with acquire_slider_lock("[测试]", lock_path=lock_path, poll_interval=0.005):
        assert True


def test_slider_lock_wait_can_be_cancelled(tmp_path):
    lock_path = tmp_path / "slider.lock"
    stop_event = threading.Event()
    entered_waiter = threading.Event()
    result = []

    with acquire_slider_lock("[持有者]", lock_path=lock_path, poll_interval=0.005):
        def waiter():
            try:
                entered_waiter.set()
                with acquire_slider_lock("[等待者]", lock_path=lock_path, stop_event=stop_event, poll_interval=0.005):
                    result.append("entered")
            except SliderLockTimeout:
                result.append("cancelled")

        thread = threading.Thread(target=waiter)
        thread.start()
        assert entered_waiter.wait(timeout=2)
        time.sleep(0.03)
        stop_event.set()
        thread.join(timeout=2)

    assert result == ["cancelled"]
