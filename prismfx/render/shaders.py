"""GLSL 330 Core GPU Vertex + Fragment Shaders for all 42 PrismFX effects + Wobbly Physics.

Designed for 165 Hz (<1.5 ms GPU cost) with zero external shader compiler dependencies.
"""

from __future__ import annotations

import math
import pathlib
import re
from typing import Optional

VERTEX_SHADER_330 = r"""#version 330 core
layout(location = 0) in vec2 a_uv;  // [0..1, 0..1] grid coordinates (top-left = 0,0)

uniform int   u_effect;        // 1..42 (or >=1000 custom)
uniform float u_progress;      // 0.0 = fully visible, 1.0 = fully gone (already eased & direction-applied)
uniform float u_raw_t;         // un-eased linear [0..1]
uniform float u_intensity;     // effect amplitude multiplier
uniform vec2  u_anchor;        // normalized anchor (e.g. taskbar / cursor) in [0..1]
uniform vec4  u_rect;          // window rect in NDC-mapped viewport pixels: (x, y, w, h)
uniform vec2  u_viewport;      // overlay viewport size (vw, vh)
uniform int   u_wobble_active; // 1 when live spring-mesh wobble is active
uniform vec2  u_wobble[16];    // 4x4 control lattice displacements

out vec2  v_uv;
out float v_shade;
out float v_alpha_mul;

const float PI = 3.14159265359;

vec2 sampleWobble(vec2 uv) {
    vec2 g = clamp(uv, 0.0, 1.0) * 3.0;
    ivec2 i0 = ivec2(floor(g));
    ivec2 i1 = min(i0 + ivec2(1), ivec2(3));
    vec2 f = fract(g);
    // Smoothstep blend across 4x4 lattice for silk-smooth Compiz curvature
    f = f * f * (3.0 - 2.0 * f);
    vec2 c00 = u_wobble[i0.y * 4 + i0.x];
    vec2 c10 = u_wobble[i0.y * 4 + i1.x];
    vec2 c01 = u_wobble[i1.y * 4 + i0.x];
    vec2 c11 = u_wobble[i1.y * 4 + i1.x];
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

void main() {
    v_uv = a_uv;
    v_shade = 1.0;
    v_alpha_mul = 1.0;

    float p = clamp(u_progress, -0.35, 1.35);
    float pc = clamp(u_progress, 0.0, 1.0);
    float amp = max(0.2, u_intensity);

    // Local coordinates centered in [-1, 1] (y up)
    vec3 pos = vec3(a_uv.x * 2.0 - 1.0, 1.0 - a_uv.y * 2.0, 0.0);

    // Live Wobbly Window Drag mesh displacement
    if (u_wobble_active == 1) {
        vec2 wdisp = sampleWobble(a_uv) * 2.0 * amp;
        pos.x += wdisp.x;
        pos.y -= wdisp.y;
    }

    // --- Vertex / 3D Mesh Deformations by Effect ID ---
    if (u_effect == 1) {
        // Prism Glide: smooth scale + subtle Z depth lift
        float s = 1.0 - 0.14 * p * amp;
        pos.xy *= s;
        pos.y += 0.06 * p * amp;
    } else if (u_effect == 2) {
        // Win11 Silk Spring
        float s = 1.0 - 0.09 * p;
        pos.xy *= s;
        pos.y -= 0.05 * p;
    } else if (u_effect == 3) {
        // Magic Genie Lamp: cubic-Bezier suction into anchor (taskbar/cursor)
        float targetX = u_anchor.x * 2.0 - 1.0;
        float targetY = 1.0 - u_anchor.y * 2.0;
        float v = a_uv.y; // 0 top, 1 bottom
        float slide = clamp((pc * 1.45) - (1.0 - v) * 0.45, 0.0, 1.0);
        float pinch = smoothstep(0.0, 1.0, clamp(pc * 1.35 - (1.0 - v) * 0.55, 0.0, 1.0));
        float curve = sin(pinch * PI * 0.5);
        pos.x = mix(pos.x, targetX, curve * 0.92);
        pos.y = mix(pos.y, targetY, slide * slide);
        v_shade = 1.0 - 0.25 * curve * abs(a_uv.x - 0.5);
    } else if (u_effect == 5) {
        // Aero Levitation
        pos.y += 0.35 * p * amp;
        pos.xy *= (1.0 - 0.08 * p);
    } else if (u_effect == 6) {
        // Elastic Spring Pop
        float s = max(0.01, 1.0 - p);
        float squash = 1.0 + 0.12 * sin(pc * PI * 2.0) * (1.0 - pc) * amp;
        pos.x *= s * squash;
        pos.y *= s / squash;
    } else if (u_effect == 7) {
        // Origami Fold: 4 vertical panels folding like an accordion/paper crane
        float panels = 4.0;
        float panelIdx = floor(clamp(a_uv.x * panels, 0.0, panels - 0.001));
        float localX = fract(a_uv.x * panels);
        float angle = pc * PI * 0.48 * amp;
        float dir = mod(panelIdx, 2.0) < 0.5 ? 1.0 : -1.0;
        float foldX = (panelIdx + localX) / panels;
        float compressed = mix(pos.x, (foldX * 2.0 - 1.0) * cos(angle), 1.0);
        float z = sin(angle) * (dir > 0.0 ? localX : (1.0 - localX)) * 0.65;
        pos.x = compressed;
        pos.z = z;
        pos.y *= (1.0 - 0.18 * pc);
        v_shade = clamp(1.0 - 0.45 * abs(sin(angle)) * (dir > 0.0 ? 0.3 : 1.0), 0.35, 1.15);
    } else if (u_effect == 8) {
        // Velvet Curtain Drape
        float wave = sin(a_uv.x * PI * 7.0 + pc * 4.0) * pc * 0.16 * amp;
        pos.x = mix(pos.x, -0.92, pc * 0.85 * (0.4 + 0.6 * a_uv.y));
        pos.z = wave;
        pos.y += wave * 0.35;
        v_shade = clamp(0.85 + 0.3 * cos(a_uv.x * PI * 7.0 + pc * 4.0), 0.45, 1.15);
    } else if (u_effect == 10) {
        // Gravity Drop + Squash
        float fall = pc * pc * 1.45 * amp;
        pos.y -= fall;
        float stretch = 1.0 + 0.25 * pc * amp;
        pos.y *= stretch;
        pos.x /= sqrt(stretch);
    } else if (u_effect == 29) {
        // Architectural Doors 3D: left & right halves swing open on outer hinges
        float angle = pc * PI * 0.52 * amp;
        if (a_uv.x < 0.5) {
            float d = a_uv.x * 2.0; // distance from left hinge [0..1]
            pos.x = -1.0 + d * cos(angle);
            pos.z = d * sin(angle) * 0.85;
            v_shade = 1.0 - 0.45 * pc * d;
        } else {
            float d = (1.0 - a_uv.x) * 2.0; // distance from right hinge [0..1]
            pos.x = 1.0 - d * cos(angle);
            pos.z = d * sin(angle) * 0.85;
            v_shade = 1.0 - 0.45 * pc * d;
        }
    } else if (u_effect == 30) {
        // Venetian Blinds 3D: 8 horizontal louvers rotating around center line
        float slats = 8.0;
        float slatY = fract(a_uv.y * slats) - 0.5;
        float slatCenter = (floor(a_uv.y * slats) + 0.5) / slats;
        float stagger = clamp((pc * 1.35) - slatCenter * 0.35, 0.0, 1.0);
        float angle = stagger * PI * 0.5 * amp;
        float newLocal = slatY * cos(angle) / slats;
        pos.y = 1.0 - (slatCenter + newLocal) * 2.0;
        pos.z = slatY * sin(angle) * 0.35;
        v_shade = clamp(cos(angle), 0.2, 1.0);
        v_alpha_mul = 1.0 - smoothstep(0.82, 1.0, stagger);
    } else if (u_effect == 31) {
        // Isometric 3D Card Flip around Y-axis
        float angle = pc * PI * 0.55 * amp;
        float x0 = pos.x;
        pos.x = x0 * cos(angle);
        pos.z = x0 * sin(angle) * 0.9;
        v_shade = clamp(0.45 + 0.55 * cos(angle), 0.25, 1.0);
    } else if (u_effect == 32) {
        // Accordion Pleat: 6 zig-zag folds compressing to left
        float folds = 6.0;
        float idx = floor(clamp(a_uv.x * folds, 0.0, folds - 0.001));
        float lx = fract(a_uv.x * folds);
        float angle = pc * PI * 0.49;
        float zig = mod(idx, 2.0) < 0.5 ? lx : (1.0 - lx);
        pos.x = -1.0 + (a_uv.x * 2.0) * cos(angle);
        pos.z = zig * sin(angle) * 0.45;
        v_shade = clamp(0.55 + 0.45 * (mod(idx, 2.0) < 0.5 ? 1.0 : cos(angle)), 0.3, 1.05);
    } else if (u_effect == 33) {
        // Shatter Glass 3D: coarse grid shards explode & tumble with gravity
        vec2 cell = floor(a_uv * vec2(8.0, 6.0));
        float rnd1 = hash21(cell + 1.3);
        float rnd2 = hash21(cell + 7.7);
        float rnd3 = hash21(cell + 19.1);
        float delay = rnd1 * 0.28;
        float st = clamp((pc - delay) / (1.0 - delay), 0.0, 1.0);
        vec2 cellCenter = (cell + 0.5) / vec2(8.0, 6.0);
        vec2 cNdc = vec2(cellCenter.x * 2.0 - 1.0, 1.0 - cellCenter.y * 2.0);
        vec2 rel = pos.xy - cNdc;
        float rot = st * (rnd2 - 0.5) * 6.5 * amp;
        float cs = cos(rot), sn = sin(rot);
        rel = mat2(cs, -sn, sn, cs) * rel * (1.0 - 0.45 * st);
        vec2 blastRaw = cNdc + vec2(0.001, 0.05);
        float blastLen = length(blastRaw);
        vec2 blastDir = (blastLen > 1e-5) ? (blastRaw / blastLen) : vec2(0.0, 1.0);
        vec2 blast = blastDir * (0.25 + rnd3 * 0.65) * st * amp;
        pos.xy = cNdc + rel + blast;
        pos.y -= st * st * 1.15 * amp;
        pos.z = (rnd1 - 0.5) * st * 0.9;
        v_shade = 0.8 + 0.35 * abs(cs);
        v_alpha_mul = 1.0 - smoothstep(0.65, 1.0, st);
    } else if (u_effect == 34) {
        // Helix Ribbon Twist
        float twist = (pc * 2.2 + a_uv.y * pc * 2.5) * PI * 0.5 * amp;
        float x0 = pos.x;
        pos.x = x0 * cos(twist);
        pos.z = x0 * sin(twist) * 0.65;
        pos.xy *= (1.0 - 0.35 * pc);
        v_shade = clamp(0.45 + 0.55 * abs(cos(twist)), 0.3, 1.05);
    } else if (u_effect == 35) {
        // 3D Page Curl from bottom-right corner
        float diag = (1.0 - a_uv.x) * 0.55 + (1.0 - a_uv.y) * 0.45;
        float curlLine = 1.0 - pc * 1.45;
        float d = (1.0 - diag) - curlLine;
        if (d > 0.0) {
            float radius = 0.22;
            float theta = min(d / radius, PI);
            float shift = d - sin(theta) * radius;
            pos.x -= shift * 1.1;
            pos.y += shift * 0.9;
            pos.z = (1.0 - cos(theta)) * radius * 1.8;
            v_shade = clamp(0.55 + 0.45 * cos(theta), 0.35, 1.15);
        }
    } else if (u_effect == 36) {
        // Domino Tile Cascade (6x4 grid flipping diagonally)
        vec2 grid = vec2(6.0, 4.0);
        vec2 cell = floor(clamp(a_uv * grid, vec2(0.0), grid - 0.001));
        vec2 loc = fract(a_uv * grid) - 0.5;
        float order = (cell.x / grid.x + cell.y / grid.y) * 0.5;
        float st = clamp((pc * 1.65) - order * 0.65, 0.0, 1.0);
        float angle = st * PI * 0.5;
        vec2 cPos = (cell + 0.5) / grid;
        vec2 cNdc = vec2(cPos.x * 2.0 - 1.0, 1.0 - cPos.y * 2.0);
        pos.x = cNdc.x + (loc.x * 2.0 / grid.x) * cos(angle);
        pos.z = (loc.x * 2.0 / grid.x) * sin(angle) * 0.9;
        v_shade = clamp(0.35 + 0.65 * cos(angle), 0.2, 1.0);
        v_alpha_mul = 1.0 - smoothstep(0.78, 1.0, st);
    } else if (u_effect == 38) {
        // Directional Slide
        pos.x += pc * 1.25 * amp;
    } else if (u_effect == 40) {
        // Corner Anchor Zoom
        vec2 anc = vec2(u_anchor.x * 2.0 - 1.0, 1.0 - u_anchor.y * 2.0);
        pos.xy = mix(pos.xy, anc, pc);
    } else if (u_effect == 41) {
        // Orbital Spin
        float a = pc * PI * 1.35 * amp;
        float cs = cos(a), sn = sin(a);
        pos.xy = mat2(cs, -sn, sn, cs) * pos.xy * max(0.01, 1.0 - pc);
    } else if (u_effect == 42) {
        // Jelly Wobble Pop
        float s = max(0.01, 1.0 - p * 0.85);
        float wobX = sin(a_uv.y * PI * 2.0 + pc * 10.0) * 0.14 * sin(pc * PI) * amp;
        float wobY = cos(a_uv.x * PI * 2.0 + pc * 8.0) * 0.08 * sin(pc * PI) * amp;
        pos.xy = pos.xy * s + vec2(wobX, wobY);
    }

    // True 3D perspective divide
    float camZ = 2.65;
    float persp = camZ / max(0.45, camZ - pos.z);
    vec2 projected = pos.xy * persp;

    // Map local projected [-1, 1] into pixel viewport rect -> final OpenGL NDC [-1, 1]
    vec2 local01 = vec2(projected.x * 0.5 + 0.5, 0.5 - projected.y * 0.5);
    vec2 px = u_rect.xy + local01 * u_rect.zw;
    vec2 ndc = vec2(
        (px.x / max(1.0, u_viewport.x)) * 2.0 - 1.0,
        1.0 - (px.y / max(1.0, u_viewport.y)) * 2.0
    );
    gl_Position = vec4(ndc, 0.0, 1.0);
}
"""

FRAGMENT_SHADER_330 = r"""#version 330 core
in vec2  v_uv;
in float v_shade;
in float v_alpha_mul;

uniform sampler2D u_tex;
uniform int   u_effect;
uniform float u_progress;    // 0 = fully visible, 1 = gone
uniform float u_raw_t;
uniform float u_time;
uniform float u_intensity;
uniform vec3  u_accent;      // accent RGB in [0..1]
uniform vec2  u_anchor;      // normalized anchor [0..1]
uniform vec2  u_win_size;    // window pixel dimensions

out vec4 fragColor;

const float PI = 3.14159265359;

// --- Fast Procedural Noise & Distance Fields ---
float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

vec2 hash22(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

float valueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash12(i);
    float b = hash12(i + vec2(1.0, 0.0));
    float c = hash12(i + vec2(0.0, 1.0));
    float d = hash12(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float fbm(vec2 p) {
    float v = 0.0;
    float a = 0.5;
    mat2 m = mat2(1.6, 1.2, -1.2, 1.6);
    for (int i = 0; i < 4; ++i) {
        v += a * valueNoise(p);
        p = m * p;
        a *= 0.5;
    }
    return v;
}

vec2 voronoi(vec2 x) {
    vec2 n = floor(x);
    vec2 f = fract(x);
    float md = 8.0;
    float id = 0.0;
    for (int j = -1; j <= 1; ++j) {
        for (int i = -1; i <= 1; ++i) {
            vec2 g = vec2(float(i), float(j));
            vec2 o = hash22(n + g);
            vec2 r = g + o - f;
            float d = dot(r, r);
            if (d < md) {
                md = d;
                id = hash12(n + g);
            }
        }
    }
    return vec2(sqrt(max(md, 0.0)), id);
}

float hexDist(vec2 p) {
    p = abs(p);
    float c = dot(p, normalize(vec2(1.0, 1.7320508)));
    return max(c, p.x);
}

vec4 hexCoords(vec2 uv) {
    vec2 r = vec2(1.0, 1.7320508);
    vec2 h = r * 0.5;
    vec2 a = mod(uv, r) - h;
    vec2 b = mod(uv - h, r) - h;
    vec2 gv = dot(a, a) < dot(b, b) ? a : b;
    float edge = 0.5 - hexDist(gv);
    vec2 id = uv - gv;
    return vec4(edge, id.x, id.y, hash12(id));
}

vec4 sampleWin(vec2 uv) {
    if (!(uv.x >= 0.0 && uv.x <= 1.0 && uv.y >= 0.0 && uv.y <= 1.0)) {
        return vec4(0.0);
    }
    return textureLod(u_tex, uv, 0.0);
}

void main() {
    float p = clamp(u_progress, 0.0, 1.0);
    float amp = max(0.2, u_intensity);
    vec2 uv = v_uv;
    vec4 col = sampleWin(uv);
    float alpha = col.a * v_alpha_mul;

    // 1: Prism Glide — crisp chromatic edge refraction
    if (u_effect == 1) {
        float ca = p * 0.012 * amp;
        vec2 dir = (uv - 0.5);
        col.r = sampleWin(uv + dir * ca).r;
        col.b = sampleWin(uv - dir * ca).b;
        float rim = smoothstep(0.35, 0.5, max(abs(uv.x - 0.5), abs(uv.y - 0.5)));
        col.rgb += u_accent * rim * p * 0.35;
        alpha *= (1.0 - smoothstep(0.15, 1.0, p));
    }
    // 2: Win11 Silk Spring
    else if (u_effect == 2) {
        alpha *= (1.0 - p);
    }
    // 3: Magic Genie Lamp
    else if (u_effect == 3) {
        alpha *= (1.0 - smoothstep(0.65, 1.0, p));
    }
    // 4: Liquid Ripple Refraction
    else if (u_effect == 4) {
        vec2 d = uv - 0.5;
        float dist = length(d);
        float wave = sin(dist * 34.0 - p * 14.0) * exp(-dist * 2.5) * p * 0.055 * amp;
        vec2 dir = (dist > 1e-5) ? (d / dist) : vec2(0.0);
        vec2 ruv = uv + dir * wave;
        col = sampleWin(ruv);
        col.rgb += u_accent * abs(wave) * 8.0;
        alpha = col.a * (1.0 - smoothstep(0.3, 1.0, p));
    }
    // 5: Aero Levitation (vertical directional blur)
    else if (u_effect == 5) {
        vec4 acc = vec4(0.0);
        float blur = p * 0.025 * amp;
        for (int i = -2; i <= 2; ++i) {
            acc += sampleWin(uv + vec2(0.0, float(i) * blur));
        }
        col = acc / 5.0;
        alpha = col.a * (1.0 - p);
    }
    // 6: Elastic Pop
    else if (u_effect == 6) {
        alpha *= (1.0 - smoothstep(0.55, 1.0, p));
    }
    // 7, 8, 10: Origami, Curtain, Gravity
    else if (u_effect == 7 || u_effect == 8 || u_effect == 10) {
        alpha *= (1.0 - smoothstep(0.55, 1.0, p));
    }
    // 9: Hyper Zoom Blur
    else if (u_effect == 9) {
        vec2 dir = uv - 0.5;
        vec4 acc = vec4(0.0);
        float strength = p * 0.12 * amp;
        for (int i = 0; i < 8; ++i) {
            float k = float(i) / 7.0;
            acc += sampleWin(0.5 + dir * (1.0 - k * strength));
        }
        col = acc / 8.0;
        col.rgb += u_accent * p * 0.18;
        alpha = col.a * (1.0 - p);
    }
    // 11: Incinerate Burn (Procedural Simplex FBM Fire Edge!)
    else if (u_effect == 11) {
        float n = fbm(uv * vec2(7.0, 5.5) + vec2(0.0, p * 1.4));
        float front = (1.0 - uv.y) * 0.45 + n * 0.65;
        float thresh = p * 1.25;
        float edge = front - thresh;
        if (edge < 0.0) {
            // Glowing drifting ash embers behind burn front
            float spark = step(0.965, hash12(floor((uv + vec2(0.0, p * 0.4)) * 90.0)));
            float fade = smoothstep(-0.22, 0.0, edge);
            col.rgb = vec3(1.0, 0.55, 0.12) * 1.8;
            alpha = spark * fade * (1.0 - p);
        } else {
            float charBand = smoothstep(0.0, 0.11, edge);
            float fireBand = 1.0 - smoothstep(0.0, 0.08, edge);
            col.rgb = mix(vec3(0.07, 0.03, 0.02), col.rgb, charBand);
            vec3 fireCol = mix(vec3(1.0, 0.25, 0.02), vec3(1.0, 0.92, 0.45), 1.0 - smoothstep(0.005, 0.06, edge));
            col.rgb += fireCol * fireBand * 2.1 * amp;
        }
    }
    // 12: Plasma Vaporize
    else if (u_effect == 12) {
        float n1 = fbm(uv * 9.0 + vec2(u_time * 0.8, -p * 2.0));
        float n2 = fbm(uv * 16.0 - vec2(p * 3.0, u_time));
        float field = mix(n1, n2, 0.45) + length(uv - 0.5) * 0.35;
        float edge = field - p * 1.32;
        if (edge < 0.0) {
            alpha = 0.0;
        } else {
            float arc = 1.0 - smoothstep(0.0, 0.09, edge);
            vec3 plasma = mix(vec3(0.55, 0.15, 1.0), vec3(0.15, 0.95, 1.0), n2);
            col.rgb = mix(col.rgb, plasma * 1.9, arc);
            col.rgb += plasma * arc * 0.9 * amp;
        }
    }
    // 13: Cryo Frost Shatter (Voronoi crystalline ice)
    else if (u_effect == 13) {
        vec2 vor = voronoi(uv * vec2(10.0, 7.0));
        float shardDelay = vor.y * 0.65;
        float st = clamp((p - shardDelay * 0.55) / (1.0 - shardDelay * 0.55), 0.0, 1.0);
        vec2 drift = (hash22(vec2(vor.y * 17.0, 3.1)) - 0.5) * st * 0.18 * amp;
        col = sampleWin(uv + drift);
        float crack = 1.0 - smoothstep(0.0, 0.08, abs(vor.x - 0.45));
        float frost = smoothstep(0.0, 0.4, p);
        col.rgb = mix(col.rgb, vec3(0.68, 0.92, 1.0), frost * 0.32 + crack * 0.55 * frost);
        alpha = col.a * (1.0 - smoothstep(0.4, 1.0, st));
    }
    // 14: Wind Dust Snap (Thanos-style erosion)
    else if (u_effect == 14) {
        float n = fbm(uv * 18.0);
        float wave = (1.0 - uv.x) * 0.55 + n * 0.5;
        float st = clamp((p * 1.35 - wave) * 2.2, 0.0, 1.0);
        vec2 windUv = uv - vec2(st * 0.22 * amp, -st * st * 0.12 * amp + (n - 0.5) * st * 0.09);
        col = sampleWin(windUv);
        float grain = hash12(floor(uv * 180.0));
        float mask = step(st * 0.95, grain);
        col.rgb = mix(col.rgb, vec3(0.88, 0.72, 0.48), st * 0.55);
        alpha = col.a * mask * (1.0 - smoothstep(0.65, 1.0, st));
    }
    // 15: Event Horizon Black Hole Vortex
    else if (u_effect == 15) {
        vec2 d = uv - u_anchor;
        float r = length(d);
        float angle = (r > 1e-5) ? atan(d.y, d.x) : 0.0;
        float pull = p * p * 1.35 * amp;
        float swirl = (3.8 * p * amp) / (r + 0.18);
        float newR = r * (1.0 + pull * 2.4);
        vec2 suv = u_anchor + vec2(cos(angle + swirl), sin(angle + swirl)) * newR;
        col = sampleWin(suv);
        float dRing = (r - (0.35 * (1.0 - p) + 0.03)) * 18.0;
        float ring = exp(-(dRing * dRing)) * sin(p * PI);
        col.rgb += mix(u_accent, vec3(1.0, 0.75, 0.35), 0.45) * ring * 1.6;
        alpha = max(col.a * (1.0 - smoothstep(0.6, 1.0, p)), ring * 0.85);
    }
    // 16: Quantum Rift Portal
    else if (u_effect == 16) {
        float slitX = abs(uv.x - 0.5);
        float scaleX = max(0.01, 1.0 - p * 0.98);
        vec2 quv = vec2((uv.x - 0.5) / scaleX + 0.5, uv.y);
        col = sampleWin(quv);
        float glow = exp(-slitX * (18.0 + 30.0 * p)) * sin(p * PI);
        col.rgb += u_accent * glow * 2.2 * amp;
        alpha = max(col.a * (1.0 - smoothstep(0.7, 1.0, p)), glow * 0.9);
    }
    // 17: Laser Scan Vapor
    else if (u_effect == 17) {
        float beamY = p * 1.12 - 0.06;
        float d = uv.y - beamY;
        float laser = exp(-abs(d) * 55.0) * sin(p * PI);
        if (d < 0.0) {
            col.rgb = u_accent * 1.8;
            alpha = laser * 0.95;
        } else {
            col.rgb += mix(u_accent, vec3(1.0), 0.55) * laser * 2.2 * amp;
        }
    }
    // 18: Aurora Veil
    else if (u_effect == 18) {
        float ribbon = sin(uv.x * 8.0 + p * 5.0) * 0.14 + fbm(uv * 5.0 + p) * 0.45;
        float edge = (uv.y + ribbon) - p * 1.4;
        if (edge < 0.0) {
            alpha = 0.0;
        } else {
            float glow = 1.0 - smoothstep(0.0, 0.18, edge);
            vec3 aur = 0.5 + 0.5 * cos(vec3(0.0, 2.0, 4.0) + uv.x * 5.0 + p * 4.0);
            col.rgb = mix(col.rgb, aur * 1.5, glow * 0.85);
        }
    }
    // 19: Shockwave Nova
    else if (u_effect == 19) {
        vec2 d = uv - 0.5;
        float r = length(d);
        float ringR = p * 0.85;
        float dBand = (r - ringR) * 18.0;
        float band = exp(-(dBand * dBand));
        vec2 dir = (r > 1e-5) ? (d / r) : vec2(0.0);
        col.r = sampleWin(uv - dir * band * 0.06 * amp).r;
        col.g = sampleWin(uv - dir * band * 0.04 * amp).g;
        col.b = sampleWin(uv - dir * band * 0.02 * amp).b;
        col.rgb += u_accent * band * 1.3 * (1.0 - p * 0.4);
        alpha = col.a * (1.0 - smoothstep(ringR - 0.06, ringR + 0.06, 0.0) * 0.0) * smoothstep(ringR - 0.04, ringR + 0.08, r);
        alpha = max(alpha, band * (1.0 - p) * 0.8);
    }
    // 20: Phoenix Thermal Rise
    else if (u_effect == 20) {
        float n = fbm(uv * vec2(8.0, 12.0) + vec2(0.0, p * 4.5));
        vec2 heatUv = uv + vec2((n - 0.5) * 0.05 * p * amp, p * 0.12);
        col = sampleWin(heatUv);
        float front = uv.y + n * 0.35 - p * 1.25;
        if (front < 0.0) {
            alpha = 0.0;
        } else {
            float flame = 1.0 - smoothstep(0.0, 0.16, front);
            col.rgb += mix(vec3(1.0, 0.35, 0.05), vec3(1.0, 0.88, 0.35), flame) * flame * 1.5;
        }
    }
    // 21: Chromatic Glitch
    else if (u_effect == 21) {
        float slice = floor(uv.y * 26.0);
        float rnd = hash12(vec2(slice, floor(p * 18.0)));
        float shift = (rnd - 0.5) * 0.22 * p * amp * step(0.45, rnd);
        float rgbSep = 0.032 * p * amp;
        vec2 guv = vec2(uv.x + shift, uv.y);
        col.r = sampleWin(guv + vec2(rgbSep, 0.0)).r;
        col.g = sampleWin(guv).g;
        col.b = sampleWin(guv - vec2(rgbSep, 0.0)).b;
        float scan = 0.88 + 0.12 * sin(uv.y * 280.0);
        col.rgb *= scan;
        float dropout = step(p * 1.05, hash12(vec2(slice, 9.1)));
        alpha = col.a * dropout * (1.0 - p * 0.45);
    }
    // 22: Retro CRT Power-Off
    else if (u_effect == 22) {
        float squashY = smoothstep(0.0, 0.55, p);
        float squashX = smoothstep(0.48, 0.98, p);
        float sy = max(0.004, 1.0 - squashY * 0.992);
        float sx = max(0.006, 1.0 - squashX * 0.994);
        vec2 cuv = (uv - 0.5) / vec2(sx, sy) + 0.5;
        col = sampleWin(cuv);
        float flash = smoothstep(0.25, 0.72, p) * (1.0 - smoothstep(0.85, 1.0, p));
        col.rgb = mix(col.rgb, vec3(0.92, 0.97, 1.0) * 1.8, flash * 0.85);
        float scan = 0.85 + 0.15 * sin(uv.y * 320.0);
        col.rgb *= scan;
        float dotGlow = exp(-length((uv - 0.5) * vec2(12.0, 32.0)) * (1.0 + squashX * 3.0)) * flash;
        alpha = max(col.a * (1.0 - smoothstep(0.9, 1.0, p)), dotGlow);
    }
    // 23: Digital Rain Melt
    else if (u_effect == 23) {
        float colIdx = floor(uv.x * 48.0);
        float speed = 0.6 + hash12(vec2(colIdx, 2.3)) * 1.1;
        float drop = p * speed * 1.25;
        vec2 muv = vec2(uv.x, uv.y - drop);
        col = sampleWin(muv);
        float head = exp(-abs(uv.y - drop) * 28.0) * sin(p * PI);
        col.rgb += mix(u_accent, vec3(0.2, 1.0, 0.55), 0.65) * head * 1.9;
        alpha = max(col.a * (1.0 - smoothstep(0.75, 1.0, p)), head * 0.85);
    }
    // 24: Pixel Sort Drift
    else if (u_effect == 24) {
        float luma = dot(col.rgb, vec3(0.299, 0.587, 0.114));
        float streak = luma * p * 0.45 * amp;
        vec2 suv = vec2(uv.x, clamp(uv.y - streak, 0.0, 1.0));
        col = sampleWin(suv);
        col.rgb += u_accent * streak * 0.6;
        alpha = col.a * (1.0 - smoothstep(0.35, 1.0, p));
    }
    // 25: Mosaic Deconstruct
    else if (u_effect == 25) {
        if (p <= 1e-4) {
            col = sampleWin(uv);
            alpha = col.a * v_alpha_mul;
        } else {
            float blocks = mix(240.0, 10.0, pow(max(p, 1e-6), 0.7));
            vec2 grid = vec2(blocks, blocks * 0.65);
            vec2 cell = floor(uv * grid);
            vec2 muv = (cell + 0.5) / grid;
            col = sampleWin(muv);
            float rnd = hash12(cell);
            alpha = col.a * step(p * 1.02, rnd * 0.85 + 0.15);
        }
    }
    // 26: Hologram De-Sync
    else if (u_effect == 26) {
        float jitter = sin(uv.y * 90.0 + u_time * 35.0) * 0.014 * p * amp;
        col = sampleWin(uv + vec2(jitter, 0.0));
        float luma = dot(col.rgb, vec3(0.3, 0.6, 0.1));
        vec3 holo = mix(col.rgb, u_accent * (0.4 + luma * 1.3), p * 0.8);
        float lines = 0.65 + 0.35 * sin(uv.y * 240.0 + u_time * 18.0);
        float flicker = 0.75 + 0.25 * sin(u_time * 55.0);
        col.rgb = holo * lines * flicker;
        alpha = col.a * (1.0 - p);
    }
    // 27: VHS Tape Warp
    else if (u_effect == 27) {
        float dBand = (uv.y - fract(p * 1.5)) * 12.0;
        float band = exp(-(dBand * dBand));
        float skew = band * 0.09 * sin(uv.y * 45.0) * amp;
        col.r = sampleWin(uv + vec2(skew + 0.015 * p, 0.0)).r;
        col.g = sampleWin(uv + vec2(skew, 0.0)).g;
        col.b = sampleWin(uv + vec2(skew - 0.015 * p, 0.0)).b;
        float snow = hash12(uv * 300.0 + u_time * 10.0);
        col.rgb = mix(col.rgb, vec3(snow), p * 0.45 + band * 0.35);
        alpha = col.a * (1.0 - smoothstep(0.4, 1.0, p));
    }
    // 28: Hex Grid Dissolve
    else if (u_effect == 28) {
        vec4 hc = hexCoords(uv * vec2(14.0, 9.0));
        float sweep = (uv.x * 0.55 + uv.y * 0.45) * 0.65 + hc.w * 0.45;
        float edge = sweep - p * 1.15;
        if (edge < 0.0) {
            alpha = 0.0;
        } else {
            float cellRim = (1.0 - smoothstep(0.0, 0.08, hc.x)) * (1.0 - smoothstep(0.0, 0.28, edge));
            float frontGlow = 1.0 - smoothstep(0.0, 0.1, edge);
            col.rgb += u_accent * (cellRim * 1.6 + frontGlow * 1.2) * amp;
        }
    }
    // 29..36: 3D Architectural Effects (vertex-deformed + smooth alpha finish)
    else if (u_effect >= 29 && u_effect <= 36) {
        if (u_effect == 35) {
            float diag = (1.0 - uv.x) * 0.55 + (1.0 - uv.y) * 0.45;
            alpha *= smoothstep(0.0, 0.15, (1.0 - diag) - (p * 1.25 - 0.25));
        } else {
            alpha *= (1.0 - smoothstep(0.72, 1.0, p));
        }
    }
    // 37: Pure Crossfade
    else if (u_effect == 37) {
        alpha *= (1.0 - p);
    }
    // 38: Directional Slide
    else if (u_effect == 38) {
        alpha *= (1.0 - p * 0.9);
    }
    // 39: Architectural Wipe (razor mask + luminous edge)
    else if (u_effect == 39) {
        float cut = p * 1.04;
        if (uv.x < cut) {
            alpha = 0.0;
        } else {
            float line = exp(-(uv.x - cut) * 80.0) * sin(p * PI);
            col.rgb += mix(u_accent, vec3(1.0), 0.4) * line * 2.2;
        }
    }
    // 40, 41, 42: Corner Zoom, Orbital Spin, Jelly Wobble
    else {
        alpha *= (1.0 - smoothstep(0.55, 1.0, p));
    }

    col.rgb *= v_shade;
    fragColor = vec4(col.rgb, clamp(alpha, 0.0, 1.0));
}
"""

CUSTOM_SHADER_WRAPPER_HEADER = r"""#version 330 core
in vec2  v_uv;
in float v_shade;
in float v_alpha_mul;

uniform sampler2D u_tex;
uniform int   u_effect;
uniform float u_progress;
uniform float u_raw_t;
uniform float u_time;
uniform float u_intensity;
uniform vec3  u_accent;
uniform vec2  u_anchor;
uniform vec2  u_win_size;

out vec4 fragColor;
"""


MAX_CUSTOM_SHADER_BYTES = 128 * 1024
MAX_CUSTOM_LOOP_ITERATIONS = 64
MAX_CUSTOM_NESTED_LOOP_ITERATIONS = 256

_GLSL_LINE_CONT_RE = re.compile(r"\\[ \t]*\n")
_GLSL_COMMENT_RE = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
_GLSL_EXTENSION_RE = re.compile(r"^\s*#\s*extension\b", re.MULTILINE)
_GLSL_VERSION_RE = re.compile(r"^\s*#\s*version\b([^\n]*)", re.MULTILINE)
_GLSL_UNBOUNDED_LOOP_RE = re.compile(r"\b(while|do)\b")
_GLSL_NUM_PAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[uUfF]?"
_GLSL_DEFINE_NUM_RE = re.compile(rf"^\s*#\s*define\s+([A-Za-z_]\w*)\s+({_GLSL_NUM_PAT})\s*$")
_GLSL_CMP_LR_RE = re.compile(rf"^([A-Za-z_]\w*)\s*(<=|<|>=|>)\s*({_GLSL_NUM_PAT})$")
_GLSL_CMP_RL_RE = re.compile(rf"^({_GLSL_NUM_PAT})\s*(<=|<|>=|>)\s*([A-Za-z_]\w*)$")
_GLSL_CONST_TRUE_RE = re.compile(r"^(?:true|[+-]?(?:[1-9]\d*|0*\.[0-9]*[1-9][0-9]*)[uUfF]?)$", re.IGNORECASE)
_GLSL_RESERVED_DEFINE_NAMES = frozenset({
    "while", "do", "for", "if", "else", "switch", "case", "default", "break", "continue",
    "return", "discard", "void", "bool", "int", "uint", "float", "double", "true", "false",
    "const", "uniform", "in", "out", "inout", "struct", "layout", "main",
})


def _splice_glsl_continuations(src: str) -> str:
    """Normalize line endings and splice GLSL §3.3 backslash line continuations."""
    normalized = src.replace("\r\n", "\n").replace("\r", "\n")
    return _GLSL_LINE_CONT_RE.sub("", normalized)


def _strip_glsl_comments(src: str) -> str:
    """Strip // and /* ... */ GLSL comments while preserving line breaks and token boundaries."""
    def _repl(match: re.Match[str]) -> str:
        txt = match.group(0)
        if txt.startswith("/*"):
            nl = txt.count("\n")
            return ("\n" * nl) if nl else " "
        return ""
    return _GLSL_COMMENT_RE.sub(_repl, src)


def _preprocess_glsl_for_scan(src: str) -> str:
    return _strip_glsl_comments(_splice_glsl_continuations(src))


def _has_unc_symlink(raw_path: str) -> bool:
    """Inspect local symlink targets via lstat/readlink without ever calling os.stat on a UNC target."""
    cur = pathlib.Path(raw_path)
    for _ in range(8):
        try:
            if not cur.is_symlink():
                return False
            target = str(cur.readlink())
        except OSError:
            return False
        if "\x00" in target or target.replace("/", "\\").startswith(("\\\\", "\\?")):
            return True
        tpath = pathlib.Path(target)
        cur = tpath if tpath.is_absolute() else (cur.parent / tpath)
    return False


def _parse_glsl_number(token: str) -> Optional[float]:
    cleaned = token.strip().rstrip("uUfF")
    try:
        val = float(cleaned)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _split_top_level(expr: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(expr[start:idx])
            start = idx + 1
    parts.append(expr[start:])
    return parts


def _strip_outer_parens(expr: str) -> str:
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        wraps = True
        for idx, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx != len(s) - 1:
                    wraps = False
                    break
        if wraps and depth == 0:
            s = s[1:-1].strip()
        else:
            break
    return s


def _skip_ws(s: str, pos: int) -> int:
    n = len(s)
    while pos < n and s[pos].isspace():
        pos += 1
    return pos


def _match_balanced(s: str, open_pos: int, open_ch: str, close_ch: str) -> Optional[int]:
    """Return index immediately AFTER the matching close_ch, or None if unclosed."""
    n = len(s)
    if open_pos >= n or s[open_pos] != open_ch:
        return None
    depth = 1
    i = open_pos + 1
    while i < n and depth > 0:
        ch = s[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
        i += 1
    return i if depth == 0 else None


def _parse_stmt_span(s: str, pos: int, depth: int = 0) -> Optional[tuple[int, int]]:
    """Return (body_start, body_end) for a GLSL statement starting at/after `pos`."""
    if depth > 64:
        return None
    start = _skip_ws(s, pos)
    n = len(s)
    if start >= n:
        return None
    if s[start] == "{":
        end = _match_balanced(s, start, "{", "}")
        return (start, end) if end is not None else None
    if s[start] == ";":
        return (start, start + 1)

    m_kw = re.match(r"(for|if|switch)\b", s[start:])
    if m_kw:
        kw = m_kw.group(1)
        p_open = _skip_ws(s, start + len(kw))
        p_after = _match_balanced(s, p_open, "(", ")")
        if p_after is None:
            return None
        sub = _parse_stmt_span(s, p_after, depth + 1)
        if sub is None:
            return None
        end = sub[1]
        if kw == "if":
            after_ws = _skip_ws(s, end)
            if re.match(r"else\b", s[after_ws:]):
                else_sub = _parse_stmt_span(s, after_ws + 4, depth + 1)
                if else_sub is None:
                    return None
                end = else_sub[1]
        return (start, end)

    # Simple statement terminated by top-level ';'
    p_depth = 0
    b_depth = 0
    i = start
    while i < n:
        ch = s[i]
        if ch == "(":
            p_depth += 1
        elif ch == ")":
            p_depth = max(0, p_depth - 1)
        elif ch == "{":
            b_depth += 1
        elif ch == "}":
            if b_depth == 0:
                return None
            b_depth -= 1
        elif ch == ";" and p_depth == 0 and b_depth == 0:
            return (start, i + 1)
        i += 1
    return None


def _find_inout_functions(stripped: str) -> set[str]:
    """Return user-defined function names that take `out` or `inout` parameters."""
    names: set[str] = set()
    skip_kw = {"if", "for", "while", "switch", "return"}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", stripped):
        fn = m.group(1)
        if fn in skip_kw:
            continue
        p_after = _match_balanced(stripped, m.end() - 1, "(", ")")
        if p_after is None:
            continue
        after_ws = _skip_ws(stripped, p_after)
        if after_ws < len(stripped) and stripped[after_ws] in ("{", ";"):
            params = stripped[m.end() : p_after - 1]
            if re.search(r"\b(out|inout)\b", params):
                names.add(fn)
    return names


def _find_user_function_bodies(stripped: str) -> list[tuple[str, int, int]]:
    """Return list of (fn_name, body_start, body_end) for user function definitions."""
    fns: list[tuple[str, int, int]] = []
    skip_kw = {"if", "for", "while", "switch", "else", "return"}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", stripped):
        fn = m.group(1)
        if fn in skip_kw:
            continue
        p_after = _match_balanced(stripped, m.end() - 1, "(", ")")
        if p_after is None:
            continue
        brace_pos = _skip_ws(stripped, p_after)
        if brace_pos < len(stripped) and stripped[brace_pos] == "{":
            body_end = _match_balanced(stripped, brace_pos, "{", "}")
            if body_end is not None:
                fns.append((fn, brace_pos, body_end))
    return fns


def _mutates_var(code: str, var: str, inout_funcs: set[str]) -> bool:
    """Return True if `code` assigns to, increments/decrements, or passes `var` to an out/inout function."""
    v = re.escape(var)
    if re.search(rf"(?:\+\+|--)\s*(?<!\.){v}\b|(?<!\.)\b{v}\s*(?:\+\+|--)", code):
        return True
    if re.search(rf"(?<!\.)\b{v}\s*(?:\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|=(?!=))", code):
        return True
    for fn in inout_funcs:
        for m in re.finditer(rf"\b{re.escape(fn)}\s*\(", code):
            p_after = _match_balanced(code, m.end() - 1, "(", ")")
            if p_after is not None:
                args_txt = code[m.end() : p_after - 1]
                if re.search(rf"(?<!\.)\b{v}\b", args_txt):
                    return True
    return False


def _extract_step_delta(step_expr: str, var: str) -> Optional[float]:
    """Parse a single non-comma step expression updating `var` by a constant delta."""
    if "," in step_expr:
        return None
    v = re.escape(var)
    s = _strip_outer_parens(step_expr)
    if re.fullmatch(rf"(?:\+\+\s*{v}|{v}\s*\+\+)", s):
        return 1.0
    if re.fullmatch(rf"(?:--\s*{v}|{v}\s*--)", s):
        return -1.0
    m_add = re.fullmatch(rf"{v}\s*\+=\s*({_GLSL_NUM_PAT})", s)
    if m_add:
        return _parse_glsl_number(m_add.group(1))
    m_sub = re.fullmatch(rf"{v}\s*-=\s*({_GLSL_NUM_PAT})", s)
    if m_sub:
        val = _parse_glsl_number(m_sub.group(1))
        return (-val) if val is not None else None
    m_eq = re.fullmatch(rf"{v}\s*=\s*{v}\s*([+-])\s*({_GLSL_NUM_PAT})", s)
    if m_eq:
        val = _parse_glsl_number(m_eq.group(2))
        if val is None:
            return None
        return val if m_eq.group(1) == "+" else -val
    return None


def _validate_for_header(
    header: Optional[str], inout_funcs: set[str]
) -> tuple[bool, str, str, int]:
    if header is None:
        return (False, "Malformed 'for' loop header in custom shader", "", 0)
    parts = _split_top_level(header, ";")
    if len(parts) != 3:
        return (False, "Malformed 'for' loop header in custom shader", "", 0)
    init_part, cond_part, step_part = (p.strip() for p in parts)
    if not cond_part or _GLSL_CONST_TRUE_RE.fullmatch(_strip_outer_parens(cond_part)):
        return (False, "Unbounded or constant-true 'for' loop condition in custom shader", "", 0)
    if not step_part:
        return (False, "Missing loop increment in 'for' loop header", "", 0)
    if "," in init_part:
        return (False, "Disallowed comma operator in 'for' loop initializer", "", 0)
    if "," in cond_part:
        return (False, "Disallowed comma operator in 'for' loop condition", "", 0)
    if "," in step_part:
        return (False, "Disallowed comma operator in 'for' loop step", "", 0)
    if "||" in cond_part:
        return (False, "Disallowed logical OR in 'for' loop condition", "", 0)

    flip_op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
    matched_bound: Optional[tuple[str, str, float]] = None
    for raw_conj in cond_part.split("&&"):
        conj = _strip_outer_parens(raw_conj)
        m_lr = _GLSL_CMP_LR_RE.fullmatch(conj)
        if m_lr:
            val = _parse_glsl_number(m_lr.group(3))
            if val is not None:
                matched_bound = (m_lr.group(1), m_lr.group(2), val)
                break
        m_rl = _GLSL_CMP_RL_RE.fullmatch(conj)
        if m_rl:
            val = _parse_glsl_number(m_rl.group(1))
            if val is not None:
                matched_bound = (m_rl.group(3), flip_op[m_rl.group(2)], val)
                break

    if matched_bound is None:
        return (
            False,
            f"Missing bounded numeric comparison in 'for' loop (<= {MAX_CUSTOM_LOOP_ITERATIONS} iterations max)",
            "",
            0,
        )

    var, op, bound = matched_bound
    if abs(bound) > MAX_CUSTOM_LOOP_ITERATIONS:
        return (
            False,
            f"'for' loop bound {bound:g} exceeds maximum of {MAX_CUSTOM_LOOP_ITERATIONS} iterations",
            "",
            0,
        )

    if _mutates_var(cond_part, var, inout_funcs):
        return (False, f"'for' loop condition must not mutate induction variable '{var}'", "", 0)

    step_delta = _extract_step_delta(step_part, var)
    if step_delta is None or abs(step_delta) < 1e-3:
        return (False, f"'for' loop step for '{var}' must use a single non-zero constant increment", "", 0)

    init_clean = _strip_outer_parens(init_part)
    m_init = re.fullmatch(
        rf"(?:(?:lowp|mediump|highp)\s+)?(?:(?:int|uint|float)\s+)?{re.escape(var)}\s*=\s*({_GLSL_NUM_PAT})",
        init_clean,
    )
    start_val = _parse_glsl_number(m_init.group(1)) if m_init else None
    if start_val is None or abs(start_val) > 1024.0:
        return (
            False,
            f"'for' loop on '{var}' requires an explicit numeric literal initializer",
            "",
            0,
        )

    iters = 0
    if op in ("<", "<="):
        if step_delta <= 0.0:
            return (False, f"'for' loop on '{var}' steps away from upper bound", "", 0)
        span = bound - start_val
        if span >= 0.0:
            iters = (
                int(math.floor(span / step_delta)) + 1
                if op == "<="
                else int(math.ceil(span / step_delta))
            )
            if iters > MAX_CUSTOM_LOOP_ITERATIONS:
                return (
                    False,
                    f"'for' loop iteration count ({iters}) exceeds maximum of {MAX_CUSTOM_LOOP_ITERATIONS}",
                    "",
                    0,
                )
    else:
        if step_delta >= 0.0:
            return (False, f"'for' loop on '{var}' steps away from lower bound", "", 0)
        span = start_val - bound
        if span >= 0.0:
            dec = -step_delta
            iters = (
                int(math.floor(span / dec)) + 1
                if op == ">="
                else int(math.ceil(span / dec))
            )
            if iters > MAX_CUSTOM_LOOP_ITERATIONS:
                return (
                    False,
                    f"'for' loop iteration count ({iters}) exceeds maximum of {MAX_CUSTOM_LOOP_ITERATIONS}",
                    "",
                    0,
                )
    return (True, "", var, iters)


def validate_custom_glsl_source(src: str) -> tuple[bool, str]:
    """Static pre-validator for custom GLSL 330 Core fragment shaders (Board Resolution #4)."""
    if not isinstance(src, str) or not src.strip():
        return (False, "Custom shader source is empty")

    # Phase 1 & 2: normalize newlines, splice GLSL §3.3 '\\\n' continuations, then strip comments
    stripped = _preprocess_glsl_for_scan(src)

    # Preprocessor allowlist: ONLY `#version 330 core` (max 1) and bare numeric `#define <IDENT> <num>`
    version_count = 0
    for line in stripped.split("\n"):
        if "#" not in line:
            continue
        if not re.match(r"^\s*#", line) or line.count("#") > 1:
            return (False, "Disallowed preprocessor token '#' in custom shader")
        if _GLSL_EXTENSION_RE.match(line):
            return (False, "Disallowed '#extension' directive in custom shader")
        m_ver = _GLSL_VERSION_RE.match(line)
        if m_ver:
            version_count += 1
            if version_count > 1:
                return (False, "Duplicate '#version' directive in custom shader")
            ver_spec = " ".join(m_ver.group(1).split())
            if ver_spec != "330 core":
                return (
                    False,
                    f"Unsupported '#version {ver_spec}' directive (only '#version 330 core' is allowed)",
                )
            continue
        m_def = _GLSL_DEFINE_NUM_RE.fullmatch(line)
        if m_def:
            def_name = m_def.group(1)
            if def_name in _GLSL_RESERVED_DEFINE_NAMES or _parse_glsl_number(m_def.group(2)) is None:
                return (False, f"Disallowed '#define {def_name}' directive in custom shader")
            continue
        return (
            False,
            "Disallowed preprocessor directive in custom shader (only '#version 330 core' and numeric '#define' allowed)",
        )

    m_loop = _GLSL_UNBOUNDED_LOOP_RE.search(stripped)
    if m_loop:
        return (
            False,
            f"Disallowed '{m_loop.group(1)}' loop in custom shader (only bounded for-loops <= {MAX_CUSTOM_LOOP_ITERATIONS} allowed)",
        )

    inout_funcs = _find_inout_functions(stripped)

    # Parse every `for` occurrence: reject any `\bfor\b` not immediately followed by `\s*\(`
    parsed_loops: list[tuple[int, str, int, int, str, int]] = []
    for m_for in re.finditer(r"\bfor\b", stripped):
        open_pos = _skip_ws(stripped, m_for.end())
        if open_pos >= len(stripped) or stripped[open_pos] != "(":
            return (False, "Malformed 'for' construct in custom shader (must be followed by '(')")
        close_after = _match_balanced(stripped, open_pos, "(", ")")
        if close_after is None:
            return (False, "Unclosed 'for' loop header in custom shader")
        header = stripped[open_pos + 1 : close_after - 1]
        ok, err, var, iters = _validate_for_header(header, inout_funcs)
        if not ok:
            return (False, err)
        body_span = _parse_stmt_span(stripped, close_after)
        if body_span is None:
            return (False, "Malformed 'for' loop body in custom shader")
        body_start, body_end = body_span
        body_text = stripped[body_start:body_end]
        if _mutates_var(body_text, var, inout_funcs):
            return (False, f"'for' loop body must not mutate induction variable '{var}'")
        parsed_loops.append((m_for.start(), var, body_start, body_end, body_text, iters))

    # Enforce nested loop iteration product <= MAX_CUSTOM_NESTED_LOOP_ITERATIONS (256)
    direct_products: list[int] = []
    for idx, (h_start, _var, _b_start, _b_end, _b_text, iters) in enumerate(parsed_loops):
        prod = max(1, iters)
        for p_idx, (_ph, _pvar, pb_start, pb_end, _pb_text, p_iters) in enumerate(parsed_loops):
            if p_idx != idx and pb_start <= h_start < pb_end:
                prod *= max(1, p_iters)
                if prod > MAX_CUSTOM_NESTED_LOOP_ITERATIONS:
                    return (
                        False,
                        f"Nested 'for' loop iteration product ({prod}) exceeds maximum of {MAX_CUSTOM_NESTED_LOOP_ITERATIONS}",
                    )
        direct_products.append(prod)

    # Also enforce iteration product across helper function calls inside loops
    fn_bodies = _find_user_function_bodies(stripped)
    fn_iters: dict[str, int] = {}
    for fn_name, f_start, f_end in fn_bodies:
        best = 1
        for (h_start, _v, _bs, _be, _bt, _it), dprod in zip(parsed_loops, direct_products):
            if f_start <= h_start < f_end:
                best = max(best, dprod)
        if best > 1:
            fn_iters[fn_name] = max(fn_iters.get(fn_name, 1), best)

    if fn_iters:
        # Propagate loop cost through chained helper calls
        for _ in range(4):
            changed = False
            for fn_name, f_start, f_end in fn_bodies:
                f_src = stripped[f_start:f_end]
                cur_cost = fn_iters.get(fn_name, 1)
                for callee, c_cost in list(fn_iters.items()):
                    if callee != fn_name and re.search(rf"\b{re.escape(callee)}\s*\(", f_src):
                        # If callee is called inside a loop of fn_name, cost multiplies; else max
                        best_in_fn = cur_cost
                        for (h_start, _v, bs, be, bt, _it), dprod in zip(parsed_loops, direct_products):
                            if f_start <= h_start < f_end and re.search(rf"\b{re.escape(callee)}\s*\(", bt):
                                best_in_fn = max(best_in_fn, dprod * c_cost)
                        best_in_fn = max(best_in_fn, c_cost)
                        if best_in_fn > cur_cost:
                            fn_iters[fn_name] = best_in_fn
                            cur_cost = best_in_fn
                            changed = True
            if not changed:
                break

        for (_h_start, _v, _bs, _be, b_text, _it), dprod in zip(parsed_loops, direct_products):
            for callee, c_cost in fn_iters.items():
                if re.search(rf"\b{re.escape(callee)}\s*\(", b_text):
                    total = dprod * c_cost
                    if total > MAX_CUSTOM_NESTED_LOOP_ITERATIONS:
                        return (
                            False,
                            f"Nested 'for' loop iteration product via '{callee}()' ({total}) exceeds maximum of {MAX_CUSTOM_NESTED_LOOP_ITERATIONS}",
                        )

    return (True, "")


def load_custom_fragment_shader_checked(path: str) -> tuple[Optional[str], str]:
    """Validate, decode, sandbox-check, and wrap a custom .glsl fragment shader."""
    if not isinstance(path, str):
        return (None, "Invalid custom shader path")
    raw_path = path.strip()
    if (
        not raw_path
        or "\x00" in raw_path
        or raw_path.replace("/", "\\").startswith(("\\\\", "\\?"))
    ):
        return (None, "Unsafe or UNC custom shader path rejected")
    p = pathlib.Path(raw_path)
    if p.suffix.lower() != ".glsl":
        return (None, "Custom shader file must have a .glsl extension")
    if _has_unc_symlink(raw_path):
        return (None, "Custom shader symlink to UNC path rejected")
    try:
        if not p.is_file():
            return (None, "Custom shader file not found")
        resolved = p.resolve()
        if str(resolved).replace("/", "\\").startswith(("\\\\", "\\?")) or resolved.suffix.lower() != ".glsl":
            return (None, "Resolved custom shader path is invalid")
        if resolved.stat().st_size > MAX_CUSTOM_SHADER_BYTES:
            return (None, f"Custom shader exceeds {MAX_CUSTOM_SHADER_BYTES} byte limit")
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        return (None, f"Failed to read custom shader: {exc}")

    if len(raw_bytes) > MAX_CUSTOM_SHADER_BYTES:
        return (None, f"Custom shader exceeds {MAX_CUSTOM_SHADER_BYTES} byte limit")

    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")) and not raw_bytes.startswith(
        (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
    ):
        src = raw_bytes.decode("utf-16", errors="replace")
    else:
        src = raw_bytes.decode("utf-8-sig", errors="replace")

    if "\x00" in src:
        return (None, "Custom shader contains unexpected NUL bytes")

    ok, err = validate_custom_glsl_source(src)
    if not ok:
        return (None, err)

    stripped = _preprocess_glsl_for_scan(src)
    if _GLSL_VERSION_RE.search(stripped):
        return (src, "")
    return (CUSTOM_SHADER_WRAPPER_HEADER + "\n" + src, "")


def load_custom_fragment_shader(path: str) -> Optional[str]:
    src, _err = load_custom_fragment_shader_checked(path)
    return src


def hex_to_rgb_f(hex_color: str) -> tuple[float, float, float]:
    h = (hex_color or "#2D81FF").lstrip("#")
    if len(h) != 6:
        return (0.176, 0.506, 1.0)
    try:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    except ValueError:
        return (0.176, 0.506, 1.0)


def build_grid_mesh(subdiv: int = 32) -> tuple[list[float], list[int]]:
    """Build a static (subdiv+1)^2 UV grid vertex & index buffer for 3D GPU deformations."""
    n = max(1, min(48, subdiv))
    verts: list[float] = []
    indices: list[int] = []
    for j in range(n + 1):
        v = j / n
        for i in range(n + 1):
            u = i / n
            verts.append(u)
            verts.append(v)
    stride = n + 1
    for j in range(n):
        for i in range(n):
            a = j * stride + i
            b = a + 1
            c = a + stride
            d = c + 1
            indices.extend((a, c, b, b, c, d))
    return verts, indices
