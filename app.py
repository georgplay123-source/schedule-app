import streamlit as st
import pandas as pd
import numpy as np
import re

st.set_page_config(page_title="Расписание", layout="wide")
st.title("📅 Просмотр расписания из Excel (все листы)")

uploaded = st.file_uploader("Загрузите Excel-файл расписания (.xlsx)", type=["xlsx"])


def _clean_str(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() == "nan" else s


# ---------- Формат 1: "недельный" (первые 3 колонки + блоки по 4) ----------
def parse_weekly_blocks_format(xlsx_file, sheet=None) -> pd.DataFrame:
    raw = pd.read_excel(xlsx_file, header=None, sheet_name=sheet)

    header_row = None
    for r in range(min(15, len(raw))):
        row = raw.iloc[r].astype(str).tolist()
        if ("Дата" in row) and ("День недели" in row) and ("Занятие" in row):
            header_row = r
            break
    if header_row is None:
        raise ValueError("Не нашёл строку заголовков (Дата/День недели/Занятие).")

    header = raw.iloc[header_row].tolist()
    data = raw.iloc[header_row + 1:].copy()
    data.columns = header

    base_cols = ["Дата", "День недели", "Занятие"]
    cols = list(data.columns)
    other_cols = cols[3:]

    result = []
    for i in range(0, len(other_cols), 4):
        block = other_cols[i:i + 4]
        if len(block) < 4:
            continue

        temp = data[base_cols + list(block)].copy()
        temp.columns = ["Дата", "День", "Пара", "Дисциплина", "Преподаватель", "Аудитория", "Группа"]

        temp["Дата"] = pd.to_datetime(temp["Дата"], errors="coerce")
        temp["Пара"] = pd.to_numeric(temp["Пара"], errors="coerce")
        temp = temp.dropna(subset=["Дата", "Пара"])

        temp["Группа"] = temp["Группа"].ffill()
        temp["Дисциплина"] = temp["Дисциплина"].replace({np.nan: ""}).astype(str)
        temp = temp[temp["Дисциплина"].str.strip() != ""]

        for c in ["Преподаватель", "Аудитория", "Группа"]:
            temp[c] = temp[c].replace({np.nan: ""}).astype(str)

        result.append(temp)

    out = pd.concat(result, ignore_index=True)
    out = out.sort_values(["Дата", "Пара", "Группа"]).reset_index(drop=True)
    return out


# ---------- Формат 2: "общий" (дисциплина строкой, преподаватель следующей строкой, группы блоками) ----------
def parse_general_college_format(xlsx_file, sheet=None) -> pd.DataFrame:
    raw = pd.read_excel(xlsx_file, header=None, sheet_name=sheet)

    # ищем строку, где есть "День недели" и "Занятие"
    header_row = None
    for r in range(min(12, len(raw))):
        row = raw.iloc[r].astype(str).tolist()
        if ("День недели" in row) and ("Занятие" in row):
            header_row = r
            break
    if header_row is None:
        raise ValueError("Не нашёл шапку (строку с 'День недели' и 'Занятие').")

    # обычно группы подписаны через 2 строки ниже шапки
    groups_row = header_row + 2

    row0 = raw.iloc[header_row].tolist()

    # колонки, где написано "Дисциплина" (старт блока группы)
    disc_cols = [i for i, v in enumerate(row0) if str(v).strip() == "Дисциплина"]
    if not disc_cols:
        raise ValueError("Не нашёл колонки 'Дисциплина' (блоки групп).")

    # аудитория обычно +3 от дисциплины (в вашем файле так)
    aud_cols = {d: d + 3 for d in disc_cols if d + 3 < raw.shape[1]}

    # имена групп — в строке groups_row в колонках дисциплин
    group_names = {}
    if groups_row < len(raw):
        for d in disc_cols:
            g = _clean_str(raw.iloc[groups_row, d])
            group_names[d] = g if g else f"Группа_{d}"
    else:
        for d in disc_cols:
            group_names[d] = f"Группа_{d}"

    records = []
    current_date = None
    current_day = ""

    start_row = header_row + 4
    r = start_row

    while r < len(raw) - 1:
        c0 = raw.iloc[r, 0]
        c1 = raw.iloc[r, 1]  # номер пары на строках дисциплин

        # обновляем дату/день если встретили
        s0 = _clean_str(c0)
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s*(.*)", s0)
        if m:
            current_date = pd.to_datetime(m.group(1), dayfirst=True, errors="coerce")
            current_day = m.group(2).strip()

        pair = pd.to_numeric(c1, errors="coerce")

        # строка дисциплины: есть номер пары, и дата уже известна
        if pd.notna(pair) and current_date is not None:
            pair = int(pair)
            teacher_row = r + 1  # следующая строка — преподаватели

            for d in disc_cols:
                discipline = _clean_str(raw.iloc[r, d])
                if not discipline:
                    continue

                teacher = _clean_str(raw.iloc[teacher_row, d])

                # аудитория может быть в "ауд" колонке, но иногда пусто — попробуем вытащить как есть
                aud_col = aud_cols.get(d, None)
                aud = _clean_str(raw.iloc[r, aud_col]) if aud_col is not None else ""

                records.append({
                    "Дата": current_date,
                    "День": current_day,
                    "Пара": pair,
                    "Группа": group_names.get(d, ""),
                    "Дисциплина": discipline,
                    "Преподаватель": teacher,
                    "Аудитория": aud
                })

            r += 2
            continue

        r += 1

    if not records:
        raise ValueError("Не удалось собрать записи (возможно, на листе другая структура).")

    out = pd.DataFrame(records)
    out = out.sort_values(["Дата", "Пара", "Группа"]).reset_index(drop=True)
    return out


def parse_any_sheet(xlsx_file, sheet) -> pd.DataFrame:
    # сначала пробуем "общий", если не вышло — "недельный"
    try:
        return parse_general_college_format(xlsx_file, sheet=sheet)
    except Exception:
        return parse_weekly_blocks_format(xlsx_file, sheet=sheet)


def parse_all_sheets(xlsx_file) -> pd.DataFrame:
    xls = pd.ExcelFile(xlsx_file)
    parts = []

    for sh in xls.sheet_names:
        try:
            part = parse_any_sheet(xlsx_file, sheet=sh)
            part["Лист"] = sh
            parts.append(part)
        except Exception:
            # лист другого формата — пропускаем
            continue

    if not parts:
        raise ValueError("Не удалось распарсить ни один лист (возможно, другой формат файла).")

    out = pd.concat(parts, ignore_index=True)

    # чистка строк
    for c in ["День", "Группа", "Дисциплина", "Преподаватель", "Аудитория", "Лист"]:
        out[c] = out[c].astype(str).replace("nan", "").str.strip()

    out = out.sort_values(["Дата", "Пара", "Лист", "Группа"]).reset_index(drop=True)
    return out


if uploaded:
    try:
        df = parse_all_sheets(uploaded)
    except Exception as e:
        st.error(f"Ошибка разбора файла: {e}")
        st.stop()

    st.success(f"Готово! Листов обработано: {df['Лист'].nunique()}, записей: {len(df)}")

    # --- фильтры ---
    col1, col2, col3, col4 = st.columns([1, 1, 2, 2])

    mode = col1.selectbox("Режим", ["По преподавателю", "По группе", "Всё"])

    days = sorted([d for d in df["День"].unique().tolist() if d])
    day_filter = col2.multiselect("Дни недели", options=days, default=[])

    sheets = sorted(df["Лист"].unique().tolist())
    sheet_filter = col3.multiselect("Листы (отделения/направления)", options=sheets, default=sheets)

    query = col4.text_input("Поиск (фамилия / предмет / аудитория / группа)")

    view = df.copy()

    if day_filter:
        view = view[view["День"].isin(day_filter)]

    if sheet_filter:
        view = view[view["Лист"].isin(sheet_filter)]

    if query.strip():
        q = query.strip().lower()
        mask = (
            view["Преподаватель"].str.lower().str.contains(q, na=False) |
            view["Дисциплина"].str.lower().str.contains(q, na=False) |
            view["Аудитория"].str.lower().str.contains(q, na=False) |
            view["Группа"].str.lower().str.contains(q, na=False) |
            view["Лист"].str.lower().str.contains(q, na=False)
        )
        view = view[mask]

    if mode == "По преподавателю":
        teachers = sorted([t for t in view["Преподаватель"].unique().tolist() if t])
        teacher = st.selectbox("Преподаватель", options=teachers)
        view = view[view["Преподаватель"] == teacher]

    elif mode == "По группе":
        groups = sorted([g for g in view["Группа"].unique().tolist() if g])
        group = st.selectbox("Группа", options=groups)
        view = view[view["Группа"] == group]

    # --- таблица ---
    show = view.copy()
    show["Дата"] = show["Дата"].dt.strftime("%d.%m.%Y")

    st.dataframe(
        show[["Лист", "Дата", "День", "Пара", "Группа", "Дисциплина", "Преподаватель", "Аудитория"]],
        use_container_width=True,
        hide_index=True
    )

    # --- вывод по дням (удобно для чтения) ---
    st.subheader("🗓️ По дням")

    view2 = view.copy()
    view2["Дата_str"] = view2["Дата"].dt.strftime("%d.%m.%Y")
    view2 = view2.sort_values(["Дата", "Пара", "Лист", "Группа"])

    for (date_str, day), chunk in view2.groupby(["Дата_str", "День"], sort=False):
        with st.expander(f"{date_str} — {day}", expanded=True):
            for _, row in chunk.iterrows():
                st.markdown(
                    f"**{int(row['Пара'])} пара** · **{row['Лист']} / {row['Группа']}** · "
                    f"{row['Дисциплина']} — {row['Преподаватель']} · ауд. {row['Аудитория']}"
                )

    # --- скачать ---
    csv = view.to_csv(index=False, encoding="utf-8-sig")
    st.download_button("⬇️ Скачать выбранное (CSV)", data=csv, file_name="schedule_filtered.csv", mime="text/csv")

else:
    st.info("Загрузите Excel — и появятся фильтры.")