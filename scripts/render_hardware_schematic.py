"""Render NAVI hardware schematic PNG (Pillow only)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1960, 1440
OUT = Path(__file__).resolve().parents[1] / "static" / "assets" / "navi-hardware-schematic.png"


def font(size: int, bold: bool = False):
    names = (
        ["arialbd.ttf", "Arial Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf"]
    )
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default()


def main():
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    f_title = font(44, True)
    f_box = font(30, True)
    f_lbl = font(22)
    f_sm = font(18)
    f_pin = font(17)

    def text_center(xy, t, ft, fill="#263238"):
        x, y = xy
        bb = d.textbbox((0, 0), t, font=ft)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text((x - tw // 2, y - th // 2), t, font=ft, fill=fill)

    def box(x, y, w, h, title=None, outline="#90a4ae", fill="#ffffff"):
        d.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=fill, outline=outline, width=3)
        if title:
            d.text((x + 24, y + 20), title, font=f_box, fill="#263238")

    d.text((W // 2 - 420, 36), "Arquitectura hardware NAVI", font=f_title, fill="#263238")
    d.text(
        (W // 2 - 520, 92),
        "Esquema final - solo ESP32-CAM (sin ESP32 Dev, INMP441 ni MAX98357A)",
        font=f_sm,
        fill="#546e7a",
    )

    # Power
    box(72, 176, 496, 400, "Power Supply")
    d.rounded_rectangle((116, 256, 260, 344), radius=8, fill="#eceff1", outline="#78909c", width=2)
    text_center((188, 300), "7.4V", f_lbl)
    text_center((188, 328), "Battery", f_sm)
    d.rounded_rectangle((336, 248, 448, 352), radius=6, fill="#fff3e0", outline="#ff8f00", width=2)
    text_center((392, 296), "7805", f_lbl)
    text_center((392, 324), "Regulator", f_sm)
    d.text((284, 396), "0.33 uF", font=f_sm, fill="#546e7a")
    d.text((436, 396), "0.1 uF", font=f_sm, fill="#546e7a")
    text_center((392, 456), "+5V", f_lbl, "#c62828")
    d.line([(260, 300), (336, 300)], fill="#c62828", width=5)
    d.line([(448, 300), (536, 300), (536, 536)], fill="#c62828", width=5)
    d.line([(188, 344), (188, 460), (116, 460)], fill="#212121", width=4)
    d.line([(392, 352), (392, 460)], fill="#212121", width=4)

    # ESP32-CAM
    box(680, 400, 600, 560, None, "#1565c0", "#e3f2fd")
    text_center((980, 464), "ESP32-CAM", f_box)
    d.rounded_rectangle((800, 496, 1160, 736), radius=12, fill="#263238")
    d.rounded_rectangle((860, 524, 956, 596), radius=6, fill="#37474f", outline="#90caf9", width=2)
    text_center((908, 560), "OV2640", font(16), "#bbdefb")
    text_center((980, 636), "WiFi / WebSocket", font(22), "#eceff1")
    text_center((980, 676), "IMG / MODE / OBSTACLE", font(18), "#80cbc4")
    d.rounded_rectangle((716, 800, 1244, 912), radius=12, fill="#fff", outline="#b0bec5", width=2)
    text_center((980, 844), "GPIOs (glass_pair_cam)", f_lbl)
    d.text((740, 876), "TRIG -> GPIO 23", font=f_pin, fill="#00695c")
    d.text((740, 908), "ECHO -> GPIO 33", font=f_pin, fill="#00695c")
    d.text((1040, 876), "Boton A -> GPIO 13", font=f_pin, fill="#00695c")
    d.text((1040, 908), "Boton B -> GPIO 12", font=f_pin, fill="#00695c")

    # Ultrasonic
    box(1400, 176, 488, 440, "Ultrasonic Sensor")
    d.rounded_rectangle((1560, 264, 1760, 376), radius=12, fill="#cfd8dc", outline="#607d8b", width=2)
    d.ellipse((1616, 308, 1656, 348), fill="#455a64")
    d.ellipse((1704, 308, 1744, 348), fill="#455a64")
    text_center((1660, 412), "HC-SR04", f_lbl)
    d.text((1448, 336), "+5V", font=f_sm)
    d.text((1448, 376), "GND", font=f_sm)
    d.text((1448, 416), "TRIG", font=f_pin, fill="#00695c")
    d.text((1448, 456), "ECHO", font=f_pin, fill="#00695c")
    d.rounded_rectangle((1432, 496, 1648, 584), radius=8, fill="#f1f8e9", outline="#7cb342", width=2)
    text_center((1540, 528), "Filtro ECHO", f_sm)
    text_center((1540, 556), "1 k + divisor 3.3 V", f_sm)
    d.line([(536, 536), (1400, 536), (1400, 296), (1560, 296)], fill="#c62828", width=5)
    d.line([(116, 460), (116, 600), (1440, 600), (1440, 360), (1560, 360)], fill="#212121", width=4)
    d.line([(1280, 560), (1400, 560), (1400, 416), (1560, 416)], fill="#2e7d32", width=4)
    d.line([(1560, 440), (1432, 520), (1280, 600)], fill="#2e7d32", width=4)

    # Power to CAM
    d.line([(536, 536), (680, 536), (680, 640), (800, 640)], fill="#c62828", width=5)
    d.line([(116, 460), (116, 760), (800, 760), (800, 720)], fill="#212121", width=4)

    # Buttons
    box(72, 640, 400, 240, "Controles")
    d.rounded_rectangle((116, 724, 244, 796), radius=36, fill="#b2dfdb", outline="#00838f", width=2)
    text_center((180, 752), "Boton A", f_sm)
    text_center((180, 776), "modo", f_pin)
    d.rounded_rectangle((296, 724, 424, 796), radius=36, fill="#b2dfdb", outline="#00838f", width=2)
    text_center((360, 752), "Boton B", f_sm)
    text_center((360, 776), "accion", f_pin)
    d.line([(244, 760), (680, 840)], fill="#2e7d32", width=4)
    d.line([(424, 760), (680, 880)], fill="#2e7d32", width=4)
    d.line([(180, 796), (180, 860), (720, 860)], fill="#212121", width=4)

    # Cloud / data link
    box(72, 936, 1816, 440, None, "#00acc1", "#fafafa")
    d.text((96, 992), "Enlace de datos (voz y vision por red)", font=f_box, fill="#006064")
    d.text(
        (96, 1032),
        "Sustituye ESP32 Dev + INMP441 + MAX98357A - audio en el telefono",
        font=f_sm,
        fill="#546e7a",
    )
    d.ellipse((400, 1180, 576, 1296), fill="#e1f5fe", outline="#0288d1", width=3)
    text_center((488, 1208), "WiFi LAN", f_lbl)
    text_center((488, 1240), "WebSocket :8000", f_sm)
    d.rounded_rectangle((720, 1080, 1160, 1280), radius=20, fill="#263238")
    text_center((940, 1144), "Servidor Python", font(28, True), "#ffffff")
    text_center((940, 1188), "Quart / ws_bridge / LiveKit", font(20), "#80cbc4")
    text_center((940, 1224), "Groq / Edge TTS", font(20), "#80cbc4")
    text_center((940, 1256), "Vision + voz -> panel /app", font(18), "#b0bec5")
    d.rounded_rectangle((1320, 1096, 1560, 1264), radius=28, fill="#fff", outline="#00838f", width=4)
    d.rounded_rectangle((1376, 1124, 1504, 1220), radius=12, fill="#e0f2f1")
    text_center((1440, 1296), "Telefono", f_lbl, "#00695c")
    text_center((1440, 1324), "TTS + modos", f_sm)

    # WiFi dashed lines
    for seg in [
        [(1160, 640), (1160, 960), (400, 960), (400, 1080)],
        [(576, 1180), (720, 1180)],
        [(1160, 1180), (1320, 1180)],
    ]:
        for i in range(len(seg) - 1):
            d.line([seg[i], seg[i + 1]], fill="#0277bd", width=5)

    # Omitted
    d.rounded_rectangle((1560, 1080, 1856, 1256), radius=12, outline="#ef9a9a", width=2)
    for i, t in enumerate(["ESP32 Dev", "INMP441", "MAX98357A"]):
        d.text((1620, 1108 + i * 36), t, font=f_sm, fill="#c62828")
    d.text((1620, 1216), "(omitido)", font=f_sm, fill="#78909c")

    # Legend
    ly = 1380
    d.line([(72, ly), (128, ly)], fill="#c62828", width=5)
    d.text((140, ly - 10), "+5V", font=f_sm)
    d.line([(232, ly), (288, ly)], fill="#212121", width=4)
    d.text((300, ly - 10), "GND", font=f_sm)
    d.line([(392, ly), (448, ly)], fill="#2e7d32", width=4)
    d.text((460, ly - 10), "Senal GPIO", font=f_sm)
    d.line([(600, ly), (656, ly)], fill="#0277bd", width=5)
    d.text((668, ly - 10), "WiFi / datos", font=f_sm)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
