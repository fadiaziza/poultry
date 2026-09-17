import os
import glob
import re
import base64
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer, util
import gradio as gr

# ==========================================
# 1. إعدادات النظام وتنبيهات الواتساب
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
MY_PHONE = "970599431267"
DRIVE_FOLDER_PATH = "./"
REAL_IMAGES_PATH = "./Real_Parts_Images"

# تحميل نموذج الرؤية البصرية خفيف الوزن وعالي الدقة للتشابه البصري
device = "cuda" if torch.cuda.is_available() else "cpu"
visual_model = SentenceTransformer('clip-ViT-B-32', device=device)

# فهرس بصري لتخزين متجهات صور المستودع
IMAGE_INDEX = []
IMAGE_PATHS = []

def build_visual_database():
    """بناء بصمات رقمية لجميع صور القطع المحفوظة في درايف"""
    global IMAGE_INDEX, IMAGE_PATHS
    IMAGE_INDEX = []
    IMAGE_PATHS = []

    if not os.path.exists(REAL_IMAGES_PATH):
        return

    valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp")
    all_imgs = []
    for ext in valid_exts:
        all_imgs.extend(glob.glob(os.path.join(REAL_IMAGES_PATH, ext)))

    for path in all_imgs:
        try:
            img = Image.open(path).convert('RGB')
            embedding = visual_model.encode(img, convert_to_tensor=True)
            IMAGE_INDEX.append(embedding)
            IMAGE_PATHS.append(path)
        except Exception:
            continue

    if IMAGE_INDEX:
        IMAGE_INDEX = torch.stack(IMAGE_INDEX)
        print(f"✅ تمت فهرسة {len(IMAGE_PATHS)} صورة قطعة غيار بنجاح")

build_visual_database()

def get_logo_base64():
    for ext in ["logo.png", "logo.jpg", "logo.jpeg"]:
        p = os.path.join(DRIVE_FOLDER_PATH, ext)
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
                f"🏭 *مسلخ شركة دواجن فلسطين - فحص صورة قطعة*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 الاستفسار: {query_text}\n"
                f"📋 النتيجة: {info_summary}"
            )
        }
        requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=4)
    except Exception as e:
        print(f"⚠️ تعذر إرسال الواتساب: {e}")

def get_clean_machine_name(pdf_path):
    fname = os.path.basename(pdf_path)
    clean_name = fname.replace(".pdf", "").replace("pdf.", "").replace("-1", "").strip()
    return f"{clean_name} — [`{fname}`]"

# ==========================================
# 2. مطابقة الصورة بصرياً مع المستودع
# ==========================================
def find_part_by_image(uploaded_image):
    """مقارنة صورة الفني بصور درايف واستخراج اسم الصورة الأقرب"""
    if len(IMAGE_INDEX) == 0:
        return None, 0.0

    uploaded_rgb = uploaded_image.convert('RGB')
    query_emb = visual_model.encode(uploaded_rgb, convert_to_tensor=True)

    # حساب نسبة التشابه الجيبي (Cosine Similarity)
    cos_scores = util.cos_sim(query_emb, IMAGE_INDEX)[0]
    best_idx = torch.argmax(cos_scores).item()
    best_score = float(cos_scores[best_idx])

    if best_score > 0.65:  # عتبة تطابق واثقة
        return IMAGE_PATHS[best_idx], best_score
    return None, best_score

# ==========================================
# 3. محرك البحث عن رقم القطعة في الكتالوجات
# ==========================================
def search_part_number_in_all_manuals(raw_input):
    clean_input = raw_input.strip()
    core_match = re.search(r'(\d{3}\.\d{3}\.\d{2})', clean_input)
    core_number = core_match.group(1) if core_match else clean_input

    search_targets = {clean_input, core_number, f"D{core_number}", f"C{core_number}", f"H{core_number}", core_number.replace(".", "")}
    valid_targets = [re.escape(t) for t in search_targets if len(t) >= 5]
    search_regex = re.compile("|".join(valid_targets), re.IGNORECASE)

    all_pdfs = glob.glob(f"{DRIVE_FOLDER_PATH}/*.pdf")
    matches = []

    for pdf_path in all_pdfs:
        doc_name = os.path.basename(pdf_path)
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                if search_regex.search(text):
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    snippet = []
                    for i, line in enumerate(lines):
                        if search_regex.search(line):
                            snippet = lines[max(0, i - 1):min(len(lines), i + 4)]
                            break
                    matches.append({
                        "manual_file": doc_name,
                        "machine_name": get_clean_machine_name(pdf_path),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في جدول الأجزاء."
                    })
                    break
        except Exception:
            continue
    return clean_input, core_number, matches

# ==========================================
# 4. المعالج المركزي للبحث النصي والصوري
# ==========================================
def visual_maintenance_copilot(image_file, text_input):
    user_text = (text_input or "").strip()

    # أ. في حال قام الفني برفع أو التقاط صورة للقطعة
    if image_file is not None:
        matched_img_path, similarity_score = find_part_by_image(image_file)

        if matched_img_path:
            img_filename = os.path.basename(matched_img_path)
            # استخراج كود القطعة من اسم الملف المحفوظ
            extracted_code = img_filename.replace(".jpg", "").replace(".png", "").replace(".jpeg", "")

            clean_input, core_num, manual_matches = search_part_number_in_all_manuals(extracted_code)
            matched_pil = Image.open(matched_img_path)

            percent = int(similarity_score * 100)
            send_whatsapp_alert(f"مطابقة بصرية لصورة قطعة ({percent}%)", f"رقم القطعة: {extracted_code}")

            res = f"### 🎯 نتيجة التعرف البصري على القطعة\n\n"
            res += f"* **نسبة التطابق البصري:** `{percent}%`\n"
            res += f"* **رقم القطعة المعتمد:** `{extracted_code}`\n\n"
            res += "--- \n"
            res += "### 📖 بيانات الكتالوجات والماكينات المشتركة:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                    res += f"* ⚙️ **البيانات والمواصفات:** `{item['details']}`\n\n"
            else:
                res += "تم التعرف على شكل القطعة، ولكن لم يتم العثور على أرقامها بوضوح في ملفات الـ PDF المرفوعة حالياً.\n"

            return res, matched_pil
        else:
            return (
                f"⚠️ لم يتم العثور على تطابق كافٍ لشكل هذه القطعة في أرشيف الصور المحفوظة (نسبة الثقة: {int(similarity_score * 100)}%).\n"
                "يرجى تصوير القطعة بزاوية أوضح أو إدخال أي رقم مطبوع عليها في خانة البحث النصي.",
                None
            )

    # ب. في حال أدخل رقماً أو استفساراً نصياً
    if user_text:
        # البحث برقم القطعة
        full_input_code, core_num, found_records = search_part_number_in_all_manuals(user_text)

        # البحث عن صورة لها بالاسم
        found_img = None
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            for p in glob.glob(os.path.join(REAL_IMAGES_PATH, ext)):
                if core_num.lower() in os.path.basename(p).lower():
                    found_img = Image.open(p)
                    break

        if found_records:
            res = f"### 📦 تقرير مطابقة قطعة الغيار: `{full_input_code}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                res += f"* ⚙️ **البيانات والمواصفات:** `{item['details']}`\n\n"
            return res, found_img
        else:
            return f"⚠️ لم يتم العثور على تطابق للرقم `{full_input_code}`.", None

    return "يرجى التقاط صورة للقطعة أو إدخال رقمها في خانة البحث.", None

# ==========================================
# 5. الواجهة الرسومية الرسمية
# ==========================================
logo_data_url = get_logo_base64()
logo_html = f'<img src="{logo_data_url}" style="height: 75px; margin-left: 20px; border-radius: 8px; vertical-align: middle;">' if logo_data_url else ''

header_markdown = f"""
<div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid #2b6cb0; padding-bottom: 12px; margin-bottom: 20px;">
    <div style="display: flex; align-items: center;">
        {logo_html}
        <div>
            <h1 style="margin: 0; color: #1a365d; font-size: 26px;">منصة الدعم الهندسي في مسلخ شركة دواجن فلسطين</h1>
            <p style="margin: 5px 0 0 0; color: #4a5568; font-size: 15px;">نظام التعرف البصري الذكي على قطع الغيار الميكانيكية ومطابقة الكتالوجات (Meyn / Automac)</p>
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
    * **فحص القطعة بالصورة:** التقط صورة واضحة للقطعة الميكانيكية (أو ارفعها من الهاتف)، وسيتعرف النظام على شكلها، يستخرج رقمها المصنعي، ويحدد كافة الماكينات المشتركة ورقم الصفحة.
    * **البحث النصي البديل:** يمكنك أيضاً كتابة رقم القطعة مباشرة إذا كان متوفراً لديك.
    """)

    with gr.Row():
        with gr.Column(scale=1):
            cam_box = gr.Image(sources=["upload", "webcam"], type="pil", label="📷 تصوير القطعة أو رفع صورة من الهاتف")
            text_box = gr.Textbox(lines=1, label="📝 أو اكتب رقم القطعة مباشرة (اختياري)", placeholder="مثال: 0000.D475.000.94")
            search_btn = gr.Button("🔍 فحص ومطابقة القطعة بالكتالوجات", variant="primary")

        with gr.Column(scale=1):
            info_output = gr.Markdown(label="📋 التقرير الفني والمطابقة")
            matched_image_display = gr.Image(label="🖼️ القطعة المطابقة من أرشيف المستودع", visible=True)

    search_btn.click(
        fn=visual_maintenance_copilot,
        inputs=[cam_box, text_box],
        outputs=[info_output, matched_image_display]
    )

    gr.HTML(footer_markdown)

demo.launch(share=True)