from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

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
    recovery_key   = db.Column(db.String(256), nullable=True)  # хеш секретного ключа восстановления
    avatar         = db.Column(db.String(300), nullable=True)   # путь к аватарке

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