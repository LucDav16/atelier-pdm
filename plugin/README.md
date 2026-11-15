# Atelier PDM - SolidCraft plugin (starting point)

Adds **Plugins › Atelier** commands inside SolidCraft: *Status*, *Check out*, *Check in*, *Undo check-out*,
*Save to vault*. The plugin itself does almost nothing: it writes a small request file, and Atelier PDM (the main
window or the docked panel - whichever is open) carries it out and writes an answer back. All the vault logic stays
in Atelier, which is why this works even though SolidCraft plugins are sandboxed (no network, only folders you allow).

## Status - please read
* **The Atelier side is finished and tested** (2.9.0, `atelier_v29.py`: the "plugin bridge").
* **This plugin is a starting point, not a finished build.** SolidCraft 0.41 made plugins official, but its plugin
  interface is still documented as a draft design (docs/plugins.md and PR #4980 in the SolidCraft repository), and
  I couldn't compile or load a plugin here. The two places that depend on SolidCraft's interface are marked
  `HOST:` in `src/lib.rs` - check them against the current docs before building.

## How it talks to Atelier
Folder (grant the plugin access to it when SolidCraft asks):  `C:\Design Files\_Atelier\plugin`

Request - write `requests\<id>.json` (write to `<id>.tmp` first, then rename):
```json
{"id": "1731628800-1", "action": "check_in", "file": "C:\\Design Files\\01_0001 PLATE.sc", "comment": "holes moved"}
```
`action`: `ping`, `status`, `check_out`, `check_in`, `undo`, `save_to_vault` (with `"folder": "Projects\\01_Rig\\Parts"`).

Answer - Atelier writes `responses\<id>.json` within about a second:
```json
{"id": "1731628800-1", "ok": true, "message": "Checked in '01_0001 PLATE.sc' (Rev A-02).", "answered": "..."}
```
`status` also returns `revision`, `status`, `state` and `locked_by`.

Before **Check in**, the plugin should run SolidCraft's own *Save* so the latest work is on disk.

## Building (once the HOST: parts are confirmed)
1. Install Rust (rustup.rs), then `rustup target add wasm32-wasip2`.
2. `cargo build --release --target wasm32-wasip2`
3. Install the `.wasm` from `target\wasm32-wasip2\release\` as SolidCraft's plugin docs describe, and allow it the
   folder above.

You can test the Atelier side without any plugin: with Atelier open, save a request file like the one above into
`requests` and watch `responses`.
