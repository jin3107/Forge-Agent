"""Process-tree isolation and termination for spawned commands.

POSIX: the command runs in its own session, and the stop paths signal the process group.

Windows: ``cmd.exe /c`` only wraps the real command, so killing the direct child leaves
the command running. Each child is therefore placed in a Job Object. The child is started
suspended, assigned to the job and then resumed, so no descendant can be created before the
job contains it. Stopping terminates the whole job and then checks the job's active-process
count, so a caller can tell a confirmed stop from an unconfirmed one. The job does not kill
anything when it is closed: like on POSIX, a command that exits on its own leaves whatever it
detached running; only an explicit stop ends the tree.

Job Objects were chosen over ``taskkill /T`` because taskkill walks parent-PID links at the
moment it runs: a grandchild whose parent already exited is not found, and PIDs can be reused.
Job membership is kept by the kernel and survives both.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# Only Windows needs a creationflags value; it is passed to the asyncio spawn helpers.
_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def spawn_options() -> dict:
    """Keyword arguments for ``asyncio.create_subprocess_*`` that isolate the command."""
    if os.name == "posix":
        return {"start_new_session": True}
    if IS_WINDOWS:
        return {"creationflags": _CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP}
    return {}


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _JobObjectBasicAccountingInformation = 1

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, wintypes.LPVOID]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class ProcessTree:
    """Handle to everything one spawned command started. Create with :func:`adopt`."""

    def __init__(self, pid: int, job=None):
        self.pid = pid
        self._job = job

    def active_count(self) -> int | None:
        """Number of live processes in the tree on Windows; ``None`` when it cannot be known."""
        if not self._job:
            return None
        info = _BasicAccounting()
        if not _k32.QueryInformationJobObject(self._job, _JobObjectBasicAccountingInformation,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return int(info.ActiveProcesses)

    def terminate(self) -> bool:
        """Kill the whole tree. Returns True when the kill request was delivered."""
        if self._job:
            return bool(_k32.TerminateJobObject(self._job, 1))
        try:
            os.killpg(self.pid, signal.SIGKILL)
            return True
        except (AttributeError, OSError):
            return False

    def wait_gone(self, timeout: float = 5.0) -> bool:
        """Poll until no process of the tree is alive. False means still running or unknown."""
        deadline = time.monotonic() + timeout
        while True:
            n = self.active_count()
            if n == 0:
                return True
            if n is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    async def confirm_gone(self, timeout: float = 5.0) -> bool:
        """Async form of :meth:`wait_gone` that does not block the event loop."""
        deadline = time.monotonic() + timeout
        while True:
            n = self.active_count()
            if n == 0:
                return True
            if n is None or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 — interpreter shutdown
            pass

    def close(self) -> None:
        """Release the job handle; processes still inside keep running."""
        job, self._job = self._job, None
        if job:
            _k32.CloseHandle(job)


def adopt(proc) -> ProcessTree | None:
    """Contain a child started with :func:`spawn_options` and let it run.

    On Windows the child is suspended until it is inside the job, then resumed. On failure the
    child is killed and ``None`` is returned, so a command never runs outside its job. On POSIX
    the process group already exists and this just wraps it.
    """
    if not IS_WINDOWS:
        return ProcessTree(proc.pid)
    job = handle = None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        handle = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME,
                                  False, proc.pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        if not _k32.AssignProcessToJobObject(job, handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        if _ntdll.NtResumeProcess(handle) != 0:
            raise OSError("NtResumeProcess failed")
        tree = ProcessTree(proc.pid, job)
        job = None
        return tree
    except OSError:
        # Never leave a suspended or uncontained command behind.
        if job:
            _k32.TerminateJobObject(job, 1)
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        return None
    finally:
        if handle:
            _k32.CloseHandle(handle)
        if job:
            _k32.CloseHandle(job)


def tree_of(proc) -> ProcessTree | None:
    """The Windows job tree attached to ``proc`` by :func:`spawn_exec`/:func:`spawn_shell`."""
    tree = getattr(proc, "_forge_tree", None)
    return tree if isinstance(tree, ProcessTree) and tree._job else None


def _attach(proc) -> None:
    if not IS_WINDOWS or not isinstance(proc, asyncio.subprocess.Process):
        return  # not a process this module started (e.g. a test double): nothing to contain
    tree = adopt(proc)
    if tree is None:
        try:
            proc.kill()  # adopt() already did; this guards any caller that returns None
        except (OSError, ProcessLookupError):
            pass
        raise OSError("could not contain the command in a job object")
    proc._forge_tree = tree


async def spawn_exec(*args, **kwargs):
    """``asyncio.create_subprocess_exec`` plus isolation; the command never runs uncontained."""
    proc = await asyncio.create_subprocess_exec(*args, **kwargs, **spawn_options())
    _attach(proc)
    return proc


async def spawn_shell(command, **kwargs):
    """``asyncio.create_subprocess_shell`` plus isolation; the command never runs uncontained."""
    proc = await asyncio.create_subprocess_shell(command, **kwargs, **spawn_options())
    _attach(proc)
    return proc


def terminate_windows_tree(proc) -> bool | None:
    """Kill the whole tree on Windows. None: no tree attached; else whether the request was delivered."""
    tree = tree_of(proc)
    return None if tree is None else tree.terminate()
