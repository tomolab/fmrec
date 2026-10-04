#!/bin/bash
# sdr_gaintest.sh : SDRのゲイン設定を順に試して聴き比べ用のファイルを作る
#
# 指定した周波数で、ゲインの組み合わせを1つずつ短時間録音し、
# m4a に変換して比較用フォルダに並べる。ファイル名に設定値が入るので、
# 聴いて良かったものをそのまま stations.conf の sdr= に書けばよい。
#
# 使い方:
#   ./sdr_gaintest.sh <周波数MHz> [秒数] [出力先]
#   ./sdr_gaintest.sh 82.5              # 既定10秒、$OUTPUT_DIR/gaintest へ
#   ./sdr_gaintest.sh 84.7 15 /tmp/yfm  # 15秒ずつ /tmp/yfm へ
#
# 試す組み合わせは GAIN_SETS で定義(fmrec.conf で上書き可)。
# 「lgain,mgain,vgain[,追加オプション]」の形式。追加オプションには
# multipath=N / mono / filter=narrow も書ける(sdr_radio.sh と同じ書式)。
#
# 聴き方: 出力先が配信フォルダ配下なら make_podcast.py でフィードに載るので
# スマホで聴き比べられる。または scp で手元に持ってくる。

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${FMREC_CONF:-${SCRIPT_DIR}/fmrec.conf}"
[ -f "${CONF}" ] && source "${CONF}"
[ -n "${EXTRA_PATH:-}" ] && export PATH="${EXTRA_PATH}:${PATH}"

freq="${1:-}"
secs="${2:-10}"
outdir="${3:-${OUTPUT_DIR:-/tmp}/gaintest}"

if [ -z "${freq}" ]; then
    echo "使い方: $0 <周波数MHz> [秒数] [出力先]" >&2
    echo "  例: $0 82.5          # NHK-FM を10秒ずつ" >&2
    echo "      $0 84.7 15 /tmp/yfm" >&2
    exit 1
fi

fmradion="${SDR_FMRADION:-airspy-fmradion}"
command -v "${fmradion}" > /dev/null || {
    echo "airspy-fmradion がありません: ${fmradion}" >&2; exit 1; }

# 試すゲインの組み合わせ(fmrec.conf や環境変数 GAIN_SETS で上書き可)。
# 1行1組。スペース区切りでも可:
#   GAIN_SETS="5,3,5 5,3,5,multipath=0" ./sdr_gaintest.sh 82.5
# 低い方から高い方へ。過入力の局は前半、弱い局は後半が良いことが多い
if [ -z "${GAIN_SETS:-}" ]; then
    GAIN_SETS="0,2,6
2,4,10
5,3,5
5,6,10
8,8,12
12,10,14"
fi

srate="${SDR_SAMPLE_RATE:-3000000}"
freq_hz=$(python3 -c "print(int(float('${freq}') * 1e6))")
mkdir -p "${outdir}"
stamp=$(date +%H%M%S)

echo "周波数: ${freq} MHz / 各 ${secs}秒 / 出力: ${outdir}"
echo

n=0
while IFS= read -r gains <&3; do
    [ -n "${gains}" ] || continue
    n=$(( n + 1 ))
    # 「5,3,5」や「5,3,5,multipath=0,mono」を lgain=/mgain=/vgain= に展開
    gains="$(echo "${gains}" | tr -d '\r' | xargs)"   # 前後の空白・CRを除去
    IFS=',' read -ra parts <<< "${gains}"
    if [ "${#parts[@]}" -lt 3 ]; then
        echo "  (書式が不正のため飛ばします: ${gains})"
        continue
    fi
    l="${parts[0]}"; m="${parts[1]}"; v="${parts[2]}"
    dev_conf="freq=${freq_hz},srate=${srate},lgain=${l},mgain=${m},vgain=${v}"
    opts=()
    multipath="${SDR_MULTIPATH:-288}"
    for extra in "${parts[@]:3}"; do
        case "${extra}" in
            multipath=*) multipath="${extra#multipath=}" ;;
            mono)        opts+=(-M) ;;
            filter=*)    opts+=(-f "${extra#filter=}") ;;
            *)           dev_conf="${dev_conf},${extra}" ;;
        esac
    done
    [ "${multipath}" -gt 0 ] 2>/dev/null && opts+=(-E "${multipath}")

    label=$(echo "${gains}" | tr ',' '-')
    wav="${outdir}/.tmp_${n}.wav"
    out="${outdir}/gain_${freq}MHz_${stamp}_$(printf '%02d' ${n})_${label}.m4a"

    printf "[%d] lgain=%s mgain=%s vgain=%s %s ... " \
        "${n}" "${l}" "${m}" "${v}" "${opts[*]}"
    if timeout "${secs}" "${fmradion}" -t "${SDR_DEVICE_TYPE:-airspy}" -q \
            -c "${dev_conf}" "${opts[@]}" -W "${wav}" \
            < /dev/null > /dev/null 2>&1; then
        :
    fi
    if [ -s "${wav}" ]; then
        ffmpeg -loglevel error -i "${wav}" -c:a aac -b:a 256k \
            -metadata "title=gain ${label} (${freq}MHz)" \
            -metadata "artist=SDRゲイン比較" \
            -metadata "album=gaintest" "${out}" -y < /dev/null
        rm -f "${wav}"
        echo "OK → $(basename "${out}")"
    else
        echo "失敗(受信機が開けない・他の録音と競合?)"
        rm -f "${wav}"
    fi
    # デバイス解放を待つ(次の設定へ)
    sleep 2
done 3<<< "$(echo "${GAIN_SETS}" | tr ' ' '\n')"

echo
echo "完了: ${outdir}"
echo "聴き比べて良かったものを stations.conf の sdr= に書いてください。"
echo "  例: JOAK-FM  82.5  NHK-FM  both  prefer=1  sdr=lgain=5,mgain=3,vgain=5"
if [ "${outdir}" = "${OUTPUT_DIR:-}/gaintest" ]; then
    echo "(配信フォルダ配下ではないため、Podcastには載りません。"
    echo " スマホで聴くなら出力先に ${OUTPUT_DIR:-配信フォルダ} を指定して make_podcast.py を実行)"
fi
