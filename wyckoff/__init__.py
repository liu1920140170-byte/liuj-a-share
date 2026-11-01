# -*- coding: utf-8 -*-
"""威科夫复盘工具（A股）。

模块划分：
    http_client  取数底层（https→http 自动降级、多编码、重试）
    datasource   行情数据源（东方财富主源，腾讯/新浪备源）
    analysis     指标与结构（均线、ATR、相对量、交易区间、趋势结构、支撑阻力）
    events       威科夫事件与 VSA 量价信号识别
    decision     阶段评分与买卖决策（入场/止损/目标/仓位/风险收益比）
    report       中文文本报告
    api          组装完整分析结果（CLI 与 Web 共用）
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
