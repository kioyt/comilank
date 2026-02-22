import os
from flask import Flask, render_template, jsonify, send_from_directory
import requests

app = Flask(__name__)

# ========== БЕЗОПАСНОСТЬ ==========
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'hard-to-guess-string')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ========== YOUTUBE API ==========
# ID стримов
VIDEO_ID_SHORTS = 'EAfxQz3RbtE'          # YouTube Shorts
VIDEO_ID_HORIZONTAL = 'NzA480NVkr8'      # Горизонтальный стрим

# Ключ API: сначала из переменной окружения (для Render), иначе fallback для локальной разработки
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY')
if not YOUTUBE_API_KEY:
    # ⚠️ Для локальной разработки укажи свой ключ здесь.
    # Перед деплоем на Render либо удали эту строку, либо убедись, что на Render задана переменная окружения.
    YOUTUBE_API_KEY = 'AIzaSyCbJF2Jl2AMdDXemcir4KVnTBJdx3rFUTA'  # замени на свой реальный ключ

# ========== ЗАГОЛОВКИ БЕЗОПАСНОСТИ ==========
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers.pop('Server', None)
    response.headers.pop('X-Powered-By', None)
    return response

# ========== РАЗДАЧА СТАТИКИ ==========
@app.route('/node_modules/<path:filename>')
def serve_node_modules(filename):
    return send_from_directory('node_modules', filename)

@app.route('/src/<path:filename>')
def serve_src(filename):
    return send_from_directory('src', filename)

# ========== ОБЩАЯ ФУНКЦИЯ ЗАПРОСА ==========
def fetch_youtube_viewers(video_id):
    url = f'https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}&key={YOUTUBE_API_KEY}'
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if 'error' in data:
            print(f'YouTube API error for {video_id}:', data['error'])
            return 0, False
        items = data.get('items', [])
        if items:
            details = items[0].get('liveStreamingDetails', {})
            if 'concurrentViewers' in details:
                return int(details['concurrentViewers']), True
        return 0, False
    except Exception as e:
        print(f'Request error for {video_id}:', e)
        return 0, False

# ========== МАРШРУТЫ ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/youtube-viewers')
def youtube_viewers():
    viewers, live = fetch_youtube_viewers(VIDEO_ID_SHORTS)
    return jsonify(viewers=viewers, live=live)

@app.route('/youtube-viewers-horizontal')
def youtube_viewers_horizontal():
    viewers, live = fetch_youtube_viewers(VIDEO_ID_HORIZONTAL)
    return jsonify(viewers=viewers, live=live)

if __name__ == '__main__':
    # Для локальной разработки можно оставить debug=True, на Render debug=False
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode)