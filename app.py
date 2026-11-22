# -*- coding: utf-8 -*-
"""퀀트 에이전트 — 모바일 대시보드 (Streamlit Community Cloud).

폰에서 열어 '실행' 한 번 누르면 GitHub Actions가 파이프라인을 돌리고,
결과는 이 화면에서 보고 CSV로 바로 받습니다. 한국장/미국장을 전환합니다.

필요한 secrets (Streamlit → Settings → Secrets):
    APP_PASSWORD = "..."            # 이 화면 잠금 비밀번호
    GH_TOKEN     = "github_pat_..." # Actions 실행 권한이 있는 토큰
    GH_REPO      = "아이디/레포이름"
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests as rq
import streamlit as st

KST = timezone(timedelta(hours=9))
RELEASE_TAG = "data-store"
API = "https://api.github.com"

MARKETS = {
    "🇰🇷 한국": {
        "key": "kr",
        "model": "model_kr_latest.parquet",
        "run": "run_kr_latest.json",
        "xlsx_key": "한국",
        "daily_wf": "daily.yml",
        "monthly_wf": "monthly.yml",
        "symbol": "종목코드",
        "name": "종목명",
        "sector": "SEC_NM_KOR",
        "has_signals": True,
        "daily_note": "티커 → 섹터 → 최근 주가 → 밸류 → QVM · 약 20~40분",
        "monthly_note": "+ 2년 주가 · FnGuide 재무제표 전체 · 약 2~4시간",
    },
    "🇺🇸 미국": {
        "key": "us",
        "model": "model_us_latest.parquet",
        "run": "run_us_latest.json",
        "xlsx_key": "미국",
        "daily_wf": "us-daily.yml",
        "monthly_wf": "us-monthly.yml",
        "symbol": "Symbol",
        "name": "Name",
        "sector": "Sector",
        "has_signals": False,
        "daily_note": "종목 → 최근 주가 → 밸류 → QVM · 약 20~40분",
        "monthly_note": "+ 2년 주가 · yahooquery 재무제표 전체 · 약 1~3시간",
    },
}

# 성과 탭의 시장별 차이를 **한 곳에** 모아 둡니다. 예전에는 성과 계산이
# kor_* 테이블과 원화 표기에 박혀 있었고, 미국 계좌를 그리려면 같은 코드를
# 복사해야 했습니다 — 그러면 앞으로 한쪽만 고치는 사고가 납니다.
PERF_MARKETS = {
    "kr": {
        "라벨": "한국", "prefix": "kor_", "통화": "원",
        "price_asset": "kor_price.parquet",
        "price_cols": ("날짜", "종목코드", "종가"),
        "backtest": "backtest_kr_latest.json",
        "지수라벨": "지수 (시총상위200)", "지수": "지수",
        "price_hint": "공개 릴리스의 kor_price.parquet을 확인하세요.",
    },
    "us": {
        "라벨": "미국", "prefix": "us_", "통화": "$",
        "price_asset": "global_price.parquet",
        "price_cols": ("Date", "Symbol", "Close"),
        "backtest": "backtest_us_latest.json",
        "지수라벨": "S&P 500 (SPY, 배당 재투자)", "지수": "SPY",
        "price_hint": "공개 릴리스의 global_price.parquet을 확인하세요.",
    },
}

st.set_page_config(page_title="퀀트 에이전트", page_icon="📈",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container {padding: 1rem 0.8rem 3rem;}
  header[data-testid="stHeader"] {height: 2.2rem;}
  .stButton > button {
      width: 100%; height: 3.2rem; font-size: 1.05rem;
      font-weight: 600; border-radius: 0.7rem;
  }
  div[data-testid="stMetricValue"] {font-size: 1.3rem;}
  div[data-testid="stMetricLabel"] {font-size: 0.78rem;}
  .stDataFrame {font-size: 0.85rem;}
  .pill {display:inline-block; padding:0.15rem 0.6rem; border-radius:1rem;
         font-size:0.78rem; font-weight:600;}
  .ok   {background:#d4f5dd; color:#0a5c2a;}
  .warn {background:#fff1cc; color:#7a5200;}
  .bad  {background:#ffd9d9; color:#8a1c1c;}
</style>
""", unsafe_allow_html=True)



def fresh_perf():
    """perf 모듈을 **파일이 바뀌면 다시 읽어서** 돌려줍니다.

    왜 필요한가 (실측): Streamlit Community Cloud는 push를 받으면 메인
    스크립트(app.py)만 다시 실행하고, import된 모듈은 sys.modules에 남아 있는
    **옛 버전을 그대로** 씁니다. 그래서 perf.py에 함수를 추가해 올리면
    app.py는 새 코드인데 perf는 옛 코드라
        AttributeError: module 'perf' has no attribute 'cost_basis_return'
    가 납니다. Manage app > Reboot을 누르면 풀리지만, 고칠 때마다 그걸
    기억해야 하는 건 함정입니다. 파일 수정 시각이 바뀌면 여기서 다시 읽습니다.
    """
    import importlib
    import os
    import perf as _p
    try:
        mt = os.path.getmtime(_p.__file__)
    except Exception:
        return _p
    if st.session_state.get("_perf_mtime") != mt:
        try:
            _p = importlib.reload(_p)
        except Exception:
            pass
        st.session_state["_perf_mtime"] = mt
    return _p


def cfg(key: str, default: str = "") -> str:
    try:
        return str(st.secrets[key])
    except Exception:
        return default


GH_TOKEN = cfg("GH_TOKEN")
GH_REPO = cfg("GH_REPO")
APP_PASSWORD = cfg("APP_PASSWORD")

# 금액이 있는 파일(보유 상태·원장·자금)은 비공개 레포에 있습니다. 성과 탭만
# 이걸 씁니다 — 설정이 없으면 성과 탭에서 안내하고 나머지는 그대로 돕니다.
STATE_REPO = cfg("STATE_REPO")
STATE_TOKEN = cfg("STATE_TOKEN") or GH_TOKEN


def headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


# ── 비밀번호 잠금 ────────────────────────────────────────────
def locked() -> bool:
    if not APP_PASSWORD:
        return False
    if st.session_state.get("unlocked"):
        return False

    st.title("📈 퀀트 에이전트")
    pw = st.text_input("비밀번호", type="password")
    if st.button("열기"):
        if pw == APP_PASSWORD:
            st.session_state["unlocked"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    return True


if locked():
    st.stop()

if not (GH_TOKEN and GH_REPO):
    st.error("GH_TOKEN / GH_REPO secrets가 설정되지 않았습니다. "
             "Streamlit 앱 설정 → Secrets에서 등록해 주세요.")
    st.stop()


# ── GitHub API ───────────────────────────────────────────────
def _explain(resp: rq.Response | None, exc: Exception | None) -> str:
    if exc is not None:
        return f"GitHub에 연결하지 못했습니다: {exc}"
    if resp is None:
        return "알 수 없는 오류"
    if resp.status_code == 401:
        return "GH_TOKEN이 잘못됐거나 만료됐습니다. Streamlit Secrets를 확인하세요."
    if resp.status_code == 403:
        return ("권한이 없습니다. 토큰에 Actions(읽기·쓰기)와 Contents(읽기) 권한이 "
                "있는지 확인하세요.")
    if resp.status_code == 404:
        return f"레포를 찾을 수 없습니다: {GH_REPO}. GH_REPO 값을 확인하세요."
    return f"GitHub 오류 {resp.status_code}: {resp.text[:200]}"


def api_get(path: str, **kw):
    try:
        r = rq.get(f"{API}{path}", headers=headers(), timeout=30, **kw)
    except Exception as e:
        return None, _explain(None, e)
    if r.status_code == 404:
        return None, None
    if not r.ok:
        return None, _explain(r, None)
    return r.json(), None


@st.cache_data(ttl=60, show_spinner=False)
def release_assets() -> tuple[dict, str | None]:
    data, err = api_get(f"/repos/{GH_REPO}/releases/tags/{RELEASE_TAG}")
    if data is None:
        return {}, err
    return ({a["name"]: (a["url"], a["size"], a["updated_at"])
             for a in data.get("assets", [])}, None)


@st.cache_data(ttl=60, show_spinner=False)
def download_asset(name: str) -> bytes | None:
    assets, _ = release_assets()
    if name not in assets:
        return None
    h = dict(headers())
    h["Accept"] = "application/octet-stream"
    try:
        r = rq.get(assets[name][0], headers=h, timeout=180, allow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


@st.cache_data(ttl=30, show_spinner=False)
def recent_runs(limit: int = 10) -> list:
    data, _ = api_get(f"/repos/{GH_REPO}/actions/runs", params={"per_page": limit})
    return (data or {}).get("workflow_runs", [])


@st.cache_data(ttl=600, show_spinner=False)
def default_branch() -> str:
    data, _ = api_get(f"/repos/{GH_REPO}")
    return (data or {}).get("default_branch", "main")


def dispatch(workflow_file: str, inputs: dict) -> tuple[bool, str]:
    try:
        r = rq.post(
            f"{API}/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches",
            headers=headers(),
            json={"ref": default_branch(), "inputs": inputs},
            timeout=30,
        )
    except Exception as e:
        return False, _explain(None, e)
    if r.status_code == 204:
        return True, "실행 요청을 보냈습니다. 1분쯤 뒤 상태가 갱신됩니다."
    return False, _explain(r, None)


def kst(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%m/%d %H:%M")
    except Exception:
        return str(iso) or "-"


# ── 데이터 ───────────────────────────────────────────────────
CODE_COLS = ("종목코드", "CMP_CD")


def to_csv(df: pd.DataFrame, excel_safe: bool = True) -> bytes:
    """UTF-8 BOM. excel_safe면 한국 종목코드를 ="005930" 형태로 고정합니다."""
    out = df.copy()
    if excel_safe:
        for col in CODE_COLS:
            if col in out.columns:
                out[col] = out[col].astype(str).map(lambda v: f'="{v}"')
    return out.to_csv(index=False).encode("utf-8-sig")


@st.cache_data(ttl=60, show_spinner=False)
def load_model(asset: str) -> pd.DataFrame:
    blob = download_asset(asset)
    if not blob:
        return pd.DataFrame()
    try:
        return pd.read_parquet(io.BytesIO(blob))
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def load_run(asset: str) -> dict:
    blob = download_asset(asset)
    if not blob:
        return {}
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        return {}


def badge(status: str) -> str:
    cls = {"정상": "ok", "주의": "warn", "실패": "bad"}.get(status, "warn")
    return f'<span class="pill {cls}">{status}</span>'


# ── 비공개 레포의 상태·원장 (성과 탭에서만) ──────────────────
@st.cache_data(ttl=120, show_spinner=False)
def state_parquet(name: str) -> pd.DataFrame:
    """비공개 레포 state/<name> 를 읽습니다. 없으면 빈 표.

    왜 릴리스가 아니라 레포인가: 평가액·체결가가 들어 있어 공개 릴리스에
    올리지 않기로 했습니다. 읽기에는 contents API가 필요하고, 토큰에
    그 레포 읽기 권한이 있어야 합니다.
    """
    if not (STATE_REPO and STATE_TOKEN):
        return pd.DataFrame()
    url = f"{API}/repos/{STATE_REPO}/contents/state/{name}"
    try:
        r = rq.get(url, timeout=120, headers={
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {STATE_TOKEN}"})
        if r.status_code != 200:
            return pd.DataFrame()
        return pd.read_parquet(io.BytesIO(r.content))
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def asset_bytes(asset: str) -> bytes:
    """큰 공개 자산의 원본 바이트 — 캐시를 길게 둡니다(35MB를 매번 받으면 느립니다)."""
    return download_asset(asset) or b""


@st.cache_data(ttl=1800, show_spinner="데이터를 읽고 있습니다…")
def parquet_slice(asset: str, columns: tuple | None = None,
                  filters: tuple | None = None) -> pd.DataFrame:
    """필요한 칸·줄만 읽습니다. **전체를 읽으면 앱이 메모리로 죽습니다.**

    실측 (Streamlit Community Cloud는 1GB 남짓):
        kor_price  전체 327MB → 날짜 3칸·최근 구간만 몇 MB
        kor_ticker 전체 581MB → 4칸·마지막 기준일만 0.2MB
        kor_sector 전체 438MB → 3칸·마지막 기준일만 0.4MB
    pandas.read_parquet로 통째로 읽으면 주가+종목표만 900MB가 되어 컨테이너가
    죽습니다('Oh no. Error running app.'). 그래서 pyarrow로 걸러 읽습니다.
    """
    raw = asset_bytes(asset)
    if not raw:
        return pd.DataFrame()
    try:
        import pyarrow.parquet as pq
        tb = pq.read_table(
            io.BytesIO(raw),
            columns=list(columns) if columns else None,
            filters=[list(f) for f in filters] if filters else None)
        return tb.to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def latest_rows(asset: str, columns: tuple,
                asof_col: str = "기준일") -> pd.DataFrame:
    """기준일이 가장 최근인 줄만. 날짜 칸만 먼저 읽어 최댓값을 찾습니다 —
    칸을 다 읽고 거르면 그 순간의 메모리가 이미 수백 MB입니다."""
    only_date = parquet_slice(asset, (asof_col,))
    if only_date.empty:
        return pd.DataFrame()
    mx = pd.to_datetime(only_date[asof_col]).max()
    return parquet_slice(asset, columns, ((asof_col, "=", mx),))


# ── 헤더 + 시장 선택 ─────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def bench_curve(start_iso: str, end_iso: str) -> pd.DataFrame:
    """시총상위200 시총가중 지수 (백테스트와 같은 정의).

    주가 전 종목 피벗이 무거워서 따로 캐시합니다 — 인자를 날짜 문자열로만
    받는 이유도 그것입니다(DataFrame을 넘기면 캐시가 안 걸립니다).
    """
    perf = fresh_perf()
    start = pd.Timestamp(start_iso)
    window = parquet_slice(
        "kor_price.parquet", ("날짜", "종목코드", "종가"),
        (("날짜", ">=", start - pd.Timedelta(days=10)),))
    ticker = latest_rows("kor_ticker.parquet",
                         ("종목코드", "시가총액", "종가", "기준일"))
    if window.empty or ticker.empty:
        return pd.DataFrame()
    return perf.index_benchmark(perf.price_pivot(window),
                                perf.shares_outstanding(ticker),
                                start=start, end=pd.Timestamp(end_iso))


@st.cache_data(ttl=1800, show_spinner=False)
def spy_curve(start_iso: str, end_iso: str) -> pd.DataFrame:
    """S&P 500 (SPY) 누적 수익률 — **수정종가**(배당 재투자)로 잡습니다.

    왜 수정종가인가: 실제 계좌 수익률에는 받은 배당이 들어 있습니다(예수금
    으로 들어와 총자본에 잡힙니다). 단순 종가로 비교하면 SPY 배당(연 1.2~
    1.5%)만큼 매년 공짜로 이기는 것처럼 보입니다.

    테이블이 global_price와 갈라져 있는 이유는 global_bench.py 주석 참고 —
    유니버스에 넣으면 모델이 SPY를 매수 후보로 집어 올립니다.
    """
    start, end = pd.Timestamp(start_iso), pd.Timestamp(end_iso)
    b = parquet_slice("global_bench.parquet",
                      ("Date", "Symbol", "AdjClose"),
                      (("Symbol", "=", "SPY"),
                       ("Date", ">=", start - pd.Timedelta(days=10))))
    if b.empty:
        return pd.DataFrame()
    b["Date"] = pd.to_datetime(b["Date"])
    b = (b[(b["Date"] >= start) & (b["Date"] <= end)]
         .dropna(subset=["AdjClose"]).sort_values("Date"))
    if len(b) < 2:
        return pd.DataFrame()
    px = pd.to_numeric(b["AdjClose"], errors="coerce").to_numpy()
    return pd.DataFrame({"누적": px / px[0]},
                        index=pd.DatetimeIndex(b["Date"], name="날짜"))


ACCOUNTS = {"상수 (main)": "main", "배우자 (alt)": "alt"}


def _suffix(acct: str) -> str:
    return "" if acct == "main" else f"_{acct}"


_AP_MEMO: dict = {}


def account_perf(acct: str, market: str = "kr") -> dict:
    """_account_perf를 한 번 실행에 한 번만. 화면이 다시 그려질 때마다
    (Streamlit은 스크립트 전체를 다시 돕니다) 비워지므로 옛 결과가 남지
    않습니다. 맨 위 띠·시장 탭·종합 탭이 같은 숫자를 보게 됩니다."""
    key = (acct, market)
    if key not in _AP_MEMO:
        _AP_MEMO[key] = _account_perf(acct, market)
    return _AP_MEMO[key]


def _account_perf(acct: str, market: str = "kr") -> dict:
    """한 계좌의 성과 한 묶음.

    맨 위 요약과 성과 탭이 **같은 함수**를 씁니다 — 두 곳에서 따로 계산하면
    같은 화면에 다른 수익률이 뜨고, 어느 쪽이 맞는지 알 수 없게 됩니다.
    무거운 읽기는 모두 캐시되어 있어 여러 번 불러도 비용은 계산뿐입니다.

    market으로 한국/미국을 가릅니다. **테이블과 통화를 섞으면 안 됩니다** —
    달러 평가액이 원화 곡선에 한 점이라도 들어가면 수익률이 통째로 거짓이
    되고 되돌릴 방법이 없습니다. 그래서 테이블 이름도, 주가 자산도, 지수도
    PERF_MARKETS 한 곳에서만 정해집니다.
    """
    perf = fresh_perf()
    P = PERF_MARKETS[market]
    pre = P["prefix"]
    out: dict = {"계좌": acct, "시장": market, "가능": False, "이유": "설정없음"}
    if not (STATE_REPO and STATE_TOKEN):
        return out

    sfx = _suffix(acct)
    trades = state_parquet(f"{pre}trades.parquet")
    hlog = state_parquet(f"{pre}holdings_log.parquet")
    out.update({"trades": trades, "hlog": hlog,
                "pstate": state_parquet(f"{pre}portfolio{sfx}.parquet"),
                "cap": state_parquet(f"{pre}capital{sfx}.parquet")})
    if hlog.empty and trades.empty:
        out["이유"] = "기록없음"
        return out

    # 종목코드 정규화는 perf._code6에 맡깁니다 — 숫자 코드만 0을 채우므로
    # 'AAPL'이 '00AAPL'로 망가지지 않습니다.
    codes = set()
    if not hlog.empty:
        codes |= set(perf._code6(hlog["종목코드"]))
    if not trades.empty:
        codes |= set(perf._code6(trades["종목코드"]))

    # 측정 시작보다 10일 앞에서부터만 읽습니다 — 주가를 통째로 읽으면
    # 327MB가 올라와 앱이 죽습니다(실측).
    first = pd.Timestamp("2000-01-01")
    if not hlog.empty:
        first = pd.to_datetime(hlog["기준일"]).min()
    elif not trades.empty:
        first = pd.to_datetime(trades["기준일"]).min()
    d_col, c_col, p_col = P["price_cols"]
    price = parquet_slice(P["price_asset"], P["price_cols"],
                          ((d_col, ">=", first - pd.Timedelta(days=10)),))
    if price.empty:
        out["이유"] = "주가없음"
        return out
    # 아래 계산은 전부 한국식 칸 이름을 씁니다. 미국 주가는 Date/Symbol/
    # Close로 오니 여기서 한 번만 맞춰 두고, 그 뒤 코드는 시장을 모릅니다.
    if (d_col, c_col, p_col) != ("날짜", "종목코드", "종가"):
        price = price.rename(columns={d_col: "날짜", c_col: "종목코드",
                                      p_col: "종가"})
    out["주가기준일"] = pd.to_datetime(price["날짜"]).max()

    px = perf.price_pivot(price, codes)
    pos = perf.positions_timeline(hlog, acct, px)
    value = perf.daily_value(pos, px)
    # 현금흐름을 평가액 곡선과 같은 시계에 올립니다.
    #
    # 평가액은 재동기화 스냅샷에서만 수량이 바뀌는 계단입니다. 금요일에
    # 사고 일요일에 재동기화하면 새 종목이 월요일에야 곡선에 나타나는데,
    # 현금흐름은 금요일에 찍혀 있어 하루가 어긋납니다. 그러면 그 점프가
    # 수익으로 계산됩니다 (2026-10-22 실측: 상수 6,231,798원 / 배우자
    # 2,062,585원이 설명되지 않는 점프로 잡혔습니다).
    flows = perf.align_flows(perf.net_flows(trades, acct), value.index,
                             pos.index)
    tw = perf.twr(value, flows)
    out.update({"px": px, "pos": pos, "value": value, "tw": tw})

    # 평단 기준 수익률 — TWR은 거래일이 둘 있어야 나오지만 이건 첫날부터
    # 나오고 주가가 갱신될 때마다 바뀝니다. 첫 주에 화면이 '–'만 띄우면
    # 갱신이 되는지 안 되는지 알 수가 없습니다.
    out["pnl"] = perf.position_pnl(trades, hlog, px, acct)
    out["평단"] = perf.cost_basis_return(out["pnl"])

    if pos.empty:
        out["이유"] = "계좌기록없음"
        return out
    if len(tw) < 2:
        out["이유"] = "한점"
        out["스냅샷"] = len(pos)
        return out

    # 못 믿을 TWR은 숫자로 내보내지 않습니다.
    #
    # TWR은 (평가액 - 그날 입출금) / 어제 평가액입니다. 매수가 원장에 안
    # 들어와 있거나 체결일이 측정 구간 밖에 찍혀 있으면 **그 매수가 수익으로**
    # 계산됩니다. 2026-10-21 실측: 분할 진입 중이던 배우자 계좌가 9일에
    # +93.57%로 찍혔고 실제 손익은 -145,625원이었습니다. 틀린 숫자를
    # 자신있게 띄우는 것이 아무 숫자도 안 띄우는 것보다 나쁩니다.
    trust = perf.twr_trust(tw)
    out["신뢰"] = trust
    if not trust["신뢰"]:
        out["이유"] = "입출금누락"
        out["신뢰사유"] = trust["사유"]
        return out

    out["가능"], out["이유"] = True, ""
    out["수익률"] = float(tw["누적"].iloc[-1] - 1)
    out["시작"], out["끝"] = tw.index[0], tw.index[-1]
    out["일수"] = int((tw.index[-1] - tw.index[0]).days)
    _curve = bench_curve if market == "kr" else spy_curve
    b = _curve(f"{tw.index[0]:%Y-%m-%d}", f"{tw.index[-1]:%Y-%m-%d}")
    out["bench"] = b
    out["지수"] = float(b["누적"].iloc[-1] - 1) if not b.empty else None
    return out


_WHY = {"설정없음": "설정 전", "기록없음": "기록 전",
        "계좌기록없음": "측정 전", "한점": "다음 주부터",
        "주가없음": "주가 없음",
        # 입출금이 원장에 없으면 매수가 수익으로 잡힙니다 — 숫자를 숨기고
        # 무엇을 고쳐야 하는지 띄웁니다.
        "입출금누락": "체결내역 필요"}


# ── 선 색 — 상수 / 배우자 / 지수 ─────────────────────────────
#
# 2026-11-20 바꿈 — 상승 빨강 ▲ / 하락 파랑 ▼ (한국 증시 관례)를 들이면서
# 상수 파랑이 '손실' 색과 겹쳤습니다. 그래서 두 사람을 팀 색으로 다시
# 정했습니다: 상수 보라 / 배우자 민트 / 지수 회색 점선(심판 — 색으로 다투지
# 않고 선 모양으로 구분). 두 테마에서 **같은 값**을 씁니다 — 차트는 CSS를
# 못 쓰고, 파이썬 쪽 테마 감지(st.context.theme)는 첫 실행에 '밝음'으로
# 잘못 나옵니다(2026-11-20 실측). validate_palette.js: 밝은(#fff)·어두운
# (#171614) 배경 모두 CVD ΔE 20.7, 일반시야 27.1, 명암비 3:1 이상 통과.
def series_trio() -> dict:
    return {"상수": "#7a6ae0", "배우자": "#169d6c", "지수": "#8f8e88"}


# ── 수익률 배틀 — 두 계좌 맞대결 화면 ─────────────────────────
# 숫자는 전부 account_perf()가 이미 계산한 것만 씁니다. 여기서 새로 재는
# 것은 '누가 앞섰나'뿐이고, 그 계산은 perf.head_to_head / weekly_rounds
# (tools/test_battle.py로 검증)에 있습니다.
#
# 공정성 규칙: 두 계좌를 **같은 측정**으로 잴 수 있을 때만 승자를 가립니다.
#   둘 다 시간가중 → 시간가중으로 승부 (입금액·입금 시점이 달라도 공정)
#   한쪽이라도 시간가중이 숨겨졌고 둘 다 평단 기준이 있으면 → 평단으로 승부
#   그것도 안 되면 → 판정 보류. 시간가중과 평단을 맞대면 승패가 거짓입니다.
import html as _html  # noqa: E402

_BATTLE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Black+Han+Sans&display=swap');
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');
.qb {
  /* 팀 색은 두 테마에서 같은 값 — 밝은·어두운 배경 모두 3:1 이상, 색맹 검사 통과.
     상승/하락과 보조 글씨는 .stApp의 color-scheme을 따라 light-dark()로 고릅니다
     (파이썬 쪽 테마 감지는 첫 실행에 '밝음'으로 잘못 나와서 쓰지 않습니다). */
  --a:#7a6ae0; --b:#169d6c; --idx:#8f8e88;
  --up:#d03b3b; --dn:#256abf;
  --up:light-dark(#d03b3b,#ef6b6b); --dn:light-dark(#256abf,#5b9cf0);
  --muted:rgba(110,110,110,.95);
  --muted:color-mix(in srgb,currentColor 64%,transparent);
  --line:rgba(128,128,128,.25);
  --line:color-mix(in srgb,currentColor 14%,transparent);
  --panel:rgba(122,106,224,.05);
  font-family:'Pretendard Variable',Pretendard,-apple-system,'Apple SD Gothic Neo',
              'Malgun Gothic',sans-serif;
  font-variant-numeric:tabular-nums; line-height:1.45;
}
.qb .up {color:var(--up);} .qb .dn {color:var(--dn);}
.qb-panel {border:1px solid var(--line); border-radius:16px;
           padding:1rem 1.05rem .85rem; background:var(--panel); margin-bottom:.6rem;}
.qb-mkt {font-size:.8rem; font-weight:700; color:var(--muted); margin-bottom:.15rem;}
.qb-board {display:grid; grid-template-columns:1fr auto 1fr; align-items:end; column-gap:.5rem;}
.qb-side.b {text-align:right;}
.qb-who {display:flex; align-items:center; gap:.35rem; font-weight:700; font-size:.95rem;}
.qb-side.b .qb-who {justify-content:flex-end;}
.qb-dot {width:.7rem; height:.7rem; border-radius:50%; display:inline-block; flex:none;}
.qb-dot.a {background:var(--a);} .qb-dot.b {background:var(--b);}
.qb-dash {display:inline-block; width:1.05rem; height:0; border-top:2.5px dashed var(--idx);
           vertical-align:.22em; margin-right:.15rem;}
.qb-score {font-family:'Black Han Sans','Pretendard Variable',sans-serif; font-weight:400;
           font-size:clamp(2rem,9.5vw,3.1rem); line-height:1.02; margin:.2rem 0 .15rem;
           letter-spacing:-.01em;}
.qb-score small {font-size:.48em; margin-left:.06em;}
.qb-score.na {color:var(--muted);}
.qb-sub {font-size:.8rem; color:var(--muted);}
.qb-sub b {font-weight:700;}
.qb-vs {font-family:'Black Han Sans',sans-serif; font-size:1.05rem; color:var(--muted);
        padding-bottom:1.55rem;}
.qb-rope {position:relative; display:flex; gap:2px; height:.62rem; margin:.95rem 0 .55rem;}
.qb-rope > span {display:block; height:100%;}
.qb-rope .ra {background:var(--a); border-radius:.31rem 0 0 .31rem;}
.qb-rope .rb {background:var(--b); border-radius:0 .31rem .31rem 0; flex:1;}
.qb-rope .mid {position:absolute; left:50%; top:-.28rem; bottom:-.28rem; width:2px;
               margin-left:-1px; background:var(--muted); opacity:.55;}
.qb-rope .knot {position:absolute; top:50%; width:1.1rem; height:1.1rem; border-radius:50%;
                background:Canvas; border:3px solid currentColor;
                transform:translate(-50%,-50%);}
.qb-verdict {text-align:center; font-weight:800; font-size:1.02rem;}
.qb-ref {text-align:center; font-size:.82rem; color:var(--muted); margin-top:.1rem;}
.qb.mini .qb-panel {padding:.7rem .85rem .6rem;}
.qb.mini .qb-score {font-size:clamp(1.45rem,6.4vw,2rem);}
.qb.mini .qb-vs {font-size:.85rem; padding-bottom:.55rem;}
.qb.mini .qb-rope {height:.42rem; margin:.6rem 0 .4rem;}
.qb.mini .qb-rope .knot {width:.85rem; height:.85rem; border-width:2.5px;}
.qb.mini .qb-verdict {font-size:.9rem;}
.qb-stats {margin-top:.85rem; border-top:1px solid var(--line); padding-top:.35rem;}
.qb-row {display:grid; grid-template-columns:5.2rem 1fr 5.2rem; align-items:center;
         row-gap:.28rem; padding:.42rem 0;}
.qb-row + .qb-row {border-top:1px dashed var(--line);}
.qb-row .v {font-weight:600; color:var(--muted); font-size:.95rem;}
.qb-row .v.r {text-align:right;}
.qb-row .v.win {color:inherit; font-weight:800;}
.qb-row .v.win.up {color:var(--up);} .qb-row .v.win.dn {color:var(--dn);}
.qb-row .v.even {color:inherit; font-weight:600;}
.qb-row .v.even.up {color:var(--up);} .qb-row .v.even.dn {color:var(--dn);}
.qb-row .lab {text-align:center; font-size:.8rem; color:var(--muted);}
.qb-split {grid-column:1/-1; display:flex; gap:2px; height:5px;}
.qb-split span {display:block; height:100%;}
.qb-split .sa {background:var(--a); border-radius:3px 0 0 3px;}
.qb-split .sb {background:var(--b); border-radius:0 3px 3px 0; flex:1;}
.qb .qb-rounds {width:100%; border-collapse:collapse; font-size:.88rem; border:none;
                margin:0 0 .9rem;}
.qb .qb-rounds th, .qb .qb-rounds td {border-left:none; border-right:none; border-top:none;}
.qb-rounds th {font-weight:600; font-size:.78rem; color:var(--muted); text-align:right;
               padding:.25rem .3rem; border-bottom:1px solid var(--line);}
.qb-rounds th:first-child, .qb-rounds td:first-child {text-align:left;}
.qb-rounds td {text-align:right; padding:.42rem .3rem; border-bottom:1px solid var(--line);}
.qb-rounds td.wk b {font-weight:800;}
.qb-rounds td.wk small {display:block; color:var(--muted); font-size:.74rem;}
.qb-rounds td.win {font-weight:800;}
.qb-rounds td.win::after {content:''; display:inline-block; width:.5rem; height:.5rem;
                          border-radius:50%; margin-left:.3rem; vertical-align:.08em;}
.qb-rounds td.win.a::after {background:var(--a);} .qb-rounds td.win.b::after {background:var(--b);}
.qb-rounds td.idx {color:var(--muted);}
.qb-rounds tr.live td {border-bottom-style:dashed;}
.qb-tally {font-size:.85rem; color:var(--muted); margin:.1rem 0 .5rem;}
.qb-tally b {color:inherit;}
.qb-duo {display:grid; grid-template-columns:1fr 1fr; column-gap:1.1rem;}
.qb-duo .hd {display:flex; align-items:center; gap:.35rem; font-weight:800; margin-bottom:.2rem;}
.qb-duo .grp {font-size:.78rem; color:var(--muted); margin:.55rem 0 .15rem; font-weight:600;}
.qb-it {display:grid; grid-template-columns:minmax(0,1fr) auto; column-gap:.4rem;
        padding:.32rem 0; border-bottom:1px solid var(--line);}
.qb-it .nm {overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600;}
.qb-it .pc {font-weight:800;}
.qb-it .am {grid-column:1/-1; font-size:.74rem; color:var(--muted);}
.qb-it .who {display:inline-flex; gap:2px; margin-right:.3rem; vertical-align:.05em;}
.qb-it .who .qb-dot {width:.55rem; height:.55rem;}
.qb-empty {font-size:.82rem; color:var(--muted); padding:.3rem 0;}
.qb-legend {display:flex; flex-wrap:wrap; gap:.3rem 1rem; font-size:.85rem; font-weight:600;}
.qb-legend > span {display:inline-flex; align-items:center; gap:.3rem; white-space:nowrap;}
.qb-legend .qb-dash {margin-right:0;}
.qb-dash.a {border-top-color:var(--a);} .qb-dash.b {border-top-color:var(--b);}
@media (max-width:380px) {
  .qb-duo {grid-template-columns:1fr;} .qb-duo > div + div {margin-top:.9rem;}
  .qb-row {grid-template-columns:4.4rem 1fr 4.4rem;}
}
@media (prefers-reduced-motion:no-preference) {
  .qb-rope .knot {animation:qb-pull .7s cubic-bezier(.3,1.4,.5,1) both;}
  @keyframes qb-pull {from {left:50%;}}
}
</style>
"""


def _qb(html_body: str, *, mini: bool = False) -> None:
    cls = "qb" + (" mini" if mini else "")
    st.markdown(f'<div class="{cls}">{html_body}</div>', unsafe_allow_html=True)


def _ga(name: str) -> str:
    """이름 + 주격 조사 (상수가 / 은진이)."""
    if not name:
        return name
    ch = name[-1]
    if "가" <= ch <= "힣":
        return name + ("이" if (ord(ch) - 0xAC00) % 28 else "가")
    return name + "가"


def _sgn(v, p: int = 2, unit: str = "%", scale: float = 100.0) -> str:
    """부호 붙은 숫자 — 마이너스는 진짜 빼기표(−)."""
    if v is None or not pd.notna(v):
        return "–"
    x = v * scale
    s = f"{abs(x):,.{p}f}{unit}"
    return ("+" if x > 0 else "−" if x < 0 else "") + s


def _cls(v) -> str:
    if v is None or not pd.notna(v) or v == 0:
        return ""
    return "up" if v > 0 else "dn"


def _arrow(v, p: int = 1) -> str:
    if v is None or not pd.notna(v):
        return "–"
    return ("▲" if v > 0 else "▼" if v < 0 else "") + f"{abs(v) * 100:.{p}f}%"


def duel(RS: dict, market: str) -> dict:
    """두 계좌를 같은 측정으로 맞댈 수 있는지 정하고, 맞대 봅니다."""
    perf = fresh_perf()
    keys = list(ACCOUNTS.values())
    names = {a: lab.split(" (")[0] for lab, a in ACCOUNTS.items()}
    d = {"mode": None, "keys": keys, "names": names, "market": market}
    if len(keys) != 2:
        return d
    ka, kb = keys
    ra, rb = RS[ka], RS[kb]
    cba, cbb = (ra.get("평단") or {}), (rb.get("평단") or {})
    if ra["가능"] and rb["가능"]:
        d.update(mode="twr", va=ra["수익률"], vb=rb["수익률"])
        d["h2h"] = perf.head_to_head(ra["tw"]["누적"], rb["tw"]["누적"])
        curves = {ka: ra["tw"]["누적"], kb: rb["tw"]["누적"]}
        if market == "all":
            # 종합은 사람마다 국장·미국장 비중이 달라 비교 지수도 다릅니다
            # ('같은 비중 지수'). 한 줄짜리 지수 값이나 라운드의 지수 칸은
            # 둘 중 누구 것인지 모호하니 띄우지 않습니다.
            d["idx"] = None
            d["idx_a"], d["idx_b"] = ra.get("지수"), rb.get("지수")
        else:
            d["idx"] = next((r["지수"] for r in (ra, rb)
                             if r.get("지수") is not None), None)
            bench = next((r["bench"] for r in (ra, rb)
                          if not r.get("bench", pd.DataFrame()).empty), None)
            if bench is not None:
                curves["지수"] = bench["누적"]
        d["rounds"] = perf.weekly_rounds(curves, accounts=(ka, kb))
        d["today"] = (float(ra["tw"]["일수익률"].iloc[-1]),
                      float(rb["tw"]["일수익률"].iloc[-1]))
        d["asof"] = max(ra["tw"].index[-1], rb["tw"].index[-1])
    elif (cba.get("수익률") is not None and cbb.get("수익률") is not None):
        d.update(mode="평단", va=cba["수익률"], vb=cbb["수익률"])
    if d["mode"]:
        gap = d["va"] - d["vb"]
        d["gap"] = gap
        d["leader"] = ka if gap > 1e-12 else kb if gap < -1e-12 else None
    return d


def _idx_name(d: dict) -> str:
    return "지수" if d["market"] == "all" else PERF_MARKETS[d["market"]]["지수"]


def _side_html(d: dict, RS: dict, k: str, side: str, *, mini: bool) -> str:
    n = _html.escape(d["names"][k])
    crown = " 👑" if d.get("leader") == k else ""
    r = RS[k]
    if d["mode"]:
        v = d["va"] if side == "a" else d["vb"]
        score = (f'<div class="qb-score {_cls(v)}">{_sgn(v)[:-1]}'
                 f'<small>%</small></div>')
    else:
        cb = (r.get("평단") or {}).get("수익률")
        v = r["수익률"] if r["가능"] else cb
        score = (f'<div class="qb-score {_cls(v)}">{_sgn(v)[:-1]}<small>%</small></div>'
                 if v is not None else '<div class="qb-score na">–</div>')
    sub = ""
    if not mini:
        idx = d.get(f"idx_{side}", d.get("idx"))
        if d["mode"] == "twr" and idx is not None:
            ex = v - idx
            sub = (f'<div class="qb-sub">{_idx_name(d)}보다 '
                   f'<b class="{_cls(ex)}">{_sgn(ex, unit="%p")}</b></div>')
        elif d["mode"] == "평단":
            sub = '<div class="qb-sub">평단 기준</div>'
        elif not d["mode"]:
            why = (r.get("사유") if not r["가능"] and r.get("사유") else
                   "평단 기준" if (not r["가능"] and (r.get("평단") or {})
                                 .get("수익률") is not None)
                   else _WHY.get(r["이유"], "측정 전") if not r["가능"] else "")
            sub = f'<div class="qb-sub">{_html.escape(why)}</div>' if why else ""
    dot = f'<span class="qb-dot {side}"></span>'
    who = (f'{dot}{n}{crown}' if side == "a" else f'{crown.strip()} {n}{dot}')
    return f'<div class="qb-side {side}"><div class="qb-who">{who}</div>{score}{sub}</div>'


def scoreboard_html(d: dict, RS: dict, *, mini: bool = False,
                    label: str = "", stats: str = "") -> str:
    ka, kb = d["keys"]
    head = f'<div class="qb-mkt">{_html.escape(label)}</div>' if label else ""
    board = (f'<div class="qb-board">{_side_html(d, RS, ka, "a", mini=mini)}'
             f'<div class="qb-vs">VS</div>'
             f'{_side_html(d, RS, kb, "b", mini=mini)}</div>')
    rope = verdict = ref = ""
    if d["mode"]:
        # 줄다리기 — 앞선 쪽 색이 더 넓게. ±4%p에서 끝까지 갑니다(고정 눈금:
        # 작은 격차를 크게 부풀려 보이지 않게).
        share = 50 + max(-1.0, min(1.0, d["gap"] * 100 / 4.0)) * 45
        rope = (f'<div class="qb-rope" role="img" aria-label="격차 '
                f'{abs(d["gap"]) * 100:.2f}%p"><span class="ra" '
                f'style="width:{share:.1f}%"></span><span class="rb"></span>'
                f'<span class="mid"></span><span class="knot" '
                f'style="left:{share:.1f}%"></span></div>')
        if d["leader"]:
            ln = d["names"][d["leader"]]
            bits = [f'{_html.escape(_ga(ln))} {abs(d["gap"]) * 100:.2f}%p 앞서는 중']
            h = d.get("h2h") or {}
            st_ = h.get("연속", {})
            if not mini and st_.get("일수", 0) >= 2:
                bits.append(f'{st_["일수"]}거래일째 리드')
            verdict = f'<div class="qb-verdict">{", ".join(bits)}</div>'
        else:
            verdict = '<div class="qb-verdict">동점</div>'
        if d["mode"] == "twr" and d.get("idx") is not None:
            beat = [d["names"][k] for k, v in ((ka, d["va"]), (kb, d["vb"]))
                    if v > d["idx"]]
            ix = PERF_MARKETS[d["market"]]["지수"]
            if len(beat) == 2:
                tail = f"두 사람 모두 {ix}를 이기는 중"
            elif beat:
                tail = f'{_ga(beat[0])} {ix}를 이기는 중'
            else:
                tail = f"둘 다 {ix}에 밀리는 중"
            ref = (f'<div class="qb-ref"><span class="qb-dash"></span>{ix} '
                   f'<b class="{_cls(d["idx"])}">{_sgn(d["idx"])}</b>, '
                   f'{_html.escape(tail)}</div>')
        elif d["mode"] == "twr" and (d.get("idx_a") is not None
                                     and d.get("idx_b") is not None):
            beat = [d["names"][k] for k, v, i in ((ka, d["va"], d["idx_a"]),
                                                  (kb, d["vb"], d["idx_b"]))
                    if v > i]
            if len(beat) == 2:
                tail = "두 사람 모두 자기 비중 지수를 이기는 중"
            elif beat:
                tail = f'{_ga(beat[0])} 자기 비중 지수를 이기는 중'
            else:
                tail = "둘 다 자기 비중 지수에 밀리는 중"
            ref = (f'<div class="qb-ref"><span class="qb-dash"></span>'
                   f'{_html.escape(tail)}</div>')
        elif d["mode"] == "평단":
            ref = '<div class="qb-ref">두 계좌 모두 평단 기준으로 비교했습니다</div>'
    else:
        stuck = [k for k in (ka, kb) if not RS[k]["가능"]]
        if any(RS[k].get("사유") for k in stuck):
            why = "; ".join(f'{d["names"][k]} {RS[k].get("사유") or "측정 전"}'
                            for k in stuck)
            verdict = f'<div class="qb-ref">판정 보류: {_html.escape(why)}</div>'
        else:
            verdict = (f'<div class="qb-ref">판정 보류: '
                       f'{_html.escape(", ".join(d["names"][k] for k in stuck))} '
                       f'계좌를 같은 기준으로 잴 수 없습니다</div>' if stuck else "")
    return f'<div class="qb-panel">{head}{board}{rope}{verdict}{ref}{stats}</div>'


def _stat_row(label: str, a_txt: str, b_txt: str, a_val, b_val, *,
              split: bool = False, color: bool = False, judge: bool = True) -> str:
    """중계 화면식 한 줄 — 가운데 항목, 좌우 값, 이긴 쪽이 진하게.

    judge=False: 승패가 없는 값(미국 비중 등) — 둘 다 같은 굵기로.
    """
    wa = wb = ""
    if (judge and a_val is not None and b_val is not None
            and pd.notna(a_val) and pd.notna(b_val)):
        if a_val > b_val:
            wa = " win"
        elif b_val > a_val:
            wb = " win"
    ca = f" {_cls(a_val)}" if color else ""
    cb = f" {_cls(b_val)}" if color else ""
    bar = ""
    tot = (a_val or 0) + (b_val or 0) if split else 0
    if split and tot > 0:
        sa = 100 * (a_val or 0) / tot
        bar = (f'<div class="qb-split"><span class="sa" style="width:{sa:.1f}%">'
               f'</span><span class="sb"></span></div>')
    if not judge:
        wa = wb = " even"
    return (f'<div class="qb-row"><div class="v{wa}{ca}">{a_txt}</div>'
            f'<div class="lab">{_html.escape(label)}</div>'
            f'<div class="v r{wb}{cb}">{b_txt}</div>{bar}</div>')


def stats_html(d: dict, RS: dict, extra: list | None = None) -> str:
    perf = fresh_perf()
    ka, kb = d["keys"]
    rows = []
    rd = d.get("rounds")
    if rd is not None and len(rd):
        wa, wb = int((rd["승자"] == ka).sum()), int((rd["승자"] == kb).sum())
        rows.append(_stat_row("주간 라운드 승", f"{wa}승", f"{wb}승", wa, wb,
                              split=True))
    h = d.get("h2h")
    if h and (h["앞선날"]["a"] or h["앞선날"]["b"]):
        la, lb = h["앞선날"]["a"], h["앞선날"]["b"]
        rows.append(_stat_row("앞선 날", f"{la}일", f"{lb}일", la, lb, split=True))
    if d.get("today"):
        ta, tb = d["today"]
        rows.append(_stat_row(f"{d['asof']:%m/%d} 하루", _sgn(ta), _sgn(tb),
                              ta, tb, color=True))
    rows.extend(extra or [])
    hl = {k: perf.pnl_highlights(RS[k].get("pnl", pd.DataFrame())) for k in (ka, kb)}
    if all(hl[k]["계산됨"] for k in (ka, kb)):
        ra_ = hl[ka]["플러스"] / hl[ka]["계산됨"]
        rb_ = hl[kb]["플러스"] / hl[kb]["계산됨"]
        rows.append(_stat_row("수익 중인 종목",
                              f'{hl[ka]["플러스"]}/{hl[ka]["계산됨"]}',
                              f'{hl[kb]["플러스"]}/{hl[kb]["계산됨"]}', ra_, rb_))
    if h and h.get("역전"):
        rows.append(f'<div class="qb-row"><div></div><div class="lab">역전 '
                    f'<b>{h["역전"]}번</b></div><div></div></div>')
    return f'<div class="qb-stats">{"".join(rows)}</div>' if rows else ""


def _race(series: list):
    """누적 수익률 레이스. series: [{"이름", "곡선"(누적 배수), "색", "점선"}]."""
    import altair as alt
    parts = [pd.DataFrame({"날짜": x["곡선"].index, "구분": x["이름"],
                           "수익률": (x["곡선"].values - 1) * 100}) for x in series]
    long = pd.concat(parts, ignore_index=True)
    order = [x["이름"] for x in series]
    colors = [x["색"] for x in series]
    dashes = [[5, 4] if x.get("점선") else [1, 0] for x in series]
    widths = [1.6 if x.get("점선") else 2.4 for x in series]
    t0, t1 = long["날짜"].min(), long["날짜"].max()
    # 끝점이 잘리지 않을 만큼만 여백. 눈금은 데이터가 있는 날까지만 찍습니다
    # — 빈 칸에 미래 날짜가 찍히면 예측처럼 읽힙니다.
    pad = (t1 - t0) * 0.03 + pd.Timedelta(days=1)
    ticks = [alt.DateTime(year=t.year, month=t.month, date=t.day)
             for t in pd.date_range(t0, t1, freq="W-MON")]
    x = alt.X("날짜:T", title=None, scale=alt.Scale(domain=[t0, t1 + pad]),
              axis=alt.Axis(grid=False, values=ticks, format="%m/%d",
                            labelOverlap=True))
    sc = dict(sort=order, legend=None)
    base = alt.Chart(long).encode(
        x=x, y=alt.Y("수익률:Q", title="누적 수익률 (%)",
                     axis=alt.Axis(gridOpacity=0.35)),
        color=alt.Color("구분:N", title=None,
                        scale=alt.Scale(domain=order, range=colors), **sc),
        strokeDash=alt.StrokeDash("구분:N",
                                  scale=alt.Scale(domain=order, range=dashes), **sc),
        strokeWidth=alt.StrokeWidth("구분:N",
                                    scale=alt.Scale(domain=order, range=widths), **sc))
    lines = base.mark_line(interpolate="monotone")
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        strokeWidth=1, opacity=0.4).encode(y="y:Q")
    solid = [x["이름"] for x in series if not x.get("점선")]
    last = long[long["구분"].isin(solid)].sort_values("날짜").groupby("구분").tail(1)
    ends = alt.Chart(last).mark_circle(size=60, opacity=1).encode(
        x="날짜:T", y="수익률:Q",
        color=alt.Color("구분:N", scale=alt.Scale(domain=order, range=colors), **sc))
    hover = alt.selection_point(fields=["날짜"], nearest=True, on="pointerover",
                                empty=False, clear="pointerout")
    wide = long.pivot_table(index="날짜", columns="구분", values="수익률").reset_index()
    tips = [alt.Tooltip("날짜:T", format="%m/%d")] + [
        alt.Tooltip(f"{c}:Q", format="+.2f") for c in order if c in wide.columns]
    rule = alt.Chart(wide).mark_rule(opacity=0.35).encode(
        x="날짜:T",
        opacity=alt.condition(hover, alt.value(0.45), alt.value(0)),
        tooltip=tips).add_params(hover)
    return (zero + lines + rule + ends).properties(height=270), long


def _legend_html(series: list, long: pd.DataFrame) -> str:
    """차트 위 범례 — 폰에서 줄바꿈되고, 글씨가 테마를 따라갑니다."""
    final = long.sort_values("날짜").groupby("구분")["수익률"].last()
    out = []
    for x in series:
        n = x["이름"]
        if n not in final.index:
            continue
        side = x.get("쪽", "")
        mark = (f'<span class="qb-dash {side}"></span>' if x.get("점선")
                else f'<span class="qb-dot {side}"></span>')
        out.append(f'<span>{mark}{_html.escape(n)} <b class="{_cls(final[n])}">'
                   f'{_sgn(final[n] / 100, 1)}</b></span>')
    return '<div class="qb-legend">' + "".join(out) + '</div>'


def _chart_race(d: dict, RS: dict, market: str):
    """시장별 레이스 — 두 사람 + 지수(회색 점선)."""
    trio = series_trio()
    ka, kb = d["keys"]
    series = [{"이름": d["names"][ka], "곡선": RS[ka]["tw"]["누적"],
               "색": trio["상수"], "쪽": "a"},
              {"이름": d["names"][kb], "곡선": RS[kb]["tw"]["누적"],
               "색": trio["배우자"], "쪽": "b"}]
    bench = next((RS[k]["bench"] for k in (ka, kb)
                  if not RS[k].get("bench", pd.DataFrame()).empty), None)
    if bench is not None:
        series.append({"이름": PERF_MARKETS[market]["지수"], "곡선": bench["누적"],
                       "색": trio["지수"], "점선": True})
    ch, long = _race(series)
    return ch, long, series


def _chart_gap(d: dict):
    """격차 — 0 위는 상수 리드, 아래는 배우자 리드."""
    import altair as alt
    trio = series_trio()
    g = d["h2h"]["격차"]
    df = pd.DataFrame({"날짜": g.index, "격차": g.values})
    df["위"] = df["격차"].clip(lower=0)
    df["아래"] = df["격차"].clip(upper=0)
    na, nb = d["names"][d["keys"][0]], d["names"][d["keys"][1]]
    ticks = [alt.DateTime(year=t.year, month=t.month, date=t.day)
             for t in pd.date_range(df["날짜"].min(), df["날짜"].max(), freq="W-MON")]
    x = alt.X("날짜:T", title=None, axis=alt.Axis(grid=False, values=ticks,
                                                 format="%m/%d", labelOverlap=True))
    top = alt.Chart(df).mark_area(opacity=0.55, interpolate="monotone",
                                  color=trio["상수"]).encode(x=x, y=alt.Y(
                                      "위:Q", title="격차 (%p)"))
    bot = alt.Chart(df).mark_area(opacity=0.55, interpolate="monotone",
                                  color=trio["배우자"]).encode(x=x, y="아래:Q")
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(opacity=0.5).encode(y="y:Q")
    pts = alt.Chart(df).mark_circle(size=90, opacity=0).encode(
        x=x, y="격차:Q",
        tooltip=[alt.Tooltip("날짜:T", format="%m/%d"),
                 alt.Tooltip("격차:Q", format="+.2f", title=f"{na} − {nb} (%p)")])
    return (top + bot + zero + pts).properties(height=150)


def rounds_html(d: dict) -> str:
    rd = d.get("rounds")
    if rd is None or rd.empty:
        return ""
    ka, kb = d["keys"]
    na, nb = (_html.escape(d["names"][ka]), _html.escape(d["names"][kb]))
    has_idx = "지수" in rd.columns
    head = (f'<tr><th>라운드</th><th>{na}</th><th>{nb}</th>'
            + ('<th>지수</th>' if has_idx else "") + '</tr>')
    body = []
    rd = rd.reset_index(drop=True)
    for i in range(len(rd) - 1, max(-1, len(rd) - 9), -1):
        r = rd.iloc[i]
        live = bool(r["진행중"])
        tag = "진행 중" if live else f'{r["시작"]:%m/%d}~{r["끝"]:%m/%d}'
        win = r["승자"]
        ca = f'win a' if (win == ka and not live) else ""
        cb = f'win b' if (win == kb and not live) else ""
        cells = (f'<td class="wk"><b>{i + 1}R</b><small>{tag}</small></td>'
                 f'<td class="{ca} {_cls(r[ka])}">{_sgn(r[ka])}</td>'
                 f'<td class="{cb} {_cls(r[kb])}">{_sgn(r[kb])}</td>')
        if has_idx:
            cells += f'<td class="idx">{_sgn(r["지수"])}</td>'
        body.append(f'<tr class="{"live" if live else ""}">{cells}</tr>')
    done = rd[~rd["진행중"].astype(bool)]
    wa, wb = int((done["승자"] == ka).sum()), int((done["승자"] == kb).sum())
    tally = (f'<div class="qb-tally">끝난 라운드 전적 <b>{na} {wa}승</b> : '
             f'<b>{nb} {wb}승</b>')
    if has_idx and len(done):
        both = int(((done[ka] > done["지수"]) & (done[kb] > done["지수"])).sum())
        tally += f', 둘 다 지수를 이긴 주 {both}번'
    tally += '</div>'
    return f'{tally}<table class="qb-rounds">{head}{"".join(body)}</table>'


def _nm(r) -> str:
    n = r.get("종목명")
    return str(n) if n is not None and pd.notna(n) and str(n).strip() else str(r["종목코드"])


def highlights_html(d: dict, RS: dict, market: str) -> str:
    perf = fresh_perf()
    ka, kb = d["keys"]
    def money(v, mk=None):
        if v is None or not pd.notna(v):
            return ""
        m = mk or market
        dp, u, pr = (0, "원", "") if m == "kr" else (2, "", "$")
        s = f"{pr}{abs(v):,.{dp}f}{u}"
        return ("+" if v > 0 else "−" if v < 0 else "") + s

    def items(df):
        if df is None or df.empty:
            return '<div class="qb-empty">없음</div>'
        out = []
        for _, r in df.iterrows():
            mk = r.get("시장") if "시장" in df.columns else None
            flag = {"kr": "🇰🇷 ", "us": "🇺🇸 "}.get(mk, "")
            out.append(f'<div class="qb-it"><span class="nm">{flag}'
                       f'{_html.escape(_nm(r))}</span>'
                       f'<span class="pc {_cls(r["수익률"])}">{_arrow(r["수익률"])}</span>'
                       f'<span class="am">{money(r["손익"], mk)}</span></div>')
        return "".join(out)

    cols = []
    for k, side in ((ka, "a"), (kb, "b")):
        hl = perf.pnl_highlights(RS[k].get("pnl", pd.DataFrame()))
        cols.append(f'<div><div class="hd"><span class="qb-dot {side}"></span>'
                    f'{_html.escape(d["names"][k])}</div>'
                    f'<div class="grp">🔥 효자 종목</div>{items(hl["효자"])}'
                    f'<div class="grp">🧊 아픈 손가락</div>{items(hl["아픈"])}</div>')
    return f'<div class="qb-duo">{"".join(cols)}</div>'


def movers_html(d: dict, RS: dict) -> tuple[str, str]:
    """두 계좌 보유 종목 전체에서 최근 거래일 급등·급락 (누가 들고 있는지 점으로)."""
    perf = fresh_perf()
    ka, kb = d["keys"]
    own, names, px, asof = {}, {}, None, None
    for k in (ka, kb):
        pnl = RS[k].get("pnl", pd.DataFrame())
        if pnl is None or pnl.empty:
            continue
        for c, (_, r) in zip(perf._code6(pnl["종목코드"]), pnl.iterrows()):
            own.setdefault(c, set()).add(k)
            names[c] = _nm(r)
        kp = RS[k].get("px", pd.DataFrame())
        if kp is not None and not kp.empty:
            px = kp if px is None else px.combine_first(kp)
    if px is None or not own:
        return "", ""
    mv = perf.last_day_moves(px, own.keys())
    if mv.empty:
        return "", ""
    asof = px.index.max()

    def row(c, v):
        dots = "".join(f'<span class="qb-dot {s}"></span>'
                       for k, s in ((ka, "a"), (kb, "b")) if k in own.get(c, ()))
        return (f'<div class="qb-it"><span class="nm"><span class="who">{dots}</span>'
                f'{_html.escape(names.get(c, c))}</span>'
                f'<span class="pc {_cls(v)}">{_arrow(v)}</span></div>')

    ups = mv[mv > 0].sort_values(ascending=False).head(3)
    dns = mv[mv < 0].sort_values().head(3)
    body = (f'<div class="qb-duo"><div><div class="grp">🚀 오늘 많이 오른 종목</div>'
            + ("".join(row(c, v) for c, v in ups.items()) or '<div class="qb-empty">없음</div>')
            + '</div><div><div class="grp">💥 오늘 많이 내린 종목</div>'
            + ("".join(row(c, v) for c, v in dns.items()) or '<div class="qb-empty">없음</div>')
            + '</div></div>')
    return body, f"{asof:%m/%d}"


def render_battle(RS: dict, market: str) -> None:
    """성과 탭 맨 위 — 스코어보드, 매치 스탯, 레이스, 라운드, 하이라이트."""
    d = duel(RS, market)
    if len(d["keys"]) != 2:
        return
    st.subheader("⚔️ 수익률 배틀")
    _qb(scoreboard_html(d, RS, stats=stats_html(d, RS) if d["mode"] else ""))
    if d["mode"] == "twr":
        st.caption("입금을 걷어낸 시간가중수익률로 겨루니 입금액이 달라도 "
                   "공정합니다. 주간 라운드는 금요일 마감입니다.")

        ch, long, series = _chart_race(d, RS, market)
        st.markdown("**레이스**")
        _qb(_legend_html(series, long))
        st.altair_chart(ch, use_container_width=True)
        if len(d["h2h"]["격차"]) >= 2:
            st.markdown("**누가 앞섰나**")
            ka, kb = d["keys"]
            _qb(f'<div class="qb-sub"><span class="qb-dot a"></span> 위는 '
                f'{_html.escape(_ga(d["names"][ka]))} 앞선 날, '
                f'<span class="qb-dot b"></span> 아래는 '
                f'{_html.escape(_ga(d["names"][kb]))} 앞선 날</div>')
            st.altair_chart(_chart_gap(d), use_container_width=True)
        st.download_button(
            "곡선 데이터 CSV", to_csv(long, excel_safe=False),
            file_name=f"curve_{market}_{tag}.csv", mime="text/csv",
            key=f"dl_curve_{market}")
        rh = rounds_html(d)
        if rh:
            st.markdown("**주간 라운드**")
            _qb(rh)
    elif d["mode"] == "평단":
        st.caption("한쪽이라도 시간가중수익률을 쓸 수 없어서 평단 기준으로 "
                   "겨룹니다. 레이스와 라운드는 두 계좌 모두 시간가중이 잡히면 "
                   "나옵니다.")

    st.markdown("**종목 하이라이트**")
    _qb(highlights_html(d, RS, market))
    mv, day = movers_html(d, RS)
    if mv:
        st.markdown(f"**{day} 급등·급락**")
        _qb(mv)
        st.caption("두 사람이 들고 있는 종목 중에서 골랐습니다. 앞의 점이 누구 "
                   "종목인지 보여 줍니다.")
    st.divider()


# ── 종합 — 국장 + 미국장을 원화로 합친 배틀 ────────────────────
# 계산은 perf.combine_markets / blended_bench (tools/test_battle.py로 검증).
# 환율은 Frankfurter API(유럽중앙은행 등 기준환율, 키 없음)에서 받습니다.
# 못 받으면 합치지 않습니다 — 환율 없이 원화와 달러를 더하면 거짓 숫자입니다.
FX_URL = "https://api.frankfurter.dev/v1/{start}..?base=USD&symbols=KRW"
_MK_LABEL = {"kr": "국장", "us": "미국장"}
_NOT_HELD = ("기록없음", "계좌기록없음", "설정없음")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def usdkrw(start: str) -> pd.Series:
    """날짜 → 1달러당 원화 (영업일만, 주말·휴일은 쓰는 쪽에서 전날 값)."""
    try:
        r = rq.get(FX_URL.format(start=start), timeout=15)
        if r.status_code != 200:
            return pd.Series(dtype="float64")
        rates = (r.json() or {}).get("rates") or {}
        s = pd.Series({pd.Timestamp(d): float(v["KRW"]) for d, v in rates.items()
                       if isinstance(v, dict) and v.get("KRW")}, dtype="float64")
        return s.sort_index()
    except Exception:
        return pd.Series(dtype="float64")


def combined_perf(acct: str) -> dict:
    """한 사람의 국장 + 미국장 합산 성과 (원화 시간가중)."""
    perf = fresh_perf()
    out = {"계좌": acct, "가능": False, "이유": "", "사유": "", "시장": {},
           "pnl": pd.DataFrame(), "평단": None, "미국있음": False}
    parts, benches, blocked, pnls = {}, {}, [], []
    for mk in ("kr", "us"):
        r = account_perf(acct, mk)
        out["시장"][mk] = {"이유": r["이유"],
                         "수익률": r.get("수익률") if r["가능"] else None}
        if r["이유"] in _NOT_HELD:
            continue
        tw = r.get("tw")
        if not (r["가능"] or r["이유"] == "한점") or tw is None or tw.empty:
            blocked.append(f'{_MK_LABEL[mk]} {_WHY.get(r["이유"], "측정 전")}')
            continue
        parts[mk] = tw
        b = r.get("bench", pd.DataFrame())
        if b is not None and not b.empty:
            benches[mk] = b["누적"]
        p = r.get("pnl", pd.DataFrame())
        if p is not None and not p.empty:
            pnls.append(p.assign(시장=mk))
    out["pnl"] = pd.concat(pnls, ignore_index=True) if pnls else pd.DataFrame()
    out["미국있음"] = "us" in parts
    if blocked:
        out.update(이유="시장보류", 사유=", ".join(blocked))
        return out
    if not parts:
        out.update(이유="기록없음", 사유="보유 기록 없음")
        return out
    fx = None
    if "us" in parts:
        start = min(t.index.min() for t in parts.values()) - pd.Timedelta(days=10)
        fx = usdkrw(f"{start:%Y-%m-%d}")
        if fx.empty:
            out.update(이유="환율없음", 사유="환율을 받지 못함")
            return out
    comb = perf.combine_markets(parts, fx)
    if len(comb) < 2:
        out.update(이유="한점", 사유="재동기화 1회 — 다음 주부터")
        return out
    bench = perf.blended_bench(comb, benches, fx)
    last = comb.iloc[-1]
    cols = [c for c in comb.columns if c.startswith("평가액_")]
    tot = float(sum(last[c] for c in cols)) or 1.0
    for mk in parts:
        out["시장"][mk]["비중"] = float(last[f"평가액_{mk}"]) / tot
    out.update(가능=True, tw=comb, bench=pd.DataFrame({"누적": bench}),
               수익률=float(comb["누적"].iloc[-1] - 1),
               지수=float(bench.iloc[-1] - 1) if len(bench) else None)
    if fx is not None:
        usk = perf.combine_markets({"us": parts["us"]}, fx)
        out["환율효과"] = (float(usk["누적"].iloc[-1] - 1)
                       - float(parts["us"]["누적"].iloc[-1] - 1))
        out["환율"] = (float(fx.iloc[-1]), fx.index[-1])
    return out


def _market_rows(d: dict, RSall: dict) -> list:
    """종합 매치 스탯에 끼워 넣는 시장별 줄."""
    ka, kb = d["keys"]
    rows = []
    for mk in ("kr", "us"):
        va = RSall[ka]["시장"].get(mk, {}).get("수익률")
        vb = RSall[kb]["시장"].get(mk, {}).get("수익률")
        if va is None and vb is None:
            continue
        rows.append(_stat_row(_MK_LABEL[mk] + (" (달러)" if mk == "us" else ""),
                              _sgn(va), _sgn(vb), va, vb, color=True))
    wa = RSall[ka]["시장"].get("us", {}).get("비중")
    wb = RSall[kb]["시장"].get("us", {}).get("비중")
    if wa is not None or wb is not None:
        f = lambda w: f"{w * 100:.0f}%" if w is not None else "0%"  # noqa: E731
        rows.append(_stat_row("미국 비중", f(wa), f(wb), wa, wb, judge=False))
    fa, fb = RSall[ka].get("환율효과"), RSall[kb].get("환율효과")
    if fa is not None or fb is not None:
        rows.append(_stat_row("환율 효과", _sgn(fa, unit="%p"), _sgn(fb, unit="%p"),
                              fa, fb, judge=False, color=True))
    return rows


def all_results() -> tuple[dict, dict]:
    RSall = {a: combined_perf(a) for a in ACCOUNTS.values()}
    return RSall, duel(RSall, "all")


def render_all() -> None:
    """🏆 종합 탭 — 국장 + 미국장 원화 합산 배틀."""
    if not (STATE_REPO and STATE_TOKEN):
        st.info("비공개 상태 레포가 설정되지 않아 성과를 볼 수 없습니다.")
        return
    RSall, d = all_results()
    if len(d["keys"]) != 2:
        return
    ka, kb = d["keys"]
    if not any(RSall[k]["미국있음"] for k in (ka, kb)) and d["mode"]:
        st.info("미국 계좌 기록이 아직 없어 종합은 국장 성과와 같습니다. "
                "성과(한국) 탭을 보세요.")
        return
    st.subheader("🏆 종합 배틀 (국장 + 미국장)")
    stats = (stats_html(d, RSall, extra=_market_rows(d, RSall))
             if d["mode"] else "")
    _qb(scoreboard_html(d, RSall, stats=stats))
    if not d["mode"]:
        st.info("두 사람의 국장·미국장을 같은 기준(원화 시간가중)으로 잴 수 있게 "
                "되면 종합 승부가 나옵니다. 사유는 위 스코어보드에 있습니다.")
        return
    fx = next((RSall[k].get("환율") for k in (ka, kb) if RSall[k].get("환율")), None)
    fx_txt = (f" 미국 주식은 날마다 유럽중앙은행 기준환율로 바꿉니다 (최근 "
              f"{fx[0]:,.1f}원, {fx[1]:%m/%d})." if fx else "")
    st.caption("국장과 미국장을 원화로 합친 시간가중수익률입니다." + fx_txt
               + " 지수는 각자의 국장·미국장 비중대로 시총상위200 지수와 "
               "SPY(원화 환산)를 샀다면의 수익률이라, 사람마다 다릅니다. "
               "환율 효과는 미국 주식을 원화로 잰 수익률에서 달러로 잰 수익률을 "
               "뺀 값입니다.")
    trio = series_trio()
    series = [{"이름": d["names"][ka], "곡선": RSall[ka]["tw"]["누적"],
               "색": trio["상수"], "쪽": "a"},
              {"이름": d["names"][kb], "곡선": RSall[kb]["tw"]["누적"],
               "색": trio["배우자"], "쪽": "b"}]
    for k, side, col in ((ka, "a", trio["상수"]), (kb, "b", trio["배우자"])):
        b = RSall[k].get("bench", pd.DataFrame())
        if b is not None and not b.empty:
            series.append({"이름": f'지수({d["names"][k]} 비중)', "곡선": b["누적"],
                           "색": col, "점선": True, "쪽": side})
    ch, long = _race(series)
    st.markdown("**레이스**")
    _qb(_legend_html(series, long))
    st.altair_chart(ch, use_container_width=True)
    if len(d["h2h"]["격차"]) >= 2:
        st.markdown("**누가 앞섰나**")
        _qb(f'<div class="qb-sub"><span class="qb-dot a"></span> 위는 '
            f'{_html.escape(_ga(d["names"][ka]))} 앞선 날, '
            f'<span class="qb-dot b"></span> 아래는 '
            f'{_html.escape(_ga(d["names"][kb]))} 앞선 날</div>')
        st.altair_chart(_chart_gap(d), use_container_width=True)
    rh = rounds_html(d)
    if rh:
        st.markdown("**주간 라운드**")
        _qb(rh)
    st.markdown("**종목 하이라이트** (두 시장 통틀어)")
    _qb(highlights_html(d, RSall, "kr"))
    st.caption("종목 수익률은 그 시장 통화 기준입니다 (미국 종목은 달러).")


def perf_strip() -> None:
    """화면 맨 위 — 계좌 수익률. 제일 먼저 보고 싶은 숫자입니다.

    시장을 **줄로 갈라** 놓습니다. 네 칸을 한 줄에 넣으면 폰에서 글자가
    뭉개지고, 더 나쁜 건 원화 수익률과 달러 수익률이 나란히 붙어 한 계좌의
    숫자처럼 읽힌다는 점입니다. 기록이 아예 없는 시장은 줄째로 빠집니다.
    """
    if not (STATE_REPO and STATE_TOKEN):
        return

    per_market = []
    for mk in ("kr", "us"):
        rows = [(lab, account_perf(a, mk)) for lab, a in ACCOUNTS.items()]
        if not all(r["이유"] == "기록없음" for _, r in rows):
            per_market.append((mk, rows))
    if not per_market:
        return

    asof = None
    rows = [r for _, rs in per_market for _, r in rs]
    if any(mk == "us" for mk, _ in per_market):
        RSall, dall = all_results()
        if any(RSall[k]["미국있음"] for k in dall["keys"]) and dall["mode"]:
            _qb(scoreboard_html(dall, RSall, mini=True,
                                label="🏆 종합 (국장 + 미국장, 원화)"), mini=True)
    for mk, mrows in per_market:
        P = PERF_MARKETS[mk]
        RSm = {r["계좌"]: r for _, r in mrows}
        flag = "🇰🇷 " if mk == "kr" else "🇺🇸 "
        # 같은 측정으로 잴 수 있을 때만 승자를 띄웁니다 (duel 참고).
        # 시간가중과 평단을 맞대지 않습니다 — 섞으면 승패가 거짓입니다.
        _qb(scoreboard_html(duel(RSm, mk), RSm, mini=True,
                            label=flag + P["라벨"]), mini=True)
        for lab, r in mrows:
            if mk == "kr":
                # 안내문의 '마지막 종가'는 한국 기준으로 씁니다 — 두 시장의
                # 휴장일이 달라 한 날짜로 뭉치면 둘 다 틀립니다.
                asof = r.get("주가기준일") or asof

    # 입출금이 빠져 TWR을 못 믿는 계좌가 있으면 그 사실을 먼저 알립니다.
    # 'alt'가 아니라 화면의 계좌 이름(배우자)으로 적습니다.
    names = {a: lab.split(" (")[0] for lab, a in ACCOUNTS.items()}
    bad = [(f"{names.get(r['계좌'], r['계좌'])}·"
            f"{PERF_MARKETS[r['시장']]['라벨']}", r)
           for r in rows if r["이유"] == "입출금누락"]
    if bad:
        st.warning(
            "**시간가중수익률을 띄우지 않았습니다** — "
            + " / ".join(f"{n}: {r.get('신뢰사유', '')}" for n, r in bad)
            + "\n\n체결내역(체결가·체결수량·체결일)을 inbox에 올리면 "
              "복구됩니다. 위에 보이는 수익률은 **평단 기준**이고 그건 맞는 "
              "숫자입니다.")

    done = [r for r in rows if r["가능"]]
    bits = []
    if asof is not None:
        bits.append(f"**주가 {asof:%m/%d} 종가** 기준")
    if done:
        bits.append(f"측정 {min(r['시작'] for r in done):%m/%d} 시작")
        bits.append("입금을 걷어낸 시간가중수익률")
    elif any(r.get("평단") for r in rows):
        bits.append("평단 기준 (시간가중수익률은 거래일 2개부터)")
    bits.append("자세한 내용은 아래 **성과** 탭 (시장별로 따로 있습니다)")
    st.caption(" · ".join(bits))
    # 왜 오늘 숫자가 안 바뀌었는지를 화면에서 바로 알 수 있게 — 일간 실행은
    # GitHub가 예약을 6~9시간 미뤄서(실측) 밤 11시~자정에 끝납니다.
    if asof is not None:
        stale = (pd.Timestamp.now(tz="Asia/Seoul").normalize().tz_localize(None)
                 - pd.Timestamp(asof).normalize()).days
        if stale >= 1:
            st.caption(
                f"↑ 마지막 종가가 {stale}일 전입니다. 평일 종가는 **그날 밤 "
                "11시~자정(KST)** 에 올라옵니다 — GitHub가 예약 실행을 "
                "6~9시간 미루기 때문입니다. 주말·휴장일은 갱신이 없습니다.")


st.markdown(_BATTLE_CSS, unsafe_allow_html=True)
st.title("📈 퀀트 에이전트")

perf_strip()

_, conn_err = release_assets()
if conn_err:
    st.error(conn_err)

market_label = st.radio("시장", list(MARKETS.keys()),
                        horizontal=True, label_visibility="collapsed")
M = MARKETS[market_label]

run_info = load_run(M["run"])
model = load_model(M["model"])
tag = str(run_info.get("finished", ""))[:10].replace("-", "") or \
    datetime.now(KST).strftime("%Y%m%d")

if run_info:
    c1, c2, c3 = st.columns(3)
    c1.metric("최근 실행", run_info.get("mode", "-"))
    c2.metric("소요", f"{run_info.get('minutes', 0)}분")
    c3.metric("완료", kst(run_info.get("finished", "")))
    st.markdown(f"상태 {badge(run_info.get('status', '?'))}", unsafe_allow_html=True)
else:
    st.info(f"{market_label} 실행 결과가 아직 없습니다. "
            "아래 **실행** 탭에서 월간 실행을 먼저 한 번 돌려주세요.")

top_label = "🔔 매수 신호" if M["has_signals"] else "🔔 상위 종목"
# 성과만 시장 탭을 따로 둡니다 — 위의 '시장' 라디오는 모델·실행·백테스트를
# 가르는 것이고, 성과는 두 계좌·두 시장을 **나란히 보고 싶은** 화면입니다.
(tab_all, tab_perf_kr, tab_perf_us, tab_top, tab_run, tab_model, tab_bt,
 tab_log) = st.tabs(
    ["🏆 종합", "💰 성과(한국)", "💰 성과(미국)", top_label, "▶️ 실행",
     "📊 모델", "🧪 백테스트", "📜 기록"])

SYM, NAME, SEC = M["symbol"], M["name"], M["sector"]


# ── 매수 신호 / 상위 종목 ────────────────────────────────────
with tab_top:
    if model.empty:
        st.warning("모델 결과가 아직 없습니다.")
    else:
        if M["has_signals"]:
            buys = model[model["매수/매도"].astype(str).str.contains("매수", na=False)]
            sells = model[model["매수/매도"].astype(str).str.contains("매도", na=False)]

            c1, c2, c3 = st.columns(3)
            c1.metric("선정 종목", f"{len(model):,}")
            c2.metric("매수 신호", f"{len(buys):,}")
            c3.metric("매도 신호", f"{len(sells):,}")

            cols = [c for c in [NAME, SYM, SEC, "매수/매도", "RSI", "MACD", "BB", "qvm"]
                    if c in model.columns]

            st.subheader("매수 신호")
            if len(buys):
                st.dataframe(
                    buys[cols].sort_values(["매수/매도", "qvm"], ascending=[False, True]),
                    use_container_width=True, hide_index=True, height=400)
            else:
                st.write("현재 매수 신호 종목이 없습니다.")

            with st.expander(f"매도 신호 {len(sells)}종목"):
                if len(sells):
                    st.dataframe(sells[cols].sort_values("매수/매도"),
                                 use_container_width=True, hide_index=True)
                else:
                    st.write("없습니다.")
            primary, primary_name = buys[cols], "buy_signals"
        else:
            # 미국장은 기술적 신호 없이 QVM 순위만 냅니다
            universe = None
            for s in run_info.get("steps", []):
                u = (s.get("stats") or {}).get("universe")
                if isinstance(u, int):
                    universe = u

            c1, c2 = st.columns(2)
            c1.metric("선정 종목", f"{len(model):,}")
            c2.metric("유니버스", f"{universe:,}" if universe else "-")

            cols = [c for c in [NAME, SYM, SEC, "qvm", "z_quality", "z_value",
                                "z_momentum", "PER", "PBR", "DY"]
                    if c in model.columns]
            st.subheader("QVM 상위 100종목")
            st.caption("qvm 점수가 낮을수록 상위입니다.")
            st.dataframe(model[cols].head(100), use_container_width=True,
                         hide_index=True, height=400)
            primary, primary_name = model[cols].head(100), "top100"

        # ── 내려받기 ────────────────────────────────────────
        st.divider()
        st.markdown("##### 결과 내려받기")

        excel_safe = st.radio(
            "어디서 여실 건가요?",
            ["엑셀에서 열기", "pandas로 읽기"],
            horizontal=True, key="csv_mode",
            help="엑셀은 CSV를 열 때 005930을 5930으로 바꿉니다. "
                 "'엑셀에서 열기'를 고르면 종목코드를 텍스트로 고정해 내보냅니다.",
        ) == "엑셀에서 열기"

        d1, d2 = st.columns(2)
        d1.download_button(
            f"⬇️ {'매수 신호' if M['has_signals'] else '상위 100'} CSV ({len(primary)})",
            to_csv(primary, excel_safe),
            file_name=f"{M['key']}_{primary_name}_{tag}.csv", mime="text/csv",
            disabled=primary.empty, key="dl_top")
        d2.download_button(
            f"⬇️ 전체 모델 CSV ({len(model)})",
            to_csv(model, excel_safe),
            file_name=f"{M['key']}_model_{tag}.csv", mime="text/csv", key="dl_all")

        st.caption("UTF-8(BOM)으로 저장돼 한글은 어느 쪽이든 깨지지 않습니다."
                   + ("  종목코드는 텍스트로 고정됩니다." if excel_safe
                      else "  종목코드가 숫자로 바뀔 수 있으니 엑셀로는 열지 마세요."))

        with st.expander("엑셀(.xlsx) 원본"):
            assets, _ = release_assets()
            xlsx = sorted([n for n in assets
                           if n.endswith(".xlsx") and M["xlsx_key"] in n])
            if xlsx:
                latest = xlsx[-1]
                blob = download_asset(latest)
                if blob:
                    st.download_button(
                        f"⬇️ {latest}", blob, file_name=latest,
                        mime="application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet", key="dl_xlsx")
            else:
                st.write("아직 엑셀 파일이 없습니다.")


# ── 실행 ─────────────────────────────────────────────────────
with tab_run:
    runs = recent_runs(10)
    active = [r for r in runs if r["status"] in ("queued", "in_progress", "waiting")]
    mine = [r for r in active
            if r.get("path", "").endswith((M["daily_wf"], M["monthly_wf"]))]

    if mine:
        r = mine[0]
        st.info(f"⏳ **{r['name']}** 실행 중 — 시작 {kst(r['created_at'])}")
        st.link_button("실행 로그 열기", r["html_url"])
    elif active:
        st.caption(f"다른 시장 작업이 실행 중입니다 — {active[0]['name']}")
    else:
        st.caption("지금 실행 중인 작업은 없습니다.")

    if st.button("🔄 상태 새로고침", key="refresh_run"):
        st.cache_data.clear()
        st.rerun()

    st.divider()

    with st.expander("고급 설정"):
        test_mode = st.checkbox("테스트 실행 (30종목만, 약 3분)", value=False,
                                help="수집이 막히지 않는지 빠르게 확인할 때 쓰세요.")
        if M["key"] == "kr":
            knob = st.slider("동시 요청 수 (차단되면 낮추세요)", 1, 10, 6)
        else:
            knob = st.slider("yfinance 묶음 크기 (차단되면 낮추세요)", 20, 200, 100, step=10)
    limit = "30" if test_mode else "0"

    st.markdown(f"##### {market_label} 일간 실행")
    st.caption(M["daily_note"])
    if st.button("▶️ 일간 실행", key="run_daily", disabled=bool(mine)):
        inputs = ({"ticker_limit": limit, "price_workers": str(knob)}
                  if M["key"] == "kr" else
                  {"ticker_limit": limit, "yf_chunk": str(knob)})
        ok, msg = dispatch(M["daily_wf"], inputs)
        (st.success if ok else st.error)(msg)
        if ok:
            time.sleep(3)
            st.cache_data.clear()
            st.rerun()

    st.markdown(f"##### {market_label} 월간 실행")
    st.caption(M["monthly_note"] + " (처음 한 번은 필수)")
    if st.button("🗓️ 월간 실행", key="run_monthly", disabled=bool(mine)):
        inputs = ({"ticker_limit": limit, "price_workers": str(knob), "fs_workers": "3"}
                  if M["key"] == "kr" else
                  {"ticker_limit": limit, "yf_chunk": str(knob), "fs_chunk": "50"})
        ok, msg = dispatch(M["monthly_wf"], inputs)
        (st.success if ok else st.error)(msg)
        if ok:
            time.sleep(3)
            st.cache_data.clear()
            st.rerun()


# ── 모델 ─────────────────────────────────────────────────────
with tab_model:
    if model.empty:
        st.warning("모델 결과가 아직 없습니다.")
    else:
        sectors = ["전체"] + sorted(model[SEC].dropna().astype(str).unique().tolist())
        sec = st.selectbox("섹터", sectors)
        view = model if sec == "전체" else model[model[SEC].astype(str) == sec]

        if M["has_signals"]:
            if st.checkbox("매수 신호만", value=False):
                view = view[view["매수/매도"].astype(str).str.contains("매수", na=False)]

        st.caption(f"{len(view):,}종목 · qvm 점수가 낮을수록 상위")
        show = [c for c in [NAME, SYM, SEC, "qvm", "z_quality", "z_value",
                            "z_momentum", "PER", "PBR", "DY", "RSI", "매수/매도"]
                if c in view.columns]
        st.dataframe(view[show].head(300), use_container_width=True,
                     hide_index=True, height=440)

        st.download_button(
            f"⬇️ 지금 화면 그대로 CSV ({len(view)}종목)",
            to_csv(view, st.session_state.get("csv_mode", "엑셀에서 열기") == "엑셀에서 열기"),
            mime="text/csv", key="dl_view",
            file_name=f"{M['key']}_model_{'all' if sec == '전체' else sec}_{tag}.csv")

        if len(view) > 5:
            st.subheader("팩터 분포")
            import altair as alt
            factor = st.radio("항목", ["qvm", "z_quality", "z_value", "z_momentum"],
                              horizontal=True)
            if factor in view.columns:
                chart = (alt.Chart(view[[factor]].dropna())
                         .mark_bar(color="#4c78a8")
                         .encode(alt.X(f"{factor}:Q", bin=alt.Bin(maxbins=40), title=factor),
                                 alt.Y("count()", title="종목 수"))
                         .properties(height=220))
                st.altair_chart(chart, use_container_width=True)


# ── 공용 — 두 탭(성과·백테스트)이 같은 색을 씁니다 ──────────
def series_colors() -> tuple[str, str]:
    """포트폴리오 / 벤치마크 색. 두 테마 모두 대비·색각 검사를 통과한 값."""
    try:
        dark = st.get_option("theme.base") == "dark"
    except Exception:
        dark = False
    return ("#3987e5", "#d95926") if dark else ("#2a78d6", "#eb6834")


# ── 성과 (실제 계좌 — 두 계좌를 한 화면에) ───────────────────
# 계좌를 라디오로 전환하면 두 계좌를 나란히 못 봅니다. 비교가 목적이니
# 한 화면에 같이 올리고, 금액이 큰 표만 계좌 열로 묶습니다.
#
# 선 색은 위의 series_trio (상수 보라 / 배우자 민트 / 지수 회색 점선).


def _won(v) -> str:
    return f"{v:,.0f}원" if v is not None and pd.notna(v) else "–"


def _money(v, market: str = "kr") -> str:
    """금액 표기 — 원은 정수, 달러는 소수 둘째 자리까지.

    달러를 '19,632원'처럼 쓰면 자릿수가 원화와 같아 보여서 두 시장을 한
    화면에서 볼 때 섞여 읽힙니다. 통화 기호를 **항상** 붙입니다.
    """
    if v is None or not pd.notna(v):
        return "–"
    return f"{v:,.0f}원" if market == "kr" else f"${v:,.2f}"


def _pct(v, p: int = 2) -> str:
    return f"{v * 100:+.{p}f}%" if v is not None and pd.notna(v) else "–"




def render_perf(market: str) -> None:
    """성과 화면 한 벌. 한국·미국 탭이 **이 함수 하나**를 각각 호출합니다.

    복사해 두 벌을 두면 앞으로 한쪽만 고치는 사고가 납니다. 화면 요소에
    key를 붙이는 이유도 그것입니다 — 같은 위젯이 두 탭에 생기면 Streamlit
    이 DuplicateWidgetID로 멈춥니다.
    """
    P = PERF_MARKETS[market]
    MK = P["라벨"]
    _m = lambda v: _money(v, market)                              # noqa: E731
    if not (STATE_REPO and STATE_TOKEN):
        st.warning("비공개 상태 레포 설정이 없습니다. Streamlit → Settings → "
                   "Secrets 에 아래 두 줄을 넣어주세요.")
        st.code('STATE_REPO = "아이디/quant-state"\n'
                'STATE_TOKEN = "github_pat_..."   # 그 레포 Contents 읽기 권한',
                language="toml")
        st.caption("평가액·체결가가 들어 있는 파일이라 공개 릴리스에 올리지 "
                   "않습니다. 그래서 이 탭만 비공개 레포를 직접 읽습니다.")
    else:
        perf = fresh_perf()

        LBL = {a: lab.split(" (")[0] for lab, a in ACCOUNTS.items()}
        RS = {a: account_perf(a, market) for a in ACCOUNTS.values()}

        if all(r["이유"] == "기록없음" for r in RS.values()):
            _when = "금요일 저녁에" if market == "kr" else "월초 거래 뒤에"
            st.info(f"아직 {MK} 계좌의 측정할 기록이 없습니다. {_when} "
                    "체결내역과 보유목록 CSV를 올려 **데이터 관리**를 한 번 "
                    "돌리면 여기에 쌓입니다.")
        else:
            for a, r in RS.items():
                if r["이유"] == "주가없음":
                    st.error(f"{LBL[a]}({MK}) — 주가 데이터를 받지 "
                             f"못했습니다. {P['price_hint']}")

            # 종목별 손익·규칙 준수를 미리 계산해 요약 표에 같이 올립니다
            PNL, RC = {}, {}
            if market == "kr":
                sector = latest_rows("kor_sector.parquet",
                                     ("CMP_CD", "SEC_NM_KOR", "기준일"))
                smap = ({} if sector.empty else
                        dict(zip(perf._code6(sector["CMP_CD"]),
                                 sector["SEC_NM_KOR"].astype(str))))
            else:
                # 미국 섹터는 종목표에 같이 들어 있습니다 (Sector 칸).
                sector = latest_rows("global_ticker.parquet",
                                     ("Symbol", "Sector", "date"),
                                     asof_col="date")
                smap = ({} if sector.empty else
                        dict(zip(sector["Symbol"].astype(str).str.strip(),
                                 sector["Sector"].astype(str))))
            LP = {}
            for a, r in RS.items():
                PNL[a] = r.get("pnl", pd.DataFrame())
                RC[a] = perf.rule_check(r.get("pstate", pd.DataFrame()),
                                        r.get("trades", pd.DataFrame()), a,
                                        sector_map=smap)
                # 원장 기준 손익 — 예수금을 몰라도, 보유목록이 늦게 올라와도
                # 흔들리지 않습니다. 팔아서 끝난 종목의 실현손익까지 셉니다.
                LP[a] = perf.ledger_pnl(r.get("trades", pd.DataFrame()), a,
                                        r.get("px", pd.DataFrame()))

            # ── 0. 수익률 배틀 — 스코어보드·레이스·라운드·하이라이트 ──
            render_battle(RS, market)

            # ── 1. 요약 — 한 표에 두 계좌 ─────────────────────
            st.subheader("한눈에")
            items: list[tuple] = []

            def row(name, fn):
                items.append((name, *[fn(a) for a in RS]))

            def _state(a):
                r = RS[a]
                # 여기 없는 이유는 '측정 전'으로 떨어집니다. 입출금누락이
                # 빠져 있어서 신뢰 점검이 숨긴 계좌가 '측정 전'으로만
                # 보였습니다(2026-11-13) — 이유를 늘리면 여기도 같이.
                return ("측정 중" if r["가능"] else
                        {"한점": f"재동기화 1회 — 다음 주부터",
                         "계좌기록없음": "보유 기록 없음",
                         "기록없음": "기록 없음",
                         "주가없음": "주가 없음",
                         "설정없음": "설정 전",
                         "입출금누락": _WHY["입출금누락"]}.get(r["이유"], "측정 전"))

            hidden = {a: r for a, r in RS.items() if r["이유"] == "입출금누락"}
            row("상태", _state)
            if hidden:
                row("숨긴 사유", lambda a: (perf.trust_brief(RS[a].get("신뢰"))
                                        if a in hidden else "–"))
            row("수익률 (시간가중)", lambda a: ("숨김" if a in hidden
                                          else _pct(RS[a].get("수익률"))))
            row("수익률 (평단 기준)",
                lambda a: _pct((RS[a].get("평단") or {}).get("수익률")))
            row(f"{P['지수']} 대비", lambda a: (
                f"{(RS[a]['수익률'] - RS[a]['지수']) * 100:+.2f}%p"
                if RS[a]["가능"] and RS[a].get("지수") is not None else "–"))
            row(P["지수라벨"], lambda a: _pct(RS[a].get("지수")))
            row("측정 기간", lambda a: (f"{RS[a]['일수']}일 "
                                   f"({RS[a]['시작']:%m/%d}~)"
                                   if RS[a]["가능"] else "–"))
            row("보유 종목", lambda a: f"{RC[a].get('보유종목수', 0)}종목")
            row("아직 안 산 종목", lambda a: (f"{RC[a]['미매수']}종목"
                                       if RC[a].get("미매수") else "없음"))
            row("목표(100) 대비", lambda a: f"{RC[a].get('종목수편차', 0):+d}종목")
            row("최대 섹터 비중", lambda a: (
                f"{RC[a]['최대섹터비중'] * 100:.1f}%"
                if RC[a].get("최대섹터비중") else "–"))
            row("합계 손익 (보유분)", lambda a: _m(
                PNL[a]["손익"].sum() if (len(PNL[a])
                                       and PNL[a]["손익"].notna().any())
                else None))
            # ── 원장 기준 ─────────────────────────────────────
            # 예수금을 기록하지 않기로 했으므로(config.TRACK_CASH) 금액은
            # 원장의 매입가·매도가로 셉니다. 비율은 위의 시간가중수익률을
            # 그대로 쓰고, 여기서 새 분모를 지어내지 않습니다.
            row("실현손익 (원장)", lambda a: (_m(LP[a]["실현손익"])
                                        if LP[a]["실현손익"] else "–"))
            row("미실현손익 (원장)", lambda a: (_m(LP[a]["미실현손익"])
                                          if LP[a]["미실현손익"] else "–"))
            row("손익 합계 (원장)", lambda a: (_m(LP[a]["손익"])
                                        if LP[a]["손익"] else "–"))
            row("원가미상 매도", lambda a: (
                f"{LP[a]['원가미상_매도종목']}종목 "
                f"({_m(LP[a]['원가미상_매도금액'])})"
                if LP[a]["원가미상_매도종목"] else "없음"))
            row("이긴 종목", lambda a: (
                f"{int((PNL[a]['손익'] > 0).sum())} / "
                f"{int(PNL[a]['손익'].notna().sum())}"
                if len(PNL[a]) and PNL[a]["손익"].notna().any() else "–"))
            row("원가미상", lambda a: (
                f"{int((PNL[a]['원가출처'] == '원가미상').sum())}종목"
                if len(PNL[a]) else "–"))
            row("원장 미체결", lambda a: (f"{RC[a]['미체결건수']}건"
                                    if RC[a].get("미체결건수") else "없음"))
            row("마지막 재동기화", lambda a: (
                f"{pd.to_datetime(RS[a]['cap'].sort_values('기준일').iloc[-1]['기준일']):%m/%d}"
                if len(RS[a].get("cap", pd.DataFrame())) else "–"))

            summary = pd.DataFrame(
                items, columns=["항목"] + [LBL[a] for a in RS]
            ).set_index("항목")
            st.dataframe(summary, use_container_width=True)

            if hidden:
                st.warning(
                    "**시간가중수익률을 숨긴 계좌가 있습니다** — "
                    + " / ".join(f"{LBL[a]}: {r.get('신뢰사유', '')}"
                                 for a, r in hidden.items())
                    + "\n\n그 날짜 앞뒤의 체결내역(체결가·체결수량·체결일)과 "
                      "보유목록이 서로 맞는지 확인해 inbox에 올리면 복구됩니다.")

            asof = next((r["주가기준일"] for r in RS.values()
                         if r.get("주가기준일") is not None), None)
            st.caption(
                (f"주가 {asof:%Y-%m-%d} 종가 기준 · " if asof is not None else "")
                + "수익률은 입금을 걷어낸 시간가중수익률입니다 — 분할 진입으로 "
                  "돈을 더 넣어도 그게 수익으로 잡히지 않습니다.")

            thin = [LBL[a] for a, r in RS.items()
                    if r["가능"] and r["일수"] < 30]
            if thin:
                st.warning(f"{', '.join(thin)} 계좌는 측정 기간이 한 달 미만입니다. "
                           "이 기간의 수익률은 거의 전부 운입니다 — 전략 판단은 "
                           "아래 **규칙 준수**를 보세요.")

            # (곡선은 맨 위 '수익률 배틀'의 레이스 차트로 옮겼습니다.)

            # ── 3. 백테스트 대비 ──────────────────────────────
            st.subheader("백테스트 대비")
            btj = load_run(P["backtest"])
            bt_cum = pd.Series((btj or {}).get("curve", {}).get("portfolio")
                               or [], dtype="float64")
            gaps = {}
            for a, r in RS.items():
                if r["가능"] and len(bt_cum) > 5:
                    gaps[a] = perf.backtest_percentile(
                        r["수익률"], bt_cum.pct_change().dropna(),
                        max(1, min(len(bt_cum) - 2, len(r["tw"]) - 1)))
            if not any(gaps.values()):
                st.caption("백테스트 수익률 곡선이 아직 없거나 측정 기간이 짧아 "
                           "비교할 수 없습니다. 백테스트 탭에서 한 번 돌려두면 "
                           "다음 주부터 비교합니다.")
            else:
                g = pd.DataFrame([
                    {"항목": "실제 수익률",
                     **{LBL[a]: _pct(gaps.get(a, {}).get("실제수익률"))
                        for a in RS}},
                    {"항목": "백테스트 중앙값",
                     **{LBL[a]: _pct(gaps.get(a, {}).get("백테스트_중앙값"))
                        for a in RS}},
                    {"항목": "정상 범위 (5~95분위)",
                     **{LBL[a]: (f"{gaps[a]['백테스트_5분위'] * 100:+.1f}% ~ "
                                 f"{gaps[a]['백테스트_95분위'] * 100:+.1f}%"
                                 if gaps.get(a) else "–") for a in RS}},
                    {"항목": "분위",
                     **{LBL[a]: (f"{gaps[a]['분위'] * 100:.0f}%"
                                 if gaps.get(a) else "–") for a in RS}},
                    {"항목": "판정",
                     **{LBL[a]: (gaps[a]["판정"] if gaps.get(a) else "–")
                        for a in RS}},
                ]).set_index("항목")
                st.dataframe(g, use_container_width=True)
                n = next((v["표본수"] for v in gaps.values() if v), 0)
                d = next((v["구간일수"] for v in gaps.values() if v), 0)
                st.caption(f"백테스트에서 같은 길이({d}일) 구간 수익률 {n:,}개와 "
                           "비교했습니다. 과거와 현재는 겹치는 기간이 없으니 "
                           "분포 안의 위치로 봅니다. 5~95분위 안이면 정상입니다.")

            # ── 4. 종목별 손익 — 두 계좌를 한 표에 ────────────
            st.subheader("종목별 손익")
            frames = []
            for a in RS:
                if len(PNL[a]):
                    f = PNL[a].copy()
                    f.insert(0, "계좌", LBL[a])
                    frames.append(f)
            if not frames:
                st.caption("보유 기록이 없습니다.")
            else:
                allp = pd.concat(frames, ignore_index=True)
                unknown = int((allp["원가출처"] == "원가미상").sum())
                if unknown:
                    st.warning(
                        f"{unknown}종목은 **원가미상**입니다 — 원장이 생기기 "
                        "전부터 들고 계신 종목이라 매입단가를 모릅니다. "
                        "손익을 추정해 채우지 않고 비워 둡니다. "
                        f"`{'holdings_main.csv' if market == 'kr' else 'holdings_us_main.csv'}`"
                        "에 **매입단가** 칸을 하나 추가해 올리시면 전부 "
                        "계산됩니다.")
                only = st.checkbox("손익이 계산된 종목만", value=bool(unknown),
                                   key=f"pnl_only_{market}")
                view = allp[allp["손익"].notna()] if only else allp
                show = view.sort_values("손익", ascending=False,
                                        na_position="last").copy()
                _dp = 0 if market == "kr" else 2
                for c in ("평단", "현재가", "평가금액", "손익"):
                    show[c] = show[c].map(
                        lambda v: f"{v:,.{_dp}f}" if pd.notna(v) else "–")
                show["수익률"] = show["수익률"].map(
                    lambda v: f"{v * 100:+.1f}%" if pd.notna(v) else "–")
                st.dataframe(show, use_container_width=True, hide_index=True)
                st.download_button("종목별 손익 CSV", to_csv(allp),
                                   file_name=f"pnl_{market}_{tag}.csv",
                                   mime="text/csv", key=f"dl_pnl_{market}")

            # ── 4.5 원장 기준 손익 — 팔아서 끝난 종목까지 ─────
            # 위 표는 **지금 들고 있는** 종목만입니다. 팔고 나간 종목의
            # 실현손익은 거기 안 들어갑니다. 예수금을 기록하지 않기로 했으니
            # (config.TRACK_CASH) 그 부분은 원장의 매입가·매도가로만 셀 수
            # 있고, 이 표가 그걸 보여줍니다.
            st.subheader("원장 기준 손익 (팔린 종목 포함)")
            lframes = []
            for a in RS:
                d = LP[a].get("종목별", pd.DataFrame())
                if len(d):
                    d = d.copy()
                    d.insert(0, "계좌", LBL[a])
                    lframes.append(d)
            if not lframes:
                st.caption("원장에 체결 기록이 아직 없습니다.")
            else:
                alll = pd.concat(lframes, ignore_index=True)
                _unk = sum(LP[a]["원가미상_매도종목"] for a in RS)
                _unka = sum(LP[a]["원가미상_매도금액"] for a in RS)
                if _unk:
                    st.info(
                        f"판 종목 중 {_unk}종목({_m(_unka)})은 **원장이 생기기 "
                        "전부터 들고 계셨던 것**입니다. 매도가는 알지만 "
                        "매입단가가 원장에 없어 실현손익을 계산하지 않고 "
                        "빼 두었습니다 — 추정해서 채우면 전체 손익이 조용히 "
                        "틀립니다. 이후에 모델이 산 종목은 매입가가 원장에 "
                        "있으니 팔 때 정확히 계산됩니다.")
                _lonly = st.checkbox("손익이 계산된 종목만", value=True,
                                     key=f"lpnl_only_{market}")
                lview = alll[alll["손익"].notna()] if _lonly else alll
                lshow = lview[["계좌", "종목코드", "종목명", "상태", "보유수량",
                               "평단", "현재가", "실현손익", "미실현손익",
                               "손익", "매도금액"]].copy()
                _ldp = 0 if market == "kr" else 2
                for c in ("평단", "현재가", "실현손익", "미실현손익", "손익",
                          "매도금액"):
                    lshow[c] = lshow[c].map(
                        lambda v: f"{v:,.{_ldp}f}" if pd.notna(v) else "–")
                lshow["보유수량"] = lshow["보유수량"].map(
                    lambda v: f"{v:,.0f}" if pd.notna(v) else "–")
                st.dataframe(lshow, use_container_width=True, hide_index=True)
                st.download_button("원장 기준 손익 CSV", to_csv(alll),
                                   file_name=f"ledger_pnl_{market}_{tag}.csv",
                                   mime="text/csv",
                                   key=f"dl_lpnl_{market}")
                st.caption("실현손익은 (매도가 − 평단) × 매도수량, 미실현손익은 "
                           "(현재가 − 평단) × 보유수량입니다. 평단은 원장의 "
                           "체결가만으로 계산하므로 예수금 기록이 필요 없고, "
                           "보유목록이 늦게 올라와도 흔들리지 않습니다. "
                           "수수료·거래세는 체결가에 들어 있지 않아 빠집니다.")

            # ── 5. 규칙 준수 ──────────────────────────────────
            st.subheader("규칙 준수")
            st.caption("수익률보다 이게 먼저입니다. 종목 수가 줄거나 한 섹터로 "
                       "쏠리면, 수익률이 좋아도 백테스트가 보장한 성질이 "
                       "사라진 상태입니다.")
            cols = st.columns(len(RS))
            for col, (a, r) in zip(cols, RS.items()):
                with col:
                    st.markdown(f"**{LBL[a]}**")
                    rc = RC[a]
                    if rc.get("등분별종목수"):
                        st.caption(f"등분별 종목 수 {rc['등분별종목수']} · "
                                   f"최대 편차 "
                                   f"{rc.get('등분최대편차', 0) * 100:.0f}%")
                    if not r.get("value", pd.Series(dtype=float)).empty:
                        to = perf.turnover(r.get("trades", pd.DataFrame()),
                                           r["value"], a)
                        if len(to):
                            st.caption("주별 회전율 " + " · ".join(
                                f"{d:%m/%d} {v * 100:.0f}%"
                                for d, v in to.items()))
                    if rc.get("섹터비중"):
                        st.dataframe(
                            pd.Series(rc["섹터비중"]).mul(100).round(1)
                            .rename("비중(%)").to_frame(),
                            use_container_width=True, height=260)



with tab_all:
    render_all()

with tab_perf_kr:
    render_perf("kr")

with tab_perf_us:
    render_perf("us")


# ── 백테스트 ─────────────────────────────────────────────────
with tab_bt:
    bt = load_run(f"backtest_{M['key']}_latest.json")

    if not bt:
        st.info("아직 백테스트 결과가 없습니다. 아래에서 한 번 돌려주세요.")
    else:
        s = bt.get("summary", {})
        p = s.get("포트폴리오", {})
        b = s.get("벤치마크(전종목 동일가중)", {})

        st.caption(f"{s.get('기간','-')} · 리밸런싱 {s.get('리밸런싱횟수','-')}회 "
                   f"· 보유 {s.get('보유종목수','-')}종목 "
                   f"· 턴오버 {s.get('평균턴오버','-')}%")

        c1, c2, c3 = st.columns(3)
        c1.metric("누적수익률", f"{p.get('누적수익률','-')}%",
                  delta=f"{s.get('초과수익률','-')}%p vs 벤치마크")
        c2.metric("최대낙폭", f"{p.get('최대낙폭','-')}%")
        c3.metric("샤프", p.get("샤프", "-"))

        curve = bt.get("curve") or {}
        if curve.get("dates"):
            port_c, bench_c = series_colors()
            long = pd.concat([
                pd.DataFrame({"날짜": pd.to_datetime(curve["dates"]),
                              "구분": "포트폴리오", "누적": curve["portfolio"]}),
                pd.DataFrame({"날짜": pd.to_datetime(curve["dates"]),
                              "구분": "벤치마크", "누적": curve["benchmark"]}),
            ])
            long["수익률"] = (long["누적"] - 1) * 100

            import altair as alt
            hover = alt.selection_point(fields=["날짜"], nearest=True,
                                        on="mouseover", empty=False)
            base = alt.Chart(long).encode(
                x=alt.X("날짜:T", title=None,
                        axis=alt.Axis(grid=False, tickCount=5)),
                y=alt.Y("수익률:Q", title="누적 수익률 (%)",
                        axis=alt.Axis(grid=True, gridOpacity=0.18)),
                color=alt.Color("구분:N", title=None,
                                scale=alt.Scale(domain=["포트폴리오", "벤치마크"],
                                                range=[port_c, bench_c]),
                                legend=None),
            )
            chart = (base.mark_line(strokeWidth=2)
                     + base.mark_point(size=60, filled=True)
                     .encode(opacity=alt.condition(hover, alt.value(1), alt.value(0)),
                             tooltip=["날짜:T", "구분:N",
                                      alt.Tooltip("수익률:Q", format=".2f", title="누적 %")])
                     .add_params(hover))
            st.altair_chart(chart.properties(height=280), use_container_width=True)

        rows = []
        for k in ("누적수익률", "연환산수익률", "연변동성", "최대낙폭", "승률", "샤프"):
            rows.append({"지표": k, "포트폴리오": p.get(k), "벤치마크": b.get(k)})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        if s.get("제약"):
            st.caption("제약: " + " · ".join(f"{k} {v}" for k, v in s["제약"].items()))

        with st.expander("⚠️ 이 숫자를 믿기 전에 — 한계", expanded=False):
            for lim in s.get("한계", []):
                st.write(f"· {lim}")
            st.caption(f"거래비용 {s.get('거래비용가정','-')} · "
                       f"공시지연 {s.get('공시지연가정','-')} 가정")

        assets, _ = release_assets()
        bx = sorted([n for n in assets
                     if n.endswith(".xlsx") and "백테스트" in n and M["xlsx_key"] in n])
        if bx:
            blob = download_asset(bx[-1])
            if blob:
                st.download_button(f"⬇️ {bx[-1]}", blob, file_name=bx[-1],
                                   mime="application/vnd.openxmlformats-officedocument."
                                        "spreadsheetml.sheet", key="dl_bt")

    st.divider()
    st.markdown("##### 백테스트 돌리기")
    bc1, bc2 = st.columns(2)
    bt_top_n = bc1.number_input("보유 종목 수", 5, 300, 100, step=5,
                                help="한국장 백테스트에서는 100종목이 30종목보다 "
                                     "수익·샤프·낙폭·비용 전부 나았습니다.")
    REB = {"B": "일간", "W-FRI": "주간(금)", "ME": "월말", "QE": "분기말"}
    bt_reb = bc2.selectbox("리밸런싱", list(REB), index=2,
                           format_func=lambda v: REB[v],
                           help="주기를 줄이려면 아래 '분할 등분 수'를 같이 올리세요. "
                                "그냥 줄이면 매번 전체가 바뀌어 비용만 늡니다.")
    bt_cost = st.slider("편도 거래비용 (bp)", 0, 100, 25, step=5,
                        help="한국 주식은 수수료+세금 합쳐 편도 25bp 안팎입니다.")
    bt_tr = st.select_slider(
        "분할 등분 수", [1, 2, 3, 4, 6, 12], value=1,
        help="1이면 매 회차 전체 교체(예전 동작). 4면 자금을 4등분해 매 회차 "
             "한 등분만 점검합니다. 주간+4등분이면 거래는 매주 작게 하면서 "
             "종목별 점검 간격은 4주로 유지됩니다.")
    if bt_tr > 1:
        st.caption(f"매 회차 약 {int(bt_top_n) // bt_tr}종목만 점검 · "
                   f"종목별 점검 간격 {bt_tr}회차")

    with st.expander("포트폴리오 제약 (끄면 예전 동작)"):
        cap_pct = st.slider("한 섹터 최대 비중 (%)", 10, 100, 25, step=5,
                            help="100%면 제한 없음. 섹터 중립 z점수를 써도 최종 "
                                 "순위는 전 종목 통합이라 특정 섹터가 쏠립니다.")
        default_turn = 500_000_000 if M["key"] == "kr" else 5_000_000
        use_liq = st.checkbox("유동성 필터 사용", value=True,
                              help="20일 평균 거래대금이 기준 미만인 종목 제외. "
                                   "실제로 원하는 수량을 살 수 있는 종목만 남깁니다.")
        turn = st.number_input(
            "거래대금 하한 (한국=원, 미국=달러)", 0, 100_000_000_000,
            default_turn, step=100_000_000 if M["key"] == "kr" else 1_000_000,
            disabled=not use_liq)
        st.caption("둘 다 끄면(상한 100% · 필터 해제) 제약 이전과 같은 조건이라 "
                   "전후 비교를 할 수 있습니다.")

    bt_active = [r for r in recent_runs(10)
                 if r["status"] in ("queued", "in_progress", "waiting")
                 and r.get("path", "").endswith("backtest.yml")]
    if bt_active:
        st.info("⏳ 백테스트 실행 중")

    if st.button("🧪 백테스트 실행", key="run_bt", disabled=bool(bt_active)):
        ok, msg = dispatch("backtest.yml", {
            "market": M["key"], "top_n": str(int(bt_top_n)),
            "rebalance": bt_reb, "cost_bps": str(int(bt_cost)),
            "max_sector_pct": str(round(cap_pct / 100, 2)),
            "min_turnover": str(int(turn) if use_liq else 0),
            "tranches": str(int(bt_tr))})
        (st.success if ok else st.error)(msg)
        if ok:
            time.sleep(3)
            st.cache_data.clear()
            st.rerun()


# ── 기록 ─────────────────────────────────────────────────────
with tab_log:
    if run_info:
        st.subheader("최근 실행 리포트")
        st.text(run_info.get("report", "")[:6000])

        with st.expander("단계별 상세"):
            for s in run_info.get("steps", []):
                mark = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skipped": "⏭️"}.get(
                    s.get("status"), "•")
                st.write(f"{mark} **{s.get('name')}** — {s.get('duration_sec')}초 "
                         f"({s.get('attempts')}회 시도)")
                for w in s.get("warnings", []):
                    st.caption(f"⚠ {w}")
                if s.get("error"):
                    st.code(s["error"][-500:], language="text")

        with st.expander("📥 재무제표(DART) 수집 현황", expanded=True):
            ds = load_run("dart_status.json")
            if not ds:
                st.caption("아직 요약이 없습니다. DART 수집이 한 번 돌면 생깁니다.")
            else:
                fs = ds.get("재무제표", {})
                if ds.get("끝남"):
                    st.success(f"**수집 완료** — {ds['완료']:,}건 전부 받았습니다.")
                else:
                    left = ds.get("남은작업", 0)
                    st.info(f"**수집 중** — {left:,}건 남음 "
                            f"(하루 약 4,000건씩, 약 {max(1, round(left / 4000))}일 더)")
                c1, c2, c3 = st.columns(3)
                c1.metric("재무제표 시작", fs.get("처음", "-"))
                c2.metric("최신", fs.get("마지막", "-"))
                c3.metric("행 수", f"{fs.get('행수', 0):,}")
                st.caption(f"마지막 실행 {ds.get('실행시각','-')} · "
                           f"{ds.get('중단사유','-')} · "
                           f"이번에 {ds.get('이번저장행수',0):,}행 저장")

                yr = pd.DataFrame(ds.get("연도별", []))
                if len(yr):
                    st.bar_chart(yr.set_index("연도")["종목수"], height=180)
                    st.caption("연도마다 종목 수가 비슷하면 그 구간까지 백테스트가 가능합니다. "
                               "앞쪽 연도만 뚝 낮으면 아직 덜 채워진 것입니다.")

        with st.expander("데이터 저장소 현황"):
            store_info = run_info.get("store", {})
            if store_info:
                st.dataframe(pd.DataFrame(store_info).T.rename(
                    columns={"rows": "행 수", "mb": "용량(MB)", "latest": "최신일"}),
                    use_container_width=True)

    st.subheader("실행 이력 (전체 시장)")
    rows = []
    for r in recent_runs(15):
        rows.append({
            "워크플로": r["name"],
            "결과": {"success": "성공", "failure": "실패",
                   "cancelled": "취소"}.get(r.get("conclusion"), "진행중"),
            "시작": kst(r["created_at"]),
            "링크": r["html_url"],
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     column_config={"링크": st.column_config.LinkColumn(
                         "열기", display_text="보기")})

    if st.button("🔄 전체 새로고침", key="refresh_all"):
        st.cache_data.clear()
        st.rerun()
