import importlib.util
import os
import sys
import types
sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=object)
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('shadowprep', 'prep.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert mod.extract_video_id('https://youtu.be/dkreokWH-04') == 'dkreokWH-04'
assert mod.extract_video_id('https://www.youtube.com/watch?v=7G8404vvKK0&t=3') == '7G8404vvKK0'
assert mod.extract_video_id('https://www.youtube.com/shorts/dkreokWH-04') == 'dkreokWH-04'
chunks = [
    {'text':'Hello there. How are you?', 'offset':1000, 'duration':3000},
    {'text':'I am fine.', 'offset':5000, 'duration':1700},
]
words = mod.timed_chunks_to_words(chunks)
assert len(words) == 8, words
assert words[0][1] == 1
assert round(words[-1][2], 1) == 6.7
assert all(w[2] > w[1] for w in words)
assert len(mod.segment_rules(words)) >= 2
with patch.object(mod, 'supadata_get', return_value=(200, {'content':chunks, 'lang':'en'})):
    data = mod.supadata_transcript('https://youtu.be/dkreokWH-04', 'en')
    assert data['content'] == chunks
print('OK: URL 처리, 구간 → 단어 타이밍, 자막 API 변환')

import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as td:
    original_out = mod.OUT_DIR
    mod.OUT_DIR = Path(td)
    fake = ({'id':'dkreokWH-04', 'title':'시험 영상', 'channel':'테스트', 'duration':10},
            words, 'en')
    seg = {'lang':'en','mode':'rules','client':None,'model':'test','local':None,'local_failed':False}
    with patch.dict(os.environ, {'SHADOW_CAPTION_PROVIDER':'supadata'}), patch.object(mod,'get_supadata_video',return_value=fake):
        assert mod.process_video('https://youtu.be/dkreokWH-04', seg, False)
        saved = __import__('json').loads((Path(td)/'dkreokWH-04.json').read_text())
        assert saved['id'] == 'dkreokWH-04'
        assert saved['sentences'] and saved['sentences'][0]['words']
    mod.OUT_DIR = original_out
print('OK: 기존 웹앱 호환 JSON 파일 생성')
