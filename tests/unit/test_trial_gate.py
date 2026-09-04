"""Tests for runtime.trial_gate: the between-trials gate (manual keypress / auto delay).

PsychoPy's ``event``/``visual``/``core`` are patched so no display or real keyboard is needed --
the same style as the FPVS task tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from xpman.runtime.trial_gate import TrialAdvanceMode, _info_text, make_trial_gate


def _fake_clock(times):
    clock = MagicMock()
    clock.get_time.side_effect = list(times)
    return clock


# ---------------------------------------------------------------------------
# _info_text
# ---------------------------------------------------------------------------


def test_info_text_manual_with_info_shows_trial_and_prompt():
    text = _info_text(TrialAdvanceMode.MANUAL, trial_index=0, n_trials=20, show_info=True)
    assert "Trial 1 of 20" in text
    assert "Press SPACE" in text


def test_info_text_manual_without_info_is_prompt_only():
    text = _info_text(TrialAdvanceMode.MANUAL, trial_index=4, n_trials=20, show_info=False)
    assert text == "Press SPACE to start"


def test_info_text_auto_with_info_has_no_prompt():
    text = _info_text(TrialAdvanceMode.AUTO, trial_index=2, n_trials=10, show_info=True)
    assert text == "Trial 3 of 10"


def test_info_text_auto_without_info_is_empty():
    assert _info_text(TrialAdvanceMode.AUTO, trial_index=0, n_trials=10, show_info=False) == ""


def test_info_text_omits_total_when_n_trials_zero():
    text = _info_text(TrialAdvanceMode.MANUAL, trial_index=0, n_trials=0, show_info=True)
    assert "Trial 1" in text
    assert "of" not in text


# ---------------------------------------------------------------------------
# make_trial_gate: manual
# ---------------------------------------------------------------------------


def test_manual_gate_returns_when_advance_key_pressed():
    window = MagicMock()
    # First poll: no key; second poll: space pressed.
    with patch("psychopy.event.getKeys", side_effect=[[], ["space"]]) as mock_keys, patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, MagicMock(), mode=TrialAdvanceMode.MANUAL, seconds=0.0, show_info=True,
            n_trials=5, abort_check=lambda: False,
        )
        gate(0)

    assert mock_keys.call_count == 2
    window.flip.assert_called_once()


def test_manual_gate_returns_early_on_abort():
    window = MagicMock()
    aborted = {"v": False}

    def abort_check():
        return aborted["v"]

    # getKeys never returns the key; abort flips true on the second loop check.
    call = {"n": 0}

    def keys(*a, **k):
        call["n"] += 1
        if call["n"] >= 1:
            aborted["v"] = True
        return []

    with patch("psychopy.event.getKeys", side_effect=keys), patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, MagicMock(), mode=TrialAdvanceMode.MANUAL, seconds=0.0, show_info=False,
            n_trials=5, abort_check=abort_check,
        )
        gate(0)  # must return, not hang


# ---------------------------------------------------------------------------
# make_trial_gate: auto
# ---------------------------------------------------------------------------


def test_auto_gate_waits_until_deadline():
    window = MagicMock()
    # get_time: initial (deadline base) then values crossing deadline (base+2.0).
    clock = _fake_clock([10.0, 10.5, 11.5, 12.5])
    with patch("psychopy.core.wait") as mock_wait, patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=2.0, show_info=True,
            n_trials=3, abort_check=lambda: False,
        )
        gate(0)

    assert mock_wait.call_count >= 1  # waited at least one poll interval
    window.flip.assert_called_once()


def test_auto_gate_zero_seconds_returns_immediately():
    window = MagicMock()
    clock = _fake_clock([10.0, 10.0])
    with patch("psychopy.core.wait") as mock_wait, patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=0.0, show_info=False,
            n_trials=3, abort_check=lambda: False,
        )
        gate(0)

    mock_wait.assert_not_called()  # deadline already reached, no waiting


def test_auto_gate_aborts_early():
    window = MagicMock()
    clock = _fake_clock([10.0, 10.1, 10.2])
    with patch("psychopy.core.wait"), patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=100.0, show_info=False,
            n_trials=3, abort_check=lambda: True,  # abort immediately
        )
        gate(0)  # must return promptly despite the 100s nominal delay


# ---------------------------------------------------------------------------
# make_trial_gate: periodic break / instructions (break_every_n_trials)
# ---------------------------------------------------------------------------


def test_break_disabled_by_default_leaves_routine_gate_unchanged():
    window = MagicMock()
    clock = _fake_clock([10.0, 10.0])
    with patch("psychopy.core.wait") as mock_wait, patch("psychopy.visual.TextStim") as mock_text:
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=0.0, show_info=False,
            n_trials=10, abort_check=lambda: False,
        )
        gate(0)  # trial 0 -- would be a break if break_every_n_trials were set

    mock_wait.assert_not_called()  # AUTO, 0s deadline already reached -- no wait, no break
    mock_text.assert_not_called()  # show_info=False, not a break -> nothing drawn


def test_break_fires_at_trial_zero_and_every_n_after():
    window = MagicMock()
    with patch("psychopy.event.getKeys", return_value=["space"]), patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim") as mock_text:
        gate = make_trial_gate(
            window, MagicMock(), mode=TrialAdvanceMode.AUTO, seconds=999.0, show_info=True,
            n_trials=10, abort_check=lambda: False,
            break_every_n_trials=5, break_text="Take a short break.",
        )
        for trial_index in (0, 5, 10):  # every one of these is a break trial
            gate(trial_index)

    assert mock_text.call_count == 3
    for call in mock_text.call_args_list:
        assert "Take a short break." in call.kwargs["text"]
        assert "Press SPACE to continue" in call.kwargs["text"]


def test_break_does_not_fire_between_multiples():
    window = MagicMock()
    clock = _fake_clock([10.0, 10.0])
    with patch("psychopy.core.wait") as mock_wait, patch("psychopy.visual.TextStim") as mock_text:
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=0.0, show_info=False,
            n_trials=10, abort_check=lambda: False,
            break_every_n_trials=5, break_text="Break!",
        )
        gate(3)  # not a multiple of 5 -> routine AUTO gate, no break text, no wait

    mock_wait.assert_not_called()
    mock_text.assert_not_called()


def test_break_waits_for_keypress_even_in_auto_mode():
    """A break must not silently time out on the routine AUTO delay -- it needs deliberate
    dismissal, exactly like a MANUAL gate, regardless of the Program's chosen trial_advance mode."""
    window = MagicMock()
    clock = MagicMock()
    clock.get_time.return_value = 0.0  # would be an instant AUTO no-op if break used AUTO logic
    with patch("psychopy.event.getKeys", side_effect=[[], ["space"]]) as mock_keys, patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, clock, mode=TrialAdvanceMode.AUTO, seconds=0.0, show_info=False,
            n_trials=1, abort_check=lambda: False,
            break_every_n_trials=1, break_text="Instructions here.",
        )
        gate(0)

    assert mock_keys.call_count == 2  # actually polled for the key, not an instant AUTO return


def test_break_honors_abort():
    window = MagicMock()
    with patch("psychopy.event.getKeys", return_value=[]), patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim"):
        gate = make_trial_gate(
            window, MagicMock(), mode=TrialAdvanceMode.MANUAL, seconds=0.0, show_info=False,
            n_trials=1, abort_check=lambda: True,  # abort immediately
            break_every_n_trials=1, break_text="Instructions.",
        )
        gate(0)  # must return promptly, not hang waiting for a key that never comes


def test_break_with_empty_text_still_shows_prompt_and_waits():
    window = MagicMock()
    with patch("psychopy.event.getKeys", return_value=["space"]), patch(
        "psychopy.event.clearEvents"
    ), patch("psychopy.core.wait"), patch("psychopy.visual.TextStim") as mock_text:
        gate = make_trial_gate(
            window, MagicMock(), mode=TrialAdvanceMode.AUTO, seconds=0.0, show_info=False,
            n_trials=1, abort_check=lambda: False,
            break_every_n_trials=1, break_text="",  # toggle on, no custom message
        )
        gate(0)

    assert mock_text.call_args.kwargs["text"] == "Press SPACE to continue"
