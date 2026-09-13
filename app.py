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


# ── 헤더 + 시장 선택 ─────────────────────────────────────────
st.title("📈 퀀트 에이전트")

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
tab_top, tab_run, tab_model, tab_log = st.tabs(
    [top_label, "▶️ 실행", "📊 모델", "📜 기록"])

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
