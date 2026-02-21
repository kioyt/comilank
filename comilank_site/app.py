import os
from flask import Flask, render_template, jsonify, send_from_directory
import requests

app = Flask(__name__)

# ⚠️ ВСТАВЬТЕ СВОЙ API-КЛЮЧ YOUTUBE (начинается с AIza...)
YOUTUBE_API_KEY = 'AIzaSyDo6Y4cxMa073cjkkuVDeuKColw9JbcK3w'  # замените на свой реальный ключ
VIDEO_ID = 'zYrpEl61uCI'

# Маршрут для раздачи файлов из node_modules
@app.route('/node_modules/<path:filename>')
def serve_node_modules(filename):
    return send_from_directory('node_modules', filename)

# Маршрут для раздачи ваших скриптов из папки src
@app.route('/src/<path:filename>')
def serve_src(filename):
    return send_from_directory('src', filename)

def get_youtube_viewers():
    url = f'https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={VIDEO_ID}&key={YOUTUBE_API_KEY}'
    try:
        resp = requests.get(url)
        data = resp.json()
        if 'error' in data:
            return 0, False
        items = data.get('items', [])
        if items:
            details = items[0].get('liveStreamingDetails', {})
            if 'concurrentViewers' in details:
                return int(details['concurrentViewers']), True
        return 0, False
    except Exception as e:
        print('Ошибка при запросе к YouTube API:', e)
        return 0, False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/youtube-viewers')
def youtube_viewers():
    viewers, live = get_youtube_viewers()
    return jsonify(viewers=viewers, live=live)

if __name__ == '__main__':
    app.run(debug=True)