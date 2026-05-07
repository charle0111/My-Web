"""
ETF 00981A 持股變化分析
======================
讀取 ezmoney_00981A_history.csv，進行三段比較：
  [A] 最近一日：前 1 交易日 → 最新日期
  [B] 最近一周：前 5 交易日 → 最新日期
  [C] 最近一月：前 20 交易日 → 最新日期

每段各產生：
  1. 股數增減率絕對值 Top 10 及對應持股權重
  2. 新增 / 移除的股票清單

輸出至 ezmoney_00981A_shares_change.csv
"""

import pandas as pd
import numpy as np
import os

# ========== 設定 ==========
INPUT_FILE  = "ETF_00981A 持股分析.csv"
OUTPUT_FILE = "ETF_00981A 持股分析變化.csv"
TOP_N       = 10
# ==========================

script_dir  = os.path.dirname(os.path.abspath(__file__))
input_path  = os.path.join(script_dir, INPUT_FILE)
output_path = os.path.join(script_dir, OUTPUT_FILE)

# ----- 讀取原始 CSV -----
df_raw = pd.read_csv(input_path, encoding="utf-8-sig")
df_raw["date"] = pd.to_datetime(df_raw["date"])
df_raw = df_raw.sort_values("date").reset_index(drop=True)

META_COLS = {
    "date", "etf_name", "nav_value",
    "已發行受益權單位總數", "每受益權單位淨資產價值(元)",
    "台積電持股權重", "台光電持股權重",
}

shares_cols = [c for c in df_raw.columns if c.endswith("股數") and c not in META_COLS]
print(f"共發現 {len(shares_cols)} 個持股欄位")


def parse_shares(row: pd.Series, col: str) -> float:
    val = row.get(col, np.nan)
    if pd.isna(val) or str(val).strip() == "":
        return np.nan
    try:
        return float(str(val).replace(",", ""))
    except ValueError:
        return np.nan


def compute_changes(base_row: pd.Series, new_row: pd.Series,
                    base_date, new_date) -> pd.DataFrame:
    """比較兩列資料，回傳包含狀態、增減率的 DataFrame。"""
    records = []
    for col in shares_cols:
        stock_name = col.replace("股數", "").strip()
        old_shares = parse_shares(base_row, col)
        new_shares = parse_shares(new_row, col)

        old_exists = not np.isnan(old_shares) and old_shares > 0
        new_exists = not np.isnan(new_shares) and new_shares > 0

        if not old_exists and not new_exists:
            continue

        if not old_exists and new_exists:
            status = "新增"
            change_pct = np.inf
        elif old_exists and not new_exists:
            status = "移除"
            change_pct = -100.0
        else:
            status = "持有"
            change_pct = (new_shares - old_shares) / old_shares * 100

        # 持股權重
        weight = ""
        weight_col = f"{stock_name}持股權重"
        if weight_col in new_row.index:
            w = new_row[weight_col]
            if not pd.isna(w):
                weight = str(w).strip()

        records.append({
            "股票名稱":     stock_name,
            "狀態":         status,
            "基準日股數":   int(old_shares) if old_exists else "",
            "最新日股數":   int(new_shares) if new_exists else "",
            "股數增減率(%)": round(change_pct, 2) if np.isfinite(change_pct) else "∞",
            "持股權重":     weight,
            "基準日期":     base_date,
            "最新日期":     new_date,
        })
    return pd.DataFrame(records)


def build_section_df(df_changes: pd.DataFrame, top_n: int) -> pd.DataFrame:
    """
    將 compute_changes 的結果拆成：
      - 股數增加 Top N（增減率 > 0，由大到小）
      - 股數減少 Top N（增減率 < 0，由小到大）
      - 新增 / 移除股票
    組成帶有標題列的 CSV 區段 DataFrame。
    """
    all_cols = ["排名", "股票名稱", "狀態", "基準日股數", "最新日股數",
                "股數增減率(%)", "持股權重", "基準日期", "最新日期"]

    def fill_cols(df: pd.DataFrame) -> pd.DataFrame:
        for c in all_cols:
            if c not in df.columns:
                df[c] = ""
        return df[all_cols].copy()

    def ranked(df: pd.DataFrame) -> pd.DataFrame:
        df = df.reset_index(drop=True).copy()
        df.insert(0, "排名", range(1, len(df) + 1))
        return df

    df_added   = df_changes[df_changes["狀態"] == "新增"].copy()
    df_removed = df_changes[df_changes["狀態"] == "移除"].copy()
    df_held    = df_changes[df_changes["狀態"] == "持有"].copy()

    # 只取數值列（排除 ∞）
    df_held_num = df_held[df_held["股數增減率(%)"] != "∞"].copy()
    df_held_num["_chg"] = pd.to_numeric(df_held_num["股數增減率(%)"], errors="coerce")

    df_up = (df_held_num[df_held_num["_chg"] > 0]
             .sort_values("_chg", ascending=False)
             .head(top_n)
             .drop(columns=["_chg"])
             .pipe(ranked))

    df_dn = (df_held_num[df_held_num["_chg"] < 0]
             .sort_values("_chg", ascending=True)
             .head(top_n)
             .drop(columns=["_chg"])
             .pipe(ranked))

    blank = pd.DataFrame([{c: "" for c in all_cols}])

    def section_row(title):
        return pd.DataFrame([{c: (title if c == "股票名稱" else "") for c in all_cols}])

    none_row = lambda: pd.DataFrame([{c: ("（無）" if c == "股票名稱" else "") for c in all_cols}])

    pieces = [
        section_row(f"--- 股數增加 Top {top_n} ---"),
        fill_cols(df_up) if not df_up.empty else none_row(),
        blank,
        section_row(f"--- 股數減少 Top {top_n} ---"),
        fill_cols(df_dn) if not df_dn.empty else none_row(),
        blank,
        section_row(f"--- 新增股票 ({len(df_added)} 檔) ---"),
        fill_cols(df_added) if not df_added.empty else none_row(),
        blank,
        section_row(f"--- 移除股票 ({len(df_removed)} 檔) ---"),
        fill_cols(df_removed) if not df_removed.empty else none_row(),
    ]
    return pd.concat(pieces, ignore_index=True)


def print_summary(label: str, df_changes: pd.DataFrame, top_n: int):
    df_added   = df_changes[df_changes["狀態"] == "新增"]
    df_removed = df_changes[df_changes["狀態"] == "移除"]
    df_held    = df_changes[df_changes["狀態"] == "持有"].copy()

    df_held_num = df_held[df_held["股數增減率(%)"] != "∞"].copy()
    df_held_num["_chg"] = pd.to_numeric(df_held_num["股數增減率(%)"], errors="coerce")

    df_up = df_held_num[df_held_num["_chg"] > 0].sort_values("_chg", ascending=False).head(top_n)
    df_dn = df_held_num[df_held_num["_chg"] < 0].sort_values("_chg", ascending=True).head(top_n)

    cols = ["股票名稱", "股數增減率(%)", "基準日股數", "最新日股數", "持股權重"]

    print(f"\n{'='*55}")
    print(f"  {label}")
    print(f"{'='*55}")
    print(f"\n  股數增加 Top {top_n}：")
    print(df_up[cols].to_string(index=False) if not df_up.empty else "  （無）")
    print(f"\n  股數減少 Top {top_n}：")
    print(df_dn[cols].to_string(index=False) if not df_dn.empty else "  （無）")
    print(f"\n  新增股票 ({len(df_added)} 檔)：")
    if not df_added.empty:
        print(df_added[["股票名稱","最新日股數","持股權重"]].to_string(index=False))
    else:
        print("  （無）")
    print(f"\n  移除股票 ({len(df_removed)} 檔)：")
    if not df_removed.empty:
        print(df_removed[["股票名稱","基準日股數"]].to_string(index=False))
    else:
        print("  （無）")


# ========== 取得日期節點 ==========
all_dates  = sorted(df_raw["date"].unique())
newest_date    = all_dates[-1]

day_1_date     = all_dates[-2] if len(all_dates) >= 2 else all_dates[0]
day_5_date     = all_dates[-6] if len(all_dates) >= 6 else all_dates[0]
day_20_date    = all_dates[-21] if len(all_dates) >= 21 else all_dates[0]

newest_row = df_raw[df_raw["date"] == newest_date].iloc[0]
day_1_row  = df_raw[df_raw["date"] == day_1_date].iloc[0]
day_5_row  = df_raw[df_raw["date"] == day_5_date].iloc[0]
day_20_row = df_raw[df_raw["date"] == day_20_date].iloc[0]

# ========== 執行三段比較 ==========
df_day   = compute_changes(day_1_row, newest_row, day_1_date.date(), newest_date.date())
df_week  = compute_changes(day_5_row, newest_row, day_5_date.date(), newest_date.date())
df_month = compute_changes(day_20_row, newest_row, day_20_date.date(), newest_date.date())

print_summary(f"[最近一日] {day_1_date.date()} → {newest_date.date()}", df_day, TOP_N)
print_summary(f"[最近一周] {day_5_date.date()} → {newest_date.date()}", df_week, TOP_N)
print_summary(f"[最近一月] {day_20_date.date()} → {newest_date.date()}", df_month, TOP_N)

# ========== 組合輸出 CSV ==========
all_cols = ["排名", "股票名稱", "狀態", "基準日股數", "最新日股數",
            "股數增減率(%)", "持股權重", "基準日期", "最新日期"]

def big_header(title):
    return pd.DataFrame([{c: (title if c == "排名" else "") for c in all_cols}])

blank2 = pd.DataFrame([{c: "" for c in all_cols}] * 2)

section_day   = build_section_df(df_day, TOP_N)
section_week  = build_section_df(df_week, TOP_N)
section_month = build_section_df(df_month, TOP_N)

output_df = pd.concat([
    big_header(f"[最近一日] {day_1_date.date()} -> {newest_date.date()}"),
    section_day,
    blank2,
    big_header(f"[最近一周] {day_5_date.date()} -> {newest_date.date()}"),
    section_week,
    blank2,
    big_header(f"[最近一月] {day_20_date.date()} -> {newest_date.date()}"),
    section_month,
], ignore_index=True)

output_df.to_csv(output_path, index=False, encoding="utf-8-sig")
print(f"\n[完成] 已輸出：{output_path}")
