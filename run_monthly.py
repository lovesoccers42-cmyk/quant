# -*- coding: utf-8 -*-
"""월간 실행: 티커 → 섹터 → 주가(2년) → 재무제표(FnGuide) → 밸류 → QVM 모델."""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.run("monthly"))
