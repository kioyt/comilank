import os
from flask import Flask, render_template, jsonify
import requests

app = Flask(__name__)

# ВСТАВЬ СЮДА СВОЙ API-КЛЮЧ (начинается с AIza...)
YOUTUBE_API_KEY = 'AIzaSy...'  # замени на свой ключ
VIDEO_ID = 'zYrpEl61uCI'

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
    except:
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
