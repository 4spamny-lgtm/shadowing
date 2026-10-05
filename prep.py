"""
prep.py - 유튜브 쉐도잉용 자막 전처리 스크립트

사용법:
  python prep.py "유튜브주소"
  python prep.py "주소1" "주소2" "주소3"
  python prep.py "재생목록주소"          (https://www.youtube.com/playlist?list=...)

옵션:
  --seg local   (기본) 내 PC의 무료 문장 부호 모델로 문장 나누기
  --seg rules   모델 없이 쉬는 구간 기준으로만 나누기 (가장 빠름, 품질 낮음)
  --seg claude  Claude API 사용 (ANTHROPIC_API_KEY 필요, 선택 사항)
  --force       이미 처리한 영상도 다시 처리
  --queue       queue.txt에 쌓인 주소도 함께 처리하고, 실패한 주소는 queue.txt에 남김 (add.bat이 사용)

자막에 문장 부호가 이미 들어 있으면(수동 자막 등) 모델 없이 그 부호를 그대로 사용합니다.

결과:
  videos/{영상ID}.json   문장 목록 (텍스트, 시작/끝 시간, 단어별 타이밍)
  videos/index.json      영상 목록 (웹앱이 읽음)
"""
import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from yt_dlp import YoutubeDL
except ImportError:
    print('yt-dlp가 설치되어 있지 않습니다. 먼저 실행하세요:  pip install -U "yt-dlp[default]"')
    sys.exit(1)

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "videos"
DEFAULT_MODEL = "claude-sonnet-5-5"

MAX_WORDS = 28      # 이보다 긴 문장은 쉬는 구간에서 쪼갬
MIN_WORDS = 3       # 이보다 짧은 문장은 가까운 문장과 합침
MERGE_GAP = 1.0     # 짧은 문장을 합칠 때 허용하는 최대 간격(초)
PAUSE_GAP = 0.5     # 규칙 기반에서 문장 경계로 볼 쉼(초)
MAX_WORD_DUR = 1.2  # 한 단어의 최대 길이(초)

LOCAL_ACCEPT = 180  # 로컬 모델: 한 번에 확정하는 단어 수
LOCAL_CONTEXT = 40  # 로컬 모델: 뒤쪽 문맥으로 함께 보는 단어 수
CHUNK = 350         # Claude: 한 번에 보내는 단어 수

NOISE = re.compile(r"\[[^\]]*\]|♪+|>>")
SENT_END = re.compile(r"[.?!][\"'”’)]*$")
PUNCT = re.compile(r"[.,;:!?]")


# ---------------------------------------------------------------- 환경 설정

def load_dotenv():
    """같은 폴더의 .env 파일이 있으면 읽음 (선택 사항)"""
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- 자막 가져오기

def expand_urls(urls):
    """재생목록 주소는 개별 영상 주소로 펼침"""
    out = []
    for u in urls:
        if "list=" in u and "watch?" not in u and "youtu.be" not in u:
            with YoutubeDL({"quiet": True, "extract_flat": "in_playlist", "skip_download": True}) as ydl:
                info = ydl.extract_info(u, download=False)
            ids = [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]
            print(f"재생목록: {info.get('title')} ({len(ids)}개 영상)")
            out += [f"https://www.youtube.com/watch?v={i}" for i in ids]
        else:
            out.append(u)
    return out


def pick_subtitle(info):
    """수동 영어 자막 우선, 없으면 자동 자막"""
    subs = info.get("subtitles") or {}
    for k in ["en", "en-US", "en-GB", "en-CA", "en-AU"] + sorted(subs):
        if k in subs and k.lower().startswith("en"):
            return k, "manual"
    autos = info.get("automatic_captions") or {}
    if "en-orig" in autos:
        return "en-orig", "auto"
    if "en" in autos:
        return "en", "auto"
    return None, None


def download_json3(url, lang, kind):
    with tempfile.TemporaryDirectory() as td:
        opts = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "writesubtitles": kind == "manual",
            "writeautomaticsub": kind == "auto",
            "subtitleslangs": [lang],
            "subtitlesformat": "json3",
            "outtmpl": os.path.join(td, "%(id)s.%(ext)s"),
        }
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(Path(td).glob("*.json3"))
        if not files:
            raise RuntimeError("자막 파일을 받지 못했습니다.")
        return json.loads(files[0].read_text(encoding="utf-8"))


def est_dur(word):
    """자동 자막은 단어 시작 시간만 있어서, 단어 길이로 끝 시간을 추정"""
    return min(MAX_WORD_DUR, 0.2 + 0.08 * len(word))


def parse_json3(data):
    """json3 자막 → [단어, 시작초, 끝초] 목록"""
    words = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        t0 = ev.get("tStartMs", 0) / 1000
        t_end = t0 + ev.get("dDurationMs", 0) / 1000
        pieces = []
        for seg in segs:
            txt = NOISE.sub(" ", html.unescape(seg.get("utf8", "")))
            if txt.strip():
                pieces.append((t0 + seg.get("tOffsetMs", 0) / 1000, txt))
        for k, (st, txt) in enumerate(pieces):
            nxt = pieces[k + 1][0] if k + 1 < len(pieces) else t_end
            toks = txt.split()
            span = max(nxt - st, 0.05 * len(toks))
            total = sum(len(t) for t in toks) or 1
            cur = st
            # 수동 자막처럼 한 조각에 여러 단어가 있으면 글자 수 비율로 시간 배분
            for t in toks:
                d = span * len(t) / total
                words.append([t, cur, cur + d])
                cur += d
    words.sort(key=lambda w: w[1])
    for i, w in enumerate(words):
        nxt = words[i + 1][1] if i + 1 < len(words) else float("inf")
        w[2] = min(w[2], nxt, w[1] + est_dur(w[0]))
        w[2] = max(w[2], w[1] + 0.05)
    return words


# ---------------------------------------------------------------- 문장 나누기 공통

def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def raw_text(words, a, b):
    return " ".join(w[0] for w in words[a:b + 1])


def finish_text(s, prev):
    """문장 첫 글자와 끝 부호 정리.
    앞 조각이 문장 중간에서 잘렸으면 이어지는 조각이라 대문자로 바꾸지 않음.
    중간에서 잘린 조각은 마침표 대신 쉼표를 그대로 두거나 '…'를 붙임."""
    s = s.strip()
    if not s:
        return s
    if not prev or SENT_END.search(prev):
        s = s[0].upper() + s[1:]
    if not SENT_END.search(s) and not s.endswith((",", ";", ":", "…")):
        s += "…"
    return s


# 긴 문장을 쪼갤 때: 이 단어 뒤에서 자르면 어색함 / 이 단어 앞에서 자르면 자연스러움
BAD_END = {"a", "an", "the", "to", "of", "and", "but", "or", "so", "because", "that",
           "with", "for", "in", "on", "at", "if", "when", "like", "my", "your", "our",
           "their", "his", "her", "i", "i'm", "is", "are", "was", "were", "just", "very"}
GOOD_START = {"and", "but", "so", "because", "when", "which", "who", "if", "then", "or",
              "while", "although", "though", "since", "until", "where", "after", "before"}


def plain(w):
    return re.sub(r"[^a-z']", "", w.lower())


def gap_after(words, i):
    return words[i + 1][1] - words[i][2]


def has_punct(words):
    """자막에 문장 부호가 충분히 들어 있는지"""
    enders = sum(1 for w in words if SENT_END.search(w[0]))
    return enders >= max(2, len(words) // 30)


def segment_rules(words):
    """문장 부호가 있으면 그것으로, 없으면 쉬는 구간으로 나눔"""
    n = len(words)
    if n == 0:
        return []
    use_punct = has_punct(words)
    ranges, a = [], 0
    for i in range(n):
        if i == n - 1:
            cut = True
        elif use_punct:
            cut = bool(SENT_END.search(words[i][0])) or gap_after(words, i) >= 1.5
        else:
            cut = gap_after(words, i) >= PAUSE_GAP
        if cut:
            ranges.append({"a": a, "b": i, "text": None})
            a = i + 1
    return ranges


# ---------------------------------------------------------------- 로컬 모델로 문장 나누기 (무료)

def load_local_model():
    try:
        from deepmultilingualpunctuation import PunctuationModel
    except ImportError:
        print("문장 부호 모델 패키지가 없어 규칙 기반으로 나눕니다.")
        print("  설치:  pip install deepmultilingualpunctuation")
        return None
    except Exception as e:
        print(f"문장 부호 모델을 불러오지 못해 규칙 기반으로 나눕니다: {e}")
        print("  Windows라면 이 설치가 필요할 수 있어요:  winget install Microsoft.VCRedist.2015+.x64")
        return None
    try:
        print("문장 부호 모델을 불러오는 중... (처음 한 번은 약 2GB를 내려받아 시간이 걸립니다)")
        return PunctuationModel()
    except Exception as e:
        print(f"모델을 불러오지 못해 규칙 기반으로 나눕니다: {e}")
        return None


def segment_local(words, model):
    n = len(words)
    if n == 0:
        return []
    labels = []
    i = 0
    while i < n:
        j = min(n, i + LOCAL_ACCEPT + LOCAL_CONTEXT)
        chunk = [PUNCT.sub("", w[0]) or w[0] for w in words[i:j]]
        accept = (j - i) if j == n else LOCAL_ACCEPT
        try:
            pred = model.predict(chunk)
            if len(pred) != len(chunk):
                raise ValueError("단어 수 불일치")
            labs = [str(p[1]) for p in pred]
        except Exception as e:
            print(f"    모델 처리 실패, 이 구간은 규칙 기반으로: {e}")
            labs = ["0"] * len(chunk)
            for r in segment_rules(words[i:j]):
                labs[r["b"]] = "."
        labels += labs[:accept]
        i += accept
        print(f"    문장 부호 넣는 중... {min(i, n)}/{n} 단어")

    ranges, a, toks = [], 0, []
    for k in range(n):
        base = PUNCT.sub("", words[k][0]) or words[k][0]
        if base.lower() == "i" or base.lower().startswith("i'"):
            base = "I" + base[1:]
        lab = labels[k]
        end = lab in (".", "?", "!") or k == n - 1
        tok = base + lab if lab in (",", ".", "?", "!", ":") else base
        if end and not SENT_END.search(tok):
            tok += "."
        toks.append(tok)
        if end:
            toks[0] = toks[0][0].upper() + toks[0][1:]
            ranges.append({"a": a, "b": k, "text": " ".join(toks)})
            a, toks = k + 1, []
    return ranges


# ---------------------------------------------------------------- Claude로 문장 나누기 (선택)

PROMPT = """You will receive an English speech transcript as a numbered word list in the form index:word.
It comes from automatic captions, so punctuation is missing or unreliable.

Split it into natural, complete sentences, the way a careful editor would punctuate it.

Rules:
- Never add, remove, reorder, or change words. Only add punctuation and capitalization.
- Keep filler words (um, uh, like, you know) inside the sentences they belong to.
- Every word belongs to exactly one sentence, in order.
- Aim for sentences of roughly 5 to 20 words. Split long run-on speech at natural clause boundaries (for example before "and", "but", "so" when they start a new thought).
- For each sentence, give the index of its LAST word and the punctuated sentence text.{NOTE}

Respond with JSON only, no other text:
{"sentences": [{"end": 7, "text": "So today we're going to talk about cooking."}, {"end": 15, "text": "..."}]}

Words:
{WORDS}"""


def ask_claude(client, model, chunk, is_last):
    listing = " ".join(f"{i}:{w[0]}" for i, w in enumerate(chunk))
    note = "" if is_last else (
        "\n- The list may stop in the middle of a sentence. "
        "That's fine; end your last sentence at the last word anyway.")
    msg = PROMPT.replace("{NOTE}", note).replace("{WORDS}", listing)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=8000,
                messages=[{"role": "user", "content": msg}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0))
            return [(int(s["end"]), str(s.get("text", ""))) for s in data["sentences"]]
        except Exception as e:
            print(f"    Claude 응답 처리 실패 ({attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def segment_claude(words, client, model):
    ranges = []
    i, n = 0, len(words)
    while i < n:
        j = min(n, i + CHUNK)
        chunk = words[i:j]
        is_last = j == n
        print(f"    문장 나누는 중... {j}/{n} 단어")
        res = ask_claude(client, model, chunk, is_last)
        if res:
            ends = dict()
            for e, t in res:
                if 0 <= e < len(chunk):
                    ends[e] = t
            ends = sorted(ends.items())
        else:
            print("    → 이 구간은 규칙 기반으로 나눕니다.")
            ends = [(r["b"], None) for r in segment_rules(chunk)]

        if is_last:
            if not ends or ends[-1][0] != len(chunk) - 1:
                ends.append((len(chunk) - 1, None))
        else:
            ends = [x for x in ends if x[0] < len(chunk) - 1]
            if not ends:
                ends = [(len(chunk) - 1, None)]

        prev = 0
        for e, t in ends:
            a, b = i + prev, i + e
            ok = bool(t) and norm(t) == norm("".join(w[0] for w in words[a:b + 1]))
            ranges.append({"a": a, "b": b, "text": t if ok else None})
            prev = e + 1
        i += prev
    return ranges


# ---------------------------------------------------------------- 후처리 (너무 짧거나 긴 문장 정리)

def merge_short(words, ranges):
    out = [dict(r) for r in ranges]
    i = 0
    while i < len(out):
        r = out[i]
        if r["b"] - r["a"] + 1 >= MIN_WORDS or len(out) == 1:
            i += 1
            continue
        cands = []
        if i > 0:
            cands.append((words[r["a"]][1] - words[out[i - 1]["b"]][2], i - 1))
        if i + 1 < len(out):
            cands.append((words[out[i + 1]["a"]][1] - words[r["b"]][2], i + 1))
        cands = [c for c in cands if c[0] <= MERGE_GAP]
        if not cands:
            i += 1
            continue
        _, j = min(cands)
        x, y = (out[j], r) if j < i else (r, out[j])
        if x["text"] is None and y["text"] is None:
            text = None
        else:
            tx = x["text"] or raw_text(words, x["a"], x["b"])
            ty = y["text"] or raw_text(words, y["a"], y["b"])
            text = tx + " " + ty
        lo = min(i, j)
        out[lo:lo + 2] = [{"a": x["a"], "b": y["b"], "text": text}]
        i = lo
    return out


def split_long(words, ranges):
    out = []
    stack = list(reversed(ranges))
    while stack:
        r = stack.pop()
        a, b = r["a"], r["b"]
        size = b - a + 1
        if size <= MAX_WORDS:
            out.append(r)
            continue
        tokens = r["text"].split() if r["text"] else None
        aligned = tokens is not None and len(tokens) == size
        best_k, best_score = None, -1e9
        for k in range(a + 3, b - 3):  # k 다음에서 자름, 양쪽 최소 4단어
            score = gap_after(words, k)
            tok = tokens[k - a] if aligned else words[k][0]
            if tok.endswith(","):
                score += 0.5
            if plain(words[k][0]) in BAD_END:
                score -= 0.6
            if plain(words[k + 1][0]) in GOOD_START:
                score += 0.3
            pos = (k - a) / (b - a)
            score += 0.3 * (1 - abs(pos - 0.5) * 2)  # 가운데에 가까울수록 가산
            if score > best_score:
                best_k, best_score = k, score
        k = best_k
        if aligned:
            lt = " ".join(tokens[:k - a + 1])
            rt = " ".join(tokens[k - a + 1:])
        else:
            lt = rt = None
        stack.append({"a": k + 1, "b": b, "text": rt})
        stack.append({"a": a, "b": k, "text": lt})
    return out


# ---------------------------------------------------------------- 저장

def update_index():
    items = []
    for p in sorted(OUT_DIR.glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append({
            "id": d["id"],
            "title": d.get("title", ""),
            "channel": d.get("channel", ""),
            "duration": d.get("duration"),
            "sentences": len(d.get("sentences", [])),
            "created": d.get("created", ""),
        })
    items.sort(key=lambda x: x["created"], reverse=True)
    (OUT_DIR / "index.json").write_text(
        json.dumps({"videos": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nvideos/index.json 갱신 완료 (영상 {len(items)}개)")


def get_local(seg):
    """로컬 모델은 필요할 때 한 번만 불러옴"""
    if seg["local"] is None and not seg["local_failed"]:
        seg["local"] = load_local_model()
        seg["local_failed"] = seg["local"] is None
    return seg["local"]


def process_video(url, seg, force):
    with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    vid = info["id"]
    title = info.get("title", vid)
    out_path = OUT_DIR / f"{vid}.json"
    if out_path.exists() and not force:
        print(f"- 이미 처리됨, 건너뜀 (다시 하려면 --force): {title}")
        return

    print(f"\n▶ {title} ({vid})")
    lang, kind = pick_subtitle(info)
    if not lang:
        print("  영어 자막이 없는 영상이라 건너뜁니다.")
        return
    orig_lang = str(info.get("language") or "")
    if kind == "auto" and lang == "en" and orig_lang and not orig_lang.startswith("en"):
        print("  ⚠ 원래 언어가 영어가 아닌 영상이라, 자동 번역 자막일 수 있습니다.")
    print(f"  자막: {lang} ({'수동' if kind == 'manual' else '자동 생성'})")

    words = parse_json3(download_json3(url, lang, kind))
    if not words:
        print("  자막에서 단어를 찾지 못했습니다.")
        return
    print(f"  단어 {len(words)}개")

    if seg["mode"] != "claude" and has_punct(words):
        print("  자막에 문장 부호가 이미 있어 그대로 사용합니다.")
        ranges, method = segment_rules(words), "punctuation"
    elif seg["mode"] == "claude" and seg["client"]:
        ranges, method = segment_claude(words, seg["client"], seg["model"]), "claude"
    elif seg["mode"] == "local" and get_local(seg):
        ranges, method = segment_local(words, seg["local"]), "local"
    else:
        ranges, method = segment_rules(words), "rules"

    ranges = merge_short(words, ranges)
    ranges = split_long(words, ranges)

    sentences = []
    prev = ""
    for r in ranges:
        ws = words[r["a"]:r["b"] + 1]
        prev = finish_text(r["text"] or raw_text(words, r["a"], r["b"]), prev)
        sentences.append({
            "text": prev,
            "start": round(ws[0][1], 2),
            "end": round(ws[-1][2], 2),
            "words": [[w[0], round(w[1], 2), round(w[2], 2)] for w in ws],
        })

    doc = {
        "id": vid,
        "title": title,
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration"),
        "subtitle": {"lang": lang, "kind": kind},
        "segmenter": method,
        "created": datetime.now().isoformat(timespec="seconds"),
        "sentences": sentences,
    }
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    # 단어 하나를 한 줄로 모아 파일을 짧고 보기 좋게
    text = re.sub(r'\[\s*("(?:[^"\\]|\\.)*"),\s*([-\d.]+),\s*([-\d.]+)\s*\]', r"[\1, \2, \3]", text)
    out_path.write_text(text, encoding="utf-8")
    print(f"  ✓ 문장 {len(sentences)}개 ({method}) → videos/{vid}.json")


# ---------------------------------------------------------------- 대기 목록 (queue.txt)

QUEUE_FILE = BASE / "queue.txt"
QUEUE_HEADER = (
    "# 여기에 유튜브 주소를 한 줄에 하나씩 적으면, 다음 처리 때 영상이 추가됩니다.\n"
    "# 처리에 성공한 주소는 자동으로 지워지고, 실패한 주소는 여기에 남습니다.\n"
)


def read_queue():
    if not QUEUE_FILE.exists():
        return []
    out = []
    for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out += line.split()  # 한 줄에 여러 개를 적어도 됨
    return out


def write_queue(urls):
    QUEUE_FILE.write_text(QUEUE_HEADER + "".join(u + "\n" for u in urls), encoding="utf-8")


def run_input(u, seg, force):
    """주소 하나(영상 또는 재생목록)를 처리. 전부 성공하면 True"""
    try:
        vids = expand_urls([u])
    except Exception as e:
        print(f"✗ 주소를 읽지 못했습니다: {u}\n  {e}")
        return False
    ok = True
    for v in vids:
        try:
            process_video(v, seg, force)
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description="유튜브 자막을 쉐도잉용 문장 JSON으로 변환")
    ap.add_argument("urls", nargs="*", help="유튜브 영상 또는 재생목록 주소")
    ap.add_argument("--queue", action="store_true",
                    help="queue.txt의 주소도 함께 처리하고, 실패한 주소는 queue.txt에 남김")
    ap.add_argument("--seg", choices=["local", "rules", "claude"], default="local",
                    help="문장 나누기 방식 (기본: local)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="--seg claude일 때 사용할 모델")
    ap.add_argument("--force", action="store_true", help="이미 처리한 영상도 다시 처리")
    args = ap.parse_args()

    load_dotenv()
    OUT_DIR.mkdir(exist_ok=True)

    seg = {"mode": args.seg, "model": args.model, "client": None,
           "local": None, "local_failed": False}

    if args.seg == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            import anthropic
            if key:
                seg["client"] = anthropic.Anthropic(api_key=key)
        except ImportError:
            pass
        if not seg["client"]:
            print("Claude API를 쓸 수 없어(키 또는 패키지 없음) 로컬 모델로 대신합니다.")
            seg["mode"] = "local"

    inputs = list(args.urls)
    if args.queue:
        queued = read_queue()
        if queued:
            print(f"대기 목록(queue.txt)에서 주소 {len(queued)}개를 가져왔습니다.")
        inputs += queued
    uniq = list(dict.fromkeys(inputs))  # 중복 제거, 순서 유지
    if not uniq:
        print("처리할 주소가 없습니다.")

    failed = [u for u in uniq if not run_input(u, seg, args.force)]

    if args.queue:
        write_queue(failed)
        if failed:
            print(f"\n처리하지 못한 주소 {len(failed)}개는 queue.txt에 남겨 두었습니다.")

    update_index()


if __name__ == "__main__":
    main()
