"""
Dorm Bath Monitor - Cloud Distribution Server
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
templates / static フォルダが存在しない場合でも、単体1ファイルで完全動作する自己完結型設計。
"""
import os
import time
import datetime
from flask import Flask, render_template, render_template_string, jsonify, request

app = Flask(__name__)

# セキュリティAPIキー（ラズパイからの更新リクエストを保護）
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

OFFLINE_THRESHOLD_SEC = 30.0

# フォルダが存在しない場合の自己完結型インラインHTML
INLINE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>寮風呂場 混雑状況モニター</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>♨</text></svg>">
  <style>
    :root {
      --primary-color: #0284c7;
      --primary-light: #e0f2fe;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --bg-color: #f1f5f9;
      --card-bg: #ffffff;
      --success-color: #10b981;
      --warning-color: #f59e0b;
      --danger-color: #ef4444;
      --radius-lg: 16px;
      --radius-md: 10px;
      --shadow-sm: 0 2px 4px rgba(0, 0, 0, 0.05);
      --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.08);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
      background-color: var(--bg-color);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      justify-content: center;
      align-items: flex-start;
      padding: 16px;
    }
    .container { width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 16px; }
    .header { display: flex; justify-content: space-between; align-items: center; padding: 8px 4px; }
    .logo { display: flex; align-items: center; gap: 10px; }
    .logo .icon {
      font-size: 28px; background: #e0f2fe; color: #0284c7; width: 44px; height: 44px;
      border-radius: 50%; display: flex; align-items: center; justify-content: center;
    }
    .logo h1 { font-size: 1.25rem; font-weight: 700; color: #1e293b; }
    .cloud-badge {
      display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px;
      background: #ffffff; border: 1px solid #e2e8f0; border-radius: 20px;
      font-size: 0.78rem; font-weight: 600; color: #475569; box-shadow: var(--shadow-sm);
    }
    .status-indicator-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-online { background-color: #10b981; box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2); }
    .dot-offline { background-color: #ef4444; box-shadow: 0 0 0 2px rgba(239, 68, 68, 0.2); }
    .main-card {
      background: var(--card-bg); border-radius: var(--radius-lg); padding: 24px 20px;
      box-shadow: var(--shadow-md); display: flex; flex-direction: column; gap: 20px;
    }
    .status-banner {
      padding: 10px 14px; border-radius: var(--radius-md); font-size: 0.85rem; font-weight: 600;
      text-align: center; transition: all 0.3s ease;
    }
    .banner-active { background-color: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }
    .banner-inactive { background-color: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
    .occupancy-display { text-align: center; padding: 10px 0; }
    .occupancy-display .label { font-size: 0.95rem; color: var(--text-muted); font-weight: 600; margin-bottom: 6px; }
    .count-wrapper { display: flex; justify-content: center; align-items: baseline; gap: 4px; }
    .count-number {
      font-size: 4.5rem; font-weight: 800; line-height: 1; color: var(--primary-color);
      font-variant-numeric: tabular-nums; transition: color 0.3s;
    }
    .count-unit { font-size: 1.5rem; font-weight: 700; color: var(--text-muted); }
    .capacity-info { font-size: 0.9rem; color: var(--text-muted); margin-top: 8px; }
    .meter-card { background: #f8fafc; padding: 14px; border-radius: var(--radius-md); border: 1px solid #e2e8f0; }
    .meter-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.9rem; font-weight: 600; margin-bottom: 10px; }
    .level-badge { padding: 4px 10px; border-radius: 20px; font-size: 0.82rem; font-weight: 700; }
    .badge-empty { background-color: #d1fae5; color: #065f46; }
    .badge-medium { background-color: #fef3c7; color: #92400e; }
    .badge-crowded { background-color: #fee2e2; color: #991b1b; }
    .progress-bar { height: 14px; background: #e2e8f0; border-radius: 8px; overflow: hidden; margin-bottom: 8px; }
    .progress-fill { height: 100%; border-radius: 8px; transition: width 0.4s ease, background-color 0.4s ease; background-color: var(--success-color); }
    .meter-legend { display: flex; justify-content: space-between; font-size: 0.82rem; color: var(--text-muted); }
    .stability-notice { display: flex; align-items: center; justify-content: center; gap: 8px; font-size: 0.82rem; color: #64748b; background: #f1f5f9; padding: 8px; border-radius: 8px; }
    .pulse-dot { width: 8px; height: 8px; background-color: #0ea5e9; border-radius: 50%; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0% { transform: scale(0.95); opacity: 0.7; } 50% { transform: scale(1.3); opacity: 1; } 100% { transform: scale(0.95); opacity: 0.7; } }
    .update-info { display: flex; justify-content: center; align-items: center; gap: 8px; font-size: 0.85rem; color: var(--text-muted); }
    .btn-refresh { background: none; border: none; font-size: 1.1rem; cursor: pointer; padding: 4px; border-radius: 4px; }
    .footer { text-align: center; font-size: 0.75rem; color: #94a3b8; display: flex; flex-direction: column; gap: 6px; padding-bottom: 20px; }
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div class="logo">
        <span class="icon">♨</span>
        <h1>寮風呂場 混雑状況</h1>
      </div>
      <div class="cloud-badge" id="onlineBadge" title="ラズパイとの接続状態">
        <span class="status-indicator-dot dot-offline" id="onlineDot"></span>
        <span id="onlineText">接続確認中...</span>
      </div>
    </header>

    <main class="main-card">
      <div id="scheduleBanner" class="status-banner banner-active">
        <span id="scheduleText">最新の混雑情報を取得しています...</span>
      </div>

      <div class="occupancy-display">
        <div class="label">現在の入浴中人数</div>
        <div class="count-wrapper">
          <span id="currentCount" class="count-number">--</span>
          <span class="count-unit">人</span>
        </div>
        <div class="capacity-info">
          （靴箱収容枠: <span id="maxSlots">36</span>人中）
        </div>
      </div>

      <div class="meter-card">
        <div class="meter-header">
          <span>混雑度目安</span>
          <span id="crowdLevelText" class="level-badge badge-empty">確認中</span>
        </div>
        <div class="progress-bar">
          <div id="crowdProgress" class="progress-fill" style="width: 0%;"></div>
        </div>
        <div class="meter-legend">
          <span>空き: <strong id="emptySlots">--</strong> 枠</span>
          <span>最大: 36 枠</span>
        </div>
      </div>

      <div id="stabilityNotice" class="stability-notice" style="display: none;">
        <span class="pulse-dot"></span> 人影・出し入れ検知中（人数を安定保持しています）
      </div>

      <div class="update-info">
        最終データ更新: <span id="updatedAt">--:--:--</span>
        <button id="refreshBtn" class="btn-refresh" title="手動更新">🔄</button>
      </div>
    </main>

    <footer class="footer">
      <p>※ 本システムはRaspberry Pi 4 ＋ AI画像認識により靴箱スリッパから自動検知しています。</p>
      <p>寮風呂場 利用状況クラウド配信システム (Edge AI ＋ Cloud)</p>
    </footer>
  </div>

  <script>
    document.addEventListener("DOMContentLoaded", () => {
      const currentCountEl = document.getElementById("currentCount");
      const maxSlotsEl = document.getElementById("maxSlots");
      const emptySlotsEl = document.getElementById("emptySlots");
      const crowdLevelTextEl = document.getElementById("crowdLevelText");
      const crowdProgressEl = document.getElementById("crowdProgress");
      const scheduleBannerEl = document.getElementById("scheduleBanner");
      const scheduleTextEl = document.getElementById("scheduleText");
      const stabilityNoticeEl = document.getElementById("stabilityNotice");
      const updatedAtEl = document.getElementById("updatedAt");
      const refreshBtn = document.getElementById("refreshBtn");
      const onlineDotEl = document.getElementById("onlineDot");
      const onlineTextEl = document.getElementById("onlineText");

      async function fetchStatus() {
        try {
          const res = await fetch("/api/status");
          if (!res.ok) throw new Error("Network error");
          const data = await res.json();

          currentCountEl.textContent = data.count;
          maxSlotsEl.textContent = data.max_slots;
          emptySlotsEl.textContent = data.empty_slots;
          updatedAtEl.textContent = data.updated_at;

          const ratio = data.max_slots > 0 ? (data.count / data.max_slots) * 100 : 0;
          crowdProgressEl.style.width = `${ratio}%`;
          crowdLevelTextEl.textContent = data.crowd_level;

          crowdLevelTextEl.className = "level-badge";
          if (data.crowd_class === "empty") {
            crowdLevelTextEl.classList.add("badge-empty");
            crowdProgressEl.style.backgroundColor = "var(--success-color)";
          } else if (data.crowd_class === "medium") {
            crowdLevelTextEl.classList.add("badge-medium");
            crowdProgressEl.style.backgroundColor = "var(--warning-color)";
          } else {
            crowdLevelTextEl.classList.add("badge-crowded");
            crowdProgressEl.style.backgroundColor = "var(--danger-color)";
          }

          if (data.is_online) {
            onlineDotEl.className = "status-indicator-dot dot-online";
            onlineTextEl.textContent = "Live（受信中）";
          } else {
            onlineDotEl.className = "status-indicator-dot dot-offline";
            onlineTextEl.textContent = "通信待機中";
          }

          scheduleTextEl.textContent = data.schedule_message;
          if (data.is_active && data.is_online) {
            scheduleBannerEl.className = "status-banner banner-active";
          } else {
            scheduleBannerEl.className = "status-banner banner-inactive";
          }

          if (data.is_occluded) {
            stabilityNoticeEl.style.display = "flex";
          } else {
            stabilityNoticeEl.style.display = "none";
          }
        } catch (err) {
          console.error("Failed to fetch bath status:", err);
          scheduleBannerEl.className = "status-banner banner-inactive";
          scheduleTextEl.textContent = "サーバーとの通信が一時的に途絶えています";
          onlineDotEl.className = "status-indicator-dot dot-offline";
          onlineTextEl.textContent = "オフライン";
        }
      }

      fetchStatus();
      setInterval(fetchStatus, 3000);
      if (refreshBtn) {
        refreshBtn.addEventListener("click", () => {
          refreshBtn.style.transform = "rotate(360deg)";
          setTimeout(() => { refreshBtn.style.transform = "none"; }, 300);
          fetchStatus();
        });
      }
    });
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    """スマホ・一般利用者向け配信トップページ (templatesフォルダがあればそれを使い、無ければ内蔵HTMLで表示)"""
    template_path = os.path.join(app.root_path, "templates", "index.html")
    if os.path.exists(template_path):
        return render_template("index.html")
    return render_template_string(INLINE_HTML)


@app.route("/api/status")
def api_status():
    """フロントエンドから2〜3秒おきにポーリングされる状態API"""
    now = time.time()
    last_recv = latest_status["last_received_timestamp"]
    
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
