from flask import Flask, render_template, request, jsonify
from flask_caching import Cache
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import re
import os, json

app = Flask(__name__)
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})

SPREADSHEET_ID = "1rsplfNq4e7d-nrp-Wlg1Mn9dsgjAcNn49yPQDXdzwg8"

GRADE_SHEETS = {"1": "M1", "2": "M2", "3": "M3"}

# 체크 컬럼 (G~J)
CHECK_COLS = ["G", "H", "I", "J"]
ALLOWED_MARKS = {"⭕", "△", "✕", ""}

# 직전보강 입력 컬럼 (K~M) — 숫자/문자 자유 입력
RECENT_TEXT_COLS = ["K", "L", "M"]

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    # 1) 배포 환경(Render 등): 환경변수로 JSON 전체를 넣는 방식
    if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)

    # 2) 로컬 개발: 파일로 읽는 방식(기존 유지)
    else:
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)

    return build("sheets", "v4", credentials=creds)

def sheet_name_by_grade(grade: str) -> str:
    if grade not in GRADE_SHEETS:
        raise ValueError("invalid grade")
    return GRADE_SHEETS[grade]

def parse_mmdd(s: str):
    """과학일 문자열에서 월/일 추출. 실패하면 None."""
    if not s:
        return None
    t = str(s).strip()
    if not t:
        return None
    nums = re.findall(r"\d+", t)
    if len(nums) < 2:
        return None
    m = int(nums[0]); d = int(nums[1])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return (m, d)

@app.route("/")
def index():
    return render_template("index.html")

# ✅ 반 목록: 10분 캐시
@app.get("/api/classes")
@cache.cached(timeout=600, query_string=True)
def api_classes():
    grade = request.args.get("grade")
    sheet = sheet_name_by_grade(grade)

    svc = get_sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A2:A"
    ).execute()

    vals = resp.get("values", [])
    classes = []
    seen = set()
    for v in vals:
        name = (v[0] if v else "").strip()
        if name and name not in seen:
            seen.add(name)
            classes.append(name)
    classes.sort()
    return jsonify({"ok": True, "grade": grade, "classes": classes})

# ✅ 학생 목록(반 단위): 30초 캐시 (A~J까지만)
@app.get("/api/students")
@cache.cached(timeout=30, query_string=True)
def api_students():
    grade = request.args.get("grade")
    class_name = request.args.get("class")
    sheet = sheet_name_by_grade(grade)

    svc = get_sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A2:J"
    ).execute()

    rows = resp.get("values", [])
    students = []

    for i, row in enumerate(rows):
        a = (row[0] if len(row) > 0 else "").strip()
        if not a or a != class_name:
            continue

        def get(idx):
            return row[idx] if len(row) > idx else ""

        sheet_row = i + 2

        students.append({
            "sheet": sheet,
            "grade": grade,
            "class": a,
            "sheet_row": sheet_row,
            "name": get(1),
            "school": get(2),
            "range": get(3),
            "period": get(4),
            "exam_date": get(5),
            "otwo": get(6),
            "essay": get(7),
            "freq": get(8),
            "freq_essay": get(9),
        })

    return jsonify({"ok": True, "grade": grade, "class": class_name, "students": students})

# ✅ 직전보강: 1~3학년 전체(A~M) + 정렬 + K~M 값 포함
@app.get("/api/recent")
@cache.cached(timeout=20, query_string=True)
def api_recent():
    svc = get_sheets_service()
    all_students = []

    for grade, sheet in [("1", "M1"), ("2", "M2"), ("3", "M3")]:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet}!A2:M"  # ✅ K~M 포함
        ).execute()
        rows = resp.get("values", [])

        for i, row in enumerate(rows):
            def get(idx):
                return row[idx] if len(row) > idx else ""

            name = str(get(1)).strip()
            if not name:
                continue

            sheet_row = i + 2
            exam_date = str(get(5)).strip()
            mmdd = parse_mmdd(exam_date)

            all_students.append({
                "sheet": sheet,
                "grade": grade,
                "class": str(get(0)).strip(),
                "sheet_row": sheet_row,
                "name": get(1),
                "school": get(2),
                "range": get(3),
                "period": get(4),
                "exam_date": exam_date,

                # G~J (읽기 전용 표시용)
                "otwo": get(6),
                "essay": get(7),
                "freq": get(8),
                "freq_essay": get(9),

                # K~M (직보 입력)
                "jb1": get(10),  # K
                "jb2": get(11),  # L
                "jb3": get(12),  # M

                "_mmdd": mmdd,
            })

    def sort_key(st):
        mmdd = st["_mmdd"]
        mmdd_key = (99, 99) if mmdd is None else mmdd
        grade_key = int(st["grade"]) if str(st["grade"]).isdigit() else 9
        school_key = str(st["school"] or "")
        return (mmdd_key[0], mmdd_key[1], grade_key, school_key)

    all_students.sort(key=sort_key)
    for st in all_students:
        st.pop("_mmdd", None)

    return jsonify({"ok": True, "students": all_students})

# ✅ apply: (A) 반 화면: grade 기반 G~J
#         (B) 직전보강: change마다 sheet 지정 + K~M 지원
@app.post("/api/apply")
def api_apply():
    data = request.get_json(force=True)
    changes = data.get("changes", [])

    grade = data.get("grade")
    default_sheet = sheet_name_by_grade(str(grade)) if grade else None

    allowed_cols = set(CHECK_COLS + RECENT_TEXT_COLS)

    updates = []
    for ch in changes:
        sheet_row = int(ch.get("sheet_row"))
        col = str(ch.get("col")).upper()
        value = "" if ch.get("value") is None else str(ch.get("value"))
        sheet = ch.get("sheet") or default_sheet

        if not sheet:
            return jsonify({"ok": False, "error": "missing sheet/grade"}), 400
        if col not in allowed_cols:
            return jsonify({"ok": False, "error": f"invalid col: {col}"}), 400
        if sheet_row < 2:
            return jsonify({"ok": False, "error": "invalid row"}), 400

        # G~J 검증(⭕/△/✕/"")
        if col in CHECK_COLS and value not in ALLOWED_MARKS:
            return jsonify({"ok": False, "error": f"invalid value for {col}: {value}"}), 400

        # K~M은 자유 입력 (너무 길지만 않게)
        if col in RECENT_TEXT_COLS and len(value) > 200:
            return jsonify({"ok": False, "error": f"value too long for {col}"}), 400

        updates.append({
            "range": f"{sheet}!{col}{sheet_row}",
            "values": [[value]]
        })

    svc = get_sheets_service()
    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates}
        ).execute()

    cache.clear()
    return jsonify({"ok": True, "applied": len(updates)})

if __name__ == "__main__":
    app.run(debug=True)

