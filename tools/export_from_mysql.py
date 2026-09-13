# -*- coding: utf-8 -*-
"""[선택] PC의 MySQL(stock_db) 데이터를 parquet으로 내보내고 GitHub에 올립니다.

해외 나가기 전에 한 번 돌려두면, 클라우드에서 5년치 주가·FnGuide 재무제표를
처음부터 다시 긁지 않아도 됩니다(월간 실행 2~4시간 절약).

실행: tools/export_from_mysql.bat  더블클릭
"""
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

TABLES = ["kor_ticker", "kor_sector", "kor_price", "kor_fs", "kor_value"]
KEEP_YEARS = int(os.getenv("QUANT_PRICE_KEEP_YEARS", "2"))
OUT = ROOT / "data"


def dump() -> list[Path]:
    import pymysql
    from sqlalchemy import create_engine

    user = os.getenv("QUANT_DB_USER", "root")
    host = os.getenv("QUANT_DB_HOST", "127.0.0.1")
    port = os.getenv("QUANT_DB_PORT", "3306")
    name = os.getenv("QUANT_DB_NAME", "stock_db")
    pw = os.getenv("QUANT_DB_PASS") or getpass.getpass("MySQL 비밀번호 (기본 1234): ") or "1234"

    url = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{name}"
    engine = create_engine(url)
    OUT.mkdir(parents=True, exist_ok=True)

    made = []
    for table in TABLES:
        try:
            df = pd.read_sql(f"select * from {table}", con=engine)
        except Exception as e:
            print(f"  [건너뜀] {table}: {e}")
            continue

        if df.empty:
            print(f"  [비어있음] {table}")
            continue

        # 날짜 타입 정리
        for col in ("기준일", "날짜"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()

        for col in ("종목코드", "CMP_CD"):
            if col in df.columns:
                df[col] = df[col].astype(str).str.zfill(6)

        # 주가는 최근 N년만 (저장소 크기 절약)
        if table == "kor_price" and "날짜" in df.columns and len(df):
            cutoff = df["날짜"].max() - pd.DateOffset(years=KEEP_YEARS)
            before = len(df)
            df = df[df["날짜"] >= cutoff]
            print(f"  {table}: 최근 {KEEP_YEARS}년만 보관 ({before:,} → {len(df):,}행)")

        path = OUT / f"{table}.parquet"
        df.to_parquet(path, index=False, compression="zstd")
        mb = path.stat().st_size / 1024 / 1024
        print(f"  [완료] {table}: {len(df):,}행 → {path.name} ({mb:.1f}MB)")
        made.append(path)

    engine.dispose()
    return made


def upload(paths: list[Path]):
    """GitHub 릴리스(data-store)에 올립니다."""
    import requests as rq

    repo = os.getenv("GH_REPO") or input("GitHub 레포 (예: hong/quant-agent): ").strip()
    token = os.getenv("GH_TOKEN") or getpass.getpass("GitHub 토큰 (github_pat_...): ").strip()
    if not (repo and token):
        print("레포/토큰이 없어 업로드를 건너뜁니다. data 폴더의 parquet을 "
              "GitHub 릴리스 'data-store'에 직접 올려도 됩니다.")
        return

    api = "https://api.github.com"
    h = {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}

    r = rq.get(f"{api}/repos/{repo}/releases/tags/data-store", headers=h, timeout=30)
    if r.status_code == 404:
        r = rq.post(f"{api}/repos/{repo}/releases", headers=h, timeout=30, json={
            "tag_name": "data-store", "name": "데이터 저장소",
            "body": "파이프라인이 관리하는 parquet 데이터입니다."})
    if not r.ok:
        print(f"  [실패] 릴리스를 준비하지 못했습니다: {r.status_code} {r.text[:200]}")
        return

    rel = r.json()
    existing = {a["name"]: a["id"] for a in rel.get("assets", [])}
    upload_url = rel["upload_url"].split("{")[0]

    for p in paths:
        if p.name in existing:   # 같은 이름이 있으면 지우고 새로 올림
            rq.delete(f"{api}/repos/{repo}/releases/assets/{existing[p.name]}",
                      headers=h, timeout=30)
        print(f"  업로드 중: {p.name} ({p.stat().st_size / 1024 / 1024:.1f}MB) ...")
        up = rq.post(f"{upload_url}?name={p.name}",
                     headers={**h, "Content-Type": "application/octet-stream"},
                     data=p.read_bytes(), timeout=600)
        print("  [완료]" if up.ok else f"  [실패] {up.status_code} {up.text[:200]}")


if __name__ == "__main__":
    print("=" * 56)
    print("  MySQL → parquet 내보내기")
    print("=" * 56)
    made = dump()
    if not made:
        print("\n내보낼 데이터가 없습니다. MySQL이 켜져 있는지 확인하세요.")
        sys.exit(1)

    total = sum(p.stat().st_size for p in made) / 1024 / 1024
    print(f"\n총 {len(made)}개 파일 / {total:.1f}MB → {OUT}")

    ans = input("\nGitHub 릴리스에 지금 올릴까요? (y/n): ").strip().lower()
    if ans == "y":
        upload(made)
    else:
        print(f"\n나중에 올리려면 data 폴더의 parquet 파일들을 "
              f"GitHub 릴리스 'data-store'에 올리면 됩니다.")
    print("\n끝났습니다.")
