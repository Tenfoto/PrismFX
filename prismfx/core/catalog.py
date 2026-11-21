"""Human-Centric Domain Layer: classifies raw processes/window classes into clear app profiles.

Phase 1 Rule #3 (windows-desktop-engineering): never leave users with raw PIDs or class names;
provide plain-language categories, safety recommendations, and smart auto-exclusions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppClassification:
    exe_name: str
    friendly_name: str
    category: str          # "System Shell", "Game / Anti-Cheat", "IDE & Dev", "Browser", "Creative", "Utility", "Application"
    category_th: str
    fx_safety: str         # "Full FX Safe", "Light FX Recommended", "Auto-Excluded (Protected)"
    fx_safety_th: str
    recommended_exclude: bool
    reason_en: str
    reason_th: str


_PROTECTED_CLASSES: set[str] = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "Windows.UI.Core.CoreWindow",
    "MultitaskingViewFrame",
    "ForegroundStaging",
    "ApplicationManager_DesktopShellWindow",
    "TaskListThumbnailWnd",
    "TaskListOverlayWnd",
    "EdgeUiInputTopWndClass",
    "EdgeUiInputWndClass",
    "DV2ControlHost",
    "TaskSwitcherWnd",
    "XamlExplorerHostIslandWindow",
    "TopLevelWindowForOverflowXamlIsland",
    "Xaml_WindowedPopupClass",
    "DesktopWindowXamlSource",
    "Windows.UI.Composition.DesktopWindowContentBridge",
    "TrayNotifyWnd",
    "ReBarWindow32",
    "MSTaskSwWClass",
    "MSTaskListWClass",
    "Shell_InputSwitchTopLevelWindow",
    "Shell_Dim",
    "PopupHost",
    "ApplicationFrameInputSinkWindow",
    "CEF-OSC-WIDGET",
    "SysShadow",
    "tooltips_class32",
    "IME",
    "MSCTFIME UI",
    "ShadowPlay",
    "NvOverlay",
}

_KNOWN_APPS: dict[str, tuple[str, str, str, bool, str, str]] = {
    # exe.lower(): (Friendly, CatEN, CatTH, auto_exclude, reason_en, reason_th)
    "dwm.exe": ("Desktop Window Manager", "System Shell", "ระบบวินโดวส์", True,
                "Core compositor process — always protected.", "คอมโพซิเตอร์หลักของ Windows — ป้องกันอัตโนมัติ"),
    "csrss.exe": ("Windows Subsystem", "System Shell", "ระบบวินโดวส์", True,
                  "Critical OS runtime — protected.", "ระบบหลักของ OS — ป้องกันอัตโนมัติ"),
    "lockapp.exe": ("Windows Lock Screen", "System Shell", "ระบบวินโดวส์", True,
                    "Lock screen surface — protected.", "หน้าจอล็อกของ Windows — ป้องกันอัตโนมัติ"),
    "searchhost.exe": ("Windows Search", "System Shell", "ระบบวินโดวส์", False,
                       "Shell flyout — works best with fast transitions.", "หน้าต่างค้นหา — เหมาะกับเอฟเฟกต์เร็ว"),
    "shellexperiencehost.exe": ("Shell Experience", "System Shell", "ระบบวินโดวส์", False,
                                "Taskbar flyout surface.", "พื้นผิวแถบงานของ Windows"),
    "explorer.exe": ("File Explorer", "System Shell", "ตัวจัดการไฟล์", False,
                     "Standard Explorer folders support full GPU shader FX.", "หน้าต่างโฟลเดอร์รองรับเชดเดอร์ GPU เต็มรูปแบบ"),
    "code.exe": ("Visual Studio Code", "IDE & Dev", "พัฒนาซอฟต์แวร์", False,
                 "Electron/GPU editor — smooth with PrismFX out-of-process overlay.", "IDE — ทำงานลื่นไหลโดยไม่ฉีด DLL"),
    "devenv.exe": ("Visual Studio", "IDE & Dev", "พัฒนาซอฟต์แวร์", False,
                   "WPF IDE — full shader transitions supported.", "IDE — รองรับแอนิเมชันเต็มรูปแบบ"),
    "windowsterminal.exe": ("Windows Terminal", "IDE & Dev", "พัฒนาซอฟต์แวร์", False,
                            "Modern DirectX terminal — full GPU FX safe.", "เทอร์มินัลยุคใหม่ — รองรับเอฟเฟกต์เต็มที่"),
    "chrome.exe": ("Google Chrome", "Browser", "เว็บเบราว์เซอร์", False,
                   "Chromium browser — zero-injection overlay keeps sandboxing intact.", "เบราว์เซอร์ — ปลอดภัย ไม่กระทบ Sandbox"),
    "msedge.exe": ("Microsoft Edge", "Browser", "เว็บเบราว์เซอร์", False,
                   "Chromium browser — full GPU transitions safe.", "เบราว์เซอร์ — รองรับแอนิเมชันเต็มรูปแบบ"),
    "firefox.exe": ("Mozilla Firefox", "Browser", "เว็บเบราว์เซอร์", False,
                    "WebRender browser — full GPU transitions safe.", "เบราว์เซอร์ — รองรับแอนิเมชันเต็มรูปแบบ"),
    "brave.exe": ("Brave Browser", "Browser", "เว็บเบราว์เซอร์", False,
                  "Chromium browser — full GPU transitions safe.", "เบราว์เซอร์ — รองรับแอนิเมชันเต็มรูปแบบ"),
    "discord.exe": ("Discord", "Communication", "แชทและสื่อสาร", False,
                    "Electron app — smooth GPU animations & window tiles.", "แอปแชท — รองรับแอนิเมชันและไทล์หน้าต่าง"),
    "spotify.exe": ("Spotify", "Media", "มีเดียและเพลง", False,
                    "Media player — great candidate for Mini-Tiles & Opacity.", "เครื่องเล่นเพลง — เหมาะกับไทล์และปรับโปร่งใส"),
    "obs64.exe": ("OBS Studio", "Creative", "ครีเอทีฟ/สตรีม", True,
                  "Capture/streaming tool — auto-excluded to prevent capture glitches.", "โปรแกรมสตรีม/บันทึกหน้าจอ — ยกเว้นอัตโนมัติเพื่อกันภาพกระตุก"),
    "photoshop.exe": ("Adobe Photoshop", "Creative", "ครีเอทีฟ", False,
                      "Creative suite — full transitions safe.", "โปรแกรมออกแบบ — รองรับแอนิเมชันปกติ"),
    "resolve.exe": ("DaVinci Resolve", "Creative", "ตัดต่อวิดีโอ", True,
                    "Heavy GPU NLE — excluded by default to preserve VRAM bandwidth.", "โปรแกรมตัดต่อ — ยกเว้นเริ่มต้นเพื่อประหยัด VRAM"),
    "valorant-win64-shipping.exe": ("VALORANT", "Game / Anti-Cheat", "เกม / แอนตี้ชีต", True,
                                    "Kernel anti-cheat game — auto-excluded for zero latency & safety.", "เกมมี Anti-Cheat — ยกเว้นอัตโนมัติเพื่อความปลอดภัย"),
    "cs2.exe": ("Counter-Strike 2", "Game / Anti-Cheat", "เกม / แอนตี้ชีต", True,
                "Competitive fullscreen game — auto-excluded.", "เกมแข่งขัน — ยกเว้นอัตโนมัติ"),
    "dota2.exe": ("Dota 2", "Game / Anti-Cheat", "เกม / แอนตี้ชีต", True,
                  "Fullscreen DirectX/Vulkan game — auto-excluded.", "เกมเต็มจอ — ยกเว้นอัตโนมัติ"),
    "genshinimpact.exe": ("Genshin Impact", "Game / Anti-Cheat", "เกม / แอนตี้ชีต", True,
                          "Fullscreen game — auto-excluded.", "เกมเต็มจอ — ยกเว้นอัตโนมัติ"),
    "cyberpunk2077.exe": ("Cyberpunk 2077", "Game / Anti-Cheat", "เกม / แอนตี้ชีต", True,
                          "GPU-intensive fullscreen game — auto-excluded.", "เกมกราฟิกสูง — ยกเว้นอัตโนมัติ"),
    "steamwebhelper.exe": ("Steam Client", "Game Launcher", "แพลตฟอร์มเกม", False,
                           "Launcher UI — safe for animations.", "หน้าต่าง Steam — รองรับแอนิเมชัน"),
    "notepad.exe": ("Notepad", "Utility", "ยูทิลิตี้", False,
                    "Lightweight Win11 editor — instant GPU FX.", "แอปจดบันทึก — เหมาะกับการทดสอบทุกเอฟเฟกต์"),
    "calculatorapp.exe": ("Calculator", "Utility", "ยูทิลิตี้", False,
                          "Compact UWP utility — great for Pin & Transparency.", "เครื่องคิดเลข — เหมาะกับการปักหมุดและโปร่งใส"),
    "windowfxconfig.exe": ("Stardock WindowFX", "Legacy FX", "โปรแกรม FX เดิม", True,
                           "Legacy WindowFX detected — excluded to avoid overlapping transitions.", "ตรวจพบ WindowFX เดิม — ยกเว้นเพื่อกันแอนิเมชันซ้อนกัน"),
    "nvidia overlay.exe": ("NVIDIA Overlay", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                           "NVIDIA Overlay (Alt+Z) — protected to prevent overlay flicker.", "โอเวอร์เลย์ NVIDIA (Alt+Z) — ป้องกันอัตโนมัติเพื่อไม่ให้จอค้าง/กระพริบ"),
    "nvidia share.exe": ("NVIDIA Share", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                         "NVIDIA Share HUD — protected automatically.", "NVIDIA Share HUD — ป้องกันอัตโนมัติ"),
    "nvsphelper64.exe": ("NVIDIA Share Helper", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                         "NVIDIA capture helper — protected automatically.", "ตัวช่วยบันทึกภาพ NVIDIA — ป้องกันอัตโนมัติ"),
    "nvcontainer.exe": ("NVIDIA Container", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                        "NVIDIA driver container — protected.", "บริการไดรเวอร์ NVIDIA — ป้องกันอัตโนมัติ"),
    "presentmon_x64.exe": ("PresentMon Telemetry", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                           "Frame telemetry overlay — protected.", "โอเวอร์เลย์วัดเฟรมเรต — ป้องกันอัตโนมัติ"),
    "gamebar.exe": ("Xbox Game Bar", "GPU Overlay", "โอเวอร์เลย์เกม", True,
                    "Windows Game Bar overlay — protected.", "Xbox Game Bar — ป้องกันอัตโนมัติ"),
    "rtss.exe": ("RivaTuner Overlay", "GPU Overlay", "โอเวอร์เลย์การ์ดจอ", True,
                 "RTSS OSD overlay — protected.", "RivaTuner OSD — ป้องกันอัตโนมัติ"),
}

_GAME_PATTERNS = (
    "-win64-shipping.exe", "unityplayer", "anticheat", "easyanticheat", "battleye",
    "riotclient", "nvidia overlay", "nvidia share", "nvcontainer", "nvsphelper", "gamebar", "rtss", "overwolf"
)


def is_protected_window_class(class_name: str) -> bool:
    return class_name in _PROTECTED_CLASSES


def classify_app(exe_name: str, window_class: str = "") -> AppClassification:
    """Translate raw executable/class name into human-centric classification & verdict."""
    clean_exe = (exe_name or "unknown.exe").strip()
    low = clean_exe.lower()

    if window_class in _PROTECTED_CLASSES:
        return AppClassification(
            exe_name=clean_exe,
            friendly_name=f"Windows Shell ({window_class})",
            category="System Shell",
            category_th="ระบบวินโดวส์",
            fx_safety="Auto-Excluded (Protected)",
            fx_safety_th="ยกเว้นอัตโนมัติ (ระบบ)",
            recommended_exclude=True,
            reason_en="Protected OS desktop shell surface.",
            reason_th="พื้นผิวหลักของระบบ Windows ที่ต้องป้องกัน",
        )

    if low in _KNOWN_APPS:
        friendly, cat_en, cat_th, auto_ex, r_en, r_th = _KNOWN_APPS[low]
        return AppClassification(
            exe_name=clean_exe,
            friendly_name=friendly,
            category=cat_en,
            category_th=cat_th,
            fx_safety="Auto-Excluded (Protected)" if auto_ex else "Full FX Safe",
            fx_safety_th="ยกเว้นอัตโนมัติ" if auto_ex else "รองรับเอฟเฟกต์เต็มที่",
            recommended_exclude=auto_ex,
            reason_en=r_en,
            reason_th=r_th,
        )

    if any(pat in low for pat in _GAME_PATTERNS):
        pretty = clean_exe.rsplit(".", 1)[0].replace("-Win64-Shipping", "")
        return AppClassification(
            exe_name=clean_exe,
            friendly_name=pretty,
            category="Game / Anti-Cheat",
            category_th="เกม / แอนตี้ชีต",
            fx_safety="Auto-Excluded (Protected)",
            fx_safety_th="ยกเว้นอัตโนมัติ (เกม)",
            recommended_exclude=True,
            reason_en="Detected Unreal/Anti-Cheat game binary — auto-excluded for safety.",
            reason_th="ตรวจพบเกมหรือระบบ Anti-Cheat — ยกเว้นอัตโนมัติเพื่อความปลอดภัย",
        )

    stem = clean_exe.rsplit(".", 1)[0]
    friendly = stem[:1].upper() + stem[1:] if stem else "Application"
    return AppClassification(
        exe_name=clean_exe,
        friendly_name=friendly,
        category="Application",
        category_th="แอปพลิเคชันทั่วไป",
        fx_safety="Full FX Safe",
        fx_safety_th="รองรับเอฟเฟกต์เต็มที่",
        recommended_exclude=False,
        reason_en="Standard desktop application — safe for out-of-process GPU shaders.",
        reason_th="แอปพลิเคชันเดสก์ท็อปทั่วไป — ปลอดภัยกับเชดเดอร์นอกโปรเซส",
    )
