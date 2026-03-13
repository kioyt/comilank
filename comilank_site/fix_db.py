# fix_db.py — запусти командой: python fix_db.py
from app import app, db
from sqlalchemy import text, inspect

with app.app_context():
    inspector = inspect(db.engine)
    columns = [c['name'] for c in inspector.get_columns('users')]

    # Добавляем created_at если нет
    if 'created_at' not in columns:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
            conn.commit()
        print("OK: добавлена колонка created_at")
    else:
        print("OK: created_at уже существует")

    db.create_all()
    print("OK: все таблицы созданы")
    print("ГОТОВО!")