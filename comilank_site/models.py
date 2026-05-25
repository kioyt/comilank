# -*- coding: utf-8 -*-
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timedelta

db = SQLAlchemy()


# ========== ПОЛЬЗОВАТЕЛЬ ==========
class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id             = db.Column(db.Integer, primary_key=True)
    username       = db.Column(db.String(80),  unique=True, nullable=False)
    email          = db.Column(db.String(120), unique=True, nullable=False)
    password_hash  = db.Column(db.String(256), nullable=False)
    role           = db.Column(db.Integer, default=0)
    # 0 = обычный пользователь
    # 1 = модератор
    # 2 = редактор (может создавать статьи)
    # 3 = старший модератор
    # 4 = главный администратор / основатель

    created_at     = db.Column(db.DateTime, default=datetime.utcnow)   # дата регистрации
    last_seen      = db.Column(db.DateTime, default=datetime.utcnow)
    banned_until   = db.Column(db.DateTime, nullable=True)
    ban_reason     = db.Column(db.String(500), nullable=True)
    banned_by_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    recovery_key   = db.Column(db.String(256), nullable=True)
    avatar         = db.Column(db.String(300), nullable=True)
    terms_agreed   = db.Column(db.Boolean, default=False)
    privacy_agreed = db.Column(db.Boolean, default=False)

    # Связи
    articles       = db.relationship('Article',  backref='author',     lazy='dynamic', foreign_keys='Article.author_id')
    comments       = db.relationship('Comment',  backref='author',     lazy='dynamic', foreign_keys='Comment.author_id')
    votes          = db.relationship('Vote',     backref='user',       lazy='dynamic')
    extra_pages    = db.relationship('ExtraPage', backref='author',    lazy='dynamic', foreign_keys='ExtraPage.author_id')
    comment_reactions = db.relationship('CommentReaction', backref='user', lazy='dynamic')

    def is_banned(self):
        if self.banned_until and self.banned_until > datetime.utcnow():
            return True
        return False

    def can_ban(self, target):
        """Может ли текущий пользователь банить target."""
        if self.role < 3:
            return False
        if target.role >= self.role:
            return False
        return True

    def can_mute(self, target):
        if self.role < 1:
            return False
        if target.role >= self.role:
            return False
        return True

    def can_ip_ban(self):
        return self.role >= 4

    def can_delete_user(self, target):
        if self.role < 4:
            return False
        if target.role >= self.role:
            return False
        return True

    def __repr__(self):
        return f'<User {self.username} role={self.role}>'


# ========== ИГРА ==========
class Game(db.Model):
    __tablename__ = 'games'

    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(200), nullable=False)
    image   = db.Column(db.String(500), nullable=True)   # обложка игры
    articles = db.relationship('Article', backref='game', lazy='dynamic')

    def __repr__(self):
        return f'<Game {self.name}>'


# ========== СТАТЬЯ ==========
class Article(db.Model):
    __tablename__ = 'articles'

    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(300), nullable=False)
    slug        = db.Column(db.String(350), nullable=True, unique=True)  # красивый URL для TG-превью
    preview     = db.Column(db.Text, nullable=True)       # краткое описание
    content     = db.Column(db.Text, nullable=False)
    image       = db.Column(db.String(500), nullable=True)  # URL изображения (старый способ)
    image_file  = db.Column(db.String(300), nullable=True)  # имя файла загруженного изображения
    video_url   = db.Column(db.String(500), nullable=True)  # ссылка на YouTube
    video_file  = db.Column(db.String(300), nullable=True)  # загруженный видео-файл
    category    = db.Column(db.String(50), default='article')
    # 'article' = статья об игре
    # 'news'    = новость об игре

    # Счётчики
    views       = db.Column(db.Integer, default=0)
    likes       = db.Column(db.Integer, default=0)
    dislikes    = db.Column(db.Integer, default=0)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    author_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    game_id     = db.Column(db.Integer, db.ForeignKey('games.id'), nullable=True)

    # Связи
    comments    = db.relationship('Comment',      backref='article', lazy='dynamic',
                                  cascade='all, delete-orphan')
    votes       = db.relationship('Vote',         backref='article', lazy='dynamic',
                                  cascade='all, delete-orphan')
    extra_pages = db.relationship('ExtraPage',    backref='article', lazy='dynamic',
                                  cascade='all, delete-orphan')
    dropdown_items = db.relationship('DropdownItem', backref='article', lazy='dynamic',
                                     cascade='all, delete-orphan', order_by='DropdownItem.order')
    article_views  = db.relationship('ArticleView',  backref='article', lazy='dynamic',
                                     cascade='all, delete-orphan')

    def get_image_url(self):
        """Возвращает URL изображения (загруженный файл имеет приоритет)."""
        if self.image_file:
            return f'/static/uploads/{self.image_file}'
        if self.image:
            return self.image
        return None

    def recalc_votes(self):
        """Пересчитывает лайки/дизлайки из таблицы Vote."""
        self.likes    = Vote.query.filter_by(article_id=self.id, value=1).count()
        self.dislikes = Vote.query.filter_by(article_id=self.id, value=-1).count()

    def __repr__(self):
        return f'<Article {self.id}: {self.title}>'


# ========== ПРОСМОТР СТАТЬИ (дедупликация) ==========
class ArticleView(db.Model):
    """Хранит факт просмотра статьи пользователем/ip — чтобы не накручивать счётчик."""
    __tablename__ = 'article_views'

    id         = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)   # NULL = гость
    ip_address = db.Column(db.String(45), nullable=True)
    viewed_at  = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_av_article_user', 'article_id', 'user_id'),
        db.Index('ix_av_article_ip',   'article_id', 'ip_address'),
    )


# ========== КОММЕНТАРИЙ ==========
class Comment(db.Model):
    __tablename__ = 'comments'

    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    author_id  = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    parent_id  = db.Column(db.Integer, db.ForeignKey('comments.id'), nullable=True)

    # Счётчики реакций (кешируем для скорости)
    likes      = db.Column(db.Integer, default=0)
    dislikes   = db.Column(db.Integer, default=0)

    replies    = db.relationship('Comment',
                                 backref=db.backref('parent', remote_side='Comment.id'),
                                 lazy='dynamic',
                                 cascade='all, delete-orphan',
                                 primaryjoin='Comment.parent_id == Comment.id',
                                 order_by='Comment.created_at')
    reactions  = db.relationship('CommentReaction', backref='comment', lazy='dynamic',
                                 cascade='all, delete-orphan')

    def recalc_reactions(self):
        self.likes    = CommentReaction.query.filter_by(comment_id=self.id, value=1).count()
        self.dislikes = CommentReaction.query.filter_by(comment_id=self.id, value=-1).count()

    def __repr__(self):
        return f'<Comment {self.id} by user {self.author_id}>'


# ========== РЕАКЦИЯ НА КОММЕНТАРИЙ ==========
class CommentReaction(db.Model):
    __tablename__ = 'comment_reactions'

    id         = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('comments.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    value      = db.Column(db.Integer, nullable=False)   # 1 = лайк, -1 = дизлайк

    __table_args__ = (
        db.UniqueConstraint('comment_id', 'user_id', name='uq_comment_reaction'),
    )


# ========== ГОЛОС ЗА СТАТЬЮ ==========
class Vote(db.Model):
    __tablename__ = 'votes'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    value      = db.Column(db.Integer, nullable=False)   # 1 или -1

    __table_args__ = (
        db.UniqueConstraint('user_id', 'article_id', name='uq_vote'),
    )


# ========== ДОПОЛНИТЕЛЬНАЯ СТРАНИЦА (DLC, секреты и т.д.) ==========
class ExtraPage(db.Model):
    __tablename__ = 'extra_pages'

    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(300), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    views      = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    author_id  = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)


# ========== ПРОСМОТР ДОП. СТРАНИЦЫ (дедупликация) ==========
class ExtraPageView(db.Model):
    """Один уникальный просмотр доп. страницы — строго 1 на IP."""
    __tablename__ = 'extra_page_views'

    id         = db.Column(db.Integer, primary_key=True)
    page_id    = db.Column(db.Integer, db.ForeignKey('extra_pages.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    viewed_at  = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_epv_page_user', 'page_id', 'user_id'),
        db.Index('ix_epv_page_ip',   'page_id', 'ip_address'),
    )


# ========== КОММЕНТАРИЙ К ДОП. СТРАНИЦЕ ==========
class ExtraPageComment(db.Model):
    """Комментарий к дополнительной странице."""
    __tablename__ = 'extra_page_comments'

    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    page_id   = db.Column(db.Integer, db.ForeignKey('extra_pages.id'),          nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'),                 nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('extra_page_comments.id'),   nullable=True)
    likes     = db.Column(db.Integer, default=0)
    dislikes  = db.Column(db.Integer, default=0)

    author  = db.relationship('User', foreign_keys=[author_id])
    replies = db.relationship(
        'ExtraPageComment',
        backref=db.backref('parent', remote_side='ExtraPageComment.id'),
        lazy='dynamic',
        cascade='all, delete-orphan',
        primaryjoin='ExtraPageComment.parent_id == ExtraPageComment.id',
    )


# ========== ВЫПАДАЮЩИЙ ПУНКТ ==========
class DropdownItem(db.Model):
    __tablename__ = 'dropdown_items'

    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    order      = db.Column(db.Integer, default=0)
    is_active  = db.Column(db.Boolean, default=True)

    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    page_id    = db.Column(db.Integer, db.ForeignKey('extra_pages.id'), nullable=True)

    extra_page = db.relationship('ExtraPage', backref='dropdown_items', foreign_keys=[page_id])


# ========== МУТ ==========
class Mute(db.Model):
    __tablename__ = 'mutes'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    muted_until = db.Column(db.DateTime, nullable=False)
    reason      = db.Column(db.String(500), nullable=True)
    muted_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


# ========== IP-БАН ==========
class IPBan(db.Model):
    __tablename__ = 'ip_bans'

    id           = db.Column(db.Integer, primary_key=True)
    ip_address   = db.Column(db.String(45), unique=True, nullable=False)
    banned_until = db.Column(db.DateTime, nullable=False)
    reason       = db.Column(db.String(500), nullable=True)
    banned_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)


# ========== ИСТОРИЯ НАКАЗАНИЙ ==========
class PenaltyHistory(db.Model):
    __tablename__ = 'penalty_history'

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action         = db.Column(db.String(50), nullable=False)   # ban / unban / mute / unmute
    duration       = db.Column(db.String(50), nullable=True)
    reason         = db.Column(db.String(500), nullable=True)
    expires_at     = db.Column(db.DateTime, nullable=True)
    created_by_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    user           = db.relationship('User', foreign_keys=[user_id],        backref='penalty_history')
    created_by     = db.relationship('User', foreign_keys=[created_by_id])


# ========== НАСТРОЙКИ САЙТА ==========
class SiteSettings(db.Model):
    __tablename__ = 'site_settings'

    id                  = db.Column(db.Integer, primary_key=True)
    chaos_button_enabled = db.Column(db.Boolean, default=False)  # кнопка "не нажимай"

    @staticmethod
    def get():
        """Всегда возвращает единственную запись настроек."""
        s = SiteSettings.query.first()
        if not s:
            s = SiteSettings(chaos_button_enabled=False)
            db.session.add(s)
            db.session.commit()
        return s

# ========== РАЗРЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ (тонкая настройка для role 1-3) ==========
class UserPermission(db.Model):
    """Позволяет role=4 выдавать/забирать конкретные возможности у модераторов/редакторов."""
    __tablename__ = 'user_permissions'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    # Доступ к разделам
    can_see_stats      = db.Column(db.Boolean, default=False)   # просмотр статистики
    can_ip_ban         = db.Column(db.Boolean, default=False)   # IP-баны
    can_create_articles= db.Column(db.Boolean, default=True)    # создание статей
    can_edit_games     = db.Column(db.Boolean, default=False)   # редактирование игр
    can_toggle_chaos   = db.Column(db.Boolean, default=False)   # кнопка хаоса
    can_see_penalty    = db.Column(db.Boolean, default=True)    # история наказаний
    can_edit_home      = db.Column(db.Boolean, default=False)   # настройки главной страницы
    can_vpn_detect     = db.Column(db.Boolean, default=False)   # VPN-детектор
    can_broadcast      = db.Column(db.Boolean, default=False)   # массовая email-рассылка
    can_next_stream    = db.Column(db.Boolean, default=False)   # виджет следующего стрима
    can_edit_films     = db.Column(db.Boolean, default=False)   # редактирование фильмов/сериалов
    can_view_reports   = db.Column(db.Boolean, default=False)   # просмотр и закрытие репортов
    can_voice_reply    = db.Column(db.Boolean, default=False)   # голосовые сообщения в комментариях
    can_see_test_tab   = db.Column(db.Boolean, default=False)   # вкладка ТЕСТ на главной

    # Метка: кто и когда выдал
    granted_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    granted_at    = db.Column(db.DateTime, default=datetime.utcnow)

    user       = db.relationship('User', foreign_keys=[user_id],       backref=db.backref('permissions', uselist=False))
    granted_by = db.relationship('User', foreign_keys=[granted_by_id])

    @staticmethod
    def get_or_create(user_id):
        perm = UserPermission.query.filter_by(user_id=user_id).first()
        if not perm:
            perm = UserPermission(user_id=user_id)
            db.session.add(perm)
            db.session.commit()
        return perm


# ═══════════════════════════════════════════════
#  ГЛАВНАЯ СТРАНИЦА — динамические блоки
# ═══════════════════════════════════════════════

class StreamPlatform(db.Model):
    """Платформы стрима: YouTube, Twitch, TikTok, Telegram."""
    __tablename__ = 'stream_platform'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(50),  nullable=False)
    key         = db.Column(db.String(20),  unique=True, nullable=False)  # youtube/twitch/tiktok/telegram
    icon_class  = db.Column(db.String(60),  default='fab fa-youtube')
    color       = db.Column(db.String(20),  default='#ff6600')
    is_live     = db.Column(db.Boolean,     default=False)
    stream_url  = db.Column(db.String(500), default='')   # ссылка на активный стрим
    channel_url = db.Column(db.String(500), default='')   # ссылка на канал (всегда)


class TopViewer(db.Model):
    """Топ активных зрителей (ручное управление)."""
    __tablename__ = 'top_viewer'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    messages      = db.Column(db.Integer, default=0)
    show_messages = db.Column(db.Boolean, default=True)
    position      = db.Column(db.Integer, default=0)
    xp            = db.Column(db.Integer, default=0)


class TopDonator(db.Model):
    """Топ донатеров (ручное управление)."""
    __tablename__ = 'top_donator'

    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    amount   = db.Column(db.Integer, default=0)
    position = db.Column(db.Integer, default=0)


class LastStream(db.Model):
    """Последний стрим — данные задаются в админке."""
    __tablename__ = 'last_stream'

    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(250), default='')
    url           = db.Column(db.String(500), default='')   # основная кликабельная ссылка
    thumbnail_url = db.Column(db.String(500), default='')
    views         = db.Column(db.String(50),  default='')
    yt_url        = db.Column(db.String(500), default='')
    twitch_url    = db.Column(db.String(500), default='')
    tiktok_url    = db.Column(db.String(500), default='')


class StreamMoment(db.Model):
    """Лучшие моменты стрима."""
    __tablename__ = 'stream_moment'

    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(200), nullable=False)
    url           = db.Column(db.String(500), nullable=False)
    thumbnail_url = db.Column(db.String(500), default='')
    views         = db.Column(db.String(50),  default='')
    game          = db.Column(db.String(100), default='')
    position      = db.Column(db.Integer, default=0)


class NextGamePoll(db.Model):
    """Голосование «Во что играем следующий раз»."""
    __tablename__ = 'next_game_poll'

    id     = db.Column(db.Integer, primary_key=True)
    active = db.Column(db.Boolean, default=True)
    games  = db.relationship('PollGame', backref='poll', lazy=True,
                              cascade='all, delete-orphan',
                              order_by='PollGame.position')


class PollGame(db.Model):
    """Игра в опросе."""
    __tablename__ = 'poll_game'

    id        = db.Column(db.Integer, primary_key=True)
    poll_id   = db.Column(db.Integer, db.ForeignKey('next_game_poll.id'), nullable=False)
    name      = db.Column(db.String(100), nullable=False)
    image_url = db.Column(db.String(500), default='')
    votes     = db.Column(db.Integer, default=0)
    position  = db.Column(db.Integer, default=0)


class PollVote(db.Model):
    """1 голос пользователя на опрос."""
    __tablename__ = 'poll_vote'

    id      = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey('next_game_poll.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'),          nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey('poll_game.id'),      nullable=False)

    __table_args__ = (
        db.UniqueConstraint('poll_id', 'user_id', name='uq_poll_user'),
    )

class NextStream(db.Model):
    """Виджет «Следующий стрим» — таймер обратного отсчёта на главной."""
    __tablename__ = 'next_stream'

    id          = db.Column(db.Integer, primary_key=True)
    enabled     = db.Column(db.Boolean, default=False)
    title       = db.Column(db.String(200), default='')
    stream_dt   = db.Column(db.String(30),  default='')   # ISO: 2026-03-20T18:00
    description = db.Column(db.String(300), default='')

# ═══════════════════════════════════════════════
#  ЖАЛОБЫ
# ═══════════════════════════════════════════════

class Report(db.Model):
    __tablename__ = 'reports'

    id             = db.Column(db.Integer,   primary_key=True)
    reporter_id    = db.Column(db.Integer,   db.ForeignKey('users.id'), nullable=True)
    target_type    = db.Column(db.String(20), nullable=False)   # 'comment' | 'user'
    target_id      = db.Column(db.Integer,   nullable=False)
    reason         = db.Column(db.String(500), default='')
    evidence_url   = db.Column(db.String(500), nullable=True)
    created_at     = db.Column(db.DateTime,  default=datetime.utcnow)
    resolved       = db.Column(db.Boolean,   default=False)
    resolved_by_id = db.Column(db.Integer,   db.ForeignKey('users.id'), nullable=True)
    resolved_at    = db.Column(db.DateTime,  nullable=True)
    rejected       = db.Column(db.Boolean,   default=False)   # ложный репорт
    comment_article_id = db.Column(db.Integer, nullable=True)  # статья где находится коммент
    extra_page_id  = db.Column(db.Integer, nullable=True)      # доп. страница

    reporter    = db.relationship('User', foreign_keys=[reporter_id])
    resolved_by = db.relationship('User', foreign_keys=[resolved_by_id])

    def get_target_user(self):
        if self.target_type == 'user':
            return User.query.get(self.target_id)
        return None


# ═══════════════════════════════════════════════
#  СБРОС ПАРОЛЯ ЧЕРЕЗ EMAIL
# ═══════════════════════════════════════════════

class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'

    id         = db.Column(db.Integer,    primary_key=True)
    user_id    = db.Column(db.Integer,    db.ForeignKey('users.id'), nullable=False)
    token      = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime,   default=datetime.utcnow)
    used       = db.Column(db.Boolean,    default=False)

    user = db.relationship('User', backref=db.backref('reset_tokens', lazy='dynamic'))

    def is_valid(self):
        return not self.used and (datetime.utcnow() - self.created_at) < timedelta(minutes=30)


# ═══════════════════════════════════════════════
#  ЛОГ УДАЛЕНИЙ АККАУНТОВ
# ═══════════════════════════════════════════════

class AccountDeletion(db.Model):
    __tablename__ = 'account_deletions'

    id         = db.Column(db.Integer,    primary_key=True)
    username   = db.Column(db.String(80))
    email_hash = db.Column(db.String(64))
    reason     = db.Column(db.String(100))
    deleted_at = db.Column(db.DateTime,   default=datetime.utcnow)

# ═══════════════════════════════════════════════
#  ПОГОДА — управляемые города
# ═══════════════════════════════════════════════

class WeatherCity(db.Model):
    """Города для отображения погоды на главной странице."""
    __tablename__ = 'weather_city'

    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    lat      = db.Column(db.Float,       nullable=False)
    lon      = db.Column(db.Float,       nullable=False)
    tz       = db.Column(db.String(60),  nullable=False)
    landmark = db.Column(db.String(150), default='')
    position = db.Column(db.Integer,     default=0)
    is_active= db.Column(db.Boolean,     default=True)


class UserCityShare(db.Model):
    """Город пользователя — поделился геолокацией."""
    __tablename__ = 'user_city_share'

    id        = db.Column(db.Integer,     primary_key=True)
    user_id   = db.Column(db.Integer,     db.ForeignKey('users.id'), nullable=True)
    city_name = db.Column(db.String(150), nullable=False)
    lat       = db.Column(db.Float,       nullable=False)
    lon       = db.Column(db.Float,       nullable=False)
    tz        = db.Column(db.String(60),  default='')
    shared_at = db.Column(db.DateTime,    default=datetime.utcnow)
    ip_hash   = db.Column(db.String(64),  default='')

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('city_shares', lazy='dynamic'))

# ═══════════════════════════════════════════════
#  УВЕДОМЛЕНИЯ
# ═══════════════════════════════════════════════

class Notification(db.Model):
    """Уведомление пользователю: ответ на комментарий или лайк."""
    __tablename__ = 'notifications'

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)   # кому
    actor_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)   # кто
    notif_type    = db.Column(db.String(20), nullable=False)  # 'reply' | 'like'
    comment_id    = db.Column(db.Integer, db.ForeignKey('comments.id'), nullable=True)
    article_id    = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True)
    preview       = db.Column(db.String(200), default='')   # превью текста ответа
    is_read       = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    user   = db.relationship('User', foreign_keys=[user_id],  backref=db.backref('notifications', lazy='dynamic'))
    actor  = db.relationship('User', foreign_keys=[actor_id])
    comment= db.relationship('Comment', foreign_keys=[comment_id])
    article= db.relationship('Article', foreign_keys=[article_id])


# ═══════════════════════════════════════════════
#  МЕССЕНДЖЕР
# ═══════════════════════════════════════════════

class MessengerChat(db.Model):
    """Чат — личный (DM) или групповой."""
    __tablename__ = 'messenger_chats'

    id            = db.Column(db.Integer, primary_key=True)
    is_group      = db.Column(db.Boolean, default=False)
    name          = db.Column(db.String(100), nullable=True)   # только для групп
    avatar        = db.Column(db.String(300), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    pinned_msg_id = db.Column(db.Integer, nullable=True)

    members  = db.relationship('ChatMember',  backref='chat', lazy='dynamic',
                                cascade='all,delete-orphan')
    messages = db.relationship('ChatMessage', backref='chat', lazy='dynamic',
                                primaryjoin='MessengerChat.id == ChatMessage.chat_id',
                                cascade='all,delete-orphan')

    @property
    def last_message(self):
        return self.messages.order_by(ChatMessage.id.desc()).first()

    def display_name(self, user):
        if self.is_group:
            return self.name or 'Группа'
        other = self.other_user(user)
        return other.username if other else 'Удалён'

    def other_user(self, user):
        m = self.members.filter(ChatMember.user_id != user.id).first()
        return m.user if m else None

    def unread_count(self, user):
        member = self.members.filter_by(user_id=user.id).first()
        if not member or not member.last_read_at:
            return self.messages.filter(ChatMessage.author_id != user.id).count()
        return self.messages.filter(
            ChatMessage.created_at > member.last_read_at,
            ChatMessage.author_id != user.id
        ).count()

    def is_member(self, user):
        return self.members.filter_by(user_id=user.id).first() is not None


class ChatMember(db.Model):
    """Участник чата."""
    __tablename__ = 'chat_members'

    id           = db.Column(db.Integer, primary_key=True)
    chat_id      = db.Column(db.Integer, db.ForeignKey('messenger_chats.id'), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_admin     = db.Column(db.Boolean, default=False)
    joined_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_read_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('chat_memberships', lazy='dynamic'))

    __table_args__ = (db.UniqueConstraint('chat_id', 'user_id', name='uq_chat_member'),)


class ChatMessage(db.Model):
    """Сообщение в чате."""
    __tablename__ = 'chat_messages'

    id          = db.Column(db.Integer, primary_key=True)
    chat_id     = db.Column(db.Integer, db.ForeignKey('messenger_chats.id'), nullable=False)
    author_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    text        = db.Column(db.Text, nullable=True)
    image_url   = db.Column(db.String(300), nullable=True)
    file_url    = db.Column(db.String(300), nullable=True)
    file_name   = db.Column(db.String(200), nullable=True)
    file_size   = db.Column(db.Integer, nullable=True)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('chat_messages.id'), nullable=True)
    edited      = db.Column(db.Boolean, default=False)
    is_read     = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    author   = db.relationship('User', foreign_keys=[author_id],
                               backref=db.backref('chat_messages', lazy='dynamic'))
    reply_to = db.relationship('ChatMessage', foreign_keys=[reply_to_id],
                               remote_side='ChatMessage.id')
    reactions = db.relationship('MsgReaction', backref='message', lazy='dynamic',
                                cascade='all,delete-orphan')

    def reactions_grouped(self):
        result = {}
        for r in self.reactions:
            result.setdefault(r.emoji, []).append(r.user_id)
        return result

    def file_size_str(self):
        if not self.file_size:
            return ''
        if self.file_size < 1024:
            return f'{self.file_size} Б'
        if self.file_size < 1048576:
            return f'{self.file_size // 1024} КБ'
        return f'{self.file_size // 1048576} МБ'

    def to_dict(self):
        return {
            'id':           self.id,
            'chat_id':      self.chat_id,
            'author_id':    self.author_id,
            'author_name':  self.author.username if self.author else '?',
            'author_avatar': self.author.avatar if self.author else None,
            'text':         self.text,
            'image_url':    self.image_url,
            'file_url':     self.file_url,
            'file_name':    self.file_name,
            'reply_to_id':  self.reply_to_id,
            'edited':       self.edited,
            'is_read':      self.is_read,
            'time':         self.created_at.strftime('%H:%M'),
            'ts':           self.created_at.isoformat(),
        }


class MsgReaction(db.Model):
    """Реакция (эмодзи) на сообщение."""
    __tablename__ = 'msg_reactions'

    id      = db.Column(db.Integer, primary_key=True)
    msg_id  = db.Column(db.Integer, db.ForeignKey('chat_messages.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    emoji   = db.Column(db.String(10), nullable=False)

    __table_args__ = (db.UniqueConstraint('msg_id', 'user_id', 'emoji', name='uq_msg_reaction'),)


class TypingStatus(db.Model):
    """Статус «печатает» — обновляется каждые ~3 сек."""
    __tablename__ = 'typing_status'

    id      = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('messenger_chats.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    ts      = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('chat_id', 'user_id', name='uq_typing'),)

# ═══════════════════════════════════════════════
#  КОМНАТЫ И КАНАЛЫ (публичные чаты)
# ═══════════════════════════════════════════════

class Room(db.Model):
    """Публичная комната или канал."""
    __tablename__ = 'rooms'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    slug        = db.Column(db.String(100), unique=True, nullable=False)  # URL-friendly id
    description = db.Column(db.String(500), default='')
    category    = db.Column(db.String(50), default='general')  # games, music, sports, etc.
    room_type   = db.Column(db.String(10), default='room')     # 'room' | 'channel'
    avatar      = db.Column(db.String(300), nullable=True)
    banner      = db.Column(db.String(300), nullable=True)
    is_active    = db.Column(db.Boolean, default=True)
    is_private   = db.Column(db.Boolean, default=False)   # приватная — только по ссылке/запросу
    is_nsfw      = db.Column(db.Boolean, default=False)
    invite_token = db.Column(db.String(32), unique=True, nullable=True)  # токен приглашения
    pinned_msg_id= db.Column(db.Integer, nullable=True)
    rules        = db.Column(db.Text, default='')
    verified     = db.Column(db.Boolean, default=False)   # верифицированный (галочка)
    is_featured  = db.Column(db.Boolean, default=False)   # закреплён в боковом меню
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    owner_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    owner    = db.relationship('User', foreign_keys=[owner_id],
                               backref=db.backref('owned_rooms', lazy='dynamic'))
    members  = db.relationship('RoomMember', backref='room', lazy='dynamic',
                               cascade='all,delete-orphan')
    messages = db.relationship('RoomMessage', backref='room', lazy='dynamic',
                               cascade='all,delete-orphan')

    @property
    def member_count(self):
        return self.members.count()

    @property
    def last_message(self):
        return self.messages.order_by(RoomMessage.id.desc()).first()

    def is_member(self, user):
        return self.members.filter_by(user_id=user.id).first() is not None

    def get_member(self, user):
        return self.members.filter_by(user_id=user.id).first()

    def unread_count(self, user):
        m = self.members.filter_by(user_id=user.id).first()
        if not m or not m.last_read_at:
            return 0
        return self.messages.filter(
            RoomMessage.created_at > m.last_read_at,
            RoomMessage.author_id != user.id
        ).count()

    def __repr__(self):
        return f'<Room {self.slug}>'




class RoomMessageRead(db.Model):
    __tablename__ = 'room_message_reads'
    id      = db.Column(db.Integer, primary_key=True)
    msg_id  = db.Column(db.Integer, db.ForeignKey('room_messages.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('msg_id','user_id'),)
    user = db.relationship('User', foreign_keys=[user_id])

class RoomMember(db.Model):
    """Участник публичной комнаты."""
    __tablename__ = 'room_members'

    id           = db.Column(db.Integer, primary_key=True)
    room_id      = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role         = db.Column(db.String(20), default='member')
    # role: 'member' | 'moderator' | 'admin' | 'owner'
    joined_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_read_at = db.Column(db.DateTime, nullable=True)
    is_banned    = db.Column(db.Boolean, default=False)
    is_muted     = db.Column(db.Boolean, default=False)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('room_memberships', lazy='dynamic'))

    __table_args__ = (db.UniqueConstraint('room_id', 'user_id', name='uq_room_member'),)


class RoomMessage(db.Model):
    """Сообщение в публичной комнате."""
    __tablename__ = 'room_messages'

    id          = db.Column(db.Integer, primary_key=True)
    room_id     = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    author_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    text        = db.Column(db.Text, nullable=True)
    image_url   = db.Column(db.String(300), nullable=True)
    file_url    = db.Column(db.String(300), nullable=True)
    file_name   = db.Column(db.String(200), nullable=True)
    file_size   = db.Column(db.Integer, nullable=True)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('room_messages.id'), nullable=True)
    edited      = db.Column(db.Boolean, default=False)
    is_pinned   = db.Column(db.Boolean, default=False)
    deleted     = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    msg_type    = db.Column(db.String(20), default='text')  # 'text'|'system'|'post'

    author   = db.relationship('User', foreign_keys=[author_id],
                               backref=db.backref('room_messages', lazy='dynamic'))
    reply_to = db.relationship('RoomMessage', foreign_keys=[reply_to_id],
                               remote_side='RoomMessage.id')
    reactions = db.relationship('RoomReaction', backref='message', lazy='dynamic',
                                cascade='all,delete-orphan')

    def reactions_grouped(self):
        result = {}
        for r in self.reactions:
            result.setdefault(r.emoji, []).append(r.user_id)
        return result

    def file_size_str(self):
        if not self.file_size:
            return ''
        if self.file_size < 1024:
            return f'{self.file_size} Б'
        if self.file_size < 1048576:
            return f'{self.file_size // 1024} КБ'
        return f'{self.file_size // 1048576} МБ'

    def to_dict(self):
        return {
            'id':          self.id,
            'room_id':     self.room_id,
            'author_id':   self.author_id,
            'author_name': self.author.username if self.author else 'Система',
            'author_avatar': self.author.avatar if self.author else None,
            'author_role': self.room.get_member(self.author).role if self.author and self.room.get_member(self.author) else 'member',
            'text':        self.text,
            'image_url':   self.image_url,
            'file_url':    self.file_url,
            'file_name':   self.file_name,
            'reply_to_id': self.reply_to_id,
            'edited':      self.edited,
            'deleted':     self.deleted,
            'msg_type':    self.msg_type,
            'time':        self.created_at.strftime('%H:%M'),
            'ts':          self.created_at.isoformat(),
        }


class RoomReaction(db.Model):
    """Реакция на сообщение в комнате."""
    __tablename__ = 'room_reactions'

    id      = db.Column(db.Integer, primary_key=True)
    msg_id  = db.Column(db.Integer, db.ForeignKey('room_messages.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    emoji   = db.Column(db.String(10), nullable=False)

    __table_args__ = (db.UniqueConstraint('msg_id', 'user_id', 'emoji', name='uq_room_reaction'),)


class RoomApplication(db.Model):
    """Заявка на создание комнаты/канала."""
    __tablename__ = 'room_applications'

    id          = db.Column(db.Integer, primary_key=True)
    applicant_id= db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), default='')
    category    = db.Column(db.String(50), default='general')
    room_type   = db.Column(db.String(10), default='room')  # 'room' | 'channel'
    reason      = db.Column(db.Text, default='')  # зачем нужна комната
    quiz_q1     = db.Column(db.String(500), default='')   # ответы на вопросы
    quiz_q2     = db.Column(db.String(500), default='')
    quiz_q3     = db.Column(db.String(500), default='')
    status      = db.Column(db.String(20), default='pending')
    # status: 'pending' | 'approved' | 'rejected'
    admin_note  = db.Column(db.String(500), nullable=True)  # комментарий админа
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    room_id     = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=True)
    # room_id заполняется после одобрения

    applicant   = db.relationship('User', foreign_keys=[applicant_id],
                                  backref=db.backref('room_applications', lazy='dynamic'))
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])
    room        = db.relationship('Room', foreign_keys=[room_id])

    def __repr__(self):
        return f'<RoomApplication {self.name} [{self.status}]>'


class RoomTypingStatus(db.Model):
    """Кто печатает в комнате."""
    __tablename__ = 'room_typing_status'

    id      = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    ts      = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('room_id', 'user_id', name='uq_room_typing'),)


class RoomJoinRequest(db.Model):
    """Запрос на вступление в приватную комнату."""
    __tablename__ = 'room_join_requests'

    id         = db.Column(db.Integer, primary_key=True)
    room_id    = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status     = db.Column(db.String(20), default='pending')
    # 'pending' | 'approved' | 'rejected'
    message    = db.Column(db.String(300), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at= db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('room_join_requests', lazy='dynamic'))
    room = db.relationship('Room', foreign_keys=[room_id],
                           backref=db.backref('join_requests', lazy='dynamic'))

    __table_args__ = (db.UniqueConstraint('room_id', 'user_id', name='uq_join_request'),)


# ═══════════════════════════════════════════════
#  ПОДПИСКИ НА СТАТЬИ (отслеживание)
# ═══════════════════════════════════════════════

class ArticleSubscription(db.Model):
    """Пользователь подписан на обновления статьи / новые доп.статьи."""
    __tablename__ = 'article_subscriptions'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user    = db.relationship('User',    foreign_keys=[user_id],
                              backref=db.backref('article_subscriptions', lazy='dynamic'))
    article = db.relationship('Article', foreign_keys=[article_id],
                              backref=db.backref('subscribers', lazy='dynamic'))

    __table_args__ = (db.UniqueConstraint('user_id', 'article_id', name='uq_article_sub'),)


# ═══════════════════════════════════════════════
#  WEB PUSH ПОДПИСКИ (браузерные push-уведомления)
# ═══════════════════════════════════════════════

class PushSubscription(db.Model):
    """Хранит endpoint и ключи Web Push подписки пользователя."""
    __tablename__ = 'push_subscriptions'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    endpoint   = db.Column(db.Text, nullable=False)
    p256dh     = db.Column(db.Text, nullable=False)   # ключ шифрования
    auth       = db.Column(db.Text, nullable=False)   # auth секрет
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('push_subscriptions', lazy='dynamic'))

    # Один endpoint = одна запись
    __table_args__ = (db.Index('ix_push_endpoint', 'endpoint'),)

