"""Cross-platform process liveness helpers."""

from __future__ import annotations


def is_process_alive(pid: int) -> bool:
    """Return whether the process is active without signaling it on Windows."""
    import os

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # Access denied still proves that the PID exists. Treating it
                # as dead would interrupt a live build or kill its preview.
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
            try:
                exit_code = wintypes.DWORD()
                return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
                    exit_code.value == 259  # STILL_ACTIVE
                )
            finally:
                close_handle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX EPERM means the process exists but cannot be signaled.
        return True
    except (OSError, OverflowError, TypeError, ValueError):
        return False
    return True
