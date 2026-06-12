"""Generate BackstageHero brand art (a Guitar Hero-flavoured flame star + wordmark).

Produces:
  assets/logo.png    horizontal lockup for the app header  (transparent)
  assets/splash.png  startup splash (matches the header)    (opaque, dark)

Run directly, or call build_brand() from build.py.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Flame palette (Guitar Hero star-power vibe)
F_TOP = (255, 196, 92)
F_MID = (255, 120, 42)
F_BOT = (214, 30, 28)
TEXT  = (237, 240, 250)
SUBT  = (150, 156, 180)
BG    = (24, 24, 37)
SS    = 4   # supersample factor for smooth edges


def _font(size, heavy=True):
    names = (['seguibl.ttf', 'ariblk.ttf', 'arialbd.ttf', 'segoeuib.ttf']
             if heavy else ['segoeuisb.ttf', 'segoeui.ttf', 'arial.ttf'])
    for nm in names:
        try:
            return ImageFont.truetype(nm, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _vgrad(w, h, stops):
    """Vertical gradient as an RGB image. stops = [(pos0..1, (r,g,b)), ...]."""
    col = Image.new('RGB', (1, h))
    for y in range(h):
        t = y / (h - 1) if h > 1 else 0.0
        # find surrounding stops
        c = stops[-1][1]
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                f = (t - p0) / (p1 - p0) if p1 > p0 else 0
                c = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
                break
        col.putpixel((0, y), c)
    return col.resize((w, h))


def _star_pts(cx, cy, outer, inner, rot=-math.pi / 2):
    pts = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = rot + i * math.pi / 5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def star_badge(size):
    """A flame-gradient 5-point star with a bevel highlight, dark rim and drop
    shadow. rendered big then downscaled for clean edges."""
    sz = size * SS
    pad = int(sz * 0.16)
    full = sz + pad * 2
    cx = cy = full / 2
    outer = sz * 0.48
    inner = sz * 0.205

    out = Image.new('RGBA', (full, full), (0, 0, 0, 0))

    # drop shadow
    sh = Image.new('L', (full, full), 0)
    ImageDraw.Draw(sh).polygon(
        _star_pts(cx, cy + sz * 0.03, outer, inner), fill=150)
    sh = sh.filter(ImageFilter.GaussianBlur(sz * 0.05))
    shadow = Image.new('RGBA', (full, full), (0, 0, 0, 0))
    shadow.putalpha(sh)
    out = Image.alpha_composite(out, shadow)

    # star mask + flame gradient fill
    mask = Image.new('L', (full, full), 0)
    ImageDraw.Draw(mask).polygon(_star_pts(cx, cy, outer, inner), fill=255)
    grad = _vgrad(full, full,
                  [(0.0, F_TOP), (0.45, F_MID), (1.0, F_BOT)]).convert('RGBA')
    grad.putalpha(mask)

    # bevel: bright highlight over the upper part of the star
    hi = Image.new('L', (full, full), 0)
    ImageDraw.Draw(hi).polygon(
        _star_pts(cx, cy - sz * 0.04, outer * 0.82, inner * 0.82), fill=130)
    hi = hi.filter(ImageFilter.GaussianBlur(sz * 0.03))
    hi = Image.composite(hi, Image.new('L', (full, full), 0), mask)
    white = Image.new('RGBA', (full, full), (255, 255, 255, 0))
    white.putalpha(hi)
    star = Image.alpha_composite(grad, white)

    # crisp dark rim for definition
    ImageDraw.Draw(star).line(
        _star_pts(cx, cy, outer, inner) + [_star_pts(cx, cy, outer, inner)[0]],
        fill=(120, 20, 20, 200), width=max(2, int(sz * 0.012)), joint='curve')

    out = Image.alpha_composite(out, star)
    return out.resize((int(full / SS), int(full / SS)), Image.LANCZOS)


def _wordmark(height):
    """'BackstageHero', Backstage in light and Hero in flame, slightly sheared
    for a rock-poster slant, with a soft drop shadow."""
    fs = int(height * SS)
    f = _font(fs)
    a_txt, b_txt = 'Backstage', 'Hero'
    tmp = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    aw = int(tmp.textlength(a_txt, font=f))
    bw = int(tmp.textlength(b_txt, font=f))
    asc, desc = f.getmetrics()
    th = asc + desc
    pad = int(fs * 0.5)
    W, H = aw + bw + pad * 2, th + pad

    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((pad, pad // 2), a_txt, font=f, fill=TEXT)
    # 'Hero' filled with the flame gradient via mask
    hmask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(hmask).text((pad + aw, pad // 2), b_txt, font=f, fill=255)
    hgrad = _vgrad(W, H, [(0.0, F_TOP), (0.5, F_MID), (1.0, F_BOT)]).convert('RGBA')
    hgrad.putalpha(hmask)
    layer = Image.alpha_composite(layer, hgrad)

    # shear for slant
    shear = -0.18
    layer = layer.transform(
        (W + int(abs(shear) * H), H), Image.AFFINE,
        (1, shear, 0, 0, 1, 0), resample=Image.BICUBIC)

    # drop shadow
    sh = layer.split()[3].filter(ImageFilter.GaussianBlur(fs * 0.03))
    shadow = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    shadow.putalpha(sh)
    base = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    base = Image.alpha_composite(base, Image.composite(
        Image.new('RGBA', layer.size, (0, 0, 0, 160)),
        Image.new('RGBA', layer.size, (0, 0, 0, 0)), sh))
    base.alpha_composite(layer, (0, -int(fs * 0.02)))
    return base.resize((int(layer.size[0] / SS), int(layer.size[1] / SS)),
                       Image.LANCZOS)


def build_brand(assets='assets'):
    os.makedirs(assets, exist_ok=True)

    # --- header lockup: star + wordmark side by side ---
    star = star_badge(46)
    word = _wordmark(34)
    gap = 12
    H = max(star.height, word.height)
    W = star.width + gap + word.width
    logo = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    logo.alpha_composite(star, (0, (H - star.height) // 2))
    logo.alpha_composite(word, (star.width + gap, (H - word.height) // 2))
    logo.save(os.path.join(assets, 'logo.png'))

    # --- splash: dark card, glow, big star, wordmark below ---
    W, H = 460, 260
    sp = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(sp)
    d.rectangle([0, 0, W - 1, H - 1], outline=(56, 56, 80))
    # warm radial glow behind the star
    glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W / 2 - 90, 28, W / 2 + 90, 168],
                                 fill=(255, 110, 40, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(34))
    sp = Image.alpha_composite(sp.convert('RGBA'), glow).convert('RGB')

    big_star = star_badge(104)
    sp.paste(big_star, (int((W - big_star.width) / 2), 26), big_star)
    word2 = _wordmark(40)
    sp.paste(word2, (int((W - word2.width) / 2), 150), word2)
    sub = _font(13, heavy=False)
    dd = ImageDraw.Draw(sp)
    t = 'Starting up...'
    dd.text(((W - dd.textlength(t, font=sub)) / 2, 206), t, font=sub, fill=SUBT)
    sp.save(os.path.join(assets, 'splash.png'))


if __name__ == '__main__':
    build_brand()
    print('brand art written to assets/')
