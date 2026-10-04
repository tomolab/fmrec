#!/bin/bash
# sdr_radio.sh : SDR(Airspy Mini等) + airspy-fmradion によるFM録音
#
# shark_radio.sh と同じ引数インターフェースを持ち、schedule_programs.py の
# RECORD_SCRIPT/SDR_RECORD_SCRIPT として差し替え可能。
# 復調は airspy-fmradion (https://github.com/jj1bdx/airspy-fmradion)。
# マルチパスフィルタ・高品質ステレオ復調により、radioSHARK等の
# ハードウェアチューナーを上回る音質を狙える。
#
# 使い方:
#   sdr_radio.sh <tuner番号> <minutes> <番組名> <artist> <freq(MHz)> <局名> [開始時刻YYYYMMDDHHMMSS]
#
# fmrec.conf の関連設定:
#   SDR_FMRADION       : airspy-fmradion のパス(既定: PATH上のairspy-fmradion)
#   SDR_DEVICE_TYPE    : fmradionの-tオプション(既定: airspy。rtlsdrも可)
#   SDR_DEVICE_CONF    : fmradionの-cオプション(ゲイン等。例: "lgain=8,mgain=8,vgain=10")
#                        stations.conf の sdr= 指定(FMREC_SDR_CONF)があればそちらが優先
#   SDR_SAMPLE_RATE    : IFサンプルレート(既定: 3000000。Airspy Miniは3M/6M、R2は10M)
#   SDR_MULTIPATH      : マルチパスフィルタ段数(-E、既定: 288。0で無効)
#   SDR_VERBOSE        : yes で fmradion の警告をログに残す(切り分け用)
#   SDR_HANDOFF_LEAD_SECONDS  : 同一局連続録音での早じまい秒数(SDR用。
#                        未設定なら共通の HANDOFF_LEAD_SECONDS)
#   SDR_HANDOFF_START_SECONDS : 同じく次番組の前倒し秒数(SDR用)
#   SDR_KILL_GRACE_SECONDS    : 録音終了時、TERMで終わらない fmradion を
#                        強制終了するまでの猶予秒(既定3)
#   SDR_FADEIN_SECONDS : 録音冒頭のフェードイン秒数(既定0.3。起動直後の
#                        クリップノイズ対策。0 で無効)
#   SDR_AUTO_USBRESET  : 録音失敗時に Airspy を USB リセットする(既定 yes)。
#                        ハングからの自動復旧用(sdr_usbreset.py が必要)
#   SHORT_FAIL_RATIO   : 予定時間に対して実測がこの割合未満なら録音失敗と
#                        みなす(既定 0.5)。途中でストリームが止まった
#                        (Airspyハング等)短いファイルを成功扱いしないため。
#                        該当ファイルは OUTPUT_DIR/failed/ に退避する
#   SDR_AUDIO_CODEC    : 出力コーデック aac(既定) / flac
#   SDR_AAC_BITRATE    : AACビットレート(既定: 256k)
#   PRE_ROLL_SECONDS   : 開始時刻指定時、開始何秒前から録るか(既定: 10)
#   PODCAST_AUTO_FEED  : 録音後にフィード更新するか(既定: yes)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"
if [ ! -f "${CONF}" ]; then
    echo "設定ファイルがありません: ${CONF}" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONF}"

# cron/systemd経由でも自前ビルドのツールが見えるようにPATHを補う
if [ -n "${EXTRA_PATH:-}" ]; then
    export PATH="${EXTRA_PATH}:${PATH}"
fi

for cmd in ffmpeg; do
    command -v "${cmd}" > /dev/null || {
        echo "必要コマンドがありません: ${cmd}" >&2; exit 1; }
done

fmradion="${SDR_FMRADION:-airspy-fmradion}"
command -v "${fmradion}" > /dev/null || {
    echo "airspy-fmradion がありません: ${fmradion}" >&2
    echo "(fmrec.conf の SDR_FMRADION でパスを指定するか、ビルドして" >&2
    echo " インストールしてください。手順は SDR.md 参照)" >&2
    exit 1; }

if [ $# -lt 6 ]; then
    echo "使い方: $0 <tuner> <minutes> <番組名> <artist> <freq> <局名> [開始時刻]" >&2
    exit 1
fi

tuner="$1"
minutes="$2"
program_name="$3"
artist="$4"
freq="$5"
station_name="$6"
start_ts="${7:-}"

logfile="${OUTPUT_DIR}/sdr_radio.log"
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


# --- 出力ファイル名(shark_radio.sh と同一の命名規則) ---
date_strings=$(date +"%Y年%m月%d日(%a)%H時%M分" --date "1 minutes")
# メタデータ(m4aのtitle/artist/album = Podcastの表示)には、schedule_programs.py が
# FMREC_TITLE/FMREC_ARTIST/FMREC_STATION で渡す元の表記(空白入り)を使う。
# 引数の program_name 等はファイル名用にサニタイズ済み(空白→_)なので、
# 環境変数が無いとき(手動実行など)のみそちらを使う
meta_title="${FMREC_TITLE:-${program_name}}"
meta_artist="${FMREC_ARTIST:-${artist}}"
meta_station="${FMREC_STATION:-${station_name}}"
title_strings="${meta_title} ${date_strings}"
filename="${program_name}_${station_name}_FM${freq}MHz_${date_strings}"
fullfilename="${OUTPUT_DIR}/${filename}"

if [ -e "${fullfilename}.m4a" ] || [ -e "${fullfilename}.recording.m4a" ]; then
    n=2
    while [ -e "${fullfilename}_${n}.m4a" ] || \
          [ -e "${fullfilename}_${n}.recording.m4a" ]; do
        n=$(( n + 1 ))
    done
    fullfilename="${fullfilename}_${n}"
fi

echo "tuner: ${tuner} (SDR / ${SDR_DEVICE_TYPE:-airspy})"
echo "Freq.: ${freq}MHz / minutes: ${minutes}"
echo "title: ${title_strings} / artist: ${artist}"
echo "file : ${fullfilename}.m4a"

endtime=$(( minutes * 60 ))

# --- 録音開始タイミング(shark_radio.sh と同じ待機ロジック) ---
pre_roll="${PRE_ROLL_SECONDS:-5}"
# 同一局の前番組から引き継ぐ側(FMREC_HANDOFF_NEXT=1)は、前番組が
# HANDOFF_LEAD_SECONDS 秒前に終了してデバイスを明け渡すため、通常の
# PRE_ROLL ではなく HANDOFF_START_SECONDS 秒前から録音する。
# mbuffer を入れる前は初期化中の取りこぼしで頭が欠けたが、パイプに
# バッファを挟んでからは定刻ちょうどでも頭から録れる。既定0秒。
# 環境によって頭が欠けるなら 3 などに増やす(その分、前番組の音が頭に入る)
if [ "${FMREC_HANDOFF_NEXT:-}" = "cross" ]; then
    # 前番組が別局: 周波数の切り替えが挟まるぶん、少し遅く始める
    pre_roll="${CROSS_HANDOFF_START_SECONDS:-${SDR_HANDOFF_START_SECONDS:-0}}"
elif [ "${FMREC_HANDOFF_NEXT:-}" = "1" ]; then
    # SDR は前番組の終了処理(fmradionのTERM→終了、ハング時はKILLまで最大10秒)
    # に時間がかかるため、shark と別の値を設定できる
    # (SDR_HANDOFF_START_SECONDS が未設定なら共通の HANDOFF_START_SECONDS)
    pre_roll="${SDR_HANDOFF_START_SECONDS:-${HANDOFF_START_SECONDS:-0}}"
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
        handoff_lead="${CROSS_HANDOFF_LEAD_SECONDS:-${SDR_HANDOFF_LEAD_SECONDS:-5}}"
        endtime=$(( endtime - handoff_lead ))
        echo "別局の次番組への引き継ぎ: 定刻の${handoff_lead}秒前に録音を終了します"
    elif [ "${FMREC_HANDOFF:-}" = "1" ]; then
        handoff_lead="${SDR_HANDOFF_LEAD_SECONDS:-${HANDOFF_LEAD_SECONDS:-5}}"
        endtime=$(( endtime - handoff_lead ))
        echo "次番組への引き継ぎ: 定刻の${handoff_lead}秒前に録音を終了します"
    fi
fi

# --- 録音冒頭のフェードイン ---
# airspy-fmradion は起動直後の数秒、DCオフセットやフィルタが収束するまで
# 出力が乱れ、ピークがフルスケールを超えて「プチッ」というクリップノイズに
# なることがある(実測: 冒頭2秒だけ max_volume 0.0dB、以降 -2.7dB)。
# 冒頭に短いフェードインをかけて目立たなくする。PRE_ROLL 区間なので実害なし。
# SDR_FADEIN_SECONDS="0" で無効化
fade_secs="${SDR_FADEIN_SECONDS:-0.3}"
fade_opt=""
if [ "${fade_secs}" != "0" ]; then
    fade_opt="afade=t=in:st=0:d=${fade_secs}"
fi

# --- 出力コーデックの選択 ---
codec="${SDR_AUDIO_CODEC:-aac}"
case "${codec}" in
    flac)
        audio_opts=(-acodec flac)
        ext="m4a"   # flac in m4a コンテナ(Podcast互換のためm4a固定)
        ;;
    *)
        audio_opts=(-acodec aac -ab "${SDR_AAC_BITRATE:-256k}")
        ext="m4a"
        ;;
esac

# --- airspy-fmradion による受信・復調 ---
# 出力: 48kHz/16bit/stereo のraw PCM を標準出力 → ffmpeg でエンコード
# -t : デバイス種別 (airspy / rtlsdr / airspyhf ...)
# -c : デバイス設定 (freq=Hz単位, ゲイン等)
# -M : マルチパスフィルタ(段数指定、0で無効)
# -E : (デバイスにより)その他オプションは SDR_FMRADION_OPTS で追加可能
freq_hz=$(python3 -c "print(int(float('${freq}') * 1e6))")
dev_type="${SDR_DEVICE_TYPE:-airspy}"
# サンプルレート: Airspy Mini は 6M/3M のみ(既定の10Mは R2 用)。
# 3M で FM放送の復調には十分(必要帯域は200kHz程度)。6M はマルチパスフィルタ
# と併用するとCPUが追いつかず処理落ち(ザラザラ雑音)することがある
# Mini なら 6000000、R2 なら 10000000、rtlsdr は空(既定)にする
srate="${SDR_SAMPLE_RATE:-3000000}"
# デバイス設定(ゲイン等): 局別指定(FMREC_SDR_CONF、stations.conf の sdr=)が
# あればそれを、無ければ conf の SDR_DEVICE_CONF(共通設定)を使う
sdr_conf="${FMREC_SDR_CONF:-${SDR_DEVICE_CONF:-}}"
multipath="${SDR_MULTIPATH:-288}"

# sdr_conf(カンマ区切り)のうち、以下は fmradion の個別オプションに変換し、
# 残り(lgain= 等)はデバイス設定(-c)にそのまま渡す:
#   multipath=N   : マルチパスフィルタ段数(-E)。0 で無効。弱い局(遠距離)は
#                   フィルタが収束せず逆効果になるので 0 が良いことがある
#   mono          : ステレオ復調を無効化(-M)。弱い局のSN改善に
#   filter=narrow : 帯域を絞る(-f narrow / medium / wide)。遠距離受信の定石
extra_opts=()
dev_pairs=()
IFS=',' read -ra _parts <<< "${sdr_conf}"
for kv in "${_parts[@]}"; do
    [ -n "${kv}" ] || continue
    case "${kv}" in
        multipath=*) multipath="${kv#multipath=}" ;;
        mono)        extra_opts+=(-M) ;;
        filter=*)    extra_opts+=(-f "${kv#filter=}") ;;
        *)           dev_pairs+=("${kv}") ;;
    esac
done
dev_pairs_str=$(IFS=','; echo "${dev_pairs[*]}")
dev_conf="freq=${freq_hz}${srate:+,srate=${srate}}${dev_pairs_str:+,${dev_pairs_str}}"

# airspy-fmradion のオプション:
#   -t 種別 / -q 静音 / -c デバイス設定
#   -E N   : マルチパスフィルタ(段数)。0 なら付けない
#   -M     : モノラル / -f : 帯域フィルタ(上の sdr_conf 指定から)
#   -R -   : raw PCM (s16le 48kHz stereo) を標準出力へ
# SDR_VERBOSE=yes で -q を外し、fmradion のサンプルドロップ等の警告を
# sdr_radio.log に残す(無音・ノイズの切り分け用。通常は静音)
if [ "${SDR_VERBOSE:-no}" = "yes" ]; then
    fmradion_args=(-t "${dev_type}" -c "${dev_conf}")
else
    fmradion_args=(-t "${dev_type}" -q -c "${dev_conf}")
fi
if [ "${multipath}" -gt 0 ] 2>/dev/null; then
    fmradion_args+=(-E "${multipath}")
fi
if [ ${#extra_opts[@]} -gt 0 ]; then
    fmradion_args+=("${extra_opts[@]}")
fi
fmradion_args+=(-R -)
# 追加オプション(任意)
if [ -n "${SDR_FMRADION_OPTS:-}" ]; then
    # shellcheck disable=SC2206
    fmradion_args+=(${SDR_FMRADION_OPTS})
fi

# 同一局の前番組(隣接)がまだ Airspy を使っている場合、解放を待つ(最大90秒)。
# 前番組は定刻5秒前に TERM を受けるが、終了処理に数秒かかる(ハング時は
# timeout -k により10秒後に KILL)。前番組が「途中から録音」で定刻を越えて
# いるケースもあるため余裕を持つ。開けずに失敗するより、待って頭が
# 欠ける方がよい
fm_name="$(basename "${fmradion}")"
waited=0
while pgrep -x "${fm_name}" > /dev/null 2>&1; do
    if [ ${waited} -eq 0 ]; then
        echo "$(date '+%F %T') Airspy使用中(前番組の終了待ち) [tuner${tuner}/SDR]" >> "${logfile}"
    fi
    sleep 1
    waited=$(( waited + 1 ))
    if [ ${waited} -ge 90 ]; then
        echo "$(date '+%F %T') Airspy解放待ちタイムアウト、開始を試みます [tuner${tuner}/SDR]" >> "${logfile}"
        break
    fi
done
[ ${waited} -gt 0 ] && echo "$(date '+%F %T') Airspy解放を確認(${waited}秒待機) [tuner${tuner}/SDR]" >> "${logfile}"

echo "$(date '+%F %T') 録音開始 [tuner${tuner}/SDR] ${title_strings}" >> "${logfile}"

# timeout で録音時間を制御(fmradion自体は無限に受信し続けるため)。
# TERM で止めれば ffmpeg 側がファイルを正しく閉じる。
# fmradion → ffmpeg の間に大きなバッファを挟む(mbuffer があれば)。
# ffmpeg がフラグメント書き出しや外付けHDDへの書き込みで一瞬止まると、
# 既定のパイプ(64KB=約0.3秒分)が詰まって fmradion がサンプルを取りこぼし、
# 周期的な小さなノイズ(フィルタありだと同調外れ状態に発散)の原因になる。
# 16MB(約80秒分)のバッファで吸収する。無ければそのままパイプ。
if command -v mbuffer > /dev/null 2>&1; then
    pipe_buf() { mbuffer -q -m 16M; }
else
    pipe_buf() { cat; }
fi

# -k N: TERM を送って N 秒以内に終わらなければ KILL する。airspy-fmradion は
# USB転送のキャンセル待ちでまれに終了処理がハングする(libairspy 1.0.12でも
# 間欠的に発生)。ハングすると次の予約がデバイスを開けなくなるため、
# 強制終了で確実に手放す。KILL でも ffmpeg は入力EOFで仕上げるので録音は残る。
# 正常終了は1〜2秒で完了するので既定3秒。SDR1台構成では次番組が待たされる
# ことになるため、HANDOFF_LEAD より十分小さい値にしておく
kill_grace="${SDR_KILL_GRACE_SECONDS:-3}"
timeout --signal=TERM -k "${kill_grace}" "${endtime}s" \
    "${fmradion}" "${fmradion_args[@]}" 2>> "${logfile}" \
| pipe_buf \
| ffmpeg -loglevel warning \
    -f s16le -ar 48000 -ac 2 -i - \
    ${fade_opt:+-af "${fade_opt}"} \
    -metadata "artist=${meta_artist}" \
    -metadata "title=${title_strings}" \
    -metadata "album=${meta_station}" \
        ${FMREC_DESC:+-metadata "comment=${FMREC_DESC}"} \
    "${audio_opts[@]}" -vn \
    -movflags frag_keyframe+empty_moov+default_base_moof \
    -frag_duration 10000000 \
    "${fullfilename}.recording.${ext}"

rc=$?
# timeout による正常終了は 124。手動killは 143 等。KILL 経由は 137。
# (自分の fmradion は timeout -k が確実に終わらせる。他の sdr_radio.sh の
#  fmradion を巻き込む恐れがあるため、ここでの一括掃除はしない)
# 失敗判定: ファイルが無い/空、または音声がほぼ入っていない(デバイスが開けず
# ffmpeg がヘッダだけ書いたケース)。受信機が開けないと数百バイトの m4a が
# できて「サイズ>0」をすり抜けるため、実測の長さで判定する
rec_dur=$(ffprobe -v error -show_entries format=duration \
          -of default=noprint_wrappers=1:nokey=1 \
          "${fullfilename}.recording.${ext}" 2>/dev/null | cut -d. -f1)
case "${rec_dur}" in ''|*[!0-9]*) rec_dur=0 ;; esac   # N/A や空なら 0 扱い
# 「開けたが途中でストリームが止まった」ケース(Airspyハングの典型。数分の
# ファイルができて 5秒判定をすり抜ける)も失敗にする: 予定どおり終了
# (rc=124 or 0)したのに実測が予定時間の SHORT_FAIL_RATIO 未満。手動停止
# (rc=143等)は意図した短縮なので対象外。短いファイルは削除せず failed/ に退避
short_fail=0
short_ratio="${SHORT_FAIL_RATIO:-0.5}"
if [ "${rec_dur:-0}" -ge 5 ] && { [ ${rc} -eq 124 ] || [ ${rc} -eq 0 ]; }; then
    min_dur=$(awk -v e="${endtime}" -v r="${short_ratio}" 'BEGIN{printf "%d", e*r}')
    if [ "${rec_dur}" -lt "${min_dur}" ]; then
        short_fail=1
    fi
fi
if [ ! -s "${fullfilename}.recording.${ext}" ] || [ "${rec_dur:-0}" -lt 5 ] \
   || [ ${short_fail} -eq 1 ]; then
    echo "$(date '+%F %T') 録音失敗 [tuner${tuner}/SDR] rc=${rc} 長さ=${rec_dur:-0}秒(予定${endtime}秒): ${title_strings}" >> "${logfile}"
    if [ ${short_fail} -eq 1 ]; then
        echo "  (途中でストリームが止まった可能性: 受信機のハングを疑う。ファイルは failed/ に退避)" >> "${logfile}"
        mkdir -p "${OUTPUT_DIR}/failed"
        mv -f "${fullfilename}.recording.${ext}" \
            "${OUTPUT_DIR}/failed/$(basename "${fullfilename}").${ext}" 2>/dev/null \
            || rm -f "${fullfilename}.recording.${ext}"
    else
        echo "  (受信機が開けなかった可能性: 他プロセスの占有・権限・USB接続を確認)" >> "${logfile}"
        rm -f "${fullfilename}.recording.${ext}"
    fi
    # Airspy がハングして「開けるがストリームが来ない」状態になることがある
    # (実例: 9/25 19時から2日近く全滅、物理的な抜き差しで復旧)。
    # 他の録音プロセスが Airspy を使っていなければ、USB リセット(抜き差し相当)
    # を自動で試みる。SDR_AUTO_USBRESET="no" で無効化
    if [ "${SDR_AUTO_USBRESET:-yes}" = "yes" ] \
       && ! pgrep -x "$(basename "${fmradion}")" > /dev/null 2>&1 \
       && [ -x "${SCRIPT_DIR}/sdr_usbreset.py" ]; then
        echo "$(date '+%F %T') Airspy の USB リセットを試みます [tuner${tuner}/SDR]" >> "${logfile}"
        if "${SCRIPT_DIR}/sdr_usbreset.py" >> "${logfile}" 2>&1; then
            echo "$(date '+%F %T') USB リセット完了(次の録音から復旧の見込み) [tuner${tuner}/SDR]" >> "${logfile}"
        else
            echo "$(date '+%F %T') USB リセット失敗。物理的な抜き差しが必要かもしれません [tuner${tuner}/SDR]" >> "${logfile}"
        fi
    fi
    exit 1
fi
if [ ${rc} -ne 0 ] && [ ${rc} -ne 124 ]; then
    echo "$(date '+%F %T') 録音が途中終了(手動停止?) [tuner${tuner}/SDR] rc=${rc}: ${title_strings}" >> "${logfile}"
fi

# 通常MP4へリマックス(faststart)
ffmpeg -loglevel warning -i "${fullfilename}.recording.${ext}" \
    -c copy -movflags +faststart "${fullfilename}.${ext}" </dev/null
if [ -s "${fullfilename}.${ext}" ]; then
    rm "${fullfilename}.recording.${ext}"
else
    mv "${fullfilename}.recording.${ext}" "${fullfilename}.${ext}" \
        2>/dev/null || true
fi
embed_cover "${fullfilename}.${ext}"

echo "$(date '+%F %T') 録音完了 [tuner${tuner}/SDR] ${title_strings}" >> "${logfile}"

# --- 録音完了後、Podcastフィードを更新(任意・失敗しても録音には影響しない) ---
if [ "${PODCAST_AUTO_FEED:-yes}" != "no" ] \
   && [ -x "${SCRIPT_DIR}/make_podcast.py" ] \
   && [ -n "${PODCAST_BASE_URL:-}" ]; then
    "${SCRIPT_DIR}/make_podcast.py" > /dev/null 2>&1 || true
fi
