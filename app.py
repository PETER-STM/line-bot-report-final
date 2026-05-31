# -*- coding: utf-8 -*-
import os
import re
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from database import init_db
from services import handle_record_expense_smart
from commands import handle_admin, handle_finance, handle_help_visual, handle_amend_last

# 初始化資料庫
init_db()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 💡 關鍵修復：專門給 Railway 健康檢查用的首頁，防止 6 秒閃退！
@app.route("/", methods=['GET'])
def home():
    return "Ahab2.0 is running perfectly!", 200
def process_batch_lines(text):
    """處理多行批量輸入 (V20.2 雙層記憶注入版)"""
    
    # 🚨 【總電源防護鎖】
    # 只要整段訊息包含這些「排班專用」的符號，直接判定為非記帳訊息，整段忽略！
    if '▲' in text or '【】' in text or '隊：' in text:
        return "" # 系統會直接閉眼，完全不往下跑解析

    lines = text.split('\n')
    results = [] 
    # 🧠 雙重記憶大腦
    current_member = ""    # 第一層：記住是誰的班表
    current_location = ""  # 第二層：記住在哪個地點
    
    for line in lines:
        line = line.strip()
        if not line: continue

        # 👇 [新增] 絕對拒絕讀取牆：過濾掉隊伍標題等純排版雜訊
        # (⚠️ 注意：這裡絕對不能放 '【】'，因為下面抓取人員名稱時會用到！)
        if '▲' in line or '隊：' in line:
            continue
        
        # 1. 記憶「人員」 (例如: 🔺慈【】) -> 抓到人，就先清空地點
        if line.startswith('🔺'):
            m = re.search(r'🔺(.*?)(?:【|$)', line)
            if m: 
                current_member = m.group(1).strip()
                current_location = ""
            continue            
        # 2. 記憶「地點標題」 (拔除括號人數，例如: 宏匯（12） -> 宏匯)
        if not re.search(r'\d+[/-]\d+', line) and not line.startswith('▲') and not line.startswith('【'):
            loc_clean = re.sub(r'[(（]\d+[)）]', '', line).strip()
            if loc_clean: current_location = loc_clean
            continue

        # 3. 過濾雜訊與時間刺客
        if re.match(r'^\d{8}.*$', line) or (re.match(r'^\d+[/-]\d+.*$', line) and len(line) < 10 and ' ' not in line): continue
        #if re.search(r'\d{1,2}[:：]\d{2}', line): continue
        if ':' in line or '：' in line: 
            continue

        # 4. 原有的雜訊過濾
        if re.match(r'^\d{8}.*$', line) or (re.match(r'^\d+[/-]\d+.*$', line) and len(line) < 10 and ' ' not in line): 
            continue

        # 4. 備註與修正
        is_item_note = re.search(r'[桌布燈架]\s*\d+', line)
        if not re.search(r'\d+[/-]\d+', line) and (line.startswith('改') or '備註' in line or is_item_note):
            amend_res = handle_amend_last(f"備註 {line}")
            if "備註已追加" in amend_res: results.append(f"  └ 📝 備註已掛載：{line}")
            else: results.append(f"  └ ❌ 備註失敗：{line}")
            continue

        # 5. 記帳邏輯 (雙重記憶注入)
        if re.search(r'\d+[/-]\d+', line):
            # 💡 魔法發生處：把「地點」跟「人」無縫塞進這一行！
            process_line = f"{current_location} {current_member} {line}".strip()
            
            res = handle_record_expense_smart(process_line)
            if res:
                if "❌" in res:
                    simple_res = res.split('\n')[0]
                    if "找不到地點" in simple_res and len(line) > 15: continue 
                    results.append(f"{simple_res} ({line})")
                else:
                    results.append("-" * 15 + "\n" + res)
            time.sleep(0.1)
            
    if not results: return "" 
    return "\n".join(results)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    reply = ""
    
    # 0. 批量處理
    if '\n' in msg and re.search(r'\d+[/-]\d+', msg):
        batch_output = process_batch_lines(msg)
        if batch_output: reply = batch_output + "\n" + "-"*15 + "\n✅ 處理完畢"

    # 1. 指令分流
    elif msg in ['幫助', 'help', '指令']: reply = handle_help_visual()
    elif msg.startswith('改價') or msg.startswith('改金額') or msg.startswith('備註') or msg.startswith('筆記'): reply = handle_amend_last(msg)
    elif msg.startswith('新增') or msg.startswith('設定') or msg.startswith('刪除') or msg == '清除異常' or msg.startswith('檢查缺漏') or msg.startswith('一鍵補幽靈') or msg == '人員名單' or msg.startswith('拆分') or msg.startswith('合併') or msg.startswith('清空月份') or msg.startswith('清除幽靈'): reply = handle_admin(msg)
    
   # 💡 匯出與報表判斷 (V20.5 終極拔除純數字觸發版)
    elif (msg.startswith('匯出') or msg.startswith('結算') or msg.startswith('百貨') or 
          msg.startswith('檔期結算') or msg in ['價目表', '清單', '統計', '報表', '明細', '完整'] or 
          re.match(r'^\d+月(報表|明細|完整)', msg)): # 🔪 物理拔除結尾的 $ 符號，解放後綴字串
        reply = handle_finance(msg)
        
    elif re.search(r'\d+[/-]\d+', msg): reply = handle_record_expense_smart(msg)
    
    # 2. 分段發送
    if reply:
        max_length = 4000
        reply_list = []
        if len(reply) > max_length:
            for i in range(0, len(reply), max_length):
                chunk = reply[i:i+max_length]
                reply_list.append(TextSendMessage(text=chunk))
            if len(reply_list) > 5:
                reply_list = reply_list[:5]
                reply_list[-1].text += "\n...(⚠️ 內容過長)"
        else:
            reply_list.append(TextSendMessage(text=reply))
        line_bot_api.reply_message(event.reply_token, reply_list)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)