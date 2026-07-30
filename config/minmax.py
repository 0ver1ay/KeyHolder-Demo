from kivy.metrics import dp

# Настройки min/max и отступов для ячеек сетки

# Базовая сетка (можно менять при инициализации контроллера)
DEFAULT_GRID_ROWS = 4
DEFAULT_GRID_COLS = 6

# Ограничения размеров ячейки
# Сделаем плитки компактнее, сохранив пропорции и стиль
CELL_MIN_W = dp(88)
CELL_MIN_H = dp(56)
CELL_MAX_W = dp(120)
CELL_MAX_H = dp(84)

# Отступ между элементами сетки
GRID_SPACING = dp(8)

__all__ = [
    "DEFAULT_GRID_ROWS",
    "DEFAULT_GRID_COLS",
    "CELL_MIN_W",
    "CELL_MIN_H",
    "CELL_MAX_W",
    "CELL_MAX_H",
    "GRID_SPACING",
]


