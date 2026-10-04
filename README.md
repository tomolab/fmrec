# fmrec — FM/radiko 番組表検索・自動録音 & Podcast配信システム

radikoの番組表を毎日取得してキーワード検索し、マッチした番組を自動録音します。
録音は **radioSHARK2(FM電波)** と **radiko(ライブ/タイムフリー)** の両方に対応。
録音したファイルは **Podcast** として配信でき、スマホのポッドキャストアプリで聴けます。

Linux(Ubuntu / Raspberry Pi)向け。個人利用を目的とした自作ツールです。

---

## 主な機能

- **番組表検索と自動予約** — キーワードにマッチする番組を`at`で自動予約
- **2系統の録音** — radioSHARK2による電波録音と、radikoによる録音(電波が届かない局や
  二重化のバックアップに)
- **radikoハイブリッド録音** — 放送前〜放送中はライブ録音、放送済みはタイムフリー取得に
  自動で切り替え
- **2チューナー自動割り当て** — radioSHARK2を2台使い、時間が重なる番組を自動で振り分け
- **FM連続番組の自動連結** — 「(1)(2)(3)」のように時間帯で分割された番組を、電波録音では
  1本の連続録音にまとめる(radikoは番組表どおり分割で取得)
- **録音の停止と再録音** — 手動で停止でき、停止した番組は次回実行で録り直せる
- **番組情報の埋め込み** — 番組内容・出演者をファイルのメタデータに記録し、Podcastの
  説明欄に表示
- **Podcast配信** — RSSフィードを自動生成し、Range対応の軽量サーバーで配信。
  FM/radikoの重複を排除、放送日時の新しい順に整列

---

## 全体の流れ

```
 systemdタイマー(毎日) → schedule_programs.py
                          ├ radiko番組表XMLを取得(今日+明日)
                          ├ keywords.txt でタイトル・出演者・番組内容を検索
                          └ マッチした番組を at で録音予約
                                ↓ 番組開始の少し前に起動
        ┌──────────────────────┴──────────────────────┐
   shark_radio.sh(FM電波)                    radiko_live.sh / radiko_timefree.sh
   radioコマンドで選局 → arecord            radiko認証 → ストリーム取得
   → ffmpegでm4a化                          → m4a化
        └──────────────────────┬──────────────────────┘
                                ↓ 録音完了
                          make_podcast.py → RSSフィード生成(feed.xml ほか)
                                ↓
                          podcast_server.py(Range対応HTTP配信)
                                ↓
                          スマホのポッドキャストアプリで購読・再生
```

---

## ドキュメント

初めての方は以下の順に読むのがおすすめです。

| ドキュメント | 内容 |
|---|---|
| **SETUP.md** | インストールマニュアル(ゼロから配信開始まで、Airspy含む) |
| **USAGE.md** | 日々の使い方(録音予約・停止・再録音・Podcast購読) |
| **CONFIG.md** | 設定ファイルの全項目リファレンス |
| **PODCAST.md** | Podcast配信とスマホでの視聴、外出先からの視聴(Tailscale) |
| **TROUBLESHOOTING.md** | よくある問題と対処 |
| systemd/INSTALL.md | systemdでの常駐化・自動実行の設定 |
| **SDR.md** | Airspy Mini による高音質FM録音のセットアップ |

---

## 必要な環境

- Linux(Ubuntu 20.04.6 LTS / Python 3.8 で動作確認。22.04 以降や Raspberry Pi OS も想定)
- Python 3.8 以上
- FM受信機(電波録音を使う場合): Airspy Mini(SDR、高音質)または radioSHARK2 USBチューナー。併用も可
- 以下のコマンド: `at`, `arecord`(alsa-utils), `ffmpeg`, `radio`(radioSHARK制御), `curl`
- radikoタイムフリー取得を使う場合: [rec_radiko_ts](https://github.com/uru2/rec_radiko_ts)

詳しくは SETUP.md を参照してください。

---

## インストール

```bash
git clone https://github.com/<あなたのアカウント>/fmrec.git
cd fmrec
./install.sh              # パッケージ導入・設定ファイルの雛形作成・(任意)systemd常駐化
./install_sdr.sh          # Airspy(SDR)を使う場合のみ: ツールチェーン構築
```

`install.sh` は対話的に進みます(`--yes` で全部承認、`--systemd` で常駐化まで)。
終わったら設定を編集します:

```bash
nano fmrec.conf        # OUTPUT_DIR(録音先)、チューナー、PODCAST_BASE_URL
nano stations.conf     # 受信する局(自分のエリアの行の # を外す)
nano keywords.txt      # 録りたい番組名

./schedule_programs.py # 予約が並べば OK
./tuner_status.py      # 状況表示
```

手動で進めたい場合や詳細は SETUP.md を参照してください。

---

## ファイル構成

### 実行スクリプト

| ファイル | 役割 |
|---|---|
| `install.sh` | セットアップ(パッケージ・設定雛形・systemd) |
| `install_sdr.sh` | Airspy(SDR)のツールチェーン構築 |
| `schedule_programs.py` | 番組表検索と録音予約(中核) |
| `shark_radio.sh` | FM電波録音(radioSHARK2) |
| `radiko_live.sh` | radikoライブ録音 |
| `radiko_timefree.sh` | radikoタイムフリー取得(rec_radiko_ts を呼ぶ) |
| `stop_recording.sh` | 録音の停止(FM/radiko) |
| `tuner_status.py` | チューナー使用状況の表示 |
| `schedule_lib.py` | 台帳操作ヘルパー(stop_recording から使用) |
| `make_podcast.py` | Podcast RSSフィード生成 |
| `podcast_server.py` | Range対応・マルチスレッドHTTP配信サーバー |
| `make_stations.py` | stations.conf の雛形生成 |
| `sdr_radio.sh` | SDR(Airspy Mini)によるFM録音(airspy-fmradion使用) |
| `sdr_gaintest.sh` | SDRのゲイン設定を順に試して聴き比べる |
| `sdr_usbreset.py` | Airspy を USB リセット(ハング時の自動復旧に使用) |
| `make_icons.py` | Podcastアイコン(ラジオ画像)の生成 |
| `fetch_logos.py` | radikoから局ロゴを取得(局別フィードのアイコン用) |

### 設定ファイル

| ファイル | 役割 |
|---|---|
| `fmrec.conf` | 共通設定(要編集。`.example` からコピー) |
| `stations.conf` | 受信局の定義(局ID・周波数・録音方式) |
| `keywords.txt` | 録りたい番組・出演者のキーワード |

### その他

| ファイル | 役割 |
|---|---|
| `systemd/` | systemdユニット(自動実行・常駐) |
| `registered.log` | 予約台帳(自動生成。編集不要) |
| 診断スクリプト各種 | `check_tuners.sh`, `radiko_debug.sh` ほか |

---

## ライセンス

MIT License(予定)。個人利用を目的としたツールです。

radiko の録音は個人利用の範囲で行ってください。録音物の再配布や商用提供は
radiko の利用規約に抵触する可能性があります。

`fetch_logos.py` で取得する局ロゴは各放送局の著作物・商標です。取得したロゴ画像は
個人利用(自分のPodcastアプリでの表示)の範囲で使い、再配布はしないでください
(このリポジトリにも含めていません。`.gitignore` で除外されます)。
