# SDR — Airspy Mini による高音質FM録音

Airspy Mini + airspy-fmradion を使い、特定局を高音質で録音するためのガイドです。
radioSHARK と併用し、電波の弱い局(例: 都内からのFMヨコハマ)を Airspy の
高ダイナミックレンジ(12bit)+マルチパスフィルタで録る構成を想定しています。

---

## 必要なもの

- Airspy Mini(+貼り付け型ヒートシンク推奨。常時稼働で発熱するため)
- FMアンテナ(付属ダイポールでも検証可。本命は屋外FM用八木を目的局の
  送信所方向へ)
- F型(75Ω)→SMA変換(TVアンテナ線を使う場合)

## 1. airspyoneホストツールの導入

```bash
sudo apt install airspy libairspy-dev
# 認識確認(Airspy Miniを挿して)
airspy_info
```

`Serial number` などが表示されれば認識OKです。

### 一般ユーザーで開けない場合(udev設定・必須)

`airspy_open() ... AIRSPY_ERROR_NOT_FOUND` と出て `sudo airspy_info` なら
動く場合は権限の問題です。録音はatジョブ(一般ユーザー)から実行されるため、
udevルールで一般ユーザーに開放する必要があります:

```bash
sudo tee /etc/udev/rules.d/52-airspy.rules > /dev/null << 'RULE'
ATTR{idVendor}=="1d50", ATTR{idProduct}=="60a1", SYMLINK+="airspy-%k", MODE="0660", GROUP="plugdev", TAG+="uaccess"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger
# Airspyを抜き差ししてから、sudo無しで
airspy_info
```

`lsusb` に `1d50:60a1` が無い場合はUSB接続の問題(挿し直し・別ポート・
ハブを介さない)です。

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

## 2. airspy-fmradion のビルド

高品質FM復調ソフト [airspy-fmradion](https://github.com/jj1bdx/airspy-fmradion)
をビルドします。

```bash
sudo apt install git build-essential cmake pkg-config \
    libusb-1.0-0-dev libasound2-dev libairspy-dev libairspyhf-dev \
    librtlsdr-dev libsndfile1-dev libvolk2-dev
git clone --recursive https://github.com/jj1bdx/airspy-fmradion
cd airspy-fmradion
mkdir build && cd build
cmake ..
make -j"$(nproc)"
sudo make install    # /usr/local/bin/airspy-fmradion
```

(依存パッケージ名はUbuntuのバージョンで多少変わります。cmake時に足りない
ライブラリが表示されたら適宜インストールしてください。)

### libairspy を最新にする(必須)

apt の libairspy(1.0.9、2016年)にはストリーム停止時にハングする不具合があり、
録音停止(stop_recording)や連続予約が失敗します。最新をソースからビルドします:

```bash
sudo apt remove libairspy-dev libairspy0 airspy
cd ~ && git clone https://github.com/airspy/airspyone_host
cd airspyone_host && mkdir build && cd build
/usr/bin/cmake .. -DINSTALL_UDEV_RULES=ON && make -j"$(nproc)" && sudo make install && sudo ldconfig
airspy_info
# airspy-fmradion はリンクし直す
cd ~/airspy-fmradion/build && rm -rf * && ~/.local/bin/cmake .. && make -j"$(nproc)" && sudo make install
```

### 同一局の連続番組(引き継ぎ)の秒数

同じ局の連続番組を1台のAirspyで録るとき、前番組の終了処理(fmradionのTERM→終了、
ハング時はKILLまで最大10秒)に時間がかかるため、radioSHARKより余裕が要ります。
fmrec.conf で SDR 専用の値を設定できます:

```bash
SDR_HANDOFF_LEAD_SECONDS="5"     # 前番組は定刻5秒前に終了(尻が5秒欠ける)
SDR_HANDOFF_START_SECONDS="0"    # 次番組は定刻ちょうどから録音
```

未設定なら共通の HANDOFF_LEAD_SECONDS / HANDOFF_START_SECONDS(既定 5/0)が
使われます。mbuffer を入れてあれば START=0 でも頭から録れます(実機で確認)。次番組の頭が欠けるなら START を増やし、LEAD も同じだけ増やして
猶予(LEAD-START)を保ってください。

### Airspy がハングして録音が全滅する場合(自動復旧)

Airspy はまれに「デバイスは開けるがストリームが来ない」状態になります。
症状は sdr_radio.log に `録音失敗 ... 長さ=0秒` が連続すること(実例: 2日近く
全滅。radiko 側は動いていたため Podcast には番組が並び、気づくのが遅れた)。
物理的な抜き差しで復旧します。

これをソフトウェアから行うのが `sdr_usbreset.py`(USB リセット ioctl、
udev で plugdev に開放してあれば root 不要)で、sdr_radio.sh は録音失敗時に
自動で実行します(`SDR_AUTO_USBRESET="yes"`、既定)。手動でも:

```bash
./sdr_usbreset.py            # リセット
./sdr_usbreset.py --check    # 見つかるかだけ確認
```

また `tuner_status.py`(と index.html の状況表示)は直近24時間の録音失敗を
先頭に `!!!` 付きで表示するので、失敗が続いていれば気づけます。

### 停止がハングする場合(USBコントローラの相性)

libairspy を最新にしても、停止時に「Stopping by getting signal」の後で終了しない
ことが**間欠的に**起きる場合があります(USB転送のキャンセル待ちが返らない)。
sdr_radio.sh は `timeout -k`(既定3秒、`SDR_KILL_GRACE_SECONDS` で調整)で
強制終了するので予約録音には影響しませんが、根本対策は **USB 2.0 ポート
(マザーボードの黒いコネクタ)に挿し替える**ことです。
SDR1台構成では次番組が確実に影響を受けるため、猶予は `HANDOFF_LEAD` より
十分小さくしておきます(既定 3秒 < LEAD 8秒)。Airspy Mini は一部の USB3(xhci)コントローラと相性問題の
報告があります。`lsusb -t` でどのコントローラ配下かを確認できます。

## 3. 受信テスト

まずコマンド単体で受信できるか確認します(TOKYO-FM 80.0MHzの例):

```bash
airspy-fmradion -t airspy -q -c freq=80000000,srate=3000000 -E 288 -R - \
  | aplay -f S16_LE -r 48000 -c 2
```

音が出れば成功。次に目的の弱い局(FMヨコハマ 84.7MHz)で:

```bash
airspy-fmradion -t airspy -q -c freq=84700000,srate=3000000,lgain=8,mgain=8,vgain=10 -E 288 -R - \
  | aplay -f S16_LE -r 48000 -c 2
```

### ゲイン比較スクリプト(sdr_gaintest.sh)

局ごとに最適ゲインを詰めるための道具です。ゲインの組み合わせを順に短時間
録音し、設定値入りのファイル名で並べます:

```bash
./sdr_gaintest.sh 82.5            # NHK-FM を既定10秒ずつ、$OUTPUT_DIR/gaintest へ
./sdr_gaintest.sh 84.7 15 /tmp/yfm  # YFM を15秒ずつ /tmp/yfm へ
```

出力先に配信フォルダ($OUTPUT_DIR)を指定して `make_podcast.py` を実行すれば、
スマホで聴き比べられます(聴き終わったらファイルを消して再生成)。

試す組み合わせは fmrec.conf の `GAIN_SETS` で変えられます(1行1組、
`lgain,mgain,vgain[,multipath=N][,mono][,filter=narrow]`):

```bash
GAIN_SETS="0,2,6
2,4,10
5,3,5
5,6,10
8,8,12"
```

良かった設定を stations.conf の `sdr=` に書けば完了です。

### 録音冒頭の「プチッ」

airspy-fmradion は起動直後の1〜2秒、DCオフセットやフィルタが収束するまで出力が
乱れ、ピークがフルスケールを超えてクリップすることがあります(実測: 冒頭2秒だけ
`max_volume: 0.0 dB`、それ以降は `-2.7 dB`)。sdr_radio.sh は既定で0.3秒の
フェードインをかけてこれを目立たなくします(実機で0.3秒あれば十分と確認)(`SDR_FADEIN_SECONDS` で調整、0で無効)。
フェードする区間は PRE_ROLL(定刻前)なので番組本編には影響しません。

確認方法:

```bash
f=<録音ファイル>
ffmpeg -v info -i "$f" -af volumedetect -f null - 2>&1 | grep max_volume          # 全編
ffmpeg -v info -ss 2 -i "$f" -af volumedetect -f null - 2>&1 | grep max_volume   # 冒頭2秒を除く
```

全編が 0.0 dB で、冒頭を除くと下がるなら、冒頭のクリップです。

### 雑音の切り分け(実機で確認した典型例)

| 症状 | 原因 | 対処 |
|---|---|---|
| ザラザラ・ブツブツ(ゲインを変えても残る) | **処理落ち**(6MSPS+マルチパスフィルタでCPUが追いつかない) | `SDR_SAMPLE_RATE="3000000"`(これで解決した実績)。それでも出るなら `SDR_MULTIPATH` を減らす |
| 周期的に同調が外れる・ザーザーになる(強い局でも) | 周期的な小さな外乱(ffmpegの書き出しでパイプが詰まりサンプル欠落)を**マルチパスフィルタが増幅**して発散 | `sudo apt install mbuffer`(sdr_radio.shが自動で16MBバッファを挟む)。フィルタは局別 `multipath=0` で切れる |
| サーッ(ホワイトノイズ) | 信号不足 | lgain/vgain を上げる。根本はアンテナ |
| 歪む・別局の音が混ざる | 過入力(強波の混変調) | lgain を下げる。根本は指向性アンテナ |
| シュワシュワ(ステレオの揺らぎ) | 弱電界でのステレオ復調 | モノラル化(`-M`) |

### ゲイン調整のコツ

Airspy Mini は3系統のゲイン(lgain/mgain/vgain、各0〜15程度)を持ちます。

- 音が歪む・バリバリいう → 強すぎ。まず lgain を下げる
- ノイズが多い(サーッという背景音) → 弱すぎ。vgain → mgain → lgain の順に上げる
- 強い局(スカイツリー波)と弱い局で最適値が違う場合は、**局ごとに変えられます**。
  stations.conf の局行に `sdr=` を書くと、その局は fmrec.conf の SDR_DEVICE_CONF
  (共通設定)より優先してその値で録音します:

  ```
  YFM      84.7  FMヨコハマ  both  tuner=2  sdr=lgain=2,mgain=8,vgain=12
  JOAK-FM  82.5  NHK-FM      both  tuner=2  sdr=lgain=0,mgain=2,vgain=6
  ```

  強電界の目安: LNA(lgain)を絞り VGA(vgain)で稼ぐ。スカイツリー至近で弱い局を
  受ける場合は lgain=0 から1ずつ上げ、混変調(別局の混入・ザラつき)が出る
  1〜2段手前が最適。強い局を直接受けるなら全段低め(lgain=0,mgain=2,vgain=6)。

  `sdr=` にはゲイン以外に、局ごとの復調オプションも書けます:

  | 指定 | 意味 | 使いどころ |
  |---|---|---|
  | `multipath=N` | マルチパスフィルタ段数(`-E`)。0で無効 | **遠距離の弱い局は 0 に**(フィルタが収束せず逆効果。IBSで確認) |
  | `mono` | ステレオ復調を切る(`-M`) | 弱い局のSN改善 |
  | `filter=narrow` | 帯域を絞る(`-f narrow`/`medium`) | 遠距離受信の定石(高域は少し減る) |

  ```
  IBS  94.6  LuckyFM  both  prefer=2  sdr=lgain=12,mgain=10,vgain=14,multipath=0,mono,filter=narrow
  ```

`-E 288` はマルチパスフィルタ(都市部の反射波によるひずみを軽減、段数指定)。
CPU負荷が問題になる場合は数を減らすか、conf で `SDR_MULTIPATH="0"` にして
無効化します。`srate=3000000` は Airspy Mini 用(6M/3Mのみ対応。R2 なら
10000000)。注意: `-M` はステレオ無効化(モノラル)のオプションなので使いません。

## 4. fmrec への組み込み

### SDR だけで運用する場合

radioSHARK を使わず Airspy 1台で運用するなら、チューナー種別を `airspy` だけに
します。`RADIO_DEVS` / `ALSA_DEVS` / `RECORD_SCRIPT`(radioSHARK用)は不要です:

```bash
TUNER_TYPES="airspy"
SDR_RECORD_SCRIPT="/home/USER/fmrec/sdr_radio.sh"
SDR_SAMPLE_RATE="3000000"
SDR_DEVICE_CONF="lgain=5,mgain=3,vgain=5"
```

この構成では全番組が1台のAirspyを共有するため、連続番組は必ず引き継ぎ
(前番組の尻が欠ける)になります。局が違っても隣接なら引き継げます。

### fmrec.conf

```
# radioSHARK2台 + Airspy Mini の3チューナー構成
TUNER_TYPES="shark shark airspy"
SDR_RECORD_SCRIPT="/home/USER/fmrec/sdr_radio.sh"
SDR_DEVICE_TYPE="airspy"
SDR_DEVICE_CONF="lgain=8,mgain=8,vgain=10"   # 3で調整した値
SDR_SAMPLE_RATE="3000000"                    # 6Mは処理落ちしやすい
SDR_MULTIPATH="288"
SDR_AUDIO_CODEC="aac"      # flac にすると可逆(高音質アーカイブ)
```

### stations.conf — 専用局の指定

```
YFM   84.7  FM-YOKOHAMA  both  tuner=2   # Airspy(tuner2)専用
FMT   80.0  TOKYO-FM     both            # 指定なし → shark(tuner0/1)に自動割当
```

`tuner=N` を付けた局はそのチューナーだけでFM録音され、付けない局の自動割当
からは tuner=N は除外されます(専用機として温存)。つまり Airspy は指定局の
ためだけに常に空けてあり、録り逃しがありません。

**複数の局をAirspyで録りたい場合**は `prefer=N`(優先)を使います。空いていれば
Airspy、埋まっていればradioSHARKへフォールバックするので、専用局(tuner=)との
衝突時にも録り逃しません:

```
YFM      84.7  FMヨコハマ  both  tuner=2   sdr=lgain=2,mgain=8,vgain=12   # 専用
JOAK-FM  82.5  NHK-FM      both  prefer=2  sdr=lgain=0,mgain=2,vgain=6    # 優先
```

この例では、YFMは常にAirspy、NHK-FMはYFMと重ならない時間帯はAirspy(高音質)、
重なる時間帯はradioSHARKで録音されます。

### 動作確認

```bash
./sdr_radio.sh 2 1 "SDRテスト" "テスト" 84.7 "FM-YOKOHAMA"   # 1分録音
./schedule_programs.py    # YFMの番組が [tuner2] で予約されるのを確認
```

## 5. 録音の停止

FM(shark)と同様に停止できます…と言いたいところですが、現状の
stop_recording.sh は arecord(shark)と radiko のみ対応です。SDR録音の停止対応は
実機での動作確認後に追加します(airspy-fmradionプロセスの停止と、ffmpegによる
ファイル仕上げの連携)。それまでは `pkill -f airspy-fmradion` で止められます
(ffmpeg側がファイルを閉じるので、そこまでの録音は残ります)。

## 高音質化のヒント

- **アンテナが最重要**。付属ダイポールより屋外FM八木(目的局の送信所方向)が
  数段上。FMヨコハマなら大山(伊勢原)方向。
- **FLAC保存** — `SDR_AUDIO_CODEC="flac"` で可逆圧縮アーカイブに。Podcast配信は
  AACのままにしたい等の使い分けは相談してください。
- **発熱対策** — Airspy Miniはかなり熱くなります。ヒートシンク貼り付け+
  風通しの良い配置で。
- 音質比較は同じ番組を radioSHARK と Airspy で同時録音すると分かりやすいです
  (stations.conf で同じ局を2エントリにはできないので、手動で
  `./sdr_radio.sh` と `./shark_radio.sh` を同時実行して比べてください)。
