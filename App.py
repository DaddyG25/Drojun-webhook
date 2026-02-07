from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("ALERT RECEIVED:", data)
    return jsonify({"status": "ok"})

@app.route("/", methods=["GET"])
def home():
    return "DROJUN WEBHOOK LIVE"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
