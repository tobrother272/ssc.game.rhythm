#version 330 core

in vec3 v_normal;
in vec2 v_uv;
in vec3 v_world_pos;
in vec3 v_base_color;
in float v_z_norm;
in float v_face_id;

uniform vec3 u_camera_pos;
uniform sampler2D u_icon_tex;
uniform float u_corner_radius;   // 0.22 = soft rounded corners

out vec4 fragColor;

float sd_rounded_box(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

void main() {
    vec3 base = v_base_color;
    int fid = int(v_face_id + 0.5);

    float depth_gain = 0.80 + 0.20 * (1.0 - v_z_norm);

    // Light/shadow contrast: side faces lit (bright), front face shadowed.
    // Top face: brightest + strong white tint (per gap-analysis spec điểm 1).
    vec3 front_col = base * 0.825 * depth_gain;          // front: shadow side, +50% from 0.55 to blend with bright side
    vec3 top_col   = (base * 1.25 + 0.55) * depth_gain;  // top: brightest + strong white tint
    vec3 side_col  = base * 1.10 * depth_gain;           // side: BRIGHT (main lit face)

    // Rounded-corner alpha mask — applied uniformly on every face so the
    // silhouette stays smooth and corners blend (no hard discard, no black
    // pixel artifacts, anti-aliased via smoothstep).
    float alpha = 1.0;
    if (u_corner_radius > 0.001) {
        vec2 p = v_uv * 2.0 - 1.0;
        float d = sd_rounded_box(p, vec2(1.0), u_corner_radius);
        // 1px-wide AA band at the edge: alpha 1 inside, fades to 0 outside.
        float aa = fwidth(d) * 1.5 + 1e-4;
        alpha = 1.0 - smoothstep(-aa, aa, d);
        if (alpha <= 0.001) discard;
    }

    vec3 face_color;

    if (fid == 0) {
        // FRONT face — solid fill with lane color + dark fist icon overlay
        face_color = front_col;

        // Fist icon overlay — large (78% of front face) per gap-analysis
        // điểm 6, dark silhouette on shadowed front face for max readability.
        vec2 icon_uv = (v_uv - 0.11) / 0.78;
        if (icon_uv.x >= 0.0 && icon_uv.x <= 1.0 &&
            icon_uv.y >= 0.0 && icon_uv.y <= 1.0) {
            vec4 icon = texture(u_icon_tex, icon_uv);
            // Near-black icon for sharp readability on the shadowed face
            vec3 icon_dark = base * 0.10;
            face_color = mix(face_color, icon_dark, icon.a * 0.95);
        }

    } else if (fid == 2) {
        // TOP face — brightest neon (creates the glowing top edge)
        face_color = top_col;

    } else if (fid == 4 || fid == 5) {
        // SIDE faces — darker shadow side (depth contrast vs lit front)
        face_color = side_col;

    } else if (fid == 3) {
        // BOTTOM — dimmer
        face_color = base * 0.3 * depth_gain;

    } else {
        // BACK — rarely visible
        face_color = vec3(0.02);
    }

    // Subtle rim lighting only at extreme grazing angles for soft edge pop
    vec3 N = normalize(v_normal);
    vec3 V = normalize(u_camera_pos - v_world_pos);
    float rim = 1.0 - max(dot(N, V), 0.0);
    rim = pow(rim, 4.0);
    face_color += (base * 1.0 + 0.1) * rim * 0.10;

    fragColor = vec4(clamp(face_color, 0.0, 1.0), alpha);
}
