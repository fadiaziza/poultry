import os
import glob
import re
import io
import time
import csv
import base64
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from collections import Counter
from PIL import Image, ImageStat
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

# رابط جدول Google Sheets المباشر والمستقر للمخزون
SHEET_ID = "1_scf-CUSouwQvJan4d12UuC7LX8eHC7E4YAjC41q2r4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

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

def send_whatsapp_alert(message):
    if not API_TOKEN_INSTANCE or "YOUR_GREEN_API" in API_TOKEN_INSTANCE:
        return
    if not ALERT_GROUP_ID or "YOUR_PHONE" in ALERT_GROUP_ID:
        return

    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    payload = {"chatId": ALERT_GROUP_ID, "message": message}
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

def fetch_inventory_data():
    current_time = time.time()
    if inventory_cache["data"] and (current_time - inventory_cache["last_sync"] < 30):
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
                    min_stock = str(row[4]).strip().strip('"') if len(row) > 4 else "0"

                    data_map[clean_k] = {
                        "raw_code": raw_code,
                        "name": part_name,
                        "qty": qty_val,
                        "location": loc_val,
                        "min_stock": min_stock
                    }

                inventory_cache["data"] = data_map
                inventory_cache["last_sync"] = current_time
                print(f"[✓] Google Sheet Connected: {len(data_map)} items loaded.")
    except Exception as e:
        print(f"[!] Error fetching Google Sheet: {e}")

    return inventory_cache["data"]

def get_part_inventory_info(part_query):
    inv_data = fetch_inventory_data()
    if not inv_data or not part_query:
        return None

    clean_target = clean_part_key(part_query)
    if clean_target in inv_data:
        return inv_data[clean_target]

    for k, info in inv_data.items():
        if len(clean_target) >= 6 and (clean_target in k or k in clean_target):
            return info

    return None

# ==========================================
# 4. محرك تحليلات ومؤشرات الأداء (KPI Analytics Engine)
# ==========================================
def generate_kpi_dashboard_data():
    """حساب مؤشرات الأداء الحية للقسم من واقع سجل الأعطال والمخزون الميداني مع قراءة فورية"""
    inv_data = fetch_inventory_data()
    total_parts = len(inv_data)
    
    safe_stock_count = 0
    low_stock_count = 0
    out_of_stock_count = 0

    for item in inv_data.values():
        try:
            qty = float(re.sub(r'[^0-9.]', '', str(item["qty"])))
            if qty > 2:
                safe_stock_count += 1
            elif qty > 0:
                low_stock_count += 1
            else:
                out_of_stock_count += 1
        except Exception:
            safe_stock_count += 1

    total_ops = 0
    machine_counter = Counter()
    parts_counter = Counter()

    if os.path.exists(LOG_FILE_PATH):
        try:
            with open(LOG_FILE_PATH, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                rows = list(reader)
                if len(rows) > 1:
                    for r in rows[1:]:
                        if len(r) >= 5:
                            total_ops += 1
                            m_name = r[3].strip()
                            p_name = r[4].strip()
                            if m_name and m_name not in ["ماكينات عامة / غير محدد", "غير محدد"]:
                                machine_counter[m_name] += 1
                            if p_name and p_name not in ["—", "غير مسجل", ""]:
                                parts_counter[p_name] += 1
        except Exception as e:
            print(f"[!] Error reading logs for KPI: {e}")

    top_machines = machine_counter.most_common(5)
    top_parts = parts_counter.most_common(5)

    top_m_md = "| # | اسم الماكينة | عدد مرات الفحص / الأعطال |\n| :-: | :--- | :-: |\n"
    if top_machines:
        for idx, (m, count) in enumerate(top_machines, 1):
            top_m_md += f"| {idx} | **{m}** | `{count}` مرات |\n"
    else:
        top_m_md += "| 1 | *لا توجد عمليات مسجلة كافية بعد* | `0` |\n"

    top_p_md = "| # | اسم القطعة المستعلام عنها | عدد مرات الطلب |\n| :-: | :--- | :-: |\n"
    if top_parts:
        for idx, (p, count) in enumerate(top_parts, 1):
            top_p_md += f"| {idx} | **{p}** | `{count}` مرات |\n"
    else:
        top_p_md += "| 1 | *لا توجد استعلامات قطع مسجلة بعد* | `0` |\n"

    total_manuals_indexed = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

    summary_cards_html = f"""
<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; direction: rtl;">
    <div style="background: #ffffff; padding: 16px; border-radius: 12px; border: 2px solid #e0e0e0; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center;">
        <span style="font-size: 13px; color: #555; font-weight: bold; display: block;">📖 الكتالوجات المعتمدة</span>
        <span style="font-size: 26px; font-weight: 900; color: #1b5e20;">{total_manuals_indexed}</span>
        <span style="font-size: 11px; color: #888; display: block;">كتالوج تشغيل وقطع</span>
    </div>
    <div style="background: #ffffff; padding: 16px; border-radius: 12px; border: 2px solid #e0e0e0; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center;">
        <span style="font-size: 13px; color: #555; font-weight: bold; display: block;">📦 قطع الغيار الحية</span>
        <span style="font-size: 26px; font-weight: 900; color: #0277bd;">{total_parts}</span>
        <span style="font-size: 11px; color: #888; display: block;">مربوطة مع Google Sheet</span>
    </div>
    <div style="background: #ffffff; padding: 16px; border-radius: 12px; border: 2px solid #e0e0e0; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center;">
        <span style="font-size: 13px; color: #555; font-weight: bold; display: block;">🔍 إجمالي بلاغات الفحص</span>
        <span style="font-size: 26px; font-weight: 900; color: #6a1b9a;">{total_ops}</span>
        <span style="font-size: 11px; color: #888; display: block;">موثقة في سجل التدقيق</span>
    </div>
    <div style="background: #ffffff; padding: 16px; border-radius: 12px; border: 2px solid #e0e0e0; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center;">
        <span style="font-size: 13px; color: #555; font-weight: bold; display: block;">🟢 رصيد المخزون الآمن</span>
        <span style="font-size: 26px; font-weight: 900; color: #2e7d32;">{safe_stock_count}</span>
        <span style="font-size: 11px; color: #d32f2f; display: block;">🔴 نافد / حرج: {out_of_stock_count + low_stock_count}</span>
    </div>
</div>
"""
    return summary_cards_html, top_m_md, top_p_md

# ==========================================
# 5. فهرسة صفحات الكتالوجات وبصمات صور المستودع
# ==========================================
manual_pages = []

def build_manual_index():
    global manual_pages
    manual_pages = []
    pdf_files = glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True)
    print(f"[*] Indexing {len(pdf_files)} PDF manuals...")
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page_text = doc[page_num].get_text("text").strip()
                if len(page_text) > 15:
                    manual_pages.append({
                        "filename": filename,
                        "filepath": pdf_path,
                        "page": page_num + 1,
                        "text": page_text
                    })
        except Exception:
            pass
    print(f"[✓] Successfully indexed {len(manual_pages)} pages.")

build_manual_index()

part_images_map = {}
image_signatures = {}

def get_img_sig(img):
    img_gray = img.convert('L').resize((16, 16), Image.Resampling.BILINEAR)
    pixels = list(img_gray.getdata())
    avg = sum(pixels) / len(pixels)
    return [1 if p > avg else 0 for p in pixels]

def build_image_index():
    global part_images_map, image_signatures
    part_images_map = {}
    image_signatures = {}
    if not os.path.exists(IMAGE_DIR):
        return
    valid_exts = ('.jpg', '.jpeg', '.png', '.JPG', '.PNG')
    for f in os.listdir(IMAGE_DIR):
        if f.endswith(valid_exts):
            part_no = os.path.splitext(f)[0]
            clean_k = re.sub(r'[^a-zA-Z0-9]', '', part_no).lower()
            img_path = os.path.join(IMAGE_DIR, f)
            part_images_map[clean_k] = (part_no, img_path)
            try:
                with Image.open(img_path) as im:
                    image_signatures[part_no] = (get_img_sig(im), img_path)
            except Exception:
                pass
    print(f"[✓] Indexed {len(image_signatures)} part images for visual comparison.")

build_image_index()

logo_base64 = ""
for p in ["logo.png", "/app/logo.png"]:
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                logo_base64 = base64.b64encode(f.read()).decode("utf-8")
            break
        except Exception:
            pass

# ==========================================
# 6. دوال استخراج ومعالجة الصور ودمج الصفحات
# ==========================================
def render_pdf_page_to_image(filepath, page_num):
    try:
        doc = fitz.open(filepath)
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=150)
        out_img_path = f"/tmp/page_{os.path.basename(filepath)}_{page_num}.png"
        pix.save(out_img_path)
        return out_img_path
    except Exception as e:
        print(f"[!] Error rendering PDF page to image: {e}")
        return None

def render_troubleshooting_pages_stitched(filepath, page_list):
    if not page_list:
        return None
    if len(page_list) == 1:
        return render_pdf_page_to_image(filepath, page_list[0])
    
    try:
        doc = fitz.open(filepath)
        pil_images = []
        for p_num in page_list[:3]:
            page = doc[p_num - 1]
            pix = page.get_pixmap(dpi=140)
            img_data = pix.tobytes("png")
            pil_images.append(Image.open(io.BytesIO(img_data)))
        
        max_width = max(im.width for im in pil_images)
        total_height = sum(im.height for im in pil_images)
        
        combined_img = Image.new("RGB", (max_width, total_height), (255, 255, 255))
        y_offset = 0
        for im in pil_images:
            combined_img.paste(im, (0, y_offset))
            y_offset += im.height
            
        out_combined_path = f"/tmp/trouble_stitched_{os.path.basename(filepath)}.png"
        combined_img.save(out_combined_path)
        return out_combined_path
    except Exception as e:
        print(f"[!] Error stitching troubleshooting pages: {e}")
        return render_pdf_page_to_image(filepath, page_list[0])

def render_machine_cover_image(filepath):
    try:
        doc = fitz.open(filepath)
        page = doc[0]
        pix = page.get_pixmap(dpi=150)
        out_img_path = f"/tmp/cover_{os.path.basename(filepath)}_{os.path.getmtime(filepath)}.png"
        pix.save(out_img_path)
        return out_img_path
    except Exception as e:
        print(f"[!] Error rendering machine cover image: {e}")
        return None

def match_uploaded_image(uploaded_img):
    if uploaded_img is None or not image_signatures:
        return None, None
    try:
        if not isinstance(uploaded_img, Image.Image):
            uploaded_img = Image.fromarray(uploaded_img)
            
        up_sig = get_img_sig(uploaded_img)
        best_part = None
        min_diff = 256
        
        for part_no, (sig, path) in image_signatures.items():
            diff = sum(c1 != c2 for c1, c2 in zip(up_sig, sig))
            if diff < min_diff:
                min_diff = diff
                best_part = (part_no, path)
                
        if min_diff <= 65:
            return best_part[0], best_part[1]
    except Exception as e:
        print(f"[!] Vision matching error: {e}")
    return None, None

def find_image_for_part(query_text):
    if not query_text or not part_images_map:
        return None
    clean_target = re.sub(r'[^a-zA-Z0-9]', '', query_text).lower()
    if clean_target in part_images_map:
        return part_images_map[clean_target][1]

    tokens = re.findall(r'[A-Za-z0-9]{4,}', query_text)
    for tok in tokens:
        c_tok = tok.lower()
        if c_tok in part_images_map:
            return part_images_map[c_tok][1]

    for k, v in part_images_map.items():
        if len(k) >= 6 and (k in clean_target or clean_target in k):
            return v[1]
    return None

def deduce_machine_from_filename(filename):
    f_lower = filename.lower()
    if any(k in f_lower for k in ["automac", "297", "298", "wrapping", "fabbri", "stretch"]):
        return "ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)"
    elif any(k in f_lower for k in ["maestro", "eviscerat", "0600"]):
        return "ماكينة التفريغ مايسترو (Maestro Eviscerator 0600)"
    elif any(k in f_lower for k in ["opening", "scissors", "0450"]):
        return "ماكينة الفتح والمقص (Opening Machine 0450)"
    elif any(k in f_lower for k in ["vent", "cutter", "0100"]):
        return "ماكينة قص دجاج نهائي (Vent Cutter 0100)"
    elif any(k in f_lower for k in ["scalder", "scalding", "0560", "0990"]):
        return "حوض السكالدر (Scalder 0560 / 0990)"
    elif any(k in f_lower for k in ["plucker", "picking", "jm64", "2470", "0770"]):
        return "ماكينة المعاطه (Plucker JM64 / 2470)"
    elif any(k in f_lower for k in ["gizzard", "peeler", "cd-6000", "1860"]):
        return "ماكينة تنظيف القوانص (Gizzard Harvester CD-6000)"
    elif any(k in f_lower for k in ["shackle", "overhead", "0230"]):
        return "الجنزير والعلاقات (Overhead Conveyor 0230)"
    elif any(k in f_lower for k in ["pan conveyor", "pan"]):
        return "خط نقل الكبدة (Pan Conveyor Single)"
    elif any(k in f_lower for k in ["hock", "leg cutter", "3000"]):
        return "ماكينة قص الأرجل (Hock Cutter 3000)"
    elif any(k in f_lower for k in ["head puller", "2920"]):
        return "ماكينة سحب الرؤوس (Head Puller 2920)"
    elif any(k in f_lower for k in ["vacuum", "lung", "robuschi", "2170", "0190"]):
        return "مضخات الفاكيوم وتفريغ الرئة (Vacuum Pumps)"
    elif any(k in f_lower for k in ["compressor", "airpol", "atlas", "refrigeration"]):
        return "كمبرسورات ومنظومة التبريد المركزية"
    else:
        clean_name = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
        return f"ماكينة {clean_name}"

def find_linked_manuals_for_machine(machine_name):
    if machine_name in machine_catalogs_db:
        return machine_catalogs_db[machine_name]
    m_clean = machine_name.lower()
    for group_k, data in machine_catalogs_db.items():
        if any(word in group_k.lower() for word in m_clean.split() if len(word) > 3):
            return data
    return {"maintenance_manual": None, "parts_catalog": None, "other_files": []}

# ==========================================
# 7. محرك الصيانة الدورية وقوائم الفحص (PM Checklist Engine)
# ==========================================
def extract_pm_checklist_for_machine(machine_name):
    if not machine_name or machine_name not in machine_catalogs_db:
        return None, "⚠️ يرجى اختيار ماكينة صحيحة."
    
    m_data = machine_catalogs_db[machine_name]
    maint_pdf = m_data.get("maintenance_manual") or (m_data["other_files"][0] if m_data.get("other_files") else None)
    
    if not maint_pdf or not os.path.exists(maint_pdf):
        return None, f"⚠️ لا يتوفر كتالوج صيانة مسجل لماكينة **{machine_name}**."

    matched_pm_pages = []
    target_filename = os.path.basename(maint_pdf)
    
    for p in manual_pages:
        if p["filename"] == target_filename and p["page"] > 5:
            t_low = p["text"].lower()
            score = 0
            if "preventive maintenance" in t_low or "periodic maintenance" in t_low:
                score += 5
            if "maintenance schedule" in t_low or "inspection schedule" in t_low:
                score += 5
            if "lubrication" in t_low and ("daily" in t_low or "weekly" in t_low or "hours" in t_low):
                score += 4
            if "checklist" in t_low or "interval" in t_low:
                score += 3
            if "daily" in t_low and "weekly" in t_low and "monthly" in t_low:
                score += 4

            if score >= 4:
                matched_pm_pages.append((score, p))

    matched_pm_pages.sort(key=lambda x: x[0], reverse=True)
    
    if matched_pm_pages:
        best_page = matched_pm_pages[0][1]
        checklist_img = render_pdf_page_to_image(best_page["filepath"], best_page["page"])
        
        prompt = f"""
ROLE:
You are the Lead Reliability & Maintenance Engineer at Palestine Poultry Company ("Aziza Slaughterhouse").

TASK:
Extract the PREVENTIVE MAINTENANCE & PERIODIC INSPECTION CHECKLIST for equipment: "{machine_name}".

MANUAL PAGE TEXT (Page {best_page['page']}):
\"\"\"{best_page['text'][:10000]}\"\"\"

MANDATORY INSTRUCTIONS:
1. Extract ALL maintenance intervals into a clean, complete Markdown Table:
   | Interval (الفترة الزمنية) | Inspection Point (نقطة الفحص) | Technical Procedure (الإجراء الفني المطلوب) | Standard / LOTO (المعيار والسلامة) |
   | :--- | :--- | :--- | :--- |
2. Group clearly by frequency: Daily (يومي), Weekly (أسبوعي), Monthly (شهري), and Periodic (ساعات التشغيل مثل 500h / 1000h).
3. Be strictly technical, practical, and direct in Professional Arabic & English terms.
"""
        try:
            res = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            checklist_text = res.text.strip()
        except Exception as e:
            checklist_text = f"⚠️ حدث خطأ أثناء تحليل جدول الصيانة عبر AI: {e}"

        final_md = f"""
## 📋 جدول الصيانة الدورية والفحص الوقائي (PM Checklist)
### 🏭 الماكينة: **{machine_name}**
* 📖 **المرجع من الكتالوج:** `{target_filename}` (صفحة رقم {best_page['page']})

{checklist_text}
"""
        return checklist_img, final_md
    else:
        first_img = render_machine_cover_image(maint_pdf)
        prompt_fallback = f"""
Generate an industrial Preventive Maintenance (PM) Checklist specifically tailored for the poultry processing equipment: "{machine_name}" at Palestine Poultry Company (Aziza Slaughterhouse).
Include Daily, Weekly, and Monthly inspection tasks covering: Mechanical drives, Pneumatics (6 bar), Sensors/Alignment, Lubrication/Food-grade grease, and Lockout/Tagout (LOTO) safety measures.
Format as an executive Markdown Table.
"""
        try:
            res = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt_fallback
            )
            checklist_text = res.text.strip()
        except Exception:
            checklist_text = "⚠️ تعذر توليد جدول الصيانة حالياً."

        final_md = f"""
## 📋 جدول الصيانة الدورية والفحص الوقائي المعتمد (PM Inspection Checklist)
### 🏭 الماكينة: **{machine_name}**
* 📖 **المرجع:** كتالوج الصيانة المعتمد للماكينة `{target_filename}`

{checklist_text}
"""
        return first_img, final_md

# ==========================================
# 8. محرك Gemini لاستخراج جميع الأعطال بالكامل
# ==========================================
def ask_gemini_engineer(user_query, context_text):
    if not ai_client or not context_text:
        return ""

    prompt = f"""
ROLE & RESPONSIBILITY:
You are the Lead Automation & Industrial Maintenance Systems Engineer at Palestine Poultry Company ("Aziza Slaughterhouse").
A maintenance supervisor/technician requested the complete troubleshooting records from the official equipment manual.

USER QUERY:
"{user_query}"

FULL TROUBLESHOOTING SECTION TEXT:
\"\"\"{context_text[:12000]}\"\"\"

MANDATORY INSTRUCTIONS:
1. EXHAUSTIVE EXTRACTION: Do NOT summarize, omit, or truncate ANY failure. Extract EVERY SINGLE ROW and condition listed across all the manual pages provided.
2. OUTPUT FORMAT:
   First, output a complete, cleanly structured Markdown Table containing all problems:
   | # | Failure / Symptom | Possible Cause | Corrective Action / Solution |
   | :-: | :--- | :--- | :--- |
   Populate every row accurately with technical precision.
3. ACTION CHECKLIST:
   After the table, generate a prioritized "Quick Field Checklist" in technical English highlighting critical inspection points (Sensors, Pneumatics, Mechanical drives, Safety circuits).
4. TONE & LANGUAGE:
   Strictly Technical English. Direct, professional, and completely free of conversational filler.
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as err:
        print(f"[!] Gemini API Error: {err}")
        return ""

# ==========================================
# 9. محرك البحث الذكي (متعدد الصفحات للأعطال)
# ==========================================
def search_engine(query, top_k=5):
    if not manual_pages:
        return [], None, None
    clean_q = query.strip()
    clean_q_lower = clean_q.lower()

    # 1. إنذارات وتحذيرات أوتوماك (E / W)
    alarm_match = re.search(r'\b([EWew])\s*0*(\d+)\b', clean_q)
    if alarm_match:
        prefix = alarm_match.group(1).upper()
        num = int(alarm_match.group(2))
        std_code = f"{prefix}{num:03d}"
        
        pattern = rf'\b{prefix}\s*0*{num}\b'
        matched = [p for p in manual_pages if re.search(pattern, p["text"], re.IGNORECASE)]
        if matched:
            matched_sorted = sorted(
                matched, 
                key=lambda x: any(k in x["filename"].lower() for k in ["automac", "297", "298", "wrapping", "fabbri"]), 
                reverse=True
            )
            return matched_sorted[:top_k], std_code, "alarm"
        return [], std_code, "alarm"

    # 2. مطابقة رقم القطعة
    code_match = re.search(r'([A-Za-z0-9]{2,4})\.([A-Za-z0-9]{4})\.([A-Za-z0-9]{3,4})\.([A-Za-z0-9]{2,4})', clean_q)
    if code_match:
        full_code = code_match.group(0)
        spaced_code = " ".join(code_match.groups())
        
        matched = [
            p for p in manual_pages 
            if full_code.lower() in p["text"].lower() or spaced_code.lower() in p["text"].lower()
        ]
        if matched:
            return matched[:top_k], full_code, "part"
        return [], full_code, "part"

    # 3. قاموس الماكينات بالمسميات الميدانية الرسمية المعتمدة
    all_machines_map = {
        "تغليف": {"name": "ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)", "keys": ["automac", "wrapping", "297", "298", "a55", "fabbri", "stretch"]},
        "أوتوماك": {"name": "ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)", "keys": ["automac", "wrapping", "297", "298", "a55", "fabbri"]},
        "اوتوماك": {"name": "ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)", "keys": ["automac", "wrapping", "297", "298", "a55", "fabbri"]},
        "مايسترو": {"name": "ماكينة التفريغ مايسترو (Maestro Eviscerator 0600)", "keys": ["maestro", "eviscerat", "0600"]},
        "تفريغ": {"name": "ماكينة التفريغ مايسترو (Maestro Eviscerator 0600)", "keys": ["maestro", "eviscerat", "0600", "unloader", "2360", "3860"]},
        "فنت": {"name": "ماكينة قص دجاج نهائي (Vent Cutter 0100)", "keys": ["vent", "cutter", "0100"]},
        "قص دجاج": {"name": "ماكينة قص دجاج نهائي (Vent Cutter 0100)", "keys": ["vent", "cutter", "0100"]},
        "قص دجاج نهائي": {"name": "ماكينة قص دجاج نهائي (Vent Cutter 0100)", "keys": ["vent", "cutter", "0100"]},
        "قص المخرج": {"name": "ماكينة قص دجاج نهائي (Vent Cutter 0100)", "keys": ["vent", "cutter", "0100"]},
        "فتح": {"name": "ماكينة الفتح والمقص (Opening Machine 0450)", "keys": ["opening", "scissors", "0450"]},
        "مقص": {"name": "ماكينة الفتح والمقص (Opening Machine 0450)", "keys": ["opening", "scissors", "0450"]},
        "سكالدر": {"name": "حوض السكالدر (Scalder 0560 / 0990)", "keys": ["scalder", "scalding", "0560", "0990"]},
        "سمط": {"name": "حوض السكالدر (Scalder 0560 / 0990)", "keys": ["scalder", "scalding", "0560", "0990"]},
        "معاطه": {"name": "ماكينة المعاطه (Plucker JM64 / 2470)", "keys": ["plucker", "picking", "jm64", "2470", "0770"]},
        "معاطة": {"name": "ماكينة المعاطه (Plucker JM64 / 2470)", "keys": ["plucker", "picking", "jm64", "2470", "0770"]},
        "رياشه": {"name": "ماكينة المعاطه (Plucker JM64 / 2470)", "keys": ["plucker", "picking", "jm64", "2470", "0770"]},
        "رياشة": {"name": "ماكينة المعاطه (Plucker JM64 / 2470)", "keys": ["plucker", "picking", "jm64", "2470", "0770"]},
        "قوانص": {"name": "ماكينة تنظيف القوانص (Gizzard Harvester CD-6000)", "keys": ["gizzard", "peeler", "cd-6000", "1860"]},
        "علاقات": {"name": "الجنزير والعلاقات (Overhead Conveyor 0230)", "keys": ["shackle", "overhead", "0230"]},
        "جنزير": {"name": "الجنزير والعلاقات (Overhead Conveyor 0230)", "keys": ["shackle", "overhead", "0230"]},
        "شواكل": {"name": "الجنزير والعلاقات (Overhead Conveyor 0230)", "keys": ["shackle", "overhead", "0230"]},
        "تعليق": {"name": "الجنزير والعلاقات (Overhead Conveyor 0230)", "keys": ["shackle", "overhead", "0230"]},
        "نقل كبدة": {"name": "خط نقل الكبدة (Pan Conveyor Single)", "keys": ["pan conveyor", "pan single", "pan"]},
        "نقل كبده": {"name": "خط نقل الكبدة (Pan Conveyor Single)", "keys": ["pan conveyor", "pan single", "pan"]},
        "كبدة": {"name": "خط نقل الكبدة (Pan Conveyor Single)", "keys": ["pan conveyor", "pan single", "pan"]},
        "كبده": {"name": "خط نقل الكبدة (Pan Conveyor Single)", "keys": ["pan conveyor", "pan single", "pan"]},
        "بانات": {"name": "خط نقل الكبدة (Pan Conveyor Single)", "keys": ["pan conveyor", "pan single", "pan"]},
        "أرجل": {"name": "ماكينة قص الأرجل (Hock Cutter 3000)", "keys": ["leg cutter", "hock", "3000"]},
        "ارجل": {"name": "ماكينة قص الأرجل (Hock Cutter 3000)", "keys": ["leg cutter", "hock", "3000"]},
        "رؤوس": {"name": "ماكينة سحب الرؤوس (Head Puller 2920)", "keys": ["head puller", "2920"]},
        "روؤس": {"name": "ماكينة سحب الرؤوس (Head Puller 2920)", "keys": ["head puller", "2920"]},
        "شفاط": {"name": "مضخات الفاكيوم وتفريغ الرئة (Vacuum Pumps)", "keys": ["vacuum", "lung", "robuschi", "2170", "0190"]},
        "فاكيوم": {"name": "مضخات الفاكيوم وتفريغ الرئة (Vacuum Pumps)", "keys": ["vacuum", "lung", "robuschi", "2170", "0190"]},
        "تبريد": {"name": "كمبرسورات ومنظومة التبريد المركزية", "keys": ["compressor", "chiller", "refrigeration", "2410", "airpol", "atlas"]},
        "كمبرسور": {"name": "كمبرسورات ومنظومة التبريد المركزية", "keys": ["compressor", "chiller", "refrigeration", "2410", "airpol", "atlas"]}
    }

    target_keys = []
    display_label = None

    for ar_term, m_data in all_machines_map.items():
        if ar_term in clean_q_lower:
            target_keys = m_data["keys"]
            display_label = m_data["name"]
            break

    num_match = re.search(r'\b\d{4}\b', clean_q)
    if num_match and not display_label:
        target_keys.append(num_match.group(0))
        display_label = f"ماكينة موديل {num_match.group(0)}"

    if target_keys:
        candidates = []
        for p in manual_pages:
            t = p["text"].lower()
            fname = p["filename"].lower()

            if p["page"] <= 7 or "....." in t or ".... " in t:
                continue

            if not any(k in fname for k in target_keys):
                continue

            score = 0
            if "trouble shooting" in t or "troubleshooting" in t:
                score += 5
            if "failure" in t and "cause" in t:
                score += 4
            if "solution" in t or "remedy" in t:
                score += 3
            if "machine doesn't" in t or "alarm" in t or "warning" in t:
                score += 4

            if score >= 3:
                candidates.append((score, p))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            primary_hit = candidates[0][1]
            filepath = primary_hit["filepath"]
            first_page = primary_hit["page"]

            sequential_pages = [
                p for p in manual_pages
                if p["filepath"] == filepath and first_page <= p["page"] <= first_page + 3
            ]
            sequential_pages.sort(key=lambda x: x["page"])
            return sequential_pages, display_label, "trouble_table"
        else:
            matched_machine_pages = [p for p in manual_pages if any(k in p["filename"].lower() for k in target_keys) and p["page"] > 5]
            if matched_machine_pages:
                return matched_machine_pages[:top_k], display_label, "keyword"

    return [], None, None

def view_machine_paired_catalogs(machine_name):
    if not machine_name or machine_name not in machine_catalogs_db:
        return None, None, None, "⚠️ يرجى اختيار ماكينة من القائمة."

    data = machine_catalogs_db[machine_name]
    maint_pdf = data.get("maintenance_manual")
    parts_pdf = data.get("parts_catalog")

    sample_file = maint_pdf or parts_pdf
    cover_img = render_machine_cover_image(sample_file) if sample_file else None

    maint_name = os.path.basename(maint_pdf) if maint_pdf else "غير متوفر كملف منفصل"
    parts_name = os.path.basename(parts_pdf) if parts_pdf else "غير متوفر كملف منفصل"

    info_md = f"""
### 🏭 منظومة كتالوجات: **{machine_name}**
* 🔧 **كتالوج الصيانة والتشغيل (Maintenance Manual):** `{maint_name}`
* ⚙️ **كتالوج قطع الغيار (Spare Parts Catalog):** `{parts_name}`

> 💡 يمكنك الآن تحميل أي من الكتالوجين المرتبطين بالماكينة مباشرة من الأزرار أدناه.
"""
    return maint_pdf, parts_pdf, cover_img, info_md

# ==========================================
# 10. دالة المعالجة والتوجيه الرئيسية
# ==========================================
def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_warehouse_image = None
    matched_catalog_page_img = None
    response = []

    recorded_machine_name = "غير محدد"
    recorded_part_name = "—"
    recorded_code = clean_q

    # 1. معالجة الصورة المرفوعة للقطعة
    if input_image is not None:
        matched_part_no, matched_img = match_uploaded_image(input_image)
        if matched_part_no:
            response.append(f"📸 **تم التعرف بصرياً على صورة القطعة:** `{matched_part_no}`")
            matched_warehouse_image = matched_img
            if not clean_q:
                clean_q = matched_part_no
                recorded_code = matched_part_no
        else:
            if not clean_q:
                tz = pytz.timezone('Asia/Hebron')
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
                fail_msg = f"⚠️ *تنبيه فحص ميداني - مسلخ عزيزا*\n⏰ الوقت: {timestamp}\n📸 تم رفع صورة قطعة لم يتعرف عليها النظام تلقائياً، يرجى التحقق اليدوي."
                send_whatsapp_alert(fail_msg)
                return "❌ لم يتم العثور على صورة متطابقة بصرياً مع قطع المستودع المفهرسة. يرجى إدخال اسم الماكينة، كود الإنذار، أو رقم القطعة كتابةً.\n---\n📲 تم إرسال إشعار لطاقم الصيانة بالمتابعة.", None, None, None, None, None, *generate_kpi_dashboard_data()

    if not clean_q:
        return "⚠️ يرجى إدخال اسم الماكينة بالعربي (مثل: ماكينة التغليف، السكالدر، المعاطه، المايسترو)، كود الإنذار (E002)، أو رقم القطعة.", None, None, None, None, None, *generate_kpi_dashboard_data()

    # 2. فحص رصيد القطعة في مستودع المسلخ من Google Sheet
    inv_info = get_part_inventory_info(clean_q)
    if inv_info:
        qty_str = inv_info["qty"]
        recorded_part_name = inv_info.get("name", "—")
        recorded_code = inv_info.get("raw_code", clean_q)
        try:
            qty_num = float(re.sub(r'[^0-9.]', '', qty_str))
            status_badge = "🟢 متوفر ورصيد آمن" if qty_num > 2 else ("🟡 رصيد منخفض" if qty_num > 0 else "🔴 نافد من المستودع")
        except Exception:
            status_badge = "⚪ رصيد مسجل"

        stock_box = (
            f"### 📦 بطاقة المخزون الميداني (مستودع المسلخ - Google Sheet):\n"
            f"- **رقم القطعة:** `{inv_info['raw_code']}`\n"
            f"- **اسم القطعة:** {inv_info['name']}\n"
            f"- **الكمية المتوفرة حالياً:** **`{qty_str}`** ({status_badge})\n"
            f"- **موقع التخزين / الرف:** {inv_info['location']}\n\n"
            f"---\n"
        )
        response.append(stock_box)

    # 3. محرك البحث في الكتالوجات
    hits, matched_term, hit_type = search_engine(clean_q, top_k=4)
    if not matched_warehouse_image and hit_type not in ["alarm", "trouble_table"]:
        matched_warehouse_image = find_image_for_part(matched_term if matched_term else clean_q)

    if hit_type == "alarm":
        recorded_code = matched_term if matched_term else clean_q
        if hits:
            recorded_machine_name = deduce_machine_from_filename(hits[0]['filename'])
        else:
            recorded_machine_name = "ماكينة التغليف أوتوماك (Automac 55 / 75 / 297)"
        recorded_part_name = f"إنذار عطل تشغيلي ({recorded_code})"
    elif hit_type == "trouble_table":
        recorded_machine_name = matched_term
        recorded_part_name = "جدول استكشاف وفحص الأعطال الشامل"
    elif hit_type == "part":
        recorded_code = matched_term if matched_term else clean_q
        if hits:
            recorded_machine_name = deduce_machine_from_filename(hits[0]['filename'])
        if recorded_part_name == "—" and hits:
            text = hits[0]['text'].replace("\r", " ")
            idx = text.lower().find(recorded_code.lower())
            if idx != -1:
                snippet_name = text[idx:idx+70].split("\n")[0]
                recorded_part_name = snippet_name.strip()
    elif hit_type == "keyword":
        recorded_machine_name = matched_term
    else:
        if hits:
            recorded_machine_name = deduce_machine_from_filename(hits[0]['filename'])

    linked_catalog_files = find_linked_manuals_for_machine(recorded_machine_name)
    maint_pdf_to_download = linked_catalog_files.get("maintenance_manual")
    parts_pdf_to_download = linked_catalog_files.get("parts_catalog")

    # توثيق العملية فورياً في سجل الإكسل
    log_search_query(clean_q, hit_type, recorded_machine_name, recorded_part_name, recorded_code, inv_info)

    header_info = (
        f"> ⚙️️ **الماكينة المستهدفة:** `{recorded_machine_name}`  \n"
        f"> 🏷️ **القطعة / العطل:** `{recorded_part_name}`  \n\n"
    )
    response.insert(0, header_info)

    # أ) إنذارات وتحذيرات ماكينات التغليف (E / W)
    if hit_type == "alarm" and matched_term:
        prefix = matched_term[0]
        alarm_status = "CRITICAL ALARM (MACHINE STOPPED)" if prefix == "E" else "WARNING ALERT (PREVENTIVE)"
        if hits:
            ai_insight = ask_gemini_engineer(clean_q, hits[0]['text'])
            if ai_insight:
                response.append(f"## 🛠️ {matched_term} - {alarm_status}\n")
                response.append(ai_insight)
                response.append("\n" + "="*55 + "\n")
            
            response.append(f"📖 **Technical Manual Reference:** `{hits[0]['filename']}` (Page {hits[0]['page']})")
            matched_catalog_page_img = render_pdf_page_to_image(hits[0]['filepath'], hits[0]['page'])
        else:
            response.append(f"⚠️ No direct catalog page found for `{matched_term}` in current indexed manuals.")

    # ب) جداول استكشاف الأعطال الشاملة
    elif hit_type == "trouble_table":
        response.append(f"## 🛠️ {matched_term} - Complete Technical Troubleshooting Records\n")
        if hits:
            matched_warehouse_image = render_machine_cover_image(hits[0]['filepath'])
            
            combined_trouble_text = "\n\n--- NEXT PAGE ---\n\n".join(
                f"[Page {p['page']}]\n" + p['text'] for p in hits
            )
            
            ai_insight = ask_gemini_engineer(clean_q, combined_trouble_text)
            if ai_insight:
                response.append(ai_insight)
                response.append("\n" + "="*55 + "\n")
            
            pages_numbers = [p['page'] for p in hits]
            pages_str = ", ".join(str(n) for n in pages_numbers)
            response.append(f"📖 **Technical Manual Reference:** `{hits[0]['filename']}` (Pages: {pages_str})")
            matched_catalog_page_img = render_troubleshooting_pages_stitched(hits[0]['filepath'], pages_numbers)
        else:
            response.append(f"⚠️ لم يتم العثور على صفحات جدول الأعطال الخاصة بـ `{matched_term}`.")

    # ج) أرقام القطع والبحث العام
    else:
        if hits:
            ai_insight = ask_gemini_engineer(clean_q, hits[0]['text'])
            if ai_insight:
                response.append("## 🔧 Technical Inspection & Part Details:\n")
                response.append(ai_insight)
                response.append("\n" + "="*55 + "\n")

            response.append("### ✅ Catalog References Found:")
            for h in hits:
                response.append(f"- **Manual:** `{h['filename']}` (Page {h['page']})")
                text = h['text'].replace("\r", "")
                target = matched_term if matched_term else clean_q
                idx = text.lower().find(target.lower().split()[0])
                if idx != -1:
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(target) + 120)
                    snippet = text[start:end].replace("\n", " ").strip()
                else:
                    words = text.split()
                    snippet = " ".join(words[:30])
                response.append(f"  > *\"...{snippet}...\"*\n")
            
            matched_catalog_page_img = render_pdf_page_to_image(hits[0]['filepath'], hits[0]['page'])
        else:
            response.append(f"❌ لم يتم العثور على أي تطابق لطلبك `{clean_q}` داخل صفحات الكتالوجات.")

    if matched_warehouse_image:
        response.append("\n🖼️ **تم إرفاق صورة الماكينة الكاملة في المربع الأيسر.**")
    if matched_catalog_page_img:
        response.append("📖 **تم دمج وعرض صفحات جدول الأعطال الكاملة للتوثيق في المربع الأيمن.**")

    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    alert_msg = f"🔔 *إشعار صيانة وتشخيص - مسلخ عزيزا*\n"
    alert_msg += f"⏰ الوقت: {timestamp}\n"
    alert_msg += f"🏭 الماكينة: {recorded_machine_name}\n"
    alert_msg += f"🏷️ القطعة/العطل: {recorded_part_name}\n"
    alert_msg += f"🔍 الاستعلام: `{clean_q}`\n"
    if inv_info:
        alert_msg += f"📦 رصيد المستودع: {inv_info['qty']} (موقع: {inv_info['location']})\n"
    if hits:
        alert_msg += f"📖 المرجع: {hits[0]['filename']} (Pages: {', '.join(str(p['page']) for p in hits)})\n"

    send_whatsapp_alert(alert_msg)
    response.append("\n---\n📲 تم إرسال إشعار فوري لطاقم الصيانة وتوثيق الماكينة والقطعة في سجل إكسل.")

    # توليد وتحديث بيانات مؤشرات الأداء الحية فوراً مع إتمام البحث
    updated_kpi_cards, updated_top_m, updated_top_p = generate_kpi_dashboard_data()

    return "\n".join(response), matched_warehouse_image, matched_catalog_page_img, LOG_FILE_PATH, maint_pdf_to_download, parts_pdf_to_download, updated_kpi_cards, updated_top_m, updated_top_p

# ==========================================
# 11. واجهة Gradio الرسمية مع التحديث اللحظي للـ KPI
# ==========================================
total_manuals = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

logo_html = f'<img src="data:image/png;base64,{logo_base64}" style="width: 100%; height: 100%; object-fit: contain;">' if logo_base64 else '<span style="font-size: 20px; font-weight: 900; color: #1b5e20;">عزيزا</span>'

HEADER_HTML = f"""
<div style="background: linear-gradient(135deg, #0b3d20 0%, #1b5e20 100%); padding: 18px 25px; border-radius: 14px; color: white; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.18); direction: rtl; text-align: right; border-bottom: 4px solid #ffcc00;">
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 15px;">
        <div style="display: flex; align-items: center; gap: 20px;">
            <div style="background: #ffffff; border-radius: 50%; padding: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; width: 85px; height: 85px; border: 3px solid #ffcc00; overflow: hidden;">
                {logo_html}
            </div>
            <div>
                <h1 style="margin: 0; font-size: 23px; font-weight: 800; color: #ffffff;">شركة دواجن فلسطين - مسلخ عزيزا</h1>
                <p style="margin: 4px 0 0 0; font-size: 14px; color: #e8f5e9;">منصة الصيانة الهندسية الذكية (جداول الفحص الدوري Checklist • مؤشرات KPI • الكتالوج المزدوج)</p>
            </div>
        </div>
        <div style="border-right: 2px solid rgba(255,255,255,0.25); padding-right: 20px;">
            <span style="font-size: 12px; color: #c8e6c9; display: block;">إعداد وتطوير النظام:</span>
            <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">م. فادي محمود</span>
            <span style="font-size: 12px; color: #e8f5e9; display: block;">مسؤول قسم الصيانة والأتمتة</span>
        </div>
    </div>
</div>
"""

with gr.Blocks(title="منصة الصيانة الهندسية الذكية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تجهيز وفهرسة `{total_manuals}` كتالوج فني، وتفعيل لوحة الصيانة الدورية (Checklists) ومؤشرات الأداء اللحظية (KPIs).")
        
    with gr.Tabs():
        # التبويب الأول: البحث الذكي والتشخيص
        with gr.Tab("🔍 التشخيص الهندسي والبحث الفوري"):
            with gr.Row():
                with gr.Column(scale=1):
                    query_input = gr.Textbox(
                        label="أدخل استعلامك: اسم الماكينة بالعربي / كود الإنذار (E002) / رقم القطعة (4 مقاطع)",
                        placeholder="أمثلة: ماكينة التغليف | حوض السكالدر | ماكينة المعاطه | خط نقل الكبدة | الجنزير والعلاقات | E002 | W010",
                        lines=2
                    )
                    image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة للتعرف البصري عليها ومطابقتها")
                    submit_btn = gr.Button("تشخيص العطل وتوثيق العملية في السجل 🔍", variant="primary")
                    clear_btn = gr.Button("مسح الحقول")
                    
                    download_log_file = gr.File(
                        label="📊 تحميل سجل الأعطال والماكينات (Excel / CSV)",
                        interactive=False
                    )
                    with gr.Row():
                        download_searched_maint = gr.File(
                            label="🔧 كتالوج الصيانة للماكينة المستهدفة (PDF)",
                            interactive=False
                        )
                        download_searched_parts = gr.File(
                            label="⚙️ كتالوج قطع الغيار للماكينة المستهدفة (PDF)",
                            interactive=False
                        )
                    
                with gr.Column(scale=1):
                    output_box = gr.Markdown(label="تقرير الفحص الفني وجدول الأعطال والمخزون")
                    with gr.Row():
                        matched_warehouse_img_output = gr.Image(type="filepath", label="صورة الماكينة الكاملة / قطعة المستودع")
                        matched_catalog_page_output = gr.Image(type="filepath", label="صفحات جدول الأعطال الكاملة (مدمجة)")

        # التبويب الثاني: لوحة الصيانة الدورية وقوائم الفحص (Checklists)
        with gr.Tab("📋 لوحة الصيانة الدورية وقوائم الفحص (PM Checklists)"):
            with gr.Row():
                with gr.Column(scale=1):
                    pm_machine_dropdown = gr.Dropdown(
                        choices=available_machines_list,
                        label="اختر الماكينة لعرض جدول الفحص الوقائي والتشحيم الدوري (Checklist):",
                        value=available_machines_list[0] if available_machines_list else None
                    )
                    get_pm_btn = gr.Button("استخراج وتحديث جدول الصيانة الدورية 📋", variant="primary")
                    pm_output_text = gr.Markdown()
                with gr.Column(scale=1):
                    pm_image_output = gr.Image(
                        type="filepath",
                        label="صفحة جدول الصيانة الدورية الأصلية من الكتالوج"
                    )

            get_pm_btn.click(
                fn=extract_pm_checklist_for_machine,
                inputs=[pm_machine_dropdown],
                outputs=[pm_image_output, pm_output_text]
            )
            pm_machine_dropdown.change(
                fn=extract_pm_checklist_for_machine,
                inputs=[pm_machine_dropdown],
                outputs=[pm_image_output, pm_output_text]
            )

        # التبويب الثالث: لوحة مؤشرات الأداء الحية (KPI Dashboard)
        with gr.Tab("📈 لوحة مؤشرات الأداء الحية للقسم (KPI Dashboard)") as kpi_tab:
            with gr.Column():
                refresh_kpi_btn = gr.Button("🔄 تحديث قراءات ومؤشرات الأداء اللحظية", variant="secondary")
                kpi_cards_html = gr.HTML()
                
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 🚨 أعلى 5 ماكينات تسجيلاً للأعطال والفحوصات (Top Critical Assets):")
                        top_machines_table = gr.Markdown()
                    with gr.Column(scale=1):
                        gr.Markdown("### ⚙️ أكثر 5 قطع غيار استعلاماً وطلباً (High-Demand Spare Parts):")
                        top_parts_table = gr.Markdown()

            # 1. تحديث المؤشرات عند تحميل الصفحة
            demo.load(
                fn=generate_kpi_dashboard_data,
                outputs=[kpi_cards_html, top_machines_table, top_parts_table]
            )
            # 2. تحديث المؤشرات تلقائياً بمجرد النقر على تبويب المؤشرات
            kpi_tab.select(
                fn=generate_kpi_dashboard_data,
                outputs=[kpi_cards_html, top_machines_table, top_parts_table]
            )
            # 3. تحديث المؤشرات عبر الزر اليدوي
            refresh_kpi_btn.click(
                fn=generate_kpi_dashboard_data,
                outputs=[kpi_cards_html, top_machines_table, top_parts_table]
            )

        # التبويب الرابع: مكتبة الماكينات المزدوجة (كتالوج الصيانة + كتالوج قطع الغيار)
        with gr.Tab("📚 مكتبة الماكينات (كتالوج الصيانة + قطع الغيار معاً)"):
            with gr.Row():
                with gr.Column(scale=1):
                    machine_dropdown = gr.Dropdown(
                        choices=available_machines_list,
                        label="اختر الماكينة لعرض كتالوج الصيانة وكتالوج قطع الغيار المرتبطين بها بالمسميات الرسمية:",
                        value=available_machines_list[0] if available_machines_list else None
                    )
                    view_machine_btn = gr.Button("استعراض منظومة الكتالوجات المرتبطة 📖", variant="secondary")
                    machine_info_output = gr.Markdown()
                    with gr.Row():
                        download_machine_maint = gr.File(
                            label="🔧 تحميل كتالوج الصيانة والتشغيل (User/Maintenance PDF)",
                            interactive=False
                        )
                        download_machine_parts = gr.File(
                            label="⚙️ تحميل كتالوج قطع الغيار والرسم المنفجر (Parts PDF)",
                            interactive=False
                        )
                with gr.Column(scale=1):
                    machine_cover_output = gr.Image(
                        type="filepath", 
                        label="صورة غلاف الماكينة من الكتالوج الأصلي"
                    )

            view_machine_btn.click(
                fn=view_machine_paired_catalogs,
                inputs=[machine_dropdown],
                outputs=[download_machine_maint, download_machine_parts, machine_cover_output, machine_info_output]
            )
            machine_dropdown.change(
                fn=view_machine_paired_catalogs,
                inputs=[machine_dropdown],
                outputs=[download_machine_maint, download_machine_parts, machine_cover_output, machine_info_output]
            )

    # ربط عملية البحث بتحديث لوحة الـ KPI فورياً مع مخرجات التقرير
    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[
            output_box, 
            matched_warehouse_img_output, 
            matched_catalog_page_output, 
            download_log_file, 
            download_searched_maint, 
            download_searched_parts,
            kpi_cards_html,
            top_machines_table,
            top_parts_table
        ]
    )
    clear_btn.click(
        lambda: ("", None, "", None, None, None, None, None),
        outputs=[
            query_input, 
            image_input, 
            output_box, 
            matched_warehouse_img_output, 
            matched_catalog_page_output, 
            download_log_file, 
            download_searched_maint, 
            download_searched_parts
        ]
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        allowed_paths=["/tmp"],
        share=True
    )
