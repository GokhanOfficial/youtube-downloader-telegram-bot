# Python 3.10-slim tabanlı imajı kullan
FROM python:3.10-slim

# ffmpeg'i yükleyin
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Çalışma dizinini oluşturun
WORKDIR /app

# Gereksinim dosyasını kopyalayın ve paketleri yükleyin
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Uygulama dosyalarını kopyalayın
COPY . .

# Konteyner başlatıldığında bot.py dosyasını çalıştırın
CMD ["python", "bot.py"]
