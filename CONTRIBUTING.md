# Contributing

Thanks for testing Atelier PDM.

## Reporting a problem
Use **Issues › New issue › Bug report**. Include what you did, what happened, your Windows version, the Atelier
version (shown under the title in the header) and the contents of `%LOCALAPPDATA%\Atelier PDM\atelier_error.log`.
**Remove anything private first** - never attach vault files, `atelier_index.db`, keys or `atelier_config.json`.

## Changing the code
1. Fork the repository and make a branch.
2. Run from source: `python -m pip install -r requirements.txt`, then `python atelier_launcher.py`.
   Use a **test vault**, never a real one.
3. Keep changes small and say what you tested. Open a pull request.

New features are usually best as a new `atelier_<name>.py` module with an `install(module)` function, added to the
loader at the end of `atelier_cam.py` and to `hiddenimports` in `atelier.spec`.
