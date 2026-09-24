import os
import json
import base64
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

try:
    from FinMind.data import DataLoader
except ImportError:
    try:
        from FinMind import DataLoader
    except ImportError:
        from FinMind.Data import DataLoader

TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
GCP_SA_KEY_BASE64 = os.environ.get("GCP_SA_KEY_BASE64", "").strip()

def calculate_kd(df, n=9):
    df = df.sort_values('date').copy()
    
    # 相容不同的欄位名稱 (min/low, max/high)
    low_col = 'min' if 'min' in df.columns else ('low' if 'low' in df.columns else None)
    high_col = 'max' if 'max' in df.columns else ('high' if 'high' in df.columns else None)
    close_col = 'close'
    
    if not low_col or not high_col:
        return None
        
    low_min = df[low_col].rolling(window=n).min()
    high_max = df[high_col].rolling(window=n).max()
    
    denom = (high_max - low_min).replace(0, 0.0001)
    rsv = ((df[close_col] - low_min) / denom * 100).fillna(50)
    
    k_list, d_list = [], []
    k, d = 50.0, 50.0
    for val in rsv:
        k = (2/3) * k + (1/3) * val
        d = (2/3) * d + (1/3) * k
        k_list.append(round(k, 2))
        d_list.append(round(d, 2))
        
    df['K'] = k_list
    df['D'] = d_list
    return df

def scan_all():
    dl = DataLoader()
    if TOKEN:
        dl.login_by_token(api_token=TOKEN)

    stock_info = dl.taiwan_stock_info()
    targets = stock_info[stock_info['type'].isin(['twse', 'tpex'])]

    # 抓取過去 3 年月資料
    today = datetime.now()
    start_date = (today - timedelta(days=1100)).strftime('%Y-%m-%d')

    golden_list, death_list = [], []
    checked_count = 0

    def fetch_worker(row):
        nonlocal checked_count
        sid = str(row['stock_id'])
        sname = str(row['stock_name'])
        try:
            df = dl.taiwan_stock_month_k_bar(stock_id=sid, start_date=start_date)
            if df is None or df.empty or len(df) < 10:
                return
            
            df_kd = calculate_kd(df)
            if df_kd is None or len(df_kd) < 2:
                return
                
            prev_row = df_kd.iloc[-2]
            curr_row = df_kd.iloc[-1]

            k_prev, d_prev = float(prev_row['K']), float(prev_row['D'])
            k_curr, d_curr = float(curr_row['K']), float(curr_row['D'])
            last_date = str(curr_row['date'])

            # 黃金交叉判定（上月 K <= D 且 本月 K > D）
            if k_prev <= d_prev and k_curr > d_curr:
                golden_list.append([sid, sname, k_curr, d_curr, last_date])
            # 死亡交叉判定（上月 K >= D 且 本月 K < D）
            elif k_prev >= d_prev and k_curr < d_curr:
                death_list.append([sid, sname, k_curr, d_curr, last_date])
                
            checked_count += 1
        except Exception as e:
            pass

    print(f"啟動多執行緒處理全市場 {len(targets)} 檔標的...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fetch_worker, [row for _, row in targets.iterrows()]))

    print(f"成功計算有效月線標的共：{checked_count} 檔")
    return golden_list, death_list

def write_sheets(golden, death):
    creds_raw = base64.b64decode(GCP_SA_KEY_BASE64).decode('utf-8')
    creds_dict = json.loads(creds_raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    print(f"連接試算表 ID: {SPREADSHEET_ID}")
    sh = gc.open_by_key(SPREADSHEET_ID)

    def update_tab(tab_name, rows):
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows="1500", cols="5")
        
        ws.clear()
        header = [["股號", "名稱", "當前月K值", "當前月D值", "更新月份"]]
        if rows:
            print(f"寫入【{tab_name}】共 {len(rows)} 筆標的...")
            ws.update(values=header + rows, range_name=f"A1:E{len(rows)+1}")
        else:
            print(f"【{tab_name}】無交叉標的，寫入標題提示。")
            ws.update(values=header + [["-", "本月無符合交叉標的", "-", "-", "-"]], range_name="A1:E2")

    update_tab("黃金", golden)
    update_tab("死亡", death)
    print("Google Sheets 全部寫入完成！")

if __name__ == '__main__':
    g, d = scan_all()
    print(f"篩選結算：黃金交叉 {len(g)} 檔，死亡交叉 {len(d)} 檔。")
    write_sheets(g, d)
