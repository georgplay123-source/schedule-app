import streamlit as st
import pandas as pd
import numpy as np
import re
import requests
import base64
from io import BytesIO
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import uuid

# Изменено: Europe/Berlin -> Asia/Yekaterinburg (Екатеринбург, UTC+5)
TZ = ZoneInfo("Asia/Yekaterinburg")

# -------------------- Page --------------------
st.set_page_config(page_title="Расписание", layout="wide")
st.title("📅 Расписание колледжа")

# ---------- simple mobile detection ----------
def detect_mobile() -> bool:
    return False

if "ui_mobile" not in st.session_state:
    st.session_state["ui_mobile"] = detect_mobile()

# -------------------- Styles --------------------
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
.smallcap { font-size: 0.88rem; opacity: 0.82; }
.bigbtn button { width: 100%; height: 3.1rem; font-size: 1.05rem; border-radius: 14px; }
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

# -------------------- Pair schedule --------------------
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

    tmp = df_view.copy().sort_values(["Дата", "Пара", "Группа", "Подгруппа"])

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

        summary = f"{row.get('Дисциплина', '')}"
        location = f"{row.get('Аудитория', '')}".strip()

        desc_parts = []
        if row.get("Преподаватель"):
            desc_parts.append(f"Преподаватель: {row.get('Преподаватель')}")
        if row.get("Группа"):
            desc_parts.append(f"Группа: {row.get('Группа')}")
        if row.get("Подгруппа"):
            desc_parts.append(f"Подгруппа: {row.get('Подгруппа')}")
        if row.get("Источник"):
            desc_parts.append(f"Источник: {row.get('Источник')}")
        if location:
            desc_parts.append(f"Аудитория: {location}")

        description = "\n".join(desc_parts)
        uid = str(uuid.uuid4()) + "@schedule-app"

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;TZID=Asia/Yekaterinburg:{dtstart}",
            f"DTEND;TZID=Asia/Yekaterinburg:{dtend}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(description)}",
        ])
        if location:
            lines.append(f"LOCATION:{ics_escape(location)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

# -------------------- Parsing uploaded schedules --------------------
def _clean_str(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() == "nan" else s

def parse_weekly_blocks_format(xlsx_file, source_name="") -> pd.DataFrame:
    """
    Формат A:
    Дата | День недели | Занятие | [Дисциплина, Преподаватель, Ауд., Группа]...
    """
    raw = pd.read_excel(xlsx_file, header=None)

    header_row = None
    for r in range(min(15, len(raw))):
        row = raw.iloc[r].astype(str).tolist()
        if ("Дата" in row) and ("День недели" in row) and ("Занятие" in row):
            header_row = r
            break
    if header_row is None:
        raise ValueError("Не найден недельный формат")

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

        temp["Подгруппа"] = ""
        temp["Источник"] = source_name
        result.append(temp)

    if not result:
        raise ValueError("Не удалось прочитать недельный формат")

    out = pd.concat(result, ignore_index=True)
    out = out.sort_values(["Дата", "Пара", "Группа"]).reset_index(drop=True)
    return out

def parse_official_wide_format(xlsx_file, source_name="") -> pd.DataFrame:
    """
    Формат B:
    День недели | Занятие | Урок | Время | ... блоки групп ...
    Широкая официальная форма.
    """
    raw = pd.read_excel(xlsx_file, header=None)

    header_row = None
    for r in range(min(20, len(raw))):
        row = raw.iloc[r].astype(str).tolist()
        if ("День недели" in row) and ("Занятие" in row) and ("Урок" in row):
            header_row = r
            break
    if header_row is None:
        raise ValueError("Не найден широкий формат")

    group_row = header_row + 2
    subgroup_row = header_row + 3
    row0 = raw.iloc[header_row].tolist()

    disc_cols = [i for i, v in enumerate(row0) if str(v).strip() == "Дисциплина"]
    if not disc_cols:
        raise ValueError("Не найдены блоки 'Дисциплина'")

    aud_cols = {d: d + 3 for d in disc_cols if d + 3 < raw.shape[1]}

    group_names = {}
    subgroup_names = {}
    for d in disc_cols:
        g = _clean_str(raw.iloc[group_row, d]) if group_row < len(raw) else ""
        sg = _clean_str(raw.iloc[subgroup_row, d]) if subgroup_row < len(raw) else ""
        group_names[d] = g
        subgroup_names[d] = sg

    records = []
    current_date = None
    current_day = ""

    start_row = header_row + 4
    r = start_row

    while r < len(raw) - 1:
        first_cell = _clean_str(raw.iloc[r, 0])

        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s*(.*)", first_cell)
        if m:
            current_date = pd.to_datetime(m.group(1), dayfirst=True, errors="coerce")
            current_day = m.group(2).strip()

        pair_candidate = pd.to_numeric(raw.iloc[r, 1], errors="coerce")

        if pd.notna(pair_candidate) and current_date is not None:
            current_pair = int(pair_candidate)
            teacher_row = r + 1

            for d in disc_cols:
                discipline = _clean_str(raw.iloc[r, d])
                teacher = _clean_str(raw.iloc[teacher_row, d]) if teacher_row < len(raw) else ""
                aud = _clean_str(raw.iloc[r, aud_cols.get(d, d)]) if d in aud_cols else ""

                if not discipline and not teacher and not aud:
                    continue

                records.append({
                    "Дата": current_date,
                    "День": current_day,
                    "Пара": current_pair,
                    "Дисциплина": discipline,
                    "Преподаватель": teacher,
                    "Аудитория": aud,
                    "Группа": group_names.get(d, ""),
                    "Подгруппа": subgroup_names.get(d, ""),
                    "Источник": source_name
                })

            r += 2
            continue

        r += 1

    if not records:
        raise ValueError("Не удалось прочитать широкий формат")

    out = pd.DataFrame(records)

    for c in ["Дисциплина", "Преподаватель", "Аудитория", "Группа", "Подгруппа", "Источник", "День"]:
        out[c] = out[c].astype(str).replace("nan", "").str.strip()

    out = out[out["Дисциплина"].str.strip() != ""]
    out = out.sort_values(["Дата", "Пара", "Группа", "Подгруппа"]).reset_index(drop=True)
    return out

def parse_single_uploaded_schedule(file_obj) -> pd.DataFrame:
    source_name = getattr(file_obj, "name", "uploaded.xlsx")

    try:
        file_obj.seek(0)
        return parse_official_wide_format(file_obj, source_name=source_name)
    except Exception:
        pass

    try:
        file_obj.seek(0)
        return parse_weekly_blocks_format(file_obj, source_name=source_name)
    except Exception:
        pass

    raise ValueError(f"Не удалось распознать формат файла: {source_name}")

def parse_multiple_uploaded_schedules(uploaded_files):
    parts = []
    errors = []

    for f in uploaded_files:
        try:
            f.seek(0)
            part = parse_single_uploaded_schedule(f)
            parts.append(part)
        except Exception as e:
            errors.append(f"{getattr(f, 'name', 'file')}: {e}")

    if not parts:
        raise ValueError("Не удалось распарсить ни один файл")

    out = pd.concat(parts, ignore_index=True)

    for c in ["День", "Группа", "Подгруппа", "Дисциплина", "Преподаватель", "Аудитория", "Источник"]:
        out[c] = out[c].astype(str).replace("nan", "").str.strip()

    out["Пара"] = pd.to_numeric(out["Пара"], errors="coerce")
    out["Дата"] = pd.to_datetime(out["Дата"], errors="coerce")
    out = out.dropna(subset=["Дата", "Пара"])

    out = out.drop_duplicates(subset=[
        "Дата", "День", "Пара", "Группа", "Подгруппа",
        "Дисциплина", "Преподаватель", "Аудитория"
    ])

    out = out.sort_values(["Дата", "Пара", "Группа", "Подгруппа"]).reset_index(drop=True)
    return out, errors

# -------------------- Loading published data --------------------
def load_published_schedule(repo: str, branch: str):
    json_url = gh_raw_url(repo, branch, "data/latest.json")
    try:
        raw_json = download_bytes(json_url)
        df = pd.read_json(BytesIO(raw_json))
        df["Дата"] = pd.to_datetime(df["Дата"], errors="coerce")
        df["Пара"] = pd.to_numeric(df["Пара"], errors="coerce")
        for c in ["День", "Группа", "Подгруппа", "Дисциплина", "Преподаватель", "Аудитория", "Источник"]:
            if c not in df.columns:
                df[c] = ""
            df[c] = df[c].astype(str).replace("nan", "").str.strip()
        df = df.dropna(subset=["Дата", "Пара"])
        return df
    except Exception:
        pass

    xlsx_url = gh_raw_url(repo, branch, "data/latest.xlsx")
    raw_xlsx = download_bytes(xlsx_url)
    df = pd.read_excel(BytesIO(raw_xlsx), sheet_name="schedule")
    df["Дата"] = pd.to_datetime(df["Дата"], errors="coerce")
    df["Пара"] = pd.to_numeric(df["Пара"], errors="coerce")
    for c in ["День", "Группа", "Подгруппа", "Дисциплина", "Преподаватель", "Аудитория", "Источник"]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].astype(str).replace("nan", "").str.strip()
    df = df.dropna(subset=["Дата", "Пара"])
    return df

# -------------------- UI render --------------------
def render_day_cards(df_day: pd.DataFrame, compact: bool):
    df_day = df_day.sort_values(["Дата", "Пара", "Группа", "Подгруппа"])
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

            subgroup_part = f" / {row.get('Подгруппа', '')}" if str(row.get("Подгруппа", "")).strip() else ""

            if compact:
                st.markdown(
                    f"""
                    <div class="rowline">
                      <span class="badge">{pair}</span>
                      <span class="muted">{time_part}</span>
                      <span class="muted"> · </span>
                      <b>{row.get('Группа', '')}{subgroup_part}</b>
                      <span class="muted"> · </span>
                      {row.get('Дисциплина', '')}
                      <span class="muted"> · </span>
                      ауд. {row.get('Аудитория', '')}
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            else:
                st.markdown(
                    f"""
                    <div class="rowline">
                      <span class="badge">{pair} пара</span>
                      <span class="muted"> {time_part}</span>
                      <span class="muted"> · </span>
                      <b>{row.get('Группа', '')}{subgroup_part}</b>
                      <span class="muted"> · </span>
                      {row.get('Дисциплина', '')}
                      <br/>
                      <span class="muted">{row.get('Преподаватель', '')}</span>
                      <span class="muted"> · ауд.</span> {row.get('Аудитория', '')}
                    </div>
                    """,
                    unsafe_allow_html=True
                )

        st.markdown("</div>", unsafe_allow_html=True)

# -------------------- Main --------------------
admin_ok = is_admin()

repo = st.secrets.get("GITHUB_REPO", "")
branch = st.secrets.get("GITHUB_BRANCH", "main")

# Header
c_hdr1, c_hdr2, c_hdr3 = st.columns([3, 2, 1.4])

with c_hdr1:
    st.markdown(
        '<div class="smallcap">Откройте ссылку — расписание будет показано автоматически. '
        'Админ обновляет комплект файлов через панель слева.</div>',
        unsafe_allow_html=True
    )

with c_hdr2:
    try:
        dt_utc = gh_get_latest_file_commit_datetime(repo, branch, "data/latest.json")
        if dt_utc is None:
            dt_utc = gh_get_latest_file_commit_datetime(repo, branch, "data/latest.xlsx")
        if dt_utc:
            dt_local_show = dt_utc.astimezone(TZ)
            st.markdown(
                f'<div class="smallcap">🕒 Обновлено: <b>{dt_local_show.strftime("%d.%m.%Y %H:%M")}</b></div>',
                unsafe_allow_html=True
            )
    except Exception:
        pass

with c_hdr3:
    st.session_state["ui_mobile"] = st.toggle("📱 Мобильный режим", value=st.session_state["ui_mobile"])

# Admin upload
if admin_ok:
    st.sidebar.markdown("### ⬆️ Обновить расписание")
    new_files = st.sidebar.file_uploader(
        "Загрузите файлы расписания (.xlsx)",
        type=["xlsx"],
        accept_multiple_files=True,
        key="admin_uploader_multi"
    )

    if new_files and st.sidebar.button("Опубликовать комплект"):
        try:
            df_all, parse_errors = parse_multiple_uploaded_schedules(new_files)

            df_json = df_all.copy()
            df_json["Дата"] = df_json["Дата"].dt.strftime("%Y-%m-%d")
            json_bytes = df_json.to_json(orient="records", force_ascii=False).encode("utf-8")

            xlsx_buffer = BytesIO()
            with pd.ExcelWriter(xlsx_buffer, engine="openpyxl") as writer:
                df_all.to_excel(writer, index=False, sheet_name="schedule")
                if parse_errors:
                    pd.DataFrame({"Ошибки": parse_errors}).to_excel(writer, index=False, sheet_name="errors")
            xlsx_bytes = xlsx_buffer.getvalue()

            gh_put_file(
                repo=repo,
                path="data/latest.xlsx",
                branch=branch,
                content_bytes=xlsx_bytes,
                message="Update merged schedule (latest.xlsx)"
            )

            gh_put_file(
                repo=repo,
                path="data/latest.json",
                branch=branch,
                content_bytes=json_bytes,
                message="Update merged schedule (latest.json)"
            )

            st.sidebar.success(f"✅ Опубликовано записей: {len(df_all)}")

            if parse_errors:
                st.sidebar.warning("Часть файлов не удалось прочитать:")
                for err in parse_errors:
                    st.sidebar.write(f"— {err}")

            st.cache_data.clear()
            st.rerun()

        except Exception as e:
            st.sidebar.error(f"Ошибка публикации: {e}")

# Load schedule
st.markdown("### 📄 Текущее расписание")

try:
    df = load_published_schedule(repo, branch)
except Exception as e:
    st.warning("Пока нет опубликованного расписания или оно недоступно.")
    st.caption(f"Детали: {e}")
    st.stop()

# Session filters
today_local = datetime.now(TZ).date()
tomorrow_local = today_local + timedelta(days=1)

if "quick_mode" not in st.session_state:
    st.session_state["quick_mode"] = "all"
if "my_teacher" not in st.session_state:
    st.session_state["my_teacher"] = ""

teachers_all = sorted([t for t in df["Преподаватель"].unique().tolist() if str(t).strip()])

with st.sidebar:
    st.markdown("### 🙋 Мой преподаватель")
    typed = st.text_input("Введите фамилию/имя (поиск)", value=st.session_state["my_teacher"])
    typed_l = typed.strip().lower()
    candidates = teachers_all
    if typed_l:
        candidates = [t for t in teachers_all if typed_l in t.lower()]
    candidates = candidates[:50] if len(candidates) > 50 else candidates
    chosen = st.selectbox("Выберите из списка", options=[""] + candidates, index=0)
    if st.button("💾 Сохранить моего преподавателя"):
        st.session_state["my_teacher"] = chosen if chosen else typed
        st.success("Сохранено для текущей сессии")

st.success(f"Записей: {len(df)} | Групп: {df['Группа'].nunique()}")

# Quick buttons
btn_class = "bigbtn" if st.session_state["ui_mobile"] else ""
b1, b2, b3, b4, b5 = st.columns([1.2, 1.2, 1.7, 1.7, 1.2])

with b1:
    st.markdown(f'<div class="{btn_class}">', unsafe_allow_html=True)
    if st.button("📍 Сегодня"):
        st.session_state["quick_mode"] = "today"
    st.markdown('</div>', unsafe_allow_html=True)

with b2:
    st.markdown(f'<div class="{btn_class}">', unsafe_allow_html=True)
    if st.button("➡️ Завтра"):
        st.session_state["quick_mode"] = "tomorrow"
    st.markdown('</div>', unsafe_allow_html=True)

with b3:
    st.markdown(f'<div class="{btn_class}">', unsafe_allow_html=True)
    if st.button("🙋 Мои пары сегодня"):
        st.session_state["quick_mode"] = "my_today"
    st.markdown('</div>', unsafe_allow_html=True)

with b4:
    st.markdown(f'<div class="{btn_class}">', unsafe_allow_html=True)
    if st.button("🙋 Мои пары завтра"):
        st.session_state["quick_mode"] = "my_tomorrow"
    st.markdown('</div>', unsafe_allow_html=True)

with b5:
    st.markdown(f'<div class="{btn_class}">', unsafe_allow_html=True)
    if st.button("♻️ Сброс"):
        st.session_state["quick_mode"] = "all"
    st.markdown('</div>', unsafe_allow_html=True)

# ============================================================
# ИЗМЕНЕНИЯ ЗДЕСЬ: Убраны фильтры по группам с главной страницы
# Теперь только поисковая строка и выбор режима
# ============================================================

# Упрощённые фильтры (только поиск и режим)
if st.session_state["ui_mobile"]:
    col1, col2 = st.columns([1.2, 2.2])
    mode = col1.selectbox("Режим", ["Всё", "По преподавателю", "По группе"])
    query = col2.text_input("🔍 Поиск (фамилия / предмет / ауд.)")
else:
    col1, col2 = st.columns([1.4, 3.6])
    mode = col1.selectbox("Режим", ["Всё", "По преподавателю", "По группе"])
    query = col2.text_input("🔍 Поиск (фамилия / предмет / аудитория / группа)")

# Дни недели - вынесены в сайдбар (опционально, можно вообще убрать)
with st.sidebar:
    st.markdown("### 📅 Дни недели")
    days = sorted([d for d in df["День"].unique().tolist() if d])
    day_filter = st.multiselect("Показать дни", options=days, default=days)
    st.markdown("---")

# Apply filters
view = df.copy()

def apply_date_filter(frame: pd.DataFrame, which: str) -> pd.DataFrame:
    if which == "today":
        return frame[frame["Дата"].dt.date == today_local]
    if which == "tomorrow":
        return frame[frame["Дата"].dt.date == tomorrow_local]
    return frame

qm = st.session_state["quick_mode"]

if qm in ("today", "tomorrow"):
    view = apply_date_filter(view, qm)

if qm in ("my_today", "my_tomorrow"):
    myt = (st.session_state.get("my_teacher") or "").strip()
    if not myt:
        st.warning("Сначала выберите и сохраните 'Мой преподаватель' в боковой панели.")
    else:
        view = view[view["Преподаватель"].astype(str).str.strip() == myt]
        view = apply_date_filter(view, "today" if qm == "my_today" else "tomorrow")

if 'day_filter' in locals() and day_filter:
    view = view[view["День"].isin(day_filter)]

# Поиск по всем полям
if query.strip():
    q = query.strip().lower()
    mask = (
        view["Преподаватель"].str.lower().str.contains(q, na=False) |
        view["Дисциплина"].str.lower().str.contains(q, na=False) |
        view["Аудитория"].str.lower().str.contains(q, na=False) |
        view["Группа"].str.lower().str.contains(q, na=False) |
        view["Подгруппа"].str.lower().str.contains(q, na=False) |
        view["Источник"].str.lower().str.contains(q, na=False)
    )
    view = view[mask]

# Выбор по преподавателю или группе
if mode == "По преподавателю":
    teachers = sorted([t for t in view["Преподаватель"].unique().tolist() if str(t).strip()])
    if teachers:
        teacher = st.selectbox("👨‍🏫 Выберите преподавателя", options=teachers)
        view = view[view["Преподаватель"] == teacher]
elif mode == "По группе":
    groups = sorted([g for g in view["Группа"].unique().tolist() if str(g).strip()])
    if groups:
        group = st.selectbox("👥 Выберите группу", options=groups)
        view = view[view["Группа"] == group]

# Tabs
tab_days, tab_table, tab_cal = st.tabs(["📅 По дням", "📋 Таблица", "🗓️ Календарь"])

with tab_days:
    if view.empty:
        st.info("Ничего не найдено по выбранным фильтрам.")
    else:
        render_day_cards(view, compact=st.session_state["ui_mobile"])

with tab_table:
    show = view.copy()
    show["Дата"] = show["Дата"].dt.strftime("%d.%m.%Y")
    if st.session_state["ui_mobile"]:
        cols = ["Дата", "День", "Пара", "Группа", "Дисциплина", "Аудитория"]
    else:
        cols = ["Дата", "День", "Пара", "Группа", "Подгруппа", "Дисциплина", "Преподаватель", "Аудитория", "Источник"]

    cols = [c for c in cols if c in show.columns]

    st.dataframe(
        show[cols],
        use_container_width=True,
        hide_index=True
    )

with tab_cal:
    st.write("📅 Скачайте календарь и импортируйте в Google Calendar / Outlook.")
    st.caption("Экспорт учитывает текущие фильтры, включая 'Мои пары сегодня/завтра'.")
    st.caption(f"🕒 Часовой пояс: Екатеринбург (UTC+5)")

    with st.expander("⏱️ Расписание пар (используется в ICS)"):
        st.markdown("""
**Понедельник:** 1) 09:00–10:10 · 2) 10:20–11:30 · 3) 11:50–13:00 · 4) 13:10–14:20 · 5) 14:30–15:30 · 6) 16:00–17:10 · 7) 17:20–18:30  
**Вт–Пт:** 1) 09:00–10:10 · 2) 10:20–11:30 · 3) 11:50–13:00 · 4) 13:10–14:20 · 5) 14:30–15:30 · 6) 15:50–17:00 · 7) 17:10–18:20  
**Суббота:** 1) 09:00–10:00 · 2) 10:10–11:10 · 3) 11:30–12:30 · 4) 12:40–13:40 · 5) 13:50–14:50 · 6) 15:00–16:00 · 7) 16:10–17:10
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
