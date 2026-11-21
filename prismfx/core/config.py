"""Atomic configuration & preset management (`.part` -> fsync -> atomic replace)."""

from __future__ import annotations

import copy
import json
import math
import os
import pathlib
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .effects import EASING_CURVES, sanitize_hex_color

_VALID_EASINGS = frozenset(item[0] for item in EASING_CURVES)
_VALID_SNAP_ZONES = frozenset({
    "left_50", "right_50", "left_66", "right_33", "left_33", "right_66",
    "left_75", "right_25", "left_25", "right_75", "center_50", "center_66",
    "center_33", "top_50", "bottom_50", "tl_corner", "tr_corner",
    "bl_corner", "br_corner", "maximize", "minimize", "none",
})
_VALID_MIDDLE_CLICK = frozenset({"none", "rollup", "minimize", "pin", "tile"})
_VALID_SPOTLIGHT_MODES = frozenset({"flat", "halo", "cursor"})
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _is_windows_reserved_name(name: str) -> bool:
    stem = name.strip().rstrip(". ").split(".", 1)[0].strip().upper()
    return stem in _WINDOWS_RESERVED_NAMES


def _sanitize_preset_name(name: object) -> str:
    raw = str(name or "").strip()
    raw_reserved = _is_windows_reserved_name(raw)
    safe = "".join(c for c in raw if c.isalnum() or c in (" ", "-", "_")).strip()
    safe = safe.rstrip(". ")[:80].rstrip(". ")
    if not safe:
        return "Custom Preset"
    if raw_reserved or _is_windows_reserved_name(safe):
        safe = f"preset_{safe}"[:80].rstrip(". ")
    if not safe or _is_windows_reserved_name(safe):
        return "Custom Preset"
    return safe


def _parse_bool(val: Any, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    return default


def _parse_float(val: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(val, bool) or val is None:
        return default
    if not isinstance(val, (int, float, str)):
        return default
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(lo, min(hi, num))


def _parse_int(val: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(val, bool) or val is None:
        return default
    if isinstance(val, int):
        return max(lo, min(hi, val))
    if isinstance(val, (float, str)):
        try:
            num = float(val)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(num):
            return default
        return max(lo, min(hi, int(round(num))))
    return default


def _parse_str(
    val: Any,
    default: str,
    allowed: Optional[frozenset[str] | tuple[str, ...]] = None,
    max_len: int = 128,
) -> str:
    if not isinstance(val, str):
        return default
    s = val.strip()
    if not s or "\x00" in s:
        return default
    if len(s) > max_len:
        s = s[:max_len]
    if allowed is not None and s not in allowed:
        return default
    return s


def _parse_str_list(
    val: Any,
    default: list[str],
    allow_empty: bool = True,
    max_items: int = 512,
) -> list[str]:
    if not isinstance(val, list):
        return list(default)
    cleaned: list[str] = []
    for item in val:
        if isinstance(item, str):
            s = item.strip()
            if s and "\x00" not in s:
                cleaned.append(s)
                if len(cleaned) >= max_items:
                    break
    if not cleaned and not allow_empty:
        return list(default)
    return cleaned


@dataclass
class TriggerConfig:
    enabled: bool = True
    effect_id: str = "prism_glide"
    duration_ms: int = 320
    easing: str = "spring_smooth"
    intensity: float = 1.0
    accent_hex: str = "#2D81FF"


@dataclass
class PhysicsConfig:
    wobbly_enabled: bool = True
    wobbly_stiffness: float = 22.0
    wobbly_damping: float = 0.56
    wobbly_amplitude: float = 1.0
    momentum_enabled: bool = True
    momentum_friction: float = 6.2
    momentum_bounce: bool = True
    momentum_strict_clamp: bool = False
    drag_transparency_enabled: bool = True
    drag_opacity_pct: int = 86
    flicks_enabled: bool = True
    flick_threshold: float = 1450.0
    edge_snap_enabled: bool = True
    smooth_maximize_enabled: bool = True
    smooth_maximize_ms: int = 220
    snap_left_zone: str = "left_50"
    snap_right_zone: str = "right_50"
    snap_top_zone: str = "maximize"
    snap_bottom_zone: str = "minimize"
    middle_click_titlebar: str = "rollup"   # none | rollup | minimize | pin | tile
    double_click_desktop_icons: bool = False


@dataclass
class FocusConfig:
    spotlight_enabled: bool = False
    spotlight_mode: str = "halo"            # flat | halo | cursor
    spotlight_opacity_pct: int = 42
    spotlight_color_hex: str = "#06080E"
    spotlight_exclude_snapped: bool = True
    inactive_fade_enabled: bool = False
    inactive_opacity_pct: int = 84
    auto_minimize_enabled: bool = False
    auto_minimize_minutes: int = 8
    desktop_icons_autohide: bool = False
    tiles_topmost: bool = True
    tile_size_px: int = 196


@dataclass
class SafetyConfig:
    pause_on_fullscreen_game: bool = True
    pause_on_battery: bool = True
    auto_exclude_games: bool = True
    suppress_native_min_anim: bool = True
    excluded_exes: list[str] = field(
        default_factory=lambda: [
            "dwm.exe",
            "obs64.exe",
            "WindowFXConfig.exe",
            "NVIDIA Overlay.exe",
            "NVIDIA Share.exe",
            "nvsphelper64.exe",
            "nvcontainer.exe",
            "PresentMon_x64.exe",
            "GameBar.exe",
            "RTSS.exe",
            "VALORANT-Win64-Shipping.exe",
            "cs2.exe",
        ]
    )


@dataclass
class PrismConfig:
    enabled: bool = True
    language: str = "th"  # "th" | "en"
    active_preset: str = "Prism Studio Default"
    global_speed_scale: float = 1.0
    target_fps: int = 165
    start_with_windows: bool = False
    start_with_windows_admin: bool = False
    autostart_minimized: bool = True
    start_minimized_to_tray: bool = False
    window_geometry: list[int] = field(default_factory=lambda: [140, 80, 1280, 800])
    triggers: dict[str, TriggerConfig] = field(
        default_factory=lambda: {
            "open": TriggerConfig(True, "prism_glide", 300, "spring_smooth", 1.0, "#2D81FF"),
            "close": TriggerConfig(True, "ember_burn", 460, "expo_out", 1.0, "#F97316"),
            "minimize": TriggerConfig(True, "genie_lamp", 380, "spring_smooth", 1.0, "#38BDF8"),
            "restore": TriggerConfig(True, "win11_silk", 300, "spring_smooth", 1.0, "#74AAFF"),
            "menu": TriggerConfig(True, "swipe_reveal", 180, "expo_out", 0.85, "#2D81FF"),
            "popup": TriggerConfig(True, "elastic_pop", 220, "spring_elastic", 0.9, "#74AAFF"),
        }
    )
    random_pool: list[str] = field(
        default_factory=lambda: [
            "prism_glide",
            "genie_lamp",
            "liquid_ripple",
            "ember_burn",
            "plasma_dissolve",
            "frost_shatter",
            "black_hole",
            "cyber_glitch",
            "crt_tv_off",
            "hex_shield",
            "doors_3d",
            "shatter_glass",
            "origami_fold",
            "page_peel",
        ]
    )
    physics: PhysicsConfig = field(default_factory=PhysicsConfig)
    focus: FocusConfig = field(default_factory=FocusConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PrismConfig":
        cfg = cls()
        if not isinstance(raw, dict):
            return cfg
        cfg.enabled = _parse_bool(raw.get("enabled"), cfg.enabled)
        cfg.language = _parse_str(raw.get("language"), cfg.language, allowed=("th", "en"))
        cfg.active_preset = _parse_str(raw.get("active_preset"), cfg.active_preset, max_len=128)
        cfg.global_speed_scale = _parse_float(raw.get("global_speed_scale"), cfg.global_speed_scale, 0.35, 2.5)
        cfg.target_fps = _parse_int(raw.get("target_fps"), cfg.target_fps, 30, 360)
        cfg.start_with_windows = _parse_bool(raw.get("start_with_windows"), cfg.start_with_windows)
        cfg.start_with_windows_admin = _parse_bool(raw.get("start_with_windows_admin"), cfg.start_with_windows_admin)
        cfg.autostart_minimized = _parse_bool(raw.get("autostart_minimized"), cfg.autostart_minimized)
        cfg.start_minimized_to_tray = _parse_bool(raw.get("start_minimized_to_tray"), cfg.start_minimized_to_tray)

        geom = raw.get("window_geometry")
        if isinstance(geom, list) and len(geom) == 4:
            def_geom = cfg.window_geometry
            cfg.window_geometry = [
                _parse_int(geom[0], def_geom[0], -32768, 32768),
                _parse_int(geom[1], def_geom[1], -32768, 32768),
                _parse_int(geom[2], def_geom[2], 320, 16384),
                _parse_int(geom[3], def_geom[3], 240, 16384),
            ]

        raw_triggers = raw.get("triggers")
        if isinstance(raw_triggers, dict):
            for k, default_tc in cfg.triggers.items():
                td = raw_triggers.get(k)
                if isinstance(td, dict):
                    cfg.triggers[k] = TriggerConfig(
                        enabled=_parse_bool(td.get("enabled"), default_tc.enabled),
                        effect_id=_parse_str(td.get("effect_id"), default_tc.effect_id, max_len=128),
                        duration_ms=_parse_int(td.get("duration_ms"), default_tc.duration_ms, 80, 1400),
                        easing=_parse_str(td.get("easing"), default_tc.easing, allowed=_VALID_EASINGS),
                        intensity=_parse_float(td.get("intensity"), default_tc.intensity, 0.2, 2.0),
                        accent_hex=sanitize_hex_color(td.get("accent_hex"), default_tc.accent_hex),
                    )

        cfg.random_pool = _parse_str_list(raw.get("random_pool"), cfg.random_pool, allow_empty=False)

        raw_physics = raw.get("physics")
        if isinstance(raw_physics, dict):
            dp = cfg.physics
            cfg.physics = PhysicsConfig(
                wobbly_enabled=_parse_bool(raw_physics.get("wobbly_enabled"), dp.wobbly_enabled),
                wobbly_stiffness=_parse_float(raw_physics.get("wobbly_stiffness"), dp.wobbly_stiffness, 5.0, 80.0),
                wobbly_damping=_parse_float(raw_physics.get("wobbly_damping"), dp.wobbly_damping, 0.1, 2.0),
                wobbly_amplitude=_parse_float(raw_physics.get("wobbly_amplitude"), dp.wobbly_amplitude, 0.0, 3.0),
                momentum_enabled=_parse_bool(raw_physics.get("momentum_enabled"), dp.momentum_enabled),
                momentum_friction=_parse_float(raw_physics.get("momentum_friction"), dp.momentum_friction, 0.5, 30.0),
                momentum_bounce=_parse_bool(raw_physics.get("momentum_bounce"), dp.momentum_bounce),
                momentum_strict_clamp=_parse_bool(raw_physics.get("momentum_strict_clamp"), dp.momentum_strict_clamp),
                drag_transparency_enabled=_parse_bool(
                    raw_physics.get("drag_transparency_enabled"), dp.drag_transparency_enabled
                ),
                drag_opacity_pct=_parse_int(raw_physics.get("drag_opacity_pct"), dp.drag_opacity_pct, 20, 100),
                flicks_enabled=_parse_bool(raw_physics.get("flicks_enabled"), dp.flicks_enabled),
                flick_threshold=_parse_float(raw_physics.get("flick_threshold"), dp.flick_threshold, 200.0, 10000.0),
                edge_snap_enabled=_parse_bool(raw_physics.get("edge_snap_enabled"), dp.edge_snap_enabled),
                smooth_maximize_enabled=_parse_bool(
                    raw_physics.get("smooth_maximize_enabled"), dp.smooth_maximize_enabled
                ),
                smooth_maximize_ms=_parse_int(raw_physics.get("smooth_maximize_ms"), dp.smooth_maximize_ms, 60, 1000),
                snap_left_zone=_parse_str(
                    raw_physics.get("snap_left_zone"), dp.snap_left_zone, allowed=_VALID_SNAP_ZONES
                ),
                snap_right_zone=_parse_str(
                    raw_physics.get("snap_right_zone"), dp.snap_right_zone, allowed=_VALID_SNAP_ZONES
                ),
                snap_top_zone=_parse_str(
                    raw_physics.get("snap_top_zone"), dp.snap_top_zone, allowed=_VALID_SNAP_ZONES
                ),
                snap_bottom_zone=_parse_str(
                    raw_physics.get("snap_bottom_zone"), dp.snap_bottom_zone, allowed=_VALID_SNAP_ZONES
                ),
                middle_click_titlebar=_parse_str(
                    raw_physics.get("middle_click_titlebar"), dp.middle_click_titlebar, allowed=_VALID_MIDDLE_CLICK
                ),
                double_click_desktop_icons=_parse_bool(
                    raw_physics.get("double_click_desktop_icons"), dp.double_click_desktop_icons
                ),
            )

        raw_focus = raw.get("focus")
        if isinstance(raw_focus, dict):
            df = cfg.focus
            cfg.focus = FocusConfig(
                spotlight_enabled=_parse_bool(raw_focus.get("spotlight_enabled"), df.spotlight_enabled),
                spotlight_mode=_parse_str(
                    raw_focus.get("spotlight_mode"), df.spotlight_mode, allowed=_VALID_SPOTLIGHT_MODES
                ),
                spotlight_opacity_pct=_parse_int(
                    raw_focus.get("spotlight_opacity_pct"), df.spotlight_opacity_pct, 5, 95
                ),
                spotlight_color_hex=sanitize_hex_color(
                    raw_focus.get("spotlight_color_hex"), df.spotlight_color_hex
                ),
                spotlight_exclude_snapped=_parse_bool(
                    raw_focus.get("spotlight_exclude_snapped"), df.spotlight_exclude_snapped
                ),
                inactive_fade_enabled=_parse_bool(
                    raw_focus.get("inactive_fade_enabled"), df.inactive_fade_enabled
                ),
                inactive_opacity_pct=_parse_int(
                    raw_focus.get("inactive_opacity_pct"), df.inactive_opacity_pct, 25, 100
                ),
                auto_minimize_enabled=_parse_bool(
                    raw_focus.get("auto_minimize_enabled"), df.auto_minimize_enabled
                ),
                auto_minimize_minutes=_parse_int(
                    raw_focus.get("auto_minimize_minutes"), df.auto_minimize_minutes, 1, 240
                ),
                desktop_icons_autohide=_parse_bool(
                    raw_focus.get("desktop_icons_autohide"), df.desktop_icons_autohide
                ),
                tiles_topmost=_parse_bool(raw_focus.get("tiles_topmost"), df.tiles_topmost),
                tile_size_px=_parse_int(raw_focus.get("tile_size_px"), df.tile_size_px, 96, 512),
            )

        raw_safety = raw.get("safety")
        if isinstance(raw_safety, dict):
            ds = cfg.safety
            cfg.safety = SafetyConfig(
                pause_on_fullscreen_game=_parse_bool(
                    raw_safety.get("pause_on_fullscreen_game"), ds.pause_on_fullscreen_game
                ),
                pause_on_battery=_parse_bool(raw_safety.get("pause_on_battery"), ds.pause_on_battery),
                auto_exclude_games=_parse_bool(raw_safety.get("auto_exclude_games"), ds.auto_exclude_games),
                suppress_native_min_anim=_parse_bool(
                    raw_safety.get("suppress_native_min_anim"), ds.suppress_native_min_anim
                ),
                excluded_exes=_parse_str_list(
                    raw_safety.get("excluded_exes"), ds.excluded_exes, allow_empty=True
                ),
            )

        return cfg


# Built-in curated presets (surpassing WindowFX's legacy .animset files)
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "Prism Studio Default": {
        "desc_en": "Balanced flagship experience: Prism Glide open, Ember Burn close, Genie minimize, Wobbly & Momentum ON.",
        "desc_th": "ชุดแนะนำมาตรฐาน: เปิดแบบปริซึม ปิดเผาไหม้ ย่อแบบตะเกียง พร้อมหน้าต่างเยลลี่และแรงเฉื่อย",
        "triggers": {
            "open": {"effect_id": "prism_glide", "duration_ms": 300, "easing": "spring_smooth", "accent_hex": "#2D81FF"},
            "close": {"effect_id": "ember_burn", "duration_ms": 460, "easing": "expo_out", "accent_hex": "#F97316"},
            "minimize": {"effect_id": "genie_lamp", "duration_ms": 380, "easing": "spring_smooth", "accent_hex": "#38BDF8"},
            "restore": {"effect_id": "win11_silk", "duration_ms": 300, "easing": "spring_smooth", "accent_hex": "#74AAFF"},
            "menu": {"effect_id": "swipe_reveal", "duration_ms": 180, "easing": "expo_out", "accent_hex": "#2D81FF"},
            "popup": {"effect_id": "elastic_pop", "duration_ms": 220, "easing": "spring_elastic", "accent_hex": "#74AAFF"},
        },
        "wobbly": True,
        "momentum": True,
    },
    "Windows 11 Silk 165Hz": {
        "desc_en": "Ultra-clean, crisp critically damped spatial motion tailored for high-refresh productivity.",
        "desc_th": "เรียบหรู สะอาดตา ระดับ 165 Hz สำหรับสายทำงานที่ต้องการความนุ่มนวลสูงสุด",
        "triggers": {
            "open": {"effect_id": "win11_silk", "duration_ms": 240, "easing": "spring_smooth", "accent_hex": "#74AAFF"},
            "close": {"effect_id": "win11_silk", "duration_ms": 200, "easing": "expo_out", "accent_hex": "#74AAFF"},
            "minimize": {"effect_id": "aero_float", "duration_ms": 260, "easing": "spring_smooth", "accent_hex": "#60A5FA"},
            "restore": {"effect_id": "aero_float", "duration_ms": 260, "easing": "spring_smooth", "accent_hex": "#60A5FA"},
            "menu": {"effect_id": "clean_fade", "duration_ms": 150, "easing": "expo_out", "accent_hex": "#94A3B8"},
            "popup": {"effect_id": "prism_glide", "duration_ms": 200, "easing": "spring_smooth", "accent_hex": "#2D81FF"},
        },
        "wobbly": False,
        "momentum": True,
    },
    "Cyberpunk Overdrive": {
        "desc_en": "Chromatic RGB glitch, CRT collapse, holographic desync, and neon laser sweeps.",
        "desc_th": "จัดเต็มสายไซเบอร์: กลิตช์แยกสี, ทีวี CRT ปิดเครื่อง, โฮโลแกรม และเลเซอร์สแกน",
        "triggers": {
            "open": {"effect_id": "holo_flicker", "duration_ms": 340, "easing": "expo_out", "accent_hex": "#22D3EE"},
            "close": {"effect_id": "crt_tv_off", "duration_ms": 340, "easing": "expo_out", "accent_hex": "#E2E8F0"},
            "minimize": {"effect_id": "cyber_glitch", "duration_ms": 300, "easing": "expo_out", "accent_hex": "#F43F5E"},
            "restore": {"effect_id": "hex_shield", "duration_ms": 380, "easing": "spring_smooth", "accent_hex": "#38BDF8"},
            "menu": {"effect_id": "neon_vapor", "duration_ms": 200, "easing": "linear", "accent_hex": "#EC4899"},
            "popup": {"effect_id": "pixelate_mosaic", "duration_ms": 240, "easing": "expo_out", "accent_hex": "#06B6D4"},
        },
        "wobbly": True,
        "momentum": True,
    },
    "Elemental Fire & Ice": {
        "desc_en": "GPU procedural Simplex fire, Voronoi cryo frost shatter, black hole vortex, and plasma vaporize.",
        "desc_th": "รวมพลังธาตุบน GPU: เผาไหม้ไฟ, แช่แข็งแตกร้าว, หลุมดำดูดวน และสลายพลาสมา",
        "triggers": {
            "open": {"effect_id": "ember_rise", "duration_ms": 440, "easing": "spring_smooth", "accent_hex": "#EF4444"},
            "close": {"effect_id": "ember_burn", "duration_ms": 540, "easing": "cubic_inout", "accent_hex": "#F97316"},
            "minimize": {"effect_id": "black_hole", "duration_ms": 460, "easing": "cubic_inout", "accent_hex": "#6366F1"},
            "restore": {"effect_id": "frost_shatter", "duration_ms": 460, "easing": "expo_out", "accent_hex": "#38BDF8"},
            "menu": {"effect_id": "plasma_dissolve", "duration_ms": 260, "easing": "expo_out", "accent_hex": "#8B5CF6"},
            "popup": {"effect_id": "shockwave_nova", "duration_ms": 320, "easing": "expo_out", "accent_hex": "#3B82F6"},
        },
        "wobbly": True,
        "momentum": True,
    },
    "Compiz & macOS Fluid": {
        "desc_en": "Classic Linux Compiz jelly physics meets Genie Lamp suction & 3D Origami folds.",
        "desc_th": "ฟิสิกส์หน้าต่างเยลลี่สไตล์ Compiz ผสานตะเกียง Genie และพับโอริกามิ 3 มิติ",
        "triggers": {
            "open": {"effect_id": "wobble_snap", "duration_ms": 360, "easing": "spring_elastic", "accent_hex": "#34D399"},
            "close": {"effect_id": "origami_fold", "duration_ms": 420, "easing": "cubic_inout", "accent_hex": "#F59E0B"},
            "minimize": {"effect_id": "genie_lamp", "duration_ms": 420, "easing": "cubic_inout", "accent_hex": "#38BDF8"},
            "restore": {"effect_id": "genie_lamp", "duration_ms": 420, "easing": "cubic_inout", "accent_hex": "#38BDF8"},
            "menu": {"effect_id": "curtain_drape", "duration_ms": 260, "easing": "spring_smooth", "accent_hex": "#A78BFA"},
            "popup": {"effect_id": "liquid_ripple", "duration_ms": 320, "easing": "spring_smooth", "accent_hex": "#06B6D4"},
        },
        "wobbly": True,
        "momentum": True,
    },
    "3D Architectural Studio": {
        "desc_en": "3D Doors, Venetian Blinds, Page Curl, Isometric Flip, and Shatter Glass.",
        "desc_th": "สาย 3 มิติเรขาคณิต: ประตูคู่, มู่ลี่หมุน, ม้วนหน้ากระดาษ และกระจกแตก",
        "triggers": {
            "open": {"effect_id": "doors_3d", "duration_ms": 380, "easing": "spring_smooth", "accent_hex": "#74AAFF"},
            "close": {"effect_id": "shatter_glass", "duration_ms": 480, "easing": "expo_out", "accent_hex": "#BAE6FD"},
            "minimize": {"effect_id": "page_peel", "duration_ms": 420, "easing": "cubic_inout", "accent_hex": "#FBBF24"},
            "restore": {"effect_id": "cube_flip", "duration_ms": 380, "easing": "spring_smooth", "accent_hex": "#2D81FF"},
            "menu": {"effect_id": "venetian_blinds", "duration_ms": 260, "easing": "expo_out", "accent_hex": "#94A3B8"},
            "popup": {"effect_id": "domino_tiles", "duration_ms": 340, "easing": "spring_smooth", "accent_hex": "#4ADE80"},
        },
        "wobbly": False,
        "momentum": True,
    },
    "Chaos Random Showcase": {
        "desc_en": "Randomly cycles across your favorite GPU shaders on every window action!",
        "desc_th": "สุ่มแอนิเมชันจากคอลเลกชันโปรดทุกครั้งที่เปิด ปิด หรือย่อหน้าต่าง!",
        "triggers": {
            "open": {"effect_id": "random", "duration_ms": 360, "easing": "spring_smooth", "accent_hex": "#74AAFF"},
            "close": {"effect_id": "random", "duration_ms": 440, "easing": "expo_out", "accent_hex": "#F97316"},
            "minimize": {"effect_id": "random", "duration_ms": 380, "easing": "spring_smooth", "accent_hex": "#38BDF8"},
            "restore": {"effect_id": "random", "duration_ms": 360, "easing": "spring_smooth", "accent_hex": "#74AAFF"},
            "menu": {"effect_id": "random", "duration_ms": 220, "easing": "expo_out", "accent_hex": "#2D81FF"},
            "popup": {"effect_id": "random", "duration_ms": 260, "easing": "spring_elastic", "accent_hex": "#A78BFA"},
        },
        "wobbly": True,
        "momentum": True,
    },
}


class ConfigStore:
    """Atomic file store for PrismConfig + user presets."""

    def __init__(self, data_dir: pathlib.Path) -> None:
        self.data_dir = data_dir
        self.config_path = data_dir / "config.json"
        self.presets_dir = data_dir / "presets"
        self.shaders_dir = data_dir / "shaders"
        self.last_load_recovered_corrupt: bool = False
        for d in (self.data_dir, self.presets_dir, self.shaders_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def load(self) -> PrismConfig:
        self.last_load_recovered_corrupt = False
        try:
            exists = self.config_path.exists()
        except OSError:
            return PrismConfig()
        if not exists:
            cfg = PrismConfig()
            self.save(cfg)
            return cfg
        try:
            raw_text = self.config_path.read_text(encoding="utf-8-sig")
            raw = json.loads(raw_text)
            if not isinstance(raw, dict):
                raise ValueError("Root config JSON must be an object")
            return PrismConfig.from_dict(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.last_load_recovered_corrupt = True
            try:
                bak_path = self.config_path.with_name(f"{self.config_path.name}.corrupt.bak")
                shutil.copyfile(self.config_path, bak_path)
            except OSError:
                pass
            return PrismConfig()
        except OSError:
            return PrismConfig()

    def save(self, cfg: PrismConfig) -> bool:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        except (OSError, TypeError, ValueError):
            return False
        tmp = self.config_path.with_suffix(".json.part")
        for attempt in range(5):
            try:
                with open(tmp, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.config_path)
                return True
            except OSError:
                if attempt == 4:
                    try:
                        with open(self.config_path, "wb") as f:
                            f.write(payload)
                            f.flush()
                            os.fsync(f.fileno())
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return True
                    except OSError:
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return False
                time.sleep(0.008 * (attempt + 1))
        return False

    def apply_preset(self, cfg: PrismConfig, preset_name: str) -> PrismConfig:
        updated = copy.deepcopy(cfg)
        if not isinstance(preset_name, str) or not preset_name.strip():
            return updated
        if preset_name in BUILTIN_PRESETS:
            p = BUILTIN_PRESETS[preset_name]
            updated.active_preset = preset_name
            for k, tv in p["triggers"].items():
                if k in updated.triggers:
                    updated.triggers[k].enabled = True
                    updated.triggers[k].effect_id = tv["effect_id"]
                    updated.triggers[k].duration_ms = tv["duration_ms"]
                    updated.triggers[k].easing = tv["easing"]
                    updated.triggers[k].accent_hex = sanitize_hex_color(
                        tv["accent_hex"], updated.triggers[k].accent_hex
                    )
            updated.physics.wobbly_enabled = bool(p["wobbly"])
            updated.physics.momentum_enabled = bool(p["momentum"])
            return updated

        clean_name = preset_name.strip()
        if (
            "\x00" in clean_name
            or "/" in clean_name
            or "\\" in clean_name
            or ":" in clean_name
            or ".." in clean_name
            or _is_windows_reserved_name(clean_name)
        ):
            return updated

        try:
            presets_root = self.presets_dir.resolve()
            candidate = self.presets_dir / f"{clean_name}.prismpreset.json"
            if candidate.is_symlink():
                link_target = str(candidate.readlink())
                if link_target.replace("/", "\\").startswith(("\\\\", "\\?")):
                    return updated
            user_file = candidate.resolve()
            if (
                not user_file.is_relative_to(presets_root)
                or not user_file.is_file()
                or user_file.stat().st_size > 512 * 1024
            ):
                return updated
            raw = json.loads(user_file.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                return updated
            loaded = PrismConfig.from_dict(raw)
            loaded.active_preset = clean_name
            loaded.window_geometry = updated.window_geometry
            loaded.language = updated.language
            return loaded
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            return updated

    def save_user_preset(self, name: str, cfg: PrismConfig) -> Optional[pathlib.Path]:
        safe = _sanitize_preset_name(name)
        tmp: Optional[pathlib.Path] = None
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
            presets_root = self.presets_dir.resolve()
            out = (self.presets_dir / f"{safe}.prismpreset.json").resolve()
            if not out.is_relative_to(presets_root):
                return None
            tmp = out.with_suffix(".part")
            data = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out)
            return out
        except (OSError, TypeError, ValueError):
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return None

    def list_presets(self) -> list[tuple[str, bool]]:
        """Return list of (preset_name, is_builtin)."""
        items: list[tuple[str, bool]] = [(name, True) for name in BUILTIN_PRESETS]
        try:
            for f in sorted(self.presets_dir.glob("*.prismpreset.json")):
                items.append((f.name.replace(".prismpreset.json", ""), False))
        except OSError:
            pass
        return items
