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

TOKEN = os.environ.get("FINMIND_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
GCP_SA_KEY_BASE64 = os.environ.get("GCP_SA_KEY_BASE64", "")

def calculate_kd(df, n=9):
    df = df.sort_values('date').copy()
    low_min = df['min'].rolling(window=n).min()
    high_max = df['max'].rolling(window=n).max()
    
    rsv = ((df['close'] - low_min) / (high_max - low_min) * 100).fillna(50)
    
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

    today = datetime.now()
    start_date = (today - timedelta(days=730)).strftime('%Y-%m-%d')

    golden_list, death_list = [], []

    def fetch_worker(row):
        sid = row['stock_id']
        sname = row['stock_name']
        try:
            df = dl.taiwan_stock_month_k_bar(stock_id=sid, start_date=start_date)
            if df.empty or len(df) < 10:
                return
            
            df_kd = calculate_kd(df)
            prev_row = df_kd.iloc[-2]
            curr_row = df_kd.iloc[-1]

            k_prev, d_prev = prev_row['K'], prev_row['D']
            k_curr, d_curr = curr_row['K'], curr_row['D']
            last_date = curr_row['date']

            if k_prev <= d_prev and k_curr > d_curr:
                golden_list.append([sid, sname, k_curr, d_curr, last_date])
            elif k_prev >= d_prev and k_curr < d_curr:
                death_list.append([sid, sname, k_curr, d_curr, last_date])
        except:
            pass

    print(f"啟動多執行緒處理全市場 {len(targets)} 檔標的...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fetch_worker, [row for _, row in targets.iterrows()]))

    return golden_list, death_list

def write_sheets(golden, death):
    creds_raw = base64.b64decode(GCP_SA_KEY_BASE64).decode('utf-8')
    creds_dict = json.loads(creds_raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    def update_tab(tab_name, rows):
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows="1000", cols="5")
        ws.clear()
        header = [["股號", "名稱", "當前月K值", "當前月D值", "更新月份"]]
        if rows:
            ws.update(values=header + rows, range_name=f"A1:E{len(rows)+1}")
        else:
            ws.update(values=[["本月無符合交叉標的"]], range_name="A1")

    update_tab("黃金", golden)
    update_tab("死亡", death)
    print("Google Sheets 更新成功！")

if __name__ == '__main__':
    g, d = scan_all()
    print(f"篩選完成：黃金交叉 {len(g)} 檔，死亡交叉 {len(d)} 檔。寫入中...")
    write_sheets(g, d)
