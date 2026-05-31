import logging
import json
import traceback
from datetime import datetime
import os

# 確保 logs 資料夾存在
if not os.path.exists('logs'):
    os.makedirs('logs')

class JSONFormatter(logging.Formatter):
    """將日誌轉換為結構化的 JSON 格式，方便未來機器分析"""
    def format(self, record):
        log_obj = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        
        # 如果有 Exception (錯誤追蹤)，一併記錄下來
        if record.exc_info:
            log_obj["exception_trace"] = traceback.format_exception(*record.exc_info)
            
        return json.dumps(log_obj, ensure_ascii=False)

def setup_logger():
    """初始化專業日誌系統"""
    logger = logging.getLogger("Ahab2_Logger")
    
    # 避免重複設定
    if logger.handlers:
        return logger
        
    logger.setLevel(logging.INFO)

    # 1. 寫入檔案的 Handler (黑盒子本體)
    file_handler = logging.FileHandler('logs/ahab_system.log', encoding='utf-8')
    file_handler.setFormatter(JSONFormatter())
    
    # 2. 顯示在終端機的 Handler (讓你看 Railway 日誌還是看得到)
    console_handler = logging.StreamHandler()
    console_format = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(console_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# 建立一個全域的 logger 實例供其他檔案使用
system_logger = setup_logger()