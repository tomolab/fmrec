#!/bin/bash
# radiko_timefree.sh : radiko タイムフリー取得ラッパー(rec_radiko_ts 連携)
# 使い方: radiko_timefree.sh <radiko局ID> <開始YYYYMMDDHHMMSS> <終了YYYYMMDDHHMMSS> \
#                          <program_name> <artist> <station_name>
# schedule_programs.py から「放送終了後」の時刻で at 起動される。
# ライブ録音ではなくタイムフリーから完全版を取得するため、
# 放送中に予約が入った番組でも頭から欠落なく保存できる。
# rec_radiko_ts: https://github.com/uru2/rec_radiko_ts (各自で導入してください)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"
[ -f "${CONF}" ] || { echo "設定ファイルがありません: ${CONF}" >&2; exit 1; }
# shellcheck source=/dev/null
source "${CONF}"
if [ -n "${EXTRA_PATH:-}" ]; then export PATH="${EXTRA_PATH}:${PATH}"; fi

sid="${1}"
ft="${2}"      # 番組開始 YYYYMMDDHHMMSS
to="${3}"      # 番組終了 YYYYMMDDHHMMSS
program_name="${4}"
artist="${5}"
station_name="${6}"

logfile="${OUTPUT_DIR}/record_error.log"

# --- カバーアート埋め込み(番組画像 FMREC_IMG があれば) ---
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

if [ -z "${RADIKO_TF_SCRIPT:-}" ] || [ ! -x "${RADIKO_TF_SCRIPT}" ]; then
    echo "$(date '+%F %T') radiko取得失敗: RADIKO_TF_SCRIPT が未設定または実行不可" >> "${logfile}"
    exit 1
fi

# ラジコプレミアム(エリアフリー)を使う場合は fmrec.conf に設定
[ -n "${RADIKO_MAIL:-}" ] && export RADIKO_MAIL
[ -n "${RADIKO_PASSWORD:-}" ] && export RADIKO_PASSWORD

# ファイル名の日時は番組の実開始時刻から生成(FM/radikoライブと同じ
# 「YYYY年MM月DD日(曜)HH時MM分」形式に統一)
wd=$(date -d "${ft:0:8}" +%a 2>/dev/null || echo "")
d="${ft:0:4}年${ft:4:2}月${ft:6:2}日${wd:+(${wd})}${ft:8:2}時${ft:10:2}分"
fullfilename="${OUTPUT_DIR}/${program_name}_${station_name}_radiko_${d}"
if [ -e "${fullfilename}.m4a" ]; then
    n=2
    while [ -e "${fullfilename}_${n}.m4a" ]; do n=$(( n + 1 )); done
    fullfilename="${fullfilename}_${n}"
fi
tmpfile="${fullfilename}.tmp.m4a"

echo "radikoタイムフリー取得: ${sid} ${ft} - ${to} -> ${fullfilename}.m4a"

# rec_radiko_ts は -f/-t に YYYYMMDDHHMM(SS) を受け付ける
"${RADIKO_TF_SCRIPT}" -s "${sid}" -f "${ft}" -t "${to}" -o "${tmpfile}"

if [ ! -s "${tmpfile}" ]; then
    echo "$(date '+%F %T') radiko取得失敗 [${sid} ${ft}]: ${program_name}" >> "${logfile}"
    exit 1
fi

# メタデータ付与(再エンコード無し)
# メタデータ(m4aのtitle/artist/album = Podcastの表示)には、schedule_programs.py が
# FMREC_TITLE/FMREC_ARTIST/FMREC_STATION で渡す元の表記(空白入り)を使う。
# 引数の program_name 等はファイル名用にサニタイズ済み(空白→_)なので、
# 環境変数が無いとき(手動実行など)のみそちらを使う
meta_title="${FMREC_TITLE:-${program_name}}"
meta_artist="${FMREC_ARTIST:-${artist}}"
meta_station="${FMREC_STATION:-${station_name}}"
title_strings="${meta_title} ${d}"
ffmpeg -loglevel warning -i "${tmpfile}" -c copy \
    -metadata "artist=${meta_artist}" \
    -metadata "title=${title_strings}" \
    -metadata "album=${meta_station}" \
        ${FMREC_DESC:+-metadata "comment=${FMREC_DESC}"} \
    -movflags +faststart "${fullfilename}.m4a" </dev/null
if [ -s "${fullfilename}.m4a" ]; then
    rm "${tmpfile}"
else
    mv "${tmpfile}" "${fullfilename}.m4a"
fi

embed_cover "${fullfilename}.m4a"

# --- 録音完了後、Podcastフィードを更新(任意・失敗しても録音には影響しない) ---
# PODCAST_AUTO_FEED="no" で無効化できる
if [ "${PODCAST_AUTO_FEED:-yes}" != "no" ] \
   && [ -x "${SCRIPT_DIR}/make_podcast.py" ] \
   && [ -n "${PODCAST_BASE_URL:-}" ]; then
    "${SCRIPT_DIR}/make_podcast.py" > /dev/null 2>&1 || true
fi
