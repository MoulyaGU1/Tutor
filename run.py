import os
from dotenv import load_dotenv

load_dotenv()

# (Optional debug – remove later)
print(f"DEBUG run.py: GEMINI_API_KEY = {os.getenv('GEMINI_API_KEY')}")

from app import create_app, db
from seed import seed_database   # ✅ ADD THIS

app = create_app()

# ✅ CREATE TABLES + AUTO SEED
with app.app_context():
    db.create_all()
    seed_database()   # ✅ THIS LINE IS MISSING

if __name__ == '__main__':
    app.run(debug=True)