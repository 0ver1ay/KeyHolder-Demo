# -*- mode: python ; coding: utf-8 -*-

import os

from kivymd import hooks_path as kivymd_hooks_path

spec_dir = os.path.dirname(os.path.abspath(SPEC))
repo_root = os.path.abspath(os.path.join(spec_dir, '..', '..'))

a = Analysis(
    [os.path.join(repo_root, 'main_admin.py')],
    pathex=[repo_root],
    binaries=[],
    datas=[
        (os.path.join(repo_root, 'config.cfg'), '.'),
        (os.path.join(repo_root, 'views'), 'views'),
    ],
    hiddenimports=[
        'psycopg2',
        'sqlalchemy.dialects.postgresql.psycopg2',
        'kivymd.uix.datatables',
        'kivymd.uix.label',
        'kivymd.utils.asynckivy',
        'asyncio',
        'asyncio.events',
        'asyncio.base_events',
        'asyncio.unix_events',
        'kivy.core.window.window_sdl2',
        'kivy.core.image.img_sdl2',
        'kivy.core.text.text_sdl2',
        'kivy.core.audio.audio_sdl2',
        'kivy.core.camera.camera_opencv',
    ],
    hookspath=[kivymd_hooks_path],
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
    name='AdminApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AdminApp',
)
