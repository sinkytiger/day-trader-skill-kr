"""DART 공시 원문: 접수번호 하나의 원본 파일을 받아 읽을 수 있는 글로 바꾼다. 표준 라이브러리만 쓴다.

공시 제목은 단서일 뿐이라, 무슨 일이 있었는지 말하기 전에 원문을 읽는다. OpenDART 의 공시서류원본파일
(document.xml) 은 ZIP 하나로 오고, 그 안에 DART 형식의 XML 이 들어 있다. 태그를 걷어 내고 표는 칸 사이에
" | " 를 넣어 한 줄로 만든다.

개인 정보: 임원·주요주주 보고서 원문에는 보고자의 생년월일(또는 등록번호)과 주소가 있다. 공시를 읽는 데
필요 없으므로 가린다. 전화번호와 이메일도 가린다.

받은 원문은 trader-kr/cache/dart/docs/<접수번호>.zip 에 둔다. 정정 공시는 새 접수번호로 나오므로 한 번
받은 원문은 바뀌지 않는다.
"""

import html
import io
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .dart import DartError

URL = "https://opendart.fss.or.kr/api/document.xml"
CACHE_DIR = os.path.join("trader-kr", "cache", "dart", "docs")
BLOCK = ("P", "TR", "TITLE", "TABLE", "BR", "SECTION-1", "SECTION-2", "SECTION-3", "COVER-TITLE",
         "DOCUMENT-NAME", "COMPANY-NAME", "LIBRARY", "PART", "COVER", "TABLE-GROUP")
CELL = ("TD", "TH", "TE", "TU")
REDACT = (
    (re.compile(r"(생년월일\s*(?:또는\s*사업자등록번호(?:\s*등)?)?)\s*\|?\s*[0-9A-Za-z*\-]{4,}"), r"\1 | (가림)"),
    (re.compile(r"(주소\s*\(본점소재지\)\s*(?:\[[^\]]*\])?)\s*\|?\s*[^|\n]+"), r"\1 | (가림)"),
    (re.compile(r"(성\s?명\s*\|\s*)[^|\n]+(\|\s*팩스)"), r"\1(가림) \2"),        # 업무 연락 담당자 이름
    (re.compile(r"(?<!\d)0\d{1,2}[-) ]\d{3,4}-\d{4}(?!\d)"), "(가림)"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "(가림)"),
)


def _http(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "day-trader-skill-kr/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        raise DartError("DART 에 연결하지 못했습니다: %s" % getattr(e, "reason", e))


def check_rcept_no(rcept_no):
    r = str(rcept_no or "").strip()
    if not re.match(r"^\d{14}$", r):
        raise DartError("접수번호는 숫자 14자리입니다 (받은 값 %r)" % rcept_no)
    return r


def viewer_url(rcept_no):
    return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % rcept_no


def fetch(rcept_no, key, cache_dir=CACHE_DIR, transport=None):
    """원본 ZIP 바이트. 캐시에 있으면 그것을, 없으면 받아서 캐시에 둔다."""
    r = check_rcept_no(rcept_no)
    path = os.path.join(cache_dir, r + ".zip") if cache_dir else None
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    raw = (transport or _http)(URL + "?" + urllib.parse.urlencode({"crtfc_key": key, "rcept_no": r}))
    if not raw.startswith(b"PK"):
        text = raw[:2000].decode("utf-8", "replace")
        st = re.search(r"<status>\s*(\d+)\s*</status>|\"status\"\s*:\s*\"(\d+)\"", text)
        msg = re.search(r"<message>(.*?)</message>|\"message\"\s*:\s*\"([^\"]*)\"", text, re.S)
        code = (st.group(1) or st.group(2)) if st else "?"
        why = ((msg.group(1) or msg.group(2)) if msg else "").strip()
        raise DartError("DART 원문 응답 %s: %s" % (code, why or "ZIP 이 아닙니다"))
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)
    return raw


def _decode(b):
    head = b[:200].decode("ascii", "replace")
    m = re.search(r'encoding="([^"]+)"', head)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp949"]:
        try:
            return b.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("utf-8", "replace")


def to_text(xml):
    """DART XML 한 개 -> 줄 글. 블록 태그는 줄바꿈, 표 칸은 ' | '."""
    t = re.sub(r"(?is)<\?xml.*?\?>|<!--.*?-->|<(style|script)\b.*?</\1\s*>", "", xml)
    t = re.sub(r"[\r\n]+", " ", t)          # 원본의 줄바꿈은 뜻이 없다. 줄은 아래에서 태그로 나눈다
    t = t.replace("&cr;", "\n")
    # 표 한 줄은 한 줄로: 칸 안의 문단·줄바꿈은 빈칸으로
    t = re.sub(r"(?is)<TR\b.*?</TR>", lambda m: re.sub(r"(?i)</?(P|BR|SPAN)\b[^>]*>", " ", m.group(0)), t)
    t = re.sub(r"(?i)</?(%s)\b[^>]*>" % "|".join(re.escape(x) for x in BLOCK), "\n", t)
    t = re.sub(r"(?i)</(%s)>" % "|".join(CELL), " | ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = []
    for line in t.split("\n"):
        line = re.sub(r"[ \t 　]+", " ", line).strip()
        line = re.sub(r"(\s*\|\s*)+", " | ", line).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def redact(text):
    for pat, rep in REDACT:
        text = pat.sub(rep, text)
    return text


def read(raw):
    """ZIP 바이트 -> [(파일 이름, 가린 글)]. 본문이 먼저 오도록 접수번호 이름의 파일을 앞에 둔다."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise DartError("DART 원문 ZIP 을 열지 못했습니다")
    names = sorted(z.namelist(), key=lambda n: (not re.match(r"^\d{14}\.xml$", n), n))
    out = [(n, redact(to_text(_decode(z.read(n))))) for n in names if n.lower().endswith(".xml")]
    if not out:
        raise DartError("DART 원문 ZIP 에 XML 이 없습니다")
    return out
