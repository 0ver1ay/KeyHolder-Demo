"""Сборка PDF-инструкций (администратор / пользователь) со скриншотами.

Скриншоты кладутся в:
  docs/screens/admin/<key>.png   (или .jpg/.jpeg/.webp)
  docs/screens/user/<key>.png

Имя файла = ключ раздела (латиницей), см. ADMIN_SECTIONS / USER_SECTIONS ниже.
Допускаются также префиксы с номером: "01_menu.png", "01.png".

Запуск:
  py -3.11 scripts/build_manual_pdf.py

Результат:
  docs/Инструкция_администратора.pdf
  docs/Инструкция_пользователя.pdf
"""
from __future__ import annotations

import glob
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
SCREENS = os.path.join(DOCS, "screens")
# Плоская папка со скриншотами (имена — по разделам, см. *_IMAGES ниже)
SCREENS_FLAT = os.path.join(ROOT, "screenshots")

WIN_FONTS = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
IMG_EXTS = ("png", "jpg", "jpeg", "webp", "bmp")

# Геометрия страницы
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
IMG_MAX_H = 150 * mm


def _register_fonts() -> tuple[str, str]:
    """Регистрирует кириллический шрифт. Возвращает (regular, bold)."""
    families = [
        ("UI", "segoeui.ttf", "UI-Bold", "segoeuib.ttf"),
        ("UI", "arial.ttf", "UI-Bold", "arialbd.ttf"),
        ("UI", "calibri.ttf", "UI-Bold", "calibrib.ttf"),
    ]
    for reg_name, reg_file, bold_name, bold_file in families:
        reg_path = os.path.join(WIN_FONTS, reg_file)
        bold_path = os.path.join(WIN_FONTS, bold_file)
        if os.path.exists(reg_path):
            pdfmetrics.registerFont(TTFont(reg_name, reg_path))
            if os.path.exists(bold_path):
                pdfmetrics.registerFont(TTFont(bold_name, bold_path))
            else:
                pdfmetrics.registerFont(TTFont(bold_name, reg_path))
            return reg_name, bold_name
    # запасной вариант — встроенные (без кириллицы)
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _register_fonts()


def _styles() -> dict:
    base = getSampleStyleSheet()
    s = {}
    s["cover_title"] = ParagraphStyle(
        "cover_title", parent=base["Title"], fontName=FONT_BOLD,
        fontSize=30, leading=36, alignment=TA_CENTER, textColor=colors.HexColor("#1f2937"),
        spaceAfter=10,
    )
    s["cover_sub"] = ParagraphStyle(
        "cover_sub", parent=base["Normal"], fontName=FONT,
        fontSize=14, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#6b7280"),
    )
    s["h1"] = ParagraphStyle(
        "h1", parent=base["Heading1"], fontName=FONT_BOLD,
        fontSize=19, leading=24, textColor=colors.HexColor("#1f2937"), spaceAfter=4,
    )
    s["num"] = ParagraphStyle(
        "num", parent=base["Normal"], fontName=FONT_BOLD,
        fontSize=11, leading=14, textColor=colors.HexColor("#2563eb"), spaceAfter=2,
    )
    s["p"] = ParagraphStyle(
        "p", parent=base["Normal"], fontName=FONT,
        fontSize=11.5, leading=17, textColor=colors.HexColor("#111827"), spaceAfter=5,
        alignment=TA_LEFT,
    )
    s["b"] = ParagraphStyle(
        "b", parent=base["Normal"], fontName=FONT,
        fontSize=11.5, leading=16, textColor=colors.HexColor("#111827"),
        leftIndent=12, bulletIndent=2, spaceAfter=2,
    )
    s["note"] = ParagraphStyle(
        "note", parent=base["Normal"], fontName=FONT,
        fontSize=10.5, leading=15, textColor=colors.HexColor("#374151"),
        leftIndent=8, spaceBefore=4, spaceAfter=4,
        backColor=colors.HexColor("#eef2ff"), borderPadding=6,
    )
    s["caption"] = ParagraphStyle(
        "caption", parent=base["Normal"], fontName=FONT,
        fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#9ca3af"),
        spaceBefore=3,
    )
    return s


def find_image(folder: str, key: str, index: int) -> str | None:
    stems = [f"{index:02d}_{key}", f"{index:02d}", f"{index}_{key}", f"{index}", key]
    for stem in stems:
        for ext in IMG_EXTS:
            cand = os.path.join(folder, f"{stem}.{ext}")
            if os.path.exists(cand):
                return cand
    for f in glob.glob(os.path.join(folder, "*")):
        if not os.path.isfile(f):
            continue
        name = os.path.splitext(os.path.basename(f))[0].lower()
        if key in name:
            return f
    return None


def _image_flowable(path: str):
    if PILImage is not None:
        with PILImage.open(path) as im:
            iw, ih = im.size
    else:
        iw, ih = (1600, 1000)
    ratio = ih / float(iw)
    w = CONTENT_W
    h = w * ratio
    if h > IMG_MAX_H:
        h = IMG_MAX_H
        w = h / ratio
    img = Image(path, width=w, height=h)
    framed = Table([[img]], colWidths=[w])
    framed.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#d1d5db")),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    framed.hAlign = "CENTER"
    return framed


def _placeholder(key: str):
    ph = Table(
        [[Paragraph(f"Скриншот не добавлен<br/><font size=8>файл: {key}.png</font>",
                    ParagraphStyle("ph", fontName=FONT, fontSize=12, leading=18,
                                   alignment=TA_CENTER, textColor=colors.HexColor("#9ca3af")))]],
        colWidths=[CONTENT_W], rowHeights=[70 * mm],
    )
    ph.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    ph.hAlign = "CENTER"
    return ph


def resolve_image(folder: str, key: str, index: int, images: dict | None) -> str | None:
    """Сначала ищем по явному сопоставлению (images[key]), затем эвристикой."""
    if images:
        name = images.get(key)
        if name:
            cand = os.path.join(folder, name)
            if os.path.exists(cand):
                return cand
    return find_image(folder, key, index)


def build_pdf(out_path: str, title: str, subtitle: str, folder: str,
              sections: list[dict], images: dict | None = None,
              show_images: bool = True) -> None:
    st = _styles()
    story = []
    missing: list[str] = []

    # Обложка
    story.append(Spacer(1, 70 * mm))
    story.append(Paragraph(title, st["cover_title"]))
    story.append(Paragraph(subtitle, st["cover_sub"]))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("Программно-аппаратный комплекс «Ключница»", st["cover_sub"]))
    story.append(PageBreak())

    total = len(sections)
    for i, sec in enumerate(sections, start=1):
        story.append(Paragraph(f"Раздел {i} из {total}", st["num"]))
        story.append(Paragraph(sec["title"], st["h1"]))
        story.append(Spacer(1, 4 * mm))

        if show_images and not sec.get("no_image"):
            img_path = resolve_image(folder, sec["key"], i, images)
            if img_path:
                story.append(_image_flowable(img_path))
            else:
                story.append(_placeholder(sec["key"]))
                missing.append(f"{i:02d}. {sec['title']} (key={sec['key']})")
            story.append(Paragraph(f"Экран: {sec['title']}", st["caption"]))
            story.append(Spacer(1, 5 * mm))

        for kind, text in sec["body"]:
            if kind == "p":
                story.append(Paragraph(text, st["p"]))
            elif kind == "b":
                story.append(Paragraph(text, st["b"], bulletText="•"))
            elif kind == "note":
                story.append(Paragraph(f"<b>Подсказка.</b> {text}", st["note"]))

        if i < total:
            story.append(PageBreak())

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawCentredString(PAGE_W / 2, 10 * mm, f"{title}   ·   стр. {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title=title, author="KeyHolder",
    )
    try:
        doc.build(story, onFirstPage=lambda c, d: None, onLaterPages=_footer)
    except PermissionError:
        print(f"!! Пропущено: файл открыт/заблокирован — {out_path}")
        print("   Закройте PDF в просмотрщике и запустите сборку снова.")
        return
    print(f"OK -> {out_path}")
    if missing:
        print(f"   без скриншота ({len(missing)}):")
        for m in missing:
            print(f"     - {m}")


# ---------------------------------------------------------------------------
# Содержание разделов
# ---------------------------------------------------------------------------

ADMIN_SECTIONS = [
    {
        "key": "menu",
        "title": "Админ-панель (главное меню)",
        "body": [
            ("p", "При запуске программы сразу открывается админ-панель — сетка плиток со всеми разделами управления системой. Окно можно растягивать: плитки автоматически перестраиваются в 1, 2 или 3 колонки под ширину экрана."),
            ("p", "Каждая плитка ведёт в свой раздел. Возврат в это меню — кнопкой «Назад» на любом экране."),
            ("b", "Пользователи: создание, выдача привилегий, удаление."),
            ("b", "Помещения, ключи и боксы: создание и привязка ключей к ячейкам шкафа."),
            ("b", "RFID и коды: регистрация меток ключей, секретные коды сдачи."),
            ("b", "Контроль: выданные ключи, экспорт журнала, просмотр фотосессий."),
            ("note", "Рекомендуемый порядок первичной настройки: бокс → помещения/ключи → назначение ключей на бокс → пользователи и их RFID → выдача привилегий → регистрация RFID ключей."),
        ],
    },
    {
        "key": "add_user",
        "title": "Создать пользователя",
        "body": [
            ("p", "Создание новой учётной записи сотрудника. Обязательны логин и пароль; телефон и комментарий заполняются по желанию."),
            ("b", "Логин — уникальное имя для входа и отображения в журналах."),
            ("b", "Пароль — используется для служебного входа; основной вход сотрудника идёт по RFID-метке."),
            ("b", "Телефон и комментарий — справочная информация для администратора."),
            ("p", "После нажатия «Создать» программа предложит сразу привязать личную RFID-метку пользователя."),
            ("note", "Если поле логина или пароля пустое, система не даст сохранить пользователя."),
        ],
    },
    {
        "key": "register_user_rfid",
        "title": "Привязка RFID пользователя",
        "no_image": True,
        "body": [
            ("p", "Экран привязки личной метки сотрудника. Открывается автоматически сразу после создания пользователя — отдельного пункта меню для него нет."),
            ("b", "Приложите RFID-карту или брелок сотрудника к считывателю — метка закрепится за учётной записью."),
            ("b", "«Пропустить» — отложить привязку; её можно выполнить позже в разделе «Переназначить RFID»."),
            ("p", "Привязанной меткой сотрудник входит в пользовательскую программу у шкафа без ввода логина и пароля."),
            ("note", "Одна метка закрепляется за одним пользователем. Повторная привязка перезапишет предыдущую."),
        ],
    },
    {
        "key": "reassign_user_rfid",
        "title": "Переназначить RFID пользователя",
        "no_image": True,
        "body": [
            ("p", "Отдельный раздел для привязки или смены личной RFID-метки уже существующего сотрудника — например, при потере карты или выдаче новой."),
            ("b", "Выберите пользователя из списка — под списком показывается его текущая метка (или «не привязана»)."),
            ("b", "Нажмите «Ждать RFID» и приложите новую метку к считывателю."),
            ("b", "Строка статуса подтвердит привязку; новая метка сразу заменяет прежнюю."),
            ("note", "Если метка уже закреплена за другим сотрудником, система предупредит об этом и привязку не выполнит."),
        ],
    },
    {
        "key": "permissions",
        "title": "Выдача привилегий пользователям",
        "body": [
            ("p", "Раздел определяет, какие ключи доступны конкретному сотруднику. Вверху выбираются пользователь и бокс, ниже отображается сетка плиток-ключей выбранного бокса."),
            ("b", "Нажатие на плитку ключа включает или выключает доступ — состояние и цвет плитки меняются."),
            ("b", "Смена пользователя или бокса перечитывает сетку под выбранную пару."),
            ("b", "«Сбросить все выдачи» — принудительно снять отметки о выданных ключах во всей системе (считать, что все ключи на месте)."),
            ("note", "«Сбросить все выдачи» используйте осторожно: операция затрагивает всю систему и нужна лишь для исправления рассинхрона с реальностью."),
        ],
    },
    {
        "key": "delete_user",
        "title": "Удалить пользователя",
        "body": [
            ("p", "Удаление учётной записи из системы. Выберите сотрудника из списка и подтвердите удаление."),
            ("b", "Вместе с учётной записью снимается привязка её RFID-метки и выданные права."),
            ("note", "Удаление необратимо. Журнал прошлых операций сотрудника при этом сохраняется для отчётности."),
        ],
    },
    {
        "key": "add_room",
        "title": "Создать помещение / ключ",
        "body": [
            ("p", "Экран объединяет два действия: создание помещения (кабинета, зоны) и создание ключа, привязанного к помещению."),
            ("b", "Помещение: название и комментарий. Кнопка «Создать помещение» добавляет его в справочник."),
            ("b", "Ключ: код, описание, помещение, бокс и при необходимости секретный код сдачи."),
            ("p", "Сначала создаётся помещение, затем — ключи, которые к нему относятся."),
            ("note", "Код ключа удобно делать осмысленным (например, K-101), он отображается в журналах и на плитках."),
        ],
    },
    {
        "key": "add_box",
        "title": "Создать бокс",
        "body": [
            ("p", "Бокс — это физический шкаф с ячейками. Здесь задаются его название и размер сетки ячеек."),
            ("b", "Название — имя шкафа для выбора в других разделах."),
            ("b", "Столбцов (X) и строк (Y) — размер сетки ячеек; их произведение задаёт вместимость бокса."),
            ("note", "Размер сетки должен соответствовать реальному оборудованию шкафа — от него зависит привязка ключей к ячейкам."),
        ],
    },
    {
        "key": "assign_box",
        "title": "Назначить ключи на бокс",
        "body": [
            ("p", "Раздел привязывает ключи к конкретному шкафу и его ячейкам. Вверху выбирается бокс, рядом отображается информация о вместимости и занятых местах."),
            ("b", "Плитки ключей: нажатие назначает ключ в бокс либо снимает назначение."),
            ("b", "Для назначенного ключа задаётся позиция в сетке (столбец/строка) — это координаты ячейки для оборудования."),
            ("b", "Кнопки страниц листают список, «Обновить» перечитывает данные после изменений."),
            ("note", "Координаты ячейки должны совпадать с физическим расположением замка, иначе откроется не та ячейка."),
        ],
    },
    {
        "key": "delete_room",
        "title": "Удалить помещение / ключ",
        "body": [
            ("p", "Экран позволяет удалить помещение целиком либо отдельный ключ. Разделы расположены друг под другом."),
            ("b", "Удаление помещения убирает его из справочника; предварительно проверьте связанные ключи."),
            ("b", "Удаление ключа снимает его привязки к боксу и правам пользователей."),
            ("note", "Нельзя удалить объект, по которому есть незакрытые выдачи — сначала верните ключи."),
        ],
    },
    {
        "key": "delete_box",
        "title": "Удалить бокс",
        "body": [
            ("p", "Удаление шкафа из системы. Выберите бокс из списка и подтвердите удаление."),
            ("note", "Перед удалением убедитесь, что в боксе нет назначенных ключей и активных выдач."),
        ],
    },
    {
        "key": "register_rfid",
        "title": "Регистрация RFID ключа",
        "body": [
            ("p", "Привязка RFID-метки к физическому ключу. Выберите ключ из списка, нажмите «Ждать RFID» и приложите метку к брелоку ключа."),
            ("b", "После считывания метка закрепляется за выбранным ключом."),
            ("b", "Строка статуса показывает ход операции и результат."),
            ("p", "Метка ключа используется при взятии (распознавание) и при сдаче через общий считыватель."),
            ("note", "У каждого физического ключа должна быть своя метка — иначе сдача и распознавание работать не будут."),
        ],
    },
    {
        "key": "secret_codes",
        "title": "Секретные коды",
        "body": [
            ("p", "Для выбранного ключа можно задать секретный код сдачи. Он позволяет сдать ключ без входа пользователя в систему."),
            ("b", "Выберите ключ и введите код, затем сохраните."),
            ("b", "Пустое поле кода убирает ранее заданный код."),
            ("p", "Сотрудник вводит этот код на гостевом экране пользовательской программы (кнопка «Сдать по секретному коду»)."),
            ("note", "Код — резервный способ сдачи. Выдавайте его ограниченно и меняйте при необходимости."),
        ],
    },
    {
        "key": "issued",
        "title": "Выданные ключи",
        "body": [
            ("p", "Показывает, какие ключи сейчас на руках и у кого. Каждая плитка содержит название/код ключа, имя пользователя и время выдачи."),
            ("b", "Фильтр сужает список по названию/коду или по пользователю."),
            ("b", "Кнопки страниц листают список, если ключей много."),
            ("note", "Это экран только для просмотра — выдать или сдать ключ отсюда нельзя."),
        ],
    },
    {
        "key": "export",
        "title": "Экспорт выдач ключей",
        "body": [
            ("p", "Журнал операций «взял/сдал» за выбранный период. Сверху — фильтры по столбцам, снизу — выбор периода и кнопки действий."),
            ("b", "Период задаётся выпадающими списками даты «От» и «До»."),
            ("b", "Поля фильтров сужают таблицу по дате, пользователю, ключу, действию, боксу и помещению."),
            ("b", "«Экспорт» сохраняет данные в файл, «Удаление» очищает записи за период."),
            ("note", "Перед удалением за период сначала выгрузите данные в файл — операция удаления необратима."),
        ],
    },
    {
        "key": "images",
        "title": "Снимки (фотосессии)",
        "body": [
            ("p", "Просмотр фотографий с камеры, сделанных во время сессий работы пользователя у шкафа. Слева — список сессий за период, справа — просмотр кадров выбранной сессии."),
            ("b", "Период задаётся датами «От» и «До»; список сессий обновляется автоматически."),
            ("b", "Ползунок и счётчик листают кадры выбранной сессии."),
            ("b", "«Удалить за период» очищает снимки за выбранный диапазон дат."),
            ("note", "Снимки появляются только если камера была подключена и включена в конфигурации."),
        ],
    },
]

USER_SECTIONS = [
    {
        "key": "guest",
        "title": "Стартовый экран (до входа)",
        "body": [
            ("p", "Первый экран у шкафа, доступный без входа в систему. Отсюда начинается любая работа с ключницей."),
            ("b", "«Авторизация» — вход по личной RFID-метке."),
            ("b", "«Просмотр выданных ключей» — список выданных ключей без входа."),
            ("b", "«Сдать по секретному коду» — сдача ключа по коду от администратора."),
            ("note", "Если метки нет, ключ можно сдать по секретному коду, а список выданных — посмотреть без входа."),
        ],
    },
    {
        "key": "auth",
        "title": "Вход в систему по RFID",
        "body": [
            ("p", "Экран входа. Приложите личную RFID-карту или брелок к считывателю."),
            ("b", "Метка зарегистрирована — откроется главное меню сотрудника."),
            ("b", "Метка не распознана — обратитесь к администратору для регистрации."),
            ("b", "«Назад» — возврат на стартовый экран."),
            ("note", "Прикладывайте метку плотно к считывателю и удерживайте до отклика."),
        ],
    },
    {
        "key": "main",
        "title": "Главное меню (после входа)",
        "body": [
            ("p", "Меню сотрудника после успешного входа. Отсюда доступны основные действия с ключами."),
            ("b", "«Взять ключ» — список доступных вам ключей."),
            ("b", "«Сдать ключ» — список ключей, которые вы держите."),
            ("b", "«Просмотр выданных ключей» — общий список по всем сотрудникам."),
            ("b", "«Выход» — завершение сеанса."),
            ("note", "При долгом бездействии программа сама завершит сеанс — таймаут задаёт администратор."),
        ],
    },
    {
        "key": "take",
        "title": "Взять ключ",
        "body": [
            ("p", "Экран выбора ключа для получения. Показаны плитки помещений/ключей, к которым у вас есть доступ."),
            ("b", "Нажмите нужную плитку либо приложите RFID-метку этого ключа к считывателю."),
            ("b", "Соответствующая ячейка шкафа откроется, ключ закрепится за вами."),
            ("b", "Заберите ключ и закройте ячейку — замок закроется автоматически через несколько секунд."),
            ("note", "Если ключей много, листайте страницы кнопками «Предыдущая» / «Следующая»."),
        ],
    },
    {
        "key": "return",
        "title": "Сдать ключ",
        "body": [
            ("p", "Экран сдачи ключа. Показаны только те ключи, которые сейчас числятся за вами."),
            ("b", "Выберите ключ из списка."),
            ("b", "Появится сообщение «Поднесите ключ к общему RFID-считывателю»."),
            ("b", "Приложите метку на самом ключе (не личную карту) — откроется нужная ячейка."),
            ("b", "Положите ключ в ячейку и закройте её."),
            ("note", "Сдача всегда идёт по метке ключа: сначала выбор ключа, затем считывание его RFID."),
        ],
    },
    {
        "key": "secret_code",
        "title": "Сдача по секретному коду (без входа)",
        "body": [
            ("p", "Способ сдать ключ без личной метки — по коду, который выдал администратор для конкретного ключа."),
            ("b", "На стартовом экране нажмите «Сдать по секретному коду»."),
            ("b", "Введите код и при необходимости выберите ключ."),
            ("b", "Поднесите RFID-метку ключа к считывателю."),
            ("b", "Положите ключ в открывшуюся ячейку."),
            ("note", "Код привязан к конкретному ключу. Если он не подходит — уточните актуальный код у администратора."),
        ],
    },
    {
        "key": "issued",
        "title": "Просмотр выданных ключей",
        "body": [
            ("p", "Список ключей, которые сейчас на руках, с указанием пользователя. Доступен и до входа (со стартового экрана), и после входа из главного меню."),
            ("b", "Можно листать страницы, если записей много."),
            ("note", "Это просмотр без действий — взять или сдать ключ отсюда нельзя."),
        ],
    },
    {
        "key": "mismatch",
        "title": "Сообщение «Неверная ячейка»",
        "body": [
            ("p", "Появляется, если ключ взят или положен не в ту ячейку, которую открыла система."),
            ("b", "Выполните действие именно в указанной ячейке."),
            ("b", "Экран закроется сам, когда всё сделано правильно, либо по таймауту."),
            ("note", "Если ячейка упорно не та — сообщите администратору: возможно, рассинхрон координат."),
        ],
    },
]


# Сопоставление «ключ раздела → файл в папке screenshots/».
# Разделы без файла рисуются с заглушкой (см. вывод «без скриншота»).
ADMIN_IMAGES = {
    "menu": "menu.jpg",
    "add_user": "создать пользователя.jpg",
    "permissions": "выдача привелегий.jpg",
    "delete_user": "удалить пользователя.jpg",
    "add_room": "создать помещение-ключ.jpg",
    "add_box": "создать бокс.jpg",
    "assign_box": "назначить ключи на бокс.jpg",
    "delete_room": "удалить помещение или ключ.jpg",
    "delete_box": "удалить бокс.jpg",
    "register_rfid": "регистрация rfid ключа.jpg",
    "secret_codes": "Секретные коды для сдачи ключей.jpg",
    "issued": "Выданные ключи.jpg",
    "export": "Экспорт выдач ключей.jpg",
    "images": "Просмотр фотосессий.jpg",
    # register_user_rfid, reassign_user_rfid — без изображения (no_image=True в разделе)
}

USER_IMAGES = {
    "issued": "просмотр выданных ключей из пользователя.png",
    # guest, auth, main, take, return, secret_code, mismatch — скриншотов пока нет
}


def main() -> None:
    build_pdf(
        os.path.join(DOCS, "Инструкция_администратора.pdf"),
        "Инструкция администратора",
        "Программа управления системой: пользователи, ключи, боксы, отчёты",
        SCREENS_FLAT,
        ADMIN_SECTIONS,
        ADMIN_IMAGES,
    )
    build_pdf(
        os.path.join(DOCS, "Инструкция_пользователя.pdf"),
        "Инструкция пользователя",
        "Работа у шкафа: вход, получение и сдача ключей",
        SCREENS_FLAT,
        USER_SECTIONS,
        USER_IMAGES,
        show_images=False,
    )


if __name__ == "__main__":
    main()
