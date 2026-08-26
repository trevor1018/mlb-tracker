"""每日更新：抓昨天的比賽 → 重建全部分析 → 產生新的推薦。

用法：
  python daily_update.py                # 更新到「美西昨天」
  python daily_update.py --through 2026-08-28
  python daily_update.py --push          # 跑完自動 git commit + push

會處理的細節：
  - 資料截止日自動設成美西昨天（美東晚場在台灣是隔天早上，所以用美西日期最保險）
  - 失效最近幾天的快取（賽程月份檔、Savant 窗口檔），否則會讀到舊的不完整資料
  - 未開打場次每次都重抓（先發預告會變）
"""
import argparse
import datetime as dt
import glob
import os
import subprocess
import sys

from common import CACHE, ROOT, log

HERE = os.path.dirname(os.path.abspath(__file__))


def pt_yesterday():
    # 美西時間 = UTC-7（夏令）；用 UTC-8 保守一點，確保昨天的比賽都完賽了
    now = dt.datetime.utcnow() - dt.timedelta(hours=8)
    return (now.date() - dt.timedelta(days=1)).isoformat()


def invalidate(through, season, days=6):
    """刪掉最近 N 天可能不完整的快取。"""
    cut = (dt.date.fromisoformat(through) - dt.timedelta(days=days)).isoformat()
    removed = 0
    # 賽程：整個月份檔（含 through 的那個月）都重抓
    month = through[:7]
    for f in glob.glob(os.path.join(CACHE, "sched", f"{month}*.json.gz")):
        os.remove(f)
        removed += 1
    # Savant 窗口：結束日在 cut 之後的都重抓
    raw = os.path.join(ROOT, "data", str(season), "savant_raw")
    for f in glob.glob(os.path.join(raw, "*.csv.gz")):
        name = os.path.basename(f).replace(".csv.gz", "")
        try:
            end = name.split("_")[1]
        except IndexError:
            continue
        if end >= cut:
            os.remove(f)
            removed += 1
    log(f"清掉 {removed} 個可能不完整的快取檔（{cut} 之後）")


def run(step, extra=(), env=None):
    log(f"══ {step} ══")
    r = subprocess.run([sys.executable, f"{step}.py", *extra], cwd=HERE, env=env)
    if r.returncode != 0:
        raise SystemExit(f"{step} 失敗（exit {r.returncode}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", default=None)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--skip-mining", action="store_true",
                    help="只更新資料與推薦，跳過耗時的條件挖掘")
    args = ap.parse_args()

    through = args.through or pt_yesterday()
    log(f"更新到 {through}（球季 {args.season}）")
    env = dict(os.environ, MLB_SEASON=str(args.season), MLB_THROUGH=through)

    invalidate(through, args.season)

    # 重置賽前個別更新的狀態：新的一天、新的比賽，窗口要重新算
    for f in ("live_state.json", "lineups.json"):
        path = os.path.join(ROOT, "data", str(args.season), f)
        if os.path.exists(path):
            os.remove(path)
            log(f"重置 {f}")

    steps = ["fetch_season", "fetch_boxscores", "fetch_people", "fetch_savant",
             "build_gamelogs", "build_splits", "build_dataset", "build_player_splits",
             "team_profiles"]
    if not args.skip_mining:
        steps += ["mine_conditions", "mine_team_conditions", "ablation"]
    steps += ["model_markets", "model_runs", "backtest", "over_rule", "market_proxy",
              "predict_slate", "build_matchup_report", "build_app_data", "make_report"]
    for s in steps:
        run(s, ["--importance"] if s == "model_markets" else (), env=env)

    if args.push:
        log("══ git commit + push ══")
        subprocess.run(["git", "add", "-A"], cwd=ROOT)
        msg = f"每日更新：資料到 {through}"
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ROOT)
        log("已推上 GitHub Pages")
    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
