import streamlit as st
import pandas as pd
import numpy as np
import re
import requests
import base64

st.set_page_config(page_title="Расписание", layout="wide")
st.title("📅 Расписание")

# -------------------- AUTH / ADMIN --------------------
def is_admin() -> bool:
    pwd = st.secrets.get("ADMIN_PASSWORD", "")
    if not pwd:
        return False
    if "admin_ok" not in st.session_state:
        st.session_state["admin_ok"] = False

    with st.sidebar:
        st.markdown("### 🔐 Админ")
        entered = st.text_input("Пароль админа", type="password")
        if st.button("Войти как админ"):
            if entered == pwd:
                st.session_state["admin_ok"] = True
                st.success("Админ режим включён")
            else:
                st.error("Неверный пароль")

    return st.session_state["admin_ok"]


# -------------------- GitHub storage --------------------
def gh_headers():
    return {
        "Authorization": f"token {st.secrets['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }

def gh_get_file_sha(repo: str, path: str, branch: str):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": branch}, timeout=30)
    if r.status_code == 200:
        return r.json().get("sha")
    if r.status_code == 404:
        return None
    raise RuntimeError(f"GitHub read error {r.status_code}: {r.text}")

def gh_put_file(repo: str, path: str, branch: str, content_bytes: bytes, message: str):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    sha = gh_get_file_sha(repo, path, branch)
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=gh_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub write error {r.status_code}: {r.text}")
    return r.json()

def gh_raw_url(repo: str, branch: str, path: str) -> str:
    # raw download URL for public repo file
    owner, name = repo.split("/", 1)
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"

@st.cache_data(ttl=60)
def download_latest_xlsx(url: str) -> bytes:
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Не удалось скачать общий файл ({r.status_code}).")
    return r.content


# -------------------- Parsing helpers --------------------
def _clean_str(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() == "nan" else s


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


def parse_general_college_format(xlsx_file, sheet=None) -> pd.DataFrame:
    raw = pd.read_excel(xlsx_file, header=None, sheet_name=sheet)

    header_row = None
    for r in range(min(12, len(raw))):
        row = raw.iloc[r].astype(str).tolist()
        if ("День недели" in row) and ("Занятие" in row):
            header_row = r
            break
    if header_row is None:
        raise ValueError("Не нашёл шапку (строку с 'День недели' и 'Занятие').")

    groups_row = header_row + 2
    row0 = raw.iloc[header_row].tolist()

    disc_cols = [i for i, v in enumerate(row0) if str(v).strip() == "Дисциплина"]
    if not disc_cols:
        raise ValueError("Не нашёл колонки 'Дисциплина' (блоки групп).")

    aud_cols = {d: d + 3 for d in disc_cols if d + 3 < raw.shape[1]}

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
        s0 = _clean_str(raw.iloc[r, 0])
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s*(.*)", s0)
        if m:
            current_date = pd.to_datetime(m.group(1), dayfirst=True, errors="coerce")
            current_day = m.group(2).strip()

        pair = pd.to_numeric(raw.iloc[r, 1], errors="coerce")
        if pd.notna(pair) and current_date is not None:
            pair = int(pair)
            teacher_row = r + 1

            for d in disc_cols:
                discipline = _clean_str(raw.iloc[r, d])
                if not discipline:
                    continue

                teacher = _clean_str(raw.iloc[teacher_row, d])
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
            continue

    if not parts:
        raise ValueError("Не удалось распарсить ни один лист (возможно, другой формат файла).")

    out = pd.concat(parts, ignore_index=True)
    for c in ["День", "Группа", "Дисциплина", "Преподаватель", "Аудитория", "Лист"]:
        out[c] = out[c].astype(str).replace("nan", "").str.strip()
    out = out.sort_values(["Дата", "Пара", "Лист", "Группа"]).reset_index(drop=True)
    return out


# -------------------- UI: Admin upload / Public view --------------------
admin_ok = is_admin()

st.info(
    "📌 Пользователям: просто откройте ссылку — расписание уже будет показано.\n"
    "Админу: слева в панели введите пароль и загрузите новый Excel, чтобы обновить расписание для всех."
)

repo = st.secrets.get("GITHUB_REPO", "")
branch = st.secrets.get("GITHUB_BRANCH", "main")
path = st.secrets.get("GITHUB_FILE_PATH", "data/latest.xlsx")

if admin_ok:
    st.subheader("⬆️ Обновить расписание (видно всем по ссылке)")
    new_file = st.file_uploader("Загрузите новый Excel (.xlsx) для публикации", type=["xlsx"], key="admin_uploader")
    if new_file and st.button("Опубликовать"):
        try:
            content = new_file.getvalue()
            gh_put_file(
                repo=repo,
                path=path,
                branch=branch,
                content_bytes=content,
                message="Update schedule (latest.xlsx) via Streamlit app"
            )
            st.success("✅ Готово! Расписание обновлено. Обновите страницу у зрителей (F5).")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка публикации: {e}")

st.subheader("📄 Текущее расписание")

# Загружаем общий файл из GitHub (если есть)
try:
    url = gh_raw_url(repo, branch, path)
    xlsx_bytes = download_latest_xlsx(url)
    df = parse_all_sheets(pd.io.common.BytesIO(xlsx_bytes))
except Exception as e:
    st.warning("Пока нет опубликованного файла расписания или он недоступен.")
    st.caption(f"Детали: {e}")
    st.stop()

st.success(f"Записей: {len(df)} | Листов: {df['Лист'].nunique()}")

# --- Filters ---
col1, col2, col3, col4 = st.columns([1, 1, 2, 2])
mode = col1.selectbox("Режим", ["По преподавателю", "По группе", "Всё"])

days = sorted([d for d in df["День"].unique().tolist() if d])
day_filter = col2.multiselect("Дни недели", options=days, default=[])

sheets = sorted(df["Лист"].unique().tolist())
sheet_filter = col3.multiselect("Листы", options=sheets, default=sheets)

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

show = view.copy()
show["Дата"] = show["Дата"].dt.strftime("%d.%m.%Y")

st.dataframe(
    show[["Лист", "Дата", "День", "Пара", "Группа", "Дисциплина", "Преподаватель", "Аудитория"]],
    use_container_width=True,
    hide_index=True
)

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

csv = view.to_csv(index=False, encoding="utf-8-sig")
st.download_button("⬇️ Скачать выбранное (CSV)", data=csv, file_name="schedule_filtered.csv", mime="text/csv")
