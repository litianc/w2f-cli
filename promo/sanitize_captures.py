"""Flatten identity replacements into the two reviewed screenshot layouts.

Inputs stay outside the repository. This coordinate template is only valid for
the documented dimensions; new captures require a new visual review.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = '/System/Library/Fonts/Hiragino Sans GB.ttc'


def label(draw, xy, value, size, color, bold=False, anchor='lm'):
    draw.text(xy, value, fill=color, font=ImageFont.truetype(FONT, size, index=int(bold)), anchor=anchor)


def avatar(draw, box, value, background):
    draw.rounded_rectangle(box, radius=18, fill=background)
    x1, y1, x2, y2 = box
    label(draw, ((x1 + x2) / 2, (y1 + y2) / 2), value, 32, '#e5f6ed', True, 'mm')


def read_capture(path, size):
    with Image.open(path) as source:
        if source.size != size:
            raise ValueError(f'Unexpected capture dimensions; expected {size}')
        # Fresh RGB canvas discards alpha, EXIF, text chunks and other metadata.
        image = Image.new('RGB', size)
        image.paste(source.convert('RGB'))
        return image


def sanitize_wechat(path):
    image = read_capture(path, (1096, 1192))
    d = ImageDraw.Draw(image)
    bg = image.getpixel((450, 60))
    d.rectangle((20, 24, 500, 88), fill=bg)
    label(d, (28, 57), '产品协作群 (3)', 32, '#dedee0', True)
    d.rectangle((505, 334, 596, 380), fill=bg)
    for top in (534, 704, 874):
        d.rectangle((32, top - 3, 125, top + 90), fill=bg)
        avatar(d, (36, top, 122, top + 86), '周', '#385b51')
        d.rectangle((145, top + 2, 305, top + 44), fill=bg)
        label(d, (150, top + 24), '小周', 26, '#949497')
    d.rectangle((978, 408, 1074, 504), fill=bg)
    avatar(d, (982, 412, 1068, 498), '林', '#426576')
    return image


def sanitize_feishu(path):
    image = read_capture(path, (1718, 1312))
    d = ImageDraw.Draw(image)
    bg = image.getpixel((900, 600))
    d.rectangle((115, 40, 353, 99), fill=bg)
    label(d, (120, 72), '产品协作镜像', 32, '#eeeeee', True)
    d.rectangle((120, 386, 317, 434), fill=bg)
    label(d, (124, 410), 'W2F CLI', 26, '#919191')
    d.rectangle((409, 387, 1610, 436), fill=bg)
    label(d, (428, 410), '由 Codex 管理 · 微信 → 飞书', 24, '#797979')
    d.rectangle((35, 383, 109, 458), fill=bg)
    avatar(d, (40, 388, 104, 452), '↗', '#536d68')
    for box, content, center_y in [
        ((142, 451, 514, 508), '小林：你们今天去玩了吗', 479),
        ((142, 541, 424, 597), '小周：哈哈，没有', 567),
        ((142, 628, 424, 685), '小周：就那么一说', 655),
        ((142, 715, 284, 774), '小周：', 743),
    ]:
        d.rectangle(box, fill=image.getpixel((135, center_y)))
        label(d, (149, center_y), content, 28, '#e9e9e9')
    d.rectangle((25, 970, 116, 1015), fill=bg)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wechat', type=Path, required=True)
    parser.add_argument('--feishu', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'docs/media')
    args = parser.parse_args()
    wechat, feishu = sanitize_wechat(args.wechat), sanitize_feishu(args.feishu)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    wechat.save(args.output_dir / 'wechat-sanitized.png', optimize=True)
    feishu.save(args.output_dir / 'feishu-sanitized.png', optimize=True)
    print('Saved flattened screenshots. Visual review is required before publication.')


if __name__ == '__main__':
    main()
