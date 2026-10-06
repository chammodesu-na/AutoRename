# -*- coding: utf-8 -*-
"""
updater.py
-----------
트레이 메뉴 「업데이트 확인」. GitHub 최신 릴리스를 보고 더 새 버전이면 설치본을 받아
SHA256 을 확인한 뒤 조용히(/SILENT) 덮어 설치한다. 설정은 홈 디렉터리에 있어 그대로 유지된다.

⚠️ 버전을 올릴 땐 APP_VERSION 과 installer.iss 의 MyAppVersion 을 같이 바꾼다.
   (둘이 다르면 설치 직후에도 "새 버전이 있다"고 계속 뜬다)
"""

import os
import re
import json
import hashlib
import tempfile
import urllib.request

APP_VERSION = "1.3.0"
REPO = "chammodesu-na/AutoRename"
ASSET_NAME = "AutoRenameSetup.exe"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"AutoRename/{APP_VERSION}"


def parse_version(v: str) -> tuple:
    """'v1.10.2' → (1, 10, 2). 문자열 비교로 하면 1.10 < 1.9 가 되므로 숫자 튜플로 비교한다."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def check_latest(timeout: int = 15) -> dict:
    """
    최신 릴리스 정보. 반환: {version, newer, url, size, sha256, notes}
    url 이 None 이면 릴리스에 설치 파일이 없다는 뜻.
    """
    req = urllib.request.Request(LATEST_API, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    digest = (asset or {}).get("digest") or ""
    return {
        "version": tag.lstrip("vV"),
        "newer": parse_version(tag) > parse_version(APP_VERSION),
        "url": (asset or {}).get("browser_download_url"),
        "size": (asset or {}).get("size") or 0,
        "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        "notes": data.get("body") or "",
    }


def download_installer(info: dict, progress=None) -> str:
    """설치 파일을 임시 폴더에 받고 크기·SHA256 을 검증한다. 어긋나면 지우고 예외."""
    dest_dir = tempfile.mkdtemp(prefix="autorename_update_")
    dest = os.path.join(dest_dir, ASSET_NAME)
    req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, info.get("size") or 0)

    problem = None
    if info.get("size") and done != info["size"]:
        problem = f"크기 불일치 (받은 {done:,} / 기대 {info['size']:,} 바이트)"
    elif info.get("sha256") and h.hexdigest() != info["sha256"]:
        problem = "SHA256 불일치 — 파일이 손상됐거나 바뀌었습니다"
    if problem:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError(problem)
    return dest


def launch_installer(path: str):
    """
    조용히 덮어 설치. /SILENT 는 진행 막대만 보이고 질문은 안 한다.
    실행 중인 AutoRename 은 설치 프로그램(Restart Manager)이 닫고, 설치 후 installer.iss 의
    'Check: WizardSilent' [Run] 항목이 다시 띄운다.
    """
    import subprocess
    subprocess.Popen([path, "/SILENT", "/SP-", "/NORESTART"], close_fds=True)


if __name__ == "__main__":
    print("현재", APP_VERSION)
    print(json.dumps({k: v for k, v in check_latest().items() if k != "notes"}, ensure_ascii=False, indent=2))
