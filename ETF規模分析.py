import os
import time
import re
from datetime import datetime, timedelta
import pandas as pd
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===== 設定參數 =====
target_etfs = ["00981A"]
target_stocks = ["台積電", "台光電"]
incremental = True
days_to_fetch = 90
file_name = "ezmoney_00981A_history.csv"

def to_roc_date(dt):
    year = dt.year - 1911
    return f"{year:03d}/{dt.month:02d}/{dt.day:02d}"

print("啟動瀏覽器")

options = webdriver.ChromeOptions()

if os.getenv("GITHUB_ACTIONS") == "true":
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

url = "https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode=49YTW"
driver.get(url)

print("等待網頁載入...")
time.sleep(3)

# ===== 抓 ETF 清單 =====
try:
    select_element = driver.find_element(By.CSS_SELECTOR, "select.select_fit_size")
    select = Select(select_element)

    etf_options = []
    for opt in select.options:
        val = opt.get_attribute("value")
        text = opt.text

        if val and "請" not in text:
            if target_etfs:
                if any(t in text for t in target_etfs):
                    etf_options.append({"value": val, "text": text})
            else:
                etf_options.append({"value": val, "text": text})

    print(f"找到 {len(etf_options)} 檔符合條件的 ETF")

except Exception as e:
    print(f"取得 ETF 清單失敗: {e}")
    driver.quit()
    exit()

# ===== 日期 =====
date_list = []
for i in range(days_to_fetch):
    d = datetime.now() - timedelta(days=i)
    if d.weekday() < 5:
        date_list.append(d)

print(f"預計查詢 {len(date_list)} 個營業日")

# ===== 舊資料 =====
if os.path.exists(file_name):
    try:
        old_df = pd.read_csv(file_name)
        print(f"已有歷史資料，共 {len(old_df)} 筆")
    except:
        old_df = pd.DataFrame()
else:
    old_df = pd.DataFrame()

all_new_data = []

# ===== 爬蟲 =====
for etf in etf_options:
    print(f"\n===== 處理 ETF：{etf['text']} =====")

    select = Select(driver.find_element(By.CSS_SELECTOR, "select.select_fit_size"))
    select.select_by_value(etf['value'])
    time.sleep(1)

    for dt in date_list:
        roc_date = to_roc_date(dt)
        date_str = dt.strftime("%Y-%m-%d")

        if incremental and not old_df.empty:
            if not old_df[(old_df['etf_name'] == etf['text']) & (old_df['date'] == date_str)].empty:
                print(f"[{date_str}] 已存在，跳過")
                continue

        print(f"抓取: {date_str}...", end=" ")

        try:
            # 輸入日期
            date_input = driver.find_element(By.ID, "ED")
            driver.execute_script(f"arguments[0].value = '{roc_date}';", date_input)

            # 點擊查詢
            search_btn = driver.find_element(By.XPATH, "//button[contains(text(), '查詢')]")
            driver.execute_script("arguments[0].click();", search_btn)

            # ⭐ 核心：等待資料出現（不是等 Loader）
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//*[contains(text(),'基金淨資產價值')]")
                    )
                )
                time.sleep(1)
            except:
                print("資料未載入")
                continue

            # alert 檢查
            try:
                alert = driver.switch_to.alert
                alert.accept()
                print("查無資料")
                continue
            except:
                pass

            # 解析
            soup = BeautifulSoup(driver.page_source, "html.parser")

            page_text = soup.text.replace("\n", " ")

            # ⭐ 不再用 roc_date 判斷
            nav_value = None
            total_issued = None
            nav_per_unit = None

            match1 = re.search(r'基金淨資產價值\(元\)\s*(?:NTD|TWD)?\s*([\d,\.]+)', page_text)
            if match1:
                nav_value = match1.group(1).replace(",", "")

            match2 = re.search(r'已發行受益權單位總數\s*([\d,\.]+)', page_text)
            if match2:
                total_issued = match2.group(1).replace(",", "")

            match3 = re.search(r'每受益權單位淨資產價值\(元\)\s*(?:NTD|TWD)?\s*([\d,\.]+)', page_text)
            if match3:
                nav_per_unit = match3.group(1).replace(",", "")

            if nav_value:
                print(nav_value)

                row_data = {
                    "date": date_str,
                    "etf_name": etf['text'],
                    "nav_value": nav_value,
                    "已發行受益權單位總數": total_issued or "",
                    "每受益權單位淨資產價值(元)": nav_per_unit or ""
                }

                # 持股權重
                for stock in target_stocks:
                    weight = ""
                    rows = soup.select("table tr")

                    for row in rows:
                        if stock in row.text:
                            cols = row.find_all("td")
                            for col in reversed(cols):
                                if "%" in col.text:
                                    weight = col.text.strip()
                                    break

                    row_data[f"{stock}持股權重"] = weight

                all_new_data.append(row_data)

            else:
                print("無資料")

            # ⭐ Debug（GitHub用）
            if os.getenv("GITHUB_ACTIONS") == "true":
                with open(f"debug_{date_str}.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)

        except Exception as e:
            print(f"錯誤: {e}")

print("\n關閉瀏覽器")
driver.quit()

# ===== 儲存 =====
new_df = pd.DataFrame(all_new_data)

if not new_df.empty:
    if not old_df.empty:
        final_df = pd.concat([old_df, new_df], ignore_index=True)
        final_df = final_df.drop_duplicates(subset=['date', 'etf_name'], keep='last')
    else:
        final_df = new_df

    final_df = final_df.sort_values(by=['etf_name', 'date'], ascending=[True, False])

    final_df.to_csv(file_name, index=False, encoding='utf_8_sig')

    print(f"完成！新增 {len(new_df)} 筆")
else:
    print("沒有新資料")
