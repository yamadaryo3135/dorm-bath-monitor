"""
Dorm Bath Monitor - Cloud Distribution Server
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
"""
import os
import time
import datetime
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# セキュリティAPIキー（ラズパイからの更新リクエストを保護）
# 環境変数 API_KEY があればそれを使用、なければデフォルト
API_KEY = os.environ.get("API_KEY", "bath_monitor_secret_key_2026")

# 最新の利用状況キャッシュ
latest_status = {
    "count": 0,
    "max_slots": 36,
    "empty_slots": 36,
    "crowd_level": "空いています",
    "crowd_class": "empty",
    "schedule_message": "データ受信待機中...",
    "is_active": True,
    "is_occluded": False,
    "updated_at": "--:--:--",
    "last_received_timestamp": 0.0,
    "is_online": False
}

# ラズパイからの通信が途絶えたと判定する秒数（例: 30秒間更新がなければオフライン表示）
OFFLINE_THRESHOLD_SEC = 30.0


@app.route("/")
def index():
    """スマホ・一般利用者向け配信トップページ"""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """フロントエンドから2〜3秒おきにポーリングされる状態API"""
    now = time.time()
    last_recv = latest_status["last_received_timestamp"]
    
    # ラズパイからの受信が30秒以上途絶えているか判定
    if last_recv == 0:
        is_online = False
        schedule_msg = "ラズベリーパイからのデータ送信を待機しています..."
    elif now - last_recv > OFFLINE_THRESHOLD_SEC:
        is_online = False
        diff_min = int((now - last_recv) // 60)
        schedule_msg = f"⚠ ラズパイ通信停止中 (最終受信: {diff_min}分前)"
    else:
        is_online = True
        schedule_msg = latest_status["schedule_message"]

    res_data = dict(latest_status)
    res_data["is_online"] = is_online
    res_data["schedule_message"] = schedule_msg
    return jsonify(res_data)


@app.route("/api/update", methods=["POST"])
def api_update():
    """ラズベリーパイから人数データを受け取るエンドポイント"""
    # 認証トークンの確認
    auth_header = request.headers.get("X-Api-Key") or request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    else:
        token = auth_header

    if token != API_KEY:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    now_str = datetime.datetime.now().strftime("%H:%M:%S")

    # キャッシュを更新
    latest_status["count"] = payload.get("count", latest_status["count"])
    latest_status["max_slots"] = payload.get("max_slots", latest_status["max_slots"])
    latest_status["empty_slots"] = payload.get("empty_slots", latest_status["empty_slots"])
    latest_status["crowd_level"] = payload.get("crowd_level", latest_status["crowd_level"])
    latest_status["crowd_class"] = payload.get("crowd_class", latest_status["crowd_class"])
    latest_status["schedule_message"] = payload.get("schedule_message", latest_status["schedule_message"])
    latest_status["is_active"] = payload.get("is_active", latest_status["is_active"])
    latest_status["is_occluded"] = payload.get("is_occluded", latest_status["is_occluded"])
    latest_status["updated_at"] = payload.get("updated_at", now_str)
    latest_status["last_received_timestamp"] = time.time()
    latest_status["is_online"] = True

    return jsonify({"status": "ok", "message": "Updated successfully", "server_time": now_str})


@app.route("/health")
def health():
    """Render等の死活監視用エンドポイント"""
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
