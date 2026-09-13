# -*- coding: utf-8 -*-
"""일간 실행: 티커 → 섹터 → 주가(최근) → 밸류 → QVM 모델 → 리포트."""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.run("daily"))
