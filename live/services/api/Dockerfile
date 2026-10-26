# NOT: BORONG-SEIS/HypoInverse'in `main` dalı artık modern gfortran (Debian 12/13,
# gfortran 12-14) ile temiz derlenmiyor — legacy fixed-form F77 (paylaşımlı DO
# sonlandırma vb.) hataları veriyor. hyp1.40'ı sıfırdan derlemek yerine, servisi
# hâlihazırda çalıştıran KANITLANMIŞ imajı temel alıp yalnızca uygulama katmanını
# (düzeltilmiş pyocto_api.py + güncel requirements) üzerine biniyoruz. Böylece
# çalışan hyp1.40 binary'si ve tüm runtime kütüphaneleri birebir korunur.
#
# Uzun vadeli temiz çözüm: HypoInverse'i derleyen bir commit'e pinlemek veya kaynağı
# `-std=legacy -ffixed-line-length-none` ile derleyecek şekilde vendor'lamak.
FROM europe-west3-docker.pkg.dev/quakemlabstreamlitwebproject/cloud-run-source-deploy/sismik-api@sha256:25c74354e88db1c0ea7eec91bf6ddb94a8408bc5003c9909243ced24b8607dd9

WORKDIR /app

# Güncel bağımlılıklar (pyocto>=0.1.10 dahil — offline tez ile aynı sürüm).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Düzeltilmiş API (DEEP hız modeli, UTM-36, max-pick event seçimi, minimal run.hyp).
COPY pyocto_api.py .

# Velocity modeli pyocto_api.py icinde ilk istekte olusturulacak.
EXPOSE 8080

CMD ["uvicorn", "pyocto_api:app", "--host", "0.0.0.0", "--port", "8080"]
