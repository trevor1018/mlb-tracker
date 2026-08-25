"""共用工具：HTTP 抓取（含重試 + 磁碟快取）、平行化、路徑常數。"""
import gzip
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
OUTPUT = os.path.join(ROOT, "output")
for d in (DATA, CACHE, OUTPUT):
    os.makedirs(d, exist_ok=True)

API = "https://statsapi.mlb.com/api/v1"
API11 = "https://statsapi.mlb.com/api/v1.1"
UA = "mlb-tracker/2.0 (personal research)"

SEASON = 2026
# 資料截止日：API 上最後一天有完賽資料的日期（跑 fetch_season 時會自動修正）
DATA_THROUGH = "2026-08-24"

try:  # Windows console 預設 cp950，中文 log 會亂碼
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def _cache_path(key):
    # key 例："box/778901" → data/cache/box/778901.json.gz
    p = os.path.join(CACHE, key + ".json.gz")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def cache_read(key):
    p = _cache_path(key)
    if not os.path.exists(p):
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        try:
            os.remove(p)
        except OSError:
            pass
        return None


def cache_write(key, obj):
    p = _cache_path(key)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, p)


def fetch_json(url, tries=5, timeout=60):
    """純 HTTP GET → JSON，指數退避重試。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
            last = e
            code = getattr(e, "code", None)
            if code in (404, 403):
                raise
            time.sleep(min(2 ** i, 20) + random.random())
    raise RuntimeError(f"fetch failed after {tries}: {url} ({last})")


def fetch_text(url, tries=4, timeout=180):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except Exception as e:  # Savant 偶發 500/超時
            last = e
            time.sleep(min(2 ** i, 30) + random.random())
    raise RuntimeError(f"fetch_text failed: {url} ({last})")


def cached_json(key, url, **kw):
    """有快取讀快取，沒有就抓並寫入。"""
    v = cache_read(key)
    if v is not None:
        return v
    v = fetch_json(url, **kw)
    cache_write(key, v)
    return v


def pmap(fn, items, workers=8, label=None, every=200):
    """平行 map，回傳 (item, result, error) 串列，順序不保證。"""
    out = []
    done = 0
    total = len(items)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for f in as_completed(futs):
            it = futs[f]
            try:
                out.append((it, f.result(), None))
            except Exception as e:
                out.append((it, None, e))
            done += 1
            if label and (done % every == 0 or done == total):
                log(f"{label}: {done}/{total}")
    return out


def jdump(obj, path, gz=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return path


def jload(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── 球隊常數 ───
TEAM_ZH = {
    108: "天使", 109: "響尾蛇", 110: "金鶯", 111: "紅襪", 112: "小熊", 113: "紅人",
    114: "守護者", 115: "洛磯", 116: "老虎", 117: "太空人", 118: "皇家", 119: "道奇",
    120: "國民", 121: "大都會", 133: "運動家", 134: "海盜", 135: "教士", 136: "水手",
    137: "巨人", 138: "紅雀", 139: "光芒", 140: "遊騎兵", 141: "藍鳥", 142: "雙城",
    143: "費城人", 144: "勇士", 145: "白襪", 146: "馬林魚", 147: "洋基", 158: "釀酒人",
}
TEAM_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "OAK", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}
