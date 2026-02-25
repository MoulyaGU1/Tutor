from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Tutor App Running 🚀"

# Important for Vercel
app = app