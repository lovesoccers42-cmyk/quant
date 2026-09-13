# -*- coding: utf-8 -*-
"""미국 일간 실행: 종목 → 주가(최근) → 밸류 → QVM 모델 → 리포트."""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.run("daily", "us"))
