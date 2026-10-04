#!/usr/bin/env python3
"""
podcast_server.py : Podcast配信用の軽量HTTPサーバー
標準の python -m http.server の問題を解決する:
  - HTTP Range リクエスト対応(206 Partial Content)
    → ポッドキャストアプリの頭出し・シークが速くなる
  - マルチスレッド対応
    → 複数エピソードの並行ダウンロードで詰まらない

使い方:
  ./podcast_server.py                # OUTPUT_DIR を 8080 で配信(fmrec.confから)
  ./podcast_server.py --port 8888    # ポート指定
  ./podcast_server.py --dir /path    # ディレクトリ指定

設定は fmrec.conf の OUTPUT_DIR / PODCAST_PORT を参照する。
"""

import argparse
import base64
import hmac
import json
import os
import re
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONF_PATH = Path(os.environ.get("FMREC_CONF", BASE_DIR / "fmrec.conf"))


def load_conf(path):
    conf = {}
    if not path.exists():
        return conf
    pat = re.compile(
        r'^([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|(\S*))\s*(?:#.*)?$')
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = pat.match(line)
        if m:
            conf[m.group(1)] = m.group(2) if m.group(2) is not None \
                else m.group(3)
    return conf


class RangeHandler(SimpleHTTPRequestHandler):
    """Range リクエストに対応した SimpleHTTPRequestHandler。
    Tailscale Funnel 等でインターネットに公開する場合のために、
    Basic認証(PODCAST_AUTH_USER/PASS)と秘匿パス(PODCAST_URL_PREFIX)に対応。"""

    # main() で設定される(クラス属性)
    auth_user = ""
    auth_pass = ""
    url_prefix = ""      # 例: "/x9k2m4pq"(先頭スラッシュ付き、末尾なし)

    def _check_access(self):
        """認証・秘匿パスの検査。通過なら True、拒否なら応答済みで False。
        通過時は self.path から秘匿パスを剥がして通常の解決に渡す。"""
        # 秘匿パス: 指定があれば、そのプレフィックスで始まらないURLは404
        if self.url_prefix:
            if self.path == self.url_prefix:
                self.path = "/"
            elif self.path.startswith(self.url_prefix + "/"):
                self.path = self.path[len(self.url_prefix):]
            else:
                self.send_error(404)
                return False
        # Basic認証: ユーザー名が設定されていれば必須
        if self.auth_user:
            hdr = self.headers.get("Authorization", "")
            ok = False
            if hdr.startswith("Basic "):
                try:
                    dec = base64.b64decode(hdr[6:]).decode("utf-8")
                    u, _, p = dec.partition(":")
                    ok = (hmac.compare_digest(u, self.auth_user)
                          and hmac.compare_digest(p, self.auth_pass))
                except Exception:
                    ok = False
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fmrec"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return True

    # --- 設定API(index.html のキーワード編集フォームから使う) ---
    # 書き込みを伴うため、Basic認証が設定されているときだけ有効にする
    keyword_file = ""
    schedule_script = ""
    podcast_script = ""
    _schedule_lock = threading.Lock()
    _podcast_lock = threading.Lock()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_enabled(self):
        if not self.auth_user:
            self._json(403, {"error": "設定APIは Basic認証(PODCAST_AUTH_USER)を"
                                      "有効にした場合のみ使えます"})
            return False
        return True

    status_script = ""

    def _handle_api_get(self):
        if self.path == "/api/status":
            if not self._api_enabled():
                return True
            if not self.status_script or not Path(self.status_script).exists():
                self._json(500, {"error": "tuner_status.py が見つかりません"})
                return True
            try:
                r = subprocess.run([sys.executable, self.status_script],
                                   capture_output=True, text=True, timeout=60)
                self._json(200, {"text": (r.stdout + r.stderr).strip()})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "タイムアウト"})
            return True
        if self.path == "/api/keywords":
            if not self._api_enabled():
                return True
            try:
                text = Path(self.keyword_file).read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            self._json(200, {"text": text, "path": self.keyword_file})
            return True
        return False

    def _handle_api_post(self):
        if self.path == "/api/keywords":
            if not self._api_enabled():
                return True
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 200_000:
                self._json(413, {"error": "大きすぎます"})
                return True
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                text = str(data.get("text", ""))
            except Exception:
                self._json(400, {"error": "JSONの解釈に失敗"})
                return True
            kw = Path(self.keyword_file)
            try:
                if kw.exists():
                    kw.with_suffix(".txt.bak").write_text(
                        kw.read_text(encoding="utf-8"), encoding="utf-8")
                if text and not text.endswith("\n"):
                    text += "\n"
                kw.write_text(text, encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": f"書き込み失敗: {e}"})
                return True
            self._json(200, {"ok": True})
            return True

        if self.path == "/api/podcast":
            if not self._api_enabled():
                return True
            if not self.podcast_script or not Path(self.podcast_script).exists():
                self._json(500, {"error": "make_podcast.py が見つかりません"})
                return True
            if not self._podcast_lock.acquire(blocking=False):
                self._json(409, {"error": "フィード生成を実行中です"})
                return True
            try:
                r = subprocess.run([sys.executable, self.podcast_script],
                                   capture_output=True, text=True, timeout=300)
                out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
                self._json(200, {"ok": r.returncode == 0, "output": out[-20000:]})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "タイムアウト(300秒)"})
            finally:
                self._podcast_lock.release()
            return True

        if self.path == "/api/schedule":
            if not self._api_enabled():
                return True
            if not self.schedule_script or not Path(self.schedule_script).exists():
                self._json(500, {"error": "schedule_programs.py が見つかりません"})
                return True
            if not self._schedule_lock.acquire(blocking=False):
                self._json(409, {"error": "予約処理を実行中です。少し待ってください"})
                return True
            try:
                r = subprocess.run([sys.executable, self.schedule_script],
                                   capture_output=True, text=True, timeout=180)
                out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
                self._json(200, {"ok": r.returncode == 0, "output": out[-20000:]})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "タイムアウト(180秒)"})
            finally:
                self._schedule_lock.release()
            return True
        return False

    def do_POST(self):
        if not self._check_access():
            return
        if self._handle_api_post():
            return
        self.send_error(404)

    def do_HEAD(self):
        if not self._check_access():
            return
        return super().do_HEAD()

    def do_GET(self):
        if not self._check_access():
            return
        if self._handle_api_get():
            return
        rng = self.headers.get("Range")
        if rng is None:
            return super().do_GET()

        m = re.match(r"bytes=(\d+)-(\d*)", rng.strip())
        if not m:
            return super().do_GET()

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return

        size = os.path.getsize(path)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_response(416)  # Range Not Satisfiable
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        length = end - start + 1
        ctype = self.guess_type(path)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return
        self.send_response(206)  # Partial Content
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range",
                         f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with f:
            f.seek(start)
            remaining = length
            chunk = 64 * 1024
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    break  # クライアントが切断(シーク時など)
                remaining -= len(data)

    def end_headers(self):
        # 通常のGET応答にもAccept-Rangesを付ける
        if not getattr(self, "_ar_sent", False):
            self.send_header("Accept-Ranges", "bytes")
            self._ar_sent = True
        super().end_headers()

    def send_response(self, *a, **k):
        self._ar_sent = False
        super().send_response(*a, **k)

    def log_message(self, fmt, *args):
        # 静かめのログ(エラーのみ標準エラーへ)
        pass


def main():
    conf = load_conf(CONF_PATH)
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(conf.get("PODCAST_PORT", "8080")))
    ap.add_argument("--dir", default=conf.get("OUTPUT_DIR", "."))
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"ディレクトリがありません: {args.dir}", file=sys.stderr)
        sys.exit(1)

    # アクセス制御(インターネット公開時用)
    RangeHandler.auth_user = conf.get("PODCAST_AUTH_USER", "")
    RangeHandler.auth_pass = conf.get("PODCAST_AUTH_PASS", "")
    prefix = conf.get("PODCAST_URL_PREFIX", "").strip("/")
    RangeHandler.url_prefix = f"/{prefix}" if prefix else ""
    base_dir = Path(__file__).resolve().parent
    RangeHandler.keyword_file = conf.get("KEYWORD_FILE") or str(base_dir / "keywords.txt")
    RangeHandler.schedule_script = str(base_dir / "schedule_programs.py")
    RangeHandler.status_script = str(base_dir / "tuner_status.py")
    RangeHandler.podcast_script = str(base_dir / "make_podcast.py")

    handler = partial(RangeHandler, directory=args.dir)
    httpd = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Podcast配信サーバー起動: http://{args.bind}:{args.port}/")
    print(f"配信ディレクトリ: {args.dir}")
    print(f"Range対応・マルチスレッド。停止は Ctrl-C")
    if RangeHandler.auth_user:
        print(f"Basic認証: 有効(ユーザー {RangeHandler.auth_user})")
    if RangeHandler.url_prefix:
        print(f"秘匿パス: {RangeHandler.url_prefix}/ 配下のみ応答")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
