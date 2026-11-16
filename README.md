# 寮風呂場利用状況 クラウド配信用サーバー (Cloud Distribution Server)

このフォルダは、**IPアドレスに左右されない固定URL（例: `https://dorm-bath.onrender.com`）** で混雑状況を全世界へ一般配信するための超軽量Webサーバーです。

完全無料のクラウドサービス **「Render.com」**（クレジットカード不要）を利用して、誰でも10分で固定URLのホームページを立ち上げることができます。

---

## 🏗️ 仕組み
1. **クラウドサーバー（本プログラム）**:
   - インターネット上で24時間稼働し、一般利用者（スマホ）向けに混雑状況画面を配信します。
2. **ラズベリーパイ（エッジAI）**:
   - 風呂場でスリッパを認識し、数秒おきに「現在3人」という数値データのみをこのクラウドサーバーへ自動送信（HTTPS POST）します。

---

## 🚀 デプロイ手順（Render.com で完全無料で公開する手順）

### 【準備】GitHubに本フォルダをアップロードする
1. GitHub（[https://github.com](https://github.com)）にログイン（または無料登録）します。
2. 新しいリポジトリ（例: `dorm-bath-monitor`）を作成します（Public / Private どちらでも可）。
3. この `cloud_server` フォルダ内のファイル一式をリポジトリにアップロード（push）します。

---

### 【手順】Render.com でWebサービスを作成する
1. **Render.com にアクセス**:
   - [https://render.com](https://render.com) を開き、**「Sign Up」** から GitHub アカウントで無料登録・ログインします。
   - ※クレジットカードの登録は一切不要です。
2. **新規 Web Service を作成**:
   - ダッシュボード右上の **「New +」** → **「Web Service」** をクリックします。
   - **「Build and deploy from a Git repository」** を選択し、先ほど作った GitHub リポジトリ（`dorm-bath-monitor`）を連携・選択します。
3. **基本設定を入力**:
   - **Name**: お好きなURL名を入力（例: `dorm-bath-monitor`）
     → これがそのままあなたの固定URL（`https://dorm-bath-monitor.onrender.com`）になります！
   - **Region**: `Singapore` または `Oregon`（日本から近いシンガポールがおすすめ）
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: **「Free」（無料）** を選択します。
4. **環境変数の設定（セキュリティ）**:
   - 画面下の **「Environment Variables」** を開き、以下を追加します：
     - **Key**: `API_KEY`
     - **Value**: `bath_monitor_secret_key_2026`（※お好きな英数字でOK）
5. **デプロイ開始**:
   - 画面一番下の **「Deploy Web Service」** をクリックします。
   - 2〜3分で自動的にビルドが完了し、画面上部に **あなたの固定URL（`https://xxxx.onrender.com`）** が表示されます！

---

## 📲 ラズベリーパイ（管理画面）での設定

クラウドサーバーが立ち上がったら、ラズパイの管理画面からURLを登録するだけです：

1. パソコンからラズパイの管理画面（`http://<ラズパイのIP>:5000/admin`）を開きます。
2. 右サイドバーの **「7. ☁️ クラウドWeb配信連携設定」** を開きます。
3. 以下を入力します：
   - **「クラウド自動配信を有効化」**: チェックを入れる（ON）
   - **クラウドサーバーURL**: 発行されたRenderのURL（例: `https://dorm-bath-monitor.onrender.com`）
   - **APIキー**: Renderで設定したキー（例: `bath_monitor_secret_key_2026`）
4. **「クラウド設定を保存」** をクリックし、**「疎通テスト送信」** を押して「接続成功！」と出れば設定完了です！

これで、ラズパイがスリッパの人数を更新するたびにクラウドへ自動送信され、世界中どこからでも固定URLで利用状況がリアルタイムに確認できるようになります！
