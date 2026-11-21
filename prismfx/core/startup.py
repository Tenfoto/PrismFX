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

import os
import pathlib
import subprocess
import sys
import tempfile
import winreg
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

from . import win32_api as w32

RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE_NAME = "PrismFX Studio"
TASK_NAME = "PrismFX Studio Autostart"
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class StartupStatus:
    enabled: bool
    mode: str                    # "none" | "registry" | "elevated_task"
    is_admin: bool
    launcher_path: str
    command_line: str
    approved_disabled: bool = False


def current_user_id() -> str:
    """Return `DOMAIN\\Username` for Windows Task Scheduler Principal/Trigger."""
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    if domain and user:
        return f"{domain}\\{user}"
    return user or "BUILTIN\\Users"


def resolve_launcher(repo_root: pathlib.Path) -> tuple[pathlib.Path, list[str], bool]:
    """Resolve the best windowless launcher: `(executable, leading_args, is_native_exe)`."""
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


def _quote_arg(arg: str) -> str:
    if not arg or any(c in arg for c in (' ', '\t', '"')):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def build_autostart_command(repo_root: pathlib.Path) -> tuple[str, str, str, str]:
    """Return `(command, arguments, working_dir, full_registry_cmd)`."""
    exe, base_args, _is_native = resolve_launcher(repo_root)
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
            ["schtasks.exe", "/Query", "/TN", task_name],
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
    """Run schtasks.exe silently when already Admin, or via official UAC prompt when needed."""
    if w32.is_user_admin() or not require_elevation:
        try:
            r = subprocess.run(
                ["schtasks.exe", *args],
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
        "schtasks.exe",
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
    is_admin = w32.is_user_admin()
    if dry_run:
        return StartupStatus(
            enabled=False,
            mode="none",
            is_admin=is_admin,
            launcher_path=str(exe),
            command_line="",
        )

    if query_elevated_task_exists():
        _cmd, _arg, _cwd, full_cmd = build_autostart_command(repo_root)
        return StartupStatus(
            enabled=True,
            mode="elevated_task",
            is_admin=is_admin,
            launcher_path=str(exe),
            command_line=full_cmd,
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
        )

    return StartupStatus(
        enabled=False,
        mode="none",
        is_admin=is_admin,
        launcher_path=str(exe),
        command_line="",
    )


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

    cmd, arguments, working_dir, full_cmd = build_autostart_command(repo_root)

    if elevated_task:
        xml_text = build_task_scheduler_xml(cmd, arguments, working_dir, current_user_id(), elevated=True)
        tmp_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".prismfx_task.xml", delete=False
            ) as tf:
                tf.write(xml_text.encode("utf-16"))
                tmp_path = pathlib.Path(tf.name)
            ok, status, msg = _run_schtasks(
                ["/Create", "/TN", TASK_NAME, "/XML", str(tmp_path), "/F"],
                require_elevation=True,
                hwnd=hwnd,
            )
            if not ok:
                return (False, status, msg)
            # Prevent duplicate launch at logon
            _delete_registry_run()
            return (True, "ok", "elevated_task")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
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
    exe, base_args, _is_native = resolve_launcher(repo_root)
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
