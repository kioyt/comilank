from flask import Flask, render_template

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hdjd94j№jm@K2d'
app.config['WTF_CSRF_ENABLED'] = True

@app.route('/')
def home():
    return render_template('index.html')
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

if __name__ == '__main__':
    app.run()