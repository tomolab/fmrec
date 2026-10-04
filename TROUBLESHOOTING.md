# TROUBLESHOOTING — よくある問題と対処

実際に遭遇した問題を中心にまとめています。

---

## Podcast関連

### スマホで再生できない / エラーが出る

最も多い原因は **PODCAST_BASE_URL の設定ミス**です。フィードに書かれた音源URLが
スマホからアクセスできないと、アプリは「再生できませんでした」になります。

確認:

```bash
grep PODCAST_BASE_URL ~/fmrec/fmrec.conf
ip -4 addr show | grep inet | grep -v 127.0.0.1   # 実際のIP
```

この2つが食い違っていたら原因です。対処:

1. `fmrec.conf` の `PODCAST_BASE_URL` を実際のIP(`http://192.168.x.x:8080`)にする
2. フィードを再生成: `./make_podcast.py`
3. スマホで一度**購読を解除して、正しいURLで購読し直す**

チェックリスト:

- `raspberrypi.local` や `<マシン名>.local` のような mDNS名は iOS から使えます
  (検証済み)。Tailscaleの名前でも可。切り分け中はまずIPアドレスで確実に。
- IPアドレスの**打ち間違い**に注意(実際にあった)。
- IPはルーター再起動で**変わる**ことがある。変わったら直して再生成。

切り分けのコツ: 問題のm4aをPCにコピーしてローカルのVLC等で開く。ローカルで再生
できるなら配信側(URL/サーバー)の問題、ローカルでも駄目ならファイル側の問題。

### 再生の頭出しが遅い

Python 3.8 の `python -m http.server` は **HTTP Range に対応していない**ため、
毎回ファイル全体を先頭から送り、頭出しが遅くなります。同梱の **podcast_server.py**
(Range対応・マルチスレッド)を使ってください。

```bash
./podcast_server.py
# Range対応の確認(206 と出ればOK)
curl -s -o /dev/null -w "%{http_code}\n" -r 0-999 "http://localhost:8080/feed.xml"
```

### エピソードの並び順が変わらない

まず feed.xml 自体の並びを確認します。

```bash
grep -oP '<title>[^<]+</title>' ~/fmrec/../<OUTPUT_DIRの中>/feed.xml | head
```

feed.xml が放送日時の新しい順に並んでいれば、原因は**スマホアプリ側の並び順設定**です。
アプリで「新しい順」に設定してください。

- **iOS純正Podcast** はキャッシュが頑固で、フィードを変えても古い表示が残ることが
  あります。番組ページを下に引っ張って更新、それでも駄目なら**削除して再購読**。
- 番組ページの並べ替え設定を「新しいものから順に」にする。

### macのゴミファイルがPodcastに出る

macでファイルを扱うと `._ファイル名` や `.DS_Store` が作られることがあります。
make_podcast.py はこれらを除外しますが、フォルダ自体を掃除するには:

```bash
find <OUTPUT_DIR> -name "._*" -delete
find <OUTPUT_DIR> -name ".DS_Store" -delete
```

### 同じ番組がPodcastに重複して出る

`PODCAST_DEDUP="yes"`(既定)なら、統合フィード(feed.xml)ではFMとradikoの重複は
排除されます。もし重複するなら:

- `PODCAST_DEDUP` が `no` になっていないか確認。
- FM/radiko別フィード(feed_fm / feed_radiko)には意図的に両方残ります。統合フィード
  (feed.xml)を購読しているか確認。

### 配信サーバーが起動しない(Address already in use)

ポートが既に使われています。古いサーバーを止めてから起動します。

```bash
sudo ss -tlnp | grep :8080          # 何が使っているか確認
sudo systemctl stop fmrec-podcast.service   # systemdで動いている場合
pkill -f podcast_server.py          # 手動起動が残っている場合
pkill -f "http.server"              # 旧サーバーが残っている場合
```

手動の `&` 起動とsystemdが二重になっていないか注意。恒久運用はsystemdに任せます。

---

## 録音関連

### 録音されない / 予約が実行されない

- `atd` が動いているか: `systemctl status atd`。動いていなければ
  `sudo systemctl enable --now atd`。
- 予約されているか: `atq`。空なら `schedule_programs.py` がマッチを見つけていない
  (キーワードを確認)。
- 時刻が合っているか: `timedatectl`。特にラズパイはNTP同期を確認。
- cron/systemd経由で `ffmpeg: command not found`(rc=127)になる場合、PATHの問題。
  `fmrec.conf` の `EXTRA_PATH` にffmpegのあるディレクトリを設定。

### radikoライブ録音が無音になる

radikoの新仕様では各AACセグメントの先頭にID3タグが付いており、単純に連結すると
デコーダが `Number of bands (59) exceeds limit` 等で無音化します。同梱の
`radiko_live.sh` はID3タグを除去する処理(`strip_id3`)を持っているので、これを
使っていれば問題ありません。デバッグには `radiko_debug.sh` / `radiko_seg_debug.sh`。

### 停止した番組が再録音されてしまう / されない

- `stop_recording.sh` で止めた番組は台帳から削除され、次回 `schedule_programs.py`
  実行時に**まだ放送時間内なら再録音されます**(これは仕様)。
- 完全に録りたくない場合は、`keywords.txt` から該当キーワードを消してから止める/
  再実行する。

### 未来の予約が消えてしまった(過去の不具合)

`stop_recording.sh all` で、録音中でない未来の予約まで削除される不具合が過去に
ありましたが、修正済みです(録音中=`開始≤現在<終了`のもののみ削除)。最新版の
`schedule_lib.py` を使ってください。もし未来の予約が消えても、`schedule_programs.py`
を再実行すれば番組表から予約し直されます。

### FMとradikoで同じ番組の分かれ方が違う

これは**仕様**です。FMは連続番組を1本に連結、radikoは番組表どおり分割で取得します。
Podcastの統合フィードではFM1本にまとまります(feed_radikoには分割版が残ります)。

### 連続番組がうまく連結されない / 別番組が連結される

連結は「同じ局・番組名の幹が一致・時間が隣接」で判定します。番組表の番組名から
分割マーカー(回数・PART・Hour・丸数字・時刻など)を除いた「幹」で同一判定するので、
珍しい分割表記だと連結されないことがあります。その番組名を控えて、
`schedule_programs.py` の `program_stem()` にパターンを追加します
(開発者向け: HANDOFF.md 参照)。

---

## チューナー関連

### どのチューナーがどのデバイスか分からない

```bash
./check_tuners.sh
ls /dev/radio*
arecord -l
```

`ALSA_DEVS` はカード名(`hw:CARD=...`)で指定するため、USBの認識順が変わっても
正しいチューナーが選ばれます。

### チューナーが足りず録音できない番組がある

同時に3つ以上の番組が重なると、2台のチューナーでは足りません。その場合、
`both`/`fm`モードの局ではradiko録音へ自動振替されることがあります。重要な番組が
重なる時間帯は、片方を `radiko` モードにするなどで回避できます。

---

## 時刻・システム関連

### ラズパイで予約時刻がずれる

RTCが無いため、起動直後は時刻が不正確なことがあります。NTP同期を確認:

```bash
timedatectl
# 「System clock synchronized: yes」「NTP service: active」を確認
```

### systemdで動かない

- ユニット内の `User=` とパスが自分の環境と合っているか。
- `ExecStart` のスクリプトに実行権限があるか(`chmod +x`)。
- `journalctl -u <ユニット名>` でログを確認。
- 詳細は systemd/INSTALL.md。

---

## それでも解決しない場合

- 各Pythonスクリプトは `python3 -c "import ast; ast.parse(open('X.py').read())"` で
  構文を確認できます。
- 各シェルスクリプトは `bash -n X.sh` で構文確認できます。
- 録音の手動テスト、フィードの手動生成で問題を切り分けてください(USAGE.md / SETUP.md)。


## タイムフリー取得ができない番組がある

radikoには権利上の理由でタイムフリー配信されない番組があります(番組表の
`ts_out_ng=1`)。**NHKの語学番組(基礎英語など)がその例**で、タイムフリーで
取得するとその時間帯はフィラー音楽に差し替えられたファイルになります。schedule_programs.py はこれを検出して「スキップ: タイムフリー
対象外」と表示し、取得を予約しません。そうした番組はライブ録音(放送と同時)
なら可能なので、予約時点で放送前であればライブで録れます。放送後に気づいた
場合は録る手段がありません(FM電波の局なら次回からFM録音で)。


## 録音ファイルが短いのに「録音完了」になっている

旧版では「5秒未満」だけを失敗としていたため、途中でストリームが止まった数分の
ファイルが成功扱いになっていました(Airspy ハングの初回がこの形になりがち)。
現在は正常終了したのに実測が予定時間の `SHORT_FAIL_RATIO`(既定 0.5)未満なら
**録音失敗**として扱い、ファイルは削除せず `OUTPUT_DIR/failed/` に退避、
SDR なら USB リセットも試みます。`stop_recording.sh` による手動停止は
対象外(そのまま「途中終了」として残ります)。

既に `録音完了` になってしまった短いファイルは、手で `failed/` に移すか削除して
ください。統合フィード(feed.xml)では radiko 側が 15% 以上長ければそちらを
採用するので、radiko バックアップがあれば Podcast 上は影響しません。

## SDR の録音が連続して失敗する(長さ0秒)

`tuner_status.py` の先頭に `!!! 直近24時間に録音失敗 N件 !!!` と出て、
sdr_radio.log に `録音失敗 [tuner0/SDR] rc=0 長さ=0秒` が並ぶ場合、Airspy が
ハングしています(開けるがストリームが来ない)。sdr_radio.sh は失敗時に
自動で USB リセットを試みますが、それでも直らなければ物理的に抜き差し
してください。復旧の確認:

```bash
airspy_info
timeout 10 airspy-fmradion -t airspy -q -c freq=82500000,srate=3000000 -W /tmp/t.wav \
  && ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1 /tmp/t.wav
```

`duration=10` 前後なら復旧です。
