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

# مسار ملف سجل عمليات البحث (Excel / CSV)
LOG_FILE_PATH = "/tmp/maintenance_search_log.csv"

# رابط استعلام جدول Google Sheets المباشر والمستقر للمخزون
SHEET_ID = "1_scf-CUSouwQvJan4d12UuC7LX8eHC7E4YAjC41q2r4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[!] Warning initializing Gemini API: {e}")

def sync_data_from_gcs():
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
# 2. محرك تسجيل وتوثيق عمليات البحث (Search Logger)
# ==========================================
def log_search_query(query, hit_type, matched_term, stock_info):
    """تسجيل تفاصيل العملية في ملف السجل الميداني لحساب تكرار الأعطال"""
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M:%S %p')
    
    file_exists = os.path.exists(LOG_FILE_PATH)
    try:
        with open(LOG_FILE_PATH, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Searched_Query", "Hit_Type", 
                    "Matched_Entity", "Stock_Available", "Warehouse_Location"
                ])
            
            qty = stock_info.get("qty", "N/A") if stock_info else "N/A"
            loc = stock_info.get("location", "N/A") if stock_info else "N/A"
            
            writer.writerow([
                timestamp,
                query,
                hit_type if hit_type else "General Search",
                matched_term if matched_term else "N/A",
                qty,
                loc
            ])
            print(f"[✓] Query logged successfully: {query}")
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
# 4. فهرسة صفحات الكتالوجات وبصمات صور المستودع
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
# 5. دوال استخراج ومعالجة الصور ودمج الصفحات
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
        out_img_path = f"/tmp/cover_{os.path.basename(filepath)}.png"
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

# ==========================================
# 6. محرك Gemini لاستخراج جميع الأعطال بالكامل
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
# 7. محرك البحث الذكي (متعدد الصفحات للأعطال)
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

    # 3. خريطة ماكينات المجزر
    all_machines_map = {
        "مايسترو": {"name": "ماكينة التفريغ مايسترو (Maestro)", "keys": ["maestro", "eviscerat", "0600"]},
        "تفريغ": {"name": "ماكينة التفريغ مايسترو (Maestro)", "keys": ["maestro", "eviscerat", "0600", "unloader", "2360", "3860"]},
        "فتح": {"name": "ماكينة الفتح والمقص (Opening Scissors)", "keys": ["opening", "scissors", "0450"]},
        "مقص": {"name": "ماكينة الفتح والمقص (Opening Scissors)", "keys": ["opening", "scissors", "0450"]},
        "فنت": {"name": "ماكينة قص المخرج الفنت (Vent Cutter)", "keys": ["vent", "cutter", "0100"]},
        "رياشة": {"name": "ماكينة نزع الريش (Plucker)", "keys": ["plucker", "picking", "jm64", "2470", "0770"]},
        "سمط": {"name": "حوض السمط (Scalder)", "keys": ["scalder", "scalding", "0560", "0990"]},
        "سكالدر": {"name": "حوض السمط (Scalder)", "keys": ["scalder", "scalding", "0560", "0990"]},
        "قوانص": {"name": "ماكينة تنظيف القوانص (Gizzard Processor)", "keys": ["gizzard", "peeler", "cd-6000", "1860"]},
        "تعليق": {"name": "سير الشواكل والتعليق (Overhead Conveyor)", "keys": ["shackle", "overhead", "0230"]},
        "شواكل": {"name": "سير الشواكل والتعليق (Overhead Conveyor)", "keys": ["shackle", "overhead", "0230"]},
        "أرجل": {"name": "ماكينة قص الأرجل (Hock / Leg Cutter)", "keys": ["leg cutter", "hock", "3000"]},
        "رؤوس": {"name": "ماكينة سحب الرؤوس (Head Puller)", "keys": ["head puller", "2920"]},
        "شفاط": {"name": "مضخات الفاكيوم وتفريغ الرئة (Vacuum Pump)", "keys": ["vacuum", "lung", "robuschi", "2170", "0190"]},
        "تغليف": {"name": "ماكينة التغليف أوتوماك (Automac)", "keys": ["automac", "wrapping", "297", "298", "a55"]},
        "تبريد": {"name": "كمبرسورات ومنظومات التبريد", "keys": ["compressor", "chiller", "refrigeration", "2410", "airpol", "atlas"]}
    }

    is_trouble_intent = any(k in clean_q_lower for k in [
        "عطل", "مشكل", "جدول", "فحص", "صيانة", "توقف", "trouble", "fault", "failure",
        "meyn", "ماكينات", "حل", "سبب", "سحب"
    ])

    target_keys = []
    display_label = "Meyn Machine"
    for ar_term, m_data in all_machines_map.items():
        if ar_term in clean_q_lower:
            target_keys = m_data["keys"]
            display_label = m_data["name"]
            break

    num_match = re.search(r'\b\d{4}\b', clean_q)
    if num_match:
        target_keys.append(num_match.group(0))
        if display_label == "Meyn Machine":
            display_label = f"ماكينة موديل {num_match.group(0)}"

    if is_trouble_intent or target_keys:
        candidates = []
        for p in manual_pages:
            t = p["text"].lower()

            if p["page"] <= 7 or "....." in t or ".... " in t:
                continue

            if target_keys and not any(k in p["filename"].lower() for k in target_keys):
                continue

            score = 0
            if "trouble shooting" in t or "troubleshooting" in t:
                score += 5
            if "failure" in t and "cause" in t:
                score += 4
            if "solution" in t or "remedy" in t:
                score += 3
            if "machine doesn't" in t or "doesn't start" in t or "doesn't cut" in t:
                score += 4

            if score >= 5:
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

    if target_keys:
        fallback = [p for p in manual_pages if any(k in p["filename"].lower() for k in target_keys) and p["page"] > 5]
        if fallback:
            return fallback[:top_k], display_label, "keyword"

    return [], None, None

# ==========================================
# 8. دالة المعالجة والتوجيه الرئيسية
# ==========================================
def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_warehouse_image = None
    matched_catalog_page_img = None
    response = []

    # 1. معالجة الصورة المرفوعة للقطعة
    if input_image is not None:
        matched_part_no, matched_img = match_uploaded_image(input_image)
        if matched_part_no:
            response.append(f"📸 **تم التعرف بصرياً على صورة القطعة:** `{matched_part_no}`")
            matched_warehouse_image = matched_img
            if not clean_q:
                clean_q = matched_part_no
        else:
            if not clean_q:
                tz = pytz.timezone('Asia/Hebron')
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
                fail_msg = f"⚠️ *تنبيه فحص ميداني - مسلخ عزيزا*\n⏰ الوقت: {timestamp}\n📸 تم رفع صورة قطعة لم يتعرف عليها النظام تلقائياً، يرجى التحقق اليدوي."
                send_whatsapp_alert(fail_msg)
                return "❌ لم يتم العثور على صورة متطابقة بصرياً مع قطع المستودع المفهرسة. يرجى إدخال اسم الماكينة، كود الإنذار، أو رقم القطعة كتابةً.\n---\n📲 تم إرسال إشعار لطاقم الصيانة بالمتابعة.", None, None, None

    if not clean_q:
        return "⚠️ يرجى إدخال اسم الماكينة بالعربي (مثل: السكالدر أو الفنت أو الفتح)، كود الإنذار (E002)، أو رقم القطعة.", None, None, None

    # 2. فحص رصيد القطعة في مستودع المسلخ من Google Sheet
    inv_info = get_part_inventory_info(clean_q)
    if inv_info:
        qty_str = inv_info["qty"]
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

    hits, matched_term, hit_type = search_engine(clean_q, top_k=4)
    if not matched_warehouse_image and hit_type not in ["alarm", "trouble_table"]:
        matched_warehouse_image = find_image_for_part(matched_term if matched_term else clean_q)

    # 3. تسجيل حركة البحث فورياً في سجل العمليات
    log_search_query(clean_q, hit_type, matched_term, inv_info)

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

    # إشعار الواتساب التلقائي
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    alert_msg = f"🔔 *إشعار صيانة وتشخيص - مسلخ عزيزا*\n"
    alert_msg += f"⏰ الوقت: {timestamp}\n"
    alert_msg += f"🔍 الاستعلام: `{clean_q}`\n"
    if inv_info:
        alert_msg += f"📦 رصيد المخزون المتوفر: {inv_info['qty']} (موقع: {inv_info['location']})\n"
    if hits:
        alert_msg += f"📖 المرجع: {hits[0]['filename']} (Pages: {', '.join(str(p['page']) for p in hits)})\n"
    if matched_warehouse_image:
        alert_msg += f"🖼️ الحالة: تم استخراج صورة الماكينة وجدول الأعطال كاملاً."

    send_whatsapp_alert(alert_msg)
    response.append("\n---\n📲 تم إرسال إشعار فوري لطاقم الصيانة وتوثيق العملية في سجل إكسل.")

    return "\n".join(response), matched_warehouse_image, matched_catalog_page_img, LOG_FILE_PATH

# ==========================================
# 9. واجهة Gradio الرسمية
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
                <p style="margin: 4px 0 0 0; font-size: 14px; color: #e8f5e9;">نظام الصيانة والتشخيص الهندسي الذكي (سجل الأعطال التراكمي • مخزون المستودع المباشر)</p>
            </div>
        </div>
        <div style="border-right: 2px solid rgba(255,255,255,0.25); padding-right: 20px;">
            <span style="font-size: 12px; color: #c8e6c9; display: block;">إعداد وتطوير النظام:</span>
            <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">م. فادي محمود</span>
            <span style="font-size: 12px; color: #e8f5e9; display: block;">مسؤول قسم الصيانة </span>
        </div>
    </div>
</div>
"""

with gr.Blocks(title="منصة الصيانة الهندسية الذكية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تجهيز وفهرسة `{total_manuals}` كتالوج فني، وربط مخزون المستودع مع نظام التوثيق التلقائي لسجل الأعطال.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل استعلامك: اسم الماكينة بالعربي / كود الإنذار (E002) / رقم القطعة (4 مقاطع)",
                placeholder="أمثلة: ما هي مشاكل السكالدر | مشاكل ماكينة الفتح | ماكينة الفنت | المايسترو | E002 | W010 | 0587.0040.008.00",
                lines=2
            )
            image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة للتعرف البصري عليها ومطابقتها")
            submit_btn = gr.Button("تشخيص العطل وتوثيق العملية في السجل 🔍", variant="primary")
            clear_btn = gr.Button("مسح الحقول")
            
            # زر وزاوية تحميل سجل عمليات البحث بصيغة إكسل للفريق الإداري ولجنة التحكيم
            download_log_file = gr.File(
                label="📊 تحميل سجل عمليات البحث والأعطال (Excel / CSV)",
                interactive=False
            )
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني وجدول الأعطال والمخزون")
            with gr.Row():
                matched_warehouse_img_output = gr.Image(type="filepath", label="صورة الماكينة الكاملة / قطعة المستودع")
                matched_catalog_page_output = gr.Image(type="filepath", label="صفحات جدول الأعطال الكاملة (مدمجة)")
            
    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[output_box, matched_warehouse_img_output, matched_catalog_page_output, download_log_file]
    )
    clear_btn.click(
        lambda: ("", None, "", None, None, None),
        outputs=[query_input, image_input, output_box, matched_warehouse_img_output, matched_catalog_page_output, download_log_file]
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        allowed_paths=["/tmp"],
        share=True
    )
