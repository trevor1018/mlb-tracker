"""每隊一句話特徵摘要 → output/team_profiles.json

把 team_splits.json 的一堆數字，自動翻成「洋基：對右投強(#3)、對變化球弱(#26)、
夜場明顯優於日場」這種可以用眼睛掃的句子。挑選規則：
  - 聯盟排名 ≤6 算強、≥25 算弱
  - 日/夜、主/客 的 wOBA 差 ≥0.020 才提（低於這個就是雜訊）
  - 投手面同樣處理，但數字越低越好
"""
import sys

from common import OUTPUT, jdump, jload, log

STRONG, WEAK = 6, 25
GAP_DAYNIGHT = 0.020
GAP_HOMEAWAY = 0.025

BAT_RANKS = [("bat_vsL_woba", "對左投"), ("bat_vsR_woba", "對右投"),
             ("bat_fastball_woba", "對速球"), ("bat_breaking_woba", "對變化球"),
             ("bat_offspeed_woba", "對慢速球")]
PIT_RANKS = [("pit_vsL_woba", "壓制左打"), ("pit_vsR_woba", "壓制右打")]


def main():
    S = jload(f"{OUTPUT}/team_splits.json")
    teams = S["teams"]
    out = {}
    for tid, t in teams.items():
        ranks = t.get("ranks", {})
        bat, pit = t["bat"], t["pit"]
        strong, weak, notes = [], [], []

        for key, zh in BAT_RANKS:
            r = ranks.get(key)
            if r is None:
                continue
            if r <= STRONG:
                strong.append(f"{zh}強(#{r})")
            elif r >= WEAK:
                weak.append(f"{zh}弱(#{r})")
        for key, zh in PIT_RANKS:
            r = ranks.get(key)
            if r is None:
                continue
            if r <= STRONG:
                strong.append(f"{zh}好(#{r})")
            elif r >= WEAK:
                weak.append(f"{zh}差(#{r})")

        d, n = (bat["day"] or {}).get("woba"), (bat["night"] or {}).get("woba")
        if d and n and abs(d - n) >= GAP_DAYNIGHT:
            notes.append(f"{'日場' if d > n else '夜場'}打擊明顯較好"
                         f"（{max(d, n):.3f} vs {min(d, n):.3f}）")
        h, a = (bat["home"] or {}).get("woba"), (bat["away"] or {}).get("woba")
        if h and a and abs(h - a) >= GAP_HOMEAWAY:
            notes.append(f"{'主場' if h > a else '客場'}打擊明顯較好"
                         f"（{max(h, a):.3f} vs {min(h, a):.3f}）")
        # 對左右投的落差
        vl, vr = (bat["vs_LHP"] or {}).get("woba"), (bat["vs_RHP"] or {}).get("woba")
        if vl and vr and abs(vl - vr) >= 0.025:
            notes.append(f"對{'左' if vl > vr else '右'}投明顯較行"
                         f"（{max(vl, vr):.3f} vs {min(vl, vr):.3f}）")
        # 投手群球種倚重
        usage = pit.get("usage") or {}
        if usage:
            top = max(usage, key=usage.get)
            if usage[top] >= 45:
                notes.append(f"投手群{top}用量高（{usage[top]:.0f}%）")

        summary = "；".join(filter(None, [
            "、".join(strong) if strong else "",
            "、".join(weak) if weak else "",
            "、".join(notes) if notes else ""]))
        out[tid] = {
            "zh": t["zh"],
            "summary": summary or "各項都接近聯盟平均",
            "strong": strong, "weak": weak, "notes": notes,
            "bat_woba": (bat["all"] or {}).get("woba"),
            "pit_woba": (pit["all"] or {}).get("woba"),
        }
        log(f"  {t['zh']:<4} {out[tid]['summary'][:78]}")

    p = jdump({"season": S["season"], "teams": out,
               "rules": {"strong_rank": STRONG, "weak_rank": WEAK,
                         "gap_daynight": GAP_DAYNIGHT, "gap_homeaway": GAP_HOMEAWAY}},
              f"{OUTPUT}/team_profiles.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
