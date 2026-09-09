"""``AuditoryFPVSTask``: the launchable auditory FPAS (Fast Periodic Auditory Stimulation) task.

Plays a periodic stream of gated sound tokens with a periodic oddball, firing an EEG trigger at each
token onset -- the auditory analogue of the visual FPVS task, with the sound card's sample clock as
the timing master. It composes the already-tested pure layers: the schema (parameters), ``schedule``
(sample-clock math), ``engine.plan_trial`` (whole-trial pre-render + trigger schedule), and
``sound_pool`` (multi-exemplar token loading). Audio comes out through the ``AudioPlayer`` on the
context (a silent ``NullAudioPlayer`` in dev/CI, real PsychPortAudio at the rig), and triggers are
fired on the main thread at each token's scheduled onset (dev plan §1c).

**Timing is not verified until the rig calibration passes** (see ``docs/audio_calibration_gate.md`` /
``audio_calibration_rig_procedure.md``). This task is launchable now so it can be configured and
piloted -- exactly as the visual dummy task shipped before its hardware verification -- but the
calibration gate (advisory, at launch) and a real loopback measurement remain the bar before trusting
its onset timing for recording.
"""

from __future__ import annotations

import time

from xpman.tasks.auditory_fpvs.advisories import condition_advisories
from xpman.tasks.auditory_fpvs.engine import plan_trial
from xpman.tasks.auditory_fpvs.schema import (
    AuditoryFPVSConditionParams,
    AuditoryFPVSSchema,
    SoundSelector,
)
from xpman.tasks.auditory_fpvs.schedule import (
    achieved_frequency_hz,
    oddball_period_tokens,
    samples_per_cycle,
)
from xpman.tasks.auditory_fpvs.sound_pool import (
    _resample,
    decode_mono,
    equalize_pools,
    gate_token,
    select_files,
)
from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.fpvs.response import ResponseCollector

#: How far ahead of "now" playback is scheduled, so the device has started before the first token
#: onset the trigger loop waits on. Small relative to a trial; generous relative to device start-up.
_PLAYBACK_LEAD_SECONDS = 0.2


class AuditoryFPVSTask(TaskModule):
    task_id = "auditory_fpvs"
    display_name = "Auditory FPAS (periodic oddball)"
    schema = AuditoryFPVSSchema()

    def __init__(self) -> None:
        self._audio_opened = False
        self._opened_config: tuple | None = None
        # Two-stage sound cache so no trial pays a file-decode cost in its hot path (mirrors how the
        # visual task GPU-uploads every image up front). ``_decode_cache`` holds each file's raw mono
        # waveform at its NATIVE rate, decoded exactly once (in on_before_run, before trial 1, so the
        # expensive libsndfile decode is front-loaded). ``_resample_cache`` holds the cheap, lazily
        # computed resample to a given output rate, keyed by (path, sample_rate_hz). Per-trial pool
        # assembly (gate + optional RMS equalization) then runs against these cached arrays with no
        # soundfile.read on the run_trial path.
        self._decode_cache: dict[str, tuple] = {}
        self._resample_cache: dict[tuple[str, int], object] = {}
        # ONE keyboard collector for the whole Run, created lazily on the first trial that has an
        # active attention overlay and reused after (PsychoPy's key buffer attaches more reliably
        # than a fresh Keyboard per trial). None until then, and left None if construction fails so a
        # headless/no-keyboard run still works (0 responses). See _ensure_response_collector.
        self._response_collector: ResponseCollector | None = None
        # Calibration gate: evaluated once per Run (on the first trial, when the audio device + its
        # tags are known) and cached, so its advisory status is logged and carried into run metadata.
        self._gate_evaluated = False
        self._gate_summary: dict = {}

    # -- lifecycle -----------------------------------------------------------------------------
    def prepare(self, ctx: TaskContext) -> None:
        ctx.event_sink.log("prepare", {"task_id": self.task_id})
        # A real (non-silent) audio backend means unverified onset timing on this machine -- log a
        # banner so the provenance is explicit. The interactive launch gate (advisory + override) is
        # wired into the launch UI separately; the engine never blocks.
        backend = ctx.audio_player.describe().get("backend")
        if backend not in (None, "none"):
            ctx.event_sink.log(
                "auditory_timing_unverified",
                {"backend": backend,
                 "note": "onset timing not verified on this machine; run the audio calibration"},
            )

    def on_before_run(self, ctx: TaskContext) -> None:
        """Decode every audio file in the resource directory once, up front, before trial 1 -- the
        auditory analogue of the visual task eagerly GPU-uploading every discovered image here.

        Without this, each file is decoded lazily the first trial that happens to draw it, so
        trial-start latency depends on which files that trial's randomized pool draw touches for the
        first time -- inconsistent trial to trial, and worst on trial 1. Front-loading the decode
        means every trial (trial 1 included) assembles its pools from an already-warm cache with no
        ``soundfile.read`` on the hot path.

        Decode is at each file's NATIVE rate (this hook has no Condition params, so the output sample
        rate isn't known yet), keyed by path. The cheap per-Condition resample to the actual output
        rate is done lazily in ``_load_pools`` and cached by (path, sample_rate_hz). Every audio file
        under the resource directory is decoded, not just the ones a given Condition selects, since
        prepare()/on_before_run() don't see the trial sequence."""
        files = select_files(ctx.resource_dir, SoundSelector())
        for path in files:
            self._decode(path)
        ctx.event_sink.log("sounds_preloaded", {"n_sounds": len(files)})

    def cleanup(self, ctx: TaskContext) -> None:
        try:
            ctx.audio_player.stop()
        finally:
            if self._audio_opened:
                ctx.audio_player.close()
                self._audio_opened = False
            self._opened_config = None  # fresh provenance for the next Run
            self._decode_cache.clear()
            self._resample_cache.clear()
            # Drop the Run-wide keyboard collector so the next Run builds a fresh one.
            self._response_collector = None
            self._gate_evaluated = False
            self._gate_summary = {}
            ctx.event_sink.log("cleanup", {"task_id": self.task_id})

    # -- per trial -----------------------------------------------------------------------------
    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        params = AuditoryFPVSConditionParams.model_validate(trial_params)
        self._ensure_audio_open(ctx, params)
        self._evaluate_calibration_gate(ctx, params)
        base_tokens, oddball_tokens = self._load_pools(ctx, params)

        planned = plan_trial(
            params, base_tokens=base_tokens, oddball_tokens=oddball_tokens, rng=ctx.rng
        )

        # --- Attention overlays (build) ------------------------------------------------------
        # Schedule + apply every ACTIVE attention overlay (currently just the volume-decrement catch
        # task) BEFORE playback: an auditory overlay modifies the pre-rendered buffer in place (there
        # is nothing to draw per frame), so the attenuation must be baked in before the buffer is
        # handed to the device. Returns the (overlay, events) pairs for end-of-trial scoring and a
        # token_index -> targets lookup for firing onsets/triggers inside the loop below. Kept as a
        # self-contained helper so it merges cleanly with other edits to this method.
        scored_overlays, onset_targets = self._build_overlays(ctx, params, planned)

        # Start the pre-rendered buffer slightly in the future and anchor every trigger to the
        # reported start time (dev plan §2) plus each token's known offset.
        when = ctx.clock.get_time() + _PLAYBACK_LEAD_SECONDS
        start = ctx.audio_player.play(planned.buffer, when=when)

        triggers_fired = 0
        onsets_reached = 0
        aborted = False
        for token_index, ev in enumerate(planned.triggers):
            target = start + ev.onset_seconds
            if not self._wait_until(ctx, target):
                aborted = True
                break
            onsets_reached += 1
            if ev.code is not None:
                # MMBT-S in Pulse Mode self-clears (~8 ms), so we do not clear between onsets; a
                # single clear at trial end covers latching boxes. set_code lands at the scheduled
                # onset; the true command->sound latency is what the rig calibration measures.
                ctx.trigger.set_code(ev.code)
            # Capture the ACTUAL clock time the code was placed, right after set_code returns (mirrors
            # the visual task logging the real flip_time). The event timestamp stays the sample-exact
            # intended onset (`target`) -- that is the meaningful time for frequency analysis -- but
            # logging fired_at exposes the wait-loop/serial overshoot (software jitter), which is
            # otherwise invisible in the event stream. See run_trial docstring / calibration.
            fired_at = ctx.clock.get_time()
            ctx.event_sink.log(
                "token_onset",
                {"trial_index": trial_index, "is_oddball": ev.is_oddball, "code": ev.code,
                 "fired_at_seconds": fired_at},
                timestamp=target,
            )
            if ev.code is not None:
                ctx.event_sink.log(
                    "trigger_sent",
                    {"trial_index": trial_index, "code": ev.code, "is_oddball": ev.is_oddball,
                     "fired_at_seconds": fired_at, "software_jitter_seconds": fired_at - target},
                    timestamp=target,
                )
                triggers_fired += 1
            # --- Attention overlays (onset) --------------------------------------------------
            # If this token is an overlay target, stamp its fire time (for RT scoring) and log its
            # onset + optional trigger. Self-contained; a no-op when no overlay targets this token.
            self._fire_overlay_onsets(ctx, onset_targets, token_index, target, trial_index)

        if not aborted:
            # Let the buffer play out so the trial has its full duration before the next trial starts.
            self._wait_until(ctx, start + planned.duration_seconds)
        ctx.audio_player.stop()
        ctx.trigger.clear_code()

        # --- Attention overlays (score) ------------------------------------------------------
        # Poll the shared keyboard collector once, then score each overlay that ran against its own
        # events (only fired ones count, so an aborted trial doesn't inflate misses). Self-contained.
        overlay_scores = self._score_overlays(ctx, scored_overlays, trial_index)
        overlay_outcome: dict = {}
        for overlay in params.all_overlays():
            overlay_outcome.update(overlay.outcome_fields(overlay_scores.get(overlay.spawn_key)))

        sr = params.audio.sample_rate_hz
        achieved_base = achieved_frequency_hz(sr, samples_per_cycle(sr, params.base.base_freq_hz))
        # The oddball has NO independent sample grid -- it is every N-th base token, so its realised
        # rate is the achieved base rate divided by that integer period, NOT achieved_frequency_hz of
        # the requested oddball value (which would report a rate the stimulus never produces). This is
        # the frequency a downstream FFT pipeline must place the oddball bin at.
        oddball_period = oddball_period_tokens(params.base.base_freq_hz, params.oddball.oddball_freq_hz)
        achieved_oddball = achieved_base / oddball_period
        return TrialResult(
            outcome_summary={
                "n_base_tokens": planned.n_base,
                "n_oddball_tokens": planned.n_oddball,
                "onsets_reached": onsets_reached,
                "triggers_fired": triggers_fired,
                "requested_base_freq_hz": params.base.base_freq_hz,
                "achieved_base_freq_hz": achieved_base,
                "requested_oddball_freq_hz": params.oddball.oddball_freq_hz,
                "achieved_oddball_freq_hz": achieved_oddball,
                "oddball_period_tokens": oddball_period,
                "sample_rate_hz": sr,
                "trial_duration_seconds": params.base.trial_duration_seconds,
                "fade_in_seconds": planned.fade_in_seconds,
                "fade_out_seconds": planned.fade_out_seconds,
                "aborted": aborted,
                **overlay_outcome,
            }
        )

    # -- GUI/advisory surfaces -----------------------------------------------------------------
    def check_triggers(self, condition_params: dict, *, resource_dir: str | None = None) -> list[str]:
        """Design-time advisories: the parameter-only advisories (sample-exactness, ratio, rate,
        gating -- see ``advisories.condition_advisories``) plus, when a ``resource_dir`` is given, a
        pool-availability check (empty/single-exemplar pools). Never raises for content problems."""
        params = AuditoryFPVSConditionParams.model_validate(condition_params)
        warnings = list(condition_advisories(params))
        if resource_dir is not None:
            pool_files: dict[str, set] = {}
            for label, selector in (("base", params.base_selector), ("oddball", params.oddball_selector)):
                try:
                    files = set(select_files(resource_dir, selector))
                except OSError:
                    files = set()
                pool_files[label] = files
                if len(files) == 0:
                    warnings.append(f"{label} sound pool matches no files under the resource directory.")
                elif len(files) == 1:
                    warnings.append(
                        f"{label} sound pool has only 1 file -- a single repeated token invites "
                        "low-level adaptation; use multiple exemplars."
                    )
            # Resource-aware overlap check: even when the selectors DIFFER, they may resolve to
            # overlapping files (no category contrast on the shared ones -> a weakened/invalid oddball
            # response). The identical-selector case is already flagged parameter-only by
            # condition_advisories; this catches partial overlap that needs the actual files to see.
            overlap = pool_files.get("base", set()) & pool_files.get("oddball", set())
            if overlap and not (
                params.base_selector.subdirectory == params.oddball_selector.subdirectory
                and params.base_selector.filename_pattern == params.oddball_selector.filename_pattern
            ):
                warnings.append(
                    f"base and oddball pools share {len(overlap)} file(s) -- those tokens carry no "
                    "category change at the oddball rate, weakening (or invalidating) the oddball "
                    "response. Use disjoint sound sets for base and oddball."
                )
        return warnings

    def describe_condition_resources(self, condition_params: dict, resource_dir: str) -> list[str]:
        params = AuditoryFPVSConditionParams.model_validate(condition_params)
        lines: list[str] = []
        for label, selector in (("Base", params.base_selector), ("Oddball", params.oddball_selector)):
            files = select_files(resource_dir, selector)
            sample = ", ".join(p.name for p in files[:3])
            more = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
            lines.append(f"{label} pool: {len(files)} sound(s)" + (f": {sample}{more}" if files else ""))
        return lines

    def run_metadata(self) -> dict:
        return {
            "audio_config": self._opened_config_summary(),
            "calibration_gate": self._gate_summary,
        }

    # -- attention overlays --------------------------------------------------------------------
    def _build_overlays(self, ctx: TaskContext, params: AuditoryFPVSConditionParams, planned):
        """Schedule and apply every active attention overlay before playback, and prepare the shared
        keyboard collector. Returns ``(scored_overlays, onset_targets)`` where ``scored_overlays`` is
        a list of ``(overlay, events)`` (in ``active_overlays`` order, for end-of-trial scoring) and
        ``onset_targets`` maps a token index to the ``(overlay, event)`` targets landing on it (for
        the run loop). Both empty when no attention task is enabled -- the trial then plays exactly as
        rendered, at zero cost.

        Each overlay is scheduled from its OWN decoupled ``ctx.rng.spawn(1)`` sub-stream so enabling
        one overlay never perturbs another's schedule or the stimulus order. ``apply_to_buffer``
        modifies ``planned.buffer`` in place (an auditory overlay changes the pre-rendered audio, it
        does not draw). ``fade_in_seconds``/``fade_out_seconds`` are read defensively via ``getattr``
        because they are added to the schema by a separate change; absent, targets simply aren't kept
        clear of a fade that isn't there."""
        active = params.active_overlays()
        scored_overlays: list = []
        onset_targets: dict[int, list] = {}
        if not active:
            return scored_overlays, onset_targets

        sr = params.audio.sample_rate_hz
        fade_in = float(getattr(params, "fade_in_seconds", 0.0) or 0.0)
        fade_out = float(getattr(params, "fade_out_seconds", 0.0) or 0.0)
        token_len_samples = max(round(params.token.duration_seconds * sr), 1)
        for overlay in active:
            events = overlay.schedule(
                planned.triggers, sr, ctx.rng.spawn(1)[0],
                fade_in_seconds=fade_in, fade_out_seconds=fade_out,
            )
            overlay.apply_to_buffer(planned.buffer, sr, token_len_samples, events)
            scored_overlays.append((overlay, events))
            for event in events:
                onset_targets.setdefault(event.token_index, []).append((overlay, event))
            ctx.event_sink.log(
                "overlay_scheduled", {"overlay": overlay.spawn_key, "n_targets": len(events)}
            )

        self._ensure_response_collector(ctx)
        return scored_overlays, onset_targets

    def _ensure_response_collector(self, ctx: TaskContext) -> None:
        """Create the Run-wide keyboard collector on first use and clear it for this trial. Guarded
        so a headless/no-PsychoPy environment (dev/CI, NullTrigger) degrades to no key capture rather
        than crashing: the collector already falls back internally, and any construction failure
        leaves ``_response_collector`` None (0 responses). Clearing at trial start drops any stray
        pre-trial press so it isn't attributed to this trial."""
        if self._response_collector is None:
            try:
                self._response_collector = ResponseCollector(enabled=True)
                ctx.event_sink.log("keyboard_ready", {"backend": self._response_collector.backend})
            except Exception as exc:  # noqa: BLE001 - never let key capture break a run/test
                self._response_collector = None
                ctx.event_sink.log("keyboard_unavailable", {"error": str(exc)})
        if self._response_collector is not None:
            self._response_collector.clear()

    def _fire_overlay_onsets(self, ctx: TaskContext, onset_targets, token_index, target, trial_index) -> None:
        """When a token that is an overlay target onsets: stamp the event's fire time (``onset_time``,
        for RT scoring), log the overlay's onset event, and fire its optional trigger. A no-op when no
        overlay targets this token. NOTE: a catch trigger coincides with the base/oddball trigger on
        the same token onset -- it is off by default (``trigger_code=None``); when set it is emitted
        after the base/oddball code on that onset."""
        for overlay, event in onset_targets.get(token_index, ()):
            event.onset_time = target
            ctx.event_sink.log(
                overlay.onset_event_type,
                {**overlay.onset_payload(event), "trial_index": trial_index},
                timestamp=target,
            )
            code = overlay.trigger_code_for(event)
            if code is not None:
                ctx.trigger.set_code(code)
                fired_at = ctx.clock.get_time()
                ctx.event_sink.log(
                    "trigger_sent",
                    {"trial_index": trial_index, "code": code, "overlay": overlay.spawn_key,
                     "fired_at_seconds": fired_at, "software_jitter_seconds": fired_at - target},
                    timestamp=target,
                )

    def _score_overlays(self, ctx: TaskContext, scored_overlays, trial_index) -> dict:
        """Poll the keyboard once and score each overlay that ran. Presses are partitioned by each
        overlay's own keys (one shared collector, as two Keyboard instances would fight over
        PsychoPy's single device buffer). Returns ``{spawn_key: score}`` for the overlays that ran;
        empty when none did (or no key capture was available)."""
        scores: dict = {}
        if not scored_overlays:
            return scores
        presses = self._response_collector.collect() if self._response_collector is not None else []
        ctx.event_sink.log(
            "keyboard_captured",
            {
                "n": len(presses),
                "source": self._response_collector.last_source if self._response_collector else "disabled",
                "backend": self._response_collector.backend if self._response_collector else "disabled",
                "keys": [{"name": r.key_name, "time": r.time} for r in presses],
            },
        )
        for overlay, events in scored_overlays:
            keys = set(overlay.params.keys)
            overlay_responses = [r for r in presses if r.key_name in keys]
            score = overlay.score(overlay_responses, events)
            scores[overlay.spawn_key] = score
            ctx.event_sink.log(
                overlay.scored_event_type,
                {**overlay.scored_payload(score), "trial_index": trial_index},
            )
        return scores

    # -- internals -----------------------------------------------------------------------------
    def _ensure_audio_open(self, ctx: TaskContext, params: AuditoryFPVSConditionParams) -> None:
        cfg = (params.audio.sample_rate_hz, params.audio.latency_class, params.audio.buffer_size,
               params.audio.output_device)
        if self._audio_opened:
            if cfg != self._opened_config:
                opened_sr = self._opened_config[0] if self._opened_config else None
                if params.audio.sample_rate_hz != opened_sr:
                    # A DIFFERENT sample rate mid-Run corrupts timing silently: the device stays at the
                    # first rate, but the buffer is rendered and every trigger onset is computed at the
                    # new rate (onset_sample / new_sr) -- so the token grid plays at the wrong speed and
                    # the trigger-to-sound alignment drifts across the trial. There is no safe way to
                    # honour it without reopening the device, so fail loudly instead of playing wrong.
                    ctx.event_sink.log(
                        "audio_sample_rate_conflict",
                        {"opened_hz": opened_sr, "requested_hz": params.audio.sample_rate_hz},
                    )
                    raise RuntimeError(
                        f"audio.sample_rate_hz changed mid-Run ({opened_sr} Hz -> "
                        f"{params.audio.sample_rate_hz} Hz). The audio device is opened once per Run, so "
                        "every Condition in a Program must use the same sample_rate_hz. Use one rate "
                        "across the Program's Conditions."
                    )
                # Non-rate device params (latency_class/buffer_size/device) can't be re-applied to the
                # open device either, but they don't corrupt the rendered timing -- record and keep the
                # first device rather than churning it.
                ctx.event_sink.log("audio_config_change_ignored",
                                   {"opened": list(self._opened_config), "requested": list(cfg)})
            return
        ctx.audio_player.open(
            sample_rate_hz=params.audio.sample_rate_hz,
            latency_class=params.audio.latency_class,
            buffer_size=params.audio.buffer_size,
            output_device=params.audio.output_device,
        )
        self._audio_opened = True
        self._opened_config = cfg
        ctx.event_sink.log("audio_device_opened", ctx.audio_player.describe())

    def _evaluate_calibration_gate(self, ctx: TaskContext, params: AuditoryFPVSConditionParams) -> None:
        """Evaluate the per-machine audio-timing calibration gate once per Run (first trial) and log
        the result, so an uncalibrated or under-budget machine is on record and recoverable in
        analysis. This is the runtime side of the advisory gate: it never blocks (the interactive
        confirm/override belongs to the launch UI), but it turns the calibration profile from
        unconnected tooling into a logged, run-metadata fact.

        The machine fingerprint comes from the audio backend (``None`` for the silent dev/Null player,
        so the gate is skipped with a log line). The profile store directory is taken from the
        Instance params (``audio_profiles_dir``), defaulting to ``data/audio_profiles``. When a passing
        profile is found, its measured mean latency is recorded (a constant trigger offset analysis can
        subtract) -- the correction is not applied to trigger timing here (that is gated on the
        timing-realization work and the offset's threshold-referencing caveat)."""
        if self._gate_evaluated:
            return
        self._gate_evaluated = True
        tags = [params.base.base_freq_hz, params.oddball.oddball_freq_hz]

        fingerprint = ctx.audio_player.machine_fingerprint()
        if fingerprint is None:
            self._gate_summary = {"status": "SKIPPED", "reason": "no real audio device on this backend"}
            ctx.event_sink.log("auditory_calibration_skipped", self._gate_summary)
            return

        from pathlib import Path

        from xpman.audio.gate import evaluate_gate
        from xpman.audio.profile import ProfileStore

        profiles_dir = Path(ctx.instance_params.get("audio_profiles_dir") or "data/audio_profiles")
        profile = ProfileStore(profiles_dir).lookup(fingerprint)
        result = evaluate_gate(fingerprint, profile, tags)
        self._gate_summary = {
            "status": result.status,
            "requires_confirmation": result.requires_confirmation,
            "budget_seconds": result.budget_seconds,
            "warnings": list(result.warnings),
            "fingerprint_id": fingerprint.fingerprint_id,
            # Recorded (not applied) so analysis can subtract this constant trigger offset if wanted.
            "mean_latency_seconds": (
                profile.stats.mean_latency_seconds if (profile is not None and result.ok) else None
            ),
        }
        ctx.event_sink.log("auditory_calibration_gate", self._gate_summary)

    def _decode(self, path) -> tuple:
        """Raw mono waveform at the file's native rate, decoded at most once per path (cached). This
        is the only place ``soundfile`` I/O happens; on_before_run warms it for every file so no trial
        pays a decode."""
        key = str(path)
        if key not in self._decode_cache:
            self._decode_cache[key] = decode_mono(path)
        return self._decode_cache[key]

    def _resampled(self, path, sample_rate_hz: int):
        """The file's mono waveform resampled to ``sample_rate_hz`` (cheap, band-limited), cached by
        (path, sample_rate_hz). Decodes on a cache miss, so this is safe even if on_before_run hasn't
        run -- but after it has, every decode is already cached and only the resample is computed."""
        key = (str(path), sample_rate_hz)
        if key not in self._resample_cache:
            mono, native_rate = self._decode(path)
            self._resample_cache[key] = _resample(mono, native_rate, sample_rate_hz)
        return self._resample_cache[key]

    def _load_pools(self, ctx: TaskContext, params: AuditoryFPVSConditionParams):
        """Assemble this Condition's (base, oddball) gated-token pools with NO file I/O on the hot
        path: select the files, pull each one's decoded+resampled waveform from the cache (decoding on
        a cache miss), gate each to a fixed-length token, and -- when equalization is enabled -- scale
        every token toward the combined pool's mean RMS. Raises ``FileNotFoundError`` if a selector
        matches nothing, since a Condition can't run without at least one token per pool."""
        sr = params.audio.sample_rate_hz
        tok = params.token
        pools: list[list] = []
        for label, selector in (("base", params.base_selector), ("oddball", params.oddball_selector)):
            files = select_files(ctx.resource_dir, selector)
            if not files:
                raise FileNotFoundError(
                    f"no audio files matched the {label} selector "
                    f"(subdirectory={selector.subdirectory!r}, "
                    f"filename_pattern={selector.filename_pattern!r}) under {ctx.resource_dir!r}"
                )
            pools.append([gate_token(self._resampled(p, sr), sr, tok) for p in files])
        base_tokens, oddball_tokens = pools
        if params.equalization.enabled:
            base_tokens, oddball_tokens = equalize_pools(
                base_tokens, oddball_tokens, strength=params.equalization.strength
            )
        return base_tokens, oddball_tokens

    def _wait_until(self, ctx: TaskContext, target: float) -> bool:
        """Block until ``ctx.clock`` reaches ``target`` (seconds), polling ``abort_check``. Returns
        False if the run was aborted before reaching it. Sleeps in short slices so an abort is
        responsive and the CPU isn't pinned."""
        while True:
            remaining = target - ctx.clock.get_time()
            if remaining <= 0:
                return True
            if ctx.abort_check():
                return False
            time.sleep(min(remaining, 0.002))

    def _opened_config_summary(self) -> dict:
        if self._opened_config is None:
            return {}
        sr, lat, buf, dev = self._opened_config
        return {"sample_rate_hz": sr, "latency_class": lat, "buffer_size": buf, "output_device": dev}
