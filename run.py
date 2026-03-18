import os
from dotenv import load_dotenv

load_dotenv()

# (Optional debug – remove later)
print(f"DEBUG run.py: GEMINI_API_KEY = {os.getenv('GEMINI_API_KEY')}")

from app import create_app, db

app = create_app()

# ✅ ADD THIS BLOCK (VERY IMPORTANT)
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)