"""Between-trials "gate": what the subject/experimenter sees *before* each trial starts.

The legacy app's default "Trials starting pattern" was to pause between trials -- either
waiting for a keypress (the experimenter advances when the subject is ready and the EEG looks
clean) or auto-advancing after a fixed delay -- optionally showing a "trial N of M" text. For
real FPVS sessions this is essential: trials are ~60 s sequences, and running them back-to-back
with no break is not how EEG data is collected. This module provides that pause as a small,
task-agnostic hook the runtime engine calls before every trial (``execute_run``'s
``on_before_trial``), so it applies to any task, exactly as the legacy Program-level setting
did.

Kept out of ``engine.py`` itself (which deliberately never draws) and out of the tasks (this is
cross-task run behavior, not paradigm logic). ``make_trial_gate`` builds the closure the engine
calls; the drawing/waiting is PsychoPy-coupled but injectable-window-friendly, so it unit-tests
with a mock window + patched keyboard the same way ``tasks/fpvs`` does.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import psychopy.visual

    from xpman.hardware.clock import Clock

#: Key the manual gate waits for. Space matches the legacy default (``StartKey`` = "Space").
_ADVANCE_KEY = "space"

#: How often the manual/auto wait loop yields to check for the advance key / elapsed time /
#: abort, in seconds. Coarse on purpose -- this is a between-trials idle wait, not a
#: timing-critical loop, so there's no need to spin every frame.
_POLL_INTERVAL_SECONDS = 0.01


class TrialAdvanceMode(str, enum.Enum):
    MANUAL = "manual"  # wait for the experimenter/subject to press the advance key
    AUTO = "auto"  # wait a fixed number of seconds, then start automatically


def _info_text(mode: TrialAdvanceMode, trial_index: int, n_trials: int, show_info: bool) -> str:
    """The message shown on the gate screen. ``trial_index`` is 0-based; displayed 1-based."""
    lines = []
    if show_info:
        position = f"Trial {trial_index + 1}" + (f" of {n_trials}" if n_trials else "")
        lines.append(position)
    if mode is TrialAdvanceMode.MANUAL:
        lines.append("Press SPACE to start")
    return "\n".join(lines)


def make_trial_gate(
    window: "psychopy.visual.Window",
    clock: "Clock",
    *,
    mode: TrialAdvanceMode,
    seconds: float,
    show_info: bool,
    n_trials: int,
    abort_check: Callable[[], bool] = lambda: False,
) -> Callable[[int], None]:
    """Build the ``on_before_trial(trial_index)`` hook the engine calls before each trial.

    ``mode=MANUAL`` shows the gate text and blocks until the advance key is pressed;
    ``mode=AUTO`` shows it (if ``show_info``) and blocks for ``seconds``. Both poll
    ``abort_check`` and return early if it becomes true, so an abort requested during a long
    manual wait is honored promptly (the engine re-checks ``abort_check`` right after the hook
    returns and stops before running the trial).
    """
    import psychopy.event as event
    import psychopy.visual as visual

    def gate(trial_index: int) -> None:
        text = _info_text(mode, trial_index, n_trials, show_info)
        if text:
            stim = visual.TextStim(window, text=text, units="norm", height=0.08)
            stim.draw()
        window.flip()

        if mode is TrialAdvanceMode.MANUAL:
            event.clearEvents()
            while not abort_check():
                if _ADVANCE_KEY in event.getKeys(keyList=[_ADVANCE_KEY]):
                    return
                _wait(_POLL_INTERVAL_SECONDS)
            return

        # AUTO: wait `seconds`, polling abort so a long delay can still be cut short.
        deadline = clock.get_time() + seconds
        while clock.get_time() < deadline and not abort_check():
            _wait(_POLL_INTERVAL_SECONDS)

    return gate


def _wait(seconds: float) -> None:
    """Sleep briefly without a hard dependency on psychopy at import time (imported lazily,
    matching the rest of this module's PsychoPy usage)."""
    import psychopy.core as core

    core.wait(seconds)
