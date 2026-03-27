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
from selenium.webdriver.support.ui import Select

# ===== 設定參數 =====
target_etfs = ["00981A"]  # 設定要抓取的特定 ETF 代號或名稱，若為空清單 [] 則全部抓取
target_stocks = ["台積電", "台光電"] # 設定要想擷取持股權重的特定股票名稱
incremental = True  # True = 只抓新的日期；False = 全部日期都抓
days_to_fetch = 90  # 往前推算的天數
file_name = "ezmoney_00981A_history.csv"

def to_roc_date(dt):
    """將西元年轉為民國年字串 (例如: 115/03/24)"""
    year = dt.year - 1911
    return f"{year:03d}/{dt.month:02d}/{dt.day:02d}"

print("啟動瀏覽器")
options = webdriver.ChromeOptions()
# 若環境變數 GITHUB_ACTIONS 存在，自動啟用 headless 模式
if os.getenv("GITHUB_ACTIONS") == "true":
    options.add_argument('--headless=new')
    
# 如果想要在本機強制不想看到網頁彈出，也可以取消下方註解
# options.add_argument('--headless=new') 
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
url = "https://www.ezmoney.com.tw/ETF/Transaction/PCF"
driver.get(url)

print("等待網頁載入...")
time.sleep(3)

# ===== 抓取所有 ETF 選項 =====
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

# ===== 準備日期清單 =====
date_list = []
for i in range(days_to_fetch):
    d = datetime.now() - timedelta(days=i)
    # 略過六日 (5=Saturday, 6=Sunday)
    if d.weekday() < 5:
        date_list.append(d)

print(f"預計查詢 {len(date_list)} 個營業日")

# ===== 讀取舊資料 =====
if os.path.exists(file_name):
    try:
        old_df = pd.read_csv(file_name)
        print(f"已有歷史資料，共 {len(old_df)} 筆")
    except Exception as e:
        print(f"讀取舊資料失敗 ({e})，重新建立 DataFrame")
        old_df = pd.DataFrame(columns=["date", "etf_name", "nav_value"])
else:
    old_df = pd.DataFrame(columns=["date", "etf_name", "nav_value"])

all_new_data = []

# ===== 開始爬蟲 =====
for etf in etf_options:
    print(f"\n===== 處理 ETF：{etf['text']} =====")
    
    # 選擇 ETF
    try:
        select = Select(driver.find_element(By.CSS_SELECTOR, "select.select_fit_size"))
        select.select_by_value(etf['value'])
        time.sleep(0.5)
    except Exception as e:
        print("選擇 ETF 發生錯誤，跳過...", e)
        continue

    for dt in date_list:
        roc_date = to_roc_date(dt)
        date_str = dt.strftime("%Y-%m-%d")
        
        # 檢查是否已存在 (若採增量更新)
        if incremental and not old_df.empty:
            if not old_df[(old_df['etf_name'] == etf['text']) & (old_df['date'] == date_str)].empty:
                print(f"[{date_str}] 已存在，跳過")
                continue
                
        print(f"抓取: {date_str} (ROC: {roc_date})... ", end="")
        
        try:
            # 填入日期
            date_input = driver.find_element(By.ID, "ED")
            driver.execute_script(f"arguments[0].value = '{roc_date}';", date_input)
            
            # 點擊查詢按鈕 (透過 JS 點擊避免被 Loader 遮擋)
            search_btn = driver.find_element(By.XPATH, "//button[contains(text(), '查詢')]")
            driver.execute_script("arguments[0].click();", search_btn)
            
            # 等待 Loader 消失
            try:
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                WebDriverWait(driver, 5).until(
                    EC.invisibility_of_element_located((By.ID, "Loader"))
                )
            except:
                pass
            time.sleep(1)  # 確保資料已渲染
            
            # 檢查是否有 alert 彈出 (例如: 查無資料)
            try:
                alert = driver.switch_to.alert
                alert.accept()
                print("無資料 (查無資料)")
                continue
            except:
                pass

            # 讀取並解析資料
            soup = BeautifulSoup(driver.page_source, "html.parser")
            
            nav_value = None
            total_issued = None
            nav_per_unit = None
            
            page_text = soup.text.replace(chr(10), ' ')
            
            # 確保外層區塊確實包含我們剛查詢的日期，避免讀到舊頁面的暫存資料
            if roc_date in page_text:
                # 擷取 "基金淨資產價值(元)"
                match1 = re.search(r'基金淨資產價值\(元\)\s*(?:NTD|TWD)?\s*([\d,\.]+)', page_text)
                if match1:
                    nav_value = match1.group(1).replace(",", "")
                    
                # 擷取 "已發行受益權單位總數"
                match2 = re.search(r'已發行受益權單位總數\s*([\d,\.]+)', page_text)
                if match2:
                    total_issued = match2.group(1).replace(",", "")
                    
                # 擷取 "每受益權單位淨資產價值(元)"
                match3 = re.search(r'每受益權單位淨資產價值\(元\)\s*(?:NTD|TWD)?\s*([\d,\.]+)', page_text)
                if match3:
                    nav_per_unit = match3.group(1).replace(",", "")

            if nav_value:
                print(nav_value)
                
                row_data = {
                    "date": date_str,
                    "etf_name": etf['text'],
                    "nav_value": nav_value,
                    "已發行受益權單位總數": total_issued if total_issued else "",
                    "每受益權單位淨資產價值(元)": nav_per_unit if nav_per_unit else ""
                }
                
                # 抓取特定股票的持股權重
                for stock in target_stocks:
                    weight = ""
                    # 尋找有該股票名稱的標籤
                    stock_th_td = soup.find(string=lambda t: t and stock in t)
                    if stock_th_td:
                        tr = stock_th_td.find_parent("tr")
                        if tr:
                            tds = tr.find_all(['td', 'th'])
                            # 從後往前找包含 % 的欄位，通常最後一欄或倒數第二欄是權重
                            for td in reversed(tds):
                                if "%" in td.text:
                                    weight = td.text.strip()
                                    break
                    row_data[f"{stock}持股權重"] = weight
                
                all_new_data.append(row_data)
            else:
                print("無資料")
                
        except Exception as e:
            print(f"發生錯誤: {e}")

print("\n關閉瀏覽器")
driver.quit()

# ===== 處理與儲存資料 =====
new_df = pd.DataFrame(all_new_data)

if not new_df.empty:
    if not old_df.empty:
        # 合併並去重
        final_df = pd.concat([old_df, new_df], ignore_index=True)
        final_df = final_df.drop_duplicates(subset=['date', 'etf_name'], keep='last')
        final_df = final_df.sort_values(by=['etf_name', 'date'], ascending=[True, False])
    else:
        final_df = new_df
        final_df = final_df.sort_values(by=['etf_name', 'date'], ascending=[True, False])
        
    try:
        final_df.to_csv(file_name, index=False, encoding='utf_8_sig')
        print(f"完成！已將 {len(new_df)} 筆新資料寫入至 {file_name}")
    except Exception as e:
        print(f"儲存發生錯誤: {e}")
else:
    print("本次無新資料需更新。")
