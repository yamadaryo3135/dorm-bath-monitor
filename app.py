"""
Dorm Bath Monitor - Cloud Distribution Server
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
templates / static フォルダが存在しない場合でも、単体1ファイルで完全動作する自己完結型設計。
一般公開モニター(/)に加えて、固定URLからいつでもログや利用率を確認できるクラウド管理画面(/admin)を搭載。
"""
import os
import io
import csv
import time
import datetime
from functools import wraps
from flask import Flask, render_template, render_template_string, jsonify, request, session, redirect, url_for, Response

app = Flask(__name__)

# セキュリティ設定
API_KEY = os.environ.get("API_KEY", "bath_monitor_secret_key_2026")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
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

    # 33区間の初期化
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
      display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;
      background: #ffffff; border: 1px solid #e2e8f0; border-radius: 20px;
      font-size: 0.78rem; font-weight: 600; color: #475569; box-shadow: var(--shadow-sm);
    }
    .btn-admin-link {
      display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px;
      background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 20px;
      font-size: 0.78rem; font-weight: 600; color: #475569; text-decoration: none;
      box-shadow: var(--shadow-sm); transition: all 0.2s;
    }
    .btn-admin-link:hover { background: #e2e8f0; color: #0f172a; }
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
      <div class="header-actions">
        <div class="cloud-badge" id="onlineBadge" title="ラズパイとの接続状態">
          <span class="status-indicator-dot dot-offline" id="onlineDot"></span>
          <span id="onlineText">確認中</span>
        </div>
        <a href="/admin" class="btn-admin-link">⚙ 管理画面</a>
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
# 2. クラウド管理画面 HTML (インライン・Chart.js連携・ログイン対応)
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
      background: #f1f5f9; display: flex; justify-content: center; align-items: center;
      min-height: 100vh; margin: 0; padding: 16px;
    }
    .login-card {
      background: #ffffff; padding: 32px 28px; border-radius: 16px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08); width: 100%; max-width: 360px;
      text-align: center;
    }
    .icon { font-size: 40px; margin-bottom: 12px; }
    h2 { font-size: 1.3rem; margin-bottom: 8px; color: #1e293b; }
    p { font-size: 0.85rem; color: #64748b; margin-bottom: 24px; }
    .form-group { margin-bottom: 20px; text-align: left; }
    label { display: block; font-size: 0.8rem; font-weight: 600; color: #475569; margin-bottom: 6px; }
    input[type="password"] {
      width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px;
      font-size: 1rem; box-sizing: border-box; outline: none; transition: border 0.2s;
    }
    input[type="password"]:focus { border-color: #0284c7; box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.15); }
    .btn-submit {
      width: 100%; padding: 12px; background: #0284c7; color: white; border: none;
      border-radius: 8px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
      transition: background 0.2s;
    }
    .btn-submit:hover { background: #0369a1; }
    .error-msg { background: #fee2e2; color: #991b1b; padding: 10px; border-radius: 6px; font-size: 0.82rem; margin-bottom: 16px; }
    .back-link { display: inline-block; margin-top: 16px; font-size: 0.82rem; color: #0284c7; text-decoration: none; }
  </style>
</head>
<body>
  <div class="login-card">
    <div class="icon">🔒</div>
    <h2>クラウド管理画面 ログイン</h2>
    <p>管理パスワードを入力してください (初期値: admin)</p>
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

INLINE_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>クラウド管理画面 - 寮風呂場混雑モニター</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --primary: #0284c7;
      --primary-hover: #0369a1;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); padding: 20px 16px; min-height: 100vh;
    }
    .wrapper { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
    .top-header {
      display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;
      background: var(--card-bg); padding: 16px 20px; border-radius: 12px; border: 1px solid var(--border);
    }
    .top-title { display: flex; align-items: center; gap: 10px; }
    .top-title .icon { font-size: 26px; }
    .top-title h1 { font-size: 1.25rem; font-weight: 700; }
    .header-btns { display: flex; align-items: center; gap: 10px; }
    .btn {
      padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; font-weight: 600;
      text-decoration: none; cursor: pointer; transition: all 0.2s; border: none;
    }
    .btn-secondary { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }
    .btn-secondary:hover { background: #e2e8f0; color: #0f172a; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-danger { background: #fee2e2; color: #991b1b; }
    .btn-danger:hover { background: #fecaca; }

    /* サマリーカードグリッド */
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
    .stat-card {
      background: var(--card-bg); padding: 18px 20px; border-radius: 12px;
      border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .stat-label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }
    .stat-val { font-size: 2rem; font-weight: 800; color: #1e293b; display: flex; align-items: baseline; gap: 4px; }
    .stat-unit { font-size: 0.9rem; font-weight: 600; color: var(--text-muted); }
    .stat-sub { font-size: 0.78rem; color: var(--text-muted); margin-top: 6px; }

    /* メイングラフカード */
    .content-card {
      background: var(--card-bg); border-radius: 12px; border: 1px solid var(--border);
      padding: 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
    .card-head h2 { font-size: 1.05rem; font-weight: 700; color: #1e293b; display: flex; align-items: center; gap: 8px; }
    .chart-box { width: 100%; height: 320px; position: relative; }

    /* ダウンロードエリア */
    .download-section {
      display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; padding-top: 16px;
      border-top: 1px dashed var(--border);
    }
    .btn-download {
      display: inline-flex; align-items: center; gap: 6px; padding: 10px 16px;
      background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;
      font-size: 0.88rem; font-weight: 600; color: #1e293b; text-decoration: none;
      transition: all 0.2s;
    }
    .btn-download:hover { background: #e0f2fe; border-color: #38bdf8; color: #0284c7; }
    .btn-download-primary {
      background: #f5f3ff; border: 1px solid #c4b5fd; color: #6d28d9;
    }
    .btn-download-primary:hover { background: #ede9fe; border-color: #8b5cf6; }

    .help-banner {
      background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px;
      padding: 14px 18px; font-size: 0.85rem; color: #1e40af; line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="wrapper">
    <!-- ヘッダー -->
    <header class="top-header">
      <div class="top-title">
        <span class="icon">⚙️</span>
        <h1>寮風呂場 クラウド管理ダッシュボード</h1>
      </div>
      <div class="header-btns">
        <a href="/" target="_blank" class="btn btn-secondary">👁 一般公開モニターを開く</a>
        <a href="/admin/logout" class="btn btn-danger">ログアウト</a>
      </div>
    </header>

    <!-- 注意喚起・ステータスインフォ -->
    <div class="help-banner">
      💡 <strong>固定URL管理システム：</strong> 学校や寮のWi-Fi制限に関係なく、このページからいつでも本日の人数推移や10分おき利用率グラフの閲覧、CSVログのダウンロードが可能です。
    </div>

    <!-- 4枚サマリーカード -->
    <div class="summary-grid">
      <div class="stat-card">
        <div class="stat-label">📡 ラズパイ通信状況</div>
        <div class="stat-val" id="statOnline">--</div>
        <div class="stat-sub" id="statLastRecv">最終データ受信: --:--:--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">👥 現在の入浴人数</div>
        <div class="stat-val"><span id="statCount">--</span> <span class="stat-unit">/ <span id="statMax">36</span> 人</span></div>
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

    <!-- 10分おき利用率グラフカード -->
    <div class="content-card">
      <div class="card-head">
        <h2>📊 風呂場利用率推移 (10分おき: 16:30〜22:00)</h2>
        <div>
          <button id="btnRefreshChart" class="btn btn-secondary">🔄 最新データに更新</button>
        </div>
      </div>

      <div class="chart-box">
        <canvas id="utilizationChart"></canvas>
      </div>

      <!-- CSVダウンロードエリア -->
      <div class="download-section">
        <a href="/api/admin/download?type=utilization_10min" class="btn-download btn-download-primary" download>
          📥 10分おき利用率CSVをダウンロード (utilization_10min.csv)
        </a>
        <a href="/api/admin/download?type=usage_log" class="btn-download" download>
          📥 本日の人数ログCSVをダウンロード (usage_log.csv)
        </a>
      </div>
    </div>
  </div>

  <script>
    let chartInstance = null;

    async function loadData() {
      try {
        const res = await fetch("/api/admin/daily_data");
        if (!res.ok) throw new Error("API error");
        const data = await res.json();

        // 1. サマリーカードの反映
        const onlineEl = document.getElementById("statOnline");
        if (data.is_online) {
          onlineEl.innerHTML = '<span style="color: #10b981;">🟢 Live</span>';
        } else {
          onlineEl.innerHTML = '<span style="color: #ef4444;">🔴 待機中</span>';
        }
        document.getElementById("statLastRecv").textContent = `最終受信: ${data.updated_at || "--:--:--"}`;
        document.getElementById("statCount").textContent = data.count;
        document.getElementById("statMax").textContent = data.max_slots;
        document.getElementById("statCrowd").textContent = `混雑度: ${data.crowd_level} (空き ${data.empty_slots} 枠)`;

        document.getElementById("statPeak").textContent = data.peak_utilization_rate.toFixed(1);
        document.getElementById("statAvg").textContent = data.avg_utilization_rate.toFixed(1);
        document.getElementById("statPeakTime").textContent = data.peak_time_label ? `ピーク帯: ${data.peak_time_label}` : "集計中";

        // 2. Chart.js 折れ線グラフ描画
        const labels = data.utilization_10min.map(item => item.label);
        const rates = data.utilization_10min.map(item => item.utilization_rate);

        const ctx = document.getElementById("utilizationChart").getContext("2d");
        if (chartInstance) {
          chartInstance.data.labels = labels;
          chartInstance.data.datasets[0].data = rates;
          chartInstance.update();
        } else {
          chartInstance = new Chart(ctx, {
            type: "line",
            data: {
              labels: labels,
              datasets: [{
                label: "利用率 (%)",
                data: rates,
                borderColor: "#8b5cf6",
                backgroundColor: "rgba(139, 92, 246, 0.15)",
                fill: true,
                tension: 0.3,
                borderWidth: 2.5,
                pointRadius: 3.5,
                pointHoverRadius: 6,
                pointBackgroundColor: "#8b5cf6"
              }]
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { position: "top", labels: { font: { weight: "600" } } },
                tooltip: {
                  callbacks: {
                    label: function(context) {
                      return `利用率: ${context.parsed.y}%`;
                    }
                  }
                }
              },
              scales: {
                y: {
                  beginAtZero: true,
                  max: 100,
                  title: { display: true, text: "利用率 (%)" },
                  grid: { color: "#e2e8f0" }
                },
                x: {
                  title: { display: true, text: "時間帯 (10分おき)" },
                  ticks: { maxRotation: 45, minRotation: 45, font: { size: 10 } },
                  grid: { display: false }
                }
              }
            }
          });
        }
      } catch (e) {
        console.error("Failed to load admin data:", e);
      }
    }

    document.addEventListener("DOMContentLoaded", () => {
      loadData();
      setInterval(loadData, 5000);
      document.getElementById("btnRefreshChart").addEventListener("click", loadData);
    });
  </script>
</body>
</html>
"""


# -------------------------------------------------------------
# 3. 認証デコレータとルーティング
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
    """クラウド管理画面ダッシュボード"""
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
    """ラズベリーパイから人数データを受け取るエンドポイント"""
    global current_log_date, cloud_daily_logs

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

    # ログ蓄積 (直近データと変化があった場合、または1分以上経過時に追加)
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
        # メモリ肥大化防止 (最大5000件)
        if len(cloud_daily_logs) > 5000:
            cloud_daily_logs.pop(0)

    return jsonify({"status": "ok", "message": "Updated successfully", "server_time": now_str})


@app.route("/health")
def health():
    """Render等の死活監視用エンドポイント"""
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
