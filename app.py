"""
Dorm Bath Monitor - Cloud Distribution Server (Remote Live Management Edition)
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
templates / static フォルダが存在しない場合でも、単体1ファイルで完全動作する自己完結型設計。
お風呂場にあるラズパイのカメラ映像・36マス判定枠・寮生による妨害検知アラートを、
自室のPCから固定URLでリアルタイムに遠隔監視・設定できる「統合システム管理画面(/admin)」を搭載。
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
    end_total = end_h * 60 + end_m
    while curr < end_total:
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
    "updated_at": "--:--:--",
    "last_received_timestamp": 0.0,
    "is_online": False
}

# カメラ映像・グリッド詳細キャッシュ（遠隔監視用）
latest_vision = {
    "image_b64": None,              # 最新スナップショット base64
    "stable_states": [False] * 36,  # 36マスの状態
    "scores": [0.0] * 36,           # 36マスの差分スコア
    "yolo_boxes": [],               # YOLO検出ボックス
    "corners": [[60, 80], [660, 80], [660, 1200], [60, 1200]],
    "is_occluded": False,
    "occlusion_reason": "",
    "yolo_conf": 0.50,
    "threshold": 0.30,
    "stable_duration_sec": 4.0,
    "camera_rotation": 90,
    "camera_zoom": 1.4,
    "camera_pan_x": 0.05,
    "camera_pan_y": -0.05
}

# 自室PCから送られた未適用の遠隔設定
pending_remote_config = {}

OFFLINE_THRESHOLD_SEC = 30.0

# クラウド側で集計・保持する本日のログデータ
cloud_daily_logs = []         # [{"timestamp": float, "date": str, "time": str, "count": int, "max_slots": int, "rate": float}]
cloud_utilization_10min = []  # ラズパイから届いたか、またはクラウド側で算出した10分おき利用率

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
            except ValueError:
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
# 1. 一般利用者向け配信トップページ HTML (インライン)
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
    <p>管理パスワードを入力してください (初期値: daaaayama)</p>
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
# 3. クラウド遠隔統合システム管理画面 HTML (インライン・黒基調・フル機能)
# -------------------------------------------------------------
INLINE_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>遠隔システム管理 - 寮風呂場モニター (Cloud Live)</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg-dark: #0b0f19;
      --panel-bg: #111827;
      --card-bg: #1e293b;
      --border-color: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-blue: #0284c7;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-red: #ef4444;
      --accent-purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg-dark); color: var(--text-main); min-height: 100vh; padding: 16px 20px;
    }
    .wrapper { max-width: 1240px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }

    /* ヘッダー */
    .top-header {
      display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;
      background: var(--panel-bg); padding: 14px 20px; border-radius: 12px; border: 1px solid var(--border-color);
    }
    .header-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .header-left h1 { font-size: 1.25rem; font-weight: 700; color: #f8fafc; }
    .badge { padding: 4px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
    .badge-live { background: #065f46; color: #6ee7b7; border: 1px solid #10b981; }
    .badge-wait { background: #7f1d1d; color: #fca5a5; border: 1px solid #ef4444; }
    .badge-yolo { background: #3b0764; color: #e9d5ff; border: 1px solid #7e22ce; }
    .header-right { display: flex; align-items: center; gap: 10px; }
    .btn {
      padding: 7px 14px; border-radius: 8px; font-size: 0.84rem; font-weight: 600;
      text-decoration: none; cursor: pointer; transition: all 0.2s; border: none; display: inline-flex; align-items: center; gap: 6px;
    }
    .btn-secondary { background: #1e293b; color: #cbd5e1; border: 1px solid #475569; }
    .btn-secondary:hover { background: #334155; color: #fff; }
    .btn-primary { background: #0284c7; color: #fff; }
    .btn-primary:hover { background: #0369a1; }
    .btn-danger { background: #991b1b; color: #fff; }
    .btn-danger:hover { background: #b91c1c; }

    /* ナビゲーションタブ */
    .nav-tabs { display: flex; gap: 8px; border-bottom: 2px solid var(--border-color); padding-bottom: 2px; }
    .tab-btn {
      background: none; border: none; padding: 10px 18px; font-size: 0.95rem; font-weight: 700;
      color: var(--text-muted); cursor: pointer; border-radius: 8px 8px 0 0; transition: all 0.2s;
    }
    .tab-btn.active { color: #38bdf8; background: var(--panel-bg); border-bottom: 3px solid #38bdf8; }

    /* タブ1: グリッドレイアウト */
    .grid-layout { display: grid; grid-template-columns: 1fr 380px; gap: 16px; }
    @media (max-width: 992px) { .grid-layout { grid-template-columns: 1fr; } }

    .canvas-card {
      background: var(--panel-bg); border: 1px solid var(--border-color); border-radius: 12px;
      padding: 16px; display: flex; flex-direction: column; gap: 12px;
    }
    .canvas-toolbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
    .hint-text { font-size: 0.84rem; color: #38bdf8; }
    .canvas-box {
      position: relative; width: 100%; max-height: 75vh; margin: 0 auto;
      background: #000; border-radius: 8px; overflow: hidden;
      display: flex; justify-content: center; align-items: center;
      border: 1px solid #334155;
    }
    #gridCanvas {
      max-width: 100%; max-height: 75vh; width: auto; height: auto;
      object-fit: contain; cursor: crosshair; display: block;
    }
    .canvas-footer {
      display: flex; justify-content: space-between; font-size: 0.82rem; color: var(--text-muted);
      padding: 4px 2px; flex-wrap: wrap; gap: 8px;
    }
    .status-normal { color: #34d399; font-weight: 600; }
    .status-occluded { color: #f87171; font-weight: 700; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }

    /* サイドバー設定カード */
    .sidebar { display: flex; flex-direction: column; gap: 14px; }
    .panel {
      background: var(--panel-bg); border: 1px solid var(--border-color); border-radius: 12px;
      padding: 14px 16px;
    }
    .panel h2 { font-size: 1rem; font-weight: 700; color: #f8fafc; margin-bottom: 6px; }
    .panel-desc { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 12px; line-height: 1.4; }
    .form-group { margin-bottom: 12px; }
    .form-group label { display: block; font-size: 0.82rem; font-weight: 600; color: #cbd5e1; margin-bottom: 4px; }
    .form-input, .form-select {
      width: 100%; padding: 8px 10px; background: #0f172a; border: 1px solid #475569;
      border-radius: 6px; color: #f8fafc; font-size: 0.88rem; outline: none;
    }
    .form-range { width: 100%; cursor: pointer; accent-color: #0284c7; }
    .btn-block { width: 100%; padding: 10px; font-size: 0.88rem; justify-content: center; }

    /* タブ2: 統計ログ */
    .stats-layout { display: flex; flex-direction: column; gap: 16px; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
    .stat-card {
      background: var(--panel-bg); padding: 16px; border-radius: 12px;
      border: 1px solid var(--border-color);
    }
    .stat-label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); margin-bottom: 4px; }
    .stat-val { font-size: 1.8rem; font-weight: 800; color: #f8fafc; display: flex; align-items: baseline; gap: 4px; }
    .stat-unit { font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }
    .stat-sub { font-size: 0.78rem; color: var(--text-muted); margin-top: 4px; }

    .chart-panel {
      background: var(--panel-bg); border-radius: 12px; border: 1px solid var(--border-color);
      padding: 20px;
    }
    .chart-box { width: 100%; height: 320px; position: relative; margin-top: 10px; }
    .download-btns { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; padding-top: 16px; border-top: 1px dashed var(--border-color); }
  </style>
</head>
<body>
  <div class="wrapper">
    <!-- トップヘッダー -->
    <header class="top-header">
      <div class="header-left">
        <h1>⚙ 寮風呂場 遠隔システム管理 (Cloud Live)</h1>
        <span class="badge badge-wait" id="cloudStatusBadge">ラズパイ通信確認中...</span>
        <span class="badge badge-yolo">🤖 YOLOv8 遠隔監視</span>
      </div>
      <div class="header-right">
        <a href="/" target="_blank" class="btn btn-secondary">👁 一般公開モニターを開く</a>
        <a href="/admin/logout" class="btn btn-danger">🚪 ログアウト</a>
      </div>
    </header>

    <!-- ナビゲーションタブ -->
    <div class="nav-tabs">
      <button class="tab-btn active" id="tabBtnVision">🎯 靴箱36スロット位置調整 & リアルタイム遠隔監視</button>
      <button class="tab-btn" id="tabBtnStats">📊 10分おき利用率 & 入退室統計ダッシュボード</button>
    </div>

    <!-- 1. 靴箱位置調整 & リアルタイム遠隔監視 タブ -->
    <div id="viewVision" class="grid-layout">
      <!-- メインキャンバス -->
      <section class="canvas-card">
        <div class="canvas-toolbar">
          <span class="hint-text">💡 自室のPCから四隅アンカー（P1〜P4）をドラッグして靴箱に合わせ、「自室から靴箱位置を保存」を押してください。</span>
          <div style="display: flex; gap: 8px;">
            <button id="btnResetCorners" class="btn btn-secondary">🔄 アンカーを中央にリセット</button>
            <button id="btnRefreshSnapshot" class="btn btn-secondary">📷 最新映像を再取得</button>
          </div>
        </div>

        <div class="canvas-box" id="canvasBox">
          <canvas id="gridCanvas" width="720" height="1280"></canvas>
        </div>

        <div class="canvas-footer">
          <span id="canvasResText">カメラ解像度: 受信待機中...</span>
          <span id="canvasYoloCount">検出スリッパ: -- 個</span>
          <span id="canvasCursorCoords">カーソル座標: (0, 0)</span>
          <span id="canvasOcclusionStatus" class="status-normal">ステータス: 正常判定中</span>
        </div>
      </section>

      <!-- サイドバー設定パネル -->
      <aside class="sidebar">
        <!-- 遠隔カメラ位置合わせ保存 -->
        <div class="panel" style="border-color: #0284c7;">
          <h2>1. 💾 靴箱位置設定の遠隔保存</h2>
          <p class="panel-desc">四隅アンカー（P1〜P4）を調整後、保存するとお風呂場のラズパイへ自動同期されます。</p>
          <button id="btnSaveCorners" class="btn btn-primary btn-block">💾 自室から靴箱位置を保存</button>
        </div>

        <!-- YOLO AI認識設定 -->
        <div class="panel">
          <h2>2. 🤖 YOLO AI認識設定</h2>
          <div class="form-group">
            <label>YOLO 検出信頼度 (Confidence): <span id="lblYoloConf">0.50</span></label>
            <input type="range" id="rngYoloConf" class="form-range" min="0.10" max="0.80" step="0.05" value="0.50">
          </div>
          <button id="btnSaveYolo" class="btn btn-secondary btn-block">🤖 YOLO信頼度を遠隔保存</button>
        </div>

        <!-- 判定パラメータ設定 -->
        <div class="panel">
          <h2>3. 🎯 判定パラメータ設定</h2>
          <div class="form-group">
            <label>差分判定しきい値: <span id="lblThreshold">0.30</span></label>
            <input type="range" id="rngThreshold" class="form-range" min="0.05" max="0.60" step="0.05" value="0.30">
          </div>
          <div class="form-group">
            <label>確定までの安定化時間: <span id="lblStableSec">4.0</span> 秒</label>
            <input type="range" id="rngStableSec" class="form-range" min="0.5" max="6.0" step="0.5" value="4.0">
          </div>
          <button id="btnSaveParams" class="btn btn-secondary btn-block">安定化パラメータを遠隔保存</button>
        </div>

        <!-- 寮生妨害アラートカード -->
        <div class="panel" id="occlusionAlertPanel" style="border-color: #334155;">
          <h2>🚨 寮生による妨害・急変検知状況</h2>
          <p class="panel-desc" id="occlusionDescText">脱衣所でのスリッパ急変やカメラ前の遮蔽をリアルタイム検知します。</p>
          <div id="occlusionBadgeContainer" style="padding: 8px; border-radius: 6px; font-size: 0.85rem; font-weight: 700; background: #0f172a; text-align: center; color: #34d399;">
            ✅ 妨害なし (正常稼働中)
          </div>
        </div>
      </aside>
    </div>

    <!-- 2. 10分おき利用率 & 統計ダッシュボード タブ -->
    <div id="viewStats" class="stats-layout" style="display: none;">
      <div class="summary-grid">
        <div class="stat-card">
          <div class="stat-label">📡 ラズパイ通信状況</div>
          <div class="stat-val" id="statOnline">--</div>
          <div class="stat-sub" id="statLastRecv">最終受信: --:--:--</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">👥 現在の入浴人数</div>
          <div class="stat-val"><span id="statCount">--</span> <span class="stat-unit">/ 36 人</span></div>
          <div class="stat-sub" id="statCrowd">混雑度: 確認中</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">📈 最高利用率 (ピーク)</div>
          <div class="stat-val"><span id="statPeak">0.0</span> <span class="stat-unit">%</span></div>
          <div class="stat-sub" id="statPeakTime">時間帯: 集計中</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">📊 平均利用率 (本日)</div>
          <div class="stat-val"><span id="statAvg">0.0</span> <span class="stat-unit">%</span></div>
          <div class="stat-sub">16:30〜22:00 全33区間</div>
        </div>
      </div>

      <div class="chart-panel">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
          <h2 style="font-size: 1.1rem; font-weight: 700;">📊 風呂場利用率推移 (10分おき: 16:30〜22:00)</h2>
          <button id="btnRefreshChart" class="btn btn-secondary">🔄 グラフ更新</button>
        </div>
        <div class="chart-box">
          <canvas id="utilizationChart"></canvas>
        </div>
        <div class="download-btns">
          <a href="/api/admin/download?type=utilization_10min" class="btn btn-primary" download>
            📥 10分おき利用率CSVをダウンロード (utilization_10min.csv)
          </a>
          <a href="/api/admin/download?type=usage_log" class="btn btn-secondary" download>
            📥 本日の人数ログCSVをダウンロード (usage_log.csv)
          </a>
        </div>
      </div>
    </div>
  </div>

  <script>
    // -------------------------------------------------------------
    // フロントエンドロジック (HTML5 Canvas 3×12グリッドエディタ & 遠隔監視)
    // -------------------------------------------------------------
    const canvas = document.getElementById("gridCanvas");
    const ctx = canvas.getContext("2d");
    const COLS = 3;
    const ROWS = 12;
    const NUM_SLOTS = COLS * ROWS; // 36
    const POINT_RADIUS = 12;

    let corners = [
      [60, 80],
      [660, 80],
      [660, 1200],
      [60, 1200]
    ];

    let currentSlotStates = new Array(NUM_SLOTS).fill(false);
    let currentScores = new Array(NUM_SLOTS).fill(0.0);
    let currentYoloBoxes = [];
    let draggingPointIdx = -1;
    let bgImage = new Image();
    let isImageLoaded = false;
    let chartInstance = null;

    // タブ切り替え
    const tabBtnVision = document.getElementById("tabBtnVision");
    const tabBtnStats = document.getElementById("tabBtnStats");
    const viewVision = document.getElementById("viewVision");
    const viewStats = document.getElementById("viewStats");

    tabBtnVision.addEventListener("click", () => {
      tabBtnVision.classList.add("active");
      tabBtnStats.classList.remove("active");
      viewVision.style.display = "grid";
      viewStats.style.display = "none";
    });

    tabBtnStats.addEventListener("click", () => {
      tabBtnStats.classList.add("active");
      tabBtnVision.classList.remove("active");
      viewVision.style.display = "none";
      viewStats.style.display = "flex";
      loadChartData();
    });

    // スライダー連動
    const rngYoloConf = document.getElementById("rngYoloConf");
    const lblYoloConf = document.getElementById("lblYoloConf");
    rngYoloConf.addEventListener("input", () => { lblYoloConf.textContent = Number(rngYoloConf.value).toFixed(2); });

    const rngThreshold = document.getElementById("rngThreshold");
    const lblThreshold = document.getElementById("lblThreshold");
    rngThreshold.addEventListener("input", () => { lblThreshold.textContent = Number(rngThreshold.value).toFixed(2); });

    const rngStableSec = document.getElementById("rngStableSec");
    const lblStableSec = document.getElementById("lblStableSec");
    rngStableSec.addEventListener("input", () => { lblStableSec.textContent = Number(rngStableSec.value).toFixed(1); });

    // スロットのポリゴン計算
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
            return [(1 - v) * topX + v * botX, (1 - v) * topY + v * botY];
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

      if (isImageLoaded) {
        ctx.drawImage(bgImage, 0, 0, canvas.width, canvas.height);
      } else {
        ctx.fillStyle = "#0f172a";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#64748b";
        ctx.font = "22px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("お風呂場のカメラ映像を受信中...", canvas.width / 2, canvas.height / 2);
      }

      // 四隅外枠 (黄色)
      ctx.strokeStyle = "#eab308";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(corners[0][0], corners[0][1]);
      for (let i = 1; i < 4; i++) {
        ctx.lineTo(corners[i][0], corners[i][1]);
      }
      ctx.closePath();
      ctx.stroke();

      // 36マスのスロット枠描画 (緑: empty, 赤: full)
      const slots = getSlotPolygons();
      slots.forEach((poly, idx) => {
        const isFull = currentSlotStates[idx];
        ctx.strokeStyle = isFull ? "#ef4444" : "#10b981";
        ctx.lineWidth = 2;
        ctx.fillStyle = isFull ? "rgba(239, 68, 68, 0.28)" : "rgba(16, 185, 129, 0.16)";

        ctx.beginPath();
        ctx.moveTo(poly[0][0], poly[0][1]);
        for (let i = 1; i < 4; i++) { ctx.lineTo(poly[i][0], poly[i][1]); }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        const cx = (poly[0][0] + poly[1][0] + poly[2][0] + poly[3][0]) / 4;
        const cy = (poly[0][1] + poly[1][1] + poly[2][1] + poly[3][1]) / 4;
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 13px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const scoreTxt = currentScores[idx] !== undefined ? ` (${currentScores[idx].toFixed(2)})` : "";
        ctx.fillText(`${idx + 1}${scoreTxt}`, cx, cy);
      });

      // YOLOバウンディングボックス描画
      currentYoloBoxes.forEach((box) => {
        const [x1, y1, x2, y2] = box.bbox;
        if (box.label === "person") {
          ctx.strokeStyle = "#d946ef";
          ctx.lineWidth = 4;
          ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
          ctx.fillStyle = "rgba(217, 70, 239, 0.85)";
          ctx.fillRect(x1, Math.max(0, y1 - 28), 240, 28);
          ctx.fillStyle = "#ffffff";
          ctx.font = "bold 13px sans-serif";
          ctx.textAlign = "left";
          ctx.fillText(`YOLO: PERSON [人数ロック中]`, x1 + 8, Math.max(14, y1 - 10));
        } else {
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

      // 四隅操作ハンドル
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

    // 座標取得
    function getCanvasCoords(e) {
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      return [
        Math.round((e.clientX - rect.left) * scaleX),
        Math.round((e.clientY - rect.top) * scaleY)
      ];
    }

    // マウス操作
    canvas.addEventListener("mousedown", (e) => {
      const [mx, my] = getCanvasCoords(e);
      for (let i = 0; i < 4; i++) {
        const dx = mx - corners[i][0];
        const dy = my - corners[i][1];
        if (Math.hypot(dx, dy) <= POINT_RADIUS + 8) {
          draggingPointIdx = i;
          renderCanvas();
          return;
        }
      }
    });

    canvas.addEventListener("mousemove", (e) => {
      const [mx, my] = getCanvasCoords(e);
      document.getElementById("canvasCursorCoords").textContent = `カーソル座標: (${mx}, ${my})`;

      if (draggingPointIdx !== -1) {
        corners[draggingPointIdx] = [
          Math.max(10, Math.min(canvas.width - 10, mx)),
          Math.max(10, Math.min(canvas.height - 10, my))
        ];
        renderCanvas();
      } else {
        let hovering = false;
        for (let i = 0; i < 4; i++) {
          const dx = mx - corners[i][0];
          const dy = my - corners[i][1];
          if (Math.hypot(dx, dy) <= POINT_RADIUS + 8) {
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

    // アンカー初期化ボタン
    document.getElementById("btnResetCorners").addEventListener("click", () => {
      const w = canvas.width || 720;
      const h = canvas.height || 1280;
      corners = [
        [Math.round(w * 0.08), Math.round(h * 0.06)],
        [Math.round(w * 0.92), Math.round(h * 0.06)],
        [Math.round(w * 0.92), Math.round(h * 0.94)],
        [Math.round(w * 0.08), Math.round(h * 0.94)]
      ];
      renderCanvas();
      alert("四隅アンカー（P1〜P4）を中央に再配置しました。\n「💾 自室から靴箱位置を保存」を押してラズパイへ反映してください。");
    });

    // 最新スナップショット取得
    async function fetchVisionStatus() {
      try {
        const res = await fetch("/api/admin/status");
        if (!res.ok) return;
        const data = await res.json();

        // 通信ステータス
        const badge = document.getElementById("cloudStatusBadge");
        if (data.is_online) {
          badge.className = "badge badge-live";
          badge.textContent = `🟢 ラズパイ接続中 (最終受信: ${data.updated_at})`;
        } else {
          badge.className = "badge badge-wait";
          badge.textContent = `🔴 ラズパイ待機中 (オフライン)`;
        }

        if (data.corners && draggingPointIdx === -1 && !userEditedCorners) {
          corners = data.corners;
        }
        if (data.stable_states) currentSlotStates = data.stable_states;
        if (data.scores) currentScores = data.scores;
        if (data.yolo_boxes) currentYoloBoxes = data.yolo_boxes;

        document.getElementById("canvasYoloCount").textContent = `検出スリッパ: ${data.yolo_slipper_count || 0} 個`;

        // 妨害・人影ステータス
        const occStatus = document.getElementById("canvasOcclusionStatus");
        const occPanelBadge = document.getElementById("occlusionBadgeContainer");
        const occCard = document.getElementById("occlusionAlertPanel");
        if (data.is_occluded) {
          occStatus.className = "status-occluded";
          occStatus.textContent = `🚨 妨害・人影検知中！ (${data.occlusion_reason || "遮蔽ロック"})`;
          occPanelBadge.style.color = "#f87171";
          occPanelBadge.style.background = "#450a0a";
          occPanelBadge.textContent = `🚨 妨害検知中: ${data.occlusion_reason || "急変遮蔽"}`;
          occCard.style.borderColor = "#ef4444";
        } else {
          occStatus.className = "status-normal";
          occStatus.textContent = "ステータス: 正常判定中";
          occPanelBadge.style.color = "#34d399";
          occPanelBadge.style.background = "#0f172a";
          occPanelBadge.textContent = "✅ 妨害なし (正常稼働中)";
          occCard.style.borderColor = "#334155";
        }

        // カメラ画像の更新 (base64 がある場合)
        if (data.image_b64) {
          bgImage.onload = () => {
            isImageLoaded = true;
            if (bgImage.naturalWidth && bgImage.naturalHeight) {
              canvas.width = bgImage.naturalWidth;
              canvas.height = bgImage.naturalHeight;
              document.getElementById("canvasResText").textContent = `カメラ解像度: ${canvas.width} × ${canvas.height} (縦長90°)`;
            }
            renderCanvas();
          };
          bgImage.src = "data:image/jpeg;base64," + data.image_b64;
        } else {
          renderCanvas();
        }
      } catch (err) {
        console.error("fetchVisionStatus error:", err);
      }
    }

    let userEditedCorners = false;
    canvas.addEventListener("mouseup", () => { userEditedCorners = true; });

    // 1. 靴箱位置遠隔保存
    document.getElementById("btnSaveCorners").addEventListener("click", async () => {
      try {
        const res = await fetch("/api/admin/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ corners: corners })
        });
        const data = await res.json();
        alert(data.message || "靴箱位置を保存しました。お風呂場のラズパイへ数秒で自動適用されます！");
        userEditedCorners = false;
      } catch (e) {
        alert("設定保存エラー: " + e.message);
      }
    });

    // 2. YOLO設定遠隔保存
    document.getElementById("btnSaveYolo").addEventListener("click", async () => {
      try {
        const res = await fetch("/api/admin/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ yolo_conf: parseFloat(rngYoloConf.value) })
        });
        const data = await res.json();
        alert(data.message || "YOLO信頼度を保存しました。");
      } catch (e) {
        alert("エラー: " + e.message);
      }
    });

    // 3. パラメータ遠隔保存
    document.getElementById("btnSaveParams").addEventListener("click", async () => {
      try {
        const res = await fetch("/api/admin/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            threshold: parseFloat(rngThreshold.value),
            stable_duration_sec: parseFloat(rngStableSec.value)
          })
        });
        const data = await res.json();
        alert(data.message || "判定パラメータを保存しました。");
      } catch (e) {
        alert("エラー: " + e.message);
      }
    });

    document.getElementById("btnRefreshSnapshot").addEventListener("click", fetchVisionStatus);

    // グラフデータのロード
    async function loadChartData() {
      try {
        const res = await fetch("/api/admin/daily_data");
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById("statOnline").innerHTML = data.is_online ? '<span style="color:#10b981;">🟢 Live</span>' : '<span style="color:#ef4444;">🔴 待機中</span>';
        document.getElementById("statLastRecv").textContent = `最終受信: ${data.updated_at || "--:--:--"}`;
        document.getElementById("statCount").textContent = data.count;
        document.getElementById("statCrowd").textContent = `混雑度: ${data.crowd_level} (空き ${data.empty_slots} 枠)`;
        document.getElementById("statPeak").textContent = data.peak_utilization_rate.toFixed(1);
        document.getElementById("statAvg").textContent = data.avg_utilization_rate.toFixed(1);
        document.getElementById("statPeakTime").textContent = data.peak_time_label ? `ピーク帯: ${data.peak_time_label}` : "集計中";

        const labels = data.utilization_10min.map(item => item.label);
        const rates = data.utilization_10min.map(item => item.utilization_rate);

        const ctxChart = document.getElementById("utilizationChart").getContext("2d");
        if (chartInstance) {
          chartInstance.data.labels = labels;
          chartInstance.data.datasets[0].data = rates;
          chartInstance.update();
        } else {
          chartInstance = new Chart(ctxChart, {
            type: "line",
            data: {
              labels: labels,
              datasets: [{
                label: "利用率 (%)",
                data: rates,
                borderColor: "#8b5cf6",
                backgroundColor: "rgba(139, 92, 246, 0.18)",
                fill: true,
                tension: 0.3,
                borderWidth: 2.5,
                pointRadius: 3.5,
                pointBackgroundColor: "#8b5cf6"
              }]
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: { legend: { labels: { color: "#cbd5e1" } } },
              scales: {
                y: { beginAtZero: true, max: 100, grid: { color: "#334155" }, ticks: { color: "#94a3b8" } },
                x: { grid: { display: false }, ticks: { color: "#94a3b8", maxRotation: 45, minRotation: 45, font: { size: 10 } } }
              }
            }
          });
        }
      } catch (err) {
        console.error("loadChartData error:", err);
      }
    }

    document.getElementById("btnRefreshChart").addEventListener("click", loadChartData);

    // 初期起動 & 定期ポーリング (3秒おきにお風呂場の最新状況を自動取得)
    document.addEventListener("DOMContentLoaded", () => {
      fetchVisionStatus();
      setInterval(fetchVisionStatus, 3000);
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
    """自室PC管理画面用 リアルタイム遠隔監視API"""
    now = time.time()
    last_recv = latest_status["last_received_timestamp"]
    is_online = (last_recv > 0) and (now - last_recv <= OFFLINE_THRESHOLD_SEC)

    res = {
        "is_online": is_online,
        "count": latest_status["count"],
        "max_slots": latest_status["max_slots"],
        "empty_slots": latest_status["empty_slots"],
        "crowd_level": latest_status["crowd_level"],
        "updated_at": latest_status["updated_at"],
        "corners": latest_vision["corners"],
        "stable_states": latest_vision["stable_states"],
        "scores": latest_vision["scores"],
        "yolo_boxes": latest_vision["yolo_boxes"],
        "yolo_slipper_count": len([b for b in latest_vision["yolo_boxes"] if b.get("label") != "person"]),
        "is_occluded": latest_vision["is_occluded"],
        "occlusion_reason": latest_vision["occlusion_reason"],
        "image_b64": latest_vision["image_b64"]
    }
    return jsonify(res)


@app.route("/api/admin/config", methods=["POST"])
@admin_login_required
def api_admin_config():
    """自室PC管理画面からの遠隔設定保存 (ラズパイ実機へ次回通信で自動適用)"""
    global pending_remote_config
    data = request.get_json(silent=True) or {}

    if "corners" in data:
        latest_vision["corners"] = data["corners"]
        pending_remote_config["corners"] = data["corners"]

    if "yolo_conf" in data:
        latest_vision["yolo_conf"] = float(data["yolo_conf"])
        pending_remote_config["yolo_conf"] = float(data["yolo_conf"])

    if "threshold" in data:
        latest_vision["threshold"] = float(data["threshold"])
        pending_remote_config["threshold"] = float(data["threshold"])

    if "stable_duration_sec" in data:
        latest_vision["stable_duration_sec"] = float(data["stable_duration_sec"])
        pending_remote_config["stable_duration_sec"] = float(data["stable_duration_sec"])

    return jsonify({
        "status": "success",
        "message": "遠隔設定を保存しました。お風呂場のラズパイへ数秒で自動適用されます。"
    })


@app.route("/api/admin/daily_data")
@admin_login_required
def api_admin_daily_data():
    """管理画面用 サマリーおよび10分おき利用率JSON"""
    global cloud_utilization_10min
    recalculate_cloud_utilization()

    rates = [item["utilization_rate"] for item in cloud_utilization_10min]
    peak_rate = max(rates) if rates else 0.0
    avg_rate = round(sum(rates) / len(rates), 1) if rates else 0.0

    peak_time_label = ""
    for item in cloud_utilization_10min:
        if item["utilization_rate"] == peak_rate and peak_rate > 0:
            peak_time_label = item["label"]
            break

    now = time.time()
    last_recv = latest_status["last_received_timestamp"]
    is_online = (last_recv > 0) and (now - last_recv <= OFFLINE_THRESHOLD_SEC)

    return jsonify({
        "is_online": is_online,
        "count": latest_status["count"],
        "max_slots": latest_status["max_slots"],
        "empty_slots": latest_status["empty_slots"],
        "crowd_level": latest_status["crowd_level"],
        "updated_at": latest_status["updated_at"],
        "peak_utilization_rate": peak_rate,
        "avg_utilization_rate": avg_rate,
        "peak_time_label": peak_time_label,
        "utilization_10min": cloud_utilization_10min
    })


@app.route("/api/admin/download")
@admin_login_required
def api_admin_download():
    """管理画面からのCSVダウンロード"""
    dtype = request.args.get("type", "utilization_10min")
    today_str = get_today_str()

    if dtype == "utilization_10min":
        recalculate_cloud_utilization()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["時間帯", "平均人数(人)", "利用率(%)", "ピーク人数(人)"])
        for row in cloud_utilization_10min:
            writer.writerow([row["label"], row["avg_users"], row["utilization_rate"], row["peak_users"]])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={today_str}_utilization_10min.csv"}
        )

    elif dtype == "usage_log":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "date", "time", "stable_count", "max_slots", "utilization_rate(%)"])
        for item in cloud_daily_logs:
            writer.writerow([
                int(item.get("timestamp", 0)),
                item.get("date", today_str),
                item.get("time", ""),
                item.get("count", 0),
                item.get("max_slots", 36),
                item.get("rate", 0.0)
            ])
        csv_bytes = output.getvalue().encode("utf-8-sig")
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={today_str}_usage_log.csv"}
        )

    return jsonify({"status": "error", "message": "Unknown type"}), 400


@app.route("/api/update", methods=["POST"])
def api_update():
    """ラズベリーパイから人数・カメラ・グリッド判定データを受け取るエンドポイント"""
    global current_log_date, cloud_daily_logs, pending_remote_config

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
        latest_vision["corners"] = payload["corners"]
    if "is_occluded" in payload:
        latest_vision["is_occluded"] = payload["is_occluded"]
        latest_vision["occlusion_reason"] = payload.get("occlusion_reason", "")
    if payload.get("image_b64"):
        latest_vision["image_b64"] = payload["image_b64"]

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
