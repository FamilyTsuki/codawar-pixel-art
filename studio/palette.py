"""Couleurs Codawar.

Table d'index des couleurs de onSynchronizeCanvas (cells[i+2]), reconstruite depuis le bundle JS du site :
["#ffffff", ...anciennes couleurs (15), ...palette posable (32) sans #ffffff]
"""

WHITE = "#ffffff"

LEGACY_COLORS = ["#e4e4e4", "#888888", "#222222", "#ffa7d1", "#e50000", "#e59500", "#a06a42", "#e5d900",
                 "#94e044", "#02be01", "#00d3dd", "#0083c7", "#0000ea", "#cf6ee4", "#820080"]
PLACEABLE_COLORS = ["#6d001a", "#be0039", "#ff4500", "#ffa800", "#ffd635", "#fff8b8", "#00a368", "#00cc78",
                    "#7eed56", "#00756f", "#009eaa", "#00ccc0", "#2450a4", "#3690ea", "#51e9f4", "#493ac1",
                    "#6a5cff", "#94b3ff", "#811e9f", "#b44ac0", "#e4abff", "#de107f", "#ff3881", "#ff99aa",
                    "#6d482f", "#9c6926", "#ffb470", "#000000", "#515252", "#898d90", "#d4d7d9", "#ffffff"]
COLOR_INDEX = [WHITE] + LEGACY_COLORS + [c for c in PLACEABLE_COLORS if c != WHITE]
COLOR_POS = {h: i for i, h in enumerate(COLOR_INDEX)}  # hex -> index (pour empaqueter un instantané)


def color_at(index):
    """Couleur hex d'un index de la table (blanc si hors table)."""
    return COLOR_INDEX[index] if 0 <= index < len(COLOR_INDEX) else WHITE
