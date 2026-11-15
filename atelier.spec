# PyInstaller build for Atelier PDM - run build.bat rather than this directly.
# One program ("Atelier PDM.exe") that opens the full app, the SolidCraft / PhysCAD panels, CAM & Tools or setup.
block_cipher = None

a = Analysis(
    ["atelier_launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[(n, d) for n, d in [("atelier.ico", "icons"), ("atelier_solidcraft.ico", "icons"),
                           ("atelier_physcad.ico", "icons"), ("atelier_cam.ico", "icons"),
                           ("Atelier PDM Manual.pdf", "docs")]],
    hiddenimports=["atelier_pdm", "atelier_panel", "atelier_secure", "atelier_setup", "atelier_cam",
                   "atelier_cam_ui", "atelier_finder", "atelier_themes", "atelier_numbering", "atelier_viewer", "atelier_ux", "atelier_pro", "atelier_polish", "atelier_guide", "atelier_flow", "atelier_access", "atelier_states", "atelier_v29", "atelier_fix291", "atelier_fix292", "atelier_fix293", "atelier_fix294", "atelier_fix295", "pypdf", "atelier_help_text", "_logo_pngs", "cryptography.hazmat.primitives.ciphers.aead"],
    excludes=["numpy", "pandas", "matplotlib", "scipy", "PyQt5", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Atelier PDM", console=False,
          icon="atelier.ico", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Atelier PDM")
