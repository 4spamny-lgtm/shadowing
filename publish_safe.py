"""Safely publish prep.py changes while preserving newer GitHub queue requests/deletions.
Uses a temporary detached worktree and retries if origin/main advances.
Requires a clean working tree BEFORE prep.py runs, and a checkout of main.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADER = ('# 여기에 유튜브 주소를 한 줄에 하나씩 적으면, 다음 처리 때 영상이 추가됩니다.\n'
          '# 처리에 성공한 주소는 자동으로 지워지고, 실패한 주소는 여기에 남습니다.\n')
ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def git(*args, cwd=ROOT, check=True):
    cp = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                        encoding='utf-8', errors='replace')
    if check and cp.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {cp.stderr.strip() or cp.stdout.strip()}")
    return cp


def qread(text):
    return list(dict.fromkeys(u for line in text.splitlines()
                              if line.strip() and not line.lstrip().startswith('#')
                              for u in line.split()))


def load_doc(path):
    try:
        d = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(d, dict) or not isinstance(d.get('id'), str)
                or not ID.fullmatch(d['id']) or path.stem != d['id']
                or not isinstance(d.get('title'), str) or not d['title'].strip()
                or not isinstance(d.get('sentences'), list)
                or not any(isinstance(s, dict) and str(s.get('text', '')).strip()
                           for s in d['sentences'])):
            return None
        return d
    except (ValueError, OSError, UnicodeError):
        return None


def rebuild_index(folder):
    vids = []
    for file in folder.glob('*.json'):
        if file.name == 'index.json':
            continue
        d = load_doc(file)
        if not d:
            continue
        vids.append(dict(id=d['id'], title=d['title'], channel=d.get('channel', ''),
                         duration=d.get('duration'), sentences=len(d['sentences']),
                         created=d.get('created', '')))
    vids.sort(key=lambda v: str(v['created']), reverse=True)
    (folder / 'index.json').write_text(json.dumps({'videos': vids}, ensure_ascii=False, indent=1), encoding='utf-8')


def main():
    if git('branch', '--show-current').stdout.strip() != 'main':
        raise RuntimeError('main 브랜치에서 실행하세요.')
    baseline_queue = git('show', 'HEAD:queue.txt', check=False)
    baseline = qread(baseline_queue.stdout if baseline_queue.returncode == 0 else '')
    local_queue = qread((ROOT / 'queue.txt').read_text(encoding='utf-8-sig'))
    completed = set(baseline) - set(local_queue)
    new_failed = [u for u in local_queue if u not in baseline]

    # Only use video documents newly created/updated by this prep run.
    changed = git('status', '--porcelain', '--untracked-files=all').stdout.splitlines()
    modified = []
    for line in changed:
        rel = line[3:].strip().replace('\\', '/')
        if re.fullmatch(r'videos/[A-Za-z0-9_-]{11}\.json', rel) and load_doc(ROOT / rel):
            modified.append(rel)
    baseline_ids = set()
    old_idx = git('show', 'HEAD:videos/index.json', check=False)
    if old_idx.returncode == 0:
        try:
            baseline_ids = {v.get('id') for v in json.loads(old_idx.stdout).get('videos', [])}
        except (ValueError, AttributeError):
            pass

    for attempt in range(1, 7):
        git('fetch', 'origin', 'main')
        with tempfile.TemporaryDirectory(prefix='shadow-publish-') as td:
            wd = Path(td)
            git('worktree', 'add', '--detach', str(wd), 'origin/main')
            try:
                # Reconcile queue against the freshest remote contents.
                path = wd / 'queue.txt'
                remote_urls = qread(path.read_text(encoding='utf-8-sig') if path.exists() else '')
                queue = list(dict.fromkeys([u for u in remote_urls if u not in completed] + new_failed))
                path.write_text(HEADER + ''.join(u + '\n' for u in queue), encoding='utf-8')
                remote_index = wd / 'videos' / 'index.json'
                remote_ids = set()
                if remote_index.exists():
                    try:
                        remote_ids = {v.get('id') for v in json.loads(remote_index.read_text(encoding='utf-8-sig')).get('videos', [])}
                    except (ValueError, AttributeError):
                        pass
                for rel in modified:
                    vid = Path(rel).stem
                    # Never resurrect an existing video deleted remotely after the baseline.
                    if vid in baseline_ids and vid not in remote_ids:
                        print(f'Skipping remotely deleted video: {vid}')
                        continue
                    dest = wd / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / rel, dest)
                (wd / 'videos').mkdir(exist_ok=True)
                rebuild_index(wd / 'videos')
                git('add', '--', 'queue.txt', 'videos', cwd=wd)
                if git('diff', '--cached', '--quiet', cwd=wd, check=False).returncode:
                    git('-c', 'user.name=shadowing-uploader',
                        '-c', f'user.email={git("config", "user.email").stdout.strip() or "41898282+github-actions[bot]@users.noreply.github.com"}',
                        'commit', '-m', 'Update processed videos and queue', cwd=wd)
                    result = git('push', 'origin', 'HEAD:main', cwd=wd, check=False)
                    if result.returncode:
                        details = (result.stderr or result.stdout).strip()
                        lower = details.lower()
                        if 'gh007' in lower or 'private email address' in lower:
                            raise RuntimeError(
                                'GitHub가 비공개 이메일이 포함된 커밋을 거부했습니다. '
                                'GitHub noreply 이메일 설정을 확인하세요.\n' + details
                            )
                        if ('non-fast-forward' in lower or 'fetch first' in lower
                                or 'stale info' in lower
                                or ('rejected' in lower and 'behind' in lower)):
                            print(f'GitHub main이 먼저 갱신되었습니다. 재시도 {attempt}/6...')
                            continue
                        raise RuntimeError('GitHub 업로드 실패:\n' + details)
                print('GitHub update succeeded.')
                # No data is discarded locally before a successful push.
                git('fetch', 'origin', 'main')
                git('reset', '--hard', 'origin/main')
                return 0
            finally:
                git('worktree', 'remove', '--force', str(wd), check=False)
    raise RuntimeError('6 upload attempts failed. Local video data is preserved; retry later.')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'Publish failed: {exc}', file=sys.stderr)
        sys.exit(1)
