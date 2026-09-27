# scripts/vendor

Bundled third-party driver binaries for **parallel-port EEG triggers**, plus their license.
`scripts/install_parallel_port_driver.ps1` copies the appropriate DLL into `System32`/`SysWOW64`.

## What's here

| File | SHA-256 | Purpose |
|------|---------|---------|
| `inpoutx64.dll` | `5f27ed4d5cd58a1ee23deeb802e09e73f3a1d884ce2135f6e827f67b171269e7` | 64-bit InpOut driver — what xpman (64-bit Python/psychopy) uses |
| `inpout32.dll` | `01bcec6ddb4964e1f5b69ba1bd3876221d8de7ae17cacfac66f095013434a78f` | 32-bit InpOut driver (kept for completeness) |
| `inpout-license.txt` | — | InpOut MIT license (redistribution requires including it) |

**Provenance:** these are the InpOut binaries by Phil Gibbons (<https://www.highrez.co.uk/downloads/inpout32/>).
The bundled files are **byte-for-byte identical** to both the current official download and the copy
shipped with the legacy Java "XP Manager" this project replaces (verified by the SHA-256 above), so
the parallel-port path behaves exactly as it did on the legacy rig. Licensed **MIT** — redistribution
is permitted, which is why they can live in the repo (the license file must travel with them).

## Why bundled (not download-on-demand)

xpman is meant to be shared with other labs: bundling means a colleague can clone and use the parallel
port with no manual download, and pins the exact, verified binary instead of depending on an external
URL that could change or disappear.

## Using it

`psychopy.parallel` loads the driver via `ctypes.windll.inpoutx64`, i.e. it must be discoverable on the
system (`System32`/`SysWOW64`/PATH), not just present here. Run
`scripts/install_parallel_port_driver.ps1` (it self-elevates via a UAC prompt) to copy `inpoutx64.dll`
into place, then restart xpman and confirm with **"Test triggers…"** in the Launch dialog. Verify real
trigger output on the amplifier per `docs/verification_protocol.md`.

> **Not using a parallel port?** If your amplifier is driven by a USB trigger box (e.g. the BioSemi USB
> Trigger Interface), you do **not** need any of this — select the **Serial (USB)** backend instead.
>
> **Antivirus note:** InpOut provides raw hardware-port I/O, so some antivirus/EDR tools heuristically
> flag it as a "hacktool"/PUA. This is a known false positive for this widely-used driver (the bundled
> file matches the official release hash above); allow-list it if your scanner quarantines it.
