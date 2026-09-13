"""Crop local CUA captures and compose an original ten-second instrumental."""
import os
import wave
from pathlib import Path

import numpy as np
from PIL import Image

root = Path(os.environ.get('CHZ_PROMO_DIR', Path.home()/'.local/share/chz-cli/promo-preview-20260913'))
assets = root/'assets'
for source, output, box in [
    ('wechat-window.png', 'wechat-crop.png', (353, 3, 1027, 253)),
    ('feishu-match.png', 'feishu-header.png', (69, 4, 715, 73)),
    ('feishu-match.png', 'feishu-content.png', (121, 348, 485, 647)),
    ('overview.png', 'progress-crop.png', (647, 438, 916, 548)),
]:
    Image.open(assets/source).crop(box).save(assets/output)

sr = 48000
duration = 29
mix = np.zeros((sr*duration, 2), dtype=np.float64)
rng = np.random.default_rng(63)
beat = 60/96

def add(sound, start, volume=1, pan=0):
    offset = round(start*sr)
    n = min(len(sound), len(mix)-offset)
    if n <= 0:
        return
    stereo = np.column_stack((sound[:n]*np.sqrt((1-pan)/2), sound[:n]*np.sqrt((1+pan)/2)))
    mix[offset:offset+n] += stereo*volume

def note(midi, length, timbre='pluck'):
    t = np.arange(round(sr*length))/sr
    freq = 440*2**((midi-69)/12)
    if timbre == 'pad':
        v = .66*np.sin(2*np.pi*freq*t)+.18*np.sin(2*np.pi*freq*1.003*t)+.13*np.sin(2*np.pi*2*freq*t)
        env = np.minimum(t/.22,1)*np.minimum((length-t)/.45,1)
    elif timbre == 'bass':
        v = np.sin(2*np.pi*freq*t)+.12*np.sin(2*np.pi*freq*2*t)
        env = (1-np.exp(-t*120))*np.exp(-t*4.4)*np.minimum((length-t)/.08,1)
    else:
        v = np.sin(2*np.pi*freq*t)+.26*np.sin(2*np.pi*freq*2*t)+.12*np.sin(2*np.pi*freq*3*t)
        env = (1-np.exp(-t*350))*np.exp(-t*5.3)*np.minimum((length-t)/.1,1)
    return v*env

# A warm eight-bar loop, repeated for the 29 second cut.
chords = [(48,[60,64,67,71,74]),(45,[57,60,64,67,72]),(41,[57,60,64,67,72]),(48,[60,64,67,71,76])]
for bar in range(10):
    bass,tones=chords[bar%4]
    start = bar*4*beat
    for j,midi in enumerate(tones[:4]):
        add(note(midi,2.8,'pad'),start,.056,pan=(j-1.5)/2.5)
    pattern=[0,2,1,3,2,4,3,1]
    for step,idx in enumerate(pattern):
        pos = start+step*beat/2
        s = note(tones[idx]+12,.95)
        add(s,pos,.09 if step%2==0 else .066,pan=-.28 if step%2==0 else .28)
        add(s,pos+beat*.75,.027,pan=.58)
        add(s,pos+beat*1.5,.013,pan=-.6)
    for step in [0,2]:
        add(note(bass-12,.8,'bass'),start+step*beat,.23)

for b in range(47):
    start=b*beat
    t=np.arange(int(sr*.38))/sr
    phase=2*np.pi*(49*t+85*.022*(1-np.exp(-t/.022)))
    kick=np.sin(phase)*np.exp(-t*13)*(1-np.exp(-t*500))
    add(kick,start,.3)
    if b%2:
        t=np.arange(int(sr*.16))/sr
        noise=rng.normal(size=len(t)); noise-=np.convolve(noise,np.ones(17)/17,mode='same')
        add(noise*np.exp(-t*38)*(1-np.exp(-t*1600)),start,.052,pan=.1)
    for sub in [0,.5]:
        t=np.arange(int(sr*.07))/sr
        noise=rng.normal(size=len(t)); noise-=np.convolve(noise,np.ones(8)/8,mode='same')
        add(noise*np.exp(-t*90)*(1-np.exp(-t*1300)),start+beat*sub,.015 if sub==0 else .024,pan=-.45 if sub==0 else .45)

# Soft swells land on the two editorial transitions, without competing with the melody.
for end in [4.0,13.0,18.0,25.0]:
    t=np.arange(int(sr*.5))/sr
    noise=rng.normal(size=len(t)); noise=np.convolve(noise,np.ones(12)/12,mode='same')
    env=np.sin(np.pi*t/.5)**1.7
    add(noise*env,end-.42,.045,pan=.2)

# Gentle space, then short fades and peak normalization. No borrowed music or samples.
dry=mix.copy()
for delay,gain in [(.113,.11),(.173,.075),(.263,.035)]:
    d=round(delay*sr)
    mix[d:] += dry[:-d,::-1]*gain
t=np.arange(len(mix))/sr
mix*=np.minimum(t/.04,1)[:,None]
mix*=np.clip((duration-t)/.72,0,1)[:,None]
mix=np.tanh(mix*1.1)
mix*=.78/np.max(np.abs(mix))
with wave.open(str(assets/'soundtrack.wav'),'wb') as w:
    w.setnchannels(2);w.setsampwidth(2);w.setframerate(sr)
    w.writeframes((mix*32767).astype('<i2').tobytes())
print('Prepared four crops and original 96 BPM stereo soundtrack.')
