"""
stickman.py — Standalone beat-synced stickman effect.

Purpose
-------
Draw an animated 2-D stickman whose poses lock to beats detected in an
audio track.  The module is fully self-contained: it can be imported by
other effects (e.g. ``rhythm.py`` overlays the stickman as a HUD on top
of the tunnel scene) OR executed directly as a CLI to render a clean
stickman-only video.

Actions
-------
All behavior is packaged in the ``ACTIONS`` registry.  Each entry
describes ONE action (pose library + beat-to-pose mapping).  Today we
ship a single action — ``"punch"`` — which contains the boxer poses
originally embedded in ``rhythm.py``.  Future actions (dance, kick,
wave, etc.) can be added by dropping another entry with the same
shape::

    ACTIONS['dance'] = {
        'poses': {pose_name: {joint_angles_dict}, ...},
        'intro': [pose_name, ...],
        'guard_default': pose_name,
        'strikes': {'L': [...], 'R': [...], 'W': [...]},
        'recover': {'L': pose_name, 'R': pose_name, 'W': pose_name},
    }

Pose dicts use the shared angle convention: 0°=down, 90°=right,
180°=up, 270°=left (screen-coords, y-down).  Optional keys: ``lean``
(lateral hip shift, ref-pixels), ``drop`` (body-crouch, ref-pixels),
``punch`` (``'L'`` / ``'R'`` — flags the active strike side so the
drawing code can enlarge the fist + add a motion streak).
"""

import math
import numpy as np
import cv2
import argparse
import platform
import time
import sys
import subprocess
import shlex

try:
    import cupy as cp
    cp.array([1])
    _CUPY = True
    print("[GPU] CuPy detected – NVENC encoder will be used if available")
except Exception:
    _CUPY = False

try:
    import librosa
    _HAVE_LIBROSA = True
except Exception:
    _HAVE_LIBROSA = False

try:
    import ffmpeg
    _HAVE_FFMPEG_PY = True
except Exception:
    _HAVE_FFMPEG_PY = False

try:
    from authorization import authourize_user
except Exception:
    def authourize_user(_token, _url):
        return True

IS_MAC = platform.system() == 'Darwin'

# ── Constants ────────────────────────────────────────────────────────────────
FPS         = 30
HOP_LENGTH  = 512
CLR_WHITE   = (250, 250, 250)
CLR_BG      = (6, 4, 10)


def _seg(canvas, p0, p1, col, t):
    cv2.line(canvas, p0, p1, col, t, lineType=cv2.LINE_AA)


def _parse_color(s):
    """Accept '#RRGGBB', 'RRGGBB' or 'R,G,B' → BGR tuple for OpenCV."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if s.startswith('#'):
        s = s[1:]
    if len(s) == 6 and all(c in '0123456789abcdefABCDEF' for c in s):
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (b, g, r)
    if ',' in s:
        parts = [p.strip() for p in s.split(',')]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            r, g, b = (max(0, min(255, int(p))) for p in parts)
            return (b, g, r)
    raise ValueError(
        f"Invalid color '{s}'. Use '#RRGGBB', 'RRGGBB', or 'R,G,B'.")


# ── ACTION library ───────────────────────────────────────────────────────────
#
# 'punch' action — boxer-style, originally built for the rhythm game.
# Angle convention: 0°=down, 90°=right, 180°=up, 270°=left (y-down).
# Keys: la_u/la_f (left upper arm / forearm), ra_u/ra_f (right),
#       ll_u/ll_f (left thigh / shin), rl_u/rl_f (right).
#       lean (hip lateral shift, ref-px), drop (body crouch, ref-px),
#       punch ('L' or 'R' — active strike side for fist scaling + streak).
# ----------------------------------------------------------------------------
_PUNCH_POSES: dict[str, dict] = {
    # Intro / warm-up
    'INTRO_WAVE': dict(la_u=190, la_f=185, ra_u=  0, ra_f=  5,
                       ll_u=350, ll_f=  5, rl_u= 10, rl_f=355,
                       lean= 0, drop=0),
    'READY':      dict(la_u=335, la_f=340, ra_u= 25, ra_f= 20,
                       ll_u=335, ll_f=355, rl_u= 25, rl_f=  5,
                       lean= 0, drop=0),

    # Guards: elbows flared OUT, fists off the face, LEGS SPLAYED
    'GUARD_BOTH': dict(la_u=305, la_f=170, ra_u= 55, ra_f=190,
                       ll_u=328, ll_f=350, rl_u= 32, rl_f= 10,
                       lean= 0, drop=6),
    'GUARD_L':    dict(la_u=300, la_f=168, ra_u= 55, ra_f=190,
                       ll_u=325, ll_f=348, rl_u= 32, rl_f= 10,
                       lean=-3, drop=6),
    'GUARD_R':    dict(la_u=305, la_f=170, ra_u= 60, ra_f=192,
                       ll_u=328, ll_f=350, rl_u= 35, rl_f= 12,
                       lean= 3, drop=6),

    # Punches: arm committed OUTWARD, fist enlarged in draw()
    'JAB_L':      dict(la_u=260, la_f=255, ra_u= 55, ra_f=190,
                       ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                       lean=-4, drop=3, punch='L'),
    'JAB_R':      dict(la_u=305, la_f=170, ra_u=100, ra_f=105,
                       ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                       lean= 4, drop=3, punch='R'),

    'CROSS_L':    dict(la_u=235, la_f=225, ra_u= 55, ra_f=190,
                       ll_u=320, ll_f=342, rl_u= 32, rl_f= 15,
                       lean= 3, drop=3, punch='L'),
    'CROSS_R':    dict(la_u=305, la_f=170, ra_u=125, ra_f=135,
                       ll_u=328, ll_f=345, rl_u= 40, rl_f= 18,
                       lean=-3, drop=3, punch='R'),

    'HOOK_L':     dict(la_u=275, la_f=215, ra_u= 55, ra_f=190,
                       ll_u=322, ll_f=345, rl_u= 32, rl_f= 12,
                       lean=-3, drop=3, punch='L'),
    'HOOK_R':     dict(la_u=305, la_f=170, ra_u= 85, ra_f=145,
                       ll_u=328, ll_f=348, rl_u= 38, rl_f= 15,
                       lean= 3, drop=3, punch='R'),

    'UPPERCUT_L': dict(la_u=195, la_f=195, ra_u= 55, ra_f=190,
                       ll_u=315, ll_f=340, rl_u= 35, rl_f= 20,
                       lean=-4, drop=8, punch='L'),
    'UPPERCUT_R': dict(la_u=305, la_f=170, ra_u=165, ra_f=165,
                       ll_u=325, ll_f=340, rl_u= 45, rl_f= 20,
                       lean= 4, drop=8, punch='R'),

    'DOUBLE_UP':  dict(la_u=175, la_f=175, ra_u=185, ra_f=185,
                       ll_u=325, ll_f=345, rl_u= 35, rl_f= 15,
                       lean= 0, drop=0),

    # ── "Both hands to one side" punches (paired-spawn on adjacent
    # same-side lanes, e.g. --lanes 1,2 or --lanes 3,4).  The stickman
    # commits BOTH fists to the same side: the near arm extends like a
    # jab, the far arm crosses the chest and reaches in the same
    # direction but slightly higher so the two fists don't overlap.
    # `punch='B'` lights BOTH fists (see `_blend`), so the motion-streak
    # and enlarged-fist effect fire on both hands simultaneously.
    'DOUBLE_LEFT': dict(la_u=258, la_f=250, ra_u=285, ra_f=270,
                        ll_u=320, ll_f=342, rl_u= 32, rl_f= 12,
                        lean=-6, drop=3, punch='B'),
    'DOUBLE_RIGHT': dict(la_u= 75, la_f= 90, ra_u=102, ra_f=110,
                         ll_u=328, ll_f=348, rl_u= 40, rl_f= 18,
                         lean= 6, drop=3, punch='B'),

    # ── HOLD poses for 'line' (long-note) strikes ────────────────────
    # Extended arm committed fully outward and FROZEN — no elbow bend
    # — so the stickman visibly "holds" the punch while the bar slides
    # past the camera.  `punch='L'/'R'` keeps the fist enlarged and
    # lights a streak tail for the full hold duration.  Elbow is
    # locked straight (la_f matches la_u on L side; symmetric on R)
    # so the pose reads as a sustained extension rather than a jab.
    'HOLD_L':     dict(la_u=270, la_f=270, ra_u= 55, ra_f=190,
                       ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                       lean=-6, drop=2, punch='L'),
    'HOLD_R':     dict(la_u=305, la_f=170, ra_u= 90, ra_f= 90,
                       ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                       lean= 6, drop=2, punch='R'),

    # ── HOLD UP/DOWN — arm direction matches zigzag block height ──────
    # UP  = block is above horizon → arm extends upward-forward.
    # DOWN = block is below horizon → arm extends downward-forward.
    # Angle convention: 0°=down, 90°=right, 180°=up, 270°=left.
    'HOLD_L_UP':   dict(la_u=210, la_f=210, ra_u= 55, ra_f=190,
                        ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                        lean=-6, drop=0, punch='L'),
    'HOLD_L_DOWN': dict(la_u=318, la_f=318, ra_u= 55, ra_f=190,
                        ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                        lean=-6, drop=5, punch='L'),
    'HOLD_R_UP':   dict(la_u=305, la_f=170, ra_u=152, ra_f=152,
                        ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                        lean= 6, drop=0, punch='R'),
    'HOLD_R_DOWN': dict(la_u=305, la_f=170, ra_u= 42, ra_f= 42,
                        ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                        lean= 6, drop=5, punch='R'),

    # ── HORIZONTAL SWEEP — arm tracks the chain's lane-to-lane direction.
    # Used by the 'line' action when the scheduler emits ZSLR / ZSRL
    # (``line_zigzag=horizontal``).  The strike pose is placed at t_hit
    # (block head arrives) and the matching HOLD-END pose is placed at
    # t_hit + sustain (block tail arrives), so the tween engine
    # animates a smooth arm sweep across the top of the screen in
    # lock-step with the block's head → tail direction.
    #
    # Angle convention: 0°=down, 90°=right, 180°=up, 270°=left.
    # 225° = upper-LEFT (between 180 and 270), 135° = upper-RIGHT
    # (between 90 and 180).  Interpolating 225° → 135° or 135° → 225°
    # both pass through 180° (straight up), producing a clean arc.
    # Elbow is locked straight (la_u == la_f on L side, ra_u == ra_f
    # on R side) so the sweep reads as a rigid pointing arm instead of
    # a floppy jab.  ``punch`` keeps the fist enlarged / streak-lit
    # for the full sustain window.
    'SWEEP_L_LEFT':  dict(la_u=225, la_f=225, ra_u= 55, ra_f=190,
                          ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                          lean=-4, drop=0, punch='L'),
    'SWEEP_L_RIGHT': dict(la_u=135, la_f=135, ra_u= 55, ra_f=190,
                          ll_u=322, ll_f=345, rl_u= 30, rl_f= 12,
                          lean= 4, drop=0, punch='L'),
    'SWEEP_R_RIGHT': dict(la_u=305, la_f=170, ra_u=135, ra_f=135,
                          ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                          lean= 4, drop=0, punch='R'),
    'SWEEP_R_LEFT':  dict(la_u=305, la_f=170, ra_u=225, ra_f=225,
                          ll_u=330, ll_f=348, rl_u= 38, rl_f= 15,
                          lean=-4, drop=0, punch='R'),
}


ACTIONS: dict[str, dict] = {
    'punch': {
        'poses':         _PUNCH_POSES,
        'intro':         ['INTRO_WAVE', 'READY', 'GUARD_BOTH'],
        'guard_default': 'GUARD_BOTH',
        'strikes': {
            # cycle through so repeated same-side hits use DIFFERENT strikes
            'L': ['JAB_L', 'CROSS_L', 'HOOK_L', 'UPPERCUT_L'],
            'R': ['JAB_R', 'CROSS_R', 'HOOK_R', 'UPPERCUT_R'],
            'W': ['DOUBLE_UP'],
            # Double-hand strikes (paired-spawn adjacent same-side lanes).
            'LL': ['DOUBLE_LEFT'],
            'RR': ['DOUBLE_RIGHT'],
        },
        'recover': {'L': 'GUARD_L', 'R': 'GUARD_R', 'W': 'GUARD_BOTH',
                    'LL': 'GUARD_L', 'RR': 'GUARD_R'},
    },
}


# ── 'line' action — long-note "hold" strikes ────────────────────────────────
#
# Pairs with rhythm.py --mode line (elongated rail-style punch bars).  The
# stickman freezes the punch in its extended position for the duration of
# the bar's pass-through — ``set_beat_events`` reads the 4th tuple element
# (``sustain`` seconds) from the scheduler and keeps the ``HOLD_L``/``HOLD_R``
# pose held for that long before lowering into the recover waypoint.
ACTIONS['line'] = {
    'poses':         _PUNCH_POSES,
    'intro':         ['INTRO_WAVE', 'READY', 'GUARD_BOTH'],
    'guard_default': 'GUARD_BOTH',
    'strikes': {
        'ZL':  ['HOLD_L'],
        'ZR':  ['HOLD_R'],
        # Per-block directional holds: U = block above horizon, D = below.
        'ZLU': ['HOLD_L_UP'],
        'ZLD': ['HOLD_L_DOWN'],
        'ZRU': ['HOLD_R_UP'],
        'ZRD': ['HOLD_R_DOWN'],
        # Horizontal sweep (line_zigzag=horizontal).  BOTH kinds use the
        # SAME (left) arm so the sweep reads as a continuous pendulum
        # across successive blocks — the end pose of one block IS the
        # start pose of the next, producing a seamless arm motion with
        # no inter-block pose-snap.
        #   ZSLR : la_u 225°(up-LEFT) → 135°(up-RIGHT)   (block L→R)
        #   ZSRL : la_u 135°(up-RIGHT) → 225°(up-LEFT)   (block R→L)
        # Only the START pose is listed here; the matching END pose
        # lives in ``holds_end`` below and is used by set_beat_events
        # when the event carries sustain > 0.
        'ZSLR': ['SWEEP_L_LEFT'],
        'ZSRL': ['SWEEP_L_RIGHT'],
        'W':   ['DOUBLE_UP'],
    },
    # Optional per-kind hold-end pose used by set_beat_events when
    # ``sustain > 0``.  If a kind has an entry here, the tween engine
    # animates arm-pose-from-start → arm-pose-from-end over the sustain
    # window (instead of the legacy "same pose frozen" behaviour).
    'holds_end': {
        'ZSLR': 'SWEEP_L_RIGHT',
        'ZSRL': 'SWEEP_L_LEFT',
    },
    'recover': {
        'ZL': 'GUARD_L', 'ZR': 'GUARD_R',
        'ZLU': 'GUARD_L', 'ZLD': 'GUARD_L',
        'ZRU': 'GUARD_R', 'ZRD': 'GUARD_R',
        # Both sweeps use the LEFT arm, so both recover toward GUARD_L.
        'ZSLR': 'GUARD_L', 'ZSRL': 'GUARD_L',
        'W': 'GUARD_BOTH',
    },
}


# ── 'dance' action — stomp feet on floor blocks ─────────────────────────────
#
# Designed to pair with rhythm.py --mode dance (flat slabs sliding along the
# floor instead of air cubes).  The beat moment is the FOOT-PLANT: the foot
# lands hard at t_hit while the opposite knee is raised a bit for balance.
# Arms swing loosely for rhythm.  'W' (wall) becomes a two-feet jump dodge.
# Angle convention identical to _PUNCH_POSES: 0°=down, 90°=right, 180°=up,
# 270°=left.  `lean` shifts hips horizontally, `drop` lowers the body.
_DANCE_POSES: dict[str, dict] = {
    # Intro / idle stance – arms up in a loose dance guard, feet together.
    'READY_DANCE': dict(la_u=295, la_f=235, ra_u= 65, ra_f=125,
                        ll_u=355, ll_f=  5, rl_u=  5, rl_f=355,
                        lean= 0, drop=0),
    # "Idle" – neutral pose reused as the default guard between beats.
    'IDLE_DANCE':  dict(la_u=320, la_f=330, ra_u= 40, ra_f= 30,
                        ll_u=355, ll_f=  5, rl_u=  5, rl_f=355,
                        lean= 0, drop=2),

    # --- THREE-PHASE STOMP: LIFT → SHIFT → STOMP → RECOVER -----------------
    # Physically-readable side-stomp in the style of dance games.  The
    # KEY visual is the STEPPING leg visibly lifting off the floor —
    # knee raised, foot dangling down (NOT tucked behind), while the
    # other leg stays PLANTED.  This sells "nhấc chân lên" clearly.
    #
    #   1) LIFT_L / LIFT_R — step-side leg's knee is raised HIGH (thigh
    #      tilted ~55° out, shin angled so foot hangs 20 ref-px above
    #      the floor), the OTHER leg stays nearly vertical (planted).
    #      Body weight shifts slightly onto the support foot (lean
    #      away from the lifted leg) and rises ~2 ref-px.
    #   2) SHIFT_L / SHIFT_R — lifted leg extends outward (foot still
    #      airborne ~12 ref-px above floor, reaches ~26 ref-px laterally
    #      from hip).  Hip starts to shift toward the target side in
    #      anticipation of landing.
    #   3) STOMP_L / STOMP_R — BOTH feet planted in a WIDE symmetric
    #      stance (thighs splay ~40° each, shins near-vertical, both
    #      ankles at the floor line).  Hip lean + lean_scale shifts
    #      the body over the striking foot without deforming the legs.
    #
    # Leg-angle reminder: 0°=down, 90°=right, 180°=up, 270°=left.

    # ── Phase 1: LIFT — knee raised on ONE side, other leg planted. ─────
    # Left step-out: LEFT knee up, RIGHT foot planted.
    'LIFT_L':      dict(la_u=290, la_f=285, ra_u= 75, ra_f=100,
                        ll_u=305, ll_f= 20, rl_u=  3, rl_f=357,
                        lean= 2, drop=-2),
    # Right step-out: RIGHT knee up, LEFT foot planted (mirror).
    'LIFT_R':      dict(la_u=285, la_f=260, ra_u= 70, ra_f= 75,
                        ll_u=357, ll_f=  3, rl_u= 55, rl_f=340,
                        lean=-2, drop=-2),
    # W (jump dodge): both knees bent + rise = load for the jump.
    'LIFT_DANCE':  dict(la_u=325, la_f=335, ra_u= 35, ra_f= 25,
                        ll_u=345, ll_f= 15, rl_u= 15, rl_f=345,
                        lean= 0, drop=-12),

    # ── Phase 2: SHIFT — stepping leg extends out, foot still airborne.
    # Hip begins transferring toward the target side.
    'SHIFT_L':     dict(la_u=300, la_f=330, ra_u= 50, ra_f= 85,
                        ll_u=315, ll_f=  5, rl_u=  5, rl_f=355,
                        lean=-3, drop=-3),
    'SHIFT_R':     dict(la_u=310, la_f=275, ra_u= 60, ra_f= 30,
                        ll_u=355, ll_f=  5, rl_u= 45, rl_f=355,
                        lean= 3, drop=-3),
    # SHIFT for W: hands rise, legs spread mid-air for two-foot landing.
    'SHIFT_W':     dict(la_u=210, la_f=195, ra_u=150, ra_f=165,
                        ll_u=340, ll_f= 10, rl_u= 20, rl_f=350,
                        lean= 0, drop=-8),

    # ── Phase 3: STOMP — body lands, WIDE planted symmetric stance.
    # Both thighs splay outward equally; shins compensate near vertical
    # so both ankles land at the floor line.  lean shifts the hip
    # toward the struck side without deforming the leg geometry.
    'STOMP_L':     dict(la_u=310, la_f=350, ra_u= 70, ra_f= 95,
                        ll_u=320, ll_f=  5, rl_u= 40, rl_f=355,
                        lean=-5, drop=10),
    'STOMP_L2':    dict(la_u=320, la_f=300, ra_u= 55, ra_f=115,
                        ll_u=315, ll_f=  8, rl_u= 45, rl_f=352,
                        lean=-7, drop=11),

    'STOMP_R':     dict(la_u=290, la_f=265, ra_u= 50, ra_f= 10,
                        ll_u=320, ll_f=  5, rl_u= 40, rl_f=355,
                        lean= 5, drop=10),
    'STOMP_R2':    dict(la_u=305, la_f=245, ra_u= 40, ra_f= 60,
                        ll_u=315, ll_f=  8, rl_u= 45, rl_f=352,
                        lean= 7, drop=11),

    # Wall / jump dodge — two-foot landing in a wide planted stance.
    'JUMP':        dict(la_u=175, la_f=175, ra_u=185, ra_f=185,
                        ll_u=320, ll_f=  5, rl_u= 40, rl_f=355,
                        lean= 0, drop=10),

    # ── Paired-dance "nhảy chụm chân" — feet-together side jump ─────────
    # Triggered every 3rd dance beat when --lanes picks 2 adjacent same-
    # side lanes (so both tiles spawn together).  Chain:
    #   LIFT_DANCE  (both knees tuck, body rises)
    #     → FEET_SHIFT_L/R  (airborne, hips shifted toward target side,
    #                        feet still CLOSE together – not splayed)
    #     → FEET_STOMP_L/R  (both feet land together on the struck side,
    #                        strong lean, drop=11 for the slam)
    # Legs stay almost vertical (feet together) to clearly read as
    # "chụm" — contrast with STOMP_L/R which splay into a wide stance.
    'FEET_SHIFT_L': dict(la_u=315, la_f=315, ra_u= 45, ra_f= 45,
                         ll_u=355, ll_f=  5, rl_u=  0, rl_f=355,
                         lean=-5, drop=-7),
    'FEET_SHIFT_R': dict(la_u=315, la_f=315, ra_u= 45, ra_f= 45,
                         ll_u=360, ll_f=  5, rl_u=  5, rl_f=355,
                         lean= 5, drop=-7),
    'FEET_STOMP_L': dict(la_u=305, la_f=325, ra_u= 55, ra_f= 35,
                         ll_u=358, ll_f=  3, rl_u=  2, rl_f=357,
                         lean=-8, drop=11),
    'FEET_STOMP_R': dict(la_u=305, la_f=325, ra_u= 55, ra_f= 35,
                         ll_u=358, ll_f=  3, rl_u=  2, rl_f=357,
                         lean= 8, drop=11),

    # Post-stomp recovery: stepped-out foot pulls back under hip, body
    # re-centers for a clean transition to IDLE_DANCE.
    'RECOVER_L':   dict(la_u=325, la_f=335, ra_u= 55, ra_f= 85,
                        ll_u=355, ll_f=  5, rl_u=  5, rl_f=355,
                        lean=-1, drop= 2),
    'RECOVER_R':   dict(la_u=305, la_f=275, ra_u= 35, ra_f= 25,
                        ll_u=355, ll_f=  5, rl_u=  5, rl_f=355,
                        lean= 1, drop= 2),
}


ACTIONS['dance'] = {
    'poses':         _DANCE_POSES,
    'intro':         ['READY_DANCE', 'IDLE_DANCE'],
    'guard_default': 'IDLE_DANCE',
    'strikes': {
        # cycle so repeated same-side stomps alternate variant
        'L':  ['STOMP_L', 'STOMP_L2'],
        'R':  ['STOMP_R', 'STOMP_R2'],
        'W':  ['JUMP'],
        # Paired-dance side-jump (feet together) — fires every 3rd dance
        # beat when --lanes picks 2 adjacent same-side lanes.
        'JL': ['FEET_STOMP_L'],
        'JR': ['FEET_STOMP_R'],
    },
    'recover': {'L': 'RECOVER_L', 'R': 'RECOVER_R', 'W': 'IDLE_DANCE',
                'JL': 'RECOVER_L', 'JR': 'RECOVER_R'},
    # Optional anticipation chain played BEFORE the strike pose.  Each
    # entry can be a single pose name OR a LIST of pose names that are
    # distributed evenly across the pre-beat window.  For dance we use:
    #   LIFT (body rises, legs straight) → SHIFT (hip slid sideways
    #   while still airborne) → [STOMP @ t_hit = body lands wide-planted]
    # Handled by ``set_beat_events``: waypoints are placed between the
    # previous recover (or intro tail) and ``t_hit - tween*0.8``; any
    # intro/guard waypoints that would fall inside the anticipation
    # window are popped so the body doesn't snap back to IDLE between
    # phases.  Omit this key on actions that don't need anticipation
    # (e.g. 'punch' renders as a single-phase strike).
    'prelift':      {'L':  ['LIFT_L', 'SHIFT_L'],
                     'R':  ['LIFT_R', 'SHIFT_R'],
                     'W':  ['LIFT_DANCE', 'SHIFT_W'],
                     # Feet-together jump: tuck up, drift toward target
                     # side still-tucked, then slam down both feet.
                     'JL': ['LIFT_DANCE', 'FEET_SHIFT_L'],
                     'JR': ['LIFT_DANCE', 'FEET_SHIFT_R']},
    # Total anticipation time (seconds) spread evenly across the
    # prelift chain.  Clamped to fit the actual gap before t_hit.
    'prelift_time': 0.26,
}


# ── 'combo' action — interleaved punch + dance ──────────────────────────────
#
# Pairs with rhythm.py's ``--mode punch,dance`` (combo mode): punch cubes
# and dance tiles alternate per beat, and the stickman has to BOTH punch
# mid-air blocks and stomp floor slabs in the same performance.  Events
# carry a type-prefixed kind so this action knows which motion to use:
#
#   'PL' / 'PR'  → punch cube, use JAB/CROSS/HOOK/UPPERCUT (no prelift)
#   'LL' / 'RR'  → paired-spawn double-hand punch (if user combined
#                  --mode punch,dance with --lanes 1,2 / 3,4)
#   'DL' / 'DR'  → dance tile, play LIFT → SHIFT → STOMP prelift chain
#   'W'          → wall → two-fist guard up (shares punch's DOUBLE_UP)
#
# Pose dict merges both libraries (no name collisions between punch and
# dance poses).  Intro uses the punch wave so the stickman enters on the
# clearly-framed guard stance even when the first beat is a dance beat.
_COMBO_POSES: dict[str, dict] = {**_PUNCH_POSES, **_DANCE_POSES}

ACTIONS['combo'] = {
    'poses':         _COMBO_POSES,
    'intro':         ['INTRO_WAVE', 'READY', 'GUARD_BOTH'],
    'guard_default': 'GUARD_BOTH',
    'strikes': {
        # Punch-beat strikes (prefix 'P').
        'PL': ['JAB_L', 'CROSS_L', 'HOOK_L', 'UPPERCUT_L'],
        'PR': ['JAB_R', 'CROSS_R', 'HOOK_R', 'UPPERCUT_R'],
        'LL': ['DOUBLE_LEFT'],
        'RR': ['DOUBLE_RIGHT'],
        'W':  ['DOUBLE_UP'],
        # Dance-beat strikes (prefix 'D') — full LIFT/SHIFT/STOMP chain
        # is configured in `prelift` below, `strikes` is the final land.
        'DL': ['STOMP_L', 'STOMP_L2'],
        'DR': ['STOMP_R', 'STOMP_R2'],
        # Paired-dance feet-together side-jump (dance-paired beats).
        'JL': ['FEET_STOMP_L'],
        'JR': ['FEET_STOMP_R'],
        # Line-beat strikes (prefix 'Z') — held extension.  Stickman's
        # 4th event tuple element (`sustain`) keeps this pose locked for
        # the bar's hold window before recovering.
        'ZL':  ['HOLD_L'],
        'ZR':  ['HOLD_R'],
        'ZLU': ['HOLD_L_UP'],
        'ZLD': ['HOLD_L_DOWN'],
        'ZRU': ['HOLD_R_UP'],
        'ZRD': ['HOLD_R_DOWN'],
        # Horizontal line-sweep (line_zigzag=horizontal) also works in
        # combo mode when the beat cycle includes a line sub-mode.
        # Both sweeps use the LEFT arm so the motion reads as a
        # continuous pendulum across blocks (end-of-block-N pose IS
        # start-of-block-N+1 pose).
        'ZSLR': ['SWEEP_L_LEFT'],
        'ZSRL': ['SWEEP_L_RIGHT'],
    },
    'holds_end': {
        'ZSLR': 'SWEEP_L_RIGHT',
        'ZSRL': 'SWEEP_L_LEFT',
    },
    'prelift': {
        # Only dance beats get an anticipation chain; punch beats snap
        # from guard → strike so the fists feel sharp.
        'DL': ['LIFT_L', 'SHIFT_L'],
        'DR': ['LIFT_R', 'SHIFT_R'],
        'JL': ['LIFT_DANCE', 'FEET_SHIFT_L'],
        'JR': ['LIFT_DANCE', 'FEET_SHIFT_R'],
    },
    'prelift_time': 0.26,
    'recover': {
        'PL': 'GUARD_L', 'PR': 'GUARD_R',
        'LL': 'GUARD_L', 'RR': 'GUARD_R',
        'W':  'GUARD_BOTH',
        'DL': 'RECOVER_L', 'DR': 'RECOVER_R',
        'JL': 'RECOVER_L', 'JR': 'RECOVER_R',
        'ZL': 'GUARD_L', 'ZR': 'GUARD_R',
        'ZLU': 'GUARD_L', 'ZLD': 'GUARD_L',
        'ZRU': 'GUARD_R', 'ZRD': 'GUARD_R',
        # Both sweeps use the LEFT arm → recover toward GUARD_L.
        'ZSLR': 'GUARD_L', 'ZSRL': 'GUARD_L',
    },
}


# ── StickmanHUD (drawable) ───────────────────────────────────────────────────
class StickmanHUD:
    """Beat-synced 2-D stickman with a pluggable action library.

    Constructor accepts either an object exposing ``.W`` and ``.H``
    (e.g. a ``PerspectiveCamera``), a ``(W, H)`` tuple, or the explicit
    ``W=`` / ``H=`` keyword arguments.  The drawing box defaults to a
    vertical strip on the left side of the frame (compatible with the
    rhythm-game HUD layout); pass ``box=(x, y, w, h)`` to place it
    elsewhere — e.g. centered for a standalone stickman video.
    """

    _REF_W, _REF_H = 260, 340
    _HIP_MID_REF   = (130, 180)

    # Asymmetric limb ratios: shorter upper-arm + longer forearm.
    # Lets the elbow flare WIDE while fists still reach chin level.
    _BONES_REF = dict(
        head_r=18, neck=15, torso=55,
        shoulder_half=17, hip_half=12,
        upper_arm=22, forearm=38,
        thigh=42, shin=46,
    )

    # Tight visual envelope of the rendered stickman, in REFERENCE coords.
    # Derived from the bone layout + worst-case pose extremes:
    #   vertical
    #     • head top  = hip_y - torso - neck - 2*head_r = 180-55-15-36 = 74
    #     • foot bot  = hip_y + thigh + shin + foot-stub ≈ 180+42+46+4 = 272
    #     + ~8 px margin for bob/breathing, drop-pose (UPPERCUT drop=8).
    #   horizontal
    #     • hook/cross punches send a hand to x≈48 or x≈214;
    #     • motion streak extends forearm*0.55 beyond the hand.
    #     + ~4 px margin for sway / fist radius.
    # Used by StickmanVisualizer --fit to crop tightly around the body.
    _BODY_ENVELOPE_REF = dict(x0=40, x1=220, y0=66, y1=282)

    _LINEAR_KEYS = ('lean', 'drop')
    _IGNORE_KEYS = ('punch',)

    # --------------------------------------------------------------- init
    def __init__(self, cam_or_size=None, action: str = 'punch', *,
                 W: int | None = None, H: int | None = None,
                 box: tuple | None = None,
                 color: tuple = CLR_WHITE,
                 fps: int = FPS):
        # Resolve canvas dims
        if cam_or_size is not None:
            if hasattr(cam_or_size, 'W') and hasattr(cam_or_size, 'H'):
                self.W, self.H = cam_or_size.W, cam_or_size.H
            else:
                self.W, self.H = cam_or_size
        elif W is not None and H is not None:
            self.W, self.H = W, H
        else:
            raise ValueError(
                "StickmanHUD: pass a camera / (W, H) tuple / W & H kwargs.")

        # Default draw box — left-column HUD (keeps rhythm.py behavior).
        if box is None:
            self.bx = int(self.W * 0.010)
            self.by = int(self.H * 0.09)
            self.bw = int(self.W * 0.135)
            self.bh = int(self.H * 0.54)
        else:
            self.bx, self.by, self.bw, self.bh = box

        sc = min(self.bw / self._REF_W, self.bh / self._REF_H)
        self._sc = sc
        self.B = {k: v * sc for k, v in self._BONES_REF.items()}

        self._hip_base_x = self.bx + self._HIP_MID_REF[0] * sc
        self._hip_base_y = self.by + self._HIP_MID_REF[1] * sc

        self.tk = max(2, int(round(sc * 4.4)))
        self._color = tuple(color)
        self._fps   = fps

        self.set_action(action)

        self._timeline:   list[tuple[float, str]] = [(0.0, self._intro_first())]
        # Parallel to `_timeline`: per-waypoint multiplier for `lean` (and
        # a softened proxy for `drop`).  Populated by `set_beat_events`.
        # Defaults to 1.0 everywhere so idle / guard / intro poses render
        # exactly as they always have.
        self._lean_scales: list[float] = [1.0]
        self._beat_times: list[float] = []
        self._tween_dur  = 0.16

    # --------------------------------------------------------------- action
    def set_action(self, action: str):
        if action not in ACTIONS:
            raise ValueError(
                f"Unknown action '{action}'. Available: {list(ACTIONS)}")
        self._action = action
        self._lib    = ACTIONS[action]
        self.POSES   = self._lib['poses']

    def _intro_first(self) -> str:
        intro = self._lib.get('intro') or []
        return intro[0] if intro else self._lib['guard_default']

    # ------------------------------------------------- beat timeline setup
    def set_beat_events(self,
                        events: list[tuple],
                        fps: int):
        """Build pose timeline from beat events.

        Accepts both legacy 2-tuples ``(hit_time_sec, kind)`` and newer
        3-tuples ``(hit_time_sec, kind, lean_scale)``.  ``kind`` picks
        which strike rotation to use – typically 'L' (left-side strike),
        'R' (right-side strike) or 'W' (wall / both-sides).  Unknown kinds
        are ignored.

        ``lean_scale`` (optional, default 1.0) is a per-event multiplier
        applied to the strike pose's lateral ``lean`` (and, softened, to
        ``drop``).  For 4-lane dance mode it lets the scheduler encode
        "reach wider on outer lanes": the rhythm renderer sets scale≈1.6
        for lanes 0/3 and ≈0.55 for inner lanes 1/2, so the stickman
        visibly takes a bigger step toward the far side.  The recovery
        waypoint is always emitted at scale=1.0 so the body returns
        cleanly to neutral between beats.
        """
        self._fps = fps
        lib       = self._lib
        strikes   = lib['strikes']
        recover   = lib['recover']
        guard_def = lib['guard_default']
        intro     = lib.get('intro') or []
        # Optional two-phase anticipation for stomp-style actions:
        # a "knee up" PRELIFT pose held briefly before the strike so the
        # beat impact reads as a real slam instead of a smooth morph.
        prelift_map  = lib.get('prelift') or {}
        prelift_time = float(lib.get('prelift_time', 0.0) or 0.0)
        # Optional per-kind hold-end pose.  When a kind has an entry here
        # AND the event carries ``sustain > 0``, the waypoint at
        # t_hit + sustain uses this END pose instead of repeating the
        # strike pose — the tween engine then interpolates between them
        # and the arm visibly SWEEPS across the screen during the hold.
        # Used by 'line' horizontal-zigzag mode (ZSLR / ZSRL) so the
        # stickman's arm tracks each block's lane-to-lane direction.
        holds_end = lib.get('holds_end') or {}

        valid_kinds = set(strikes.keys())
        # Each entry: (t_hit, kind, lean_scale, sustain).  Sustain ≥ 0
        # seconds; non-zero values extend the strike's dwell time before
        # the recovery waypoint (used by 'line' / hold-note strikes so
        # the stickman visibly HOLDS the punch while the bar slides past
        # the camera).  Legacy 2-tuple / 3-tuple events get sustain=0
        # which falls through to the default `hold` dwell.
        normalized: list[tuple[float, str, float, float]] = []
        for ev in events:
            sustain = 0.0
            if len(ev) == 4:
                t, k, s, sustain = ev
            elif len(ev) == 3:
                t, k, s = ev
            elif len(ev) == 2:
                t, k = ev
                s = 1.0
            else:
                continue
            if k not in valid_kinds:
                continue
            normalized.append((float(t), k, float(s), float(sustain)))
        normalized.sort(key=lambda e: e[0])

        if not normalized:
            self._timeline    = [(0.0, guard_def)]
            self._lean_scales = [1.0]
            self._beat_times  = []
            return

        first_beat = normalized[0][0]

        # Auto-compress the intro so the stickman finishes "entering" just
        # before the first beat.  If the song starts basically on beat-1,
        # skip the intro entirely.
        if intro:
            intro_budget = max(0.0, first_beat - 0.05)
            if intro_budget <= 0.01:
                tl: list[tuple[float, str]] = [(0.0, guard_def)]
                scales: list[float] = [1.0]
            else:
                scale = min(1.0, intro_budget / 1.8)
                tl = []
                scales = []
                n = len(intro)
                for i, pose_name in enumerate(intro):
                    t = (i / max(1, n - 1)) * 1.8 * scale if n > 1 else 0.0
                    tl.append((t, pose_name))
                    scales.append(1.0)
        else:
            tl = [(0.0, guard_def)]
            scales = [1.0]

        # Adaptive tween length — snappier on dense rhythms, smoother on sparse.
        # Widened 0.07–0.16 → 0.10–0.22 and coefficient 0.25 → 0.32 so
        # transitions read as smooth weight-shifts instead of snappy cuts.
        if len(normalized) >= 2:
            median_gap = float(np.median(np.diff([t for t, _, _, _ in normalized])))
            self._tween_dur = max(0.10, min(0.22, median_gap * 0.32))
        else:
            self._tween_dur = 0.16
        tween = self._tween_dur
        hold  = max(0.12, tween + 0.04)     # extended-arm hold after beat

        # Rotate through strikes per side so consecutive same-side hits
        # look different (jab → cross → hook → uppercut …).
        idx_per_kind: dict[str, int] = {k: 0 for k in strikes}

        # Minimum gap between the previous recover (or intro tail) and
        # the FIRST prelift waypoint — only needs to be large enough to
        # avoid exact overlap (the easing engine re-uses the previous
        # waypoint as the tween starting point).  Keep it tiny so a
        # multi-phase prelift chain (LIFT→SHIFT→STOMP) actually has room
        # to play on tightly-spaced beats.
        prelift_min_hold = 0.02
        # Track the end of the intro so the first event's PRELIFT doesn't
        # land BEFORE the intro has finished (which would leave an IDLE
        # waypoint sandwiched between the lift and the stomp).
        intro_end_t = tl[-1][0] if tl else 0.0

        for idx, (t_hit, kind, l_scale, sustain) in enumerate(normalized):
            rotation = strikes[kind]
            pose_name = rotation[idx_per_kind[kind] % len(rotation)]
            idx_per_kind[kind] += 1

            # ── PRELIFT chain insertion ──────────────────────────────
            # The prelift entry can be a single pose OR a list — list
            # entries are distributed evenly across the pre-beat
            # window (LIFT → SHIFT → … → STOMP @ t_hit) so the user
            # sees discrete LIFT / SHIFT / SLAM phases instead of one
            # smooth morph.
            pl_entry = prelift_map.get(kind)
            if pl_entry is not None and prelift_time > 0.0:
                pl_poses = ([pl_entry] if isinstance(pl_entry, str)
                            else list(pl_entry))
                # Latest allowed time for the FIRST prelift pose — we
                # must leave at least `tween*0.8` seconds for the
                # stomp-down easing AND, if there are N chain poses,
                # enough room for the intermediate waypoints too.
                latest = t_hit - tween * 0.8
                # Ideal start of the chain: aim for `prelift_time`
                # ahead of the beat, clamped to `latest`.
                t_first = min(t_hit - prelift_time, latest)

                # Evict any existing waypoints (intro / previous
                # recover) that would fall INSIDE the anticipation
                # window — otherwise the body snaps back to IDLE
                # mid-chain.  After popping, `tl[-1][0]` is the true
                # earliest time available for the new chain.
                while tl and tl[-1][0] > t_first:
                    tl.pop()
                    if scales:
                        scales.pop()
                if tl:
                    earliest = tl[-1][0] + prelift_min_hold
                    t_first = max(t_first, earliest)
                t_first = min(t_first, latest)

                if pl_poses and t_first >= 0.0 and t_first < t_hit - 0.02:
                    n = len(pl_poses)
                    if n == 1:
                        tl.append((t_first, pl_poses[0]))
                        scales.append(l_scale)
                    else:
                        span = latest - t_first
                        # If the window is too tight for all N poses,
                        # drop intermediate phases (keep the FIRST and
                        # LAST at minimum — e.g. LIFT + SHIFT) but
                        # still put them at distinct times.
                        min_phase_gap = 0.04
                        while n > 1 and span < min_phase_gap * (n - 1):
                            # Drop the second-to-last phase (keep LIFT
                            # as the initial impulse and SHIFT_x as the
                            # pose that leads directly into STOMP).
                            pl_poses.pop(-2) if n > 2 else pl_poses.pop(0)
                            n = len(pl_poses)
                        if n == 1:
                            tl.append((t_first, pl_poses[0]))
                            scales.append(l_scale)
                        else:
                            for i, pose_n in enumerate(pl_poses):
                                t_i = t_first + span * (i / (n - 1))
                                tl.append((t_i, pose_n))
                                scales.append(l_scale)

            tl.append((t_hit, pose_name))
            scales.append(l_scale)

            # For long-note "hold" strikes (``sustain > 0``), pin the
            # strike pose for the entire sustain window by adding a
            # second waypoint at ``t_hit + sustain`` using the same
            # pose.  The easing engine interpolates waypoint-to-waypoint
            # linearly, so two identical waypoints produce a perfectly
            # frozen pose for the whole duration — i.e. a true HOLD —
            # regardless of ``tween`` / ``hold``.  Non-hold strikes
            # skip this and fall through to the normal recover.
            if sustain > 0.0:
                t_hold_end = t_hit + sustain
                # If the action library declares a SEPARATE end pose for
                # this kind, use it so the tween engine animates the
                # strike → end pose transition across the sustain window
                # (e.g. arm SWEEP for horizontal line chains).  Otherwise
                # repeat the strike pose → legacy frozen hold.
                end_pose = holds_end.get(kind, pose_name)
                tl.append((t_hold_end, end_pose))
                scales.append(l_scale)
                # Retract happens ``tween`` seconds AFTER the hold
                # ends so there's a clean lower-arm animation even on
                # a hold that ends right on the next beat.
                t_rec = t_hold_end + tween
            else:
                # Insert retract (recovery) waypoint so the arm visibly
                # lowers.
                t_rec = t_hit + hold
            next_t = normalized[idx + 1][0] if idx + 1 < len(normalized) else None
            recov_pose = recover.get(kind, guard_def)
            if next_t is None:
                tl.append((t_rec, recov_pose))
                scales.append(1.0)
            elif next_t - t_rec >= tween:
                tl.append((t_rec, recov_pose))
                scales.append(1.0)

        # Sort tl + scales together by time.
        order = sorted(range(len(tl)), key=lambda i: tl[i][0])
        self._timeline    = [tl[i]     for i in order]
        self._lean_scales = [scales[i] for i in order]
        self._beat_times  = [t for t, _, _, _ in normalized]

    def set_beat_times(self, beat_times_sec: list[float], fps: int):
        """Convenience: plain beat list → alternate L/R (back-compat)."""
        evs = [(float(b), 'L' if i % 2 == 0 else 'R', 1.0)
               for i, b in enumerate(beat_times_sec)]
        self.set_beat_events(evs, fps)

    # ------------------------------------------------- angle interpolation
    @staticmethod
    def _lerp_angle(a: float, b: float, t: float) -> float:
        diff = ((b - a + 180.0) % 360.0) - 180.0
        return (a + diff * t) % 360.0

    @staticmethod
    def _ease(t: float) -> float:
        """Softened back-out ease with a gentle settle overshoot.

        Snap lands slightly *past* the target around t≈0.80 then settles
        back to exactly 1.0 at t=1.0 — gives strikes a real "impact +
        recoil" punch instead of a perfectly monotonic slide.  Overshoot
        trimmed from the classic c=1.70158 (~+10 %) down to c=0.95
        (~+4 %) so the rig reads as MỀM (soft-landing) instead of
        snappy — matches the dancer's weight-shift feel.
        """
        t = max(0.0, min(1.0, t))
        c = 0.95
        t1 = t - 1.0
        return 1.0 + (c + 1.0) * t1 * t1 * t1 + c * t1 * t1

    @staticmethod
    def _ease_smooth(t: float) -> float:
        """Plain smoothstep — used for blending the overshoot factor itself."""
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def _blend(self, pa: dict, pb: dict, t: float) -> dict:
        out = {}
        for k in pa:
            if k in self._IGNORE_KEYS:
                continue
            if k in self._LINEAR_KEYS:
                out[k] = pa[k] * (1.0 - t) + pb.get(k, pa[k]) * t
            else:
                out[k] = self._lerp_angle(pa[k], pb.get(k, pa[k]), t)
        # 'punch' marker drives the fist-enlargement + motion-streak
        # overlays.  'L' → left fist only, 'R' → right fist only,
        # 'B' → BOTH fists (used by DOUBLE_LEFT / DOUBLE_RIGHT poses for
        # paired-spawn same-side hits).  Anything else → neither.
        pv_a = pa.get('punch')
        pv_b = pb.get('punch')
        pl_a = 1.0 if pv_a in ('L', 'B') else 0.0
        pl_b = 1.0 if pv_b in ('L', 'B') else 0.0
        pr_a = 1.0 if pv_a in ('R', 'B') else 0.0
        pr_b = 1.0 if pv_b in ('R', 'B') else 0.0
        out['_pL'] = pl_a * (1.0 - t) + pl_b * t
        out['_pR'] = pr_a * (1.0 - t) + pr_b * t
        return out

    def _pose_at(self, cur_t: float) -> dict:
        """'Land-at-target' sampling: hold p0 until the last tween_dur
        seconds before t1, then blend — so p1 locks in AT t1 (the beat).

        Also interpolates the per-waypoint `_lean_scales` and applies the
        resulting factor to the blended pose's `lean` (and, softened, to
        `drop`).  Result: on 4-lane dance mode, outer-lane stomps flare
        the hip farther out and dip slightly lower than inner-lane stomps.
        """
        tl     = self._timeline
        scales = self._lean_scales
        n = len(tl)

        def _apply_scale(pose: dict, s: float) -> dict:
            if abs(s - 1.0) < 1e-4:
                return pose
            pose = dict(pose)
            # Lean scales 1:1 — outer (s>1) reaches farther, inner (s<1)
            # stays tucked.  Drop is softened toward 1.0 so tiny inner
            # stomps don't look like the character is standing tall:
            #   s=0.55 → drop_k ≈ 0.78   s=1.0 → 1.0   s=1.6 → 1.24
            drop_k = 0.5 + 0.5 * s
            if 'lean' in pose:
                pose['lean'] = pose['lean'] * s
            if 'drop' in pose:
                pose['drop'] = pose['drop'] * drop_k
            return pose

        for i in range(n - 1):
            t0, p0 = tl[i]
            t1, p1 = tl[i + 1]
            s0 = scales[i]      if i     < len(scales) else 1.0
            s1 = scales[i + 1]  if i + 1 < len(scales) else 1.0
            if t0 <= cur_t < t1:
                time_to_next = t1 - cur_t
                span         = t1 - t0
                tween = min(self._tween_dur, span)
                if time_to_next < tween:
                    blend = self._ease(1.0 - time_to_next / tween)
                    s_now = s0 * (1.0 - blend) + s1 * blend
                    return _apply_scale(
                        self._blend(_apply_scale(self.POSES[p0], s0),
                                    _apply_scale(self.POSES[p1], s1),
                                    blend),
                        1.0)  # already baked into the operands
                # Holding on p0 — apply its scale.
                return _apply_scale(
                    self._blend(self.POSES[p0], self.POSES[p0], 0.0), s0)
        last = self.POSES[tl[-1][1]]
        s_last = scales[-1] if scales else 1.0
        return _apply_scale(self._blend(last, last, 0.0), s_last)

    # ------------------------------------------------- joint position helper
    @staticmethod
    def _jp(origin: tuple, angle_deg: float, length: float) -> tuple:
        """0°=down, 90°=right, 180°=up, 270°=left (screen y-down)."""
        r = math.radians(angle_deg)
        return (origin[0] + math.sin(r) * length,
                origin[1] + math.cos(r) * length)

    # --------------------------------------------------------------- draw
    def draw(self, canvas: np.ndarray, frame: int, mode: str = 'walk'):
        cur_t = frame / self._fps
        pose  = self._pose_at(cur_t)

        B   = self.B
        col = self._color
        tk  = self.tk

        # -------------------------------------------------- dynamics layers
        # (analysed from nguoique.mp4 — the reference dancer has:
        #   • visible hip-sway + counter shoulder-sway → S-curve body
        #   • weight shift: loaded side sinks a few px, unloaded side rises
        #   • torso ROTATES independently of hip lean (contrapposto twist)
        #   • ever-present arm drift + knee flex even "at rest"
        #   • pre-beat dip + post-beat recoil instead of a static pose)
        # All layers are SCALED down during explicit strike poses so sharp
        # punches / stomps stay readable.  `rest_factor` is 1.0 mid-way
        # between beats, 0.0 right on a beat.

        lean_px = float(pose.get('lean', 0)) * self._sc
        drop_px = float(pose.get('drop', 0)) * self._sc

        # -- Proximity to nearest beat (normalized 0..1).  1 = far from any
        #    beat → layer everything; 0 = exactly on beat → layers muted so
        #    the scripted pose lands clean.
        rest_factor = 1.0
        nearest_dt = 1e9
        for bt in self._beat_times:
            dt = abs(cur_t - bt)
            if dt < nearest_dt:
                nearest_dt = dt
        # Full idle sway beyond 0.4s from any beat; mute to ~25% on-beat.
        # (Higher floor = body stays softly alive even AT beat impact so
        # strikes don't feel like a hard freeze.)
        rest_factor = 0.25 + 0.75 * min(1.0, nearest_dt / 0.40)

        # -- Continuous idle oscillators (freq in Hz).  Two sinusoids with
        #    different periods so hip and shoulder never line up → visible
        #    S-curve / contrapposto every frame even when no beat fires.
        #    Slightly slowed (0.55 → 0.45 Hz) and amped (0.55→0.70 hip,
        #    0.45→0.58 shoulder) so motion breathes wider and softer.
        hip_sway  = (B['hip_half'] * 0.70 *
                     math.sin(2 * math.pi * 0.45 * cur_t + 0.7))
        sho_sway  = (B['shoulder_half'] * 0.58 *
                     math.sin(2 * math.pi * 0.45 * cur_t + 0.7 + math.pi))
        breath    = (B['thigh'] * 0.085 *
                     math.sin(2 * math.pi * 0.70 * cur_t))
        # Torso rotation (deg) — slow 0.28 Hz roll; shoulders tilt INDEPENDENT
        # of hip lean, so pose-driven lean can coexist with the idle twist
        # for an S-curve body instead of a frozen trapezoid.
        # Widened ±6 → ±9° so the torso visibly twists.
        torso_roll = (9.0 *
                      math.sin(2 * math.pi * 0.28 * cur_t + 1.3))
        hip_sway  *= rest_factor
        sho_sway  *= rest_factor
        breath    *= rest_factor
        torso_roll *= rest_factor

        # -- Weight shift: when lean pushes hip to one side, that side sinks
        #    (loaded leg compresses).  Scales with |lean|, bumped
        #    0.18 → 0.24 so the weight-drop is more visible.
        weight_dip = abs(lean_px) * 0.24

        # -- Pre-beat anticipation dip.  In the 0.22 s window before the
        #    next beat, crouch up to ~0.10 * thigh then release into the
        #    post-beat bob.  Window widened (0.16 → 0.22) + depth bumped
        #    (0.08 → 0.10) so the "load" before impact reads softer and
        #    longer — less snappy transition into the strike.
        pre_dip = 0.0
        dip_win = 0.22
        next_beat_dt = 1e9
        for bt in self._beat_times:
            if bt > cur_t:
                next_beat_dt = bt - cur_t
                break
        if next_beat_dt < dip_win:
            p = 1.0 - (next_beat_dt / dip_win)  # 0 → 1 as we approach beat
            pre_dip = B['thigh'] * 0.10 * (p * p)   # quadratic ramp

        # -- Beat-driven body drop (impact bob) — same easing as before but
        #    a touch deeper so the ground-contact reads as mass.
        bob_y = 0.0
        bob_dur = 0.26
        for bt in self._beat_times:
            if bt > cur_t:
                break
            if cur_t < bt + bob_dur:
                phase = (cur_t - bt) / bob_dur
                bob_y = -B['thigh'] * 0.22 * math.sin(phase * math.pi) \
                        * math.exp(-phase * 1.7)
                break

        # -- Final hip anchor.  lean_px from pose + idle hip_sway.
        hip_x = self._hip_base_x + lean_px + hip_sway
        hip_y = (self._hip_base_y + drop_px + bob_y + breath
                 + weight_dip + pre_dip)

        # -- Shoulder anchor.  NOTE: counter-phase sway + torso_roll give
        #    the S-curve.  Shoulder-x is NOT strongly tied to hip_x — this
        #    is the biggest fluidity change.
        sho_mid_x = self._hip_base_x - lean_px * 0.35 + sho_sway
        sho_y     = hip_y - B['torso']

        # -- Rotated shoulder line.  Rotate (±shoulder_half, 0) around
        #    (sho_mid_x, sho_y) by torso_roll degrees.  Torso is now a
        #    true quadrilateral that leans its TOP edge opposite to hips.
        rr  = math.radians(torso_roll)
        cr  = math.cos(rr)
        sr  = math.sin(rr)
        sh2 = B['shoulder_half']
        l_sho = (sho_mid_x - sh2 * cr, sho_y - sh2 * sr)
        r_sho = (sho_mid_x + sh2 * cr, sho_y + sh2 * sr)

        l_hip = (hip_x - B['hip_half'], hip_y)
        r_hip = (hip_x + B['hip_half'], hip_y)
        # Neck sprouts perpendicular to shoulder line so the head leans
        # naturally with the torso roll (−sin, cos) = up-normal rotated.
        neck_base = (sho_mid_x, sho_y)
        neck_top  = (sho_mid_x + (-sr) * B['neck'],
                     sho_y      + (-cr) * B['neck'])
        head_c    = (sho_mid_x + (-sr) * (B['neck'] + B['head_r']),
                     sho_y      + (-cr) * (B['neck'] + B['head_r']))

        def ip(p): return (int(round(p[0])), int(round(p[1])))

        # -- Idle arm/leg-angle drift.  Small continuous angular wobble on
        #    each limb's upper segment — barely a few degrees — layered on
        #    top of the scripted pose.  Suppressed while the side is
        #    punching (pL/pR > 0) or while knees are heavily scripted
        #    (detect via how far the pose leg angle is from neutral).
        pL_now = float(pose.get('_pL', 0.0))
        pR_now = float(pose.get('_pR', 0.0))
        arm_drift_amp = 8.5 * rest_factor      # degrees
        leg_drift_amp = 5.0 * rest_factor      # degrees
        phase_t  = 2 * math.pi * 0.55 * cur_t
        la_drift = arm_drift_amp * math.sin(phase_t)         * (1.0 - pL_now)
        ra_drift = arm_drift_amp * math.sin(phase_t + math.pi) * (1.0 - pR_now)
        # Knee "flex" offset: bend upper leg in/out by a few degrees.
        # Skip if pose already has a big knee lift (LIFT_L / STOMP_R etc.)
        ll_u_raw = pose['ll_u']
        rl_u_raw = pose['rl_u']
        ll_neutral = abs(((ll_u_raw + 5.0) % 360.0) - 5.0)  # dist from 355°
        rl_neutral = abs(((rl_u_raw - 5.0) % 360.0) - 355.0)
        ll_flex_scale = max(0.0, 1.0 - ll_neutral / 20.0)
        rl_flex_scale = max(0.0, 1.0 - rl_neutral / 20.0)
        ll_drift = (leg_drift_amp * ll_flex_scale *
                    math.sin(2 * math.pi * 0.55 * cur_t + 0.4))
        rl_drift = (leg_drift_amp * rl_flex_scale *
                    math.sin(2 * math.pi * 0.55 * cur_t + 0.4 + math.pi))

        l_elbow = self._jp(l_sho, pose['la_u'] + la_drift, B['upper_arm'])
        l_hand  = self._jp(l_elbow, pose['la_f'] + la_drift * 0.6,
                           B['forearm'])
        r_elbow = self._jp(r_sho, pose['ra_u'] + ra_drift, B['upper_arm'])
        r_hand  = self._jp(r_elbow, pose['ra_f'] + ra_drift * 0.6,
                           B['forearm'])

        l_knee  = self._jp(l_hip, pose['ll_u'] + ll_drift, B['thigh'])
        l_ankle = self._jp(l_knee, pose['ll_f'] - ll_drift * 0.4, B['shin'])
        r_knee  = self._jp(r_hip, pose['rl_u'] + rl_drift, B['thigh'])
        r_ankle = self._jp(r_knee, pose['rl_f'] - rl_drift * 0.4, B['shin'])

        # HEAD + NECK — neck attaches at the shoulder-line midpoint so
        # the head leans with the rotating torso instead of floating.
        cv2.circle(canvas, ip(head_c), int(B['head_r']), col, tk,
                   lineType=cv2.LINE_AA)
        _seg(canvas, ip(neck_top), ip(neck_base), col, tk)

        # TORSO (quadrilateral outline — top edge rotates with torso_roll
        # so the body reads as "twisted" not a rigid column).
        torso_pts = np.array(
            [ip(l_sho), ip(r_sho), ip(r_hip), ip(l_hip)], dtype=np.int32)
        cv2.polylines(canvas, [torso_pts], True, col, tk, lineType=cv2.LINE_AA)

        # ARMS + LEGS
        for a, b in [(l_sho, l_elbow), (l_elbow, l_hand),
                     (r_sho, r_elbow), (r_elbow, r_hand),
                     (l_hip, l_knee),  (l_knee, l_ankle),
                     (r_hip, r_knee),  (r_knee, r_ankle)]:
            _seg(canvas, ip(a), ip(b), col, tk)

        # FEET — short angled stubs so the character reads as "planted"
        foot_len = B['shin'] * 0.36
        l_toe = (l_ankle[0] - foot_len, l_ankle[1] + foot_len * 0.25)
        r_toe = (r_ankle[0] + foot_len, r_ankle[1] + foot_len * 0.25)
        _seg(canvas, ip(l_ankle), ip(l_toe), col, tk)
        _seg(canvas, ip(r_ankle), ip(r_toe), col, tk)

        # FISTS + motion streak — only while that side is actively punching.
        pL = float(pose.get('_pL', 0.0))
        pR = float(pose.get('_pR', 0.0))
        base_fist_r = max(2, int(round(tk * 0.55)))
        if pL > 0.12:
            r_fist = int(round(base_fist_r + base_fist_r * 3.4 * pL))
            cv2.circle(canvas, ip(l_hand), r_fist, col, -1, lineType=cv2.LINE_AA)
        if pR > 0.12:
            r_fist = int(round(base_fist_r + base_fist_r * 3.4 * pR))
            cv2.circle(canvas, ip(r_hand), r_fist, col, -1, lineType=cv2.LINE_AA)

        def _streak(elbow, hand, intensity):
            if intensity < 0.6:
                return
            ex, ey = elbow
            hx, hy = hand
            dx, dy = hx - ex, hy - ey
            d  = math.hypot(dx, dy) or 1.0
            ux, uy = dx / d, dy / d
            s = (hx - ux * B['forearm'] * 0.35,
                 hy - uy * B['forearm'] * 0.35)
            e = (hx + ux * B['forearm'] * 0.55,
                 hy + uy * B['forearm'] * 0.55)
            alpha = min(1.0, (intensity - 0.6) / 0.4)
            w = max(1, int(round(tk * (0.7 + 0.8 * alpha))))
            streak_col = tuple(int(c * (0.55 + 0.45 * alpha)) for c in col)
            cv2.line(canvas, ip(s), ip(e), streak_col, w, lineType=cv2.LINE_AA)

        _streak(l_elbow, l_hand, pL)
        _streak(r_elbow, r_hand, pR)

        return canvas


# ── Beat pipeline ────────────────────────────────────────────────────────────
#
# This block mirrors the full audio→stick_events pipeline used by
# ``rhythm.py`` so a stickman video rendered standalone can line up
# perfectly with a rhythm-game video produced from the same song +
# same parameters.  Steps, in order, matching rhythm.py:
#
#   1. detect_beat_times(...)          ← librosa → float seconds
#   2. extract_bass_array(...)         ← bass energy per video frame
#   3. beat_times_to_frames(...)       ← frame-quantise (critical for sync)
#   4. apply_density(...)              ← keep Nth / multiply by N
#   5. resolve_travel(...)             ← auto-travel from BPM + speed
#   6. compute_min_lane_gap(...)       ← per-lane spacing guard
#   7. schedule_events(...)            ← strict L↔R alternation + walls
#
# The scheduler reproduces ``GameManager.pre_schedule`` bit-for-bit,
# including the deterministic RNG used for wall spawns, so the exact
# same `--seed` + identical inputs emit the exact same event stream.


def detect_beat_times(y: np.ndarray, sr: int,
                      source: str = 'tempo',
                      bpm: float | None = None,
                      sens: float = 0.5,
                      subdiv: int = 1,
                      total_duration: float | None = None
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Run librosa → return (beat_times_sec, onset_env, spec_mag, tempo)."""
    if not _HAVE_LIBROSA:
        raise RuntimeError("librosa is required for beat detection.")

    if total_duration is None:
        total_duration = len(y) / sr

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH)
    spec      = librosa.stft(y, hop_length=HOP_LENGTH)
    spec_mag  = librosa.magphase(spec)[0]

    sens = float(np.clip(sens, 0.0, 1.0))
    tempo_val = 0.0

    if source == 'tempo':
        if bpm is not None and bpm > 0:
            tempo_val = float(bpm)
            try:
                _, bh = librosa.beat.beat_track(
                    onset_envelope=onset_env, sr=sr,
                    hop_length=HOP_LENGTH, tightness=120)
                first_t = float(librosa.frames_to_time(
                    bh[:1], sr=sr, hop_length=HOP_LENGTH)[0]) \
                    if len(bh) else 0.0
            except Exception:
                first_t = 0.0
        else:
            tempo, bh = librosa.beat.beat_track(
                onset_envelope=onset_env, sr=sr,
                hop_length=HOP_LENGTH, tightness=120)
            tempo_val = float(tempo) if np.ndim(tempo) == 0 else float(tempo[0])
            first_t = float(librosa.frames_to_time(
                bh[:1], sr=sr, hop_length=HOP_LENGTH)[0]) \
                if len(bh) else 0.0
        if tempo_val <= 0:
            tempo_val = 120.0
        period = 60.0 / tempo_val / max(1, subdiv)
        while first_t - period >= 0:
            first_t -= period
        beat_times = np.arange(first_t, total_duration, period)

    elif source == 'onset':
        delta = 0.60 - sens * 0.55
        wait  = max(1, int(6 - sens * 4))
        hops  = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, hop_length=HOP_LENGTH,
            delta=delta, wait=wait, units='frames')
        beat_times = librosa.frames_to_time(hops, sr=sr, hop_length=HOP_LENGTH)

    else:  # 'beat'
        tempo, hops = librosa.beat.beat_track(
            onset_envelope=onset_env, sr=sr,
            hop_length=HOP_LENGTH, tightness=120)
        beat_times = librosa.frames_to_time(hops, sr=sr, hop_length=HOP_LENGTH)
        if len(hops) > 0:
            strengths = onset_env[hops]
            factor = 1.10 - sens * 1.10
            thresh = float(np.median(strengths)) * factor
            mask = strengths >= thresh
            beat_times = beat_times[mask]
        tempo_val = float(tempo) if np.ndim(tempo) == 0 else float(tempo[0])

        if subdiv > 1 and len(beat_times) >= 2:
            sub = []
            for a, b in zip(beat_times, beat_times[1:]):
                sub.append(a)
                for k in range(1, subdiv):
                    sub.append(a + (b - a) * k / subdiv)
            sub.append(beat_times[-1])
            beat_times = np.array(sub)

    return np.asarray(beat_times, dtype=np.float64), onset_env, spec_mag, tempo_val


def extract_bass_array(onset_env: np.ndarray, spec_mag: np.ndarray,
                       total_frames: int, bass_range: int = 20
                       ) -> np.ndarray:
    """Per video-frame bass energy in [0, 1]. Matches rhythm.py."""
    bass_arr = np.zeros(total_frames, dtype=np.float32)
    bass_max = max(float(np.max(spec_mag[:bass_range])), 1e-6)
    oe_len   = len(onset_env)
    for f in range(total_frames):
        oi = min(int(f * oe_len / max(1, total_frames)), oe_len - 1)
        bass_arr[f] = float(np.clip(
            np.mean(spec_mag[:bass_range, oi]) / bass_max * 3, 0, 1))
    return bass_arr


def beat_times_to_frames(beat_times: np.ndarray, fps: int,
                         total_frames: int) -> list[int]:
    """Float beat times → integer video-frame indices, clipped to clip length."""
    frames = [int(round(float(t) * fps)) for t in beat_times]
    return [bf for bf in frames if 0 <= bf < total_frames]


def apply_density(beat_frames: list[int], density: float) -> list[int]:
    """Keep every Nth beat (density<1) or interpolate sub-beats (density>1)."""
    d = float(density)
    if d < 0.999 and len(beat_frames) > 1:
        step = max(1, int(round(1.0 / d)))
        return beat_frames[::step]
    if d > 1.001 and len(beat_frames) >= 2:
        mult = int(round(d))
        dense: list[int] = []
        for a, b in zip(beat_frames, beat_frames[1:]):
            dense.append(a)
            for k in range(1, mult):
                dense.append(int(round(a + (b - a) * k / mult)))
        dense.append(beat_frames[-1])
        return dense
    return beat_frames


def resolve_travel(beat_frames: list[int], travel_frames: int,
                   block_speed: float) -> int:
    """Mirror rhythm.py auto-travel: 2× median-beat-period / speed."""
    if travel_frames >= 0:
        return travel_frames
    if len(beat_frames) < 2:
        return 40
    diffs = np.diff(beat_frames)
    base = int(round(float(np.median(diffs)) * 2))
    speed = max(0.05, float(block_speed))
    return max(8, int(round(base / speed)))


def compute_min_lane_gap(beat_frames: list[int], travel: int,
                         max_per_lane: int) -> int:
    """Per-lane spawn-frame spacing guard (same formula as rhythm.py)."""
    if len(beat_frames) >= 2:
        base_cycle = int(round(float(np.median(np.diff(beat_frames))) * 2))
    else:
        base_cycle = 16
    max_per_lane = max(1, int(max_per_lane))
    return max(1, travel // max_per_lane, base_cycle // 2)


def _parse_lanes_spec(spec: str | None, n_lanes: int) -> set[int] | None:
    """Parse a 1-based lane-filter CLI string into a set of 0-based indices.

    Mirrors ``rhythm._parse_lanes`` so the two modules accept identical
    --lanes syntax ("1,2", "1,4", "1-3", "all", "1,2,3,4").  Returns
    ``None`` when the spec means "no filter" so the scheduler keeps all
    lanes active.
    """
    if spec is None:
        return None
    s = spec.strip().lower()
    if not s or s in ('all', '*', '0'):
        return None
    out: set[int] = set()
    for tok in s.split(','):
        tok = tok.strip()
        if not tok:
            continue
        if '-' in tok:
            a, _, b = tok.partition('-')
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"Invalid lane range '{tok}' in --lanes.")
            if lo > hi:
                lo, hi = hi, lo
            for v in range(lo, hi + 1):
                out.add(v - 1)
        else:
            try:
                out.add(int(tok) - 1)
            except ValueError:
                raise ValueError(f"Invalid lane index '{tok}' in --lanes.")
    for v in out:
        if v < 0 or v >= n_lanes:
            raise ValueError(
                f"Lane {v + 1} out of range — valid lanes are 1..{n_lanes}.")
    return out if out else None


def schedule_events(beat_frames: list[int],
                    bass_arr: np.ndarray,
                    fps: int,
                    travel: int,
                    min_gap_frames: int = 4,
                    min_lane_gap: int = 0,
                    wall_prob: float = 0.12,
                    bass_thresh: float = 0.60,
                    kind_mode: str = 'rhythm',
                    rng_seed: int = 42,
                    n_lanes: int = 2,
                    lane_filter: set[int] | None = None,
                    modes: list[str] | None = None,
                    dance_pair_cycle: int = 4,
                    punch_pair_cycle: int = 4,
                    line_beats: int = 2,
                    ) -> list[tuple]:
    """Produce (t_sec, kind, lean_scale) events identical to rhythm.py's
    scheduler.

    ``lean_scale`` is the per-event multiplier for pose ``lean``/``drop`` –
    1.0 on legacy 2-lane punch, >1 for outer lanes and <1 for inner lanes
    on wider layouts, so the stickman visibly reaches further on outer
    targets.  See ``set_beat_events`` for how it is consumed.

    kind_mode:
      'rhythm'    – bit-for-bit clone of rhythm.py GameManager.pre_schedule
                    (strict L↔R side alternation + sub-lane cycling + wall
                    spawns on strong bass + lane-gap filtering).
                    ← USE THIS to match a rhythm.py video.
      'alternate' – simple L/R alternation, no filtering (for fun previews).
      'all_L'     – every beat is a left-side strike.
      'all_R'     – every beat is a right-side strike.

    `n_lanes` must match the `rhythm.py` lane count for the rendered video
    (2 for punch mode, 4 for dance mode).  Affects the 'rhythm' mode only.

    `modes` enables **combo mode** (stickman action='combo') when it has
    more than one entry — events alternate kinds per beat, with each
    kind prefixed by its sub-mode letter: 'PL'/'PR' for punch beats and
    'DL'/'DR' for dance beats.  ``None`` or a single-element list keeps
    the legacy 'L'/'R' kinds so older ``--action punch/dance`` renders
    stay bit-identical.  Walls always emit ``'W'`` and paired-spawn
    beats always emit ``'LL'``/``'RR'`` — neither takes a sub-mode
    prefix since the combo action maps them directly to double-hand
    punches regardless of which beat they land on.
    """
    import random
    # Normalize combo-mode sequence.  [] / None / ['punch'] → single-mode
    # (no prefixing); ['punch','dance'] → alternate per emitted beat.
    modes_seq: list[str] = list(modes) if modes else []
    combo_active = len(modes_seq) >= 2

    if kind_mode == 'alternate':
        return [((bf / fps), 'L' if i % 2 == 0 else 'R', 1.0)
                for i, bf in enumerate(beat_frames)]
    if kind_mode == 'all_L':
        return [((bf / fps), 'L', 1.0) for bf in beat_frames]
    if kind_mode == 'all_R':
        return [((bf / fps), 'R', 1.0) for bf in beat_frames]

    rng = random.Random(rng_seed)

    # Split lanes L (first half) / R (second half), mirroring
    # GameManager.pre_schedule so event ordering stays bit-for-bit
    # identical across the two modules.
    n_lanes = max(2, int(n_lanes))
    mid = n_lanes // 2
    side_lanes = {
        0: list(range(0, mid)),
        1: list(range(mid, n_lanes)),
    }
    side_lanes[1].reverse()
    # Apply user lane filter (keep relative order inside each side).
    if lane_filter is not None:
        mask = set(int(l) for l in lane_filter)
        side_lanes[0] = [l for l in side_lanes[0] if l in mask]
        side_lanes[1] = [l for l in side_lanes[1] if l in mask]
    side_cursor = [0, 0]

    # Paired-spawn detection — mirrors GameManager.pre_schedule so
    # stand-alone stickman renders emit the SAME paired-event kinds
    # rhythm.py would for the same --lanes / --mode inputs.  Both
    # sub-modes share the same generalized "(N-1) đơn + 1 đôi" cycle
    # model now — punch beats follow `punch_pair_cycle`, dance beats
    # follow `dance_pair_cycle`, each with its own counter + cursor.
    # Only evaluated for 'rhythm' kind_mode; other --kinds modes have
    # their own bespoke patterns.
    enabled_lanes_sorted = (sorted(lane_filter)
                            if lane_filter is not None
                            else list(range(n_lanes)))
    adjacent_pairs: list[tuple[int, int]] = []
    for i in range(len(enabled_lanes_sorted) - 1):
        a, b = enabled_lanes_sorted[i], enabled_lanes_sorted[i + 1]
        if b - a != 1:
            continue
        # Cross-side adjacent pairs (e.g. lanes 1,2 on a 4-lane layout,
        # straddling the centre) are skipped so the paired strike leans
        # cleanly toward one side rather than straddling both.
        if (a < mid) != (b < mid):
            continue
        adjacent_pairs.append((a, b))
    punch_adjacent_pairs = list(adjacent_pairs)
    dance_adjacent_pairs = list(adjacent_pairs)
    # Pair-eligibility by mode:
    #   • combo (multiple modes) → BOTH gates open, the per-beat loop
    #     picks the right rule via `cur_mode`.
    #   • solo (empty / single-element modes) → only the mode present
    #     (or 'punch' as the legacy default when `modes` is empty) is
    #     eligible, so --action dance + --lanes 1,2 yields dance-paired
    #     events without accidentally emitting punch-paired LL/RR.
    if combo_active:
        _has_punch = 'punch' in modes_seq
        _has_dance = 'dance' in modes_seq
    else:
        solo = modes_seq[0] if modes_seq else 'punch'
        _has_punch = (solo == 'punch')
        _has_dance = (solo == 'dance')
    punch_paired_enabled = (bool(punch_adjacent_pairs)
                            and _has_punch
                            and punch_pair_cycle > 0)
    dance_paired_enabled = (bool(dance_adjacent_pairs)
                            and _has_dance
                            and dance_pair_cycle > 0)

    # Line (hold-note) support.  Events for line targets are 4-tuples
    # ``(t, 'ZL'|'ZR', lean_scale, sustain)`` where sustain encodes the
    # bar's hold window in seconds — same formula rhythm.py uses so the
    # stickman-only video stays bit-identical.
    if len(beat_frames) >= 2 and line_beats > 0:
        _gaps = np.diff(np.asarray(beat_frames, dtype=np.int64))
        _median_gap = max(1, int(np.median(_gaps)))
        line_hold_frames = max(4, int(line_beats * _median_gap))
    else:
        line_hold_frames = max(4, travel // 3)

    events: list[tuple] = []
    last_bf = -999
    next_side = 0
    last_spawn_on = [-10 ** 9] * n_lanes
    # Line-busy per-lane bookkeeping: any beat with ``bf <
    # line_busy_until[l]`` is skipped for that lane (the long-note bar
    # is still sliding past and we mustn't stack a second target on
    # the same rail).  Mirrors GameManager.pre_schedule exactly.
    line_busy_until = [-10 ** 9] * n_lanes
    # Global chain lock: all lanes blocked until the previous chain ends.
    line_global_busy_until: int = -10 ** 9
    skipped_early = skipped_stacked = merged = 0
    half = (n_lanes - 1) / 2.0
    # Combo-mode cursor: advances once per emitted beat-event (walls,
    # paired spawns, single strikes) so the punch↔dance alternation
    # matches rhythm.py's scheduler cadence bit-for-bit.
    emit_idx = 0
    # Independent counters / cursors for the two paired sub-cycles.
    # Each only ticks when a same-sub-mode event is successfully emitted
    # (so lane-stacked skips don't drift the pattern).  Cursors rotate
    # through the same-side adjacent pair list; with all 4 lanes on the
    # doubles alternate L-pair (0,1) ↔ R-pair (2,3).
    punch_paired_count = 0
    punch_pair_cursor  = 0
    dance_paired_count = 0
    dance_pair_cursor  = 0

    _solo_mode = modes_seq[0] if modes_seq else 'punch'

    def _mode_for(idx: int) -> str:
        if combo_active:
            return modes_seq[idx % len(modes_seq)]
        # Solo: single mode for every beat (respects --mode line, dance,
        # etc.).  Defaults to 'punch' when `modes` is empty/None so
        # legacy callers that never passed `modes=` still behave as
        # they always have.
        return _solo_mode

    for bf in beat_frames:
        if bf - last_bf < min_gap_frames:
            merged += 1
            continue
        if bf - travel < 0:
            skipped_early += 1
            continue
        spawn_f = bf - travel

        b = float(bass_arr[min(bf, len(bass_arr) - 1)]) if len(bass_arr) else 0.0
        r = rng.random()
        if b > bass_thresh and r < wall_prob:
            events.append((bf / fps, 'W', 1.0))
            last_bf = bf
            emit_idx += 1
            continue

        cur_mode = _mode_for(emit_idx)

        # ── Punch paired: (N-1) đấm đơn + 1 đấm đôi cycle, same model
        # as dance-paired below.  Every Nth punch beat (N =
        # punch_pair_cycle) picks the next same-side adjacent pair
        # from `punch_adjacent_pairs` (rotating cursor) → emits 'LL'
        # or 'RR' double-hand strike.  N-1 preceding beats fall
        # through to single-hand PL/PR.
        punch_paired_single = False
        if (punch_paired_enabled
                and (not combo_active or cur_mode == 'punch')):
            if ((punch_paired_count % punch_pair_cycle)
                    == punch_pair_cycle - 1):
                pair = punch_adjacent_pairs[
                    punch_pair_cursor % len(punch_adjacent_pairs)
                ]
                if any(bf < line_busy_until[l] for l in pair):
                    skipped_stacked += 1
                    continue
                punch_pair_cursor += 1
                pair_is_left = pair[1] < mid
                kind = 'LL' if pair_is_left else 'RR'
                events.append((bf / fps, kind, 1.0))
                last_bf = bf
                for lane in pair:
                    last_spawn_on[lane] = spawn_f
                punch_paired_count += 1
                emit_idx += 1
                continue
            punch_paired_single = True

        # ── Dance paired: (N-1) single + 1 double cycle, N =
        # dance_pair_cycle (default 4 → 3 đơn + 1 chụm, musical 4/4).
        # The LAST beat of each cycle emits 'JL'/'JR' (feet-together
        # side-jump) on the next same-side adjacent pair (cursor
        # rotates).  The N-1 preceding beats fall through to the
        # regular single-stomp path and alternate lanes via
        # side_cursor cycling.  Counter advances only after a
        # successful emit so a lane-stacked skip doesn't drift the
        # pattern.
        dance_paired_single = False
        if (dance_paired_enabled
                and (not combo_active or cur_mode == 'dance')):
            if ((dance_paired_count % dance_pair_cycle)
                    == dance_pair_cycle - 1):
                pair = dance_adjacent_pairs[
                    dance_pair_cursor % len(dance_adjacent_pairs)
                ]
                if any(bf < line_busy_until[l] for l in pair):
                    skipped_stacked += 1
                    continue
                dance_pair_cursor += 1
                pair_is_left = pair[1] < mid
                kind = 'JL' if pair_is_left else 'JR'
                events.append((bf / fps, kind, 1.0))
                last_bf = bf
                for lane in pair:
                    last_spawn_on[lane] = spawn_f
                dance_paired_count += 1
                emit_idx += 1
                continue
            dance_paired_single = True

        chosen_lane = -1
        chosen_side = next_side
        for side_try in (next_side, 1 - next_side):
            lanes_list = side_lanes[side_try]
            if not lanes_list:
                continue
            start = side_cursor[side_try] % len(lanes_list)
            for off in range(len(lanes_list)):
                lane_try = lanes_list[(start + off) % len(lanes_list)]
                if bf < line_busy_until[lane_try]:
                    continue
                if spawn_f - last_spawn_on[lane_try] >= min_lane_gap:
                    chosen_lane = lane_try
                    chosen_side = side_try
                    side_cursor[side_try] = (start + off + 1) % len(lanes_list)
                    break
            if chosen_lane != -1:
                break

        if chosen_lane == -1:
            skipped_stacked += 1
            continue

        side_tag = 'L' if chosen_side == 0 else 'R'
        # Wider stance toward outer lanes.  For n_lanes==2 this yields 1.0
        # on every event (backward-compatible with legacy punch mode).
        if n_lanes <= 2:
            lean_scale = 1.0
        else:
            offset_norm = abs(chosen_lane - half) / half     # 0..1
            lean_scale = 0.55 + 1.05 * offset_norm

        if cur_mode == 'line':
            # Global lock: reject if a previous chain is still active.
            if bf < line_global_busy_until:
                skipped_stacked += 1
                continue
            # Emit one event per block: head (i=0) first, tails after.
            # Mirrors LineTarget.__init__ exactly: 8th-note subdivision
            # (2 blocks per beat), D derived so chain spans line_hold.
            _n_cubes  = min(8, max(2, 2 * line_beats))
            _CHAIN_D  = max(1, int(round(line_hold_frames /
                                         max(1, _n_cubes - 1))))
            _per_sus  = _CHAIN_D / float(fps)
            for _i in range(_n_cubes):
                _t_i  = (bf + _i * _CHAIN_D) / fps
                _vert = 'D' if (_i % 2 == 0) else 'U'
                events.append((_t_i, 'Z' + side_tag + _vert,
                               lean_scale, _per_sus))
            # Lock next chain until current chain fully dies (past last
            # block's shrink), matching LineTarget.is_dead.
            _chain_life = line_hold_frames + _CHAIN_D + 3
            line_busy_until[chosen_lane] = bf + _chain_life
            line_global_busy_until       = bf + _chain_life
        else:
            # In combo mode prefix the kind with the sub-mode letter
            # ('P' or 'D') so the combo action knows whether to punch
            # or stomp.
            if combo_active:
                kind = ('D' if cur_mode == 'dance' else 'P') + side_tag
            else:
                kind = side_tag
            events.append((bf / fps, kind, lean_scale))
        last_spawn_on[chosen_lane] = spawn_f
        next_side = 1 - chosen_side
        last_bf = bf
        emit_idx += 1
        if punch_paired_single:
            punch_paired_count += 1
        if dance_paired_single:
            dance_paired_count += 1

    print(f"[schedule] {len(events)} events from {len(beat_frames)} beats "
          f"(lanes={n_lanes}, skipped_early={skipped_early}, "
          f"lane_stacked={skipped_stacked}, merged_too_close={merged})")
    return events


# ── Events file I/O (JSON) ───────────────────────────────────────────────────
def save_events(path: str, events: list[tuple],
                meta: dict | None = None):
    """Persist stickman-events to JSON so rhythm.py + stickman.py share timing.

    Supported tuple shapes (stored compactly):

      • 2-tuple ``(t, kind)``                        — legacy
      • 3-tuple ``(t, kind, lean_scale)``            — wider-stance dance
      • 4-tuple ``(t, kind, lean_scale, sustain)``   — line hold-notes

    Trailing optional fields are omitted from the JSON row when they
    equal their default (lean_scale=1.0, sustain=0.0) so legacy files
    stay bit-identical when re-saved.
    """
    import json
    serialized: list[list] = []
    for ev in events:
        if len(ev) == 4:
            t, k, s, sustain = ev
            row: list = [float(t), str(k)]
            if abs(float(s) - 1.0) >= 1e-6 or float(sustain) > 0.0:
                row.append(float(s))
            if float(sustain) > 0.0:
                row.append(float(sustain))
            serialized.append(row)
        elif len(ev) == 3:
            t, k, s = ev
            if abs(float(s) - 1.0) < 1e-6:
                serialized.append([float(t), str(k)])
            else:
                serialized.append([float(t), str(k), float(s)])
        else:
            t, k = ev
            serialized.append([float(t), str(k)])
    payload = {
        "version":  2,
        "meta":     meta or {},
        "events":   serialized,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)


def load_events(path: str) -> tuple[list[tuple], dict]:
    """Load events previously written by :func:`save_events`.

    Handles v1 (2-element), v2 (optional 3-element ``lean_scale``) and
    v2.1 (optional 4-element ``sustain`` for line hold-notes) rows.
    Rows without a sustain field get sustain=0.0 (non-hold); rows
    without a lean_scale get 1.0.  Legacy callers that only unpack the
    first 3 fields keep working (sustain trails at index [3]).
    """
    import json
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    events: list[tuple] = []
    for row in data.get("events", []):
        if len(row) >= 4:
            events.append((float(row[0]), str(row[1]),
                           float(row[2]), float(row[3])))
        elif len(row) == 3:
            events.append((float(row[0]), str(row[1]), float(row[2])))
        else:
            events.append((float(row[0]), str(row[1]), 1.0))
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    return events, meta


# ── Convenience wrapper (backwards-compat) ───────────────────────────────────
def detect_beat_events(audio_file: str,
                       duration: float | None = None,
                       source: str = 'tempo',
                       bpm: float | None = None,
                       sens: float = 0.5,
                       subdiv: int = 1,
                       alternate_kinds: bool = True
                       ) -> tuple[list[tuple[float, str]], float, int]:
    """Legacy one-shot helper — no rhythm-scheduler parity.

    For accurate sync with ``rhythm.py`` use the full pipeline:
    ``detect_beat_times`` → ``extract_bass_array`` → ``beat_times_to_frames``
    → ``apply_density`` → ``schedule_events``.
    """
    y, sr = librosa.load(audio_file, mono=True)
    if duration is not None:
        y = y[:int(duration * sr)]
    total_duration = len(y) / sr
    beat_times, _, _, _ = detect_beat_times(
        y, sr, source=source, bpm=bpm, sens=sens, subdiv=subdiv,
        total_duration=total_duration)
    events: list[tuple[float, str, float]] = []
    for i, t in enumerate(beat_times):
        kind = ('L' if i % 2 == 0 else 'R') if alternate_kinds else 'L'
        events.append((float(t), kind, 1.0))
    return events, total_duration, len(events)


# ── StickmanVisualizer (standalone CLI) ──────────────────────────────────────
class StickmanVisualizer:
    """Render a stickman-only video synced to beats in an audio track."""

    def __init__(self):
        self.WIDTH   = 1920
        self.HEIGHT  = 1080
        self.FPS     = FPS
        self.TIME_LIMIT: float | None = None
        self.ACTION  = 'punch'
        # Combo-mode sub-modes (e.g. ['punch', 'dance']).  Empty / single-
        # element = legacy single-mode behaviour.  Populated from
        # ``--mode`` on the CLI so stand-alone stickman renders can
        # mirror rhythm.py's punch↔dance alternation bit-for-bit.
        self.MODES: list[str] = []
        # Dance paired-spawn cycle length (must match rhythm.py's
        # --dance_pair_cycle to stay in sync).  4 = "3 đơn + 1 chụm".
        self.DANCE_PAIR_CYCLE: int = 4
        # Punch paired-spawn cycle length (mirror of DANCE_PAIR_CYCLE
        # for the punch sub-mode).  4 = "3 đấm đơn + 1 đấm 2-tay".
        # Must match rhythm.py --punch_pair_cycle to stay in sync.
        self.PUNCH_PAIR_CYCLE: int = 4
        # Line (hold-note) length in beats — must match rhythm.py
        # --line_beats so the stickman holds the punch for the same
        # duration the bar slides past on-screen.
        self.LINE_BEATS:      int = 2
        # Lane count of the rhythm.py video this stickman pairs with.
        # 'punch' mode uses 2 lanes, 'dance' mode uses 4 lanes.  Only
        # affects the 'rhythm' kind_mode scheduler (event ordering must
        # match rhythm.py bit-for-bit).  0 = auto (derive from ACTION).
        self.N_LANES: int = 0

        # -- Beat detection (same names / defaults as RhythmVisualizer) --
        self.BEAT_SOURCE: str = 'tempo'
        self.BEAT_BPM:    float | None = None
        self.BEAT_SENS:   float = 0.5
        self.BEAT_SUBDIV: int = 1
        self.BEAT_MIN_GAP: int = 4        # frames between targets
        self.BEAT_DENSITY: float = 1.0    # 0.5 = half, 2.0 = double cadence

        # -- Target-scheduler tuning (mirror rhythm.py) --------------------
        self.TRAVEL_FRAMES: int = -1      # <0 = auto from BPM + speed
        self.BLOCK_SPEED:   float = 1.0
        self.MAX_PER_LANE:  int = 3
        self.WALL_PROB:     float = 0.12
        self.BASS_THRESH:   float = 0.60
        self.RNG_SEED:      int = 42
        self.KIND_MODE:     str = 'rhythm'  # rhythm|alternate|all_L|all_R
        # Optional lane filter (set of 0-based lane indices). None = all
        # lanes active.  CLI flag --lanes uses 1-based numbers.
        self.LANE_FILTER:   set[int] | None = None

        # -- Events file I/O (for exact sync with a rhythm video) ---------
        self.EVENTS_FILE:   str | None = None  # load events from JSON
        self.EXPORT_EVENTS: str | None = None  # save events to JSON

        # -- Visual --------------------------------------------------------
        self.STICK_COLOR = CLR_WHITE
        self.BG_COLOR    = CLR_BG
        self.BG_IMAGE:   str | None = None
        self.BOX:        tuple | None = None

        # -- Tight-crop output (trim excess around the stickman) ----------
        # When FIT is on, the output video is resized to (box_w+2·PAD, box_h+2·PAD)
        # and the stickman is re-anchored at (PAD, PAD).  Handy when compositing
        # a stickman-only clip on top of a rhythm-game video.
        self.FIT: bool = False
        self.PAD: int  = 24

        self.is_mac = IS_MAC

    # ---------------------------------------------------------- render
    def process_video(self, audio_file: str) -> str | None:
        t0 = time.time()
        print(f"Starting Stickman processing  (action={self.ACTION})...")

        # --------------------------------------------------------------
        # Beat pipeline — mirrors rhythm.py exactly so the same song +
        # same params produce the SAME event timeline on both effects.
        # --------------------------------------------------------------
        if self.EVENTS_FILE:
            print(f"Loading pre-computed events: {self.EVENTS_FILE}")
            events, meta = load_events(self.EVENTS_FILE)
            total_duration = float(meta.get('duration', 0.0))
            if total_duration <= 0:
                if _HAVE_LIBROSA:
                    total_duration = float(librosa.get_duration(path=audio_file))
                else:
                    total_duration = (events[-1][0] + 1.5) if events else 1.0
            if self.TIME_LIMIT is not None:
                total_duration = min(total_duration, self.TIME_LIMIT)
            total_frames = int(total_duration * self.FPS)
            print(f"[events_file] {len(events)} events loaded, "
                  f"duration={total_duration:.2f}s, meta_fps={meta.get('fps')}")
        else:
            print(f"Loading audio: {audio_file}")
            y, sr = librosa.load(audio_file, mono=True)
            if self.TIME_LIMIT is not None:
                y = y[:int(self.TIME_LIMIT * sr)]
            total_duration = len(y) / sr
            total_frames   = int(total_duration * self.FPS)

            beat_times, onset_env, spec_mag, tempo_val = detect_beat_times(
                y, sr,
                source=self.BEAT_SOURCE,
                bpm=self.BEAT_BPM,
                sens=self.BEAT_SENS,
                subdiv=self.BEAT_SUBDIV,
                total_duration=total_duration,
            )
            bass_arr    = extract_bass_array(onset_env, spec_mag, total_frames)
            beat_frames = beat_times_to_frames(beat_times, self.FPS, total_frames)
            beat_frames = apply_density(beat_frames, self.BEAT_DENSITY)

            travel = resolve_travel(beat_frames, self.TRAVEL_FRAMES,
                                    self.BLOCK_SPEED)
            min_lane_gap = compute_min_lane_gap(beat_frames, travel,
                                                self.MAX_PER_LANE)
            print(f"[audio] {tempo_val:.1f} BPM  beats={len(beat_frames)} "
                  f"travel={travel}f  min_lane_gap={min_lane_gap}f  "
                  f"density={self.BEAT_DENSITY}  subdiv={self.BEAT_SUBDIV}")

            # Auto-pick n_lanes from ACTION when the user left N_LANES=0
            # (0 is the "auto" sentinel).  Both 'punch' and 'dance' now
            # use the 4-lane layout that rhythm.py emits by default, so
            # the auto value is 4 regardless of action.  Any positive
            # N_LANES still overrides this.
            n_lanes = int(self.N_LANES)
            if n_lanes <= 0:
                n_lanes = 4

            if self.LANE_FILTER is not None:
                lane_list = sorted(v + 1 for v in self.LANE_FILTER)
                print(f"[lane_filter] Enabled lanes (1-based): {lane_list}")

            events = schedule_events(
                beat_frames, bass_arr, self.FPS,
                travel=travel,
                min_gap_frames=self.BEAT_MIN_GAP,
                min_lane_gap=min_lane_gap,
                wall_prob=self.WALL_PROB,
                bass_thresh=self.BASS_THRESH,
                kind_mode=self.KIND_MODE,
                rng_seed=self.RNG_SEED,
                n_lanes=n_lanes,
                lane_filter=self.LANE_FILTER,
                modes=self.MODES or None,
                dance_pair_cycle=self.DANCE_PAIR_CYCLE,
                punch_pair_cycle=self.PUNCH_PAIR_CYCLE,
                line_beats=self.LINE_BEATS,
            )

            if self.EXPORT_EVENTS:
                save_events(self.EXPORT_EVENTS, events, meta={
                    'fps':       self.FPS,
                    'duration':  total_duration,
                    'tempo':     float(tempo_val),
                    'source':    self.BEAT_SOURCE,
                    'subdiv':    self.BEAT_SUBDIV,
                    'density':   self.BEAT_DENSITY,
                    'travel':    travel,
                    'speed':     self.BLOCK_SPEED,
                    'max_per_lane': self.MAX_PER_LANE,
                    'beat_min_gap': self.BEAT_MIN_GAP,
                    'wall_prob': self.WALL_PROB,
                    'bass_thresh': self.BASS_THRESH,
                    'seed':      self.RNG_SEED,
                    'kind_mode': self.KIND_MODE,
                    'action':    self.ACTION,
                    'mode':      (','.join(self.MODES) if self.MODES
                                  else None),
                    'dance_pair_cycle': int(self.DANCE_PAIR_CYCLE),
                    'punch_pair_cycle': int(self.PUNCH_PAIR_CYCLE),
                    'line_beats':       int(self.LINE_BEATS),
                    'n_lanes':   n_lanes,
                    'lanes':     (sorted(v + 1 for v in self.LANE_FILTER)
                                  if self.LANE_FILTER is not None else None),
                    'audio':     audio_file,
                })
                print(f"[export] Wrote {len(events)} events → {self.EXPORT_EVENTS}")

        # Default centered box (tall portrait) unless user overrode.
        if self.BOX is None:
            bw = int(self.WIDTH  * 0.24)
            bh = int(self.HEIGHT * 0.78)
            bx = self.WIDTH // 2 - bw // 2
            by = int(self.HEIGHT * 0.11)
            box = (bx, by, bw, bh)
        else:
            box = self.BOX

        # If FIT is enabled, shrink the output canvas to hug the stickman's
        # actual rendered body (not the whole reference box — the ref box
        # has ~22% empty space above the head and ~17% below the feet, which
        # is what shows up as "dư phần dưới" when you --fit the raw box).
        #
        # We read the body envelope from StickmanHUD._BODY_ENVELOPE_REF
        # (in reference coords), scale it by the box fit-factor, add --pad,
        # and then RE-ANCHOR the draw box so the body sits pad-pixels inside
        # the final canvas on all four sides.
        if self.FIT:
            bx, by, bw, bh = box
            sc = min(bw / StickmanHUD._REF_W, bh / StickmanHUD._REF_H)
            env = StickmanHUD._BODY_ENVELOPE_REF
            body_w_px = (env['x1'] - env['x0']) * sc
            body_h_px = (env['y1'] - env['y0']) * sc
            pad = max(0, int(self.PAD))
            new_w = int(round(body_w_px)) + 2 * pad
            new_h = int(round(body_h_px)) + 2 * pad
            # Keep dimensions even (required by yuv420p / NVENC).
            if new_w % 2: new_w += 1
            if new_h % 2: new_h += 1
            # Re-anchor the stickman box so env.x0 / env.y0 map to (pad, pad)
            # in the new canvas.  The box itself extends off-canvas on the
            # empty sides – that's fine, StickmanHUD uses absolute pixel
            # coords and only draws what lies inside the frame.
            new_bx = pad - int(round(env['x0'] * sc))
            new_by = pad - int(round(env['y0'] * sc))
            self.WIDTH, self.HEIGHT = new_w, new_h
            box = (new_bx, new_by, bw, bh)
            print(f"[fit] output canvas trimmed to {new_w}x{new_h} "
                  f"(pad={pad}px, body {int(body_w_px)}x{int(body_h_px)})")

        stick = StickmanHUD((self.WIDTH, self.HEIGHT),
                            action=self.ACTION,
                            box=box,
                            color=self.STICK_COLOR,
                            fps=self.FPS)
        stick.set_beat_events(events, self.FPS)
        print(f"[stickman] {len(events)} events  tween={stick._tween_dur:.3f}s "
              f"waypoints={len(stick._timeline)}  "
              f"box=({box[0]},{box[1]},{box[2]},{box[3]})")

        # Background setup
        bg_frame = None
        if self.BG_IMAGE:
            bg_frame = cv2.imread(self.BG_IMAGE, cv2.IMREAD_COLOR)
            if bg_frame is None:
                print(f"[bg] failed to load '{self.BG_IMAGE}', falling back "
                      f"to solid color")
            else:
                bg_frame = cv2.resize(bg_frame, (self.WIDTH, self.HEIGHT))

        all_frames: list[np.ndarray] = []
        print("Rendering frames...")
        t_render = time.time()
        last_pct = 0

        for fi in range(total_frames):
            pct = int(fi / max(1, total_frames) * 100)
            if pct // 10 > last_pct:
                elapsed = time.time() - t_render
                fps_r   = fi / elapsed if elapsed > 0 else 0
                eta     = (total_frames - fi) / fps_r if fps_r > 0 else 0
                print(f"Progress: {pct}% | FPS: {fps_r:.1f} | ETA: {eta:.1f}s")
                last_pct = pct // 10

            if bg_frame is not None:
                canvas = bg_frame.copy()
            else:
                canvas = np.full((self.HEIGHT, self.WIDTH, 3),
                                 self.BG_COLOR, dtype=np.uint8)
            stick.draw(canvas, fi)
            all_frames.append(canvas)

        print(f"\nFrame rendering done in {time.time()-t_render:.2f}s  "
              f"avg {total_frames/max(0.001,time.time()-t_render):.1f} FPS")

        # Write video
        temp_video = 'temp_stickman.mp4'
        print("Writing video (NVENC)..." if (not self.is_mac and _CUPY) else "Writing video...")
        t_write = time.time()
        if self.is_mac:
            fourcc = cv2.VideoWriter_fourcc(*'avc1')
            out = cv2.VideoWriter(temp_video, fourcc, self.FPS,
                                  (self.WIDTH, self.HEIGHT))
            for frm in all_frames:
                out.write(frm)
            out.release()
        else:
            vcodec = 'h264_nvenc' if _CUPY else 'libx264'
            preset = 'p4'         if vcodec == 'h264_nvenc' else 'fast'
            cmd = (f'ffmpeg -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 '
                   f'-s {self.WIDTH}x{self.HEIGHT} -r {self.FPS} -i pipe:0 '
                   f'-vcodec {vcodec} -preset {preset} -b:v 3500k '
                   f'-bf 0 -vsync cfr -pix_fmt yuv420p '
                   f'-r {self.FPS} "{temp_video}"')
            proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            for frm in all_frames:
                proc.stdin.write(frm.tobytes())
            proc.stdin.close()
            proc.wait()
        print(f"Video written in {time.time()-t_write:.2f}s")
        print(f"\nTotal time: {time.time()-t0:.2f}s")
        return temp_video

    # ---------------------------------------------------------- audio merge
    def merge_audio(self, temp_video: str, audio_file: str,
                    output_filename: str = 'stickman_output.mp4') -> bool:
        """Mux audio onto the rendered temp video.

        The temp video is already H.264, so we stream-copy the video
        track (``-c:v copy``) instead of re-encoding it.  This is:
          * instant (no second encode pass),
          * immune to NVENC minimum-dimension constraints (which can
            trigger when ``--fit`` shrinks the canvas),
          * lossless for the video stream.

        If stream-copy fails for some reason (rare), we fall back to a
        libx264 re-encode and print the real ffmpeg stderr so the cause
        is visible.
        """
        import os
        print("\nMerging audio...")
        t0 = time.time()

        if not os.path.exists(temp_video):
            print(f"Error merging audio: temp video '{temp_video}' not found.")
            return False

        def _run(cmd: list[str]) -> tuple[int, str]:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            _, err = proc.communicate()
            return proc.returncode, err.decode(errors='replace')

        # ── Attempt 1: stream-copy video, re-encode audio to AAC ──────────
        cmd_copy = ['ffmpeg', '-y', '-i', temp_video, '-i', audio_file,
                    '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                    '-shortest']
        if self.TIME_LIMIT:
            cmd_copy += ['-t', str(self.TIME_LIMIT)]
        cmd_copy.append(output_filename)

        rc, err = _run(cmd_copy)
        if rc != 0:
            print(f"[merge] stream-copy failed (rc={rc}); "
                  f"falling back to libx264 re-encode.")
            print("── ffmpeg stderr (copy attempt) ──")
            print(err.strip().splitlines()[-20:] if err else "(no stderr)")

            # ── Attempt 2: full re-encode (always works) ────────────────
            cmd_enc = ['ffmpeg', '-y', '-i', temp_video, '-i', audio_file,
                       '-map', '0:v:0', '-map', '1:a:0',
                       '-c:v', 'libx264', '-preset', 'fast', '-b:v', '3500k',
                       '-pix_fmt', 'yuv420p',
                       '-c:a', 'aac', '-b:a', '192k',
                       '-shortest']
            if self.TIME_LIMIT:
                cmd_enc += ['-t', str(self.TIME_LIMIT)]
            cmd_enc.append(output_filename)

            rc, err = _run(cmd_enc)
            if rc != 0:
                print("── ffmpeg stderr (re-encode attempt) ──")
                print(err)
                print(f"Error merging audio: ffmpeg exited with code {rc}.")
                return False

        if os.path.exists(temp_video):
            try:
                os.remove(temp_video)
            except OSError:
                pass
        print(f"Audio merged in {time.time()-t0:.2f}s → {output_filename}")
        return True


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_arguments():
    p = argparse.ArgumentParser(
        description="Stickman – beat-synced 2-D stickman effect")
    p.add_argument('-W', '--width',    type=int, default=1920)
    p.add_argument('-H', '--height',   type=int, default=1080)
    p.add_argument('--fps',            type=int, default=FPS)
    p.add_argument('-i', '--input',    type=str, required=True,
                   help='Input audio file')
    p.add_argument('-o', '--output',   type=str, required=True,
                   help='Output video file (.mp4)')
    p.add_argument('-d', '--duration', type=float, default=None,
                   help='Clip duration in seconds (full if omitted)')
    p.add_argument('-a', '--audio',    type=int, default=0,
                   help='Include source audio in the output (1/0)')

    p.add_argument('--action', type=str, default='punch',
                   choices=sorted(ACTIONS.keys()),
                   help=('Which action library to run. Each action defines '
                         'its own pose set + beat-to-pose mapping. '
                         "Default: 'punch' (boxer strikes). "
                         "Use 'combo' when mirroring rhythm.py --mode "
                         "punch,dance — pair with --mode to set the "
                         "punch/dance interleave sequence."))
    p.add_argument('--mode', type=str, default=None,
                   help=('Combo sub-mode sequence, mirrors rhythm.py '
                         '--mode.  Single value ("punch" / "dance") keeps '
                         'legacy L/R kinds.  Comma-combined "punch,dance" '
                         '(or "dance,punch") enables combo mode: events '
                         'alternate PL/PR and DL/DR per beat and the '
                         "stickman uses the 'combo' action library. "
                         'MUST match rhythm.py --mode to stay in sync.'))
    p.add_argument('--dance_pair_cycle', type=int, default=4, metavar='N',
                   help=('Dance paired-spawn cycle length (active when '
                         'enabled lanes contain ≥ 1 same-side adjacent '
                         'pair + dance/combo action).  N-1 đơn + 1 '
                         'CHỤM per cycle, chụm on the LAST beat.  '
                         'N=4 (default) ≈ one chụm per 4/4 bar.  '
                         'N=3 = legacy 2+1 triplet.  N=2 = heavy alt.  '
                         'N=1 = every beat chụm.  N≤0 = disabled.  '
                         'MUST match rhythm.py --dance_pair_cycle.'))
    p.add_argument('--line_beats', type=int, default=2, metavar='N',
                   help=('Line (hold-note) length in BEATS.  Stickman '
                         "holds the punch pose for ~N beats after a "
                         "'ZL'/'ZR' event so the arm stays extended "
                         "for the same duration rhythm.py's on-screen "
                         "bar slides past the camera.  Default 2.  "
                         'MUST match rhythm.py --line_beats.'))
    p.add_argument('--punch_pair_cycle', type=int, default=4, metavar='N',
                   help=('Punch paired-spawn cycle length, same model '
                         'as --dance_pair_cycle but for punch beats.  '
                         'N-1 đấm đơn + 1 đấm 2-tay per cycle; double '
                         "on the LAST beat → kind 'LL'/'RR' (stickman "
                         'throws both hands to one side).  N=4 default. '
                         'N=1 = every punch beat double.  N≤0 = '
                         'disabled.  MUST match rhythm.py '
                         '--punch_pair_cycle.'))

    # ── Beat detection (must match rhythm.py names + defaults) ───────────
    p.add_argument('--beat_source', type=str, default='tempo',
                   choices=['tempo', 'beat', 'onset'],
                   help=('"tempo" = uniform BPM-derived cadence (default). '
                         '"beat" = librosa beat_track. '
                         '"onset" = every transient.'))
    p.add_argument('--bpm',         type=float, default=None,
                   help='Force BPM in tempo mode (overrides auto-detect).')
    p.add_argument('--beat_sens',   type=float, default=0.5,
                   help='Beat sensitivity 0..1 (onset/beat modes). Default 0.5')
    p.add_argument('--beat_subdiv', type=int, default=1, choices=[1, 2, 4, 8],
                   help='Strikes per beat. 1=on beat, 2=eighths, 4=sixteenths.')
    p.add_argument('--beat_min_gap', type=int, default=4,
                   help=('Min frames between consecutive targets (too-close '
                         'beats are merged). MUST match rhythm.py to sync.'))
    p.add_argument('--density', type=float, default=1.0,
                   help=('Beat density multiplier. 0.5=half, 1.0=unchanged, '
                         '2.0=double. MUST match rhythm.py to sync.'))

    # ── Target-scheduler parity with rhythm.py ───────────────────────────
    p.add_argument('--travel', type=int, default=-1,
                   help=('Travel frames for a block (negative = auto from '
                         'BPM + --speed). MUST match rhythm.py --travel.'))
    p.add_argument('--speed', type=float, default=1.0,
                   help=('Block speed multiplier (auto-travel only). '
                         '1.0 = one block/lane visible, 0.5 = 2x slower.'))
    p.add_argument('--max_per_lane', type=int, default=3,
                   help='Hard cap on blocks visible per lane. Default 3.')
    p.add_argument('--wall_prob', type=float, default=0.12,
                   help=('Probability a strong-bass beat becomes a WALL event. '
                         '0 = never, 1 = always. Default 0.12.'))
    p.add_argument('--bass_thresh', type=float, default=0.60,
                   help='Bass energy threshold for wall spawns (0..1).')
    p.add_argument('--seed', type=int, default=42,
                   help=('Deterministic RNG seed for wall spawns. Use the '
                         'SAME seed on rhythm.py + stickman.py to sync walls.'))
    p.add_argument('--kinds', type=str, default='rhythm',
                   choices=['rhythm', 'alternate', 'all_L', 'all_R'],
                   help=('Lane pattern. "rhythm" = full rhythm.py scheduler '
                         '(use this for sync). "alternate" = simple L/R. '
                         '"all_L"/"all_R" = single-side.'))
    p.add_argument('--n_lanes', type=int, default=0,
                   help=('Lane count of the paired rhythm.py video (2 for '
                         'punch, 4 for dance).  0 = auto (picks 4 when '
                         '--action dance, 2 otherwise).  MUST match '
                         'rhythm.py when --kinds rhythm is used.'))
    p.add_argument('--lanes', type=str, default=None, metavar='SPEC',
                   help=('Restrict target spawns to the listed 1-based '
                         'lanes (must match rhythm.py --lanes when pairing '
                         'via --kinds rhythm).  Examples: "1,2", "1,4", '
                         '"1,2,3,4", "1-3".  "all" or omit = no filter.'))

    # ── Events file I/O (exact sync via JSON) ────────────────────────────
    p.add_argument('--events_file', type=str, default=None,
                   help=('Load pre-computed event timeline from JSON '
                         '(exported by rhythm.py --export_events).  When '
                         'set, all --beat_* / --travel / --speed flags are '
                         'ignored — events are used verbatim.'))
    p.add_argument('--export_events', type=str, default=None,
                   help='Save computed event timeline to JSON for later reuse.')

    # ── Visual ───────────────────────────────────────────────────────────
    p.add_argument('--color', type=str, default=None, metavar='COLOR',
                   help=('Stickman line color. Accepts "#RRGGBB", "RRGGBB" '
                         'or "R,G,B". Default: white.'))
    p.add_argument('--bg_color', type=str, default=None, metavar='COLOR',
                   help='Background solid color. Default: near-black.')
    p.add_argument('--bg_image', type=str, default=None,
                   help='Optional background image (PNG/JPG). Overrides bg_color.')
    p.add_argument('--box', type=str, default=None, metavar='X,Y,W,H',
                   help=('Override draw box in pixels as "x,y,w,h". '
                         'Default: centered portrait strip (~24%% x 78%% of frame).'))
    p.add_argument('--fit', type=int, default=0, metavar='0|1',
                   help=('Auto-crop the output video to tightly fit the '
                         'stickman box (+ --pad). The final resolution becomes '
                         '(box_w + 2*pad) x (box_h + 2*pad), ideal for '
                         'overlay compositing. Default 0 (full --width x --height).'))
    p.add_argument('--pad', type=int, default=24, metavar='PX',
                   help=('Padding in pixels around the stickman box when '
                         '--fit 1 is set. Default 24.'))

    p.add_argument('-t', '--token', type=str, default=None)
    p.add_argument('-u', '--url',   type=str, default=None)
    return p.parse_args()


def _parse_box(s: str | None, W: int, H: int) -> tuple | None:
    if s is None:
        return None
    parts = [p.strip() for p in s.split(',')]
    if len(parts) != 4 or not all(p.lstrip('-').isdigit() for p in parts):
        raise ValueError(f"Invalid --box '{s}', expected 'X,Y,W,H'.")
    x, y, w, h = (int(p) for p in parts)
    return (x, y, w, h)


if __name__ == '__main__':
    args = parse_arguments()
    if args.token:
        if not authourize_user(args.token, args.url):
            print("Authentication failed.")
            sys.exit(1)
    else:
        print("No token provided – authentication skipped.")
        sys.exit(1)

    try:
        stick_col = _parse_color(args.color) or CLR_WHITE
        bg_col    = _parse_color(args.bg_color) or CLR_BG
        box       = _parse_box(args.box, args.width, args.height)
    except ValueError as e:
        print(f"[color] {e}")
        sys.exit(1)

    viz = StickmanVisualizer()
    viz.WIDTH         = args.width
    viz.HEIGHT        = args.height
    viz.FPS           = args.fps
    viz.TIME_LIMIT    = args.duration
    viz.ACTION        = args.action
    # Resolve --mode into a modes list (empty list = scheduler falls
    # back to inferring from --action for pairing gates).  Auto-promote
    # to ACTION='combo' when the user asked for "punch,dance" but
    # forgot to set --action; that's the only stickman action that
    # understands the PL/DL typed kinds.
    if args.mode:
        parts = [m.strip().lower() for m in args.mode.split(',') if m.strip()]
        bad = [p for p in parts if p not in ('punch', 'dance', 'line')]
        if bad:
            print(f"[--mode] Unknown sub-mode(s) {bad}; use 'punch' / "
                  f"'dance' / 'line' / comma-combined e.g. "
                  f"'punch,dance,line'.")
            sys.exit(1)
        viz.MODES = parts
        # Multi-mode OR any mode that can't be rendered by the solo
        # action library → auto-promote to 'combo'.  'combo' is the
        # only action whose strike table covers every prefixed kind
        # ('PL'/'DL'/'ZL'/'JL'/'LL'…).
        needs_combo = (
            len(parts) >= 2
            or (len(parts) == 1 and parts[0] not in ('punch', 'dance', 'line'))
        )
        if needs_combo and viz.ACTION != 'combo':
            print(f"[--mode] {','.join(parts)} → auto-setting "
                  f"--action combo (was '{viz.ACTION}').")
            viz.ACTION = 'combo'
        # Solo 'line' mode → use the matching solo action so the
        # stickman has the HOLD_L/HOLD_R poses available.
        elif len(parts) == 1 and parts[0] == 'line' and viz.ACTION != 'line':
            print(f"[--mode] line → auto-setting --action line "
                  f"(was '{viz.ACTION}').")
            viz.ACTION = 'line'
    else:
        # No explicit --mode → infer single-mode list from --action so
        # the scheduler's pairing gates (punch-every-beat vs dance-2+1)
        # pick the correct rule for solo runs.  'combo' without --mode
        # is ambiguous; default it to punch,dance alternation.
        if viz.ACTION == 'combo':
            viz.MODES = ['punch', 'dance']
        elif viz.ACTION in ('punch', 'dance', 'line'):
            viz.MODES = [viz.ACTION]
    viz.BEAT_SOURCE   = args.beat_source
    viz.BEAT_BPM      = args.bpm
    viz.BEAT_SENS     = args.beat_sens
    viz.BEAT_SUBDIV   = args.beat_subdiv
    viz.BEAT_MIN_GAP  = args.beat_min_gap
    viz.BEAT_DENSITY  = args.density
    viz.TRAVEL_FRAMES = args.travel
    viz.BLOCK_SPEED   = args.speed
    viz.MAX_PER_LANE  = args.max_per_lane
    viz.WALL_PROB     = args.wall_prob
    viz.BASS_THRESH   = args.bass_thresh
    viz.RNG_SEED      = args.seed
    viz.KIND_MODE     = args.kinds
    viz.N_LANES       = int(args.n_lanes)
    viz.DANCE_PAIR_CYCLE = int(args.dance_pair_cycle)
    viz.PUNCH_PAIR_CYCLE = int(args.punch_pair_cycle)
    viz.LINE_BEATS       = max(1, int(args.line_beats))
    try:
        # Resolve lane-spec against the n_lanes that will actually be used
        # at render time (auto → 4 when n_lanes=0, same rule as in
        # StickmanVisualizer.process_video).
        _nl_for_filter = int(args.n_lanes) if int(args.n_lanes) > 0 else 4
        viz.LANE_FILTER = _parse_lanes_spec(args.lanes, _nl_for_filter)
    except ValueError as e:
        print(f"[--lanes] {e}")
        sys.exit(1)
    viz.EVENTS_FILE   = args.events_file
    viz.EXPORT_EVENTS = args.export_events
    viz.STICK_COLOR   = stick_col
    viz.BG_COLOR      = bg_col
    viz.BG_IMAGE      = args.bg_image
    viz.BOX           = box
    viz.FIT           = bool(args.fit)
    viz.PAD           = max(0, int(args.pad))

    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.',
                exist_ok=True)

    temp = viz.process_video(args.input)
    if temp:
        out_path = args.output if args.output.endswith('.mp4') \
            else args.output + '.mp4'
        if args.audio:
            viz.merge_audio(temp, args.input, out_path)
        else:
            import shutil
            shutil.move(temp, out_path)
            print(f"Video saved to: {out_path}")
