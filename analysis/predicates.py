"""把特徵離散化成「條件」（predicate）：布林遮罩 + 中文標籤。

分位數門檻只用訓練期資料計算，再套用到測試期，避免用到未來資訊。
"""
import numpy as np
import pandas as pd

# (欄位, 中文名, 方向說明) — 數值型，切 前/後 30% 與 前/後 15%
NUM_FEATURES_TG = [
    ("my_woba_vsL", "我隊對左投 wOBA"),
    ("my_woba_vsR", "我隊對右投 wOBA"),
    ("my_woba_fast", "我隊對速球 wOBA"),
    ("my_woba_break", "我隊對變化球 wOBA"),
    ("my_woba_off", "我隊對慢速球 wOBA"),
    ("my_bat_vs_oppSP_hand_woba", "我隊對「對手先發慣用手」wOBA"),
    ("my_bat_vs_oppSP_main_woba", "我隊對「對手先發主球種」wOBA"),
    ("my_k_pct", "我隊被三振率"),
    ("my_bb_pct", "我隊保送率"),
    ("my_hard_pct", "我隊強擊率"),
    ("my_barrel_pct", "我隊桶率"),
    ("my_rpg", "我隊場均得分"),
    ("my_rpg_l10", "我隊近10場均得分"),
    ("my_rapg", "我隊場均失分"),
    ("my_rapg_l10", "我隊近10場均失分"),
    ("my_win_pct", "我隊勝率"),
    ("my_win_pct_l10", "我隊近10場勝率"),
    ("my_elo", "我隊 Elo"),
    ("my_sp_r9", "我隊先發 R/9"),
    ("my_sp_k9", "我隊先發 K/9"),
    ("my_sp_bb9", "我隊先發 BB/9"),
    ("my_sp_hr9", "我隊先發 HR/9"),
    ("my_sp_ip_per_start", "我隊先發平均局數"),
    ("my_sp_whiff", "我隊先發揮空率"),
    ("my_sp_woba_vs_us", "對手先發對我隊主要打側 wOBA"),
    ("my_bp_r9_14", "我隊牛棚14天 R/9"),
    ("my_bp_ip14", "我隊牛棚14天局數"),
    ("op_woba_vsL", "對手對左投 wOBA"),
    ("op_woba_vsR", "對手對右投 wOBA"),
    ("op_woba_fast", "對手對速球 wOBA"),
    ("op_woba_break", "對手對變化球 wOBA"),
    ("op_bat_vs_oppSP_hand_woba", "對手對「我隊先發慣用手」wOBA"),
    ("op_bat_vs_oppSP_main_woba", "對手對「我隊先發主球種」wOBA"),
    ("op_k_pct", "對手被三振率"),
    ("op_rpg", "對手場均得分"),
    ("op_rpg_l10", "對手近10場均得分"),
    ("op_rapg", "對手場均失分"),
    ("op_win_pct", "對手勝率"),
    ("op_win_pct_l10", "對手近10場勝率"),
    ("op_elo", "對手 Elo"),
    ("op_sp_r9", "對手先發 R/9"),
    ("op_sp_k9", "對手先發 K/9"),
    ("op_sp_bb9", "對手先發 BB/9"),
    ("op_sp_hr9", "對手先發 HR/9"),
    ("op_sp_ip_per_start", "對手先發平均局數"),
    ("op_sp_whiff", "對手先發揮空率"),
    ("op_bp_r9_14", "對手牛棚14天 R/9"),
    ("elo_diff", "Elo 差（我-對）"),
    ("sp_r9_diff", "先發 R/9 差（對-我）"),
    ("rpg_diff", "場均得分差（我-對）"),
    ("form_diff", "近10場勝率差（我-對）"),
    ("total_expect", "雙方攻守推估總分"),
    ("temp", "氣溫"),
    ("my_rest", "我隊休息天數"),
    ("my_sp_rest", "我隊先發休息天數"),
    ("op_sp_rest", "對手先發休息天數"),
    ("my_sp_velo_delta", "我隊先發球速變化"),
]

NUM_FEATURES_G = [
    ("home_woba_vsL", "主隊對左投 wOBA"),
    ("home_woba_vsR", "主隊對右投 wOBA"),
    ("away_woba_vsL", "客隊對左投 wOBA"),
    ("away_woba_vsR", "客隊對右投 wOBA"),
    ("home_bat_vs_oppSP_hand_woba", "主隊對客隊先發慣用手 wOBA"),
    ("away_bat_vs_oppSP_hand_woba", "客隊對主隊先發慣用手 wOBA"),
    ("home_bat_vs_oppSP_main_woba", "主隊對客隊先發主球種 wOBA"),
    ("away_bat_vs_oppSP_main_woba", "客隊對主隊先發主球種 wOBA"),
    ("home_sp_r9", "主隊先發 R/9"),
    ("away_sp_r9", "客隊先發 R/9"),
    ("home_sp_k9", "主隊先發 K/9"),
    ("away_sp_k9", "客隊先發 K/9"),
    ("home_sp_ip_per_start", "主隊先發平均局數"),
    ("away_sp_ip_per_start", "客隊先發平均局數"),
    ("home_sp_whiff", "主隊先發揮空率"),
    ("away_sp_whiff", "客隊先發揮空率"),
    ("home_bp_r9_14", "主隊牛棚14天 R/9"),
    ("away_bp_r9_14", "客隊牛棚14天 R/9"),
    ("home_rpg", "主隊場均得分"),
    ("away_rpg", "客隊場均得分"),
    ("home_rapg", "主隊場均失分"),
    ("away_rapg", "客隊場均失分"),
    ("home_rpg_l10", "主隊近10場均得分"),
    ("away_rpg_l10", "客隊近10場均得分"),
    ("sum_rpg", "雙方場均得分合計"),
    ("sum_sp_r9", "雙方先發 R/9 合計"),
    ("sum_bp_r9", "雙方牛棚14天 R/9 合計"),
    ("total_expect", "推估總分"),
    ("k_sum", "雙方先發 K/9 合計"),
    ("temp", "氣溫"),
]

# 類別型：(欄位, 值, 標籤)
CAT_TG = [
    ("is_home", True, "主場"),
    ("is_home", False, "客場"),
    ("day_game", True, "日場"),
    ("day_game", False, "夜場"),
    ("my_sp_hand", "L", "我隊先發左投"),
    ("my_sp_hand", "R", "我隊先發右投"),
    ("op_sp_hand", "L", "對手先發左投"),
    ("op_sp_hand", "R", "對手先發右投"),
    ("my_oppSP_main", "fastball", "對手先發以速球為主"),
    ("my_oppSP_main", "breaking", "對手先發以變化球為主"),
    ("my_oppSP_main", "offspeed", "對手先發以慢速球為主"),
    ("series_game", 1, "系列賽首戰"),
    ("series_game", 3, "系列賽第3戰"),
]
CAT_G = [
    ("day_game", True, "日場"),
    ("day_game", False, "夜場"),
    ("home_sp_hand", "L", "主隊先發左投"),
    ("home_sp_hand", "R", "主隊先發右投"),
    ("away_sp_hand", "L", "客隊先發左投"),
    ("away_sp_hand", "R", "客隊先發右投"),
    ("hand_matchup", "LL", "雙方先發皆左投"),
    ("hand_matchup", "RR", "雙方先發皆右投"),
    ("series_game", 1, "系列賽首戰"),
]

QUANTILES = [(0.85, "hi15", "前15%"), (0.70, "hi30", "前30%"),
             (0.30, "lo30", "後30%"), (0.15, "lo15", "後15%")]


def add_derived(df, kind):
    df = df.copy()
    if kind == "tg":
        df["elo_diff"] = df["my_elo"] - df["op_elo"]
        df["sp_r9_diff"] = df["op_sp_r9"] - df["my_sp_r9"]
        df["rpg_diff"] = df["my_rpg"] - df["op_rpg"]
        df["form_diff"] = df["my_win_pct_l10"] - df["op_win_pct_l10"]
        # 推估總分：我方攻擊 vs 對方守備、對方攻擊 vs 我方守備
        df["total_expect"] = ((df["my_rpg"] + df["op_rapg"]) / 2
                              + (df["op_rpg"] + df["my_rapg"]) / 2)
    else:
        df["sum_rpg"] = df["home_rpg"] + df["away_rpg"]
        df["sum_sp_r9"] = df["home_sp_r9"] + df["away_sp_r9"]
        df["sum_bp_r9"] = df["home_bp_r9_14"] + df["away_bp_r9_14"]
        df["k_sum"] = df["home_sp_k9"] + df["away_sp_k9"]
        df["total_expect"] = ((df["home_rpg"] + df["away_rapg"]) / 2
                              + (df["away_rpg"] + df["home_rapg"]) / 2)
        df["hand_matchup"] = (df["home_sp_hand"].fillna("?").astype(str)
                              + df["away_sp_hand"].fillna("?").astype(str))
        df["hand_matchup"] = df["hand_matchup"].map(
            {"LL": "LL", "RR": "RR", "LR": "LR", "RL": "LR"}).fillna("?")
    return df


def build_predicates(df, kind, train_mask=None, min_support=40):
    """回傳 (names, labels, masks) — masks 為 (k, n) 的 bool 陣列。"""
    nums = NUM_FEATURES_TG if kind == "tg" else NUM_FEATURES_G
    cats = CAT_TG if kind == "tg" else CAT_G
    if train_mask is None:
        train_mask = np.ones(len(df), bool)

    names, labels, masks = [], [], []

    for col, zh in nums:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        tr = s[train_mask].dropna()
        if len(tr) < 200:
            continue
        for q, tag, qzh in QUANTILES:
            thr = float(tr.quantile(q))
            if tag.startswith("hi"):
                m = (s >= thr).fillna(False).to_numpy()
                lab = f"{zh} {qzh}(≥{thr:.3g})"
            else:
                m = (s <= thr).fillna(False).to_numpy()
                lab = f"{zh} {qzh}(≤{thr:.3g})"
            if m.sum() < min_support:
                continue
            names.append(f"{col}:{tag}")
            labels.append(lab)
            masks.append(m)

    for col, val, zh in cats:
        if col not in df.columns:
            continue
        m = (df[col] == val).fillna(False).to_numpy()
        if m.sum() < min_support:
            continue
        names.append(f"{col}={val}")
        labels.append(zh)
        masks.append(m)

    M = np.vstack(masks) if masks else np.zeros((0, len(df)), bool)
    return names, labels, M
