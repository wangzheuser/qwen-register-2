
import threading
import time

import portalocker
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


def test_slider_file_lock_timeout_releases_foreground_coordinator(tmp_path):
    """跨进程文件锁被占用时，不能一直持有进程内前台锁。"""
    lock_path = tmp_path / "held-by-other-process.lock"

    with portalocker.Lock(str(lock_path), timeout=0):
        with pytest.raises(SliderLockTimeout):
            with acquire_slider_lock(
                "[滑块]",
                lock_path=lock_path,
                poll_interval=0.005,
                file_lock_timeout=0.03,
            ):
                pytest.fail("文件锁被占用时不应进入滑块临界区")

    # 如果上面的异常没有释放 coordinator，这里会卡住或被取消。
    with acquire_foreground_window_lock("[窗口]", lock_path=lock_path, poll_interval=0.005):
        assert True


def test_window_lock_does_not_wait_for_cross_process_foreground_file_lock(tmp_path):
    """window 不持有/等待主滑块文件锁，避免 Camoufox new_page 卡住拖死滑块。"""
    lock_path = tmp_path / "cross-process-foreground.lock"

    with portalocker.Lock(str(lock_path), timeout=0):
        with acquire_foreground_window_lock(
            "[窗口]",
            lock_path=lock_path,
            poll_interval=0.005,
            file_lock_timeout=0.03,
        ):
            assert True


def test_window_lock_waits_while_cross_process_slider_intent_exists(tmp_path):
    """跨进程滑块正在占用前台时，window 操作不能进入抢焦点。"""
    lock_path = tmp_path / "cross-process-intent.lock"
    intent_path = tmp_path / "cross-process-intent.lock.intent"

    with portalocker.Lock(str(intent_path), timeout=0):
        with pytest.raises(SliderLockTimeout):
            with acquire_foreground_window_lock(
                "[窗口]",
                lock_path=lock_path,
                poll_interval=0.005,
                file_lock_timeout=0.03,
            ):
                pytest.fail("slider intent 存在时 window 不应进入前台临界区")



def test_window_locks_can_overlap_without_slider_intent(tmp_path):
    """没有 slider 意图时，跨进程 window 操作应共享前台锁，避免启动阶段全局串行化。"""
    lock_path = tmp_path / "shared-window.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    entered = []

    def first_window():
        with acquire_foreground_window_lock("[窗口1]", lock_path=lock_path, poll_interval=0.005):
            entered.append("first")
            first_entered.set()
            release_first.wait(timeout=2)

    thread = threading.Thread(target=first_window)
    thread.start()
    assert first_entered.wait(timeout=2)

    with acquire_foreground_window_lock("[窗口2]", lock_path=lock_path, poll_interval=0.005):
        entered.append("second")

    release_first.set()
    thread.join(timeout=2)

    assert entered == ["first", "second"]

def test_window_intent_timeout_does_not_poison_local_window_lock(tmp_path):
    """跨进程 slider intent 等待超时后，必须释放本进程 window owner。"""
    lock_path = tmp_path / "window-intent-timeout.lock"
    intent_path = tmp_path / "window-intent-timeout.lock.intent"

    with portalocker.Lock(str(intent_path), timeout=0):
        with pytest.raises(SliderLockTimeout):
            with acquire_foreground_window_lock(
                "[窗口等待]",
                lock_path=lock_path,
                poll_interval=0.005,
                file_lock_timeout=0.03,
            ):
                pytest.fail("slider intent 存在时 window 不应进入")

    with acquire_foreground_window_lock("[窗口恢复]", lock_path=lock_path, poll_interval=0.005):
        assert True


def test_window_lock_waits_on_cross_process_slider_intent(tmp_path, monkeypatch):
    from captcha_solvers import slider_lock

    if slider_lock.portalocker is None:
        pytest.skip("portalocker 不可用")

    lock_path = tmp_path / "slider.lock"
    intent_path = slider_lock._foreground_intent_lock_path(lock_path)
    blocker = slider_lock.portalocker.Lock(str(intent_path), timeout=0, flags=slider_lock.portalocker.LockFlags.EXCLUSIVE | slider_lock.portalocker.LockFlags.NON_BLOCKING)
    blocker.acquire()
    try:
        with pytest.raises(SliderLockTimeout):
            with slider_lock.acquire_foreground_window_lock(
                label="window-test",
                lock_path=lock_path,
                poll_interval=0.005,
                file_lock_timeout=0.03,
            ):
                pytest.fail("slider intent 存在时 window 不应进入")
    finally:
        blocker.release()


def test_slider_waiting_on_file_lock_does_not_claim_foreground_lock(tmp_path, capsys):
    from captcha_solvers import slider_lock

    if slider_lock.portalocker is None:
        pytest.skip("portalocker 不可用")

    lock_path = tmp_path / "claimed-too-early.lock"
    with slider_lock.portalocker.Lock(str(lock_path), timeout=0):
        with pytest.raises(SliderLockTimeout):
            with acquire_slider_lock(
                "[等待者]",
                lock_path=lock_path,
                poll_interval=0.005,
                file_lock_timeout=0.03,
            ):
                pytest.fail("文件锁被占用时不应进入滑块临界区")

    output = capsys.readouterr().out
    assert "[等待者] 已获得前台焦点锁 type=slider" not in output
    assert "[等待者] 释放前台焦点锁 type=slider" not in output
