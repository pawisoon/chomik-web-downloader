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
from functools import wraps
from urllib.parse import urlparse

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

# Per-job status: job_id -> { 'files': [ {name, status, message, percent}, ... ], 'done': bool, 'error': str }
download_status = {}
download_lock = threading.Lock()


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
            <h2>Status pobierania</h2>
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
        function addFileStatus(name, size) {
            const sizeStr = size >= 1024*1024 ? (size/1024/1024).toFixed(2) + ' MB' : (size/1024).toFixed(2) + ' KB';
            const id = 's-' + name.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 40);
            const div = document.createElement('div');
            div.className = 'file-item pending';
            div.id = id;
            div.innerHTML = '<div class="file-name">' + escapeHtml(name) + '</div>' +
                '<div class="file-size">Rozmiar: ' + sizeStr + '</div>' +
                '<div class="progress-bar"><div class="progress-fill" id="' + id + '-p">0%</div></div>' +
                '<div class="status-text status-pending" id="' + id + '-t">Oczekiwanie...</div>';
            document.getElementById('statusList').appendChild(div);
        }
        function updateFileStatus(name, status, message, percent) {
            const id = 's-' + name.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 40);
            const el = document.getElementById(id);
            if (!el) return;
            const p = document.getElementById(id + '-p');
            const t = document.getElementById(id + '-t');
            if (p) { p.style.width = (percent != null ? percent : 0) + '%'; p.textContent = (percent != null ? percent + '%' : '0%'); }
            if (t) t.textContent = message;
            el.className = 'file-item ' + (status === 'success' ? 'success' : status === 'error' ? 'error' : 'pending');
            if (t) t.className = 'status-text ' + (status === 'success' ? 'status-success' : status === 'error' ? 'status-error' : 'status-pending');
        }
        function startDownload(e) {
            e.preventDefault();
            const url = document.getElementById('folderUrl').value.trim();
            const dest = document.getElementById('destFolder').value.trim();
            const recursive = document.getElementById('recursive').checked;
            const structure = document.getElementById('structure').checked;
            const overwrite = document.getElementById('overwrite').checked;
            document.getElementById('messages').innerHTML = '';
            document.getElementById('statusList').innerHTML = '';
            document.getElementById('startBtn').disabled = true;
            fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url, destFolder: dest || undefined, recursive, structure, overwrite })
            })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    showMessage(data.error, 'error');
                    document.getElementById('startBtn').disabled = false;
                    return;
                }
                showMessage('Pobieranie rozpoczęte. Odświeżam status...', 'success');
                pollStatus(data.job_id, data.job_id);
            })
            .catch(err => {
                showMessage('Błąd: ' + err.message, 'error');
                document.getElementById('startBtn').disabled = false;
            });
        }
        function pollStatus(jobId, _first) {
            fetch('/api/status/' + jobId)
            .then(r => r.json())
            .then(data => {
                if (data.files && data.files.length) {
                    data.files.forEach(function (f) {
                        let el = document.getElementById('s-' + f.name.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 40));
                        if (!el) addFileStatus(f.name, f.size || 0);
                        updateFileStatus(f.name, f.status, f.message || '', f.percent != null ? f.percent : 0);
                    });
                }
                if (data.done) {
                    document.getElementById('startBtn').disabled = false;
                    if (data.error) showMessage(data.error, 'error');
                    else showMessage('Pobieranie zakończone.', 'success');
                    return;
                }
                setTimeout(function() { pollStatus(jobId); }, 1500);
            })
            .catch(function() {
                setTimeout(function() { pollStatus(jobId); }, 2000);
            });
        }
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

        job_id = str(uuid.uuid4())
        with download_lock:
            download_status[job_id] = {'files': [], 'done': False, 'error': None}

        def on_files_listed(files):
            with download_lock:
                st = download_status.get(job_id)
                if st and not st['done']:
                    st['files'] = [{'name': f['name'], 'size': f['size'], 'status': 'pending', 'message': 'Oczekiwanie...', 'percent': 0} for f in files]

        def progress_callback(name, status, message, percent):
            with download_lock:
                st = download_status.get(job_id)
                if not st or st['done']:
                    return
                for f in st['files']:
                    if f['name'] == name:
                        f['status'] = status
                        f['message'] = message
                        f['percent'] = percent if percent is not None else f.get('percent', 0)
                        break

        class Args(object):
            pass
        args = Args()
        args.user = CHOMIK_USER
        args.password = CHOMIK_PASS
        args.hash = None
        args.url = url
        args.recursive = bool(data.get('recursive', True))
        args.structure = bool(data.get('structure', True))
        args.overwrite = bool(data.get('overwrite', False))
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
            with download_lock:
                st = download_status.get(job_id)
                if st:
                    st['done'] = True

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


if __name__ == '__main__':
    os.makedirs(DOWNLOAD_BASE, exist_ok=True)
    app.run(host='0.0.0.0', port=5000)
