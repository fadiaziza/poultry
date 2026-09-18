# استخدام بيئة بايثون رسمية وخفيفة
FROM python:3.10-slim

# منع بايثون من كتابة ملفات التخزين المؤقت وتفعيل الإخراج الفوري للسجلات
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تعيين المنفذ الافتراضي لـ Cloud Run
ENV PORT=8080

# تثبيت الحزم النظامية اللازمة لمعالجة الصور والمستندات
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل داخل الحاوية
WORKDIR /app

# نسخ ملف الاعتماديات وتثبيتها أولاً للاستفادة من الـ Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع والكود
COPY . .

# فتح المنفذ المطلوب
EXPOSE 8080

# تشغيل التطبيق عبر python مباشرة
CMD ["python", "app.py"]
