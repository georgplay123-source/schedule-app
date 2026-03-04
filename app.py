import streamlit as st
import pandas as pd
import numpy as np
import re
import requests
import base64
from io import BytesIO
from datetime import datetime, time
from zoneinfo import ZoneInfo
import uuid

TZ = ZoneInfo("Europe/Berlin")

# -------------------- Page --------------------
st.set_page_config(page_title="Расписание", layout="wide")
st.title("📅 Расписание")

st.markdown("""
<style>
.block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1200px; }
.card {
  border: 1px solid rgba(49, 51, 63, 0.20);
  border-radius: 16px;
  padding: 14px 16px;
  margin-bottom: 10px;
  background: rgba(255,255,255,0.02);
}
.card h4 { margin: 0 0 10px 0; font-size: 1.05rem; }
.rowline { margin: 8px 0; line-height: 1.35; }
.badge {
  display:inline-block;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid rgba(49, 51, 63, 0.25);
  font-size: 0.82rem;
  opacity: 0.92;
}
.muted { opacity: 0.78; }
hr.soft { border: none; border-top: 1px solid rgba(49, 51, 63, 0.15); margin: 12px 0; }
.smallcap { font-size: 0.88rem; opacity: 0.8; }
</style>
""", unsafe_allow_html=True)

# -------------------- Admin auth --------------------
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

# -------------------- GitHub helpers --------------------
def gh_headers():
    return {
        "Authorization": f"token {st.secrets['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }

def gh_raw_url(repo: str, branch: str, path: str) -> str:
    owner, name = repo.split("/", 1)
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"

@st.cache_data(ttl=60)
def download_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Не удалось скачать файл ({r.status_code}).")
    return r.content

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

@st.cache_data(ttl=60)
def gh_get_latest_file_commit_datetime(repo: str, branch: str, path: str):
    url = f"https://api.github.com/repos/{repo}/commits"
    r = requests.get(
        url,
        headers=gh_headers(),
        params={"path": path, "sha": branch, "per_page": 1},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub commits error {r.status_code}: {r.text}")

    arr = r.json()
    if not arr:
        return None

    commit = arr[0].get("commit", {})
    committer = commit.get("committer", {}) or {}
    author = commit.get("author", {}) or {}
    dt_str = committer.get("date") or author.get("date")
    if not dt_str:
        return None

    dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt_utc

# -------------------- Pair schedule (your rules) --------------------
def get_pair_times_for_day(day_name: str) -> dict[int, tuple[str, str]]:
    d = (day_name or "").strip().upper()

    monday = {
        1: ("09:00", "10:10"),
        2: ("10:20", "11:30"),
        3: ("11:50", "13:00"),
        4: ("13:10", "14:20"),
        5: ("14:30", "15:30"),
        6: ("16:00", "17:10"),
        7: ("17:20", "18:30"),
    }
    tue_fri = {
        1: ("09:00", "10:10"),
        2: ("10:20", "11:30"),
        3: ("11:50", "13:00"),
        4: ("13:10", "14:20"),
        5: ("14:30", "15:30"),
        6: ("15:50", "17:00"),
        7: ("17:10", "18:20"),
    }
    saturday = {
        1: ("09:00", "10:00"),
        2: ("10:10", "11:10"),
        3: ("11:30", "12:30"),
        4: ("12:40", "13:40"),
        5: ("13:50", "14:50"),
        6: ("15:00", "16:00"),
        7: ("16:10", "17:10"),
    }

    if "ПОН" in d:
        return monday
    if "СУБ" in d:
        return saturday
    # Вт–Пт (и fallback)
    return tue_fri

def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))

def dt_local(d: datetime.date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=TZ)

def ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

def make_ics(df_view: pd.DataFrame) -> str:
    now_utc = datetime.now(tz=ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//College Schedule//Streamlit//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    if df_view.empty:
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines)

    # Ensure types
    tmp = df_view.copy()
    tmp = tmp.sort_values(["Дата", "Пара", "Лист", "Группа"])

    for _, row in tmp.iterrows():
        d = row["Дата"].date() if hasattr(row["Дата"], "date") else pd.to_datetime(row["Дата"]).date()
        day_name = str(row.get("День", "")).strip()
        pair = int(row["Пара"])

        pair_map = get_pair_times_for_day(day_name)
        start_s, end_s = pair_map.get(pair, ("09:00", "10:00"))

        start_t = parse_hhmm(start_s)
        end_t = parse_hhmm(end_s)

        dtstart = dt_local(d, start_t).strftime("%Y%m%dT%H%M%S")
        dtend = dt_local(d, end_t).strftime("%Y%m%dT%H%M%S")

        summary = f"{row.get('Дисциплина','')}"
        location = f"{row.get('Аудитория','')}".strip()

        desc_parts = []
        if row.get("Преподаватель"):
            desc_parts.append(f"Преподаватель: {row.get('Преподаватель')}")
        if row.get("Группа"):
            desc_parts.append(f"Группа: {row.get('Группа')}")
        if row.get("Лист"):
            desc_parts.append(f"Лист: {row.get('Лист')}")
        if location:
            desc_parts.append(f"Аудитория: {location}")
        description = "\n".join(desc_parts)

        uid = str(uuid.uuid4()) + "@schedule-app"

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;TZID=Europe/Berlin:{dtstart}",
            f"DTEND;TZID=Europe/Berlin:{dtend}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(description)}",
        ])
        if location:
            lines.append(f"LOCATION:{ics_escape(location)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

# -------------------- Parsing (your formats) --------------------
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

# -------------------- Render helpers --------------------
def render_day_cards(df_day: pd.DataFrame):
    df_day = df_day.sort_values(["Дата", "Пара", "Лист", "Группа"])
    df_day["Дата_str"] = df_day["Дата"].dt.strftime("%d.%m.%Y")

    for (date_str, day), chunk in df_day.groupby(["Дата_str", "День"], sort=False):
        st.markdown(f"""
        <div class="card">
          <h4>🗓️ {date_str} <span class="muted">— {day}</span></h4>
        """, unsafe_allow_html=True)

        last_pair = None
        for _, row in chunk.iterrows():
            pair = int(row["Пара"])
            if last_pair is not None and pair != last_pair:
                st.markdown('<hr class="soft" />', unsafe_allow_html=True)
            last_pair = pair

            pair_times = get_pair_times_for_day(day)
            start_s, end_s = pair_times.get(pair, ("", ""))
            time_part = f"{start_s}–{end_s}" if start_s else ""

            st.markdown(
                f"""
                <div class="rowline">
                  <span class="badge">{pair} пара</span>
                  <span class="muted"> {time_part}</span>
                  <span class="muted"> · </span>
                  <b>{row.get('Лист','')}</b> / <b>{row.get('Группа','')}</b>
                  <span class="muted"> · </span>
                  {row.get('Дисциплина','')}
                  <br/>
                  <span class="muted">{row.get('Преподаватель','')}</span>
                  <span class="muted"> · ауд.</span> {row.get('Аудитория','')}
                </div>
                """,
                unsafe_allow_html=True
            )

        st.markdown("</div>", unsafe_allow_html=True)

# -------------------- Main --------------------
admin_ok = is_admin()

repo = st.secrets.get("GITHUB_REPO", "")
branch = st.secrets.get("GITHUB_BRANCH", "main")
path = st.secrets.get("GITHUB_FILE_PATH", "data/latest.xlsx")

# Header info
c_hdr1, c_hdr2 = st.columns([3, 2])

with c_hdr1:
    st.markdown('<div class="smallcap">Откройте ссылку — расписание уже будет показано. '
                'Админ обновляет файл через панель слева.</div>', unsafe_allow_html=True)

with c_hdr2:
    try:
        dt_utc = gh_get_latest_file_commit_datetime(repo, branch, path)
        if dt_utc:
            dt_local_show = dt_utc.astimezone(TZ)
            st.markdown(
                f'<div class="smallcap" style="text-align:right;">🕒 Обновлено: <b>{dt_local_show.strftime("%d.%m.%Y %H:%M")}</b></div>',
                unsafe_allow_html=True
            )
    except Exception:
        pass

# Admin upload
if admin_ok:
    st.sidebar.markdown("### ⬆️ Обновить расписание")
    new_file = st.sidebar.file_uploader("Загрузите Excel (.xlsx)", type=["xlsx"], key="admin_uploader")
    if new_file and st.sidebar.button("Опубликовать"):
        try:
            content = new_file.getvalue()
            gh_put_file(
                repo=repo,
                path=path,
                branch=branch,
                content_bytes=content,
                message="Update schedule (latest.xlsx) via Streamlit app"
            )
            st.sidebar.success("✅ Расписание обновлено!")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Ошибка публикации: {e}")

# Load published schedule
st.markdown("### 📄 Текущее расписание")

try:
    url = gh_raw_url(repo, branch, path)
    xlsx_bytes = download_bytes(url)
    df = parse_all_sheets(BytesIO(xlsx_bytes))
except Exception as e:
    st.warning("Пока нет опубликованного файла расписания или он недоступен.")
    st.caption(f"Детали: {e}")
    st.stop()

st.success(f"Записей: {len(df)} | Листов: {df['Лист'].nunique()}")

# Quick: Today
today_local = datetime.now(TZ).date()
if "date_quick_mode" not in st.session_state:
    st.session_state["date_quick_mode"] = "all"  # all | today

qb1, qb2, qb3 = st.columns([1.2, 1.0, 8.0])
if qb1.button("📍 Сегодня"):
    st.session_state["date_quick_mode"] = "today"
if qb2.button("♻️ Сброс"):
    st.session_state["date_quick_mode"] = "all"

if st.session_state["date_quick_mode"] == "today":
    st.info(f"Показаны занятия только за сегодня: {today_local.strftime('%d.%m.%Y')}")

# Filters
col1, col2, col3, col4 = st.columns([1.4, 1.7, 2.6, 2.6])
mode = col1.selectbox("Режим", ["По преподавателю", "По группе", "Всё"])

days = sorted([d for d in df["День"].unique().tolist() if d])
day_filter = col2.multiselect("Дни недели", options=days, default=[])

sheets = sorted(df["Лист"].unique().tolist())
sheet_filter = col3.multiselect("Листы", options=sheets, default=sheets)

query = col4.text_input("Поиск (фамилия / предмет / аудитория / группа)")

view = df.copy()

if st.session_state["date_quick_mode"] == "today":
    view = view[view["Дата"].dt.date == today_local]

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

# Tabs
tab_days, tab_table, tab_cal = st.tabs(["📅 По дням", "📋 Таблица", "🗓️ Календарь"])

with tab_days:
    if view.empty:
        st.info("Ничего не найдено по выбранным фильтрам.")
    else:
        render_day_cards(view)

with tab_table:
    show = view.copy()
    show["Дата"] = show["Дата"].dt.strftime("%d.%m.%Y")
    st.dataframe(
        show[["Лист", "Дата", "День", "Пара", "Группа", "Дисциплина", "Преподаватель", "Аудитория"]],
        use_container_width=True,
        hide_index=True
    )

with tab_cal:
    st.write("Скачайте календарь и импортируйте в Google Calendar / Outlook.")
    st.caption("Экспорт учитывает текущие фильтры (например: только ваш преподаватель, только 'Сегодня', выбранные листы).")

    with st.expander("⏱️ Расписание пар (как используется в ICS)"):
        st.markdown("""
**Понедельник:**
1) 09:00–10:10  
2) 10:20–11:30  
3) 11:50–13:00  
4) 13:10–14:20  
5) 14:30–15:30  
6) 16:00–17:10  
7) 17:20–18:30  

**Вторник–Пятница:**
1) 09:00–10:10  
2) 10:20–11:30  
3) 11:50–13:00  
4) 13:10–14:20  
5) 14:30–15:30  
6) 15:50–17:00  
7) 17:10–18:20  

**Суббота:**
1) 09:00–10:00  
2) 10:10–11:10  
3) 11:30–12:30  
4) 12:40–13:40  
5) 13:50–14:50  
6) 15:00–16:00  
7) 16:10–17:10
        """)

# Downloads
d1, d2 = st.columns([1, 1])

csv = view.to_csv(index=False, encoding="utf-8-sig")
d1.download_button("⬇️ Скачать выбранное (CSV)", data=csv, file_name="schedule_filtered.csv", mime="text/csv")

try:
    ics_text = make_ics(view)
    d2.download_button(
        "📅 Скачать календарь (ICS)",
        data=ics_text.encode("utf-8"),
        file_name="schedule.ics",
        mime="text/calendar"
    )
except Exception as e:
    d2.warning(f"Не удалось собрать ICS: {e}")
