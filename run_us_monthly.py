# -*- coding: utf-8 -*-
"""미국 월간 실행: 종목 → 주가(2년) → 재무제표(yahooquery) → 밸류 → QVM 모델."""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.run("monthly", "us"))
