ATELIER PDM 2.7.1 - CAM & TOOLS
===============================

Open it from:  the "CAM & Tools" button in the main window,  a CAM row under a part in the main tree
(double-click or right-click),  "CAM & Tools…" in the SolidCraft / PhysCAD panel menu (≡),  or the Start-menu
shortcut "Atelier CAM & Tools" (purple icon).

Everything on the buttons is also on the right-click menu. Windows keys work in every list:
Enter = open / edit,  Delete = remove (or supersede),  F2 = rename,  Ctrl+N = new,  Ctrl+D = duplicate,  F5 = refresh.

PARTS & CAM  (each part with its CAM files underneath - the same in the main Atelier PDM tree)
* Open part in CAM:  puts a STEP of the part's current revision in <working folder>\CAM\Models and opens it in
  CamCraft / FreeCAD / PhysCAD / SolidCraft. If the program isn't found you're asked to locate it once.
  The STEP comes from a same-named .step saved with the part, or is made automatically if the CAD program allows.
* CAM setups (the CAM program's own job file):
    File CAM setup…            stores it as  <project>\CAM Setups\<part number> <name> <OP> <revision>.<ext>
    Open setup for editing     working copy in <working folder>\CAM, opened in its CAM program
    Save setup back            files your edited copy again (old copy kept in CAM Setups\_Replaced)
  If the part has been up-issued, saving back files it against the new revision and supersedes the old one.
* NC programs:  File NC program… -> <project>\NC Programs\...  Send to machine copies to the machine's folder.
* Out of date: up-issuing or checking in the part again flags its setups and programs (orange).
* Delete marks a file Superseded - it's kept; tick "Show superseded" to see or restore it.
* Tick "All parts" to list parts that have no CAM yet.

TOOL LIBRARIES (shared by every CAM program)
* Tools: type, number, geometry, holder, CAD model, notes, and feeds & speeds per material
  (Vc + fz, or fixed rpm / feed; plunge, ap, ae, coolant). Drills, reamers and taps: fz = feed per revolution.
* Holders: name, gauge length, optional profile, and the holder's CAD model.
* Right-click a library to rename, duplicate, delete (kept in _CAM\Deleted) or export it.
* The calculator shows rpm and feed on a machine, limited to its spindle and feed (chip load kept).
* Export (to <working folder>\CAM Exports): Fusion 360 / HSM .json (PhysCAD imports this), FreeCAD tool bits,
  CSV, LinuxCNC .tbl.  Import: Fusion 360 / HSM (.json / .tools) and Atelier CSV.

MACHINES & POSTS (administrators)
* Each machine: controller, spindle min/max, max feed, travels, tool slots, NC file type, program folder.
* Posts per CAM program: <vault>\_CAM\Machines\<machine>\Posts\CamCraft | FreeCAD | PhysCAD | SolidCraft
  Replacing or removing a post keeps the old copy in the machine's History folder.

NC file types: .nc .tap .ngc .gcode .gc .cnc .eia .iso .mpf - add more in atelier_config.json: "nc_exts": [".nc", ".h"]
CAM program locations can be set in atelier_config.json: "cam_apps": {"CamCraft": {"exe": "C:\\...\\CamCraft.exe"}}
