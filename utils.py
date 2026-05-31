# -*- coding: utf-8 -*-
import re
import calendar
from datetime import date

def safe_eval(expr):
    """安全執行數學運算字串"""
    try:
        allowed = set('0123456789+-*/() ')
        if not set(expr).issubset(allowed): return None
        return int(eval(expr))
    except: return None

def calculate_effective_days(year, month, days_str):
    """計算當月有效營業日數"""
    if not days_str: return calendar.monthrange(year, month)[1]
    week_map = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6}
    target = []
    for char in days_str:
        if char in week_map:
            target.append(week_map[char])
            
    if not target: return calendar.monthrange(year, month)[1]
    
    count = 0
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        if date(year, month, d).weekday() in target: count += 1
    return count

def clean_input_text(text):
    """預處理輸入文字：去補字、去頭尾空白"""
    return text.lstrip('補').strip()

def smart_split_text(text):
    """智慧斷詞：切開中英文，但不切開中文數字"""
    text = re.sub(r'([a-zA-Z])([\u4e00-\u9fa5])', r'\1 \2', text)
    text = re.sub(r'([\u4e00-\u9fa5])([a-zA-Z])', r'\1 \2', text)
    return text