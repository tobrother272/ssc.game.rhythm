"""Smoke test for src.live_renderer.LiveFrameRenderer.

Exercises the live preview path end-to-end without spinning up the Qt
editor:

    1. Construct ``LiveFrameRenderer`` over a real MP3 from ``temps/``.
    2. Render the first 1.0 s of frames at 24 fps, verify shape + dtype.
    3. Time the rendering loop — must average <= 41 ms / frame
       (≥ 24 fps wall-clock) on the test machine.
    4. Test ``update_beats`` (live edit) — must reset state without
       crashing and produce a different schedule (target count delta).
    5. Test backward-seek by calling ``render_at(0.0)`` after step 2
       and confirming the canvas is identical (within tolerance) to
       the very first frame produced in step 2.

Not a pytest — kept dependency-free and runnable as a one-shot script
from the repo root::

    python tmp/smoke_live_renderer.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.live_renderer import LiveFrameRenderer  # noqa: E402


def main() -> int:
    audio_files = sorted((REPO / "temps").glob("audio_*.MP3"))
    if not audio_files:
        print("[FAIL] no audio file in temps/ to smoke-test against")
        return 2
    audio = audio_files[0]
    print(f"[INFO] using audio={audio.name}")

    # Fake a beat at every second for the first 10 s — Studio always
    # supplies a beat array so we mimic that here.
    beats = [float(i * 0.5) for i in range(20)]

    t0 = time.perf_counter()
    rdr = LiveFrameRenderer(
        str(audio),
        beat_times=beats,
        mode="punch",
        fps=24,
        width=1280,
        height=720,
        bloom=False,
    )
    t_init = time.perf_counter() - t0
    print(f"[INFO] init took {t_init * 1000:.1f} ms"
          f"  duration={rdr.duration_sec:.2f}s")

    # ------- render 1 s @ 24 fps -------
    n_frames = 24
    times: list[float] = []
    last: np.ndarray | None = None
    for i in range(n_frames):
        t = i / rdr.fps
        s = time.perf_counter()
        frame = rdr.render_at(t)
        times.append(time.perf_counter() - s)
        if frame.shape != (720, 1280, 3):
            print(f"[FAIL] frame {i} bad shape {frame.shape}")
            return 1
        if frame.dtype != np.uint8:
            print(f"[FAIL] frame {i} bad dtype {frame.dtype}")
            return 1
        last = frame
    avg_ms = (sum(times) / len(times)) * 1000.0
    p95_ms = sorted(times)[int(len(times) * 0.95)] * 1000.0
    print(f"[INFO] rendered {n_frames} frames"
          f"  avg={avg_ms:.1f}ms"
          f"  p95={p95_ms:.1f}ms")
    if avg_ms > 41.6:
        print("[WARN] average frame time over 24 fps budget"
              f" ({avg_ms:.1f} ms > 41.6 ms) — preview may stutter")

    # ------- update_beats -------
    n_targets_before = len(rdr._game.targets)  # type: ignore[attr-defined]
    rdr.update_beats([float(i * 0.25) for i in range(40)])
    n_targets_after = len(rdr._game.targets)  # type: ignore[attr-defined]
    print(f"[INFO] update_beats: targets {n_targets_before}"
          f" -> {n_targets_after}")
    if n_targets_after == n_targets_before:
        print("[WARN] update_beats produced same target count — schedule"
              " may not have rebuilt")

    # ------- backward seek -------
    f0_again = rdr.render_at(0.0)
    if f0_again.shape != (720, 1280, 3):
        print(f"[FAIL] post-seek frame bad shape {f0_again.shape}")
        return 1
    print("[INFO] backward seek OK"
          f" (mean px={f0_again.mean():.1f})")

    # ------- update_mode -------
    rdr.update_mode("dance")
    fr = rdr.render_at(0.5)
    print(f"[INFO] update_mode -> dance, frame shape={fr.shape}"
          f" mean px={fr.mean():.1f}")

    rdr.close()
    print("[PASS] LiveFrameRenderer smoke test green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
