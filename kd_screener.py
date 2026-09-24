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

def fetch_daily_data(dl, sid, start_date):
    """
    相容 FinMind 不同版本的股價取得方法
    """
    try:
        if hasattr(dl, 'taiwan_stock_daily'):
            return dl.taiwan_stock_daily(stock_id=sid, start_date=start_date)
        elif hasattr(dl, 'get_data'):
            return dl.get_data(dataset='TaiwanStockPrice', data_id=sid, start_date=start_date)
    except Exception:
        pass
    return pd.DataFrame()

def calculate_kd(df_month, n=9):
    if len(df_month) < n + 1:
        return None
    
    df_month = df_month.sort_values('date').copy()
    low_min = df_month['min'].rolling(window=n).min()
    high_max = df_month['max'].rolling(window=n).max()
    
    denom = (high_max - low_min).replace(0, 0.0001)
    rsv = ((df_month['close'] - low_min) / denom * 100).fillna(50)
    
    k_list, d_list = [], []
    k, d = 50.0, 50.0
    for val in rsv:
        k = (2/3) * k + (1/3) * val
        d = (2/3) * d + (1/3) * k
        k_list.append(round(k, 2))
        d_list.append(round(d, 2))
        
    df_month['K'] = k_list
    df_month['D'] = d_list
    return df_month

def get_monthly_bars(df_daily):
    if df_daily.empty:
        return pd.DataFrame()
    df_daily['date'] = pd.to_datetime(df_daily['date'])
    df_daily = df_daily.set_index('date').sort_index()
    
    month_df = df_daily.resample('ME').agg({
        'open': 'first',
        'max': 'max',
        'min': 'min',
        'close': 'last'
    }).dropna().reset_index()
    
    month_df['date'] = month_df['date'].dt.strftime('%Y-%m')
    return month_df

def test_single_stock(dl, sid="2330", sname="台積電"):
    today = datetime.now()
    start_date = (today - timedelta(days=600)).strftime('%Y-%m-%d')
    df_daily = fetch_daily_data(dl, sid, start_date)
    if df_daily is not None and not df_daily.empty:
        df_m = get_monthly_bars(df_daily)
        df_kd = calculate_kd(df_m)
        if df_kd is not None:
            last3 = df_kd[['date', 'close', 'K', 'D']].tail(3).to_dict('records')
            print(f"【單股測試成功】{sid} {sname} 最近三期月KD: {last3}")
    else:
        print(f"【單股測試失敗】無法取得 {sid} 的股價資料")

def scan_all():
    dl = DataLoader()
    if TOKEN:
        dl.login_by_token(api_token=TOKEN)

    test_single_stock(dl, "2330", "台積電")
    test_single_stock(dl, "0050", "元大台灣50")

    stock_info = dl.taiwan_stock_info()
    targets = stock_info[stock_info['type'].isin(['twse', 'tpex'])]

    today = datetime.now()
    start_date = (today - timedelta(days=600)).strftime('%Y-%m-%d')

    golden_list, death_list = [], []
    valid_count = 0

    def fetch_worker(row):
        nonlocal valid_count
        sid = str(row['stock_id'])
        sname = str(row['stock_name'])
        try:
            df_daily = fetch_daily_data(dl, sid, start_date)
            if df_daily is None or df_daily.empty or len(df_daily) < 40:
                return

            df_month = get_monthly_bars(df_daily)
            if len(df_month) < 10:
                return

            df_kd = calculate_kd(df_month, n=9)
            if df_kd is None or len(df_kd) < 2:
                return

            prev_row = df_kd.iloc[-2]
            curr_row = df_kd.iloc[-1]

            k_prev, d_prev = float(prev_row['K']), float(prev_row['D'])
            k_curr, d_curr = float(curr_row['K']), float(curr_row['D'])
            month_str = str(curr_row['date'])

            if k_prev <= d_prev and k_curr > d_curr:
                golden_list.append([sid, sname, k_curr, d_curr, month_str])
            elif k_prev >= d_prev and k_curr < d_curr:
                death_list.append([sid, sname, k_curr, d_curr, month_str])

            valid_count += 1
        except Exception:
            pass

    print(f"啟動多執行緒處理全市場 {len(targets)} 檔標的...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fetch_worker, [row for _, row in targets.iterrows()]))

    print(f"有效計算標的：{valid_count} 檔")
    return golden_list, death_list

def write_sheets(golden, death):
    creds_raw = base64.b64decode(GCP_SA_KEY_BASE64).decode('utf-8')
    creds_dict = json.loads(creds_raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    print(f"開啟試算表: {SPREADSHEET_ID}")
    sh = gc.open_by_key(SPREADSHEET_ID)

    def update_tab(tab_name, rows):
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows="1500", cols="5")
        
        ws.clear()
        header = [["股號", "名稱", "當前月K值", "當前月D值", "資料月份"]]
        if rows:
            print(f"成功寫入【{tab_name}】分頁共 {len(rows)} 筆標的！")
            ws.update(values=header + rows, range_name=f"A1:E{len(rows)+1}")
        else:
            print(f"【{tab_name}】分頁無交叉標的，寫入標題與提示行。")
            ws.update(values=header + [["-", "本月無最新交叉標的", "-", "-", "-"]], range_name="A1:E2")

    update_tab("黃金", golden)
    update_tab("死亡", death)
    print("試算表寫入作業全部完成！")

if __name__ == '__main__':
    g, d = scan_all()
    print(f"篩選完成：黃金交叉 {len(g)} 檔，死亡交叉 {len(d)} 檔。")
    if g:
        print(f"黃金交叉範例（前 3 檔）: {g[:3]}")
    if d:
        print(f"死亡交叉範例（前 3 檔）: {d[:3]}")
    write_sheets(g, d)
