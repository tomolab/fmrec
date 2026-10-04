#!/bin/bash
# radiko_live.sh : radiko ライブ録音(自前実装・外部ツール非依存)
# 使い方: radiko_live.sh <radiko局ID> <minutes> <program_name> <artist> <station_name> [開始時刻YYYYMMDDHHMMSS]
#
# 仕組み: radiko公式Webプレイヤーと同じ認証フロー(auth1/auth2)で
# トークンを取得し、ストリームURLをradikoのXMLから動的に取得して
# ffmpegでHLSを保存する。URLを固定で持たないため仕様変更に比較的強い。
# 認証フローは rec_radiko_ts (https://github.com/uru2/rec_radiko_ts) と
# 同方式。radiko側の変更で動かなくなった場合は rec_radiko_ts の最新の
# 認証部と見比べて修正すること。
# 制限: ラジコプレミアム(エリアフリー)は未対応。自エリアの局のみ。

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"
[ -f "${CONF}" ] || { echo "設定ファイルがありません: ${CONF}" >&2; exit 1; }
# shellcheck source=/dev/null
source "${CONF}"
if [ -n "${EXTRA_PATH:-}" ]; then export PATH="${EXTRA_PATH}:${PATH}"; fi

sid="${1}"
minutes="${2}"
program_name="${3}"
artist="${4}"
station_name="${5}"
start_ts="${6:-}"

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

err() {
    echo "$(date '+%F %T') radikoライブ録音失敗 [${sid}]: $1" >> "${logfile}"
    echo "$1" >&2
    exit 1
}
command -v curl > /dev/null || err "curl が見つかりません"
command -v ffmpeg > /dev/null || err "ffmpeg が見つかりません (PATH=${PATH})"

# --- 認証 (auth1 -> partialkey -> auth2) ---
AUTHKEY="bcd151073c03b352e1ef2fd66c32209da9ca0afa"
auth1=$(curl -s -i \
    -H "X-Radiko-App: pc_html5" \
    -H "X-Radiko-App-Version: 0.0.1" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth1") || err "auth1 接続失敗"

token=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-authtoken"{print $2}')
offset=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-keyoffset"{print $2}')
length=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-keylength"{print $2}')
[ -n "${token}" ] && [ -n "${offset}" ] && [ -n "${length}" ] \
    || err "auth1 応答の解析失敗(仕様変更の可能性)"

partialkey=$(printf '%s' "${AUTHKEY}" | dd bs=1 "skip=${offset}" "count=${length}" 2>/dev/null | base64)

curl -s -o /dev/null \
    -H "X-Radiko-AuthToken: ${token}" \
    -H "X-Radiko-Partialkey: ${partialkey}" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth2" || err "auth2 失敗"

# --- ライブストリームURLの動的取得 ---
# timefree="0" areafree="0" の url_template を先頭から採用する
stream_xml=$(curl -s "https://radiko.jp/v3/station/stream/pc_html5/${sid}.xml") \
    || err "ストリームXML取得失敗"
# ライブ用URL候補(timefree=0, areafree=0)をすべて収集する。
# radikoのXMLには複数の配信URLが載っており、2025年以降の新仕様
# (子プレイリストが medialist 形式)はffmpegで再生できないため、
# 実際に取得検証して ffmpeg互換のものを選ぶ。
mapfile -t candidates < <(echo "${stream_xml}" \
    | tr '\n' ' ' \
    | grep -o '<url [^>]*>[[:space:]]*<playlist_create_url>[^<]*</playlist_create_url>' \
    | grep 'timefree="0"' \
    | grep 'areafree="0"' \
    | sed -e 's/.*<playlist_create_url>//' -e 's|</playlist_create_url>.*||')
if [ "${#candidates[@]}" -eq 0 ]; then
    mapfile -t candidates < <(echo "${stream_xml}" \
        | tr '\n' ' ' \
        | grep -o '<playlist_create_url>[^<]*' \
        | sed 's/<playlist_create_url>//')
fi
if [ "${#candidates[@]}" -eq 0 ]; then
    echo "$(date '+%F %T') ストリームXML解析失敗 [${sid}] 先頭400文字: $(echo "${stream_xml}" | head -c 400)" >> "${logfile}"
    err "ストリームURLの解析失敗(record_error.log にXMLの内容を記録しました)"
fi

lsid=$(head -c 16 /dev/urandom | md5sum | cut -c 1-32)
fetch_master() {
    curl -s --max-time 10 -H "X-Radiko-AuthToken: ${token}" "$1"
}
full_url=""
fallback_url=""
for base in "${candidates[@]}"; do
    for u in "${base}?station_id=${sid}&l=15&lsid=${lsid}&type=b" "${base}"; do
        body=$(fetch_master "${u}")
        echo "${body}" | head -1 | grep -q "#EXTM3U" || continue
        if echo "${body}" | grep -q "medialist"; then
            # 新仕様(ffmpeg非互換)。他候補が全滅した場合の最終手段として保持
            [ -z "${fallback_url}" ] && fallback_url="${u}"
            continue
        fi
        full_url="${u}"
        break 2
    done
done
if [ -z "${full_url}" ] && [ -n "${fallback_url}" ]; then
    echo "注意: ffmpeg互換の配信URLが見つからず、新仕様URLで試行します" >&2
    full_url="${fallback_url}"
fi
if [ -z "${full_url}" ]; then
    {
        echo "$(date '+%F %T') 全候補URLでプレイリスト取得失敗 [${sid}]"
        for base in "${candidates[@]}"; do
            echo "  候補: ${base}"
            echo "  応答: $(fetch_master "${base}" | head -c 150)"
        done
    } >> "${logfile}"
    err "プレイリスト取得失敗(record_error.log に候補と応答を記録しました)"
fi
echo "stream: ${full_url}"

# --- ファイル名・メタデータ ---
date_strings=$(date +"%Y年%m月%d日(%a)%H時%M分" --date "1 minutes")
# メタデータ(m4aのtitle/artist/album = Podcastの表示)には、schedule_programs.py が
# FMREC_TITLE/FMREC_ARTIST/FMREC_STATION で渡す元の表記(空白入り)を使う。
# 引数の program_name 等はファイル名用にサニタイズ済み(空白→_)なので、
# 環境変数が無いとき(手動実行など)のみそちらを使う
meta_title="${FMREC_TITLE:-${program_name}}"
meta_artist="${FMREC_ARTIST:-${artist}}"
meta_station="${FMREC_STATION:-${station_name}}"
title_strings="${meta_title} ${date_strings}"
fullfilename="${OUTPUT_DIR}/${program_name}_${station_name}_radiko_${date_strings}"
if [ -e "${fullfilename}.m4a" ] || [ -e "${fullfilename}.recording.m4a" ]; then
    n=2
    while [ -e "${fullfilename}_${n}.m4a" ] || \
          [ -e "${fullfilename}_${n}.recording.m4a" ]; do n=$(( n + 1 )); done
    fullfilename="${fullfilename}_${n}"
fi

endtime=$(( minutes * 60 ))

# 開始時刻が渡されていれば、その PRE_ROLL_SECONDS 秒前まで待機
pre_roll="${PRE_ROLL_SECONDS:-5}"
# radikoの配信は実放送より15〜25秒ほど遅れて届くため、録音窓全体を
# 遅延分だけ後ろへずらす(開始を遅らせれば終了も同じだけ遅れる)。
# これで頭の前番組混入が減り、番組の尻が録り足しの中に確実に収まる
radiko_delay="${RADIKO_DELAY_SECONDS:-20}"
if [ -n "${start_ts}" ]; then
    target=$(date -d "${start_ts:0:8} ${start_ts:8:2}:${start_ts:10:2}:${start_ts:12:2}" +%s)
    wait_sec=$(( target - pre_roll + radiko_delay - $(date +%s) ))
    if [ "${wait_sec}" -gt 0 ]; then
        echo "録音開始まで ${wait_sec}秒 待機"
        sleep "${wait_sec}"
    fi
    endtime=$(( endtime + pre_roll ))
fi

echo "radikoライブ録音: ${sid} ${minutes}分 -> ${fullfilename}.m4a"

# セグメント一時ディレクトリ(1本ずつダウンロードする作業用)
segdir=$(mktemp -d "${TMPDIR:-/tmp}/radiko_live.XXXXXX")
cleanup() { rm -rf "${segdir}"; }
trap cleanup EXIT

# 停止要求フラグ。stop_recording.sh からの SIGTERM/SIGINT を受けたら
# 現在のセグメントまでで録音を打ち切り、m4a化まで完遂する。
stop_requested=0
on_stop() {
    stop_requested=1
    echo "停止要求を受信。現在の内容で録音を仕上げます..."
}
trap on_stop TERM INT

# --- 録音方式の選択 ---
# 新仕様(medialist)のプレイリストは一部ffmpegのHLSクライアントと
# 相性問題があるため、プレイリスト巡回とセグメント取得をcurlで自前実装する。
# 各AACセグメントは先頭にID3タグを持つため、それを剥がして純ADTSにし、
# 育っていく .recording.aac に逐次追記する。ADTSは追記可能なので
# 録音中でも mpv 等で頭から追いかけ再生できる。録音後にm4aへ変換する。
# 旧仕様URLが選ばれた場合は従来どおりffmpegに直接HLSを渡す。

# ID3v2タグを取り除いて純粋なADTSストリームを標準出力へ出す
# (ID3ヘッダ: "ID3"+version2+flags1+synchsafe-size4 = 10バイト+本体)
strip_id3() {
    python3 - "$1" <<'PYEOF'
import sys
data = open(sys.argv[1], "rb").read()
off = 0
# 連続するID3v2タグをすべてスキップ(通常は先頭に1つ)
while data[off:off+3] == b"ID3" and len(data) >= off + 10:
    size = ((data[off+6] & 0x7f) << 21 | (data[off+7] & 0x7f) << 14 |
            (data[off+8] & 0x7f) << 7 | (data[off+9] & 0x7f))
    off += 10 + size
sys.stdout.buffer.write(data[off:])
PYEOF
}

record_via_chunks() {
    # $1: マスタープレイリストURL / $2: 追記先ADTSファイル
    local master medialist end_epoch playlist seg tmp idx=0
    local outfile="$2"
    master=$(fetch_master "$1")
    medialist=$(echo "${master}" | grep -o 'https\?://[^ ]*medialist[^ ]*' \
                | head -1 | tr -d '\r')
    if [ -z "${medialist}" ]; then
        echo "$(date '+%F %T') medialist URL抽出失敗 [${sid}]" >> "${logfile}"
        return 1
    fi
    end_epoch=$(( $(date +%s) + endtime ))
    local -A seen=()
    local first=1 all_segs total skip i2
    while [ "$(date +%s)" -lt "${end_epoch}" ] \
          && [ "${stop_requested}" -eq 0 ]; do
        playlist=$(curl -s --max-time 10 \
            -H "X-Radiko-AuthToken: ${token}" "${medialist}")
        if ! echo "${playlist}" | head -1 | grep -q "#EXTM3U"; then
            # セッション失効: マスターを取り直して継続
            master=$(fetch_master "$1")
            medialist=$(echo "${master}" \
                | grep -o 'https\?://[^ ]*medialist[^ ]*' | head -1 | tr -d '\r')
            sleep 2
            continue
        fi
        if [ "${first}" -eq 1 ]; then
            # ライブエッジ追従: radikoのプレイリストは接続時点で過去60秒前後
            # のセグメントを含む。全部保存するとファイル頭に古い放送が入り、
            # その分だけ番組の尻が録り足しからはみ出て欠けるため、初回は
            # 末尾2セグメント(約10秒)だけ取り込み、それ以前は捨てる
            all_segs=$(echo "${playlist}" | grep -E '^https?://.*\.aac')
            total=$(echo "${all_segs}" | grep -c . || true)
            skip=$(( total - 2 )); [ "${skip}" -lt 0 ] && skip=0
            i2=0
            while IFS= read -r seg; do
                [ -n "${seg}" ] || continue
                i2=$(( i2 + 1 ))
                if [ "${i2}" -le "${skip}" ]; then
                    seen[${seg}]=1
                fi
            done <<< "${all_segs}"
            first=0
        fi
        while IFS= read -r seg; do
            [ -n "${seg}" ] || continue
            if [ -z "${seen[${seg}]:-}" ]; then
                seen[${seg}]=1
                tmp="${segdir}/seg.aac"
                if curl -s --max-time 15 "${seg}" -o "${tmp}" \
                        && [ -s "${tmp}" ]; then
                    # ID3タグを剥がして育成中のADTSファイルへ追記
                    strip_id3 "${tmp}" >> "${outfile}"
                    idx=$(( idx + 1 ))
                fi
            fi
        done < <(echo "${playlist}" | grep -E '^https?://.*\.aac')
        # 4秒待機(停止要求が来たら即座に抜ける)
        for _ in 1 2 3 4; do
            [ "${stop_requested}" -eq 1 ] && break
            sleep 1
        done
    done
    [ "${idx}" -gt 0 ]  # 1本も取れなければ失敗
}

if echo "${full_url}" | grep -q "so/playlist.m3u8"; then
    # 新仕様: セグメントをADTSで逐次追記(録音中も再生可能)→ 完了後m4a化
    adts="${fullfilename}.recording.aac"
    : > "${adts}"
    if record_via_chunks "${full_url}" "${adts}"; then
        ffmpeg -loglevel warning -f aac -i "${adts}" \
            -acodec copy -vn -bsf:a aac_adtstoasc \
            -metadata "artist=${meta_artist}" \
            -metadata "title=${title_strings}" \
            -metadata "album=${meta_station}" \
        ${FMREC_DESC:+-metadata "comment=${FMREC_DESC}"} \
            -movflags +faststart \
            "${fullfilename}.m4a" </dev/null 2>> "${logfile}"
        [ -s "${fullfilename}.m4a" ] && rm -f "${adts}"
    else
        echo "$(date '+%F %T') セグメント取得失敗 [${sid}]: ${title_strings}" >> "${logfile}"
    fi
else
    # 旧仕様: ffmpegに直接HLSを渡す(フラグメントMP4で追いかけ再生可能)
    ffmpeg -loglevel warning \
        -headers "X-Radiko-Authtoken: ${token}"$'\r\n' \
        -i "${full_url}" \
        -t "${endtime}" \
        -acodec copy -vn -bsf:a aac_adtstoasc \
        -metadata "artist=${meta_artist}" \
        -metadata "title=${title_strings}" \
        -metadata "album=${meta_station}" \
        ${FMREC_DESC:+-metadata "comment=${FMREC_DESC}"} \
        -movflags frag_keyframe+empty_moov+default_base_moof \
        -frag_duration 10000000 \
        "${fullfilename}.recording.m4a" </dev/null 2>> "${logfile}"
    # 通常MP4へリマックス
    if [ -s "${fullfilename}.recording.m4a" ]; then
        ffmpeg -loglevel warning -i "${fullfilename}.recording.m4a" \
            -c copy -movflags +faststart "${fullfilename}.m4a" </dev/null
        [ -s "${fullfilename}.m4a" ] && rm -f "${fullfilename}.recording.m4a" \
            || mv "${fullfilename}.recording.m4a" "${fullfilename}.m4a"
    fi
fi

embed_cover "${fullfilename}.m4a"

if [ ! -s "${fullfilename}.m4a" ]; then
    err "録音データが空です: ${title_strings}"
fi

# --- 録音完了後、Podcastフィードを更新(任意・失敗しても録音には影響しない) ---
# PODCAST_AUTO_FEED="no" で無効化できる
if [ "${PODCAST_AUTO_FEED:-yes}" != "no" ] \
   && [ -x "${SCRIPT_DIR}/make_podcast.py" ] \
   && [ -n "${PODCAST_BASE_URL:-}" ]; then
    "${SCRIPT_DIR}/make_podcast.py" > /dev/null 2>&1 || true
fi
