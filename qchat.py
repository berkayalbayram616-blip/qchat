from flask import Flask, request, render_template_string, redirect, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
try:
    import tkinter as tk
    from tkinter import simpledialog
except Exception:
    tk = None
    simpledialog = None
import queue
import threading
import secrets
import time
import subprocess
import shutil
import json
import os
import sys
import logging
try:
    import keyboard
except Exception:
    keyboard = None
import re
from datetime import timedelta
from email.message import EmailMessage
import smtplib

logging.getLogger('werkzeug').setLevel(logging.ERROR)

DOSYA = "veriler.json"

def verileri_yukle():
    if os.path.exists(DOSYA):
        try:
            with open(DOSYA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def verileri_kaydet(veriler):
    with open(DOSYA, "w", encoding="utf-8") as f:
        json.dump(veriler, f, ensure_ascii=False, indent=2)

veriler = verileri_yukle()

try:
    _ngrok_yolu = "C:\\Users\\berka\\AppData\\Local\\Microsoft\\WindowsApps\\ngrok.exe"
    if not os.path.exists(_ngrok_yolu):
        _ngrok_yolu = shutil.which("ngrok") or _ngrok_yolu  # PATH üzerinde ngrok varsa onu kullan
    ngrok_process = subprocess.Popen(
        [_ngrok_yolu, "http", "5000"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
except Exception as e:
    print(f"⚠️ Ngrok başlatılamadı: {e}")

time.sleep(1)
try:
    import winsound
except ImportError:
    winsound = None

app = Flask(__name__)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_SESSION_COOKIE_SECURE', '0') == '1'
app.permanent_session_lifetime = timedelta(days=30)

app.secret_key = secrets.token_hex(32)

def _ban_geri_bildirim_sayfasi(durum_mesaji="", gonderildi=False, status=200):
    return render_template_string(
        ban_html,
        durum_mesaji=durum_mesaji,
        gonderildi=gonderildi
    ), status

@app.errorhandler(Exception)
def _tum_hatalari_yakala(e):
    """
    Herhangi bir /api/ isteğinde beklenmeyen bir hata (Exception) oluşursa,
    Flask varsayılan olarak HTML bir hata sayfası döndürür
    (örn. "<!doctype html>...Internal Server Error...").
    Bu da tarayıcı tarafında `r.json()` çağrısını
    "SyntaxError: Unexpected token '<' ... is not valid JSON" hatasıyla patlatır.
    Bu handler, /api/ altındaki tüm hataları düzgün bir JSON gövdesine çevirir.
    """
    import traceback
    traceback.print_exc()  # gerçek hatayı konsola/log dosyasına yazdır (teşhis için)
    kod = getattr(e, "code", 500) or 500
    if request.path.startswith("/api/"):
        return jsonify({"basarili": False, "hata": f"Sunucu hatası: {e}"}), kod
    if request.path == "/ban-geri-bildirim":
        return _ban_geri_bildirim_sayfasi(
            "Geri bildirimin alındı.",
            gonderildi=True,
            status=200,
        )
    return f"<h2 style='font-family:sans-serif'>Sunucu hatası ({kod})</h2>", kod

veri_kilidi = threading.RLock()  # sohbet_gecmisi / odalar_db / oda_* gibi paylaşılan verileri korur

mesaj_kuyrugu = queue.Queue()
izin_istek_kuyrugu = queue.Queue()  # Sistem'e gönderilecek "oda kurma izni" istekleri
oda_izin_istekleri = {}  # {kullanici_adi: istek_zamani} - cevap bekleyen istekler
yaziyor_durumu = {}
son_aktiflik = {}
son_mesaj_zamani = {}
kullanici_renames = {}
sistem_loglari = []
geri_bildirimler = []
kullanici_kayit_zamani = veriler.get("kullanici_kayit_zamani", {})
kullanici_oturum_toplam_saniye = veriler.get("kullanici_oturum_toplam_saniye", {})
kullanici_oturum_son_kayit = {}

# ==================== ŞİKAYET SİSTEMİ ====================
# Şikayetler veriler.json içine yazılmaz. Ayrı bir dosyada tutulur.
SIKAYET_DOSYA = "sikayetler.json"
SIKAYET_NEDENLERI = [
    "Küfür",
    "Taciz",
    "Uygunsuz davranış",
    "Spam",
    "Hakaret",
    "Tehdit / güvenlik",
    "Diğer"
]
SIKAYET_MAKS_30DK = 5
SIKAYET_AYNI_KISI_COOLDOWN = 60

def sikayetleri_yukle():
    if os.path.exists(SIKAYET_DOSYA):
        try:
            with open(SIKAYET_DOSYA, "r", encoding="utf-8") as f:
                veri = json.load(f)
                return veri if isinstance(veri, list) else []
        except Exception:
            return []
    return []

def sikayetleri_kaydet(liste):
    tmp = SIKAYET_DOSYA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SIKAYET_DOSYA)

sikayetler = sikayetleri_yukle()
sikayet_kilidi = threading.Lock()

_son_sistem_acilis = 0
bakim_modu = False
yavas_mod_saniye = 0
kufur_filtresi = False
sabit_duyuru = ""
zorla_cikis = set()

# ==================== GİRİŞ BRUTE-FORCE KORUMASI ====================
GIRIS_MAKS_DENEME = 5          # bu sayıda hatalı şifreden sonra hesap kilitlenir
GIRIS_KILIT_SANIYE = 30 * 60   # kilit süresi: 30 dakika
GIRIS_DENEME_SIFIRLAMA_SANIYE = 15 * 60  # bu süre boyunca hiç deneme yoksa sayaç sıfırlanır

giris_hatali_deneme = {}  # {kullanici_adi: {"sayi": int, "son_deneme": zaman}}
giris_kilitli = {}        # {kullanici_adi: kilit_bitis_zamani}

def giris_kilidini_kontrol_et(kullanici):
    """Kullanıcı kilitliyse kalan süreyi (saniye) döndürür, değilse None döndürür."""
    bitis = giris_kilitli.get(kullanici)
    if not bitis:
        return None
    kalan = bitis - time.time()
    if kalan <= 0:
        giris_kilitli.pop(kullanici, None)
        giris_hatali_deneme.pop(kullanici, None)
        return None
    return kalan

def giris_hatali_deneme_kaydet(kullanici):
    """Hatalı şifre denemesini kaydeder; limit aşılırsa hesabı kilitler."""
    simdi = time.time()
    kayit = giris_hatali_deneme.get(kullanici)
    if not kayit or simdi - kayit["son_deneme"] > GIRIS_DENEME_SIFIRLAMA_SANIYE:
        kayit = {"sayi": 0, "son_deneme": simdi}
    kayit["sayi"] += 1
    kayit["son_deneme"] = simdi
    giris_hatali_deneme[kullanici] = kayit

    if kayit["sayi"] >= GIRIS_MAKS_DENEME:
        giris_kilitli[kullanici] = simdi + GIRIS_KILIT_SANIYE
        giris_hatali_deneme.pop(kullanici, None)
        log_ekle(f"'{kullanici}' hesabı {GIRIS_MAKS_DENEME} hatalı denemeden sonra 30 dakika kilitlendi.")
        return True
    return False

def giris_denemesini_temizle(kullanici):
    giris_hatali_deneme.pop(kullanici, None)
    giris_kilitli.pop(kullanici, None)

YASAKLI_KELIMELER = [
    "amk", "aq", "sik", "siktir", "piç", "pic", "orospu", "yarrak", "yarak",
    "ipne", "ibne", "göt", "got", "gerizekalı", "gerizekali", "mal herif",
    "puşt", "pust", "kahpe", "şerefsiz", "serefsiz", "oç", "oc"
]

_LEET_CEVIRI = str.maketrans({
    "4": "a", "@": "a", "3": "e", "1": "i", "!": "i",
    "0": "o", "5": "s", "$": "s", "7": "t", "+": "t"
})

def _filtre_icin_sikistir(metin):
    sikisik = []
    index_haritasi = []
    for i, ch in enumerate(metin):
        kucuk = ch.lower().translate(_LEET_CEVIRI)
        if kucuk.isalpha():
            sikisik.append(kucuk)
            index_haritasi.append(i)
    return "".join(sikisik), index_haritasi

def kufur_filtrele(metin):
    """Boşluk, nokta, yıldız gibi ayraçlarla veya leetspeak (4mk, s1k vb.) ile
    yazılan küfürleri de yakalayıp yalnızca ilgili harfleri yıldızlar; mesajın
    geri kalanının büyük/küçük harfini bozmaz."""
    sikisik, index_haritasi = _filtre_icin_sikistir(metin)
    if not sikisik:
        return metin

    mesaj_listesi = list(metin)
    bulundu = False
    for kelime in YASAKLI_KELIMELER:
        kw = kelime.lower().translate(_LEET_CEVIRI)
        if not kw:
            continue
        baslangic = 0
        while True:
            pos = sikisik.find(kw, baslangic)
            if pos == -1:
                break
            bulundu = True
            orig_baslangic = index_haritasi[pos]
            orig_bitis = index_haritasi[pos + len(kw) - 1]
            for j in range(orig_baslangic, orig_bitis + 1):
                mesaj_listesi[j] = "*"
            baslangic = pos + len(kw)

    return "".join(mesaj_listesi) if bulundu else metin

def log_ekle(metin):
    zaman = time.strftime("%H:%M:%S")
    sistem_loglari.append(f"[{zaman}] {metin}")
    if len(sistem_loglari) > 100:
        sistem_loglari.pop(0)

def sifre_hashle(sifre):
    return generate_password_hash(sifre)

def sifre_dogrula(kullanici_adi, girilen_sifre):
    """
    Kayıtlı şifreyi doğrular. Hem yeni (hash'lenmiş) hem de eski (düz metin,
    bu güncellemeden önce kaydedilmiş) hesaplarla uyumludur: eski bir hesap
    başarıyla giriş yaparsa şifresi otomatik olarak hash'e yükseltilir.
    """
    kayitli = kullanici_db.get(kullanici_adi, "")
    if not kayitli:
        return False
    try:
        if check_password_hash(kayitli, girilen_sifre):
            return True
    except Exception:
        pass
    # Hash değilse (eski düz metin kayıt), doğrudan karşılaştır ve hash'e yükselt
    if kayitli == girilen_sifre:
        kullanici_db[kullanici_adi] = sifre_hashle(girilen_sifre)
        return True
    return False

_OZEL_KARAKTER_REGEX = re.compile(r"[!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|`~]")

def sifre_guclu_mu(sifre):
    if len(sifre) < 7 or len(sifre) > 15:
        return False, "❌ Şifre 7 ile 15 karakter arasında olmalı."
    if not re.search(r"[A-ZÇĞİÖŞÜ]", sifre):
        return False, "❌ Şifre en az 1 büyük harf içermeli."
    if not _OZEL_KARAKTER_REGEX.search(sifre):
        return False, "❌ Şifre en az 1 özel karakter içermeli (!@#$% vb.)."
    return True, None

def e_posta_normalize(email):
    return (email or "").strip().lower()

def e_posta_gecerli_mi(email):
    email = e_posta_normalize(email)
    return bool(re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email))

def dogrulama_kodu_uret():
    return f"{secrets.randbelow(1_000_000):06d}"

def kullanici_emaili_al(kullanici):
    return e_posta_normalize(kullanici_emailleri.get(kullanici, ""))

def kullanici_adi_bul(kimlik):
    kimlik = e_posta_normalize(kimlik)
    if not kimlik:
        return None
    if kimlik in kullanici_db:
        return kimlik
    if kimlik in kullanici_renames:
        return kullanici_renames[kimlik]
    return None

def email_baska_hesapta_var_mi(email, mevcut_kullanici=None):
    email = e_posta_normalize(email)
    sahib = email_hesaplari.get(email)
    return bool(sahib and sahib != mevcut_kullanici)

def kullanici_emailini_kaydet(kullanici, email):
    email = e_posta_normalize(email)
    eski = kullanici_emailleri.get(kullanici)
    if eski:
        eski = e_posta_normalize(eski)
        if email_hesaplari.get(eski) == kullanici:
            email_hesaplari.pop(eski, None)
    kullanici_emailleri[kullanici] = email
    email_hesaplari[email] = kullanici

def kullanici_banla_ve_email(kullanici):
    if not kullanici:
        return
    engellenenler.add(kullanici)
    email = kullanici_emaili_al(kullanici)
    if email:
        banli_emailler.add(email)

def kullanici_banini_ac(kullanici):
    if not kullanici:
        return
    engellenenler.discard(kullanici)
    email = kullanici_emaili_al(kullanici)
    if email:
        banli_emailler.discard(email)

def eposta_kodu_gonder(hedef_email, konu, icerik):
    smtp_host = os.getenv("QCHAT_SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("QCHAT_SMTP_PORT", "587"))
    smtp_kullanici = os.getenv("QCHAT_SMTP_USER", "").strip()
    smtp_sifre = os.getenv("QCHAT_SMTP_PASS", "").strip()
    gonderen = os.getenv("QCHAT_SMTP_FROM", smtp_kullanici).strip() or smtp_kullanici

    if not smtp_host or not smtp_kullanici or not smtp_sifre:
        raise RuntimeError("SMTP ayarları eksik. QCHAT_SMTP_HOST, QCHAT_SMTP_USER ve QCHAT_SMTP_PASS gerekli.")

    msg = EmailMessage()
    msg["Subject"] = konu
    msg["From"] = gonderen
    msg["To"] = hedef_email
    msg.set_content(icerik)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        server.ehlo()
        if os.getenv("QCHAT_SMTP_TLS", "1") == "1":
            server.starttls()
            server.ehlo()
        server.login(smtp_kullanici, smtp_sifre)
        server.send_message(msg)

def eposta_dogrulama_kodu_gonder(hedef_email, kod, kullanici):
    icerik = (
        f"Merhaba {kullanici},\n\n"
        f"QChat doğrulama kodun: {kod}\n\n"
        "Bu kodu kimseyle paylaşma. Kod 10 dakika geçerlidir.\n"
    )
    eposta_kodu_gonder(hedef_email, "QChat Doğrulama Kodu", icerik)

def eposta_sifre_sifirlama_kodu_gonder(hedef_email, kod, kullanici):
    icerik = (
        f"Merhaba {kullanici},\n\n"
        f"QChat şifre sıfırlama kodun: {kod}\n\n"
        "Bu kodu kimseyle paylaşma. Kod 10 dakika geçerlidir.\n"
    )
    eposta_kodu_gonder(hedef_email, "QChat Şifre Sıfırlama Kodu", icerik)

sohbet_gecmisi = veriler.get("sohbet_gecmisi", [])
engellenenler = set(veriler.get("engellenenler", []))
susturulanlar = veriler.get("susturulanlar", {})
geri_bildirimler = veriler.get("geri_bildirimler", [])
kullanici_db = veriler.get("kullanici_db", {})
odalar_db = veriler.get("odalar_db", {"Genel": ""}) 

kullanici_emailleri = veriler.get("kullanici_emailleri", {})
email_hesaplari = veriler.get("email_hesaplari", {})
banli_emailler = set(veriler.get("banli_emailler", []))
bekleyen_kayitlar = {}  # {token: {kullanici, email, sifre_hash, kod, olusturma_zamani}}
bekleyen_sifre_sifirlama = {}  # {token: {kullanici, email, kimlik, kod, olusturma_zamani}}

EMAIL_DOGRULAMA_SANIYE = 10 * 60
SIFRE_SIFIRLAMA_SANIYE = 10 * 60

# ==================== ODA LİDERLİK / ROL / YETKİ SİSTEMİ ====================
MAKS_OZEL_ODA = 3  # "Genel" dışında toplamda oluşturulabilecek maksimum oda sayısı

oda_liderleri = veriler.get("oda_liderleri", {})          # {oda_adi: lider_kullanici_adi}
oda_roller = veriler.get("oda_roller", {})                # {oda_adi: {kullanici_adi: "yonetici"}}
oda_yasaklari = veriler.get("oda_yasaklari", {})           # {oda_adi: [kullanici_adi, ...]}  (kalıcı banlar)
oda_gecici_banlar = veriler.get("oda_gecici_banlar", {})   # {oda_adi: {kullanici_adi: bitis_zamani}}  (süreli banlar)
oda_kurma_izni = set(veriler.get("oda_kurma_izni", []))    # oda kurma yetkisi verilmiş kullanıcılar

if "Genel" not in oda_liderleri:
    oda_liderleri["Genel"] = "Sistem"

def oda_rolunu_al(oda, kullanici):
    """Kullanıcının bir odadaki rolünü döndürür: 'lider', 'yonetici' veya 'uye'."""
    if not kullanici:
        return "uye"
    if oda_liderleri.get(oda) == kullanici:
        return "lider"
    return oda_roller.get(oda, {}).get(kullanici, "uye")

def oda_yonetebilir_mi(oda, kullanici):
    """Kullanıcı bu odayı yönetebilir mi (lider, liderin yetki verdiği yönetici veya sistem Sistem'i)?"""
    if kullanici == "Sistem":
        return True
    return oda_rolunu_al(oda, kullanici) in ("lider", "yonetici")

def oda_lideri_mi(oda, kullanici):
    return kullanici == "Sistem" or oda_liderleri.get(oda) == kullanici

def oda_kurma_yetkisi_var_mi(kullanici):
    return kullanici == "Sistem" or kullanici in oda_kurma_izni

def oda_banli_mi(oda, kullanici):
    if kullanici in oda_yasaklari.get(oda, []):
        return True
    bitis = oda_gecici_banlar.get(oda, {}).get(kullanici)
    if bitis and time.time() < bitis:
        return True
    if bitis:
        # süresi dolmuş, kaydı temizle
        oda_gecici_banlar.get(oda, {}).pop(kullanici, None)
    return False

def oda_gecici_ban_kalan_dk(oda, kullanici):
    """Kullanıcının bu odadaki süreli banından kalan dakika (yoksa None)."""
    bitis = oda_gecici_banlar.get(oda, {}).get(kullanici)
    if not bitis:
        return None
    kalan = bitis - time.time()
    if kalan <= 0:
        return None
    return max(1, int(kalan // 60) + 1)

def oda_olustur_kaydi(oda_adi, kurucu):
    """Yeni kurulan bir odaya lider/rol kaydı açar (kurucu = odanın lideri)."""
    oda_liderleri[oda_adi] = kurucu
    oda_roller[oda_adi] = {}
    oda_yasaklari[oda_adi] = []
    oda_gecici_banlar[oda_adi] = {}

def oda_kaydini_sil(oda_adi):
    oda_liderleri.pop(oda_adi, None)
    oda_roller.pop(oda_adi, None)
    oda_yasaklari.pop(oda_adi, None)
    oda_gecici_banlar.pop(oda_adi, None)

def oda_kaydini_tasi(eski_oda, yeni_oda):
    if eski_oda in oda_liderleri:
        oda_liderleri[yeni_oda] = oda_liderleri.pop(eski_oda)
    if eski_oda in oda_roller:
        oda_roller[yeni_oda] = oda_roller.pop(eski_oda)
    if eski_oda in oda_yasaklari:
        oda_yasaklari[yeni_oda] = oda_yasaklari.pop(eski_oda)
    if eski_oda in oda_gecici_banlar:
        oda_gecici_banlar[yeni_oda] = oda_gecici_banlar.pop(eski_oda)

def oturum_suresini_guncelle(kullanici, simdi=None):
    """Kullanıcının aktif oturum süresini toplam süreye işler."""
    if not kullanici:
        return
    if simdi is None:
        simdi = time.time()
    with veri_kilidi:
        onceki = kullanici_oturum_son_kayit.get(kullanici)
        if onceki is not None and simdi >= onceki:
            kullanici_oturum_toplam_saniye[kullanici] = int(kullanici_oturum_toplam_saniye.get(kullanici, 0) + (simdi - onceki))
        kullanici_oturum_son_kayit[kullanici] = simdi

def kullanici_oturum_toplamini_al(kullanici, simdi=None):
    """Toplam oturum süresini (aktif oturum dahil) saniye olarak döndürür."""
    if not kullanici:
        return 0
    if simdi is None:
        simdi = time.time()
    with veri_kilidi:
        toplam = int(kullanici_oturum_toplam_saniye.get(kullanici, 0))
        son = kullanici_oturum_son_kayit.get(kullanici)
        if son is not None and simdi >= son and session.get("kullanici") == kullanici:
            toplam += int(simdi - son)
        return max(0, toplam)

def kullanici_kredi_hesapla(kullanici, simdi=None):
    if not kullanici:
        return 0
    if simdi is None:
        simdi = time.time()
    saniye = kullanici_oturum_toplamini_al(kullanici, simdi)
    return max(0, int(saniye // 60) * 20)

def kullanici_bilgi_hazirla(kullanici):
    simdi = time.time()
    mesaj_sayisi = sum(1 for m in sohbet_gecmisi if m.get("gonderen") == kullanici)
    kayit_ts = kullanici_kayit_zamani.get(kullanici)
    return {
        "isim": kullanici,
        "email": kullanici_emailleri.get(kullanici),
        "kayit_zamani": kayit_ts,
        "kayit_tarihi": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(kayit_ts)) if kayit_ts else None,
        "mesaj_sayisi": mesaj_sayisi,
        "oturum_suresi_saniye": kullanici_oturum_toplamini_al(kullanici, simdi),
        "kredi": kullanici_kredi_hesapla(kullanici, simdi),
        "online": bool(son_aktiflik.get(kullanici) and simdi - son_aktiflik.get(kullanici, 0) < 10),
    }

# ============================================================================

MAKS_MESAJ_GECMISI = 500  # sohbet_gecmisi'nin sınırsız büyüyüp RAM/disk şişirmesini engeller

def durumu_kaydet():
    # Kilit altında, diğer thread'ler dict/listeleri değiştirirken json.dump'ın
    # "dictionary changed size during iteration" gibi hatalarla çökmesini engeller.
    with veri_kilidi:
        if len(sohbet_gecmisi) > MAKS_MESAJ_GECMISI:
            del sohbet_gecmisi[:-MAKS_MESAJ_GECMISI]
        guncel_veriler = {
            "sohbet_gecmisi": list(sohbet_gecmisi),
            "engellenenler": list(engellenenler),
            "susturulanlar": dict(susturulanlar),
            "kullanici_db": dict(kullanici_db),
            "kullanici_emailleri": dict(kullanici_emailleri),
            "email_hesaplari": dict(email_hesaplari),
            "banli_emailler": list(banli_emailler),
            "odalar_db": dict(odalar_db),
            "oda_liderleri": dict(oda_liderleri),
            "oda_roller": {k: dict(v) for k, v in oda_roller.items()},
            "oda_yasaklari": {k: list(v) for k, v in oda_yasaklari.items()},
            "oda_gecici_banlar": {k: dict(v) for k, v in oda_gecici_banlar.items()},
            "oda_kurma_izni": list(oda_kurma_izni),
            "geri_bildirimler": list(geri_bildirimler),
            "kullanici_kayit_zamani": dict(kullanici_kayit_zamani),
            "kullanici_oturum_toplam_saniye": dict(kullanici_oturum_toplam_saniye)
        }
    verileri_kaydet(guncel_veriler)

def otomatik_kayit():
    while True:
        durumu_kaydet()
        time.sleep(3)

bakim_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bakım Modu</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, sans-serif;
            background: linear-gradient(180deg, #bcd6ee 0%, #d8e6f5 45%, #eaf2fa 100%);
            min-height: 100vh; margin: 0; display: flex;
            align-items: center; justify-content: center; padding: 20px;
        }
        .win7-window {
            background: #f4f8fc; border: 1px solid #5b8ac4; border-radius: 8px;
            box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            width: 100%; max-width: 420px; overflow: hidden;
        }
        .win7-titlebar {
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 45%, #1c5fb0 55%, #2d7fd6 100%);
            color: #fff; font-weight: 600; font-size: 13px; padding: 10px 14px;
            text-shadow: 0 1px 1px rgba(0,0,0,.4); border-bottom: 1px solid #14417f;
        }
        .win7-content { padding: 30px 24px; text-align: center; }
        .win7-content h2 { margin: 0 0 10px; color: #1c3d5c; font-size: 19px; }
        .win7-content p { color: #33475c; font-size: 14px; line-height: 1.6; margin: 0; }
    </style>
</head>
<body>
    <div class="win7-window">
        <div class="win7-titlebar">💬 Sohbet Sistemi — Bakım Modu</div>
        <div class="win7-content">
            <h2>🛠️ Site Bakım Modunda</h2>
            <p>Sistemde şu anda bakım çalışması yapılmaktadır.<br>Lütfen daha sonra tekrar deneyin.</p>
        </div>
    </div>
</body>
</html>
"""

@app.before_request
def guvenlik_kontrolu():
    global bakim_modu
    kullanici = session.get("kullanici")

    if kullanici and kullanici in kullanici_renames:
        session["kullanici"] = kullanici_renames[kullanici]
        kullanici = session["kullanici"]

    if kullanici:
        oturum_suresini_guncelle(kullanici)

    if kullanici and kullanici in engellenenler:
        if request.path == "/ban-geri-bildirim":
            return None
        return render_template_string(ban_html, durum_mesaji=""), 403

    if kullanici and kullanici in zorla_cikis:
        zorla_cikis.discard(kullanici)
        session.pop("kullanici", None)
        if request.path.startswith("/api/"):
            return "Kick", 403
        return redirect("/giris")

    if bakim_modu and kullanici != "Sistem":
        if request.path.startswith("/api/"):
            return "Bakım", 403
        if request.path == "/":
            return render_template_string(bakim_html)

ban_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Engellendiniz</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, sans-serif;
            background: linear-gradient(180deg, #bcd6ee 0%, #d8e6f5 45%, #eaf2fa 100%);
            min-height: 100vh; margin: 0; display: flex;
            align-items: center; justify-content: center; padding: 20px;
        }
        .win7-window {
            background: #f4f8fc; border: 1px solid #5b8ac4; border-radius: 8px;
            box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            width: 100%; max-width: 460px; overflow: hidden;
        }
        .win7-titlebar {
            background: linear-gradient(180deg,#79bdf7 0%,#3d8ede 100%);
            color: #fff; font-weight: 700; padding: 10px 14px; font-size: 15px;
        }
        .win7-content { padding: 18px; color: #223; }
        h2 { margin: 0 0 10px; color: #b91c1c; }
        p { margin: 0 0 14px; line-height: 1.5; }
        .btn {
            appearance: none; border: 1px solid #1d4ed8; background: #2563eb; color: white;
            border-radius: 6px; padding: 9px 12px; font-weight: 700; cursor: pointer;
        }
        .panel {
            margin-top: 12px; display: none; border: 1px solid #c9d7ea; background: #fff;
            border-radius: 8px; padding: 12px;
        }
        textarea {
            width: 100%; min-height: 110px; resize: vertical; border: 1px solid #c9d7ea;
            border-radius: 6px; padding: 10px; font: inherit; margin-top: 8px;
        }
        .row { display: flex; gap: 8px; margin-top: 10px; }
        .secondary { background: #94a3b8; border-color: #64748b; }
        .note {
            margin-top: 10px; font-size: 13px; color: #475569;
        }
        .status {
            margin-top: 10px; padding: 10px 12px; border-radius: 6px;
            background: #ecfeff; border: 1px solid #67e8f9; color: #155e75;
        }
    </style>
</head>
<body>
    <div class="win7-window">
        <div class="win7-titlebar">🚫 Engellendiniz</div>
        <div class="win7-content">
            <h2>🚫 Sistem yöneticisi tarafından engellendiniz.</h2>
            <p>Yaşanan durumu aşağıya yazabilirsin. Gönderdiğin not yönetim panelinde <b>Geri Bildirimler</b> bölümünde görünecek.</p>

            {% if durum_mesaji %}
            <div class="status">{{ durum_mesaji }}</div>
            {% endif %}

            {% if not gonderildi %}
            <form class="panel" method="post" action="/ban-geri-bildirim" style="display:block;">
                <label for="mesaj" style="font-weight:700; color:#334155;">Neler oldu?</label>
                <textarea id="mesaj" name="mesaj" placeholder="Örneğin: 'Yanlışlıkla engellendiğimi düşünüyorum.' veya 'Kuralların hangi kısmını ihlal ettiğimi öğrenmek istiyorum.'"></textarea>
                <div class="row">
                    <button class="btn" type="submit">Gönder</button>
                </div>
                <div class="note">Bu kutu, ne olduğunu açıklaman için bırakıldı.</div>
            </form>
            {% else %}
            <div class="status">Geri bildirimin alındı.</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

@app.route("/ban-geri-bildirim", methods=["GET", "POST"])
def ban_geri_bildirim_gonder():
    kullanici = session.get("kullanici", "Bilinmeyen")
    if kullanici not in engellenenler:
        return redirect("/")

    gonderildi = bool(session.get("ban_geri_bildirim_gonderildi"))

    if request.method == "GET":
        return _ban_geri_bildirim_sayfasi("", gonderildi=gonderildi, status=200)

    if gonderildi:
        return _ban_geri_bildirim_sayfasi("Geri bildirimin alındı.", gonderildi=True, status=200)

    mesaj = request.form.get("mesaj", "").strip()
    if mesaj:
        try:
            with veri_kilidi:
                geri_bildirimler.append({
                    "kullanici": kullanici,
                    "oda": "Ban Ekranı",
                    "mesaj": mesaj,
                    "zaman": time.time()
                })
                durumu_kaydet()
            log_ekle(f"'{kullanici}' ban ekranından geri bildirim gönderdi.")
        except Exception as e:
            log_ekle(f"Ban geri bildirim kaydı sırasında hata: {e}")

    session["ban_geri_bildirim_gonderildi"] = True
    return _ban_geri_bildirim_sayfasi("Geri bildirimin alındı.", gonderildi=True, status=200)

giris_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Giriş</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, sans-serif;
            background: linear-gradient(180deg, #bcd6ee 0%, #d8e6f5 45%, #eaf2fa 100%);
            min-height: 100vh; margin: 0; display: flex;
            align-items: center; justify-content: center; padding: 20px;
        }
        .win7-window {
            background: #f4f8fc; border: 1px solid #5b8ac4; border-radius: 8px;
            box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            width: 100%; max-width: 380px; overflow: hidden;
        }
        .win7-titlebar {
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 45%, #1c5fb0 55%, #2d7fd6 100%);
            color: #fff; font-weight: 600; font-size: 13px; padding: 10px 14px;
            text-shadow: 0 1px 1px rgba(0,0,0,.4); border-bottom: 1px solid #14417f;
            display: flex; align-items: center; gap: 6px;
        }
        .win7-content { padding: 24px 22px; }
        .win7-content h3 { text-align: center; margin: 0 0 18px; color: #1c3d5c; font-size: 16px; }
        .field-label { font-size: 12px; font-weight: 600; color: #3a5a7a; margin: 0 0 4px 2px; display: block; }
        input {
            width: 100%; padding: 9px 10px; font-size: 14px; font-family: inherit;
            border: 1px solid #8fa9c4; border-radius: 4px; margin-bottom: 14px; outline: none;
            box-shadow: inset 0 1px 3px rgba(0,0,0,.12); background: #fff; color: #1c2b3a;
            transition: border-color .15s, box-shadow .15s;
        }
        input:focus { border-color: #3a8ee6; box-shadow: 0 0 0 3px rgba(58,142,230,.25) inset, inset 0 1px 3px rgba(0,0,0,.08); }
        button {
            width: 100%; padding: 10px; font-size: 14px; font-weight: 600; font-family: inherit;
            border: 1px solid #1c5fb0; border-radius: 4px; cursor: pointer;
            color: #fff; text-shadow: 0 1px 1px rgba(0,0,0,.25);
            background: linear-gradient(180deg, #79bdf7 0%, #3a8ee6 48%, #1f74d6 52%, #1c5fb0 100%);
            box-shadow: 0 1px 0 rgba(255,255,255,.5) inset;
        }
        button:hover { background: linear-gradient(180deg, #8ec8fb 0%, #4a99ec 48%, #2c80de 52%, #2468bd 100%); }
        button:active { background: linear-gradient(180deg, #2c80de 0%, #1c5fb0 100%); box-shadow: inset 0 2px 4px rgba(0,0,0,.25); }
        .error {
            color: #a4141a; font-weight: 600; font-size: 12.5px; margin: 0 0 14px;
            text-align: center; background: #fdeaea; border: 1px solid #e6a6a9; border-radius: 4px; padding: 6px 8px;
        }
        .sifre-ipucu {
            font-size: 11px; color: #5a7a9a; margin: -10px 0 14px 2px;
        }
        .bot-dogrulama {
            display: flex; align-items: center; gap: 8px; background: #eef4fb;
            border: 1px solid #b9cfe6; border-radius: 4px; padding: 10px 12px;
            margin: 0 0 16px; font-size: 13px; color: #2c4a68;
        }
        .bot-dogrulama input[type="checkbox"] {
            width: 18px; height: 18px; margin: 0; flex-shrink: 0;
            box-shadow: none; accent-color: #1c5fb0; cursor: pointer;
        }
        .bot-dogrulama label { cursor: pointer; user-select: none; }
    </style>
</head>
<body>
    <div class="win7-window">
        <div class="win7-titlebar"><span>🔐</span><span>Sohbet Sistemi — Giriş / Kayıt</span></div>
        <div class="win7-content">
            <h3>Hesabınıza Giriş Yapın</h3>
            {% if hata %}<div class="error">⚠️ {{ hata }}</div>{% endif %}
            <form method="POST">
                <span class="field-label">Kullanıcı Adı</span>
                <input type="text" name="kullanici" placeholder="Kullanıcı adınızı girin" maxlength="15" value="{{ kullanici|default('') }}" required autocomplete="off">
                <span class="field-label">Şifre</span>
                <input type="password" name="sifre" placeholder="Şifrenizi girin" minlength="7" maxlength="15" required autocomplete="off">
                {% if kod_gerekli %}
                <span class="field-label">Doğrulama Kodu</span>
                <input type="text" name="dogrulama_kodu" placeholder="E-postaya gelen 6 haneli kod" inputmode="numeric" maxlength="6" pattern="\\d{6}" autocomplete="off">
                {% endif %}
                <div class="sifre-ipucu">Yeni hesap için: 7-15 karakter, en az 1 büyük harf ve 1 özel karakter (!@#$% vb.)</div>
                {% if onay_mesaji %}<div class="error" style="color:#1d5d2a;background:#e7f7ea;border-color:#b9e3c1;">✅ {{ onay_mesaji }}</div>{% endif %}
                <div class="bot-dogrulama">
                    <input type="checkbox" id="bot_dogrulama" name="bot_dogrulama" required>
                    <label for="bot_dogrulama">🤖 Robot değilim</label>
                </div>
                <div class="bot-dogrulama" style="margin-top:-6px;">
                    <input type="checkbox" id="beni_hatirla" name="beni_hatirla" checked>
                    <label for="beni_hatirla">🔒 Beni hatırla</label>
                </div>
                <button type="submit">GİRİŞ YAP / KAYDOL</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

sifre_unuttum_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Şifremi Unuttum</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, sans-serif;
            background: linear-gradient(180deg, #bcd6ee 0%, #d8e6f5 45%, #eaf2fa 100%);
            min-height: 100vh; margin: 0; display: flex;
            align-items: center; justify-content: center; padding: 20px;
        }
        .win7-window {
            background: #f4f8fc; border: 1px solid #5b8ac4; border-radius: 8px;
            box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            width: 100%; max-width: 380px; overflow: hidden;
        }
        .win7-titlebar {
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 45%, #1c5fb0 55%, #2d7fd6 100%);
            color: #fff; font-weight: 600; font-size: 13px; padding: 10px 14px;
            text-shadow: 0 1px 1px rgba(0,0,0,.4); border-bottom: 1px solid #14417f;
        }
        .win7-content { padding: 18px 16px 16px; }
        h3 { margin: 0 0 12px; color: #1c3d5c; text-align: center; }
        .error {
            background: #fff4f4; border: 1px solid #d9a6a6; color: #9b1c1c;
            padding: 10px 12px; border-radius: 4px; margin-bottom: 10px; font-size: 13px;
            line-height: 1.35;
        }
        .field-label {
            display:block; margin: 8px 0 4px; color:#2a4a68; font-size:12px; font-weight:700;
        }
        input[type="text"], input[type="password"], input[type="email"] {
            width:100%; padding: 8px 10px; border:1px solid #8fa9c4; border-radius:4px;
            outline:none; font-family:inherit; font-size:13px; background:#fff;
        }
        .sifre-ipucu { margin-top: 8px; color:#53718f; font-size:11.5px; line-height:1.35; }
        button {
            width:100%; margin-top: 10px; padding: 10px 12px; border:1px solid #1c5fb0;
            border-radius:4px; color:#fff; cursor:pointer; font-weight:700; font-family:inherit;
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 48%, #1f74d6 52%, #1c5fb0 100%);
            text-shadow: 0 1px 1px rgba(0,0,0,.25);
        }
        button:hover { filter: brightness(1.03); }
    </style>
</head>
<body>
    <div class="win7-window">
        <div class="win7-titlebar">QChat - Şifre Sıfırlama</div>
        <div class="win7-content">
            <h3>Şifremi Unuttum</h3>
            {% if hata %}<div class="error">⚠️ {{ hata }}</div>{% endif %}
            {% if onay_mesaji %}<div class="error" style="color:#1d5d2a;background:#e7f7ea;border-color:#b9e3c1;">✅ {{ onay_mesaji }}</div>{% endif %}
            <form method="POST">
                <span class="field-label">Kullanıcı Adı</span>
                <input type="text" name="kimlik" placeholder="Kullanıcı adınızı girin" maxlength="120" value="{{ kimlik|default('') }}" required autocomplete="off">
                {% if kod_gerekli %}
                <span class="field-label">Doğrulama Kodu</span>
                <input type="text" name="dogrulama_kodu" placeholder="E-postaya gelen 6 haneli kod" inputmode="numeric" maxlength="6" pattern="\\d{6}" autocomplete="off">
                <span class="field-label">Yeni Şifre</span>
                <input type="password" name="yeni_sifre" placeholder="Yeni şifrenizi girin" minlength="7" maxlength="15" autocomplete="off">
                {% endif %}
                <div class="sifre-ipucu">Kod kayıtlı e-posta adresinize gönderilir. Yeni şifre 7-15 karakter olmalı ve en az 1 büyük harf ile 1 özel karakter içermelidir.</div>
                <button type="submit">{% if kod_gerekli %}ŞİFREYİ SIFIRLA{% else %}KOD GÖNDER{% endif %}</button>
                {% if kod_gerekli %}
                <button type="submit" name="kod_yenile" value="1" style="background:linear-gradient(180deg,#7fb8e0,#2a6ea8); border-color:#1c4a70;">KODU YENİDEN GÖNDER</button>
                {% endif %}
                <div style="text-align:center; margin-top:10px; font-size:12px;">
                    <a href="/giris" style="color:#1c5fb0; font-weight:700; text-decoration:none;">← Girişe dön</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""

mesaj_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sohbet</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, sans-serif;
            background: linear-gradient(180deg, #bcd6ee 0%, #d8e6f5 40%, #eaf2fa 100%);
            min-height: 100vh; margin: 0; padding: 3vh 12px; display: flex; justify-content: center;
        }
        .win7-window {
            background: #f4f8fc; border: 1px solid #5b8ac4; border-radius: 8px;
            box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            width: 100%; max-width: 480px; overflow: hidden; height: fit-content;
        }
        .win7-titlebar {
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 45%, #1c5fb0 55%, #2d7fd6 100%);
            color: #fff; font-weight: 600; font-size: 13px; padding: 10px 14px;
            text-shadow: 0 1px 1px rgba(0,0,0,.4); border-bottom: 1px solid #14417f;
            display: flex; align-items: center; gap: 6px;
        }
        .content { padding: 12px 14px 14px; }

        .topbar {
            display: flex; justify-content: space-between; align-items: center;
            background: linear-gradient(180deg,#eef4fb,#dde9f6);
            border: 1px solid #b9cfe4; border-radius: 5px; padding: 6px 8px; margin-bottom: 8px;
        }
        .user-info { font-size: 12.5px; font-weight: 700; color: #1c3d5c; display: flex; align-items: center; gap: 5px; }
        .logout-btn {
            background: linear-gradient(180deg,#f28b82,#c0392b); color: #fff; text-decoration: none;
            font-size: 11.5px; font-weight: 700; padding: 5px 12px; border-radius: 4px;
            border: 1px solid #8f241a; text-shadow: 0 1px 1px rgba(0,0,0,.3);
        }
        .logout-btn:active { filter: brightness(0.9); }
        .settings-btn { background: linear-gradient(180deg,#7fb8e0,#2a6ea8); border: 1px solid #1c4a70; }
        .settings-btn:hover { filter: brightness(1.03); }

        .settings-overlay {
            position: fixed; inset: 0; background: rgba(12,24,38,0.45);
            display: none; align-items: center; justify-content: center; z-index: 9999;
            padding: 18px;
        }
        .settings-card {
            width: 100%; max-width: 380px; background: #f4f8fc; border: 1px solid #5b8ac4;
            border-radius: 10px; box-shadow: 0 12px 32px rgba(20,60,110,0.35), 0 0 0 1px rgba(255,255,255,.5) inset;
            overflow: hidden;
        }
        .settings-title {
            background: linear-gradient(180deg,#79bdf7 0%, #3a8ee6 45%, #1c5fb0 55%, #2d7fd6 100%);
            color: #fff; font-weight: 700; padding: 10px 14px; border-bottom: 1px solid #14417f;
        }
        .settings-body { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 10px; }
        .settings-row {
            display: flex; align-items: center; justify-content: space-between; gap: 10px;
            background: #fff; border: 1px solid #b9cfe4; border-radius: 6px; padding: 8px 10px;
            font-size: 12.5px; color: #24465f;
        }
        .settings-row label { display: flex; align-items: center; gap: 8px; font-weight: 700; cursor: pointer; }
        .settings-row input[type="range"] { width: 140px; }
        .settings-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .settings-actions .small-btn { flex: 1; min-width: 90px; }

        .pinned-banner {
            background: #fff6d0; border: 1px solid #e0b400; border-radius: 4px;
            padding: 6px 8px; margin-bottom: 8px; font-size: 12px; font-weight: 600;
            display: none; color: #6b5200;
        }

        .panel-row {
            display: flex; gap: 6px; margin-bottom: 8px;
        }
        .panel-box {
            background: #fff; border: 1px solid #b9cfe4; border-radius: 5px;
            padding: 6px 8px; display: flex; align-items: center; gap: 6px;
            font-size: 12px; font-weight: 700; color: #2a4a68; flex: 1; min-width: 0;
        }
        .panel-box.grow select { flex: 1; }

        select {
            padding: 5px 6px; font-size: 12.5px; border: 1px solid #8fa9c4; border-radius: 3px;
            outline: none; background: #fbfdff; font-family: inherit; font-weight: 600; color: #1c2b3a;
            min-width: 0;
        }

        .small-btn {
            font-size: 11.5px; font-weight: 700; padding: 5px 9px; border-radius: 4px;
            border: 1px solid #1c5fb0; cursor: pointer; white-space: nowrap; color: #fff;
            text-shadow: 0 1px 1px rgba(0,0,0,.25);
            background: linear-gradient(180deg, #79bdf7 0%, #3a8ee6 48%, #1f74d6 52%, #1c5fb0 100%);
        }
        .small-btn:active { filter: brightness(0.9); }

        .room-create-panel {
            display: none; background: #fff; border: 1px solid #b9cfe4; border-radius: 5px;
            padding: 8px; margin-bottom: 8px; flex-direction: column; gap: 6px; font-size: 12px;
        }
        .room-create-panel .field-label { font-size: 11.5px; font-weight: 700; color: #3a5a7a; }
        .room-create-panel input {
            width: 100%; box-sizing: border-box; padding: 6px 8px; font-size: 13px;
            border: 1px solid #8fa9c4; border-radius: 3px; outline: none; font-family: inherit;
        }
        .room-create-btn-row { display: flex; gap: 6px; }
        .room-create-btn-row button {
            flex: 1; margin: 0; padding: 7px; font-size: 12px; font-weight: 700; border-radius: 4px;
            cursor: pointer; border: 1px solid; color: #fff; text-shadow: 0 1px 1px rgba(0,0,0,.25);
        }
        .btn-ok { background: linear-gradient(180deg,#7fd68a,#2e9e3f); border-color: #1f7a2e; }
        .btn-cancel { background: linear-gradient(180deg,#f28b82,#c0392b); border-color: #8f241a; }

        select#aliciSec { width: 100%; margin-bottom: 8px; }

        .chat-title {
            font-size: 11.5px; font-weight: 700; color: #fff; padding: 4px 8px;
            display: inline-block; margin-top: 4px; margin-bottom: 4px; border-radius: 3px;
            background: linear-gradient(180deg,#4a4a4a,#2b2b2b);
        }
        .chat-box {
            background: #ffffff; border: 1px solid #b9cfe4; border-radius: 5px;
            height: 190px; overflow-y: auto; padding: 8px 10px; margin-bottom: 4px;
            display: flex; flex-direction: column-reverse; font-size: 13px;
        }
        .chat-box::-webkit-scrollbar { width: 10px; }
        .chat-box::-webkit-scrollbar-track { background: #eef3f9; }
        .chat-box::-webkit-scrollbar-thumb { background: #a9c3dd; border-radius: 5px; border: 2px solid #eef3f9; }

        .typing-indicator { height: 18px; font-size: 11px; font-style: italic; color: #4a6580; font-weight: 600; margin-bottom: 6px; padding-left: 2px; }

        .msg-item, .msg-private {
            margin-bottom: 5px; word-break: break-word; padding: 3px 5px; border-radius: 3px; line-height: 1.4;
        }
        .msg-item:hover, .msg-private:hover { background: #f2f7fc; }
        .msg-private { background: #eef6ff; border-left: 3px solid #3a8ee6; }
        .msg-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
        .msg-user { font-weight: 700; }
        .msg-sistem { color: #b8281f !important; font-weight: 700; }
        .msg-time { font-size: 10.5px; color: #90a2b4; font-weight: 600; }
        .msg-body { color: #1c2b3a; }

        .msg-reply {
            margin: 4px 0 6px;
            padding: 5px 6px;
            border-left: 3px solid #3a8ee6;
            background: #eef6ff;
            border-radius: 4px;
            font-size: 11.5px;
            color: #33475c;
        }
        .msg-reply .reply-from { font-weight: 700; color: #1c3d5c; }
        .msg-reply .reply-text { display: block; margin-top: 2px; white-space: pre-wrap; word-break: break-word; }

        .msg-duyuru { color: #7a3d99; font-weight: 700; background: #f7ecff; border: 1px dashed #b07fd9; padding: 5px 6px; }
        .msg-sayac {
            color: #fff200 !important; background: linear-gradient(180deg,#d3261a,#a01912) !important;
            border: 1px solid #6e0f0a; border-radius: 3px; font-weight: 700; padding: 7px; font-size: 15px;
            text-align: center; margin-bottom: 5px; word-break: break-word;
        }

        .msg-action-btn {
            margin-left: auto; flex: 0 0 auto;
            font-size:10px; font-weight:700; padding:2px 5px; border-radius:3px;
            color:#fff; cursor:pointer; white-space:nowrap; line-height:1.2;
        }
        .msg-action-btn + .msg-action-btn { margin-left: 2px; }
        .sikayet-mesaj-btn {
            border:1px solid #8d2c22;
            background:linear-gradient(180deg,#ef8b7d,#c0392b);
        }
        .sil-mesaj-btn {
            border:1px solid #6d4a11;
            background:linear-gradient(180deg,#e5be67,#b77c10);
        }
        .msg-action-btn:hover { filter: brightness(1.05); }
        .msg-action-btn:active { filter: brightness(0.9); }
        .input-row { display: flex; gap: 6px; }
        input[type="text"] {
            flex: 1; padding: 8px 10px; font-size: 14px; font-family: inherit;
            border: 1px solid #8fa9c4; border-radius: 4px; outline: none; box-shadow: inset 0 1px 3px rgba(0,0,0,.1);
        }
        input[type="text"]:focus { border-color: #3a8ee6; box-shadow: 0 0 0 3px rgba(58,142,230,.25) inset; }

        button[type="submit"] {
            width: 100%; padding: 9px; font-size: 14px; font-weight: 700; font-family: inherit;
            border: 1px solid #1c5fb0; border-radius: 4px; cursor: pointer; margin-top: 6px;
            color: #fff; text-shadow: 0 1px 1px rgba(0,0,0,.25);
            background: linear-gradient(180deg, #79bdf7 0%, #3a8ee6 48%, #1f74d6 52%, #1c5fb0 100%);
            box-shadow: 0 1px 0 rgba(255,255,255,.5) inset;
        }
        button[type="submit"]:active { background: linear-gradient(180deg, #2c80de 0%, #1c5fb0 100%); box-shadow: inset 0 2px 4px rgba(0,0,0,.25); }

        @media (max-width: 420px) {
            .panel-row { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="win7-window">
        <div class="win7-titlebar"><span>💬</span><span>Konuşma</span></div>
        <div class="content">
            <div id="pinnedBanner" class="pinned-banner">📌 <span id="pinnedText"></span></div>

            <div class="topbar">
                <div class="user-info">👤 {{ kullanici }} <span id="krediGoster" style="margin-left:8px; font-size:11.5px; font-weight:700; color:#7a5200; background:#fff6d0; border:1px solid #e0b400; border-radius:999px; padding:3px 8px;">🪙 0 kredi</span></div>
                <div style="display:flex; gap:6px; align-items:center;">
                    <button type="button" class="logout-btn settings-btn" onclick="ayarlarPenceresiAc()">⚙️ Ayarlar</button>
                    <a href="/cikis" class="logout-btn">Çıkış Yap</a>
                </div>
            </div>

            <div class="settings-overlay" id="ayarlarOverlay" onclick="if(event.target===this) ayarlarPenceresiKapat();">
                <div class="settings-card">
                    <div class="settings-title">⚙️ Ayarlar</div>
                    <div class="settings-body">
                        <div class="settings-row">
                            <label><input type="checkbox" id="ayarSesler"> Sesler</label>
                        </div>
                        <div class="settings-actions">
                            <button type="button" class="small-btn" onclick="ayarlarKaydet()">Kaydet</button>
                            <button type="button" class="small-btn" onclick="ayarlarSifirla()">Sıfırla</button>
                            <button type="button" class="small-btn" onclick="ayarlarPenceresiKapat()">Kapat</button>
                        </div>
                        {% if kullanici != "Sistem" %}
                        <form method="POST" action="/hesap-sil" onsubmit="return confirm('Hesabın kalıcı olarak silinsin ve çıkış yapılsın mı?');" style="margin:0;">
                            <button type="submit" class="small-btn" style="width:100%; background:linear-gradient(180deg,#f28b82,#c0392b); border-color:#8f241a;">🗑️ Hesabı Sil ve Çıkış Yap</button>
                        </form>
                        {% endif %}
                    </div>
                </div>
            </div>

            <div class="panel-row">
                <div class="panel-box grow">
                    Mekan:
                    <select id="odaSec" onchange="odaDegistir()"><option value="Genel">Genel</option></select>
                </div>
                <button type="button" class="small-btn" onclick="document.getElementById('odaKurPanel').style.display='flex';">➕ Oda Kur</button>
                <button type="button" class="small-btn" id="odaYonetimBtn" onclick="odaYonetimAcKapat();" style="background:linear-gradient(180deg,#8a7fe0,#5a4bc7); border-color:#3d2f9e;">🛡️ Oda Yönetimi</button>
            </div>

            <div class="room-create-panel" id="odaKurPanel">
                <span class="field-label">Yeni Oda Adı</span>
                <input type="text" id="yeniOdaAdi" placeholder="Örn: Oyun Odası" maxlength="15">
                <span class="field-label">Şifre (İsteğe Bağlı)</span>
                <input type="text" id="yeniOdaSifre" placeholder="Boş bırakılırsa şifresiz olur" maxlength="15">
                <div class="room-create-btn-row">
                    <button type="button" class="btn-ok" onclick="yeniOdaKur()">Kur</button>
                    <button type="button" class="btn-cancel" onclick="document.getElementById('odaKurPanel').style.display='none';">İptal</button>
                </div>
            </div>

            <div class="room-create-panel" id="odaYonetimPanel">
                <span class="field-label" id="odaYonetimBaslik">🛡️ Oda Yönetimi</span>
                <div id="odaYonetimYetkisiz" style="font-size:12px; color:#7a3d3d; display:none;">Bu odada yönetim yetkiniz yok.</div>
                <div id="odaYonetimIcerik" style="display:none; flex-direction:column; gap:6px;">
                    <span class="field-label">Kullanıcı Seç</span>
                    <select id="odaYonetimHedef"></select>
                    <span class="field-label">Rol</span>
                    <select id="odaYonetimRol">
                        <option value="yonetici">Yönetici / Moderatör</option>
                        <option value="uye">Sıradan Üye</option>
                    </select>
                    <div class="room-create-btn-row">
                        <button type="button" class="btn-ok" onclick="odaRolVer()">Rol Ver</button>
                    </div>
                    <div class="room-create-btn-row" id="odaLiderYapSatiri" style="display:none;">
                        <button type="button" class="btn-ok" onclick="odaLiderYap()" style="background:linear-gradient(180deg,#f7c948,#c78e1a); border-color:#8a5f10;">👑 Lider Yap</button>
                    </div>
                    <div id="odaSifreDegistirBlok" style="display:none;">
                        <span class="field-label">Oda Şifresini Değiştir (Boş = Şifresiz)</span>
                        <input type="text" id="odaYeniSifre" placeholder="Yeni şifre" maxlength="15">
                        <div class="room-create-btn-row">
                            <button type="button" class="btn-ok" onclick="odaSifreDegistir()" style="background:linear-gradient(180deg,#7fb8e0,#2a6ea8); border-color:#1c4a70;">🔑 Şifreyi Değiştir</button>
                        </div>
                        <div class="room-create-btn-row" id="odaKapatSatiri" style="display:none;">
                            <button type="button" class="btn-cancel" onclick="odaKapat()" style="background:linear-gradient(180deg,#e07f7f,#a82a2a); border-color:#701c1c; color:#fff;">🚫 Odayı Kapat</button>
                        </div>
                    </div>
                </div>
                <div class="room-create-btn-row">
                    <button type="button" class="btn-cancel" onclick="document.getElementById('odaYonetimPanel').style.display='none';">Kapat</button>
                </div>
            </div>

            <select id="aliciSec">
                <option value="Genel">📢 Odadaki Herkes</option>
            </select>

            <div class="chat-title" id="aktifOdaBaslik">📢 Genel Odası</div>
            <div class="chat-box" id="chatBox"></div>

            <div class="chat-title">🔒 DM'lerim</div>
            <div class="chat-box" id="dmBox"></div>

            <div class="typing-indicator" id="yaziyorBox"></div>

            <form id="mesajForm" onsubmit="mesajGonder(event)">
                <div id="replyBar" style="display:none; margin:0 0 8px; padding:8px 10px; border:1px solid #b9cfe4; border-radius:6px; background:#eef6ff; font-size:12px; color:#24465f; display:flex; align-items:flex-start; justify-content:space-between; gap:10px;">
                    <div style="min-width:0;">
                        <div><strong id="replyToName">Yanıt</strong></div>
                        <div id="replyToText" style="margin-top:3px; font-size:11.5px; color:#44627c; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:360px;"></div>
                    </div>
                    <button type="button" onclick="yanitTemizle()" style="flex:0 0 auto; padding:4px 8px; border:1px solid #8fa9c4; border-radius:4px; background:#fff; color:#24465f; cursor:pointer; font-weight:700;">İptal</button>
                </div>
                <div class="input-row">
                    <input type="text" id="mesajInput" placeholder="Mesajınızı yazın..." maxlength="200" autocomplete="off" oninput="yaziyorBildir()" required>
                </div>
                <button type="submit">GÖNDER</button>
            </form>
        </div>
    </div>

    <script>
        let sonMesajSayisi = 0;
        let ilkYukleme = true;
        let sonYazmaSuresi = 0;
        let audioCtx = null;
        let aktifOda = "Genel";
        let yanitHedefi = null;
        const AYARLAR_KEY = "qchat_ayarlar";
        let kullaniciAyarlar = { sesler: true };

        function ayarlarYukle() {
            try {
                const ham = localStorage.getItem(AYARLAR_KEY);
                if (ham) {
                    const kayitli = JSON.parse(ham);
                    kullaniciAyarlar.sesler = kayitli.sesler !== false;
                }
            } catch (e) {}
            ayarlarUygula();
        }

        function ayarlarUygula() {
            const ses = document.getElementById('ayarSesler');
            if (ses) ses.checked = kullaniciAyarlar.sesler;
        }

        function ayarlarKaydet() {
            const ses = document.getElementById('ayarSesler');
            kullaniciAyarlar.sesler = !!(ses && ses.checked);
            try { localStorage.setItem(AYARLAR_KEY, JSON.stringify(kullaniciAyarlar)); } catch (e) {}
            ayarlarUygula();
            ayarlarPenceresiKapat();
        }

        function ayarlarSifirla() {
            kullaniciAyarlar = { sesler: true };
            try { localStorage.removeItem(AYARLAR_KEY); } catch (e) {}
            ayarlarUygula();
        }

        function krediGuncelle() {
            fetch('/api/kullanici_bilgi?kullanici=' + encodeURIComponent("{{ kullanici }}"))
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById('krediGoster');
                    if (el && data && data.basarili) {
                        el.textContent = '🪙 ' + (data.kredi || 0) + ' kredi';
                    }
                })
                .catch(() => {});
        }

        function ayarlarPenceresiAc() {
            ayarlarUygula();
            document.getElementById('ayarlarOverlay').style.display = 'flex';
        }

        function ayarlarPenceresiKapat() {
            document.getElementById('ayarlarOverlay').style.display = 'none';
        }

        ayarlarYukle();
        krediGuncelle();

        function sesContextBaslat() {
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }
        }

        function gonderSesiCal() {
            if (!kullaniciAyarlar.sesler) return;
            sesContextBaslat();
            if (!audioCtx) return;
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = 'square';
            osc.frequency.setValueAtTime(800, audioCtx.currentTime);
            osc.frequency.exponentialRampToValueAtTime(400, audioCtx.currentTime + 0.05);
            gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
            gain.gain.linearRampToValueAtTime(0.01, audioCtx.currentTime + 0.05);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.05);
        }

        function almaSesiCal() {
            if (!kullaniciAyarlar.sesler) return;
            sesContextBaslat();
            if (!audioCtx) return;
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(600, audioCtx.currentTime);
            osc.frequency.setValueAtTime(900, audioCtx.currentTime + 0.08);
            gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
            gain.gain.linearRampToValueAtTime(0.01, audioCtx.currentTime + 0.2);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.2);
        }

        function sirenSesiCal() {
            sesContextBaslat();
            if (!audioCtx) return;
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = 'sawtooth';
            osc.frequency.setValueAtTime(300, audioCtx.currentTime);
            osc.frequency.linearRampToValueAtTime(1200, audioCtx.currentTime + 0.3);
            gain.gain.setValueAtTime(0.3, audioCtx.currentTime);
            gain.gain.linearRampToValueAtTime(0.01, audioCtx.currentTime + 0.3);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.3);
        }

        // --- Kullanıcı adına göre sabit bir renk üretir (yalnızca görsel amaçlı) ---
        function kullaniciRengi(isim) {
            let hash = 0;
            for (let i = 0; i < isim.length; i++) {
                hash = isim.charCodeAt(i) + ((hash << 5) - hash);
                hash |= 0;
            }
            const hue = Math.abs(hash) % 360;
            return `hsl(${hue}, 62%, 33%)`;
        }

        // --- Unix zaman damgasını HH:MM biçiminde gösterir ---
        function saatFormatla(zaman) {
            if (!zaman) return "";
            const d = new Date(zaman * 1000);
            const s = d.getHours().toString().padStart(2, '0');
            const dk = d.getMinutes().toString().padStart(2, '0');
            return s + ":" + dk;
        }

        function tarihFormatlaUnix(zaman) {
            if (!zaman) return "Bilinmiyor";
            const d = new Date(zaman * 1000);
            return d.toLocaleString('tr-TR', {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
        }

        function sureFormatla(toplamSaniye) {
            const saniye = Math.max(0, Math.floor(toplamSaniye || 0));
            const dk = Math.floor(saniye / 60);
            const saat = Math.floor(dk / 60);
            const kalanDk = dk % 60;
            const kalanSn = saniye % 60;
            if (saat > 0) return `${saat} saat ${kalanDk} dk`;
            if (dk > 0) return `${dk} dk ${kalanSn} sn`;
            return `${kalanSn} sn`;
        }

        function formatliSayi(sayi) {
            return new Intl.NumberFormat('tr-TR').format(sayi || 0);
        }

        let kullaniciBilgiTimer = null;

        function kullaniciBilgiKapat() {
            const onceki = document.getElementById('kullaniciBilgiModal');
            if (onceki) onceki.remove();
            if (kullaniciBilgiTimer) {
                clearInterval(kullaniciBilgiTimer);
                kullaniciBilgiTimer = null;
            }
        }

        function kullaniciBilgiAc(kullanici) {
            if (!kullanici) return;
            kullaniciBilgiKapat();

            const modal = document.createElement('div');
            modal.id = 'kullaniciBilgiModal';
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;z-index:10000;padding:12px;';
            modal.innerHTML = `
                <div style="width:min(420px,100%);background:#f4f8fc;border:1px solid #5b8ac4;border-radius:8px;box-shadow:0 12px 30px rgba(0,0,0,.35);overflow:hidden;">
                    <div style="background:linear-gradient(180deg,#79bdf7 0%,#3a8ee6 45%,#1c5fb0 55%,#2d7fd6 100%);color:#fff;padding:9px 12px;font-weight:700;display:flex;justify-content:space-between;align-items:center;">
                        <span>👤 Kullanıcı Bilgisi</span>
                        <button type="button" id="kullaniciBilgiKapat" style="background:#c00;color:#fff;border:0;padding:3px 7px;border-radius:3px;cursor:pointer;">✕</button>
                    </div>
                    <div style="padding:12px;display:flex;flex-direction:column;gap:8px;">
                        <div id="kullaniciBilgiIcerik" style="font-family:Segoe UI,Tahoma,sans-serif;color:#1f2937;line-height:1.5;">Yükleniyor...</div>
                    </div>
                </div>`;
            document.body.appendChild(modal);

            const kapat = () => kullaniciBilgiKapat();
            document.getElementById('kullaniciBilgiKapat').onclick = kapat;
            modal.addEventListener('click', (e) => {
                if (e.target === modal) kapat();
            });

            const yukle = () => {
                fetch('/api/kullanici_bilgi?kullanici=' + encodeURIComponent(kullanici))
                    .then(r => r.json())
                    .then(data => {
                        const alan = document.getElementById('kullaniciBilgiIcerik');
                        if (!alan) return;
                        if (!data.basarili) {
                            alan.innerHTML = `<div style="color:#b91c1c;font-weight:700;">⚠️ ${data.hata || 'Kullanıcı bilgisi alınamadı.'}</div>`;
                            return;
                        }
                        const onlineText = data.online ? 'Çevrim içi' : 'Çevrim dışı';
                        const onlineColor = data.online ? '#166534' : '#6b7280';
                        alan.innerHTML = `
                            <div style="font-size:18px;font-weight:800;color:#0f172a;">${data.isim || kullanici}</div>
                            <div style="margin-top:4px;color:${onlineColor};font-weight:700;">${onlineText}</div>
                            <div style="margin-top:8px;display:grid;gap:6px;">
                                <div><strong>Kredi:</strong> ${formatliSayi(data.kredi || 0)}</div>
                                <div><strong>Hesap oluşturma:</strong> ${data.kayit_tarihi || 'Bilinmiyor'}</div>
                                <div><strong>Mesaj sayısı:</strong> ${formatliSayi(data.mesaj_sayisi || 0)}</div>
                                <div><strong>Bu sitede süre:</strong> ${sureFormatla(data.oturum_suresi_saniye || 0)}</div>
                            </div>
                        `;
                    })
                    .catch(() => {
                        const alan = document.getElementById('kullaniciBilgiIcerik');
                        if (alan) alan.innerHTML = '<div style="color:#b91c1c;font-weight:700;">⚠️ Kullanıcı bilgisi alınamadı.</div>';
                    });
            };

            yukle();
            kullaniciBilgiTimer = setInterval(yukle, 5000);
        }

        function yaziyorBildir() {
            const simdi = Date.now();
            if (simdi - sonYazmaSuresi > 1500) { 
                sonYazmaSuresi = simdi;
                fetch('/api/yaziyor', { method: 'POST' });
            }
        }
        
        function odalariGuncelle() {
            fetch('/api/odalar')
                .then(r => r.json())
                .then(data => {
                    const sec = document.getElementById('odaSec');
                    const val = sec.value;
                    sec.innerHTML = '';
                    let odayiBulduk = false;
                    data.forEach(o => {
                        const opt = document.createElement('option');
                        opt.value = o.ad;
                        opt.textContent = o.ad + (o.kilitli ? " 🔒" : "");
                        if(o.ad === (val || aktifOda)) { opt.selected = true; odayiBulduk = true; }
                        sec.appendChild(opt);
                    });
                    
                    if(!odayiBulduk && aktifOda !== "Genel") {
                        aktifOda = "Genel";
                        document.getElementById('aktifOdaBaslik').textContent = "📢 Genel Odası";
                        mesajlariGuncelle(true);
                    }
                });
        }
        
        function odaDegistir() {
            const sec = document.getElementById('odaSec');
            const secilen = sec.value;
            if(secilen === aktifOda) return;
            
            fetch('/api/oda_kontrol?oda=' + encodeURIComponent(secilen))
            .then(r => r.json()).then(data => {
                if (data.kilitli && secilen !== "Genel") {
                    const pass = prompt(secilen + " odası şifreli. Şifreyi girin:");
                    if(pass === null) {
                        sec.value = aktifOda;
                        return;
                    }
                    fetch('/api/oda_giris', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'oda=' + encodeURIComponent(secilen) + '&sifre=' + encodeURIComponent(pass)
                    }).then(r => r.json()).then(res => {
                        if(res.basarili) {
                            aktifOda = secilen;
                            document.getElementById('aktifOdaBaslik').textContent = "📢 " + secilen + " Odası";
                            mesajlariGuncelle(true);
                        } else {
                            alert("Yanlış şifre!");
                            sec.value = aktifOda;
                        }
                    });
                } else {
                    aktifOda = secilen;
                    document.getElementById('aktifOdaBaslik').textContent = "📢 " + secilen + " Odası";
                    mesajlariGuncelle(true);
                }
                odaYetkiYukle();
            });
        }

        // ==================== ODA LİDERLİK / ROL / YETKİ PANELİ ====================
        function odaYonetimAcKapat() {
            const panel = document.getElementById('odaYonetimPanel');
            const acik = panel.style.display === 'flex';
            panel.style.display = acik ? 'none' : 'flex';
            if (!acik) odaYetkiYukle();
        }

        function odaYetkiYukle() {
            fetch('/api/oda_yetki?oda=' + encodeURIComponent(aktifOda))
                .then(r => r.json())
                .then(data => {
                    document.getElementById('odaYonetimBaslik').textContent = "🛡️ " + aktifOda + " Odası — Lider: " + (data.lider || "-");

                    const yetkisizKutu = document.getElementById('odaYonetimYetkisiz');
                    const icerikKutu = document.getElementById('odaYonetimIcerik');

                    if (data.yonetebilir_mi) {
                        yetkisizKutu.style.display = 'none';
                        icerikKutu.style.display = 'flex';

                        const hedefSec = document.getElementById('odaYonetimHedef');
                        const secili = hedefSec.value;
                        hedefSec.innerHTML = '';
                        (data.uyeler || []).forEach(u => {
                            if (u.isim === "{{ kullanici }}") return;
                            const opt = document.createElement('option');
                            opt.value = u.isim;
                            let etiket = u.isim;
                            if (u.isim === data.lider) etiket += " (Lider)";
                            else if (u.rol === "yonetici") etiket += " (Yönetici)";
                            if (u.banli) etiket += u.ban_kalan_dk ? ` (Banlı: ${u.ban_kalan_dk}dk)` : " (Atılmış)";
                            opt.textContent = etiket;
                            if (u.isim === secili) opt.selected = true;
                            hedefSec.appendChild(opt);
                        });

                        document.getElementById('odaLiderYapSatiri').style.display = data.lider_mi ? 'flex' : 'none';
                        document.getElementById('odaSifreDegistirBlok').style.display = data.lider_mi ? 'block' : 'none';
                        document.getElementById('odaKapatSatiri').style.display = (data.lider_mi && aktifOda !== 'Genel') ? 'flex' : 'none';
                    } else {
                        yetkisizKutu.style.display = 'block';
                        icerikKutu.style.display = 'none';
                    }
                })
                .catch(err => console.log(err));
        }

        function odaRolVer() {
            const hedef = document.getElementById('odaYonetimHedef').value;
            const rol = document.getElementById('odaYonetimRol').value;
            if (!hedef) return;
            fetch('/api/oda_rol_ver', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'oda=' + encodeURIComponent(aktifOda) + '&hedef=' + encodeURIComponent(hedef) + '&rol=' + encodeURIComponent(rol)
            }).then(r => r.json()).then(res => {
                if (res.basarili) odaYetkiYukle();
                else alert("⚠️ " + (res.hata || "İşlem başarısız."));
            });
        }

        function odaLiderYap() {
            const hedef = document.getElementById('odaYonetimHedef').value;
            if (!hedef) return;
            if (!confirm(hedef + " kullanıcısını bu odanın lideri yapmak istiyor musunuz? Liderliğiniz devredilecek.")) return;
            fetch('/api/oda_lider_yap', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'oda=' + encodeURIComponent(aktifOda) + '&hedef=' + encodeURIComponent(hedef)
            }).then(r => r.json()).then(res => {
                if (res.basarili) odaYetkiYukle();
                else alert("⚠️ " + (res.hata || "İşlem başarısız."));
            });
        }

        function odaSifreDegistir() {
            const yeniSifre = document.getElementById('odaYeniSifre').value.trim();
            fetch('/api/oda_sifre_degistir', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'oda=' + encodeURIComponent(aktifOda) + '&sifre=' + encodeURIComponent(yeniSifre)
            }).then(r => r.json()).then(res => {
                if (res.basarili) {
                    alert("Oda şifresi güncellendi.");
                    document.getElementById('odaYeniSifre').value = '';
                    odalariGuncelle();
                } else {
                    alert("⚠️ " + (res.hata || "İşlem başarısız."));
                }
            });
        }

        function odaKapat() {
            if (aktifOda === 'Genel') return;
            if (!confirm("'" + aktifOda + "' odasını kalıcı olarak kapatmak istiyor musunuz? Bu işlem geri alınamaz.")) return;
            fetch('/api/oda_kapat', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'oda=' + encodeURIComponent(aktifOda)
            }).then(r => r.json()).then(res => {
                if (res.basarili) {
                    alert("Oda kapatıldı.");
                    document.getElementById('odaYonetimPanel').style.display = 'none';
                    aktifOda = "Genel";
                    document.getElementById('aktifOdaBaslik').textContent = "📢 Genel Odası";
                    odalariGuncelle();
                    mesajlariGuncelle(true);
                } else {
                    alert("⚠️ " + (res.hata || "İşlem başarısız."));
                }
            });
        }
        // ============================================================================

        function yeniOdaKur() {
            const adInput = document.getElementById('yeniOdaAdi');
            const sifreInput = document.getElementById('yeniOdaSifre');
            const ad = adInput.value.trim();
            const sifre = sifreInput.value.trim();
            
            if (!ad) {
                alert("Lütfen bir oda adı girin.");
                return;
            }
            
            fetch('/api/oda_olustur', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'oda=' + encodeURIComponent(ad) + '&sifre=' + encodeURIComponent(sifre)
            })
            .then(r => r.json())
            .then(res => {
                if (res.basarili) {
                    alert("Oda '" + ad + "' başarıyla kuruldu!");
                    adInput.value = '';
                    sifreInput.value = '';
                    document.getElementById('odaKurPanel').style.display = 'none';
                    aktifOda = ad;
                    document.getElementById('aktifOdaBaslik').textContent = "📢 " + ad + " Odası";
                    odalariGuncelle();
                    mesajlariGuncelle(true);
                    odaYetkiYukle();
                } else {
                    alert("⚠️ " + (res.hata || "Oda kurulamadı."));
                }
            })
            .catch(err => alert("Oda kurulurken hata oluştu: " + err));
        }
function kullanicilariGuncelle() {
            fetch('/api/kullanicilar')
                .then(res => {
                    if (res.status === 403) window.location.reload();
                    return res.json();
                })
                .then(data => {
                    const sec = document.getElementById('aliciSec');
                    const secili = sec.value;
                    sec.innerHTML = '<option value="Genel">📢 Odadaki Herkes</option>';
                    data.forEach(k => {
                        if(k !== "{{ kullanici }}") {
                            const opt = document.createElement('option');
                            opt.value = k;
                            opt.textContent = "🔒 Özel: " + k;
                            if(k === secili) opt.selected = true;
                            sec.appendChild(opt);
                        }
                    });
                })
                .catch(err => console.log(err));
        }


        function yanitOzetMetni(metin) {
            return (metin || '').toString().replace(/\s+/g, ' ').trim().slice(0, 140);
        }

        function yanitTemizle() {
            yanitHedefi = null;
            const bar = document.getElementById('replyBar');
            const name = document.getElementById('replyToName');
            const text = document.getElementById('replyToText');
            if (bar) bar.style.display = 'none';
            if (name) name.textContent = 'Yanıt';
            if (text) text.textContent = '';
        }

        function yanitHazirla(m) {
            if (!m || !m.gonderen || m.gonderen === "{{ kullanici }}" || m.gonderen === "Sistem") return;
            yanitHedefi = {
                id: m.id || '',
                gonderen: m.gonderen || '',
                mesaj: m.mesaj || '',
                alici: m.alici || 'Genel',
                oda: m.oda || aktifOda
            };
            const bar = document.getElementById('replyBar');
            const name = document.getElementById('replyToName');
            const text = document.getElementById('replyToText');
            if (bar) bar.style.display = 'flex';
            if (name) name.textContent = 'Yanıt: ' + (yanitHedefi.gonderen || '');
            if (text) text.textContent = yanitOzetMetni(yanitHedefi.mesaj);
            const input = document.getElementById('mesajInput');
            if (input) input.focus();
        }

        function mesajSwipeKur(div, m) {
            if (!div || !m || !m.gonderen || m.gonderen === "{{ kullanici }}" || m.gonderen === "Sistem") return;
            let basladi = false;
            let tamamlandi = false;
            let startX = 0;
            let startY = 0;
            const esik = 65;
            div.style.touchAction = 'pan-y';

            const temizle = () => { basladi = false; tamamlandi = false; };

            div.addEventListener('pointerdown', (e) => {
                if (e.button !== undefined && e.button !== 0) return;
                basladi = true;
                tamamlandi = false;
                startX = e.clientX;
                startY = e.clientY;
            });

            div.addEventListener('pointermove', (e) => {
                if (!basladi || tamamlandi) return;
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                if (Math.abs(dx) < 18 || Math.abs(dx) < Math.abs(dy)) return;
                if (Math.abs(dx) >= esik) {
                    tamamlandi = true;
                    yanitHazirla(m);
                }
            });

            div.addEventListener('pointerup', temizle);
            div.addEventListener('pointercancel', temizle);
            div.addEventListener('pointerleave', temizle);
        }

        function mesajlariGuncelle(zorla = false) {
            if (zorla) { sonMesajSayisi = 0; ilkYukleme = true; }
            fetch('/api/mesajlar?oda=' + encodeURIComponent(aktifOda))
                .then(res => {
                    if (res.status === 403) window.location.reload();
                    return res.json();
                })
                .then(data => {
                    if (!data) return;

                    if (data.oda_banli && aktifOda !== "Genel") {
                        alert("🚫 '" + aktifOda + "' odasından atıldınız.");
                        aktifOda = "Genel";
                        document.getElementById('odaSec').value = "Genel";
                        document.getElementById('aktifOdaBaslik').textContent = "📢 Genel Odası";
                        mesajlariGuncelle(true);
                        return;
                    }

                    if (data.sabit_duyuru) {
                        document.getElementById('pinnedBanner').style.display = 'block';
                        document.getElementById('pinnedText').textContent = data.sabit_duyuru;
                    } else {
                        document.getElementById('pinnedBanner').style.display = 'none';
                    }

                    const msgs = data.mesajlar || [];

                    if (!ilkYukleme && msgs.length > sonMesajSayisi) {
                        const sonMesaj = msgs[msgs.length - 1];
                        if (sonMesaj.gonderen === '📢 ALARM') {
                            sirenSesiCal();
                            alert("🚨 SİSTEM ALARMI: " + sonMesaj.mesaj);
                        } else if (sonMesaj.gonderen === 'Sistem' || sonMesaj.gonderen === '📢 DUYURU' || sonMesaj.gonderen === '📢 SAYAÇ') {
                            almaSesiCal();
                        }
                    }
                    
                    const oncekiUzunluk = sonMesajSayisi;
                    sonMesajSayisi = msgs.length;
                    ilkYukleme = false;

                    const sayacVar = msgs.some(m => m.gonderen === '📢 SAYAÇ' && (m.bitis_zamani - (Date.now() / 1000)) > -2);
                    if (!zorla && msgs.length === oncekiUzunluk && !sayacVar) {
                        return; // İçerik değişmediyse gereksiz yeniden çizim yapma (daha akıcı/hızlı sohbet)
                    }

                    const chatBox = document.getElementById('chatBox');
                    const dmBox = document.getElementById('dmBox');
                    chatBox.innerHTML = '';
                    dmBox.innerHTML = '';
                    
                    msgs.slice().reverse().forEach(m => {
                        const div = document.createElement('div');
                        const isPrivate = m.alici && m.alici !== "Genel";
                        const isDuyuru = m.gonderen === '📢 DUYURU' || m.gonderen === '📢 ALARM';
                        const isSayac = m.gonderen === '📢 SAYAÇ';
                        
                        if (isSayac) {
                            div.className = 'msg-item msg-sayac';
                            let kalanSaniye = Math.max(0, Math.floor(m.bitis_zamani - (Date.now() / 1000)));
                            let dk = Math.floor(kalanSaniye / 60);
                            let sn = kalanSaniye % 60;
                            let formatli = dk.toString().padStart(2, '0') + ":" + sn.toString().padStart(2, '0');
                            
                            if (kalanSaniye > 0) {
                                div.textContent = '⏱️ ' + formatli;
                            } else {
                                div.textContent = '⏱️ 00:00 (Süre Bitti!)';
                            }
                        } else {
                            div.className = isPrivate ? 'msg-private' : (isDuyuru ? 'msg-item msg-duyuru' : 'msg-item');

                            const head = document.createElement('div');
                            head.className = 'msg-head';

                            if (m.reply_to && m.reply_to.gonderen) {
                                const reply = document.createElement('div');
                                reply.className = 'msg-reply';
                                const replyFrom = document.createElement('span');
                                replyFrom.className = 'reply-from';
                                replyFrom.textContent = '↩ ' + m.reply_to.gonderen;
                                reply.appendChild(replyFrom);
                                const replyText = document.createElement('span');
                                replyText.className = 'reply-text';
                                replyText.textContent = m.reply_to.mesaj || '';
                                reply.appendChild(replyText);
                                div.appendChild(reply);
                            }

                            const isim = document.createElement('span');
                            isim.className = isDuyuru ? 'msg-user msg-sistem' : (m.gonderen === 'Sistem' ? 'msg-user msg-sistem' : 'msg-user');

                            let gosterim = m.gonderen;
                            if (isPrivate) {
                                gosterim = m.gonderen + " ➔ " + m.alici;
                            }
                            isim.textContent = gosterim;
                            if (!isDuyuru && m.gonderen !== 'Sistem') {
                                isim.style.color = kullaniciRengi(m.gonderen || '');
                            }
                            head.appendChild(isim);

                            if (m.zaman) {
                                const saat = document.createElement('span');
                                saat.className = 'msg-time';
                                saat.textContent = saatFormatla(m.zaman);
                                head.appendChild(saat);
                            }

                            // Mesajın en sağında gerekli işlem butonları.
                            if (!isDuyuru && m.gonderen !== 'Sistem' && m.gonderen !== "{{ kullanici }}") {
                                const sikayetBtn = document.createElement('button');
                                sikayetBtn.type = 'button';
                                sikayetBtn.className = 'msg-action-btn sikayet-mesaj-btn';
                                sikayetBtn.textContent = '⚠️ Şikayet Et';
                                sikayetBtn.title = 'Bu mesajı şikayet et';
                                sikayetBtn.onclick = (event) => {
                                    event.stopPropagation();
                                    sikayetPenceresiAc(m.gonderen, m);
                                };
                                head.appendChild(sikayetBtn);
                            }

                            // Kullanıcının kendi mesajının sağında sil butonu.
                            if (!isDuyuru && m.gonderen === "{{ kullanici }}" && m.id) {
                                const silBtn = document.createElement('button');
                                silBtn.type = 'button';
                                silBtn.className = 'msg-action-btn sil-mesaj-btn';
                                silBtn.textContent = '🗑️ Sil';
                                silBtn.title = 'Kendi mesajını sil';
                                silBtn.onclick = (event) => {
                                    event.stopPropagation();
                                    mesajSil(m.id);
                                };
                                head.appendChild(silBtn);
                            }

                            div.appendChild(head);

                            const govde = document.createElement('div');
                            govde.className = 'msg-body';
                            govde.textContent = m.mesaj;
                            div.appendChild(govde);

                            if (!isDuyuru && m.gonderen) {
                                div.style.cursor = 'pointer';
                                div.title = 'Kullanıcı bilgilerini aç';
                                div.onclick = () => kullaniciBilgiAc(m.gonderen);
                            }
                            mesajSwipeKur(div, m);
                        }
                        if (isPrivate && !isSayac) {
                            dmBox.appendChild(div);
                        } else {
                            chatBox.appendChild(div);
                        }
                    });
            })
            .catch(err => console.log(err));

            fetch('/api/yaziyor_durumu')
                .then(res => res.json())
                .then(data => {
                    const yaziyorBox = document.getElementById('yaziyorBox');
                    if (data.yazanlar && data.yazanlar.length > 0) {
                        yaziyorBox.textContent = '✍️ ' + data.yazanlar.join(', ') + ' yazıyor...';
                    } else {
                        yaziyorBox.textContent = '';
                    }
                })
                .catch(err => console.log(err));
            krediGuncelle();
        }

        function mesajSil(mesajId) {
            if (!mesajId) return;
            if (!confirm('Bu mesajı silmek istediğine emin misin?')) return;

            fetch('/api/mesaj_sil', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'mesaj_id=' + encodeURIComponent(mesajId)
            })
            .then(res => res.json())
            .then(data => {
                if (data.basarili) {
                    mesajlariGuncelle(true);
                } else {
                    alert('⚠️ ' + (data.hata || 'Mesaj silinemedi.'));
                }
            })
            .catch(() => alert('⚠️ Mesaj silinirken bağlantı hatası oluştu.'));
        }

        function mesajGonder(event) {
            event.preventDefault();
            const input = document.getElementById('mesajInput');
            const alici = document.getElementById('aliciSec').value;
            const mesaj = input.value.trim();
            if(!mesaj) return;
            const reply = yanitHedefi;
            let body = 'mesaj=' + encodeURIComponent(mesaj) + '&alici=' + encodeURIComponent(alici) + '&oda=' + encodeURIComponent(aktifOda);
            if (reply) {
                body += '&reply_id=' + encodeURIComponent(reply.id || '') +
                        '&reply_gonderen=' + encodeURIComponent(reply.gonderen || '') +
                        '&reply_mesaj=' + encodeURIComponent(reply.mesaj || '') +
                        '&reply_alici=' + encodeURIComponent(reply.alici || '') +
                        '&reply_oda=' + encodeURIComponent(reply.oda || '');
            }
            fetch('/api/gonder', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: body
            }).then(res => {
                if (res.status === 403) {
                    res.text().then(text => {
                        if(text === "Bakım" || text === "Kick") window.location.reload();
                        else alert("🚫 " + text);
                    });
                } else {
                    gonderSesiCal();
                    input.value = '';
                    yanitTemizle();
                    mesajlariGuncelle();
        krediGuncelle();
                }
            });
        }

        function sikayetPenceresiAc(kullanici, ilgiliMesaj = null) {
            if (!kullanici || kullanici === "{{ kullanici }}" || kullanici === "Sistem") return;
            const nedenler = ["Küfür","Taciz","Uygunsuz davranış","Spam","Hakaret","Tehdit / güvenlik","Diğer"];
            let secenekler = nedenler.map(n => `<option value="${n}">${n}</option>`).join('');
            const onceki = document.getElementById('sikayetModal');
            if (onceki) onceki.remove();
            const modal = document.createElement('div');
            modal.id = 'sikayetModal';
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;z-index:9999;padding:12px;';
            modal.innerHTML = `
                <div style="width:min(430px,100%);background:#f4f8fc;border:1px solid #5b8ac4;border-radius:8px;box-shadow:0 12px 30px rgba(0,0,0,.35);overflow:hidden;">
                    <div style="background:linear-gradient(180deg,#79bdf7 0%,#3a8ee6 45%,#1c5fb0 55%,#2d7fd6 100%);color:#fff;padding:9px 12px;font-weight:700;display:flex;justify-content:space-between;align-items:center;">
                        <span>⚠️ ${kullanici} — Şikayet Et</span>
                        <button type="button" id="sikayetKapat" style="background:#c00;color:#fff;border:0;padding:3px 7px;border-radius:3px;cursor:pointer;">✕</button>
                    </div>
                    <div style="padding:12px;display:flex;flex-direction:column;gap:7px;">
                        <label style="font-weight:700;font-size:12px;color:#3a5a7a;">Şikayet nedeni</label>
                        <select id="sikayetNedeni" style="padding:7px;border:1px solid #8fa9c4;border-radius:4px;">${secenekler}</select>
                        <label style="font-weight:700;font-size:12px;color:#3a5a7a;">İlgili mesaj (isteğe bağlı)</label>
                        <select id="sikayetMesaj" style="padding:7px;border:1px solid #8fa9c4;border-radius:4px;"><option value="">Mesaj seçilmedi</option></select>
                        <label style="font-weight:700;font-size:12px;color:#3a5a7a;">Açıklama <span style="font-weight:600;color:#777;">(en az 30 karakter)</span></label>
                        <textarea id="sikayetAciklama" maxlength="1000" rows="6" placeholder="Yaşanan durumu ayrıntılı şekilde açıklayın..." style="resize:vertical;padding:8px;border:1px solid #8fa9c4;border-radius:4px;font-family:inherit;"></textarea>
                        <div id="sikayetKarakter" style="font-size:11px;color:#777;text-align:right;">0 / 30 minimum</div>
                        <div style="display:flex;gap:6px;">
                            <button type="button" id="sikayetGonderBtn" style="flex:1;background:linear-gradient(180deg,#7fd68a,#2e9e3f);color:#fff;border:1px solid #1f7a2e;padding:8px;border-radius:4px;font-weight:700;cursor:pointer;">Gönder</button>
                            <button type="button" id="sikayetIptalBtn" style="flex:1;background:linear-gradient(180deg,#f28b82,#c0392b);color:#fff;border:1px solid #8f241a;padding:8px;border-radius:4px;font-weight:700;cursor:pointer;">İptal</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(modal);

            const aciklama = document.getElementById('sikayetAciklama');
            aciklama.addEventListener('input', () => {
                document.getElementById('sikayetKarakter').textContent = aciklama.value.length + ' / 30 minimum';
            });

            fetch('/api/sikayet_mesajlari?kullanici=' + encodeURIComponent(kullanici))
                .then(r => r.json()).then(data => {
                    const sec = document.getElementById('sikayetMesaj');
                    let tiklananMesajId = ilgiliMesaj && ilgiliMesaj.id ? String(ilgiliMesaj.id) : '';
                    let bulundu = false;
                    (data.mesajlar || []).forEach(m => {
                        const o = document.createElement('option');
                        o.value = m.id;
                        const tarih = new Date((m.zaman || 0) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
                        o.textContent = '[' + tarih + '] ' + (m.mesaj || '').slice(0, 100);
                        if (tiklananMesajId && String(m.id) === tiklananMesajId) {
                            o.selected = true;
                            bulundu = true;
                        }
                        sec.appendChild(o);
                    });
                    if (!bulundu && ilgiliMesaj && ilgiliMesaj.mesaj) {
                        const o = document.createElement('option');
                        o.value = ilgiliMesaj.id || '';
                        const tarih = ilgiliMesaj.zaman ? new Date(ilgiliMesaj.zaman * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
                        o.textContent = (tarih ? '[' + tarih + '] ' : '') + String(ilgiliMesaj.mesaj).slice(0, 100);
                        o.selected = true;
                        sec.appendChild(o);
                    }
                }).catch(() => {});

            document.getElementById('sikayetKapat').onclick = () => modal.remove();
            document.getElementById('sikayetIptalBtn').onclick = () => modal.remove();
            document.getElementById('sikayetGonderBtn').onclick = () => {
                const ac = aciklama.value.trim();
                if (ac.length < 30) { alert('⚠️ Açıklama en az 30 karakter olmalıdır.'); return; }
                const body = new URLSearchParams({
                    sikayet_edilen: kullanici,
                    neden: document.getElementById('sikayetNedeni').value,
                    aciklama: ac,
                    mesaj_id: document.getElementById('sikayetMesaj').value,
                    oda: aktifOda
                });
                fetch('/api/sikayet_olustur', {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body})
                    .then(r => r.json()).then(res => {
                        if (res.basarili) { alert('✅ Şikayetiniz yönetime gönderildi.'); modal.remove(); }
                        else alert('⚠️ ' + (res.hata || 'Şikayet gönderilemedi.'));
                    }).catch(() => alert('⚠️ Şikayet gönderilirken bağlantı hatası oluştu.'));
            };
        }

        setInterval(odalariGuncelle, 5000);
        setInterval(kullanicilariGuncelle, 5000);
        setInterval(mesajlariGuncelle, 1000);
        setInterval(krediGuncelle, 30000);
        odalariGuncelle();
        kullanicilariGuncelle();
        mesajlariGuncelle();
        odaYetkiYukle();
    </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def ana_sayfa():
    if "kullanici" not in session:
        return redirect("/giris")
    return render_template_string(mesaj_html, kullanici=session["kullanici"])

@app.route("/cikis", methods=["GET"])
def cikis():
    kullanici = session.get("kullanici")
    if kullanici:
        oturum_suresini_guncelle(kullanici)
    session.pop("kullanici", None)
    return redirect("/giris")

@app.route("/hesap-sil", methods=["POST"])
def hesap_sil():
    kullanici = session.get("kullanici")
    if not kullanici:
        return redirect("/giris")
    if kullanici == "Sistem":
        return "<h2 style='font-family:sans-serif;color:#b91c1c;text-align:center;'>Sistem hesabı silinemez.</h2>", 403

    with veri_kilidi:
        kullanici_db.pop(kullanici, None)

        eski_email = kullanici_emailleri.pop(kullanici, None)
        if eski_email:
            eski_email = e_posta_normalize(eski_email)
            if email_hesaplari.get(eski_email) == kullanici:
                email_hesaplari.pop(eski_email, None)
            banli_emailler.discard(eski_email)

        engellenenler.discard(kullanici)
        susturulanlar.pop(kullanici, None)
        oda_kurma_izni.discard(kullanici)
        giris_denemesini_temizle(kullanici)

        son_aktiflik.pop(kullanici, None)
        son_mesaj_zamani.pop(kullanici, None)
        yaziyor_durumu.pop(kullanici, None)
        kullanici_kayit_zamani.pop(kullanici, None)
        kullanici_oturum_toplam_saniye.pop(kullanici, None)
        kullanici_oturum_son_kayit.pop(kullanici, None)
        kullanici_renames.pop(kullanici, None)
        giris_hatali_deneme.pop(kullanici, None)
        giris_kilitli.pop(kullanici, None)
        zorla_cikis.discard(kullanici)

        for oda_adi, lider in list(oda_liderleri.items()):
            if lider == kullanici:
                oda_liderleri[oda_adi] = "Sistem"
            oda_roller.get(oda_adi, {}).pop(kullanici, None)
            if kullanici in oda_yasaklari.get(oda_adi, []):
                try:
                    oda_yasaklari[oda_adi].remove(kullanici)
                except ValueError:
                    pass
            oda_gecici_banlar.get(oda_adi, {}).pop(kullanici, None)

        for msg in sohbet_gecmisi:
            if msg.get("gonderen") == kullanici:
                msg["gonderen"] = "Silinmiş Hesap"
            if msg.get("alici") == kullanici:
                msg["alici"] = "Silinmiş Hesap"

        durumu_kaydet()

    session.clear()
    return redirect("/giris")

@app.route("/giris", methods=["GET", "POST"])
def giris():
    hata = None
    onay_mesaji = None
    kod_gerekli = False
    form_kullanici = ""
    form_email = ""

    bekleyen_token = session.get("kayit_dogrulama_token")
    bekleyen = bekleyen_kayitlar.get(bekleyen_token) if bekleyen_token else None

    if request.method == "POST":
        kullanici = request.form.get("kullanici", "").strip()
        email = e_posta_normalize(request.form.get("email", ""))
        sifre = request.form.get("sifre", "").strip()
        kod = request.form.get("dogrulama_kodu", "").strip()
        remember = request.form.get("beni_hatirla") == "on"

        form_kullanici = kullanici
        form_email = email

        if kullanici and sifre:
            if request.form.get("bot_dogrulama") != "on":
                hata = "🤖 Lütfen robot olmadığınızı doğrulayın."
                return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if len(kullanici) > 15:
                hata = "❌ Kullanıcı adı çok uzun (en fazla 15 karakter)."
                return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if len(sifre) > 20:
                hata = "❌ Şifre çok uzun (en fazla 20 karakter)."
                return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if kullanici.lower() == "sistem" and kullanici not in kullanici_db:
                hata = "❌ Bu kullanıcı adını kullanamazsınız!"
                return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if kullanici in engellenenler:
                return "<h2 style='color:red; text-align:center;'>🚫 Bu kullanıcı engellenmiştir.</h2>", 403

            kalan_kilit = giris_kilidini_kontrol_et(kullanici)
            if kalan_kilit is not None:
                kalan_dk = max(1, int(kalan_kilit // 60) + 1)
                hata = f"🔒 Çok fazla hatalı deneme! Hesap {kalan_dk} dakika daha kilitli."
                return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if kullanici in kullanici_db:
                if sifre_dogrula(kullanici, sifre):
                    giris_denemesini_temizle(kullanici)
                    session["kullanici"] = kullanici
                    session.permanent = remember
                    son_aktiflik[kullanici] = time.time()
                    kullanici_oturum_son_kayit[kullanici] = time.time()
                    log_ekle(f"'{kullanici}' oturum açtı.")
                    return redirect("/")
                else:
                    kilitlendi = giris_hatali_deneme_kaydet(kullanici)
                    if kilitlendi:
                        hata = f"🔒 Çok fazla hatalı deneme! Hesap {GIRIS_KILIT_SANIYE // 60} dakika kilitlendi."
                    else:
                        kalan_hak = GIRIS_MAKS_DENEME - giris_hatali_deneme[kullanici]["sayi"]
                        hata = f"❌ Hatalı şifre girdiniz! ({kalan_hak} deneme hakkınız kaldı)"
                    return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            if kullanici in kullanici_db:
                if sifre_dogrula(kullanici, sifre):
                    giris_denemesini_temizle(kullanici)
                    session["kullanici"] = kullanici
                    session.permanent = remember
                    son_aktiflik[kullanici] = time.time()
                    kullanici_oturum_son_kayit[kullanici] = time.time()
                    log_ekle(f"'{kullanici}' oturum açtı.")
                    return redirect("/")
                else:
                    kilitlendi = giris_hatali_deneme_kaydet(kullanici)
                    if kilitlendi:
                        hata = f"🔒 Çok fazla hatalı deneme! Hesap {GIRIS_KILIT_SANIYE // 60} dakika kilitlendi."
                    else:
                        kalan_hak = GIRIS_MAKS_DENEME - giris_hatali_deneme[kullanici]["sayi"]
                        hata = f"❌ Hatalı şifre girdiniz! ({kalan_hak} deneme hakkınız kaldı)"
                    return render_template_string(giris_html, hata=hata, kod_gerekli=False, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

            gecerli, hata_mesaji = sifre_guclu_mu(sifre)
            if not gecerli:
                hata = hata_mesaji
                return render_template_string(giris_html, hata=hata, kod_gerekli=False, onay_mesaji=None, kullanici=form_kullanici, email=form_email)

            kullanici_db[kullanici] = sifre_hashle(sifre)
            kullanici_kayit_zamani[kullanici] = time.time()
            session["kullanici"] = kullanici
            session.permanent = remember
            son_aktiflik[kullanici] = time.time()
            kullanici_oturum_son_kayit[kullanici] = time.time()
            log_ekle(f"Yeni hesap oluşturuldu: '{kullanici}'")
            durumu_kaydet()
            return redirect("/")

        hata = "Lütfen tüm alanları doldurun."

    if bekleyen and bekleyen.get("kullanici"):
        kod_gerekli = True
        onay_mesaji = "Doğrulama kodu gönderildi. Lütfen e-postanıza bakın."

    return render_template_string(giris_html, hata=hata, kod_gerekli=kod_gerekli, onay_mesaji=onay_mesaji, kullanici=form_kullanici, email=form_email)

@app.route("/sifre-unuttum", methods=["GET", "POST"])
def sifre_unuttum():
    return render_template_string("""
    <!DOCTYPE html><html lang="tr"><head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Devre Dışı</title>
    <style>
    body{font-family:Segoe UI,Tahoma,sans-serif;background:#eaf2fa;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}
    .box{background:#fff;border:1px solid #b9cfe4;border-radius:8px;padding:24px;max-width:420px;text-align:center;box-shadow:0 12px 32px rgba(20,60,110,.2)}
    h2{margin:0 0 10px;color:#1c3d5c} p{margin:0;color:#33475c;line-height:1.5}
    a{display:inline-block;margin-top:14px;color:#1c5fb0;font-weight:700;text-decoration:none}
    </style></head><body><div class="box">
    <h2>Şifremi unuttum kapatıldı</h2>
    <p>E-posta ile işlem kaldırıldı. Şu an sadece kullanıcı adı ve şifre ile giriş yapılır.</p>
    <a href="/giris">Girişe dön</a>
    </div></body></html>
    """)

@app.route("/api/kullanicilar", methods=["GET"])

def get_kullanicilar():
    if "kullanici" in session:
        son_aktiflik[session["kullanici"]] = time.time()
    return jsonify(list(kullanici_db.keys()))

@app.route("/api/kullanici_bilgi", methods=["GET"])
def kullanici_bilgi():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Oturumunuz bulunmuyor."}), 403

    hedef = request.args.get("kullanici", "").strip()
    if not hedef or hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Geçersiz kullanıcı."}), 400

    if hedef in kullanici_renames:
        hedef = kullanici_renames[hedef]

    bilgi = kullanici_bilgi_hazirla(hedef)
    bilgi["basarili"] = True
    return jsonify(bilgi)

@app.route("/api/sikayet_mesajlari", methods=["GET"])
def sikayet_mesajlari():
    if "kullanici" not in session:
        return jsonify({"mesajlar": []}), 403
    hedef = request.args.get("kullanici", "").strip()
    mevcut = session.get("kullanici")
    if not hedef or hedef == mevcut:
        return jsonify({"mesajlar": []})
    sonuclar = [m for m in sohbet_gecmisi if m.get("gonderen") == hedef][-25:]
    return jsonify({"mesajlar": [{"id": m.get("id", str(i)), "mesaj": m.get("mesaj", ""), "zaman": m.get("zaman", 0), "oda": m.get("oda", "Genel")} for i, m in enumerate(sonuclar)]})

@app.route("/api/sikayet_olustur", methods=["POST"])
def sikayet_olustur():
    global sikayetler
    bildiren = session.get("kullanici")
    if not bildiren:
        return jsonify({"basarili": False, "hata": "Oturumunuz bulunmuyor."}), 403
    hedef = request.form.get("sikayet_edilen", "").strip()
    neden = request.form.get("neden", "").strip()
    aciklama = request.form.get("aciklama", "").strip()
    mesaj_id = request.form.get("mesaj_id", "").strip()
    oda = request.form.get("oda", "Genel").strip() or "Genel"

    if not hedef or hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Geçersiz kullanıcı."}), 400
    if hedef == bildiren:
        return jsonify({"basarili": False, "hata": "Kendi hesabınızı şikayet edemezsiniz."}), 400
    if neden not in SIKAYET_NEDENLERI:
        return jsonify({"basarili": False, "hata": "Geçersiz şikayet nedeni."}), 400
    if len(aciklama) < 30:
        return jsonify({"basarili": False, "hata": "Açıklama en az 30 karakter olmalıdır."}), 400
    aciklama = aciklama[:1000]

    simdi = time.time()
    with sikayet_kilidi:
        son_30dk = [x for x in sikayetler if x.get("bildiren") == bildiren and simdi - x.get("zaman", 0) < 1800]
        if len(son_30dk) >= SIKAYET_MAKS_30DK:
            return jsonify({"basarili": False, "hata": "Çok fazla şikayet gönderildi. Lütfen daha sonra tekrar deneyin."}), 429
        son_ayni = [x for x in sikayetler if x.get("bildiren") == bildiren and x.get("sikayet_edilen") == hedef and simdi - x.get("zaman", 0) < SIKAYET_AYNI_KISI_COOLDOWN]
        if son_ayni:
            return jsonify({"basarili": False, "hata": "Aynı kullanıcı için kısa süre içinde tekrar şikayet gönderemezsiniz."}), 429

        ilgili = next((m for m in sohbet_gecmisi if m.get("id") == mesaj_id and m.get("gonderen") == hedef), None) if mesaj_id else None
        kayit = {
            "id": secrets.token_hex(10),
            "bildiren": bildiren,
            "sikayet_edilen": hedef,
            "neden": neden,
            "aciklama": aciklama,
            "zaman": simdi,
            "oda": oda,
            "mesaj_id": mesaj_id,
            "ilgili_mesaj": ilgili.get("mesaj", "") if ilgili else "",
            "ilgili_mesaj_zamani": ilgili.get("zaman", 0) if ilgili else 0,
            "ilgili_mesaj_oda": ilgili.get("oda", oda) if ilgili else oda,
            "durum": "Yeni"
        }
        sikayetler.append(kayit)
        if len(sikayetler) > 1000:
            sikayetler = sikayetler[-1000:]
        sikayetleri_kaydet(sikayetler)

    log_ekle(f"Yeni şikayet: {bildiren} -> {hedef} ({neden})")
    return jsonify({"basarili": True})

@app.route("/api/odalar", methods=["GET"])
def get_odalar():
    return jsonify([{"ad": k, "kilitli": bool(v)} for k, v in odalar_db.items()])

@app.route("/api/oda_olustur", methods=["POST"])
def post_oda_olustur():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Önce giriş yapmalısınız."})

    kullanici = session["kullanici"]

    oda = request.form.get("oda", "").strip()
    sifre = request.form.get("sifre", "").strip()

    if not oda:
        return jsonify({"basarili": False, "hata": "Oda adı boş olamaz."})
        
    if len(oda) > 15:
        return jsonify({"basarili": False, "hata": "Oda adı çok uzun."})

    if len(sifre) > 15:
        return jsonify({"basarili": False, "hata": "Oda şifresi çok uzun."})

    with veri_kilidi:
        # Sayım da kilit içinde yapılıyor ki aynı anda gelen iki istek limiti aşamasın
        ozel_oda_sayisi = len([o for o in odalar_db.keys() if o != "Genel"])
        if ozel_oda_sayisi >= MAKS_OZEL_ODA:
            return jsonify({"basarili": False, "hata": f"En fazla {MAKS_OZEL_ODA} oda oluşturulabilir."})

        if oda in odalar_db:
            return jsonify({"basarili": False, "hata": "Bu isimde bir oda zaten var."})

        odalar_db[oda] = sifre
        oda_olustur_kaydi(oda, kullanici)

    log_ekle(f"Oda kuruldu: '{oda}' ({'Şifreli' if sifre else 'Şifresiz'}) - Lider: {kullanici}")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_izin_iste", methods=["POST"])
def post_oda_izin_iste():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Önce giriş yapmalısınız."})

    kullanici = session["kullanici"]

    if oda_kurma_yetkisi_var_mi(kullanici):
        return jsonify({"basarili": False, "hata": "Zaten oda kurma izniniz var."})

    with veri_kilidi:
        if kullanici in oda_izin_istekleri:
            return jsonify({"basarili": False, "hata": "Zaten bekleyen bir isteğiniz var, Sistem'in cevabını bekleyin."})
        oda_izin_istekleri[kullanici] = time.time()

    log_ekle(f"'{kullanici}' oda kurma izni istedi.")
    izin_istek_kuyrugu.put(kullanici)
    return jsonify({"basarili": True})

@app.route("/api/oda_kontrol", methods=["GET"])
def get_oda_kontrol():
    oda = request.args.get("oda", "Genel")
    kullanici = session.get("kullanici")
    # Odanın lideri (veya sistem Sistem'i) kendi odasına şifresiz girebilir
    kilitli = bool(odalar_db.get(oda, "")) and not oda_lideri_mi(oda, kullanici)
    return jsonify({"kilitli": kilitli})

@app.route("/api/oda_giris", methods=["POST"])
def post_oda_giris():
    oda = request.form.get("oda")
    sifre = request.form.get("sifre", "")
    kullanici = session.get("kullanici")
    if kullanici and oda_banli_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bu odadan atıldınız."})
    # Oda lideri (veya sistem Sistem'i) şifreyi bilmese de kendi odasına girebilir
    if kullanici and oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": True})
    if odalar_db.get(oda) == sifre:
        return jsonify({"basarili": True})
    return jsonify({"basarili": False})

@app.route("/api/oda_sifre_degistir", methods=["POST"])
def post_oda_sifre_degistir():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "Genel").strip()
    yeni_sifre = request.form.get("sifre", "").strip()

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    if not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Odanın şifresini yalnızca lider değiştirebilir."})

    if len(yeni_sifre) > 15:
        return jsonify({"basarili": False, "hata": "Oda şifresi çok uzun."})

    with veri_kilidi:
        odalar_db[oda] = yeni_sifre
    log_ekle(f"'{kullanici}', '{oda}' odasının şifresini değiştirdi.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_kapat", methods=["POST"])
def post_oda_kapat():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "").strip()

    if oda == "Genel":
        return jsonify({"basarili": False, "hata": "'Genel' odası kapatılamaz."})

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    # Yalnızca odanın lideri (odayı kuran/devralan kişi) veya sistem Sistem'i kapatabilir
    if not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bu odayı yalnızca lideri kapatabilir."})

    with veri_kilidi:
        odalar_db.pop(oda, None)
        oda_kaydini_sil(oda)
        # O odaya ait mesajları da temizleyelim (artık girilemeyen bir odaya ait kalmasınlar)
        sohbet_gecmisi[:] = [m for m in sohbet_gecmisi if m.get("oda") != oda]

    log_ekle(f"'{kullanici}', '{oda}' odasını kapattı.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_yetki", methods=["GET"])
def get_oda_yetki():
    if "kullanici" not in session:
        return jsonify({"hata": "Giriş yapmalısınız"}), 403

    kullanici = session["kullanici"]
    oda = request.args.get("oda", "Genel")

    uyeler = []
    for isim in kullanici_db.keys():
        if isim == "Sistem":
            continue
        uyeler.append({
            "isim": isim,
            "rol": oda_rolunu_al(oda, isim),
            "banli": oda_banli_mi(oda, isim),
            "ban_kalan_dk": oda_gecici_ban_kalan_dk(oda, isim)
        })

    return jsonify({
        "oda": oda,
        "rol": oda_rolunu_al(oda, kullanici),
        "lider": oda_liderleri.get(oda, ""),
        "lider_mi": oda_lideri_mi(oda, kullanici),
        "yonetebilir_mi": oda_yonetebilir_mi(oda, kullanici),
        "oda_kurma_izni_var_mi": oda_kurma_yetkisi_var_mi(kullanici),
        "uyeler": uyeler
    })

@app.route("/api/oda_rol_ver", methods=["POST"])
def post_oda_rol_ver():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "Genel").strip()
    hedef = request.form.get("hedef", "").strip()
    rol = request.form.get("rol", "uye").strip()

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    # Rol atama yetkisi yalnızca odanın liderinde (veya sistem Sistem'inde) olsun
    if not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bu işlem için odanın lideri olmanız gerekir."})

    if hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Kullanıcı bulunamadı."})

    if hedef == oda_liderleri.get(oda):
        return jsonify({"basarili": False, "hata": "Odanın liderinin rolü buradan değiştirilemez."})

    if rol not in ("yonetici", "uye"):
        return jsonify({"basarili": False, "hata": "Geçersiz rol."})

    with veri_kilidi:
        oda_roller.setdefault(oda, {})
        if rol == "uye":
            oda_roller[oda].pop(hedef, None)
        else:
            oda_roller[oda][hedef] = rol

    log_ekle(f"'{kullanici}', '{oda}' odasında '{hedef}' kullanıcısına '{rol}' rolü verdi.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_kick", methods=["POST"])
def post_oda_kick():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "Genel").strip()
    hedef = request.form.get("hedef", "").strip()

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    if not oda_yonetebilir_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bu odayı yönetme yetkiniz yok."})

    if oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Oda sahibi bu işlemi yapamaz."})

    if oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Oda sahibi bu işlemi yapamaz."})

    if hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Kullanıcı bulunamadı."})

    if hedef == oda_liderleri.get(oda):
        return jsonify({"basarili": False, "hata": "Oda liderini atamazsınız."})

    # Yöneticiler yalnızca sıradan üyeleri atabilir; sadece lider (veya Sistem) diğer yöneticileri de atabilir
    if oda_rolunu_al(oda, hedef) == "yonetici" and not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bir yöneticiyi ancak lider odadan atabilir."})

    with veri_kilidi:
        oda_yasaklari.setdefault(oda, [])
        if hedef not in oda_yasaklari[oda]:
            oda_yasaklari[oda].append(hedef)
        oda_roller.setdefault(oda, {}).pop(hedef, None)

    log_ekle(f"'{kullanici}', '{oda}' odasından '{hedef}' kullanıcısını attı.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_ban", methods=["POST"])
def post_oda_ban():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "Genel").strip()
    hedef = request.form.get("hedef", "").strip()

    try:
        dakika = float(request.form.get("dakika", "0"))
    except ValueError:
        dakika = 0

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    if not oda_yonetebilir_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bu odayı yönetme yetkiniz yok."})

    if oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Oda sahibi bu işlemi yapamaz."})

    if hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Kullanıcı bulunamadı."})

    if hedef == oda_liderleri.get(oda):
        return jsonify({"basarili": False, "hata": "Oda liderini banlayamazsınız."})

    # Yöneticiler yalnızca sıradan üyeleri banlayabilir; yöneticileri ancak lider (veya Sistem) banlayabilir
    if oda_rolunu_al(oda, hedef) == "yonetici" and not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Bir yöneticiyi ancak lider banlayabilir."})

    if dakika <= 0:
        return jsonify({"basarili": False, "hata": "Geçerli bir süre (dakika) girin."})
    if dakika > 10080:  # 7 gün üst sınır
        dakika = 10080

    with veri_kilidi:
        oda_gecici_banlar.setdefault(oda, {})[hedef] = time.time() + (dakika * 60)
        oda_roller.setdefault(oda, {}).pop(hedef, None)
        engellenenler.add(hedef)
        hedef_email = kullanici_emaili_al(hedef)
        if hedef_email:
            banli_emailler.add(hedef_email)
    log_ekle(f"'{kullanici}', '{oda}' odasında '{hedef}' kullanıcısını {int(dakika)} dakikalığına banladı ve site genelinde engelledi.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_lider_yap", methods=["POST"])
def post_oda_lider_yap():
    if "kullanici" not in session:
        return jsonify({"basarili": False, "hata": "Giriş yapmalısınız."})

    kullanici = session["kullanici"]
    oda = request.form.get("oda", "Genel").strip()
    hedef = request.form.get("hedef", "").strip()

    if oda not in odalar_db:
        return jsonify({"basarili": False, "hata": "Oda bulunamadı."})

    if not oda_lideri_mi(oda, kullanici):
        return jsonify({"basarili": False, "hata": "Sadece odanın lideri, liderliği devredebilir."})

    if hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Kullanıcı bulunamadı."})

    if oda_banli_mi(oda, hedef):
        return jsonify({"basarili": False, "hata": "Bu odadan atılmış bir kullanıcı lider yapılamaz."})

    with veri_kilidi:
        eski_lider = oda_liderleri.get(oda)
        oda_liderleri[oda] = hedef
        oda_roller.setdefault(oda, {}).pop(hedef, None)
        if eski_lider and eski_lider != hedef and eski_lider != "Sistem":
            oda_roller.setdefault(oda, {})[eski_lider] = "yonetici"

    log_ekle(f"'{kullanici}', '{oda}' odasının liderliğini '{hedef}' kullanıcısına devretti.")
    durumu_kaydet()
    return jsonify({"basarili": True})

@app.route("/api/oda_kurma_izni_ver", methods=["POST"])
def post_oda_kurma_izni_ver():
    if session.get("kullanici") != "Sistem":
        return jsonify({"basarili": False, "hata": "Bu işlem yalnızca sistem Sistem'i tarafından yapılabilir."})

    hedef = request.form.get("hedef", "").strip()
    izin_ver = request.form.get("izin", "1") == "1"

    if hedef not in kullanici_db:
        return jsonify({"basarili": False, "hata": "Kullanıcı bulunamadı."})

    with veri_kilidi:
        if izin_ver:
            oda_kurma_izni.add(hedef)
        else:
            oda_kurma_izni.discard(hedef)

    log_ekle(f"'{hedef}' kullanıcısının oda kurma izni {'verildi' if izin_ver else 'alındı'}.")
    durumu_kaydet()
    return jsonify({"basarili": True, "izinli": hedef in oda_kurma_izni})

@app.route("/api/mesajlar", methods=["GET"])
def get_mesajlar():
    kullanici = session.get("kullanici")
    aktif_oda = request.args.get("oda", "Genel")
    
    if kullanici:
        son_aktiflik[kullanici] = time.time()
        
    filtrelenmis = []
    for m in sohbet_gecmisi:
        alici = m.get("alici", "Genel")
        oda = m.get("oda", "Genel")
        
        is_global = m.get("gonderen") in ["📢 DUYURU", "📢 SAYAÇ", "📢 ALARM"]
        is_dm = (alici == kullanici or m.get("gonderen") == kullanici) and alici != "Genel"
        
        if is_global or is_dm or (oda == aktif_oda and alici == "Genel"):
            filtrelenmis.append(m)

    oda_banli = oda_banli_mi(aktif_oda, kullanici) if kullanici else False
    return jsonify({"mesajlar": filtrelenmis, "sabit_duyuru": sabit_duyuru, "oda_banli": oda_banli})

@app.route("/api/mesaj_sil", methods=["POST"])
def post_mesaj_sil():
    kullanici = session.get("kullanici")
    if not kullanici:
        return jsonify({"basarili": False, "hata": "Oturumunuz bulunmuyor."}), 403

    mesaj_id = request.form.get("mesaj_id", "").strip()
    if not mesaj_id:
        return jsonify({"basarili": False, "hata": "Geçersiz mesaj."}), 400

    with veri_kilidi:
        bulunan = None
        for m in sohbet_gecmisi:
            if str(m.get("id", "")) == mesaj_id:
                bulunan = m
                break

        if not bulunan:
            return jsonify({"basarili": False, "hata": "Mesaj bulunamadı."}), 404

        if bulunan.get("gonderen") != kullanici:
            return jsonify({"basarili": False, "hata": "Sadece kendi mesajınızı silebilirsiniz."}), 403

        # Duyuru/sistem mesajları normal kullanıcı mesajı olarak silinemez.
        if bulunan.get("gonderen") in ["Sistem", "📢 DUYURU", "📢 SAYAÇ", "📢 ALARM"]:
            return jsonify({"basarili": False, "hata": "Bu mesaj silinemez."}), 403

        sohbet_gecmisi.remove(bulunan)
        try:
            guncel_veriler = dict(veriler)
            guncel_veriler["sohbet_gecmisi"] = list(sohbet_gecmisi)
            verileri_kaydet(guncel_veriler)
        except Exception as e:
            # Bellekten de silindi; kalıcı kayıt başarısızsa kullanıcıya bilgi ver.
            return jsonify({"basarili": False, "hata": f"Mesaj silindi ancak kayıt güncellenemedi: {e}"}), 500

    log_ekle(f"'{kullanici}' kendi mesajını sildi: {mesaj_id}")
    return jsonify({"basarili": True})

@app.route("/api/yaziyor", methods=["POST"])
def post_yaziyor():
    if "kullanici" in session:
        yaziyor_durumu[session["kullanici"]] = time.time()
        son_aktiflik[session["kullanici"]] = time.time()
    return "OK", 200

@app.route("/api/yaziyor_durumu", methods=["GET"])
def get_yaziyor_durumu():
    simdi = time.time()
    mevcut_kullanici = session.get("kullanici")
    if mevcut_kullanici:
        son_aktiflik[mevcut_kullanici] = simdi
    yazanlar = [k for k, t in yaziyor_durumu.items() if simdi - t < 2.5 and k != mevcut_kullanici]
    return jsonify({"yazanlar": yazanlar})

@app.route("/api/gonder", methods=["POST"])
def post_gonder():
    if "kullanici" in session:
        kullanici = session["kullanici"]
        simdi = time.time()
        son_aktiflik[kullanici] = simdi
        
        if kullanici not in kullanici_db:
            session.pop("kullanici", None)
            return "Hesabınız silindi veya oturumunuz kapatıldı.", 403
            
        oda = request.form.get("oda", "Genel").strip()

        if oda_banli_mi(oda, kullanici):
            return "Bu odadan atıldınız.", 403

        if yavas_mod_saniye > 0 and kullanici != "Sistem":
            son_z = son_mesaj_zamani.get(kullanici, 0)
            fark = simdi - son_z
            if fark < yavas_mod_saniye:
                kalan = int(yavas_mod_saniye - fark) + 1
                return f"Yavaş Mod aktif! Lütfen {kalan} saniye bekleyin.", 403

        if kullanici in susturulanlar:
            mute_bilgi = susturulanlar[kullanici]
            bitis = mute_bilgi if isinstance(mute_bilgi, float) else mute_bilgi.get("bitis", 0)
            mute_oda = "Hepsi" if isinstance(mute_bilgi, float) else mute_bilgi.get("oda", "Hepsi")
            if simdi < bitis:
                if mute_oda == "Hepsi" or mute_oda == oda:
                    kalan_dk = max(1, int((bitis - simdi) // 60) + 1)
                    return f"Susturuldunuz! Kalan süre: {kalan_dk} dakika", 403
            else:
                susturulanlar.pop(kullanici, None)

        mesaj = request.form.get("mesaj", "").strip()[:200]  # istemcideki maxlength=200 ile aynı sınır, sunucu tarafında da uygulanır
        alici = request.form.get("alici", "Genel").strip()

        if kufur_filtresi and kullanici != "Sistem":
            mesaj = kufur_filtrele(mesaj)

        if mesaj:
            veri = {"id": secrets.token_hex(8), "gonderen": kullanici, "mesaj": mesaj, "alici": alici, "oda": oda, "zaman": simdi}
            reply_id = request.form.get("reply_id", "").strip()
            reply_gonderen = request.form.get("reply_gonderen", "").strip()
            reply_mesaj = request.form.get("reply_mesaj", "").strip()
            reply_alici = request.form.get("reply_alici", "").strip()
            reply_oda = request.form.get("reply_oda", "").strip()
            if reply_id or reply_gonderen or reply_mesaj:
                veri["reply_to"] = {
                    "id": reply_id,
                    "gonderen": reply_gonderen,
                    "mesaj": reply_mesaj,
                    "alici": reply_alici,
                    "oda": reply_oda
                }
            sohbet_gecmisi.append(veri)
            mesaj_kuyrugu.put(veri)
            yaziyor_durumu.pop(kullanici, None)
            son_mesaj_zamani[kullanici] = simdi

    return "OK", 200

def mesaj_goster(veri):
    kullanici = veri.get("gonderen")
    mesaj = veri.get("mesaj", "")
    alici = veri.get("alici", "Genel")
    oda = veri.get("oda", "Genel")

    if winsound:
        try:
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass

    pencere = tk.Toplevel(root)
    pencere.title("Sistem Uyarısı")
    pencere.timer_id = None

    bg_color = "#F3F7FB"
    pencere.attributes("-topmost", True)
    pencere.overrideredirect(True)
    pencere.configure(bg=bg_color)

    cerceve_dis = tk.Frame(pencere, bg="black", bd=0)
    cerceve_dis.pack(fill="both", expand=True)

    cerceve_ic = tk.Frame(cerceve_dis, bg=bg_color, bd=0, relief="flat")
    cerceve_ic.pack(fill="both", expand=True, padx=2, pady=2)

    baslik_cubugu = tk.Frame(cerceve_ic, bg="#111827", height=34)
    baslik_cubugu.pack(fill="x", side="top")

    ust_container = tk.Frame(cerceve_ic, bg=bg_color)
    ust_container.pack(fill="x", expand=True, padx=12, pady=(10, 5))

    kimden_yazisi = f"[{oda}] {kullanici} ➔ {alici}:" if alici != "Genel" else f"[{oda}] {kullanici}:"

    tk.Label(ust_container, text=kimden_yazisi, font=("Arial", 12, "bold"), bg=bg_color, fg="black").pack(side="left", anchor="nw")
    tk.Label(ust_container, text=mesaj, font=("Arial", 12), bg=bg_color, fg="black", anchor="nw", justify="left", wraplength=260).pack(side="left", anchor="nw", fill="both", expand=True, padx=(5, 5))

    btn_container = tk.Frame(ust_container, bg=bg_color)
    btn_container.pack(side="right", anchor="ne")

    def kullanici_banla():
        kullanici_banla_ve_email(kullanici)
        log_ekle(f"'{kullanici}' hızlı bildirimden banlandı.")
        pencere.destroy()

    ban_btn = tk.Button(
        btn_container,
        text="⛔ Banla",
        font=("Arial", 9, "bold"),
        bg="#AA0000",
        fg="white",
        bd=2,
        relief="raised",
        command=kullanici_banla
    )
    ban_btn.pack(side="top", pady=(0, 4))

    cevap_buton = tk.Button(
        btn_container,
        text="Cevapla",
        font=("Arial", 9, "bold"),
        bg="#CCCCCC",
        fg="black",
        bd=2,
        relief="raised"
    )
    cevap_buton.pack(side="top")

    alt_panel = tk.Frame(cerceve_ic, bg=bg_color)
    cevap_giris = tk.Entry(alt_panel, font=("Arial", 11), bd=2, relief="sunken")
    cevap_giris.pack(side="left", fill="x", expand=True, padx=(12, 5), pady=10)

    def cevabi_gonder(event=None):
        yanit = cevap_giris.get().strip()
        if yanit:
            hedef = kullanici if alici != "Genel" else "Genel"
            sohbet_gecmisi.append({"gonderen": "Sistem", "mesaj": yanit, "alici": hedef, "oda": oda, "zaman": time.time()})
            pencere.destroy()

    cevap_giris.bind("<Return>", cevabi_gonder)
    tk.Button(alt_panel, text="Gönder", font=("Arial", 10, "bold"), bg="#CCCCCC", bd=2, relief="raised", command=cevabi_gonder).pack(side="right", padx=(0, 12), pady=10)

    geri_panel = tk.Frame(cerceve_ic, bg=bg_color)
    tk.Label(geri_panel, text="Neler oldu?", font=("Arial", 9, "bold"), bg=bg_color).pack(anchor="w", padx=12, pady=(10, 4))
    geri_metin = tk.Text(geri_panel, height=4, font=("Arial", 10), bd=2, relief="sunken", wrap="word")
    geri_metin.pack(fill="x", padx=12)
    geri_buton_satir = tk.Frame(geri_panel, bg=bg_color)
    geri_buton_satir.pack(fill="x", padx=12, pady=10)

    def geri_bildirim_gonder(event=None):
        icerik = geri_metin.get("1.0", "end").strip()
        if not icerik:
            return
        with veri_kilidi:
            geri_bildirimler.append({
                "kullanici": kullanici,
                "oda": oda,
                "mesaj": icerik,
                "zaman": time.time()
            })
            durumu_kaydet()
        log_ekle(f"'{kullanici}' geri bildirim gönderdi.")
        pencere.destroy()

    tk.Button(geri_buton_satir, text="Gönder", font=("Arial", 10, "bold"), bg="#2563eb", fg="white", bd=2, relief="raised", command=geri_bildirim_gonder).pack(side="right")
    tk.Button(geri_buton_satir, text="İptal", font=("Arial", 10, "bold"), bg="#CCCCCC", bd=2, relief="raised", command=pencere.destroy).pack(side="right", padx=(0, 8))

    def cevapla_tiklandi():
        if pencere.timer_id:
            pencere.after_cancel(pencere.timer_id)
            pencere.timer_id = None

        geri_panel.pack_forget()
        btn_container.pack_forget()
        alt_panel.pack(fill="x", side="bottom")

        pencere.update_idletasks()
        y_yeni = ekran_yukseklik - (cerceve_ic.winfo_reqheight() + 30) - 150
        pencere.geometry(f"440x{cerceve_ic.winfo_reqheight() + 10}+{x}+{y_yeni}")
        cevap_giris.focus_set()

    cevap_buton.configure(command=cevapla_tiklandi)

    ekran_genislik = root.winfo_screenwidth()
    ekran_yukseklik = root.winfo_screenheight()

    pencere_genislik = 440
    pencere.geometry(f"{pencere_genislik}x110")
    pencere.update_idletasks()

    pencere_yukseklik = cerceve_ic.winfo_reqheight() + 15
    x = ekran_genislik - pencere_genislik - 20
    y = ekran_yukseklik - pencere_yukseklik - 150

    pencere.geometry(f"{pencere_genislik}x{pencere_yukseklik}+{x}+{y}")
    pencere.timer_id = pencere.after(7000, lambda: pencere.destroy() if pencere.winfo_exists() else None)

def izin_istek_goster(kullanici):
    """Oda izin isteği kaydı tutulur; sağ alt köşe bildirimi kapatıldı."""
    return

def sistem_yazma_penceresi():
    global _son_sistem_acilis
    simdi = time.time()
    if simdi - _son_sistem_acilis < 0.4:
        return
    _son_sistem_acilis = simdi

    sistem_win = tk.Toplevel(root)
    sistem_win.title("Sistem Hızlı Mesaj")
    sistem_win.attributes("-topmost", True)
    sistem_win.overrideredirect(True)
    bg_color = "#F3F7FB"
    sistem_win.configure(bg=bg_color)

    cerceve_dis = tk.Frame(sistem_win, bg="black", bd=0)
    cerceve_dis.pack(fill="both", expand=True)

    cerceve_ic = tk.Frame(cerceve_dis, bg=bg_color, bd=0, relief="flat")
    cerceve_ic.pack(fill="both", expand=True, padx=2, pady=2)

    baslik_cubugu = tk.Frame(cerceve_ic, bg="#111827", height=34)
    baslik_cubugu.pack(fill="x", side="top")

    kapat_btn = tk.Button(baslik_cubugu, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=sistem_win.destroy)
    kapat_btn.pack(side="right", padx=4, pady=2)

    alt_panel = tk.Frame(cerceve_ic, bg=bg_color)
    alt_panel.pack(fill="x", side="bottom", pady=10)
    
    tk.Label(alt_panel, text="Sistem:", font=("Arial", 11, "bold"), bg=bg_color, fg="black").pack(side="left", padx=(12, 5))

    cevap_giris = tk.Entry(alt_panel, font=("Arial", 11), bd=2, relief="sunken")
    cevap_giris.pack(side="left", fill="x", expand=True, padx=(0, 5))

    def cevabi_gonder(event=None):
        yanit = cevap_giris.get().strip()
        if yanit:
            sohbet_gecmisi.append({"gonderen": "Sistem", "mesaj": yanit, "alici": "Genel", "oda": "Genel", "zaman": time.time()})
        sistem_win.destroy()

    cevap_giris.bind("<Return>", lambda e: cevabi_gonder())
    sistem_win.bind("<Escape>", lambda e: sistem_win.destroy())

    tk.Button(alt_panel, text="Gönder", font=("Arial", 10, "bold"), bg="#CCCCCC", bd=2, relief="raised", command=cevabi_gonder).pack(side="right", padx=(0, 12))

    sistem_win.update_idletasks()
    pencere_genislik = 440
    pencere_yukseklik = cerceve_ic.winfo_reqheight() + 15
    
    ekran_genislik = root.winfo_screenwidth()
    ekran_yukseklik = root.winfo_screenheight()
    
    x = ekran_genislik - pencere_genislik - 20
    y = ekran_yukseklik - pencere_yukseklik - 150
    
    sistem_win.geometry(f"{pencere_genislik}x{pencere_yukseklik}+{x}+{y}")
    cevap_giris.focus_set()

def ghost_mode_chat(ilk_isim):
    chat_win = tk.Toplevel(root)
    chat_win.title("Ghost Mode")
    chat_win.attributes("-topmost", True)
    chat_win.overrideredirect(True)
    bg_color = "#F3F7FB"
    chat_win.configure(bg=bg_color)

    c_dis = tk.Frame(chat_win, bg="black", bd=0)
    c_dis.pack(fill="both", expand=True)

    c_ic = tk.Frame(c_dis, bg=bg_color, bd=2, relief="flat")
    c_ic.pack(fill="both", expand=True, padx=2, pady=2)

    baslik = tk.Frame(c_ic, bg="black", height=22)
    baslik.pack(fill="x", side="top")
    tk.Label(baslik, text="👻 System 7 - Ghost Control Center", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=6)
    
    tk.Button(baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=chat_win.destroy).pack(side="right", padx=4, pady=2)
    
    def pencereyi_tasi(event):
        chat_win.geometry(f"+{event.x_root - chat_win._offset_x}+{event.y_root - chat_win._offset_y}")

    def pozisyon_al(event):
        chat_win._offset_x = event.x
        chat_win._offset_y = event.y

    baslik.bind("<Button-1>", pozisyon_al)
    baslik.bind("<B1-Motion>", pencereyi_tasi)

    top_ctrl = tk.Frame(c_ic, bg=bg_color)
    top_ctrl.pack(fill="x", padx=8, pady=(6, 2))

    tk.Label(top_ctrl, text="👻 Kimlik:", bg=bg_color, font=("Arial", 9, "bold")).pack(side="left")
    isim_entry = tk.Entry(top_ctrl, font=("Arial", 9, "bold"), bd=2, relief="sunken", width=12)
    isim_entry.insert(0, ilk_isim)
    isim_entry.pack(side="left", padx=(3, 8))

    tk.Label(top_ctrl, text="🏠 Oda:", bg=bg_color, font=("Arial", 9, "bold")).pack(side="left")
    oda_entry = tk.Entry(top_ctrl, font=("Arial", 9, "bold"), bd=2, relief="sunken", width=12)
    oda_entry.insert(0, "Genel")
    oda_entry.pack(side="left", padx=(3, 0))

    chat_container = tk.Frame(c_ic, bg=bg_color)
    chat_container.pack(fill="both", expand=True, padx=8, pady=4)
    
    chat_scroll = tk.Scrollbar(chat_container)
    chat_scroll.pack(side="right", fill="y")
    
    chat_text = tk.Text(chat_container, width=52, height=14, font=("Consolas", 9), bd=2, relief="sunken", yscrollcommand=chat_scroll.set, state="disabled", bg="#FFFFFF")
    chat_text.pack(side="left", fill="both", expand=True)
    chat_scroll.config(command=chat_text.yview)

    preset_frame = tk.Frame(c_ic, bg=bg_color)
    preset_frame.pack(fill="x", padx=8, pady=2)

    def preset_at(metin):
        msg_giris.delete(0, tk.END)
        msg_giris.insert(0, metin)
        mesaj_yolla()

    tk.Button(preset_frame, text="👋 Selam", font=("Arial", 8, "bold"), bg="#BBB", command=lambda: preset_at("Selam millet!")).pack(side="left", padx=2)
    tk.Button(preset_frame, text="⚠️ Dikkat", font=("Arial", 8, "bold"), bg="#BBB", command=lambda: preset_at("⚠️ Kurallara uyalım lütfen.")).pack(side="left", padx=2)
    tk.Button(preset_frame, text="🤖 Bot Mesajı", font=("Arial", 8, "bold"), bg="#BBB", command=lambda: preset_at("🤖 Sistem otomatik mesajıdır.")).pack(side="left", padx=2)

    alt_panel = tk.Frame(c_ic, bg=bg_color)
    alt_panel.pack(fill="x", side="bottom", padx=8, pady=(4, 8))

    msg_giris = tk.Entry(alt_panel, font=("Arial", 10), bd=2, relief="sunken")
    msg_giris.pack(side="left", fill="x", expand=True, padx=(0, 6))
    
    def mesaj_yolla(event=None):
        metin = msg_giris.get().strip()
        secilen_isim = isim_entry.get().strip() or "Ghost"
        secilen_oda = oda_entry.get().strip() or "Genel"
        if metin:
            sohbet_gecmisi.append({"gonderen": secilen_isim, "mesaj": metin, "alici": "Genel", "oda": secilen_oda, "zaman": time.time()})
            msg_giris.delete(0, tk.END)
            guncelle(zorla=True)

    msg_giris.bind("<Return>", mesaj_yolla)
    tk.Button(alt_panel, text="Gönder 👻", font=("Arial", 9, "bold"), bg="#444", fg="white", bd=2, relief="raised", command=mesaj_yolla).pack(side="right")
    
    son_mesaj_sayisi = [0]
    
    def guncelle(zorla=False):
        if not chat_win.winfo_exists(): return
        
        if zorla or len(sohbet_gecmisi) != son_mesaj_sayisi[0]:
            chat_text.config(state="normal")
            chat_text.delete("1.0", tk.END)
            simdi = time.time()
            for m in sohbet_gecmisi:
                gonderen = m.get("gonderen", "Bilinmeyen")
                alici = m.get("alici", "Genel")
                oda = m.get("oda", "Genel")
                hedef = f"➔{alici}" if alici != "Genel" else ""
                
                if gonderen == "📢 SAYAÇ":
                    kalan = max(0, int(m.get("bitis_zamani", 0) - simdi))
                    dk, sn = kalan // 60, kalan % 60
                    mesaj = f"⏱️ {dk:02d}:{sn:02d}" + (" (Bitti)" if kalan == 0 else "")
                    chat_text.insert(tk.END, f"[{oda}] {mesaj}\n")
                else:
                    mesaj = m.get("mesaj", "")
                    chat_text.insert(tk.END, f"[{oda}] [{gonderen}{hedef}]: {mesaj}\n")
            
            chat_text.see(tk.END)
            chat_text.config(state="disabled")
            son_mesaj_sayisi[0] = len(sohbet_gecmisi)
            
        chat_win.after(800, guncelle)

    guncelle(zorla=True)
    
    chat_win.update_idletasks()
    cw, ch = 520, 380
    eg, ey = root.winfo_screenwidth(), root.winfo_screenheight()
    chat_win.geometry(f"{cw}x{ch}+{(eg//2)-(cw//2)}+{(ey//2)-(ch//2)}")
    msg_giris.focus_set()

def ghost_mode_isteme():
    prompt = tk.Toplevel(root)
    prompt.title("Ghost Mode Login")
    prompt.attributes("-topmost", True)
    prompt.overrideredirect(True)
    bg_color = "#F3F7FB"
    prompt.configure(bg=bg_color)

    p_dis = tk.Frame(prompt, bg="black", bd=0)
    p_dis.pack(fill="both", expand=True)
    
    p_ic = tk.Frame(p_dis, bg=bg_color, bd=2, relief="flat")
    p_ic.pack(fill="both", expand=True, padx=2, pady=2)

    baslik = tk.Frame(p_ic, bg="black", height=20)
    baslik.pack(fill="x", side="top")
    tk.Label(baslik, text="👻 Ghost Mode Giriş", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
    tk.Button(baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=prompt.destroy).pack(side="right", padx=4, pady=2)

    icerik = tk.Frame(p_ic, bg=bg_color)
    icerik.pack(fill="both", expand=True, padx=10, pady=10)

    tk.Label(icerik, text="Hangi İsimle Takılacaksın?:", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w", pady=(0, 5))
    isim_giris = tk.Entry(icerik, font=("Arial", 11), bd=2, relief="sunken")
    isim_giris.insert(0, "GhostUser")
    isim_giris.pack(fill="x", pady=(0, 10))
    isim_giris.focus_set()

    def giris_yap(event=None):
        isim = isim_giris.get().strip() or "GhostUser"
        prompt.destroy()
        ghost_mode_chat(isim)

    isim_giris.bind("<Return>", giris_yap)
    tk.Button(icerik, text="Ghost Ekranını Aç 🚀", font=("Arial", 10, "bold"), bg="#333", fg="white", bd=2, relief="raised", command=giris_yap).pack(fill="x")
    
    prompt.update_idletasks()
    pw, ph = 300, 140
    eg, ey = root.winfo_screenwidth(), root.winfo_screenheight()
    prompt.geometry(f"{pw}x{ph}+{(eg//2)-(pw//2)}+{(ey//2)-(ph//2)}")

def sistem_sikayetler_penceresi(parent):
    if hasattr(root, "sikayet_penceresi") and root.sikayet_penceresi.winfo_exists():
        root.sikayet_penceresi.lift(); return

    win = tk.Toplevel(parent)
    root.sikayet_penceresi = win
    win.title("Şikayetler")
    win.attributes("-topmost", True)
    win.overrideredirect(True)
    bg = "#DDDDDD"
    win.configure(bg=bg)

    outer = tk.Frame(win, bg="black"); outer.pack(fill="both", expand=True)
    inner = tk.Frame(outer, bg=bg, bd=2); inner.pack(fill="both", expand=True, padx=2, pady=2)
    title = tk.Frame(inner, bg="black", height=20); title.pack(fill="x")
    tk.Label(title, text="⚠️ Şikayetler", bg="black", fg="white", font=("Arial",9,"bold")).pack(side="left", padx=5)
    tk.Button(title, text="✕", font=("Arial",8,"bold"), bg="#CC0000", fg="white", bd=0, command=win.destroy).pack(side="right", padx=4, pady=2)

    body = tk.Frame(inner, bg=bg); body.pack(fill="both", expand=True, padx=8, pady=8)
    left = tk.Frame(body, bg=bg); left.pack(side="left", fill="y")
    tk.Label(left, text="Gelen Şikayetler", bg=bg, font=("Arial",10,"bold")).pack(anchor="w")
    lc = tk.Frame(left,bg=bg); lc.pack(fill="both",expand=True,pady=5)
    scroll = tk.Scrollbar(lc); scroll.pack(side="right",fill="y")
    liste = tk.Listbox(lc, width=45, height=18, font=("Arial",9), bd=2, relief="sunken", yscrollcommand=scroll.set)
    liste.pack(side="left",fill="both",expand=True); scroll.config(command=liste.yview)

    right = tk.Frame(body,bg=bg); right.pack(side="left",fill="both",expand=True,padx=(10,0))
    tk.Label(right,text="Şikayet Detayı",bg=bg,font=("Arial",10,"bold")).pack(anchor="w")
    detail = tk.Text(right,width=52,height=16,font=("Arial",9),bd=2,relief="sunken",wrap="word",state="disabled")
    detail.pack(fill="both",expand=True,pady=5)

    action = tk.Frame(right,bg=bg); action.pack(fill="x")
    durum_var = tk.StringVar(value="Yeni")
    tk.Label(action,text="Durum:",bg=bg,font=("Arial",9,"bold")).pack(side="left")
    durum_menu = tk.OptionMenu(action,durum_var,"Yeni","İnceleniyor","Çözüldü")
    durum_menu.config(font=("Arial",9,"bold"),bd=2); durum_menu.pack(side="left",padx=5)

    secili = {"index": None}

    def temizle_detay():
        detail.config(state="normal"); detail.delete("1.0",tk.END); detail.config(state="disabled")

    def yenile():
        liste.delete(0,tk.END)
        with sikayet_kilidi:
            mevcut = list(sikayetler)
        for x in reversed(mevcut):
            zaman = time.strftime("%d.%m.%Y %H:%M", time.localtime(x.get("zaman",0)))
            liste.insert(tk.END, f"[{x.get('durum','Yeni')}] {x.get('bildiren','?')} → {x.get('sikayet_edilen','?')} | {x.get('neden','?')} | {zaman}")
        temizle_detay()
        secili["index"] = None

    def secim(event=None):
        sel=liste.curselection()
        if not sel: return
        idx=len(sikayetler)-1-sel[0]
        if idx<0 or idx>=len(sikayetler): return
        secili["index"]=idx
        x=sikayetler[idx]
        durum_var.set(x.get("durum","Yeni"))
        zaman=time.strftime("%d.%m.%Y %H:%M:%S",time.localtime(x.get("zaman",0)))
        metin=(f"Bildiren: {x.get('bildiren','')}\n"
               f"Şikayet edilen: {x.get('sikayet_edilen','')}\n"
               f"Neden: {x.get('neden','')}\n"
               f"Tarih/Saat: {zaman}\n"
               f"Oda: {x.get('oda','Genel')}\n\n"
               f"Açıklama:\n{x.get('aciklama','')}\n\n"
               f"İlgili mesaj:\n{x.get('ilgili_mesaj','(Mesaj eklenmemiş)')}")
        detail.config(state="normal"); detail.delete("1.0",tk.END); detail.insert("1.0",metin); detail.config(state="disabled")

    def durum_kaydet():
        i=secili.get("index")
        if i is None: return
        with sikayet_kilidi:
            if 0<=i<len(sikayetler):
                sikayetler[i]["durum"]=durum_var.get(); sikayetleri_kaydet(sikayetler)
        yenile()

    def hedefi_al():
        i=secili.get("index")
        if i is None: return None
        return sikayetler[i].get("sikayet_edilen")

    def sustur():
        hedef=hedefi_al()
        if not hedef or hedef=="Sistem": return
        dakika=tk.simpledialog.askinteger("Sustur", f"{hedef} kaç dakika susturulsun?", minvalue=1, maxvalue=10080, parent=win)
        if dakika:
            susturulanlar[hedef]={"bitis":time.time()+dakika*60,"oda":"Hepsi"}
            log_ekle(f"Şikayet üzerinden '{hedef}' {dakika} dakika susturuldu."); durumu_kaydet()

    def kick():
        hedef=hedefi_al()
        if not hedef or hedef=="Sistem": return
        zorla_cikis.add(hedef); log_ekle(f"Şikayet üzerinden '{hedef}' kicklendi."); durumu_kaydet()

    def ban():
        hedef=hedefi_al()
        if not hedef or hedef=="Sistem": return
        kullanici_banla_ve_email(hedef); log_ekle(f"Şikayet üzerinden '{hedef}' banlandı."); durumu_kaydet()

    tk.Button(action,text="💾 Durumu Kaydet",font=("Arial",8,"bold"),bg="#0066CC",fg="white",command=durum_kaydet).pack(side="right",padx=2)
    tk.Button(action,text="🔇 Sustur",font=("Arial",8,"bold"),bg="#5555AA",fg="white",command=sustur).pack(side="right",padx=2)
    tk.Button(action,text="👢 Uzaklaştır",font=("Arial",8,"bold"),bg="#FF5500",fg="white",command=kick).pack(side="right",padx=2)
    tk.Button(action,text="⛔ Engelle",font=("Arial",8,"bold"),bg="#AA0000",fg="white",command=ban).pack(side="right",padx=2)

    liste.bind("<<ListboxSelect>>", secim)
    yenile()
    win.update_idletasks()
    win.geometry(f"920x500+{(root.winfo_screenwidth()-920)//2}+{(root.winfo_screenheight()-500)//2}")

def sistem_yonetim_penceresi():
    if hasattr(root, "yonetim_paneli") and root.yonetim_paneli.winfo_exists():
        root.yonetim_paneli.lift()
        return

    panel = tk.Toplevel(root)
    root.yonetim_paneli = panel
    panel.title("Sistem Yönetimi")
    panel.attributes("-topmost", True)
    panel.overrideredirect(True)
    bg_color = "#F3F7FB"
    panel.configure(bg="#0F172A")

    cerceve_dis = tk.Frame(panel, bg="#0F172A", bd=0)
    cerceve_dis.pack(fill="both", expand=True)

    cerceve_ic = tk.Frame(cerceve_dis, bg=bg_color, bd=0, relief="flat")
    cerceve_ic.pack(fill="both", expand=True, padx=2, pady=2)

    baslik_cubugu = tk.Frame(cerceve_ic, bg="#111827", height=34)
    baslik_cubugu.pack(fill="x", side="top")
    tk.Label(baslik_cubugu, text="System 7 - Gelişmiş Yönetim Paneli", bg="#111827", fg="white", font=("Segoe UI", 10, "bold")).pack(side="left", padx=10)
    kapat_btn = tk.Button(baslik_cubugu, text="✕", font=("Segoe UI", 9, "bold"), bg="#DC2626", fg="white", bd=0, activebackground="#B91C1C", activeforeground="white", command=panel.destroy)
    kapat_btn.pack(side="right", padx=4, pady=2)

    def pencereyi_tasi(event):
        panel.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def pozisyon_al(event):
        global offset_x, offset_y
        offset_x = event.x
        offset_y = event.y

    baslik_cubugu.bind("<Button-1>", pozisyon_al)
    baslik_cubugu.bind("<B1-Motion>", pencereyi_tasi)

    stats_frame = tk.Frame(cerceve_ic, bg="white", bd=1, relief="solid")
    stats_frame.pack(fill="x", padx=10, pady=(10, 0))

    lbl_hesap = tk.Label(stats_frame, text="Hesaplar: 0", bg="white", font=("Segoe UI", 9, "bold"))
    lbl_hesap.pack(side="left", expand=True, pady=4)

    lbl_online = tk.Label(stats_frame, text="Çevrim İçi: 0", bg="white", font=("Segoe UI", 9, "bold"), fg="#16a34a")
    lbl_online.pack(side="left", expand=True, pady=4)

    lbl_ban = tk.Label(stats_frame, text="Banlılar: 0", bg="white", font=("Segoe UI", 9, "bold"), fg="#dc2626")
    lbl_ban.pack(side="left", expand=True, pady=4)

    lbl_yavas = tk.Label(stats_frame, text="Yavaş Mod: Kapalı", bg="white", font=("Segoe UI", 9, "bold"), fg="#1d4ed8")
    lbl_yavas.pack(side="left", expand=True, pady=4)

    main_frame = tk.Frame(cerceve_ic, bg=bg_color)
    main_frame.pack(fill="both", expand=True, padx=10, pady=10)

    chat_frame = tk.Frame(main_frame, bg=bg_color)
    chat_frame.pack(side="left", fill="both", expand=True)
    tk.Label(chat_frame, text="Canlı Sohbet Akışı (Çift Tıkla Mesaj Sil)", bg=bg_color, font=("Segoe UI", 10, "bold"), fg="#b91c1c").pack(anchor="w")
    
    chat_container = tk.Frame(chat_frame, bg=bg_color)
    chat_container.pack(side="left", fill="both", expand=True, pady=(5,0))
    
    chat_scroll = tk.Scrollbar(chat_container)
    chat_scroll.pack(side="right", fill="y")
    
    chat_text = tk.Text(chat_container, width=42, height=18, font=("Segoe UI", 9), bd=1, relief="solid", bg="white", fg="#111827", insertbackground="#111827", yscrollcommand=chat_scroll.set, state="disabled")
    chat_text.pack(side="left", fill="both", expand=True)
    chat_scroll.config(command=chat_text.yview)

    def mesaj_sil(event):
        try:
            index = int(chat_text.index(f"@{event.x},{event.y}").split('.')[0]) - 1
            if 0 <= index < len(sohbet_gecmisi):
                silinen = sohbet_gecmisi.pop(index)
                log_ekle(f"Mesaj silindi: {silinen.get('gonderen')}: {silinen.get('mesaj')}")
                guncelle_veriler(zorla=True)
        except Exception:
            pass

    chat_text.bind("<Double-Button-1>", mesaj_sil)

    ctrl_frame = tk.Frame(main_frame, bg=bg_color)
    ctrl_frame.pack(side="right", fill="y", padx=(10, 0))

    tk.Label(ctrl_frame, text="Kullanıcı Listesi", bg=bg_color, font=("Segoe UI", 10, "bold"), fg="#0f172a").pack(anchor="w")
    
    list_container = tk.Frame(ctrl_frame, bg=bg_color)
    list_container.pack(side="top", fill="both", expand=True, pady=(5, 5))
    
    kul_scroll = tk.Scrollbar(list_container)
    kul_scroll.pack(side="right", fill="y")
    
    kullanici_liste = tk.Listbox(list_container, width=26, height=7, font=("Segoe UI", 9), bd=1, relief="solid", bg="white", fg="#111827", yscrollcommand=kul_scroll.set)
    kullanici_liste.pack(side="left", fill="both", expand=True)
    kul_scroll.config(command=kullanici_liste.yview)

    btn_frame = tk.Frame(ctrl_frame, bg=bg_color)
    btn_frame.pack(side="bottom", fill="x")

    def panel_btn(master, **kwargs):
        opts = {
            "font": ("Segoe UI", 9, "bold"),
            "bd": 0,
            "relief": "flat",
            "activeforeground": "white",
        }
        opts.update(kwargs)
        if "bg" not in opts:
            opts["bg"] = "#2563eb"
        if "fg" not in opts:
            opts["fg"] = "white"
        return tk.Button(master, **opts)

    def k_ismini_al(metin):
        return metin.split(" (")[0].strip()

    def banla():
        secili = kullanici_liste.curselection()
        if secili:
            k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
            if k_isim != "Sistem" and k_isim != "":
                kullanici_banla_ve_email(k_isim)
                log_ekle(f"'{k_isim}' engellendi (Ban).")
                guncelle_veriler(zorla=True)

    def ban_ac():
        secili = kullanici_liste.curselection()
        if secili:
            k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
            if k_isim in engellenenler:
                kullanici_banini_ac(k_isim)
            if k_isim in susturulanlar:
                del susturulanlar[k_isim]
            log_ekle(f"'{k_isim}' ban/mute kaldırıldı.")
            guncelle_veriler(zorla=True)

    def kisiyi_sil():
        secili = kullanici_liste.curselection()
        if secili:
            k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
            if k_isim != "Sistem" and k_isim != "":
                if k_isim in kullanici_db: del kullanici_db[k_isim]
                if k_isim in engellenenler: kullanici_banini_ac(k_isim)
                if k_isim in susturulanlar: del susturulanlar[k_isim]
                oda_kurma_izni.discard(k_isim)
                for oda_adi, lider in list(oda_liderleri.items()):
                    if lider == k_isim:
                        oda_roller.get(oda_adi, {}).pop(k_isim, None)
                        oda_liderleri[oda_adi] = "Sistem"
                for roller in oda_roller.values():
                    roller.pop(k_isim, None)
                for yasakli_liste in oda_yasaklari.values():
                    if k_isim in yasakli_liste:
                        yasakli_liste.remove(k_isim)
                for gecici_liste in oda_gecici_banlar.values():
                    gecici_liste.pop(k_isim, None)
                log_ekle(f"Kullanıcı hesabı tamamen silindi: '{k_isim}'")
                guncelle_veriler(zorla=True)

    def oturum_kapat_kick():
        secili = kullanici_liste.curselection()
        if secili:
            k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
            if k_isim != "Sistem" and k_isim in kullanici_db:
                zorla_cikis.add(k_isim)
                log_ekle(f"'{k_isim}' oturumdan atıldı (Kick).")
                sohbet_gecmisi.append({"gonderen": "📢 DUYURU", "mesaj": f"👢 {k_isim} sunucudan atıldı.", "alici": "Genel", "oda": "Genel", "zaman": time.time()})
                son_aktiflik.pop(k_isim, None)
                guncelle_veriler(zorla=True)

    def geri_bildirimler_goster():
        win = tk.Toplevel(panel)
        win.title("📝 Geri Bildirimler")
        win.attributes("-topmost", True)
        win.configure(bg=bg_color)

        ana = tk.Frame(win, bg=bg_color, padx=12, pady=12)
        ana.pack(fill="both", expand=True)

        ust = tk.Frame(ana, bg=bg_color)
        ust.pack(fill="x")
        tk.Label(ust, text=f"Toplam geri bildirim: {len(geri_bildirimler)}", font=("Arial", 10, "bold"), bg=bg_color).pack(side="left")

        govde = tk.Frame(ana, bg=bg_color)
        govde.pack(fill="both", expand=True, pady=(10, 0))

        sol = tk.Frame(govde, bg=bg_color)
        sol.pack(side="left", fill="y", padx=(0, 10))

        sag = tk.Frame(govde, bg=bg_color)
        sag.pack(side="right", fill="both", expand=True)

        lst = tk.Listbox(sol, width=34, height=18, font=("Arial", 9))
        lst.pack(side="left", fill="y")

        sc = tk.Scrollbar(sol, orient="vertical", command=lst.yview)
        sc.pack(side="right", fill="y")
        lst.config(yscrollcommand=sc.set)

        detay = tk.Text(sag, width=60, height=18, font=("Arial", 10), wrap="word")
        detay.pack(fill="both", expand=True)

        if not geri_bildirimler:
            lst.insert("end", "Henüz geri bildirim yok")
            detay.insert("1.0", "Ban ekranından gönderilen geri bildirimler burada görünecek.")
            detay.config(state="disabled")
        else:
            for i, fb in enumerate(reversed(geri_bildirimler)):
                zaman = time.strftime("%d.%m.%Y %H:%M", time.localtime(fb.get("zaman", time.time())))
                k = fb.get("kullanici", "Bilinmeyen")
                oda = fb.get("oda", "Genel")
                lst.insert("end", f"{i+1}. {k} | {oda} | {zaman}")

            def secim(_event=None):
                sec = lst.curselection()
                if not sec:
                    return
                idx = len(geri_bildirimler) - 1 - sec[0]
                fb = geri_bildirimler[idx]
                detay.config(state="normal")
                detay.delete("1.0", "end")
                detay.insert("1.0", f"Kullanıcı: {fb.get('kullanici', '')}\n")
                detay.insert("end", f"Oda: {fb.get('oda', '')}\n")
                detay.insert("end", f"Tarih: {time.strftime('%d.%m.%Y %H:%M', time.localtime(fb.get('zaman', time.time())))}\n\n")
                detay.insert("end", fb.get("mesaj", ""))
                detay.config(state="disabled")

            lst.bind("<<ListboxSelect>>", secim)
            lst.selection_set(0)
            secim()

        tk.Button(ana, text="Kapat", font=("Arial", 10, "bold"), bg="#888888", fg="white", command=win.destroy).pack(fill="x", pady=(10, 0))

    def kullanici_duzenle_penceresi():
        secili = kullanici_liste.curselection()
        if not secili: return
        eski_isim = k_ismini_al(kullanici_liste.get(secili[0]))
        if eski_isim == "Sistem" or not eski_isim: return

        duzen_win = tk.Toplevel(panel)
        duzen_win.title("Üye Düzenle")
        duzen_win.attributes("-topmost", True)
        duzen_win.overrideredirect(True)
        duzen_win.configure(bg=bg_color)

        d_dis = tk.Frame(duzen_win, bg="black", bd=0)
        d_dis.pack(fill="both", expand=True)

        d_ic = tk.Frame(d_dis, bg=bg_color, bd=2, relief="flat")
        d_ic.pack(fill="both", expand=True, padx=2, pady=2)

        d_baslik = tk.Frame(d_ic, bg="black", height=20)
        d_baslik.pack(fill="x", side="top")
        tk.Label(d_baslik, text=f"✏️ {eski_isim} - Düzenle", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(d_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=duzen_win.destroy).pack(side="right", padx=4, pady=2)

        icerik = tk.Frame(d_ic, bg=bg_color)
        icerik.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(icerik, text="Yeni İsim (Boş=Değişmez):", bg=bg_color, font=("Arial", 9, "bold")).pack(anchor="w")
        isim_giris = tk.Entry(icerik, font=("Arial", 10), bd=2, relief="sunken")
        isim_giris.pack(fill="x", pady=(0, 5))

        tk.Label(icerik, text="Yeni Şifre (Boş=Değişmez):", bg=bg_color, font=("Arial", 9, "bold")).pack(anchor="w")
        sifre_giris = tk.Entry(icerik, font=("Arial", 10), bd=2, relief="sunken")
        sifre_giris.pack(fill="x", pady=(0, 10))

        def kaydet():
            yeni_isim = isim_giris.get().strip()
            yeni_sifre = sifre_giris.get().strip()
            
            hedef_isim = eski_isim
            if yeni_isim and yeni_isim != eski_isim and yeni_isim not in kullanici_db:
                kullanici_db[yeni_isim] = kullanici_db.pop(eski_isim)
                
                kullanici_renames[eski_isim] = yeni_isim
                if eski_isim in son_aktiflik:
                    son_aktiflik[yeni_isim] = son_aktiflik.pop(eski_isim)
                if eski_isim in kullanici_kayit_zamani:
                    kullanici_kayit_zamani[yeni_isim] = kullanici_kayit_zamani.pop(eski_isim)
                if eski_isim in kullanici_oturum_toplam_saniye:
                    kullanici_oturum_toplam_saniye[yeni_isim] = kullanici_oturum_toplam_saniye.pop(eski_isim)
                if eski_isim in kullanici_oturum_son_kayit:
                    kullanici_oturum_son_kayit[yeni_isim] = kullanici_oturum_son_kayit.pop(eski_isim)
                eski_email = kullanici_emailleri.pop(eski_isim, None)
                if eski_email:
                    eski_email = e_posta_normalize(eski_email)
                    if email_hesaplari.get(eski_email) == eski_isim:
                        email_hesaplari.pop(eski_email, None)
                    kullanici_emailleri[yeni_isim] = eski_email
                    email_hesaplari[eski_email] = yeni_isim
                if eski_isim in engellenenler:
                    engellenenler.discard(eski_isim)
                    if eski_email:
                        banli_emailler.add(eski_email)
                    engellenenler.add(yeni_isim)
                if eski_isim in susturulanlar:
                    susturulanlar[yeni_isim] = susturulanlar.pop(eski_isim)
                for msg in sohbet_gecmisi:
                    if msg.get("gonderen") == eski_isim: msg["gonderen"] = yeni_isim
                    if msg.get("alici") == eski_isim: msg["alici"] = yeni_isim

                # Oda liderliği / rolleri / banları / oda kurma izni de yeni isme taşınmalı,
                # yoksa isim değişince kullanıcı kendi odasının lideri olmaktan sessizce çıkıyordu.
                for oda_adi, lider in list(oda_liderleri.items()):
                    if lider == eski_isim:
                        oda_liderleri[oda_adi] = yeni_isim
                for roller in oda_roller.values():
                    if eski_isim in roller:
                        roller[yeni_isim] = roller.pop(eski_isim)
                for yasakli_liste in oda_yasaklari.values():
                    if eski_isim in yasakli_liste:
                        yasakli_liste.remove(eski_isim)
                        yasakli_liste.append(yeni_isim)
                for gecici_liste in oda_gecici_banlar.values():
                    if eski_isim in gecici_liste:
                        gecici_liste[yeni_isim] = gecici_liste.pop(eski_isim)
                if eski_isim in oda_kurma_izni:
                    oda_kurma_izni.discard(eski_isim)
                    oda_kurma_izni.add(yeni_isim)
                
                log_ekle(f"Kullanıcı adı değiştirildi: '{eski_isim}' ➔ '{yeni_isim}'")
                hedef_isim = yeni_isim
            
            if yeni_sifre:
                kullanici_db[hedef_isim] = sifre_hashle(yeni_sifre)
                log_ekle(f"'{hedef_isim}' şifresi güncellendi.")
                
            durumu_kaydet()
            guncelle_veriler(zorla=True)
            duzen_win.destroy()

        tk.Button(icerik, text="💾 Kaydet", font=("Arial", 10, "bold"), bg="#008800", fg="white", bd=2, relief="raised", command=kaydet).pack(fill="x")
        
        duzen_win.update_idletasks()
        dw, dh = 300, 180
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        duzen_win.geometry(f"{dw}x{dh}+{(ekran_g//2)-(dw//2)}+{(ekran_y//2)-(dh//2)}")

    def sustur_penceresi():
        secili = kullanici_liste.curselection()
        if not secili: return
        k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
        if k_isim == "Sistem" or not k_isim: return

        s_win = tk.Toplevel(panel)
        s_win.title("Sustur")
        s_win.attributes("-topmost", True)
        s_win.overrideredirect(True)
        s_win.configure(bg=bg_color)

        s_dis = tk.Frame(s_win, bg="black", bd=0)
        s_dis.pack(fill="both", expand=True)

        s_ic = tk.Frame(s_dis, bg=bg_color, bd=2, relief="flat")
        s_ic.pack(fill="both", expand=True, padx=2, pady=2)

        s_baslik = tk.Frame(s_ic, bg="black", height=20)
        s_baslik.pack(fill="x", side="top")
        tk.Label(s_baslik, text=f"🔇 {k_isim} Sustur", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(s_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=s_win.destroy).pack(side="right", padx=4, pady=2)

        s_content = tk.Frame(s_ic, bg=bg_color)
        s_content.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(s_content, text="Süre (Dakika):", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w")
        s_giris = tk.Entry(s_content, font=("Arial", 11), bd=2, relief="sunken")
        s_giris.insert(0, "5")
        s_giris.pack(fill="x", pady=(0, 5))

        tk.Label(s_content, text="Oda (Boş = Her Yer):", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w")
        oda_giris = tk.Entry(s_content, font=("Arial", 11), bd=2, relief="sunken")
        oda_giris.pack(fill="x", pady=(0, 10))

        s_giris.focus_set()

        def sustur_onay(event=None):
            try:
                dk = float(s_giris.get().strip())
                oda_adi = oda_giris.get().strip() or "Hepsi"
                if dk > 0:
                    susturulanlar[k_isim] = {"bitis": time.time() + (dk * 60), "oda": oda_adi}
                    sohbet_gecmisi.append({"gonderen": "📢 DUYURU", "mesaj": f"🔇 {k_isim} kullanıcısı {int(dk)} dakika susturuldu. (Mekan: {oda_adi})", "alici": "Genel", "oda": "Genel", "zaman": time.time()})
                    log_ekle(f"'{k_isim}' {int(dk)} dk susturuldu.")
                    guncelle_veriler(zorla=True)
            except ValueError:
                pass
            s_win.destroy()

        s_giris.bind("<Return>", sustur_onay)
        tk.Button(s_content, text="Sustur", font=("Arial", 10, "bold"), bg="#0066CC", fg="white", bd=2, relief="raised", command=sustur_onay).pack(fill="x")

        s_win.update_idletasks()
        sw, sh = 280, 180
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        s_win.geometry(f"{sw}x{sh}+{(ekran_g//2)-(sw//2)}+{(ekran_y//2)-(sh//2)}")

    def temizle():
        sohbet_gecmisi.clear()
        log_ekle("Sohbet geçmişi temizlendi.")
        guncelle_veriler(zorla=True)

    def log_penceresi():
        log_win = tk.Toplevel(panel)
        log_win.title("📋 Sistem Logları")
        log_win.attributes("-topmost", True)
        log_win.configure(bg=bg_color)

        ic = tk.Frame(log_win, bg=bg_color, padx=10, pady=10)
        ic.pack(fill="both", expand=True)

        tk.Label(ic, text="📋 Sistem Denetim Logları", font=("Arial", 10, "bold"), bg=bg_color).pack(anchor="w")
        txt = tk.Text(ic, width=50, height=15, font=("Consolas", 9), bd=2, relief="sunken")
        txt.pack(fill="both", expand=True, pady=5)

        for l in sistem_loglari:
            txt.insert(tk.END, l + "\n")
        txt.see(tk.END)

    def sabit_duyuru_ayarla():
        global sabit_duyuru
        duy_win = tk.Toplevel(panel)
        duy_win.title("📌 Sabit Duyuru")
        duy_win.attributes("-topmost", True)
        duy_win.configure(bg=bg_color)

        ic = tk.Frame(duy_win, bg=bg_color, padx=10, pady=10)
        ic.pack()

        tk.Label(ic, text="📌 Üst Bilgi Bandı Mesajı:", font=("Arial", 10, "bold"), bg=bg_color).pack(anchor="w")
        e = tk.Entry(ic, font=("Arial", 10), width=35, bd=2, relief="sunken")
        e.insert(0, sabit_duyuru)
        e.pack(pady=5)

        def kaydet():
            global sabit_duyuru
            sabit_duyuru = e.get().strip()
            log_ekle(f"Sabit duyuru güncellendi: '{sabit_duyuru}'")
            duy_win.destroy()

        tk.Button(ic, text="Kaydet / Güncelle", font=("Arial", 9, "bold"), bg="#008800", fg="white", command=kaydet).pack(fill="x")

    def yavas_mod_ayarla():
        global yavas_mod_saniye
        ym_win = tk.Toplevel(panel)
        ym_win.title("⏳ Yavaş Mod")
        ym_win.attributes("-topmost", True)
        ym_win.configure(bg=bg_color)

        ic = tk.Frame(ym_win, bg=bg_color, padx=10, pady=10)
        ic.pack()

        tk.Label(ic, text="Mesaj Aralığı Saniye (0 = Kapalı):", font=("Arial", 9, "bold"), bg=bg_color).pack(anchor="w")
        e = tk.Entry(ic, font=("Arial", 10), width=20, bd=2, relief="sunken")
        e.insert(0, str(yavas_mod_saniye))
        e.pack(pady=5)

        def kaydet():
            global yavas_mod_saniye
            try:
                yavas_mod_saniye = max(0, int(e.get().strip()))
                log_ekle(f"Yavaş mod {yavas_mod_saniye} saniye yapıldı.")
            except ValueError:
                pass
            ym_win.destroy()

        e.bind("<Return>", lambda ev: kaydet())
        tk.Button(ic, text="Ayarla", font=("Arial", 9, "bold"), bg="#0066CC", fg="white", command=kaydet).pack(fill="x")

        ym_win.update_idletasks()
        yw, yh = 260, 140
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        ym_win.geometry(f"{yw}x{yh}+{((ekran_g-yw)//2)}+{((ekran_y-yh)//2)}")

    def oda_izin_istekleri_penceresi():
        win = tk.Toplevel(panel)
        win.title("🏷️ Oda İzin İstekleri")
        win.attributes("-topmost", True)
        win.configure(bg=bg_color)

        ust = tk.Frame(win, bg=bg_color, padx=10, pady=10)
        ust.pack(fill="both", expand=True)

        tk.Label(ust, text="🏷️ Oda İzin İstekleri", font=("Segoe UI", 11, "bold"), bg=bg_color).pack(anchor="w")
        tk.Label(ust, text="Bekleyen oda kurma izni isteklerini buradan onaylayabilir veya reddedebilirsin.",
                 font=("Segoe UI", 9), bg=bg_color, fg="#334155", justify="left", wraplength=560).pack(anchor="w", pady=(2, 8))

        orta = tk.Frame(ust, bg=bg_color)
        orta.pack(fill="both", expand=True)

        sol = tk.Frame(orta, bg=bg_color)
        sol.pack(side="left", fill="both", expand=True, padx=(0, 8))

        sag = tk.Frame(orta, bg=bg_color)
        sag.pack(side="right", fill="y")

        tk.Label(sol, text="Bekleyen İstekler", bg=bg_color, font=("Segoe UI", 10, "bold"), fg="#0f172a").pack(anchor="w")
        list_container = tk.Frame(sol, bg=bg_color)
        list_container.pack(fill="both", expand=True, pady=(5, 0))
        sb = tk.Scrollbar(list_container)
        sb.pack(side="right", fill="y")
        liste = tk.Listbox(list_container, font=("Segoe UI", 10), height=14, yscrollcommand=sb.set)
        liste.pack(side="left", fill="both", expand=True)
        sb.config(command=liste.yview)

        detay_baslik = tk.Label(sag, text="Detay", bg=bg_color, font=("Segoe UI", 10, "bold"), fg="#0f172a")
        detay_baslik.pack(anchor="w")
        detay = tk.Text(sag, width=42, height=14, font=("Segoe UI", 9), bd=1, relief="solid", wrap="word")
        detay.pack(fill="both", expand=True, pady=(5, 8))
        detay.config(state="disabled")

        secili = {"kullanici": None}

        def istekleri_al():
            with veri_kilidi:
                return sorted(oda_izin_istekleri.items(), key=lambda item: item[1])

        def detay_yaz(metin):
            detay.config(state="normal")
            detay.delete("1.0", tk.END)
            detay.insert("1.0", metin)
            detay.config(state="disabled")

        def yenile():
            istekler = istekleri_al()
            liste.delete(0, tk.END)
            secili["kullanici"] = None
            if not istekler:
                liste.insert(tk.END, "Bekleyen istek yok.")
                detay_yaz("Şu anda bekleyen oda izin isteği bulunmuyor.")
                return
            for k, t in istekler:
                zaman = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(t))
                liste.insert(tk.END, f"{k}  |  {zaman}")
            liste.selection_clear(0, tk.END)
            detay_yaz("Soldan bir isteği seçerek onaylayabilir veya reddedebilirsin.")

        def secim(event=None):
            idx = liste.curselection()
            if not idx:
                return
            istekler = istekleri_al()
            if not istekler:
                return
            i = idx[0]
            if i >= len(istekler):
                return
            k, t = istekler[i]
            secili["kullanici"] = k
            zaman = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(t))
            detay_yaz(
                f"Kullanıcı: {k}\n"
                f"İstek zamanı: {zaman}\n"
                f"Durum: Bekliyor\n\n"
                f"Bu kullanıcıya oda kurma izni vermek için 'İzin Ver', reddetmek için 'Reddet' kullan."
            )

        def istek_cevapla(izin_ver):
            hedef = secili.get("kullanici")
            if not hedef:
                return
            with veri_kilidi:
                if hedef not in oda_izin_istekleri:
                    pass
                else:
                    oda_izin_istekleri.pop(hedef, None)
                    if izin_ver and hedef in kullanici_db and hedef != "Sistem":
                        oda_kurma_izni.add(hedef)
                        log_ekle(f"'{hedef}' kullanıcısına oda kurma izni verildi (istekler panelinden onaylandı).")
                    elif not izin_ver:
                        log_ekle(f"'{hedef}' kullanıcısının oda kurma isteği reddedildi (istekler panelinden).")
                    durumu_kaydet()
            yenile()

        btnler = tk.Frame(sag, bg=bg_color)
        btnler.pack(fill="x")
        tk.Button(btnler, text="✅ İzin Ver", font=("Segoe UI", 9, "bold"), bg="#16a34a", fg="white", command=lambda: istek_cevapla(True)).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(btnler, text="❌ Reddet", font=("Segoe UI", 9, "bold"), bg="#dc2626", fg="white", command=lambda: istek_cevapla(False)).pack(side="left", expand=True, fill="x", padx=(4, 0))

        liste.bind("<<ListboxSelect>>", secim)
        yenile()
        win.update_idletasks()
        win.geometry(f"680x380+{(root.winfo_screenwidth()-680)//2}+{(root.winfo_screenheight()-380)//2}")

    def toggle_kufur_filtresi():
        global kufur_filtresi
        kufur_filtresi = not kufur_filtresi
        btn_kufur.config(text=f"🔞 Küfür Filtresi: {'AÇIK' if kufur_filtresi else 'KAPALI'}", bg="#008800" if kufur_filtresi else "#888888")
        log_ekle(f"Küfür filtresi {'açıldı' if kufur_filtresi else 'kapatıldı'}.")

    def alarm_gonder():
        alarm_win = tk.Toplevel(panel)
        alarm_win.title("🚨 Sesli Siren Gönder")
        alarm_win.attributes("-topmost", True)
        alarm_win.overrideredirect(True)
        alarm_win.configure(bg=bg_color)

        a_dis = tk.Frame(alarm_win, bg="black", bd=0)
        a_dis.pack(fill="both", expand=True)

        a_ic = tk.Frame(a_dis, bg=bg_color, bd=2, relief="flat")
        a_ic.pack(fill="both", expand=True, padx=2, pady=2)

        a_baslik = tk.Frame(a_ic, bg="black", height=20)
        a_baslik.pack(fill="x", side="top")
        tk.Label(a_baslik, text="🚨 Sesli Siren Gönder", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(a_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=alarm_win.destroy).pack(side="right", padx=4, pady=2)

        a_content = tk.Frame(a_ic, bg=bg_color)
        a_content.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(a_content, text="Siren Mesajı:", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w", pady=(0, 5))
        a_giris = tk.Entry(a_content, font=("Arial", 11), bd=2, relief="sunken")
        a_giris.insert(0, "TÜM KULLANICILARIN DİKKATİNE! YÖNETİCİ UYARISI!")
        a_giris.pack(fill="x", pady=(0, 10))
        a_giris.focus_set()
        a_giris.select_range(0, tk.END)

        def sireni_yolla(event=None):
            metin = a_giris.get().strip()
            if metin:
                sohbet_gecmisi.append({"gonderen": "📢 ALARM", "mesaj": metin, "alici": "Genel", "oda": "Genel", "zaman": time.time()})
                log_ekle(f"🚨 Sitede sesli alarm tetiklendi: '{metin}'")
                guncelle_veriler(zorla=True)
            alarm_win.destroy()

        a_giris.bind("<Return>", sireni_yolla)
        tk.Button(a_content, text="🚨 Sireni Gönder", font=("Arial", 10, "bold"), bg="#AA0000", fg="white", bd=2, relief="raised", command=sireni_yolla).pack(fill="x")

        alarm_win.update_idletasks()
        aw, ah = 380, 140
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        alarm_win.geometry(f"{aw}x{ah}+{((ekran_g-aw)//2)}+{((ekran_y-ah)//2)}")

    def duyuru_gonder_penceresi():
        duyuru_win = tk.Toplevel(panel)
        duyuru_win.title("📢 Duyuru Gönder")
        duyuru_win.attributes("-topmost", True)
        duyuru_win.overrideredirect(True)
        duyuru_win.configure(bg=bg_color)

        d_dis = tk.Frame(duyuru_win, bg="black", bd=0)
        d_dis.pack(fill="both", expand=True)

        d_ic = tk.Frame(d_dis, bg=bg_color, bd=2, relief="flat")
        d_ic.pack(fill="both", expand=True, padx=2, pady=2)

        d_baslik = tk.Frame(d_ic, bg="black", height=20)
        d_baslik.pack(fill="x", side="top")
        tk.Label(d_baslik, text="📢 Duyuru Yayınla", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(d_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=duyuru_win.destroy).pack(side="right", padx=4, pady=2)

        d_content = tk.Frame(d_ic, bg=bg_color)
        d_content.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(d_content, text="Duyuru Metni:", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w", pady=(0, 5))
        d_giris = tk.Entry(d_content, font=("Arial", 11), bd=2, relief="sunken")
        d_giris.pack(fill="x", pady=(0, 10))
        d_giris.focus_set()

        def duyuruyu_yayinla(event=None):
            metin = d_giris.get().strip()
            if metin:
                sohbet_gecmisi.append({"gonderen": "📢 DUYURU", "mesaj": metin, "alici": "Genel", "oda": "Genel", "zaman": time.time()})
                log_ekle(f"Duyuru yayınlandı: '{metin}'")
                guncelle_veriler(zorla=True)
            duyuru_win.destroy()

        d_giris.bind("<Return>", duyuruyu_yayinla)
        tk.Button(d_content, text="Yayınla", font=("Arial", 10, "bold"), bg="#CCCCCC", fg="black", bd=2, relief="raised", command=duyuruyu_yayinla).pack(fill="x")

        duyuru_win.update_idletasks()
        dw, dh = 360, 130
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        duyuru_win.geometry(f"{dw}x{dh}+{((ekran_g-dw)//2)}+{((ekran_y-dh)//2)}")

    def sayac_baslat_penceresi():
        sayac_win = tk.Toplevel(panel)
        sayac_win.title("⏱️ Sayaç Başlat")
        sayac_win.attributes("-topmost", True)
        sayac_win.overrideredirect(True)
        sayac_win.configure(bg=bg_color)

        s_dis = tk.Frame(sayac_win, bg="black", bd=0)
        s_dis.pack(fill="both", expand=True)

        s_ic = tk.Frame(s_dis, bg=bg_color, bd=2, relief="flat")
        s_ic.pack(fill="both", expand=True, padx=2, pady=2)

        s_baslik = tk.Frame(s_ic, bg="black", height=20)
        s_baslik.pack(fill="x", side="top")
        tk.Label(s_baslik, text="⏱️ Sayaç Başlat", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(s_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=sayac_win.destroy).pack(side="right", padx=4, pady=2)

        s_content = tk.Frame(s_ic, bg=bg_color)
        s_content.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(s_content, text="Süre (Dakika):", bg=bg_color, font=("Arial", 10, "bold"), fg="black").pack(anchor="w", pady=(0, 5))
        s_giris = tk.Entry(s_content, font=("Arial", 11), bd=2, relief="sunken")
        s_giris.pack(fill="x", pady=(0, 10))
        s_giris.focus_set()

        def sayaci_baslat(event=None):
            try:
                dk = float(s_giris.get().strip())
                if dk > 0:
                    bitis = time.time() + (dk * 60)
                    sohbet_gecmisi.append({"gonderen": "📢 SAYAÇ", "mesaj": "", "alici": "Genel", "oda": "Genel", "bitis_zamani": bitis, "zaman": time.time()})
                    log_ekle(f"{int(dk)} dakikalık geri sayım başlatıldı.")
                    guncelle_veriler(zorla=True)
            except ValueError:
                pass
            sayac_win.destroy()

        s_giris.bind("<Return>", sayaci_baslat)
        tk.Button(s_content, text="Başlat", font=("Arial", 10, "bold"), bg="#FF0000", fg="yellow", bd=2, relief="raised", command=sayaci_baslat).pack(fill="x")

        sayac_win.update_idletasks()
        sw, sh = 280, 130
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        sayac_win.geometry(f"{sw}x{sh}+{((ekran_g-sw)//2)}+{((ekran_y-sh)//2)}")

    def toggle_bakim():
        global bakim_modu
        bakim_modu = not bakim_modu
        if bakim_modu:
            btn_bakim.config(text="🟢 Bakım Modunu Kapat", bg="#008800")
            log_ekle("Site bakım moduna alındı.")
        else:
            btn_bakim.config(text="🛠️ Bakım Modunu Aç", bg="#FF8800")
            log_ekle("Site bakımdan çıkarıldı.")
        guncelle_veriler(zorla=True)

    def oda_yonetim_isteme():
        oda_win = tk.Toplevel(panel)
        oda_win.title("🏠 Oda Yönetimi")
        oda_win.attributes("-topmost", True)
        oda_win.overrideredirect(True)
        bg_color = "#F3F7FB"
        oda_win.configure(bg=bg_color)

        d_dis = tk.Frame(oda_win, bg="black", bd=0)
        d_dis.pack(fill="both", expand=True)

        d_ic = tk.Frame(d_dis, bg=bg_color, bd=2, relief="flat")
        d_ic.pack(fill="both", expand=True, padx=2, pady=2)

        d_baslik = tk.Frame(d_ic, bg="black", height=20)
        d_baslik.pack(fill="x", side="top")
        tk.Label(d_baslik, text="🏠 Oda Yönetimi", bg="black", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Button(d_baslik, text="✕", font=("Arial", 8, "bold"), bg="#CC0000", fg="white", bd=0, command=oda_win.destroy).pack(side="right", padx=4, pady=2)

        icerik = tk.Frame(d_ic, bg=bg_color)
        icerik.pack(fill="both", expand=True, padx=10, pady=10)

        liste_frame = tk.Frame(icerik, bg=bg_color)
        liste_frame.pack(side="left", fill="both", expand=True)
        tk.Label(liste_frame, text="Mevcut Odalar", bg=bg_color, font=("Arial", 10, "bold")).pack(anchor="w")
        oda_liste = tk.Listbox(liste_frame, font=("Arial", 10), bd=2, relief="sunken")
        oda_liste.pack(fill="both", expand=True, pady=5)

        sag_frame = tk.Frame(icerik, bg=bg_color)
        sag_frame.pack(side="right", fill="y", padx=(10,0))

        tk.Label(sag_frame, text="Yeni Ad (Boş=Değişmez):", bg=bg_color, font=("Arial", 10, "bold")).pack(anchor="w")
        isim_giris = tk.Entry(sag_frame, font=("Arial", 10), bd=2, relief="sunken")
        isim_giris.pack(fill="x", pady=(0, 5))

        tk.Label(sag_frame, text="Şifre (Boş = Şifresiz):", bg=bg_color, font=("Arial", 10, "bold")).pack(anchor="w")
        sifre_giris = tk.Entry(sag_frame, font=("Arial", 10), bd=2, relief="sunken")
        sifre_giris.pack(fill="x", pady=(0, 10))

        def listeyi_yenile():
            oda_liste.delete(0, tk.END)
            for oda, sif in odalar_db.items():
                durum = "🔒" if sif else "🔓"
                oda_liste.insert(tk.END, f"{oda} {durum}")

        def oda_kur():
            isim = isim_giris.get().strip()
            sif = sifre_giris.get().strip()
            ozel_oda_sayisi = len([o for o in odalar_db.keys() if o != "Genel"])
            if isim and isim not in odalar_db and ozel_oda_sayisi < MAKS_OZEL_ODA:
                odalar_db[isim] = sif
                oda_olustur_kaydi(isim, "Sistem")
                durumu_kaydet()
                log_ekle(f"Sistem oda kurdu: '{isim}'")
                listeyi_yenile()
                isim_giris.delete(0, tk.END)
                sifre_giris.delete(0, tk.END)

        def oda_duzenle():
            secili = oda_liste.curselection()
            if not secili: return
            eski_oda = oda_liste.get(secili[0]).split(" ")[0]
            if eski_oda == "Genel": return
            
            yeni_oda = isim_giris.get().strip()
            yeni_sifre = sifre_giris.get().strip()
            
            if yeni_oda and yeni_oda not in odalar_db:
                odalar_db[yeni_oda] = yeni_sifre
                del odalar_db[eski_oda]
                oda_kaydini_tasi(eski_oda, yeni_oda)
                for msg in sohbet_gecmisi:
                    if msg.get("oda") == eski_oda: msg["oda"] = yeni_oda
            else:
                odalar_db[eski_oda] = yeni_sifre
                
            durumu_kaydet()
            listeyi_yenile()
            isim_giris.delete(0, tk.END)
            sifre_giris.delete(0, tk.END)

        def oda_sil():
            secili = oda_liste.curselection()
            if secili:
                secilen = oda_liste.get(secili[0]).split(" ")[0]
                if secilen != "Genel":
                    del odalar_db[secilen]
                    oda_kaydini_sil(secilen)
                    durumu_kaydet()
                    log_ekle(f"Oda silindi: '{secilen}'")
                    listeyi_yenile()

        tk.Button(sag_frame, text="➕ Oluştur", font=("Arial", 10, "bold"), bg="#008800", fg="white", bd=2, relief="raised", command=oda_kur).pack(fill="x", pady=2)
        tk.Button(sag_frame, text="✏️ Düzenle", font=("Arial", 10, "bold"), bg="#0066CC", fg="white", bd=2, relief="raised", command=oda_duzenle).pack(fill="x", pady=2)
        tk.Button(sag_frame, text="🗑️ Sil", font=("Arial", 10, "bold"), bg="#AA0000", fg="white", bd=2, relief="raised", command=oda_sil).pack(fill="x", pady=2)

        def oda_secildi(event):
            secili = oda_liste.curselection()
            if secili:
                secilen_oda = oda_liste.get(secili[0]).split(" ")[0]
                isim_giris.delete(0, tk.END)
                sifre_giris.delete(0, tk.END)
                if secilen_oda != "Genel":
                    isim_giris.insert(0, secilen_oda)
                    sifre_giris.insert(0, odalar_db.get(secilen_oda, ""))

        oda_liste.bind('<<ListboxSelect>>', oda_secildi)
        listeyi_yenile()
        
        oda_win.update_idletasks()
        ow, oh = 420, 270
        ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
        oda_win.geometry(f"{ow}x{oh}+{((ekran_g-ow)//2)}+{((ekran_y-oh)//2)}")

    panel_btn(btn_frame, text="✏️ İsim/Hesap Düzenle", command=kullanici_duzenle_penceresi).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="📝 Geri Bildirimler", bg="#16a34a", command=geri_bildirimler_goster).pack(fill="x", pady=1)

    panel_btn(btn_frame, text="⛔ Banla", bg="#dc2626", command=banla).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="🔇 Sustur", command=sustur_penceresi).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="👢 Kick At", bg="#f97316", command=oturum_kapat_kick).pack(fill="x", pady=1)

    panel_btn(btn_frame, text="✅ Ban/Mute Kaldır", bg="#16a34a", command=ban_ac).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="❌ Hesabı Tamamen Sil", bg="#991b1b", command=kisiyi_sil).pack(fill="x", pady=1)

    tk.Frame(btn_frame, height=2, bg="black").pack(fill="x", pady=4)

    def oda_kurma_izni_toggle():
        secili = kullanici_liste.curselection()
        if secili:
            k_isim = k_ismini_al(kullanici_liste.get(secili[0]))
            if k_isim != "Sistem" and k_isim in kullanici_db:
                if k_isim in oda_kurma_izni:
                    oda_kurma_izni.discard(k_isim)
                    log_ekle(f"'{k_isim}' kullanıcısının oda kurma izni alındı.")
                else:
                    oda_kurma_izni.add(k_isim)
                    log_ekle(f"'{k_isim}' kullanıcısına oda kurma izni verildi.")
                durumu_kaydet()

    panel_btn(btn_frame, text="⚠️ Şikayetler", bg="#b45309", command=lambda: sistem_sikayetler_penceresi(panel)).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="🏠 Oda Yönetimi", command=oda_yonetim_isteme).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="🏷️ Oda Kurma İzni Ver/Al", command=oda_kurma_izni_toggle).pack(fill="x", pady=1)
    btn_bakim = panel_btn(btn_frame, text="🟢 Bakım Kapalı" if not bakim_modu else "🛠️ Bakım Açık", bg="#16a34a" if not bakim_modu else "#f97316", command=toggle_bakim)
    btn_bakim.pack(fill="x", pady=1)

    panel_btn(btn_frame, text="📢 Duyuru Yap", bg="#eab308", fg="#111827", command=duyuru_gonder_penceresi).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="⏱️ Sayaç Başlat", bg="#ef4444", fg="white", command=sayac_baslat_penceresi).pack(fill="x", pady=1)

    panel_btn(btn_frame, text="📌 Sabit Duyuru", bg="#475569", command=sabit_duyuru_ayarla).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="⏳ Yavaş Mod", bg="#475569", command=yavas_mod_ayarla).pack(fill="x", pady=1)
    btn_kufur = panel_btn(btn_frame, text=f"🔞 Küfür Filtresi: {'AÇIK' if kufur_filtresi else 'KAPALI'}", bg="#16a34a" if kufur_filtresi else "#64748b", command=toggle_kufur_filtresi)
    btn_kufur.pack(fill="x", pady=1)

    panel_btn(btn_frame, text="🚨 Sesli Siren Gönder", bg="#dc2626", command=alarm_gonder).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="🏷️ Oda İzin İstekleri", bg="#334155", command=oda_izin_istekleri_penceresi).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="👻 Ghost Mode", bg="#334155", command=ghost_mode_isteme).pack(fill="x", pady=1)
    panel_btn(btn_frame, text="🗑️ Chati Temizle", bg="#64748b", command=temizle).pack(fill="x", pady=1)

    _panel_onbellek = {"mesaj_sayisi": None, "kullanici_satirlari": None, "durumlar": None}

    def guncelle_veriler(zorla=False):
        if not panel.winfo_exists(): return

        simdi = time.time()
        c_ici = sum(1 for t in son_aktiflik.values() if simdi - t < 10)

        yeni_durumlar = (len(kullanici_db), c_ici, len(engellenenler), yavas_mod_saniye)
        if zorla or yeni_durumlar != _panel_onbellek["durumlar"]:
            lbl_hesap.config(text=f"Hesaplar: {len(kullanici_db)}")
            lbl_online.config(text=f"Çevrim İçi: {c_ici}")
            lbl_ban.config(text=f"Banlılar: {len(engellenenler)}")
            lbl_yavas.config(text=f"Yavaş Mod: {yavas_mod_saniye}sn" if yavas_mod_saniye > 0 else "Yavaş Mod: Kapalı")
            _panel_onbellek["durumlar"] = yeni_durumlar

        # Aktif bir sayaç varsa her tikte yeniden çizilmeli, yoksa sadece mesaj sayısı değiştiğinde
        sayac_aktif = any(m.get("gonderen") == "📢 SAYAÇ" and m.get("bitis_zamani", 0) > simdi for m in sohbet_gecmisi)
        mesaj_sayisi = len(sohbet_gecmisi)
        if zorla or sayac_aktif or mesaj_sayisi != _panel_onbellek["mesaj_sayisi"]:
            chat_text.config(state="normal")
            chat_text.delete("1.0", tk.END)
            for m in sohbet_gecmisi:
                gonderen = m.get("gonderen", "Bilinmeyen")
                alici = m.get("alici", "Genel")
                oda = m.get("oda", "Genel")
                hedef = f" ➔ {alici}" if alici != "Genel" else ""

                if gonderen == "📢 SAYAÇ":
                    kalan = max(0, int(m.get("bitis_zamani", 0) - simdi))
                    dk, sn = kalan // 60, kalan % 60
                    mesaj = f"⏱️ {dk:02d}:{sn:02d}" + (" (Süre Bitti)" if kalan == 0 else "")
                    chat_text.insert(tk.END, f"[{oda}] {mesaj}\n")
                else:
                    mesaj = m.get("mesaj", "")
                    chat_text.insert(tk.END, f"[{oda}] [{gonderen}{hedef}]: {mesaj}\n")

            chat_text.see(tk.END)
            chat_text.config(state="disabled")
            _panel_onbellek["mesaj_sayisi"] = mesaj_sayisi

        # Süresi dolan mute'ları temizle (liste üretmeden önce, iterasyon sırasında değil)
        for k, mute_veri in list(susturulanlar.items()):
            bitis = mute_veri if isinstance(mute_veri, float) else mute_veri.get("bitis", 0)
            if simdi >= bitis:
                del susturulanlar[k]

        yeni_satirlar = []
        for k in kullanici_db.keys():
            durum = ""
            if k in engellenenler:
                durum = " (Banlı)"
            elif k in susturulanlar:
                mute_veri = susturulanlar[k]
                bitis = mute_veri if isinstance(mute_veri, float) else mute_veri.get("bitis", 0)
                kalan_dk = max(1, int((bitis - simdi) // 60) + 1)
                oda_adi = "Hepsi" if isinstance(mute_veri, float) else mute_veri.get("oda", "Hepsi")
                durum = f" (Mute: {kalan_dk}dk - {oda_adi})"
            yeni_satirlar.append(f"{k}{durum}")

        if zorla or yeni_satirlar != _panel_onbellek["kullanici_satirlari"]:
            secili_idx = kullanici_liste.curselection()
            secili_isim = k_ismini_al(kullanici_liste.get(secili_idx[0])) if secili_idx else None

            kullanici_liste.delete(0, tk.END)
            for satir in yeni_satirlar:
                kullanici_liste.insert(tk.END, satir)

            if secili_isim:
                for i in range(kullanici_liste.size()):
                    if k_ismini_al(kullanici_liste.get(i)) == secili_isim:
                        kullanici_liste.selection_set(i)
                        break
            _panel_onbellek["kullanici_satirlari"] = yeni_satirlar

        if not zorla:
            panel.after(500, guncelle_veriler)

    guncelle_veriler()
    
    panel.update_idletasks()
    w, h = 980, 700
    ekran_g, ekran_y = root.winfo_screenwidth(), root.winfo_screenheight()
    panel.geometry(f"{w}x{h}+{((ekran_g-w)//2)}+{((ekran_y-h)//2)}")

son_kisayol_zamani = 0

def guvenli_tetikle(fonksiyon):
    global son_kisayol_zamani
    simdi = time.time()
    if simdi - son_kisayol_zamani > 0.5:
        son_kisayol_zamani = simdi
        root.after(10, fonksiyon)

def kisayol_dinle():
    try:
        keyboard.add_hotkey('ctrl+alt+c', lambda: guvenli_tetikle(sistem_yazma_penceresi))
        keyboard.add_hotkey('ctrl+alt+p', lambda: guvenli_tetikle(sistem_yonetim_penceresi))
    except Exception as e:
        print(f"⚠️ Kısayol dinleyici başlatılamadı: {e}")

def mesaj_kontrol():
    try:
        while True:
            veri = mesaj_kuyrugu.get_nowait()
            mesaj_goster(veri)
    except queue.Empty:
        pass

    try:
        while True:
            istek_kullanici = izin_istek_kuyrugu.get_nowait()
            izin_istek_goster(istek_kullanici)
    except queue.Empty:
        pass

    root.after(100, mesaj_kontrol)

def flask_baslat():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

def terminal_dinle():
    print("💻 Terminal komutları aktif: 'bans', 'unban <isim>'")
    while True:
        try:
            komut = input().strip().split()
            if not komut: continue
            
            islem = komut[0].lower()
            
            if islem == "bans":
                if engellenenler:
                    print(f"🚫 Banlı kişiler: {', '.join(engellenenler)}")
                else:
                    print("✅ Banlı kimse yok.")
            
            elif islem == "unban":
                if len(komut) > 1:
                    kisi = komut[1]
                    if kisi in engellenenler:
                        kullanici_banini_ac(kisi)
                        print(f"✅ '{kisi}' banı açıldı.")
                    else:
                        print(f"⚠️ '{kisi}' banlı değil.")
                else:
                    print("⚠️ Kullanım: unban <isim>")
        except Exception:
            time.sleep(1)

if __name__ == "__main__":
    if tk is None:
        print("Tkinter bulunamadı; yalnızca web sunucusu başlatılıyor.")
        flask_baslat()
    else:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()

        kayit_thread = threading.Thread(target=otomatik_kayit, daemon=True)
        kayit_thread.start()

        term_thread = threading.Thread(target=terminal_dinle, daemon=True)
        term_thread.start()

        flask_thread = threading.Thread(target=flask_baslat, daemon=True)
        flask_thread.start()

        kisayol_dinle()

        root.after(100, mesaj_kontrol)
        root.mainloop()
