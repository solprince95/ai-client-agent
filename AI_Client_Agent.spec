# AI_Client_Agent.spec
# Build with:  pyinstaller AI_Client_Agent.spec
#
# Produces a single executable that bundles Flask, templates, static
# assets, and all Python dependencies. The .exe / binary can be run
# directly by double-clicking — no Python installation required.

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'cryptography.hazmat.backends.openssl',
        'cryptography.hazmat.primitives.asymmetric.ed25519',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AI_Client_Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    runtime_tmpdir=None,
)
