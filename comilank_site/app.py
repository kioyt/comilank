import os
import re
import click
import random
import string
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, send_from_directory, request, redirect, url_for, flash, abort, session
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
                    ArticleView, CommentReaction)

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

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

@app.before_request
def update_last_seen():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.utcnow()
        db.session.commit()

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

def record_article_view(article):
    """Засчитывает просмотр один раз на пользователя/IP."""
    ip = get_client_ip()
    if current_user.is_authenticated:
        existing = ArticleView.query.filter_by(article_id=article.id, user_id=current_user.id).first()
        if not existing:
            db.session.add(ArticleView(article_id=article.id, user_id=current_user.id, ip_address=ip))
            article.views += 1
            db.session.commit()
    else:
        existing = ArticleView.query.filter_by(article_id=article.id, ip_address=ip, user_id=None).first()
        if not existing:
            db.session.add(ArticleView(article_id=article.id, ip_address=ip, user_id=None))
            article.views += 1
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
def index(): return render_template('index.html')

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

@app.route('/welcome')
def welcome():
    if not current_user.is_authenticated: return redirect(url_for('login'))
    return render_template('welcome.html', user=current_user)

# ========== АУТЕНТИФИКАЦИЯ ==========
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email    = request.form['email']
        password = request.form['password']
        recovery = request.form.get('recovery_key', '').strip()
        if User.query.filter(func.lower(User.username) == func.lower(username)).first():
            flash('Имя пользователя уже занято', 'error')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email уже зарегистрирован', 'error')
            return redirect(url_for('register'))
        # Если ключ не введён — генерируем автоматически
        if not recovery:
            recovery = secrets.token_hex(8)  # 16 символов, удобно для запоминания
        recovery_hash = generate_password_hash(recovery)
        user = User(username=username, email=email,
                    password_hash=generate_password_hash(password),
                    recovery_key=recovery_hash, role=0)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        # Показываем ключ один раз после регистрации
        flash(f'Добро пожаловать! Ваш ключ восстановления: {recovery} — сохраните его, он больше не будет показан!', 'success')
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
            login_user(user, remember=True)  # запомнить на 30 дней
            # Вернуться на страницу откуда пришли, или на главную
            next_page = request.args.get('next') or request.form.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('welcome'))
        flash('Неверное имя пользователя или пароль', 'error')
    return render_template('login.html')

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

@app.route('/recover', methods=['GET', 'POST'])
def recover():
    """Восстановление аккаунта по секретному ключу."""
    if current_user.is_authenticated:
        return redirect(url_for('welcome'))
    if request.method == 'POST':
        username   = request.form.get('username', '').strip()
        key        = request.form.get('recovery_key', '').strip()
        new_pw     = request.form.get('new_password', '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        user = User.query.filter(func.lower(User.username) == func.lower(username)).first()
        if not user or not user.recovery_key:
            flash('Пользователь не найден или ключ восстановления не установлен', 'error')
            return redirect(url_for('recover'))
        if not check_password_hash(user.recovery_key, key):
            flash('Неверный ключ восстановления', 'error')
            return redirect(url_for('recover'))
        if len(new_pw) < 6:
            flash('Пароль должен быть не менее 6 символов', 'error')
            return redirect(url_for('recover'))
        if new_pw != confirm_pw:
            flash('Пароли не совпадают', 'error')
            return redirect(url_for('recover'))
        user.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        login_user(user, remember=True)
        flash('Пароль успешно восстановлен! Вы вошли в аккаунт.', 'success')
        return redirect(url_for('welcome'))
    return render_template('recover.html')

@app.route('/settings/recovery-key', methods=['POST'])
@login_required
def update_recovery_key():
    """Обновление секретного ключа восстановления из профиля."""
    new_key = request.form.get('recovery_key', '').strip()
    if not new_key:
        new_key = secrets.token_hex(8)
    current_user.recovery_key = generate_password_hash(new_key)
    db.session.commit()
    flash(f'Ключ восстановления обновлён: {new_key} — сохраните его!', 'success')
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
@admin_required
def admin_panel():
    return render_template('admin/dashboard.html')

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users     = User.query.all()
    muted_ids = {m.user_id for m in Mute.query.filter(Mute.muted_until > datetime.utcnow()).all()}
    return render_template('admin/users.html', users=users, muted_ids=muted_ids)

@app.route('/admin/users/<int:user_id>/role', methods=['POST'])
@login_required
@admin_required
def change_user_role(user_id):
    new_role    = request.form.get('role', type=int)
    target_user = User.query.get_or_404(user_id)
    if target_user.id == current_user.id:
        flash('Нельзя изменить свою роль', 'error')
    else:
        target_user.role = new_role
        db.session.commit()
        flash(f'Роль {target_user.username} изменена', 'success')
    return redirect(url_for('admin_users'))

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
    # Удаляем реакции на комментарии пользователя
    CommentReaction.query.filter_by(user_id=user_id).delete()
    # Обнуляем author_id у комментариев (или удаляем)
    comments = Comment.query.filter_by(author_id=user_id).all()
    for c in comments:
        CommentReaction.query.filter_by(comment_id=c.id).delete()
    Comment.query.filter_by(author_id=user_id).delete()
    # Удаляем голоса
    Vote.query.filter_by(user_id=user_id).delete()
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
    return render_template('admin/user_profile.html', user=target_user, history=history, muted_ids=muted_ids)

@app.route('/admin/upload-image', methods=['POST'])
@login_required
@editor_required
def upload_image():
    file = request.files.get('file')
    if not file or not allowed_file(file.filename):
        return jsonify(error='Недопустимый файл'), 400
    filename = secure_filename(f"content_{secrets.token_hex(8)}_{file.filename}")
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return jsonify(url=f'/static/uploads/{filename}')

# ========== ФОРУМ ==========

@app.route('/forum')
def forum():
    category     = request.args.get('category', 'all')
    q            = Article.query
    if category in ('article', 'news'):
        q = q.filter_by(category=category)
    articles     = q.order_by(Article.created_at.desc()).all()
    top_comments = Comment.query.order_by(Comment.likes.desc()).limit(5).all()
    return render_template('forum.html', articles=articles,
                           category=category, top_comments=top_comments)

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
    return redirect(url_for('article', article_id=article_id) + '#comments')

@app.route('/extra/<int:page_id>')
def extra_page(page_id):
    page = ExtraPage.query.get_or_404(page_id)
    page.views += 1; db.session.commit()
    return render_template('extra.html', page=page)

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
        if not User.query.filter_by(role=4).first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin'), role=4)
            db.session.add(admin); db.session.commit()
            print('Создан администратор: admin / admin')
    app.run(debug=os.environ.get('FLASK_DEBUG', 'False').lower() == 'true')