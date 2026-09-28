"""Generated green pixels; no real chat images or local decoding keys."""
import base64
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from agents import media_images
from agents.wechat_feishu_mirror import ffmpeg_executable

# Generated with FFmpeg/libx265 from a solid 32x32 green lavfi frame, info=0.
HEVC = base64.b64decode('AAAAAUABDAH//wFgAAADAJAAAAMAAAMAHpWYCQAAAAFCAQEBYAAAAwCQAAADAAADAB6gQghZZWa5MrwFoCAAAAMAIAAAAwMhAAAAAUQBwXPQiQAAASgBrx2A8A4I/+OYP5MI/AOw')
WXGF = b'wxgf' + bytes([19,0,2,0,32,0,32]) + bytes(8) + HEVC


class WxgfImageTest(unittest.TestCase):
    def test_real_codec_preserves_dimensions_and_pixels(self):
        executable=ffmpeg_executable()
        if not executable:self.skipTest('optional FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as directory:
            codec=Path(directory)/Path(executable).name
            shutil.copy2(executable,codec)
            with patch.object(media_images.shutil,'which',return_value=str(codec)):
                data=media_images.decode_wxgf(WXGF)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size,(32,32))
            r,g,b=image.convert('RGB').getpixel((16,16))
            self.assertGreater(g,100);self.assertLess(r,10);self.assertLess(b,10)
    def test_missing_codec_invalid_stream_and_oversized_header(self):
        with patch.object(media_images.shutil,'which',return_value=None),self.assertRaisesRegex(media_images.ImageResourceError,'wxgf_decoder_missing'):
            media_images.decode_wxgf(WXGF)
        for data in (b'wxgf',WXGF[:19],WXGF[:7]+b'\xff\xff\xff\xff'+WXGF[11:]):
            with self.subTest(size=len(data)), self.assertRaises(media_images.ImageResourceError):
                media_images.decode_wxgf(data)
    def test_multiframe_or_partial_result_never_silently_becomes_static(self):
        out=BytesIO();Image.new('RGB',(32,32),'green').save(out,format='PNG');png=out.getvalue()
        for result in (png+png,png[:-10],b''):
            with patch.object(media_images.shutil,'which',return_value='fixture-ffmpeg'),patch.object(media_images.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=result)),self.assertRaises(media_images.ImageResourceError):
                media_images.decode_wxgf(WXGF)
    def test_wrong_dimensions_are_rejected(self):
        out=BytesIO();Image.new('RGB',(8,8),'green').save(out,format='PNG')
        with patch.object(media_images.shutil,'which',return_value='fixture-ffmpeg'),patch.object(media_images.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=out.getvalue())),self.assertRaisesRegex(media_images.ImageResourceError,'尺寸不匹配'):
            media_images.decode_wxgf(WXGF)
