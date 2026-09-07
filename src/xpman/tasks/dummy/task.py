"""``DummyTask``: the Phase 2 proving-ground task.

Flashes a square at a fixed rate for a fixed duration, toggling colour (and sending one trigger
code) every ``round(refresh / flip_rate_hz)`` monitor frames while flipping the window every
frame, and logging every per-frame flip timestamp + trigger-sent timestamp to ``ctx.event_sink``.
Exists solely to exercise ``runtime.engine`` + ``hardware.trigger``/``clock`` +
``runtime.logging_sink`` end to end -- see ``docs/verification_protocol.md`` for how this gets
validated against real hardware (oscilloscope/logic analyzer + photodiode) before any
FPVS-specific work begins. Nothing about this task's visual design or timing values is meant to
resemble a real paradigm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.dummy.schema import DummyConditionParams, DummySchema

if TYPE_CHECKING:
    import psychopy.visual

#: Refresh rate assumed when ``getActualFrameRate()`` can't return a stable reading. Unlike the
#: FPVS task (which aborts, because a wrong refresh silently corrupts real EEG frequencies), this
#: proving-ground task falls back and keeps running -- a slightly-off colour-toggle cadence doesn't
#: invalidate a pipeline/timing smoke test -- but the fallback is always logged, never silent.
FALLBACK_REFRESH_RATE_HZ = 60.0


class DummyTask(TaskModule):
    task_id = "dummy"
    display_name = "Dummy timing/trigger proving-ground"
    schema = DummySchema()

    def __init__(self) -> None:
        self._stim: "psychopy.visual.Rect | None" = None
        self._refresh_rate_hz: float = FALLBACK_REFRESH_RATE_HZ
        self._refresh_measured_successfully: bool = False

    def prepare(self, ctx: TaskContext) -> None:
        """Build the flashing stimulus and measure the monitor's refresh once per Run. Imports
        ``psychopy.visual`` lazily so this module stays importable without a display/PsychoPy
        environment present."""
        import psychopy.visual as visual

        self._stim = visual.Rect(ctx.window, width=1, height=1, units="pix", fillColor="black")
        ctx.event_sink.log("prepare", {"task_id": self.task_id})

        # Measure the real refresh once (an expensive call) so run_trial can hold each colour for a
        # whole number of frames -> flip_rate_hz is actually honoured (see run_trial). float():
        # getActualFrameRate() returns numpy.float64 on real hardware; keep event payloads native.
        measured = ctx.window.getActualFrameRate()
        self._refresh_measured_successfully = measured is not None
        if measured is None:
            self._refresh_rate_hz = FALLBACK_REFRESH_RATE_HZ
            ctx.event_sink.log("refresh_rate_measurement_failed", {"fallback_hz": FALLBACK_REFRESH_RATE_HZ})
        else:
            self._refresh_rate_hz = float(measured)
        ctx.event_sink.log(
            "refresh_rate_measured",
            {
                "refresh_rate_hz": self._refresh_rate_hz,
                "measured_successfully": self._refresh_measured_successfully,
            },
        )

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        if self._stim is None:
            raise RuntimeError("run_trial called before prepare()")

        params = DummyConditionParams.model_validate(trial_params)
        self._stim.size = (params.square_size_pix, params.square_size_pix)

        # Pace the colour flips to flip_rate_hz by holding each colour for a whole number of
        # *monitor frames*: the window still flips every frame (so inter-flip timing is captured
        # per refresh -- what docs/verification_protocol.md item 1 and core.verification_report
        # expect), but the square only TOGGLES colour, and a trigger only fires, every
        # frames_per_half_cycle frames. This mirrors FPVS's frames_per_cycle. (Previously the loop
        # toggled every frame and ran duration*flip_rate *frames* total, so flip_rate_hz set only
        # the frame count, never the rate: a "2 Hz / 30 s" request flashed at the refresh rate for
        # ~1 s. See docs/verification_protocol.md.)
        refresh_hz = self._refresh_rate_hz
        frames_per_half_cycle = max(round(refresh_hz / params.flip_rate_hz), 1)
        total_frames = max(round(params.duration_seconds * refresh_hz), 0)
        n_flips_requested = max(round(params.duration_seconds * params.flip_rate_hz), 0)
        achieved_flip_rate_hz = refresh_hz / frames_per_half_cycle

        flips_completed = 0  # colour toggles actually shown (== triggers sent)
        frames_rendered = 0
        is_white = False

        # trial_start/trial_end boundary markers are emitted by runtime.engine (see issue #22),
        # so this task does not log its own -- the counts below are in the returned outcome_summary.
        for frame_index in range(total_frames):
            if ctx.abort_check():
                break

            is_onset = frame_index % frames_per_half_cycle == 0
            if is_onset:
                is_white = not is_white
                self._stim.fillColor = "white" if is_white else "black"
                # Bind the trigger to the vsync: window.callOnFlip runs the callback the instant
                # the next flip() swaps buffers (the edge the amplifier timestamps), so the code
                # lands at the flip rather than after flip() returns -- the same tightened path the
                # FPVS loop uses (see tasks/fpvs/paradigm_oddball.py).
                ctx.window.callOnFlip(ctx.trigger.set_code, params.trigger_code)
            elif frame_index % frames_per_half_cycle == 1:
                # Clear on the frame right after the onset so the pulse spans ~one refresh, matching
                # the FPVS non-onset frames. A no-op on auto-pulse boxes (e.g. the MMBT-S in Pulse
                # Mode, which self-clears after 8 ms), an active reset-to-0 on latching ones.
                ctx.window.callOnFlip(ctx.trigger.clear_code)

            self._stim.draw()

            # win.flip() blocks until the vertical blank and returns that wall-clock time when the
            # window was built with waitBlanking=True (xpman.hardware.display's default) -- the
            # precise, frame-locked timestamp; ctx.clock is only a fallback when blanking wasn't
            # requested. Any callOnFlip registered above has fired by the time flip() returns.
            flip_time = ctx.window.flip()
            if flip_time is None:
                flip_time = ctx.clock.get_time()

            ctx.event_sink.log(
                "flip",
                {
                    "trial_index": trial_index,
                    "frame_index": frame_index,
                    "color": "white" if is_white else "black",
                    "is_onset": is_onset,
                },
                timestamp=flip_time,
            )
            if is_onset:
                ctx.event_sink.log(
                    "trigger_sent",
                    {"trial_index": trial_index, "flip_index": flips_completed, "code": params.trigger_code},
                    timestamp=flip_time,
                )
                flips_completed += 1
            frames_rendered += 1

        # Reset the port after the final frame (a clear may have been registered on a flip that
        # never comes; auto-pulse boxes ignore it anyway).
        ctx.trigger.clear_code()

        return TrialResult(
            outcome_summary={
                "flips_completed": flips_completed,
                "flips_requested": n_flips_requested,
                "frames_rendered": frames_rendered,
                "frames_per_half_cycle": frames_per_half_cycle,
                "achieved_flip_rate_hz": achieved_flip_rate_hz,
                "measured_refresh_hz": refresh_hz,
                "refresh_measured_successfully": self._refresh_measured_successfully,
                "aborted": frames_rendered < total_frames,
            }
        )

    def run_metadata(self) -> dict:
        """Persist the measured refresh onto the Run row (read by runtime.engine), so a dummy run's
        provenance records the rate its flip pacing used -- the same key FPVS exposes."""
        return {
            "measured_refresh_hz": self._refresh_rate_hz,
            "refresh_measured_successfully": self._refresh_measured_successfully,
        }

    def cleanup(self, ctx: TaskContext) -> None:
        self._stim = None
        ctx.event_sink.log("cleanup", {"task_id": self.task_id})
