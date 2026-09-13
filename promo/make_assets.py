"""Prepare reviewed chat captures, synthetic management UI and original music.

Only flattened, reviewed screenshots in docs/media are accepted as chat input.
Raw captures remain outside the repository and are never read during rendering.
"""
import os
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

root = Path(os.environ.get('CHZ_PROMO_DIR', Path.home() / '.local/share/chz-cli/promo-preview-20260913'))
assets = root / 'assets'
assets.mkdir(parents=True, exist_ok=True)

FONT = '/System/Library/Fonts/Hiragino Sans GB.ttc'


def font(size, bold=False):
    return ImageFont.truetype(FONT, size=size, index=1 if bold else 0)


def text(draw, xy, value, size, fill, bold=False, anchor=None):
    draw.text(xy, value, font=font(size, bold), fill=fill, anchor=anchor)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def make_chat_captures():
    source = Path(__file__).resolve().parents[1] / 'docs/media'
    # Include the matching conversation and sticker, excluding unused chrome.
    for name, box, output in [
        ('wechat-sanitized.png', (0, 385, 1096, 1185), 'wechat-real-chat.png'),
        ('feishu-sanitized.png', (30, 386, 790, 1194), 'feishu-real-chat.png'),
    ]:
        with Image.open(source / name) as image:
            image.crop(box).convert('RGB').save(assets / output)


def make_progress():
    w, h = 500, 126
    im = Image.new('RGB', (w, h), '#f9fcfa')
    d = ImageDraw.Draw(im)
    text(d, (20, 17), '产品协作群 → 产品协作镜像', 18, '#284a3b', True)
    text(d, (20, 42), 'Profile · demo-mirror', 13, '#789184')
    rounded(d, (20, 58, 194, 100), 9, '#dcefe3')
    text(d, (38, 79), '已追平', 17, '#287756', True, 'lm')
    rounded(d, (208, 58, 480, 100), 9, '#eef5f0')
    text(d, (226, 79), '0 条待发送 · 游标 0248', 15, '#547466', anchor='lm')
    text(d, (20, 116), '心跳正常 · 失败项 0 · 10 分钟检查', 12, '#668679')
    im.save(assets / 'progress-crop.png')


def make_overview():
    w, h = 1280, 720
    im = Image.new('RGB', (w, h), '#f4f7f5')
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 220, h), fill='#ffffff')
    d.line((220, 0, 220, h), fill='#dfe8e2', width=1)
    rounded(d, (22, 30, 62, 70), 12, '#2f9d76')
    text(d, (42, 50), '↗', 25, '#ffffff', True, 'mm')
    text(d, (76, 40), '会话同步', 24, '#193b31', True)
    text(d, (22, 88), '本机管理台 · 演示', 14, '#799287')
    nav = ['运行概览', '群映射', '飞书应用', '人员与实名', '失败与补发', '接入引导']
    for i, label in enumerate(nav):
        y = 135 + i * 54
        if i == 0:
            rounded(d, (20, y - 12, 200, y + 28), 11, '#e6f3ec')
        text(d, (34, y + 8), label, 16, '#367761' if i == 0 else '#344e43', i == 0, 'lm')
    text(d, (22, 640), '由 Codex 控制与维护', 14, '#71877c')
    text(d, (22, 666), '演示数据 · 未连接真实群组', 13, '#8ca097')

    text(d, (260, 51), 'WECHAT  →  FEISHU', 13, '#4d8b72', True)
    text(d, (260, 85), '运行概览', 32, '#193b31', True)
    text(d, (1132, 62), '服务在线 · 演示', 13, '#5b7669', anchor='rm')
    rounded(d, (1152, 42, 1240, 78), 9, '#ffffff', '#d8e4dc')
    text(d, (1196, 60), '刷新', 13, '#48695b', anchor='mm')

    cards = [('运行中的镜像', '1'), ('待发送消息', '0'), ('媒体待处理', '0'), ('待确认实名', '0')]
    for i, (label, value) in enumerate(cards):
        x = 260 + i * 244
        rounded(d, (x, 130, x + 224, 236), 14, '#ffffff', '#dce7df')
        text(d, (x + 22, 159), label, 14, '#70877b')
        text(d, (x + 22, 207), value, 34, '#203a31', True)

    rounded(d, (260, 260, 1240, 368), 14, '#ffffff', '#dce7df')
    text(d, (282, 290), '恢复后，先补齐离线消息', 20, '#203a31', True)
    text(d, (282, 326), 'SQLite 保存游标，按顺序处理积压；异常退出后由 Codex 控制恢复。', 14, '#71877c')

    rounded(d, (260, 392, 1240, 540), 14, '#ffffff', '#dce7df')
    text(d, (282, 423), '镜像关系', 14, '#6b8276', True)
    text(d, (655, 423), '运行与补发', 14, '#6b8276', True)
    text(d, (929, 423), '最近活动', 14, '#6b8276', True)
    d.line((260, 445, 1240, 445), fill='#e4ece6', width=1)
    text(d, (282, 477), '产品协作群 → 产品协作镜像', 16, '#203a31', True)
    text(d, (282, 506), 'demo-mirror', 13, '#769184')
    rounded(d, (655, 463, 726, 491), 7, '#dcefe3')
    text(d, (690, 477), '已追平', 13, '#287756', True, 'mm')
    text(d, (655, 518), '0 条待发送 · 游标 0248', 13, '#547466')
    text(d, (929, 477), '心跳正常', 13, '#547466')
    text(d, (929, 506), '演示状态 · 不含真实数据', 13, '#769184')
    im.save(assets / 'overview.png')


make_chat_captures()
make_progress()
make_overview()

# Original 96 BPM instrumental. No third-party samples or user recordings.
sr = 48000
duration = 29
mix = np.zeros((sr * duration, 2), dtype=np.float64)
rng = np.random.default_rng(63)
beat = 60 / 96


def add(sound, start, volume=1, pan=0):
    offset = round(start * sr)
    n = min(len(sound), len(mix) - offset)
    if n <= 0:
        return
    stereo = np.column_stack((sound[:n] * np.sqrt((1 - pan) / 2), sound[:n] * np.sqrt((1 + pan) / 2)))
    mix[offset:offset + n] += stereo * volume


def note(midi, length, timbre='pluck'):
    t = np.arange(round(sr * length)) / sr
    freq = 440 * 2 ** ((midi - 69) / 12)
    if timbre == 'pad':
        v = .66 * np.sin(2 * np.pi * freq * t) + .18 * np.sin(2 * np.pi * freq * 1.003 * t) + .13 * np.sin(2 * np.pi * 2 * freq * t)
        env = np.minimum(t / .22, 1) * np.minimum((length - t) / .45, 1)
    elif timbre == 'bass':
        v = np.sin(2 * np.pi * freq * t) + .12 * np.sin(2 * np.pi * freq * 2 * t)
        env = (1 - np.exp(-t * 120)) * np.exp(-t * 4.4) * np.minimum((length - t) / .08, 1)
    else:
        v = np.sin(2 * np.pi * freq * t) + .26 * np.sin(2 * np.pi * freq * 2 * t) + .12 * np.sin(2 * np.pi * freq * 3 * t)
        env = (1 - np.exp(-t * 350)) * np.exp(-t * 5.3) * np.minimum((length - t) / .1, 1)
    return v * env


chords = [(48, [60, 64, 67, 71, 74]), (45, [57, 60, 64, 67, 72]), (41, [57, 60, 64, 67, 72]), (48, [60, 64, 67, 71, 76])]
for bar in range(10):
    bass, tones = chords[bar % 4]
    start = bar * 4 * beat
    for j, midi in enumerate(tones[:4]):
        add(note(midi, 2.8, 'pad'), start, .056, pan=(j - 1.5) / 2.5)
    pattern = [0, 2, 1, 3, 2, 4, 3, 1]
    for step, idx in enumerate(pattern):
        pos = start + step * beat / 2
        s = note(tones[idx] + 12, .95)
        add(s, pos, .09 if step % 2 == 0 else .066, pan=-.28 if step % 2 == 0 else .28)
        add(s, pos + beat * .75, .027, pan=.58)
        add(s, pos + beat * 1.5, .013, pan=-.6)
    for step in [0, 2]:
        add(note(bass - 12, .8, 'bass'), start + step * beat, .23)

for b in range(47):
    start = b * beat
    t = np.arange(int(sr * .38)) / sr
    phase = 2 * np.pi * (49 * t + 85 * .022 * (1 - np.exp(-t / .022)))
    add(np.sin(phase) * np.exp(-t * 13) * (1 - np.exp(-t * 500)), start, .3)
    if b % 2:
        t = np.arange(int(sr * .16)) / sr
        noise = rng.normal(size=len(t)); noise -= np.convolve(noise, np.ones(17) / 17, mode='same')
        add(noise * np.exp(-t * 38) * (1 - np.exp(-t * 1600)), start, .052, pan=.1)
    for sub in [0, .5]:
        t = np.arange(int(sr * .07)) / sr
        noise = rng.normal(size=len(t)); noise -= np.convolve(noise, np.ones(8) / 8, mode='same')
        add(noise * np.exp(-t * 90) * (1 - np.exp(-t * 1300)), start + beat * sub, .015 if sub == 0 else .024, pan=-.45 if sub == 0 else .45)

for end in [4.0, 13.0, 18.0, 25.0]:
    t = np.arange(int(sr * .5)) / sr
    noise = rng.normal(size=len(t)); noise = np.convolve(noise, np.ones(12) / 12, mode='same')
    add(noise * np.sin(np.pi * t / .5) ** 1.7, end - .42, .045, pan=.2)

dry = mix.copy()
for delay, gain in [(.113, .11), (.173, .075), (.263, .035)]:
    d = round(delay * sr)
    mix[d:] += dry[:-d, ::-1] * gain
t = np.arange(len(mix)) / sr
mix *= np.minimum(t / .04, 1)[:, None]
mix *= np.clip((duration - t) / .72, 0, 1)[:, None]
mix = np.tanh(mix * 1.1)
mix *= .78 / np.max(np.abs(mix))
with wave.open(str(assets / 'soundtrack.wav'), 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes((mix * 32767).astype('<i2').tobytes())

print('Prepared reviewed chat captures, demo management UI and original 96 BPM stereo soundtrack.')
