"""Complete registry of 42 original GPU shader & physics effects + custom GLSL plugin loader.

Zero UI imports — pure data & plugin discovery for both headless core and GPU renderer.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
from dataclasses import dataclass, field
from typing import Optional

from .logging_setup import get_logger

_log = get_logger("prismfx.core.effects")

MAX_CUSTOM_SHADER_BYTES = 128 * 1024
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def sanitize_hex_color(value: object, fallback: str = "#2D81FF") -> str:
    """Validate a strict #RRGGBB hex color string, returning `fallback` on invalid input."""
    s = str(value or "").strip()
    if _HEX_COLOR_RE.fullmatch(s):
        return s
    fb = str(fallback or "").strip()
    if _HEX_COLOR_RE.fullmatch(fb):
        return fb
    return "#2D81FF"


def _safe_int_clamp(value: object, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return max(lo, min(hi, value))
    if isinstance(value, (float, str)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(num):
            return default
        return max(lo, min(hi, int(round(num))))
    return default

CATEGORIES: dict[str, tuple[str, str]] = {
    "all": ("All Effects", "ทั้งหมด"),
    "spatial": ("Spatial & Fluid", "สมูทและฟิสิกส์"),
    "elemental": ("Elemental & Energy", "ไฟ น้ำแข็ง พลังงาน"),
    "cyber": ("Cyber & Glitch", "ไซเบอร์และกลิตช์"),
    "arch3d": ("3D Architectural", "3 มิติและเรขาคณิต"),
    "classic": ("Clean & Directional", "คลาสสิกและมินิมอล"),
    "custom": ("Custom Shaders", "เชดเดอร์กำหนดเอง"),
}

TRIGGER_TYPES: list[tuple[str, str, str]] = [
    ("open", "Window Open", "เปิดหน้าต่าง"),
    ("close", "Window Close", "ปิดหน้าต่าง"),
    ("minimize", "Window Minimize", "ย่อหน้าต่าง"),
    ("restore", "Window Restore", "คืนค่าหน้าต่าง"),
    ("menu", "Menus & Context", "เมนูคลิกขวา"),
    ("popup", "Popups & Flyouts", "หน้าต่างป๊อปอัป"),
]

EASING_CURVES: list[tuple[str, str, str]] = [
    ("spring_smooth", "Critically Damped Spring", "สปริงนุ่มนวล (165Hz)"),
    ("spring_elastic", "Elastic Overshoot", "สปริงเด้งยืดหยุ่น"),
    ("expo_out", "Exponential Decel", "เร่งเร็วแล้วค่อยหยุด"),
    ("cubic_inout", "Cubic Silk", "ซิลค์สมูทหัวท้าย"),
    ("back_out", "Kinetic Snap", "สแนปเกินเล็กน้อย"),
    ("linear", "Linear Constant", "ความเร็วคงที่"),
]


@dataclass(frozen=True)
class EffectSpec:
    id: str
    shader_id: int
    name_en: str
    name_th: str
    category: str
    desc_en: str
    desc_th: str
    default_ms: int = 340
    accent_hex: str = "#2D81FF"
    mesh_subdiv: int = 24
    supports_open: bool = True
    supports_close: bool = True
    is_custom: bool = False
    custom_frag_path: Optional[str] = None
    badge: str = "GPU"


BUILTIN_EFFECTS: list[EffectSpec] = [
    # --- Special modes ---
    EffectSpec("none", 0, "OS Default (Off)", "ค่าเริ่มต้น Windows", "classic",
               "Use native Windows transition without PrismFX overlay.",
               "ใช้แอนิเมชันมาตรฐานของ Windows โดยไม่ซ้อนทับ", 180, "#6E7681", 1, badge="SYS"),
    EffectSpec("random", -1, "Smart Random Pool", "สุ่มจากรายการโปรด", "spatial",
               "Picks a fresh effect from your curated random pool on every trigger.",
               "สุ่มแอนิเมชันจากคอลเลกชันที่คุณเลือกไว้ในทุกครั้งที่ทำงาน", 360, "#74AAFF", 24, badge="RND"),

    # --- Category 1: Spatial & Fluid (10) ---
    EffectSpec("prism_glide", 1, "Prism Glide", "ปริซึมลื่นไหล", "spatial",
               "Critically damped spring scale with subtle chromatic prism refraction.",
               "ขยาย-หดด้วยสปริงนุ่มนวลพร้อมแสงหักเหปริซึมบางเบา", 300, "#2D81FF", 16, badge="NEW"),
    EffectSpec("win11_silk", 2, "Win11 Silk Spring", "วินโดวส์ 11 ซิลค์", "spatial",
               "Refined Windows 11 spatial depth lift tuned for 165 Hz displays.",
               "ยกระดับแอนิเมชันสไตล์ Win11 ให้นุ่มลื่นระดับ 165 Hz", 260, "#74AAFF", 8, badge="165Hz"),
    EffectSpec("genie_lamp", 3, "Magic Genie Lamp", "ตะเกียงวิเศษ (Genie)", "spatial",
               "True cubic-Bezier suction funnel curving smoothly into taskbar or cursor.",
               "ดูดหน้าต่างเป็นเส้นโค้งเบซิเยร์ลงสู่ทาสก์บาร์หรือเมาส์", 420, "#38BDF8", 32, badge="3D"),
    EffectSpec("liquid_ripple", 4, "Liquid Ripple", "คลื่นน้ำกระจาย", "spatial",
               "Radial water-surface ripple refraction with smooth damped dispersion.",
               "ผิวน้ำกระเพื่อมเป็นวงคลื่นพร้อมการหักเหแสงแบบเรียลไทม์", 400, "#06B6D4", 24, badge="GLSL"),
    EffectSpec("aero_float", 5, "Aero Levitation", "ลอยตัวแอโร", "spatial",
               "Weightless vertical float with subtle directional motion blur.",
               "ลอยขึ้นลงไร้น้ำหนักพร้อมเบลอตามทิศทางการเคลื่อนที่", 320, "#60A5FA", 12, badge="GPU"),
    EffectSpec("elastic_pop", 6, "Elastic Spring Pop", "ป๊อปเด้งยืดหยุ่น", "spatial",
               "Playful rubberized overshoot pop driven by closed-form spring math.",
               "เด้งขยายแบบยืดหยุ่นด้วยสมการสปริงฟิสิกส์แท้", 360, "#34D399", 16, badge="PHYS"),
    EffectSpec("origami_fold", 7, "Origami Fold", "พับกระดาษโอริกามิ", "spatial",
               "Multi-panel geometric paper fold with 3D specular shading.",
               "พับหน้าต่างเป็นแผ่นกระดาษหลายทบพร้อมเงา 3 มิติ", 440, "#F59E0B", 24, badge="3D"),
    EffectSpec("curtain_drape", 8, "Velvet Curtain", "ม่านกำมะหยี่", "spatial",
               "Soft cloth wave gather sweeping across the window frame.",
               "รวบหน้าต่างเป็นลอนคลื่นคล้ายผ้าม่านนุ่มนวล", 420, "#A78BFA", 28, badge="3D"),
    EffectSpec("zoom_blur", 9, "Hyper Zoom Blur", "ซูมเบลอความเร็วสูง", "spatial",
               "GPU radial speed-blur burst zooming cleanly in or out of depth.",
               "ซูมพุ่งเข้า-ออกพร้อมเรเดียลเบลอความเร็วสูงบน GPU", 280, "#2D81FF", 8, badge="GLSL"),
    EffectSpec("gravity_drop", 10, "Gravity Squash", "แรงโน้มถ่วงตกกระทบ", "spatial",
               "Parabolic gravity fall with squash-and-stretch impulse.",
               "ร่วงหล่นตามแรงโน้มถ่วงพร้อมแรงยุบและดีดตัว", 380, "#FB7185", 20, badge="PHYS"),

    # --- Category 2: Elemental & Energy Shaders (10) ---
    EffectSpec("ember_burn", 11, "Incinerate Burn", "เผาไหม้เป็นเถ้าถ่าน", "elemental",
               "Procedural Simplex noise burn front with glowing molten embers and ash.",
               "เผาไหม้ขอบหน้าต่างด้วยไฟสีส้มทองและสะเก็ดเถ้าถ่านบน GPU", 540, "#F97316", 12, badge="GLSL"),
    EffectSpec("plasma_dissolve", 12, "Plasma Vaporize", "สลายพลังงานพลาสมา", "elemental",
               "High-voltage cyan-violet plasma arcs vaporizing window pixels.",
               "สลายหน้าต่างเป็นประจุไฟฟ้าพลาสมาสีฟ้า-ม่วง", 480, "#8B5CF6", 12, badge="GLSL"),
    EffectSpec("frost_shatter", 13, "Cryo Frost Shatter", "แช่แข็งแตกร้าว", "elemental",
               "Voronoi ice crystals spread across glass before shattering away.",
               "เกล็ดน้ำแข็งวอรอนอยเกาะทั่วหน้าต่างก่อนแตกกระจาย", 520, "#38BDF8", 24, badge="GLSL"),
    EffectSpec("thanos_snap", 14, "Wind Dust Snap", "สลายเป็นฝุ่นตามลม", "elemental",
               "Directional noise erosion lifting pixels into wind-blown dust particles.",
               "สลายหน้าต่างเป็นละอองฝุ่นปลิวหายไปตามแรงลม", 580, "#D97706", 20, badge="GLSL"),
    EffectSpec("black_hole", 15, "Event Horizon Vortex", "หลุมดำดูดวน", "elemental",
               "Gravitational lensing spiral with glowing accretion ring into singularity.",
               "หมุนวนบิดเบี้ยวด้วยแรงโน้มถ่วงหลุมดำพร้อมวงแหวนแสง", 480, "#6366F1", 32, badge="GLSL"),
    EffectSpec("quantum_portal", 16, "Quantum Rift", "ประตูมิติควอนตัม", "elemental",
               "Spatial warp compressing the window through a luminous dimensional slit.",
               "บีบอัดหน้าต่างหายเข้าไปในรอยแยกมิติเรืองแสง", 440, "#0EA5E9", 24, badge="GLSL"),
    EffectSpec("neon_vapor", 17, "Laser Scan Vapor", "เลเซอร์สแกนสลาย", "elemental",
               "High-energy horizontal laser sweep with phosphor glow trail.",
               "ลำแสงเลเซอร์กวาดสแกนผ่านหน้าต่างพร้อมหางแสงฟอสเฟอร์", 380, "#EC4899", 8, badge="GLSL"),
    EffectSpec("aurora_veil", 18, "Aurora Veil", "ม่านแสงเหนือออโรรา", "elemental",
               "Iridescent northern-lights shimmer dissolving along flowing sine ribbons.",
               "ละลายเป็นริ้วแสงเหนือหลากสีพลิ้วไหวบนหน้าจอ", 460, "#10B981", 20, badge="GLSL"),
    EffectSpec("shockwave_nova", 19, "Shockwave Nova", "คลื่นกระแทกโนวา", "elemental",
               "Expanding chromatic ring shockwave rippling out from window center.",
               "ระเบิดคลื่นกระแทกสีรุ้งกระจายออกจากกึ่งกลางหน้าต่าง", 420, "#3B82F6", 16, badge="GLSL"),
    EffectSpec("ember_rise", 20, "Phoenix Thermal", "เปลวไฟฟีนิกซ์", "elemental",
               "Rising heat distortion and golden ember wave (great for Open & Close).",
               "คลื่นความร้อนและประกายไฟสีทองลอยขึ้น เหมาะทั้งเปิดและปิด", 480, "#EF4444", 20, badge="GLSL"),

    # --- Category 3: Cyber & Glitch Shaders (8) ---
    EffectSpec("cyber_glitch", 21, "Chromatic Glitch", "กลิตช์แยกสี RGB", "cyber",
               "RGB channel split, horizontal block slice tear, and digital static jitter.",
               "แยกสี RGB ตัดแถบภาพฉีกขาดแบบไซเบอร์พังก์", 340, "#F43F5E", 8, badge="GLSL"),
    EffectSpec("crt_tv_off", 22, "Retro CRT Power-Off", "ทีวีจอแก้ว CRT ปิดเครื่อง", "cyber",
               "Iconic cathode-ray collapse into a blazing horizontal beam & white dot.",
               "ยุบจอเป็นเส้นแสงแนวนอนและจุดสว่างกลางจอแบบทีวี CRT", 360, "#E2E8F0", 8, badge="RETRO"),
    EffectSpec("matrix_rain", 23, "Digital Rain Melt", "สายฝนดิจิทัล", "cyber",
               "Vertical column cascade dripping pixels with cyber phosphor highlights.",
               "ภาพไหลลงเป็นคอลัมน์ดิจิทัลพร้อมแสงขอบฟอสเฟอร์", 460, "#22C55E", 16, badge="GLSL"),
    EffectSpec("pixel_sort", 24, "Pixel Sort Drift", "เรียงพิกเซลไหลยืด", "cyber",
               "Luminance-driven vertical pixel stretching like generative glitch art.",
               "ดึงยืดพิกเซลตามค่าความสว่างสไตล์ศิลปะดิจิทัล", 400, "#A855F7", 8, badge="GLSL"),
    EffectSpec("pixelate_mosaic", 25, "Mosaic Deconstruct", "แตกเป็นโมเสกพิกเซล", "cyber",
               "Progressive GPU block mosaic coarsening into dissolving pixel tiles.",
               "ค่อยๆ แตกภาพเป็นบล็อกพิกเซลขนาดใหญ่แล้วจางหาย", 360, "#06B6D4", 8, badge="GLSL"),
    EffectSpec("holo_flicker", 26, "Hologram De-Sync", "โฮโลแกรมหลุดเฟรม", "cyber",
               "Sci-fi cyan hologram interference flicker with scanline desynchronization.",
               "กระพริบสัญญาณโฮโลแกรมไซไฟพร้อมเส้นสแกนไลน์", 360, "#22D3EE", 12, badge="GLSL"),
    EffectSpec("vhs_rewind", 27, "VHS Tape Warp", "เทปวิดีโอ VHS", "cyber",
               "Analog tracking line warp, tape skew, and nostalgic snow static.",
               "ภาพบิดเบี้ยวแบบม้วนเทป VHS พร้อมคลื่นสัญญาณแอนะล็อก", 400, "#FB923C", 12, badge="RETRO"),
    EffectSpec("hex_shield", 28, "Hex Grid Dissolve", "เกราะรังผึ้งหกเหลี่ยม", "cyber",
               "Hexagonal energy honeycomb sweeping diagonally with luminous borders.",
               "ลายรังผึ้งหกเหลี่ยมเรืองแสงกวาดปิด-เปิดทั่วทั้งหน้าต่าง", 440, "#38BDF8", 8, badge="GLSL"),

    # --- Category 4: 3D Architectural & Geometric (8) ---
    EffectSpec("doors_3d", 29, "Architectural Doors", "ประตูคู่ 3 มิติ", "arch3d",
               "Window splits cleanly down the center and swings open in true 3D depth.",
               "แยกหน้าต่างครึ่งซ้าย-ขวาแล้วเปิดออกเป็นบานประตู 3 มิติ", 400, "#74AAFF", 24, badge="3D"),
    EffectSpec("venetian_blinds", 30, "Venetian Blinds 3D", "มู่ลี่หมุน 3 มิติ", "arch3d",
               "Horizontal louvers rotate synchronously with depth shading.",
               "ซอยหน้าต่างเป็นแถบมู่ลี่หมุนพลิกพร้อมมิติแสงเงา", 400, "#94A3B8", 24, badge="3D"),
    EffectSpec("cube_flip", 31, "Isometric 3D Flip", "พลิกการ์ด 3 มิติ", "arch3d",
               "Full 3D perspective Y-axis card flip with clean specular sheen.",
               "หมุนพลิกหน้าต่างรอบแกนแนวตั้งแบบ 3 มิติเต็มรูปแบบ", 380, "#2D81FF", 16, badge="3D"),
    EffectSpec("accordion_fold", 32, "Accordion Pleat", "พับหีบเพลง 3 มิติ", "arch3d",
               "Alternating zig-zag perspective folds compressing tightly to one edge.",
               "พับจีบสลับฟันปลาแบบหีบเพลงหดชิดขอบหน้าต่าง", 420, "#60A5FA", 32, badge="3D"),
    EffectSpec("shatter_glass", 33, "Shatter Glass 3D", "กระจกแตกกระจาย", "arch3d",
               "Voronoi glass facets break apart, spin, and tumble with gravity.",
               "แตกหน้าต่างเป็นเศษกระจกหลายเหลี่ยมหมุนปลิวตามแรงโน้มถ่วง", 500, "#BAE6FD", 24, badge="3D"),
    EffectSpec("spiral_twist", 34, "Helix Ribbon Twist", "เกลียวบิดเฮลิกซ์", "arch3d",
               "Progressive vertical-row twist corkscrewing gracefully into depth.",
               "บิดหน้าต่างเป็นเกลียวคลื่นต่อเนื่องจากบนลงล่าง", 440, "#C084FC", 32, badge="3D"),
    EffectSpec("page_peel", 35, "3D Page Curl", "ม้วนเปิดหน้ากระดาษ", "arch3d",
               "Smooth cylindrical corner page peel with underside highlight.",
               "ม้วนเปิดมุมหน้าต่างโค้งงอเหมือนพลิกหน้าหนังสือ", 440, "#FBBF24", 32, badge="3D"),
    EffectSpec("domino_tiles", 36, "Domino Tile Cascade", "กระเบื้องโดมิโนพลิก", "arch3d",
               "Staggered matrix of tiles flipping sequentially across the window.",
               "แผ่นตารางโดมิโนทยอยพลิกล้มเรียงต่อกันทั่วหน้าต่าง", 460, "#4ADE80", 24, badge="3D"),

    # --- Category 5: Clean & Directional (6) ---
    EffectSpec("clean_fade", 37, "Pure Crossfade", "เฟดจางบริสุทธิ์", "classic",
               "Zero-distortion ultra-crisp alpha fade for minimalist speed.",
               "จางเข้า-ออกแบบคลีนที่สุด เร็วและสบายตา", 220, "#94A3B8", 2, badge="FAST"),
    EffectSpec("slide_edge", 38, "Directional Slide", "สไลด์จากขอบจอ", "classic",
               "Silky spring glide from the nearest monitor edge with motion fade.",
               "เลื่อนสไลด์เข้ามาอย่างนุ่มนวลจากขอบจอที่ใกล้ที่สุด", 280, "#2D81FF", 4, badge="FAST"),
    EffectSpec("swipe_reveal", 39, "Architectural Wipe", "ปาดม่านเปิด-ปิด", "classic",
               "Sharp razor-line mask wipe with thin luminous leading edge.",
               "ปาดเปิด-ปิดหน้าต่างด้วยเส้นแสงคมกริบสไตล์สถาปัตยกรรม", 300, "#74AAFF", 4, badge="CLEAN"),
    EffectSpec("corner_collapse", 40, "Corner Anchor Zoom", "ยุบสู่มุมหน้าต่าง", "classic",
               "Crisp perspective zoom anchored toward the window's active corner.",
               "ย่อ-ขยายอย่างแม่นยำยึดตำแหน่งมุมหน้าต่าง", 280, "#38BDF8", 4, badge="FAST"),
    EffectSpec("spin_orbit", 41, "Orbital Spin", "หมุนควงออร์บิทัล", "classic",
               "Smooth Z-axis orbital rotation paired with exponential zoom.",
               "หมุนควงรอบแกนกลางอย่างสมดุลพร้อมย่อ-ขยาย", 360, "#818CF8", 8, badge="GPU"),
    EffectSpec("wobble_snap", 42, "Jelly Wobble Pop", "เด้งดึ๋งเยลลี่", "classic",
               "Multi-frequency Compiz-inspired jelly bend as the window enters or exits.",
               "หน้าต่างยืดหยุ่นสั่นไหวสไตล์เยลลี่ขณะเปิดและปิด", 380, "#34D399", 24, badge="PHYS"),
]


class EffectRegistry:
    """Lookup & custom GLSL shader plugin manager."""

    def __init__(self, custom_dirs: Optional[list[pathlib.Path]] = None) -> None:
        self._effects: dict[str, EffectSpec] = {e.id: e for e in BUILTIN_EFFECTS}
        self._order: list[str] = [e.id for e in BUILTIN_EFFECTS]
        self._next_custom_shader_id: int = 1000
        self._custom_shader_ids: dict[str, int] = {}
        self._custom_dirs: list[pathlib.Path] = [pathlib.Path(d) for d in (custom_dirs or [])]
        for d in self._custom_dirs:
            self.scan_custom_dir(d)

    def reload_custom_dirs(self, custom_dirs: Optional[list[pathlib.Path]] = None) -> int:
        """Remove all loaded custom shaders and rescan configured custom shader directories."""
        if custom_dirs is not None:
            self._custom_dirs = [pathlib.Path(d) for d in custom_dirs]

        custom_ids = [eid for eid, spec in self._effects.items() if spec.is_custom]
        for eid in custom_ids:
            self._effects.pop(eid, None)
        self._order = [eid for eid in self._order if eid in self._effects]

        total_loaded = 0
        for d in self._custom_dirs:
            total_loaded += self.scan_custom_dir(d)
        return total_loaded

    def scan_custom_dir(self, directory: pathlib.Path) -> int:
        """Discover user `.prismfx.json` + `.glsl` custom fragment shader plugins."""
        directory = pathlib.Path(directory)
        try:
            if not directory.exists() or not directory.is_dir():
                return 0
            base_dir = directory.resolve()
            meta_paths = sorted(directory.glob("*.prismfx.json"))
        except OSError as exc:
            _log.warning("Failed to scan custom shader directory %s: %s", directory, exc)
            return 0
        loaded = 0
        for meta_path in meta_paths:
            try:
                if meta_path.is_symlink():
                    link_target = str(meta_path.readlink())
                    if link_target.replace("/", "\\").startswith(("\\\\", "\\?")):
                        continue
                resolved_meta = meta_path.resolve()
                if (
                    not resolved_meta.is_relative_to(base_dir)
                    or not resolved_meta.is_file()
                    or resolved_meta.stat().st_size > MAX_CUSTOM_SHADER_BYTES
                ):
                    continue
                raw = json.loads(resolved_meta.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    continue
                raw_eid = raw.get("id")
                eid = (
                    raw_eid.strip()
                    if isinstance(raw_eid, str) and raw_eid.strip()
                    else meta_path.stem.replace(".prismfx", "").strip()
                )
                if not eid or "\x00" in eid:
                    continue

                if "fragment" in raw and raw["fragment"] is not None:
                    if not isinstance(raw["fragment"], str):
                        continue
                    frag_name = raw["fragment"].strip()
                else:
                    frag_name = f"{eid}.glsl"

                # Reject empty, NUL, drive/ADS, UNC (\\ or //), absolute, or traversal paths
                # BEFORE any filesystem call so Windows never issues outbound SMB auth.
                if (
                    not frag_name
                    or "\x00" in frag_name
                    or ":" in frag_name
                    or frag_name.startswith(("\\", "/"))
                ):
                    continue
                frag_rel = pathlib.Path(frag_name)
                if (
                    frag_rel.is_absolute()
                    or frag_rel.drive
                    or frag_rel.root
                    or ".." in frag_rel.parts
                    or frag_rel.suffix.lower() != ".glsl"
                ):
                    continue

                candidate = resolved_meta.parent / frag_rel
                if candidate.is_symlink():
                    link_target = str(candidate.readlink())
                    if link_target.replace("/", "\\").startswith(("\\\\", "\\?")):
                        continue
                frag_path = candidate.resolve()
                if (
                    not frag_path.is_relative_to(base_dir)
                    or frag_path.suffix.lower() != ".glsl"
                    or not frag_path.is_file()
                    or frag_path.stat().st_size > MAX_CUSTOM_SHADER_BYTES
                ):
                    continue

                name_en = raw.get("name_en")
                name_en_s = name_en.strip() if isinstance(name_en, str) and name_en.strip() else eid
                name_th = raw.get("name_th")
                name_th_s = name_th.strip() if isinstance(name_th, str) and name_th.strip() else name_en_s
                desc_en = raw.get("desc_en")
                desc_en_s = (
                    desc_en.strip()
                    if isinstance(desc_en, str) and desc_en.strip()
                    else "Custom user GLSL shader effect."
                )
                desc_th = raw.get("desc_th")
                desc_th_s = (
                    desc_th.strip()
                    if isinstance(desc_th, str) and desc_th.strip()
                    else "เชดเดอร์ GLSL กำหนดเองโดยผู้ใช้"
                )

                full_id = f"custom_{eid}"
                existing = self._effects.get(full_id)
                if existing is not None and existing.is_custom:
                    shader_id = existing.shader_id
                elif full_id in self._custom_shader_ids:
                    shader_id = self._custom_shader_ids[full_id]
                else:
                    shader_id = self._next_custom_shader_id
                    self._next_custom_shader_id += 1
                    self._custom_shader_ids[full_id] = shader_id

                spec = EffectSpec(
                    id=full_id,
                    shader_id=shader_id,
                    name_en=name_en_s,
                    name_th=name_th_s,
                    category="custom",
                    desc_en=desc_en_s,
                    desc_th=desc_th_s,
                    default_ms=_safe_int_clamp(raw.get("default_ms"), 380, 80, 1500),
                    accent_hex=sanitize_hex_color(raw.get("accent_hex"), "#2D81FF"),
                    mesh_subdiv=_safe_int_clamp(raw.get("mesh_subdiv"), 16, 2, 48),
                    is_custom=True,
                    custom_frag_path=str(frag_path),
                    badge="USER",
                )
                self._effects[spec.id] = spec
                if spec.id not in self._order:
                    self._order.append(spec.id)
                loaded += 1
            except Exception as exc:
                _log.warning("Failed to load custom shader metadata %s: %s", meta_path, exc)
                continue
        return loaded

    def get(self, effect_id: str) -> EffectSpec:
        return self._effects.get(effect_id) or self._effects["prism_glide"]

    def all_effects(self, include_special: bool = True) -> list[EffectSpec]:
        items = [self._effects[k] for k in self._order]
        if not include_special:
            items = [e for e in items if e.id not in ("none", "random")]
        return items

    def by_category(self, category: str) -> list[EffectSpec]:
        if category == "all":
            return self.all_effects(include_special=True)
        return [self._effects[k] for k in self._order if self._effects[k].category == category]

    def shader_effect_count(self) -> int:
        return len([e for e in self._effects.values() if e.shader_id > 0])
