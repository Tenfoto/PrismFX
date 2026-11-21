"""165 Hz closed-form spring math, Compiz-style wobbly mesh, momentum drag, flicks & snap layouts.

Strictly zero UI/Qt imports — deterministic and unit-testable headless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .win32_api import MonitorRect


# ---------------------------------------------------------------------------
# Easing & Closed-Form Analytical Damped Springs
# ---------------------------------------------------------------------------
def eval_easing(t: float, curve: str = "spring_smooth") -> float:
    """Evaluate normalized progress t in [0, 1] -> eased progress (may briefly overshoot)."""
    t = max(0.0, min(1.0, float(t)))
    if curve == "linear":
        return t
    if curve == "cubic_inout":
        return 4 * t * t * t if t < 0.5 else 1.0 - pow(-2 * t + 2, 3) / 2.0
    if curve == "expo_out":
        return 1.0 if t >= 1.0 else 1.0 - pow(2.0, -10.0 * t)
    if curve == "back_out":
        c1 = 1.70158
        c3 = c1 + 1.0
        return 1.0 + c3 * pow(t - 1.0, 3) + c1 * pow(t - 1.0, 2)
    if curve == "spring_elastic":
        # Under-damped analytical spring normalized to reach 1.0 cleanly at t=1
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        omega = 12.5
        zeta = 0.52
        wd = omega * math.sqrt(1.0 - zeta * zeta)
        raw = 1.0 - math.exp(-zeta * omega * t) * (
            math.cos(wd * t) + (zeta * omega / wd) * math.sin(wd * t)
        )
        end = 1.0 - math.exp(-zeta * omega) * (
            math.cos(wd) + (zeta * omega / wd) * math.sin(wd)
        )
        return raw / max(1e-6, end)
    # Default: "spring_smooth" — critically damped spring (zeta = 1.0)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    omega = 7.5
    raw = 1.0 - math.exp(-omega * t) * (1.0 + omega * t)
    end = 1.0 - math.exp(-omega) * (1.0 + omega)
    return raw / max(1e-6, end)


@dataclass
class DampedSpring:
    """Exact closed-form critically/under-damped spring — unconditionally stable at any dt."""

    value: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    omega: float = 18.0   # angular frequency (rad/s)
    zeta: float = 0.85    # damping ratio (<1 elastic, =1 critical, >1 overdamped)

    def step(self, dt: float) -> float:
        dt = max(0.0, min(0.05, float(dt)))
        if dt <= 0.0:
            return self.value
        x0 = self.value - self.target
        v0 = self.velocity
        w = max(0.1, self.omega)
        z = max(0.05, self.zeta)

        if abs(z - 1.0) < 1e-4:
            # Critically damped closed form
            exp_t = math.exp(-w * dt)
            c1 = x0
            c2 = v0 + w * x0
            x = (c1 + c2 * dt) * exp_t
            v = (c2 - w * (c1 + c2 * dt)) * exp_t
        elif z < 1.0:
            # Under-damped closed form
            wd = w * math.sqrt(1.0 - z * z)
            exp_t = math.exp(-z * w * dt)
            c1 = x0
            c2 = (v0 + z * w * x0) / wd
            cos_t = math.cos(wd * dt)
            sin_t = math.sin(wd * dt)
            x = exp_t * (c1 * cos_t + c2 * sin_t)
            v = exp_t * (
                (-z * w * c1 + wd * c2) * cos_t + (-z * w * c2 - wd * c1) * sin_t
            )
        else:
            # Over-damped closed form
            s = w * math.sqrt(z * z - 1.0)
            r1 = -w * z + s
            r2 = -w * z - s
            c2 = (v0 - r1 * x0) / (r2 - r1)
            c1 = x0 - c2
            e1 = math.exp(r1 * dt)
            e2 = math.exp(r2 * dt)
            x = c1 * e1 + c2 * e2
            v = c1 * r1 * e1 + c2 * r2 * e2

        self.value = self.target + x
        self.velocity = v
        return self.value

    def is_settled(self, eps: float = 0.002) -> bool:
        return abs(self.value - self.target) < eps and abs(self.velocity) < eps * 5.0


# ---------------------------------------------------------------------------
# Compiz-Style Wobbly Window 2D Spring Mesh Solver
# ---------------------------------------------------------------------------
class WobblyMesh:
    """Real-time deformable Bezier/spring corner & interior displacement field.

    Computes 4×4 control-point offsets (16 vec2 displacements) in normalized window
    space [-1, 1] suitable for ultra-fast uniform upload to GPU Bicubic/Bilinear vertex shader.
    """

    GRID = 4  # 4×4 control lattice = 16 control points (smooth cubic interpolation on GPU)

    def __init__(self, stiffness: float = 24.0, damping: float = 0.58, amplitude: float = 1.0) -> None:
        self.stiffness = stiffness
        self.damping = damping
        self.amplitude = amplitude
        self.grab_u = 0.5
        self.grab_v = 0.08  # titlebar grab default
        # Per-control-point lag springs (dx, dy)
        self._sx: list[DampedSpring] = []
        self._sy: list[DampedSpring] = []
        self._reset_springs()

    def _reset_springs(self) -> None:
        self._sx.clear()
        self._sy.clear()
        for j in range(self.GRID):
            for i in range(self.GRID):
                u = i / (self.GRID - 1)
                v = j / (self.GRID - 1)
                dist = math.hypot(u - self.grab_u, v - self.grab_v)
                # Farther corners have lower angular frequency -> natural jelly lag!
                omega = max(7.0, self.stiffness * (1.25 - 0.52 * dist))
                zeta = max(0.32, min(0.95, self.damping))
                self._sx.append(DampedSpring(0.0, 0.0, 0.0, omega=omega, zeta=zeta))
                self._sy.append(DampedSpring(0.0, 0.0, 0.0, omega=omega, zeta=zeta))

    def set_grab_point(self, u: float, v: float) -> None:
        self.grab_u = max(0.0, min(1.0, u))
        self.grab_v = max(0.0, min(1.0, v))
        self._reset_springs()

    def configure(self, stiffness: float, damping: float, amplitude: float) -> None:
        self.stiffness = max(8.0, min(50.0, stiffness))
        self.damping = max(0.28, min(0.95, damping))
        self.amplitude = max(0.0, min(2.5, amplitude))
        for j in range(self.GRID):
            for i in range(self.GRID):
                idx = j * self.GRID + i
                u = i / (self.GRID - 1)
                v = j / (self.GRID - 1)
                dist = math.hypot(u - self.grab_u, v - self.grab_v)
                self._sx[idx].omega = max(7.0, self.stiffness * (1.25 - 0.52 * dist))
                self._sx[idx].zeta = self.damping
                self._sy[idx].omega = self._sx[idx].omega
                self._sy[idx].zeta = self.damping

    def apply_drag_delta(self, dx_norm: float, dy_norm: float) -> None:
        """Feed normalized window movement delta (dx / win_w, dy / win_h)."""
        if self.amplitude <= 0.0:
            return
        for j in range(self.GRID):
            for i in range(self.GRID):
                idx = j * self.GRID + i
                u = i / (self.GRID - 1)
                v = j / (self.GRID - 1)
                dist = math.hypot(u - self.grab_u, v - self.grab_v)
                # Lag opposite to motion proportional to distance from grab point
                weight = min(1.35, dist * 1.15) * self.amplitude
                self._sx[idx].value = max(-0.45, min(0.45, self._sx[idx].value - dx_norm * weight))
                self._sy[idx].value = max(-0.45, min(0.45, self._sy[idx].value - dy_norm * weight))

    def step(self, dt: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for idx in range(self.GRID * self.GRID):
            out.append((self._sx[idx].step(dt), self._sy[idx].step(dt)))
        return out

    def is_settled(self) -> bool:
        return all(s.is_settled(0.0015) for s in self._sx) and all(
            s.is_settled(0.0015) for s in self._sy
        )

    def flat_offsets(self) -> list[float]:
        """Return 32 floats [dx0, dy0, dx1, dy1, ...] for GLSL uniform vec2 u_wobble[16]."""
        res: list[float] = []
        for idx in range(self.GRID * self.GRID):
            res.append(self._sx[idx].value)
            res.append(self._sy[idx].value)
        return res


# ---------------------------------------------------------------------------
# Momentum / Inertia Window Glide + Edge Bounce / Clamp
# ---------------------------------------------------------------------------
@dataclass
class MomentumBody:
    hwnd: int
    x: float
    y: float
    w: int
    h: int
    vx: float  # px/s
    vy: float  # px/s
    bounds_left: int
    bounds_top: int
    bounds_right: int
    bounds_bottom: int
    friction: float = 6.2
    bounce: bool = True
    restitution: float = 0.42
    strict_clamp: bool = False

    def step(self, dt: float) -> tuple[int, int, bool]:
        """Advance physics by dt seconds. Returns (new_x, new_y, still_moving)."""
        dt = max(0.0, min(0.05, dt))
        decay = math.exp(-self.friction * dt)
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vx *= decay
        self.vy *= decay

        min_x = float(self.bounds_left)
        min_y = float(self.bounds_top)
        max_x = float(max(self.bounds_left, self.bounds_right - self.w))
        max_y = float(max(self.bounds_top, self.bounds_bottom - self.h))

        if self.strict_clamp or self.bounce:
            if self.x < min_x:
                self.x = min_x
                self.vx = abs(self.vx) * self.restitution if (self.bounce and not self.strict_clamp) else 0.0
            elif self.x > max_x:
                self.x = max_x
                self.vx = -abs(self.vx) * self.restitution if (self.bounce and not self.strict_clamp) else 0.0

            if self.y < min_y:
                self.y = min_y
                self.vy = abs(self.vy) * self.restitution if (self.bounce and not self.strict_clamp) else 0.0
            elif self.y > max_y:
                self.y = max_y
                self.vy = -abs(self.vy) * self.restitution if (self.bounce and not self.strict_clamp) else 0.0

        speed = math.hypot(self.vx, self.vy)
        return int(round(self.x)), int(round(self.y)), speed > 28.0


# ---------------------------------------------------------------------------
# Window Flick Gesture Detector & Smart Edge/Corner Snap Layouts
# ---------------------------------------------------------------------------
@dataclass
class DragSample:
    t: float
    x: int
    y: int


class DragVelocityTracker:
    """Rolling-window velocity estimator for Momentum Drag & Window Flicks."""

    def __init__(self, window_sec: float = 0.09) -> None:
        self.window_sec = window_sec
        self._samples: list[DragSample] = []

    def reset(self, t: float, x: int, y: int) -> None:
        self._samples = [DragSample(t, x, y)]

    def push(self, t: float, x: int, y: int) -> None:
        self._samples.append(DragSample(t, x, y))
        cutoff = t - self.window_sec
        while len(self._samples) > 2 and self._samples[0].t < cutoff:
            self._samples.pop(0)

    def velocity(self, now: Optional[float] = None) -> tuple[float, float]:
        if len(self._samples) < 2:
            return (0.0, 0.0)
        a = self._samples[0]
        b = self._samples[-1]
        if now is not None and (now - b.t) > self.window_sec:
            return (0.0, 0.0)
        dt = b.t - a.t
        if dt < 1e-4:
            return (0.0, 0.0)
        return ((b.x - a.x) / dt, (b.y - a.y) / dt)

    def detect_flick(self, threshold_px_s: float = 1450.0, now: Optional[float] = None) -> Optional[str]:
        """Detect directional flick gesture: 'left', 'right', 'up', 'down', or None."""
        vx, vy = self.velocity(now=now)
        speed = math.hypot(vx, vy)
        if speed < threshold_px_s:
            return None
        if abs(vx) >= abs(vy) * 1.25:
            return "right" if vx > 0 else "left"
        if abs(vy) >= abs(vx) * 1.25:
            return "down" if vy > 0 else "up"
        return None


SNAP_PRESETS: dict[str, tuple[str, str]] = {
    "left_50": ("Left 50%", "ซ้าย 50%"),
    "right_50": ("Right 50%", "ขวา 50%"),
    "left_66": ("Left 66%", "ซ้าย 66%"),
    "right_33": ("Right 33%", "ขวา 33%"),
    "left_75": ("Left 75%", "ซ้าย 75%"),
    "right_25": ("Right 25%", "ขวา 25%"),
    "center_50": ("Center 50%", "กลาง 50%"),
    "center_66": ("Center 66%", "กลาง 66%"),
    "top_50": ("Top Half", "ครึ่งบน"),
    "bottom_50": ("Bottom Half", "ครึ่งล่าง"),
    "tl_corner": ("Top-Left 25%", "มุมซ้ายบน"),
    "tr_corner": ("Top-Right 25%", "มุมขวาบน"),
    "bl_corner": ("Bottom-Left 25%", "มุมซ้ายล่าง"),
    "br_corner": ("Bottom-Right 25%", "มุมขวาล่าง"),
    "maximize": ("Maximize", "เต็มจอ"),
    "minimize": ("Minimize", "ย่อเก็บ"),
}


def compute_snap_rect(monitor: MonitorRect, zone: str) -> Optional[tuple[int, int, int, int]]:
    """Compute (x, y, w, h) inside monitor work area for any snap zone."""
    wl, wt, ww, wh = monitor.work_left, monitor.work_top, monitor.work_width, monitor.work_height
    table: dict[str, tuple[float, float, float, float]] = {
        "left_50": (0.0, 0.0, 0.5, 1.0),
        "right_50": (0.5, 0.0, 0.5, 1.0),
        "left_66": (0.0, 0.0, 2.0 / 3.0, 1.0),
        "right_33": (2.0 / 3.0, 0.0, 1.0 / 3.0, 1.0),
        "left_33": (0.0, 0.0, 1.0 / 3.0, 1.0),
        "right_66": (1.0 / 3.0, 0.0, 2.0 / 3.0, 1.0),
        "left_75": (0.0, 0.0, 0.75, 1.0),
        "right_25": (0.75, 0.0, 0.25, 1.0),
        "left_25": (0.0, 0.0, 0.25, 1.0),
        "right_75": (0.25, 0.0, 0.75, 1.0),
        "center_50": (0.25, 0.0, 0.5, 1.0),
        "center_66": (1.0 / 6.0, 0.0, 2.0 / 3.0, 1.0),
        "center_33": (1.0 / 3.0, 0.0, 1.0 / 3.0, 1.0),
        "top_50": (0.0, 0.0, 1.0, 0.5),
        "bottom_50": (0.0, 0.5, 1.0, 0.5),
        "tl_corner": (0.0, 0.0, 0.5, 0.5),
        "tr_corner": (0.5, 0.0, 0.5, 0.5),
        "bl_corner": (0.0, 0.5, 0.5, 0.5),
        "br_corner": (0.5, 0.5, 0.5, 0.5),
        "maximize": (0.0, 0.0, 1.0, 1.0),
    }
    if zone not in table:
        return None
    fx, fy, fw, fh = table[zone]
    return (
        int(round(wl + fx * ww)),
        int(round(wt + fy * wh)),
        max(160, int(round(fw * ww))),
        max(120, int(round(fh * wh))),
    )


def detect_edge_snap_zone(
    cursor_x: int,
    cursor_y: int,
    monitor: MonitorRect,
    edge_px: int = 12,
    corner_px: int = 64,
    left_zone: str = "left_50",
    right_zone: str = "right_50",
    top_zone: str = "maximize",
    bottom_zone: str = "minimize",
) -> Optional[str]:
    """Detect whether a cursor drop hit a monitor edge or corner snap target."""
    at_left = cursor_x <= monitor.work_left + edge_px
    at_right = cursor_x >= monitor.work_right - edge_px
    at_top = cursor_y <= monitor.work_top + edge_px
    at_bottom = cursor_y >= monitor.work_bottom - edge_px

    near_top = cursor_y <= monitor.work_top + corner_px
    near_bottom = cursor_y >= monitor.work_bottom - corner_px
    near_left = cursor_x <= monitor.work_left + corner_px
    near_right = cursor_x >= monitor.work_right - corner_px

    if (at_left and near_top) or (at_top and near_left):
        return "tl_corner"
    if (at_right and near_top) or (at_top and near_right):
        return "tr_corner"
    if (at_left and near_bottom) or (at_bottom and near_left):
        return "bl_corner"
    if (at_right and near_bottom) or (at_bottom and near_right):
        return "br_corner"
    if at_left:
        return left_zone
    if at_right:
        return right_zone
    if at_top:
        return top_zone
    if at_bottom:
        return bottom_zone
    return None
