# systemd サービス化手順

fmrec は3系統のsystemdユニットで運用します。

- **fmrec-schedule**(service + timer) — 毎日、番組表を取得して録音予約
- **fmrec-podcast**(service) — Podcast配信サーバーを常駐
- **fmrec-podcast-feed**(service + timer) — 15分ごとにフィードを再生成(保険)

すべて、各 `.service` の `User=` と `ExecStart=` のパスを自分の環境に合わせて
編集してから配置してください。

---

## A. 毎日の予約処理(fmrec-schedule)

### 1. 配置

```bash
sudo cp fmrec-schedule.service fmrec-schedule.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 2. 有効化

```bash
# atd(個々の録音ジョブの実行に必要)
sudo systemctl enable --now atd

# 毎日の予約処理タイマー
sudo systemctl enable --now fmrec-schedule.timer
```

既存の crontab に同じ処理の行があれば削除しておく(二重実行防止):

```bash
crontab -e   # schedule_programs.py の行を削除
```

### 3. 動作確認

```bash
# タイマーの次回実行時刻を確認
systemctl list-timers fmrec-schedule.timer

# 手動で今すぐ1回実行してみる
sudo systemctl start fmrec-schedule.service

# 実行ログを見る
journalctl -u fmrec-schedule.service -e

# 予約が入ったか確認
atq
```

---

## B. Podcast配信サーバー(fmrec-podcast)

録音ファイルとRSSフィードを配信する常駐サーバーです。Range対応の
`podcast_server.py` を起動します。

```bash
sudo cp fmrec-podcast.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmrec-podcast.service
sudo systemctl status fmrec-podcast.service   # active (running) を確認
```

`ExecStart` が `podcast_server.py` を指していることを確認してください(古い
`python -m http.server` だとRange非対応で再生が遅くなります)。

手動の `&` 起動と二重にならないよう、手動起動は止めてからにしてください。

---

## C. フィードの定期更新(fmrec-podcast-feed)

録音完了時にも各録音スクリプトがフィードを更新しますが(`PODCAST_AUTO_FEED="yes"`)、
その取りこぼしを拾うため、15分ごとにフィードを再生成するタイマーです。

```bash
sudo cp fmrec-podcast-feed.service fmrec-podcast-feed.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmrec-podcast-feed.timer
systemctl list-timers fmrec-podcast-feed.timer   # 次回実行時刻を確認
```

`fmrec-podcast-feed.service` は oneshot(実行後すぐ終了)です。実行後に
`inactive (dead)` になるのが正常です。手動で今すぐ更新したいときは:

```bash
sudo systemctl start fmrec-podcast-feed.service
```

## 仕組みと利点

- 毎日05:00に schedule_programs.py が走り、番組表を検索して
  at に録音ジョブを登録する(録音自体は従来どおり atd が実行)
- `Persistent=true` により、05:00に電源が落ちていても
  次回起動時に自動で追いつき実行される(cronとの最大の違い。
  ラズパイの電源断・再起動に強い)
- 標準出力・エラーはすべて journalctl に記録される

## ラズパイ移行時

このファイル一式とスクリプト・設定をコピーし、
`User=` とパスを直して同じ手順を踏むだけで動きます。
