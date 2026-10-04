#!/bin/bash
# radiko_seg_debug.sh : radikoの実セグメント1本を取得してffmpegで解析する
# 使い方: ./radiko_seg_debug.sh FMT
set -u
sid="${1:-FMT}"
AUTHKEY="bcd151073c03b352e1ef2fd66c32209da9ca0afa"
a1=$(curl -s -i -H "X-Radiko-App: pc_html5" -H "X-Radiko-App-Version: 0.0.1" \
    -H "X-Radiko-User: dummy_user" -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth1")
tok=$(echo "$a1"|tr -d '\r'|awk -F': ' 'tolower($1)=="x-radiko-authtoken"{print $2}')
off=$(echo "$a1"|tr -d '\r'|awk -F': ' 'tolower($1)=="x-radiko-keyoffset"{print $2}')
len=$(echo "$a1"|tr -d '\r'|awk -F': ' 'tolower($1)=="x-radiko-keylength"{print $2}')
pk=$(printf '%s' "$AUTHKEY"|dd bs=1 skip="$off" count="$len" 2>/dev/null|base64)
curl -s -o /dev/null -H "X-Radiko-AuthToken: $tok" -H "X-Radiko-Partialkey: $pk" \
    -H "X-Radiko-User: dummy_user" -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth2"

lsid=$(head -c 16 /dev/urandom|md5sum|cut -c1-32)
master="https://si-f-radiko.smartstream.ne.jp/so/playlist.m3u8?station_id=${sid}&l=15&lsid=${lsid}&type=b"
ml=$(curl -s -H "X-Radiko-AuthToken: $tok" "$master"|grep -o 'https://[^ ]*medialist[^ ]*'|head -1|tr -d '\r')
pl=$(curl -s -H "X-Radiko-AuthToken: $tok" "$ml")
seg=$(echo "$pl"|grep -E '^https?://.*\.aac'|head -1|tr -d '\r')
echo "== セグメントURL: ${seg}"

mkdir -p /tmp/segdbg
curl -s "$seg" -o /tmp/segdbg/s1.aac
echo "== ファイルサイズ: $(stat -c%s /tmp/segdbg/s1.aac) バイト"
echo "== 先頭16バイト(hex):"
od -A x -t x1 /tmp/segdbg/s1.aac | head -1
echo "== ffprobe(1セグメント単体):"
ffprobe -v error -show_entries format=format_name,duration:stream=codec_name,profile,sample_rate,channels,bit_rate \
    -of default=noprint_wrappers=1 /tmp/segdbg/s1.aac 2>&1
echo "== 2本取得して concat demuxer で結合できるか:"
seg2=$(echo "$pl"|grep -E '^https?://.*\.aac'|sed -n 2p|tr -d '\r')
curl -s "$seg2" -o /tmp/segdbg/s2.aac
printf "file '/tmp/segdbg/s1.aac'\nfile '/tmp/segdbg/s2.aac'\n" > /tmp/segdbg/list.txt
ffmpeg -loglevel error -f concat -safe 0 -i /tmp/segdbg/list.txt \
    -c copy -bsf:a aac_adtstoasc /tmp/segdbg/concat.m4a -y 2>&1 && \
    echo "concat結合OK: $(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1 /tmp/segdbg/concat.m4a)"
echo "== 単純catで結合した場合(現行方式・比較用):"
cat /tmp/segdbg/s1.aac /tmp/segdbg/s2.aac > /tmp/segdbg/cat.aac
ffmpeg -loglevel error -f aac -i /tmp/segdbg/cat.aac -c copy -bsf:a aac_adtstoasc /tmp/segdbg/cat.m4a -y 2>&1 | head -3
echo "(catでエラーが出てconcatで出なければ、concat方式が正解)"
