import os
import re
import click
import random
import string
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, send_from_directory, request, redirect, url_for, flash, abort, session, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
import secrets
import requests
from sqlalchemy import func
from flask_mail import Mail, Message

load_dotenv(encoding="utf-8")

# ── Простой in-memory кеш для тяжёлых запросов главной страницы ──────────────
import threading, time as _time
_CACHE = {}
_CACHE_LOCK = threading.Lock()

def _cache_get(key, ttl=10):
    """Вернуть кешированное значение или None если устарело."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (_time.monotonic() - entry['ts']) < ttl:
            return entry['val']
    return None

def _cache_set(key, val):
    with _CACHE_LOCK:
        _CACHE[key] = {'val': val, 'ts': _time.monotonic()}

def _cache_bust(key):
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
# ─────────────────────────────────────────────────────────────────────────────

from models import (db, User, Game, Article, Comment, Vote, DropdownItem,
                    ExtraPage, Mute, IPBan, PenaltyHistory,
                    ArticleView, CommentReaction, SiteSettings, ExtraPageView,
                    UserPermission,
                    StreamPlatform, TopViewer, TopDonator, LastStream,
                    StreamMoment, NextGamePoll, PollGame, PollVote,
                    NextStream,
                    WeatherCity, UserCityShare,
                    Report, PasswordResetToken, AccountDeletion,
                    MessengerChat, ChatMember, ChatMessage, MsgReaction, TypingStatus,
                    Room, RoomMember, RoomMessage, RoomReaction, RoomApplication,
                    RoomTypingStatus, RoomJoinRequest,
                    ArticleSubscription, PushSubscription)

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'comilank-secret-key-2026-change-me')

_db_url = os.environ.get('DATABASE_URL', 'sqlite:///forum.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# SQLite не поддерживает connection pooling — определяем тип БД
_is_sqlite = _db_url.startswith('sqlite')
if _is_sqlite:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'connect_args': {'check_same_thread': False},
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle':  280,
        'pool_size':     10,
        'max_overflow':  20,
    }
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 год кэш для статики
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB
# Постоянная сессия - помнить пользователя 30 дней
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_SECURE']   = False   # True если HTTPS
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE']  = 'Lax'
app.config['PREFERRED_URL_SCHEME'] = os.environ.get('PREFERRED_URL_SCHEME', 'https')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS


# ═══════════════════════════════════════════════
#  PUSH HELPER FUNCTIONS
# ═══════════════════════════════════════════════

def _do_send_push(user_id, title, body, url='/forum'):
    """Отправить Web Push одному пользователю на все его устройства."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return   # pywebpush не установлен — тихо пропускаем

    vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
    vapid_email   = os.environ.get('VAPID_CLAIMS_EMAIL', 'webmaster@comilank.net')
    if not vapid_private:
        return

    import json
    subs = PushSubscription.query.filter_by(user_id=user_id).all()
    bad  = []
    for sub in subs:
        try:
            webpush(
                subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
                data=json.dumps({'title': title, 'body': body, 'url': url}),
                vapid_private_key=vapid_private,
                vapid_claims={'sub': f'mailto:{vapid_email}'}
            )
        except Exception as e:
            if '410' in str(e) or '404' in str(e):
                bad.append(sub.endpoint)
    for ep in bad:
        PushSubscription.query.filter_by(endpoint=ep).delete()
    if bad:
        db.session.commit()


def _push_article_update(article, event_type='update', extra_title=None):
    """Push всем подписчикам конкретной статьи."""
    subs = ArticleSubscription.query.filter_by(article_id=article.id).all()
    actor_id = current_user.id if current_user.is_authenticated else None
    for sub in subs:
        if sub.user_id == actor_id:
            continue   # себе не шлём
        if event_type == 'extra':
            title = f'📄 Новая доп. статья: «{article.title[:35]}»'
            body  = extra_title or 'Появилась новая дополнительная статья'
        else:
            title = f'✏️ Обновлена: «{article.title[:40]}»'
            body  = 'Статья была изменена — заходи посмотреть'
        _do_send_push(sub.user_id, title, body, f'/article/{article.id}')


def _push_new_forum_article(article):
    """Push всем у кого есть push-подписка — о новой статье на форуме."""
    all_user_ids = db.session.query(PushSubscription.user_id).distinct().all()
    actor_id = current_user.id if current_user.is_authenticated else None
    title = '🔥 Новая статья на Comilank!'
    body  = f'«{article.title[:60]}» — читай прямо сейчас'
    for (uid,) in all_user_ids:
        if uid == actor_id:
            continue
        _do_send_push(uid, title, body, f'/article/{article.id}')

def safe_filename(filename):
    """
    Безопасное имя файла с поддержкой кириллицы и любых Unicode-имён.
    secure_filename() на Linux отбрасывает кириллицу → пустая строка → файл не сохраняется.
    Решение: транслитерируем кириллицу → латиницу, затем применяем secure_filename().
    """
    _TRANSLIT = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
        'и':'i','й':'j','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
        'с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh',
        'щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
        'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'Yo','Ж':'Zh','З':'Z',
        'И':'I','Й':'J','К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R',
        'С':'S','Т':'T','У':'U','Ф':'F','Х':'Kh','Ц':'Ts','Ч':'Ch','Ш':'Sh',
        'Щ':'Sch','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'Yu','Я':'Ya',
    }
    transliterated = ''.join(_TRANSLIT.get(c, c) for c in filename)
    result = secure_filename(transliterated)
    if not result:
        # Если всё равно пусто (очень необычные символы) — генерируем имя из расширения
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
        result = f'file_{secrets.token_hex(6)}.{ext}'
    return result

app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL']        = os.environ.get('MAIL_USE_SSL', 'False').lower() == 'true'
# Берём из .env, убираем пробелы и приводим email к нижнему регистру
_raw_mail_user = os.environ.get('MAIL_USERNAME', 'comilanksite@gmail.com')
_raw_mail_pass = os.environ.get('MAIL_PASSWORD', 'bkwtuyeocnvvmtvl')
app.config['MAIL_USERNAME']       = _raw_mail_user.lower().strip()
app.config['MAIL_PASSWORD']       = _raw_mail_pass.replace(' ', '').strip()
app.config['MAIL_DEFAULT_SENDER'] = ('Comilank', app.config['MAIL_USERNAME'])
app.config['MAIL_MAX_EMAILS']     = None
app.config['MAIL_ASCII_ATTACHMENTS'] = False

mail = Mail(app)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите для доступа к этой странице.'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Запускаем миграцию при загрузке модуля (для Gunicorn / Render)
# Вызов _startup() перенесён вниз файла - после определения _run_migration
def _startup():
    try:
        db.create_all()
        # Safe column migrations
        _safe_cols = [
            "ALTER TABLE rooms ADD COLUMN is_featured BOOLEAN DEFAULT 0",
            "ALTER TABLE room_messages ADD COLUMN is_pinned BOOLEAN DEFAULT 0",
            "CREATE TABLE IF NOT EXISTS room_message_reads (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL REFERENCES room_messages(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(msg_id, user_id))",
        ]
        for _sql in _safe_cols:
            try:
                db.session.execute(db.text(_sql));
                db.session.commit()
            except:
                db.session.rollback()
        
        _run_migration()
    
    except Exception as _e:
        print(f"[STARTUP MIGRATION ERROR] {_e}")

@app.before_request
def update_last_seen():
    # Обновляем last_seen не чаще раза в 60 сек — иначе каждый запрос делает commit
    if current_user.is_authenticated:
        from flask import g as _g
        _now = datetime.utcnow()
        _last = session.get('_ls_ts', 0)
        if _now.timestamp() - _last > 60:
            current_user.last_seen = _now
            db.session.commit()
            session['_ls_ts'] = _now.timestamp()

@app.route('/api/check-vpn')
def api_check_vpn():
    from datetime import datetime as _dt
    cached = session.get('_vpn_cache')
    cached_at = session.get('_vpn_cache_at', 0)
    if cached is not None and (_dt.utcnow().timestamp() - cached_at) < 600:
        return jsonify({'vpn': cached})
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip:
            ip = ip.split(',')[0].strip()

        # Локальный IP - получаем реальный внешний IP
        _local = ('127.', '::1', 'localhost', '10.', '192.168.', '172.')
        if not ip or any(ip.startswith(p) for p in _local):
            try:
                ext = requests.get('https://api.ipify.org?format=json', timeout=3)
                ip = ext.json().get('ip', ip)
            except Exception:
                pass

        is_vpn = False
        country = ''
        vpn_type = 'VPN'

        # Попытка 1: ip-api.com - только proxy (hosting даёт ложные срабатывания)
        try:
            r1 = requests.get(
                f'http://ip-api.com/json/{ip}?fields=status,proxy,hosting,query,country,isp',
                timeout=3
            )
            d1 = r1.json()
            if d1.get('status') == 'success':
                country = d1.get('country', '')
                if d1.get('proxy'):
                    is_vpn = True
                    vpn_type = 'Proxy'
        except Exception:
            pass

        # Попытка 2: ipwho.is - vpn/proxy/tor
        if not is_vpn:
            try:
                r2 = requests.get(f'https://ipwho.is/{ip}', timeout=4)
                d2 = r2.json()
                sec = d2.get('security', {})
                if not country:
                    country = d2.get('country', '')
                if d2.get('success') and (sec.get('vpn') or sec.get('proxy') or sec.get('tor')):
                    is_vpn = True
                    vpn_type = 'Tor' if sec.get('tor') else ('Proxy' if sec.get('proxy') else 'VPN')
            except Exception:
                pass

        # Попытка 3: proxycheck.io
        if not is_vpn:
            try:
                r3 = requests.get(
                    f'https://proxycheck.io/v2/{ip}?vpn=1&asn=1',
                    timeout=3
                )
                d3 = r3.json()
                ip_data = d3.get(ip, {})
                if ip_data.get('proxy') == 'yes' or ip_data.get('type') == 'VPN':
                    is_vpn = True
                    vpn_type = ip_data.get('type', 'VPN')
                    if not country:
                        country = ip_data.get('country', '')
            except Exception:
                pass

        # Логируем - не дублируем одного пользователя с одного IP чаще раза в час
        if is_vpn:
            try:
                uid = current_user.id if current_user.is_authenticated else None
                hour_ago = _dt.utcnow() - __import__('datetime').timedelta(hours=1)
                existing = db.session.execute(db.text(
                    "SELECT id FROM vpn_logs WHERE ip_address=:ip AND (user_id=:uid OR (:uid IS NULL AND user_id IS NULL)) AND detected_at > :hr LIMIT 1"
                ), {'ip': ip, 'uid': uid, 'hr': hour_ago}).fetchone()
                if not existing:
                    db.session.execute(db.text(
                        "INSERT INTO vpn_logs (user_id, ip_address, vpn_type, country, detected_at) "
                        "VALUES (:uid, :ip, :vt, :co, :dt)"
                    ), {'uid': uid, 'ip': ip, 'vt': vpn_type, 'co': country, 'dt': _dt.utcnow()})
                    db.session.commit()
            except Exception:
                pass

        session['_vpn_cache'] = is_vpn
        session['_vpn_cache_at'] = _dt.utcnow().timestamp()
        return jsonify({'vpn': is_vpn})
    except Exception:
        return jsonify({'vpn': False})

@app.route('/api/check-vpn-debug')
def api_check_vpn_debug():

    from datetime import datetime as _dt
    if not current_user.is_authenticated or current_user.role < 4:
        abort(403)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip:
        ip = ip.split(',')[0].strip()
    results = {'ip': ip, 'services': {}}
    try:
        r1 = requests.get(f'http://ip-api.com/json/{ip}?fields=status,proxy,hosting,query', timeout=3)
        results['services']['ip-api'] = r1.json()
    except Exception as e:
        results['services']['ip-api'] = str(e)
    try:
        r2 = requests.get(f'https://ipwho.is/{ip}', timeout=4)
        results['services']['ipwho'] = r2.json()
    except Exception as e:
        results['services']['ipwho'] = str(e)
    try:
        r3 = requests.get(f'https://proxycheck.io/v2/{ip}?vpn=1&asn=1', timeout=3)
        results['services']['proxycheck'] = r3.json()
    except Exception as e:
        results['services']['proxycheck'] = str(e)
    return jsonify(results)

def parse_duration(duration_str):
    if not duration_str:
        return None
    match = re.match(r'^(\d+)([mhd])$', duration_str.strip().lower())
    if not match:
        return None
    value, unit = match.groups()
    value = int(value)
    if unit == 'm': return timedelta(minutes=value)
    elif unit == 'h': return timedelta(hours=value)
    elif unit == 'd': return timedelta(days=value)
    return None

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def normalize_username(username):
    """
    Нормализует ник для проверки уникальности.
    Убирает только буквы (любого регистра) для сравнения,
    оставляя цифры и спецсимволы как есть.
    Пример: 'Вини', 'вини', 'ВИНИ', 'ВиНи' → одинаковы.
            'Вини1' и 'вини1' → одинаковы.
            'Вини1' и 'вини2' → разные.
    Для обычных юзеров (role=0): только буквы нормализуются к нижнему регистру.
    Для роли 4 (главный админ): полная свобода - проверка только по func.lower().
    """
    # Убираем все не-буквенные символы, приводим буквы к нижнему регистру
    letters_only = re.sub(r'[^a-zA-Zа-яёА-ЯЁ]', '', username).lower()
    return letters_only

def _is_vpn_ip(ip):
    """Проверяет IP по кэшу сессии и таблице vpn_logs. Без внешних запросов."""
    try:
        cached = session.get('_vpn_cache')
        if cached is not None:
            return bool(cached)
        week_ago = datetime.utcnow() - timedelta(days=7)
        row = db.session.execute(
            db.text("SELECT id FROM vpn_logs WHERE ip_address=:ip AND detected_at > :d LIMIT 1"),
            {'ip': ip, 'd': week_ago}
        ).fetchone()
        return row is not None
    except Exception:
        return False

def record_article_view(article):
    ip = get_client_ip()
    try:
        if current_user.is_authenticated:
            # Уже смотрел залогиненным?
            if ArticleView.query.filter_by(article_id=article.id, user_id=current_user.id).first():
                return
            # Смотрел гостем с этого IP?
            existing_ip = ArticleView.query.filter_by(article_id=article.id, ip_address=ip).first()
            if existing_ip:
                if existing_ip.user_id is None:
                    existing_ip.user_id = current_user.id
                    db.session.commit()
                return
            # VPN — не считаем
            if _is_vpn_ip(ip):
                return
            db.session.add(ArticleView(article_id=article.id, user_id=current_user.id, ip_address=ip))
            article.views += 1
            db.session.commit()
        else:
            # Гость: уже смотрел с этого IP?
            if ArticleView.query.filter_by(article_id=article.id, ip_address=ip).first():
                return
            if _is_vpn_ip(ip):
                return
            db.session.add(ArticleView(article_id=article.id, ip_address=ip, user_id=None))
            article.views += 1
            db.session.commit()
    except Exception as _e:
        try: db.session.rollback()
        except Exception: pass

def record_extra_page_view(page):
    ip = get_client_ip()
    try:
        if current_user.is_authenticated:
            if ExtraPageView.query.filter_by(page_id=page.id, user_id=current_user.id).first():
                return
            existing_ip = ExtraPageView.query.filter_by(page_id=page.id, ip_address=ip).first()
            if existing_ip:
                if existing_ip.user_id is None:
                    existing_ip.user_id = current_user.id
                    db.session.commit()
                return
            if _is_vpn_ip(ip):
                return
            db.session.add(ExtraPageView(page_id=page.id, user_id=current_user.id, ip_address=ip))
            page.views += 1
            db.session.commit()
        else:
            if ExtraPageView.query.filter_by(page_id=page.id, ip_address=ip).first():
                return
            if _is_vpn_ip(ip):
                return
            db.session.add(ExtraPageView(page_id=page.id, ip_address=ip, user_id=None))
            page.views += 1
            db.session.commit()
    except Exception as _e:
        try: db.session.rollback()
        except Exception: pass

@app.cli.command('set-admin')
@click.argument('username')
def set_admin(username):
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user:
            user.role = 4
            db.session.commit()
            click.echo(f'Пользователь {username} теперь главный администратор.')
        else:
            click.echo(f'Пользователь {username} не найден.')

@app.cli.command('set-role')
@click.argument('username')
@click.argument('role', type=int)
def set_role(username, role):

    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user:
            user.role = role
            db.session.commit()
            roles = {0:'пользователь',1:'модератор',2:'редактор',3:'ст.модератор',4:'администратор'}
            click.echo(f'{username} -> роль {role} ({roles.get(role,"?")})')
        else:
            click.echo(f'Пользователь {username} не найден.')

YOUTUBE_API_KEY     = os.environ.get('YOUTUBE_API_KEY') or 'AIzaSyCbJF2Jl2AMdDXemcir4KVnTBJdx3rFUTA'
VIDEO_ID_SHORTS     = os.environ.get('VIDEO_ID_SHORTS', '')
VIDEO_ID_HORIZONTAL = os.environ.get('VIDEO_ID_HORIZONTAL', '')

def fetch_youtube_viewers(video_id):
    if not video_id: return 0, False
    url = f'https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}&key={YOUTUBE_API_KEY}'
    try:
        data  = requests.get(url, timeout=5).json()
        if 'error' in data: return 0, False
        items = data.get('items', [])
        if items:
            details = items[0].get('liveStreamingDetails', {})
            if 'concurrentViewers' in details:
                return int(details['concurrentViewers']), True
        return 0, False
    except Exception:
        return 0, False

@app.after_request
def add_security_headers(response):
    # SAMEORIGIN для /game/ и /chat — используются в iframe внутри сайта
    if request.path.startswith('/game') or request.path.startswith('/chat'):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    else:
        response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection']       = '1; mode=block'
    response.headers.pop('Server', None)
    response.headers.pop('X-Powered-By', None)
    # Долгий кеш для статических файлов (картинки, JS, CSS, шрифты)
    if request.path.startswith('/static/'):
        ct = response.content_type or ''
        if any(x in ct for x in ('image', 'javascript', 'css', 'font', 'woff')):
            response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'
    # Не кешировать API и HTML-страницы
    elif request.path.startswith('/api/') or response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/node_modules/<path:filename>')
def serve_node_modules(filename): return send_from_directory('node_modules', filename)

@app.route('/src/<path:filename>')
def serve_src(filename): return send_from_directory('src', filename)

@app.route('/')
def index():
    # F5-редирект: если есть cookie с реальным путём - возвращаем туда
    # Но НЕ редиректим на статьи/доп.страницы — у них свои прямые URL
    _NO_REDIRECT = ('/article/', '/extra/', '/forum', '/admin', '/login',
                    '/register', '/profile', '/api/', '/static/')
    rp = request.cookies.get('_rp')
    if rp and rp != '/' and not rp.startswith('//'):
        # Проверяем что путь не относится к страницам с прямым URL
        _skip_rp = any(rp.startswith(p) for p in _NO_REDIRECT)
        if not _skip_rp:
            resp = make_response(redirect(rp))
            resp.delete_cookie('_rp')
            return resp
        else:
            # Удаляем некорректную cookie и показываем главную
            resp = make_response(redirect(url_for('index')))
            resp.delete_cookie('_rp')
            return resp

    _init_stream_platforms()   # создаём платформы если нет

    # Кешируем тяжёлые редко-меняющиеся данные на 30 секунд
    _cached_heavy = _cache_get('index_heavy', ttl=30)
    if _cached_heavy is None:
        _cached_heavy = {
            'stream_platforms': StreamPlatform.query.order_by(StreamPlatform.id).all(),
            'top_viewers':      TopViewer.query.order_by(TopViewer.position).limit(4).all(),
            'top_donators':     TopDonator.query.order_by(TopDonator.position).limit(3).all(),
            'stream_moments':   StreamMoment.query.order_by(StreamMoment.position).all(),
            'next_stream_widget': NextStream.query.first(),
            'next_poll':        NextGamePoll.query.order_by(NextGamePoll.id.desc()).first(),
        }
        _cache_set('index_heavy', _cached_heavy)

    stream_platforms   = _cached_heavy['stream_platforms']
    top_viewers        = _cached_heavy['top_viewers']
    top_donators       = _cached_heavy['top_donators']
    stream_moments     = _cached_heavy['stream_moments']
    next_stream_widget = _cached_heavy['next_stream_widget']
    next_poll          = _cached_heavy.get('next_poll')

    last_stream       = LastStream.query.order_by(LastStream.id.desc()).first()
    last_article      = Article.query.filter_by(category='article').order_by(Article.created_at.desc()).first()
    last_news         = Article.query.filter_by(category='news').order_by(Article.created_at.desc()).first()
    last_film         = Article.query.filter_by(category='film').order_by(Article.created_at.desc()).first()
    # next_poll берётся из кэша выше
    current_user_vote = None
    if current_user.is_authenticated and next_poll:
        pv = PollVote.query.filter_by(poll_id=next_poll.id, user_id=current_user.id).first()
        if pv:
            current_user_vote = pv.game_id
    return render_template('index.html',
        stream_platforms=stream_platforms,
        top_viewers=top_viewers,
        top_donators=top_donators,
        last_stream=last_stream,
        last_article=last_article,
        last_news=last_news,
        last_film=last_film,
        stream_moments=stream_moments,
        next_poll=next_poll,
        next_stream_widget=next_stream_widget,
        current_user_vote=current_user_vote,
    )

@app.route('/youtube-viewers')
def youtube_viewers():
    viewers, live = fetch_youtube_viewers(VIDEO_ID_SHORTS)
    return jsonify(viewers=viewers, live=live)

@app.route('/youtube-viewers-horizontal')
def youtube_viewers_horizontal():
    viewers, live = fetch_youtube_viewers(VIDEO_ID_HORIZONTAL)
    return jsonify(viewers=viewers, live=live)

def _init_weather_cities():

    try:
        if WeatherCity.query.count() == 0:
            _defaults = [
                ('Дубай',           25.2048,  55.2708,  'Asia/Dubai',       '', 1),
                ('Москва',          55.7558,  37.6173,  'Europe/Moscow',     '', 2),
                ('Лондон',          51.5074,  -0.1278,  'Europe/London',     '', 3),
                ('Нью-Йорк',        40.7128, -74.0060,  'America/New_York',  '', 4),
                ('Токио',           35.6762, 139.6503,  'Asia/Tokyo',        '', 5),
                ('Сидней',         -33.8688, 151.2093,  'Australia/Sydney',  '', 6),
                ('Рио-де-Жанейро', -22.9068, -43.1729,  'America/Sao_Paulo', '', 7),
                ('Кейптаун',       -33.9249,  18.4241,  'Africa/Johannesburg','',8),
                ('Берлин',          52.5200,  13.4050,  'Europe/Berlin',     '', 9),
                ('Сеул',            37.5665, 126.9780,  'Asia/Seoul',        '',10),
            ]
            for name, lat, lon, tz, lm, pos in _defaults:
                db.session.add(WeatherCity(name=name, lat=lat, lon=lon, tz=tz,
                                           landmark=lm, position=pos, is_active=True))
            db.session.commit()
    except Exception:
        db.session.rollback()

# ══════════════════════════════════════════════════════
# API: УВЕДОМЛЕНИЯ
# ══════════════════════════════════════════════════════
@app.route('/api/notifications')
@login_required
def api_notifications():
    from models import Notification
    limit  = min(int(request.args.get('limit', 15)), 50)
    offset = max(int(request.args.get('offset', 0)), 0)
    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).limit(limit).offset(offset).all()
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    result = []
    for n in notifs:
        actor = n.actor
        extra_page_id = None
        try:
            extra_page_id = n.extra_page_id
        except Exception:
            pass
        result.append({
            'id':           n.id,
            'type':         n.notif_type,
            'actor':        actor.username if actor else '?',
            'actor_avatar': actor.avatar if actor else None,
            'article_id':   n.article_id,
            'comment_id':   n.comment_id,
            'extra_page_id': extra_page_id,
            'preview':      n.preview or '',
            'is_read':      n.is_read,
            'created_at':   n.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        })
    return jsonify(notifications=result, unread=unread)

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_notifications_read():
    from models import Notification
    data = request.get_json(silent=True) or {}
    nid = data.get('id')
    if nid:
        n = Notification.query.filter_by(id=nid, user_id=current_user.id).first()
        if n:
            n.is_read = True
            db.session.commit()
    else:
        Notification.query.filter_by(user_id=current_user.id, is_read=False)\
            .update({'is_read': True})
        db.session.commit()
    return jsonify(ok=True)

def _create_notification(user_id, actor_id, notif_type, article_id=None, comment_id=None, preview=None, extra_page_id=None):
    """Создаёт уведомление. Не создаёт если actor == user (сам себе)."""
    try:
        if user_id == actor_id:
            return
        from models import Notification
        n = Notification(
            user_id=user_id,
            actor_id=actor_id,
            notif_type=notif_type,
            article_id=article_id,
            comment_id=comment_id,
            preview=(preview or '')[:200],
            is_read=False,
            created_at=datetime.utcnow()
        )
        # Добавляем поле extra_page_id если есть такое поле
        if extra_page_id is not None:
            try:
                n.extra_page_id = extra_page_id
            except Exception:
                pass
        db.session.add(n)
        db.session.commit()
    except Exception as _e:
        try: db.session.rollback()
        except Exception: pass

# ══════════════════════════════════════════════════════
# ГОЛОСОВЫЕ СООБЩЕНИЯ В КОММЕНТАРИЯХ
# ══════════════════════════════════════════════════════
import os as _os

def _can_use_voice(user):
    """Проверяет право на голосовые сообщения: role>=4 всегда, остальным — через UserPermission."""
    if user.role >= 4:
        return True
    from models import UserPermission
    perm = UserPermission.query.filter_by(user_id=user.id).first()
    return bool(perm and getattr(perm, 'can_voice_reply', False))

@app.route('/api/voice-comment', methods=['POST'])
@login_required
def api_voice_comment():
    """Загрузка голосового сообщения как комментария."""
    if not _can_use_voice(current_user):
        return jsonify(ok=False, error='Нет прав на голосовые сообщения'), 403
    article_id = request.form.get('article_id', type=int)
    parent_id  = request.form.get('parent_id', type=int)
    if not article_id:
        return jsonify(ok=False, error='article_id required'), 400
    art = Article.query.get_or_404(article_id)
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify(ok=False, error='no audio'), 400
    # Сохраняем файл
    import uuid as _uuid
    ext = '.webm'
    fname = f'voice_{_uuid.uuid4().hex}{ext}'
    upload_dir = _os.path.join('static', 'voice_messages')
    _os.makedirs(upload_dir, exist_ok=True)
    fpath = _os.path.join(upload_dir, fname)
    audio_file.save(fpath)
    voice_url = f'/static/voice_messages/{fname}'
    # Создаём комментарий со специальным маркером
    content = f'[VOICE]{voice_url}[/VOICE]'
    from models import Mute as _Mute
    active_mute = _Mute.query.filter_by(user_id=current_user.id)\
        .filter(_Mute.muted_until > datetime.utcnow()).first()
    if active_mute:
        return jsonify(ok=False, error='muted'), 403
    comment = Comment(content=content, article_id=article_id,
                      author_id=current_user.id, parent_id=parent_id)
    db.session.add(comment)
    db.session.commit()
    return jsonify(ok=True, id=comment.id, voice_url=voice_url,
                   username=current_user.username,
                   avatar=current_user.avatar or '',
                   role=current_user.role)

@app.route('/api/voice-comment/extra', methods=['POST'])
@login_required
def api_voice_comment_extra():
    """Голосовой ответ в extra (форум/доп.страницы)."""
    if not _can_use_voice(current_user):
        return jsonify(ok=False, error='Нет прав на голосовые сообщения'), 403
    extra_id  = request.form.get('extra_id', type=int)
    parent_id = request.form.get('parent_id', type=int)
    if not extra_id:
        return jsonify(ok=False, error='extra_id required'), 400
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify(ok=False, error='no audio'), 400
    import uuid as _uuid
    fname = f'voice_{_uuid.uuid4().hex}.webm'
    upload_dir = _os.path.join('static', 'voice_messages')
    _os.makedirs(upload_dir, exist_ok=True)
    audio_file.save(_os.path.join(upload_dir, fname))
    voice_url = f'/static/voice_messages/{fname}'
    content = f'[VOICE]{voice_url}[/VOICE]'
    from models import ExtraPageComment, Mute as _Mute
    active_mute = _Mute.query.filter_by(user_id=current_user.id)\
        .filter(_Mute.muted_until > datetime.utcnow()).first()
    if active_mute:
        return jsonify(ok=False, error='muted'), 403
    comment = ExtraPageComment(content=content, page_id=extra_id,
                               author_id=current_user.id, parent_id=parent_id)
    db.session.add(comment)
    db.session.commit()
    return jsonify(ok=True, id=comment.id, voice_url=voice_url,
                   username=current_user.username,
                   avatar=current_user.avatar or '',
                   role=current_user.role)

@app.route('/api/can-voice')
@login_required
def api_can_voice():
    return jsonify(can_voice=_can_use_voice(current_user))

@app.route('/api/weather')
def api_weather():
    """Прокси погоды через wttr.in — без ключей, реальное время."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _parse_wttr(data):
        try:
            cur = data['current_condition'][0]
            temp = float(cur.get('temp_C', 0))
            wind = round(float(cur.get('windspeedKmph', 0)) / 3.6, 1)
            ww   = int(cur.get('weatherCode', 0))
            desc_list = cur.get('weatherDesc', [{}])
            desc_en = desc_list[0].get('value', '') if desc_list else ''
            hour = int(cur.get('observation_time', '12:00 AM').replace(':','').replace(' AM','').replace(' PM','')[:2] if ':' in cur.get('observation_time','') else 12)
            is_day = 1 if 6 <= hour <= 20 else 0
            def ww_to_wmo(ww):
                if ww in (113,):            return 0
                if ww in (116,):            return 1
                if ww in (119, 122):        return 3
                if ww in (143, 248, 260):   return 45
                if ww in (176, 263, 266, 293, 296):  return 61
                if ww in (299, 302, 305, 308):        return 63
                if ww in (281, 284, 185, 182):        return 51
                if ww in (179, 323, 326, 329, 332, 335, 338, 368, 371): return 73
                if ww in (311, 314, 317, 320):  return 71
                if ww in (227, 230):            return 86
                if ww in (200, 386, 389, 392, 395): return 95
                if ww in (362, 365, 374, 377):  return 80
                return 0
            code = ww_to_wmo(ww)
            desc_map = {0:'Ясно', 1:'Переменная облачность', 3:'Облачно', 45:'Туман', 51:'Морось', 61:'Дождь', 63:'Дождь', 65:'Ливень', 71:'Снег', 73:'Снег', 80:'Ливень', 86:'Метель', 95:'Гроза'}
            desc_ru = desc_map.get(code, desc_en or 'Ясно')
            return {'temp': round(temp, 1), 'wind': wind, 'code': code, 'desc': desc_ru, 'is_day': is_day}
        except Exception:
            return {'temp': None, 'wind': 0, 'code': 0, 'desc': 'Ясно', 'is_day': 1}

    def _fetch_city(lat, lon, name):
        try:
            url = f'https://wttr.in/{lat},{lon}?format=j1'
            r = requests.get(url, timeout=6, headers={'User-Agent': 'comilank-weather/1.0'})
            d = _parse_wttr(r.json())
            d['name'] = name
            return d
        except Exception:
            return {'name': name, 'temp': None, 'wind': 0, 'code': 0, 'desc': 'Ясно', 'is_day': 1}

    # Single-city mode — кэш 5 минут на город
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    tz  = request.args.get('tz', '')
    name = request.args.get('name', '')
    if lat is not None and lon is not None:
        _wkey = f'weather_{lat:.3f}_{lon:.3f}'
        _cached = _cache_get(_wkey, ttl=300)
        if _cached is not None:
            return jsonify(cities=[_cached])
        d = _fetch_city(lat, lon, name)
        _cache_set(_wkey, d)
        return jsonify(cities=[d])

    # Multi-city mode — кэш 5 минут для всех городов
    _wkey_all = 'weather_all_cities'
    _cached_all = _cache_get(_wkey_all, ttl=300)
    if _cached_all is not None:
        return jsonify(cities=_cached_all)

    _init_weather_cities()
    db_cities = WeatherCity.query.filter_by(is_active=True).order_by(WeatherCity.position).all()
    result_map = {}
    def _fetch_db_city(c):
        d = _fetch_city(c.lat, c.lon, c.name)
        d['id'] = c.id
        return d
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_fetch_db_city, c): c.id for c in db_cities}
        for fut in as_completed(futures):
            try:
                d = fut.result()
                result_map[d['id']] = d
            except Exception:
                pass
    result = [result_map.get(c.id, {'id': c.id, 'name': c.name, 'temp': None, 'wind': 0, 'code': 0, 'desc': 'Ясно', 'is_day': 1}) for c in db_cities]
    _cache_set(_wkey_all, result)
    return jsonify(cities=result)
@app.route('/comilank-secret-admin-x7k2/Вини')
def make_admin():
    user = User.query.filter(func.lower(User.username) == func.lower('Вини')).first()
    if user:
        user.role = 4
        db.session.commit()
        return 'Готово! Роль администратора выдана.'
    return 'Пользователь не найден. Сначала зарегистрируйся.'

@app.route('/admin/chaos-button/toggle', methods=['POST'])
@login_required
def toggle_chaos_button():

    if current_user.role < 4:
        abort(403)
    settings = SiteSettings.get()
    settings.chaos_button_enabled = not settings.chaos_button_enabled
    db.session.commit()
    state = 'включена' if settings.chaos_button_enabled else 'выключена'
    flash(f'Кнопка "Не нажимай" {state}', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/quick-ban', methods=['POST'])
@login_required
def admin_quick_ban():
    if current_user.role < 4:
        return jsonify(ok=False, message='Нет прав'), 403
    username = request.form.get('username', '').strip()
    duration = request.form.get('duration', '1d')
    reason   = request.form.get('reason', '').strip() or 'Нарушение правил'
    target = User.query.filter_by(username=username).first()
    if not target:
        return jsonify(ok=False, message=f'Пользователь «{username}» не найден')
    if not current_user.can_ban(target):
        return jsonify(ok=False, message='Нельзя забанить этого пользователя')
    dur_map = {'30m': 30, '1h': 60, '6h': 360, '1d': 1440, '3d': 4320, '7d': 10080, '30d': 43200, 'perm': None}
    minutes = dur_map.get(duration)
    if minutes is None:
        target.banned_until = datetime(9999, 12, 31)
    else:
        target.banned_until = datetime.utcnow() + timedelta(minutes=minutes)
    target.ban_reason   = reason
    target.banned_by_id = current_user.id
    ph = PenaltyHistory(user_id=target.id, action='ban', duration=duration,
                        reason=reason, expires_at=target.banned_until, created_by_id=current_user.id)
    db.session.add(ph)
    db.session.commit()
    return jsonify(ok=True, message=f'Пользователь «{username}» забанен')

@app.route('/admin/quick-mute', methods=['POST'])
@login_required
def admin_quick_mute():
    if current_user.role < 1:
        return jsonify(ok=False, message='Нет прав'), 403
    username = request.form.get('username', '').strip()
    duration = request.form.get('duration', '1h')
    reason   = request.form.get('reason', '').strip() or 'Нарушение правил'
    target = User.query.filter_by(username=username).first()
    if not target:
        return jsonify(ok=False, message=f'Пользователь «{username}» не найден')
    if not current_user.can_mute(target):
        return jsonify(ok=False, message='Нельзя заглушить этого пользователя')
    dur_map = {'30m': 30, '1h': 60, '6h': 360, '1d': 1440, '3d': 4320, '7d': 10080}
    minutes = dur_map.get(duration, 60)
    muted_until = datetime.utcnow() + timedelta(minutes=minutes)
    mute = Mute(user_id=target.id, muted_until=muted_until, reason=reason, muted_by_id=current_user.id)
    db.session.add(mute)
    ph = PenaltyHistory(user_id=target.id, action='mute', duration=duration,
                        reason=reason, expires_at=muted_until, created_by_id=current_user.id)
    db.session.add(ph)
    db.session.commit()
    return jsonify(ok=True, message=f'Пользователь «{username}» заглушен')

@app.route('/admin/quick-unban', methods=['POST'])
@login_required
def admin_quick_unban():
    if current_user.role < 3:
        return jsonify(ok=False, message='Нет прав'), 403
    username = request.form.get('username', '').strip()
    target = User.query.filter_by(username=username).first()
    if not target:
        return jsonify(ok=False, message=f'Пользователь «{username}» не найден')
    target.banned_until = None
    target.ban_reason   = None
    ph = PenaltyHistory(user_id=target.id, action='unban', created_by_id=current_user.id)
    db.session.add(ph)
    db.session.commit()
    return jsonify(ok=True, message=f'Пользователь «{username}» разбанен')

@app.route('/admin/broadcast', methods=['POST'])
@login_required
def admin_broadcast():
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 4 and not (perm and getattr(perm, 'can_broadcast', False)):
        return jsonify(ok=False, message='Нет прав'), 403
    subject = request.form.get('subject', '').strip()
    message_text = request.form.get('message', '').strip()
    if not subject or not message_text:
        return jsonify(ok=False, message='Заполните тему и текст')
    users = User.query.filter(User.email != None).all()
    sent = 0
    for u in users:
        try:
            msg = MailMessage(
                subject=f'[Comilank] {subject}',
                recipients=[u.email],
                body=f'{message_text}\n\n- Команда Comilank'
            )
            mail.send(msg)
            sent += 1
        except Exception:
            pass
    return jsonify(ok=True, message=f'Отправлено {sent} пользователям')

@app.route('/chaos')
def chaos_trigger():

    settings = SiteSettings.get()
    if not settings.chaos_button_enabled:
        abort(404)
    return jsonify(triggered=True)

@app.route('/welcome')
def welcome():
    if not current_user.is_authenticated: return redirect(url_for('login'))
    return render_template('welcome.html', user=current_user)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username     = request.form['username'].strip()
        email        = request.form['email'].strip()
        password     = request.form['password']
        recovery_key = request.form.get('recovery_key', '').strip()
        terms        = request.form.get('terms_agreed')
        privacy      = request.form.get('privacy_agreed')
        cookies_ok   = request.form.get('cookies_agreed')

        if not terms or not privacy or not cookies_ok:
            flash('Необходимо принять все три соглашения: пользовательское, политику конфиденциальности и использование cookie', 'error')
            return redirect(url_for('register'))

        if User.query.filter(func.lower(User.username) == func.lower(username)).first():
            flash('Имя пользователя уже занято', 'error')
            return redirect(url_for('register'))

        new_norm = normalize_username(username)
        if new_norm:
            for existing in User.query.filter(User.role < 4).all():
                if normalize_username(existing.username) == new_norm:
                    flash('Имя пользователя слишком похоже на уже существующее', 'error')
                    return redirect(url_for('register'))

        if User.query.filter(func.lower(User.email) == func.lower(email)).first():
            flash('Этот email уже привязан к существующему аккаунту. Войдите или восстановите пароль.', 'error')
            return redirect(url_for('register'))

        # Валидация формата email
        import re as _re_email
        _email_pattern = _re_email.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
        if not _email_pattern.match(email):
            flash('Введите корректный email адрес (например: user@gmail.com)', 'error')
            return redirect(url_for('register'))

        # DNS-проверка домена почты
        _email_domain = email.split('@')[1].lower()
        try:
            import dns.resolver as _dns_r
            _valid_domain = False
            try:
                _dns_r.resolve(_email_domain, 'MX', lifetime=3)
                _valid_domain = True
            except Exception:
                try:
                    _dns_r.resolve(_email_domain, 'A', lifetime=3)
                    _valid_domain = True
                except Exception:
                    pass
            if not _valid_domain:
                flash('Почта не существует или домен не найден. Введите реальный email.', 'error')
                return redirect(url_for('register'))
        except ImportError:
            pass  # dnspython не установлен — пропускаем DNS-проверку

        # Создаём пользователя сразу без шага верификации
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            recovery_key=recovery_key,
            terms_agreed=True,
            privacy_agreed=True,
            role=0
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        # Приветственное письмо (опционально, в отдельном потоке чтобы не блокировать)
        def _send_welcome(_app, _email, _username, _recovery_key):
            with _app.app_context():
                try:
                    _msg = Message(
                        subject='[Comilank] Добро пожаловать!',
                        recipients=[_email],
                        body=(
                            f'Привет, {_username}!\n\n'
                            f'Ваш аккаунт на Comilank успешно создан.\n'
                            f'Ключ восстановления сохраните в надёжном месте: {_recovery_key or "(не задан)"}\n\n'
                            f'- Команда Comilank\nhttps://comilank.net'
                        )
                    )
                    mail.send(_msg)
                except Exception as _e:
                    print(f'[MAIL WELCOME SKIP] {_e}')
        try:
            import threading as _threading
            _t = _threading.Thread(target=_send_welcome, args=(app, email, username, recovery_key), daemon=True)
            _t.start()
        except Exception:
            pass
        flash('Добро пожаловать!', 'success')
        return redirect(url_for('welcome'))
    return render_template('register.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        entered_code = request.form['code']
        if 'reg_code' not in session:
            flash('Сессия истекла.', 'error')
            return redirect(url_for('register'))
        code_time = datetime.fromisoformat(session['reg_code_time'])
        if datetime.utcnow() - code_time > timedelta(minutes=10):
            flash('Код устарел.', 'error')
            return redirect(url_for('register'))
        if entered_code == session['reg_code']:
            user = User(
                username=session['reg_username'],
                email=session['reg_email'],
                password_hash=session['reg_password'],
                recovery_key=session.get('reg_recovery_key', ''),
                terms_agreed=session.get('reg_terms', False),
                privacy_agreed=session.get('reg_privacy', False),
                role=0
            )
            db.session.add(user)
            db.session.commit()
            for k in ('reg_username','reg_email','reg_password','reg_recovery_key',
                      'reg_code','reg_code_time','reg_terms','reg_privacy'):
                session.pop(k, None)
            login_user(user)
            flash('Добро пожаловать!', 'success')
            return redirect(url_for('welcome'))
        else:
            flash('Неверный код', 'error')
            return redirect(url_for('verify'))
    return render_template('verify.html')

@app.route('/resend-code')
def resend_code():
    if 'reg_email' not in session:
        flash('Сессия истекла', 'error')
        return redirect(url_for('register'))
    code = ''.join(random.choices(string.digits, k=6))
    session['reg_code']      = code
    session['reg_code_time'] = datetime.utcnow().isoformat()
    email = session['reg_email']
    print(f"\n📧 НОВЫЙ КОД для {email}: {code}\n")
    if app.config.get('MAIL_USERNAME') and app.config.get('MAIL_PASSWORD'):
        try:
            msg = Message('[Comilank] Новый код подтверждения', recipients=[email])
            msg.body = f'Ваш новый код подтверждения: {code}\n\nДействителен 10 минут.'
            mail.send(msg)
            flash('Новый код отправлен на email', 'success')
        except Exception as e:
            print(f'[MAIL ERROR resend] {e}')
            flash(f'Почта не настроена. Код в консоли сервера.', 'error')
    else:
        flash('Почта не настроена. Код выведен в консоль сервера.', 'success')
    return redirect(url_for('verify'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if user and check_password_hash(user.password_hash, password):
            session.pop('failed_logins', None)
            session.pop('failed_login_user', None)
            login_user(user, remember=True)
            next_page = request.args.get('next') or request.form.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('welcome'))
        # Считаем неудачные попытки
        failed = session.get('failed_logins', 0) + 1
        session['failed_logins'] = failed
        session['failed_login_user'] = username
        if failed >= 3:
            flash('Неверный логин или пароль. Вы можете восстановить аккаунт ключом или через email.', 'error')
        else:
            flash('Неверный логин или пароль. Пожалуйста, попробуйте ещё раз.', 'error')

    show_recovery = session.get('failed_logins', 0) >= 3
    return render_template('login.html', show_recovery=show_recovery)

@app.route('/recover', methods=['GET', 'POST'])
def recover():
    username = request.args.get('username') or request.form.get('username', '')
    error = None
    success = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        recovery_key = request.form.get('recovery_key', '').strip()
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if not user:
            error = 'Пользователь не найден'
        elif not user.recovery_key or user.recovery_key != recovery_key:
            error = 'Неверный ключ восстановления'
        elif len(new_password) < 6:
            error = 'Пароль должен быть не менее 6 символов'
        elif new_password != confirm_password:
            error = 'Пароли не совпадают'
        else:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            session.pop('failed_logins', None)
            session.pop('failed_login_user', None)
            flash('Пароль успешно изменён! Войдите с новым паролем.', 'success')
            return redirect(url_for('login'))
    return render_template('recover.html', username=username, error=error, success=success)

@app.route('/settings/recovery-key', methods=['POST'])
@login_required
def update_recovery_key():
    key = request.form.get('recovery_key', '').strip()
    if len(key) < 6:
        flash('Ключ должен быть не менее 6 символов', 'error')
        return redirect(url_for('profile', username=current_user.username))
    current_user.recovery_key = key
    db.session.commit()
    flash('Ключ восстановления сохранён', 'success')
    return redirect(url_for('profile', username=current_user.username))

@app.route('/admin/users/<int:user_id>/ip-ban', methods=['POST'])
@login_required
def ip_ban_by_profile(user_id):
    if current_user.role < 3:
        abort(403)
    target = User.query.get_or_404(user_id)
    reason = request.form.get('reason', 'Нарушение правил').strip()
    duration = request.form.get('duration', '30d')
    ip = None
    try:
        from sqlalchemy import text as sqlt2
        row = db.session.execute(sqlt2("SELECT ip_address FROM article_views WHERE user_id=:uid AND ip_address IS NOT NULL ORDER BY id DESC LIMIT 1"), {'uid': user_id}).fetchone()
        if row: ip = row[0]
    except Exception: pass
    if not ip:
        try:
            row = db.session.execute(db.text("SELECT ip_address FROM vpn_logs WHERE user_id=:uid AND ip_address IS NOT NULL ORDER BY id DESC LIMIT 1"), {'uid': user_id}).fetchone()
            if row: ip = row[0]
        except Exception: pass
    if not ip:
        flash(f'Не удалось определить IP пользователя {target.username}. Нет данных об активности.', 'error')
        return redirect(url_for('admin_user_profile', user_id=user_id))
    dur_map = {'1h': timedelta(hours=1), '1d': timedelta(days=1), '7d': timedelta(days=7), '30d': timedelta(days=30)}
    td = dur_map.get(duration)
    until = (datetime.utcnow() + td) if td else datetime(9999, 12, 31)
    existing = IPBan.query.filter_by(ip_address=ip).first()
    if existing:
        existing.banned_until = until
        existing.reason = reason
        existing.banned_by_id = current_user.id
    else:
        db.session.add(IPBan(ip_address=ip, banned_until=until, reason=reason, banned_by_id=current_user.id))
    db.session.commit()
    flash(f'IP {ip} пользователя {target.username} заблокирован', 'success')
    return redirect(url_for('admin_user_profile', user_id=user_id))

def _send_reset_code(user):

    import random as _rnd
    code = ''.join([str(_rnd.randint(0, 9)) for _ in range(6)])
    # Инвалидируем старые токены
    PasswordResetToken.query.filter_by(user_id=user.id, used=False).update({'used': True})
    db.session.commit()
    # Сохраняем код как token
    reset = PasswordResetToken(user_id=user.id, token=code)
    db.session.add(reset)
    db.session.commit()
    print(f'\n📧 КОД СБРОСА ПАРОЛЯ для {user.username} ({user.email}): {code}\n')
    try:
        import smtplib as _smtplib, ssl as _ssl
        _server   = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
        _port     = int(app.config.get('MAIL_PORT', 587))
        _user     = app.config.get('MAIL_USERNAME', '')
        _pw       = app.config.get('MAIL_PASSWORD', '')
        _from     = _user
        _to       = user.email
        _subject  = '[Comilank] Код для сброса пароля'
        _body     = (
            f'Привет, {user.username}!\n\n'
            f'Ваш код для сброса пароля:\n\n'
            f'    {code}\n\n'
            f'Код действует 30 минут.\n'
            f'Если вы ничего не запрашивали - проигнорируйте это письмо.\n\n'
            f'- Команда Comilank'
        )
        _raw = (
            f'From: Comilank <{_from}>\r\n'
            f'To: {_to}\r\n'
            f'Subject: {_subject}\r\n'
            f'Content-Type: text/plain; charset=utf-8\r\n'
            f'\r\n'
            f'{_body}'
        ).encode('utf-8')
        _ctx = _ssl.create_default_context()
        with _smtplib.SMTP(_server, _port, timeout=15) as _s:
            _s.ehlo()
            _s.starttls(context=_ctx)
            _s.ehlo()
            _s.login(_user, _pw)
            _s.sendmail(_from, [_to], _raw)
        print(f'[MAIL] ✅ Код отправлен на {user.email}')
        return True, code
    except Exception as e:
        import traceback
        print(f'[MAIL ERROR] {e}')
        print(traceback.format_exc())
        # Код всё равно сохранён в БД — виден в логах
        return False, code

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if not user or not user.email:
            error = 'Пользователь не найден или почта не привязана. Введите реальное имя пользователя.'
            return render_template('forgot_password.html', error=error)
        ok, code = _send_reset_code(user)
        session['reset_username'] = user.username
        session['reset_code_sent_at'] = datetime.utcnow().isoformat()
        session.pop('reset_code_verified', None)
        if not ok:
            session['reset_mail_failed'] = True
        return redirect(url_for('reset_password_code'))
    return render_template('forgot_password.html', error=error)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password_code():
    """Этап 1: ввод кода из письма."""
    username = session.get('reset_username', '')
    error = None
    if not username:
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if not user:
            return redirect(url_for('forgot_password'))
        reset = PasswordResetToken.query.filter_by(
            user_id=user.id, token=code, used=False
        ).order_by(PasswordResetToken.id.desc()).first()
        if not reset or not reset.is_valid():
            error = 'Неверный или устаревший код. Попробуйте получить новый.'
        else:
            # Код верный — сохраняем и переходим к вводу нового пароля
            session['reset_code_verified'] = code
            return redirect(url_for('reset_password_new'))
    return render_template('reset_password_code_only.html', username=username, error=error)

@app.route('/reset-password/new', methods=['GET', 'POST'])
def reset_password_new():
    """Этап 2: ввод нового пароля (только после верного кода)."""
    username = session.get('reset_username', '')
    code     = session.get('reset_code_verified', '')
    error    = None
    if not username or not code:
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        new_pw  = request.form.get('new_password', '')
        conf_pw = request.form.get('confirm_password', '')
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if not user:
            return redirect(url_for('forgot_password'))
        reset = PasswordResetToken.query.filter_by(
            user_id=user.id, token=code, used=False
        ).order_by(PasswordResetToken.id.desc()).first()
        if not reset or not reset.is_valid():
            session.pop('reset_code_verified', None)
            flash('Код устарел. Пожалуйста, запросите новый.', 'error')
            return redirect(url_for('forgot_password'))
        if len(new_pw) < 6:
            error = 'Пароль должен быть не менее 6 символов'
        elif new_pw != conf_pw:
            error = 'Пароли не совпадают'
        else:
            user.password_hash = generate_password_hash(new_pw)
            reset.used = True
            db.session.commit()
            session.pop('reset_username', None)
            session.pop('reset_code_sent_at', None)
            session.pop('reset_code_verified', None)
            flash('Пароль успешно изменён! Войдите с новым паролем.', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html', username=username, error=error)


@app.route('/verify-reset-code', methods=['POST'])
def verify_reset_code_ajax():
    username = session.get('reset_username', '')
    code = request.form.get('code', '').strip()
    if not username or not code:
        return jsonify(ok=False, error='Сессия истекла')
    user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
    if not user:
        return jsonify(ok=False, error='Пользователь не найден')
    reset = PasswordResetToken.query.filter_by(
        user_id=user.id, token=code, used=False
    ).order_by(PasswordResetToken.id.desc()).first()
    if not reset or not reset.is_valid():
        return jsonify(ok=False, error='Неверный или устаревший код')
    return jsonify(ok=True)

@app.route('/forgot-password/resend', methods=['POST'])
def forgot_password_resend():

    username = session.get('reset_username', '')
    if not username:
        return redirect(url_for('forgot_password'))
    user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
    if user and user.email:
        _send_reset_code(user)
        session['reset_code_sent_at'] = datetime.utcnow().isoformat()
        flash('Новый код отправлен на вашу почту.', 'success')
    return redirect(url_for('reset_password_code'))

# Старый маршрут reset-password/<token> - редирект для совместимости
@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    return redirect(url_for('forgot_password'))

@app.route('/report', methods=['POST'])
def submit_report():
    target_type = request.form.get('target_type', '')
    target_id   = request.form.get('target_id', 0, type=int)
    reason      = request.form.get('reason', '').strip()[:500]
    if target_type not in ('comment', 'user') or not target_id:
        return jsonify(ok=False, error='Неверные данные запроса'), 400
    if not reason:
        return jsonify(ok=False, error='Выберите причину жалобы'), 400

    evidence_url = None
    ev_file = request.files.get('evidence_file')
    if ev_file and ev_file.filename:
        is_img   = allowed_file(ev_file.filename)
        is_video = allowed_video_file(ev_file.filename)
        if is_img or is_video:
            data = ev_file.read()
            if len(data) <= 10 * 1024 * 1024:
                ev_file.seek(0)
                filename = f"report_{secrets.token_hex(8)}_{safe_filename(ev_file.filename)}"
                ev_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                evidence_url = f'/static/uploads/{filename}'
    if not evidence_url:
        url_inp = request.form.get('evidence_url_input', '').strip()
        if url_inp and (url_inp.startswith('http://') or url_inp.startswith('https://')):
            evidence_url = url_inp

    reporter_id = current_user.id if current_user.is_authenticated else None

    # Проверка — нельзя жаловаться на свой комментарий
    if target_type == 'comment' and reporter_id:
        try:
            with db.engine.connect() as _c:
                _r = _c.execute(db.text("SELECT author_id FROM comments WHERE id=:cid"), {'cid': target_id}).fetchone()
                if _r and _r[0] == reporter_id:
                    return jsonify(ok=False, error='Нельзя пожаловаться на собственный комментарий'), 400
        except Exception:
            pass

    # comment_article_id — берём из формы или из БД
    comment_article_id = None
    if target_type == 'comment':
        direct_aid = request.form.get('article_id', 0, type=int)
        if direct_aid:
            comment_article_id = direct_aid
        else:
            try:
                with db.engine.connect() as _c:
                    _r2 = _c.execute(db.text("SELECT article_id FROM comments WHERE id=:cid"), {'cid': target_id}).fetchone()
                    if _r2:
                        comment_article_id = _r2[0]
            except Exception:
                pass

    extra_page_id_rep = request.form.get('extra_page_id', 0, type=int) or None

    # Вставляем через raw SQL в отдельном соединении.
    # Пробуем сначала с extra_page_id, если колонки ещё нет — без неё.
    def _do_insert(conn, with_extra):
        if with_extra:
            conn.execute(db.text(
                "INSERT INTO reports (reporter_id, target_type, target_id, reason, "
                "evidence_url, comment_article_id, extra_page_id) "
                "VALUES (:rid, :tt, :tid, :rs, :ev, :caid, :epid)"
            ), {'rid': reporter_id, 'tt': target_type, 'tid': target_id,
                'rs': reason, 'ev': evidence_url,
                'caid': comment_article_id, 'epid': extra_page_id_rep})
        else:
            conn.execute(db.text(
                "INSERT INTO reports (reporter_id, target_type, target_id, reason, "
                "evidence_url, comment_article_id) "
                "VALUES (:rid, :tt, :tid, :rs, :ev, :caid)"
            ), {'rid': reporter_id, 'tt': target_type, 'tid': target_id,
                'rs': reason, 'ev': evidence_url, 'caid': comment_article_id})
        conn.commit()

    try:
        with db.engine.connect() as _conn:
            try:
                _do_insert(_conn, with_extra=True)
            except Exception as _e1:
                _m = str(_e1).lower()
                if 'extra_page_id' in _m or 'column' in _m:
                    try: _conn.rollback()
                    except Exception: pass
                    _do_insert(_conn, with_extra=False)
                else:
                    raise
        return jsonify(ok=True, message='Репорт отправлен. Спасибо!')
    except Exception:
        return jsonify(ok=False, error='Ошибка соединения. Попробуйте ещё раз.'), 500

@app.route('/admin/reports')
@login_required
def admin_reports():
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 3 and not (perm and getattr(perm, 'can_view_reports', False)):
        abort(403)
    tab = request.args.get('tab', 'open')
    open_reports     = Report.query.filter_by(resolved=False)\
                              .order_by(Report.created_at.desc()).all()
    resolved_reports = Report.query.filter_by(resolved=True)\
                              .order_by(Report.resolved_at.desc()).limit(100).all()
    return render_template('admin/reports.html',
                           open_reports=open_reports,
                           resolved_reports=resolved_reports,
                           tab=tab,
                           now=datetime.utcnow())

@app.route('/admin/reports/<int:report_id>/resolve', methods=['POST'])
@login_required
def admin_resolve_report(report_id):
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 3 and not (perm and getattr(perm, 'can_view_reports', False)):
        abort(403)
    r = Report.query.get_or_404(report_id)
    r.resolved = True
    r.resolved_by_id = current_user.id
    r.resolved_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('admin_reports'))

@app.route('/admin/reports/<int:report_id>/reject', methods=['POST'])
@login_required
def admin_reject_report(report_id):
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 3 and not (perm and getattr(perm, 'can_view_reports', False)):
        abort(403)
    r = Report.query.get_or_404(report_id)
    r.resolved = True
    r.rejected = True
    r.resolved_by_id = current_user.id
    r.resolved_at = datetime.utcnow()
    db.session.commit()
    flash('Репорт отклонён как ложный', 'success')
    return redirect(url_for('admin_reports'))

@app.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    import hashlib
    reason   = request.form.get('reason', 'Не указана')
    password = request.form.get('confirm_password', '')
    if not check_password_hash(current_user.password_hash, password):
        flash('Неверный пароль. Аккаунт не удалён.', 'error')
        return redirect(url_for('profile', username=current_user.username))
    try:
        uid          = current_user.id
        email_hash   = hashlib.sha256(current_user.email.encode()).hexdigest()[:32]
        username_bak = current_user.username
        t = db.text
        _sp_idx = [0]

        def _safe(sql, params=None):
            _sp_idx[0] += 1
            sp = f"sp_da_{_sp_idx[0]}"
            try:
                db.session.execute(t(f"SAVEPOINT {sp}"))
                db.session.execute(t(sql), params or {'u': uid})
                db.session.execute(t(f"RELEASE SAVEPOINT {sp}"))
            except Exception:
                try: db.session.execute(t(f"ROLLBACK TO SAVEPOINT {sp}"))
                except Exception: pass

        # Логируем удаление
        log = AccountDeletion(username=username_bak, email_hash=email_hash, reason=reason)
        db.session.add(log)

        # 1. Notifications по comment_id ПЕРЕД удалением комментариев
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT c2.id FROM comments c2 JOIN comments c1 ON c2.parent_id=c1.id WHERE c1.author_id=:u)")
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT id FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u))")

        # 2. Реакции
        _safe("DELETE FROM comment_reactions WHERE user_id=:u")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT c2.id FROM comments c2 JOIN comments c1 ON c2.parent_id=c1.id WHERE c1.author_id=:u)")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u))")

        # 3. Комментарии
        _safe("DELETE FROM comments WHERE parent_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM comments WHERE author_id=:u")
        _safe("DELETE FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")

        # 4. Статьи
        _safe("DELETE FROM votes WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")
        _safe("DELETE FROM article_views WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")
        _safe("DELETE FROM articles WHERE author_id=:u")

        # 5. Прочее
        _safe("DELETE FROM votes WHERE user_id=:u")
        _safe("DELETE FROM article_views WHERE user_id=:u")
        _safe("DELETE FROM penalty_history WHERE user_id=:u OR created_by_id=:u")
        _safe("DELETE FROM mutes WHERE user_id=:u")
        _safe("DELETE FROM user_permissions WHERE user_id=:u")
        _safe("DELETE FROM notifications WHERE user_id=:u")
        _safe("DELETE FROM notifications WHERE actor_id=:u")
        _safe("DELETE FROM password_reset_tokens WHERE user_id=:u")
        _safe("DELETE FROM extra_page_views WHERE user_id=:u")
        _safe("DELETE FROM extra_comment_reactions WHERE comment_id IN (SELECT id FROM extra_page_comments WHERE author_id=:u)")
        _safe("DELETE FROM extra_page_comments WHERE author_id=:u")
        _safe("DELETE FROM battle_invites WHERE from_id=:u OR to_id=:u")
        _safe("DELETE FROM battle_results WHERE winner_id=:u OR loser_id=:u")
        _safe("DELETE FROM reports WHERE reporter_id=:u")
        _safe("DELETE FROM reports WHERE target_type='user' AND target_id=:u")
        _safe("DELETE FROM vpn_logs WHERE user_id=:u")
        _safe("DELETE FROM online_bans WHERE user_id=:u")
        _safe("DELETE FROM poll_votes WHERE user_id=:u")
        _safe("DELETE FROM user_city_share WHERE user_id=:u")

        # 6. Удаляем пользователя
        logout_user()
        db.session.execute(t("DELETE FROM users WHERE id=:u"), {'u': uid})
        db.session.commit()
        flash('Ваш аккаунт был успешно удалён.', 'success')
        return redirect(url_for('index'))
    except Exception as _e:
        db.session.rollback()
        flash(f'Ошибка при удалении аккаунта: {_e}', 'error')
        return redirect(url_for('index'))

@app.route('/terms')
def terms():
    return render_template('legal/terms.html')

@app.route('/privacy')
def privacy_page():
    return render_template('legal/privacy.html')

@app.route('/cookies')
def cookies_page():
    return render_template('legal/cookies.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из аккаунта', 'success')
    return redirect(url_for('index'))

@app.route('/profile/<username>')
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    # Скрываем email - виден только владельцу или старшим администраторам (роль 3+)
    can_see_email = (current_user.is_authenticated and
                     (current_user.id == user.id or current_user.role >= 3))
    return render_template('profile.html', user=user, can_see_email=can_see_email)

@app.route('/settings/password', methods=['POST'])
@login_required
def change_password():
    current_pw = request.form.get('current_password', '')
    new_pw     = request.form.get('new_password', '')
    confirm_pw = request.form.get('confirm_password', '')
    if not check_password_hash(current_user.password_hash, current_pw):
        flash('Неверный текущий пароль', 'error')
        return redirect(url_for('profile', username=current_user.username))
    if len(new_pw) < 6:
        flash('Новый пароль должен быть не менее 6 символов', 'error')
        return redirect(url_for('profile', username=current_user.username))
    if new_pw != confirm_pw:
        flash('Пароли не совпадают', 'error')
        return redirect(url_for('profile', username=current_user.username))
    current_user.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    flash('Пароль успешно изменён', 'success')
    return redirect(url_for('profile', username=current_user.username))

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role < 4: abort(403)
        return f(*args, **kwargs)
    return decorated

def mod_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role < 3: abort(403)
        return f(*args, **kwargs)
    return decorated

def editor_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role < 2: abort(403)
        return f(*args, **kwargs)
    return decorated

@app.route('/admin')
@login_required
@editor_required
def admin_panel():
    from sqlalchemy import func as sqlfunc
    settings = SiteSettings.get()
    # Быстрая статистика для дашборда
    stats = None
    digest_data = {}
    if current_user.role >= 4:
        stats = type('S', (), {
            'articles': Article.query.count(),
            'users':    User.query.count(),
            'comments': Comment.query.count(),
            'views':    db.session.query(sqlfunc.sum(Article.views)).scalar() or 0,
        })()
        # Последние 5 комментариев
        recent_comments = Comment.query.order_by(Comment.created_at.desc()).limit(5).all()
        now = datetime.utcnow()
        def ago(dt):
            if not dt: return '—'
            total = (now - dt).total_seconds()
            if total < 60: return 'только что'
            if total < 3600: return f'{int(total//60)} мин назад'
            if total < 86400: return f'{int(total//3600)} ч назад'
            days = int(total // 86400)
            return f'{days} д назад'
        digest_data['recent_comments'] = [
            {'author': c.author.username if c.author else '?', 'ago': ago(c.created_at)}
            for c in recent_comments
        ]
        # Последние 5 активных пользователей
        recent_users = User.query.order_by(User.last_seen.desc()).limit(5).all()
        digest_data['recent_users'] = [
            {'username': u.username,
             'ago': ago(u.last_seen),
             'online': (now - u.last_seen).total_seconds() < 300}
            for u in recent_users
        ]
    open_reports_count = Report.query.filter(
        (Report.resolved == False) | (Report.resolved == None)
    ).count()
    return render_template('admin/dashboard.html',
                           chaos_button_enabled=settings.chaos_button_enabled,
                           stats=stats,
                           digest_data=digest_data,
                           next_stream_widget=NextStream.query.first(),
                           open_reports_count=open_reports_count)

@app.route('/admin/users')
@login_required
@mod_required
def admin_users():
    users     = User.query.all()
    muted_ids = {m.user_id for m in Mute.query.filter(Mute.muted_until > datetime.utcnow()).all()}
    return render_template('admin/users.html', users=users, muted_ids=muted_ids, now=datetime.utcnow())

@app.route('/admin/users/<int:user_id>/role', methods=['POST'])
@login_required
@admin_required
def change_user_role(user_id):
    new_role    = request.form.get('role', type=int)
    target_user = User.query.get_or_404(user_id)
    # Нельзя менять роль главного админа (role=4)
    if target_user.role == 4 and target_user.id != current_user.id:
        flash('Нельзя изменить роль главного администратора', 'error')
        return redirect(url_for('admin_users'))
    if target_user.id == current_user.id:
        flash('Нельзя изменить свою роль', 'error')
        return redirect(url_for('admin_users'))
    # Только главный админ (role=4) может выдавать роль 4
    if new_role >= 4 and current_user.role < 4:
        flash('Недостаточно прав для выдачи этой роли', 'error')
        return redirect(url_for('admin_users'))
    target_user.role = new_role
    db.session.commit()
    flash(f'Роль {target_user.username} изменена', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/permissions', methods=['POST'])
@login_required
def set_user_permissions(user_id):

    if current_user.role < 4:
        abort(403)
    target = User.query.get_or_404(user_id)
    if target.id == current_user.id:
        flash('Нельзя изменить свои разрешения', 'error')
        return redirect(request.referrer or url_for('admin_users'))

    perm = UserPermission.get_or_create(user_id)
    perm.can_see_stats       = bool(request.form.get('can_see_stats'))
    perm.can_ip_ban          = bool(request.form.get('can_ip_ban'))
    perm.can_create_articles = bool(request.form.get('can_create_articles'))
    perm.can_edit_games      = bool(request.form.get('can_edit_games'))
    perm.can_toggle_chaos    = bool(request.form.get('can_toggle_chaos'))
    perm.can_see_penalty     = bool(request.form.get('can_see_penalty'))
    perm.can_edit_home       = bool(request.form.get('can_edit_home'))
    perm.can_vpn_detect      = bool(request.form.get('can_vpn_detect'))
    perm.can_broadcast       = bool(request.form.get('can_broadcast'))
    perm.can_next_stream     = bool(request.form.get('can_next_stream'))
    perm.can_edit_films      = bool(request.form.get('can_edit_films'))
    perm.can_view_reports    = bool(request.form.get('can_view_reports'))
    perm.can_voice_reply     = bool(request.form.get('can_voice_reply'))
    perm.can_see_test_tab    = bool(request.form.get('can_see_test_tab'))
    perm.granted_by_id       = current_user.id
    perm.granted_at          = datetime.utcnow()
    db.session.commit()
    flash(f'Разрешения {target.username} обновлены', 'success')
    return redirect(request.referrer or url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/ban', methods=['POST'])
@login_required
@mod_required
def ban_user(user_id):
    target_user = User.query.get_or_404(user_id)
    if not current_user.can_ban(target_user):
        flash('Недостаточно прав', 'error')
        return redirect(url_for('admin_users'))
    forever      = request.form.get('forever') == 'on' or request.form.get('duration') == 'perm'
    reason       = request.form.get('reason', '')
    duration_str = request.form.get('duration') if not forever else None
    until = datetime.utcnow() + timedelta(days=365*10) if forever else None
    if not until:
        delta = parse_duration(duration_str)
        if not delta:
            flash('Неверный формат. Пример: 2h, 30m, 7d', 'error')
            return redirect(url_for('admin_users'))
        until = datetime.utcnow() + delta
    target_user.banned_until = until
    target_user.ban_reason   = reason
    target_user.banned_by_id = current_user.id
    db.session.add(PenaltyHistory(user_id=target_user.id, action='ban',
                                  duration=duration_str if not forever else 'forever',
                                  reason=reason, expires_at=until, created_by_id=current_user.id))
    db.session.commit()
    flash(f'{target_user.username} забанен до {until.strftime("%d.%m.%Y %H:%M")}', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/unban', methods=['POST'])
@login_required
@mod_required
def unban_user(user_id):
    target_user = User.query.get_or_404(user_id)
    if not current_user.can_ban(target_user):
        flash('Недостаточно прав', 'error')
        return redirect(url_for('admin_users'))
    target_user.banned_until = target_user.ban_reason = target_user.banned_by_id = None
    db.session.add(PenaltyHistory(user_id=target_user.id, action='unban', created_by_id=current_user.id))
    db.session.commit()
    flash(f'{target_user.username} разбанен', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/mute', methods=['POST'])
@login_required
@mod_required
def mute_user(user_id):
    target_user = User.query.get_or_404(user_id)
    if not current_user.can_mute(target_user):
        flash('Недостаточно прав', 'error')
        return redirect(url_for('admin_users'))
    forever      = request.form.get('forever') == 'on' or request.form.get('duration') == 'perm'
    reason       = request.form.get('reason', '')
    duration_str = request.form.get('duration') if not forever else None
    until = datetime.utcnow() + timedelta(days=365*10) if forever else None
    if not until:
        delta = parse_duration(duration_str)
        if not delta:
            flash('Неверный формат. Пример: 2h, 30m, 7d', 'error')
            return redirect(url_for('admin_users'))
        until = datetime.utcnow() + delta
    db.session.add(Mute(user_id=target_user.id, muted_until=until, reason=reason, muted_by_id=current_user.id))
    db.session.add(PenaltyHistory(user_id=target_user.id, action='mute',
                                  duration=duration_str if not forever else 'forever',
                                  reason=reason, expires_at=until, created_by_id=current_user.id))
    db.session.commit()
    flash(f'{target_user.username} замучен до {until.strftime("%d.%m.%Y %H:%M")}', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/unmute', methods=['POST'])
@login_required
@mod_required
def unmute_user(user_id):
    for m in Mute.query.filter_by(user_id=user_id).filter(Mute.muted_until > datetime.utcnow()).all():
        db.session.delete(m)
    db.session.add(PenaltyHistory(user_id=user_id, action='unmute', created_by_id=current_user.id))
    db.session.commit()
    flash('Мут снят', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/ip_ban', methods=['POST'])
@login_required
@admin_required
def ip_ban():
    ip       = request.form.get('ip')
    duration = request.form.get('duration')
    reason   = request.form.get('reason', '')
    if not ip:
        flash('Не указан IP', 'error')
        return redirect(url_for('admin_users'))
    durations = {'1h': timedelta(hours=1), '1d': timedelta(days=1), 'forever': timedelta(days=365*10)}
    if duration not in durations:
        flash('Неверная длительность', 'error')
        return redirect(url_for('admin_users'))
    until    = datetime.utcnow() + durations[duration]
    existing = IPBan.query.filter_by(ip_address=ip).first()
    if existing:
        existing.banned_until = until; existing.reason = reason
        existing.banned_by_id = current_user.id; existing.created_at = datetime.utcnow()
    else:
        db.session.add(IPBan(ip_address=ip, banned_until=until, reason=reason, banned_by_id=current_user.id))
    db.session.commit()
    flash(f'IP {ip} забанен до {until.strftime("%d.%m.%Y %H:%M")}', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/ip_ban_list')
@login_required
@admin_required
def ip_ban_list():
    return render_template('admin/ip_bans.html', bans=IPBan.query.all())

@app.route('/admin/ip_ban/<int:ban_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_ip_ban(ban_id):
    ban = IPBan.query.get_or_404(ban_id)
    db.session.delete(ban); db.session.commit()
    flash('IP-бан удалён', 'success')
    return redirect(url_for('ip_ban_list'))

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    target_user = User.query.get_or_404(user_id)
    if not current_user.can_delete_user(target_user):
        flash('Недостаточно прав', 'error')
        return redirect(url_for('admin_users'))
    if target_user.id == current_user.id:
        flash('Нельзя удалить себя', 'error')
        return redirect(url_for('admin_users'))
    # Нельзя удалить главного админа (role=4)
    if target_user.role == 4:
        flash('Нельзя удалить главного администратора', 'error')
        return redirect(url_for('admin_users'))
    username_bak = target_user.username
    try:
        uid = user_id
        t = db.text
        _sp_idx = [0]

        def _safe(sql, params=None):
            """Выполняет SQL с уникальным SAVEPOINT — если ошибка, откатывает только этот шаг."""
            _sp_idx[0] += 1
            sp = f"sp_du_{_sp_idx[0]}"
            try:
                db.session.execute(t(f"SAVEPOINT {sp}"))
                db.session.execute(t(sql), params or {'u': uid})
                db.session.execute(t(f"RELEASE SAVEPOINT {sp}"))
            except Exception:
                try: db.session.execute(t(f"ROLLBACK TO SAVEPOINT {sp}"))
                except Exception: pass

        # 1. Notifications по comment_id ПЕРЕД удалением комментариев (FK constraint)
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT c2.id FROM comments c2 JOIN comments c1 ON c2.parent_id=c1.id WHERE c1.author_id=:u)")
        _safe("DELETE FROM notifications WHERE comment_id IN (SELECT id FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u))")

        # 2. Реакции на комментарии
        _safe("DELETE FROM comment_reactions WHERE user_id=:u")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT c2.id FROM comments c2 JOIN comments c1 ON c2.parent_id=c1.id WHERE c1.author_id=:u)")
        _safe("DELETE FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u))")

        # 3. Комментарии
        _safe("DELETE FROM comments WHERE parent_id IN (SELECT id FROM comments WHERE author_id=:u)")
        _safe("DELETE FROM comments WHERE author_id=:u")
        _safe("DELETE FROM comments WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")

        # 4. Статьи
        _safe("DELETE FROM votes WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")
        _safe("DELETE FROM article_views WHERE article_id IN (SELECT id FROM articles WHERE author_id=:u)")
        _safe("DELETE FROM articles WHERE author_id=:u")

        # 5. Прочее
        _safe("DELETE FROM votes WHERE user_id=:u")
        _safe("DELETE FROM article_views WHERE user_id=:u")
        _safe("DELETE FROM penalty_history WHERE user_id=:u OR created_by_id=:u")
        _safe("DELETE FROM mutes WHERE user_id=:u")
        _safe("DELETE FROM user_permissions WHERE user_id=:u")
        _safe("DELETE FROM notifications WHERE user_id=:u")
        _safe("DELETE FROM notifications WHERE actor_id=:u")
        _safe("DELETE FROM password_reset_tokens WHERE user_id=:u")
        _safe("DELETE FROM extra_page_views WHERE user_id=:u")
        _safe("DELETE FROM extra_comment_reactions WHERE comment_id IN (SELECT id FROM extra_page_comments WHERE author_id=:u)")
        _safe("DELETE FROM extra_page_comments WHERE author_id=:u")
        _safe("DELETE FROM battle_invites WHERE from_id=:u OR to_id=:u")
        _safe("DELETE FROM battle_results WHERE winner_id=:u OR loser_id=:u")
        _safe("DELETE FROM reports WHERE reporter_id=:u")
        _safe("DELETE FROM reports WHERE target_type='user' AND target_id=:u")
        _safe("DELETE FROM vpn_logs WHERE user_id=:u")
        _safe("DELETE FROM online_bans WHERE user_id=:u")
        _safe("DELETE FROM poll_votes WHERE user_id=:u")
        _safe("DELETE FROM user_city_share WHERE user_id=:u")
        _safe("DELETE FROM ip_bans WHERE banned_by_id=:u")

        # 6. Удаляем пользователя
        db.session.execute(t("DELETE FROM users WHERE id=:u"), {'u': uid})
        db.session.commit()
        flash(f'{username_bak} удалён', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления: {e}', 'error')
    return redirect(url_for('admin_users'))

@app.route('/admin/user/<int:user_id>')
@login_required
@mod_required
def admin_user_profile(user_id):
    target_user = User.query.get_or_404(user_id)
    history     = PenaltyHistory.query.filter_by(user_id=user_id).order_by(PenaltyHistory.created_at.desc()).all()
    muted_ids   = {m.user_id for m in Mute.query.filter(Mute.muted_until > datetime.utcnow()).all()}
    user_perm   = UserPermission.get_or_create(user_id) if current_user.role >= 4 else None
    return render_template('admin/user_profile.html', user=target_user, history=history,
                           muted_ids=muted_ids, user_perm=user_perm)

@app.route('/admin/upload-image', methods=['POST'])
@login_required
@editor_required
def upload_image():
    file = request.files.get('file') or request.files.get('image')
    if not file or not allowed_file(file.filename):
        return jsonify(error='Недопустимый файл'), 400
    filename = f"content_{secrets.token_hex(8)}_{safe_filename(file.filename)}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return jsonify(url=f'/static/uploads/{filename}')

@app.route('/api/upload-comment-image', methods=['POST'])
@login_required
def upload_comment_image():
    file = request.files.get('file')
    if not file or not allowed_file(file.filename):
        return jsonify(error='Недопустимый файл'), 400
    filename = f"cmt_{secrets.token_hex(8)}_{safe_filename(file.filename)}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return jsonify(url=f'/static/uploads/{filename}')

@app.route('/upload-avatar', methods=['POST'])
@login_required
def upload_avatar():
    file = request.files.get('avatar')
    if not file or not allowed_file(file.filename):
        flash('Недопустимый файл. Разрешены: PNG, JPG, GIF, WEBP', 'error')
        return redirect(url_for('profile', username=current_user.username))
    if len(file.read()) > 5 * 1024 * 1024:
        flash('Файл слишком большой (макс. 5MB)', 'error')
        return redirect(url_for('profile', username=current_user.username))
    file.seek(0)
    # Удаляем старую аватарку если есть
    if current_user.avatar:
        old_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(current_user.avatar))
        try: os.remove(old_path)
        except OSError: pass
    filename = f"avatar_{current_user.id}_{secrets.token_hex(6)}_{safe_filename(file.filename)}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    current_user.avatar = f'/static/uploads/{filename}'
    db.session.commit()
    flash('Аватарка обновлена!', 'success')
    return redirect(url_for('profile', username=current_user.username))

@app.route('/delete-avatar', methods=['POST'])
@login_required
def delete_avatar():
    if current_user.avatar:
        old_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(current_user.avatar))
        try: os.remove(old_path)
        except OSError: pass
        current_user.avatar = None
        db.session.commit()
        flash('Аватарка удалена', 'success')
    return redirect(url_for('profile', username=current_user.username))

@app.route('/admin/stats')
@login_required
def admin_stats():
    # Доступ: role>=4 ИЛИ явное разрешение can_see_stats
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 4 and not (perm and perm.can_see_stats):
        abort(403)
    from sqlalchemy import func as sqlfunc
    total_users    = User.query.count()
    total_articles = Article.query.count()
    total_comments = Comment.query.count()
    total_views    = db.session.query(sqlfunc.sum(Article.views)).scalar() or 0

    roles_data  = db.session.query(User.role, sqlfunc.count(User.id)).group_by(User.role).all()
    roles_map   = {0:'Пользователи',1:'Модераторы чата',2:'Редакторы',3:'Ст. модераторы',4:'Администраторы'}
    roles_stats = [(roles_map.get(r, f'Роль {r}'), cnt) for r, cnt in sorted(roles_data)]

    top_articles = Article.query.order_by(Article.views.desc()).limit(10).all()

    top_commenters = db.session.query(
        User.username, sqlfunc.count(Comment.id).label('cnt')
    ).join(Comment, Comment.author_id == User.id)\
     .group_by(User.id, User.username)\
     .order_by(sqlfunc.count(Comment.id).desc()).limit(10).all()

    week_ago       = datetime.utcnow() - timedelta(days=7)
    new_users_week = User.query.filter(User.created_at >= week_ago).count() if hasattr(User, 'created_at') else 0

    active_bans    = User.query.filter(User.banned_until > datetime.utcnow()).count()
    active_mutes   = Mute.query.filter(Mute.muted_until > datetime.utcnow()).count()
    active_ip_bans = IPBan.query.filter(IPBan.banned_until > datetime.utcnow()).count()

    cat_data  = db.session.query(Article.category, sqlfunc.count(Article.id)).group_by(Article.category).all()
    cat_stats = [(c or 'article', cnt) for c, cnt in cat_data]

    recent_penalties = PenaltyHistory.query.order_by(PenaltyHistory.created_at.desc()).limit(15).all()

    return render_template('admin/stats.html',
        total_users=total_users,
        total_articles=total_articles,
        total_comments=total_comments,
        total_views=total_views,
        roles_stats=roles_stats,
        top_articles=top_articles,
        top_commenters=top_commenters,
        new_users_week=new_users_week,
        active_bans=active_bans,
        active_mutes=active_mutes,
        active_ip_bans=active_ip_bans,
        cat_stats=cat_stats,
        recent_penalties=recent_penalties,
        now=datetime.utcnow(),
    )

@app.route('/forum')
def forum():
    category     = request.args.get('category', 'all')
    q            = Article.query
    if category in ('article', 'news', 'film'):
        q = q.filter_by(category=category)
    articles     = q.order_by(Article.created_at.desc()).all()
    top_comments = Comment.query.filter(Comment.likes > 0).order_by(Comment.likes.desc()).limit(5).all()
    settings     = SiteSettings.get()
    return render_template('forum.html', articles=articles,
                           category=category, top_comments=top_comments,
                           chaos_button_enabled=settings.chaos_button_enabled)

@app.route('/article/<int:article_id>')
def article(article_id):
    art = Article.query.get_or_404(article_id)
    record_article_view(art)
    comments = Comment.query.filter_by(article_id=article_id, parent_id=None)\
                             .order_by(Comment.created_at.desc()).all()
    user_vote = None
    user_comment_reactions = {}
    if current_user.is_authenticated:
        vote = Vote.query.filter_by(user_id=current_user.id, article_id=article_id).first()
        user_vote = vote.value if vote else None
        reacts = CommentReaction.query.filter_by(user_id=current_user.id).all()
        user_comment_reactions = {r.comment_id: r.value for r in reacts}
    return render_template('article.html', article=art, comments=comments,
                           user_vote=user_vote,
                           user_comment_reactions=user_comment_reactions)

@app.route('/article/<int:article_id>/vote', methods=['POST'])
@login_required
def vote_article(article_id):
    art   = Article.query.get_or_404(article_id)
    value = int(request.form.get('value'))
    vote  = Vote.query.filter_by(user_id=current_user.id, article_id=article_id).first()
    if vote:
        if vote.value == value: db.session.delete(vote)
        else: vote.value = value
    else:
        db.session.add(Vote(user_id=current_user.id, article_id=article_id, value=value))
    db.session.commit()
    art.recalc_votes(); db.session.commit()
    user_vote_val = None
    new_vote = Vote.query.filter_by(user_id=current_user.id, article_id=article_id).first()
    if new_vote: user_vote_val = new_vote.value
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(likes=art.likes, dislikes=art.dislikes, user_vote=user_vote_val)
    return redirect(url_for('article', article_id=article_id))

@app.route('/article/<int:article_id>/comment', methods=['POST'])
@login_required
def add_comment(article_id):
    art     = Article.query.get_or_404(article_id)
    content = request.form.get('content', '').strip()
    if not content:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error='empty'), 400
        return redirect(url_for('article', article_id=article_id), 303)
    # Check mute
    from models import Mute as _Mute
    active_mute = _Mute.query.filter_by(user_id=current_user.id).filter(_Mute.muted_until > datetime.utcnow()).first()
    if active_mute:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error='muted'), 403
        flash('Вы заглушены и не можете писать комментарии.', 'error')
        return redirect(url_for('article', article_id=article_id), 303)
    parent_id = request.form.get('parent_id', type=int)
    comment   = Comment(content=content, article_id=article_id,
                        author_id=current_user.id, parent_id=parent_id)
    db.session.add(comment); db.session.commit()
    # Уведомление: если это ответ на комментарий — уведомляем автора родителя
    if parent_id:
        parent_comment = Comment.query.get(parent_id)
        if parent_comment and parent_comment.author_id != current_user.id:
            _create_notification(
                user_id=parent_comment.author_id,
                actor_id=current_user.id,
                notif_type='reply',
                article_id=article_id,
                comment_id=comment.id,
                preview=content[:120]
            )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        del_url = url_for('delete_comment', comment_id=comment.id)
        article_url = url_for('add_comment', article_id=article_id)
        return jsonify(ok=True, id=comment.id,
                       username=current_user.username,
                       avatar=current_user.avatar or '',
                       role=current_user.role,
                       content=content,
                       parent_id=parent_id,
                       is_owner=True,
                       can_delete=True,
                       del_url=del_url,
                       article_url=article_url)
    return redirect(url_for('article', article_id=article_id) + f'#comment-{comment.id}', 303)

@app.route('/comment/<int:comment_id>/react', methods=['POST'])
@login_required
def react_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)
    if comment.author_id == current_user.id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(likes=comment.likes, dislikes=comment.dislikes)
        return redirect(url_for('article', article_id=comment.article_id))
    value   = int(request.form.get('value'))
    react   = CommentReaction.query.filter_by(comment_id=comment_id, user_id=current_user.id).first()
    if react:
        if react.value == value: db.session.delete(react)
        else: react.value = value
    else:
        db.session.add(CommentReaction(comment_id=comment_id, user_id=current_user.id, value=value))
    db.session.commit()
    comment.recalc_reactions(); db.session.commit()
    # Уведомление при лайке (value=1) — только если не дизлайк и не снятие реакции
    if value == 1 and comment.author_id != current_user.id:
        # Проверяем что это не снятие лайка (react был и совпадал — уже удалён)
        still_liked = CommentReaction.query.filter_by(
            comment_id=comment_id, user_id=current_user.id).first()
        if still_liked and still_liked.value == 1:
            art = Article.query.get(comment.article_id)
            _create_notification(
                user_id=comment.author_id,
                actor_id=current_user.id,
                notif_type='like',
                article_id=comment.article_id,
                comment_id=comment_id,
                preview=comment.content[:80] if comment.content else ''
            )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(likes=comment.likes, dislikes=comment.dislikes)
    return redirect(url_for('article', article_id=comment.article_id) + f'#comment-{comment_id}')

@app.route('/comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def delete_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)
    # Только автор или модератор+
    if current_user.id != comment.author_id and current_user.role < 2:
        abort(403)
    article_id = comment.article_id
    from models import Notification
    # Собираем id всех затронутых комментариев (сам + ответы)
    reply_ids = [r.id for r in comment.replies]
    all_ids = reply_ids + [comment_id]
    # 1. Удаляем уведомления, ссылающиеся на эти комментарии (FK constraint)
    Notification.query.filter(Notification.comment_id.in_(all_ids)).delete(synchronize_session=False)
    # 2. Удаляем реакции
    CommentReaction.query.filter_by(comment_id=comment_id).delete()
    for reply in list(comment.replies):
        CommentReaction.query.filter_by(comment_id=reply.id).delete()
        db.session.delete(reply)
    db.session.flush()
    db.session.delete(comment)
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True)
    return redirect(url_for('article', article_id=article_id) + '#comments')

@app.route('/extra/<int:page_id>')
def extra_page(page_id):
    # Откатываем любую незавершённую транзакцию от предыдущих запросов
    try:
        db.session.rollback()
    except Exception:
        pass
    page = ExtraPage.query.get_or_404(page_id)
    try:
        record_extra_page_view(page)
    except Exception:
        pass
    extra_article = None
    try:
        if page.article_id:
            extra_article = Article.query.get(page.article_id)
    except Exception:
        pass
    try:
        rows = db.session.execute(
            db.text("SELECT id,content,created_at,author_id,parent_id,likes,dislikes "
                    "FROM extra_page_comments WHERE page_id=:pid AND parent_id IS NULL "
                    "ORDER BY created_at DESC"),
            {'pid': page_id}
        ).fetchall()
        from models import User as UserModel
        extra_comments = []
        for r in rows:
            author = UserModel.query.get(r[3])
            reply_rows = db.session.execute(
                db.text("SELECT id,content,created_at,author_id,likes,dislikes "
                        "FROM extra_page_comments WHERE parent_id=:pid ORDER BY created_at"),
                {'pid': r[0]}
            ).fetchall()
            from datetime import datetime as _dt
            def _parse_dt(v):
                if isinstance(v, str):
                    try: return _dt.fromisoformat(v.split('.')[0])
                    except: return _dt.utcnow()
                return v if v else _dt.utcnow()
            replies = [{'id':rr[0],'content':rr[1],'created_at':_parse_dt(rr[2]),
                        'author': UserModel.query.get(rr[3]),
                        'likes':rr[4],'dislikes':rr[5]} for rr in reply_rows]
            extra_comments.append({'id':r[0],'content':r[1],'created_at':_parse_dt(r[2]),
                                   'author':author,'likes':r[5],'dislikes':r[6],
                                   'replies':replies})
        user_extra_reactions = {}
        if current_user.is_authenticated:
            reacts = db.session.execute(
                db.text("SELECT comment_id,value FROM extra_comment_reactions WHERE user_id=:uid"),
                {'uid': current_user.id}
            ).fetchall()
            user_extra_reactions = {r[0]: r[1] for r in reacts}
    except Exception:
        extra_comments = []
        user_extra_reactions = {}
    # Лайки самой страницы
    page_likes = 0; page_dislikes = 0; user_page_vote = 0
    try:
        page_likes    = db.session.execute(db.text("SELECT COUNT(*) FROM extra_page_votes WHERE page_id=:pid AND value=1"),  {'pid':page_id}).scalar() or 0
        page_dislikes = db.session.execute(db.text("SELECT COUNT(*) FROM extra_page_votes WHERE page_id=:pid AND value=-1"), {'pid':page_id}).scalar() or 0
        if current_user.is_authenticated:
            rv = db.session.execute(db.text("SELECT value FROM extra_page_votes WHERE page_id=:pid AND user_id=:uid"), {'pid':page_id,'uid':current_user.id}).fetchone()
            user_page_vote = rv[0] if rv else 0
    except Exception:
        pass
    # Получаем can_voice_reply заранее чтобы избежать lazy load в шаблоне
    # после возможной сломанной транзакции
    can_voice_reply = False
    try:
        if current_user.is_authenticated:
            if current_user.role >= 4:
                can_voice_reply = True
            else:
                _perm = UserPermission.query.filter_by(user_id=current_user.id).first()
                can_voice_reply = bool(_perm and getattr(_perm, 'can_voice_reply', False))
    except Exception:
        can_voice_reply = False
    return render_template('extra.html', page=page,
                           extra_article=extra_article,
                           extra_comments=extra_comments,
                           user_extra_reactions=user_extra_reactions,
                           page_likes=page_likes, page_dislikes=page_dislikes,
                           user_page_vote=user_page_vote,
                           can_voice_reply=can_voice_reply)

@app.route('/extra/<int:page_id>/vote', methods=['POST'])
@login_required
def vote_extra_page(page_id):
    ExtraPage.query.get_or_404(page_id)
    value = int(request.form.get('value', 0))
    try:
        existing = db.session.execute(
            db.text("SELECT id,value FROM extra_page_votes WHERE page_id=:pid AND user_id=:uid"),
            {'pid':page_id,'uid':current_user.id}
        ).fetchone()
        if existing:
            if existing[1] == value:
                db.session.execute(db.text("DELETE FROM extra_page_votes WHERE id=:id"), {'id':existing[0]})
            else:
                db.session.execute(db.text("UPDATE extra_page_votes SET value=:v WHERE id=:id"), {'v':value,'id':existing[0]})
        else:
            db.session.execute(db.text("INSERT INTO extra_page_votes (page_id,user_id,value) VALUES (:pid,:uid,:v)"), {'pid':page_id,'uid':current_user.id,'v':value})
        likes    = db.session.execute(db.text("SELECT COUNT(*) FROM extra_page_votes WHERE page_id=:pid AND value=1"),  {'pid':page_id}).scalar()
        dislikes = db.session.execute(db.text("SELECT COUNT(*) FROM extra_page_votes WHERE page_id=:pid AND value=-1"), {'pid':page_id}).scalar()
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(likes=likes, dislikes=dislikes)
    except Exception:
        db.session.rollback()
    return redirect(url_for('extra_page', page_id=page_id))

@app.route('/extra/<int:page_id>/comment', methods=['POST'])
@login_required
def add_extra_comment(page_id):
    import traceback as _tb
    _is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # Полностью сбрасываем сессию перед работой
    try: db.session.remove()
    except Exception: pass

    # Проверка существования страницы
    try:
        page = ExtraPage.query.get(page_id)
    except Exception:
        try: db.session.rollback()
        except Exception: pass
        page = ExtraPage.query.get(page_id)

    if not page:
        if _is_xhr:
            return jsonify(ok=False, error='Страница не найдена'), 404
        return redirect(url_for('extra_page', page_id=page_id))

    # Получаем текст
    content = (request.form.get('content') or '').strip()
    if not content:
        if _is_xhr:
            return jsonify(ok=False, error='empty'), 400
        return redirect(url_for('extra_page', page_id=page_id))

    # Надёжный парсинг parent_id
    parent_id_raw = (request.form.get('parent_id') or '').strip()
    parent_id = None
    if parent_id_raw and parent_id_raw.isdigit():
        parent_id = int(parent_id_raw)

    # Проверка бана
    try:
        if current_user.banned_until and current_user.banned_until > datetime.utcnow():
            if _is_xhr:
                return jsonify(ok=False, error='Вы заблокированы'), 403
            return redirect(url_for('extra_page', page_id=page_id))
    except Exception:
        pass

    # Проверка мута
    try:
        active_mute = Mute.query.filter_by(user_id=current_user.id)\
            .filter(Mute.muted_until > datetime.utcnow()).first()
        if active_mute:
            if _is_xhr:
                return jsonify(ok=False, error='muted'), 403
            return redirect(url_for('extra_page', page_id=page_id))
    except Exception:
        pass

    # ── СПОСОБ 1: через engine.connect() в отдельном соединении ──────────────
    comment_id = None
    _err1 = None
    _is_pg = 'postgresql' in str(db.engine.url) or 'postgres' in str(db.engine.url)
    try:
        with db.engine.connect() as _conn:
            if _is_pg:
                _row = _conn.execute(
                    db.text(
                        "INSERT INTO extra_page_comments (content, page_id, author_id, parent_id) "
                        "VALUES (:c, :pid, :aid, :par) RETURNING id"
                    ),
                    {'c': content, 'pid': page_id, 'aid': current_user.id, 'par': parent_id}
                ).fetchone()
                _conn.commit()
                comment_id = _row[0] if _row else None
            else:
                _conn.execute(
                    db.text(
                        "INSERT INTO extra_page_comments (content, page_id, author_id, parent_id) "
                        "VALUES (:c, :pid, :aid, :par)"
                    ),
                    {'c': content, 'pid': page_id, 'aid': current_user.id, 'par': parent_id}
                )
                _conn.commit()
                _row2 = _conn.execute(
                    db.text(
                        "SELECT id FROM extra_page_comments WHERE page_id=:pid "
                        "AND author_id=:aid ORDER BY id DESC LIMIT 1"
                    ),
                    {'pid': page_id, 'aid': current_user.id}
                ).fetchone()
                comment_id = _row2[0] if _row2 else None
    except Exception as _e1:
        _err1 = _e1
        print(f'[EXTRA CMT engine] {_e1}\n{_tb.format_exc()}')

    # ── СПОСОБ 2: ORM в свежей сессии ─────────────────────────────────────────
    if comment_id is None:
        try:
            db.session.remove()
            from models import ExtraPageComment as _EPC
            _c = _EPC(content=content, page_id=page_id,
                      author_id=current_user.id, parent_id=parent_id)
            db.session.add(_c)
            db.session.commit()
            comment_id = _c.id
        except Exception as _e2:
            try: db.session.rollback()
            except Exception: pass
            print(f'[EXTRA CMT orm] {_e2}\n{_tb.format_exc()}')
            if _is_xhr:
                # Возвращаем реальную ошибку чтобы увидеть причину
                _msg = f'engine: {str(_err1)[:80]} | orm: {str(_e2)[:80]}'
                return jsonify(ok=False, error=_msg), 500
            return redirect(url_for('extra_page', page_id=page_id))

    if comment_id is None:
        if _is_xhr:
            return jsonify(ok=False, error=f'Не удалось получить ID: {str(_err1)[:100]}'), 500
        return redirect(url_for('extra_page', page_id=page_id))

    # ── Уведомление при ответе ─────────────────────────────────────────────────
    if parent_id:
        try:
            _pr = db.session.execute(
                db.text("SELECT author_id FROM extra_page_comments WHERE id=:id"),
                {'id': parent_id}
            ).fetchone()
            if _pr and _pr[0] != current_user.id:
                _create_notification(
                    user_id=_pr[0], actor_id=current_user.id,
                    notif_type='reply_extra', extra_page_id=page_id,
                    comment_id=comment_id, preview=content[:120]
                )
        except Exception:
            pass

    if _is_xhr:
        return jsonify(ok=True, id=comment_id,
                       username=current_user.username,
                       avatar=current_user.avatar or '',
                       role=current_user.role,
                       content=content,
                       parent_id=parent_id,
                       page_id=page_id,
                       is_owner=True,
                       can_delete=True)
    anchor = ('#ec-' + str(parent_id)) if parent_id else ('#ec-' + str(comment_id))
    return redirect(url_for('extra_page', page_id=page_id) + anchor)

@app.route('/extra-comment/<int:comment_id>/react', methods=['POST'])
@login_required
def react_extra_comment(comment_id):
    value = int(request.form.get('value', 0))
    page_id = 0
    try:
        row = db.session.execute(
            db.text("SELECT page_id FROM extra_page_comments WHERE id=:id"),
            {'id': comment_id}
        ).fetchone()
        if row:
            page_id = row[0]
        existing = db.session.execute(
            db.text("SELECT id,value FROM extra_comment_reactions WHERE comment_id=:c AND user_id=:u"),
            {'c': comment_id, 'u': current_user.id}
        ).fetchone()
        if existing:
            if existing[1] == value:
                db.session.execute(db.text("DELETE FROM extra_comment_reactions WHERE id=:id"), {'id': existing[0]})
            else:
                db.session.execute(db.text("UPDATE extra_comment_reactions SET value=:v WHERE id=:id"), {'v': value, 'id': existing[0]})
        else:
            db.session.execute(
                db.text("INSERT INTO extra_comment_reactions (comment_id,user_id,value) VALUES (:c,:u,:v)"),
                {'c': comment_id, 'u': current_user.id, 'v': value}
            )
        likes    = db.session.execute(db.text("SELECT COUNT(*) FROM extra_comment_reactions WHERE comment_id=:c AND value=1"),  {'c': comment_id}).scalar()
        dislikes = db.session.execute(db.text("SELECT COUNT(*) FROM extra_comment_reactions WHERE comment_id=:c AND value=-1"), {'c': comment_id}).scalar()
        db.session.execute(db.text("UPDATE extra_page_comments SET likes=:l,dislikes=:d WHERE id=:id"), {'l': likes, 'd': dislikes, 'id': comment_id})
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(likes=likes, dislikes=dislikes)
    except Exception:
        db.session.rollback()
    return redirect(url_for('extra_page', page_id=page_id) + f'#ec-{comment_id}')

@app.route('/extra-comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def delete_extra_comment(comment_id):
    row = db.session.execute(
        db.text("SELECT page_id,author_id FROM extra_page_comments WHERE id=:id"),
        {'id': comment_id}
    ).fetchone()
    if not row:
        abort(404)
    page_id, author_id = row
    if current_user.id != author_id and current_user.role < 2:
        abort(403)
    try:
        reply_ids = [r[0] for r in db.session.execute(
            db.text("SELECT id FROM extra_page_comments WHERE parent_id=:id"), {'id': comment_id}
        ).fetchall()]
        for rid in reply_ids:
            db.session.execute(db.text("DELETE FROM extra_comment_reactions WHERE comment_id=:id"), {'id': rid})
        if reply_ids:
            db.session.execute(db.text("DELETE FROM extra_page_comments WHERE parent_id=:id"), {'id': comment_id})
        db.session.execute(db.text("DELETE FROM extra_comment_reactions WHERE comment_id=:id"), {'id': comment_id})
        db.session.execute(db.text("DELETE FROM extra_page_comments WHERE id=:id"), {'id': comment_id})
        db.session.commit()
    except Exception:
        db.session.rollback()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True)
    return redirect(url_for('extra_page', page_id=page_id) + '#extra-comments')

@app.route('/admin/articles')
@login_required
@editor_required
def admin_articles():
    articles = Article.query.order_by(Article.created_at.desc()).all()
    return render_template('admin/articles.html', articles=articles)

def _save_article_form(article=None):
    title     = request.form.get('title', '').strip()
    content   = request.form.get('content', '').strip()
    preview   = request.form.get('preview', '').strip()
    image_url = request.form.get('image', '').strip()
    video_url = request.form.get('video_url', '').strip()
    game_id   = request.form.get('game_id', type=int) or None
    category  = request.form.get('category', 'article')
    if category not in ('article', 'news', 'film'):
        category = 'article'
    if not title or not content:
        flash('Заголовок и содержание обязательны', 'error')
        return None
    # Загрузка изображения
    image_file = None
    file = request.files.get('image_file')
    if file and file.filename and allowed_file(file.filename):
        filename   = f"{secrets.token_hex(8)}_{safe_filename(file.filename)}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        image_file = filename
    # Загрузка видео-файла
    video_file = None
    vfile = request.files.get('video_file_upload')
    if vfile and vfile.filename and allowed_video_file(vfile.filename):
        vfilename = f"{secrets.token_hex(8)}_{safe_filename(vfile.filename)}"
        vfile.save(os.path.join(app.config['UPLOAD_FOLDER'], vfilename))
        video_file = vfilename
    if article is None:
        article = Article(author_id=current_user.id)
        db.session.add(article)
    article.title    = title
    article.content  = content
    article.preview  = preview
    article.image    = image_url if image_url else (article.image if article.id else None)
    article.video_url = video_url
    article.game_id  = game_id
    article.category = category
    if image_file:
        article.image_file = image_file
    if video_file:
        article.video_file = video_file
    db.session.commit()
    return article

@app.route('/admin/articles/create', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_article_create():
    if request.method == 'POST':
        art = _save_article_form()
        if art:
            flash('Статья создана!', 'success')
            # Рассылаем push всем подписчикам форума
            try:
                _push_new_forum_article(art)
            except Exception:
                pass
            return redirect(url_for('admin_articles'))
    games = Game.query.order_by(Game.name).all()
    default_category = request.args.get('category', 'article')
    if default_category not in ('article', 'news', 'film'):
        default_category = 'article'
    return render_template('admin/article_edit.html', article=None, games=games, default_category=default_category)

@app.route('/admin/article/<int:article_id>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_article_edit(article_id):
    art = Article.query.get_or_404(article_id)
    # Роль 3 (ст.мод) может редактировать только свои статьи, роль 4 - любые
    if current_user.role == 3 and art.author_id != current_user.id:
        flash('Вы можете редактировать только свои статьи', 'error')
        return redirect(url_for('admin_articles'))
    if request.method == 'POST':
        if _save_article_form(art):
            flash('Статья обновлена!', 'success')
            # Рассылаем push подписчикам статьи об обновлении
            try:
                _push_article_update(art, event_type='update')
            except Exception:
                pass
            return redirect(url_for('admin_articles'))
    games = Game.query.order_by(Game.name).all()
    return render_template('admin/article_edit.html', article=art, games=games)

# Совместимость со старыми ссылками
@app.route('/admin/article/<int:article_id>', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_article_edit_compat(article_id):
    return redirect(url_for('admin_article_edit', article_id=article_id))

@app.route('/admin/article/<int:article_id>/delete', methods=['POST'])
@login_required
@editor_required
def admin_article_delete(article_id):
    art = Article.query.get_or_404(article_id)
    # Роль 3 может удалять только свои статьи
    if current_user.role == 3 and art.author_id != current_user.id:
        flash('Вы можете удалять только свои статьи', 'error')
        return redirect(url_for('admin_articles'))
    if art.image_file:
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], art.image_file))
        except OSError: pass
    # Удаляем все зависимости вручную через raw SQL в правильном порядке
    # чтобы не получить FK violation (notifications_comment_id_fkey и др.)
    _aid = article_id
    def _s(sql, **kw):
        try:
            db.session.execute(db.text(sql), kw)
        except Exception:
            try: db.session.rollback()
            except Exception: pass
    # 1. Уведомления, ссылающиеся на комментарии этой статьи
    _s("DELETE FROM notifications WHERE comment_id IN (SELECT id FROM comments WHERE article_id=:aid)", aid=_aid)
    # 2. Уведомления, ссылающиеся на саму статью
    _s("DELETE FROM notifications WHERE article_id=:aid", aid=_aid)
    # 2b. Подписки на статью
    _s("DELETE FROM article_subscriptions WHERE article_id=:aid", aid=_aid)
    # 3. Реакции на комментарии этой статьи
    _s("DELETE FROM comment_reactions WHERE comment_id IN (SELECT id FROM comments WHERE article_id=:aid)", aid=_aid)
    # 4. Просмотры доп. страниц этой статьи
    _s("DELETE FROM extra_page_views WHERE page_id IN (SELECT id FROM extra_pages WHERE article_id=:aid)", aid=_aid)
    # 5. Реакции на комментарии доп. страниц этой статьи
    _s("DELETE FROM extra_comment_reactions WHERE comment_id IN (SELECT id FROM extra_page_comments WHERE page_id IN (SELECT id FROM extra_pages WHERE article_id=:aid))", aid=_aid)
    # 6. Комментарии доп. страниц (ответы первыми из-за self-FK)
    _s("DELETE FROM extra_page_comments WHERE parent_id IN (SELECT id FROM extra_page_comments WHERE page_id IN (SELECT id FROM extra_pages WHERE article_id=:aid))", aid=_aid)
    _s("DELETE FROM extra_page_comments WHERE page_id IN (SELECT id FROM extra_pages WHERE article_id=:aid)", aid=_aid)
    db.session.flush()
    # Теперь безопасно удаляем статью — ORM cascade уберёт comments, votes, extra_pages и т.д.
    db.session.delete(art)
    db.session.commit()
    flash('Статья удалена', 'success')
    return redirect(url_for('admin_articles'))

@app.route('/admin/extra/new', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_extra_create():
    if request.method == 'POST':
        ep = ExtraPage(title=request.form.get('title'),
                       content=request.form.get('content'),
                       article_id=request.form.get('article_id', type=int),
                       author_id=current_user.id)
        db.session.add(ep)
        db.session.commit()
        # Рассылаем push подписчикам родительской статьи
        try:
            parent_art = Article.query.get(ep.article_id)
            if parent_art:
                _push_article_update(parent_art, event_type='extra', extra_title=ep.title)
        except Exception:
            pass
        flash('Страница создана', 'success')
        return redirect(url_for('admin_articles'))
    return render_template('admin/extra_edit.html', page=None,
                           articles=Article.query.order_by(Article.title).all())

@app.route('/admin/extra/<int:page_id>', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_extra_edit(page_id):
    page = ExtraPage.query.get_or_404(page_id)
    # Роль 3 может редактировать только свои допы
    if current_user.role == 3 and page.author_id != current_user.id:
        flash('Вы можете редактировать только свои страницы', 'error')
        return redirect(url_for('admin_articles'))
    if request.method == 'POST':
        page.title = request.form.get('title')
        page.content = request.form.get('content')
        page.article_id = request.form.get('article_id', type=int)
        db.session.commit()
        flash('Страница обновлена', 'success')
        return redirect(url_for('admin_articles'))
    return render_template('admin/extra_edit.html', page=page,
                           articles=Article.query.order_by(Article.title).all())

@app.route('/admin/extra/<int:page_id>/delete', methods=['POST'])
@login_required
@editor_required
def admin_extra_delete(page_id):
    page = ExtraPage.query.get_or_404(page_id)
    # Роль 3 может удалять только свои допы
    if current_user.role == 3 and page.author_id != current_user.id:
        flash('Вы можете удалять только свои страницы', 'error')
        return redirect(url_for('admin_articles'))
    db.session.delete(page); db.session.commit()
    flash('Страница удалена', 'success')
    return redirect(url_for('admin_articles'))

@app.route('/admin/article/<int:article_id>/dropdowns')
@login_required
@editor_required
def admin_dropdown_list(article_id):
    art       = Article.query.get_or_404(article_id)
    dropdowns = DropdownItem.query.filter_by(article_id=article_id).order_by(DropdownItem.order).all()
    return render_template('admin/dropdown_items.html', article=art, dropdowns=dropdowns)

@app.route('/admin/article/<int:article_id>/dropdown/new', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_dropdown_create(article_id):
    art = Article.query.get_or_404(article_id)
    if request.method == 'POST':
        max_order = db.session.query(func.max(DropdownItem.order)).filter_by(article_id=article_id).scalar() or 0
        db.session.add(DropdownItem(title=request.form.get('title'), article_id=article_id,
                                    page_id=request.form.get('page_id', type=int) or None,
                                    order=max_order+1, is_active=request.form.get('is_active') == 'on'))
        db.session.commit()
        flash('Пункт добавлен', 'success')
        return redirect(url_for('admin_dropdown_list', article_id=article_id))
    extra_pages = ExtraPage.query.filter_by(article_id=article_id).all()
    return render_template('admin/dropdown_edit.html', article=art, item=None, extra_pages=extra_pages)

@app.route('/admin/dropdown/<int:item_id>', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_dropdown_edit(item_id):
    item = DropdownItem.query.get_or_404(item_id)
    if request.method == 'POST':
        item.title     = request.form.get('title')
        item.page_id   = request.form.get('page_id', type=int) or None
        item.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('Пункт обновлён', 'success')
        return redirect(url_for('admin_dropdown_list', article_id=item.article_id))
    extra_pages = ExtraPage.query.filter_by(article_id=item.article_id).all()
    return render_template('admin/dropdown_edit.html', article=item.article, item=item, extra_pages=extra_pages)

@app.route('/admin/dropdown/<int:item_id>/delete', methods=['POST'])
@login_required
@editor_required
def admin_dropdown_delete(item_id):
    item = DropdownItem.query.get_or_404(item_id)
    article_id = item.article_id
    db.session.delete(item); db.session.commit()
    flash('Пункт удалён', 'success')
    return redirect(url_for('admin_dropdown_list', article_id=article_id))

@app.route('/admin/dropdown/<int:item_id>/toggle', methods=['POST'])
@login_required
@editor_required
def admin_dropdown_toggle(item_id):
    item = DropdownItem.query.get_or_404(item_id)
    item.is_active = not item.is_active
    db.session.commit()
    flash(f'Пункт {"активирован" if item.is_active else "деактивирован"}', 'success')
    return redirect(url_for('admin_dropdown_list', article_id=item.article_id))

@app.route('/admin/dropdown/<int:item_id>/move/<direction>')
@login_required
@editor_required
def admin_dropdown_move(item_id, direction):
    item       = DropdownItem.query.get_or_404(item_id)
    article_id = item.article_id
    if direction == 'up':
        neighbor = DropdownItem.query.filter_by(article_id=article_id)\
                               .filter(DropdownItem.order < item.order)\
                               .order_by(DropdownItem.order.desc()).first()
    elif direction == 'down':
        neighbor = DropdownItem.query.filter_by(article_id=article_id)\
                               .filter(DropdownItem.order > item.order)\
                               .order_by(DropdownItem.order.asc()).first()
    else:
        abort(400)
    if neighbor:
        item.order, neighbor.order = neighbor.order, item.order
        db.session.commit()
    return redirect(url_for('admin_dropdown_list', article_id=article_id))

@app.route('/admin/games')
@login_required
@editor_required
def admin_games():
    return render_template('admin/games.html', games=Game.query.order_by(Game.name).all())

@app.route('/admin/games/create', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_game_create():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Название обязательно', 'error')
        else:
            game = Game(name=name)
            file = request.files.get('image_file')
            if file and file.filename and allowed_file(file.filename):
                filename = f"game_{secrets.token_hex(6)}_{safe_filename(file.filename)}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                game.image = f'/static/uploads/{filename}'
            elif request.form.get('image_url'):
                game.image = request.form.get('image_url')
            db.session.add(game); db.session.commit()
            flash(f'Игра «{name}» добавлена', 'success')
            return redirect(url_for('admin_games'))
    return render_template('admin/game_edit.html', game=None)

@app.route('/admin/games/<int:game_id>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_game_edit(game_id):
    game = Game.query.get_or_404(game_id)
    if request.method == 'POST':
        game.name = request.form.get('name', '').strip()
        file = request.files.get('image_file')
        if file and file.filename and allowed_file(file.filename):
            filename = f"game_{secrets.token_hex(6)}_{safe_filename(file.filename)}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            game.image = f'/static/uploads/{filename}'
        elif request.form.get('image_url'):
            game.image = request.form.get('image_url')
        db.session.commit()
        flash('Игра обновлена', 'success')
        return redirect(url_for('admin_games'))
    return render_template('admin/game_edit.html', game=game)

@app.route('/admin/games/<int:game_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_game_delete(game_id):
    game = Game.query.get_or_404(game_id)
    db.session.delete(game); db.session.commit()
    flash('Игра удалена', 'success')
    return redirect(url_for('admin_games'))

#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ГЛАВНОЙ

def _init_stream_platforms():

    try:
        if StreamPlatform.query.count() == 0:
            _defaults = [
                ('YouTube',  'youtube',  'fab fa-youtube',  '#ff0000', 'https://youtube.com/@comilank?si=chTQSh7wKdOonUxL'),
                ('Twitch',   'twitch',   'fab fa-twitch',   '#9146ff', 'https://www.twitch.tv/comilank_game'),
                ('TikTok',   'tiktok',   'fab fa-tiktok',   '#ffffff', 'https://www.tiktok.com/@comilank'),
                ('Telegram', 'telegram', 'fab fa-telegram', '#2aabee', 'https://t.me/Comilank'),
            ]
            for name, key, icon, color, ch_url in _defaults:
                db.session.add(StreamPlatform(name=name, key=key, icon_class=icon,
                                              color=color, channel_url=ch_url))
            db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/admin/vpn-logs')
@login_required
def admin_vpn_logs():

    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 4 and not (perm and getattr(perm, 'can_vpn_detect', False)):
        return jsonify(logs=[]), 403
    # Простейший журнал из логов Flask (или можно хранить в БД)
    # Здесь возвращаем пустой список - функция расширяется при наличии VPN-таблицы
    try:
        from sqlalchemy import text as sqlt
        rows = db.session.execute(sqlt(
            "SELECT u.username, v.ip_address, v.vpn_type, v.country, v.detected_at "
            "FROM vpn_logs v LEFT JOIN users u ON u.id=v.user_id "
            "ORDER BY v.detected_at DESC LIMIT 100"
        )).fetchall()
        logs = [{'username': r[0], 'ip': r[1], 'vpn_type': r[2],
                 'country': r[3], 'detected_at': str(r[4])} for r in rows]
    except Exception:
        logs = []
    return jsonify(logs=logs)

def _home_edit_required(f):

    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        perm = UserPermission.query.filter_by(user_id=current_user.id).first()
        if current_user.role < 2 and not (perm and getattr(perm, 'can_edit_home', False)):
            abort(403)
        return f(*args, **kwargs)
    return wrapper

#  ГОЛОСОВАНИЕ «ВО ЧТО ИГРАЕМ»

@app.route('/vote-next-game', methods=['POST'])
@login_required
def vote_next_game():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    game_id = request.form.get('game_id', type=int)
    if not game_id:
        if is_ajax: return jsonify(ok=False, error='no_game')
        return redirect(url_for('index'))
    game = PollGame.query.get_or_404(game_id)
    poll = NextGamePoll.query.get(game.poll_id)
    if not poll or not poll.active:
        if is_ajax: return jsonify(ok=False, error='closed')
        flash('Голосование завершено', 'error')
        return redirect(url_for('index'))
    existing = PollVote.query.filter_by(poll_id=poll.id, user_id=current_user.id).first()
    if existing:
        if existing.game_id != game_id:
            old_game = PollGame.query.get(existing.game_id)
            if old_game:
                old_game.votes = max(0, old_game.votes - 1)
            existing.game_id = game_id
            game.votes += 1
    else:
        vote = PollVote(poll_id=poll.id, user_id=current_user.id, game_id=game_id)
        db.session.add(vote)
        game.votes += 1
    db.session.commit()
    _cache_bust('index_heavy')
    if is_ajax:
        return jsonify(ok=True, game_id=game_id, votes=game.votes)
    flash('Голос учтён!', 'success')
    return redirect(url_for('index'))

#  АДМИН: НАСТРОЙКИ ГЛАВНОЙ СТРАНИЦЫ

@app.route('/admin/home-settings')
@login_required
def admin_home_settings():
    # Доступ: role >= 2 ИЛИ явное разрешение can_edit_home
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 2 and not (perm and perm.can_edit_home):
        abort(403)
    _init_stream_platforms()
    stream_platforms = StreamPlatform.query.order_by(StreamPlatform.id).all()
    top_viewers      = TopViewer.query.order_by(TopViewer.position).all()
    top_donators     = TopDonator.query.order_by(TopDonator.position).all()
    last_stream      = LastStream.query.order_by(LastStream.id.desc()).first()
    stream_moments   = StreamMoment.query.order_by(StreamMoment.position).all()
    next_poll        = NextGamePoll.query.order_by(NextGamePoll.id.desc()).first()
    next_stream_widget = NextStream.query.first()
    return render_template('admin/home_settings.html',
        stream_platforms=stream_platforms,
        top_viewers=top_viewers,
        top_donators=top_donators,
        last_stream=last_stream,
        stream_moments=stream_moments,
        next_poll=next_poll,
        next_stream_widget=next_stream_widget,
    )

@app.route('/admin/home/platforms', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_platforms():
    platforms = StreamPlatform.query.all()
    for p in platforms:
        p.stream_url  = request.form.get(f'stream_url_{p.key}', '').strip()
        p.channel_url = request.form.get(f'channel_url_{p.key}', '').strip()
        p.is_live     = bool(request.form.get(f'is_live_{p.key}'))
    db.session.commit()
    flash('Платформы обновлены', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/viewer/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_add():
    max_slots = 4 if current_user.role >= 4 else 3
    if TopViewer.query.count() >= max_slots:
        flash(f'Максимум {max_slots} зрителей', 'error')
        if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
        return redirect(url_for('admin_home_settings'))
    pos = (db.session.query(func.max(TopViewer.position)).scalar() or 0) + 1
    xp_val = int(request.form.get('xp', 0) or 0)
    v = TopViewer(
        name=request.form['name'].strip(),
        messages=int(request.form.get('messages', 0) or 0),
        show_messages=bool(request.form.get('show_messages')),
        position=pos,
        xp=xp_val,
    )
    db.session.add(v)
    db.session.commit()
    flash('Зритель добавлен', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/viewer/<int:vid>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_delete(vid):
    db.session.delete(TopViewer.query.get_or_404(vid))
    db.session.commit()
    flash('Зритель удалён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/viewer/<int:vid>/edit', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_edit(vid):
    v = TopViewer.query.get_or_404(vid)
    v.name          = request.form.get('name', v.name).strip() or v.name
    v.messages      = int(request.form.get('messages', v.messages) or 0)
    v.show_messages = bool(request.form.get('show_messages'))
    v.xp            = int(request.form.get('xp', 0) or 0)
    db.session.commit()
    flash('Зритель обновлён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/viewer/<int:vid>/move/<direction>', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_move(vid, direction):
    viewers = TopViewer.query.order_by(TopViewer.position).all()
    idx = next((i for i, v in enumerate(viewers) if v.id == vid), None)
    if idx is None:
        abort(404)
    if direction == 'up' and idx > 0:
        viewers[idx].position, viewers[idx-1].position = viewers[idx-1].position, viewers[idx].position
    elif direction == 'down' and idx < len(viewers) - 1:
        viewers[idx].position, viewers[idx+1].position = viewers[idx+1].position, viewers[idx].position
    db.session.commit()
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/donator/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_donator_add():
    if TopDonator.query.count() >= 3:
        flash('Максимум 3 донатера', 'error')
        if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
        return redirect(url_for('admin_home_settings'))
    pos = (db.session.query(func.max(TopDonator.position)).scalar() or 0) + 1
    d = TopDonator(
        name=request.form['name'].strip(),
        amount=int(request.form.get('amount', 0) or 0),
        position=pos,
    )
    db.session.add(d)
    db.session.commit()
    flash('Донатер добавлен', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/donator/<int:did>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_donator_delete(did):
    db.session.delete(TopDonator.query.get_or_404(did))
    db.session.commit()
    flash('Донатер удалён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/donator/<int:did>/move/<direction>', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_donator_move(did, direction):
    donators = TopDonator.query.order_by(TopDonator.position).all()
    idx = next((i for i, d in enumerate(donators) if d.id == did), None)
    if idx is None:
        abort(404)
    if direction == 'up' and idx > 0:
        donators[idx].position, donators[idx-1].position = donators[idx-1].position, donators[idx].position
    elif direction == 'down' and idx < len(donators) - 1:
        donators[idx].position, donators[idx+1].position = donators[idx+1].position, donators[idx].position
    db.session.commit()
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/last-stream', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_last_stream():
    ls = LastStream.query.order_by(LastStream.id.desc()).first()
    if not ls:
        ls = LastStream()
        db.session.add(ls)
    ls.title      = request.form.get('title', '').strip()
    ls.url        = request.form.get('url', '').strip()
    ls.views      = request.form.get('views', '').strip()
    ls.yt_url     = request.form.get('yt_url', '').strip()
    ls.twitch_url = request.form.get('twitch_url', '').strip()
    ls.tiktok_url = request.form.get('tiktok_url', '').strip()
    # Превью: файл приоритетнее URL
    thumb_file = request.files.get('thumbnail_file')
    if thumb_file and thumb_file.filename and allowed_file(thumb_file.filename):
        filename = f"stream_{secrets.token_hex(6)}_{safe_filename(thumb_file.filename)}"
        thumb_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        ls.thumbnail_url = f'/static/uploads/{filename}'
    else:
        url_val = request.form.get('thumbnail_url', '').strip()
        if url_val:
            ls.thumbnail_url = url_val
    db.session.commit()
    flash('Последний стрим обновлён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/next-stream', methods=['POST'])
@login_required
def admin_home_next_stream():
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 4 and not (perm and getattr(perm, 'can_next_stream', False)):
        abort(403)
    ns = NextStream.query.first()
    if not ns:
        ns = NextStream()
        db.session.add(ns)
    ns.enabled     = bool(request.form.get('enabled'))
    ns.title       = request.form.get('title', '').strip()
    ns.stream_dt   = request.form.get('stream_dt', '').strip()
    ns.description = request.form.get('description', '').strip()
    try:
        preview_url = request.form.get('preview_url', '').strip()
        preview_file = request.files.get('preview_file')
        if preview_file and preview_file.filename and allowed_file(preview_file.filename):
            filename = f"nsw_{secrets.token_hex(6)}_{safe_filename(preview_file.filename)}"
            preview_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            preview_url = f'/static/uploads/{filename}'
        ns.preview_url = preview_url
    except Exception:
        pass  # поле preview_url появится после миграции модели
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Виджет стрима обновлён', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/home/moment/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_add():
    pos = (db.session.query(func.max(StreamMoment.position)).scalar() or 0) + 1
    thumb_url = request.form.get('thumbnail_url', '').strip()
    thumb_file = request.files.get('thumbnail_file')
    if thumb_file and thumb_file.filename and allowed_file(thumb_file.filename):
        filename = f"moment_{secrets.token_hex(6)}_{safe_filename(thumb_file.filename)}"
        thumb_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        thumb_url = f'/static/uploads/{filename}'
    m = StreamMoment(
        title=request.form['title'].strip(),
        url=request.form['url'].strip(),
        thumbnail_url=thumb_url,
        views=request.form.get('views', '').strip(),
        game=request.form.get('game', '').strip(),
        position=pos,
    )
    db.session.add(m)
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Момент добавлен', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/moment/bulk', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_bulk():

    files = request.files.getlist('bulk_files')
    urls_raw = request.form.get('bulk_urls', '').strip()
    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
    pos = (db.session.query(func.max(StreamMoment.position)).scalar() or 0) + 1
    added = 0
    # Загрузка файлов
    for i, f in enumerate(files):
        if f and f.filename and allowed_file(f.filename):
            filename = f"moment_{secrets.token_hex(6)}_{safe_filename(f.filename)}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            thumb_url = f'/static/uploads/{filename}'
            title = f.filename.rsplit('.', 1)[0]
            url = urls[i] if i < len(urls) else '#'
            m = StreamMoment(title=title, url=url, thumbnail_url=thumb_url, position=pos + added)
            db.session.add(m)
            added += 1
    db.session.commit()
    _cache_bust('index_heavy')
    flash(f'Добавлено {added} моментов', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/moment/<int:mid>/edit', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_edit(mid):

    m = StreamMoment.query.get_or_404(mid)
    m.title = request.form.get('title', '').strip() or m.title
    m.url   = request.form.get('url', '').strip() or m.url
    m.views = request.form.get('views', '').strip()
    m.game  = request.form.get('game', '').strip()
    thumb_file = request.files.get('thumbnail_file')
    if thumb_file and thumb_file.filename and allowed_file(thumb_file.filename):
        filename = f"moment_{secrets.token_hex(6)}_{safe_filename(thumb_file.filename)}"
        thumb_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        m.thumbnail_url = f'/static/uploads/{filename}'
    else:
        new_thumb = request.form.get('thumbnail_url', '').strip()
        if new_thumb:
            m.thumbnail_url = new_thumb
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Момент обновлён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/moment/<int:mid>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_delete(mid):
    db.session.delete(StreamMoment.query.get_or_404(mid))
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Момент удалён', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/moment/<int:mid>/move/<direction>', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_move(mid, direction):
    moments = StreamMoment.query.order_by(StreamMoment.position).all()
    idx = next((i for i, m in enumerate(moments) if m.id == mid), None)
    if idx is None:
        abort(404)
    if direction == 'up' and idx > 0:
        moments[idx].position, moments[idx-1].position = moments[idx-1].position, moments[idx].position
    elif direction == 'down' and idx < len(moments) - 1:
        moments[idx].position, moments[idx+1].position = moments[idx+1].position, moments[idx].position
    db.session.commit()
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/poll/game/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_poll_game_add():
    poll = NextGamePoll.query.filter_by(active=True).order_by(NextGamePoll.id.desc()).first()
    if not poll:
        poll = NextGamePoll(active=True)
        db.session.add(poll)
        db.session.flush()
    pos = (db.session.query(func.max(PollGame.position)).filter_by(poll_id=poll.id).scalar() or 0) + 1
    # Поддержка загрузки файла или URL
    image_url = request.form.get('game_image', '').strip()
    img_file = request.files.get('game_image_file')
    if img_file and img_file.filename and allowed_file(img_file.filename):
        filename = f"poll_{secrets.token_hex(6)}_{safe_filename(img_file.filename)}"
        img_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        image_url = f'/static/uploads/{filename}'
    g = PollGame(
        poll_id=poll.id,
        name=request.form['game_name'].strip(),
        image_url=image_url,
        position=pos,
    )
    db.session.add(g)
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Игра добавлена в опрос', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/poll/game/<int:gid>/delete', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_game_delete(gid):
    game = PollGame.query.get_or_404(gid)
    PollVote.query.filter_by(game_id=gid).delete()
    db.session.delete(game)
    db.session.commit()
    _cache_bust('index_heavy')
    flash('Игра удалена из опроса', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/poll/end', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_end():

    poll = NextGamePoll.query.filter_by(active=True).order_by(NextGamePoll.id.desc()).first()
    if poll:
        poll.active = False
        db.session.commit()
        flash('Голосование завершено', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/admin/home/poll/reset', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_reset():

    NextGamePoll.query.update({'active': False})
    db.session.commit()
    flash('Старый опрос закрыт. Добавь игры для нового.', 'success')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest': return jsonify(ok=True)
    return redirect(url_for('admin_home_settings'))

@app.route('/sitemap.xml')
def sitemap():

    from flask import Response
    base = request.host_url.rstrip('/')
    pages = []
    for route, priority, freq in [
        ('/',       '1.0', 'daily'),
        ('/forum',  '0.9', 'daily'),
    ]:
        pages.append({
            'loc':        base + route,
            'priority':   priority,
            'changefreq': freq,
            'lastmod':    datetime.utcnow().strftime('%Y-%m-%d'),
        })
    for art in Article.query.order_by(Article.created_at.desc()).all():
        pages.append({
            'loc':        base + f'/article/{art.id}',
            'priority':   '0.8',
            'changefreq': 'weekly',
            'lastmod':    (art.updated_at or art.created_at).strftime('%Y-%m-%d'),
        })
    for page in ExtraPage.query.all():
        pages.append({
            'loc':        base + f'/extra/{page.id}',
            'priority':   '0.6',
            'changefreq': 'monthly',
            'lastmod':    page.created_at.strftime('%Y-%m-%d') if hasattr(page, 'created_at') and page.created_at else datetime.utcnow().strftime('%Y-%m-%d'),
        })
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in pages:
        xml_lines.append(f"""  <url>
    <loc>{p['loc']}</loc>
    <lastmod>{p['lastmod']}</lastmod>
    <changefreq>{p['changefreq']}</changefreq>
    <priority>{p['priority']}</priority>
  </url>""")
    xml_lines.append('</urlset>')
    return Response('\n'.join(xml_lines), mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    """robots.txt - разрешаем Google индексировать всё, кроме админки."""
    from flask import Response
    base = request.host_url.rstrip('/')
    txt = f"""User-agent: *
Allow: /
Disallow: /admin/
Disallow: /login
Disallow: /register
Disallow: /logout

Sitemap: {base}/sitemap.xml
"""
    return Response(txt, mimetype='text/plain')

def _run_migration():

    _is_pg = 'postgresql' in str(db.engine.url) or 'postgres' in str(db.engine.url)
    _alters = [
        ("users",    "recovery_key",   "VARCHAR(256)"),
        ("users",    "avatar",         "VARCHAR(300)"),
        ("articles", "video_file",     "VARCHAR(300)"),
        ("users",    "banned_by_id",   "INTEGER"),
        ("extra_page_comments", "parent_id", "INTEGER"),
        ("extra_page_comments", "likes",     "INTEGER DEFAULT 0"),
        ("extra_page_comments", "dislikes",  "INTEGER DEFAULT 0"),
        ("user_permissions", "can_edit_home",    "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_vpn_detect",   "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_broadcast",    "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_next_stream",  "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_edit_films",   "BOOLEAN DEFAULT FALSE"),
        ("reports",          "evidence_url",     "VARCHAR(500)"),
        ("user_permissions", "can_view_reports", "BOOLEAN DEFAULT FALSE"),
        ("reports",          "rejected",         "BOOLEAN DEFAULT FALSE"),
        ("reports",          "comment_article_id", "INTEGER"),
        ("reports",          "reporter_id",      "INTEGER"),
        ("reports",          "extra_page_id",    "INTEGER"),
        ("users",    "terms_agreed",   "BOOLEAN DEFAULT FALSE"),
        ("users",    "privacy_agreed", "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_voice_reply",  "BOOLEAN DEFAULT FALSE"),
        ("user_permissions", "can_see_test_tab", "BOOLEAN DEFAULT FALSE"),
        # Новые колонки для battle_invites (выбор карты/персонажа)
        ("battle_invites", "from_card",      "VARCHAR(50) DEFAULT ''"),
        ("battle_invites", "from_character", "VARCHAR(50) DEFAULT ''"),
        ("battle_invites", "to_character",   "VARCHAR(50) DEFAULT ''"),
        # Новая колонка для уведомлений (extra page)
        ("notifications", "extra_page_id", "INTEGER"),
        # XP для топ зрителей
        ("top_viewers", "xp", "INTEGER DEFAULT 0"),
    ]
    _is_sq = not _is_pg
    _idc   = 'INTEGER PRIMARY KEY' if _is_sq else 'SERIAL PRIMARY KEY'
    _new_tables = [
        f"""CREATE TABLE IF NOT EXISTS weather_city (id {_idc}, name VARCHAR(100) NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, tz VARCHAR(60) NOT NULL, landmark VARCHAR(150) DEFAULT '', position INTEGER DEFAULT 0, is_active BOOLEAN DEFAULT TRUE)""",
        f"""CREATE TABLE IF NOT EXISTS user_city_share (id {_idc}, user_id INTEGER, city_name VARCHAR(150) NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, tz VARCHAR(60) DEFAULT '', shared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ip_hash VARCHAR(64) DEFAULT '')""",
        f"""CREATE TABLE IF NOT EXISTS reports (id {_idc}, reporter_id INTEGER, target_type VARCHAR(20) NOT NULL, target_id INTEGER NOT NULL, reason VARCHAR(500) DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, resolved BOOLEAN DEFAULT FALSE, resolved_by_id INTEGER, resolved_at TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS password_reset_tokens (id {_idc}, user_id INTEGER NOT NULL, token VARCHAR(64) UNIQUE NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, used BOOLEAN DEFAULT FALSE)""",
        f"""CREATE TABLE IF NOT EXISTS account_deletions (id {_idc}, username VARCHAR(80), email_hash VARCHAR(64), reason VARCHAR(100), deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS site_settings (id {_idc}, chaos_button_enabled BOOLEAN DEFAULT FALSE)""",
        f"""CREATE TABLE IF NOT EXISTS extra_page_views (id {_idc}, page_id INTEGER NOT NULL, user_id INTEGER, ip_address VARCHAR(45), viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS mini_game_config (id INTEGER PRIMARY KEY, name VARCHAR(200) DEFAULT 'ШЕФ-БОЕЦ', avatar_url VARCHAR(500) DEFAULT '')""",
        f"""CREATE TABLE IF NOT EXISTS battle_invites (id {_idc}, from_id INTEGER NOT NULL, to_id INTEGER NOT NULL, room_id VARCHAR(32) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, accepted BOOLEAN DEFAULT FALSE, from_card VARCHAR(50) DEFAULT '', from_character VARCHAR(50) DEFAULT '', to_character VARCHAR(50) DEFAULT '')""",

        f"""CREATE TABLE IF NOT EXISTS battle_results (id {_idc}, winner_id INTEGER NOT NULL, loser_id INTEGER NOT NULL, room_id VARCHAR(32) NOT NULL, played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS game_state (room_id VARCHAR(32) PRIMARY KEY, p1_state TEXT DEFAULT '{{}}', p2_state TEXT DEFAULT '{{}}', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS online_bans (user_id INTEGER PRIMARY KEY, ban_until TIMESTAMP NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS extra_page_comments (id {_idc}, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, page_id INTEGER NOT NULL, author_id INTEGER NOT NULL, parent_id INTEGER, likes INTEGER DEFAULT 0, dislikes INTEGER DEFAULT 0)""",
        f"""CREATE TABLE IF NOT EXISTS extra_comment_reactions (id {_idc}, comment_id INTEGER NOT NULL, user_id INTEGER NOT NULL, value INTEGER NOT NULL)""",
    ]
    try:
        with db.engine.connect() as conn:
            for table, col, typ in _alters:
                try:
                    if _is_pg:
                        conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}"))
                    else:
                        conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
                    conn.commit()
                except Exception as _e:
                    _m = str(_e).lower()
                    if 'duplicate' in _m or 'already exists' in _m or 'no such table' in _m:
                        pass
                    try: conn.rollback()
                    except Exception: pass
            for _sql in _new_tables:
                try:
                    conn.execute(db.text(_sql))
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
    except Exception as _ex:
        print(f"[MIGRATION WARNING] {_ex}")

# Запускаем миграцию для Gunicorn/Render (здесь _run_migration уже определена)
with app.app_context():
    _startup()

# ══════════════════════════════════════════════════════════════
#  ИГРА ШЕФ-БОЕЦ — маршруты и API
# ══════════════════════════════════════════════════════════════

@app.route('/game/')
def serve_game():
    """Отдаёт index.html игры из static/game/"""
    return send_from_directory(os.path.join('static', 'game'), 'index.html')

@app.route('/game/<path:filename>')
def serve_game_assets(filename):
    """Отдаёт ассеты игры (sprites, music, src и т.д.)"""
    return send_from_directory(os.path.join('static', 'game'), filename)

@app.route('/api/mini-game-config')
def api_mini_game_config():
    """Возвращает название и аватарку игры для отображения на главной."""
    try:
        row = db.session.execute(db.text(
            "SELECT name, avatar_url FROM mini_game_config WHERE id=1"
        )).fetchone()
        if row:
            return jsonify(name=row[0], avatar_url=row[1] or '')
    except Exception:
        pass
    return jsonify(name='ШЕФ-БОЕЦ', avatar_url='')

@app.route('/api/mini-game-config/update', methods=['POST'])
@login_required
def api_mini_game_config_update():
    """Обновляет название и аватарку игры (только admin role>=4)."""
    if current_user.role < 4:
        return jsonify(ok=False, error='Нет прав'), 403
    name = request.form.get('name', '').strip() or 'ШЕФ-БОЕЦ'
    avatar_url = ''
    file = request.files.get('avatar_file')
    if file and file.filename and allowed_file(file.filename):
        filename = f"minigame_avatar_{secrets.token_hex(6)}_{safe_filename(file.filename)}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        avatar_url = f'/static/uploads/{filename}'
    else:
        avatar_url = request.form.get('avatar_url', '').strip()
    try:
        existing = db.session.execute(db.text(
            "SELECT id FROM mini_game_config WHERE id=1"
        )).fetchone()
        if existing:
            db.session.execute(db.text(
                "UPDATE mini_game_config SET name=:n, avatar_url=:a WHERE id=1"
            ), {'n': name, 'a': avatar_url})
        else:
            db.session.execute(db.text(
                "INSERT INTO mini_game_config (id, name, avatar_url) VALUES (1,:n,:a)"
            ), {'n': name, 'a': avatar_url})
        db.session.commit()
        return jsonify(ok=True, name=name, avatar_url=avatar_url)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/online-users')
def api_game_online_users():
    """Список онлайн-пользователей (last_seen < 5 мин) для лобби 1vs1."""
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    users = User.query.filter(User.last_seen >= cutoff).all()
    result = []
    for u in users:
        if current_user.is_authenticated and u.id == current_user.id:
            continue
        result.append({'id': u.id, 'username': u.username, 'avatar': u.avatar or ''})
    return jsonify(users=result, total=len(result))

@app.route('/api/game/battle-invite', methods=['POST'])
@login_required
def api_game_battle_invite():
    """Создаёт приглашение на бой 1vs1."""
    data = request.get_json(silent=True, force=True) or {}
    target_id = data.get('target_id')
    from_card = (data.get('from_card') or '').strip()[:50]
    from_character = (data.get('from_character') or '').strip()[:50]
    if not target_id:
        return jsonify(ok=False, error='target_id required'), 400
    target = User.query.get(target_id)
    if not target:
        return jsonify(ok=False, error='Пользователь не найден'), 404
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    if not target.last_seen or target.last_seen < cutoff:
        return jsonify(ok=False, error='Пользователь не в сети'), 409
    room_id = secrets.token_hex(8)
    try:
        db.session.execute(db.text(
            "INSERT INTO battle_invites (from_id, to_id, room_id, created_at, accepted, from_card, from_character, to_character) "
            "VALUES (:f, :t, :r, :dt, FALSE, :fc, :fch, '')"
        ), {'f': current_user.id, 't': target_id, 'r': room_id, 'dt': datetime.utcnow(),
            'fc': from_card, 'fch': from_character})
        db.session.commit()
        # Уведомление — вызов на бой
        _create_notification(
            user_id=target_id,
            actor_id=current_user.id,
            notif_type='game_invite',
            preview=f'вызывает вас на бой в игре!'
        )
        return jsonify(ok=True, room_id=room_id,
                       from_user=current_user.username, to_user=target.username)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/battle-check')
@login_required
def api_game_battle_check():
    """Polling: проверить есть ли непринятый вызов для текущего пользователя."""
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=30)
        row = db.session.execute(db.text(
            "SELECT bi.id, bi.room_id, u.username, bi.from_card, bi.from_character "
            "FROM battle_invites bi "
            "JOIN users u ON u.id = bi.from_id "
            "WHERE bi.to_id=:uid AND bi.accepted=FALSE AND bi.created_at > :cut "
            "ORDER BY bi.created_at DESC LIMIT 1"
        ), {'uid': current_user.id, 'cut': cutoff}).fetchone()
        if row:
            return jsonify(invite=True, invite_id=row[0],
                           room_id=row[1], from_user=row[2],
                           from_card=row[3] or '', from_character=row[4] or '')
    except Exception:
        pass
    return jsonify(invite=False)

@app.route('/api/game/battle-respond', methods=['POST'])
@login_required
def api_game_battle_respond():
    """Принять или отклонить вызов на бой."""
    data = request.get_json(silent=True, force=True) or {}
    invite_id = data.get('invite_id')
    accept = data.get('accept', False)
    to_character = (data.get('to_character') or '').strip()[:50]
    if not invite_id:
        return jsonify(ok=False), 400
    try:
        if accept:
            db.session.execute(db.text(
                "UPDATE battle_invites SET accepted=TRUE, to_character=:tch WHERE id=:id AND to_id=:uid"
            ), {'id': invite_id, 'uid': current_user.id, 'tch': to_character})
        else:
            db.session.execute(db.text(
                "DELETE FROM battle_invites WHERE id=:id AND to_id=:uid"
            ), {'id': invite_id, 'uid': current_user.id})
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/battle-status/<room_id>')
@login_required
def api_game_battle_status(room_id):
    """Инициатор проверяет — принял ли враг приглашение."""
    try:
        row = db.session.execute(db.text(
            "SELECT accepted, to_id, to_character FROM battle_invites WHERE room_id=:r AND from_id=:uid"
        ), {'r': room_id, 'uid': current_user.id}).fetchone()
        if row:
            return jsonify(accepted=bool(row[0]), opponent_id=row[1],
                           to_character=row[2] or '')
    except Exception:
        pass
    return jsonify(accepted=False)

@app.route('/api/game/battle-result', methods=['POST'])
@login_required
def api_game_battle_result():
    """Записываем результат онлайн-боя. Вызывается от победителя."""
    data = request.get_json(silent=True, force=True) or {}
    room_id = data.get('room_id', '')
    loser_id = data.get('loser_id')
    if not room_id or not loser_id:
        return jsonify(ok=False), 400
    try:
        # Проверяем что room существует и участники верные
        row = db.session.execute(db.text(
            "SELECT from_id, to_id FROM battle_invites WHERE room_id=:r AND accepted=TRUE"
        ), {'r': room_id}).fetchone()
        if not row:
            return jsonify(ok=False, error='room not found'), 404
        participants = {row[0], row[1]}
        if current_user.id not in participants or int(loser_id) not in participants:
            return jsonify(ok=False, error='not a participant'), 403
        # Не дублируем запись
        existing = db.session.execute(db.text(
            "SELECT id FROM battle_results WHERE room_id=:r"
        ), {'r': room_id}).fetchone()
        if not existing:
            db.session.execute(db.text(
                "INSERT INTO battle_results (winner_id, loser_id, room_id, played_at) VALUES (:w, :l, :r, :dt)"
            ), {'w': current_user.id, 'l': int(loser_id), 'r': room_id, 'dt': datetime.utcnow()})
            db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/state/push', methods=['POST'])
@login_required
def api_game_state_push():
    """Игрок шлёт своё состояние (позиция, HP, действия)."""
    data = request.get_json(silent=True, force=True) or {}
    room_id = data.get('room_id', '')
    role    = data.get('role', '')  # 'p1' или 'p2'
    state   = data.get('state', {})
    if not room_id or role not in ('p1','p2'):
        return jsonify(ok=False), 400
    import json as _json
    state_json = _json.dumps(state)[:4000]  # max 4KB
    col = 'p1_state' if role == 'p1' else 'p2_state'
    try:
        db.session.execute(db.text(
            f"INSERT INTO game_state (room_id, {col}, updated_at) VALUES (:r, :s, :t) "
            f"ON CONFLICT (room_id) DO UPDATE SET {col}=:s, updated_at=:t"
        ), {'r': room_id, 's': state_json, 't': datetime.utcnow()})
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/state/pull/<room_id>/<role>')
@login_required
def api_game_state_pull(room_id, role):
    """Игрок получает состояние ПРОТИВНИКА."""
    import json as _json
    opp_col = 'p2_state' if role == 'p1' else 'p1_state'
    try:
        row = db.session.execute(db.text(
            f"SELECT {opp_col}, updated_at FROM game_state WHERE room_id=:r"
        ), {'r': room_id}).fetchone()
        if not row:
            return jsonify(ok=False, state=None)
        try:
            state = _json.loads(row[0] or '{}')
        except Exception:
            state = {}
        return jsonify(ok=True, state=state, ts=str(row[1]))
    except Exception:
        return jsonify(ok=False, state=None)

@app.route('/api/game/my-stats')
@login_required
def api_game_my_stats():
    """Статистика побед/поражений текущего пользователя."""
    try:
        wins = db.session.execute(db.text(
            "SELECT COUNT(*) FROM battle_results WHERE winner_id=:uid"
        ), {'uid': current_user.id}).scalar() or 0
        losses = db.session.execute(db.text(
            "SELECT COUNT(*) FROM battle_results WHERE loser_id=:uid"
        ), {'uid': current_user.id}).scalar() or 0
        return jsonify(wins=wins, losses=losses, total=wins+losses)
    except Exception:
        return jsonify(wins=0, losses=0, total=0)

@app.route('/api/game/forfeit', methods=['POST'])
@login_required
def api_game_forfeit():
    """Игрок сдаётся или закрывает вкладку во время боя.
    Победа засчитывается противнику, форфейтер получает бан онлайна на 5 минут."""
    data = request.get_json(silent=True, force=True) or {}
    room_id = data.get('room_id', '')
    role    = data.get('role', '')   # 'p1' или 'p2'
    if not room_id:
        return jsonify(ok=False, error='no room_id'), 400
    try:
        row = db.session.execute(db.text(
            "SELECT from_id, to_id FROM battle_invites WHERE room_id=:r AND accepted=TRUE"
        ), {'r': room_id}).fetchone()
        if row:
            p1_id, p2_id = row[0], row[1]
            loser_id  = current_user.id
            winner_id = p2_id if loser_id == p1_id else p1_id
            # Записываем результат если ещё нет
            existing = db.session.execute(db.text(
                "SELECT id FROM battle_results WHERE room_id=:r"
            ), {'r': room_id}).fetchone()
            if not existing:
                db.session.execute(db.text(
                    "INSERT INTO battle_results (winner_id, loser_id, room_id, played_at) "
                    "VALUES (:w, :l, :r, :dt)"
                ), {'w': winner_id, 'l': loser_id, 'r': room_id, 'dt': datetime.utcnow()})
        # Бан на онлайн на 5 минут — сохраняем в таблице
        ban_until = datetime.utcnow() + timedelta(minutes=5)
        db.session.execute(db.text(
            "INSERT INTO online_bans (user_id, ban_until) VALUES (:uid, :bu) "
            "ON CONFLICT (user_id) DO UPDATE SET ban_until=:bu"
        ), {'uid': current_user.id, 'bu': ban_until})
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/game/check-ban')
@login_required
def api_game_check_ban():
    """Проверяет, забанен ли текущий пользователь от онлайна."""
    try:
        row = db.session.execute(db.text(
            "SELECT ban_until FROM online_bans WHERE user_id=:uid"
        ), {'uid': current_user.id}).fetchone()
        if row:
            ban_until = row[0]
            if isinstance(ban_until, str):
                ban_until = datetime.fromisoformat(ban_until)
            if ban_until > datetime.utcnow():
                secs = int((ban_until - datetime.utcnow()).total_seconds())
                return jsonify(banned=True, seconds_left=secs)
        return jsonify(banned=False)
    except Exception:
        return jsonify(banned=False)



# ═══════════════════════════════════════════════════════════════════
#  МЕССЕНДЖЕР — маршруты
# ═══════════════════════════════════════════════════════════════════

@app.route('/messenger')
@app.route('/messenger/<int:chat_id>')
@login_required
def messenger(chat_id=None):
    """Главная страница мессенджера."""
    memberships = ChatMember.query.filter_by(user_id=current_user.id).all()
    chat_ids = [m.chat_id for m in memberships]
    chats = MessengerChat.query.filter(MessengerChat.id.in_(chat_ids)).all() if chat_ids else []
    chats.sort(
        key=lambda c: c.last_message.created_at if c.last_message else c.created_at,
        reverse=True
    )
    active_chat = None
    messages = []
    if chat_id:
        active_chat = MessengerChat.query.get_or_404(chat_id)
        if not active_chat.is_member(current_user):
            abort(403)
        messages = ChatMessage.query.filter_by(chat_id=chat_id).order_by(ChatMessage.id.asc()).all()
        member = ChatMember.query.filter_by(chat_id=chat_id, user_id=current_user.id).first()
        if member:
            member.last_read_at = datetime.utcnow()
            # Отмечаем чужие сообщения как прочитанные
            ChatMessage.query.filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.author_id != current_user.id,
                ChatMessage.is_read == False
            ).update({'is_read': True})
            db.session.commit()
    resp = make_response(render_template('messenger.html',
                           chats=chats,
                           active_chat=active_chat,
                           messages=messages,
                           now=datetime.utcnow()))
    # Сбрасываем cookie редиректа чтобы следующий переход на / шёл на главную
    resp.set_cookie('_rp', '', expires=0, path='/')
    return resp


@app.route('/api/messenger/send', methods=['POST'])
@login_required
def api_messenger_send():
    data = request.get_json(silent=True) or {}
    chat_id     = data.get('chat_id')
    text        = (data.get('text') or '').strip()
    reply_to_id = data.get('reply_to_id')
    if not chat_id or not text:
        return jsonify(ok=False, error='Пустое сообщение'), 400
    chat = MessengerChat.query.get_or_404(chat_id)
    if not chat.is_member(current_user):
        return jsonify(ok=False, error='Нет доступа'), 403
    if len(text) > 4000:
        return jsonify(ok=False, error='Слишком длинное сообщение'), 400
    msg = ChatMessage(
        chat_id=chat_id,
        author_id=current_user.id,
        text=text,
        reply_to_id=reply_to_id if reply_to_id else None
    )
    db.session.add(msg)
    # Сбрасываем статус "печатает"
    TypingStatus.query.filter_by(chat_id=chat_id, user_id=current_user.id).delete()
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/messenger/edit', methods=['POST'])
@login_required
def api_messenger_edit():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    text   = (data.get('text') or '').strip()
    if not msg_id or not text:
        return jsonify(ok=False), 400
    msg = ChatMessage.query.get_or_404(msg_id)
    if msg.author_id != current_user.id:
        return jsonify(ok=False, error='Нет доступа'), 403
    msg.text   = text
    msg.edited = True
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/messenger/delete', methods=['POST'])
@login_required
def api_messenger_delete():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify(ok=False), 400
    msg = ChatMessage.query.get_or_404(msg_id)
    if msg.author_id != current_user.id and current_user.role < 2:
        return jsonify(ok=False, error='Нет доступа'), 403
    db.session.delete(msg)
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/messenger/upload', methods=['POST'])
@login_required
def api_messenger_upload():
    chat_id     = request.form.get('chat_id', type=int)
    ftype       = request.form.get('type', 'file')
    reply_to_id = request.form.get('reply_to_id', type=int)
    if not chat_id:
        return jsonify(ok=False, error='Нет chat_id'), 400
    chat = MessengerChat.query.get_or_404(chat_id)
    if not chat.is_member(current_user):
        return jsonify(ok=False, error='Нет доступа'), 403
    f = request.files.get('file')
    if not f:
        return jsonify(ok=False, error='Нет файла'), 400
    filename    = safe_filename(f.filename)
    folder      = os.path.join(app.config['UPLOAD_FOLDER'], 'messenger')
    os.makedirs(folder, exist_ok=True)
    unique_name = f'{secrets.token_hex(8)}_{filename}'
    path        = os.path.join(folder, unique_name)
    f.save(path)
    rel = f'messenger/{unique_name}'
    msg = ChatMessage(
        chat_id=chat_id,
        author_id=current_user.id,
        reply_to_id=reply_to_id
    )
    if ftype == 'image':
        msg.image_url = rel
    else:
        msg.file_url  = rel
        msg.file_name = f.filename
        msg.file_size = os.path.getsize(path)
    db.session.add(msg)
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/messenger/poll')
@login_required
def api_messenger_poll():
    chat_id = request.args.get('chat_id', type=int)
    after   = request.args.get('after',   type=int, default=0)
    if not chat_id:
        return jsonify(messages=[], typing=[])
    chat = MessengerChat.query.get(chat_id)
    if not chat or not chat.is_member(current_user):
        return jsonify(messages=[], typing=[])
    new_msgs = ChatMessage.query.filter(
        ChatMessage.chat_id == chat_id,
        ChatMessage.id > after
    ).order_by(ChatMessage.id.asc()).limit(50).all()
    if new_msgs:
        member = ChatMember.query.filter_by(chat_id=chat_id, user_id=current_user.id).first()
        if member:
            member.last_read_at = datetime.utcnow()
        ChatMessage.query.filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.author_id != current_user.id,
            ChatMessage.is_read == False
        ).update({'is_read': True})
        db.session.commit()
    threshold = datetime.utcnow() - timedelta(seconds=5)
    typing_rows = TypingStatus.query.filter(
        TypingStatus.chat_id == chat_id,
        TypingStatus.user_id != current_user.id,
        TypingStatus.ts > threshold
    ).all()
    typing_names = []
    for t in typing_rows:
        u = User.query.get(t.user_id)
        if u:
            typing_names.append(u.username)
    return jsonify(
        messages=[m.to_dict() for m in new_msgs],
        typing=typing_names
    )


@app.route('/api/messenger/read', methods=['POST'])
@login_required
def api_messenger_read():
    data    = request.get_json(silent=True) or {}
    chat_id = data.get('chat_id') or request.args.get('chat_id', type=int)
    if not chat_id:
        return jsonify(ok=False), 400
    member = ChatMember.query.filter_by(chat_id=chat_id, user_id=current_user.id).first()
    if member:
        member.last_read_at = datetime.utcnow()
        ChatMessage.query.filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.author_id != current_user.id,
            ChatMessage.is_read == False
        ).update({'is_read': True})
        db.session.commit()
    return jsonify(ok=True)


@app.route('/api/messenger/typing', methods=['POST'])
@login_required
def api_messenger_typing():
    data    = request.get_json(silent=True) or {}
    chat_id = data.get('chat_id') or request.args.get('chat_id', type=int)
    if not chat_id:
        return jsonify(ok=False), 400
    ts = TypingStatus.query.filter_by(chat_id=chat_id, user_id=current_user.id).first()
    if ts:
        ts.ts = datetime.utcnow()
    else:
        ts = TypingStatus(chat_id=chat_id, user_id=current_user.id)
        db.session.add(ts)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True)


@app.route('/api/messenger/react', methods=['POST'])
@login_required
def api_messenger_react():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    emoji  = data.get('emoji', '')
    if not msg_id or not emoji:
        return jsonify(ok=False), 400
    msg  = ChatMessage.query.get_or_404(msg_id)
    chat = MessengerChat.query.get(msg.chat_id)
    if not chat or not chat.is_member(current_user):
        return jsonify(ok=False, error='Нет доступа'), 403
    existing = MsgReaction.query.filter_by(
        msg_id=msg_id, user_id=current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(MsgReaction(msg_id=msg_id, user_id=current_user.id, emoji=emoji))
    db.session.commit()
    return jsonify(ok=True, reactions=msg.reactions_grouped())


@app.route('/api/messenger/pin', methods=['POST'])
@login_required
def api_messenger_pin():
    data    = request.get_json(silent=True) or {}
    chat_id = data.get('chat_id')
    msg_id  = data.get('msg_id')
    if not chat_id or not msg_id:
        return jsonify(ok=False), 400
    chat = MessengerChat.query.get_or_404(chat_id)
    if not chat.is_member(current_user):
        return jsonify(ok=False, error='Нет доступа'), 403
    chat.pinned_msg_id = msg_id
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/messenger/pinned')
@login_required
def api_messenger_pinned():
    chat_id = request.args.get('chat_id', type=int)
    if not chat_id:
        return jsonify(text=None)
    chat = MessengerChat.query.get(chat_id)
    if not chat or not chat.pinned_msg_id:
        return jsonify(text=None)
    msg = ChatMessage.query.get(chat.pinned_msg_id)
    if not msg:
        return jsonify(text=None)
    return jsonify(text=msg.text or '[медиафайл]', msg_id=msg.id)


@app.route('/api/messenger/new-chat', methods=['POST'])
@login_required
def api_messenger_new_chat():
    data       = request.get_json(silent=True) or {}
    user_id    = data.get('user_id')
    is_group   = data.get('is_group', False)
    group_name = (data.get('group_name') or '').strip()
    if not user_id:
        return jsonify(ok=False, error='Не указан пользователь'), 400
    target = User.query.get(user_id)
    if not target:
        return jsonify(ok=False, error='Пользователь не найден'), 404
    if target.id == current_user.id and not is_group:
        return jsonify(ok=False, error='Нельзя создать личный чат с собой'), 400
    if not is_group:
        # Ищем существующий личный чат
        my_chat_ids = [m.chat_id for m in
                       ChatMember.query.filter_by(user_id=current_user.id).all()]
        their_chat_ids = [m.chat_id for m in
                          ChatMember.query.filter_by(user_id=target.id).all()]
        common = set(my_chat_ids) & set(their_chat_ids)
        for cid in common:
            c = MessengerChat.query.get(cid)
            if c and not c.is_group:
                return jsonify(ok=True, chat_id=cid)
    chat = MessengerChat(
        is_group=is_group,
        name=group_name if is_group else None
    )
    db.session.add(chat)
    db.session.flush()
    # Add creator as admin
    db.session.add(ChatMember(chat_id=chat.id, user_id=current_user.id, is_admin=True))
    # For groups: add target only if different from creator
    if not is_group or target.id != current_user.id:
        # Check not already added
        existing_m = ChatMember.query.filter_by(chat_id=chat.id, user_id=target.id).first()
        if not existing_m:
            db.session.add(ChatMember(chat_id=chat.id, user_id=target.id))
    # Add extra member_ids if provided (group creation with multiple members)
    member_ids = data.get('member_ids', [])
    for mid in member_ids:
        if mid and mid != current_user.id:
            existing_m = ChatMember.query.filter_by(chat_id=chat.id, user_id=mid).first()
            if not existing_m:
                u_extra = User.query.get(mid)
                if u_extra:
                    db.session.add(ChatMember(chat_id=chat.id, user_id=mid))
    db.session.commit()
    return jsonify(ok=True, chat_id=chat.id)


@app.route('/api/messenger/search-users')
@login_required
def api_messenger_search_users():
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify(users=[])
    users = User.query.filter(
        User.username.ilike(f'%{q}%'),
        User.id != current_user.id
    ).limit(10).all()
    return jsonify(users=[{
        'id':       u.id,
        'username': u.username,
        'avatar':   u.avatar
    } for u in users])



# ═══════════════════════════════════════════════════════════════════
#  КОМНАТЫ И КАНАЛЫ
# ═══════════════════════════════════════════════════════════════════

ROOM_CATEGORIES = [
    ('games',    '🎮', 'Игры'),
    ('music',    '🎵', 'Музыка'),
    ('sports',   '⚽', 'Спорт'),
    ('anime',    '🌸', 'Аниме'),
    ('art',      '🎨', 'Арт'),
    ('tech',     '💻', 'Технологии'),
    ('cinema',   '🎬', 'Кино'),
    ('general',  '💬', 'Общение'),
    ('humor',    '😂', 'Юмор'),
    ('news',     '📰', 'Новости'),
]


def _cat_label(key):
    for k, icon, label in ROOM_CATEGORIES:
        if k == key:
            return f'{icon} {label}'
    return key


@app.route('/chat')
@app.route('/chat/<path:subpath>')
def chat_index(subpath=''):
    """Единый SPA чат — каталог + комнаты + заявки + админка."""
    rooms = Room.query.filter_by(is_active=True, room_type='room').order_by(Room.id.desc()).all()
    channels = Room.query.filter_by(is_active=True, room_type='channel').order_by(Room.id.desc()).all()
    featured_rooms = Room.query.filter_by(is_active=True, is_featured=True).order_by(Room.id.desc()).all()
    my_rooms, my_apps, pending_apps, all_rooms_admin, join_reqs = [], [], [], [], []
    active_room, active_member, room_messages = None, None, []

    if current_user.is_authenticated:
        my_ids   = [m.room_id for m in current_user.room_memberships.all()]
        my_rooms = Room.query.filter(Room.id.in_(my_ids), Room.is_active == True).order_by(Room.id.desc()).all() if my_ids else []
        my_apps  = current_user.room_applications.order_by(RoomApplication.created_at.desc()).limit(10).all()

        # If subpath is a room slug
        if subpath and not subpath.startswith('admin') and not subpath.startswith('apply') and not subpath.startswith('my') and not subpath.startswith('rooms') and not subpath.startswith('channels'):
            slug = subpath.split('?')[0].strip('/')
            active_room = Room.query.filter_by(slug=slug, is_active=True).first()
            if active_room:
                active_member = active_room.get_member(current_user)
                if not active_member:
                    active_member = RoomMember(room_id=active_room.id, user_id=current_user.id, role='member')
                    db.session.add(active_member)
                    db.session.add(RoomMessage(room_id=active_room.id, author_id=None,
                        text=f'{current_user.username} вступил в комнату', msg_type='system'))
                    db.session.commit()
                elif active_member.is_banned:
                    active_room = None; active_member = None
                if active_room and active_member:
                    room_messages = RoomMessage.query.filter_by(room_id=active_room.id, deleted=False)                                               .order_by(RoomMessage.id.asc()).limit(120).all()
                    active_member.last_read_at = datetime.utcnow()
                    db.session.commit()

        if current_user.role >= 3:
            pending_apps   = RoomApplication.query.filter_by(status='pending').order_by(RoomApplication.created_at.asc()).all()
            all_rooms_admin= Room.query.order_by(Room.created_at.desc()).all()

    return render_template('chat.html',
        subpath=subpath,
        rooms=rooms, channels=channels,
        featured_rooms=featured_rooms,
        my_rooms=my_rooms, my_apps=my_apps,
        active_room=active_room, active_member=active_member,
        room_messages=room_messages,
        pending_apps=pending_apps, all_rooms_admin=all_rooms_admin,
        categories=ROOM_CATEGORIES, cat_label=_cat_label,
        now=datetime.utcnow())


@app.route('/chat/room/<slug>')
@login_required
def chat_room(slug):
    return redirect(url_for('chat_index', subpath=slug))

@app.route('/chat/_room/<slug>')
@login_required
def chat_room_legacy(slug):
    """Страница конкретной комнаты."""
    room = Room.query.filter_by(slug=slug, is_active=True).first_or_404()
    # Автовход при первом посещении
    member = room.get_member(current_user)
    if not member:
        member = RoomMember(room_id=room.id, user_id=current_user.id, role='member')
        db.session.add(member)
        # Системное сообщение
        sys_msg = RoomMessage(
            room_id=room.id, author_id=None,
            text=f'{current_user.username} вступил в комнату',
            msg_type='system'
        )
        db.session.add(sys_msg)
        db.session.commit()
    elif member.is_banned:
        return render_template('error.html', error='Вы заблокированы в этой комнате.'), 403

    messages = RoomMessage.query.filter_by(room_id=room.id, deleted=False)                                .order_by(RoomMessage.id.asc()).limit(100).all()
    # Отмечаем как прочитанное
    member.last_read_at = datetime.utcnow()
    db.session.commit()

    return render_template('chat_room.html',
                           room=room,
                           member=member,
                           messages=messages,
                           categories=ROOM_CATEGORIES,
                           cat_label=_cat_label,
                           now=datetime.utcnow())


# ── Заявка на создание комнаты ───────────────────────────────────

@app.route('/chat/apply', methods=['GET', 'POST'])
@login_required
def chat_apply():
    if request.method == 'POST':
        name      = (request.form.get('name') or '').strip()
        desc      = (request.form.get('description') or '').strip()
        category  = request.form.get('category', 'general')
        room_type = request.form.get('room_type', 'room')
        reason    = (request.form.get('reason') or '').strip()
        quiz_q1   = (request.form.get('quiz_q1') or '').strip()
        quiz_q2   = (request.form.get('quiz_q2') or '').strip()
        quiz_q3   = (request.form.get('quiz_q3') or '').strip()
        if not name or len(name) < 3:
            flash('Название слишком короткое', 'error')
            return redirect(url_for('chat_index', subpath='apply'))
        if not reason or len(reason) < 40:
            flash('Опишите цель подробнее (минимум 40 символов)', 'error')
            return redirect(url_for('chat_index', subpath='apply'))
        pending = RoomApplication.query.filter_by(
            applicant_id=current_user.id, status='pending'
        ).count()
        if pending >= 3:
            flash('У вас уже есть 3 ожидающих заявки', 'error')
            return redirect(url_for('chat_index', subpath='apply'))
        app_obj = RoomApplication(
            applicant_id=current_user.id,
            name=name, description=desc,
            category=category, room_type=room_type,
            reason=reason,
            quiz_q1=quiz_q1, quiz_q2=quiz_q2, quiz_q3=quiz_q3
        )
        db.session.add(app_obj)
        db.session.commit()
        flash('Заявка отправлена! Рассмотрим в ближайшее время 👍', 'success')
        return redirect(url_for('chat_index'))
    return redirect(url_for('chat_index', subpath='apply'))


# ── Админская панель заявок ──────────────────────────────────────

@app.route('/chat/admin')
@login_required
def chat_admin():
    if current_user.role < 3:
        abort(403)
    pending   = RoomApplication.query.filter_by(status='pending')                               .order_by(RoomApplication.created_at.asc()).all()
    approved  = RoomApplication.query.filter_by(status='approved')                               .order_by(RoomApplication.reviewed_at.desc()).limit(20).all()
    rejected  = RoomApplication.query.filter_by(status='rejected')                               .order_by(RoomApplication.reviewed_at.desc()).limit(20).all()
    all_rooms = Room.query.order_by(Room.created_at.desc()).all()
    return redirect(url_for('chat_index', subpath='admin'))


@app.route('/chat/admin/review/<int:app_id>', methods=['POST'])
@login_required
def chat_admin_review(app_id):
    if current_user.role < 3:
        abort(403)
    app_obj = RoomApplication.query.get_or_404(app_id)
    action  = request.form.get('action')  # 'approve' | 'reject'
    note    = (request.form.get('note') or '').strip()

    if action == 'approve':
        # Генерируем slug
        import re, unicodedata
        base = app_obj.name.lower().strip()
        base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode()
        base = re.sub(r'[^a-z0-9]+', '-', base).strip('-') or 'room'
        slug = base
        counter = 1
        while Room.query.filter_by(slug=slug).first():
            slug = f'{base}-{counter}'; counter += 1

        # Создаём комнату
        room = Room(
            name=app_obj.name,
            slug=slug,
            description=app_obj.description,
            category=app_obj.category,
            room_type=app_obj.room_type,
            owner_id=app_obj.applicant_id,
            is_active=True,
            rules='''Уважай других участников.
Не публикуй незаконный контент.
Соблюдай правила сайта.'''
        )
        db.session.add(room)
        db.session.flush()

        # Добавляем создателя как owner
        owner_member = RoomMember(
            room_id=room.id,
            user_id=app_obj.applicant_id,
            role='owner'
        )
        db.session.add(owner_member)

        # Приветственное системное сообщение
        welcome = RoomMessage(
            room_id=room.id,
            author_id=None,
            msg_type='system',
                    text = (
            f"📢 Комната «{room.name}» открыта!\n\n"
            f"👋 Добро пожаловать, {app_obj.applicant.username}! Ты создатель и администратор этой комнаты.\n\n"
            f"📋 Правила:\n"
            f"• Уважай всех участников\n"
            f"• Не публикуй незаконный контент\n"
            f"• Следи за порядком в комнате\n"
            f"• При нарушениях – блокируй участника и сообщай админам сайта\n\n"
            f"Удачи! 🚀"
            )
        )
        db.session.add(welcome)

        app_obj.status       = 'approved'
        app_obj.reviewed_by_id = current_user.id
        app_obj.reviewed_at  = datetime.utcnow()
        app_obj.admin_note   = note
        app_obj.room_id      = room.id
        db.session.commit()
        flash(f'Заявка одобрена. Комната «{room.name}» создана по адресу /chat/room/{slug}', 'success')
    elif action == 'reject':
        app_obj.status       = 'rejected'
        app_obj.reviewed_by_id = current_user.id
        app_obj.reviewed_at  = datetime.utcnow()
        app_obj.admin_note   = note
        db.session.commit()
        flash('Заявка отклонена.', 'success')

    return redirect(url_for('chat_index', subpath='admin'))


@app.route('/chat/admin/room/<int:room_id>/delete', methods=['POST'])
@login_required
def chat_admin_delete_room(room_id):
    if current_user.role < 3:
        abort(403)
    room = Room.query.get_or_404(room_id)
    room.is_active = False
    db.session.commit()
    flash(f'Комната «{room.name}» деактивирована.', 'success')
    return redirect(url_for('chat_index', subpath='admin'))


@app.route('/chat/admin/room/<int:room_id>/restore', methods=['POST'])
@login_required
def chat_admin_restore_room(room_id):
    if current_user.role < 3:
        abort(403)
    room = Room.query.get_or_404(room_id)
    room.is_active = True
    db.session.commit()
    flash(f'Комната «{room.name}» восстановлена.', 'success')
    return redirect(url_for('chat_index', subpath='admin'))


# ── API комнаты ──────────────────────────────────────────────────

@app.route('/api/room/send', methods=['POST'])
@login_required
def api_room_send():
    data = request.get_json(silent=True) or {}
    room_id     = data.get('room_id')
    text        = (data.get('text') or '').strip()
    reply_to_id = data.get('reply_to_id')
    if not room_id or not text:
        return jsonify(ok=False, error='Пустое сообщение'), 400
    room = Room.query.get_or_404(room_id)
    if not room.is_active:
        return jsonify(ok=False, error='Комната неактивна'), 403
    member = room.get_member(current_user)
    if not member:
        return jsonify(ok=False, error='Вы не участник'), 403
    if member.is_banned:
        return jsonify(ok=False, error='Вы заблокированы'), 403
    if member.is_muted:
        return jsonify(ok=False, error='Вы в муте'), 403
    if len(text) > 4000:
        return jsonify(ok=False, error='Слишком длинное'), 400
    msg = RoomMessage(
        room_id=room_id,
        author_id=current_user.id,
        text=text,
        reply_to_id=reply_to_id
    )
    db.session.add(msg)
    RoomTypingStatus.query.filter_by(room_id=room_id, user_id=current_user.id).delete()
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/room/upload', methods=['POST'])
@login_required
def api_room_upload():
    room_id = request.form.get('room_id', type=int)
    ftype   = request.form.get('type', 'file')
    if not room_id:
        return jsonify(ok=False, error='Нет room_id'), 400
    room = Room.query.get_or_404(room_id)
    member = room.get_member(current_user)
    if not member or member.is_banned:
        return jsonify(ok=False, error='Нет доступа'), 403
    f = request.files.get('file')
    if not f:
        return jsonify(ok=False, error='Нет файла'), 400
    filename    = safe_filename(f.filename)
    folder      = os.path.join(app.config['UPLOAD_FOLDER'], 'rooms')
    os.makedirs(folder, exist_ok=True)
    unique_name = f'{secrets.token_hex(8)}_{filename}'
    path        = os.path.join(folder, unique_name)
    f.save(path)
    rel = f'rooms/{unique_name}'
    msg = RoomMessage(room_id=room_id, author_id=current_user.id)
    if ftype == 'image':
        msg.image_url = rel
    else:
        msg.file_url = rel; msg.file_name = f.filename
        msg.file_size = os.path.getsize(path)
    db.session.add(msg)
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/room/poll')
@login_required
def api_room_poll():
    room_id = request.args.get('room_id', type=int)
    after   = request.args.get('after',   type=int, default=0)
    if not room_id:
        return jsonify(messages=[], typing=[])
    room = Room.query.get(room_id)
    if not room:
        return jsonify(messages=[], typing=[])
    member = room.get_member(current_user)
    if not member:
        return jsonify(messages=[], typing=[])

    new_msgs = RoomMessage.query.filter(
        RoomMessage.room_id == room_id,
        RoomMessage.id > after,
        RoomMessage.deleted == False
    ).order_by(RoomMessage.id.asc()).limit(50).all()

    if new_msgs:
        member.last_read_at = datetime.utcnow()
        db.session.commit()

    threshold = datetime.utcnow() - timedelta(seconds=5)
    typing_rows = RoomTypingStatus.query.filter(
        RoomTypingStatus.room_id == room_id,
        RoomTypingStatus.user_id != current_user.id,
        RoomTypingStatus.ts > threshold
    ).all()
    typing_names = [User.query.get(t.user_id).username for t in typing_rows
                    if User.query.get(t.user_id)]

    return jsonify(
        messages=[m.to_dict() for m in new_msgs],
        typing=typing_names,
        member_count=room.member_count
    )


@app.route('/api/room/typing', methods=['POST'])
@login_required
def api_room_typing():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    if not room_id:
        return jsonify(ok=False), 400
    ts = RoomTypingStatus.query.filter_by(room_id=room_id, user_id=current_user.id).first()
    if ts:
        ts.ts = datetime.utcnow()
    else:
        ts = RoomTypingStatus(room_id=room_id, user_id=current_user.id)
        db.session.add(ts)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True)


@app.route('/api/room/react', methods=['POST'])
@login_required
def api_room_react():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    emoji  = data.get('emoji', '')
    if not msg_id or not emoji:
        return jsonify(ok=False), 400
    msg = RoomMessage.query.get_or_404(msg_id)
    existing = RoomReaction.query.filter_by(
        msg_id=msg_id, user_id=current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(RoomReaction(msg_id=msg_id, user_id=current_user.id, emoji=emoji))
    db.session.commit()
    return jsonify(ok=True, reactions=msg.reactions_grouped())


@app.route('/api/room/delete-msg', methods=['POST'])
@login_required
def api_room_delete_msg():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify(ok=False), 400
    msg = RoomMessage.query.get_or_404(msg_id)
    room = Room.query.get(msg.room_id)
    member = room.get_member(current_user) if room else None
    # Может удалить: автор, модератор комнаты, глобальный модератор
    can_delete = (
        msg.author_id == current_user.id or
        (member and member.role in ('moderator', 'admin', 'owner')) or
        current_user.role >= 2
    )
    if not can_delete:
        return jsonify(ok=False, error='Нет прав'), 403
    msg.deleted = True
    msg.text    = 'Сообщение удалено'
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/pin', methods=['POST'])
@login_required
def api_room_pin():
    data   = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    msg_id  = data.get('msg_id')
    room    = Room.query.get_or_404(room_id)
    member  = room.get_member(current_user)
    if not member or member.role not in ('moderator','admin','owner') and current_user.role < 2:
        return jsonify(ok=False, error='Нет прав'), 403
    room.pinned_msg_id = msg_id
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/ban', methods=['POST'])
@login_required
def api_room_ban():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    action  = data.get('action', 'ban')  # 'ban'|'unban'|'mute'|'unmute'
    room    = Room.query.get_or_404(room_id)
    me      = room.get_member(current_user)
    if not me or me.role not in ('moderator','admin','owner') and current_user.role < 2:
        return jsonify(ok=False, error='Нет прав'), 403
    target = RoomMember.query.filter_by(room_id=room_id, user_id=user_id).first()
    if not target:
        return jsonify(ok=False, error='Участник не найден'), 404
    if action == 'ban':
        target.is_banned = True
    elif action == 'unban':
        target.is_banned = False
    elif action == 'mute':
        target.is_muted = True
    elif action == 'unmute':
        target.is_muted = False
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/edit-settings', methods=['POST'])
@login_required
def api_room_edit_settings_extended():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    room    = Room.query.get_or_404(room_id)
    me      = room.get_member(current_user)
    if not me or me.role not in ('admin','owner') and current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    if data.get('name'):
        room.name = data['name'][:100]
    if data.get('description') is not None:
        room.description = data['description'][:500]
    if data.get('rules') is not None:
        room.rules = data['rules'][:2000]
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/leave', methods=['POST'])
@login_required
def api_room_leave():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    room    = Room.query.get_or_404(room_id)
    member  = room.get_member(current_user)
    if member:
        db.session.delete(member)
        # Системное сообщение
        sys_msg = RoomMessage(
            room_id=room_id, author_id=None,
            text=f'{current_user.username} покинул комнату',
            msg_type='system'
        )
        db.session.add(sys_msg)
        db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/upload-banner', methods=['POST'])
@login_required
def api_room_upload_banner():
    room_id = request.form.get('room_id') or (request.get_json() or {}).get('room_id')
    room = Room.query.get(int(room_id)) if room_id else None
    if not room: return jsonify(ok=False), 404
    member = room.get_member(current_user)
    if not member or member.role not in ('owner','admin'): return jsonify(ok=False, error='Нет доступа'), 403
    if request.json and request.json.get('remove'):
        room.banner = None
        db.session.commit()
        return jsonify(ok=True)
    file = request.files.get('banner')
    if not file: return jsonify(ok=False), 400
    import os, uuid
    ext = os.path.splitext(file.filename)[1].lower()
    fname = f'banner_{room.id}_{uuid.uuid4().hex[:8]}{ext}'
    upload_dir = os.path.join(app.static_folder, 'uploads')
    file.save(os.path.join(upload_dir, fname))
    room.banner = fname
    db.session.commit()
    return jsonify(ok=True, url=f'/static/uploads/{fname}')

@app.route('/api/room/join', methods=['POST'])
@login_required
def api_room_join():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    room    = Room.query.get_or_404(room_id)
    if not room.is_active:
        return jsonify(ok=False, error='Комната неактивна'), 403
    if room.get_member(current_user):
        return jsonify(ok=True)  # уже участник
    member = RoomMember(room_id=room_id, user_id=current_user.id, role='member')
    db.session.add(member)
    sys_msg = RoomMessage(
        room_id=room_id, author_id=None,
        text=f'{current_user.username} вступил в комнату',
        msg_type='system'
    )
    db.session.add(sys_msg)
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/room/search-members')
@login_required
def api_room_search_members():
    room_id = request.args.get('room_id', type=int)
    q       = request.args.get('q', '').strip()
    if not room_id or not q:
        return jsonify(members=[])
    room    = Room.query.get_or_404(room_id)
    members = room.members.join(User).filter(
        User.username.ilike(f'%{q}%')
    ).limit(10).all()
    return jsonify(members=[{
        'user_id': m.user_id,
        'username': m.user.username,
        'role': m.role,
        'avatar': m.user.avatar,
        'is_banned': m.is_banned,
        'is_muted': m.is_muted,
    } for m in members])



@app.route('/api/room/list')
@login_required
def api_room_list():
    """Список комнат и каналов для сайдбара."""
    my_ids = [m.room_id for m in current_user.room_memberships.all()]
    rooms = Room.query.filter(Room.id.in_(my_ids), Room.room_type=='room', Room.is_active==True).all() if my_ids else []
    channels = Room.query.filter(Room.id.in_(my_ids), Room.room_type=='channel', Room.is_active==True).all() if my_ids else []
    def room_dict(r):
        uc = r.unread_count(current_user)
        return {
            'id': r.id, 'name': r.name, 'slug': r.slug,
            'avatar': r.avatar, 'member_count': r.member_count,
            'unread': uc,
        }
    return jsonify(rooms=[room_dict(r) for r in rooms], channels=[room_dict(r) for r in channels])


@app.route('/api/room/read', methods=['POST'])
@login_required
def api_room_read():
    data = request.get_json()
    room_id = data.get('room_id')
    msg_id  = data.get('msg_id')
    if not room_id or not msg_id: return jsonify(ok=False), 400
    from models import RoomMessageRead
    existing = RoomMessageRead.query.filter_by(msg_id=msg_id, user_id=current_user.id).first()
    if not existing:
        db.session.add(RoomMessageRead(msg_id=msg_id, user_id=current_user.id))
        db.session.commit()
    return jsonify(ok=True)

@app.route('/api/room/msg-reads')
@login_required
def api_room_msg_reads():
    msg_id  = request.args.get('msg_id', type=int)
    room_id = request.args.get('room_id', type=int)
    if not msg_id or not room_id: return jsonify(ok=False), 400
    room = Room.query.get(room_id)
    if not room: return jsonify(ok=False), 404
    member = room.get_member(current_user)
    if not member: return jsonify(ok=False, error='Нет доступа'), 403
    from models import RoomMessageRead
    reads = RoomMessageRead.query.filter_by(msg_id=msg_id).all()
    return jsonify(ok=True, reads=[{
        'username': r.user.username,
        'avatar': r.user.avatar or ''
    } for r in reads if r.user_id != current_user.id])

@app.route('/api/room/pinned')
@login_required
def api_room_pinned():
    room_id = request.args.get('room_id', type=int)
    if not room_id:
        return jsonify(text=None)
    room = Room.query.get(room_id)
    if not room or not room.pinned_msg_id:
        return jsonify(text=None)
    msg = RoomMessage.query.get(room.pinned_msg_id)
    if not msg:
        return jsonify(text=None)
    return jsonify(text=msg.text or '[медиафайл]', msg_id=msg.id)



# ── Invite link join ─────────────────────────────────────────────

@app.route('/chat/invite/<token>')
@login_required
def chat_invite(token):
    room = Room.query.filter_by(invite_token=token, is_active=True).first_or_404()
    member = room.get_member(current_user)
    if not member:
        member = RoomMember(room_id=room.id, user_id=current_user.id, role='member')
        db.session.add(member)
        sys_msg = RoomMessage(room_id=room.id, author_id=None,
            text=f'{current_user.username} вступил по приглашению', msg_type='system')
        db.session.add(sys_msg)
        db.session.commit()
        flash(f'Вы вступили в «{room.name}»!', 'success')
    elif member.is_banned:
        flash('Вы заблокированы в этой комнате.', 'error')
        return redirect(url_for('chat_index'))
    return redirect(url_for('chat_room', slug=room.slug))


# ── Join request (for private rooms without link) ────────────────

@app.route('/api/room/request-join', methods=['POST'])
@login_required
def api_room_request_join():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    message = (data.get('message') or '').strip()[:300]
    room    = Room.query.get_or_404(room_id)
    if not room.is_private:
        # Public room — just join
        return api_room_join()
    existing = RoomJoinRequest.query.filter_by(
        room_id=room_id, user_id=current_user.id
    ).first()
    if existing:
        return jsonify(ok=False, error='Заявка уже отправлена', status=existing.status)
    req = RoomJoinRequest(room_id=room_id, user_id=current_user.id, message=message)
    db.session.add(req)
    db.session.commit()
    return jsonify(ok=True, message='Заявка отправлена')


@app.route('/api/room/review-join', methods=['POST'])
@login_required
def api_room_review_join():
    data   = request.get_json(silent=True) or {}
    req_id = data.get('req_id')
    action = data.get('action')  # 'approve' | 'reject'
    req    = RoomJoinRequest.query.get_or_404(req_id)
    room   = Room.query.get(req.room_id)
    me     = room.get_member(current_user) if room else None
    if not me or me.role not in ('owner','admin','moderator') and current_user.role < 2:
        return jsonify(ok=False, error='Нет прав'), 403
    if action == 'approve':
        existing = RoomMember.query.filter_by(room_id=req.room_id, user_id=req.user_id).first()
        if not existing:
            db.session.add(RoomMember(room_id=req.room_id, user_id=req.user_id, role='member'))
        req.status = 'approved'
        req.reviewed_at = datetime.utcnow()
    elif action == 'reject':
        req.status = 'rejected'
        req.reviewed_at = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True)


# ── Admin: create room directly ──────────────────────────────────

@app.route('/api/room/admin-create', methods=['POST'])
@login_required
def api_room_admin_create():
    if current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    data      = request.get_json(silent=True) or {}
    name      = (data.get('name') or '').strip()
    room_type = data.get('room_type', 'room')
    category  = data.get('category', 'general')
    desc      = (data.get('description') or '').strip()
    is_private= data.get('is_private', False)
    owner_id  = data.get('owner_id', current_user.id)

    if not name:
        return jsonify(ok=False, error='Нет названия'), 400

    import re, unicodedata
    base = name.lower()
    base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode()
    base = re.sub(r'[^a-z0-9]+', '-', base).strip('-') or 'room'
    slug = base; counter = 1
    while Room.query.filter_by(slug=slug).first():
        slug = f'{base}-{counter}'; counter += 1

    room = Room(
        name=name, slug=slug, description=desc,
        category=category, room_type=room_type,
        owner_id=owner_id, is_active=True,
        is_private=is_private,
        invite_token=secrets.token_hex(16),
        rules='Соблюдай правила сайта.',
        verified=True
    )
    db.session.add(room)
    db.session.flush()
    db.session.add(RoomMember(room_id=room.id, user_id=owner_id, role='owner'))
    db.session.commit()
    return jsonify(ok=True, slug=room.slug, room_id=room.id)


# ── Transfer ownership ────────────────────────────────────────────

@app.route('/api/room/transfer-owner', methods=['POST'])
@login_required
def api_room_transfer_owner():
    data       = request.get_json(silent=True) or {}
    room_id    = data.get('room_id')
    new_owner  = data.get('user_id')
    room       = Room.query.get_or_404(room_id)
    me         = room.get_member(current_user)
    if (not me or me.role != 'owner') and current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    target = RoomMember.query.filter_by(room_id=room_id, user_id=new_owner).first()
    if not target:
        return jsonify(ok=False, error='Пользователь не в комнате'), 404
    # Downgrade current owner
    if me:
        me.role = 'admin'
    target.role = 'owner'
    room.owner_id = new_owner
    db.session.commit()
    return jsonify(ok=True)


# ── Generate new invite link ──────────────────────────────────────

@app.route('/api/room/gen-invite', methods=['POST'])
@login_required
def api_room_gen_invite():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    room    = Room.query.get_or_404(room_id)
    me      = room.get_member(current_user)
    if (not me or me.role not in ('owner','admin')) and current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    room.invite_token = secrets.token_hex(16)
    db.session.commit()
    return jsonify(ok=True, token=room.invite_token,
                   link=f'/chat/invite/{room.invite_token}')


# ── Promote/demote member ─────────────────────────────────────────

@app.route('/api/room/set-role', methods=['POST'])
@login_required
def api_room_set_role():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    new_role= data.get('role')  # 'member'|'moderator'|'admin'
    if new_role not in ('member','moderator','admin'):
        return jsonify(ok=False, error='Неверная роль'), 400
    room   = Room.query.get_or_404(room_id)
    me     = room.get_member(current_user)
    if (not me or me.role not in ('owner','admin')) and current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    target = RoomMember.query.filter_by(room_id=room_id, user_id=user_id).first()
    if not target:
        return jsonify(ok=False, error='Не найден'), 404
    target.role = new_role
    db.session.commit()
    return jsonify(ok=True)


# ── Edit room settings (extended) ────────────────────────────────

@app.route('/api/room/edit-settings', methods=['POST'])
@login_required
def api_room_edit_settings():
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    room    = Room.query.get_or_404(room_id)
    me      = room.get_member(current_user)
    if (not me or me.role not in ('admin','owner')) and current_user.role < 3:
        return jsonify(ok=False, error='Нет прав'), 403
    if data.get('name'):
        room.name = data['name'][:100]
    if 'description' in data:
        room.description = (data['description'] or '')[:500]
    if 'rules' in data:
        room.rules = (data['rules'] or '')[:2000]
    if 'is_private' in data:
        room.is_private = bool(data['is_private'])
    if 'category' in data and data['category']:
        room.category = data['category']
    db.session.commit()
    return jsonify(ok=True)


# ── Get join requests for room ────────────────────────────────────

@app.route('/api/room/feature', methods=['POST'])
@login_required
def api_room_feature():
    if current_user.role < 4:
        return jsonify(ok=False, error='Нет доступа'), 403
    data = request.get_json()
    room = Room.query.get(data.get('room_id'))
    if not room: return jsonify(ok=False), 404
    room.is_featured = bool(data.get('featured', False))
    db.session.commit()
    return jsonify(ok=True)

@app.route('/api/room/join-requests')
@login_required
def api_room_join_requests():
    room_id = request.args.get('room_id', type=int)
    room    = Room.query.get_or_404(room_id)
    me      = room.get_member(current_user)
    if (not me or me.role not in ('owner','admin','moderator')) and current_user.role < 2:
        return jsonify(ok=False, error='Нет прав'), 403
    pending = RoomJoinRequest.query.filter_by(room_id=room_id, status='pending').all()
    return jsonify(requests=[{
        'id': r.id,
        'user_id': r.user_id,
        'username': r.user.username,
        'avatar': r.user.avatar,
        'message': r.message,
        'created_at': r.created_at.strftime('%d.%m.%Y %H:%M'),
    } for r in pending])



@app.route('/api/room/edit-msg', methods=['POST'])
@login_required
def api_room_edit_msg():
    data   = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    text   = (data.get('text') or '').strip()
    if not msg_id or not text:
        return jsonify(ok=False), 400
    msg  = RoomMessage.query.get_or_404(msg_id)
    room = Room.query.get(msg.room_id)
    me   = room.get_member(current_user) if room else None
    can  = msg.author_id == current_user.id or (me and me.role in ('moderator','admin','owner')) or current_user.role >= 2
    if not can:
        return jsonify(ok=False, error='Нет прав'), 403
    msg.text   = text[:4000]
    msg.edited = True
    db.session.commit()
    return jsonify(ok=True, message=msg.to_dict())


@app.route('/api/room/verify', methods=['POST'])
@login_required
def api_room_verify():
    if current_user.role < 4:
        return jsonify(ok=False, error='Нет прав'), 403
    data    = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    val     = bool(data.get('verified', True))
    room    = Room.query.get_or_404(room_id)
    room.verified = val
    db.session.commit()
    return jsonify(ok=True)


# ═══════════════════════════════════════════════
#  WEB PUSH — подписка/отписка
# ═══════════════════════════════════════════════

@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def api_push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '').strip()
    p256dh   = data.get('p256dh', '').strip()
    auth     = data.get('auth', '').strip()
    if not endpoint or not p256dh or not auth:
        return jsonify(ok=False, error='Неполные данные'), 400
    sub = PushSubscription.query.filter_by(user_id=current_user.id, endpoint=endpoint).first()
    if not sub:
        sub = PushSubscription(user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        db.session.add(sub)
    else:
        sub.p256dh = p256dh
        sub.auth   = auth
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def api_push_unsubscribe():
    data     = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '').strip()
    if endpoint:
        PushSubscription.query.filter_by(user_id=current_user.id, endpoint=endpoint).delete()
        db.session.commit()
    return jsonify(ok=True)


@app.route('/api/push/vapid-public-key')
def api_push_vapid_key():
    key = os.environ.get('VAPID_PUBLIC_KEY', '')
    return jsonify(key=key)


# ═══════════════════════════════════════════════
#  ПОДПИСКА НА СТАТЬЮ
# ═══════════════════════════════════════════════

@app.route('/api/article/<int:article_id>/subscribe', methods=['POST'])
@login_required
def api_article_subscribe(article_id):
    Article.query.get_or_404(article_id)
    existing = ArticleSubscription.query.filter_by(user_id=current_user.id, article_id=article_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify(ok=True, subscribed=False)
    db.session.add(ArticleSubscription(user_id=current_user.id, article_id=article_id))
    db.session.commit()
    return jsonify(ok=True, subscribed=True)


@app.route('/api/article/<int:article_id>/subscribe/status')
@login_required
def api_article_subscribe_status(article_id):
    existing = ArticleSubscription.query.filter_by(user_id=current_user.id, article_id=article_id).first()
    return jsonify(subscribed=bool(existing))


# ═══════════════════════════════════════════════
#  SERVICE WORKER
# ═══════════════════════════════════════════════

@app.route('/sw.js')
def service_worker():
    sw_path = os.path.join(app.static_folder, 'sw.js')
    if os.path.exists(sw_path):
        resp = make_response(send_from_directory(app.static_folder, 'sw.js'))
    else:
        sw_code = """
self.addEventListener('push', function(e) {
    var data = {};
    try { data = e.data.json(); } catch(err) { data = {title:'Comilank', body: e.data ? e.data.text() : ''}; }
    var opts = {body: data.body||'', icon:'/static/favicon.ico', badge:'/static/favicon.ico', data:{url:data.url||'/'}};
    e.waitUntil(self.registration.showNotification(data.title||'Comilank', opts));
});
self.addEventListener('notificationclick', function(e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) ? e.notification.data.url : '/';
    e.waitUntil(clients.matchAll({type:'window'}).then(function(cs){
        for(var i=0;i<cs.length;i++){ if(cs[i].url===url && 'focus' in cs[i]) return cs[i].focus(); }
        if(clients.openWindow) return clients.openWindow(url);
    }));
});
"""
        resp = make_response(sw_code, 200)
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        _run_migration()
        # При сбросе БД admin создаётся с ролью 3 (ст. модератор), НЕ 4
        # Роль 4 (главный админ) выдаётся вручную через секретный маршрут
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin'), role=3)
            db.session.add(admin); db.session.commit()
            print('Создан пользователь: admin / admin (роль 3 - старший модератор)')
            print('Для получения роли 4 используйте секретный маршрут.')
    app.run(debug=True, host='0.0.0.0', port=5000)
