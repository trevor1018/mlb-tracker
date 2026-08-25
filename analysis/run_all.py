"""一鍵跑完整條管線。抓取步驟都有快取，重跑很快。

用法：
  python run_all.py            # 全部
  python run_all.py --from build_dataset
  python run_all.py --only mine_conditions,model_markets
"""
import argparse
import subprocess
import sys
import time

from common import log

STEPS = [
    ("fetch_season", []),
    ("fetch_boxscores", []),
    ("fetch_people", []),
    ("fetch_savant", []),
    ("build_gamelogs", []),
    ("build_splits", []),
    ("build_dataset", []),
    ("build_player_splits", []),
    ("team_profiles", []),
    ("mine_conditions", []),
    ("mine_team_conditions", []),
    ("model_markets", ["--importance"]),
    ("model_runs", []),
    ("backtest", []),
    ("over_rule", []),
    ("market_proxy", []),
    ("predict_slate", []),
    ("build_matchup_report", []),
    ("build_app_data", []),
    ("make_report", []),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--skip", default="")
    args = ap.parse_args()

    steps = STEPS
    if args.only:
        want = set(args.only.split(","))
        steps = [s for s in steps if s[0] in want]
    elif args.start:
        names = [s[0] for s in steps]
        if args.start not in names:
            log(f"找不到步驟 {args.start}")
            return 1
        steps = steps[names.index(args.start):]
    skip = set(args.skip.split(",")) if args.skip else set()

    t0 = time.time()
    for name, extra in steps:
        if name in skip:
            log(f"跳過 {name}")
            continue
        log(f"══ {name} ══")
        t = time.time()
        r = subprocess.run([sys.executable, f"{name}.py"] + extra)
        if r.returncode != 0:
            log(f"步驟 {name} 失敗（exit {r.returncode}），中止")
            return r.returncode
        log(f"══ {name} 完成，{time.time() - t:.0f} 秒 ══")
    log(f"全部完成，總耗時 {(time.time() - t0) / 60:.1f} 分鐘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
