#!/bin/bash
# stop_recording.sh : 実行中の録音を停止する(FM電波 + radikoライブ 両対応)
# 使い方:
#   stop_recording.sh              # 実行中の録音を一覧表示
#   stop_recording.sh 0            # tuner0 のFM録音を停止
#   stop_recording.sh radiko       # radikoライブ録音を全て停止
#   stop_recording.sh radiko FMT   # radikoライブ録音のうちFMTを停止
#   stop_recording.sh all          # 全ての録音を停止
#
# FM録音は arecord のみを終了させ、ffmpeg がファイルを閉じて仕上げる。
# radikoライブ録音は SIGTERM を送り、スクリプトが現在のセグメントまでで
# 録音を打ち切って m4a 化(番組情報メタデータ付き)まで完遂する。

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"
if [ ! -f "${CONF}" ]; then
    echo "設定ファイルがありません: ${CONF}" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONF}"

# SDR単体構成では ALSA_DEVS を設定しないので、未定義でも動くようにする
read -ra alsa_devs <<< "${ALSA_DEVS:-}"
# チューナー種別(shark/airspy)。省略時は全て shark。
# 例: TUNER_TYPES="shark airspy" → tuner0=radioSHARK, tuner1=Airspy(SDR)
read -ra tuner_types <<< "${TUNER_TYPES:-}"
n_tuners=${#alsa_devs[@]}
[ "${#tuner_types[@]}" -gt "${n_tuners}" ] && n_tuners=${#tuner_types[@]}
tuner_type() {   # $1: tuner番号 → shark / airspy
    local t="${tuner_types[$1]:-shark}"
    echo "${t}"
}
# tuner番号に対応する録音プロセスのPIDを返す
tuner_pids() {
    local i="$1"
    if [ "$(tuner_type "$i")" = "shark" ]; then
        # コマンドライン先頭が arecord のプロセスに限定(誤マッチ防止)
        pgrep -f "^arecord .*${alsa_devs[$i]}" || true
    else
        # SDR: プロセス名の完全一致で airspy-fmradion を探す。
        # 複数SDRの区別は現状不可(1台運用を想定)
        pgrep -x "airspy-fmradion" || true
    fi
}
tuner_label() {
    local i="$1"
    if [ "$(tuner_type "$i")" = "shark" ]; then
        echo "${alsa_devs[$i]}"
    else
        echo "$(tuner_type "$i") SDR"
    fi
}

# 予約台帳から録音中エントリを削除するヘルパー
SCHEDULE_LIB="${SCRIPT_DIR}/schedule_lib.py"
remove_from_schedule() {
    # $1: --tuner または --radiko / $2: 値
    [ -x "${SCHEDULE_LIB}" ] || return 0
    FMREC_SCHEDULE_LOG="${SCRIPT_DIR}/registered.log" \
        "${SCHEDULE_LIB}" remove-active "$1" "$2"
}

# --- 一覧表示 ---
# 台帳(registered.log)から「いま録音中」の番組名を引く。
# $1: チューナー識別子(FMは番号 "0"/"1"、radikoは "R")
# $2: (radikoのみ)局ID。prog_idの先頭と突き合わせる
title_from_log() {
    local tuner="$1" sid="${2:-}"
    local log="${SCRIPT_DIR}/registered.log"
    [ -f "${log}" ] || return 0
    local now
    now=$(date +%Y%m%d%H%M%S)
    awk -F'\t' -v tuner="${tuner}" -v sid="${sid}" -v now="${now}" '
        $2 == tuner && $3 <= now && now < $4 {
            if (sid == "" || index($1, sid "_") == 1) { print $6; exit }
        }' "${log}"
}

list_recordings() {
    echo "[FM電波録音]"
    local i
    for (( i=0; i<n_tuners; i++ )); do
        local pids title
        pids=$(tuner_pids "${i}")
        if [ -n "${pids}" ]; then
            title=$(title_from_log "${i}")
            echo "  tuner${i} ($(tuner_label "${i}")) : 録音中 ${title:+「${title}」}(PID: ${pids})"
        else
            echo "  tuner${i} ($(tuner_label "${i}")) : 待機中"
        fi
    done
    echo "[radikoライブ録音]"
    local rk
    rk=$(pgrep -af "radiko_live.sh" | grep -v "stop_recording" || true)
    if [ -n "${rk}" ]; then
        echo "${rk}" | while read -r pid rest; do
            # コマンドラインから局IDを抽出し、台帳から番組名を引く
            local sid title
            sid=$(echo "${rest}" | awk '{for(i=1;i<=NF;i++) if($i ~ /radiko_live.sh$/){print $(i+1); exit}}')
            title=$(title_from_log "R" "${sid}")
            echo "  radiko [${sid:-?}] : 録音中 ${title:+「${title}」}(PID: ${pid})"
        done
    else
        echo "  (なし)"
    fi
}

# --- FM録音停止 ---
stop_tuner() {
    local i="$1"
    local pids
    pids=$(tuner_pids "${i}")
    if [ -z "${pids}" ]; then
        echo "tuner${i} は録音していません"
        return
    fi
    echo "tuner${i} ($(tuner_label "${i}")) の録音を停止します (PID: ${pids})"
    # shark: arecord を止めるとパイプ下流の ffmpeg が閉じて仕上げへ進む。
    # SDR: airspy-fmradion(timeout配下)を TERM で止めると同様に仕上げへ進む。
    # airspy-fmradion は TERM で終了しないことがある(USBストリーム停止待ちで
    # 固まる)ため、5秒待って生きていれば KILL する。KILL でも ffmpeg は
    # 入力EOFでファイルを閉じるので、そこまでの録音は残る
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null
    local waited=0 alive
    while [ ${waited} -lt 5 ]; do
        sleep 1; waited=$(( waited + 1 ))
        alive=""
        for p in ${pids}; do kill -0 "$p" 2>/dev/null && alive="${alive} $p"; done
        [ -z "${alive}" ] && break
    done
    if [ -n "${alive}" ]; then
        echo "  TERMで終了しないため強制終了します (PID:${alive})"
        # shellcheck disable=SC2086
        kill -9 ${alive} 2>/dev/null
        sleep 1
    fi
    echo "停止しました。ファイルはそこまでの内容で仕上げられます"
    # 台帳から削除 → 放送時間内なら次回 schedule_programs.py で再録音される
    remove_from_schedule --tuner "${i}"
}

# --- radikoライブ録音停止 ---
# $1: 局ID(省略時は全radikoライブ録音)
stop_radiko() {
    local filter="${1:-}"
    local pids=()
    while read -r pid rest; do
        [ -z "${pid}" ] && continue
        if [ -n "${filter}" ]; then
            # 指定局IDのプロセスだけに絞る
            echo "${rest}" | grep -q "radiko_live.sh ${filter} " || continue
        fi
        pids+=("${pid}")
    done < <(pgrep -af "radiko_live.sh" | grep -v "stop_recording" || true)

    if [ "${#pids[@]}" -eq 0 ]; then
        echo "停止対象のradikoライブ録音はありません${filter:+ (${filter})}"
        return
    fi
    echo "radikoライブ録音を停止します (PID: ${pids[*]})"
    # SIGTERM を送る。スクリプト側が現在の内容で録音を仕上げる
    kill -TERM "${pids[@]}"
    # m4a化の完了を待つ(セグメント結合に少し時間がかかる)
    echo "録音の仕上げを待機中..."
    for _ in $(seq 1 20); do
        sleep 1
        # 対象プロセスが全て終了したら抜ける
        local alive=0
        for p in "${pids[@]}"; do
            kill -0 "${p}" 2>/dev/null && alive=1
        done
        [ "${alive}" -eq 0 ] && break
    done
    echo "停止しました。番組情報付きのm4aとして仕上げられます"
    # 台帳から削除 → 放送時間内なら次回 schedule_programs.py で再録音される
    remove_from_schedule --radiko "${filter}"
}

# --- 引数処理 ---
case "${1:-}" in
    "")
        list_recordings
        ;;
    all)
        for (( i=0; i<n_tuners; i++ )); do stop_tuner "$i"; done
        stop_radiko
        ;;
    radiko)
        stop_radiko "${2:-}"
        ;;
    [0-9])
        if [ "$1" -ge "${n_tuners}" ]; then
            echo "tuner番号が不正です: $1" >&2; exit 1
        fi
        stop_tuner "$1"
        ;;
    *)
        echo "使い方: $0 [tuner番号 | radiko [局ID] | all]" >&2
        exit 1
        ;;
esac
