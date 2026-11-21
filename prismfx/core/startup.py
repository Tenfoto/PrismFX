"""Windows Startup & Administrator Privilege Manager (Headless — Zero UI Imports).

Supports two official, transparent Windows startup modes (mirroring Stardock WindowFX):
  1. Standard User Auto-Start via HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
     (instant, zero UAC prompt, shows cleanly in Windows Task Manager Startup Apps).
  2. Elevated Auto-Start via Windows Task Scheduler (`LogonTrigger` + `HighestAvailable`)
     (requires one-time UAC consent when enabled; subsequently launches silently at logon
     with full Administrator privileges and zero UAC popup so PrismFX can also animate
     elevated windows such as Task Manager, Admin PowerShell, and Registry Editor).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import pathlib
import secrets
import subprocess
import sys
import winreg
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

from . import win32_api as w32

RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE_NAME = "PrismFX Studio"
TASK_NAME = "PrismFX Studio Autostart"
CREATE_NO_WINDOW = 0x08000000
SCHTASKS_EXE = str(
    pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
)

# Win32 DACL inspection constants (fail-closed admin-protected path verification)
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5

_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_DELETE_CHILD = 0x0040
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_WRITE = 0x40000000
_GENERIC_ALL = 0x10000000
_UNSAFE_USER_WRITE_MASK = (
    _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_DELETE_CHILD
    | _DELETE
    | _WRITE_DAC
    | _WRITE_OWNER
    | _GENERIC_WRITE
    | _GENERIC_ALL
)

_UNPRIVILEGED_WRITE_SIDS = frozenset({
    "S-1-1-0",       # Everyone
    "S-1-5-4",       # INTERACTIVE
    "S-1-5-7",       # ANONYMOUS LOGON
    "S-1-5-11",      # Authenticated Users
    "S-1-5-32-545",  # BUILTIN\Users
    "S-1-15-2-1",    # ALL APPLICATION PACKAGES
    "S-1-15-2-2",    # ALL RESTRICTED APPLICATION PACKAGES
})


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


_ADVAPI32 = None
_KERNEL32_SEC = None
try:
    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True, winmode=w32.LOAD_LIBRARY_SEARCH_SYSTEM32)
    _ADVAPI32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _ADVAPI32.GetAclInformation.restype = wintypes.BOOL
    _ADVAPI32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.GetAce.restype = wintypes.BOOL
    _ADVAPI32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _KERNEL32_SEC = ctypes.WinDLL("kernel32", use_last_error=True, winmode=w32.LOAD_LIBRARY_SEARCH_SYSTEM32)
    _KERNEL32_SEC.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32_SEC.LocalFree.restype = ctypes.c_void_p
except Exception:
    _ADVAPI32 = None
    _KERNEL32_SEC = None


@dataclass(frozen=True)
class StartupStatus:
    enabled: bool
    mode: str                    # "none" | "registry" | "elevated_task"
    is_admin: bool
    launcher_path: str
    command_line: str
    approved_disabled: bool = False
    elevated_task_allowed: bool = False
    elevated_task_block_reason: str = ""


def current_user_id() -> str:
    """Return `DOMAIN\\Username` for Windows Task Scheduler Principal/Trigger."""
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    if domain and user:
        return f"{domain}\\{user}"
    return user or "BUILTIN\\Users"


def _is_under_protected_root(resolved: pathlib.Path) -> bool:
    roots: list[pathlib.Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemRoot"):
        val = os.environ.get(env_key, "").strip()
        if not val:
            continue
        try:
            roots.append(pathlib.Path(val).resolve())
        except OSError:
            continue
    if not roots:
        return False
    for root in roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def _sid_to_string(sid_ptr: ctypes.c_void_p) -> str | None:
    if _ADVAPI32 is None or _KERNEL32_SEC is None or not sid_ptr.value:
        return None
    str_ptr = ctypes.c_void_p()
    if not _ADVAPI32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
        return None
    if not str_ptr.value:
        return None
    try:
        return ctypes.wstring_at(str_ptr.value)
    finally:
        _KERNEL32_SEC.LocalFree(str_ptr)


def is_admin_protected_path(path: pathlib.Path) -> bool:
    """Return True iff `path` is under a protected Windows root AND its DACL blocks non-admin writes.

    Fail-closed: returns False if the path does not exist, cannot be resolved, has a NULL DACL,
    grants any write/modify/ACL permission to Users/Everyone/Authenticated Users, or if DACL
    inspection fails for any reason.
    """
    if _ADVAPI32 is None or _KERNEL32_SEC is None:
        return False
    try:
        resolved = pathlib.Path(path).resolve(strict=True)
    except OSError:
        return False

    if not _is_under_protected_root(resolved):
        return False

    p_dacl = ctypes.c_void_p()
    p_sd = ctypes.c_void_p()
    try:
        rc = _ADVAPI32.GetNamedSecurityInfoW(
            str(resolved),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(p_dacl),
            None,
            ctypes.byref(p_sd),
        )
        if rc != 0 or not p_dacl.value:
            # Non-zero Win32 error OR NULL DACL (grants full access to Everyone) -> fail-closed
            return False

        acl_info = _ACL_SIZE_INFORMATION()
        if not _ADVAPI32.GetAclInformation(
            p_dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            return False

        for idx in range(acl_info.AceCount):
            p_ace = ctypes.c_void_p()
            if not _ADVAPI32.GetAce(p_dacl, idx, ctypes.byref(p_ace)) or not p_ace.value:
                return False
            hdr = _ACE_HEADER.from_address(p_ace.value)
            if hdr.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                if hdr.AceSize < 12:
                    return False
                mask = ctypes.c_uint32.from_address(p_ace.value + 4).value
                if mask & _UNSAFE_USER_WRITE_MASK:
                    sid_str = _sid_to_string(ctypes.c_void_p(p_ace.value + 8))
                    if sid_str is None or sid_str in _UNPRIVILEGED_WRITE_SIDS:
                        return False
            elif hdr.AceType == _ACCESS_ALLOWED_OBJECT_ACE_TYPE:
                if hdr.AceSize < 16:
                    return False
                mask = ctypes.c_uint32.from_address(p_ace.value + 4).value
                if mask & _UNSAFE_USER_WRITE_MASK:
                    obj_flags = ctypes.c_uint32.from_address(p_ace.value + 8).value
                    sid_off = 12 + (16 if (obj_flags & 0x1) else 0) + (16 if (obj_flags & 0x2) else 0)
                    if hdr.AceSize <= sid_off:
                        return False
                    sid_str = _sid_to_string(ctypes.c_void_p(p_ace.value + sid_off))
                    if sid_str is None or sid_str in _UNPRIVILEGED_WRITE_SIDS:
                        return False
        return True
    except Exception:
        return False
    finally:
        if p_sd.value:
            _KERNEL32_SEC.LocalFree(p_sd)


def resolve_launcher(
    repo_root: pathlib.Path,
    elevated_task: bool = False,
) -> tuple[pathlib.Path, list[str], bool]:
    """Resolve the best windowless launcher: `(executable, leading_args, is_native_exe)`."""
    if getattr(sys, "frozen", False):
        return (pathlib.Path(sys.executable).resolve(), [], True)

    ignore_env = bool(elevated_task or w32.is_process_elevated())
    if not ignore_env:
        env_exe = os.environ.get("PRISMFX_LAUNCHER_EXE", "").strip()
        if env_exe:
            p = pathlib.Path(env_exe)
            if p.is_file():
                return (p.resolve(), [], True)

    for candidate in (
        repo_root / "PrismFX.exe",
        repo_root / "dist" / "PrismFX-Portable" / "PrismFX.exe",
    ):
        if candidate.is_file():
            return (candidate.resolve(), [], True)

    # Fallback: windowless pythonw.exe + main.py
    py_exe = pathlib.Path(sys.executable)
    pyw = py_exe.with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = py_exe
    main_py = (repo_root / "main.py").resolve()
    return (pyw.resolve(), [str(main_py)], False)


def can_enable_elevated_task(repo_root: pathlib.Path) -> tuple[bool, str]:
    """Allow HighestAvailable logon task ONLY for Frozen EXE installed in an admin-protected path."""
    if not getattr(sys, "frozen", False):
        return (
            False,
            "Zero-UAC Admin autostart requires the standalone installed EXE under Program Files "
            "(disabled in portable/source mode to prevent privilege escalation).",
        )
    exe, _args, is_native = resolve_launcher(repo_root, elevated_task=True)
    if not is_native or not exe.is_file():
        return (False, "Standalone executable not found for elevated startup task.")
    if not is_admin_protected_path(exe) or not is_admin_protected_path(exe.parent):
        return (
            False,
            f"Install PrismFX under an administrator-protected folder (e.g. C:\\Program Files\\PrismFX) "
            f"to enable Zero-UAC Admin autostart (current folder is user-writable: {exe.parent}).",
        )
    return (True, "")


def _quote_arg(arg: str) -> str:
    return subprocess.list2cmdline([arg])


def build_autostart_command(
    repo_root: pathlib.Path,
    elevated_task: bool = False,
) -> tuple[str, str, str, str]:
    """Return `(command, arguments, working_dir, full_registry_cmd)`."""
    exe, base_args, _is_native = resolve_launcher(repo_root, elevated_task=elevated_task)
    args = [*base_args, "--autostart"]
    arg_str = " ".join(_quote_arg(a) for a in args)
    full_cmd = f'{_quote_arg(str(exe))} {arg_str}'.strip()
    return (str(exe), arg_str, str(exe.parent), full_cmd)


def build_task_scheduler_xml(
    command: str,
    arguments: str,
    working_dir: str,
    user_id: str,
    elevated: bool = True,
) -> str:
    """Build a Windows Task Scheduler 1.2 XML definition for immediate zero-delay logon start."""
    run_level = "HighestAvailable" if elevated else "LeastPrivilege"
    u_esc = xml_escape(user_id)
    cmd_esc = xml_escape(command)
    arg_esc = xml_escape(arguments)
    cwd_esc = xml_escape(working_dir)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        '  <RegistrationInfo>\r\n'
        '    <Description>Starts PrismFX Studio automatically at Windows logon for 165 Hz GPU window animations.</Description>\r\n'
        '    <URI>\\' + xml_escape(TASK_NAME) + '</URI>\r\n'
        '  </RegistrationInfo>\r\n'
        '  <Triggers>\r\n'
        '    <LogonTrigger>\r\n'
        '      <Enabled>true</Enabled>\r\n'
        f'      <UserId>{u_esc}</UserId>\r\n'
        '    </LogonTrigger>\r\n'
        '  </Triggers>\r\n'
        '  <Principals>\r\n'
        '    <Principal id="Author">\r\n'
        f'      <UserId>{u_esc}</UserId>\r\n'
        '      <LogonType>InteractiveToken</LogonType>\r\n'
        f'      <RunLevel>{run_level}</RunLevel>\r\n'
        '    </Principal>\r\n'
        '  </Principals>\r\n'
        '  <Settings>\r\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\r\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\r\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n'
        '    <IdleSettings>\r\n'
        '      <StopOnIdleEnd>false</StopOnIdleEnd>\r\n'
        '      <RestartOnIdle>false</RestartOnIdle>\r\n'
        '    </IdleSettings>\r\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n'
        '    <Enabled>true</Enabled>\r\n'
        '    <Hidden>false</Hidden>\r\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\r\n'
        '    <WakeToRun>false</WakeToRun>\r\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n'
        '    <Priority>5</Priority>\r\n'
        '  </Settings>\r\n'
        '  <Actions Context="Author">\r\n'
        '    <Exec>\r\n'
        f'      <Command>{cmd_esc}</Command>\r\n'
        f'      <Arguments>{arg_esc}</Arguments>\r\n'
        f'      <WorkingDirectory>{cwd_esc}</WorkingDirectory>\r\n'
        '    </Exec>\r\n'
        '  </Actions>\r\n'
        '</Task>\r\n'
    )


def _read_registry_run(value_name: str = RUN_VALUE_NAME) -> tuple[str, bool]:
    """Return `(command_line_or_empty, disabled_in_task_manager)`."""
    cmd = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_READ) as k:
            val, _typ = winreg.QueryValueEx(k, value_name)
            if isinstance(val, str):
                cmd = val.strip()
    except OSError:
        return ("", False)

    approved_disabled = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_PATH, 0, winreg.KEY_READ) as k:
            raw, _typ = winreg.QueryValueEx(k, value_name)
            if isinstance(raw, (bytes, bytearray)) and len(raw) >= 1:
                # Windows StartupApproved: 0x02/0x06 = enabled, odd (0x03/0x07) = disabled by user
                approved_disabled = (raw[0] & 1) == 1
    except OSError:
        pass
    return (cmd, approved_disabled)


def _write_registry_run(command_line: str, value_name: str = RUN_VALUE_NAME) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, command_line)
    # Re-enable in StartupApproved if Windows Task Manager previously disabled this value
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_PATH, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            raw, typ = winreg.QueryValueEx(k, value_name)
            if isinstance(raw, (bytes, bytearray)) and len(raw) >= 1 and (raw[0] & 1) == 1:
                winreg.SetValueEx(k, value_name, 0, typ, bytes([2] + [0] * 11))
    except OSError:
        pass


def _delete_registry_run(value_name: str = RUN_VALUE_NAME) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value_name)
    except OSError:
        pass


def query_elevated_task_exists(task_name: str = TASK_NAME) -> bool:
    """Return True if our Task Scheduler logon task exists."""
    try:
        r = subprocess.run(
            [SCHTASKS_EXE, "/Query", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True
        combined = f"{r.stdout}\n{r.stderr}".lower()
        # A HighestAvailable task created by Admin can report "Access is denied" when queried as Standard User
        if "access is denied" in combined:
            return True
        return False
    except Exception:
        return False


def _run_schtasks(args: list[str], require_elevation: bool, hwnd: int = 0) -> tuple[bool, str, str]:
    """Run System32 schtasks.exe silently when already Admin, or via official UAC prompt when needed."""
    if w32.is_process_elevated() or not require_elevation:
        try:
            r = subprocess.run(
                [SCHTASKS_EXE, *args],
                capture_output=True,
                text=True,
                timeout=12.0,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return (True, "ok", (r.stdout or "").strip())
            return (False, "error", (r.stderr or r.stdout or f"exit {r.returncode}").strip())
        except Exception as exc:
            return (False, "error", str(exc))

    param_str = " ".join(_quote_arg(a) for a in args)
    ok, code, status = w32.shell_execute_elevated(
        SCHTASKS_EXE,
        parameters=param_str,
        show_cmd=w32.SW_HIDE,
        wait_timeout_ms=20000,
        hwnd=hwnd,
    )
    if ok:
        return (True, "ok", "")
    if status == "cancelled":
        return (False, "cancelled", "UAC cancelled by user")
    return (False, status, f"schtasks exit/error {code}")


def query_startup_status(repo_root: pathlib.Path, dry_run: bool = False) -> StartupStatus:
    """Inspect real Windows Registry + Task Scheduler startup status."""
    exe, _args, _native = resolve_launcher(repo_root)
    is_admin = w32.is_process_elevated()
    elev_allowed, elev_reason = can_enable_elevated_task(repo_root)
    if dry_run:
        return StartupStatus(
            enabled=False,
            mode="none",
            is_admin=is_admin,
            launcher_path=str(exe),
            command_line="",
            elevated_task_allowed=elev_allowed,
            elevated_task_block_reason=elev_reason,
        )

    if query_elevated_task_exists():
        _cmd, _arg, _cwd, full_cmd = build_autostart_command(repo_root, elevated_task=True)
        return StartupStatus(
            enabled=True,
            mode="elevated_task",
            is_admin=is_admin,
            launcher_path=str(exe),
            command_line=full_cmd,
            elevated_task_allowed=elev_allowed,
            elevated_task_block_reason=elev_reason,
        )

    reg_cmd, approved_disabled = _read_registry_run()
    if reg_cmd:
        return StartupStatus(
            enabled=not approved_disabled,
            mode="registry",
            is_admin=is_admin,
            launcher_path=str(exe),
            command_line=reg_cmd,
            approved_disabled=approved_disabled,
            elevated_task_allowed=elev_allowed,
            elevated_task_block_reason=elev_reason,
        )

    return StartupStatus(
        enabled=False,
        mode="none",
        is_admin=is_admin,
        launcher_path=str(exe),
        command_line="",
        elevated_task_allowed=elev_allowed,
        elevated_task_block_reason=elev_reason,
    )


def install_elevated_task_direct(repo_root: pathlib.Path) -> tuple[bool, str, str]:
    """Install the HighestAvailable logon task from an ALREADY-elevated, verified Frozen EXE.

    Writes the XML exclusively inside `%SystemRoot%\\Temp` with `O_CREAT|O_EXCL` and a 128-bit
    random filename after elevation (zero user `%TEMP%` UAC TOCTOU window).
    """
    if not w32.is_process_elevated():
        return (False, "not_elevated", "Administrator privileges are required to register the elevated task.")

    allowed, reason = can_enable_elevated_task(repo_root)
    if not allowed:
        return (False, "blocked_insecure_path", reason)

    cmd, arguments, working_dir, _full_cmd = build_autostart_command(repo_root, elevated_task=True)
    xml_text = build_task_scheduler_xml(cmd, arguments, working_dir, current_user_id(), elevated=True)

    sys_temp = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"
    if not sys_temp.is_dir():
        return (False, "error", f"System temp directory not available: {sys_temp}")

    tmp_path: pathlib.Path | None = None
    fd = -1
    try:
        for _ in range(8):
            candidate = sys_temp / f"prismfx_task_{secrets.token_hex(16)}.xml"
            try:
                fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                tmp_path = candidate
                break
            except FileExistsError:
                continue
        if fd < 0 or tmp_path is None:
            return (False, "error", "Could not allocate exclusive temporary task XML file.")

        with os.fdopen(fd, "wb") as tf:
            fd = -1
            tf.write(xml_text.encode("utf-16"))
            tf.flush()
            os.fsync(tf.fileno())

        r = subprocess.run(
            [SCHTASKS_EXE, "/Create", "/TN", TASK_NAME, "/XML", str(tmp_path), "/F"],
            capture_output=True,
            text=True,
            timeout=12.0,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return (True, "ok", "elevated_task")
        return (False, "error", (r.stderr or r.stdout or f"exit {r.returncode}").strip())
    except Exception as exc:
        return (False, "error", str(exc))
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def apply_startup_settings(
    repo_root: pathlib.Path,
    enable: bool,
    elevated_task: bool = False,
    hwnd: int = 0,
    dry_run: bool = False,
) -> tuple[bool, str, str]:
    """Enable, switch mode, or disable Windows auto-start cleanly with zero duplicate entries."""
    if dry_run:
        return (True, "ok", "dry-run")

    if not enable:
        _delete_registry_run()
        if query_elevated_task_exists():
            ok, status, msg = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"], require_elevation=True, hwnd=hwnd)
            if not ok:
                return (False, status, msg)
        return (True, "ok", "disabled")

    if elevated_task:
        allowed, reason = can_enable_elevated_task(repo_root)
        if not allowed:
            return (False, "blocked_insecure_path", reason)

        if w32.is_process_elevated():
            ok, status, msg = install_elevated_task_direct(repo_root)
            if not ok:
                return (False, status, msg)
            _delete_registry_run()
            return (True, "ok", "elevated_task")

        # Self-elevate the verified Frozen EXE with --install-elevated-task so NO XML is ever
        # written to user-writable %TEMP% prior to UAC confirmation (eliminates TOCTOU race).
        exe, _base_args, _is_native = resolve_launcher(repo_root, elevated_task=True)
        ok, code, status = w32.shell_execute_elevated(
            str(exe),
            parameters="--install-elevated-task",
            cwd=str(exe.parent),
            show_cmd=w32.SW_HIDE,
            wait_timeout_ms=20000,
            hwnd=hwnd,
        )
        if not ok:
            if status == "cancelled":
                return (False, "cancelled", "UAC cancelled by user")
            return (False, status, f"elevated task install error ({code})")
        # Prevent duplicate launch at logon
        _delete_registry_run()
        return (True, "ok", "elevated_task")
    else:
        _cmd, _arguments, _working_dir, full_cmd = build_autostart_command(repo_root, elevated_task=False)
        # Standard HKCU\...\Run auto-start (instant, no UAC!)
        if query_elevated_task_exists():
            ok, status, msg = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"], require_elevation=True, hwnd=hwnd)
            if not ok:
                return (False, status, msg)
        try:
            _write_registry_run(full_cmd)
            return (True, "ok", "registry")
        except OSError as exc:
            return (False, "error", str(exc))


def relaunch_as_admin(
    repo_root: pathlib.Path,
    extra_args: list[str] | None = None,
    hwnd: int = 0,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Request official Windows UAC elevation and seamlessly replace the current standard instance."""
    if dry_run:
        return (True, "ok")
    exe, base_args, _is_native = resolve_launcher(repo_root, elevated_task=True)
    args = [*base_args, "--replace", *(extra_args or [])]
    param_str = " ".join(_quote_arg(a) for a in args)
    ok, _code, status = w32.shell_execute_elevated(
        str(exe),
        parameters=param_str,
        cwd=str(exe.parent),
        show_cmd=w32.SW_SHOWNORMAL,
        wait_timeout_ms=0,
        hwnd=hwnd,
    )
    return (ok, status)
