
import threading
import time

import pytest

from captcha_solvers.slider_lock import (
    SliderLockTimeout,
    acquire_foreground_lock,
    acquire_foreground_window_lock,
    acquire_slider_lock,
)


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

def test_foreground_lock_and_slider_lock_share_same_mutex(tmp_path):
    lock_path = tmp_path / "foreground.lock"
    entered = []
    stop_event = threading.Event()

    with acquire_slider_lock("[滑块]", lock_path=lock_path, poll_interval=0.005):
        def waiter():
            try:
                with acquire_foreground_lock("[前台]", lock_path=lock_path, stop_event=stop_event, poll_interval=0.005):
                    entered.append("entered")
            except SliderLockTimeout:
                entered.append("cancelled")

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.03)
        assert entered == []
        stop_event.set()
        thread.join(timeout=2)

    assert entered == ["cancelled"]


def test_slider_lock_is_fifo_for_slider_waiters(tmp_path):
    lock_path = tmp_path / "slider-fifo.lock"
    entered = []

    def slider_waiter(name):
        with acquire_slider_lock(name, lock_path=lock_path, poll_interval=0.005):
            entered.append(name)

    with acquire_slider_lock("[持有者]", lock_path=lock_path, poll_interval=0.005):
        first = threading.Thread(target=slider_waiter, args=("[滑块1]",))
        second = threading.Thread(target=slider_waiter, args=("[滑块2]",))
        first.start()
        time.sleep(0.03)
        second.start()
        time.sleep(0.03)
        assert entered == []

    first.join(timeout=2)
    second.join(timeout=2)

    assert entered == ["[滑块1]", "[滑块2]"]


def test_slider_waiter_has_priority_over_earlier_window_waiter(tmp_path):
    lock_path = tmp_path / "slider-priority.lock"
    entered = []

    def window_waiter():
        with acquire_foreground_window_lock("[窗口]", lock_path=lock_path, poll_interval=0.005):
            entered.append("window")

    def slider_waiter():
        with acquire_slider_lock("[滑块]", lock_path=lock_path, poll_interval=0.005):
            entered.append("slider")

    with acquire_foreground_window_lock("[持有窗口]", lock_path=lock_path, poll_interval=0.005):
        window = threading.Thread(target=window_waiter)
        slider = threading.Thread(target=slider_waiter)
        window.start()
        time.sleep(0.03)
        slider.start()
        time.sleep(0.03)
        assert entered == []

    window.join(timeout=2)
    slider.join(timeout=2)

    assert entered == ["slider", "window"]


def test_new_window_cannot_enter_while_slider_is_waiting(tmp_path):
    lock_path = tmp_path / "slider-blocks-window.lock"
    entered = []

    def slider_waiter():
        with acquire_slider_lock("[滑块]", lock_path=lock_path, poll_interval=0.005):
            entered.append("slider")
            time.sleep(0.02)

    def window_waiter():
        with acquire_foreground_window_lock("[窗口]", lock_path=lock_path, poll_interval=0.005):
            entered.append("window")

    with acquire_foreground_window_lock("[持有窗口]", lock_path=lock_path, poll_interval=0.005):
        slider = threading.Thread(target=slider_waiter)
        window = threading.Thread(target=window_waiter)
        slider.start()
        time.sleep(0.03)
        window.start()
        time.sleep(0.03)
        assert entered == []

    slider.join(timeout=2)
    window.join(timeout=2)

    assert entered == ["slider", "window"]


def test_window_lock_is_fifo_when_no_slider_waits(tmp_path):
    lock_path = tmp_path / "window-fifo.lock"
    entered = []

    def window_waiter(name):
        with acquire_foreground_window_lock(name, lock_path=lock_path, poll_interval=0.005):
            entered.append(name)

    with acquire_foreground_window_lock("[持有窗口]", lock_path=lock_path, poll_interval=0.005):
        first = threading.Thread(target=window_waiter, args=("[窗口1]",))
        second = threading.Thread(target=window_waiter, args=("[窗口2]",))
        first.start()
        time.sleep(0.03)
        second.start()
        time.sleep(0.03)
        assert entered == []

    first.join(timeout=2)
    second.join(timeout=2)

    assert entered == ["[窗口1]", "[窗口2]"]


def test_cancelled_waiter_does_not_log_release_without_acquire(tmp_path, capsys):
    lock_path = tmp_path / "cancel-no-release.lock"
    stop_event = threading.Event()
    entered_waiter = threading.Event()
    result = []

    with acquire_slider_lock("[持有者]", lock_path=lock_path, poll_interval=0.005):
        capsys.readouterr()

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
        captured = capsys.readouterr().out

    assert result == ["cancelled"]
    assert "[等待者] 释放前台焦点锁" not in captured


def test_acquire_foreground_lock_is_window_lock_alias(tmp_path):
    lock_path = tmp_path / "alias.lock"
    entered = []

    with acquire_foreground_lock("[兼容]", lock_path=lock_path, poll_interval=0.005):
        with pytest.raises(SliderLockTimeout):
            stop_event = threading.Event()
            stop_event.set()
            with acquire_foreground_window_lock("[窗口]", lock_path=lock_path, stop_event=stop_event, poll_interval=0.005):
                entered.append("window")

    assert entered == []

