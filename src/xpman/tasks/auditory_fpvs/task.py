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
from xpman.tasks.auditory_fpvs.schedule import achieved_frequency_hz, samples_per_cycle
from xpman.tasks.auditory_fpvs.sound_pool import (
    _resample,
    decode_mono,
    equalize_pools,
    gate_token,
    select_files,
)
from xpman.tasks.base import TaskContext, TaskModule, TrialResult

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
            self._decode_cache.clear()
            self._resample_cache.clear()
            ctx.event_sink.log("cleanup", {"task_id": self.task_id})

    # -- per trial -----------------------------------------------------------------------------
    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        params = AuditoryFPVSConditionParams.model_validate(trial_params)
        self._ensure_audio_open(ctx, params)
        base_tokens, oddball_tokens = self._load_pools(ctx, params)

        planned = plan_trial(
            params, base_tokens=base_tokens, oddball_tokens=oddball_tokens, rng=ctx.rng
        )

        # Start the pre-rendered buffer slightly in the future and anchor every trigger to the
        # reported start time (dev plan §2) plus each token's known offset.
        when = ctx.clock.get_time() + _PLAYBACK_LEAD_SECONDS
        start = ctx.audio_player.play(planned.buffer, when=when)

        triggers_fired = 0
        onsets_reached = 0
        aborted = False
        for ev in planned.triggers:
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
            ctx.event_sink.log(
                "token_onset",
                {"trial_index": trial_index, "is_oddball": ev.is_oddball, "code": ev.code},
                timestamp=target,
            )
            if ev.code is not None:
                ctx.event_sink.log(
                    "trigger_sent",
                    {"trial_index": trial_index, "code": ev.code, "is_oddball": ev.is_oddball},
                    timestamp=target,
                )
                triggers_fired += 1

        if not aborted:
            # Let the buffer play out so the trial has its full duration before the next trial starts.
            self._wait_until(ctx, start + planned.duration_seconds)
        ctx.audio_player.stop()
        ctx.trigger.clear_code()

        sr = params.audio.sample_rate_hz
        return TrialResult(
            outcome_summary={
                "n_base_tokens": planned.n_base,
                "n_oddball_tokens": planned.n_oddball,
                "onsets_reached": onsets_reached,
                "triggers_fired": triggers_fired,
                "requested_base_freq_hz": params.base.base_freq_hz,
                "achieved_base_freq_hz": achieved_frequency_hz(sr, samples_per_cycle(sr, params.base.base_freq_hz)),
                "requested_oddball_freq_hz": params.oddball.oddball_freq_hz,
                "achieved_oddball_freq_hz": achieved_frequency_hz(sr, samples_per_cycle(sr, params.oddball.oddball_freq_hz)),
                "sample_rate_hz": sr,
                "trial_duration_seconds": params.base.trial_duration_seconds,
                "fade_in_seconds": planned.fade_in_seconds,
                "fade_out_seconds": planned.fade_out_seconds,
                "aborted": aborted,
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
            for label, selector in (("base", params.base_selector), ("oddball", params.oddball_selector)):
                try:
                    n = len(select_files(resource_dir, selector))
                except OSError:
                    n = 0
                if n == 0:
                    warnings.append(f"{label} sound pool matches no files under the resource directory.")
                elif n == 1:
                    warnings.append(
                        f"{label} sound pool has only 1 file -- a single repeated token invites "
                        "low-level adaptation; use multiple exemplars."
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
        return {"audio_config": self._opened_config_summary()}

    # -- internals -----------------------------------------------------------------------------
    def _ensure_audio_open(self, ctx: TaskContext, params: AuditoryFPVSConditionParams) -> None:
        cfg = (params.audio.sample_rate_hz, params.audio.latency_class, params.audio.buffer_size,
               params.audio.output_device)
        if self._audio_opened:
            if cfg != self._opened_config:
                # Reopening the device mid-run is disruptive; the audio config is expected to be
                # constant across a Program's Conditions. Keep the first device and record the
                # mismatch rather than silently churning it.
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
