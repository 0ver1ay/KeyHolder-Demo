from __future__ import annotations

import os
from kivy.core.text import LabelBase


def register_roboto() -> None:
    """Register Roboto fonts if available.

    Looks for common Roboto TTFs in Windows fonts directory and local project.
    Registers family under the name 'Roboto'. Safe to call multiple times.
    """
    candidates = []
    # Local project fonts folders
    here = os.path.dirname(os.path.abspath(__file__))
    # 1) views/fonts (old expectation)
    local_fonts_lvl1 = os.path.join(os.path.dirname(here), 'fonts')
    # 2) project_root/fonts (actual in this repo)
    local_fonts_lvl2 = os.path.join(os.path.dirname(os.path.dirname(here)), 'fonts')
    for folder in (local_fonts_lvl1, local_fonts_lvl2):
        if os.path.isdir(folder):
            for fname in os.listdir(folder):
                if fname.lower().endswith('.ttf') and 'roboto' in fname.lower():
                    candidates.append(os.path.join(folder, fname))
    # Windows fonts directory
    win_fonts = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
    for name in (
        'Roboto-Regular.ttf',
        'Roboto-Medium.ttf',
        'Roboto-Bold.ttf',
    ):
        path = os.path.join(win_fonts, name)
        if os.path.exists(path):
            candidates.append(path)
    # If nothing found, silently skip (Kivy will fallback)
    if not candidates:
        return
    # Prefer regular first (case-insensitive contains)
    base_names = [os.path.basename(p).lower() for p in candidates]
    # map back helper
    def pick(sub: str, default: str | None = None) -> str:
        for i, bn in enumerate(base_names):
            if sub in bn:
                return candidates[i]
        return default or candidates[0]

    regular = pick('regular')
    bold = pick('bold', regular)
    italic = regular
    try:
        LabelBase.register(name='Roboto', fn_regular=regular, fn_bold=bold, fn_italic=italic)
    except Exception:
        # Ignore if already registered or if any issue occurs
        pass
