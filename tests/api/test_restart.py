"""api/restart.py's trigger_restart: under `uvicorn --reload` this process is
a worker child of the reload supervisor (PID 1), so exiting only the worker
can strand the container with nothing to respawn it. See that module's
docstring for the live-debugging trail that found this."""
from __future__ import annotations

import signal
from unittest.mock import patch

from api.restart import trigger_restart


def _run_die(*, getpid, getppid, kill_side_effect=None):
    """Call trigger_restart(delay=0) without spawning a real thread: capture
    the daemon-thread target it would have started and run it inline, with
    os.kill/os._exit mocked so nothing here actually touches a real process."""
    with patch("api.restart.os._exit") as exit_mock, \
         patch("api.restart.os.kill", side_effect=kill_side_effect) as kill_mock, \
         patch("api.restart.threading.Thread") as thread_cls, \
         patch("api.restart.os.getpid", lambda: getpid), \
         patch("api.restart.os.getppid", lambda: getppid):
        trigger_restart(delay=0)
        thread_cls.assert_called_once()
        thread_cls.call_args.kwargs["target"]()
    return kill_mock, exit_mock


def test_worker_signals_its_reload_supervisor_before_exiting():
    kill_mock, exit_mock = _run_die(getpid=8, getppid=1)
    kill_mock.assert_called_once_with(1, signal.SIGTERM)
    exit_mock.assert_called_once_with(0)


def test_pid1_process_exits_without_signalling_anyone():
    kill_mock, exit_mock = _run_die(getpid=1, getppid=0)
    kill_mock.assert_not_called()
    exit_mock.assert_called_once_with(0)


def test_a_dead_parent_does_not_stop_self_exit():
    kill_mock, exit_mock = _run_die(
        getpid=8, getppid=1, kill_side_effect=OSError("no such process"))
    kill_mock.assert_called_once_with(1, signal.SIGTERM)
    exit_mock.assert_called_once_with(0)
