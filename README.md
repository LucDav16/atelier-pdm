# Atelier PDM  ·  2.9.5 beta

**A free product data management (PDM) system for small engineering teams.** One shared vault for your CAD files,
with check-out / check-in, automatic part numbering, revisions and a release workflow, history and roll back,
assemblies and drawings that stay linked when parts are renamed, bills of materials, CAM & tool libraries, and
companion panels docked beside the CAD program.

Works with **SolidCraft**, **FreeCAD**, **PhysCAD** and **CamCraft** · Windows 10/11 · Python / Tkinter

> **Beta.** Test it on a copy of your work or a test vault first. Please report problems under **Issues**.

## Try it
1. Download `AtelierPDM-Setup-2.9.5.exe` from **Releases** and run it (SmartScreen: *More info › Run anyway* -
   the installer isn't code-signed).
2. In the setup wizard choose **This PC › Start a new vault** (e.g. `C:\Test Vault`).
3. Read `docs/Atelier PDM - Installation and User Manual.pdf`, or press **F1** in the program.

## Run from source
```
python -m pip install -r requirements.txt
python atelier_launcher.py                      # the main window
python atelier_launcher.py --panel solidcraft   # the SolidCraft panel (or physcad)
python atelier_launcher.py --cam                # CAM & Tools
```

## Build the installer
Install **Python 3.10+** and **Inno Setup 6** (`winget install JRSoftware.InnoSetup`), then run **`build.bat`**.
The installer appears in `Output\`. Or on GitHub: **Actions › Build installer › Run workflow**.

## How the code is organised
| File / folder | What it is |
|---|---|
| `atelier_pdm.py` | The main program: vault, database, numbering, check-out / check-in, main window |
| `atelier_panel.py` | The companion panel docked beside SolidCraft / PhysCAD |
| `atelier_cam.py` | CAM & Tools - and the loader: at the end of this file each feature module below is installed in turn |
| `atelier_*.py` | Feature modules layered on top (themes, numbering, workflow, server mode, panel fixes, …) - they wrap functions in `atelier_pdm` / `atelier_panel` |
| `atelier_launcher.py` | What the installed `Atelier PDM.exe` runs |
| `atelier_help_text.py` | The in-app Help and the PDF manual's content |
| `atelier.spec`, `installer.iss`, `build.bat`, `*.ico` | PyInstaller and Inno Setup build |
| `server/` | The Atelier server (shared vault database for a team) and its Raspberry Pi install script |
| `plugin/` | Starting point for a SolidCraft plugin (SolidCraft's plugin interface is still experimental) |
| `docs/` | Manual, Raspberry Pi and Windows server guides |

## Never commit
Vault data, `atelier_index.db`, `atelier_vault.key`, `atelier_config.json` (it holds the server token), settings
or recovery codes. `.gitignore` blocks them - check the list of changes before each commit anyway.

## Licence
See `LICENSE`.
