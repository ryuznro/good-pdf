# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import tempfile


project_dir = Path(SPECPATH).resolve()
generated_icon_icns = None
icon_png = project_dir / 'icon.png'
fallback_icon_icns = project_dir / 'icon.icns'

if icon_png.exists():
    try:
        from PIL import Image

        generated_icon_icns = Path(tempfile.gettempdir()) / 'good_pdf_icon.icns'
        with Image.open(icon_png) as img:
            img.save(
                generated_icon_icns,
                sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)],
            )
    except Exception:
        generated_icon_icns = None

bundle_icon = None
if generated_icon_icns:
    bundle_icon = str(generated_icon_icns)
elif fallback_icon_icns.exists():
    bundle_icon = str(fallback_icon_icns)

exe_icon = [bundle_icon] if bundle_icon else []


a = Analysis(
    [str(project_dir / 'pdf_editor.py')],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=['pikepdf._core'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Good PDF',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Good PDF',
)
app = BUNDLE(
    coll,
    name='Good PDF.app',
    icon=bundle_icon,
    bundle_identifier=None,
)
