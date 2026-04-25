"""Entry point for the Flask web app.

Usage:
    pip install -r requirements.txt
    python run.py

Then open http://127.0.0.1:5000 in a browser. The default admin user is:
    Username: Admin
    Password: Welcome@1
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
