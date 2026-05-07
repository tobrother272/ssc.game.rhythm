#version 330 core

// Per-vertex attributes
in vec3 in_position;      // Local cube vertex (-0.5..0.5)
in vec3 in_normal;        // Face normal
in vec2 in_uv;            // UV coords (0..1 per face)
in float in_face_id;      // 0=front, 1=back, 2=top, 3=bottom, 4=left, 5=right

// Per-instance attributes
in vec3 in_inst_pos;      // World position of cube center (wx corrected via lane_x)
in vec3 in_inst_scale;    // Half-size per axis (width_half, height_half, depth_half)
in vec3 in_inst_color;    // Base color (RGB 0..1)
in float in_inst_z_norm;  // Depth progress (1=far, 0=hit)
in float in_inst_yaw;     // Yaw rotation around Y axis (radians)

uniform mat4 u_view_proj;
uniform float u_depth_extrude;  // Base exaggeration strength (0.55 default)

out vec3 v_normal;
out vec2 v_uv;
out vec3 v_world_pos;
out vec3 v_base_color;
out float v_z_norm;
out float v_face_id;

void main() {
    // Apply yaw rotation around Y axis
    float c = cos(in_inst_yaw);
    float s = sin(in_inst_yaw);
    mat3 rot_y = mat3(
        c,    0.0,  s,
        0.0,  1.0,  0.0,
        -s,   0.0,  c
    );

    vec3 scaled = in_position * in_inst_scale * 2.0;
    vec3 rotated = rot_y * scaled;
    vec3 world_pos = rotated + in_inst_pos;

    // Perspective-correct tilt: blocks parallel to floor.
    float depth_factor = 1.0 - in_inst_z_norm * 0.75;  // 1.0 at hit, 0.25 at far
    float back_frac = max(0.0, in_position.z);  // 0 at front, 0.5 at back
    float extrude = u_depth_extrude * in_inst_scale.y * depth_factor;

    // Push back vertices UP — tilts block to show top face.
    world_pos.y -= back_frac * extrude * 2.0;

    gl_Position = u_view_proj * vec4(world_pos, 1.0);

    // Rotate normal vector together with cube
    v_normal = rot_y * in_normal;
    v_uv = in_uv;
    v_world_pos = world_pos;
    v_base_color = in_inst_color;
    v_z_norm = in_inst_z_norm;
    v_face_id = in_face_id;
}
