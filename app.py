"""
Dorm Bath Monitor - Cloud Distribution & Dedicated Logs Dashboard Server
寮風呂場利用状況配信システム クラウド配信用Webサーバー (Render / VPS / Cloud用)
自室PCからは「入退室ログ & 利用時間統計ダッシュボード」に専念。
カメラ画像などの重いデータは一切送受信せず、超軽量テキストデータのみで通信量を最小化（1日2〜3MB以下）。
"""
import os
import io
import csv
import time
import datetime
from collections import OrderedDict
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

# 日別統計サマリーキャッシュ (ラズパイから受信)
latest_daily_summary = {}
latest_available_dates = [datetime.datetime.now().strftime("%Y-%m-%d")]

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
# 0. 稼働時間内アクセス数集計（1時間区切り: 00:00〜24:00）
# -------------------------------------------------------------
HOURLY_BINS = [f"{h:02d}:00-{(h+1):02d}:00" for h in range(23)] + ["23:00-24:00"]
ACCESS_LOGS_DIR = "access_logs"
access_hourly_cache = {}

def is_bath_active_now():
    """現在の時刻が稼働時間内かどうかを判定（ラズパイ通知優先、通信待機時は16:30〜22:00）"""
    if latest_status.get("is_online", False):
        return bool(latest_status.get("is_active", False))
    now_t = datetime.datetime.now().time()
    return datetime.time(16, 30) <= now_t <= datetime.time(22, 0)

def load_hourly_access(date_str):
    """指定日の1時間区切りアクセス数を取得（キャッシュまたはCSVから読込）"""
    if date_str in access_hourly_cache:
        return access_hourly_cache[date_str]

    data = OrderedDict((bin_lbl, 0) for bin_lbl in HOURLY_BINS)
    file_path = os.path.join(ACCESS_LOGS_DIR, f"{date_str}_hourly_access.csv")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                next(reader, None)  # ヘッダー読飛
                for row in reader:
                    if len(row) >= 2 and row[0] in data:
                        try:
                            data[row[0]] = int(row[1])
                        except ValueError:
                            pass
        except Exception as e:
            app.logger.error(f"Error reading hourly access: {e}")

    access_hourly_cache[date_str] = data
    return data

def save_hourly_access(date_str):
    """指定日の1時間区切りアクセス数をCSVに保存"""
    try:
        os.makedirs(ACCESS_LOGS_DIR, exist_ok=True)
        file_path = os.path.join(ACCESS_LOGS_DIR, f"{date_str}_hourly_access.csv")
        data = access_hourly_cache.get(date_str, {})
        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["時間帯(1時間間隔)", "アクセス数"])
            for bin_lbl in HOURLY_BINS:
                writer.writerow([bin_lbl, data.get(bin_lbl, 0)])
    except Exception as e:
        app.logger.error(f"Error saving hourly access: {e}")

def record_access_if_active():
    """稼働時間内の場合のみアクセス数をカウント（1時間区切り）"""
    if not is_bath_active_now():
        return
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    h = now_dt.hour
    bin_lbl = f"{h:02d}:00-{(h+1):02d}:00" if h < 23 else "23:00-24:00"
    data = load_hourly_access(today_str)
    data[bin_lbl] = data.get(bin_lbl, 0) + 1
    save_hourly_access(today_str)


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
# 2. ログイン画面 HTML (インライン・パスワード露出なし)
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
    <p>管理パスワードを入力してください</p>
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
# 3. 入退室ログ & 統計ダッシュボード HTML (インライン・専用画面)
# -------------------------------------------------------------
INLINE_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>入退室ログ & 利用時間統計ダッシュボード - 寮風呂場モニター</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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


    /* ダッシュボード専用調整 */
    .logs-header-bar {
      margin-top: 10px;
      margin-bottom: 20px;
    }
    .admin-wrapper {
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px 20px 40px;
    }
  </style>
</head>
<body>
  <div class="admin-wrapper">
    <header class="admin-header">
      <div class="header-left">
        <h1>📊 寮風呂場 入退室ログ & 利用時間統計ダッシュボード</h1>
        <span class="badge" id="onlineBadge" style="background: #065f46; color: #6ee7b7;">🟢 ラズパイ通信確認中...</span>
      </div>
      <div class="header-right" style="display: flex; gap: 10px; align-items: center;">
        <a href="/" class="btn-outline">← 混雑状況モニターに戻る</a>
        <a href="/logout" class="btn-secondary" style="text-decoration: none; padding: 7px 14px; font-size: 0.84rem; background: #334155; color: #fff; border-radius: 8px;">🚪 ログアウト</a>
      </div>
    </header>

    <!-- 1. 上部ツールバー: 日付選択 & CSVダウンロード -->
    <div class="logs-header-bar">
      <div class="logs-header-left">
        <label for="logDateSelect" style="font-weight: 600; font-size: 0.95rem;">📅 表示日付:</label>
        <select id="logDateSelect" class="form-select" style="min-width: 160px; font-weight: 600; padding: 7px 12px; background: #1e293b; color: #f8fafc; border: 1px solid #334155; border-radius: 8px;"></select>
        <button id="refreshLogsBtn" class="btn-secondary" style="padding: 7px 14px; background: #1e293b; border: 1px solid #334155; color: #f8fafc; border-radius: 8px; cursor: pointer;">🔄 最新に更新</button>
      </div>
      <div class="logs-header-right" style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
        <span style="font-size: 0.85rem; color: #94a3b8;">分析用CSVダウンロード:</span>
        <a id="btnDownloadSessions" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 入退室ログCSV</a>
        <a id="btnDownloadUtilization" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px; border-color: #8b5cf6; color: #c084fc;">📥 10分おき利用率CSV</a>
        <a id="btnDownloadEntryHist" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 30分間隔CSV</a>
        <a id="btnDownloadDurationHist" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px;">📥 5分間隔CSV</a>
        <a id="btnDownloadAccess" href="#" class="btn-outline" style="font-size: 0.82rem; padding: 6px 12px; border-color: #0ea5e9; color: #38bdf8;">📥 アクセス数(1時間区切り)CSV</a>
      </div>
    </div>

    <!-- 2. サマリー統計カード (6枚) -->
    <div class="stats-summary-grid">
      <div class="stat-card" style="border-left: 4px solid #0ea5e9;">
        <div class="stat-icon">📱</div>
        <div class="stat-content">
          <span class="stat-label">配信用サイト アクセス数</span>
          <span class="stat-value" id="statTotalAccess">0 <small>回 (稼働中)</small></span>
        </div>
      </div>
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

    <!-- 3. 配信用サイト 時間帯別アクセス数グラフ (1時間区切り: 稼働時間内) -->
    <div class="chart-card" style="margin-bottom: 20px;">
      <div class="chart-card-header">
        <h3>📶 配信用サイト 時間帯別アクセス数 (1時間区切り: 稼働時間帯)</h3>
        <span class="chart-subtitle">寮生による混雑状況モニター（配信用サイト）閲覧アクセス回数の推移</span>
      </div>
      <div class="chart-body" style="height: 250px;">
        <canvas id="hourlyAccessChart"></canvas>
      </div>
    </div>

    <!-- 4. 10分おき風呂場利用率［％］推移グラフ (メイン) -->
    <div class="chart-card" style="margin-bottom: 20px;">
      <div class="chart-card-header">
        <h3>📊 10分おき風呂場利用率［％］推移 (16:30〜22:00)</h3>
        <span class="chart-subtitle">時間帯ごとの収容枠（36枠）に対する利用率推移</span>
      </div>
      <div class="chart-body" style="height: 280px;">
        <canvas id="utilizationRateChart"></canvas>
      </div>
    </div>

    <!-- 5. ヒストグラムグラフ (2つ横並び) -->
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

    <!-- 5. 入退室セッションログ テーブル -->
    <div class="log-table-card" style="margin-top: 24px;">
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

  <script>
    document.addEventListener("DOMContentLoaded", () => {
      const logDateSelect = document.getElementById("logDateSelect");
      const refreshLogsBtn = document.getElementById("refreshLogsBtn");
      const btnDownloadSessions = document.getElementById("btnDownloadSessions");
      const btnDownloadUtilization = document.getElementById("btnDownloadUtilization");
      const btnDownloadEntryHist = document.getElementById("btnDownloadEntryHist");
      const btnDownloadDurationHist = document.getElementById("btnDownloadDurationHist");
      const btnDownloadAccess = document.getElementById("btnDownloadAccess");

      const statTotalAccess = document.getElementById("statTotalAccess");
      const statTotalUsers = document.getElementById("statTotalUsers");
      const statAvgDuration = document.getElementById("statAvgDuration");
      const statMaxDuration = document.getElementById("statMaxDuration");
      const statPeakRate = document.getElementById("statPeakRate");
      const statAvgRate = document.getElementById("statAvgRate");
      const sessionTableBody = document.getElementById("sessionTableBody");
      const logCountLabel = document.getElementById("logCountLabel");
      const onlineBadge = document.getElementById("onlineBadge");

      let entryChart = null;
      let durationChart = null;
      let utilizationChart = null;
      let accessChart = null;

      async function checkOnlineStatus() {
        try {
          const res = await fetch("/api/status");
          const data = await res.json();
          if (data.is_online) {
            onlineBadge.style.background = "#065f46";
            onlineBadge.style.color = "#6ee7b7";
            onlineBadge.textContent = "🟢 ラズパイ常時中継中";
          } else {
            onlineBadge.style.background = "#450a0a";
            onlineBadge.style.color = "#fca5a5";
            onlineBadge.textContent = "🔴 ラズパイ通信待機中";
          }
        } catch (e) {}
      }

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
          btnDownloadUtilization.href = `/api/logs/download?date=${dateStr}&type=utilization_10min`;
          btnDownloadEntryHist.href = `/api/logs/download?date=${dateStr}&type=entry_hist`;
          btnDownloadDurationHist.href = `/api/logs/download?date=${dateStr}&type=duration_hist`;
          if (btnDownloadAccess) btnDownloadAccess.href = `/api/logs/download?date=${dateStr}&type=hourly_access`;

          const res = await fetch(`/api/logs/daily?date=${dateStr}`);
          const data = await res.json();

          if (statTotalAccess) {
            statTotalAccess.innerHTML = `${data.total_access_count !== undefined ? data.total_access_count : 0} <small>回 (稼働中)</small>`;
          }
          statTotalUsers.innerHTML = `${data.total_users} <small>人</small>`;
          statAvgDuration.innerHTML = `${data.avg_duration_min} <small>分</small>`;
          statMaxDuration.innerHTML = `${data.max_duration_min} <small>分</small>`;
          statPeakRate.innerHTML = `${data.peak_utilization_rate !== undefined ? data.peak_utilization_rate : 0.0} <small>%</small>`;
          statAvgRate.innerHTML = `${data.avg_utilization_rate !== undefined ? data.avg_utilization_rate : 0.0} <small>%</small>`;
          logCountLabel.textContent = `${data.total_users} 件の記録`;

          renderSessionTable(data.sessions);
          renderHistograms(data.entry_histogram, data.duration_histogram, data.utilization_10min);
          renderAccessChart(data.hourly_access);
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
            <td>${s["入室時間"] || s["entry_time"] || "-"}</td>
            <td>${s["退室時間"] || s["exit_time"] || "-"}</td>
            <td><span class="duration-tag">${s["利用時間(分)"] || s["duration_min"] || 0} 分</span></td>
          `;
          sessionTableBody.appendChild(tr);
        });
      }

      function renderHistograms(entryHist, durationHist, utilHist) {
        if (typeof Chart === "undefined") return;

        // 1. 10分おき利用率グラフ
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

        // 2. 入室時間帯ヒストグラム (30分間隔)
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
            plugins: { legend: { display: false } },
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

        // 3. 利用時間ヒストグラム (5分間隔)
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
            plugins: { legend: { display: false } },
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

      function renderAccessChart(accessList) {
        if (typeof Chart === "undefined") return;
        const canvasAccess = document.getElementById("hourlyAccessChart");
        if (!canvasAccess) return;

        const ctxAccess = canvasAccess.getContext("2d");
        const accessLabels = accessList && accessList.length > 0 ? accessList.map(h => h.label) : [];
        const accessCounts = accessList && accessList.length > 0 ? accessList.map(h => h.count) : [];

        if (accessChart) accessChart.destroy();
        accessChart = new Chart(ctxAccess, {
          type: "bar",
          data: {
            labels: accessLabels,
            datasets: [{
              label: "アクセス数 (回)",
              data: accessCounts,
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
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: function(context) {
                    return `アクセス数: ${context.parsed.y} 回`;
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
          checkOnlineStatus();
          loadDailyLogData(logDateSelect.value);
        });
      }

      // 初回ロード & 15秒ごとの定期ステータス更新
      loadLogDates();
      checkOnlineStatus();
      setInterval(checkOnlineStatus, 15000);
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
    record_access_if_active()
    template_path = os.path.join(app.root_path, "templates", "index.html")
    if os.path.exists(template_path):
        return render_template("index.html")
    return render_template_string(INLINE_HTML)


@app.route("/admin")
@admin_login_required
def admin_dashboard():
    """クラウド 入退室ログ & 利用時間統計ダッシュボード"""
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

    hourly_data = load_hourly_access(date_str)
    hourly_access_list = [{"label": k, "count": v} for k, v in hourly_data.items()]
    total_access = sum(hourly_data.values())

    if date_str == today_str and latest_daily_summary:
        res = dict(latest_daily_summary)
        res["hourly_access"] = hourly_access_list
        res["total_access_count"] = total_access
        return jsonify(res)

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
        "utilization_10min": cloud_utilization_10min,
        "hourly_access": hourly_access_list,
        "total_access_count": total_access
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
        # ラズパイから届いたutilization_10minがあればそちらを優先
        rows_to_write = latest_daily_summary.get("utilization_10min") or cloud_utilization_10min
        for row in rows_to_write:
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

    elif log_type in ("hourly_access", "access_hourly"):
        writer.writerow(["時間帯(1時間間隔)", "アクセス数"])
        hourly_data = load_hourly_access(date_str)
        for bin_lbl in HOURLY_BINS:
            writer.writerow([bin_lbl, hourly_data.get(bin_lbl, 0)])
        filename = f"{date_str}_hourly_access.csv"

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
    """ラズベリーパイから超軽量テキストデータのみを受信するエンドポイント"""
    global current_log_date, cloud_daily_logs, latest_daily_summary, latest_available_dates

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

    return jsonify({
        "status": "ok",
        "message": "Updated successfully",
        "server_time": now_str
    })


@app.route("/health")
def health():
    """Render等の死活監視用エンドポイント"""
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
