"""Generate cached translations for existing shadowing videos via Google Cloud Translation Basic v2.
Requires GOOGLE_TRANSLATE_API_KEY; never put the key in HTML or the repository.
"""
import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LANGS = ['ko', 'en', 'ja', 'zh-CN', 'es', 'fr', 'de']

def translate(batch, target, source, key):
    body = urllib.parse.urlencode([('q', s) for s in batch] + [('target',target), ('source',source), ('format','text'), ('key',key)]).encode('utf-8')
    req=urllib.request.Request('https://translation.googleapis.com/language/translate/v2',data=body,headers={'Content-Type':'application/x-www-form-urlencoded; charset=utf-8'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                obj=json.load(response)
            texts=obj['data']['translations']
            if len(texts)!=len(batch): raise RuntimeError('Translation response count mismatch')
            return [html.unescape(x['translatedText']) for x in texts]
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
            if attempt==2: raise RuntimeError(f'Google translation failed: {exc}') from exc
            time.sleep(2**attempt)

def run(target, video_id):
    key=os.environ.get('GOOGLE_TRANSLATE_API_KEY','').strip()
    if not key: raise RuntimeError('GOOGLE_TRANSLATE_API_KEY is missing. Configure it as a GitHub Actions secret.')
    folder=ROOT/'videos'
    paths=([folder/f'{video_id}.json'] if video_id else sorted(folder.glob('*.json')))
    count=0
    for path in paths:
        if path.name=='index.json' or not path.is_file(): continue
        doc=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(doc.get('sentences'),list) or not doc['sentences'] or not doc.get('id'): continue
        source=(doc.get('subtitle') or {}).get('lang','en').split('-')[0]
        if source==target: continue
        texts=[s.get('text','').strip() for s in doc['sentences']]
        if not all(texts): continue
        translations=doc.setdefault('translations',{})
        if isinstance(translations.get(target),list) and len(translations[target])==len(texts):
            print(f'Skip {doc["id"]}: {target} already exists');continue
        result=[]
        for start in range(0,len(texts),60):
            batch=texts[start:start+60]
            result+=translate(batch,target,source,key)
            print(f'{doc["id"]}: {min(start+60,len(texts))}/{len(texts)} translated',flush=True)
        translations[target]=result
        temp=path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(doc,ensure_ascii=False,indent=1),encoding='utf-8')
        temp.replace(path)
        count+=1
    print(f'Updated {count} videos')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--target', choices=LANGS, default='ko')
    parser.add_argument('--video-id', default='',help='Optional 11-character YouTube video ID (empty = all videos)')
    args=parser.parse_args()
    try: run(args.target,args.video_id)
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr);sys.exit(1)
