; Inno Setup script for xpman -- wraps the existing PyInstaller build
; (dist\xpman\, produced by scripts\build_windows_exe.ps1) into a single
; click-through xpman-setup-<version>.exe.
;
; Not run directly: use scripts\build_installer.ps1, which rebuilds dist\xpman\ fresh,
; reads the version out of pyproject.toml, and invokes ISCC.exe with /DMyAppVersion=...
; (see that script for why: never build an installer from a stale dist\, and there is
; deliberately no version string hand-duplicated here).
;
; Per-user install (PrivilegesRequired=lowest): no admin rights needed for the main
; install, matching how VS Code/Discord/etc. install by default -- works on a lab PC
; where the researcher may not have admin rights. The one thing that DOES need admin is
; the optional, unchecked-by-default parallel-port-driver step below, which requests its
; own separate elevation only if the user opts into it.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{B8B6C7C4-9B7C-4F2B-8B2C-8C7B1A6E5D3F}
AppName=xpman
AppVersion={#MyAppVersion}
AppPublisher=UCLouvain Face Categorization Lab
DefaultDirName={localappdata}\Programs\xpman
DefaultGroupName=xpman
UninstallDisplayIcon={app}\xpman.exe
LicenseFile=..\LICENSE
OutputDir=output
OutputBaseFilename=xpman-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; The entire existing PyInstaller one-folder build, unmodified.
;
; Deliberately no [UninstallDelete] section: xpman.gui.app._default_base_dir() writes the
; researcher's SQLite database + Parquet run logs to {app}\data\ (next to the exe). Inno
; Setup's default uninstaller only removes files it itself installed -- {app}\data\ (created
; later, at runtime) is correctly left untouched on uninstall. Verified empirically (installed,
; ran the app so data\xpman.db existed, uninstalled): the app files/registry entry/Start Menu
; shortcut were removed, data\ was not. This is the right default for a research tool --
; uninstalling to fix something must never silently destroy collected experiment data.
Source: "..\dist\xpman\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Bundled so the optional post-install step below can offer to run it against an installed
; copy, not just a dev checkout -- scripts\vendor\ may not exist/be empty on this build
; machine (see docs/architecture.md's Windows 11 parallel-port driver caveat), hence
; skipifsourcedoesntexist on the vendor subfolder specifically.
Source: "..\scripts\install_parallel_port_driver.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\vendor\*"; DestDir: "{app}\scripts\vendor"; Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist ignoreversion

[Icons]
Name: "{group}\xpman"; Filename: "{app}\xpman.exe"
Name: "{group}\Uninstall xpman"; Filename: "{uninstallexe}"
Name: "{autodesktop}\xpman"; Filename: "{app}\xpman.exe"; Tasks: desktopicon

[Run]
; Deliberately unchecked by default and elevated separately from the (unelevated, per-user)
; main install -- this is the one step that touches System32 and only matters on a machine
; actually wired to the EEG amplifier via parallel port. install_parallel_port_driver.ps1
; itself requires -RunAsAdministrator but does not self-elevate, so it's launched here via
; Start-Process -Verb RunAs to trigger its own UAC prompt.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -Command ""Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \""{app}\scripts\install_parallel_port_driver.ps1\""' -Verb RunAs"""; \
    Description: "Install the parallel-port EEG trigger driver (admin required -- only needed if this machine connects to the amplifier via a parallel port)"; \
    Flags: postinstall unchecked skipifsilent
Filename: "{app}\xpman.exe"; Description: "Launch xpman now"; Flags: postinstall nowait skipifsilent unchecked
