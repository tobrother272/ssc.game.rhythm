# Game Mode Render Spec

This document summarizes the current rendering and scheduling behavior for
`punch`, `dance`, `line`, and `relax` modes so another agent can modify the
game without losing the intended design.

Primary code:

- `src/rhythm.py`: audio analysis, camera/tunnel, target scheduling, target rendering, VFX, final video render.
- `src/stickman.py`: stickman action libraries and beat-synced pose timeline.
- `.cursor/rules/obstacle-visual-style.mdc`: persistent design rule for future obstacle visuals.

## Global Render Pipeline

1. Load audio with `librosa`.
2. Build beat events from `--beat_source`:
   - `tempo`: uniform BPM cadence.
   - `beat`: `librosa.beat.beat_track`.
   - `onset`: transient/onset detection.
3. Optional subdivision via `--beat_subdiv`.
4. Optional density adjustment via `--density`.
5. Detect RMS wave columns with `detect_wave_columns`.
6. Resolve `--mode` into one or more sub-modes using `_parse_modes`.
   - Valid modes: `punch`, `dance`, `line`, `relax`.
   - Comma list means combo mode, e.g. `punch,dance,relax`.
   - Combo mode cycles one sub-mode per beat.
7. Create `PerspectiveCamera`, `TunnelRenderer`, `GameManager`, `StickmanHUD`, `ComboHUD`, and `ViewportFrame`.
8. Auto-calculate travel if `--travel -1`.
9. `GameManager.pre_schedule(...)` creates all targets up front.
10. Convert scheduled targets into stickman events.
11. Per frame:
    - `game.update(fi)` checks auto-hit.
    - Draw tunnel.
    - Draw live targets back-to-front.
    - Trigger VFX for hits, except `RelaxTarget`.
    - Draw particles.
    - Apply bloom.
    - Apply relax camera bob if `relax` is active.
    - Draw viewport, stickman, combo, debug overlays.
12. Encode video and mux audio.

## Camera And Scene

`PerspectiveCamera` supports both legacy 2D helpers and 3D projection.

Important constants:

- `FPS = 30`.
- `N_LANES = 4`, `N_LANES_DANCE = 4`.
- `DEPTH_MODE = 'linear'` by default.
- `Z_NEAR = 2.5`, `Z_FAR = 28.0`.
- `horizon_frac = 0.45`.
- `hit_zone_frac = 0.86`.
- `floor_spread_frac`:
  - `punch`: `0.50`.
  - `dance`: `0.552`.

Depth rules:

- Normal targets use normalized depth `z_norm` where `1.0 = horizon/far`, `0.0 = hit zone`.
- `PerspectiveCamera.z_from_norm(z_norm)` maps normalized depth to world Z.
- Default `linear` mode moves targets at constant world speed. Perspective makes them stay small far away and zoom near the camera.

Tunnel:

- `TunnelRenderer.draw(...)` draws dark receding tunnel, floor grid, horizon line, subtle side-wall guides.
- `lane_tiles=True` currently used for all modes, so the runway has 1 floor-panel column per lane.
- `ViewportFrame` draws lane-aligned floor panels and shakes/lights them on hits.

## Shared Scheduling Rules

`GameManager.pre_schedule(...)` owns target placement.

Common rules:

- Beat stream alternates strict body side: left side, right side, left side, right side.
- In 4 lanes:
  - Left side lanes: `0,1`.
  - Right side lanes: `3,2` after reverse, producing `L0 -> R3 -> L1 -> R2 -> ...`.
- `--lanes` filters available lanes using 1-based CLI input.
- `min_gap_frames` merges beats that are too close.
- `min_lane_gap` prevents same-lane visual stacking.
- `MAX_PER_LANE` controls `min_lane_gap` indirectly.
- Wall targets, if used, span full tunnel and are independent of lane filter.

Combo mode:

- `--mode punch,dance` means beat 0 uses `punch`, beat 1 uses `dance`, beat 2 uses `punch`, etc.
- `stick_action = 'combo'` when more than one sub-mode is active.
- Stickman event kinds are prefixed in combo:
  - Punch: `PL`, `PR`.
  - Dance: `DL`, `DR`.
  - Line: `Z...`.
  - Relax: `JP`, `SQ`.

## Punch Mode

Purpose:

- Air cubes fly toward the hit zone at chest height.
- Stickman punches left or right based on target side.

Target class:

- `PunchTarget`.

Rendering:

- Default visual is a shaded 3D neon cube rendered by `draw_cube_3d`.
- Left targets default to green, right targets default to red.
- Each cube has:
  - Proper 3D projection.
  - Lambert-like face shading.
  - Rounded silhouette via `_round_poly`.
  - Neon edge glow via `_draw_neon_edges`.
  - Bright inset front face.
  - White fist icon via `_draw_fist_icon`.
- Default `PunchTarget.CUBE_HALF = 0.154`.
- Default `PunchTarget.CORNER_RADIUS = 0.18`.
- Cube yaw:
  - Left side: `+0.35`.
  - Right side: `-0.35`.
  - This exposes side/top faces and prevents flat-square look.

Custom assets:

- `--cube_image`, `--cube_image_left`, `--cube_image_right` wrap images on cube faces.
- `--cube_model`, `--cube_model_left`, `--cube_model_right` replace cubes with mesh assets.
- Mesh assets are normalized and rendered with software Lambert shading.
- Mesh overrides image texture.
- `--mesh_wireframe` draws white wireframe on mesh only.

Scheduling:

- Uses normal side/lane cycling.
- Paired punch is supported through `--punch_pair_cycle`.
- If same-side adjacent pairs exist:
  - Every N-th punch beat emits two cubes on the next same-side adjacent pair.
  - Stickman event collapses to `LL` or `RR`.
  - `N=4` default: 3 single punches + 1 double-hand punch.
  - `N=1`: every punch beat double.
  - `N<=0`: disabled.

Stickman:

- Action library: `ACTIONS['punch']`.
- Single hits:
  - `L`: cycles `JAB_L`, `CROSS_L`, `HOOK_L`, `UPPERCUT_L`.
  - `R`: cycles `JAB_R`, `CROSS_R`, `HOOK_R`, `UPPERCUT_R`.
- Paired:
  - `LL`: `DOUBLE_LEFT`.
  - `RR`: `DOUBLE_RIGHT`.
- Recover:
  - Left kinds recover to `GUARD_L`.
  - Right kinds recover to `GUARD_R`.

VFX:

- On hit:
  - Particles burst at air height.
  - `ViewportFrame.trigger(1.0)`.
  - Combo increments.

## Dance Mode

Purpose:

- Floor pads slide along the floor.
- Stickman performs stomp/dance movement.

Target class:

- `DanceTarget`.

Rendering:

- Default path draws a flat glowing floor tile with `_draw_flat_tile`.
- Tile is a 3D-projected floor quad, flush with `cam.FLOOR_WORLD_Y`.
- Size:
  - `HALF_X = 0.20`.
  - `HALF_Z = 0.32`.
  - `HALF_Y = 0.02` is retained for legacy mesh path.
- Visual layers:
  - Outer soft glow.
  - Main translucent fill.
  - Inner hot highlight biased toward front edge.
  - Crisp neon rim.
  - Dark inner shadow line.
  - Dark stomp/foot icon.
- Depth gain increases glow as target approaches.
- If mesh is provided, uses mesh renderer but keeps asset flush with floor.

Scheduling:

- Default solo density is `0.5`, meaning one dance tile per 2 beats.
- In combo mode, density default stays `1.0` because the sub-mode cycle already reduces each mode's cadence.
- Uses same side/lane cycling as punch.
- Paired dance is supported through `--dance_pair_cycle`.
- If same-side adjacent pairs exist:
  - Every N-th dance beat emits two tiles on the next same-side adjacent pair.
  - Stickman event collapses to `JL` or `JR`.
  - `N=4` default: 3 single stomps + 1 feet-together jump.
  - `N=1`: every dance beat paired.
  - `N<=0`: disabled.

Stickman:

- Action library: `ACTIONS['dance']`.
- Single hits:
  - `L`: cycles `STOMP_L`, `STOMP_L2`.
  - `R`: cycles `STOMP_R`, `STOMP_R2`.
- Paired hits:
  - `JL`: `FEET_STOMP_L`.
  - `JR`: `FEET_STOMP_R`.
- Prelift:
  - `L`: `LIFT_L -> SHIFT_L -> STOMP_L/STOMP_L2`.
  - `R`: `LIFT_R -> SHIFT_R -> STOMP_R/STOMP_R2`.
  - `JL`: `LIFT_DANCE -> FEET_SHIFT_L -> FEET_STOMP_L`.
  - `JR`: `LIFT_DANCE -> FEET_SHIFT_R -> FEET_STOMP_R`.
- `prelift_time = 0.26s`.
- Recover:
  - `L/JL`: `RECOVER_L`.
  - `R/JR`: `RECOVER_R`.

Lane-dependent lean:

- For 4-lane mode, stickman lean scale depends on distance from center.
- Formula: `0.55 + 1.05 * offset_norm`.
- Outer lanes produce large side movement; inner lanes produce tighter movement.

VFX:

- On hit:
  - Particles burst near floor.
  - `ViewportFrame.trigger(0.9)`.
  - Combo increments.

## Line Mode

Purpose:

- Hold-note / rail-style mode.
- Renders a chain of connected punch-like blocks.
- Stickman holds or sweeps punch poses while the chain passes.

Target class:

- `LineTarget`, subclass of `PunchTarget`.

Core idea:

- A line target is not one long primitive.
- It is a chain of block segments whose front/back faces are time-anchored.
- Each block has:
  - `block_hit_frames[i]`: front face reaches punch plane.
  - `block_back_frames[i]`: back face reaches punch plane.
  - `block_shrink_dur[i] = block_back_frames[i] - block_hit_frames[i]`.
- Adjacent blocks share the same Z at junctions, so the chain has no visible gaps.

Wave-column scheduling:

- Solo `line` mode derives chains directly from RMS wave columns when available.
- `detect_wave_columns` returns:
  - `rise_f`: start of RMS rise, used as block hit/arrival frame.
  - `peak_f`: column peak.
  - `end_f`: descent midpoint, used for shrink end.
  - `height`: peak strength.
- `blocks_per_chain = 2 * line_beats`.
- Strongest required columns are selected by RMS height, then sorted by time and grouped into chains.
- For each chain:
  - Chain start = first column `rise_f`.
  - Per-block hit frames = each column `rise_f`.
  - Per-block shrink duration = `end_f - rise_f`.
  - Effective hold = last hit frame - first hit frame.

Fallback scheduling:

- If no wave columns are available, line duration is based on median beat gap:
  - `line_hold_frames = line_beats * median_gap`.
- `LineTarget` creates `n_cubes = min(8, max(2, 2 * line_beats))`.

Line locking:

- Only one line chain is allowed globally at a time.
- `line_global_busy_until` blocks new chains until previous chain tail clears.
- `line_busy_until[lane]` also blocks other targets from occupying that lane.

Zigzag modes:

- `--line_zigzag vertical`:
  - Chain stays on one lane.
  - Blocks alternate vertical position up/down around air height.
  - Stickman events alternate `Z?D` and `Z?U`.
- `--line_zigzag horizontal`:
  - Chain spans outer lane 0 to outer lane n-1.
  - Blocks alternate direction left-to-right and right-to-left.
  - Chain is placed higher with `LineTarget.HORIZONTAL_WY = -0.30`.
  - Stickman events alternate sweep kinds:
    - Even block: `ZSLR`.
    - Odd block: `ZSRL`.

Rendering:

- Uses true 3D cube/box geometry, but each segment is time-anchored by its front and back frames.
- During approach:
  - Front face moves naturally toward camera.
  - Back face follows its own arrival time.
- During shrink:
  - Front face retreats toward the moving back face.
  - Block collapses front-to-back.
  - Junction remains stable, preserving seamless chain.
- Rendering sorts segments far-to-near.
- Visible faces are rounded.
- Active/approaching segment can be brighter; shrinking segment keeps front neon while body fades.
- Fist icon is drawn on readable front faces.

Hit/VFX:

- `LineTarget.check_hit` fires once per block, not once per chain.
- Each block can trigger particles, viewport shake, and combo.
- In horizontal zigzag, particle X/Y follows the actual block index and elevated world-Y.

Stickman:

- Solo action library: `ACTIONS['line']`.
- Vertical:
  - `ZLU`, `ZLD`, `ZRU`, `ZRD` map to hold poses.
- Horizontal:
  - `ZSLR`: sweep left-to-right.
  - `ZSRL`: sweep right-to-left.
  - `holds_end` maps start pose to end pose so the arm sweeps across sustain.
- Each block event carries sustain equal to that block's shrink duration.

Debug:

- `--line_debug 1` draws:
  - Top timeline event markers.
  - Current frame cursor.
  - Optional RMS waveform overlay.

## Relax Mode

Purpose:

- Calm dodge-style mode.
- No lane-specific input.
- Obstacles span the tunnel and are avoided, not hit.
- Two obstacle kinds:
  - `low`: ground slab, stickman jumps.
  - `high`: floating overhead bar, stickman squats.

Target class:

- `RelaxTarget`.

Scheduling:

- Each relax target picks random kind unless forced internally:
  - 50% `low`.
  - 50% `high`.
- Relax uses no lane cycling and no paired-spawn.
- Solo relax applies a 4x travel slowdown:
  - `_relax_slow_mult = 4.0`.
  - If `--travel -1`, auto travel is multiplied by 4.
  - If `--travel` is explicit, explicit travel is still multiplied by 4.
- Solo relax has two cadence modes:
  - `--relax_interval 0.0`: music-driven, uses beat frames.
  - `--relax_interval > 0.0`: fixed delay after previous block fully disappears.

Fixed-delay relax:

- Only active for solo `--mode relax`.
- `--relax_interval` means idle delay after disappearance, not hit interval.
- Next spawn begins after previous block is dead plus `delay * FPS`.
- Formula:
  - `exit_pad = round(travel * 1.2 / v2)`.
  - `step_f = travel + exit_pad + 1 + delay_f`.
  - `first_f = travel`.
- This guarantees no overlap when fixed-delay mode is used.

Music-driven solo relax:

- Drops beats where `bf < travel` so blocks do not pop in mid-flight at frame 0.

Motion profile:

- `RelaxTarget` does not use base linear `Target.depth`.
- It uses two-phase piecewise motion:
  - `PHASE_SPLIT_D = 0.70`: first 70% of z-distance.
  - `PHASE_SPEED_RATIO = 12.0`: final phase is 12x phase-1 world speed.
- Phase 1:
  - Slow drift from z=1.0 to z=0.30.
- Phase 2:
  - Fast "vut" from z=0.30 to z=0.0.
- Pass-by:
  - Carries phase-2 velocity into negative z until clamp `z=-1.2`.
- `_phase_split_t()` computes the time split from distance split and speed ratio.

Lifecycle:

- `check_hit` fires once at `hit_frame`, but target remains `flying`.
- Relax obstacles do not disappear on hit.
- They continue past camera and die once pass-by reaches the analytical exit pad.
- `is_dead` returns true after target has effectively moved off-screen.

Dodge timing:

- `dodge_frame = hit_frame + round(travel * DODGE_OFFSET)`.
- Current constants:
  - `DODGE_OFFSET_LOW = +0.01`.
  - `DODGE_OFFSET_HIGH = +0.064`.
  - `DODGE_HOLD_FRAC = 0.04`.
- LOW:
  - Jump starts just after z=0, when the ground slab is at the hit-zone edge.
  - This was changed because jumping earlier looked too soon after increasing "vut" speed.
- HIGH:
  - Squat starts after the overhead bar has started passing, roughly when top 1/3 is obscured.
- Hold:
  - Stickman holds jump/squat very briefly, then returns immediately to neutral.
  - Current hold is `0.04 * travel`, about 7 frames for `travel=180`.

Camera bob:

- `_relax_camera_dy(...)` computes vertical post-render shift.
- LOW jump:
  - Canvas shifts down, making camera feel like it rises.
- HIGH squat:
  - Canvas shifts up, making camera feel like it drops.
- Envelope:
  - Ramp in over `_RELAX_BOB_WINDOW_F = 8` frames.
  - Hold until `dodge_end_frame`.
  - Ramp out over same window.
- Peak:
  - `_RELAX_BOB_HEIGHT_FRAC = 0.08` of screen height.
- Applied after bloom and before HUDs so scene moves but UI remains pinned.

Relax obstacle visual style:

- Source of truth: `.cursor/rules/obstacle-visual-style.mdc`.
- All current/future relax-style obstacles must follow neon 3D brick system.

Shared palette:

- Front face: `CLR_WALL_PINK`.
- Top/bottom secondary face: `(140, 25, 190)`.
- Hidden/side face: `(70, 25, 95)`.
- Groove/rib color: `(15, 5, 25)`.
- No white outlines.
- No white rim/highlight separating faces.

Shared geometry:

- Full runway width, centered by `_span_x`.
- Back depth offset: `z_off_back = 0.10`.
- Front face is a rounded rectangle:
  - `_rounded_rect_points(x_l, y_t, x_r, y_b, r, n=10)`.
  - `r = max(3, int(min(width, height) * 0.22))`.
- Fixed 24-line groove system:
  - `N_STRIPES = 24`.
  - `rib_w = max(2, int(2.4 * scale))`.
  - Same x positions reused on both visible faces.
  - Stripes are clipped to rounded front mask.

LOW visual:

- `kind='low'`.
- Bottom sits on `cam.floor_y(z)`.
- Height is `LOW_HEIGHT_FRAC = 0.07` of tunnel height.
- Visible secondary face is TOP face.
- Top face is purple trapezoid with diagonal near-black ribs:
  - `(sxf, y_t) -> (sxb, y_tb)`.
- Front face is magenta rounded rect with vertical black stripes:
  - `(sxf, y_t) -> (sxf, y_b)`.

HIGH visual:

- `kind='high'`.
- Center anchored above horizon:
  - `HIGH_HORIZON_OFFSET_FRAC = 0.26`.
  - `HIGH_HEIGHT_FRAC = 0.35`.
- During pass-by, anchor uses steeper envelope so bar exits top of viewport quickly.
- Visible secondary face is BOTTOM face because camera is below it.
- Bottom face is purple trapezoid with diagonal near-black ribs:
  - `(sxf, y_b) -> (sxb, y_bb)`.
- Front face is magenta rounded rect with vertical black stripes.
- Hidden top face is dark plum fill only.

Relax VFX:

- `RelaxTarget` is skipped in hit VFX loop.
- No particles.
- No combo increment.
- No viewport shake.
- Feedback is only stickman dodge + camera bob + visual pass-by.

Stickman:

- Solo action library: `ACTIONS['relax']`.
- `JP`: `RELAX_JUMP`, recovers to `RELAX_STAND`.
- `SQ`: `RELAX_SQUAT`, recovers to `RELAX_STAND`.
- Combo action also supports `JP` and `SQ`, recovering to `GUARD_BOTH`.

## Stickman Event System

`StickmanHUD.set_beat_events(events, fps)` turns scheduled events into pose waypoints.

Event tuple shapes:

- `(t_hit, kind, lean_scale)`.
- `(t_hit, kind, lean_scale, sustain)`.

Important behavior:

- `lean_scale` multiplies pose lean/drop, mainly to make outer lanes wider.
- `prelift` sequences are inserted before `t_hit` for dance-type actions.
- If sustain is present:
  - Stickman holds the strike pose for the sustain window.
  - `holds_end` can animate from start hold pose to end hold pose, used by horizontal line sweeps.
- Hold truncation exists to ensure recovery before the next event when events are too close.
- This is important for relax so `SQ -> STAND -> JP` or `JP -> STAND -> SQ` remains readable.

## VFX And HUD Rules

Bloom:

- `gpu_glow(canvas, sigma=9.0, gain=0.32)` is applied after particles and before UI.
- CuPy acceleration is used if available.

Hit VFX:

- Punch/Line:
  - Air particles.
  - Viewport shake intensity around `1.0`.
  - Combo increments.
- Dance:
  - Floor particles.
  - Viewport shake intensity around `0.9`.
  - Combo increments.
- Wall:
  - Center burst.
  - Strong shake.
  - Combo increments.
- Relax:
  - No particles.
  - No shake.
  - No combo.

Combo HUD:

- Shows combo number top-right.
- Shows `GOOD` badge after each non-relax hit.

Viewport:

- Idle panels are dim grey outlines.
- Hit panels shake and glow amber by default.
- `--panel_color` can override neon color.

## CLI Options Relevant To Render Modes

General:

- `--mode punch|dance|line|relax|comma,list`
- `--travel`
- `--speed`
- `--density`
- `--max_per_lane`
- `--lanes`
- `--beat_source tempo|beat|onset`
- `--bpm`
- `--beat_sens`
- `--beat_subdiv`
- `--beat_min_gap`
- `--bloom`
- `--floor_panels`
- `--stickman`
- `--export_events`

Punch/custom cube:

- `--cube_radius`
- `--cube_image`
- `--cube_image_left`
- `--cube_image_right`
- `--cube_model`
- `--cube_model_left`
- `--cube_model_right`
- `--mesh_wireframe`
- `--cube_color_left`
- `--cube_color_right`

Mode-specific:

- `--punch_pair_cycle`
- `--dance_pair_cycle`
- `--line_beats`
- `--line_debug`
- `--line_zigzag vertical|horizontal`
- `--relax_interval`

## Design Rules For Future Changes

1. Do not change scheduler timing and render timing independently. Stickman events must come from scheduled targets, not raw beat frames.
2. If a target persists after hit, override lifecycle explicitly like `LineTarget` or `RelaxTarget`.
3. For combo mode, event kind prefixes are required so `ACTIONS['combo']` can choose the correct motion.
4. Do not add hit particles/combo/shake to relax obstacles. They are dodge objects.
5. New relax-style obstacles must follow `.cursor/rules/obstacle-visual-style.mdc`.
6. If modifying `RelaxTarget.PHASE_SPEED_RATIO`, re-check:
   - `DODGE_OFFSET_LOW`.
   - `DODGE_OFFSET_HIGH`.
   - `DODGE_HOLD_FRAC`.
   - fixed-delay `exit_pad`.
7. If modifying line block timings, keep `block_hit_frames`, `block_back_frames`, and `block_shrink_dur` consistent. The seamless chain depends on shared front/back timing.
8. If modifying dance stomp timing, keep prelift readable: lift, shift, stomp, recover.
9. If modifying punch visuals, preserve the 3D read: yaw, shaded faces, rounded silhouette, glow, and fist icon.
10. If adding new mode combinations, update both `rhythm.py` target event emission and `stickman.py` `ACTIONS['combo']`.
