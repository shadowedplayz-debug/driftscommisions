from flask import Flask
from threading import Thread
import time
import requests

app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

# This background loop pings your own server every 4 minutes automatically
def self_ping():
    while True:
        try:
            time.sleep(240)  # Waits 4 minutes
            requests.get("http://127.0.0.1:8080/")
        except Exception as e:
            print(f"Self-ping error: {e}")

def keep_alive():
    # Start the Flask web server
    t = Thread(target=run)
    t.start()

    # Start the self-ping loop
    p = Thread(target=self_ping)
    p.start()
