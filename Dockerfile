FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# تثبيت الاعتماديات الأساسية للنظام
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# نسخ وتثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع والكود
COPY . .

EXPOSE 8080

# تشغيل التطبيق
CMD ["python", "app.py"]
