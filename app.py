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
    import perf
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


ACCOUNTS = {"상수 (main)": "main", "배우자 (alt)": "alt"}


def _suffix(acct: str) -> str:
    return "" if acct == "main" else f"_{acct}"


def account_perf(acct: str) -> dict:
    """한 계좌의 성과 한 묶음.

    맨 위 요약과 성과 탭이 **같은 함수**를 씁니다 — 두 곳에서 따로 계산하면
    같은 화면에 다른 수익률이 뜨고, 어느 쪽이 맞는지 알 수 없게 됩니다.
    무거운 읽기는 모두 캐시되어 있어 여러 번 불러도 비용은 계산뿐입니다.
    """
    import perf
    out: dict = {"계좌": acct, "가능": False, "이유": "설정없음"}
    if not (STATE_REPO and STATE_TOKEN):
        return out

    sfx = _suffix(acct)
    trades = state_parquet("kor_trades.parquet")
    hlog = state_parquet("kor_holdings_log.parquet")
    out.update({"trades": trades, "hlog": hlog,
                "pstate": state_parquet(f"kor_portfolio{sfx}.parquet"),
                "cap": state_parquet(f"kor_capital{sfx}.parquet")})
    if hlog.empty and trades.empty:
        out["이유"] = "기록없음"
        return out

    codes = set()
    if not hlog.empty:
        codes |= set(hlog["종목코드"].astype(str).str.zfill(6))
    if not trades.empty:
        codes |= set(trades["종목코드"].astype(str).str.zfill(6))

    # 측정 시작보다 10일 앞에서부터만 읽습니다 — 주가를 통째로 읽으면
    # 327MB가 올라와 앱이 죽습니다(실측).
    first = pd.Timestamp("2000-01-01")
    if not hlog.empty:
        first = pd.to_datetime(hlog["기준일"]).min()
    elif not trades.empty:
        first = pd.to_datetime(trades["기준일"]).min()
    price = parquet_slice("kor_price.parquet", ("날짜", "종목코드", "종가"),
                          (("날짜", ">=", first - pd.Timedelta(days=10)),))
    if price.empty:
        out["이유"] = "주가없음"
        return out
    out["주가기준일"] = pd.to_datetime(price["날짜"]).max()

    px = perf.price_pivot(price, codes)
    pos = perf.positions_timeline(hlog, acct, px)
    value = perf.daily_value(pos, px)
    tw = perf.twr(value, perf.net_flows(trades, acct))
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

    out["가능"], out["이유"] = True, ""
    out["수익률"] = float(tw["누적"].iloc[-1] - 1)
    out["시작"], out["끝"] = tw.index[0], tw.index[-1]
    out["일수"] = int((tw.index[-1] - tw.index[0]).days)
    b = bench_curve(f"{tw.index[0]:%Y-%m-%d}", f"{tw.index[-1]:%Y-%m-%d}")
    out["bench"] = b
    out["지수"] = float(b["누적"].iloc[-1] - 1) if not b.empty else None
    return out


_WHY = {"설정없음": "설정 전", "기록없음": "기록 전",
        "계좌기록없음": "측정 전", "한점": "다음 주부터",
        "주가없음": "주가 없음"}


def perf_strip() -> None:
    """화면 맨 위 — 두 계좌 수익률. 제일 먼저 보고 싶은 숫자입니다."""
    if not (STATE_REPO and STATE_TOKEN):
        return
    rows = [(lab, account_perf(a)) for lab, a in ACCOUNTS.items()]
    if all(r["이유"] == "기록없음" for _, r in rows):
        return

    cols = st.columns(len(rows))
    asof = None
    for col, (lab, r) in zip(cols, rows):
        name = lab.split(" (")[0]
        cb = r.get("평단") or {}
        if r["가능"]:
            delta = (f"{(r['수익률'] - r['지수']) * 100:+.2f}%p vs 지수"
                     if r.get("지수") is not None else f"{r['일수']}일")
            col.metric(name, f"{r['수익률'] * 100:+.2f}%", delta=delta)
        elif cb:
            # 시간가중수익률은 아직 못 내지만 평단 기준은 낼 수 있습니다.
            # 어느 측정인지 반드시 밝혀 적습니다 — 섞으면 안 됩니다.
            col.metric(name, f"{cb['수익률'] * 100:+.2f}%",
                       delta="평단 기준", delta_color="off")
        else:
            col.metric(name, "–", delta=_WHY.get(r["이유"], "측정 전"),
                       delta_color="off")
        asof = r.get("주가기준일") or asof

    done = [r for _, r in rows if r["가능"]]
    bits = []
    if asof is not None:
        bits.append(f"**주가 {asof:%m/%d} 종가** 기준")
    if done:
        bits.append(f"측정 {min(r['시작'] for r in done):%m/%d} 시작")
        bits.append("입금을 걷어낸 시간가중수익률")
    elif any(r.get("평단") for _, r in rows):
        bits.append("평단 기준 (시간가중수익률은 거래일 2개부터)")
    bits.append("자세한 내용은 아래 **성과** 탭")
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
tab_perf, tab_top, tab_run, tab_model, tab_bt, tab_log = st.tabs(
    ["💰 성과", top_label, "▶️ 실행", "📊 모델", "🧪 백테스트", "📜 기록"])

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
# 선 색 — 검증된 3색(이 레포의 series_colors와 같은 팔레트의 1·2·3번 슬롯):
#   상수 파랑 / 배우자 청록 / 지수 주황.
# validate_palette.js 로 밝은·어두운 테마 모두 전 조합 통과(최악 CVD ΔE 9.2
# 밝은 / 9.4 어두운, 일반시야 24.0 / 20.9). 밝은 테마에서 청록의 명암비가
# 2.74:1로 3:1 아래라 **범례와 숫자 표를 반드시 함께** 둡니다(색만으로
# 구분되지 않게) — 아래 요약 표가 그 역할을 합니다.
def series_trio() -> dict:
    try:
        dark = st.get_option("theme.base") == "dark"
    except Exception:
        dark = False
    return ({"상수": "#3987e5", "배우자": "#199e70", "지수": "#d95926"} if dark
            else {"상수": "#2a78d6", "배우자": "#1baf7a", "지수": "#eb6834"})


def _won(v) -> str:
    return f"{v:,.0f}원" if v is not None and pd.notna(v) else "–"


def _pct(v, p: int = 2) -> str:
    return f"{v * 100:+.{p}f}%" if v is not None and pd.notna(v) else "–"


with tab_perf:
    if not (STATE_REPO and STATE_TOKEN):
        st.warning("비공개 상태 레포 설정이 없습니다. Streamlit → Settings → "
                   "Secrets 에 아래 두 줄을 넣어주세요.")
        st.code('STATE_REPO = "아이디/quant-state"\n'
                'STATE_TOKEN = "github_pat_..."   # 그 레포 Contents 읽기 권한',
                language="toml")
        st.caption("평가액·체결가가 들어 있는 파일이라 공개 릴리스에 올리지 "
                   "않습니다. 그래서 이 탭만 비공개 레포를 직접 읽습니다.")
    else:
        import perf

        LBL = {a: lab.split(" (")[0] for lab, a in ACCOUNTS.items()}
        RS = {a: account_perf(a) for a in ACCOUNTS.values()}

        if all(r["이유"] == "기록없음" for r in RS.values()):
            st.info("아직 측정할 기록이 없습니다. 금요일 저녁에 체결내역과 "
                    "보유목록 CSV를 올려 **데이터 관리**를 한 번 돌리면 "
                    "여기에 쌓입니다.")
        else:
            for a, r in RS.items():
                if r["이유"] == "주가없음":
                    st.error(f"{LBL[a]} — 주가 데이터를 받지 못했습니다. "
                             "공개 릴리스의 kor_price.parquet을 확인하세요.")

            # 종목별 손익·규칙 준수를 미리 계산해 요약 표에 같이 올립니다
            PNL, RC = {}, {}
            sector = latest_rows("kor_sector.parquet",
                                 ("CMP_CD", "SEC_NM_KOR", "기준일"))
            smap = ({} if sector.empty else
                    dict(zip(sector["CMP_CD"].astype(str).str.zfill(6),
                             sector["SEC_NM_KOR"].astype(str))))
            for a, r in RS.items():
                PNL[a] = r.get("pnl", pd.DataFrame())
                RC[a] = perf.rule_check(r.get("pstate", pd.DataFrame()),
                                        r.get("trades", pd.DataFrame()), a,
                                        sector_map=smap)

            # ── 1. 요약 — 한 표에 두 계좌 ─────────────────────
            st.subheader("한눈에")
            items: list[tuple] = []

            def row(name, fn):
                items.append((name, *[fn(a) for a in RS]))

            def _state(a):
                r = RS[a]
                return ("측정 중" if r["가능"] else
                        {"한점": f"재동기화 1회 — 다음 주부터",
                         "계좌기록없음": "보유 기록 없음",
                         "기록없음": "기록 없음",
                         "주가없음": "주가 없음"}.get(r["이유"], "측정 전"))

            row("상태", _state)
            row("수익률 (시간가중)", lambda a: _pct(RS[a].get("수익률")))
            row("수익률 (평단 기준)",
                lambda a: _pct((RS[a].get("평단") or {}).get("수익률")))
            row("지수 대비", lambda a: (
                f"{(RS[a]['수익률'] - RS[a]['지수']) * 100:+.2f}%p"
                if RS[a]["가능"] and RS[a].get("지수") is not None else "–"))
            row("지수 (시총상위200)", lambda a: _pct(RS[a].get("지수")))
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
            row("합계 손익", lambda a: _won(
                PNL[a]["손익"].sum() if (len(PNL[a])
                                       and PNL[a]["손익"].notna().any())
                else None))
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

            # ── 2. 곡선 — 두 계좌 + 지수를 한 그래프에 ────────
            parts = []
            for a, r in RS.items():
                if r["가능"]:
                    tw = r["tw"]
                    parts.append(pd.DataFrame({
                        "날짜": tw.index, "구분": LBL[a],
                        "수익률": (tw["누적"] - 1) * 100}))
            bench = next((r["bench"] for r in RS.values()
                          if r["가능"] and not r.get("bench",
                                                    pd.DataFrame()).empty), None)
            if bench is not None:
                parts.append(pd.DataFrame({
                    "날짜": bench.index, "구분": "지수",
                    "수익률": (bench["누적"] - 1) * 100}))

            if parts and max(len(p) for p in parts) >= 2:
                import altair as alt
                trio = series_trio()
                long = pd.concat(parts)
                order = [c for c in ("상수", "배우자", "지수")
                         if c in set(long["구분"])]
                ch = (alt.Chart(long)
                      .mark_line(strokeWidth=2)
                      .encode(x=alt.X("날짜:T", title=None,
                                      axis=alt.Axis(grid=False, tickCount=5)),
                              y=alt.Y("수익률:Q", title="누적 수익률 (%)"),
                              color=alt.Color("구분:N", title=None,
                                              sort=order,
                                              scale=alt.Scale(
                                                  domain=order,
                                                  range=[trio[c] for c in order])),
                              tooltip=["날짜:T", "구분:N",
                                       alt.Tooltip("수익률:Q", format=".2f")])
                      .properties(height=260))
                st.altair_chart(ch, use_container_width=True)
                st.download_button(
                    "곡선 데이터 CSV", to_csv(long, excel_safe=False),
                    file_name=f"curve_{tag}.csv", mime="text/csv")
            else:
                st.info("수익률은 두 시점 사이의 변화이니 재동기화가 두 번은 "
                        "있어야 곡선이 그려집니다. 다음 주 갱신부터 나옵니다.")

            # ── 3. 백테스트 대비 ──────────────────────────────
            st.subheader("백테스트 대비")
            btj = load_run("backtest_kr_latest.json")
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
                        "`holdings_main.csv`에 **매입단가** 칸을 하나 추가해 "
                        "올리시면 전부 계산됩니다.")
                only = st.checkbox("손익이 계산된 종목만", value=bool(unknown))
                view = allp[allp["손익"].notna()] if only else allp
                show = view.sort_values("손익", ascending=False,
                                        na_position="last").copy()
                for c in ("평단", "현재가", "평가금액", "손익"):
                    show[c] = show[c].map(
                        lambda v: f"{v:,.0f}" if pd.notna(v) else "–")
                show["수익률"] = show["수익률"].map(
                    lambda v: f"{v * 100:+.1f}%" if pd.notna(v) else "–")
                st.dataframe(show, use_container_width=True, hide_index=True)
                st.download_button("종목별 손익 CSV", to_csv(allp),
                                   file_name=f"pnl_{tag}.csv", mime="text/csv")

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
                                legend=alt.Legend(orient="top", direction="horizontal")),
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
