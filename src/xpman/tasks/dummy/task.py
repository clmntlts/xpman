"""``DummyTask``: the Phase 2 proving-ground task.

Flashes a square at a fixed rate for a fixed duration, sending one trigger code per flip and
logging every flip timestamp + trigger-sent timestamp to ``ctx.event_sink``. Exists solely to
exercise ``runtime.engine`` + ``hardware.trigger``/``clock`` + ``runtime.logging_sink`` end to
end -- see ``docs/verification_protocol.md`` for how this gets validated against real hardware
(oscilloscope/logic analyzer + photodiode) before any FPVS-specific work begins. Nothing about
this task's visual design or timing values is meant to resemble a real paradigm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.dummy.schema import DummyConditionParams, DummySchema

if TYPE_CHECKING:
    import psychopy.visual


class DummyTask(TaskModule):
    task_id = "dummy"
    display_name = "Dummy timing/trigger proving-ground"
    schema = DummySchema()

    def __init__(self) -> None:
        self._stim: "psychopy.visual.Rect | None" = None

    def prepare(self, ctx: TaskContext) -> None:
        """Build the flashing stimulus once per Run. Imports ``psychopy.visual`` lazily so
        this module stays importable without a display/PsychoPy environment present."""
        import psychopy.visual as visual

        self._stim = visual.Rect(ctx.window, width=1, height=1, units="pix", fillColor="black")
        ctx.event_sink.log("prepare", {"task_id": self.task_id})

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        if self._stim is None:
            raise RuntimeError("run_trial called before prepare()")

        params = DummyConditionParams.model_validate(trial_params)
        self._stim.size = (params.square_size_pix, params.square_size_pix)

        n_flips = max(round(params.duration_seconds * params.flip_rate_hz), 0)
        flips_completed = 0
        is_white = False

        ctx.event_sink.log(
            "trial_start", {"trial_index": trial_index, "n_flips_requested": n_flips}
        )

        for flip_index in range(n_flips):
            if ctx.abort_check():
                break

            is_white = not is_white
            self._stim.fillColor = "white" if is_white else "black"

            # Bind the trigger to the vsync: window.callOnFlip runs the callback the instant the
            # next flip() swaps buffers (the edge the amplifier timestamps), so the code lands at
            # the flip rather than after flip() returns -- the same tightened path the FPVS loop
            # uses (see tasks/fpvs/paradigm_oddball.py). Registered before draw()/flip(); the code
            # set here is cleared on the *next* iteration's flip (and the trailing clear_code after
            # the loop resets the port after the final flip), giving a ~1-frame pulse without any
            # blocking hold after flip.
            ctx.window.callOnFlip(ctx.trigger.set_code, params.trigger_code)
            self._stim.draw()

            # win.flip() blocks until the vertical blank and returns that wall-clock time when
            # the window was built with waitBlanking=True (xpman.hardware.display's default) --
            # this is the precise, frame-locked timestamp; ctx.clock is only a fallback for the
            # (non-timing-critical) case where blanking wasn't requested. The set_code registered
            # above has fired by the time flip() returns.
            flip_time = ctx.window.flip()
            if flip_time is None:
                flip_time = ctx.clock.get_time()

            ctx.event_sink.log(
                "flip",
                {"trial_index": trial_index, "flip_index": flip_index, "color": "white" if is_white else "black"},
                timestamp=flip_time,
            )
            ctx.event_sink.log(
                "trigger_sent",
                {"trial_index": trial_index, "flip_index": flip_index, "code": params.trigger_code},
                timestamp=flip_time,
            )
            # Clear the code on the next frame's flip so the pulse spans ~one refresh, matching the
            # FPVS non-onset frames. The final flip's code is reset by the trailing clear_code below.
            ctx.window.callOnFlip(ctx.trigger.clear_code)
            flips_completed += 1

        # Reset the port after the final flip (its clear is registered on a flip that never comes).
        ctx.trigger.clear_code()

        ctx.event_sink.log(
            "trial_end", {"trial_index": trial_index, "flips_completed": flips_completed}
        )

        return TrialResult(
            outcome_summary={
                "flips_completed": flips_completed,
                "flips_requested": n_flips,
                "aborted": flips_completed < n_flips,
            }
        )

    def cleanup(self, ctx: TaskContext) -> None:
        self._stim = None
        ctx.event_sink.log("cleanup", {"task_id": self.task_id})
