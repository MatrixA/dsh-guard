"""Native Windows primitives for DSH supervision, using only the stdlib.

Importing on other platforms is supported; Win32 operations raise OSError there.
Paths must be ordinary absolute local drive paths. Reparse points (including
junctions and cloud placeholders) are refused. Ancestors are held open without
write/delete sharing during file operations to resist junction/rename races.
The caller must still control directory ACLs: MoveFileExW is a name-based API,
and cannot exclude a hostile writer swapping a leaf name between check and move.
POSIX mode bits do not establish Windows permissions; inherited Windows ACLs do.

Replacement uses MOVEFILE_WRITE_THROUGH, not a POSIX directory fsync equivalent.
Flush file contents before replacement. Neither that flag nor sync_directory
promises recovery of metadata after power loss on every filesystem/device.

Persistent children need their own console and successful job breakaway. Keep
that console proof separately from identity, and prove listener ownership before
calling graceful_stop: CTRL+C is broadcast to the entire attached console.
"""

import base64
from collections import OrderedDict
import contextlib
import ctypes
import errno
import json
import math
import ntpath
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

__all__ = ['open_regular', 'exclusive', 'replace_file', 'sync_directory',
           'is_link', 'identity', 'listener_pids', 'persistent_options',
           'graceful_stop', 'environment', 'GuardJob', 'wait_guard_job', 'process_running']

# ctypes.wintypes.DWORD follows the host C long on non-Windows systems.
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_PTR = ctypes.c_void_p
_WSTR = ctypes.c_wchar_p
_PD = ctypes.POINTER(_DWORD)
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_REPARSE = 0x400
_DIRECTORY = 0x10
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_READ_ATTRIBUTES = 0x80
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_INSUFFICIENT_BUFFER = 122
_STILL_ACTIVE = 259
_WAIT_TIMEOUT = 258
_PROCESS_ACCESS = 0x1000 | 0x00100000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
_CACHE_LIMIT = 128
_COMMAND_LIMIT = 32767
_COMMAND_CACHE = OrderedDict()
# ponytail: one lock serializes CIM queries; split per PID if polling throughput matters.
_IDENTITY_LOCK = threading.Lock()


class _FILETIME(ctypes.Structure):
    _fields_ = [('low', _DWORD), ('high', _DWORD)]


class _FILE_INFORMATION(ctypes.Structure):
    _fields_ = [('attributes', _DWORD), ('creation', _FILETIME),
                ('access', _FILETIME), ('write', _FILETIME), ('volume', _DWORD),
                ('size_high', _DWORD), ('size_low', _DWORD), ('links', _DWORD),
                ('index_high', _DWORD), ('index_low', _DWORD)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', _DWORD), ('offset_high', _DWORD), ('event', _HANDLE)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('sid', _PTR), ('attributes', _DWORD)]


class _JOB_BASIC_LIMITS(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', _DWORD), ('minimum_working_set', ctypes.c_size_t),
                ('maximum_working_set', ctypes.c_size_t), ('active_processes', _DWORD),
                ('affinity', ctypes.c_size_t), ('priority', _DWORD), ('scheduling', _DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [('read_operations', ctypes.c_uint64), ('write_operations', ctypes.c_uint64),
                ('other_operations', ctypes.c_uint64), ('read_bytes', ctypes.c_uint64),
                ('write_bytes', ctypes.c_uint64), ('other_bytes', ctypes.c_uint64)]


class _JOB_EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [('basic', _JOB_BASIC_LIMITS), ('io', _IO_COUNTERS),
                ('process_memory_limit', ctypes.c_size_t), ('job_memory_limit', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class _TCP4_ROW(ctypes.Structure):
    _fields_ = [('state', _DWORD), ('local_address', _DWORD),
                ('local_port', _DWORD), ('remote_address', _DWORD),
                ('remote_port', _DWORD), ('pid', _DWORD)]


class _TCP6_ROW(ctypes.Structure):
    _fields_ = [('local_address', ctypes.c_ubyte * 16), ('local_scope', _DWORD),
                ('local_port', _DWORD), ('remote_address', ctypes.c_ubyte * 16),
                ('remote_scope', _DWORD), ('remote_port', _DWORD),
                ('state', _DWORD), ('pid', _DWORD)]


if os.name == 'nt':
    import msvcrt

    def _bind(dll, name, arguments, result=_BOOL):
        function = getattr(dll, name)
        function.argtypes = arguments
        function.restype = result
        return function

    # Search only the system directory, never cwd or the inherited PATH.
    _kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
    _security = ctypes.WinDLL('advapi32.dll', use_last_error=True, winmode=0x800)
    _iphelper = ctypes.WinDLL('iphlpapi.dll', use_last_error=True, winmode=0x800)
    _CreateFileW = _bind(_kernel, 'CreateFileW',
                        [_WSTR, _DWORD, _DWORD, _PTR, _DWORD, _DWORD, _HANDLE], _HANDLE)
    _CloseHandle = _bind(_kernel, 'CloseHandle', [_HANDLE])
    _GetFileInformationByHandle = _bind(_kernel, 'GetFileInformationByHandle',
                                      [_HANDLE, ctypes.POINTER(_FILE_INFORMATION)])
    _GetFileType = _bind(_kernel, 'GetFileType', [_HANDLE], _DWORD)
    _GetDriveTypeW = _bind(_kernel, 'GetDriveTypeW', [_WSTR], _DWORD)
    _LockFileEx = _bind(_kernel, 'LockFileEx',
                       [_HANDLE, _DWORD, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_OVERLAPPED)])
    _UnlockFileEx = _bind(_kernel, 'UnlockFileEx',
                         [_HANDLE, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_OVERLAPPED)])
    _MoveFileExW = _bind(_kernel, 'MoveFileExW', [_WSTR, _WSTR, _DWORD])
    _OpenProcess = _bind(_kernel, 'OpenProcess', [_DWORD, _BOOL, _DWORD], _HANDLE)
    _GetProcessTimes = _bind(_kernel, 'GetProcessTimes',
                            [_HANDLE] + [ctypes.POINTER(_FILETIME)] * 4)
    _GetExitCodeProcess = _bind(_kernel, 'GetExitCodeProcess', [_HANDLE, _PD])
    _WaitForSingleObject = _bind(_kernel, 'WaitForSingleObject', [_HANDLE, _DWORD], _DWORD)
    _QueryFullProcessImageNameW = _bind(_kernel, 'QueryFullProcessImageNameW',
                                      [_HANDLE, _DWORD, _WSTR, _PD])
    _OpenProcessToken = _bind(_security, 'OpenProcessToken',
                              [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)])
    _GetTokenInformation = _bind(_security, 'GetTokenInformation',
                                 [_HANDLE, ctypes.c_int32, _PTR, _DWORD, _PD])
    _ConvertSidToStringSidW = _bind(_security, 'ConvertSidToStringSidW',
                                  [_PTR, ctypes.POINTER(_WSTR)])
    _LocalFree = _bind(_kernel, 'LocalFree', [_PTR], _PTR)
    _GetSystemDirectoryW = _bind(_kernel, 'GetSystemDirectoryW', [_WSTR, _DWORD], _DWORD)
    _GetExtendedTcpTable = _bind(_iphelper, 'GetExtendedTcpTable',
                                 [_PTR, _PD, _BOOL, _DWORD, ctypes.c_int32, _DWORD], _DWORD)
    _CreateJobObjectW = _bind(_kernel, 'CreateJobObjectW', [_PTR, _WSTR], _HANDLE)
    _SetInformationJobObject = _bind(_kernel, 'SetInformationJobObject',
                                     [_HANDLE, ctypes.c_int32, _PTR, _DWORD])
    _AssignProcessToJobObject = _bind(_kernel, 'AssignProcessToJobObject', [_HANDLE, _HANDLE])
    _OpenJobObjectW = _bind(_kernel, 'OpenJobObjectW', [_DWORD, _BOOL, _WSTR], _HANDLE)
    _IsProcessInJob = _bind(_kernel, 'IsProcessInJob',
                           [_HANDLE, _HANDLE, ctypes.POINTER(_BOOL)])
    _GetCurrentProcess = _bind(_kernel, 'GetCurrentProcess', [], _HANDLE)


def _require_windows():
    if os.name != 'nt':
        raise OSError(errno.ENOSYS, 'This operation requires Windows')


def _winerror(code=None):
    return ctypes.WinError(ctypes.get_last_error() if code is None else code)


def _normal_path(path):
    """Validate before normalization so traversal and Win32 aliases cannot vanish."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise OSError(errno.EINVAL, 'A Unicode Windows path is required')
    value = value.replace('/', '\\')
    if (len(value) < 3 or not value[0].isascii() or not value[0].isalpha()
            or value[1:3] != ':\\'):
        raise OSError(errno.EINVAL, 'An absolute local drive path is required')
    parts = value[3:].split('\\')
    if parts and parts[-1] == '':
        parts.pop()  # A trailing separator is permitted for directories.
    for part in parts:
        if (not part or part in ('.', '..') or part[-1] in '. '
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|'
                                r'COM[1-9¹²³]|LPT[1-9¹²³])', part.split('.')[0].rstrip(' '))):
            raise OSError(errno.EINVAL, 'Unsafe Windows path component')
    return value[:2].upper() + '\\' + '\\'.join(parts)


def _local_path(path):
    path = _normal_path(path)
    drive_type = _GetDriveTypeW(path[:3])
    if drive_type == 1:  # DRIVE_NO_ROOT_DIR
        raise FileNotFoundError(errno.ENOENT, 'Drive does not exist', path)
    if drive_type not in (2, 3, 5, 6):  # Removable, fixed, CD-ROM, RAM disk.
        raise OSError(errno.ENODEV, 'A local Windows drive is required', path)
    return path


def _close(handle):
    if not _CloseHandle(handle):
        raise _winerror()


def _create(path, access=_READ_ATTRIBUTES, share=_SHARE_ALL, disposition=_OPEN_EXISTING):
    handle = _CreateFileW(path, access, share, None, disposition,
                          _OPEN_REPARSE | _BACKUP_SEMANTICS | 0x80, None)
    if handle == _INVALID_HANDLE or handle is None:
        raise _winerror()
    return handle


def _check_handle(handle, path, directory=False):
    info = _FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _winerror()
    if info.attributes & _REPARSE:
        raise OSError(errno.ELOOP, 'Reparse point refused', path)
    if bool(info.attributes & _DIRECTORY) != directory:
        if directory:
            raise NotADirectoryError(errno.ENOTDIR, 'Directory required', path)
        raise IsADirectoryError(errno.EISDIR, 'Regular file required', path)
    if _GetFileType(handle) != 1:  # FILE_TYPE_DISK, never a device or pipe.
        raise OSError(errno.EINVAL, 'Local disk object required', path)


@contextlib.contextmanager
def _parents(path):
    """Hold each checked ancestor before opening the next path component."""
    path = _local_path(path)
    parent = ntpath.dirname(path)
    ancestors = [path[:3]]
    for component in parent[3:].split('\\'):
        if component:
            ancestors.append(ntpath.join(ancestors[-1], component))
    with contextlib.ExitStack() as stack:
        for ancestor in ancestors:
            # No WRITE/DELETE sharing: prevent ancestor rename or reparse conversion.
            handle = _create(ancestor, share=1)
            stack.callback(_close, handle)
            _check_handle(handle, ancestor, directory=True)
        yield path


def open_regular(path, flags, mode=0o600):
    """Open a nonreparse regular file; the returned noninheritable CRT fd owns it.

    Supports access/create/exclusive/append flags, never truncation. mode is an
    API compatibility argument; Windows default/inherited ACLs set permissions.
    The leaf handle shares read, write and delete, allowing atomic replacement.
    """
    _require_windows()
    allowed = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND
    allowed |= getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
    if (type(flags) is not int or flags < 0 or flags & ~allowed
            or flags & 3 == 3 or (flags & os.O_EXCL and not flags & os.O_CREAT)
            or (flags & os.O_APPEND and flags & 3 == os.O_RDONLY)
            or type(mode) is not int or mode < 0):
        raise OSError(errno.EINVAL, 'Unsupported open flags or mode (truncation is forbidden)')
    access = _READ_ATTRIBUTES  # Inspect even write-only handles without following links.
    if flags & 3 != os.O_WRONLY:
        access |= 0x80000000  # GENERIC_READ
    if flags & 3 != os.O_RDONLY:
        # Append-only kernel access also prevents overwrites through the native handle.
        access |= (0x4 | _READ_ATTRIBUTES | 0x00100000) if flags & os.O_APPEND else 0x40000000
    disposition = (1 if flags & os.O_EXCL else 4) if flags & os.O_CREAT else _OPEN_EXISTING
    with _parents(path) as path:
        handle = _create(path, access, disposition=disposition)
        fd = None
        try:
            _check_handle(handle, path)
            crt_flags = (flags & (3 | os.O_APPEND)) | os.O_BINARY | os.O_NOINHERIT
            fd = msvcrt.open_osfhandle(handle, crt_flags)
            handle = None  # Ownership passed to the CRT, including on later errors.
            os.set_inheritable(fd, False)
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        finally:
            if handle is not None:
                _close(handle)


@contextlib.contextmanager
def exclusive(path):
    """Nonblocking exclusive byte-range lock; an empty sentinel needs no writes."""
    _require_windows()
    fd = open_regular(path, os.O_RDWR | os.O_CREAT)
    try:
        handle = msvcrt.get_osfhandle(fd)
        overlapped = _OVERLAPPED()  # Offset zero; Windows permits locks beyond EOF.
        if not _LockFileEx(handle, 0x1 | 0x2, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EWOULDBLOCK, 'Lock is already held') from None
            raise _winerror(error)
        try:
            yield fd
        finally:
            if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                raise _winerror()
    finally:
        os.close(fd)


def replace_file(source, target):
    """Replace a regular file with bounded sharing-violation retries (0.75 s).

    Source and target must be on the same volume: no copy/delete fallback. Flush
    source contents first. WRITE_THROUGH is requested, but does not provide the
    POSIX file-fsync + rename + directory-fsync power-loss contract.
    """
    _require_windows()
    with _parents(source) as source, _parents(target) as target:
        for attempt in range(6):
            try:
                with contextlib.ExitStack() as stack:
                    for path in (source, target):
                        try:
                            handle = _create(path)
                        except FileNotFoundError:
                            if path == target:
                                continue
                            raise
                        stack.callback(_close, handle)
                        _check_handle(handle, path)
                    if not _MoveFileExW(source, target, 0x1 | 0x8):
                        raise _winerror()
                return
            except OSError as error:
                if getattr(error, 'winerror', None) != 32 or attempt == 5:
                    raise
                time.sleep(.05 * (attempt + 1))


def sync_directory(path):
    """Validate a local nonreparse directory, without claiming directory fsync.

    Windows metadata durability differs from POSIX. No FlushFileBuffers call on
    a directory is presented as equivalent; replace_file requests write-through.
    This validation-only operation adds no power-loss durability guarantee.
    """
    _require_windows()
    with _parents(path) as path:
        handle = _create(path, share=1)
        try:
            _check_handle(handle, path, directory=True)
        finally:
            _close(handle)


def is_link(path):
    """True for symlinks, junctions and any lstat-reported reparse point."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return bool(stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE)


def _pid(value):
    # Decimal strings are allowed for callers reading a CLI PID; bool/float are not.
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value):
        value = int(value)
    if type(value) is not int or not 0 < value <= 0xffffffff:
        raise ValueError('Invalid Windows PID')
    return value


def _alive(handle):
    code = _DWORD()
    if not _GetExitCodeProcess(handle, ctypes.byref(code)):
        raise _winerror()
    wait = _WaitForSingleObject(handle, 0)
    if wait == 0xffffffff:
        raise _winerror()
    # A process may actually exit with code STILL_ACTIVE (259); also check the handle.
    return code.value == _STILL_ACTIVE and wait == _WAIT_TIMEOUT


def process_running(pid):
    """False means a proven exit; inspection denial raises instead of implying death."""
    _require_windows()
    pid = _pid(pid)
    handle = _OpenProcess(_PROCESS_ACCESS, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in (87, 1168):  # Invalid PID / no process found.
            return False
        raise _winerror(error)
    try:
        return _alive(handle)
    finally:
        _close(handle)


def _native_facts(handle):
    if not _alive(handle):
        return None
    created, exited, kernel, user = (_FILETIME() for _ in range(4))
    if not _GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                           ctypes.byref(kernel), ctypes.byref(user)):
        raise _winerror()
    ticks = (created.high << 32) | created.low
    image = ctypes.create_unicode_buffer(32768)
    size = _DWORD(len(image))
    if not _QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        raise _winerror()
    if not 0 < size.value < len(image):
        return None
    executable = image[:size.value]
    token = _HANDLE()
    if not _OpenProcessToken(handle, 0x8, ctypes.byref(token)):  # TOKEN_QUERY
        raise _winerror()
    try:
        needed = _DWORD()
        if _GetTokenInformation(token, 1, None, 0, ctypes.byref(needed)):
            raise OSError(errno.EIO, 'Invalid TokenUser sizing response')
        error = ctypes.get_last_error()
        if error != _INSUFFICIENT_BUFFER:
            raise _winerror(error)
        if not ctypes.sizeof(_SID_AND_ATTRIBUTES) <= needed.value <= 65536:
            raise OSError(errno.EIO, 'Invalid TokenUser size')
        buffer = ctypes.create_string_buffer(needed.value)
        if not _GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise _winerror()
        sid = _SID_AND_ATTRIBUTES.from_buffer(buffer).sid
        text = _WSTR()
        if not _ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _winerror()
        try:
            account = text.value
        finally:
            _LocalFree(ctypes.cast(text, _PTR))
    finally:
        _close(token)
    if not ticks or not account or not executable or not _alive(handle):
        return None
    return ticks, account, executable


def _command_line(pid):
    directory = ctypes.create_unicode_buffer(32768)
    count = _GetSystemDirectoryW(directory, len(directory))
    if not 0 < count < len(directory):
        raise OSError(errno.EIO, 'Cannot locate system PowerShell')
    powershell = ntpath.join(directory.value, 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    # Only a validated decimal PID is substituted. Output is base64 UTF-16LE so
    # console encodings cannot silently alter a Unicode command line.
    script = ("$ErrorActionPreference='Stop';"
              "Import-Module -Name ($PSHOME+'\\Modules\\CimCmdlets\\CimCmdlets.psd1') "
              "-ErrorAction Stop;"
              "$p=CimCmdlets\\Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = "
              + str(pid) + "';"
              "if ($null -eq $p -or [string]::IsNullOrEmpty($p.CommandLine) "
              "-or $p.CommandLine.Length -gt 32767) { exit 1 };"
              "[Console]::Out.Write([Convert]::ToBase64String("
              "[Text.Encoding]::Unicode.GetBytes($p.CommandLine)))")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run([powershell, '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-EncodedCommand', encoded], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=8, env=environment(), close_fds=True,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW, CIM helper only.
    if result.returncode or not result.stdout or len(result.stdout) > 4 * _COMMAND_LIMIT:
        return None
    command = base64.b64decode(result.stdout.strip(), validate=True).decode('utf-16-le')
    if not command or '\0' in command or len(command) > _COMMAND_LIMIT:
        return None
    return command


def identity(pid):
    """PID + creation FILETIME ticks, account SID, executable and full command.

    Native facts and liveness are checked before and after CIM retrieval (or a
    cache lookup), holding the process handle throughout. Any query ambiguity
    returns None. Commands are cached snapshots, not observations of later PEB
    command-line edits; no command line or query diagnostic is logged.
    """
    _require_windows()
    try:
        pid = _pid(pid)
        with _IDENTITY_LOCK:
            handle = _OpenProcess(_PROCESS_ACCESS, False, pid)
            if not handle:
                return None
            try:
                before = _native_facts(handle)
                if before is None:
                    return None
                key = (pid, before[0])
                cached = _COMMAND_CACHE.get(key)
                if cached is not None and cached[:2] != before[1:]:
                    _COMMAND_CACHE.pop(key, None)
                    return None
                command = cached[2] if cached is not None else _command_line(pid)
                if command is None or _native_facts(handle) != before:
                    _COMMAND_CACHE.pop(key, None)
                    return None
                _COMMAND_CACHE[key] = (*before[1:], command)
                _COMMAND_CACHE.move_to_end(key)
                while len(_COMMAND_CACHE) > _CACHE_LIMIT:
                    _COMMAND_CACHE.popitem(last=False)
                signature = json.dumps(['win32-v1', *before, command], separators=(',', ':'))
                return {'pid': pid, 'signature': signature}
            finally:
                _close(handle)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None


def _listener_rows(family, row_type):
    size = _DWORD()
    error = _GetExtendedTcpTable(None, ctypes.byref(size), False, family, 3, 0)
    if error != _INSUFFICIENT_BUFFER:
        if error:
            raise _winerror(error)
        raise OSError(errno.EIO, 'Missing TCP listener table')
    for _ in range(5):
        if not 4 <= size.value <= 16 * 1024 * 1024:
            raise OSError(errno.EIO, 'Invalid TCP listener table size')
        buffer = ctypes.create_string_buffer(size.value)
        error = _GetExtendedTcpTable(buffer, ctypes.byref(size), False, family, 3, 0)
        if error == _INSUFFICIENT_BUFFER:
            continue
        if error:
            raise _winerror(error)
        if not 4 <= size.value <= len(buffer):
            raise OSError(errno.EIO, 'Invalid TCP listener table response')
        count = _DWORD.from_buffer(buffer).value
        if count > (size.value - 4) // ctypes.sizeof(row_type):
            raise OSError(errno.EIO, 'Truncated TCP listener table')
        return [row_type.from_buffer_copy(buffer, 4 + i * ctypes.sizeof(row_type))
                for i in range(count)]
    raise OSError(errno.EAGAIN, 'TCP listener table kept changing')


def listener_pids(port):
    """All interface/wildcard/loopback TCP listeners, IPv4 AND IPv6; fail closed."""
    _require_windows()
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid TCP port')
    owners = set()
    # Windows AF_INET6 is 23, unlike several Unix socket.AF_INET6 constants.
    for family, row_type in ((2, _TCP4_ROW), (23, _TCP6_ROW)):
        for row in _listener_rows(family, row_type):
            if socket.ntohs(row.local_port & 0xffff) == port:
                if row.state != 2:  # MIB_TCP_STATE_LISTEN
                    raise OSError(errno.EIO, 'Unexpected non-listener in TCP table')
                owners.add(int(row.pid))
    return sorted(owners)


def persistent_options():
    """Popen options requiring an independent hidden console and job breakaway.

    A failed breakaway must propagate from Popen; there is no weaker fallback.
    CREATE_NEW_PROCESS_GROUP is deliberately absent, to preserve CTRL+C handling.
    """
    _require_windows()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= 0x1  # STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE, console still exists and can be attached.
    return {'creationflags': 0x10 | 0x01000000, 'startupinfo': startup,
            'close_fds': True}


class GuardJob:
    """Launcher-owned job for one transient Popen guard, not persistent DSH.

    Create before Popen, pass DSH_GUARD_JOB=self.name, then assign immediately.
    The guard must call wait_guard_job before starting DSH, whose Popen must use
    persistent_options. Assignment failure propagates; the launcher then stops
    only its own Popen guard. Closing this handle kills assigned job processes.
    """

    def __init__(self):
        _require_windows()
        self._handle = None
        self._assigned = False
        self.name = 'Local\\dsh-guard-' + uuid.uuid4().hex
        ctypes.set_last_error(0)
        handle = _CreateJobObjectW(None, self.name)  # NULL security: noninheritable.
        if not handle:
            raise _winerror()
        error = ctypes.get_last_error()
        try:
            if error == 183:  # ERROR_ALREADY_EXISTS: never modify someone else's job.
                raise FileExistsError(errno.EEXIST, 'Guard job name already exists')
            limits = _JOB_EXTENDED_LIMITS()
            limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK.
            if not _SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise _winerror()
        except BaseException:
            _close(handle)
            raise
        self._handle = handle

    def assign(self, child):
        """Assign exactly one owned Popen handle; a stored PID is never authority."""
        if self._handle is None:
            raise OSError(errno.EBADF, 'Guard job is closed')
        if self._assigned:
            raise OSError(errno.EALREADY, 'Guard job already owns its guard')
        if not isinstance(child, subprocess.Popen) or getattr(child, '_handle', None) is None:
            raise TypeError('Guard job assignment requires a Windows Popen child')
        if child.poll() is not None:
            raise ProcessLookupError(errno.ESRCH, 'Guard exited before job assignment')
        # Popen retains the authoritative CreateProcess handle; never reopen by PID.
        if not _AssignProcessToJobObject(self._handle, int(child._handle)):
            raise _winerror()
        self._assigned = True

    def close(self):
        """Idempotently release the launcher's handle (kill-on-last-close)."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _close(handle)

    def __enter__(self):
        if self._handle is None:
            raise OSError(errno.EBADF, 'Guard job is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def wait_guard_job(name, timeout=5):
    """Startup gate: wait until this process belongs to the launcher's named job.

    Probe handles are closed before returning and between polls, so the guard
    cannot keep its own kill-on-close job alive after abrupt launcher death.
    Missing/inaccessible jobs and failed queries raise OSError; no launch fallback.
    """
    _require_windows()
    if not isinstance(name, str) or not re.fullmatch(r'Local\\dsh-guard-[0-9a-f]{32}', name):
        raise OSError(errno.EINVAL, 'Invalid guard job name')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0):
        raise OSError(errno.EINVAL, 'Invalid guard job wait timeout')
    deadline = time.monotonic() + timeout
    while True:
        handle = _OpenJobObjectW(0x4, False, name)  # JOB_OBJECT_QUERY, noninheritable.
        if not handle:
            raise _winerror()
        try:
            assigned = _BOOL()
            if not _IsProcessInJob(_GetCurrentProcess(), handle, ctypes.byref(assigned)):
                raise _winerror()
        finally:
            _close(handle)
        if assigned.value:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(errno.ETIMEDOUT, 'Guard job assignment timed out')
        time.sleep(min(.02, remaining))


# Fixed isolated helper: no module/path imports, shell, credentials, or PID text
# interpolation. The held process handle prevents PID reuse during attachment.
_STOP_HELPER = r'''
import ctypes
import sys
import time
D = ctypes.c_uint32
B = ctypes.c_int32
H = ctypes.c_void_p
class FT(ctypes.Structure):
    _fields_ = [('low', D), ('high', D)]
k = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
def bind(name, args, result=B):
    f = getattr(k, name)
    f.argtypes, f.restype = args, result
    return f
open_process = bind('OpenProcess', [D, B, D], H)
close = bind('CloseHandle', [H])
times = bind('GetProcessTimes', [H] + [ctypes.POINTER(FT)] * 4)
exit_code = bind('GetExitCodeProcess', [H, ctypes.POINTER(D)])
wait = bind('WaitForSingleObject', [H, D], D)
free = bind('FreeConsole', [])
attach = bind('AttachConsole', [D])
ignore = bind('SetConsoleCtrlHandler', [ctypes.c_void_p, B])
generate = bind('GenerateConsoleCtrlEvent', [D, D])
def main():
    if len(sys.argv) != 3 or not all(v.isascii() and v.isdecimal() for v in sys.argv[1:]):
        return 1
    pid, creation = map(int, sys.argv[1:])
    if not 0 < pid <= 0xffffffff or not 0 < creation <= 0xffffffffffffffff:
        return 1
    handle = open_process(0x1000 | 0x00100000, False, pid)
    if not handle:
        return 1
    attached = False
    def matches():
        created, ended, kernel, user = (FT() for _ in range(4))
        code = D()
        return (times(handle, ctypes.byref(created), ctypes.byref(ended),
                      ctypes.byref(kernel), ctypes.byref(user))
                and ((created.high << 32) | created.low) == creation
                and exit_code(handle, ctypes.byref(code)) and code.value == 259
                and wait(handle, 0) == 258)
    try:
        if not matches() or not free() or not attach(pid):
            return 1
        attached = True
        # AttachConsole resets handlers; ignore our own CTRL+C only after attach.
        if not ignore(None, True) or not matches():
            return 1
        if not generate(0, 0):
            return 1
        time.sleep(0.2)  # Allow asynchronous console dispatch before detaching.
        return 0
    finally:
        if attached:
            free()
        close(handle)
try:
    sys.exit(main())
except Exception:
    sys.exit(1)
'''


def _record_ticks(record):
    if (not isinstance(record, dict) or set(record) != {'pid', 'signature'}
            or type(record['pid']) is not int or not isinstance(record['signature'], str)):
        raise ValueError('Invalid Windows process identity')
    pid = _pid(record['pid'])
    fields = json.loads(record['signature'])
    if (not isinstance(fields, list) or len(fields) != 5 or fields[0] != 'win32-v1'
            or type(fields[1]) is not int or not 0 < fields[1] <= 0xffffffffffffffff
            or any(not isinstance(v, str) or not v or '\0' in v for v in fields[2:])):
        raise ValueError('Invalid Windows process signature')
    return pid, fields[1]


def graceful_stop(record):
    """Dispatch CTRL+C only for an equal current identity; never force termination.

    The caller must prove this is its independent console and owned listener.
    Successful dispatch does not prove shutdown; the caller must wait and verify.
    Failures (including helper timeouts) raise generic OSError without command
    lines, inherited credentials or captured diagnostics. No target kill fallback.
    """
    _require_windows()
    try:
        pid, ticks = _record_ticks(record)
    except (ValueError, TypeError, KeyError):
        raise OSError(errno.EINVAL, 'Invalid Windows process identity') from None
    if identity(pid) != record:
        raise ProcessLookupError(errno.ESRCH, 'Process identity could not be verified')
    try:
        result = subprocess.run([sys.executable, '-I', '-c', _STOP_HELPER, str(pid), str(ticks)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=environment(),
                                creationflags=0x10, close_fds=True,
                                startupinfo=persistent_options()['startupinfo'], timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise OSError(errno.EIO, 'Windows graceful-stop helper failed') from None
    if result.returncode:
        raise OSError(errno.EIO, 'Windows console identity or CTRL+C dispatch could not be verified')


_ENV_KEYS = ('SystemRoot', 'WINDIR', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
             'LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT',
             'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)')


def environment():
    """Allowlisted Windows environment, canonical keys and case-insensitive lookup."""
    values = {key.upper(): value for key, value in os.environ.items()}
    return {key: values[key.upper()] for key in _ENV_KEYS if key.upper() in values}


def _self_check():
    """Portable checks only: python -I windows_platform.py (no Win32 execution)."""
    assert ctypes.sizeof(_DWORD) == 4
    assert ctypes.sizeof(_FILE_INFORMATION) == 52
    assert ctypes.sizeof(_TCP4_ROW) == 24 and ctypes.sizeof(_TCP6_ROW) == 56
    assert ctypes.sizeof(_IO_COUNTERS) == 48
    if ctypes.sizeof(_PTR) == 8:
        assert ctypes.sizeof(_JOB_BASIC_LIMITS) == 64
        assert ctypes.sizeof(_JOB_EXTENDED_LIMITS) == 144
    assert _normal_path('c:/Users/person/file.json') == 'C:\\Users\\person\\file.json'
    assert _normal_path('C:\\') == 'C:\\'
    for bad in ('file', 'C:file', '\\file', '//server/share/file', '//?/C:/file',
                '//./C:/file', 'C:/file:stream', 'C:/a/../file', 'C:/NUL.json',
                'C:/dir./file', 'C:/dir /file', 'C:/a//file', 'C:/x\0y'):
        try:
            _normal_path(bad)
        except OSError:
            pass
        else:
            raise AssertionError('Unsafe Windows path accepted')
    record = {'pid': 42, 'signature': json.dumps(['win32-v1', 123, 'S-1-5-18',
                                                'C:\\node.exe', 'node app.js'])}
    assert _record_ticks(record) == (42, 123)
    compile(_STOP_HELPER, '<windows-stop-helper>', 'exec')
    if os.name != 'nt':
        try:
            listener_pids(3080)
        except OSError as error:
            assert error.errno == errno.ENOSYS
        else:
            raise AssertionError('Win32 operation accepted on another platform')


if __name__ == '__main__':
    _self_check()
