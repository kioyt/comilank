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

load_dotenv()

from models import (db, User, Game, Article, Comment, Vote, DropdownItem,
                    ExtraPage, Mute, IPBan, PenaltyHistory,
                    ArticleView, CommentReaction, SiteSettings, ExtraPageView,
                    UserPermission,
                    StreamPlatform, TopViewer, TopDonator, LastStream,
                    StreamMoment, NextGamePoll, PollGame, PollVote,
                    NextStream)

app = Flask(__name__)

# ========== КОНФИГУРАЦИЯ ==========
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'comilank-secret-key-2026-change-me')

# ── Поддержка PostgreSQL (Render даёт postgres://, SQLAlchemy требует postgresql://) ──
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///forum.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB
# Постоянная сессия — помнить пользователя 30 дней
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_SECURE']   = False   # True если HTTPS
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE']  = 'Lax'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS

# ========== НАСТРОЙКИ ПОЧТЫ ==========
app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')

mail = Mail(app)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите для доступа к этой странице.'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.before_request
def create_tables():
    db.create_all()
    # Автомиграция: каждый ALTER TABLE в отдельном try/except
    # (SQLite не поддерживает "IF NOT EXISTS" в ALTER TABLE)
    with db.engine.connect() as conn:
        # ALTER TABLE — каждый в отдельном try/except
        # PostgreSQL: используем DO $$ IF NOT EXISTS $$
        _alters = [
            ("users",    "recovery_key", "VARCHAR(256)"),
            ("users",    "avatar",       "VARCHAR(300)"),
            ("articles", "video_file",   "VARCHAR(300)"),
            ("users",    "banned_by_id", "INTEGER"),
            ("extra_page_comments", "parent_id", "INTEGER"),
            ("extra_page_comments", "likes",     "INTEGER DEFAULT 0"),
            ("extra_page_comments", "dislikes",  "INTEGER DEFAULT 0"),
            ("user_permissions", "can_edit_home",   "BOOLEAN DEFAULT FALSE"),
            ("user_permissions", "can_vpn_detect",  "BOOLEAN DEFAULT FALSE"),
            ("user_permissions", "can_broadcast",   "BOOLEAN DEFAULT FALSE"),
            ("user_permissions", "can_next_stream", "BOOLEAN DEFAULT FALSE"),
        ]
        for table, col, typ in _alters:
            try:
                conn.execute(db.text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}"
                ))
                conn.commit()
            except Exception:
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
                    ))
                    conn.commit()
                except Exception:
                    pass

        # Таблицы создаём через CREATE IF NOT EXISTS (это поддерживается)
        # SQLite: INTEGER PRIMARY KEY  |  PostgreSQL: SERIAL PRIMARY KEY
        _is_sqlite = 'sqlite' in str(db.engine.url)
        _id_col = 'INTEGER PRIMARY KEY' if _is_sqlite else 'SERIAL PRIMARY KEY'
        try:
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS site_settings (
                    id {_id_col},
                    chaos_button_enabled BOOLEAN DEFAULT FALSE
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS extra_page_views (
                    id {_id_col},
                    page_id INTEGER NOT NULL,
                    user_id INTEGER,
                    ip_address VARCHAR(45),
                    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS user_permissions (
                    id {_id_col},
                    user_id INTEGER UNIQUE NOT NULL,
                    can_see_stats      BOOLEAN DEFAULT FALSE,
                    can_ip_ban         BOOLEAN DEFAULT FALSE,
                    can_create_articles BOOLEAN DEFAULT TRUE,
                    can_edit_games     BOOLEAN DEFAULT FALSE,
                    can_toggle_chaos   BOOLEAN DEFAULT FALSE,
                    can_see_penalty    BOOLEAN DEFAULT TRUE,
                    can_edit_home      BOOLEAN DEFAULT FALSE,
                    can_vpn_detect     BOOLEAN DEFAULT FALSE,
                    can_broadcast      BOOLEAN DEFAULT FALSE,
                    granted_by_id INTEGER,
                    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS extra_page_comments (
                    id {_id_col},
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    page_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    parent_id INTEGER,
                    likes INTEGER DEFAULT 0,
                    dislikes INTEGER DEFAULT 0
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS extra_comment_reactions (
                    id {_id_col},
                    comment_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    value INTEGER NOT NULL,
                    UNIQUE(comment_id, user_id)
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS extra_page_votes (
                    id {_id_col},
                    page_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    value INTEGER NOT NULL,
                    UNIQUE(page_id, user_id)
                )"""
            ))
            # ── Новые таблицы для главной страницы ──
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS stream_platform (
                    id {_id_col},
                    name        VARCHAR(50)  NOT NULL,
                    key         VARCHAR(20)  UNIQUE NOT NULL,
                    icon_class  VARCHAR(60)  DEFAULT 'fab fa-youtube',
                    color       VARCHAR(20)  DEFAULT '#ff6600',
                    is_live     BOOLEAN      DEFAULT FALSE,
                    stream_url  VARCHAR(500) DEFAULT '',
                    channel_url VARCHAR(500) DEFAULT ''
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS top_viewer (
                    id            {_id_col},
                    name          VARCHAR(100) NOT NULL,
                    messages      INTEGER      DEFAULT 0,
                    show_messages BOOLEAN      DEFAULT TRUE,
                    position      INTEGER      DEFAULT 0
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS top_donator (
                    id       {_id_col},
                    name     VARCHAR(100) NOT NULL,
                    amount   INTEGER      DEFAULT 0,
                    position INTEGER      DEFAULT 0
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS last_stream (
                    id            {_id_col},
                    title         VARCHAR(250) DEFAULT '',
                    url           VARCHAR(500) DEFAULT '',
                    thumbnail_url VARCHAR(500) DEFAULT '',
                    views         VARCHAR(50)  DEFAULT '',
                    yt_url        VARCHAR(500) DEFAULT '',
                    twitch_url    VARCHAR(500) DEFAULT '',
                    tiktok_url    VARCHAR(500) DEFAULT ''
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS stream_moment (
                    id            {_id_col},
                    title         VARCHAR(200) NOT NULL,
                    url           VARCHAR(500) NOT NULL,
                    thumbnail_url VARCHAR(500) DEFAULT '',
                    views         VARCHAR(50)  DEFAULT '',
                    game          VARCHAR(100) DEFAULT '',
                    position      INTEGER      DEFAULT 0
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS next_game_poll (
                    id     {_id_col},
                    active BOOLEAN DEFAULT TRUE
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS poll_game (
                    id        {_id_col},
                    poll_id   INTEGER      NOT NULL,
                    name      VARCHAR(100) NOT NULL,
                    image_url VARCHAR(500) DEFAULT '',
                    votes     INTEGER      DEFAULT 0,
                    position  INTEGER      DEFAULT 0
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS poll_vote (
                    id      {_id_col},
                    poll_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    game_id INTEGER NOT NULL,
                    UNIQUE(poll_id, user_id)
                )"""
            ))
            conn.execute(db.text(
                f"""CREATE TABLE IF NOT EXISTS next_stream (
                    id          {_id_col},
                    enabled     BOOLEAN      DEFAULT FALSE,
                    title       VARCHAR(200) DEFAULT '',
                    stream_dt   VARCHAR(30)  DEFAULT '',
                    description VARCHAR(300) DEFAULT ''
                )"""
            ))
            conn.commit()
        except Exception:
            pass

@app.before_request
def update_last_seen():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.utcnow()
        db.session.commit()

# ========== VPN CHECK API ==========
@app.route('/api/check-vpn')
def api_check_vpn():
    # Кеш в сессии — не долбим API каждый запрос
    from datetime import datetime as _dt
    cached = session.get('_vpn_cache')
    cached_at = session.get('_vpn_cache_at', 0)
    if cached is not None and (_dt.utcnow().timestamp() - cached_at) < 600:
        return jsonify({'vpn': cached})
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip:
            ip = ip.split(',')[0].strip()

        # Локальный IP — получаем реальный внешний IP
        _local = ('127.', '::1', 'localhost', '10.', '192.168.', '172.')
        if not ip or any(ip.startswith(p) for p in _local):
            try:
                ext = requests.get('https://api.ipify.org?format=json', timeout=3)
                ip = ext.json().get('ip', ip)
            except Exception:
                pass

        resp = requests.get(f'https://ipwho.is/{ip}', timeout=4)
        data = resp.json()
        sec = data.get('security', {})
        is_vpn = bool(data.get('success')) and bool(
            sec.get('vpn') or sec.get('proxy') or sec.get('tor')
        )
        session['_vpn_cache'] = is_vpn
        session['_vpn_cache_at'] = _dt.utcnow().timestamp()
        return jsonify({'vpn': is_vpn})
    except Exception:
        return jsonify({'vpn': False})

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
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
    Для роли 4 (главный админ): полная свобода — проверка только по func.lower().
    """
    # Убираем все не-буквенные символы, приводим буквы к нижнему регистру
    letters_only = re.sub(r'[^a-zA-Zа-яёА-ЯЁ]', '', username).lower()
    return letters_only

def record_article_view(article):
    """Строго 1 просмотр на уникальный IP-адрес. Если смотрел гостем, а потом зашёл — не считается повторно."""
    ip = get_client_ip()
    if current_user.is_authenticated:
        # Уже смотрел залогиненным?
        if ArticleView.query.filter_by(article_id=article.id, user_id=current_user.id).first():
            return
        # Смотрел гостем с этого IP?
        existing_ip = ArticleView.query.filter_by(article_id=article.id, ip_address=ip).first()
        if existing_ip:
            if existing_ip.user_id is None:
                existing_ip.user_id = current_user.id  # привязываем, счётчик не трогаем
                db.session.commit()
            return
        db.session.add(ArticleView(article_id=article.id, user_id=current_user.id, ip_address=ip))
        article.views += 1
        db.session.commit()
    else:
        # Гость: уже смотрел с этого IP (залогиненным или гостем)?
        if ArticleView.query.filter_by(article_id=article.id, ip_address=ip).first():
            return
        db.session.add(ArticleView(article_id=article.id, ip_address=ip, user_id=None))
        article.views += 1
        db.session.commit()


def record_extra_page_view(page):
    """Та же логика что для статей — строго 1 просмотр на IP, независимо от авторизации."""
    ip = get_client_ip()
    if current_user.is_authenticated:
        if ExtraPageView.query.filter_by(page_id=page.id, user_id=current_user.id).first():
            return
        existing_ip = ExtraPageView.query.filter_by(page_id=page.id, ip_address=ip).first()
        if existing_ip:
            if existing_ip.user_id is None:
                existing_ip.user_id = current_user.id
                db.session.commit()
            return
        db.session.add(ExtraPageView(page_id=page.id, user_id=current_user.id, ip_address=ip))
        page.views += 1
        db.session.commit()
    else:
        if ExtraPageView.query.filter_by(page_id=page.id, ip_address=ip).first():
            return
        db.session.add(ExtraPageView(page_id=page.id, ip_address=ip, user_id=None))
        page.views += 1
        db.session.commit()

# ========== КОНСОЛЬНЫЕ КОМАНДЫ ==========
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
    """Установить роль: 0=user 1=mod 2=editor 3=sr.mod 4=admin"""
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user:
            user.role = role
            db.session.commit()
            roles = {0:'пользователь',1:'модератор',2:'редактор',3:'ст.модератор',4:'администратор'}
            click.echo(f'{username} -> роль {role} ({roles.get(role,"?")})')
        else:
            click.echo(f'Пользователь {username} не найден.')

# ========== YOUTUBE API ==========
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
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options']        = 'DENY'
    response.headers['X-XSS-Protection']       = '1; mode=block'
    response.headers.pop('Server', None)
    response.headers.pop('X-Powered-By', None)
    return response

@app.route('/node_modules/<path:filename>')
def serve_node_modules(filename): return send_from_directory('node_modules', filename)

@app.route('/src/<path:filename>')
def serve_src(filename): return send_from_directory('src', filename)

@app.route('/')
def index():
    # F5-редирект: если есть cookie с реальным путём — возвращаем туда
    rp = request.cookies.get('_rp')
    if rp and rp != '/' and not rp.startswith('//'):
        resp = make_response(redirect(rp))
        resp.delete_cookie('_rp')
        return resp
    # ── Данные для главной страницы ──
    _init_stream_platforms()   # создаём платформы если нет
    stream_platforms  = StreamPlatform.query.order_by(StreamPlatform.id).all()
    top_viewers       = TopViewer.query.order_by(TopViewer.position).limit(4).all()
    top_donators      = TopDonator.query.order_by(TopDonator.position).limit(3).all()
    last_stream       = LastStream.query.order_by(LastStream.id.desc()).first()
    last_article      = Article.query.filter_by(category='article').order_by(Article.created_at.desc()).first()
    last_news         = Article.query.filter_by(category='news').order_by(Article.created_at.desc()).first()
    stream_moments    = StreamMoment.query.order_by(StreamMoment.position).all()
    next_poll         = NextGamePoll.query.order_by(NextGamePoll.id.desc()).first()
    next_stream_widget = NextStream.query.first()
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

@app.route('/comilank-secret-admin-x7k2/Вини')
def make_admin():
    user = User.query.filter(func.lower(User.username) == func.lower('Вини')).first()
    if user:
        user.role = 4
        db.session.commit()
        return 'Готово! Роль администратора выдана.'
    return 'Пользователь не найден. Сначала зарегистрируйся.'

# ========== КНОПКА "НЕ НАЖИМАЙ" ==========
@app.route('/admin/chaos-button/toggle', methods=['POST'])
@login_required
def toggle_chaos_button():
    """Только роль 4 может включать/выключать кнопку."""
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
                body=f'{message_text}\n\n— Команда Comilank'
            )
            mail.send(msg)
            sent += 1
        except Exception:
            pass
    return jsonify(ok=True, message=f'Отправлено {sent} пользователям')


@app.route('/chaos')
def chaos_trigger():
    """Срабатывает когда юзер нажал кнопку — возвращает JSON-статус."""
    settings = SiteSettings.get()
    if not settings.chaos_button_enabled:
        abort(404)
    return jsonify(triggered=True)

@app.route('/welcome')
def welcome():
    if not current_user.is_authenticated: return redirect(url_for('login'))
    return render_template('welcome.html', user=current_user)

# ========== АУТЕНТИФИКАЦИЯ ==========
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email    = request.form['email'].strip()
        password = request.form['password']

        # Проверка 1: точное совпадение (без учёта регистра) — для всех
        if User.query.filter(func.lower(User.username) == func.lower(username)).first():
            flash('Имя пользователя уже занято', 'error')
            return redirect(url_for('register'))

        # Проверка 2: нормализованное совпадение только по буквам (для обычных юзеров)
        # Вини = вини = ВиНи = ВИНИ — одинаковы; Вини1 ≠ Вини2
        new_norm = normalize_username(username)
        if new_norm:  # если в нике вообще есть буквы
            for existing in User.query.filter(User.role < 4).all():
                if normalize_username(existing.username) == new_norm:
                    flash('Имя пользователя слишком похоже на уже существующее', 'error')
                    return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email уже зарегистрирован', 'error')
            return redirect(url_for('register'))

        user = User(username=username, email=email,
                    password_hash=generate_password_hash(password), role=0)
        db.session.add(user)
        db.session.commit()
        login_user(user)
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
            user = User(username=session['reg_username'], email=session['reg_email'],
                        password_hash=session['reg_password'], role=0)
            db.session.add(user)
            db.session.commit()
            for k in ('reg_username','reg_email','reg_password','reg_code','reg_code_time'):
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
    if app.config['MAIL_USERNAME'] and app.config['MAIL_PASSWORD']:
        try:
            msg = Message('Новый код', recipients=[email])
            msg.body = f'Ваш новый код: {code}'
            mail.send(msg)
            flash('Новый код отправлен', 'success')
        except Exception as e:
            print(e); flash('Ошибка отправки', 'error')
    else:
        print(f"\n Новый код для {email}: {code}\n")
        flash(f'Новый код (консоль): {code}', 'success')
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
            flash('Слишком много неудачных попыток. Введите ключ восстановления.', 'error')
            return redirect(url_for('recover', username=username))
        flash('Неверное имя пользователя или пароль', 'error')
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

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из аккаунта', 'success')
    return redirect(url_for('index'))

@app.route('/profile/<username>')
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    return render_template('profile.html', user=user)

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

# ========== ДЕКОРАТОРЫ ==========
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

# ========== АДМИН-ПАНЕЛЬ ==========
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
            d = now - dt
            if d.seconds < 60: return 'только что'
            if d.seconds < 3600: return f'{d.seconds//60} мин назад'
            if d.days == 0: return f'{d.seconds//3600} ч назад'
            return f'{d.days} д назад'
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
    return render_template('admin/dashboard.html',
                           chaos_button_enabled=settings.chaos_button_enabled,
                           stats=stats,
                           digest_data=digest_data,
                           next_stream_widget=NextStream.query.first())

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
    """Только role=4 может менять точечные разрешения."""
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
    # Удаляем реакции на комментарии пользователя
    CommentReaction.query.filter_by(user_id=user_id).delete()
    # Удаляем реакции на комментарии этого пользователя и сами комментарии
    comments = Comment.query.filter_by(author_id=user_id).all()
    for c in comments:
        # Удаляем ответы на комментарий
        for reply in Comment.query.filter_by(parent_id=c.id).all():
            CommentReaction.query.filter_by(comment_id=reply.id).delete()
            db.session.delete(reply)
        CommentReaction.query.filter_by(comment_id=c.id).delete()
        db.session.delete(c)
    # Удаляем голоса
    Vote.query.filter_by(user_id=user_id).delete()
    # Удаляем просмотры
    ArticleView.query.filter_by(user_id=user_id).delete()
    # Удаляем историю наказаний
    PenaltyHistory.query.filter_by(user_id=user_id).delete()
    PenaltyHistory.query.filter_by(created_by_id=user_id).delete()
    # Удаляем муты
    Mute.query.filter_by(user_id=user_id).delete()
    db.session.flush()
    db.session.delete(target_user)
    db.session.commit()
    flash(f'{target_user.username} удалён', 'success')
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
    filename = secure_filename(f"content_{secrets.token_hex(8)}_{file.filename}")
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
    filename = secure_filename(f"avatar_{current_user.id}_{secrets.token_hex(6)}_{file.filename}")
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

# ========== СТАТИСТИКА ==========
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

# ========== ФОРУМ ==========

@app.route('/forum')
def forum():
    category     = request.args.get('category', 'all')
    q            = Article.query
    if category in ('article', 'news'):
        q = q.filter_by(category=category)
    articles     = q.order_by(Article.created_at.desc()).all()
    top_comments = Comment.query.order_by(Comment.likes.desc()).limit(5).all()
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
        return redirect(url_for('article', article_id=article_id), 303)
    parent_id = request.form.get('parent_id', type=int)
    comment   = Comment(content=content, article_id=article_id,
                        author_id=current_user.id, parent_id=parent_id)
    db.session.add(comment); db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, id=comment.id,
                       username=current_user.username,
                       avatar=current_user.avatar or '',
                       role=current_user.role,
                       content=content,
                       parent_id=parent_id)
    return redirect(url_for('article', article_id=article_id) + f'#comment-{comment.id}', 303)

@app.route('/comment/<int:comment_id>/react', methods=['POST'])
@login_required
def react_comment(comment_id):
    """Реакция на комментарий (лайк/дизлайк)."""
    comment = Comment.query.get_or_404(comment_id)
    value   = int(request.form.get('value'))
    react   = CommentReaction.query.filter_by(comment_id=comment_id, user_id=current_user.id).first()
    if react:
        if react.value == value: db.session.delete(react)
        else: react.value = value
    else:
        db.session.add(CommentReaction(comment_id=comment_id, user_id=current_user.id, value=value))
    db.session.commit()
    comment.recalc_reactions(); db.session.commit()
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
    # Удаляем реакции на комментарий
    CommentReaction.query.filter_by(comment_id=comment_id).delete()
    # Удаляем ответы
    for reply in comment.replies:
        CommentReaction.query.filter_by(comment_id=reply.id).delete()
    db.session.delete(comment)
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True)
    return redirect(url_for('article', article_id=article_id) + '#comments')

@app.route('/extra/<int:page_id>')
def extra_page(page_id):
    page = ExtraPage.query.get_or_404(page_id)
    record_extra_page_view(page)
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
    return render_template('extra.html', page=page,
                           extra_comments=extra_comments,
                           user_extra_reactions=user_extra_reactions,
                           page_likes=page_likes, page_dislikes=page_dislikes,
                           user_page_vote=user_page_vote)


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
    ExtraPage.query.get_or_404(page_id)
    content = request.form.get('content', '').strip()
    if not content:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error='empty')
        return redirect(url_for('extra_page', page_id=page_id))

    # Надёжный парсинг parent_id — защита от 'undefined', '', None
    parent_id_raw = request.form.get('parent_id', '').strip()
    parent_id = None
    if parent_id_raw and parent_id_raw.isdigit():
        parent_id = int(parent_id_raw)

    comment_id = 0
    try:
        # Простой INSERT без RETURNING — работает на SQLite и PostgreSQL
        db.session.execute(
            db.text("INSERT INTO extra_page_comments "
                    "(content, page_id, author_id, parent_id) "
                    "VALUES (:c, :pid, :aid, :par)"),
            {'c': content, 'pid': page_id, 'aid': current_user.id, 'par': parent_id}
        )
        db.session.commit()
        # Получаем ID вставленной записи
        row = db.session.execute(
            db.text("SELECT id FROM extra_page_comments "
                    "WHERE page_id=:pid AND author_id=:aid AND content=:c "
                    "ORDER BY id DESC LIMIT 1"),
            {'pid': page_id, 'aid': current_user.id, 'c': content}
        ).fetchone()
        comment_id = row[0] if row else 0
    except Exception:
        db.session.rollback()
        comment_id = 0

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
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

# ========== АДМИНКА: СТАТЬИ ==========

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
    if not title or not content:
        flash('Заголовок и содержание обязательны', 'error')
        return None
    # Загрузка изображения
    image_file = None
    file = request.files.get('image_file')
    if file and file.filename and allowed_file(file.filename):
        filename   = secure_filename(f"{secrets.token_hex(8)}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        image_file = filename
    # Загрузка видео-файла
    video_file = None
    vfile = request.files.get('video_file_upload')
    if vfile and vfile.filename and allowed_video_file(vfile.filename):
        vfilename = secure_filename(f"{secrets.token_hex(8)}_{vfile.filename}")
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
            return redirect(url_for('admin_articles'))
    games = Game.query.order_by(Game.name).all()
    return render_template('admin/article_edit.html', article=None, games=games)

@app.route('/admin/article/<int:article_id>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_article_edit(article_id):
    art = Article.query.get_or_404(article_id)
    # Роль 3 (ст.мод) может редактировать только свои статьи, роль 4 — любые
    if current_user.role == 3 and art.author_id != current_user.id:
        flash('Вы можете редактировать только свои статьи', 'error')
        return redirect(url_for('admin_articles'))
    if request.method == 'POST':
        if _save_article_form(art):
            flash('Статья обновлена!', 'success')
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
    db.session.delete(art); db.session.commit()
    flash('Статья удалена', 'success')
    return redirect(url_for('admin_articles'))

# ========== ДОПОЛНИТЕЛЬНЫЕ СТРАНИЦЫ ==========

@app.route('/admin/extra/new', methods=['GET', 'POST'])
@login_required
@editor_required
def admin_extra_create():
    if request.method == 'POST':
        db.session.add(ExtraPage(title=request.form.get('title'),
                                 content=request.form.get('content'),
                                 article_id=request.form.get('article_id', type=int),
                                 author_id=current_user.id))
        db.session.commit()
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

# ========== ВЫПАДАЮЩИЕ ПУНКТЫ ==========

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

# ========== ИГРЫ ==========

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
                filename = secure_filename(f"game_{secrets.token_hex(6)}_{file.filename}")
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
            filename = secure_filename(f"game_{secrets.token_hex(6)}_{file.filename}")
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

# ═══════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ГЛАВНОЙ
# ═══════════════════════════════════════════════════════════

def _init_stream_platforms():
    """Создаёт 4 записи платформ при первом запуске, если их ещё нет."""
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



# ── VPN-детектор ──
@app.route('/admin/vpn-logs')
@login_required
def admin_vpn_logs():
    """Журнал VPN-сессий — доступен role>=4 или can_vpn_detect."""
    perm = UserPermission.query.filter_by(user_id=current_user.id).first()
    if current_user.role < 4 and not (perm and getattr(perm, 'can_vpn_detect', False)):
        return jsonify(logs=[]), 403
    # Простейший журнал из логов Flask (или можно хранить в БД)
    # Здесь возвращаем пустой список — функция расширяется при наличии VPN-таблицы
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
    """Доступ к настройкам главной: role>=2 ИЛИ can_edit_home."""
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


# ═══════════════════════════════════════════════════════════
#  ГОЛОСОВАНИЕ «ВО ЧТО ИГРАЕМ»
# ═══════════════════════════════════════════════════════════

@app.route('/vote-next-game', methods=['POST'])
@login_required
def vote_next_game():
    game_id = request.form.get('game_id', type=int)
    if not game_id:
        return redirect(url_for('index'))
    game = PollGame.query.get_or_404(game_id)
    poll = NextGamePoll.query.get(game.poll_id)
    if not poll or not poll.active:
        flash('Голосование завершено', 'error')
        return redirect(url_for('index'))
    existing = PollVote.query.filter_by(poll_id=poll.id, user_id=current_user.id).first()
    if existing:
        # Изменить голос
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
    flash('Голос учтён!', 'success')
    return redirect(url_for('index'))


# ═══════════════════════════════════════════════════════════
#  АДМИН: НАСТРОЙКИ ГЛАВНОЙ СТРАНИЦЫ
# ═══════════════════════════════════════════════════════════

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


# ── Платформы стрима ──
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
    return redirect(url_for('admin_home_settings'))


# ── Топ зрителей ──
@app.route('/admin/home/viewer/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_add():
    max_slots = 4 if current_user.role >= 4 else 3
    if TopViewer.query.count() >= max_slots:
        flash(f'Максимум {max_slots} зрителей', 'error')
        return redirect(url_for('admin_home_settings'))
    pos = (db.session.query(func.max(TopViewer.position)).scalar() or 0) + 1
    v = TopViewer(
        name=request.form['name'].strip(),
        messages=int(request.form.get('messages', 0) or 0),
        show_messages=bool(request.form.get('show_messages')),
        position=pos,
    )
    db.session.add(v)
    db.session.commit()
    flash('Зритель добавлен', 'success')
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/viewer/<int:vid>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_viewer_delete(vid):
    db.session.delete(TopViewer.query.get_or_404(vid))
    db.session.commit()
    flash('Зритель удалён', 'success')
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
    return redirect(url_for('admin_home_settings'))


# ── Топ донатеров ──
@app.route('/admin/home/donator/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_donator_add():
    if TopDonator.query.count() >= 3:
        flash('Максимум 3 донатера', 'error')
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
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/donator/<int:did>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_donator_delete(did):
    db.session.delete(TopDonator.query.get_or_404(did))
    db.session.commit()
    flash('Донатер удалён', 'success')
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
    return redirect(url_for('admin_home_settings'))


# ── Последний стрим ──
@app.route('/admin/home/last-stream', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_last_stream():
    ls = LastStream.query.order_by(LastStream.id.desc()).first()
    if not ls:
        ls = LastStream()
        db.session.add(ls)
    ls.title         = request.form.get('title', '').strip()
    ls.url           = request.form.get('url', '').strip()
    ls.thumbnail_url = request.form.get('thumbnail_url', '').strip()
    ls.views         = request.form.get('views', '').strip()
    ls.yt_url        = request.form.get('yt_url', '').strip()
    ls.twitch_url    = request.form.get('twitch_url', '').strip()
    ls.tiktok_url    = request.form.get('tiktok_url', '').strip()
    db.session.commit()
    flash('Последний стрим обновлён', 'success')
    return redirect(url_for('admin_home_settings'))


# ── Следующий стрим (виджет-таймер) ──
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
    ns.enabled     = request.form.get('enabled') == '1'
    ns.title       = request.form.get('title', '').strip()
    ns.stream_dt   = request.form.get('stream_dt', '').strip()
    ns.description = request.form.get('description', '').strip()
    db.session.commit()
    flash('Виджет стрима обновлён', 'success')
    return redirect(url_for('admin_home_settings'))


# ── Моменты стрима ──
@app.route('/admin/home/moment/add', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_add():
    pos = (db.session.query(func.max(StreamMoment.position)).scalar() or 0) + 1
    # Обработка загрузки файла превью
    thumb_url = request.form.get('thumbnail_url', '').strip()
    thumb_file = request.files.get('thumbnail_file')
    if thumb_file and thumb_file.filename and allowed_file(thumb_file.filename):
        filename = secure_filename(f"moment_{secrets.token_hex(6)}_{thumb_file.filename}")
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
    flash('Момент добавлен', 'success')
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/moment/<int:mid>/delete', methods=['POST'])
@login_required
@_home_edit_required
def admin_home_moment_delete(mid):
    db.session.delete(StreamMoment.query.get_or_404(mid))
    db.session.commit()
    flash('Момент удалён', 'success')
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
    return redirect(url_for('admin_home_settings'))


# ── Голосование ──
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
    g = PollGame(
        poll_id=poll.id,
        name=request.form['game_name'].strip(),
        image_url=request.form.get('game_image', '').strip(),
        position=pos,
    )
    db.session.add(g)
    db.session.commit()
    flash('Игра добавлена в опрос', 'success')
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/poll/game/<int:gid>/delete', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_game_delete(gid):
    db.session.delete(PollGame.query.get_or_404(gid))
    db.session.commit()
    flash('Игра удалена из опроса', 'success')
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/poll/end', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_end():
    """Завершить голосование — только роль 4."""
    poll = NextGamePoll.query.filter_by(active=True).order_by(NextGamePoll.id.desc()).first()
    if poll:
        poll.active = False
        db.session.commit()
        flash('Голосование завершено', 'success')
    return redirect(url_for('admin_home_settings'))


@app.route('/admin/home/poll/reset', methods=['POST'])
@login_required
@admin_required
def admin_home_poll_reset():
    """Закрыть все старые опросы и начать новый — только роль 4."""
    NextGamePoll.query.update({'active': False})
    db.session.commit()
    flash('Старый опрос закрыт. Добавь игры для нового.', 'success')
    return redirect(url_for('admin_home_settings'))


# ========== SEO: SITEMAP + ROBOTS ==========

@app.route('/sitemap.xml')
def sitemap():
    """Автоматический sitemap.xml для Google."""
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
    """robots.txt — разрешаем Google индексировать всё, кроме админки."""
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


# ========== ЗАПУСК ==========
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # При сбросе БД admin создаётся с ролью 3 (ст. модератор), НЕ 4
        # Роль 4 (главный админ) выдаётся вручную через секретный маршрут
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin'), role=3)
            db.session.add(admin); db.session.commit()
            print('Создан пользователь: admin / admin (роль 3 — старший модератор)')
            print('Для получения роли 4 используйте секретный маршрут.')
    app.run(debug=os.environ.get('FLASK_DEBUG', 'False').lower() == 'true')