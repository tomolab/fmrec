#!/bin/bash
# radiko_debug.sh : 新仕様プレイリスト(medialist)の応答調査用
# 使い方: ./radiko_debug.sh FMT
# 出力をそのまま貼り付けてもらう想定。認証トークンは伏せ字で表示する。

set -u
sid="${1:-FMT}"

# --- 認証(radiko_live.shと同じ) ---
AUTHKEY="bcd151073c03b352e1ef2fd66c32209da9ca0afa"
auth1=$(curl -s -i \
    -H "X-Radiko-App: pc_html5" \
    -H "X-Radiko-App-Version: 0.0.1" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth1")
token=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-authtoken"{print $2}')
offset=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-keyoffset"{print $2}')
length=$(echo "${auth1}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-radiko-keylength"{print $2}')
partialkey=$(printf '%s' "${AUTHKEY}" | dd bs=1 "skip=${offset}" "count=${length}" 2>/dev/null | base64)
curl -s -o /dev/null \
    -H "X-Radiko-AuthToken: ${token}" \
    -H "X-Radiko-Partialkey: ${partialkey}" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    "https://radiko.jp/v2/api/auth2"
echo "== 認証OK (token: ${token:0:6}...) =="

lsid=$(head -c 16 /dev/urandom | md5sum | cut -c 1-32)
master_url="https://si-f-radiko.smartstream.ne.jp/so/playlist.m3u8?station_id=${sid}&l=15&lsid=${lsid}&type=b"

echo
echo "== [1] マスタープレイリストの全文 =="
master=$(curl -s -H "X-Radiko-AuthToken: ${token}" "${master_url}")
echo "${master}"

medialist=$(echo "${master}" | grep -o 'https://[^ ]*medialist[^ ]*' | head -1 | tr -d '\r')
echo
echo "== medialist URL: ${medialist}"

probe() {
    # $1=ラベル $2=URL $3=トークン(空なら無し)
    echo
    echo "== [$1] =="
    local hdr=()
    [ -n "$3" ] && hdr=(-H "X-Radiko-AuthToken: $3")
    local resp meta body
    resp=$(curl -s -w '\n@@HTTP_CODE:%{http_code} CONTENT_TYPE:%{content_type}@@' \
           --max-time 10 "${hdr[@]}" "$2")
    meta=$(echo "${resp}" | grep -o '@@HTTP_CODE.*@@')
    body=$(echo "${resp}" | sed 's/@@HTTP_CODE.*@@//')
    echo "${meta}"
    echo "${body}" | head -c 500
    echo
}

probe "2: medialist トークン有り" "${medialist}" "${token}"
probe "3: medialist トークン無し" "${medialist}" ""
probe "4: medialist cython無し・トークン有り" "${medialist%%&cython=1*}" "${token}"
probe "5: medialist +lsid・トークン有り" "${medialist}&lsid=${lsid}" "${token}"
