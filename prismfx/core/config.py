"""Atomic configuration & preset management (`.part` -> fsync -> atomic replace)."""

from __future__ import annotations

import copy
import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any


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
        cfg.enabled = bool(raw.get("enabled", cfg.enabled))
        cfg.language = str(raw.get("language", cfg.language)) if raw.get("language") in ("th", "en") else "th"
        cfg.active_preset = str(raw.get("active_preset", cfg.active_preset))
        cfg.global_speed_scale = max(0.35, min(2.5, float(raw.get("global_speed_scale", cfg.global_speed_scale))))
        cfg.target_fps = max(30, min(360, int(raw.get("target_fps", cfg.target_fps))))
        cfg.start_with_windows = bool(raw.get("start_with_windows", cfg.start_with_windows))
        cfg.start_with_windows_admin = bool(raw.get("start_with_windows_admin", cfg.start_with_windows_admin))
        cfg.autostart_minimized = bool(raw.get("autostart_minimized", cfg.autostart_minimized))
        cfg.start_minimized_to_tray = bool(raw.get("start_minimized_to_tray", cfg.start_minimized_to_tray))
        geom = raw.get("window_geometry")
        if isinstance(geom, list) and len(geom) == 4:
            cfg.window_geometry = [int(v) for v in geom]

        raw_triggers = raw.get("triggers", {})
        if isinstance(raw_triggers, dict):
            for k, default_tc in cfg.triggers.items():
                if isinstance(raw_triggers.get(k), dict):
                    td = raw_triggers[k]
                    cfg.triggers[k] = TriggerConfig(
                        enabled=bool(td.get("enabled", default_tc.enabled)),
                        effect_id=str(td.get("effect_id", default_tc.effect_id)),
                        duration_ms=max(80, min(1400, int(td.get("duration_ms", default_tc.duration_ms)))),
                        easing=str(td.get("easing", default_tc.easing)),
                        intensity=max(0.2, min(2.0, float(td.get("intensity", default_tc.intensity)))),
                        accent_hex=str(td.get("accent_hex", default_tc.accent_hex)),
                    )

        rp = raw.get("random_pool")
        if isinstance(rp, list) and rp:
            cfg.random_pool = [str(x) for x in rp]

        for sub_name, sub_cls in (
            ("physics", PhysicsConfig),
            ("focus", FocusConfig),
            ("safety", SafetyConfig),
        ):
            sub_raw = raw.get(sub_name)
            if isinstance(sub_raw, dict):
                base = asdict(getattr(cfg, sub_name))
                for k, v in sub_raw.items():
                    if k in base:
                        base[k] = v
                setattr(cfg, sub_name, sub_cls(**base))
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
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.shaders_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> PrismConfig:
        if not self.config_path.exists():
            cfg = PrismConfig()
            self.save(cfg)
            return cfg
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            return PrismConfig.from_dict(raw)
        except Exception:
            return PrismConfig()

    def save(self, cfg: PrismConfig) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.part")
        payload = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        for attempt in range(5):
            try:
                with open(tmp, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.config_path)
                return
            except PermissionError:
                if attempt == 4:
                    with open(self.config_path, "wb") as f:
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                    tmp.unlink(missing_ok=True)
                    return
                time.sleep(0.008 * (attempt + 1))

    def apply_preset(self, cfg: PrismConfig, preset_name: str) -> PrismConfig:
        updated = copy.deepcopy(cfg)
        if preset_name in BUILTIN_PRESETS:
            p = BUILTIN_PRESETS[preset_name]
            updated.active_preset = preset_name
            for k, tv in p["triggers"].items():
                if k in updated.triggers:
                    updated.triggers[k].enabled = True
                    updated.triggers[k].effect_id = tv["effect_id"]
                    updated.triggers[k].duration_ms = tv["duration_ms"]
                    updated.triggers[k].easing = tv["easing"]
                    updated.triggers[k].accent_hex = tv["accent_hex"]
            updated.physics.wobbly_enabled = bool(p["wobbly"])
            updated.physics.momentum_enabled = bool(p["momentum"])
            return updated

        user_file = self.presets_dir / f"{preset_name}.prismpreset.json"
        if user_file.exists():
            raw = json.loads(user_file.read_text(encoding="utf-8"))
            loaded = PrismConfig.from_dict(raw)
            loaded.active_preset = preset_name
            loaded.window_geometry = updated.window_geometry
            loaded.language = updated.language
            return loaded
        return updated

    def save_user_preset(self, name: str, cfg: PrismConfig) -> pathlib.Path:
        safe = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip() or "Custom Preset"
        out = self.presets_dir / f"{safe}.prismpreset.json"
        tmp = out.with_suffix(".part")
        data = json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
        return out

    def list_presets(self) -> list[tuple[str, bool]]:
        """Return list of (preset_name, is_builtin)."""
        items: list[tuple[str, bool]] = [(name, True) for name in BUILTIN_PRESETS]
        for f in sorted(self.presets_dir.glob("*.prismpreset.json")):
            items.append((f.name.replace(".prismpreset.json", ""), False))
        return items
