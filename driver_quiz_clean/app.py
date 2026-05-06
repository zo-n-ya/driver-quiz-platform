import os, json, logging
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, render_template, redirect, url_for, session
from flask_cors import CORS
import psycopg2, psycopg2.extras
from werkzeug.utils import secure_filename
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.getenv('SECRET_KEY', 'driver-quiz-change-me')
CORS(app)
logging.basicConfig(level=logging.INFO)

DB = dict(host=os.getenv('DB_HOST', '127.0.0.1'), port=int(os.getenv('DB_PORT', '5432')), dbname=os.getenv('DB_NAME', 'driver_quiz_db'), user=os.getenv('DB_USER', 'driver_quiz_user'), password=os.getenv('DB_PASSWORD', os.getenv('DATABASE_PASSWORD', '')))
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL', '').strip()
GOOGLE_SHEET_TAB = os.getenv('GOOGLE_SHEET_TAB', 'quiz_results').strip()
GOOGLE_KEY_FILE = os.getenv('GOOGLE_KEY_FILE', '/path/to/your/google-service-account.json')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'podadmin')
QUESTION_BANK_PASSWORD = os.getenv('QUESTION_BANK_PASSWORD', 'uni12345')
app.permanent_session_lifetime = timedelta(days=30)
LABELS = {'no_address': {'zh': '没有地址信息', 'en': 'No Address Info', 'es': 'Sin información de dirección'}, 'location_clear': {'zh': '投递位置不清楚', 'en': 'Location Not Clear', 'es': 'Ubicación no clara'}, 'no_label': {'zh': '面单不清楚', 'en': 'No Clear Shipping Label', 'es': 'Etiqueta no clara'}, 'unsafe': {'zh': '公共区域或不安全区域', 'en': 'Public or Unsafe Area', 'es': 'Área pública o insegura'}, 'outside': {'zh': '放在建筑物外面', 'en': 'Leave Outside of Building', 'es': 'Dejado fuera del edificio'}}
WAREHOUSES = ['31 ATL','44 SAV','46 CHS','50 BNA','60 TYS','61 GSP','62 CAE','66 BFM','67 BHM','76 JAN','81 GPT']
TEAM_IDS = ['997','1006','1014','1050','1061','1064','1087','1093','1119','1120','1151','1152','1174','1175','1200','1202','1228','1243','1251','1275','1422','1431']
CATEGORY_ORDER = ['location_clear','no_address','no_label','outside','unsafe']
CATEGORY_PREFIX = {code: f'{i+1:02d}' for i, code in enumerate(CATEGORY_ORDER)}
CAUGHT_REASONS = {'caught', 'caught_today', 'pod_failed', 'pod_check_failed', 'violation', '被查到', 'pod被查到不合格'}
_sheet_cache = None

def conn():
    return psycopg2.connect(**DB, cursor_factory=psycopg2.extras.RealDictCursor)

def init():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS quiz_attempts(id SERIAL PRIMARY KEY,driver_id TEXT NOT NULL,warehouse TEXT,team_id TEXT,language TEXT DEFAULT 'zh',category_code TEXT NOT NULL,trigger_reason TEXT,stage INT NOT NULL,quiz_set INT,is_random BOOLEAN DEFAULT FALSE,score INT DEFAULT 0,total INT DEFAULT 0,passed BOOLEAN DEFAULT FALSE,warning_count INT DEFAULT 0,started_at TIMESTAMPTZ DEFAULT NOW(),submitted_at TIMESTAMPTZ);""")
            cur.execute("""CREATE TABLE IF NOT EXISTS quiz_attempt_answers(id SERIAL PRIMARY KEY,attempt_id INT REFERENCES quiz_attempts(id) ON DELETE CASCADE,question_id INT REFERENCES quiz_questions(id) ON DELETE CASCADE,selected_answers JSONB DEFAULT '[]'::jsonb,correct_answers JSONB DEFAULT '[]'::jsonb,is_correct BOOLEAN DEFAULT FALSE,answered_at TIMESTAMPTZ DEFAULT NOW());""")
            cur.execute("""CREATE TABLE IF NOT EXISTS driver_penalties(id SERIAL PRIMARY KEY,driver_id TEXT NOT NULL,category_code TEXT NOT NULL,warehouse TEXT,team_id TEXT,trigger_reason TEXT,created_at TIMESTAMPTZ DEFAULT NOW());""")
            cur.execute("ALTER TABLE driver_penalties ADD COLUMN IF NOT EXISTS warehouse TEXT;")
            cur.execute("ALTER TABLE driver_penalties ADD COLUMN IF NOT EXISTS team_id TEXT;")
            cur.execute("ALTER TABLE driver_penalties ADD COLUMN IF NOT EXISTS trigger_reason TEXT;")
            cur.execute("""CREATE TABLE IF NOT EXISTS quiz_assignments(
                id SERIAL PRIMARY KEY,
                driver_id TEXT NOT NULL,
                warehouse TEXT NOT NULL,
                team_id TEXT NOT NULL,
                category_code TEXT NOT NULL,
                created_by TEXT,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INT NOT NULL DEFAULT 0,
                last_attempt_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                warning BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );""")
            cur.execute("ALTER TABLE quiz_attempts ADD COLUMN IF NOT EXISTS assignment_id INT;")
            cur.execute("ALTER TABLE quiz_assignments ADD COLUMN IF NOT EXISTS created_by TEXT;")
            cur.execute("ALTER TABLE quiz_assignments ADD COLUMN IF NOT EXISTS note TEXT;")
            cur.execute("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'published';")
            cur.execute("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS question_es TEXT;")
            cur.execute("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
            cur.execute("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")
            cur.execute("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS image_layout TEXT DEFAULT 'horizontal';")
            cur.execute("ALTER TABLE quiz_attempts ADD COLUMN IF NOT EXISTS question_ids JSONB DEFAULT '[]'::jsonb;")
            cur.execute("UPDATE quiz_questions SET status='published' WHERE status IS NULL;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quiz_assignments_filter ON quiz_assignments(warehouse, team_id, category_code, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quiz_assignments_driver ON quiz_assignments(driver_id, category_code, status);")
        c.commit()

def norm(v):
    v = '' if v is None else str(v).strip()
    return '✗' if v in ['x', 'X', '✗', '×'] else v

def arr(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return [norm(x) for x in v if norm(x)]

def same(a, b):
    return sorted(arr(a)) == sorted(arr(b))

def explain(t):
    t = (t or '').strip()
    if not t:
        return {'zh': '', 'en': '', 'es': ''}
    p = [x.strip() for x in t.split(' / ')]
    return {'zh': p[0], 'en': p[1], 'es': ' / '.join(p[2:])} if len(p) >= 3 else {'zh': t, 'en': t, 'es': t}

def qdict(r, answers=False):
    imgs = r.get('image_paths') or []
    if isinstance(imgs, str):
        try:
            imgs = json.loads(imgs)
        except Exception:
            imgs = []
    d = {'id': r['id'], 'category_code': r['category_code'], 'quiz_set': r['quiz_set'], 'question_order': r['question_order'], 'question_type': r['question_type'], 'question_zh': r.get('question_zh') or '', 'question_en': r.get('question_en') or '', 'question_es': r.get('question_es') or '', 'options': arr(r.get('options') or []), 'image_paths': imgs, 'image_layout': r.get('image_layout') or 'horizontal'}
    if answers:
        d.update(correct_answers=arr(r.get('correct_answers') or []), explanation=r.get('explanation') or '', explanation_by_lang=explain(r.get('explanation') or ''))
    return d

def questions(category, stage):
    with conn() as c:
        with c.cursor() as cur:
            if int(stage) <= 5:
                cur.execute("SELECT * FROM quiz_questions WHERE category_code=%s AND quiz_set=%s AND COALESCE(status,'published')='published' ORDER BY RANDOM()", (category, int(stage)))
            else:
                cur.execute("SELECT * FROM quiz_questions WHERE category_code=%s AND COALESCE(status,'published')='published' ORDER BY RANDOM() LIMIT 5", (category,))
            return cur.fetchall()

def questions_by_ids(ids):
    ids = [int(x) for x in (ids or [])]
    if not ids:
        return []
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM quiz_questions WHERE id = ANY(%s)", (ids,))
            rows = cur.fetchall()
    by_id = {int(r['id']): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]

def is_caught_reason(reason):
    return (reason or '').lower().strip() in CAUGHT_REASONS

def assignment_warning_count(driver, cat):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) cnt FROM quiz_assignments WHERE driver_id=%s AND category_code=%s", (driver, cat))
            return int(cur.fetchone()['cnt'])

def penalty_count(driver, cat):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""SELECT COUNT(*) cnt FROM driver_penalties WHERE driver_id=%s AND category_code=%s AND trigger_reason IN ('caught','caught_today','pod_failed','pod_check_failed','violation','被查到','pod被查到不合格')""", (driver, cat))
            return int(cur.fetchone()['cnt'])

def record_penalty(driver, cat, warehouse, team, reason):
    if is_caught_reason(reason):
        with conn() as c:
            with c.cursor() as cur:
                cur.execute('INSERT INTO driver_penalties(driver_id,category_code,warehouse,team_id,trigger_reason) VALUES(%s,%s,%s,%s,%s)', (driver, cat, warehouse, team, 'caught'))
            c.commit()
    return penalty_count(driver, cat)

def find_open_assignment(cur, driver, category, warehouse=None, team_id=None):
    sql = """
        SELECT * FROM quiz_assignments
        WHERE driver_id=%s AND category_code=%s AND status IN ('pending','in_progress')
    """
    params = [driver, category]
    if warehouse:
        sql += " AND warehouse=%s"
        params.append(warehouse)
    if team_id:
        sql += " AND team_id=%s"
        params.append(team_id)
    sql += " ORDER BY created_at ASC LIMIT 1"
    cur.execute(sql, params)
    return cur.fetchone()

def mark_assignment_started(cur, assignment_id):
    if assignment_id:
        cur.execute("""
            UPDATE quiz_assignments
            SET status='in_progress', attempt_count=attempt_count+1, last_attempt_at=NOW(), updated_at=NOW()
            WHERE id=%s
        """, (assignment_id,))

def mark_assignment_completed(cur, assignment_id, warning=False):
    if assignment_id:
        cur.execute("""
            UPDATE quiz_assignments
            SET status='completed', completed_at=NOW(), warning=%s, updated_at=NOW()
            WHERE id=%s
        """, (warning, assignment_id))

def admin_required():
    return session.get('admin_ok') is True

def question_bank_required():
    return session.get('question_bank_ok') is True


def parse_json_array(text, field_name):
    """Accept either a JSON array or one option per line.
    This keeps older JSON data compatible while making the editor easier for non-technical users.
    """
    text = (text or '').strip()
    if not text:
        return []
    if text.startswith('['):
        try:
            val = json.loads(text)
        except Exception as e:
            raise ValueError(f'{field_name} must be either one item per line or a JSON array. Example: ["√", "✗"]') from e
        if not isinstance(val, list):
            raise ValueError(f'{field_name} must be an array or one item per line.')
        return [norm(x) for x in val if norm(x)]
    # Friendly mode: one item per line. If someone pasted a comma-separated list, support that too.
    raw_items = text.splitlines()
    if len(raw_items) == 1 and ',' in raw_items[0]:
        raw_items = raw_items[0].split(',')
    return [norm(x.strip()) for x in raw_items if norm(x.strip())]

def parse_image_paths(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []

def save_question_images(files, category_code, question_id=None):
    saved = []
    upload_root = os.path.join(app.root_path, 'static', 'quiz_images', category_code or 'misc')
    os.makedirs(upload_root, exist_ok=True)
    for f in files:
        if not f or not getattr(f, 'filename', ''):
            continue
        original = secure_filename(f.filename)
        ext = os.path.splitext(original)[1].lower() or '.jpg'
        if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
            ext = '.jpg'
        name = f"q{question_id or 'new'}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}"
        full_path = os.path.join(upload_root, name)
        f.save(full_path)
        saved.append(f"/static/quiz_images/{category_code or 'misc'}/{name}")
    return saved

def get_sheet():
    global _sheet_cache
    if _sheet_cache is not None:
        return _sheet_cache
    if not GOOGLE_SHEET_URL:
        app.logger.warning('GOOGLE_SHEET_URL is empty. Google Sheet write skipped.')
        return None
    if gspread is None or Credentials is None:
        app.logger.warning('gspread/google-auth is not installed. Google Sheet write skipped.')
        return None
    if not os.path.exists(GOOGLE_KEY_FILE):
        app.logger.warning('Google key file not found: %s. Google Sheet write skipped.', GOOGLE_KEY_FILE)
        return None
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_file(GOOGLE_KEY_FILE, scopes=scope)
    client = gspread.authorize(creds)
    _sheet_cache = client.open_by_url(GOOGLE_SHEET_URL).worksheet(GOOGLE_SHEET_TAB)
    return _sheet_cache

def append_sheet_row(attempt, score, total, passed, penalty_total, warning):
    sheet = get_sheet()
    if sheet is None:
        return False
    row = [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), attempt.get('driver_id') or '', attempt.get('warehouse') or '', attempt.get('team_id') or '', attempt.get('category_code') or '', attempt.get('trigger_reason') or '', attempt.get('quiz_set') if attempt.get('quiz_set') is not None else 'random', score, total, 'YES' if passed else 'NO', penalty_total, 'YES' if warning else 'NO']
    sheet.append_row(row, value_input_option='USER_ENTERED')
    return True

@app.before_request
def once():
    if not getattr(app, 'ready', False):
        init()
        app.ready = True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify(ok=True, time=datetime.now(timezone.utc).isoformat())

@app.route('/api/categories')
def categories():
    lang = request.args.get('language', 'zh')
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT category_code,COUNT(*) question_count,COUNT(DISTINCT quiz_set) set_count FROM quiz_questions WHERE COALESCE(status,'published')='published' GROUP BY category_code")
            rows = cur.fetchall()
    def sort_key(r):
        code = r['category_code']
        return (CATEGORY_ORDER.index(code) if code in CATEGORY_ORDER else 99, code)
    cats=[]
    for r in sorted(rows, key=sort_key):
        code=r['category_code']
        prefix=CATEGORY_PREFIX.get(code,'')
        base=LABELS.get(code, {}).get(lang, code)
        label=(prefix + ' ' + base).strip()
        cats.append({'category_code': code, 'label': label, 'question_count': int(r['question_count']), 'set_count': int(r['set_count'])})
    return jsonify(categories=cats)

@app.route('/api/attempts/start', methods=['POST'])
def start():
    d = request.get_json(force=True, silent=True) or {}
    miss = [k for k in ['driver_id', 'warehouse', 'team_id', 'category_code'] if not str(d.get(k, '')).strip()]
    if miss:
        return jsonify(error='Missing required field: ' + ', '.join(miss)), 400
    requested_stage = max(1, int(d.get('stage') or 1))
    category = d['category_code'].strip()
    driver = d['driver_id'].strip()
    warehouse = d.get('warehouse', '').strip()
    team = d.get('team_id', '').strip()
    reason = d.get('trigger_reason', 'caught')

    with conn() as c:
        with c.cursor() as cur:
            # Resume an unfinished attempt after refresh. This keeps the exact same quiz and question order.
            if requested_stage == 1:
                cur.execute("""
                    SELECT * FROM quiz_attempts
                    WHERE driver_id=%s AND category_code=%s AND warehouse=%s AND team_id=%s AND submitted_at IS NULL
                    ORDER BY started_at DESC LIMIT 1
                """, (driver, category, warehouse, team))
                open_attempt = cur.fetchone()
                if open_attempt and open_attempt.get('question_ids'):
                    ids = open_attempt.get('question_ids') or []
                    qs = questions_by_ids(ids)
                    warn_count = assignment_warning_count(driver, category)
                    return jsonify(attempt_id=open_attempt['id'], assignment_id=open_attempt.get('assignment_id'), stage=open_attempt['stage'], quiz_set=open_attempt['quiz_set'], is_random=bool(open_attempt['is_random']), warning_count=warn_count, show_warning=warn_count >= 3, resumed=True, questions=[qdict(x) for x in qs])
                # If the latest submitted attempt did not pass, continue from the next set.
                # If the latest submitted attempt passed, do not force the driver back into the old flow.
                cur.execute("""
                    SELECT * FROM quiz_attempts
                    WHERE driver_id=%s AND category_code=%s AND warehouse=%s AND team_id=%s AND submitted_at IS NOT NULL
                    ORDER BY submitted_at DESC LIMIT 1
                """, (driver, category, warehouse, team))
                latest_submitted = cur.fetchone()
                if latest_submitted and not latest_submitted.get('passed'):
                    requested_stage = min(int(latest_submitted['stage']) + 1, 6)

            qs = questions(category, requested_stage)
            if not qs:
                return jsonify(error='No questions found for this category/stage'), 404
            qids = [int(q['id']) for q in qs]
            warn_count = assignment_warning_count(driver, category)
            assignment = find_open_assignment(cur, driver, category, warehouse, team)
            assignment_id = None
            if assignment:
                assignment_id = assignment['id']
                mark_assignment_started(cur, assignment_id)
            cur.execute('''INSERT INTO quiz_attempts(driver_id,warehouse,team_id,language,category_code,trigger_reason,stage,quiz_set,is_random,total,warning_count,assignment_id,question_ids) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id''', (driver, warehouse, team, d.get('language', 'zh'), category, reason, requested_stage, None if requested_stage > 5 else requested_stage, requested_stage > 5, len(qs), warn_count, assignment_id, json.dumps(qids)))
            aid = cur.fetchone()['id']
        c.commit()
    return jsonify(attempt_id=aid, assignment_id=assignment_id, stage=requested_stage, quiz_set=None if requested_stage > 5 else requested_stage, is_random=requested_stage > 5, warning_count=warn_count, show_warning=warn_count >= 3, resumed=False, questions=[qdict(x) for x in qs])

@app.route('/api/attempts/<int:aid>/submit', methods=['POST'])
def submit(aid):
    data = request.get_json(force=True, silent=True) or {}
    answers = data.get('answers') or {}
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM quiz_attempts WHERE id=%s', (aid,))
            at = cur.fetchone()
            if not at:
                return jsonify(error='Attempt not found'), 404
            qs = questions_by_ids(at.get('question_ids') or []) or questions(at['category_code'], at['stage'])
            score = 0
            review = []
            cur.execute('DELETE FROM quiz_attempt_answers WHERE attempt_id=%s', (aid,))
            for r in qs:
                sel = arr(answers.get(str(r['id']), []))
                cor = arr(r.get('correct_answers') or [])
                ok = same(sel, cor)
                score += 1 if ok else 0
                cur.execute('INSERT INTO quiz_attempt_answers(attempt_id,question_id,selected_answers,correct_answers,is_correct) VALUES(%s,%s,%s::jsonb,%s::jsonb,%s)', (aid, r['id'], json.dumps(sel, ensure_ascii=False), json.dumps(cor, ensure_ascii=False), ok))
                item = qdict(r, True)
                item.update(selected_answers=sel, is_correct=ok)
                review.append(item)
            total = len(qs)
            passed = score == total
            next_stage = None if passed else int(at['stage']) + 1
            penalty_total = assignment_warning_count(at['driver_id'], at['category_code'])
            warning = penalty_total >= 3
            cur.execute('UPDATE quiz_attempts SET score=%s,total=%s,passed=%s,warning_count=%s,submitted_at=NOW() WHERE id=%s', (score, total, passed, penalty_total, aid))
            if passed:
                mark_assignment_completed(cur, at.get('assignment_id'), warning)
        c.commit()
    sheet_written = False
    sheet_error = ''
    try:
        sheet_written = append_sheet_row(at, score, total, passed, penalty_total, warning)
    except Exception as e:
        app.logger.exception('Failed to write Google Sheet')
        sheet_error = str(e)
    return jsonify(attempt_id=aid, score=score, total=total, passed=passed, next_stage=next_stage, stage=int(at['stage']), quiz_set=at['quiz_set'], is_random=bool(at['is_random']), warning=warning, warning_count=penalty_total, sheet_written=sheet_written, sheet_error=sheet_error, review_items=review)

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    error = ''
    if request.method == 'POST':
        if request.form.get('password','') == ADMIN_PASSWORD:
            session['admin_ok'] = True
            return redirect(url_for('admin_dashboard'))
        error = 'Password is incorrect'
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/questions/login', methods=['GET','POST'])
def question_bank_login():
    error = ''
    if request.method == 'POST':
        if request.form.get('password','') == QUESTION_BANK_PASSWORD:
            session['question_bank_ok'] = True
            if request.form.get('remember') == 'yes':
                session.permanent = True
            return redirect(url_for('admin_questions'))
        error = 'Password is incorrect'
    return render_template('question_bank_login.html', error=error)

@app.route('/admin/questions/logout')
def question_bank_logout():
    session.pop('question_bank_ok', None)
    return redirect(url_for('question_bank_login'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if not admin_required():
        return redirect(url_for('admin_login'))
    warehouse = request.args.get('warehouse','').strip()
    team_id = request.args.get('team_id','').strip()
    category = request.args.get('category','').strip()
    status = request.args.get('status','').strip()
    creator = request.args.get('creator','').strip()
    date_from = request.args.get('date_from','').strip()
    date_to = request.args.get('date_to','').strip()
    status = 'unfinished' if status == 'unfinished' else status
    where = ['1=1']
    params = []
    if warehouse:
        where.append('warehouse=%s')
        params.append(warehouse)
    if team_id:
        where.append('team_id=%s')
        params.append(team_id)
    if category:
        where.append('category_code=%s')
        params.append(category)
    if status == 'unfinished':
        where.append("status IN ('pending','in_progress')")
    elif status:
        where.append('status=%s')
        params.append(status)
    if creator:
        where.append('created_by ILIKE %s')
        params.append('%' + creator + '%')
    if date_from:
        where.append('created_at::date >= %s')
        params.append(date_from)
    if date_to:
        where.append('created_at::date <= %s')
        params.append(date_to)
    where_sql = ' AND '.join(where)

    attempt_where = ['1=1']
    attempt_params = []
    if warehouse:
        attempt_where.append('warehouse=%s')
        attempt_params.append(warehouse)
    if team_id:
        attempt_where.append('team_id=%s')
        attempt_params.append(team_id)
    if category:
        attempt_where.append('category_code=%s')
        attempt_params.append(category)
    if creator:
        attempt_where.append("assignment_id IN (SELECT id FROM quiz_assignments WHERE created_by ILIKE %s)")
        attempt_params.append('%' + creator + '%')
    if date_from:
        attempt_where.append('submitted_at::date >= %s')
        attempt_params.append(date_from)
    if date_to:
        attempt_where.append('submitted_at::date <= %s')
        attempt_params.append(date_to)
    attempt_where_sql = ' AND '.join(attempt_where)

    with conn() as c:
        with c.cursor() as cur:
            cur.execute(f"""SELECT COUNT(*) total,
                COUNT(*) FILTER (WHERE status='completed') completed,
                COUNT(*) FILTER (WHERE status='pending') pending,
                COUNT(*) FILTER (WHERE status='in_progress') in_progress,
                COUNT(*) FILTER (WHERE warning=true) warnings
                FROM quiz_assignments WHERE {where_sql}""", params)
            stats = cur.fetchone()
            cur.execute(f"""SELECT * FROM quiz_assignments WHERE {where_sql}
                ORDER BY created_at DESC LIMIT 500""", params)
            assignments = cur.fetchall()
            cur.execute("SELECT DISTINCT created_by FROM quiz_assignments WHERE COALESCE(created_by,'')<>'' ORDER BY created_by")
            creators = [r['created_by'] for r in cur.fetchall()]
            cur.execute(f"""SELECT COUNT(*) total_submissions,
                COUNT(DISTINCT driver_id) drivers,
                COUNT(*) FILTER (WHERE passed=true) passed_count,
                COUNT(*) FILTER (WHERE passed=false) failed_count,
                COUNT(*) FILTER (WHERE warning_count>=3) warning_results
                FROM quiz_attempts WHERE submitted_at IS NOT NULL AND {attempt_where_sql}""", attempt_params)
            attempt_stats = cur.fetchone()
            cur.execute(f"""SELECT id, driver_id, warehouse, team_id, language, category_code, trigger_reason,
                       stage, quiz_set, is_random, score, total, passed, warning_count,
                       started_at, submitted_at, assignment_id
                FROM quiz_attempts
                WHERE submitted_at IS NOT NULL AND {attempt_where_sql}
                ORDER BY submitted_at DESC
                LIMIT 500""", attempt_params)
            attempts = cur.fetchall()
    return render_template('admin_dashboard.html', stats=stats, attempt_stats=attempt_stats, assignments=assignments, attempts=attempts, warehouses=WAREHOUSES, teams=TEAM_IDS, labels=LABELS, filters={'warehouse':warehouse,'team_id':team_id,'category':category,'status':status,'creator':creator,'date_from':date_from,'date_to':date_to}, creators=creators)

@app.route('/admin/assignments/new', methods=['GET','POST'])
def admin_new_assignment():
    if not admin_required():
        return redirect(url_for('admin_login'))
    error = ''
    if request.method == 'POST':
        raw_driver_ids = request.form.get('driver_ids','') or request.form.get('driver_id','')
        warehouse = request.form.get('warehouse','').strip()
        team_id = request.form.get('team_id','').strip()
        category_code = request.form.get('category_code','').strip()
        creator = request.form.get('created_by','').strip() or 'admin'
        note = request.form.get('note','').strip()
        pieces = raw_driver_ids.replace(',', '\n').replace(';', '\n').splitlines()
        driver_ids = []
        seen = set()
        for x in pieces:
            x = x.strip()
            if x and x not in seen:
                driver_ids.append(x)
                seen.add(x)
        if not all([driver_ids, warehouse, team_id, category_code, creator]):
            error = 'Creator, at least one Driver ID, warehouse, team ID, and category are required.'
        else:
            with conn() as c:
                with c.cursor() as cur:
                    for driver_id in driver_ids:
                        cur.execute("SELECT COUNT(*) cnt FROM quiz_assignments WHERE driver_id=%s AND category_code=%s", (driver_id, category_code))
                        will_warn = int(cur.fetchone()['cnt']) + 1 >= 3
                        cur.execute("""INSERT INTO quiz_assignments(driver_id,warehouse,team_id,category_code,created_by,note,status,warning)
                            VALUES(%s,%s,%s,%s,%s,%s,'pending',%s)""", (driver_id, warehouse, team_id, category_code, creator, note, will_warn))
                c.commit()
            return redirect(url_for('admin_dashboard', creator=creator, warehouse=warehouse, team_id=team_id, category=category_code, status='unfinished'))
    return render_template('admin_assignment_new.html', labels=LABELS, error=error, warehouses=WAREHOUSES, teams=TEAM_IDS)



def get_question_admin_meta():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT category_code FROM quiz_questions ORDER BY category_code")
            categories = [r['category_code'] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT quiz_set FROM quiz_questions ORDER BY quiz_set")
            quiz_sets = [r['quiz_set'] for r in cur.fetchall()]
    return categories, quiz_sets

def next_question_order(category_code, quiz_set):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(question_order),0)+1 AS n FROM quiz_questions WHERE category_code=%s AND quiz_set=%s", (category_code, quiz_set))
            return int(cur.fetchone()['n'])

@app.route('/admin/questions')
def admin_questions():
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    category = request.args.get('category','').strip()
    quiz_set = request.args.get('quiz_set','').strip()
    qtype = request.args.get('question_type','').strip()
    status = request.args.get('status','').strip()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT category_code, COUNT(*) total, COUNT(DISTINCT quiz_set) set_count
                FROM quiz_questions
                GROUP BY category_code
            """)
            category_rows = cur.fetchall()
            def sort_key(r):
                code = r['category_code']
                return (CATEGORY_ORDER.index(code) if code in CATEGORY_ORDER else 99, code)
            category_rows = sorted(category_rows, key=sort_key)
            if not category and category_rows:
                category = category_rows[0]['category_code']
            cur.execute("""
                SELECT quiz_set, COUNT(*) total,
                       COUNT(*) FILTER (WHERE COALESCE(status,'published')='draft') draft_count,
                       COUNT(*) FILTER (WHERE COALESCE(status,'published')='published') published_count,
                       COUNT(*) FILTER (WHERE COALESCE(status,'published')='inactive') inactive_count
                FROM quiz_questions
                WHERE category_code=%s
                GROUP BY quiz_set
                ORDER BY quiz_set
            """, (category,))
            set_rows = cur.fetchall()
            if not quiz_set and set_rows:
                quiz_set = str(set_rows[0]['quiz_set'])

    where = ['category_code=%s']
    params = [category]
    if quiz_set:
        where.append('quiz_set=%s')
        params.append(int(quiz_set))
    if qtype:
        where.append('question_type=%s')
        params.append(qtype)
    if status:
        where.append("COALESCE(status,'published')=%s")
        params.append(status)
    sql = f"""
        SELECT id, category_code, quiz_set, question_order, question_type,
               question_zh, question_en, question_es, options, correct_answers,
               explanation, image_paths, COALESCE(image_layout,'horizontal') AS image_layout, COALESCE(status,'published') AS status,
               created_at, updated_at
        FROM quiz_questions
        WHERE {' AND '.join(where)}
        ORDER BY id
        LIMIT 1000
    """
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    categories, all_quiz_sets = get_question_admin_meta()
    return render_template('admin_questions.html', rows=rows, labels=LABELS,
                           category_rows=category_rows, set_rows=set_rows,
                           categories=categories, quiz_sets=all_quiz_sets,
                           filters={'category':category,'quiz_set':quiz_set,'question_type':qtype,'status':status})

@app.route('/admin/questions/publish', methods=['POST'])
def admin_questions_publish():
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    category_code = request.form.get('category_code','').strip()
    quiz_set = request.form.get('quiz_set','').strip()
    if category_code and quiz_set:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute("""
                    UPDATE quiz_questions
                    SET status='published', updated_at=NOW()
                    WHERE category_code=%s AND quiz_set=%s AND COALESCE(status,'published') IN ('draft','inactive','published')
                """, (category_code, int(quiz_set)))
            c.commit()
    return redirect(url_for('admin_questions', category=category_code, quiz_set=quiz_set))

@app.route('/admin/questions/<int:question_id>/set-status', methods=['POST'])
def admin_question_set_status(question_id):
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    status = request.form.get('status','draft').strip()
    if status not in ['draft','published','inactive']:
        status = 'draft'
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE quiz_questions SET status=%s, updated_at=NOW() WHERE id=%s", (status, question_id))
        c.commit()
    return redirect(request.referrer or url_for('admin_questions'))

@app.route('/admin/questions/new', methods=['GET','POST'])
def admin_question_new():
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    error = ''
    form = {}
    categories, quiz_sets = get_question_admin_meta()
    if request.method == 'POST':
        form = request.form.to_dict()
        try:
            category_code = request.form.get('category_code','').strip()
            quiz_set = int(request.form.get('quiz_set','0'))
            question_type = request.form.get('question_type','single').strip()
            question_zh = request.form.get('question_zh','').strip()
            question_en = request.form.get('question_en','').strip()
            question_es = request.form.get('question_es','').strip()
            explanation = request.form.get('explanation','').strip()
            status = request.form.get('status','draft').strip()
            image_layout = request.form.get('image_layout','horizontal').strip()
            if image_layout not in ['horizontal','vertical']:
                image_layout = 'horizontal'
            if status not in ['draft','published','inactive']:
                status = 'draft'
            options = parse_json_array(request.form.get('options','[]'), 'Options')
            correct_answers = parse_json_array(request.form.get('correct_answers','[]'), 'Correct answers')
            if not category_code or quiz_set < 1 or question_type not in ['single','multi'] or not question_zh or not options or not correct_answers:
                raise ValueError('Category, quiz set, type, Chinese question, options, and correct answers are required.')
            question_order = next_question_order(category_code, quiz_set)
            image_paths = save_question_images(request.files.getlist('images'), category_code)
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO quiz_questions(category_code, quiz_set, question_order, question_type,
                            question_zh, question_en, question_es, options, correct_answers, explanation,
                            image_urls, image_paths, status, image_layout, created_at, updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,'[]'::jsonb,%s::jsonb,%s,%s,NOW(),NOW())
                        RETURNING id
                    """, (category_code, quiz_set, question_order, question_type, question_zh, question_en, question_es,
                          json.dumps(options, ensure_ascii=False), json.dumps(correct_answers, ensure_ascii=False),
                          explanation, json.dumps(image_paths, ensure_ascii=False), status, image_layout))
                    new_id = cur.fetchone()['id']
                c.commit()
            return redirect(url_for('admin_question_edit', question_id=new_id))
        except Exception as e:
            error = str(e)
    defaults = {
        'question_zh':'请判断这张POD是否合格',
        'question_en':'Is this POD acceptable?',
        'question_es':'¿Este POD es aceptable?',
        'options':'√\n✗',
        'correct_answers':'✗',
        'status':'draft',
        'quiz_set':request.args.get('quiz_set') or 1,
        'category_code':request.args.get('category') or '',
        'question_type':'single',
        'image_layout':'horizontal'
    }
    defaults.update(form)
    return render_template('admin_question_form.html', mode='new', q=defaults, labels=LABELS,
                           categories=categories, quiz_sets=quiz_sets, error=error, image_paths=[])

@app.route('/admin/questions/<int:question_id>/edit', methods=['GET','POST'])
def admin_question_edit(question_id):
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    error = ''
    categories, quiz_sets = get_question_admin_meta()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM quiz_questions WHERE id=%s', (question_id,))
            q = cur.fetchone()
    if not q:
        return 'Question not found', 404
    image_paths = parse_image_paths(q.get('image_paths'))
    if request.method == 'POST':
        try:
            category_code = request.form.get('category_code','').strip()
            quiz_set = int(request.form.get('quiz_set','0'))
            question_type = request.form.get('question_type','single').strip()
            question_zh = request.form.get('question_zh','').strip()
            question_en = request.form.get('question_en','').strip()
            question_es = request.form.get('question_es','').strip()
            explanation = request.form.get('explanation','').strip()
            status = request.form.get('status','draft').strip()
            image_layout = request.form.get('image_layout','horizontal').strip()
            if image_layout not in ['horizontal','vertical']:
                image_layout = 'horizontal'
            if status not in ['draft','published','inactive']:
                status = 'draft'
            options = parse_json_array(request.form.get('options','[]'), 'Options')
            correct_answers = parse_json_array(request.form.get('correct_answers','[]'), 'Correct answers')
            if not category_code or quiz_set < 1 or question_type not in ['single','multi'] or not question_zh or not options or not correct_answers:
                raise ValueError('Category, quiz set, type, Chinese question, options, and correct answers are required.')
            remove_images = set(request.form.getlist('remove_images'))
            if request.form.get('replace_images') == 'yes':
                new_paths = []
            else:
                new_paths = [p for p in image_paths if p not in remove_images]
            new_paths.extend(save_question_images(request.files.getlist('images'), category_code, question_id))
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute("""
                        UPDATE quiz_questions
                        SET category_code=%s, quiz_set=%s, question_type=%s,
                            question_zh=%s, question_en=%s, question_es=%s,
                            options=%s::jsonb, correct_answers=%s::jsonb,
                            explanation=%s, image_paths=%s::jsonb, status=%s, image_layout=%s, updated_at=NOW()
                        WHERE id=%s
                    """, (category_code, quiz_set, question_type, question_zh, question_en, question_es,
                          json.dumps(options, ensure_ascii=False), json.dumps(correct_answers, ensure_ascii=False),
                          explanation, json.dumps(new_paths, ensure_ascii=False), status, image_layout, question_id))
                c.commit()
            return redirect(url_for('admin_question_edit', question_id=question_id))
        except Exception as e:
            error = str(e)
            q = dict(q)
            q.update(request.form.to_dict())
            image_paths = [p for p in image_paths if p not in set(request.form.getlist('remove_images'))]
    q = dict(q)
    q['options_text'] = '\n'.join(arr(q.get('options') or []))
    q['correct_answers_text'] = '\n'.join(arr(q.get('correct_answers') or []))
    q['status'] = q.get('status') or 'published'
    return render_template('admin_question_form.html', mode='edit', q=q, labels=LABELS,
                           categories=categories, quiz_sets=quiz_sets, error=error, image_paths=image_paths)


@app.route('/admin/questions/<int:question_id>/preview')
def admin_question_preview(question_id):
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM quiz_questions WHERE id=%s', (question_id,))
            q = cur.fetchone()
    if not q:
        return 'Question not found', 404
    return render_template('admin_question_preview.html', q=qdict(q, True), raw=q, labels=LABELS)

@app.route('/admin/questions/<int:question_id>/delete', methods=['POST'])
def admin_question_delete(question_id):
    if not question_bank_required():
        return redirect(url_for('question_bank_login'))
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('DELETE FROM quiz_questions WHERE id=%s', (question_id,))
        c.commit()
    return redirect(url_for('admin_questions'))

if __name__ == '__main__':
    init()
    app.run(host=os.getenv('APP_HOST', '0.0.0.0'), port=int(os.getenv('APP_PORT', '5757')), debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')
