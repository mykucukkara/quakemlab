FROM python:3.9-slim

WORKDIR /app

# Sistem bağımlılıklarını kur (obspy vb. derlemek için gerekebilir)
RUN apt-get update && apt-get install -y \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Bağımlılıkları kopyala ve kur
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Streamlit ayarları klasörünü oluştur
RUN mkdir -p ~/.streamlit/
# .NET iframe'inde görünebilmesi için CORS ve XSRF'yi kapatan ayar, temayı sabitle
RUN echo "\
[server]\n\
enableCORS = false\n\
enableXsrfProtection = false\n\
headless = true\n\
port = 8080\n\
\n\
[theme]\n\
base=\"light\"\n\
backgroundColor=\"#ffffff\"\n\
secondaryBackgroundColor=\"#f0f2f6\"\n\
textColor=\"#31333F\"\n\
" > ~/.streamlit/config.toml

# Uygulama kodunu kopyala
COPY . .

# Google Cloud Run varsayılan portu 8080'dir
EXPOSE 8080

# Streamlit uygulamasını başlat
CMD ["streamlit", "run", "app.py"]
