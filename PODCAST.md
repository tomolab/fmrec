# Podcast(スマホ再生)セットアップ

録音した m4a を、スマホのポッドキャストアプリで購読・再生できるようにします。

## 全体像

```
録音(m4a) → make_podcast.py がRSS生成(feed.xml)
          → HTTPサーバーが録音とfeed.xmlを配信
          → スマホのPodcastアプリでfeed.xmlを購読
```

## 1. 設定(fmrec.conf)

```
PODCAST_BASE_URL="http://<配信アドレス>:8080"
PODCAST_TITLE="わが家のラジオ録音"
PODCAST_SPLIT_BY_STATION="yes"   # 局ごとの個別フィードも作る
```

`<配信アドレス>` はスマホからアクセスできるアドレスにします。
外出先からも聴くなら次章のTailscaleを使うのがおすすめです。

## 2. 配信サーバーとフィード更新

```bash
sudo cp systemd/fmrec-podcast.service /etc/systemd/system/
sudo cp systemd/fmrec-podcast-feed.service /etc/systemd/system/
sudo cp systemd/fmrec-podcast-feed.timer /etc/systemd/system/
# service内の User= と WorkingDirectory=(OUTPUT_DIRと同じ)を自分の環境に修正
sudo systemctl daemon-reload
sudo systemctl enable --now fmrec-podcast.service        # 配信サーバー
sudo systemctl enable --now fmrec-podcast-feed.timer     # 15分ごとにRSS更新
```

手動でRSSを作るには:

```bash
./make_podcast.py
```

## 3. 外出先からも聴く(Tailscale・推奨)

放送音源を世界に公開しないため、ポートを直接インターネットに
開けるのは避け、VPNのTailscaleを挟みます。無料で設定も簡単です。

```bash
# ラズパイ側
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# 表示されるTailscale上の名前(例: raspberrypi.tail1234.ts.net)を
# PODCAST_BASE_URL に設定する:
#   PODCAST_BASE_URL="http://raspberrypi.tail1234.ts.net:8080"
```

スマホにもTailscaleアプリを入れて同じアカウントでログインすれば、
外出先でも自宅のラズパイにVPN経由で安全にアクセスできます。

## 4. スマホで購読

Podcastアプリで「URLで購読 / RSSを追加」を選び、購読URLを入力:

```
http://<配信アドレス>:8080/feed.xml            # 全番組
http://<配信アドレス>:8080/feed_TOKYO-FM.xml   # 局別(SPLIT=yes時)
```

対応アプリ例:
- **AntennaPod** (Android・オープンソース・自宅サーバー向き)
- **Pocket Casts** (iOS/Android・URL購読対応)
- Apple Podcast は独自URL購読がやや手間なので上記が楽です

以降、新しい録音は15分以内にフィードへ反映され、アプリの更新で
自動的にスマホへダウンロードされます。

## 注意

- 私的利用の範囲での運用を前提としています。フィードや音源を
  インターネットに広く公開しないでください(Tailscale等の閉じた
  経路を使う)。
- HTTPサーバーは最小構成(python http.server)です。認証が必要なら
  リバースプロキシ(Caddy/nginx)でBasic認証を足せます。

## フィードの自動更新

feed.xml は2つの仕組みで自動更新されます(併用):

1. **録音完了時に即更新** — 各録音スクリプトが録音を終えると
   make_podcast.py を呼んでフィードを更新します。新しい録音は
   すぐにフィードへ反映されます。
   (fmrec.conf の `PODCAST_AUTO_FEED="no"` で無効化可能)

2. **タイマーで定期更新(保険)** — 15分ごとにフィードを再生成します。
   録音時の即更新が何らかの理由で失敗しても、これで拾われます。

   ```bash
   sudo cp fmrec-podcast-feed.service /etc/systemd/system/
   sudo cp fmrec-podcast-feed.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fmrec-podcast-feed.timer
   systemctl list-timers fmrec-podcast-feed.timer   # 次回実行時刻を確認
   ```

手動で今すぐ更新したいときは `./make_podcast.py` を実行します。


---

## インターネットから聴く(Tailscale Funnel)

Tailscale を入れていないスマホや外出先の回線からも聴きたい場合、**Tailscale Funnel**
で自宅の配信サーバーをインターネットに公開できます。ポート開放・DDNS・レンタル
サーバー・証明書はすべて不要で、音声は自宅から直接配信されます。

### 1. Funnel を有効化

```bash
sudo tailscale funnel --bg 8080
#   初回は同意ページのURLが表示される → ブラウザで承認 → もう一度実行
tailscale funnel status
#   https://<マシン名>.<tailnet>.ts.net (Funnel on)  |-- / proxy http://127.0.0.1:8080
```

`--bg` を付けると再起動後も自動で復帰します。止めるときは `sudo tailscale funnel reset`。

### 2. アクセス制御(必須)

Funnel で公開すると URL を知った人は誰でもアクセスできるため、**Basic認証と
秘匿パス**を設定してから使います:

```bash
# fmrec.conf
PODCAST_AUTH_USER="tomo"
PODCAST_AUTH_PASS="$(openssl rand -base64 18 の出力)"     # 長いランダム文字列
PODCAST_URL_PREFIX="$(openssl rand -hex 8 の出力)"        # 例: x9k2m4pq7r1s
PODCAST_BASE_URL="https://radio:パスワード@<マシン名>.<tailnet>.ts.net/x9k2m4pq7r1s"
```

設定後、配信サーバーを再起動(`sudo systemctl restart fmrec-podcast`)し、フィードを
再生成(`./make_podcast.py`)します。BASE_URL に認証情報と秘匿パスを含めておくと、
フィード内の音声URLと index.html の購読リンクにも自動的に含まれ、Podcastアプリは
そのまま再生できます(iOS純正Podcastは `https://user:pass@host/...` 形式に対応)。

確認:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<マシン名>.<tailnet>.ts.net/feed.xml           # 404(秘匿パス外)
curl -s -o /dev/null -w "%{http_code}\n" https://<マシン名>.<tailnet>.ts.net/x9k2m4pq7r1s/feed.xml   # 401(認証なし)
curl -s -o /dev/null -w "%{http_code}\n" -u radio:パスワード https://<マシン名>.<tailnet>.ts.net/x9k2m4pq7r1s/feed.xml  # 200
```

### 注意

- tailnet 内から使っていた `http://…:8080` の URL は、認証を有効にすると同じく
  認証が必要になります(サーバー全体に掛かるため)。スマホは Funnel の URL に
  購読し直してください(index.html から)
- パスワードを変えたら BASE_URL も更新してフィード再生成・再購読
- Funnel の転送は Tailscale のリレー経由なので、多数の同時ストリーミングには
  向きませんが、個人利用には十分です
