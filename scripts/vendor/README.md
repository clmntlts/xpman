# scripts/vendor

This folder holds **third-party driver binaries that xpman does not ship** (licensing / provenance),
which `scripts/install_parallel_port_driver.ps1` copies into place.

## inpoutx64.dll (parallel-port EEG triggers)

`psychopy.parallel` drives the parallel port through the **InpOut** kernel driver, which is not
bundled with psychopy or xpman. If it is missing, psychopy sets `parallel.ParallelPort = None`, so:

- **the driver install script reports "not found" and installs nothing**, and
- **selecting the Parallel-port trigger backend fails** (xpman shows: *"No parallel-port driver is
  available…"*) — a run would otherwise silently deliver **zero** triggers to the amplifier.

To install it (once per machine):

1. Download the InpOut binaries from the official source:
   <https://www.highrez.co.uk/downloads/inpout32/>
2. Extract the **64-bit** `inpoutx64.dll`.
3. Copy it here, next to this README: `scripts/vendor/inpoutx64.dll`
4. Run `scripts/install_parallel_port_driver.ps1` (it self-elevates via a UAC prompt) to copy the DLL
   into `System32` and `SysWOW64`, then restart xpman.

Confirm it works with **"Test triggers…"** in the Launch dialog, and verify real trigger output on
the amplifier per `docs/verification_protocol.md`.

> This DLL is intentionally **not** committed to the repository. If you use serial (USB) triggers
> instead of a parallel port, you do not need it at all.
