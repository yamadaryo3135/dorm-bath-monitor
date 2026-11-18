"""
Dorm Bath Monitor - Cloud Distribution & Full Remote Management Server
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
templates / static フォルダが存在しない場合でも、単体1ファイルで完全動作する自己完結型設計。
お風呂場のラズパイと同一の本格システム管理画面（黒基調・全7パネル＋入退室統計ログ＋4種CSVダウンロード）を
自室PCからブラウザで遠隔操作・双方向自動同期可能。
"""
import os
import io
import csv
import time
import base64
import datetime
from functools import wraps
from flask import Flask, render_template, render_template_string, jsonify, request, session, redirect, url_for, Response

app = Flask(__name__)

# セキュリティ設定
API_KEY = os.environ.get("API_KEY", "bath_monitor_secret_key_2026")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "daaaayama")
app.secret_key = os.environ.get("SECRET_KEY", "bath_monitor_cloud_secret_2026_salt")

# 10分おき利用率集計用タイムビン (16:30〜22:00, 計33区間)
def _generate_10min_bins(start_h=16, start_m=30, end_h=22, end_m=0):
    bins = []
    curr = start_h * 60 + start_m
    end = end_h * 60 + end_m
    while curr < end:
        nxt = curr + 10
        sh, sm = curr // 60, curr % 60
        eh, em = nxt // 60, nxt % 60
        label = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
        bins.append((label, curr, nxt))
        curr = nxt
    return bins

UTILIZATION_10MIN_BINS = _generate_10min_bins(16, 30, 22, 0)

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
    "occlusion_reason": "",
    "updated_at": "--:--:--",
    "last_received_timestamp": 0.0,
    "is_online": False
}

# 最新の設定キャッシュ（ラズパイと同期）
latest_config = {
    "corners": [[60, 80], [660, 80], [660, 1200], [60, 1200]],
    "threshold": 0.30,
    "stable_duration_sec": 4.0,
    "mode": "hybrid",
    "yolo_model_path": "yolov8n.pt",
    "yolo_conf": 0.50,
    "person_occlusion_enabled": True,
    "camera_rotation": 90,
    "camera_zoom": 1.4,
    "camera_pan_x": 0.05,
    "camera_pan_y": -0.05,
    "yolo_backend": "YOLOv8 ONNX / Fallback",
    "schedule": {
        "enabled": True,
        "start_time": "16:30",
        "end_time": "22:00"
    }
}

# カメラ映像・グリッド詳細キャッシュ（遠隔監視用）
latest_vision = {
    "image_b64": None,              # 最新スナップショット base64
    "stable_states": [False] * 36,  # 36マスの状態
    "scores": [0.0] * 36,           # 36マスの差分スコア
    "yolo_boxes": [],               # YOLO検出ボックス
    "is_occluded": False,
    "occlusion_reason": ""
}

# 日別統計サマリーキャッシュ (ラズパイから受信)
latest_daily_summary = {}
latest_available_dates = [datetime.datetime.now().strftime("%Y-%m-%d")]

# 自室PCから送られた未適用の遠隔設定キュー
pending_remote_config = {}

OFFLINE_THRESHOLD_SEC = 30.0

# クラウド側で集計・保持する本日のログデータ
cloud_daily_logs = []
cloud_utilization_10min = []

def get_today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")

current_log_date = get_today_str()

def recalculate_cloud_utilization():
    """クラウド側で蓄積した本日のログから10分おき利用率を算出"""
    global cloud_utilization_10min
    max_slots = latest_status.get("max_slots", 36)
    if max_slots <= 0:
        max_slots = 36

    bin_counts = {label: [] for (label, _, _) in UTILIZATION_10MIN_BINS}

    for log in cloud_daily_logs:
        t_str = log.get("time", "")
        parts = t_str.split(":")
        if len(parts) >= 2:
            try:
                minute_of_day = int(parts[0]) * 60 + int(parts[1])
                for (label, start_m, end_m) in UTILIZATION_10MIN_BINS:
                    if start_m <= minute_of_day < end_m:
                        bin_counts[label].append(log.get("count", 0))
                        break
            except Exception:
                pass

    calculated = []
    for (label, _, _) in UTILIZATION_10MIN_BINS:
        counts = bin_counts[label]
        if counts:
            avg_c = round(sum(counts) / len(counts), 1)
            rate = round((avg_c / max_slots) * 100, 1)
            peak_c = max(counts)
        else:
            avg_c = 0.0
            rate = 0.0
            peak_c = 0
        calculated.append({
            "label": label,
            "avg_users": avg_c,
            "utilization_rate": rate,
            "peak_users": peak_c
        })
    cloud_utilization_10min = calculated


# -------------------------------------------------------------
# 1. 一般利用者向け配信トップページ HTML (インライン・管理ボタンなし)
# -------------------------------------------------------------
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
    .header-actions { display: flex; align-items: center; gap: 8px; }
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
        <span id="onlineText">確認中</span>
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
            onlineTextEl.textContent = "Live";
          } else {
            onlineDotEl.className = "status-indicator-dot dot-offline";
            onlineTextEl.textContent = "待機中";
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

# -------------------------------------------------------------
# 2. ログイン画面 HTML (インライン)
# -------------------------------------------------------------
INLINE_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>管理者ログイン - 寮風呂場モニター</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a; display: flex; justify-content: center; align-items: center;
      min-height: 100vh; margin: 0; padding: 16px; color: #f8fafc;
    }
    .login-card {
      background: #1e293b; padding: 36px 30px; border-radius: 16px;
      box-shadow: 0 8px 30px rgba(0,0,0,0.5); width: 100%; max-width: 380px;
      text-align: center; border: 1px solid #334155;
    }
    .icon { font-size: 44px; margin-bottom: 12px; }
    h2 { font-size: 1.35rem; margin-bottom: 8px; color: #f8fafc; font-weight: 700; }
    p { font-size: 0.85rem; color: #94a3b8; margin-bottom: 24px; }
    .form-group { margin-bottom: 20px; text-align: left; }
    label { display: block; font-size: 0.82rem; font-weight: 600; color: #cbd5e1; margin-bottom: 8px; }
    input[type="password"] {
      width: 100%; padding: 12px 14px; border: 1px solid #475569; border-radius: 8px;
      font-size: 1rem; box-sizing: border-box; outline: none; background: #0f172a;
      color: #f8fafc; transition: border 0.2s;
    }
    input[type="password"]:focus { border-color: #38bdf8; box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2); }
    .btn-submit {
      width: 100%; padding: 12px; background: #0284c7; color: white; border: none;
      border-radius: 8px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
      transition: background 0.2s;
    }
    .btn-submit:hover { background: #0369a1; }
    .error-msg { background: #450a0a; color: #fca5a5; padding: 10px; border-radius: 6px; font-size: 0.82rem; margin-bottom: 16px; border: 1px solid #991b1b; }
    .back-link { display: inline-block; margin-top: 18px; font-size: 0.85rem; color: #38bdf8; text-decoration: none; }
  </style>
</head>
<body>
  <div class="login-card">
    <div class="icon">🔒</div>
    <h2>遠隔システム管理 ログイン</h2>
    <p>パスワードを入力してください</p>
    {% if error %}
      <div class="error-msg">{{ error }}</div>
    {% endif %}
    <form method="POST" action="/admin/login">
      <div class="form-group">
        <label for="password">管理者パスワード</label>
        <input type="password" id="password" name="password" required autofocus placeholder="パスワードを入力">
      </div>
      <button type="submit" class="btn-submit">ログイン</button>
    </form>
    <a href="/" class="back-link">← 一般公開モニターに戻る</a>
  </div>
</body>
</html>
"""

# -------------------------------------------------------------
# 3. クラウド遠隔統合システム管理画面 HTML (インライン・完全版)
# -------------------------------------------------------------
INLINE_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>靴箱位置調整・入退室ログ管理画面</title>
  <style>
/* PC専用靴箱位置調整・システム管理画面スタイル */
:root {
  --admin-bg: #0f172a;
  --panel-bg: #1e293b;
  --panel-border: #334155;
  --text-primary: #f8fafc;
  --text-muted: #94a3b8;
  --accent-blue: #0ea5e9;
  --accent-green: #10b981;
  --accent-amber: #f59e0b;
  --accent-red: #ef4444;
  --accent-purple: #a855f7;
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
  background-color: var(--admin-bg);
  color: var(--text-primary);
  min-height: 100vh;
}

/* スマホ・狭い画面用ブロッカー */
.mobile-blocker {
  display: flex;
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.96);
  z-index: 9999;
  justify-content: center;
  align-items: center;
  padding: 24px;
}

.blocker-card {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  padding: 32px 24px;
  border-radius: 16px;
  text-align: center;
  max-width: 420px;
}

.blocker-card .icon {
  font-size: 3rem;
  margin-bottom: 16px;
}

.blocker-card h2 {
  font-size: 1.3rem;
  margin-bottom: 12px;
  color: #fff;
}

.blocker-card p {
  font-size: 0.9rem;
  line-height: 1.6;
  color: var(--text-muted);
  margin-bottom: 24px;
}

.btn-back {
  display: inline-block;
  background: var(--accent-blue);
  color: #fff;
  text-decoration: none;
  padding: 10px 20px;
  border-radius: 8px;
  font-weight: 600;
}

@media (min-width: 1024px) {
  .mobile-blocker {
    display: none;
  }
}

.admin-wrapper {
  padding: 20px;
  max-width: 1720px;
  margin: 0 auto;
}

.admin-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--panel-border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.header-left h1 {
  font-size: 1.4rem;
  font-weight: 700;
}

.badge {
  background: #065f46;
  color: #6ee7b7;
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 0.78rem;
  font-weight: 600;
}

.badge-yolo {
  background: #3b0764;
  color: #e9d5ff;
  border: 1px solid #7e22ce;
}

.btn-outline {
  display: inline-block;
  padding: 8px 16px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  color: var(--text-primary);
  text-decoration: none;
  font-size: 0.88rem;
  transition: background 0.2s;
}

.btn-outline:hover {
  background: var(--panel-border);
}

.admin-grid-layout {
  display: grid;
  grid-template-columns: 1fr 390px;
  gap: 20px;
  align-items: start;
}

.canvas-section {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.canvas-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}

.instruction-hint {
  font-size: 0.84rem;
  color: #38bdf8;
}

.canvas-container {
  position: relative;
  width: 100%;
  max-height: 75vh;
  margin: 0 auto;
  background: #000;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: inset 0 0 10px rgba(0,0,0,0.8);
  display: flex;
  justify-content: center;
  align-items: center;
}

#gridCanvas {
  max-width: 100%;
  max-height: 75vh;
  width: auto;
  height: auto;
  object-fit: contain;
  cursor: crosshair;
  display: block;
}

.live-stream-overlay {
  position: absolute;
  max-width: 100%;
  max-height: 75vh;
  width: auto;
  height: auto;
  object-fit: contain;
}

.canvas-footer-info {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  color: var(--text-muted);
  padding: 4px 2px;
  flex-wrap: wrap;
  gap: 8px;
}

.status-normal {
  color: var(--accent-green);
}

.status-occluded {
  color: var(--accent-red);
  font-weight: bold;
}

.sidebar-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-card {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 14px 16px;
}

.panel-yolo {
  border-color: #0284c7;
  box-shadow: 0 0 12px rgba(14, 165, 233, 0.12);
}

.panel-card h2 {
  font-size: 0.96rem;
  margin-bottom: 8px;
  color: #fff;
  border-bottom: 1px solid rgba(255,255,255,0.08);
  padding-bottom: 6px;
}

.panel-desc {
  font-size: 0.8rem;
  color: var(--text-muted);
  line-height: 1.45;
  margin-bottom: 10px;
}

button {
  cursor: pointer;
  border: none;
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 0.86rem;
  font-weight: 600;
  transition: opacity 0.2s, background 0.2s;
}

button:hover {
  opacity: 0.9;
}

.btn-primary {
  background: var(--accent-blue);
  color: #fff;
}

.btn-secondary {
  background: #334155;
  color: #fff;
}

.btn-warning {
  background: var(--accent-amber);
  color: #000;
}

.btn-danger {
  background: #be123c;
  color: #fff;
}

.btn-block {
  width: 100%;
}

.form-group {
  margin-bottom: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-group label {
  font-size: 0.82rem;
  color: #e2e8f0;
}

.form-select,
.form-input {
  background: #0f172a;
  border: 1px solid var(--panel-border);
  color: #fff;
  padding: 7px 10px;
  border-radius: 6px;
  font-size: 0.84rem;
}

.form-hint {
  font-size: 0.73rem;
  color: var(--text-muted);
  line-height: 1.35;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

input[type="range"] {
  width: 100%;
  accent-color: var(--accent-blue);
}

input[type="time"] {
  background: #0f172a;
  border: 1px solid var(--panel-border);
  color: #fff;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 0.85rem;
}

.form-group-checkbox {
  margin-bottom: 10px;
}

.form-group-checkbox label {
  font-size: 0.82rem;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.slot-buttons-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
  max-height: 160px;
  overflow-y: auto;
  padding: 4px;
  background: #0f172a;
  border-radius: 6px;
}

.slot-btn {
  padding: 4px;
  font-size: 0.72rem;
  background: #1e293b;
  color: #94a3b8;
  border: 1px solid #334155;
  border-radius: 4px;
  text-align: center;
}

.slot-btn.full {
  background: #991b1b;
  color: #fecaca;
  border-color: #ef4444;
}

/* ナビゲーションタブ */
.admin-nav-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--panel-border);
  padding-bottom: 12px;
}

.nav-tab-btn {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  color: var(--text-muted);
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 0.95rem;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.2s;
}

.nav-tab-btn:hover {
  background: #334155;
  color: #fff;
}

.nav-tab-btn.active {
  background: var(--accent-blue);
  color: #fff;
  border-color: var(--accent-blue);
  box-shadow: 0 0 12px rgba(14, 165, 233, 0.3);
}

.tab-pane {
  display: block;
}

/* ログ画面ヘッダーバー */
.logs-header-bar {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 14px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 12px;
}

.logs-header-left,
.logs-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* サマリー統計グリッド */
.stats-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.stat-card {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  font-size: 2.4rem;
  background: #0f172a;
  width: 60px;
  height: 60px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat-content {
  display: flex;
  flex-direction: column;
}

.stat-label {
  font-size: 0.82rem;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.stat-value {
  font-size: 1.8rem;
  font-weight: 800;
  color: #fff;
}

.stat-value small {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-muted);
}

/* グラフグリッド */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 24px;
}

@media (max-width: 1200px) {
  .charts-grid {
    grid-template-columns: 1fr;
  }
}

.chart-card {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chart-card-header {
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  padding-bottom: 10px;
}

.chart-card-header h3 {
  font-size: 1rem;
  color: #fff;
  margin-bottom: 2px;
}

.chart-subtitle {
  font-size: 0.78rem;
  color: var(--text-muted);
}

.chart-body {
  position: relative;
  height: 280px;
  width: 100%;
}

/* 入退室ログテーブルカード */
.log-table-card {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 20px;
}

.log-table-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  padding-bottom: 12px;
  margin-bottom: 16px;
}

.log-table-header h3 {
  font-size: 1.05rem;
  color: #fff;
}

.log-table-wrapper {
  max-height: 480px;
  overflow-y: auto;
  border-radius: 8px;
  border: 1px solid var(--panel-border);
}

.session-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 0.88rem;
}

.session-table th {
  background: #0f172a;
  color: #94a3b8;
  font-weight: 600;
  padding: 12px 16px;
  position: sticky;
  top: 0;
  border-bottom: 1px solid var(--panel-border);
}

.session-table td {
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  color: #e2e8f0;
}

.session-table tr:hover {
  background: rgba(255, 255, 255, 0.02);
}

.badge-slot {
  display: inline-block;
  background: #0369a1;
  color: #e0f2fe;
  padding: 3px 10px;
  border-radius: 6px;
  font-weight: 700;
  font-size: 0.82rem;
}

.duration-tag {
  color: var(--accent-green);
  font-weight: 700;
}


</style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <!-- モバイル・小画面用警告ブロック -->
  <div class="mobile-blocker">
    <div class="blocker-card">
      <div class="icon">💻</div>
      <h2>パソコン専用ページです</h2>
      <p>靴箱の位置調整および管理機能は、高精度なマウス操作と大画面表示が必要なため、パソコンのブラウザからアクセスしてください。</p>
      <a href="/" class="btn-back">利用状況トップに戻る</a>
    </div>
  </div>

  <!-- PC用メインコンテンツ -->
  <div class="admin-wrapper">
    <header class="admin-header">
      <div class="header-left">
        <h1>⚙ 寮風呂場 システム管理画面</h1>
        <span class="badge" id="cameraModeBadge">カメラ接続中</span>
        <span class="badge badge-yolo" id="yoloBackendBadge">YOLO準備中</span>
      </div>
      <div class="header-right" style="display: flex; gap: 10px; align-items: center;">
        <a href="/" class="btn-outline">← 配信画面へ</a>
        <a href="/logout" class="btn-secondary" style="text-decoration: none; padding: 7px 12px; font-size: 0.84rem;">🚪 ログアウト</a>
      </div>
    </header>

    <!-- ナビゲーションタブ -->
    <div class="admin-nav-tabs">
      <button class="nav-tab-btn active" id="tabBtnGrid">🎯 靴箱36スロット位置調整 & YOLO設定</button>
      <button class="nav-tab-btn" id="tabBtnLogs">📊 入退室ログ & 利用時間統計ヒストグラム</button>
    </div>

    <!-- 1. 靴箱位置調整 & 設定タブ -->
    <div id="sectionGrid" class="tab-pane active">
      <div class="admin-grid-layout">
        <!-- メインエリア：キャンバス -->
        <section class="canvas-section">
          <div class="canvas-toolbar">
            <div class="toolbar-left">
              <span class="instruction-hint">💡 四隅のアンカー（P1〜P4）をドラッグして靴箱の外枠に合わせてください。縦向き時にアンカーが見失われた場合は「アンカーを中央にリセット」を押してください。</span>
            </div>
            <div class="toolbar-right" style="display: flex; gap: 8px; flex-wrap: wrap;">
              <button id="resetGridBtn" class="btn-secondary" title="現在のカメラ解像度に合わせてアンカーを中央に再配置します">🔄 アンカーを中央にリセット</button>
              <button id="refreshSnapshotBtn" class="btn-secondary">📷 静止画を再取得</button>
              <button id="toggleLiveBtn" class="btn-secondary">🔴 ライブ映像で確認</button>
            </div>
          </div>

          <div class="canvas-container" id="canvasContainer">
            <canvas id="gridCanvas" width="1280" height="720"></canvas>
            <img id="liveStreamImg" class="live-stream-overlay" src="" style="display: none;" alt="ライブ映像">
          </div>

          <div class="canvas-footer-info">
            <span id="resolutionInfoText">解像度: 1280 × 720 (ロジクール C270n HD)</span>
            <span id="yoloCountText">YOLO検出スリッパ: 0 個</span>
            <span id="canvasCoordsText">カーソル座標: (0, 0)</span>
            <span id="occlusionStatusText" class="status-normal">ステータス: 正常判定中</span>
          </div>
        </section>

        <!-- サイドバー：設定パネル -->
        <aside class="sidebar-section">
          <!-- カメラ回転・ズーム調整パネル -->
          <div class="panel-card" style="border-color: #38bdf8;">
            <h2>📷 カメラ向き・倍率（ズーム）調整</h2>
            <p class="panel-desc">縦長靴箱に合わせて90度回転したり、靴箱を画面いっぱいに拡大できます。</p>
            <div class="form-group">
              <label for="cameraRotationSelect">カメラの向き（回転）</label>
              <select id="cameraRotationSelect" class="form-select">
                <option value="0">0° (通常の横向き)</option>
                <option value="90" selected>90° (時計回り・縦長靴箱に最適)</option>
                <option value="180">180° (上下逆さま)</option>
                <option value="270">270° (反時計回り・縦長)</option>
              </select>
            </div>

            <div class="form-group">
              <label for="cameraZoomInput">
                ズーム倍率: <span id="cameraZoomVal">1.4</span>x
              </label>
              <input type="range" id="cameraZoomInput" min="1.0" max="3.0" step="0.1" value="1.4">
            </div>

            <div class="form-row">
              <div class="form-group">
                <label for="cameraPanXInput">左右位置 (Pan X)</label>
                <input type="range" id="cameraPanXInput" min="-1.0" max="1.0" step="0.05" value="0.05">
              </div>
              <div class="form-group">
                <label for="cameraPanYInput">上下位置 (Pan Y)</label>
                <input type="range" id="cameraPanYInput" min="-1.0" max="1.0" step="0.05" value="-0.05">
              </div>
            </div>

            <button id="saveCameraTransformBtn" class="btn-primary btn-block">📷 カメラ向き・倍率を適用</button>
          </div>

          <div class="panel-card">
            <h2>1. 靴箱位置設定の保存</h2>
            <p class="panel-desc">四隅を調整後、保存すると36マスの座標が即座に更新されます。</p>
            <button id="saveGridBtn" class="btn-primary btn-block">💾 靴箱位置を保存</button>
          </div>

          <div class="panel-card panel-yolo">
            <h2>2. 🤖 YOLO AI認識設定</h2>
            <div class="form-group">
              <label for="detectionModeSelect">判別アルゴリズムモード</label>
              <select id="detectionModeSelect" class="form-select">
                <option value="hybrid">ハイブリッド (YOLO + 背景差分 併用)</option>
                <option value="yolo">YOLO 単独モード (カスタムモデル推奨)</option>
                <option value="diff">背景差分・画像解析単独モード (軽量)</option>
              </select>
            </div>

            <div class="form-group">
              <label for="yoloModelInput">YOLOモデルファイル (.pt / .onnx)</label>
              <input type="text" id="yoloModelInput" class="form-input" value="yolov8n.pt" placeholder="例: yolov8n.pt または best.pt">
              <span class="form-hint">自作のスリッパ学習モデルがある場合は <code>best.pt</code> や <code>best.onnx</code> を指定</span>
            </div>

            <div class="form-group">
              <label for="yoloConfInput">
                YOLO 検出信頼度 (Confidence): <span id="yoloConfVal">0.50</span>
              </label>
              <input type="range" id="yoloConfInput" min="0.10" max="0.80" step="0.05" value="0.50">
            </div>

            <div class="form-group-checkbox">
              <label>
                <input type="checkbox" id="personOcclusionCheck" checked>
                YOLOで人(<code>person</code>)を検出時に人数変化をロック
              </label>
            </div>

            <button id="saveYoloBtn" class="btn-primary btn-block">🤖 YOLO設定を保存・適用</button>
          </div>

          <div class="panel-card">
            <h2>3. 背景キャリブレーション & 安定化</h2>
            <button id="calibrateBtn" class="btn-warning btn-block" style="margin-bottom: 12px;">🎯 現在の映像を「空」として学習</button>

            <div class="form-group">
              <label for="thresholdInput">
                差分判定しきい値: <span id="thresholdVal">0.30</span>
              </label>
              <input type="range" id="thresholdInput" min="0.05" max="0.60" step="0.05" value="0.30">
            </div>

            <div class="form-group">
              <label for="stableSecInput">
                確定までの安定化時間: <span id="stableSecVal">4.0</span> 秒
              </label>
              <input type="range" id="stableSecInput" min="0.5" max="6.0" step="0.5" value="4.0">
            </div>

            <button id="saveParamsBtn" class="btn-secondary btn-block">安定化パラメータを保存</button>
          </div>

          <div class="panel-card">
            <h2>4. 稼働スケジュール (16:30〜22:00)</h2>
            <div class="form-group-checkbox">
              <label>
                <input type="checkbox" id="scheduleEnableCheck" checked> スケジュール制御を有効化
              </label>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>開始時刻</label>
                <input type="time" id="schedStartTime" value="16:30">
              </div>
              <div class="form-group">
                <label>終了時刻</label>
                <input type="time" id="schedEndTime" value="22:00">
              </div>
            </div>
            <button id="saveScheduleBtn" class="btn-secondary btn-block">スケジュール保存</button>
          </div>

          <div class="panel-card" id="mockControlCard">
            <h2>5. 動作テスト（人影ロック & スロット切替）</h2>
            <button id="triggerPersonBtn" class="btn-danger btn-block" style="margin-bottom: 10px;">
              🚶 人影(person)映り込みテスト (5秒間)
            </button>
            <p class="panel-desc">スロット番号クリックでスリッパ出し入れをテスト：</p>
            <div class="slot-buttons-grid" id="slotTestGrid"></div>
          </div>

          <div class="panel-card">
            <h2>6. 🔐 管理者パスワードの変更</h2>
            <div class="form-group">
              <label for="newAdminPassword">新しいパスワード</label>
              <input type="password" id="newAdminPassword" class="form-input" placeholder="新しいパスワードを入力">
            </div>
            <button id="savePasswordBtn" class="btn-secondary btn-block">パスワードを変更</button>
          </div>

          <div class="panel-card">
            <h2>7. ☁️ クラウド常時中継ステータス</h2>
            <p class="panel-desc">お風呂場のラズパイと常時同期中。自室PCからの設定変更は数秒で実機へ自動送信されます。</p>
            <div class="form-group checkbox-group" style="margin-bottom: 12px;">
              <label>
                <input type="checkbox" id="cloudSyncEnabled">
                <strong>クラウド自動配信を有効化</strong>
              </label>
            </div>
            <div class="form-group">
              <label for="cloudUrlInput">クラウドサーバーURL</label>
              <input type="url" id="cloudUrlInput" class="form-input" placeholder="https://dorm-bath-monitor.onrender.com">
            </div>
            <div class="form-group">
              <label for="cloudApiKeyInput">APIキー (セキュリティ認証トークン)</label>
              <input type="password" id="cloudApiKeyInput" class="form-input" placeholder="bath_monitor_secret_key_2026">
            </div>
            <div style="background: #0f172a; padding: 10px; border-radius: 6px; font-size: 0.82rem; margin-bottom: 12px;">
              <span style="color: #94a3b8;">同期状態: </span>
              <strong id="cloudSyncStatusText" style="color: #38bdf8;">未同期</strong>
            </div>
            <div style="display: flex; gap: 8px;">
              <button id="saveCloudSyncBtn" class="btn-primary" style="flex: 1;">クラウド設定を保存</button>
              <button id="testCloudSyncBtn" class="btn-secondary" style="flex: 1;">疎通テスト送信</button>
            </div>
          </div>
        </aside>
      </div>
    </div>

    <!-- 2. 入退室ログ & 統計ヒストグラムタブ -->
    <div id="sectionLogs" class="tab-pane" style="display: none;">
      <!-- 上部ツールバー: 日付選択 & CSVダウンロード -->
      <div class="logs-header-bar">
        <div class="logs-header-left">
          <label for="logDateSelect" style="font-weight: 600; font-size: 0.95rem;">📅 表示日付:</label>
          <select id="logDateSelect" class="form-select" style="min-width: 160px; font-weight: 600;"></select>
          <button id="refreshLogsBtn" class="btn-secondary">🔄 最新に更新</button>
        </div>
        <div class="logs-header-right">
          <span style="font-size: 0.85rem; color: #94a3b8;">CSV保存データ:</span>
          <a id="btnDownloadSessions" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 入退室ログCSV</a>
          <a id="btnDownloadUtilization" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px; border-color: #8b5cf6; color: #c084fc;">📥 10分おき利用率CSV</a>
          <a id="btnDownloadEntryHist" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 30分間隔CSV</a>
          <a id="btnDownloadDurationHist" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 5分間隔CSV</a>
        </div>
      </div>

      <!-- サマリー統計カード -->
      <div class="stats-summary-grid">
        <div class="stat-card">
          <div class="stat-icon">👥</div>
          <div class="stat-content">
            <span class="stat-label">総入室回数</span>
            <span class="stat-value" id="statTotalUsers">0 <small>人</small></span>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">⏱</div>
          <div class="stat-content">
            <span class="stat-label">平均利用時間</span>
            <span class="stat-value" id="statAvgDuration">0.0 <small>分</small></span>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">⏳</div>
          <div class="stat-content">
            <span class="stat-label">最長利用時間</span>
            <span class="stat-value" id="statMaxDuration">0.0 <small>分</small></span>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">📊</div>
          <div class="stat-content">
            <span class="stat-label">最高利用率 (ピーク)</span>
            <span class="stat-value" id="statPeakRate">0.0 <small>%</small></span>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">📈</div>
          <div class="stat-content">
            <span class="stat-label">平均利用率</span>
            <span class="stat-value" id="statAvgRate">0.0 <small>%</small></span>
          </div>
        </div>
      </div>

      <!-- 10分おき風呂場利用率［％］推移グラフ (メイン) -->
      <div class="chart-card" style="margin-bottom: 20px;">
        <div class="chart-card-header">
          <h3>📊 10分おき風呂場利用率［％］推移 (16:30〜22:00)</h3>
          <span class="chart-subtitle">時間帯ごとの収容枠（36枠）に対する利用率推移</span>
        </div>
        <div class="chart-body" style="height: 260px;">
          <canvas id="utilizationRateChart"></canvas>
        </div>
      </div>

      <!-- ヒストグラムグラフ（2つ横並び） -->
      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-card-header">
            <h3>📈 入室時間帯ヒストグラム (30分間隔: 16:30〜22:00)</h3>
            <span class="chart-subtitle">時間帯ごとの風呂場入室人数</span>
          </div>
          <div class="chart-body">
            <canvas id="entryHistogramChart"></canvas>
          </div>
        </div>

        <div class="chart-card">
          <div class="chart-card-header">
            <h3>⏱ 風呂場利用時間ヒストグラム (5分間隔: 0〜60分以上)</h3>
            <span class="chart-subtitle">滞在時間の分布（何分入っていたか）</span>
          </div>
          <div class="chart-body">
            <canvas id="durationHistogramChart"></canvas>
          </div>
        </div>
      </div>

      <!-- 入退室セッションログ テーブル -->
      <div class="log-table-card">
        <div class="log-table-header">
          <h3>📋 入退室ログ一覧（スロット | 入室時間 | 退室時間 | 利用時間）</h3>
          <span id="logCountLabel" style="font-size: 0.85rem; color: #94a3b8;">0 件の記録</span>
        </div>
        <div class="log-table-wrapper">
          <table class="session-table">
            <thead>
              <tr>
                <th>No.</th>
                <th>スロット</th>
                <th>入室時間</th>
                <th>退室時間</th>
                <th>利用時間</th>
              </tr>
            </thead>
            <tbody id="sessionTableBody">
              <tr>
                <td colspan="5" style="text-align: center; color: #94a3b8; padding: 24px;">
                  まだ入退室データがありません。スリッパが出し入れされると自動的に記録されます。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <script>
/**
 * PC専用靴箱位置調整・YOLO管理画面用スクリプト (HTML5 Canvas 3×12グリッドエディタ & YOLO可視化)
 */

document.addEventListener("DOMContentLoaded", () => {
  const canvas = document.getElementById("gridCanvas");
  const ctx = canvas.getContext("2d");
  const liveImg = document.getElementById("liveStreamImg");
  const toggleLiveBtn = document.getElementById("toggleLiveBtn");
  const refreshSnapshotBtn = document.getElementById("refreshSnapshotBtn");
  const saveGridBtn = document.getElementById("saveGridBtn");
  const calibrateBtn = document.getElementById("calibrateBtn");
  const canvasCoordsText = document.getElementById("canvasCoordsText");
  const occlusionStatusText = document.getElementById("occlusionStatusText");
  const cameraModeBadge = document.getElementById("cameraModeBadge");
  const yoloBackendBadge = document.getElementById("yoloBackendBadge");
  const yoloCountText = document.getElementById("yoloCountText");

  // カメラ回転・ズーム要素
  const cameraRotationSelect = document.getElementById("cameraRotationSelect");
  const cameraZoomInput = document.getElementById("cameraZoomInput");
  const cameraZoomVal = document.getElementById("cameraZoomVal");
  const cameraPanXInput = document.getElementById("cameraPanXInput");
  const cameraPanYInput = document.getElementById("cameraPanYInput");
  const saveCameraTransformBtn = document.getElementById("saveCameraTransformBtn");

  // YOLO設定要素
  const detectionModeSelect = document.getElementById("detectionModeSelect");
  const yoloModelInput = document.getElementById("yoloModelInput");
  const yoloConfInput = document.getElementById("yoloConfInput");
  const yoloConfVal = document.getElementById("yoloConfVal");
  const personOcclusionCheck = document.getElementById("personOcclusionCheck");
  const saveYoloBtn = document.getElementById("saveYoloBtn");

  // パラメータ要素
  const thresholdInput = document.getElementById("thresholdInput");
  const thresholdVal = document.getElementById("thresholdVal");
  const stableSecInput = document.getElementById("stableSecInput");
  const stableSecVal = document.getElementById("stableSecVal");
  const saveParamsBtn = document.getElementById("saveParamsBtn");

  // スケジュール要素
  const scheduleEnableCheck = document.getElementById("scheduleEnableCheck");
  const schedStartTime = document.getElementById("schedStartTime");
  const schedEndTime = document.getElementById("schedEndTime");
  const saveScheduleBtn = document.getElementById("saveScheduleBtn");

  // テスト用要素
  const triggerPersonBtn = document.getElementById("triggerPersonBtn");
  const slotTestGrid = document.getElementById("slotTestGrid");

  // 内部状態
  const COLS = 3;
  const ROWS = 12;
  const NUM_SLOTS = COLS * ROWS; // 36
  let corners = [
    [180, 60],   // P1: 左上
    [1100, 60],  // P2: 右上
    [1100, 660], // P3: 右下
    [180, 660]   // P4: 左下
  ];

  let backgroundImage = new Image();
  let isImageLoaded = false;
  let isLiveMode = false;

  let draggingPointIdx = -1;
  const POINT_RADIUS = 12;

  let currentSlotStates = new Array(NUM_SLOTS).fill(false);
  let currentScores = new Array(NUM_SLOTS).fill(0.0);
  let currentYoloBoxes = [];

  const resetGridBtn = document.getElementById("resetGridBtn");
  const resolutionInfoText = document.getElementById("resolutionInfoText");

  // アンカーを現在の解像度に合わせて中央にリセット
  function resetAnchorsToCenter() {
    const w = canvas.width || 1280;
    const h = canvas.height || 720;
    const marginX = Math.round(w * 0.10);
    const marginY = Math.round(h * 0.06);
    corners = [
      [marginX, marginY],             // P1: 左上
      [w - marginX, marginY],         // P2: 右上
      [w - marginX, h - marginY],     // P3: 右下
      [marginX, h - marginY]          // P4: 左下
    ];
    renderCanvas();
  }

  // 画面外にはみ出たアンカーを自動で画面内に引き戻す
  function clampCornersToCanvas() {
    if (!canvas.width || !canvas.height) return;
    const w = canvas.width;
    const h = canvas.height;

    // 縦長画像なのに横長用のアンカー（X > w）があるか判定
    const isOutOfBounds = corners.some(pt => pt[0] > w - 10 || pt[1] > h - 10 || pt[0] < 5 || pt[1] < 5);
    if (isOutOfBounds) {
      console.warn("Corners out of bounds detected. Auto-adjusting to canvas resolution:", w, h);
      // はみ出している場合は中央に綺麗に再配置
      resetAnchorsToCenter();
      return;
    }

    // 軽微なズレをクランプ
    const pad = 20;
    corners = corners.map(pt => [
      Math.max(pad, Math.min(w - pad, pt[0])),
      Math.max(pad, Math.min(h - pad, pt[1]))
    ]);
  }

  // 初期化：スナップショット読み込み（画像の向き・解像度にキャンバスを自動追従）
  function loadSnapshot() {
    backgroundImage.onload = () => {
      isImageLoaded = true;
      if (backgroundImage.naturalWidth && backgroundImage.naturalHeight) {
        canvas.width = backgroundImage.naturalWidth;
        canvas.height = backgroundImage.naturalHeight;
        const container = document.getElementById("canvasContainer");
        if (container) {
          container.style.aspectRatio = `${canvas.width} / ${canvas.height}`;
        }
        if (resolutionInfoText) {
          const orientation = canvas.width < canvas.height ? "縦長90°" : "横長";
          resolutionInfoText.textContent = `解像度: ${canvas.width} × ${canvas.height} (${orientation})`;
        }
      }
      clampCornersToCanvas();
      renderCanvas();
    };
    backgroundImage.onerror = () => {
      console.error("スナップショット画像の読み込みに失敗しました。");
    };
    backgroundImage.src = `/api/camera/snapshot?t=${Date.now()}`;
  }

  if (resetGridBtn) {
    resetGridBtn.addEventListener("click", () => {
      resetAnchorsToCenter();
      alert("四隅アンカー（P1〜P4）を画面中央に再配置しました。\n靴箱の外枠に合わせてドラッグ調整し、「💾 靴箱位置を保存」を押してください。");
    });
  }

  // 初期ステータス取得
  async function initStatus() {
    try {
      const res = await fetch("/api/admin/status");
      const data = await res.json();
      if (data.corners && data.corners.length === 4) {
        corners = data.corners;
      }
      if (data.mode) {
        detectionModeSelect.value = data.mode;
      }
      if (data.yolo_model_path) {
        yoloModelInput.value = data.yolo_model_path;
      }
      if (data.yolo_conf !== undefined) {
        yoloConfInput.value = data.yolo_conf;
        yoloConfVal.textContent = Number(data.yolo_conf).toFixed(2);
      }
      if (data.person_occlusion_enabled !== undefined) {
        personOcclusionCheck.checked = data.person_occlusion_enabled;
      }
      if (data.yolo_backend) {
        yoloBackendBadge.textContent = `🤖 ${data.yolo_backend}`;
      }
      if (data.threshold !== undefined) {
        thresholdInput.value = data.threshold;
        thresholdVal.textContent = Number(data.threshold).toFixed(2);
      }
      if (data.stable_duration_sec !== undefined) {
        stableSecInput.value = data.stable_duration_sec;
        stableSecVal.textContent = Number(data.stable_duration_sec).toFixed(1);
      }
      if (data.camera_rotation !== undefined && cameraRotationSelect) {
        cameraRotationSelect.value = String(data.camera_rotation);
      }
      if (data.camera_zoom !== undefined && cameraZoomInput) {
        cameraZoomInput.value = data.camera_zoom;
        cameraZoomVal.textContent = Number(data.camera_zoom).toFixed(1);
      }
      if (data.camera_pan_x !== undefined && cameraPanXInput) {
        cameraPanXInput.value = data.camera_pan_x;
      }
      if (data.camera_pan_y !== undefined && cameraPanYInput) {
        cameraPanYInput.value = data.camera_pan_y;
      }

      if (data.schedule_enabled !== undefined) {
        scheduleEnableCheck.checked = data.schedule_enabled;
      }
      if (data.schedule_start) schedStartTime.value = data.schedule_start;
      if (data.schedule_end) schedEndTime.value = data.schedule_end;

      // クラウド設定反映
      if (data.cloud_sync) {
        const cs = data.cloud_sync;
        const csCheck = document.getElementById("cloudSyncEnabled");
        const csUrl = document.getElementById("cloudUrlInput");
        const csKey = document.getElementById("cloudApiKeyInput");
        const csStatus = document.getElementById("cloudSyncStatusText");
        if (csCheck) csCheck.checked = cs.enabled;
        if (csUrl && cs.cloud_url) csUrl.value = cs.cloud_url;
        if (csKey && cs.api_key) csKey.value = cs.api_key;
        if (csStatus) {
          csStatus.textContent = cs.message || "未同期";
          csStatus.style.color = cs.is_success ? "#34d399" : (cs.enabled ? "#f87171" : "#94a3b8");
        }
      }

      if (data.is_mock) {
        cameraModeBadge.textContent = "モックカメラ（シミュレーション動作中）";
        cameraModeBadge.style.background = "#78350f";
        cameraModeBadge.style.color = "#fde68a";
      } else {
        cameraModeBadge.textContent = "ロジクール C270n 接続中";
      }

      updateSlotStates(data.result || data);

    // クラウド遠隔監視用：ステータス更新時にbase64画像があればCanvas背景に即時反映
    if (data.image_b64 && !isLiveMode) {
      backgroundImage.src = "data:image/jpeg;base64," + data.image_b64;
    }

      buildSlotButtons();
      loadSnapshot();
    } catch (e) {
      console.error("初期ステータス取得失敗:", e);
      loadSnapshot();
    }
  }

  function updateSlotStates(result) {
    if (!result) return;
    if (result.stable_states) currentSlotStates = result.stable_states;
    if (result.scores) currentScores = result.scores;
    if (result.yolo_boxes) currentYoloBoxes = result.yolo_boxes;

    if (result.yolo_slipper_count !== undefined && yoloCountText) {
      yoloCountText.textContent = `YOLO検出スリッパ: ${result.yolo_slipper_count} 個`;
    }
    if (result.yolo_backend && yoloBackendBadge) {
      yoloBackendBadge.textContent = `🤖 ${result.yolo_backend}`;
    }

    if (result.is_occluded) {
      occlusionStatusText.className = "status-occluded";
      occlusionStatusText.textContent = `ステータス: 急変・人影ロック中 (${result.occlusion_reason || "遮蔽"})`;
    } else {
      occlusionStatusText.className = "status-normal";
      occlusionStatusText.textContent = "ステータス: 正常判定中";
    }
    updateSlotButtonsUI();
  }

  // 双線形補間による3x12スロットの四隅計算
  function getSlotPolygons() {
    const p0 = corners[0];
    const p1 = corners[1];
    const p2 = corners[2];
    const p3 = corners[3];
    const padding = 0.12;

    const slots = [];

    for (let r = 0; r < ROWS; r++) {
      const v0 = r / ROWS;
      const v1 = (r + 1) / ROWS;
      for (let c = 0; c < COLS; c++) {
        const u0 = c / COLS;
        const u1 = (c + 1) / COLS;

        const padU = (u1 - u0) * padding;
        const padV = (v1 - v0) * padding;

        const su0 = u0 + padU;
        const su1 = u1 - padU;
        const sv0 = v0 + padV;
        const sv1 = v1 - padV;

        function interp(u, v) {
          const topX = (1 - u) * p0[0] + u * p1[0];
          const topY = (1 - u) * p0[1] + u * p1[1];
          const botX = (1 - u) * p3[0] + u * p2[0];
          const botY = (1 - u) * p3[1] + u * p2[1];
          return [
            (1 - v) * topX + v * botX,
            (1 - v) * topY + v * botY
          ];
        }

        slots.push([
          interp(su0, sv0),
          interp(su1, sv0),
          interp(su1, sv1),
          interp(su0, sv1)
        ]);
      }
    }
    return slots;
  }

  // キャンバス描画
  function renderCanvas() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // 背景画像
    if (isImageLoaded) {
      ctx.drawImage(backgroundImage, 0, 0, canvas.width, canvas.height);
    } else {
      ctx.fillStyle = "#111827";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#64748b";
      ctx.font = "20px sans-serif";
      ctx.fillText("カメラ映像読み込み中...", 520, 360);
    }

    // 外枠ポリライン (黄色)
    ctx.strokeStyle = "#eab308";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(corners[0][0], corners[0][1]);
    for (let i = 1; i < 4; i++) {
      ctx.lineTo(corners[i][0], corners[i][1]);
    }
    ctx.closePath();
    ctx.stroke();

    // 36マスのスロット枠描画
    const slots = getSlotPolygons();
    slots.forEach((poly, idx) => {
      const isFull = currentSlotStates[idx];
      ctx.strokeStyle = isFull ? "#ef4444" : "#10b981"; // 赤: full, 緑: empty
      ctx.lineWidth = 2;
      ctx.fillStyle = isFull ? "rgba(239, 68, 68, 0.25)" : "rgba(16, 185, 129, 0.15)";

      ctx.beginPath();
      ctx.moveTo(poly[0][0], poly[0][1]);
      for (let i = 1; i < 4; i++) {
        ctx.lineTo(poly[i][0], poly[i][1]);
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      // スロット番号とスコア
      const cx = (poly[0][0] + poly[1][0] + poly[2][0] + poly[3][0]) / 4;
      const cy = (poly[0][1] + poly[1][1] + poly[2][1] + poly[3][1]) / 4;
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 13px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const scoreText = currentScores[idx] !== undefined ? ` (${currentScores[idx].toFixed(2)})` : "";
      ctx.fillText(`${idx + 1}${scoreText}`, cx, cy);
    });

    // YOLOバウンディングボックス描画
    currentYoloBoxes.forEach((box) => {
      const [x1, y1, x2, y2] = box.bbox;
      if (box.label === "person") {
        // 人物検出時はマゼンタ太枠で警告描画
        ctx.strokeStyle = "#d946ef";
        ctx.lineWidth = 4;
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        ctx.fillStyle = "rgba(217, 70, 239, 0.85)";
        ctx.fillRect(x1, Math.max(0, y1 - 28), 260, 28);
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 14px sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(`YOLO: PERSON (${box.conf}) [人数ロック中]`, x1 + 8, Math.max(14, y1 - 14));
      } else {
        // スリッパ検出時はシアン枠＋中心点ドット
        ctx.strokeStyle = "#00f2fe";
        ctx.lineWidth = 2;
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        if (box.center) {
          ctx.beginPath();
          ctx.arc(box.center[0], box.center[1], 4, 0, Math.PI * 2);
          ctx.fillStyle = "#00f2fe";
          ctx.fill();
        }
      }
    });

    // 四隅の操作ハンドル (P1〜P4)
    const labels = ["P1 (左上)", "P2 (右上)", "P3 (右下)", "P4 (左下)"];
    corners.forEach((pt, idx) => {
      ctx.beginPath();
      ctx.arc(pt[0], pt[1], POINT_RADIUS, 0, Math.PI * 2);
      ctx.fillStyle = draggingPointIdx === idx ? "#f97316" : "#0ea5e9";
      ctx.fill();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 3;
      ctx.stroke();

      ctx.fillStyle = "#f8fafc";
      ctx.font = "bold 14px sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(labels[idx], pt[0] + 16, pt[1] - 8);
    });
  }

  function getCanvasCoords(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return [
      Math.round((e.clientX - rect.left) * scaleX),
      Math.round((e.clientY - rect.top) * scaleY)
    ];
  }

  canvas.addEventListener("mousedown", (e) => {
    if (isLiveMode) return;
    const [mx, my] = getCanvasCoords(e);
    for (let i = 0; i < 4; i++) {
      const dx = mx - corners[i][0];
      const dy = my - corners[i][1];
      if (Math.hypot(dx, dy) <= POINT_RADIUS + 6) {
        draggingPointIdx = i;
        renderCanvas();
        return;
      }
    }
  });

  canvas.addEventListener("mousemove", (e) => {
    const [mx, my] = getCanvasCoords(e);
    canvasCoordsText.textContent = `カーソル座標: (${mx}, ${my})`;

    if (draggingPointIdx !== -1) {
      corners[draggingPointIdx] = [
        Math.max(0, Math.min(canvas.width, mx)),
        Math.max(0, Math.min(canvas.height, my))
      ];
      renderCanvas();
    } else {
      let hovering = false;
      for (let i = 0; i < 4; i++) {
        const dx = mx - corners[i][0];
        const dy = my - corners[i][1];
        if (Math.hypot(dx, dy) <= POINT_RADIUS + 6) {
          hovering = true;
          break;
        }
      }
      canvas.style.cursor = hovering ? "grab" : "crosshair";
    }
  });

  window.addEventListener("mouseup", () => {
    if (draggingPointIdx !== -1) {
      draggingPointIdx = -1;
      renderCanvas();
    }
  });

  refreshSnapshotBtn.addEventListener("click", () => {
    if (isLiveMode) toggleLiveMode(false);
    loadSnapshot();
  });

  toggleLiveBtn.addEventListener("click", () => {
    toggleLiveMode(!isLiveMode);
  });

  liveImg.onerror = () => {
    console.error("ライブストリーム切断またはエラー");
    if (isLiveMode) {
      alert("ライブ映像のストリームが一時的に切断されました。\n「📷 静止画を再取得」または再度「🔴 ライブ映像で確認」をお試しください。");
      toggleLiveMode(false);
    }
  };

  function toggleLiveMode(live) {
    isLiveMode = live;
    if (isLiveMode) {
      liveImg.style.display = "block";
      canvas.style.display = "none";
      toggleLiveBtn.textContent = "⏹ 静止画編集に戻る";
      toggleLiveBtn.classList.add("btn-primary");
      toggleLiveBtn.classList.remove("btn-secondary");
      liveImg.src = `/api/camera/stream?overlay=1&t=${Date.now()}`;
    } else {
      liveImg.src = ""; // 古いMJPEG接続を確実に破棄
      liveImg.style.display = "none";
      canvas.style.display = "block";
      toggleLiveBtn.textContent = "🔴 ライブ映像で確認";
      toggleLiveBtn.classList.remove("btn-primary");
      toggleLiveBtn.classList.add("btn-secondary");
      loadSnapshot();
    }
  }

  // 0. カメラ回転・ズーム設定保存
  if (cameraZoomInput) {
    cameraZoomInput.addEventListener("input", () => {
      cameraZoomVal.textContent = Number(cameraZoomInput.value).toFixed(1);
    });
  }

  if (saveCameraTransformBtn) {
    saveCameraTransformBtn.addEventListener("click", async () => {
      try {
        saveCameraTransformBtn.disabled = true;
        saveCameraTransformBtn.textContent = "適用中...";
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            camera_rotation: parseInt(cameraRotationSelect.value, 10),
            camera_zoom: parseFloat(cameraZoomInput.value),
            camera_pan_x: parseFloat(cameraPanXInput.value),
            camera_pan_y: parseFloat(cameraPanYInput.value)
          })
        });
        const data = await res.json();
        alert(data.message || "カメラ向き・倍率設定を適用しました。");
        loadSnapshot();
      } catch (e) {
        alert("カメラ設定の適用に失敗しました: " + e.message);
      } finally {
        saveCameraTransformBtn.disabled = false;
        saveCameraTransformBtn.textContent = "📷 カメラ向き・倍率を適用";
      }
    });
  }

  // 1. 靴箱位置保存
  saveGridBtn.addEventListener("click", async () => {
    try {
      saveGridBtn.disabled = true;
      saveGridBtn.textContent = "保存中...";
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ corners })
      });
      const data = await res.json();
      alert(data.message || "靴箱の位置設定を保存しました。");
    } catch (e) {
      alert("保存に失敗しました: " + e.message);
    } finally {
      saveGridBtn.disabled = false;
      saveGridBtn.textContent = "💾 靴箱位置を保存";
    }
  });

  // 2. YOLO設定保存
  yoloConfInput.addEventListener("input", () => {
    yoloConfVal.textContent = Number(yoloConfInput.value).toFixed(2);
  });

  saveYoloBtn.addEventListener("click", async () => {
    try {
      saveYoloBtn.disabled = true;
      saveYoloBtn.textContent = "適用中...";
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: detectionModeSelect.value,
          yolo_model_path: yoloModelInput.value.trim(),
          yolo_conf: parseFloat(yoloConfInput.value),
          person_occlusion_enabled: personOcclusionCheck.checked
        })
      });
      const data = await res.json();
      if (data.yolo_backend) {
        yoloBackendBadge.textContent = `🤖 ${data.yolo_backend}`;
      }
      alert(data.message || "YOLO設定を保存しました。");
    } catch (e) {
      alert("YOLO設定保存失敗: " + e.message);
    } finally {
      saveYoloBtn.disabled = false;
      saveYoloBtn.textContent = "🤖 YOLO設定を保存・適用";
    }
  });

  // 3. キャリブレーション & 安定化パラメータ
  calibrateBtn.addEventListener("click", async () => {
    if (!confirm("現在の靴箱映像を『空（靴なし）』の基準データとして学習・保存しますか？\n※すべてのマスが空の状態で実行してください。")) {
      return;
    }
    try {
      calibrateBtn.disabled = true;
      calibrateBtn.textContent = "学習中...";
      const res = await fetch("/api/calibrate", { method: "POST" });
      const data = await res.json();
      alert(data.message || "キャリブレーションを完了しました。");
      loadSnapshot();
    } catch (e) {
      alert("キャリブレーション失敗: " + e.message);
    } finally {
      calibrateBtn.disabled = false;
      calibrateBtn.textContent = "🎯 現在の映像を「空」として学習";
    }
  });

  thresholdInput.addEventListener("input", () => {
    thresholdVal.textContent = Number(thresholdInput.value).toFixed(2);
  });
  stableSecInput.addEventListener("input", () => {
    stableSecVal.textContent = Number(stableSecInput.value).toFixed(1);
  });

  saveParamsBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          threshold: parseFloat(thresholdInput.value),
          stable_duration_sec: parseFloat(stableSecInput.value)
        })
      });
      const data = await res.json();
      alert(data.message || "パラメータを保存しました。");
    } catch (e) {
      alert("パラメータ保存失敗: " + e.message);
    }
  });

  // 4. スケジュール保存
  saveScheduleBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          schedule: {
            enabled: scheduleEnableCheck.checked,
            start_time: schedStartTime.value,
            end_time: schedEndTime.value
          }
        })
      });
      const data = await res.json();
      alert(data.message || "スケジュールを保存しました。");
    } catch (e) {
      alert("スケジュール保存失敗: " + e.message);
    }
  });

  // 6. パスワード変更
  const savePasswordBtn = document.getElementById("savePasswordBtn");
  const newAdminPassword = document.getElementById("newAdminPassword");
  if (savePasswordBtn) {
    savePasswordBtn.addEventListener("click", async () => {
      const pwd = newAdminPassword.value.trim();
      if (!pwd) {
        alert("新しいパスワードを入力してください。");
        return;
      }
      try {
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ admin_password: pwd })
        });
        const data = await res.json();
        alert(data.message || "管理者パスワードを変更しました。次回ログイン時から有効になります。");
        newAdminPassword.value = "";
      } catch (e) {
        alert("パスワード変更失敗: " + e.message);
      }
    });
  }

  // 7. クラウドWeb配信連携設定
  const saveCloudSyncBtn = document.getElementById("saveCloudSyncBtn");
  const testCloudSyncBtn = document.getElementById("testCloudSyncBtn");
  const cloudSyncEnabled = document.getElementById("cloudSyncEnabled");
  const cloudUrlInput = document.getElementById("cloudUrlInput");
  const cloudApiKeyInput = document.getElementById("cloudApiKeyInput");
  const cloudSyncStatusText = document.getElementById("cloudSyncStatusText");

  if (saveCloudSyncBtn) {
    saveCloudSyncBtn.addEventListener("click", async () => {
      const enabled = cloudSyncEnabled ? cloudSyncEnabled.checked : false;
      const cloudUrl = cloudUrlInput ? cloudUrlInput.value.trim() : "";
      const apiKey = cloudApiKeyInput ? cloudApiKeyInput.value.trim() : "";

      try {
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cloud_sync: {
              enabled: enabled,
              cloud_url: cloudUrl,
              api_key: apiKey
            }
          })
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}: 設定保存に失敗しました。ログイン状態をご確認ください。`);
        }
        const data = await res.json();
        alert(data.message || "クラウド配信設定を保存しました。");
        if (cloudSyncStatusText) {
          cloudSyncStatusText.textContent = enabled ? "送信開始待機中..." : "同期停止中 (OFF)";
          cloudSyncStatusText.style.color = enabled ? "#38bdf8" : "#94a3b8";
        }
      } catch (e) {
        alert("クラウド設定保存失敗: " + e.message);
      }
    });
  }

  if (testCloudSyncBtn) {
    testCloudSyncBtn.addEventListener("click", async () => {
      if (cloudSyncStatusText) {
        cloudSyncStatusText.textContent = "疎通テスト送信中...";
        cloudSyncStatusText.style.color = "#fbbf24";
      }
      try {
        const res = await fetch("/api/cloud/test", { method: "POST" });
        if (res.status === 404) {
          throw new Error("HTTP 404: 疎通テスト用エンドポイントが見つかりません。ラズパイで 'python app.py' を再起動（Ctrl+Cして再実行）してください。");
        }
        if (res.status === 401) {
          throw new Error("HTTP 401: ログインセッションが切れました。ページを再読み込みして再ログインしてください。");
        }
        if (!res.ok) {
          throw new Error(`HTTP ${res.status} エラーが発生しました。`);
        }
        const data = await res.json();
        alert(data.message);
        if (cloudSyncStatusText) {
          cloudSyncStatusText.textContent = data.message;
          cloudSyncStatusText.style.color = data.status === "success" ? "#34d399" : "#f87171";
        }
      } catch (e) {
        alert("疎通テスト通信エラー: " + e.message);
        if (cloudSyncStatusText) {
          cloudSyncStatusText.textContent = "通信エラー";
          cloudSyncStatusText.style.color = "#f87171";
        }
      }
    });
  }

  // 5. 人影映り込みテスト & スロットトグル
  if (triggerPersonBtn) {
    triggerPersonBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/test/trigger_person", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ duration: 5.0 })
        });
        setTimeout(() => loadSnapshot(), 300);
        setTimeout(() => loadSnapshot(), 5500);
      } catch (e) {
        console.error(e);
      }
    });
  }

  function buildSlotButtons() {
    slotTestGrid.innerHTML = "";
    for (let i = 0; i < NUM_SLOTS; i++) {
      const btn = document.createElement("button");
      btn.className = "slot-btn" + (currentSlotStates[i] ? " full" : "");
      btn.textContent = `${i + 1}`;
      btn.title = `スロット ${i + 1} の切替`;
      btn.addEventListener("click", async () => {
        try {
          const res = await fetch("/api/test/toggle_slot", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ index: i })
          });
          const d = await res.json();
          currentSlotStates[i] = d.is_full;
          btn.className = "slot-btn" + (d.is_full ? " full" : "");
          loadSnapshot();
        } catch (err) {
          console.error(err);
        }
      });
      slotTestGrid.appendChild(btn);
    }
  }

  function updateSlotButtonsUI() {
    const btns = slotTestGrid.querySelectorAll(".slot-btn");
    btns.forEach((btn, idx) => {
      if (idx < currentSlotStates.length) {
        if (currentSlotStates[idx]) {
          btn.classList.add("full");
        } else {
          btn.classList.remove("full");
        }
      }
    });
  }

  // 定期ステータスポーリング（判定状態の同期）
  setInterval(async () => {
    if (isLiveMode) return;
    try {
      const res = await fetch("/api/admin/status");
      const data = await res.json();
      updateSlotStates(data.result || data);

    // クラウド遠隔監視用：ステータス更新時にbase64画像があればCanvas背景に即時反映
    if (data.image_b64 && !isLiveMode) {
      backgroundImage.src = "data:image/jpeg;base64," + data.image_b64;
    }

      if (draggingPointIdx === -1) {
        renderCanvas();
      }
    } catch (e) {}
  }, 1500);

  // ----------------- タブ切り替え -----------------
  const tabBtnGrid = document.getElementById("tabBtnGrid");
  const tabBtnLogs = document.getElementById("tabBtnLogs");
  const sectionGrid = document.getElementById("sectionGrid");
  const sectionLogs = document.getElementById("sectionLogs");

  if (tabBtnGrid && tabBtnLogs) {
    tabBtnGrid.addEventListener("click", () => {
      tabBtnGrid.classList.add("active");
      tabBtnLogs.classList.remove("active");
      sectionGrid.style.display = "block";
      sectionLogs.style.display = "none";
      if (!isLiveMode) loadSnapshot();
    });

    tabBtnLogs.addEventListener("click", () => {
      tabBtnLogs.classList.add("active");
      tabBtnGrid.classList.remove("active");
      sectionGrid.style.display = "none";
      sectionLogs.style.display = "block";
      loadLogDates();
    });
  }

  // ----------------- 入退室ログ & 統計ヒストグラム -----------------
  const logDateSelect = document.getElementById("logDateSelect");
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const btnDownloadSessions = document.getElementById("btnDownloadSessions");
  const btnDownloadUtilization = document.getElementById("btnDownloadUtilization");
  const btnDownloadEntryHist = document.getElementById("btnDownloadEntryHist");
  const btnDownloadDurationHist = document.getElementById("btnDownloadDurationHist");

  const statTotalUsers = document.getElementById("statTotalUsers");
  const statAvgDuration = document.getElementById("statAvgDuration");
  const statMaxDuration = document.getElementById("statMaxDuration");
  const statPeakRate = document.getElementById("statPeakRate");
  const statAvgRate = document.getElementById("statAvgRate");
  const sessionTableBody = document.getElementById("sessionTableBody");
  const logCountLabel = document.getElementById("logCountLabel");

  let entryChart = null;
  let durationChart = null;
  let utilizationChart = null;

  async function loadLogDates() {
    try {
      const res = await fetch("/api/logs/dates");
      const data = await res.json();
      const currentSelected = logDateSelect.value;
      logDateSelect.innerHTML = "";
      if (data.dates && data.dates.length > 0) {
        data.dates.forEach(d => {
          const opt = document.createElement("option");
          opt.value = d;
          opt.textContent = d;
          logDateSelect.appendChild(opt);
        });
        if (currentSelected && data.dates.includes(currentSelected)) {
          logDateSelect.value = currentSelected;
        }
      }
      loadDailyLogData(logDateSelect.value);
    } catch (e) {
      console.error("日付一覧取得失敗:", e);
    }
  }

  async function loadDailyLogData(dateStr) {
    if (!dateStr) return;
    try {
      btnDownloadSessions.href = `/api/logs/download?date=${dateStr}&type=sessions`;
      if (btnDownloadUtilization) btnDownloadUtilization.href = `/api/logs/download?date=${dateStr}&type=utilization_10min`;
      btnDownloadEntryHist.href = `/api/logs/download?date=${dateStr}&type=entry_hist`;
      btnDownloadDurationHist.href = `/api/logs/download?date=${dateStr}&type=duration_hist`;

      const res = await fetch(`/api/logs/daily?date=${dateStr}`);
      const data = await res.json();

      statTotalUsers.innerHTML = `${data.total_users} <small>人</small>`;
      statAvgDuration.innerHTML = `${data.avg_duration_min} <small>分</small>`;
      statMaxDuration.innerHTML = `${data.max_duration_min} <small>分</small>`;
      if (statPeakRate) statPeakRate.innerHTML = `${data.peak_utilization_rate !== undefined ? data.peak_utilization_rate : 0.0} <small>%</small>`;
      if (statAvgRate) statAvgRate.innerHTML = `${data.avg_utilization_rate !== undefined ? data.avg_utilization_rate : 0.0} <small>%</small>`;
      logCountLabel.textContent = `${data.total_users} 件の記録`;

      renderSessionTable(data.sessions);
      renderHistograms(data.entry_histogram, data.duration_histogram, data.utilization_10min);
    } catch (e) {
      console.error("ログデータ取得失敗:", e);
    }
  }

  function renderSessionTable(sessions) {
    sessionTableBody.innerHTML = "";
    if (!sessions || sessions.length === 0) {
      sessionTableBody.innerHTML = `
        <tr>
          <td colspan="5" style="text-align: center; color: #94a3b8; padding: 24px;">
            この日の入退室データはまだありません。スリッパが出し入れされると自動的に記録されます。
          </td>
        </tr>`;
      return;
    }

    sessions.forEach((s, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="badge-slot">スロット ${s["スロット"] || s["slot"]}</span></td>
        <td>${s["入室時間"] || "-"}</td>
        <td>${s["退室時間"] || "-"}</td>
        <td><span class="duration-tag">${s["利用時間(分)"] || s["duration_min"]} 分</span> (${s["利用秒数"] || 0}秒)</td>
      `;
      sessionTableBody.appendChild(tr);
    });
  }

  function renderHistograms(entryHist, durationHist, utilHist) {
    if (typeof Chart === "undefined") return;

    // 1. 10分おき利用率［％］推移グラフ (折れ線・塗りつぶし)
    const canvasUtil = document.getElementById("utilizationRateChart");
    if (canvasUtil) {
      const ctxUtil = canvasUtil.getContext("2d");
      const utilLabels = utilHist && utilHist.length > 0 ? utilHist.map(h => h.label) : [];
      const utilRates = utilHist && utilHist.length > 0 ? utilHist.map(h => h.utilization_rate) : [];

      if (utilizationChart) utilizationChart.destroy();
      utilizationChart = new Chart(ctxUtil, {
        type: "line",
        data: {
          labels: utilLabels,
          datasets: [{
            label: "利用率 (%)",
            data: utilRates,
            borderColor: "#8b5cf6",
            backgroundColor: "rgba(139, 92, 246, 0.18)",
            borderWidth: 2.5,
            fill: true,
            tension: 0.35,
            pointRadius: 3.5,
            pointHoverRadius: 6,
            pointBackgroundColor: "#c084fc",
            pointBorderColor: "#7c3aed"
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(context) {
                  const idx = context.dataIndex;
                  const item = (utilHist && utilHist[idx]) ? utilHist[idx] : null;
                  if (item) {
                    return `利用率: ${context.parsed.y}% (平均 ${item.avg_users}人 / ピーク ${item.peak_users}人)`;
                  }
                  return `利用率: ${context.parsed.y}%`;
                }
              }
            }
          },
          scales: {
            x: {
              grid: { color: "rgba(255, 255, 255, 0.05)" },
              ticks: { color: "#94a3b8", maxRotation: 45, minRotation: 45, font: { size: 10 } }
            },
            y: {
              beginAtZero: true,
              max: 100,
              ticks: {
                color: "#94a3b8",
                stepSize: 20,
                callback: function(val) { return val + "%"; }
              },
              grid: { color: "rgba(255, 255, 255, 0.08)" }
            }
          }
        }
      });
    }

    const canvasEntry = document.getElementById("entryHistogramChart");
    const canvasDur = document.getElementById("durationHistogramChart");
    if (!canvasEntry || !canvasDur) return;

    const ctxEntry = canvasEntry.getContext("2d");
    const entryLabels = entryHist ? entryHist.map(h => h.label) : [];
    const entryData = entryHist ? entryHist.map(h => h.count) : [];

    if (entryChart) entryChart.destroy();
    entryChart = new Chart(ctxEntry, {
      type: "bar",
      data: {
        labels: entryLabels,
        datasets: [{
          label: "入室人数 (人)",
          data: entryData,
          backgroundColor: "rgba(14, 165, 233, 0.75)",
          borderColor: "#0284c7",
          borderWidth: 1.5,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: {
            grid: { color: "rgba(255, 255, 255, 0.05)" },
            ticks: { color: "#94a3b8", maxRotation: 45, minRotation: 45, font: { size: 10 } }
          },
          y: {
            beginAtZero: true,
            ticks: { color: "#94a3b8", stepSize: 1 },
            grid: { color: "rgba(255, 255, 255, 0.08)" }
          }
        }
      }
    });

    const ctxDur = canvasDur.getContext("2d");
    const durLabels = durationHist ? durationHist.map(h => h.label) : [];
    const durData = durationHist ? durationHist.map(h => h.count) : [];

    if (durationChart) durationChart.destroy();
    durationChart = new Chart(ctxDur, {
      type: "bar",
      data: {
        labels: durLabels,
        datasets: [{
          label: "人数 (人)",
          data: durData,
          backgroundColor: "rgba(16, 185, 129, 0.75)",
          borderColor: "#059669",
          borderWidth: 1.5,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: {
            grid: { color: "rgba(255, 255, 255, 0.05)" },
            ticks: { color: "#94a3b8", maxRotation: 45, minRotation: 45, font: { size: 10 } }
          },
          y: {
            beginAtZero: true,
            ticks: { color: "#94a3b8", stepSize: 1 },
            grid: { color: "rgba(255, 255, 255, 0.08)" }
          }
        }
      }
    });
  }

  if (logDateSelect) {
    logDateSelect.addEventListener("change", () => {
      loadDailyLogData(logDateSelect.value);
    });
  }

  if (refreshLogsBtn) {
    refreshLogsBtn.addEventListener("click", () => {
      loadDailyLogData(logDateSelect.value);
    });
  }

  initStatus();
});

</script>
</body>
</html>
"""

# -------------------------------------------------------------
# 4. 認証デコレータとWebルーティング
# -------------------------------------------------------------
def admin_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/")
def index():
    """スマホ・一般利用者向け配信トップページ"""
    template_path = os.path.join(app.root_path, "templates", "index.html")
    if os.path.exists(template_path):
        return render_template("index.html")
    return render_template_string(INLINE_HTML)


@app.route("/admin")
@admin_login_required
def admin_dashboard():
    """クラウド遠隔統合システム管理画面ダッシュボード"""
    return render_template_string(INLINE_ADMIN_HTML)


@app.route("/login", methods=["GET", "POST"])
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """管理画面ログイン"""
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "パスワードが正しくありません。"
    return render_template_string(INLINE_LOGIN_HTML, error=error)


@app.route("/logout")
@app.route("/admin/logout")
def admin_logout():
    """管理画面ログアウト"""
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


@app.route("/api/status")
def api_status():
    """一般画面用 ポーリングAPI"""
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


@app.route("/api/admin/status")
@admin_login_required
def api_admin_status():
    """自室PC管理画面用 リアルタイム遠隔監視詳細API"""
    now = time.time()
    last_recv = latest_status["last_received_timestamp"]
    is_online = (last_recv > 0) and (now - last_recv <= OFFLINE_THRESHOLD_SEC)

    res_result = {
        "stable_count": latest_status["count"],
        "stable_states": latest_vision["stable_states"],
        "scores": latest_vision["scores"],
        "yolo_boxes": latest_vision["yolo_boxes"],
        "yolo_slipper_count": len([b for b in latest_vision["yolo_boxes"] if b.get("label") != "person"]),
        "is_occluded": latest_vision["is_occluded"],
        "occlusion_reason": latest_vision["occlusion_reason"],
        "yolo_backend": latest_config["yolo_backend"]
    }

    res = {
        "status": "success",
        "result": res_result,
        "is_online": is_online,
        "count": latest_status["count"],
        "max_slots": latest_status["max_slots"],
        "empty_slots": latest_status["empty_slots"],
        "crowd_level": latest_status["crowd_level"],
        "updated_at": latest_status["updated_at"],
        "corners": latest_config["corners"],
        "cols": 3,
        "rows": 12,
        "mode": latest_config["mode"],
        "yolo_model_path": latest_config["yolo_model_path"],
        "yolo_conf": latest_config["yolo_conf"],
        "person_occlusion_enabled": latest_config["person_occlusion_enabled"],
        "yolo_backend": latest_config["yolo_backend"],
        "threshold": latest_config["threshold"],
        "stable_duration_sec": latest_config["stable_duration_sec"],
        "camera_rotation": latest_config["camera_rotation"],
        "camera_zoom": latest_config["camera_zoom"],
        "camera_pan_x": latest_config["camera_pan_x"],
        "camera_pan_y": latest_config["camera_pan_y"],
        "schedule": latest_config["schedule"],
        "cloud_sync": {
            "enabled": True,
            "cloud_url": "https://dorm-bath-monitor.onrender.com",
            "api_key": "********",
            "message": "常時オンライン中継中" if is_online else "ラズパイ待機中",
            "is_success": is_online
        },
        "is_mock": False,
        "image_b64": latest_vision["image_b64"]
    }
    return jsonify(res)


@app.route("/api/camera/snapshot")
@admin_login_required
def api_camera_snapshot():
    """管理画面用 カメラ静止画の取得 (最新フレームを返却)"""
    b64 = latest_vision.get("image_b64")
    if b64:
        try:
            raw_bytes = base64.b64decode(b64)
            return Response(raw_bytes, mimetype="image/jpeg")
        except Exception:
            pass

    # 静止画未着時のプレースホルダーSVG
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="720" height="1280" viewBox="0 0 720 1280">'
           '<rect width="100%" height="100%" fill="#0f172a"/>'
           '<text x="50%" y="48%" fill="#64748b" font-family="sans-serif" font-size="28" text-anchor="middle">寮風呂場モニター</text>'
           '<text x="50%" y="53%" fill="#38bdf8" font-family="sans-serif" font-size="22" text-anchor="middle">ラズパイからのカメラ映像待機中...</text>'
           '</svg>')
    return Response(svg, mimetype="image/svg+xml")


@app.route("/camera_stream")
@admin_login_required
def camera_stream():
    """管理画面用 ライブストリーム (最新受信フレームを中継)"""
    def generate():
        while True:
            b64 = latest_vision.get("image_b64")
            if b64:
                try:
                    raw_bytes = base64.b64decode(b64)
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" + raw_bytes + b"\r\n")
                except Exception:
                    pass
            time.sleep(2.0)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/config", methods=["POST"])
@app.route("/api/admin/config", methods=["POST"])
@admin_login_required
def api_admin_config():
    """自室PC管理画面からの遠隔設定保存 (ラズパイ実機へ次回通信で自動適用)"""
    global pending_remote_config
    data = request.get_json(silent=True) or {}

    # 1. 靴箱位置
    if "corners" in data:
        latest_config["corners"] = data["corners"]
        pending_remote_config["corners"] = data["corners"]

    # 2. 判定パラメータ
    if "threshold" in data:
        latest_config["threshold"] = float(data["threshold"])
        pending_remote_config["threshold"] = float(data["threshold"])
    if "stable_duration_sec" in data:
        latest_config["stable_duration_sec"] = float(data["stable_duration_sec"])
        pending_remote_config["stable_duration_sec"] = float(data["stable_duration_sec"])

    # 3. YOLO AI認識設定
    if "mode" in data:
        latest_config["mode"] = data["mode"]
        pending_remote_config["mode"] = data["mode"]
    if "yolo_conf" in data:
        latest_config["yolo_conf"] = float(data["yolo_conf"])
        pending_remote_config["yolo_conf"] = float(data["yolo_conf"])
    if "yolo_model_path" in data:
        latest_config["yolo_model_path"] = data["yolo_model_path"]
        pending_remote_config["yolo_model_path"] = data["yolo_model_path"]
    if "person_occlusion_enabled" in data:
        latest_config["person_occlusion_enabled"] = bool(data["person_occlusion_enabled"])
        pending_remote_config["person_occlusion_enabled"] = bool(data["person_occlusion_enabled"])

    # 4. カメラ向き・倍率
    cam_rot = data.get("camera_rotation")
    cam_zoom = data.get("camera_zoom")
    cam_pan_x = data.get("camera_pan_x")
    cam_pan_y = data.get("camera_pan_y")
    if cam_rot is not None:
        latest_config["camera_rotation"] = int(cam_rot)
        pending_remote_config["camera_rotation"] = int(cam_rot)
    if cam_zoom is not None:
        latest_config["camera_zoom"] = float(cam_zoom)
        pending_remote_config["camera_zoom"] = float(cam_zoom)
    if cam_pan_x is not None:
        latest_config["camera_pan_x"] = float(cam_pan_x)
        pending_remote_config["camera_pan_x"] = float(cam_pan_x)
    if cam_pan_y is not None:
        latest_config["camera_pan_y"] = float(cam_pan_y)
        pending_remote_config["camera_pan_y"] = float(cam_pan_y)

    # 5. スケジュール
    if "schedule" in data:
        latest_config["schedule"] = data["schedule"]
        pending_remote_config["schedule"] = data["schedule"]

    # 6. パスワード
    if "admin_password" in data:
        global ADMIN_PASSWORD
        ADMIN_PASSWORD = str(data["admin_password"]).strip()
        pending_remote_config["admin_password"] = ADMIN_PASSWORD

    return jsonify({
        "status": "success",
        "message": "遠隔設定を保存しました。お風呂場のラズパイへ数秒で自動適用されます。"
    })


@app.route("/api/calibrate", methods=["POST"])
@admin_login_required
def api_calibrate():
    """自室PCからのお風呂場背景キャリブレーション指示"""
    pending_remote_config["calibrate"] = True
    return jsonify({
        "status": "success",
        "message": "背景キャリブレーション指示を送信しました。ラズパイが次回通信時に現在の映像を空靴箱として学習します。"
    })


@app.route("/api/password", methods=["POST"])
@admin_login_required
def api_password():
    """管理者パスワード変更"""
    global ADMIN_PASSWORD
    data = request.get_json(silent=True) or {}
    new_pw = data.get("password") or data.get("admin_password")
    if not new_pw:
        return jsonify({"status": "error", "message": "パスワードを入力してください。"}), 400
    ADMIN_PASSWORD = str(new_pw).strip()
    pending_remote_config["admin_password"] = ADMIN_PASSWORD
    return jsonify({
        "status": "success",
        "message": "管理者パスワードを更新しました。クラウドおよびラズパイの両方に適用されます。"
    })


@app.route("/api/cloud/test", methods=["POST"])
@admin_login_required
def api_cloud_test():
    """管理画面からの接続テスト応答"""
    return jsonify({
        "status": "success",
        "message": "クラウドサーバーと正常に接続されています。"
    })


@app.route("/api/test/toggle_slot", methods=["POST"])
@admin_login_required
def api_test_toggle_slot():
    """テスト用：スロット状態の反転"""
    data = request.get_json(silent=True) or {}
    idx = data.get("index", 0)
    pending_remote_config["test_toggle_slot"] = idx
    if 0 <= idx < 36:
        latest_vision["stable_states"][idx] = not latest_vision["stable_states"][idx]
        return jsonify({"status": "success", "slot_index": idx, "is_full": latest_vision["stable_states"][idx]})
    return jsonify({"status": "error", "message": "Invalid index"}), 400


@app.route("/api/test/trigger_person", methods=["POST"])
@admin_login_required
def api_test_trigger_person():
    """テスト用：人影（person）映り込みシミュレート"""
    data = request.get_json(silent=True) or {}
    duration = float(data.get("duration", 5.0))
    pending_remote_config["test_trigger_person"] = duration
    latest_vision["is_occluded"] = True
    latest_vision["occlusion_reason"] = "人影映り込みシミュレート"
    return jsonify({
        "status": "success",
        "message": f"人影（person）映り込みを {duration:.0f} 秒間シミュレートします。"
    })


@app.route("/api/logs/dates")
@admin_login_required
def api_log_dates():
    """ログが存在する日付一覧を取得"""
    dates = list(latest_available_dates)
    today_str = get_today_str()
    if today_str not in dates:
        dates.insert(0, today_str)
    return jsonify({"dates": dates})


@app.route("/api/logs/daily")
@admin_login_required
def api_daily_log():
    """指定日のセッション一覧とヒストグラムデータを取得"""
    date_str = request.args.get("date", get_today_str())
    today_str = get_today_str()

    if date_str == today_str and latest_daily_summary:
        return jsonify(latest_daily_summary)

    recalculate_cloud_utilization()
    rates = [item["utilization_rate"] for item in cloud_utilization_10min]
    peak_rate = max(rates) if rates else 0.0
    avg_rate = round(sum(rates) / len(rates), 1) if rates else 0.0

    return jsonify({
        "date": date_str,
        "total_users": len(cloud_daily_logs),
        "avg_duration_min": 0.0,
        "max_duration_min": 0.0,
        "peak_utilization_rate": peak_rate,
        "avg_utilization_rate": avg_rate,
        "sessions": [],
        "entry_histogram": [],
        "duration_histogram": [],
        "utilization_10min": cloud_utilization_10min
    })


@app.route("/api/logs/download")
@app.route("/api/admin/download")
@admin_login_required
def api_download_log():
    """指定日のログCSVファイルをダウンロード"""
    date_str = request.args.get("date", get_today_str())
    log_type = request.args.get("type", "sessions")

    output = io.StringIO()
    writer = csv.writer(output)
    filename = f"{date_str}_{log_type}.csv"

    if log_type == "sessions":
        writer.writerow(["No.", "スロット", "入室時間", "退室時間", "利用時間(分)"])
        sessions = latest_daily_summary.get("sessions", [])
        for i, s in enumerate(sessions):
            writer.writerow([
                s.get("No.", i + 1),
                s.get("スロット", s.get("slot", "")),
                s.get("入室時間", s.get("entry_time", "")),
                s.get("退室時間", s.get("exit_time", "")),
                s.get("利用時間(分)", s.get("duration_min", ""))
            ])
        filename = f"{date_str}_sessions.csv"

    elif log_type == "utilization_10min":
        recalculate_cloud_utilization()
        writer.writerow(["時間帯", "平均人数(人)", "利用率(%)", "ピーク人数(人)"])
        for row in cloud_utilization_10min:
            writer.writerow([row["label"], row["avg_users"], row["utilization_rate"], row["peak_users"]])
        filename = f"{date_str}_utilization_10min.csv"

    elif log_type in ("entry_hist", "histogram_entry_30min"):
        writer.writerow(["時間帯", "入室人数(人)"])
        entry_hist = latest_daily_summary.get("entry_histogram", [])
        for row in entry_hist:
            writer.writerow([row.get("label", ""), row.get("count", 0)])
        filename = f"{date_str}_histogram_entry_30min.csv"

    elif log_type in ("duration_hist", "histogram_duration_5min"):
        writer.writerow(["滞在時間帯", "人数(人)"])
        dur_hist = latest_daily_summary.get("duration_histogram", [])
        for row in dur_hist:
            writer.writerow([row.get("label", ""), row.get("count", 0)])
        filename = f"{date_str}_histogram_duration_5min.csv"

    elif log_type == "usage_log":
        writer.writerow(["timestamp", "date", "time", "stable_count", "max_slots", "utilization_rate(%)"])
        for item in cloud_daily_logs:
            writer.writerow([
                int(item.get("timestamp", 0)),
                item.get("date", date_str),
                item.get("time", ""),
                item.get("count", 0),
                item.get("max_slots", 36),
                item.get("rate", 0.0)
            ])
        filename = f"{date_str}_usage_log.csv"

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/api/update", methods=["POST"])
def api_update():
    """ラズベリーパイから人数・カメラ・グリッド判定データを受け取るエンドポイント"""
    global current_log_date, cloud_daily_logs, pending_remote_config, latest_daily_summary, latest_available_dates

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

    now_ts = time.time()
    dt_now = datetime.datetime.now()
    now_str = dt_now.strftime("%H:%M:%S")
    today_str = dt_now.strftime("%Y-%m-%d")

    # 日付変更時のローテーション
    if today_str != current_log_date:
        current_log_date = today_str
        cloud_daily_logs.clear()

    count = payload.get("count", latest_status["count"])
    max_slots = payload.get("max_slots", latest_status["max_slots"])
    rate = round((count / max_slots) * 100, 1) if max_slots > 0 else 0.0

    latest_status["count"] = count
    latest_status["max_slots"] = max_slots
    latest_status["empty_slots"] = payload.get("empty_slots", latest_status["empty_slots"])
    latest_status["crowd_level"] = payload.get("crowd_level", latest_status["crowd_level"])
    latest_status["crowd_class"] = payload.get("crowd_class", latest_status["crowd_class"])
    latest_status["schedule_message"] = payload.get("schedule_message", latest_status["schedule_message"])
    latest_status["is_active"] = payload.get("is_active", latest_status["is_active"])
    latest_status["is_occluded"] = payload.get("is_occluded", latest_status["is_occluded"])
    latest_status["occlusion_reason"] = payload.get("occlusion_reason", "")
    latest_status["updated_at"] = payload.get("updated_at", now_str)
    latest_status["last_received_timestamp"] = now_ts
    latest_status["is_online"] = True

    # 遠隔監視用グリッド詳細・カメラ画像の受信保存
    if "stable_states" in payload:
        latest_vision["stable_states"] = payload["stable_states"]
    if "scores" in payload:
        latest_vision["scores"] = payload["scores"]
    if "yolo_boxes" in payload:
        latest_vision["yolo_boxes"] = payload["yolo_boxes"]
    if "corners" in payload and not pending_remote_config.get("corners"):
        latest_config["corners"] = payload["corners"]
    if "is_occluded" in payload:
        latest_vision["is_occluded"] = payload["is_occluded"]
        latest_vision["occlusion_reason"] = payload.get("occlusion_reason", "")
    if payload.get("image_b64"):
        latest_vision["image_b64"] = payload["image_b64"]

    # 設定情報の同期
    if "threshold" in payload and not pending_remote_config.get("threshold"):
        latest_config["threshold"] = float(payload["threshold"])
    if "stable_duration_sec" in payload and not pending_remote_config.get("stable_duration_sec"):
        latest_config["stable_duration_sec"] = float(payload["stable_duration_sec"])
    if "mode" in payload and not pending_remote_config.get("mode"):
        latest_config["mode"] = payload["mode"]
    if "yolo_model_path" in payload and not pending_remote_config.get("yolo_model_path"):
        latest_config["yolo_model_path"] = payload["yolo_model_path"]
    if "yolo_conf" in payload and not pending_remote_config.get("yolo_conf"):
        latest_config["yolo_conf"] = float(payload["yolo_conf"])
    if "person_occlusion_enabled" in payload and not pending_remote_config.get("person_occlusion_enabled"):
        latest_config["person_occlusion_enabled"] = bool(payload["person_occlusion_enabled"])
    if "camera_rotation" in payload and not pending_remote_config.get("camera_rotation"):
        latest_config["camera_rotation"] = int(payload["camera_rotation"])
    if "camera_zoom" in payload and not pending_remote_config.get("camera_zoom"):
        latest_config["camera_zoom"] = float(payload["camera_zoom"])
    if "camera_pan_x" in payload and not pending_remote_config.get("camera_pan_x"):
        latest_config["camera_pan_x"] = float(payload["camera_pan_x"])
    if "camera_pan_y" in payload and not pending_remote_config.get("camera_pan_y"):
        latest_config["camera_pan_y"] = float(payload["camera_pan_y"])
    if "yolo_backend" in payload:
        latest_config["yolo_backend"] = payload["yolo_backend"]
    if "schedule" in payload and not pending_remote_config.get("schedule"):
        latest_config["schedule"] = payload["schedule"]

    # 日別統計サマリーの同期
    if "daily_summary" in payload:
        latest_daily_summary = payload["daily_summary"]
    if "available_dates" in payload:
        latest_available_dates = payload["available_dates"]

    # ログ蓄積 (変化時または1分以上経過時)
    should_log = False
    if not cloud_daily_logs:
        should_log = True
    else:
        last_entry = cloud_daily_logs[-1]
        if last_entry.get("count") != count or (now_ts - last_entry.get("timestamp", 0) >= 60.0):
            should_log = True

    if should_log:
        cloud_daily_logs.append({
            "timestamp": now_ts,
            "date": today_str,
            "time": now_str,
            "count": count,
            "max_slots": max_slots,
            "rate": rate
        })
        if len(cloud_daily_logs) > 5000:
            cloud_daily_logs.pop(0)

    # 未適用の遠隔設定があればラズパイに引き渡す
    response_data = {
        "status": "ok",
        "message": "Updated successfully",
        "server_time": now_str
    }
    if pending_remote_config:
        response_data["remote_config"] = dict(pending_remote_config)
        pending_remote_config.clear()

    return jsonify(response_data)


@app.route("/health")
def health():
    """Render等の死活監視用エンドポイント"""
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
