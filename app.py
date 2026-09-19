import os
import glob
import re
import io
import base64
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer, util
import gradio as gr
from google.cloud import storage

PORT = int(os.environ.get("PORT", 8080))

# ==========================================
# 1. إعدادات النظام وتنبيهات الواتساب
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
MY_PHONE = "970599431267"

BUCKET_NAME = "aziza-manuals-storage"
LOCAL_DIR = "/tmp/Maintenance_Manuals"
os.makedirs(LOCAL_DIR, exist_ok=True)

print(f"📦 جاري مزامنة الكتالوجات والصور من Google Cloud Storage: {BUCKET_NAME}...")
try:
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)
    blobs = list(bucket.list_blobs())
    print(f"🔍 تم العثور على {len(blobs)} ملف في الحاوية السحابية.")
    for blob in blobs:
        # تجنب المجلدات الافتراضية
        if blob.name.endswith("/"):
            continue
        dest_path = os.path.join(LOCAL_DIR, blob.name)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if not os.path.exists(dest_path):
            blob.download_to_filename(dest_path)
    print("✅ اكتملت المزامنة السحابية فائقة السرعة بنجاح!")
except Exception as e:
    print(f"⚠️ خطأ أثناء المزامنة السحابية: {e}")

# مسار العمل على الملفات المحلية بعد سحبها
MANUALS_DIR = LOCAL_DIR

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"⚡ جاري تحميل نموذج CLIP على: {device}")
visual_model = SentenceTransformer('clip-ViT-B-32', device=device)

IMAGE_INDEX = []
IMAGE_PATHS = []

def build_visual_database():
    """بناء بصمات صور القطع من المجلد المحلي"""
    global IMAGE_INDEX, IMAGE_PATHS
    IMAGE_INDEX = []
    IMAGE_PATHS = []

    valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG")
    all_imgs = []
    for ext in valid_exts:
        all_imgs.extend(glob.glob(os.path.join(MANUALS_DIR, "**", ext), recursive=True))

    target_imgs = [p for p in set(all_imgs) if "logo" not in os.path.basename(p).lower()]
    print(f"🔍 جاري فهرسة {len(target_imgs)} صورة قطع...")

    for path in target_imgs:
        try:
            img = Image.open(path).convert('RGB')
            IMAGE_INDEX.append(visual_model.encode(img, convert_to_tensor=True))
            IMAGE_PATHS.append(path)
        except Exception:
            continue

    if IMAGE_INDEX:
        IMAGE_INDEX = torch.stack(IMAGE_INDEX)
        print(f"✅ تم فهرسة {len(IMAGE_PATHS)} صورة بنجاح.")

build_visual_database()

all_pdfs_count = len(glob.glob(os.path.join(MANUALS_DIR, "**", "*.pdf"), recursive=True))
SYSTEM_LOGS = f"✅ النظام جاهز بالكامل! مفهرس: {all_pdfs_count} كتالوج PDF و {len(IMAGE_PATHS)} صورة قطعة."
print(SYSTEM_LOGS)

def get_logo_base64():
    for ext in ["logo.png", "logo.jpg", "logo.jpeg"]:
        for p in glob.glob(os.path.join(MANUALS_DIR, "**", ext), recursive=True):
            if os.path.exists(p):
                with open(p, "rb") as img_file:
                    b64 = base64.b64encode(img_file.read()).decode("utf-8")
                    mime = "image/png" if ext.endswith("png") else "image/jpeg"
                    return f"data:{mime};base64,{b64}"
    return None

def send_whatsapp_alert(query_text, info_summary):
    try:
        url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        local_tz = pytz.timezone("Asia/Gaza")
        now_str = datetime.now(local_tz).strftime("%I:%M %p")
        payload = {
            "chatId": f"{MY_PHONE}@c.us",
            "message": (
                f"🏭 *مسلخ شركة دواجن فلسطين - استفسار فني*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 الاستفسار: {query_text}\n"
                f"📋 النتيجة: {info_summary}"
            )
        }
        requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=4)
    except Exception as e:
        print(f"WhatsApp Error: {e}")

def get_clean_machine_name(pdf_path):
    fname = os.path.basename(pdf_path)
    clean_name = fname.replace(".pdf", "").replace("pdf.", "").replace("-1", "").strip()
    return f"{clean_name} — [`{fname}`]"

def search_part_number_in_all_manuals(raw_input):
    clean_input = raw_input.strip()
    alarm_match = re.search(r'\b([Ee]\s*[-_]?\s*\d{1,4})\b', clean_input)
    core_match = re.search(r'(\d{3,4}\.[\w\d]+\.\d{2,3}(?:\.\d{2})?)', clean_input)
    if not core_match:
        core_match = re.search(r'(\d{3,4}\.\d{3}\.\d{2})', clean_input)

    search_targets = set()
    detected_key = clean_input

    if alarm_match:
        raw_code = alarm_match.group(1).replace(" ", "").upper()
        digits = re.sub(r'[^0-9]', '', raw_code)
        int_val = int(digits) if digits else 0
        detected_key = raw_code
        search_targets.update([
            raw_code, f"E-{digits}", f"E {digits}", f"E{int_val}", f"E-{int_val}",
            f"E0{int_val}", f"E-0{int_val}", f"Error {int_val}", f"Alarm {int_val}",
            f"Error {digits}", f"Alarm {digits}"
        ])
    elif core_match:
        core_number = core_match.group(1)
        detected_key = core_number
        search_targets.update([
            clean_input, core_number, f"D{core_number}", f"C{core_number}",
            f"H{core_number}", core_number.replace(".", "")
        ])
    else:
        words = [w for w in re.split(r'[\s,;:_/\-]+', clean_input) if len(w) >= 3]
        search_targets.update(words)

    sub_parts = re.findall(r'\d{2,4}', detected_key)
    if len(sub_parts) >= 3:
        p1, p2, p3 = sub_parts[-3], sub_parts[-2], sub_parts[-1]
        search_targets.update([
            f"{p1}.{p2}.{p3}", f"{p1}{p2}{p3}", f"D{p1}.{p2}.{p3}",
            f"C{p1}.{p2}.{p3}", f"{p1}.{p2}", f"{p2}.{p3}"
        ])

    valid_targets = [re.escape(t) for t in search_targets if len(t) >= 2]
    if not valid_targets:
        return clean_input, detected_key, []

    search_regex = re.compile(r'(' + '|'.join(valid_targets) + r')', re.IGNORECASE)
    all_pdfs = glob.glob(f"{MANUALS_DIR}/**/*.pdf", recursive=True)
    matches = []

    for pdf_path in all_pdfs:
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                if search_regex.search(text):
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    snippet = []
                    for i, line in enumerate(lines):
                        if search_regex.search(line):
                            start = max(0, i - 1)
                            end = min(len(lines), i + 4)
                            snippet = lines[start:end]
                            break
                    matches.append({
                        "machine_name": get_clean_machine_name(pdf_path),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في الدليل الفني."
                    })
                    if len([m for m in matches if m['machine_name'] == get_clean_machine_name(pdf_path)]) >= 2:
                        break
            doc.close()
        except Exception:
            continue

    return clean_input, detected_key, matches

def visual_maintenance_copilot(image_file, text_input):
    status_prefix = f"> 📡 **حالة النظام:** `{SYSTEM_LOGS}`\n\n---\n\n"

    if image_file is not None:
        if len(IMAGE_INDEX) == 0:
            return status_prefix + "⚠️ لا توجد صور مفهرسة في المجلد.", None

        uploaded_rgb = image_file.convert('RGB')
        query_emb = visual_model.encode(uploaded_rgb, convert_to_tensor=True)
        cos_scores = util.cos_sim(query_emb, IMAGE_INDEX)[0]
        best_idx = torch.argmax(cos_scores).item()
        best_score = float(cos_scores[best_idx])

        if best_score >= 0.40:
            matched_img_path = IMAGE_PATHS[best_idx]
            raw_part_code = os.path.splitext(os.path.basename(matched_img_path))[0]

            num_blocks = re.findall(r'\d{2,4}', raw_part_code)
            if len(num_blocks) >= 3:
                refined_query = f"{num_blocks[-3]}.{num_blocks[-2]}.{num_blocks[-1]}"
            else:
                refined_query = raw_part_code

            _, _, manual_matches = search_part_number_in_all_manuals(refined_query)
            matched_pil = Image.open(matched_img_path)
            percent = int(best_score * 100)

            send_whatsapp_alert(f"مطابقة بصرية لصورة قطعة ({percent}%)", f"رقم القطعة: {refined_query}")

            res = status_prefix + f"### 🎯 نتيجة التعرف البصري على القطعة\n\n"
            res += f"* **نسبة التطابق البصري:** `{percent}%`\n"
            res += f"* **رقم القطعة المعتمد:** `{refined_query}`\n\n"
            res += "### 📖 بيانات الكتالوجات والماكينات المشتركة:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                    res += f"* ⚙️ **التفاصيل الفنية:** `{item['details']}`\n\n"
            else:
                res += f"تم التعرف على شكل القطعة ولكن لم يتم العثور على الرقم `{refined_query}` داخل نصوص الكتالوجات المرفوعة.\n"
            return res, matched_pil
        else:
            return status_prefix + f"⚠️ لم يتم العثور على تطابق كافٍ (نسبة الثقة: {int(best_score * 100)}%).", None

    user_text = (text_input or "").strip()
    if user_text:
        full_input, detected_key, found_records = search_part_number_in_all_manuals(user_text)
        found_img = None

        clean_key = re.sub(r'[^a-zA-Z0-9]', '', detected_key).lower()
        full_clean = re.sub(r'[^a-zA-Z0-9]', '', full_input).lower()

        for p in IMAGE_PATHS:
            fname = os.path.splitext(os.path.basename(p))[0].lower()
            fname_clean = re.sub(r'[^a-zA-Z0-9]', '', fname)
            if (clean_key and (clean_key in fname_clean or fname_clean in clean_key)) or \
               (len(fname_clean) >= 4 and fname_clean in full_clean):
                try:
                    found_img = Image.open(p)
                    break
                except Exception:
                    continue

        summary_info = f"تم إيجاد {len(found_records)} كتالوج مطابق" if found_records else "لا يوجد تطابق"
        send_whatsapp_alert(user_text, summary_info)

        if found_records:
            res = status_prefix + f"### 📦 تقرير المطابقة للاستفسار: `{detected_key}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 الصفحة داخل الدليل: **{item['page']}**\n"
                res += f"* ⚙️ البيانات والحلول الفنية: `{item['details']}`\n\n"
            return res, found_img
        else:
            return status_prefix + f"⚠️ لم يتم العثور على تطابق في الكتالوجات للرمز/الرقم `{detected_key}`.", None

    return status_prefix + "يرجى التقاط صورة للقطعة أو كتابة رقمها/كود الإنذار للبدء.", None

logo_data_url = get_logo_base64()
logo_html = f'<img src="{logo_data_url}" style="height: 75px; margin-left: 20px; border-radius: 8px; vertical-align: middle;">' if logo_data_url else ''

header_markdown = f"""
<div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid #2b6cb0; padding-bottom: 12px; margin-bottom: 20px;">
    <div style="display: flex; align-items: center;">
        {logo_html}
        <div>
            <h1 style="margin: 0; color: #1a365d; font-size: 26px;">منصة الدعم الهندسي في مسلخ شركة دواجن فلسطين</h1>
            <p style="margin: 5px 0 0 0; color: #4a5568; font-size: 15px;">نظام التعرف البصري الذكي على قطع الغيار الميكانيكية ومطابقة الكتالوجات وحلول الأعطال (Meyn / Automac)</p>
        </div>
    </div>
</div>
"""

footer_markdown = """
<div style="margin-top: 40px; padding-top: 15px; border-top: 1px solid #e2e8f0; text-align: center; color: #718096; font-size: 14px;">
    ⚙️ تم تصميم وتطوير المنصة بواسطة <strong>م. فادي محمود</strong> | قسم الصيانة والدعم الهندسي — مسلخ شركة دواجن فلسطين
</div>
"""

with gr.Blocks(title="منصة الدعم الهندسي - التعرف البصري على القطع") as demo:
    gr.HTML(header_markdown)
    gr.Markdown("""
    * **فحص القطعة بالصورة:** التقط صورة واضحة للقطعة الميكانيكية، وسيتعرف النظام على شكلها ويحدد كافة الماكينات المشتركة ورقم الصفحة.
    * **البحث النصي / أكواد الأعطال:** يمكنك إدخال رقم القطعة مباشرة، أو كتابة كود الإنذار (مثل **E002**) لمطابقة العطل في أدلة التشغيل والصيانة.
    """)

    with gr.Row():
        with gr.Column(scale=1):
            cam_box = gr.Image(sources=["upload", "webcam"], type="pil", label="📷 تصوير القطعة أو رفع صورة من الهاتف")
            text_box = gr.Textbox(lines=1, label="📝 أو اكتب رقم القطعة / كود الإنذار (اختياري)", placeholder="مثال: 0098.0020.003.03 أو E002")
            search_btn = gr.Button("🔍 فحص ومطابقة القطعة بالكتالوجات", variant="primary")

        with gr.Column(scale=1):
            info_output = gr.Markdown(label="📋 التقرير الفني والمطابقة")
            matched_image_display = gr.Image(label="🖼️ القطعة المطابقة من أرشيف المستودع")

    search_btn.click(
        fn=visual_maintenance_copilot,
        inputs=[cam_box, text_box],
        outputs=[info_output, matched_image_display]
    )

    gr.HTML(footer_markdown)

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT,
        share=False,
        inbrowser=False
    )
