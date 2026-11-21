// Example PrismFX Custom GLSL 330 Core Fragment Shader Plugin
// Available uniforms: u_tex, u_progress (0..1), u_raw_t, u_time, u_intensity, u_accent, u_anchor, u_win_size

void main() {
    float p = clamp(u_progress, 0.0, 1.0);
    vec2 center = v_uv - 0.5;
    float dist = length(center);
    float ring = sin(dist * 28.0 - p * 16.0) * exp(-dist * 2.2) * p * 0.045 * u_intensity;
    vec2 dir = normalize(center + 1e-4);

    vec4 col;
    col.r = texture(u_tex, v_uv + dir * (ring + 0.015 * p)).r;
    col.g = texture(u_tex, v_uv + dir * ring).g;
    col.b = texture(u_tex, v_uv + dir * (ring - 0.015 * p)).b;
    col.a = texture(u_tex, v_uv + dir * ring).a;

    col.rgb += u_accent * abs(ring) * 12.0;
    fragColor = vec4(col.rgb * v_shade, col.a * (1.0 - smoothstep(0.25, 1.0, p)));
}
