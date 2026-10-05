import os
import glob
import re
import io
import time
import csv
import base64
import json
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from collections import Counter
from PIL import Image
import gradio as gr
from google.cloud import storage
from google import genai

# ==========================================
# 0. إعدادات السحابة والمنفذ والذكاء الاصطناعي
# ==========================================
PORT = int(os.environ.get("PORT", 8080))
BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

LOG_FILE_PATH = "/tmp/maintenance_search_log.csv"

# رابط استعلام جدول Google Sheets للقراءة
SHEET_ID = "1_scf-CUSouwQvJan4d12UuC7LX8eHC7E4YAjC41q2r4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

# 🟢 رابط Google Apps Script Webhook الفعلي المعتمد لحسابك
SHEET_WEBHOOK_URL = os.environ.get(
    "SHEET_WEBHOOK_URL", 
    "https://script.google.com/macros/s/AKfycbwKYnWN2z8TQrV-Qb8CuO1YW_SbpQrVjHJSPriSzBwXDCgyaQNc59jqGTykqNY9e5LAAg/exec"
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[!] Warning initializing Gemini API: {e}")

machine_catalogs_db = {}
available_machines_list = []

def sync_data_from_gcs():
    global machine_catalogs_db, available_machines_list
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    print(f"[*] Starting download from GCS bucket: {BUCKET_NAME}...")
    try:
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blobs = bucket.list_blobs(prefix="Maintenance_Manuals/")
        
        count = 0
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            relative_path = os.path.relpath(blob.name, "Maintenance_Manuals")
            dest_path = os.path.join(BASE_DIR, relative_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if not os.path.exists(dest_path):
                blob.download_to_filename(dest_path)
                count += 1
        print(f"[✓] GCS Sync completed. Downloaded {count} files.")
    except Exception as e:
        print(f"[!] Warning during GCS sync: {e}")

    build_machine_catalog_groups()

def build_machine_catalog_groups():
    global machine_catalogs_db, available_machines_list
    machine_catalogs_db = {}
    
    pdf_list = glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True)
    
    machine_patterns = [
        ("ماكينة التفريغ مايسترو (Maestro Eviscerator 0600)", ["maestro", "eviscerat", "0600"]),
        ("ماكينة قص دجاج نهائي (Vent Cutter 0100)", ["vent", "cutter", "0100"]),
        ("ماكينة الفتح والمقص (Opening Machine 0450)", ["opening", "scissors", "0450"]),
        ("حوض السكالدر (Scalder 0560 / 0990)", ["scalder", "scalding", "0560", "0990"]),
        ("ماكينة المعاطه (Plucker JM64 / 2470)", ["plucker", "picking", "jm64", "2470", "0770"]),
        ("ماكينة تنظيف القوانص (Gizzard Harvester CD-6000)", ["gizzard", "peeler", "cd-6000", "1860"]),
        ("ماكينة سحب الرؤوس (Head Puller 2920)", ["head puller", "2920"]),
        ("ماكينة قص الأرجل (Hock Cutter 3000)", ["hock", "leg cutter", "3000"]),
        ("الجنزير والعلاقات (Overhead Conveyor 0230)", ["shackle", "overhead", "0230"]),
        ("خط نقل الكبدة (Pan Conveyor Single)", ["pan conveyor", "pan single", "pan"]),
        ("ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)", ["automac", "wrapping", "297", "298", "a55", "fabbri", "stretch"]),
        ("مضخات الفاكيوم وتفريغ الرئة (Vacuum Pumps)", ["vacuum", "lung", "robuschi", "2170", "0190"]),
        ("كمبرسورات ومنظومة التبريد المركزية", ["compressor", "airpol", "atlas", "refrigeration", "2410"])
    ]

    for p in pdf_list:
        fname = os.path.basename(p)
        fname_lower = fname.lower()
        
        is_parts = any(k in fname_lower for k in ["part", "parts", "spare", "component", "قطعة", "قطع"])
        
        assigned_group = None
        for m_name, keys in machine_patterns:
            if any(k in fname_lower for k in keys):
                assigned_group = m_name
                break
                
        if not assigned_group:
            clean_name = os.path.splitext(fname)[0].replace("_", " ").replace("-", " ")
            clean_name = re.sub(r'\b(part|parts|maintenance|user|manual|catalog)\b', '', clean_name, flags=re.I).strip()
            assigned_group = f"ماكينة {clean_name}" if clean_name else "ماكينات عامة"

        if assigned_group not in machine_catalogs_db:
            machine_catalogs_db[assigned_group] = {
                "maintenance_manual": None,
                "parts_catalog": None,
                "other_files": []
            }

        if is_parts:
            if not machine_catalogs_db[assigned_group]["parts_catalog"]:
                machine_catalogs_db[assigned_group]["parts_catalog"] = p
            else:
                machine_catalogs_db[assigned_group]["other_files"].append(p)
        else:
            if not machine_catalogs_db[assigned_group]["maintenance_manual"]:
                machine_catalogs_db[assigned_group]["maintenance_manual"] = p
            else:
                machine_catalogs_db[assigned_group]["other_files"].append(p)

    available_machines_list = sorted(list(machine_catalogs_db.keys()))
    print(f"[✓] Grouped {len(pdf_list)} manuals into {len(available_machines_list)} machine families.")

sync_data_from_gcs()

# ==========================================
# 1. إعدادات تنبيهات الواتساب (Green-API)
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN_INSTANCE = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"

ALERT_GROUP_ID = "970599431267@c.us"
PURCHASING_MANAGER_PHONE = "972595470033@c.us"

def send_whatsapp_alert(message, target_phone=None):
    if not API_TOKEN_INSTANCE or "YOUR_GREEN_API" in API_TOKEN_INSTANCE:
        return

    dest_chat = target_phone if target_phone else ALERT_GROUP_ID
    if not dest_chat or "YOUR_PHONE" in dest_chat:
        return

    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    payload = {"chatId": dest_chat, "message": message}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"[!] WhatsApp notification error: {err}")

# ==========================================
# 2. محرك تسجيل وتوثيق العمليات في الإكسل (Audit Logger)
# ==========================================
def log_search_query(raw_query, hit_type, machine_name, part_name, part_code, stock_info):
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M:%S %p')
    
    file_exists = os.path.exists(LOG_FILE_PATH)
    try:
        with open(LOG_FILE_PATH, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "التاريخ والوقت (Timestamp)",
                    "الاستعلام الأصلي (Original_Query)",
                    "نوع العملية (Operation_Type)",
                    "اسم الماكينة (Machine_Name)",
                    "اسم القطعة (Part_Name)",
                    "كود القطعة / رمز الإنذار (Code_or_Alarm)",
                    "الرصيد في المستودع (Stock_Qty)",
                    "موقع الرف / التخزين (Location)"
                ])
            
            qty = stock_info.get("qty", "غير مسجل") if stock_info else "غير مسجل"
            loc = stock_info.get("location", "مستودع المسلخ") if stock_info else "مستودع المسلخ"
            
            type_labels = {
                "alarm": "إنذار تشغيلي (Alarm/Fault)",
                "trouble_table": "استكشاف أعطال (Troubleshooting)",
                "pm_checklist": "صيانة دورية وقائية (Preventive Checklist)",
                "part": "قطعة غيار (Spare Part)",
                "stock_out": "صرف قطعة غيار (Stock Issued)",
                "stock_in": "توريد/إضافة قطعة (Stock Added)",
                "keyword": "بحث عام (General Search)"
            }
            op_label = type_labels.get(hit_type, "بحث عام")

            writer.writerow([
                timestamp,
                raw_query,
                op_label,
                machine_name if machine_name else "ماكينات عامة / غير محدد",
                part_name if part_name else "—",
                part_code if part_code else "—",
                qty,
                loc
            ])
            print(f"[✓] Logged: Machine='{machine_name}', Part='{part_name}', Code='{part_code}'")
    except Exception as e:
        print(f"[!] Error writing search log: {e}")

# ==========================================
# 3. محرك مخزون قطع الغيار من Google Sheets
# ==========================================
inventory_cache = {
    "data": {},
    "last_sync": 0
}

def clean_part_key(key_text):
    if not key_text:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(key_text)).lower()

def fetch_inventory_data(force_refresh=False):
    current_time = time.time()
    if not force_refresh and inventory_cache["data"] and (current_time - inventory_cache["last_sync"] < 15):
        return inventory_cache["data"]

    try:
        headers_req = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(GOOGLE_SHEET_CSV_URL, headers=headers_req, timeout=10, allow_redirects=True)
        if res.status_code == 200:
            lines = res.text.splitlines()
            reader = csv.reader(lines)
            rows = [r for r in reader if any(field.strip() for field in r)]
            
            if len(rows) >= 2:
                data_map = {}
                for row in rows[1:]:
                    if len(row) < 3:
                        continue
                    raw_code = str(row[0]).strip().strip('"')
                    if not raw_code or raw_code.lower() in ['nan', 'none', '']:
                        continue
                    
                    clean_k = clean_part_key(raw_code)
                    part_name = str(row[1]).strip().strip('"') if len(row) > 1 else ""
                    qty_val = str(row[2]).strip().strip('"') if len(row) > 2 else "0"
                    loc_val = str(row[3]).strip().strip('"') if len(row) > 3 else "مستودع قطع الغيار"
                    min_stock = str(row[4]).strip().strip('"') if len(row) > 4 else "2"

                    data_map[clean_k] = {
                        "raw_code": raw_code,
                        "name": part_name,
                        "qty": qty_val,
                        "location": loc_val,
                        "min_stock": min_stock
                    }

                inventory_
