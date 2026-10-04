#!/bin/bash
# check_tuners.sh : /dev/radioN と ALSAカードの対応関係を確認する
# 2台の radioSHARK を「別々の局」に選局し、各ALSAカードから5秒ずつ
# 録音して聴き比べることで、正しいペアリングを特定する。
#
# 使い方: ./check_tuners.sh <周波数A> <周波数B>
# 例:     ./check_tuners.sh 80.0 81.3
#   → /dev/radio0 を 80.0、/dev/radio1 を 81.3 に選局し、
#     各カードの5秒サンプルを /tmp/tuner_check/ に保存する

set -u
FREQ_A="${1:?周波数を2つ指定してください 例: $0 80.0 81.3}"
FREQ_B="${2:?周波数を2つ指定してください 例: $0 80.0 81.3}"

OUT=/tmp/tuner_check
mkdir -p "$OUT"; rm -f "$OUT"/*.wav

# 録音中は選局を変えてしまい進行中の録音を壊すため、実行を拒否する
if pgrep -f "arecord.*radioSHARK" > /dev/null; then
    echo "エラー: 録音が進行中です。このテストは両チューナーの選局を" >&2
    echo "変更するため、録音中の実行は進行中の録音を破壊します。" >&2
    echo "録音終了後(atq と pgrep -af arecord で確認)に再実行してください。" >&2
    exit 1
fi

echo "== 選局: /dev/radio0 -> ${FREQ_A}MHz, /dev/radio1 -> ${FREQ_B}MHz =="
radio -c /dev/radio0 -qf "${FREQ_A}"
radio -c /dev/radio1 -qf "${FREQ_B}"
sleep 3

echo "== ALSAカード一覧 =="
arecord -l | grep -E "^カード|^card"

for card in radioSHARK radioSHARK_1; do
    f="$OUT/card_${card}.wav"
    echo "== hw:CARD=${card} から5秒録音 -> ${f} =="
    if arecord -D "hw:CARD=${card},DEV=0" -f dat -d 5 "$f" 2>/dev/null; then
        # 音量(RMS)を表示: ほぼ無音なら選局失敗かミュートの可能性
        lvl=$(ffmpeg -i "$f" -af volumedetect -f null - 2>&1 \
              | grep mean_volume | awk '{print $5, $6}')
        echo "   平均音量: ${lvl:-測定不可}"
    else
        echo "   録音失敗(デバイス使用中の可能性。録音停止後に再実行を)"
    fi
done

echo
echo "== 判定方法 =="
echo "aplay ${OUT}/card_radioSHARK.wav    # を再生して聴く"
echo "aplay ${OUT}/card_radioSHARK_1.wav"
echo "・radioSHARK   から ${FREQ_A}MHz の局が聴こえる → 現在の fmrec.conf のままで正しい"
echo "・radioSHARK   から ${FREQ_B}MHz の局が聴こえる → 対応が逆。fmrec.conf の"
echo "  ALSA_DEVS の2つの並び順を入れ替えてください"
