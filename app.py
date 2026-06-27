# -*- coding: utf-8 -*-
"""
ChomikDownloader web UI – same look & feel as chomik-uploader.
Accepts folder URL from chomikuj.pl and downloads files to a folder.
"""
import os
import re
import json
import hashlib
import hmac
import threading
import uuid
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, unquote_plus

from flask import Flask, request, redirect, render_template_string, Response, session

# Import downloader (ensure chomik.py is in path)
from chomik import ChomikDownloader

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-change-me')

# Password security (optional – protects the web panel)
PASSWORD_HASH = os.environ.get('PANEL_PASSWORD_HASH', '')
PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '')
if PANEL_PASSWORD and not PASSWORD_HASH:
    PASSWORD_HASH = hashlib.sha256(PANEL_PASSWORD.encode()).hexdigest()

# Chomikuj credentials (from env, like uploader)
CHOMIK_USER = os.environ.get('CHOMIK_USERNAME', '')
CHOMIK_PASS = os.environ.get('CHOMIK_PASSWORD', '')
DOWNLOAD_BASE = os.environ.get('DOWNLOAD_FOLDER', '/app/downloads')

# Where job history is persisted (survives server restart). Lives under the
# downloads bind mount by default so it persists across container recreation.
STATE_FILE = os.environ.get('STATE_FILE', os.path.join(DOWNLOAD_BASE, 'jobs_state.json'))
# Max retained jobs; oldest *done* jobs are pruned beyond this.
MAX_JOBS = int(os.environ.get('MAX_JOBS', '50'))

# Per-job status: job_id -> {
#   'job_id', 'label', 'url', 'dest_folder', 'options': {recursive,structure,overwrite},
#   'created_at', 'files': [ {name, size, status, message, percent}, ... ],
#   'done': bool, 'error': str|None, 'interrupted': bool }
download_status = {}
download_lock = threading.Lock()

_state_initialized = False


# ---------- Persistence (single process; all access under download_lock) ----------

def prune_jobs():
    """Drop oldest *done* jobs beyond MAX_JOBS. Never prunes a running job.
    Must be called while holding download_lock."""
    if len(download_status) <= MAX_JOBS:
        return
    done_ids = [jid for jid, st in download_status.items() if st.get('done')]
    done_ids.sort(key=lambda jid: download_status[jid].get('created_at') or '')
    while len(download_status) > MAX_JOBS and done_ids:
        del download_status[done_ids.pop(0)]


def persist_state():
    """Atomically write a snapshot of all jobs to STATE_FILE.
    Must be called while holding download_lock. Never raises — a failed
    snapshot must not crash an in-flight download."""
    try:
        prune_jobs()
        payload = json.dumps({'version': 1, 'jobs': download_status})
        tmp = STATE_FILE + '.tmp'
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        app.logger.warning('Nie udało się zapisać stanu zadań: %s', e)


def load_state():
    """Read STATE_FILE -> dict of job_id -> job. Tolerates a missing or
    corrupt file (returns {} and quarantines the bad file)."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        jobs = data.get('jobs', {})
        if isinstance(jobs, dict):
            return jobs
        app.logger.warning('jobs_state.json ma nieoczekiwany format; ignoruję')
        return {}
    except Exception as e:
        app.logger.warning('Uszkodzony plik stanu (%s); przenoszę do .corrupt', e)
        try:
            os.replace(STATE_FILE, STATE_FILE + '.corrupt')
        except Exception:
            pass
        return {}


def init_state():
    """Load persisted jobs and reconcile any that were running when the server
    stopped: the daemon thread is gone, so mark them interrupted. Idempotent."""
    global _state_initialized
    with download_lock:
        if _state_initialized:
            return
        jobs = load_state()
        download_status.clear()
        download_status.update(jobs)
        for st in download_status.values():
            if not st.get('done'):
                st['done'] = True
                st['interrupted'] = True
                if not st.get('error'):
                    st['error'] = u'Pobieranie przerwane (restart serwera)'
        _state_initialized = True
        persist_state()


def derive_label(url):
    """Human label from a chomikuj URL: last 1-2 path segments, URL-decoded."""
    try:
        path = urlparse(url).path
        parts = [unquote_plus(p) for p in path.split('/') if p]
        if parts:
            return '/'.join(parts[-2:])
    except Exception:
        pass
    return url


def verify_password(password):
    if not PASSWORD_HASH:
        return True  # no password set = no auth
    h = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(h, PASSWORD_HASH)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if PASSWORD_HASH and session.get('logged_in') != True:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


def json_response(data, status_code=200):
    return Response(json.dumps(data), mimetype='application/json', status=status_code)


def normalize_chomik_url(url):
    s = (url or '').strip()
    if not s:
        return None
    parsed = urlparse(s)
    if not parsed.netloc:
        s = 'https://chomikuj.pl' + (s if s.startswith('/') else '/' + s)
        parsed = urlparse(s)
    if parsed.netloc and 'chomikuj.pl' in parsed.netloc:
        path = (parsed.path or '/').strip('/')
        return 'https://chomikuj.pl/' + path
    return None


# ---------- HTML (same style as chomik-uploader) ----------

HTML_LOGIN = u"""
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ChomikDownloader - Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-container {
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
        }
        h1 { text-align: center; color: #333; margin: 0 0 30px 0; font-size: 28px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; color: #555; font-weight: bold; }
        input[type="password"] {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
            box-sizing: border-box;
        }
        input[type="password"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }
        button:hover { transform: translateY(-2px); }
        .error {
            background: #fee;
            color: #c33;
            padding: 12px;
            border-radius: 4px;
            margin-bottom: 20px;
            border-left: 4px solid #c33;
        }
        .info { text-align: center; color: #999; font-size: 14px; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>ChomikDownloader</h1>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <div class="form-group">
                <label for="password">Hasło:</label>
                <input type="password" id="password" name="password" required autofocus>
            </div>
            <button type="submit">Zaloguj się</button>
            <div class="info">Wpisz hasło panelu, aby uzyskać dostęp</div>
        </form>
    </div>
</body>
</html>
"""

HTML_FORM = u"""
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ChomikDownloader</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        h1 { margin: 0; color: #333; }
        .logout-btn {
            background: #dc3545;
            color: white;
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
        }
        .logout-btn:hover { background: #c82333; }
        .container {
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h2 { color: #555; margin-top: 30px; margin-bottom: 15px; font-size: 18px; }
        .info-box {
            background: #e3f2fd;
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 20px;
            border-left: 4px solid #2196f3;
        }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 6px; color: #555; font-weight: bold; }
        input[type="text"], input[type="url"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            box-sizing: border-box;
        }
        input:focus {
            outline: none;
            border-color: #007bff;
        }
        .checkbox-row { display: flex; align-items: center; gap: 8px; margin: 10px 0; }
        .checkbox-row input { width: auto; }
        button {
            background: #007bff;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
            margin: 5px 5px 5px 0;
        }
        button:hover { background: #0056b3; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        .file-list { margin-top: 30px; }
        .file-item {
            background: #f9f9f9;
            padding: 15px;
            margin: 10px 0;
            border-radius: 4px;
            border-left: 4px solid #007bff;
        }
        .file-item.success { border-left-color: #28a745; }
        .file-item.error { border-left-color: #dc3545; }
        .file-item.pending { border-left-color: #ffc107; }
        .file-name { font-weight: bold; margin-bottom: 8px; word-break: break-all; }
        .file-size { font-size: 12px; color: #999; margin-bottom: 8px; }
        .progress-bar {
            width: 100%;
            height: 25px;
            background: #e0e0e0;
            border-radius: 4px;
            overflow: hidden;
            margin: 8px 0;
        }
        .progress-fill {
            height: 100%;
            background: #28a745;
            width: 0%;
            transition: width 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 12px;
            font-weight: bold;
        }
        .status-text { font-size: 14px; color: #666; margin-top: 5px; }
        .status-success { color: #28a745; }
        .status-error { color: #dc3545; }
        .status-pending { color: #ffc107; }
        .job-card {
            background: #fff;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 15px;
            margin: 15px 0;
            box-shadow: 0 1px 4px rgba(0,0,0,0.06);
        }
        .job-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }
        .job-title { font-weight: bold; color: #333; word-break: break-all; }
        .job-meta { font-size: 12px; color: #999; margin-top: 2px; }
        .job-head-left { min-width: 0; }
        .job-head-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
        .status-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            white-space: nowrap;
        }
        .status-badge.running { background: #e3f2fd; color: #1565c0; }
        .status-badge.done { background: #d4edda; color: #155724; }
        .status-badge.error { background: #f8d7da; color: #721c24; }
        .status-badge.interrupted { background: #fff3cd; color: #856404; }
        .rerun-btn {
            background: #6c757d;
            color: white;
            padding: 6px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            margin: 0;
        }
        .rerun-btn:hover { background: #5a6268; }
        .job-error { font-size: 13px; color: #721c24; margin-top: 6px; }
        .messages { margin: 20px 0; }
        .alert {
            padding: 12px;
            margin: 10px 0;
            border-radius: 4px;
            border-left: 4px solid #ffc107;
            background: #fff3cd;
            color: #856404;
        }
        .alert.success { border-left-color: #28a745; background: #d4edda; color: #155724; }
        .alert.error { border-left-color: #dc3545; background: #f8d7da; color: #721c24; }
        #statusList { margin-top: 15px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Pobierz pliki z Chomika</h1>
        <a href="/logout" class="logout-btn">Wyloguj się</a>
    </div>
    <div class="container">
        <div class="info-box">
            <strong>ℹ️ Informacja:</strong> Wklej adres URL folderu z chomikuj.pl (np. https://chomikuj.pl/Użytkownik/Nazwa+Folderu).
            Pliki zostaną pobrane do wybranego folderu na serwerze.
        </div>
        <div id="messages" class="messages"></div>
        <h2>Ustawienia pobierania</h2>
        <form id="downloadForm" onsubmit="startDownload(event)">
            <div class="form-group">
                <label for="folderUrl">URL folderu na chomikuj.pl:</label>
                <input type="url" id="folderUrl" name="folderUrl" placeholder="https://chomikuj.pl/User/Folder" required>
            </div>
            <div class="form-group">
                <label for="destFolder">Podfolder docelowy (opcjonalnie, względem folderu pobierania):</label>
                <input type="text" id="destFolder" name="destFolder" placeholder="np. moje_pliki">
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="recursive" name="recursive" checked>
                <label for="recursive" style="margin:0;">Pobieraj rekursywnie (podfoldery)</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="structure" name="structure" checked>
                <label for="structure" style="margin:0;">Zachowaj strukturę folderów</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="overwrite" name="overwrite">
                <label for="overwrite" style="margin:0;">Nadpisz istniejące pliki</label>
            </div>
            <button type="submit" id="startBtn">Rozpocznij pobieranie</button>
        </form>
        <div class="file-list">
            <h2>Zadania pobierania</h2>
            <div id="statusList"></div>
        </div>
    </div>
    <script>
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        function showMessage(msg, type) {
            const el = document.createElement('div');
            el.className = 'alert' + (type === 'success' ? ' success' : type === 'error' ? ' error' : '');
            el.textContent = msg;
            document.getElementById('messages').appendChild(el);
        }
        function slug(name) { return name.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 40); }
        function fileElId(jobId, name) { return 'j-' + jobId + '-s-' + slug(name); }
        function fmtSize(size) {
            return size >= 1024*1024 ? (size/1024/1024).toFixed(2) + ' MB' : (size/1024).toFixed(2) + ' KB';
        }
        function fmtDate(iso) {
            if (!iso) return '';
            return String(iso).replace('T', ' ').slice(0, 19);
        }
        function jobState(job) {
            if (!job.done) return { cls: 'running', text: 'W toku' };
            if (job.interrupted) return { cls: 'interrupted', text: 'Przerwane' };
            if (job.error) return { cls: 'error', text: 'Błąd' };
            return { cls: 'done', text: 'Zakończone' };
        }
        function renderJobCard(job, prepend) {
            const jobId = job.job_id;
            if (!jobId) return null;
            let card = document.getElementById('job-' + jobId);
            if (!card) {
                card = document.createElement('div');
                card.className = 'job-card';
                card.id = 'job-' + jobId;
                card.innerHTML =
                    '<div class="job-header">' +
                        '<div class="job-head-left">' +
                            '<div class="job-title">' + escapeHtml(job.label || job.url || jobId) + '</div>' +
                            '<div class="job-meta" id="meta-' + jobId + '"></div>' +
                        '</div>' +
                        '<div class="job-head-right">' +
                            '<span class="status-badge" id="badge-' + jobId + '"></span>' +
                            '<button class="rerun-btn" id="rerun-' + jobId + '" style="display:none;">Uruchom ponownie</button>' +
                        '</div>' +
                    '</div>' +
                    '<div class="job-error" id="err-' + jobId + '" style="display:none;"></div>' +
                    '<div class="job-files" id="files-' + jobId + '"></div>';
                const list = document.getElementById('statusList');
                if (prepend && list.firstChild) list.insertBefore(card, list.firstChild);
                else list.appendChild(card);
                document.getElementById('rerun-' + jobId).addEventListener('click', function () {
                    rerunJob(card._job || job);
                });
            }
            card._job = job;
            const meta = document.getElementById('meta-' + jobId);
            if (meta) {
                let m = fmtDate(job.created_at);
                if (job.dest_folder) m += (m ? ' • ' : '') + 'do: ' + escapeHtml(job.dest_folder);
                meta.innerHTML = m;
            }
            const st = jobState(job);
            const badge = document.getElementById('badge-' + jobId);
            if (badge) { badge.className = 'status-badge ' + st.cls; badge.textContent = st.text; }
            const rerun = document.getElementById('rerun-' + jobId);
            if (rerun) rerun.style.display = job.done ? 'inline-block' : 'none';
            const err = document.getElementById('err-' + jobId);
            if (err) {
                if (job.error) { err.style.display = 'block'; err.textContent = job.error; }
                else err.style.display = 'none';
            }
            return card;
        }
        function addFileStatus(jobId, name, size) {
            const id = fileElId(jobId, name);
            if (document.getElementById(id)) return;
            const div = document.createElement('div');
            div.className = 'file-item pending';
            div.id = id;
            div.innerHTML = '<div class="file-name">' + escapeHtml(name) + '</div>' +
                '<div class="file-size">Rozmiar: ' + fmtSize(size) + '</div>' +
                '<div class="progress-bar"><div class="progress-fill" id="' + id + '-p">0%</div></div>' +
                '<div class="status-text status-pending" id="' + id + '-t">Oczekiwanie...</div>';
            const container = document.getElementById('files-' + jobId);
            if (container) container.appendChild(div);
        }
        function updateFileStatus(jobId, name, status, message, percent) {
            const id = fileElId(jobId, name);
            const el = document.getElementById(id);
            if (!el) return;
            const p = document.getElementById(id + '-p');
            const t = document.getElementById(id + '-t');
            if (p) { p.style.width = (percent != null ? percent : 0) + '%'; p.textContent = (percent != null ? percent + '%' : '0%'); }
            if (t) t.textContent = message;
            el.className = 'file-item ' + (status === 'success' ? 'success' : status === 'error' ? 'error' : 'pending');
            if (t) t.className = 'status-text ' + (status === 'success' ? 'status-success' : status === 'error' ? 'status-error' : 'status-pending');
        }
        function applyJobFiles(job) {
            if (!job.files) return;
            job.files.forEach(function (f) {
                addFileStatus(job.job_id, f.name, f.size || 0);
                updateFileStatus(job.job_id, f.name, f.status, f.message || '', f.percent != null ? f.percent : 0);
            });
        }
        function postDownload(payload) {
            document.getElementById('startBtn').disabled = true;
            return fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                document.getElementById('startBtn').disabled = false;
                if (data.error) { showMessage(data.error, 'error'); return; }
                showMessage('Pobieranie rozpoczęte.', 'success');
                renderJobCard({
                    job_id: data.job_id, label: payload.url, url: payload.url,
                    dest_folder: payload.destFolder || '', created_at: null,
                    files: [], done: false, error: null, interrupted: false
                }, true);
                pollStatus(data.job_id);
            })
            .catch(err => {
                document.getElementById('startBtn').disabled = false;
                showMessage('Błąd: ' + err.message, 'error');
            });
        }
        function startDownload(e) {
            e.preventDefault();
            const url = document.getElementById('folderUrl').value.trim();
            const dest = document.getElementById('destFolder').value.trim();
            document.getElementById('messages').innerHTML = '';
            postDownload({
                url: url,
                destFolder: dest || undefined,
                recursive: document.getElementById('recursive').checked,
                structure: document.getElementById('structure').checked,
                overwrite: document.getElementById('overwrite').checked
            });
        }
        function rerunJob(job) {
            const opts = job.options || {};
            document.getElementById('messages').innerHTML = '';
            postDownload({
                url: job.url,
                destFolder: job.dest_folder || undefined,
                recursive: opts.recursive !== false,
                structure: opts.structure !== false,
                overwrite: !!opts.overwrite
            });
        }
        function pollStatus(jobId) {
            fetch('/api/status/' + jobId)
            .then(r => r.json())
            .then(data => {
                data.job_id = data.job_id || jobId;
                renderJobCard(data);
                applyJobFiles(data);
                if (data.done) return;
                setTimeout(function () { pollStatus(jobId); }, 1500);
            })
            .catch(function () {
                setTimeout(function () { pollStatus(jobId); }, 2000);
            });
        }
        function loadJobs() {
            fetch('/api/jobs')
            .then(r => r.json())
            .then(data => {
                (data.jobs || []).forEach(function (job) {
                    renderJobCard(job);
                    applyJobFiles(job);
                    if (!job.done) pollStatus(job.job_id);
                });
            })
            .catch(function () {});
        }
        document.addEventListener('DOMContentLoaded', loadJobs);
    </script>
</body>
</html>
"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not PASSWORD_HASH:
        session['logged_in'] = True
        return redirect('/')
    error = None
    if request.method == 'POST':
        if verify_password(request.form.get('password', '')):
            session['logged_in'] = True
            return redirect('/')
        error = u'Błędne hasło'
    return render_template_string(HTML_LOGIN, error=error)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')


@app.route('/')
@login_required
def index():
    return render_template_string(HTML_FORM)


@app.route('/api/download', methods=['POST'])
@login_required
def api_download():
    try:
        data = request.get_json(force=True, silent=True) or {}
        url = normalize_chomik_url((data.get('url') or '').strip())
        if not url:
            return json_response({'error': 'Podaj prawidłowy URL folderu z chomikuj.pl'}, 200)
        if not CHOMIK_USER or not CHOMIK_PASS:
            return json_response({'error': 'Brak konfiguracji CHOMIK_USERNAME lub CHOMIK_PASSWORD w środowisku'}, 200)

        dest_folder = (data.get('destFolder') or '').strip() or ''
        dest_path = os.path.join(DOWNLOAD_BASE, dest_folder) if dest_folder else DOWNLOAD_BASE
        try:
            os.makedirs(dest_path, exist_ok=True)
        except Exception as e:
            return json_response({'error': 'Nie można utworzyć folderu: ' + str(e)}, 200)

        recursive = bool(data.get('recursive', True))
        structure = bool(data.get('structure', True))
        overwrite = bool(data.get('overwrite', False))

        job_id = str(uuid.uuid4())
        with download_lock:
            download_status[job_id] = {
                'job_id': job_id,
                'label': derive_label(url),
                'url': url,
                'dest_folder': dest_folder,
                'options': {'recursive': recursive, 'structure': structure, 'overwrite': overwrite},
                'created_at': datetime.now().isoformat(),
                'files': [],
                'done': False,
                'error': None,
                'interrupted': False,
            }
            persist_state()

        def on_files_listed(files):
            with download_lock:
                st = download_status.get(job_id)
                if st and not st['done']:
                    st['files'] = [{'name': f['name'], 'size': f['size'], 'status': 'pending', 'message': 'Oczekiwanie...', 'percent': 0} for f in files]
                    persist_state()

        def progress_callback(name, status, message, percent):
            with download_lock:
                st = download_status.get(job_id)
                if not st or st['done']:
                    return
                for f in st['files']:
                    if f['name'] == name:
                        changed = f['status'] != status
                        f['status'] = status
                        f['message'] = message
                        f['percent'] = percent if percent is not None else f.get('percent', 0)
                        # Persist only on terminal transitions, never per-chunk 'downloading' ticks.
                        if changed and status in ('success', 'error'):
                            persist_state()
                        break

        class Args(object):
            pass
        args = Args()
        args.user = CHOMIK_USER
        args.password = CHOMIK_PASS
        args.hash = None
        args.url = url
        args.recursive = recursive
        args.structure = structure
        args.overwrite = overwrite
        args.noprogress = True
        args.ext = None
        args.max_limit = None
        args.progress_callback = progress_callback
        args.on_files_listed = on_files_listed

        def run():
            try:
                d = ChomikDownloader(args)
                d.download_files([url], dest_path)
            except Exception as e:
                with download_lock:
                    st = download_status.get(job_id)
                    if st:
                        st['error'] = str(e)
                        st['done'] = True
                        persist_state()
            with download_lock:
                st = download_status.get(job_id)
                if st:
                    st['done'] = True
                    persist_state()

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        return json_response({'job_id': job_id})
    except Exception as e:
        app.logger.exception('download error')
        return json_response({'error': str(e)}, 200)


@app.route('/api/status/<job_id>')
@login_required
def api_status(job_id):
    with download_lock:
        st = download_status.get(job_id)
    if not st:
        return json_response({'files': [], 'done': True, 'error': 'Nieznane zadanie'})
    return json_response(st)


@app.route('/api/jobs')
@login_required
def api_jobs():
    with download_lock:
        jobs = list(download_status.values())
    jobs.sort(key=lambda st: st.get('created_at') or '', reverse=True)
    return json_response({'jobs': jobs})


# Reconcile persisted jobs at import time so any WSGI entrypoint sees them too
# (idempotent — guarded by _state_initialized).
init_state()


if __name__ == '__main__':
    os.makedirs(DOWNLOAD_BASE, exist_ok=True)
    init_state()
    app.run(host='0.0.0.0', port=5000)
