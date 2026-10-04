# SETUP — インストールマニュアル

fmrec をゼロから導入し、自動録音と Podcast 配信が動くまでの手順です。
上から順に実行してください。Ubuntu 20.04.6 LTS(Python 3.8)で検証済みです。
22.04 以降や Raspberry Pi OS でもほぼ同じ手順で動く想定です(相違点は各所と末尾に記載)。

所要時間の目安: 基本構成(radioSHARK + radiko)で1〜2時間、SDR(Airspy)追加で
さらに1時間程度。

**スクリプトで自動化する場合**は `./install.sh`(2〜5章、11章)と `./install_sdr.sh`
(4章)を使えます。本書は手動で進める場合と、各ステップの意味を知りたい場合の
リファレンスです。

---

## 0. 全体像

```
番組表(radiko API) ──▶ schedule_programs.py ──▶ at(予約ジョブ)
                              │                        │
                       keywords.txt              録音スクリプト
                       stations.conf         ┌────────┼────────┐
                                        shark_radio.sh  sdr_radio.sh  radiko_live.sh
                                        (radioSHARK)    (Airspy)      radiko_timefree.sh
                                              └────────┼────────┘
                                                   m4a + jpg
                                                       │
                                              make_podcast.py ──▶ feed*.xml / index.html
                                                       │
                                              podcast_server.py ──▶ スマホのPodcastアプリ
```

- **FM電波録音**: radioSHARK2(USBチューナー)または Airspy Mini(SDR、高音質)
- **radiko録音**: ライブ(放送と同時)+タイムフリー(放送後取得)のハイブリッド
- 両方を同じ番組で二重化でき(bothモード)、Podcast側で自動的に重複排除します

---

## 1. ハードウェアの準備

### radioSHARK2(基本)

USBに接続します。2台使う場合はセルフパワーのUSBハブ推奨。
制御用の `radio` コマンド(V4L2でチューニング)が必要です。お使いの環境に合わせて
用意してください。

### Airspy Mini(任意・高音質FM録音)

SMAコネクタにFMアンテナを接続します。都内で弱い局(FMヨコハマ等)を狙うなら
屋外FM八木を目的局の送信所方向へ。連続稼働で発熱するのでヒートシンク貼付推奨。
セットアップは **4章**、詳細は **SDR.md**。

---

## 2. 基本パッケージ

```bash
sudo apt update
sudo apt install at alsa-utils ffmpeg curl python3 python3-pip git
sudo systemctl enable --now atd      # 予約ジョブの実行デーモン(必須)
pip install cairosvg pillow --break-system-packages   # Podcastアイコン・局ロゴ用
```

| パッケージ | 用途 |
|---|---|
| at / atd | 番組ごとの録音ジョブを時刻指定で実行。**atd が動いていないと録音されません** |
| alsa-utils | `arecord`(radioSHARKの録音) |
| ffmpeg | m4a変換・メタデータ・カバーアート埋め込み。**HE-AAC対応版推奨**(下記) |
| cairosvg / pillow | Podcastのアイコン生成と局ロゴ合成(pipで導入) |

### ffmpeg について

radiko のライブ配信は HE-AAC のため、Ubuntu 標準の ffmpeg では変換時に警告が出る
ことがあります。自前ビルドの ffmpeg を `~/bin` 等に置いた場合、fmrec.conf の
`EXTRA_PATH` にそのディレクトリを指定すると、systemd/cron/at から実行されるすべての
スクリプトがそれを優先して使います(**録音スクリプトだけでなく make_podcast.py も**
これに従います)。

### rec_radiko_ts(radikoタイムフリー用)

```bash
cd ~ && git clone https://github.com/uru2/rec_radiko_ts
```

`~/rec_radiko_ts/rec_radiko_ts.sh` のパスを後で fmrec.conf に書きます。

---

## 3. radioSHARK の確認

```bash
ls /dev/radio*     # /dev/radio0 (2台なら radio1 も)
arecord -l         # カード名を確認: radioSHARK, radioSHARK_1 など
./check_tuners.sh  # (fmrec配置後)デバイスとALSAカードの対応を確認
```

`arecord -l` に出るカード名は fmrec.conf の `ALSA_DEVS` に使います。
**2台目のカード名(`radioSHARK_1`)はUSBの認識順で変わることがある**ので、
挿し直したときは再確認してください。

---

## 4. Airspy Mini のセットアップ(任意)

Airspy を使わない場合はこの章を飛ばしてください。

### 4-1. 認識と権限(udev)

```bash
sudo apt install airspy libairspy-dev mbuffer   # mbuffer: 録音パイプのバッファ(推奨)
airspy_info
```

`Found AirSpy board 1` と出ればOK。**`AIRSPY_ERROR_NOT_FOUND` で `sudo airspy_info`
なら成功する場合は権限の問題**です。録音は一般ユーザーのatジョブから実行されるため、
udevルールを追加します:

```bash
sudo tee /etc/udev/rules.d/52-airspy.rules > /dev/null << 'RULE'
ATTR{idVendor}=="1d50", ATTR{idProduct}=="60a1", SYMLINK+="airspy-%k", MODE="0660", GROUP="plugdev", TAG+="uaccess"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger
# Airspy を抜き差ししてから sudo 無しで
airspy_info
```

### カーネルの airspy ドライバを無効化(必須)

Linux には V4L2 の airspy ドライバが含まれており、Airspy を挿すとカーネルが
先に掴みます(`dmesg` に `airspy 1-8:1.0: Registered as swradio0`)。airspy-fmradion
が使う libairspy はユーザー空間から USB を直接扱うため、**二重管理の競合**になり、
デバイスが開けない・転送が不安定になる原因になります(`usbfs: interface 0 claimed
by usbfs while 'airspy-fmradion' sets config` という警告が出ます)。無効化します:

```bash
echo "blacklist airspy" | sudo tee /etc/modprobe.d/blacklist-airspy.conf
sudo modprobe -r airspy      # 使用中で外せなければ再起動
# Airspy を抜き差しして、dmesg に「Registered as swradio0」が出ないことを確認
airspy_info
```

### 4-2. airspy-fmradion のビルド

高品質FM復調ソフト airspy-fmradion をソースからビルドします。
**Ubuntu 20.04 では以下3つの壁があります**(検証済みの回避策付き):

```bash
# 依存パッケージ
sudo apt install build-essential cmake pkg-config libusb-1.0-0-dev \
    libasound2-dev libairspy-dev libairspyhf-dev librtlsdr-dev \
    libsndfile1-dev libvolk2-dev portaudio19-dev
#   (libvolk2-dev が無ければ libvolk1-dev)

# 壁1: cmake 3.18 以上が必要(20.04 は 3.16) → pip で新版を入れる
pip install cmake --break-system-packages
#   以後 airspy-fmradion のビルドには ~/.local/bin/cmake を使う

# 壁2: libairspyhf が古い(airspyhf_is_low_if 未定義) → ソースから再ビルド
sudo apt remove libairspyhf-dev libairspyhf1
cd ~ && git clone https://github.com/airspy/airspyhf
cd airspyhf && mkdir build && cd build
/usr/bin/cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
#   (こちらは古い cmake でよいので /usr/bin/cmake をフルパス指定)

# 壁3: portaudio → 上の依存に portaudio19-dev を含めてあります

# 壁4: libairspy も古い(1.0.9=2016年。ストリーム停止時にハングする既知の不具合
#       があり、録音停止・再予約が失敗する) → 最新をソースから
sudo apt remove libairspy-dev libairspy0 airspy
cd ~ && git clone https://github.com/airspy/airspyone_host
cd airspyone_host && mkdir build && cd build
/usr/bin/cmake .. -DINSTALL_UDEV_RULES=ON && make -j"$(nproc)" && sudo make install && sudo ldconfig
airspy_info    # airspy_lib_version が新しくなっていることを確認
#   (公式のudevルールも入るので 4-1 の手作りルールは不要になる)

# 本体
cd ~ && git clone --recursive https://github.com/jj1bdx/airspy-fmradion
cd airspy-fmradion && mkdir build && cd build
~/.local/bin/cmake .. && make -j"$(nproc)" && sudo make install
airspy-fmradion 2>&1 | head -2    # バージョンが出ればOK
```

Ubuntu 22.04 以降や Raspberry Pi OS(Bookworm)では apt の cmake / libairspyhf で
そのまま通ることが多いですが、**libairspy はどの環境でもソースビルド推奨**です
(apt版で停止ハングを実機確認)。

ビルド後の**停止テスト**(重要): `timeout 10 airspy-fmradion -t airspy -q -c
freq=80000000,srate=3000000 -W /tmp/t.wav` を実行し、10秒後にプロンプトが戻れば
OK。戻らない(「Stopping by getting signal」の後ハングする)なら libairspy が古い
ままです。

### 4-3. 受信テスト

```bash
# 強い局で10秒録音(TOKYO FM 80.0 / J-WAVE 81.3 など)
timeout 10 airspy-fmradion -t airspy -q -c freq=81300000,srate=3000000 -E 288 \
    -W /tmp/test.wav
ffprobe /tmp/test.wav     # 48kHz / 2ch / 約10秒 なら復調OK
```

- `srate=3000000` は Airspy Mini 用(6M/3Mのみ対応。R2 なら 10000000)
- `-E 288` はマルチパスフィルタ(段数)。**`-M` はモノラル化なので使わない**
- ゲイン(lgain/mgain/vgain)の詰め方は SDR.md。局ごとに設定できます(7章)

---

## 5. ファイルの配置

```bash
mkdir -p ~/fmrec && cd ~/fmrec
# 一式をここにコピーしてから
chmod +x *.sh *.py
```

主なファイル:

| ファイル | 役割 |
|---|---|
| schedule_programs.py | 番組表からキーワード検索して予約(毎日実行) |
| shark_radio.sh / sdr_radio.sh | FM電波録音(radioSHARK / Airspy) |
| radiko_live.sh / radiko_timefree.sh | radiko録音(ライブ / タイムフリー) |
| make_podcast.py / podcast_server.py | フィード生成 / 配信サーバー |
| make_icons.py / fetch_logos.py | Podcastアイコン生成 / 局ロゴ取得・合成 |
| tuner_status.py / stop_recording.sh | 状況確認 / 録音停止 |
| make_stations.py | stations.conf の雛形生成 |
| fmrec.conf / stations.conf / keywords.txt | 設定(3ファイル、次章) |

録音の保存先(`OUTPUT_DIR`)は容量の大きいディスクを別に用意することを推奨します
(1番組1時間で約100〜200MB)。

---

## 6. fmrec.conf(共通設定)

```bash
cp fmrec.conf.example fmrec.conf
nano fmrec.conf
```

最低限設定する項目(実運用値の例):

```bash
OUTPUT_DIR="/media/data/radio/fmrec"          # 録音・配信フォルダ
#EXTRA_PATH="/home/USER/bin"                   # 自前ffmpegがあれば

# チューナー構成(既定は Airspy 1台。radioSHARK2 を足すなら "airspy shark")
TUNER_TYPES="airspy"
SDR_RECORD_SCRIPT="/home/USER/fmrec/sdr_radio.sh"
SDR_SAMPLE_RATE="3000000"
SDR_DEVICE_CONF="lgain=5,mgain=3,vgain=5"

# radioSHARK2 を使う場合(TUNER_TYPES に shark を含めたとき)
#RADIO_DEVS="/dev/radio0"
#ALSA_DEVS="hw:CARD=radioSHARK,DEV=0"
#RECORD_SCRIPT="/home/USER/fmrec/shark_radio.sh"

# radiko
RADIKO_LIVE_SCRIPT="/home/USER/fmrec/radiko_live.sh"
RADIKO_TF_SCRIPT="/home/USER/rec_radiko_ts/rec_radiko_ts.sh"
RADIKO_DELAY_SECONDS="72"     # 配信遅延の補償(環境依存。下記)

# 録音タイミング
PRE_ROLL_SECONDS="5"          # 定刻の何秒前から録るか
EXTRA_MINUTES="1"             # 定刻後の録り足し
GUIDE_DAYS="7"                # 番組表を何日先まで予約するか

# Podcast
PODCAST_BASE_URL="http://<マシン名>.<tailnet>.ts.net:8080"   # 10章参照
```

### RADIKO_DELAY_SECONDS(要調整)

radiko の配信は実放送より遅れて届きます(環境で15〜80秒)。この値だけ録音窓を
後ろへずらすことで、頭の前番組混入を減らし、番組の尻が欠けないようにします。
**最初は既定(20)で1本録音し、「番組開始まで何秒あったか」を測って、その秒数を
足してください**(例: 60秒残っていれば 20+60=80 → 安全側で 72)。

全項目の説明は **CONFIG.md**。

---

## 7. stations.conf(受信局)

雛形の入手は2通り:

```bash
# A. 全国カタログから選ぶ(オフラインで可)
cp stations.conf.example stations.conf   # 自分のエリアの局の行頭 # を外す
# B. radiko API から自動生成(局IDが確実)
./make_stations.py JP13 > stations.conf   # JP13=東京。JP27=大阪 など
```

書式: `<radiko局ID> <周波数MHz|-> <局名> [モード] [tuner=N|prefer=N] [sdr=...]`

```
FMT      80.0  TOKYO-FM     both
JOAK-FM  82.5  NHK-FM       both  prefer=2  sdr=lgain=0,mgain=2,vgain=6
YFM      84.7  FMヨコハマ   both  tuner=2   sdr=lgain=2,mgain=8,vgain=12
TBS      90.5  TBSラジオ    both
RN1      -     ラジオNIKKEI1 radiko
```

- **モード**: `fm`(電波のみ) / `radiko`(radikoのみ) / `both`(両方・既定)
- **tuner=N**: その局はチューナーN専用(他局はNを使えない=温存)。Airspyを弱い局の
  専用機にする用途
- **prefer=N**: 優先。Nが空いていれば使い、埋まっていれば他へフォールバック。
  Airspyで複数局を高音質録音したいとき
- **sdr=...**: SDRで録るときのゲイン等(fmrec.conf の SDR_DEVICE_CONF より優先)。
  強い局は低め、弱い局は高め、と局ごとに変える
- 局名は日本語可(空白は不可)。ファイル名・Podcast表示・局別フィード名に使われます

---

## 8. keywords.txt(録りたい番組)

```
安住紳一郎
JUNK
オールナイトニッポン -MUSIC10 -GOLD
きょうのニュース @JOAK-FM
!放送休止
```

- 1行1キーワード。番組名・出演者・番組内容に部分一致
- `@局ID` でその局だけ(NHKのR1/FMサイマル二重録音の回避に)
- `-語` でその行の除外、`!語` の行は全キーワード共通の除外
- 詳細は CONFIG.md

---

## 9. 動作確認

### 9-1. 手動録音(各1分)

```bash
./shark_radio.sh 0 1 "テストFM" "テスト" 80.0 "TOKYO-FM"          # radioSHARK
./sdr_radio.sh   2 1 "テストSDR" "テスト" 81.3 "J-WAVE"           # Airspy(使う場合)
./radiko_live.sh TBS 1 "テストradiko" "テスト" "TBSラジオ"         # radikoライブ
ls -la $OUTPUT_DIR/テスト*.m4a
```

3種類とも m4a ができて再生できれば録音系はOK。Airspy の録音が本当にSDR経由かは、
録音中に `pgrep -a airspy-fmradion` で確認できます。

### 9-2. 予約

```bash
./schedule_programs.py     # 番組表取得→キーワード検索→at予約
./tuner_status.py          # チューナーごとの予約状況を1画面で
atq                        # atジョブ一覧
```

「予約: … [tuner0] …」の行が並べば予約系はOK。マッチしないときは keywords.txt と
stations.conf の局IDを見直してください。

---

## 10. Podcast 配信

### 10-1. PODCAST_BASE_URL(重要)

スマホがこのマシンへアクセスできるURLを設定します。選択肢:

- **Tailscale**(推奨・外出先からも可): `http://<マシン名>.<tailnet>.ts.net:8080`
- **mDNS**(LAN内): `http://<マシン名>.local:8080`(iOSから使用可、検証済み)
- **LAN内IP**: `http://192.168.x.x:8080`(ルーター再起動で変わることがある)

**入力ミスがあると再生できません**(過去のトラブルの原因はこれでした)。

### 10-2. アイコン・局ロゴ・フィード

```bash
./make_icons.py "$OUTPUT_DIR"   # ラジオのアイコン一式(podcast*.png)
./fetch_logos.py                # 局ロゴを取得してアイコンに合成(logo_<局名>.png)
./make_podcast.py               # フィード生成(feed*.xml, index.html)
```

生成されるフィード:

| ファイル | 内容 |
|---|---|
| feed.xml | すべての録音(FM/radiko重複は自動排除) |
| feed_fm.xml / feed_radiko.xml | FM録音のみ / radiko録音のみ |
| feed_<局名>.xml | 放送局別(局ロゴ入りアイコン) |
| feed_p_<番組名>.xml | **番組別(自動生成・設定不要)**。番組画像がカバーになる |
| index.html | 上記すべての一覧。スマホのブラウザで開いて「Podcastで購読」 |

### 10-3. 配信サーバー

```bash
./podcast_server.py &          # 動作確認用(常駐は11章のsystemdで)
curl -sI -H "Range: bytes=0-1" "$PODCAST_BASE_URL/feed.xml" | head -1   # 206 ならOK
```

スマホのブラウザで `$PODCAST_BASE_URL/index.html` を開き、聴きたいフィードの
「Podcastで購読」をタップ → 純正Podcastアプリに登録されます。

補足: 番組画像(radikoの番組表から取得)は m4a にも埋め込まれますが、iOS純正
Podcastはエピソード単位の画像を表示しない世代がありました(iOS 26以前。
iOS 27では表示されます)。そのため番組別フィードのカバーとしても使う設計に
なっています。詳細は PODCAST.md。

---

## 11. 常駐化(systemd)

```bash
cd systemd/
# 各 .service の User= と ExecStart= のパスを自分の環境に合わせて編集してから
sudo cp fmrec-*.service fmrec-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmrec-schedule.timer      # 毎日の予約
sudo systemctl enable --now fmrec-podcast.service     # 配信サーバー
sudo systemctl enable --now fmrec-podcast-feed.timer  # フィード再生成(保険)
systemctl list-timers | grep fmrec
```

詳細と確認方法は **systemd/INSTALL.md**。

---

## 12. 日常の運用

| やりたいこと | コマンド |
|---|---|
| いまの状況を見る | `./tuner_status.py` |
| 録音を止める | `./stop_recording.sh`(一覧) / `stop_recording.sh 0` / `all` |
| キーワードを変えた | `./schedule_programs.py`(次回タイマーでも反映) |
| 予約を全部作り直す | `./schedule_programs.py --reset` |
| 局ロゴを取り直す | `./fetch_logos.py && ./make_podcast.py` |

### スクリプトを更新したとき

録音スクリプトは起動時のファイル内容で動くので、差し替えは次の録音から効きます。
**予約コマンドの中身が変わる更新**(schedule_programs.py の変更)は既存の
atジョブに反映されないため、`./schedule_programs.py --reset` で作り直してください
(取得済みの番組は再取得されません)。

### 古い録音の削除

m4a と同名の .jpg(番組画像)はペアで消してください。フィードは次回生成で追従します。

詳しくは **USAGE.md**。困ったときは **TROUBLESHOOTING.md**。

---

## 13. Raspberry Pi での注意

- **NTP必須** — RTCが無いので起動時の時刻同期が要ります。`timedatectl` で確認
- **パス** — fmrec.conf と systemd ユニットの `/home/USER/...` を自分の環境に
- **USB電力** — radioSHARK 2台 + Airspy はセルフパワーのハブを
- **CPU** — airspy-fmradion のマルチパスフィルタ(-E 288)は負荷が高め。Pi 4 以上推奨、
  厳しければ `SDR_MULTIPATH` を減らすか 0 に
- Raspberry Pi OS(Bookworm)なら4-2章の cmake / libairspyhf の壁は apt で通ります

---

## ドキュメント索引

| ファイル | 内容 |
|---|---|
| README.md | 概要とファイル構成 |
| **SETUP.md**(本書) | インストール手順 |
| USAGE.md | 日々の使い方(予約・停止・Podcast購読) |
| CONFIG.md | 全設定項目のリファレンス |
| PODCAST.md | Podcast配信の詳細(Tailscale、フィード構成) |
| SDR.md | Airspy の詳細(ゲイン調整・アンテナ・音質) |
| TROUBLESHOOTING.md | トラブル対処 |
| systemd/INSTALL.md | 常駐化の詳細 |
