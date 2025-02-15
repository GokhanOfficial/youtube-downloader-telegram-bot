# Python 3.10-alpine tabanlı imajı kullan
FROM python:3.10-alpine

# ffmpeg ve curl'u yükleyin
RUN apk add --no-cache \
    ffmpeg \
    curl

# Çalışma dizinini oluşturun
WORKDIR /app

# Gereksinim dosyasını kopyalayın ve paketleri yükleyin
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt 

# Uygulama dosyalarını kopyalayın
# COPY . .

# Konteyner başlatıldığında bot.py dosyasını çalıştırın
CMD ["python", "bot.py"]
