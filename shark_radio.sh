#!/bin/bash
# shark_radio.sh : radioSHARK2 録音スクリプト(2台統合・config対応版)
# 使い方: shark_radio.sh <tuner番号(0|1)> <minutes> <program_name> <artist> <freq> <station_name> [開始時刻YYYYMMDDHHMMSS]
# 開始時刻を渡すと、選局後「開始のPRE_ROLL_SECONDS秒前」まで正確に待って
# から録音を開始する(前番組の混入を最小化)。省略時は従来の45秒待ち。
#
# 録音は arecord の出力をパイプで ffmpeg に渡し、直接 m4a に
# リアルタイムエンコードする。中間 wav を作らないため、
# WAVの2GB制限(-f dat で約3時間)によるファイル分割が起きない。

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"

if [ ! -f "${CONF}" ]; then
    echo "設定ファイルがありません: ${CONF}" >&2
    echo "fmrec.conf.example をコピーして fmrec.conf を作成してください" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONF}"

# cron/systemd経由のatジョブは最小PATHで動くため、必要なパスを明示する
# (自前ビルドのffmpeg等が /usr/bin 以外にある環境への対応)
if [ -n "${EXTRA_PATH:-}" ]; then
    export PATH="${EXTRA_PATH}:${PATH}"
fi
for cmd in radio arecord ffmpeg; do
    if ! command -v "${cmd}" > /dev/null; then
        mkdir -p "${OUTPUT_DIR}"
        echo "$(date '+%F %T') 起動失敗: ${cmd} が見つかりません (PATH=${PATH})" \
            >> "${OUTPUT_DIR}/record_error.log"
        exit 127
    fi
done

tuner="${1}"
minutes="${2}"
program_name="${3}"
artist="${4}"
freq="${5}"
station_name="${6}"
start_ts="${7:-}"
amfm="FM"

# tuner番号からデバイスを引く
read -ra radio_devs <<< "${RADIO_DEVS}"
read -ra alsa_devs  <<< "${ALSA_DEVS}"
if [ "${tuner}" -ge "${#radio_devs[@]}" ]; then
    echo "tuner番号が不正です: ${tuner}" >&2
    exit 1
fi
shark_no="${radio_devs[$tuner]}"
alsa_dev="${alsa_devs[$tuner]}"

# --- ALSA自動ペアリング ---
# /dev/radioN と同一のUSB物理デバイスに属するALSAカードをsysfsから特定する。
# USBの列挙順が変わって radioSHARK / radioSHARK_1 の対応が入れ替わっても、
# 常に「選局した個体」の音声を録音できる。
# 無効化したい場合は fmrec.conf に ALSA_AUTOPAIR="no" を設定。
usb_dev_of() {
    # sysfsノードから、idVendor を持つ階層(=USBデバイス本体)まで遡る
    local p
    p=$(readlink -f "$1") || return 1
    while [ -n "${p}" ] && [ "${p}" != "/" ] && [ ! -e "${p}/idVendor" ]; do
        p=$(dirname "${p}")
    done
    [ -e "${p}/idVendor" ] && echo "${p}"
}

if [ "${ALSA_AUTOPAIR:-yes}" != "no" ]; then
    radio_name=$(basename "${shark_no}")
    radio_usb=$(usb_dev_of "/sys/class/video4linux/${radio_name}/device" \
                2>/dev/null || true)
    if [ -n "${radio_usb}" ]; then
        for c in /sys/class/sound/card*; do
            [ -e "${c}/device" ] || continue
            if [ "$(usb_dev_of "${c}/device" 2>/dev/null)" = "${radio_usb}" ]
            then
                card_id=$(cat "${c}/id")
                detected="hw:CARD=${card_id},DEV=0"
                if [ "${detected}" != "${alsa_dev}" ]; then
                    echo "自動ペアリング: ${shark_no} の音声は ${detected}" \
                         "(設定値 ${alsa_dev} から補正)"
                fi
                alsa_dev="${detected}"
                break
            fi
        done
    else
        echo "自動ペアリング失敗: 設定値 ${alsa_dev} をそのまま使用します"
    fi
fi

logfile="${OUTPUT_DIR}/record_error.log"
# --- カバーアート埋め込み ---
# FMREC_IMG(番組画像URL、schedule_programs.py が番組表から取得)があれば
# 画像をダウンロードして m4a に attached_pic として埋め込む。
# 失敗しても録音ファイルには影響しない(カバー無しのまま)。
embed_cover() {
    local target="$1"
    [ -n "${FMREC_IMG:-}" ] || return 0
    [ -s "${target}" ] || return 0
    local cov="${target}.cover"
    if ! curl -s --max-time 20 "${FMREC_IMG}" -o "${cov}" \
            || [ ! -s "${cov}" ]; then
        rm -f "${cov}"
        return 0
    fi
    local out="${target}.withcover.m4a"
    if ffmpeg -loglevel warning -i "${target}" -i "${cov}" \
            -map 0 -map 1:v -c copy -disposition:v:0 attached_pic \
            -movflags +faststart "${out}" </dev/null 2>> "${logfile}" \
            && [ -s "${out}" ]; then
        mv "${out}" "${target}"
    else
        rm -f "${out}"
    fi
    # 画像はRSSのエピソードアートワーク用に <録音名>.jpg としても保存する
    # (iOS純正Podcast等はm4a埋め込みでなくフィード側の画像を表示する)。
    # Appleのアートワーク要件(正方形・1400px以上)に合わせ、白背景の
    # 1400x1400に整形して保存。整形に失敗したら元画像のまま保存
    ffmpeg -loglevel error -i "${cov}" \
        -vf "scale=1400:1400:force_original_aspect_ratio=decrease,pad=1400:1400:(ow-iw)/2:(oh-ih)/2:white" \
        -frames:v 1 "${target%.*}.jpg" </dev/null 2>> "${logfile}" \
        || mv "${cov}" "${target%.*}.jpg" 2>/dev/null
    rm -f "${cov}"
}

mkdir -p "${OUTPUT_DIR}"

date_strings=$(date +"%Y年%m月%d日(%a)%H時%M分" --date "1 minutes")
# メタデータ(m4aのtitle/artist/album = Podcastの表示)には、schedule_programs.py が
# FMREC_TITLE/FMREC_ARTIST/FMREC_STATION で渡す元の表記(空白入り)を使う。
# 引数の program_name 等はファイル名用にサニタイズ済み(空白→_)なので、
# 環境変数が無いとき(手動実行など)のみそちらを使う
meta_title="${FMREC_TITLE:-${program_name}}"
meta_artist="${FMREC_ARTIST:-${artist}}"
meta_station="${FMREC_STATION:-${station_name}}"
title_strings="${meta_title} ${date_strings}"
filename="${program_name}_${station_name}_${amfm}${freq}MHz_${date_strings}"
fullfilename="${OUTPUT_DIR}/${filename}"

# 同名ファイルが既にある場合(同じ分内での再実行など)は連番を付与
if [ -e "${fullfilename}.m4a" ] || [ -e "${fullfilename}.recording.m4a" ]; then
    n=2
    while [ -e "${fullfilename}_${n}.m4a" ] || \
          [ -e "${fullfilename}_${n}.recording.m4a" ]; do
        n=$(( n + 1 ))
    done
    fullfilename="${fullfilename}_${n}"
fi

echo "tuner: ${tuner} (${shark_no} / ${alsa_dev})"
echo "Freq.: ${freq} / minutes: ${minutes}"
echo "title: ${title_strings} / artist: ${artist}"
echo "file : ${fullfilename}.m4a"

sleep 2s
radio -c "${shark_no}" -qf "${freq}"

endtime=$(( minutes * 60 ))

# 録音開始タイミング: 開始時刻が渡されていれば、その PRE_ROLL_SECONDS 秒前
# まで正確に待つ。渡されていなければ従来互換の45秒待ち。
pre_roll="${PRE_ROLL_SECONDS:-5}"
# 同一局の前番組から引き継ぐ側(FMREC_HANDOFF_NEXT=1)は、前番組が
# HANDOFF_LEAD_SECONDS 秒前に終了してデバイスを明け渡すため、通常の
# PRE_ROLL ではなく HANDOFF_START_SECONDS 秒前から録音する。
# mbuffer を入れる前は初期化中の取りこぼしで頭が欠けたが、パイプに
# バッファを挟んでからは定刻ちょうどでも頭から録れる。既定0秒。
# 環境によって頭が欠けるなら 3 などに増やす(その分、前番組の音が頭に入る)
if [ "${FMREC_HANDOFF_NEXT:-}" = "cross" ]; then
    # 前番組が別局: 周波数の切り替えが挟まるぶん、少し遅く始める
    pre_roll="${CROSS_HANDOFF_START_SECONDS:-${HANDOFF_START_SECONDS:-0}}"
elif [ "${FMREC_HANDOFF_NEXT:-}" = "1" ]; then
    pre_roll="${HANDOFF_START_SECONDS:-0}"
fi
if [ -n "${start_ts}" ]; then
    target=$(date -d "${start_ts:0:8} ${start_ts:8:2}:${start_ts:10:2}:${start_ts:12:2}" +%s)
    wait_sec=$(( target - pre_roll - $(date +%s) ))
    if [ "${wait_sec}" -gt 0 ]; then
        echo "録音開始まで ${wait_sec}秒 待機 (開始${pre_roll}秒前から録音)"
        sleep "${wait_sec}"
    fi
    endtime=$(( endtime + pre_roll ))
    # 同一局の次番組への引き継ぎ(FMREC_HANDOFF=1):
    # 定刻の HANDOFF_LEAD_SECONDS 秒前に録音を切り上げてデバイスを明け渡す
    # (この番組の末尾がその秒数だけ欠ける)。次番組はその後 HANDOFF_START_
    # SECONDS 秒前から録音を始めるので、差の秒数がデバイス解放と初期化の猶予
    if [ "${FMREC_HANDOFF:-}" = "cross" ]; then
        handoff_lead="${CROSS_HANDOFF_LEAD_SECONDS:-${HANDOFF_LEAD_SECONDS:-5}}"
        endtime=$(( endtime - handoff_lead ))
        echo "別局の次番組への引き継ぎ: 定刻の${handoff_lead}秒前に録音を終了します"
    elif [ "${FMREC_HANDOFF:-}" = "1" ]; then
        handoff_lead="${HANDOFF_LEAD_SECONDS:-5}"
        endtime=$(( endtime - handoff_lead ))
        echo "次番組への引き継ぎ: 定刻の${handoff_lead}秒前に録音を終了します"
    fi
else
    sleep 45s
fi

# 同一チューナーの前番組がまだ録音中(同一局の隣接番組引き継ぎ)の場合、
# デバイスの解放を待つ(最大90秒)。/proc/asound で非破壊に確認する。
card_name=$(echo "${alsa_dev}" | sed -n 's/.*CARD=\([^,]*\).*/\1/p')
if [ -n "${card_name}" ] && [ -e "/proc/asound/${card_name}/pcm0c/sub0/status" ]; then
    waited=0
    while grep -q "state: RUNNING" \
            "/proc/asound/${card_name}/pcm0c/sub0/status" 2>/dev/null; do
        if [ ${waited} -eq 0 ]; then
            echo "$(date '+%F %T') デバイス使用中(前番組の録音終了待ち) [tuner${tuner}]" >> "${logfile}"
        fi
        sleep 2
        waited=$(( waited + 2 ))
        if [ ${waited} -ge 90 ]; then
            echo "$(date '+%F %T') デバイス解放待ちタイムアウト [tuner${tuner}]" >> "${logfile}"
            break
        fi
    done
fi

# -f dat = 48kHz / 16bit / stereo。-t raw でヘッダ無しのまま
# ffmpeg に流し、リアルタイムで AAC エンコードする。
# フラグメントMP4(-movflags)で書くことで、録音途中のファイルも
# VLC/mpv 等でそのまま追いかけ再生できる(電源断にも強い)
# arecord → ffmpeg の間に大きなバッファを挟む(mbuffer があれば)。
# ffmpeg がフラグメント書き出しや外付けHDDへの書き込みで一瞬止まると、
# 既定のパイプ(64KB=約0.3秒分)が詰まって arecord がサンプルを取りこぼす
# (オーバーラン)。16MB(約80秒分)のバッファで吸収する。
# 無ければそのままパイプ(従来どおり)
if command -v mbuffer > /dev/null 2>&1; then
    pipe_buf() { mbuffer -q -m 16M; }
else
    pipe_buf() { cat; }
fi

arecord --buffer-size 50000 -D "${alsa_dev}" \
    -d "${endtime}" -f dat -t raw 2>> "${logfile}" \
| pipe_buf \
| ffmpeg -loglevel warning \
    -f s16le -ar 48000 -ac 2 -i - \
    -metadata "artist=${meta_artist}" \
    -metadata "title=${title_strings}" \
    -metadata "album=${meta_station}" \
        ${FMREC_DESC:+-metadata "comment=${FMREC_DESC}"} \
    -acodec aac -ab 256k -vn \
    -movflags frag_keyframe+empty_moov+default_base_moof \
    -frag_duration 10000000 \
    "${fullfilename}.recording.m4a"

rc=$?
# rc != 0 でもファイルに中身があれば「途中終了」として仕上げる
# (stop_recording.sh による手動停止や停電直前までの救済)
# 失敗判定はサイズだけでなく実測の長さでも行う(arecord が開けず
# ffmpeg がヘッダだけ書いた数百バイトのファイルを失敗として扱う)
rec_dur=$(ffprobe -v error -show_entries format=duration \
          -of default=noprint_wrappers=1:nokey=1 \
          "${fullfilename}.recording.m4a" 2>/dev/null | cut -d. -f1)
case "${rec_dur}" in ''|*[!0-9]*) rec_dur=0 ;; esac   # N/A や空なら 0 扱い
# 正常終了(rc=0)なのに実測が予定時間の SHORT_FAIL_RATIO(既定0.5)未満なら、
# 途中で音声が止まった(デバイス異常)とみなして失敗扱い。手動停止(rc!=0)は
# 対象外。短いファイルは削除せず OUTPUT_DIR/failed/ に退避する
short_fail=0
if [ "${rec_dur:-0}" -ge 5 ] && [ ${rc} -eq 0 ]; then
    min_dur=$(awk -v e="${endtime}" -v r="${SHORT_FAIL_RATIO:-0.5}" 'BEGIN{printf "%d", e*r}')
    [ "${rec_dur}" -lt "${min_dur}" ] && short_fail=1
fi
if [ ! -s "${fullfilename}.recording.m4a" ] || [ "${rec_dur:-0}" -lt 5 ] \
   || [ ${short_fail} -eq 1 ]; then
    echo "$(date '+%F %T') 録音失敗 [tuner${tuner}] rc=${rc} 長さ=${rec_dur:-0}秒(予定${endtime}秒): ${title_strings}" >> "${logfile}"
    if [ ${short_fail} -eq 1 ]; then
        echo "  (途中で音声が止まった可能性。ファイルは failed/ に退避)" >> "${logfile}"
        mkdir -p "${OUTPUT_DIR}/failed"
        mv -f "${fullfilename}.recording.m4a" \
            "${OUTPUT_DIR}/failed/$(basename "${fullfilename}").m4a" 2>/dev/null \
            || rm -f "${fullfilename}.recording.m4a"
    else
        rm -f "${fullfilename}.recording.m4a"
    fi
    exit 1
fi
if [ ${rc} -ne 0 ]; then
    echo "$(date '+%F %T') 録音が途中終了(手動停止?) [tuner${tuner}] rc=${rc}: ${title_strings}" >> "${logfile}"
fi

# 録音完了後、通常のMP4構造にリマックス(再エンコード無し・数秒で完了)
# 古いプレイヤーやスマホアプリとの互換性のため
ffmpeg -loglevel warning -i "${fullfilename}.recording.m4a" \
    -c copy -movflags +faststart "${fullfilename}.m4a" </dev/null
if [ -s "${fullfilename}.m4a" ]; then
    rm "${fullfilename}.recording.m4a"
else
    # リマックス失敗時はフラグメント版をそのまま残す(再生は可能)
    mv "${fullfilename}.recording.m4a" "${fullfilename}.m4a"
    echo "$(date '+%F %T') リマックス失敗(フラグメント版を保存) [tuner${tuner}]: ${title_strings}" >> "${logfile}"
fi
embed_cover "${fullfilename}.m4a"

# --- 録音完了後、Podcastフィードを更新(任意・失敗しても録音には影響しない) ---
# PODCAST_AUTO_FEED="no" で無効化できる
if [ "${PODCAST_AUTO_FEED:-yes}" != "no" ] \
   && [ -x "${SCRIPT_DIR}/make_podcast.py" ] \
   && [ -n "${PODCAST_BASE_URL:-}" ]; then
    "${SCRIPT_DIR}/make_podcast.py" > /dev/null 2>&1 || true
fi
