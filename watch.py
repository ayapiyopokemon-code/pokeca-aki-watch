#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポケモンカード プレイヤーズクラブ イベント「先着申込の空き」監視ツール

players.pokemon-card.com のイベント検索を定期的に叩き、
先着枠の空きが出た瞬間に macOS 通知・音・読み上げ・ブラウザ自動オープンで知らせる。

Mac ではローカル通知（バナー・音・読み上げ・ブラウザ起動）、
GitHub Actions などの Linux 上では ntfy によるスマホ通知だけが動く。

使い方:
    python3 watch.py                 # 常時監視（Ctrl-C で停止）
    python3 watch.py --once          # 1回だけ確認して終了（GitHub Actions 用）
    python3 watch.py --status        # 現在の検索結果を表示するだけ（状態は更新しない）
    python3 watch.py --test-notify   # 通知のテスト
    python3 watch.py --reset         # 保存済みの状態をリセット
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    sys.exit(
        "curl_cffi が必要です。次のコマンドで入れてください:\n"
        "    python3 -m pip install --user curl_cffi\n"
        "（このサイトは Cloudflare で普通の requests / curl は 403 になります）"
    )

BASE = "https://players.pokemon-card.com"
API_PATH = "/event_search"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "watch.log")
ENV_PATH = os.path.join(HERE, ".env")

IS_MAC = sys.platform == "darwin"

PAGE_SIZE = 20
MAX_PAGES = 15          # 念のための上限（=最大300件）
PAGE_SLEEP = 3.0        # ページ送りの間隔（秒）。短すぎるとブロックされる
IMPERSONATE = "chrome"  # curl_cffi の TLS 偽装プロファイル

# entryStatusCode: 2=先着受付中 / 6=満席 / None=受付前など
STATUS_OPEN_CODES = {2}

# 都道府県の絞り込みは prefecture[]=13&prefecture[]=14 の形式（prefecture_id は無視される）
PREFECTURES = {
    "北海道": 1, "青森": 2, "岩手": 3, "宮城": 4, "秋田": 5, "山形": 6, "福島": 7,
    "茨城": 8, "栃木": 9, "群馬": 10, "埼玉": 11, "千葉": 12, "東京": 13, "神奈川": 14,
    "新潟": 15, "富山": 16, "石川": 17, "福井": 18, "山梨": 19, "長野": 20,
    "岐阜": 21, "静岡": 22, "愛知": 23, "三重": 24, "滋賀": 25, "京都": 26,
    "大阪": 27, "兵庫": 28, "奈良": 29, "和歌山": 30, "鳥取": 31, "島根": 32,
    "岡山": 33, "広島": 34, "山口": 35, "徳島": 36, "香川": 37, "愛媛": 38,
    "高知": 39, "福岡": 40, "佐賀": 41, "長崎": 42, "熊本": 43, "大分": 44,
    "宮崎": 45, "鹿児島": 46, "沖縄": 47,
}

# サイトの「エリア」区分に合わせたショートカット
AREAS = {
    "北海道・東北": [1, 2, 3, 4, 5, 6, 7],
    "北信越": [15, 16, 17, 18, 20],
    "関東": [8, 9, 10, 11, 12, 13, 14],
    "中部": [19, 21, 22, 23, 24],
    "関西": [25, 26, 27, 28, 29, 30],
    "四国・中国": [31, 32, 33, 34, 35, 36, 37, 38, 39],
    "九州": [40, 41, 42, 43, 44, 45, 46, 47],
    # よく使う言い方の別名
    "関東圏": [8, 9, 10, 11, 12, 13, 14],
    "首都圏": [11, 12, 13, 14],
    "東北": [2, 3, 4, 5, 6, 7],
    "中国・四国": [31, 32, 33, 34, 35, 36, 37, 38, 39],
    "九州・沖縄": [40, 41, 42, 43, 44, 45, 46, 47],
}


def resolve_prefectures(items):
    """['関東'] や ['東京','神奈川'] や [13,14] を都道府県IDのリストにする。"""
    ids = []
    for item in items or []:
        if isinstance(item, int):
            ids.append(item)
            continue
        name = str(item).strip()
        if name.isdigit():
            ids.append(int(name))
        elif name in AREAS:
            ids.extend(AREAS[name])
        else:
            key = name.replace("都", "").replace("府", "").replace("県", "")
            if key in PREFECTURES:
                ids.append(PREFECTURES[key])
            else:
                raise ValueError("都道府県／エリア名が不明です: {}".format(item))
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ---------------------------------------------------------------- utilities

def log(msg, quiet=False):
    line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if not quiet:
        print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_dotenv():
    """.env があれば環境変数として読み込む（ntfy トピック名をリポジトリに載せないため）。"""
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


def apply_env_overrides(cfg):
    """
    秘密情報は環境変数を最優先にする。
    ローカル Mac は .env、GitHub Actions は Secrets から供給される。
    """
    notify_cfg = cfg.setdefault("notify", {})
    for env_key, cfg_key in (("NTFY_TOPIC", "ntfy_topic"),
                             ("DISCORD_WEBHOOK", "discord_webhook")):
        value = os.environ.get(env_key)
        if value:
            notify_cfg[cfg_key] = value.strip()
    return cfg


# ---------------------------------------------------------------- fetching

def watch_params(watch):
    """
    watch 定義（url と任意の prefectures）から API 用のクエリを組み立てる。
    prefecture[] のように同じキーが複数回出るため、dict ではなく (key, value) のリストで扱う。
    """
    parsed = urllib.parse.urlparse(watch["url"])
    pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k != "offset"
    ]
    prefs = resolve_prefectures(watch.get("prefectures"))
    if prefs:
        pairs = [(k, v) for k, v in pairs if k not in ("prefecture", "prefecture[]")]
        pairs += [("prefecture[]", str(i)) for i in prefs]
    return pairs


def encode_query(pairs, offset=None):
    q = list(pairs)
    if offset is not None:
        q.append(("offset", str(offset)))
    # "[]" はそのまま残す（%5B%5D でも通るが、サイトと同じ形にしておく）
    return urllib.parse.urlencode(q, safe="[]")


def build_search_page_url(pairs):
    return BASE + "/event/search?" + encode_query(pairs)


def event_detail_url(ev):
    """検索結果1件から詳細ページの URL を組み立てる。"""
    try:
        return "{}/event/detail/{}/{}/{}/{}/{}".format(
            BASE,
            ev["event_holding_id"],
            ev.get("shop_term", 1),
            ev["shop_id"],
            ev["event_date_params"],
            ev["date_id"],
        )
    except KeyError:
        return BASE + "/event/search"


class Fetcher:
    """Cloudflare 越しに JSON API を叩く。ブロックされたら段階的に待つ。"""

    def __init__(self):
        self.session = cffi_requests.Session(impersonate=IMPERSONATE)
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": BASE + "/event/search",
        }

    def get_page(self, pairs, offset):
        url = BASE + API_PATH + "?" + encode_query(pairs, offset)
        res = self.session.get(url, headers=self.headers, timeout=25)
        text = (res.text or "").lstrip()

        # 本文が JSON でない = Cloudflare のブロックページ等
        if not text.startswith("{"):
            raise BlockedError(
                "HTTP {} / 想定外のレスポンス（Cloudflare のブロックか一時的な制限）".format(res.status_code)
            )

        data = json.loads(text)
        code = data.get("code")

        # このAPIは「該当0件」を HTTP 404 +「イベント情報が存在しません。」で返す
        if code == 404:
            return {"code": 404, "event": [], "eventCount": 0}

        if code != 200:
            raise BlockedError("API code {} / {}".format(code, data.get("message", "")))

        return data

    def fetch_all(self, pairs):
        events, offset = [], 0
        total = None
        for page in range(MAX_PAGES):
            data = self.get_page(pairs, offset)
            if total is None:
                total = data.get("eventCount", 0)
            chunk = data.get("event") or []
            events.extend(chunk)
            offset += PAGE_SIZE
            if len(events) >= (total or 0) or not chunk:
                break
            time.sleep(PAGE_SLEEP)
        return events, (total or len(events))


class BlockedError(Exception):
    pass


# ---------------------------------------------------------------- event model

def summarize(ev):
    """差分比較に使う最小限のスナップショット。"""
    return {
        "title": ev.get("event_title", ""),
        "shop": ev.get("shop_name", ""),
        "pref": ev.get("prefecture_name", ""),
        "date": ev.get("event_date", ""),
        "start": ev.get("event_started_at", ""),
        "end": ev.get("event_ended_at", ""),
        "capacity": ev.get("capacity"),
        "status": ev.get("entryStatus"),
        "status_code": ev.get("entryStatusCode"),
        "full": ev.get("fullOccupiedFlg"),
        "restart": ev.get("entryRestartFlg"),
        "recruit": ev.get("recruitFlg"),
        "cancel": ev.get("cancelFlg"),
        "url": event_detail_url(ev),
    }


def is_first_come(snap):
    status = snap.get("status") or ""
    return snap.get("status_code") in STATUS_OPEN_CODES or "先着" in status


def has_vacancy(snap):
    """先着で今すぐ申し込めそうか。"""
    if snap.get("cancel"):
        return False
    if not is_first_come(snap):
        return False
    return snap.get("full") in (0, None, False)


def describe(snap):
    parts = [
        "{} {}〜{}".format(snap.get("date", ""), snap.get("start", ""), snap.get("end", "")).strip(),
        snap.get("pref", ""),
        snap.get("title", ""),
    ]
    head = " / ".join(p for p in parts if p)
    tail = "{} / 定員{}人 / {}{}".format(
        snap.get("shop", ""),
        snap.get("capacity"),
        snap.get("status") or "受付前",
        "（満席フラグあり）" if snap.get("full") else "（空きあり）",
    )
    return head + "\n" + tail


# ---------------------------------------------------------------- diffing

def diff_events(prev, curr, rules):
    """
    prev / curr: {date_id(str): snapshot}
    戻り値: [(理由, snapshot), ...]
    """
    hits = []
    first_come_only = rules.get("first_come_only", True)

    for key, now in curr.items():
        if first_come_only and not is_first_come(now):
            continue
        before = prev.get(key)

        if before is None:
            if rules.get("alert_on_new_event", True) and has_vacancy(now):
                hits.append(("新しい先着枠が出ました", now))
            elif rules.get("alert_on_new_event", True):
                hits.append(("条件に合うイベントが新しく載りました", now))
            continue

        if rules.get("alert_on_full_cleared", True):
            if before.get("full") and not now.get("full"):
                hits.append(("満席が解消されました（空きが出ました）", now))
                continue

        if rules.get("alert_on_status_open", True):
            was_open = before.get("status_code") in STATUS_OPEN_CODES
            is_open = now.get("status_code") in STATUS_OPEN_CODES
            if is_open and not was_open:
                hits.append(("先着受付が開始／再開されました", now))
                continue
            if now.get("restart") and not before.get("restart"):
                hits.append(("受付が再開されました", now))
                continue

    return hits


# ---------------------------------------------------------------- notifying

def _osa_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title, message, url, cfg):
    n = cfg.get("notify", {})

    # Linux（GitHub Actions）では osascript / afplay / say / open が無いので飛ばす
    if not IS_MAC:
        if n.get("ntfy_topic"):
            _push_ntfy(n["ntfy_topic"], title, message, url)
        if n.get("discord_webhook"):
            _push_discord(n["discord_webhook"], title, message, url)
        if not n.get("ntfy_topic") and not n.get("discord_webhook"):
            log("警告: Mac 以外で通知先が未設定です（NTFY_TOPIC を設定してください）")
        return

    if n.get("macos_notification", True):
        script = 'display notification "{}" with title "{}" sound name "{}"'.format(
            _osa_escape(message.replace("\n", " / ")),
            _osa_escape(title),
            _osa_escape(n.get("sound") or "Glass"),
        )
        _run(["osascript", "-e", script])

    sound = n.get("sound") or "Glass"
    sound_file = "/System/Library/Sounds/{}.aiff".format(sound)
    if os.path.exists(sound_file):
        for _ in range(max(0, int(n.get("sound_repeat", 0)))):
            _run(["afplay", sound_file])

    if n.get("speak"):
        cmd = ["say"]
        voice = n.get("speak_voice")
        if voice:
            cmd += ["-v", voice]
        cmd.append(n.get("speak_text") or "空きが出ました")
        _run(cmd)

    if n.get("ntfy_topic"):
        _push_ntfy(n["ntfy_topic"], title, message, url)

    if n.get("discord_webhook"):
        _push_discord(n["discord_webhook"], title, message, url)

    if n.get("auto_open") and url:
        _run(["open", url])


def _run(cmd):
    try:
        subprocess.run(cmd, check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        log("通知コマンド失敗 {}: {}".format(cmd[0], e))


def _push_ntfy(topic, title, message, url):
    """
    スマホへのプッシュ（https://ntfy.sh/<topic> を購読しておく。無料・アカウント不要）

    ヘッダー方式(Title: ...)は ASCII しか通らず日本語が化けるので、
    JSON ボディで送る（こちらは UTF-8 がそのまま通る）。
    """
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 5,          # urgent: 消音中でも鳴りやすい
        "tags": ["tada"],
    }
    if url:
        payload["click"] = url
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            res.read()
        log("スマホ通知を送信しました (ntfy: {})".format(topic))
    except Exception as e:  # noqa: BLE001 - 通知失敗で監視を止めたくない
        log("ntfy 送信失敗: {}".format(e))


def _push_discord(webhook, title, message, url):
    try:
        payload = json.dumps(
            {"content": "**{}**\n{}\n{}".format(title, message, url or "")}
        ).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        log("Discord 送信失敗: {}".format(e))


# ---------------------------------------------------------------- main loop

def check_watch(fetcher, watch, state, cfg, first_run, dry_run=False):
    name = watch.get("name") or watch["url"]
    params = watch_params(watch)
    events, total = fetcher.fetch_all(params)

    curr = {str(ev.get("date_id")): summarize(ev) for ev in events if ev.get("date_id")}
    prev = state.get(name, {}).get("events", {})

    vacant = [s for s in curr.values() if has_vacancy(s)]
    log("[{}] {}件ヒット / 空きあり {}件".format(name, total, len(vacant)))
    for snap in curr.values():
        mark = "◎空きあり" if has_vacancy(snap) else "  満/対象外"
        log("    {} {}".format(mark, describe(snap).replace("\n", " | ")), quiet=not first_run)

    if dry_run:
        return

    if first_run and not cfg.get("rules", {}).get("alert_on_first_run", False):
        log("[{}] 初回のため基準値として記録（通知なし）".format(name))
    else:
        for reason, snap in diff_events(prev, curr, cfg.get("rules", {})):
            title = "🎯 {} — {}".format(reason, name)
            body = describe(snap)
            log("*** 通知: {}\n{}\n{}".format(title, body, snap["url"]))
            notify(title, body, snap["url"], cfg)

    # 中身が変わっていないのに updated_at だけ動くと、GitHub Actions が
    # 5分ごとに state.json をコミットしてしまう。変化したときだけ更新する。
    prev_entry = state.get(name, {})
    changed = prev_entry.get("events") != curr
    state[name] = {
        "events": curr,
        "updated_at": (datetime.now().isoformat(timespec="seconds")
                       if changed or not prev_entry.get("updated_at")
                       else prev_entry["updated_at"]),
        "search_url": build_search_page_url(params),
    }


def main():
    ap = argparse.ArgumentParser(description="ポケカ先着枠の空き監視")
    ap.add_argument("--once", action="store_true", help="1回だけ確認して終了")
    ap.add_argument("--status", action="store_true", help="現在の結果を表示するだけ（状態を更新しない）")
    ap.add_argument("--test-notify", action="store_true", help="通知のテスト（Mac＋スマホ全部）")
    ap.add_argument("--test-push", action="store_true", help="スマホ通知だけテスト（Macは音を出さない）")
    ap.add_argument("--reset", action="store_true", help="保存済みの状態を削除")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json が読めません: " + CONFIG_PATH)

    load_dotenv()
    apply_env_overrides(cfg)

    if args.test_push:
        n = cfg.get("notify", {})
        if not n.get("ntfy_topic") and not n.get("discord_webhook"):
            sys.exit("config.json の notify.ntfy_topic（または discord_webhook）が空です。")
        sample = build_search_page_url(watch_params(cfg["watches"][0]))
        if n.get("ntfy_topic"):
            _push_ntfy(n["ntfy_topic"],
                       "🎯 テスト通知 — ポケカ空き監視",
                       "スマホにこの形で届きます。\n本番では店舗名・日時・定員・申込ページのリンクが入ります。",
                       sample)
        if n.get("discord_webhook"):
            _push_discord(n["discord_webhook"], "🎯 テスト通知 — ポケカ空き監視",
                          "スマホにこの形で届きます。", sample)
        print("送信しました。スマホの ntfy アプリを確認してください。")
        return

    if args.test_notify:
        notify("🎯 テスト通知 — 空き監視ツール",
               "通知はこんな見た目で届きます。\n音・読み上げ・自動オープンの確認用です。",
               build_search_page_url(watch_params(cfg["watches"][0])),
               cfg)
        print("通知を送りました。届かない場合は システム設定 > 通知 でターミナル/Python の通知を許可してください。")
        return

    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        print("state.json を削除しました。")
        return

    watches = [w for w in cfg.get("watches", []) if w.get("enabled", True)]
    if not watches:
        sys.exit("config.json の watches が空です。")

    state = load_json(STATE_PATH, {})
    fetcher = Fetcher()
    interval = int(cfg.get("poll_interval_sec", 60))
    jitter = int(cfg.get("jitter_sec", 15))
    backoff = 0

    log("監視開始: {}件の条件 / 約{}秒おき".format(len(watches), interval))
    for w in watches:
        log("  - {}".format(w.get("name") or w["url"]))

    try:
        while True:
            blocked = False
            succeeded = 0
            for w in watches:
                name = w.get("name") or w["url"]
                first_run = name not in state
                try:
                    check_watch(fetcher, w, state, cfg, first_run, dry_run=args.status)
                    succeeded += 1
                except BlockedError as e:
                    blocked = True
                    log("[{}] 取得できず: {}".format(name, e))
                except Exception as e:  # noqa: BLE001 - 監視は止めない
                    log("[{}] エラー: {}: {}".format(name, type(e).__name__, e))
                if len(watches) > 1:
                    time.sleep(PAGE_SLEEP)

            if not args.status:
                save_json(STATE_PATH, state)

            if args.once or args.status:
                # 1件も取れていないのに正常終了すると「空きゼロ」と区別がつかず危険。
                # 異常終了させて GitHub Actions を失敗（赤）にし、気づけるようにする。
                if succeeded == 0:
                    sys.exit("全ての条件で取得に失敗しました（ブロックまたは通信エラー）")
                return

            if blocked:
                backoff = min(600, (backoff * 2) or 60)
                log("アクセス制限のため {} 秒待機します".format(backoff))
                time.sleep(backoff)
                continue

            backoff = 0
            wait = max(15, interval + random.randint(-jitter, jitter))
            time.sleep(wait)
    except KeyboardInterrupt:
        save_json(STATE_PATH, state)
        log("監視を停止しました。")


if __name__ == "__main__":
    main()
