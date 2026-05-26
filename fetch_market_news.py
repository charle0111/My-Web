import os
import sys
import json
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
import yfinance as yf
from google import genai
from google.genai import types
import webbrowser
import urllib3
import ssl
import httpx

# ==========================================
# 0. 全域 SSL 憑證驗證繞過 (解決企業防火牆與自簽憑證問題)
# ==========================================
# 停用 urllib3 安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 建立不進行驗證的 SSL 上下文 (針對 standard urllib & feedparser)
ssl_context = ssl._create_unverified_context()
ssl._create_default_https_context = ssl._create_unverified_context

# 建立預設不驗證 SSL 的 standard requests Session (針對經濟日曆抓取)
session_requests = requests.Session()
session_requests.verify = False

# 建立預設不驗證 SSL 且模擬 Chrome 的 curl_cffi Session (針對 yfinance 抓取)
try:
    from curl_cffi import requests as requests_cffi
    session_yf = requests_cffi.Session(impersonate="chrome", verify=False)
    print("[+] 成功載入 curl_cffi 並建立模擬 Chrome 的免驗證 Session。")
except Exception as e:
    session_yf = None
    print(f"[-] 無法載入 curl_cffi: {e}，yfinance 將使用預設 Session。")

# ==========================================
# 1. 載入設定與初始化
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.html")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

# 建立報告目錄
if not os.path.exists(REPORTS_DIR):
    os.makedirs(REPORTS_DIR)

# 讀取設定檔
config = {
    "gemini_api_key": "YOUR_GEMINI_API_KEY",
    "rss_feeds": [],
    "monitored_tickers": [],
    "color_scheme": "us_standard"
}

if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    except Exception as e:
        print(f"[-] 讀取 config.json 失敗: {e}")

# 取得 Gemini API 金鑰 (優先採用環境變數，其次採用設定檔)
api_key = os.environ.get("GEMINI_API_KEY", config.get("gemini_api_key", ""))
has_api_key = api_key and api_key != "YOUR_GEMINI_API_KEY"
client_genai = None

if has_api_key:
    try:
        # 建立不進行 SSL 驗證的 httpx.Client (解決企業網路對 Gemini API 的阻斷問題)
        httpx_client = httpx.Client(verify=False)
        
        # 使用最新的 google-genai 用戶端初始化，並傳入免驗證 httpx 客戶端
        client_genai = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(httpx_client=httpx_client)
        )
        print("[+] Gemini (google-genai) API 用戶端初始化成功。")
    except Exception as e:
        print(f"[-] Gemini API 初始化失敗: {e}")
        has_api_key = False
else:
    print("[-] 未偵測到有效的 Gemini API 金鑰。系統將採用「標準聚合模式」運行。")

# ==========================================
# 2. 獲取美股主要指數與監控個股數據
# ==========================================
def get_ticker_data(ticker_symbol):
    try:
        # 使用我們建立的免 SSL 驗證且模擬 Chrome 的 curl_cffi Session
        ticker = yf.Ticker(ticker_symbol, session=session_yf)
        
        # 獲取最近 5 天數據以計算最新一天的變化量
        hist = ticker.history(period="5d")
        if hist.empty:
            print(f"[-] {ticker_symbol} 歷史數據為空")
            return None
            
        latest_close = hist['Close'].iloc[-1]
        
        # 獲取前一日收盤價以計算漲跌
        if len(hist) >= 2:
            prev_close = hist['Close'].iloc[-2]
            change = latest_close - prev_close
            change_pct = (change / prev_close) * 100
        else:
            change = 0.0
            change_pct = 0.0
            
        # 嘗試獲取公司中文簡稱或英文名稱
        name = ticker_symbol
        try:
            info = ticker.info
            if info and isinstance(info, dict):
                name = info.get('shortName', ticker_symbol)
        except Exception:
            # 忽視 info 獲取失敗，直接使用 ticker 代號作為名稱
            pass
        
        # 翻譯與美化常見標的名稱
        custom_names = {
            "^GSPC": "標普 500 指數",
            "^IXIC": "那斯達克指數",
            "^DJI": "道瓊工業指數",
            "^VIX": "波動率指數 (VIX)",
            "SPY": "S&P 500 ETF",
            "QQQ": "Nasdaq 100 ETF",
            "AAPL": "蘋果公司",
            "NVDA": "輝達",
            "TSLA": "特斯拉",
            "TSM": "台積電 ADR"
        }
        if ticker_symbol in custom_names:
            name = custom_names[ticker_symbol]

        return {
            "symbol": ticker_symbol,
            "name": name,
            "price": latest_close,
            "change": change,
            "change_pct": change_pct
        }
    except Exception as e:
        print(f"[-] 獲取 {ticker_symbol} 數據失敗: {e}")
        return None

# ==========================================
# 3. 獲取 RSS 財經新聞
# ==========================================
def fetch_rss_news(feeds):
    news_items = []
    print("[+] 正在抓取財經新聞 RSS...")
    for feed in feeds:
        url = feed.get("url")
        name = feed.get("name", "財經源")
        try:
            # 全域 monkeypatch 已經處理了 urllib 的 SSL 憑證驗證，這裡直接 parse 即可
            parsed = feedparser.parse(url)
            # 每個新聞源取前 8 條新聞
            for entry in parsed.entries[:8]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")
                
                # 清除 summary 中的 HTML 標籤
                if summary:
                    summary = BeautifulSoup(summary, "html.parser").get_text().strip()
                
                if title and link:
                    news_items.append({
                        "source": name,
                        "title": title,
                        "link": link,
                        "summary": summary[:250] + "..." if len(summary) > 250 else summary
                    })
        except Exception as e:
            print(f"[-] 解析新聞源 {name} ({url}) 失敗: {e}")
    
    # 依標題去重
    seen_titles = set()
    unique_news = []
    for item in news_items:
        if item["title"].lower() not in seen_titles:
            seen_titles.add(item["title"].lower())
            unique_news.append(item)
            
    # 只取最關鍵的前 12 條進行後續處理
    return unique_news[:12]

# ==========================================
# 4. 抓取今日美國重要經濟日曆
# ==========================================
def fetch_economic_calendar():
    print("[+] 正在獲取今日美國經濟日曆...")
    events = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        # 抓取 Yahoo Finance 經濟日曆 (verify=False 繞過 SSL)
        url = "https://finance.yahoo.com/calendar/economic/"
        r = session_requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            table = soup.find('table')
            if table:
                rows = table.find_all('tr')[1:8] # 前 7 個事件
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 3:
                        time_str = cols[0].get_text(strip=True)
                        event_name = cols[1].get_text(strip=True)
                        actual = cols[2].get_text(strip=True) if len(cols) > 2 else "-"
                        forecast = cols[3].get_text(strip=True) if len(cols) > 3 else "-"
                        prior = cols[4].get_text(strip=True) if len(cols) > 4 else "-"
                        
                        events.append({
                            "time": time_str,
                            "event": event_name,
                            "actual": actual,
                            "forecast": forecast,
                            "prior": prior
                        })
    except Exception as e:
        print(f"[-] 抓取經濟日曆失敗: {e}")
        
    # 如果沒抓到數據，提供預設的高關聯美股經濟數據範本
    if not events:
        events = [
            {"time": "08:30 AM", "event": "美國初請失業金人數 (Weekly Initial Jobless Claims)", "actual": "未公佈", "forecast": "220K", "prior": "222K"},
            {"time": "09:45 AM", "event": "標普全球美國製造業 PMI (S&P Global US Manufacturing PMI)", "actual": "未公佈", "forecast": "50.5", "prior": "50.0"},
            {"time": "10:00 AM", "event": "美國成屋銷售年化總數 (Existing Home Sales)", "actual": "未公佈", "forecast": "3.85M", "prior": "3.90M"}
        ]
    return events

# ==========================================
# 5. 整合與 AI 智慧分析
# ==========================================
def get_ai_analysis(indices_data, watchlist_data, news_items, calendar_data):
    if not has_api_key or client_genai is None:
        return None
        
    print("[+] 正在透過 Gemini API 進行深度市場分析與翻譯...")
    
    # 準備送給 AI 的數據結構
    input_data = {
        "date": datetime.date.today().strftime("%Y-%m-%d"),
        "market_indices": [
            {"symbol": x["symbol"], "name": x["name"], "price": f"{x['price']:.2f}", "change_pct": f"{x['change_pct']:.2f}%"} 
            for x in indices_data if x
        ],
        "watchlist": [
            {"symbol": x["symbol"], "name": x["name"], "price": f"{x['price']:.2f}", "change_pct": f"{x['change_pct']:.2f}%"} 
            for x in watchlist_data if x
        ],
        "news_articles": [
            {"source": x["source"], "title": x["title"], "summary": x["summary"]} 
            for x in news_items
        ],
        "economic_calendar": calendar_data
    }
    
    prompt = f"""
你是一位資深的華爾街美股策略分析師。請分析以下今日最新的美股市場行情與新聞數據，並以「繁體中文」輸出結構化的分析結果。

輸入數據如下：
{json.dumps(input_data, ensure_ascii=False, indent=2)}

請嚴格根據以下 JSON 格式回傳分析結果。不要包含任何 markdown 標記（如 ```json），只回傳純 JSON 字串：

{{
  "hero_summary": "今日美股大盤分析...（請用專業、流暢的繁體中文分析昨日美股收盤或今日晨間的核心驅動因素，長度約 120-180 字）",
  "hero_bullets": [
    "第一點關鍵要聞總結與對股市的潛在影響（1-2句）",
    "第二點關鍵要聞總結與對股市的潛在影響（1-2句）",
    "第三點關鍵要聞總結與對股市的潛在影響（1-2句）"
  ],
  "market_sentiment": {{
    "score": 60, // 整體市場情緒分數，範圍 0 (極度恐慌/看空) 到 100 (極度貪婪/看多)
    "label": "偏向樂觀", // 對應情緒的簡短中文標籤（例如：極度悲觀、偏向悲觀、中性觀望、偏向樂觀、極度樂觀）
    "class": "bullish" // 對應情緒的 CSS Class。看多為 "bullish", 看空為 "bearish", 中性為 "neutral"
  }},
  "watchlist_analysis": {{
    // 針對 watchlist_data 中每個 Symbol 提供一小句（約 20-30 字）的 AI 觀點或波動原因解析
    "AAPL": "...",
    "NVDA": "...",
    "TSLA": "..."
  }},
  "news_analysis": [
    // 依序針對 news_articles 的每一篇新聞進行繁體中文翻譯與情緒解讀（必須保留原有順序）
    {{
      "title_zh": "精準、地道的財經繁體中文標題翻譯",
      "summary_zh": "精簡的繁體中文核心摘要，直指這則新聞對市場或特定板塊的影響",
      "sentiment": "bullish", // 該新聞的情緒傾向: "bullish" (利多), "bearish" (利空), "neutral" (中性)
      "impact": "high" // 對今日美股的影響權重: "high" (高), "medium" (中), "low" (低)
    }}
  ]
}}

請注意：
1. 財經術語必須符合台灣市場常用習慣（例如：Nasdaq 翻譯為「那斯達克」、ETF 保持英文、Yield 翻譯為「殖利率」、Federal Reserve 翻譯為「聯準會」）。
2. news_analysis 陣列中的項目必須與 news_articles 的順序完全一致，且項目個數相同。
"""
    
    try:
        # 使用最新的 google-genai 介面
        response = client_genai.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        # 剝離可能多包裹的 markdown json 標籤，確保 loads 順暢
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:].strip()
        elif text.startswith("```"):
            text = text[3:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
            
        return json.loads(text, strict=False)
    except Exception as e:
        print(f"[-] Gemini API 呼叫或解析 JSON 失敗: {e}")
        try:
            if 'response' in locals() and response and hasattr(response, 'text'):
                print(f"[i] API 回傳原始文字內容 (前 500 字)：\n{response.text[:500]}")
        except Exception:
            pass
        return None

# ==========================================
# 6. 生成 HTML 報告
# ==========================================
def generate_report(indices_data, watchlist_data, news_items, calendar_data, ai_result):
    print("[+] 正在套用視覺範本生成 HTML 報告...")
    
    # 讀取範本
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()
        
    # 日期計算 (強制使用台灣時區 UTC+8)
    tz_taiwan = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz_taiwan)
    today_tw_str = now.strftime("%Y-%m-%d")
    today_us_str = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    gen_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 替換日期
    html = html.replace("{{REPORT_DATE}}", today_tw_str)
    html = html.replace("{{US_DATE}}", today_us_str)
    html = html.replace("{{GENERATION_TIME}}", gen_time_str)
    
    # 2. 替換大盤指數卡片 (Market Pulse)
    import urllib.parse
    pulse_html = ""
    if not indices_data:
        pulse_html = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-secondary); padding: 1rem;">[提示] 無法即時取得大盤指數數據（請檢查網路或代理伺服器）</div>'
    else:
        for idx in indices_data:
            if not idx:
                continue
                
            direction_class = "neutral"
            icon = '<i class="fa-solid fa-minus"></i>'
            
            if idx["change_pct"] > 0.05:
                direction_class = "up"
                icon = '<i class="fa-solid fa-arrow-trend-up"></i>'
            elif idx["change_pct"] < -0.05:
                direction_class = "down"
                icon = '<i class="fa-solid fa-arrow-trend-down"></i>'
                
            sign = "+" if idx["change_pct"] > 0 else ""
            
            # URL 編碼指數代碼 (如 ^GSPC -> %5EGSPC)
            encoded_symbol = urllib.parse.quote(idx['symbol'])
            quote_url = f"https://finance.yahoo.com/quote/{encoded_symbol}"
            
            pulse_html += f"""
            <div class="pulse-card glass-card {direction_class}" onclick="window.open('{quote_url}', '_blank')" style="cursor: pointer;" title="點擊查看 {idx['name']} 的詳細行情分析">
                <div class="pulse-header">
                    <span class="pulse-name">{idx['name']}</span>
                    <span class="pulse-ticker">{idx['symbol']}</span>
                </div>
                <div class="pulse-value">{idx['price']:,.2f}</div>
                <div class="pulse-change">
                    {icon} {sign}{idx['change']:,.2f} ({sign}{idx['change_pct']:.2f}%)
                </div>
            </div>
            """
    html = html.replace("{{MARKET_PULSE_CARDS}}", pulse_html)
    
    # 3. 替換 AI 英雄總結板塊
    if ai_result:
        hero_summary = ai_result.get("hero_summary", "今日市場主要受總體經濟數據與主要企業財報驅動。")
        
        bullets_html = ""
        for bullet in ai_result.get("hero_bullets", []):
            bullets_html += f"<li>{bullet}</li>"
            
        sentiment_data = ai_result.get("market_sentiment", {"score": 50, "label": "中性觀望", "class": "neutral"})
        sent_score = sentiment_data.get("score", 50)
        sent_label = sentiment_data.get("label", "中性觀望")
        sent_class = sentiment_data.get("class", "neutral")
    else:
        # 標準模式 (降級) 的 AI 板塊
        hero_summary = "目前正以標準聚合模式運行。請至 <code>config.json</code> 中配置您的 <code>gemini_api_key</code> 以啟用由 Gemini 驅動的每日美股深度中文情緒分析、新聞翻譯與關鍵亮點整理。"
        bullets_html = """
        <li><b>如何啟用 AI 功能？</b> 在專案目錄下的 <code>config.json</code> 中，將 <code>gemini_api_key</code> 欄位填入您的 Gemini API 金鑰。</li>
        <li><b>標準模式限制</b>：本報告中的新聞標題與內容為英文原文，且無情緒標籤，部分解讀功能受限。</li>
        <li><b>指數與經濟數據</b>：大盤指數與今日美國經濟指標依舊能正常即時獲取。</li>
        """
        sent_score = 50
        sent_label = "中性觀望 (標準模式)"
        sent_class = "neutral"
        
    html = html.replace("{{AI_HERO_SUMMARY}}", hero_summary)
    html = html.replace("{{AI_HERO_BULLETS}}", bullets_html)
    html = html.replace("{{SENTIMENT_CLASS}}", sent_class)
    html = html.replace("{{SENTIMENT_TEXT}}", sent_label)
    html = html.replace("{{SENTIMENT_PERCENT}}", str(sent_score))
    
    # 4. 替換重點觀察股卡片 (Watchlist)
    watchlist_html = ""
    if not watchlist_data:
        watchlist_html = '<div style="color: var(--text-secondary); text-align: center; padding: 2rem; width: 100%;">[提示] 無法即時取得關注個股行情</div>'
    else:
        for item in watchlist_data:
            if not item:
                continue
                
            direction_class = "neutral"
            sign = ""
            if item["change_pct"] > 0.05:
                direction_class = "up"
                sign = "+"
            elif item["change_pct"] < -0.05:
                direction_class = "down"
                
            ticker_ai_note = ""
            if ai_result:
                ticker_ai_note = ai_result.get("watchlist_analysis", {}).get(item["symbol"], "")
                
            if ticker_ai_note:
                ticker_ai_html = f'<div class="ticker-ai-impact"><i class="fa-solid fa-robot"></i> {ticker_ai_note}</div>'
            else:
                ticker_ai_html = ""
                
            # URL 編碼關注個股代碼 (如 AAPL -> AAPL, ^IXIC -> %5EIXIC)
            encoded_symbol = urllib.parse.quote(item['symbol'])
            quote_url = f"https://finance.yahoo.com/quote/{encoded_symbol}"
            
            watchlist_html += f"""
            <div class="watchlist-card glass-card {direction_class}" onclick="window.open('{quote_url}', '_blank')" style="cursor: pointer;" title="點擊查看 {item['name']} ({item['symbol']}) 的詳細行情分析">
                <div class="watchlist-left">
                    <div class="ticker-avatar">{item['symbol'][:2]}</div>
                    <div class="ticker-details">
                        <span class="ticker-sym">{item['symbol']}</span>
                        <span class="ticker-name">{item['name']}</span>
                    </div>
                </div>
                <div class="watchlist-right">
                    <span class="ticker-price">${item['price']:.2f}</span>
                    <span class="ticker-pct">{sign}{item['change_pct']:.2f}%</span>
                </div>
                {ticker_ai_html}
            </div>
            """
    html = html.replace("{{WATCHLIST_CARDS}}", watchlist_html)
    
    # 5. 替換經濟日曆 (Economic Calendar)
    calendar_html = ""
    for ev in calendar_data:
        calendar_html += f"""
        <div class="calendar-item glass-card">
            <div class="calendar-item-header">
                <span class="calendar-time"><i class="fa-regular fa-clock"></i> {ev['time']}</span>
            </div>
            <div class="calendar-event">{ev['event']}</div>
            <div class="calendar-values-grid">
                <div class="calendar-val-box">
                    <span class="cal-label">實際值</span>
                    <span class="cal-val actual">{ev['actual']}</span>
                </div>
                <div class="calendar-val-box">
                    <span class="cal-label">預測值</span>
                    <span class="cal-val">{ev['forecast']}</span>
                </div>
                <div class="calendar-val-box">
                    <span class="cal-label">前值</span>
                    <span class="cal-val">{ev['prior']}</span>
                </div>
            </div>
        </div>
        """
    html = html.replace("{{ECONOMIC_CALENDAR_ITEMS}}", calendar_html)
    
    # 6. 替換今日焦點財經新聞
    news_cards_html = ""
    rendered_news_count = 0
    
    if not news_items:
        news_cards_html = """
        <div style="text-align: center; color: var(--text-muted); padding: 4rem 2rem; width: 100%;">
            <i class="fa-solid fa-cloud-slash" style="font-size: 3rem; margin-bottom: 1.5rem; color: var(--text-muted);"></i>
            <p>目前未抓取到任何焦點財經新聞。</p>
            <p style="font-size: 0.85rem; margin-top: 0.5rem; color: var(--text-muted);">請確保您的電腦已連線上網，或嘗試於 config.json 中更換其他新聞 RSS 源。</p>
        </div>
        """
    else:
        for i, item in enumerate(news_items):
            # 如果有 AI 處理，使用中文翻譯；否則使用英文
            if ai_result and i < len(ai_result.get("news_analysis", [])):
                ai_news = ai_result["news_analysis"][i]
                title_display = ai_news.get("title_zh", item["title"])
                summary_display = ai_news.get("summary_zh", item["summary"])
                sentiment = ai_news.get("sentiment", "neutral")
                impact = ai_news.get("impact", "medium")
                
                # 只保留中、高影響力新聞，過濾低影響力 (low) 的新聞
                if impact == "low":
                    continue
            else:
                title_display = item["title"]
                summary_display = item["summary"]
                sentiment = "neutral"
                impact = "medium"
                
            rendered_news_count += 1
            sentiment_labels = {
                "bullish": "看多情緒",
                "bearish": "看空情緒",
                "neutral": "中性觀望"
            }
            impact_labels = {
                "high": "高影響力",
                "medium": "中影響力",
                "low": "低影響力"
            }
            
            sent_label = sentiment_labels.get(sentiment, "中性觀望")
            imp_label = impact_labels.get(impact, "中影響力")
            
            news_cards_html += f"""
            <div class="news-card glass-card" data-sentiment="{sentiment}" data-impact="{impact}">
                <div class="news-card-header">
                    <div class="news-meta-left">
                        <span class="news-source">{item['source']}</span>
                        <span><i class="fa-regular fa-clock"></i> 今日晨間</span>
                    </div>
                    <div class="news-badges">
                        <span class="badge-sentiment {sentiment}"><i class="fa-solid fa-circle-half-stroke"></i> {sent_label}</span>
                        <span class="badge-impact {impact}"><i class="fa-solid fa-bolt"></i> {imp_label}</span>
                    </div>
                </div>
                <a href="{item['link']}" target="_blank" class="news-title">{title_display}</a>
                <div class="news-summary">{summary_display}</div>
                <div class="news-footer">
                    <a href="{item['link']}" target="_blank" class="news-link">閱讀英文原文 <i class="fa-solid fa-arrow-up-right-from-square"></i></a>
                </div>
            </div>
            """
            
    # 如果過濾後沒有任何中高影響力新聞，顯示友好提示
    if news_items and rendered_news_count == 0:
        news_cards_html = """
        <div style="text-align: center; color: var(--text-muted); padding: 4rem 2rem; width: 100%;">
            <i class="fa-solid fa-filter" style="font-size: 3rem; margin-bottom: 1.5rem; color: var(--text-muted);"></i>
            <p>今日未偵測到中、高影響力的財經新聞。</p>
            <p style="font-size: 0.85rem; margin-top: 0.5rem; color: var(--text-muted);">所有抓取的新聞均評估為低影響力，已被系統自動過濾。</p>
        </div>
        """
        
    html = html.replace("{{NEWS_COUNT}}", str(rendered_news_count))
    html = html.replace("{{NEWS_CARDS}}", news_cards_html)
    
    # 儲存報告
    report_filename = f"report_{today_tw_str}.html"
    report_path = os.path.join(REPORTS_DIR, report_filename)
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
        
    print(f"[+] 報告生成成功！存檔路徑：{report_path}")
    return report_path

# ==========================================
# 6.5. 歷史報告入口網站生成器與 GitHub 發布引擎
# ==========================================
def generate_index_page():
    print("[+] 正在生成歷史報告入口網站 (index.html)...")
    
    # 1. 取得 reports 目錄下的所有報告檔案
    if not os.path.exists(REPORTS_DIR):
        os.makedirs(REPORTS_DIR)
        
    report_files = [f for f in os.listdir(REPORTS_DIR) if f.startswith("report_") and f.endswith(".html")]
    
    # 2. 提取日期並排序 (YYYY-MM-DD)
    reports = []
    for f in report_files:
        date_str = f.replace("report_", "").replace(".html", "")
        reports.append({
            "filename": f,
            "date": date_str,
            "path": f"reports/{f}"
        })
        
    # 按日期降序排序 (最新的在最前面)
    reports.sort(key=lambda x: x["date"], reverse=True)
    
    if not reports:
        print("[-] reports 目錄下沒有任何報告檔案，無法生成入口網頁。")
        return None
        
    latest_report = reports[0]
    
    # 3. 渲染歷史列表 HTML
    history_cards_html = ""
    for r in reports:
        history_cards_html += f"""
        <a href="reports/{r['filename']}" class="glass-card history-card" style="text-decoration: none; color: inherit;">
            <div class="history-card-content">
                <div class="history-icon"><i class="fa-solid fa-calendar-day"></i></div>
                <div class="history-info">
                    <span class="history-date">{r['date']}</span>
                    <span class="history-title">美股晨間股市訊息與情緒分析報告</span>
                </div>
                <div class="history-arrow"><i class="fa-solid fa-chevron-right"></i></div>
            </div>
        </a>
        """
    
    index_html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Charle's 美股市場資訊分析 dashboard</title>
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <!-- FontAwesome for Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {{
            --bg-color: #0b0f19;
            --card-bg: rgba(17, 24, 39, 0.75);
            --card-border: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --text-muted: #6b7280;
            --accent-blue: #3b82f6;
            --accent-purple: #8b5cf6;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', 'Outfit', system-ui, -apple-system, sans-serif;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
            position: relative;
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(139, 92, 246, 0.08) 0%, transparent 40%);
            background-attachment: fixed;
            padding: 3rem 1.5rem;
        }}

        .ambient-glow {{
            position: absolute;
            width: 400px;
            height: 400px;
            background: radial-gradient(circle, rgba(139, 92, 246, 0.12) 0%, transparent 70%);
            top: -100px;
            right: 15%;
            pointer-events: none;
            z-index: 0;
            filter: blur(50px);
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
            position: relative;
            z-index: 1;
        }}

        header {{
            text-align: center;
            margin-bottom: 3.5rem;
        }}

        .header-icon {{
            font-size: 3rem;
            background: linear-gradient(135deg, #6366f1, #a855f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            filter: drop-shadow(0 2px 10px rgba(99, 102, 241, 0.4));
            margin-bottom: 1rem;
            display: inline-block;
        }}

        h1 {{
            font-family: 'Outfit', sans-serif;
            font-size: 2.5rem;
            font-weight: 800;
            letter-spacing: -0.5px;
            background: linear-gradient(to right, #ffffff, #d1d5db);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}

        .subtitle {{
            color: var(--text-secondary);
            font-size: 1.1rem;
            font-weight: 400;
        }}

        .glass-card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            backdrop-filter: blur(16px) saturate(180%);
            -webkit-backdrop-filter: blur(16px) saturate(180%);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}

        .glass-card:hover {{
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
            transform: translateY(-2px);
        }}

        /* Features Layout (Latest Report & ETF Dashboard) */
        .features-section {{
            margin-bottom: 3.5rem;
        }}

        .section-title {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: 1.2rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: #ffffff;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}

        .section-title i {{
            color: #818cf8;
        }}

        .features-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1.5rem;
        }}

        @media (max-width: 768px) {{
            .features-grid {{
                grid-template-columns: 1fr;
            }}
        }}

        .feature-card {{
            padding: 2.2rem;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 290px;
        }}

        /* Latest Report Card: Neon Purple */
        .latest-card {{
            background: linear-gradient(135deg, rgba(17, 24, 39, 0.85), rgba(88, 28, 135, 0.15));
            border: 1px solid rgba(139, 92, 246, 0.25);
            box-shadow: 0 10px 40px rgba(99, 102, 241, 0.15);
        }}

        .latest-badge {{
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.25), rgba(99, 102, 241, 0.25));
            border: 1px solid rgba(139, 92, 246, 0.4);
            color: #c084fc;
        }}

        /* ETF Dashboard Card: Neon Green */
        .etf-card {{
            background: linear-gradient(135deg, rgba(17, 24, 39, 0.85), rgba(6, 78, 59, 0.15));
            border: 1px solid rgba(16, 185, 129, 0.25);
            box-shadow: 0 10px 40px rgba(16, 185, 129, 0.1);
        }}

        .etf-badge {{
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(5, 150, 105, 0.2));
            border: 1px solid rgba(16, 185, 129, 0.4);
            color: #34d399;
        }}

        .feature-badge {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.3rem 0.8rem;
            border-radius: 50px;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            margin-bottom: 1.2rem;
            width: fit-content;
        }}

        .feature-card-title {{
            font-family: 'Outfit', sans-serif;
            font-size: 2.2rem;
            font-weight: 800;
            margin-bottom: 0.6rem;
            background: linear-gradient(to right, #ffffff, #e5e7eb);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .latest-card .feature-card-title {{
            background: linear-gradient(to right, #ffffff, #a5b4fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .etf-card .feature-card-title {{
            background: linear-gradient(to right, #ffffff, #a7f3d0);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .feature-card-desc {{
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.6;
            margin-bottom: 2rem;
            flex-grow: 1;
        }}

        .btn-action {{
            display: inline-flex;
            align-items: center;
            gap: 0.6rem;
            color: #ffffff;
            padding: 0.9rem 1.8rem;
            border-radius: 12px;
            font-weight: 700;
            text-decoration: none;
            transition: all 0.2s;
            border: 1px solid rgba(255, 255, 255, 0.1);
            width: 100%;
            justify-content: center;
        }}

        .btn-latest {{
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            box-shadow: 0 4px 15px rgba(99, 102, 241, 0.3);
        }}

        .btn-latest:hover {{
            background: linear-gradient(135deg, #4f46e5, #7c3aed);
            box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
            transform: translateY(-1px);
        }}

        .btn-etf {{
            background: linear-gradient(135deg, #10b981, #059669);
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.25);
        }}

        .btn-etf:hover {{
            background: linear-gradient(135deg, #059669, #047857);
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.35);
            transform: translateY(-1px);
        }}

        .btn-action:active {{
            transform: translateY(1px);
        }}

        /* History Section */
        .history-list {{
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }}

        .history-card {{
            padding: 1.2rem 1.5rem;
            cursor: pointer;
        }}

        .history-card-content {{
            display: flex;
            align-items: center;
            gap: 1.2rem;
        }}

        .history-icon {{
            width: 44px;
            height: 44px;
            border-radius: 12px;
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.2);
            color: #818cf8;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
        }}

        .history-info {{
            display: flex;
            flex-direction: column;
            gap: 0.2rem;
            flex-grow: 1;
        }}

        .history-date {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.1rem;
            font-weight: 700;
            color: #ffffff;
        }}

        .history-title {{
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}

        .history-arrow {{
            color: var(--text-muted);
            transition: transform 0.2s;
            font-size: 0.9rem;
        }}

        .history-card:hover .history-arrow {{
            color: #818cf8;
            transform: translateX(4px);
        }}

        /* Footer */
        footer {{
            margin-top: 5rem;
            padding-top: 2rem;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            text-align: center;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}

        /* Mobile Responsive */
        @media (max-width: 600px) {{
            body {{
                padding: 2rem 1rem;
            }}
            
            h1 {{
                font-size: 2rem;
            }}
            
            .feature-card {{
                padding: 1.5rem;
                min-height: 250px;
            }}
            
            .feature-card-title {{
                font-size: 1.8rem;
            }}
            
            .btn-action {{
                width: 100%;
                justify-content: center;
            }}
        }}
    </style>
</head>
<body>
    <div class="ambient-glow"></div>
    <div class="container">
        <!-- HEADER -->
        <header>
            <div class="header-icon"><i class="fa-solid fa-chart-line-up"></i></div>
            <h1>美股市場深度分析 dashboard</h1>
            <p class="subtitle">每日自動化晨報與 AI 情緒分析存檔庫</p>
        </header>

        <!-- FEATURES SECTION -->
        <section class="features-section">
            <h2 class="section-title"><i class="fa-solid fa-wand-magic-sparkles"></i> 精選分析看板</h2>
            <div class="features-grid">
                <!-- Latest Report Card -->
                <div class="glass-card feature-card latest-card">
                    <div>
                        <span class="feature-badge latest-badge"><i class="fa-solid fa-bolt"></i> Today's Report</span>
                        <div class="feature-card-title">{latest_report['date']}</div>
                        <p class="feature-card-desc">
                            今日美股市場指數、自選監控股行情、最新財經晨報及 AI Sentiment 深度情緒分析。
                        </p>
                    </div>
                    <a href="{latest_report['path']}" class="btn-action btn-latest"><i class="fa-solid fa-book-open"></i> 閱讀完整分析報告</a>
                </div>

                <!-- ETF Dashboard Card -->
                <div class="glass-card feature-card etf-card">
                    <div>
                        <span class="feature-badge etf-badge"><i class="fa-solid fa-chart-pie"></i> ETF Trend Dashboard</span>
                        <div class="feature-card-title">ETF 趨勢</div>
                        <p class="feature-card-desc">
                            精選 ETF 歷史持股比例、淨值趨勢走勢及權重變化多維度可視化儀表板。
                        </p>
                    </div>
                    <a href="ETF%20%E6%B7%A8%E5%80%BC%E8%88%87%E6%8C%81%E8%82%A1%E8%B6%A8%E5%8B%A2%E5%84%80%E8%A1%A8%E6%9D%BF.html" class="btn-action btn-etf"><i class="fa-solid fa-chart-line"></i> 進入 ETF 儀表板</a>
                </div>
            </div>
        </section>

        <!-- HISTORICAL ARCHIVE -->
        <section class="archive-section">
            <h2 class="section-title"><i class="fa-solid fa-box-archive"></i> 歷史報告存檔</h2>
            <div class="history-list">
                {history_cards_html}
            </div>
        </section>

        <!-- FOOTER -->
        <footer>
            <p>© 2026 Charle's Stock Dashboard. All rights reserved.</p>
            <p style="margin-top: 0.4rem; font-size: 0.75rem;">系統採用 google-genai API 與 yfinance 每日自動更新</p>
        </footer>
    </div>
</body>
    </html>
<!-- Deploy ID: {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")} -->"""
    
    # 寫入 index.html
    index_path = os.path.join(BASE_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)
        
    print(f"[+] 入口網站生成成功！存檔路徑：{index_path}")
    return index_path



# ==========================================
# 7. 主程式控制流程
# ==========================================
def main():
    print("==========================================")
    tz_taiwan = datetime.timezone(datetime.timedelta(hours=8))
    print(f"美股晨間股市訊息與情緒分析系統啟動 (時間: {datetime.datetime.now(tz_taiwan)} [台灣時間])")
    print("==========================================")
    
    # 1. 抓取大盤與個股
    print("[+] 正在從 Yahoo Finance 抓取市場指數與個股即時數據...")
    indices = ["^GSPC", "^IXIC", "^DJI", "^VIX"]
    indices_data = [get_ticker_data(t) for t in indices]
    
    watchlist = config.get("monitored_tickers", ["AAPL", "NVDA", "TSLA"])
    watchlist_data = [get_ticker_data(t) for t in watchlist]
    
    # 過濾失敗的數據
    indices_data = [x for x in indices_data if x is not None]
    watchlist_data = [x for x in watchlist_data if x is not None]
    
    # 2. 抓取 RSS
    rss_feeds = config.get("rss_feeds", [])
    news_items = fetch_rss_news(rss_feeds)
    
    if not news_items:
        print("[-] 未抓取到 any 財經新聞。請檢查網路連接或 config.json 設定。")
        
    # 3. 抓取經濟日曆
    calendar_data = fetch_economic_calendar()
    
    # 4. 呼叫 AI 進行分析
    ai_result = None
    if has_api_key and news_items:
        ai_result = get_ai_analysis(indices_data, watchlist_data, news_items, calendar_data)
        
    # 5. 生成精美 HTML 報告
    report_path = generate_report(indices_data, watchlist_data, news_items, calendar_data, ai_result)
    
    # 6. 生成歷史報告入口網頁 (index.html)
    index_path = generate_index_page()
    
    # 7. GitHub Pages 發布已轉移至 GitHub Actions 雲端原生 git push，此處已安全移去

    
    # 8. 自動開啟報告
    print("[+] 正在自動在瀏覽器中開啟生成的報告...")
    try:
        webbrowser.open(f"file:///{report_path}")
    except Exception as e:
        print(f"[-] 無法自動開啟瀏覽器: {e}")
        
    print("[+] 美股晨報抓取任務已圓滿完成！")

if __name__ == "__main__":
    main()

