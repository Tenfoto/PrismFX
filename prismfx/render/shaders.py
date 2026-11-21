"""GLSL 330 Core GPU Vertex + Fragment Shaders for all 42 PrismFX effects + Wobbly Physics.

Designed for 165 Hz (<1.5 ms GPU cost) with zero external shader compiler dependencies.
"""

from __future__ import annotations

import pathlib
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
        v_alpha_mul = smoothstep(1.0, 0.82, stagger);
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
        vec2 blast = normalize(cNdc + vec2(0.001, 0.05)) * (0.25 + rnd3 * 0.65) * st * amp;
        pos.xy = cNdc + rel + blast;
        pos.y -= st * st * 1.15 * amp;
        pos.z = (rnd1 - 0.5) * st * 0.9;
        v_shade = 0.8 + 0.35 * abs(cs);
        v_alpha_mul = smoothstep(1.0, 0.65, st);
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
        v_alpha_mul = smoothstep(1.0, 0.78, st);
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
    return vec2(sqrt(md), id);
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
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        return vec4(0.0);
    }
    return texture(u_tex, uv);
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
        vec2 ruv = uv + normalize(d + 1e-4) * wave;
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
            float fireBand = smoothstep(0.08, 0.0, edge);
            col.rgb = mix(vec3(0.07, 0.03, 0.02), col.rgb, charBand);
            vec3 fireCol = mix(vec3(1.0, 0.25, 0.02), vec3(1.0, 0.92, 0.45), smoothstep(0.06, 0.005, edge));
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
            float arc = smoothstep(0.09, 0.0, edge);
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
        float crack = smoothstep(0.08, 0.0, abs(vor.x - 0.45));
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
        float angle = atan(d.y, d.x);
        float pull = p * p * 1.35 * amp;
        float swirl = (3.8 * p * amp) / (r + 0.18);
        float newR = r * (1.0 + pull * 2.4);
        vec2 suv = u_anchor + vec2(cos(angle + swirl), sin(angle + swirl)) * newR;
        col = sampleWin(suv);
        float ring = exp(-pow((r - (0.35 * (1.0 - p) + 0.03)) * 18.0, 2.0)) * sin(p * PI);
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
            float glow = smoothstep(0.18, 0.0, edge);
            vec3 aur = 0.5 + 0.5 * cos(vec3(0.0, 2.0, 4.0) + uv.x * 5.0 + p * 4.0);
            col.rgb = mix(col.rgb, aur * 1.5, glow * 0.85);
        }
    }
    // 19: Shockwave Nova
    else if (u_effect == 19) {
        vec2 d = uv - 0.5;
        float r = length(d);
        float ringR = p * 0.85;
        float band = exp(-pow((r - ringR) * 18.0, 2.0));
        vec2 dir = normalize(d + 1e-4);
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
            float flame = smoothstep(0.16, 0.0, front);
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
        float blocks = mix(240.0, 10.0, pow(p, 0.7));
        vec2 grid = vec2(blocks, blocks * 0.65);
        vec2 cell = floor(uv * grid);
        vec2 muv = (cell + 0.5) / grid;
        col = sampleWin(muv);
        float rnd = hash12(cell);
        alpha = col.a * step(p * 1.02, rnd * 0.85 + 0.15);
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
        float band = exp(-pow((uv.y - fract(p * 1.5)) * 12.0, 2.0));
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
            float cellRim = smoothstep(0.08, 0.0, hc.x) * smoothstep(0.28, 0.0, edge);
            float frontGlow = smoothstep(0.1, 0.0, edge);
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


def load_custom_fragment_shader(path: str) -> Optional[str]:
    p = pathlib.Path(path)
    if not p.exists():
        return None
    src = p.read_text(encoding="utf-8", errors="ignore")
    if "#version" in src:
        return src
    return CUSTOM_SHADER_WRAPPER_HEADER + "\n" + src


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
