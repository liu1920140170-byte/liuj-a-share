# -*- coding: utf-8 -*-
"""A股个股基本面尽调分析（严格参照九鼎投资尽调框架 13 章，适配上市公司公开数据）。

数据来源：东方财富 datacenter F10 接口 + 行情快照；各数据源独立降级，
任一接口失败不影响其他章节，失败项在 errors 中如实标注。

章节严格对应原框架 13 章：
    摘要           业务简介 + 财务简况 + 投资方案 + 退出计划 + 投资价值
    1 主营业务     1.1业务简介 / 1.2企业简史 / 1.3业务定位 / 1.4采购分析 /
                   1.5生产分析 / 1.6销售分析 / 1.7技术研发 / 1.8小结
    2 行业分析     2.1行业界定 / 2.2行业政策 / 2.3行业概况 / 2.4行业周期 /
                   2.5市场规模及驱动力 / 2.6产业链 / 2.7小结
    3 竞争分析     3.1竞争要素 / 3.2竞争态势 / 3.3公司地位 / 3.4小结
    4 团队治理     4.1组织结构 / 4.2股权分布 / 4.3实际控制人 / 4.4高管团队 /
                   4.5员工结构 / 4.6人员管理 / 4.7小结
    5 财务情况     5.1合并范围 / 5.2审计 / 5.3环境·竞争·战略与财务特征 /
                   5.4财务及核算 / 5.5财务与业务融合 / 5.6主要报表项目 /
                   5.7同行比较 / 5.8小结
    6 历史沿革     6.1股权结构 / 6.2历史沿革 / 6.3关联方 / 6.4小结
    7 成长路径     7.1短期发展规划 / 7.2长期发展路径 / 7.3小结
    8 盈利预测与估值 8.1盈利预测 / 8.2估值 / 8.3未来展望
    9 上市安排     9.1私募计划 / 9.2上市安排 / 9.3上市可行性 / 9.4增值服务
                   （已上市公司改为「上市概况与再融资」）
   10 投资方案     10.1交易结构 / 10.2投资收益测算
                   （已上市公司改为「估值与交易建议」）
   11 投资风险     风险性质 / 对业务与上市的影响 / 量化 / 概率 / 应对措施
   12 投资建议     12.1投资价值 / 12.2投资建议 / 12.3投资结论
   13 附件         数据来源声明

每节统一结构：{guide:框架分析要点, data:可获取数据, inference:基于数据的规则化研判,
               note:数据缺失说明}；status 标注 auto/semi/manual。
"""
from __future__ import annotations

import datetime as _dt
import time
import urllib.parse
from statistics import median

from .datasource import (_f, _resolve_bj_new_code, _secu_code, get_quote,
                         resolve_input)
from .http_client import fetch_json

_DC = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
       "reportName={report}&columns=ALL&filter={filt}&pageSize={ps}&pageNumber={pn}{extra}")
_REF = "https://emweb.securities.eastmoney.com/"

_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600.0  # 尽调数据 10 分钟缓存

# 框架分析要点（摘自原 PDF 各节【要求】）
GUIDES = {
    "1.1": "概括企业名称、所在地、注册资本或净资产、实际控制人、主要产品或服务、盈利模式及细分行业地位。",
    "1.2": "简要介绍公司成立至今的发展历程、关键发展阶段、业务演变过程、资产演变过程等。",
    "1.3": "说明公司业务价值链（或商业模式），简要介绍业务结构（产品结构）、主要产品/服务经营业绩、业务流程等。",
    "1.4": "采购部门基本情况、采购模式、采购账期、结算方式、主要原材料及其供应情况、采购价格、供应商采购情况表（披露金额至少占采购总额50%以上）。",
    "1.5": "生产部门基本情况、生产模式、生产工艺流程图、产能/产量情况、固定资产投资额变化、土地厂房设备变化、质量控制、环保、安全生产等。",
    "1.6": "销售部门基本情况、销售模式、结算方式、收入结构、销售量、销售价格、客户销售情况表（披露金额至少占销售总额50%以上）、销售政策、定价策略等。",
    "1.7": "技术来源及资质、研发团队、研发投入、主要知识产权等。",
    "1.8": "总结评价公司业务经营情况，重点评价商业模式和盈利能力的持续性，判断产品/服务/商业模式是否有核心竞争力。",
    "2.1": "根据证监会/国家统计局行业分类界定目标公司所处行业，简要描述行业基本特点；多业务分属不同行业的应分别列明。",
    "2.2": "简要描述对目标公司所处行业的监管体制、政策要点（建议表格列示），重点点评影响较大的政策。",
    "2.3": "行业发展概况（国际、国内发展历史和现状）；影响行业发展的有利和不利因素（产业政策、技术替代、行业瓶颈、国际市场冲击等）。",
    "2.4": "行业生命周期分析：周期性特征，判断生命周期（幼稚期、成长期、成熟期或衰退期）。",
    "2.5": "市场容量及行业增长率、市场结构与集中度、市场供求情况、行业利润水平、驱动因素及发展趋势，尽量分析长周期历史数据。",
    "2.6": "所处行业与上、下游行业之间的关联性，主要供应商及议价能力、主要客户及议价能力、潜在进入者、替代品等。",
    "2.7": "市场规模、行业增长率、进入壁垒、对上下游议价能力、替代品威胁、未来趋势，从产业结构角度给出项目盈利能力的判断。",
    "3.1": "根据行业分析结论，归纳总结进入行业的主要障碍、竞争关键资源或要素。",
    "3.2": "行业梯队分层、主要竞争对手分析、与主要竞争对手相比的优劣势、资本回报率等指标比较。",
    "3.3": "公司所属地位、市场占有率及其演变趋势、主要竞争对手的市场占有率及演变趋势。",
    "3.4": "归纳总结拟投企业的核心竞争优势（有别于主要竞争对手的杀手锏），从竞争角度给出项目盈利能力的判断。",
    "4.1": "图示最新组织结构图，表格列示董事会/监事会成员名单，简要分析决策治理机制、组织运行情况。",
    "4.2": "最新的股权结构图、实际控制人持股、主要股东、管理团队持股等。",
    "4.3": "实际控制人综合评价：事业心、才能、执行力、道德品性。",
    "4.4": "高管团队名单（职务、工作经历、专长），对专业知识、学习能力、管理能力等评价。",
    "4.5": "人员部门分布、年龄结构、学历结构、收入等。",
    "4.6": "激励约束机制，管理团队薪资、奖金及福利，社保缴纳情况等。",
    "4.7": "总结治理机制、管理团队知识结构、管理水平、公司文化、团队精神、稳定性及创新精神、员工素质。",
    "5.1": "尽职调查主体及年度；重点审核程序及结果。",
    "5.2": "重大审计问题、与审计师沟通情况。",
    "5.3": "环境、行业竞争关系与战略同财务数据特征的对应分析。",
    "5.4": "纳税申报表主要项目、调整后财务报表、重要调整事项及依据。",
    "5.5": "销售及收款、采购及付款、生产、固定资产投资等财务与业务融合。",
    "5.6": "主要报表项目，从业务角度解读形成原因。",
    "5.7": "盈利能力、偿债能力、资产运营能力与同行业对比，并分析市场地位、技术、规模、体制等差异。",
    "5.8": "财务首要目的是真实，必须下结论；涉及上市可行性与企业竞争力也要下结论。",
    "6.1": "截至调查基准日最新股权结构图（上至实际控制人，下至子公司）。",
    "6.2": "根据工商登记资料用表格或图示描述历史沿革及每次重大变化原因，重点考察重大股权变更与资产重组的原因及影响。",
    "6.3": "控股股东、实际控制人基本情况（含财务）；关联交易、同业竞争的兄弟公司情况及处理措施。",
    "6.4": "归纳历史沿革主要问题，评估对投资和未来上市的影响，提出解决方案（含时间与资金成本）。",
    "7.1": "短期发展规划逐项列出并分析可行性；结合行业和竞争分析讨论现有业务业绩可持续性；新增业务/产能/产品能否按时完成、达预期效果。",
    "7.2": "现有业务自然增长（行业趋向、速率、驱动因素、企业角色）；向上下游拓展的能力及可行性；兼并收购实现快速成长的可能性。",
    "7.3": "成长路径是盈利预测和投资收益测算最重要的逻辑依据，必须结合核心竞争力分析，做到务实、可行、充分、明确。",
    "8.1": "盈利预测依据、核心逻辑和关键过程：收入与产品/服务数量对应；费用率综合考虑投资后影响；资金模型与预测简式报表；时限至少覆盖拟投资期。",
    "8.2": "投前估值和投后估值测算。",
    "8.3": "未来5-10年展望：第5年、第10年的产业格局，以及企业市场占有率、收入、净利润。",
    "9.1": "私募方案、私募原因、募集资金投向及募投项目可行性。",
    "9.2": "上市主体、预计上市/申报时间、拟上市地、上市准备（券商/律师/会计师是否选定）、上市难度评估。",
    "9.3": "上市可行性：资产演变是否有瑕疵、股权变化是否合规、财务指标是否符合上市要求、清除障碍的隐性成本。",
    "9.4": "上市协助、兼并收购等增值服务。",
    "10.1": "交易价格、总体估值、每股价格、出资额、市盈率、借款、分步支付、倒押股权；交易类型；交易对象；交易完成后股权结构；业绩承诺与退出。",
    "10.2": "以盈利预测为基础，结合退出市盈率估计：股权比例、退出市值依据净利润、退出P/E倍数假设依据。",
    "11": "风险性质、对业务和上市进程的影响、对财务业绩的影响与量化、发生概率及判断依据、应对措施。",
    "12.1": "对投资价值作总体判断（行业地位、发展机遇），分项论述推高收益率的要素，剔除空洞优势。",
    "12.2": "对投资建议的依据进行简要概括。",
    "12.3": "给出明确的投资结论。",
}


def _dc_get(report: str, filt: str, ps: int = 5, pn: int = 1, extra: str = "") -> list[dict]:
    url = _DC.format(report=report, filt=urllib.parse.quote(filt, safe="()"),
                     ps=ps, pn=pn, extra=extra)
    data = fetch_json(url, encoding="utf-8", referer=_REF)
    return ((data or {}).get("result") or {}).get("data") or []


def _num(v, default=None):
    if v is None or v == "" or v == "-":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct_rank(values: list[float], current: float) -> float | None:
    vals = [v for v in values if v and v > 0]
    if len(vals) < 60 or not current or current <= 0:
        return None
    below = sum(1 for v in vals if v <= current)
    return round(below / len(vals) * 100.0, 1)


def _cagr(end, start, years):
    if not end or not start or start <= 0 or end <= 0 or years <= 0:
        return None
    try:
        return round(((end / start) ** (1.0 / years) - 1.0) * 100.0, 1)
    except (ZeroDivisionError, OverflowError):
        return None


def _sec(guide_key: str, data=None, inference: str = "", note: str = "") -> dict:
    """统一小节结构。data 为可渲染数据（dict/list），inference 为规则化研判文字。"""
    return {
        "guide": GUIDES.get(guide_key, ""),
        "data": data,
        "inference": inference,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# 数据抓取
# --------------------------------------------------------------------------- #
def _fetch_basic(secu):
    rows = _dc_get("RPT_F10_ORG_BASICINFO", f'(SECUCODE="{secu}")', ps=1)
    if not rows:
        return {}, "基本资料无数据"
    r = rows[0]
    return {
        "org_name": str(r.get("ORG_NAME") or ""),
        "industry_em": str(r.get("EM2016") or ""),
        "chairman": str(r.get("CHAIRMAN") or ""),
        "legal_person": str(r.get("LEGAL_PERSON") or ""),
        "president": str(r.get("PRESIDENT") or ""),
        "secretary": str(r.get("SECRETARY") or ""),
        "found_date": str(r.get("FOUND_DATE") or "")[:10],
        "listing_date": str(r.get("LISTING_DATE") or r.get("LISTED_DATE") or "")[:10],
        "reg_capital": _num(r.get("REG_CAPITAL")),
        "staff_num": _num(r.get("TOTAL_NUM")),
        "main_business": str(r.get("MAIN_BUSINESS") or ""),
        "org_profile": str(r.get("ORG_PROFIE") or ""),
        "org_web": str(r.get("ORG_WEB") or ""),
        "address": str(r.get("ADDRESS") or ""),
        "concepts": str(r.get("BLGAINIAN") or ""),
        "org_code": str(r.get("ORG_CODE") or ""),
    }, ""


def _fetch_mainfin(secu):
    rows = _dc_get("RPT_F10_FINANCE_MAINFINADATA", f'(SECUCODE="{secu}")',
                   ps=16, extra="&sortColumns=REPORT_DATE&sortTypes=-1")
    out = []
    for r in rows:
        out.append({
            "date": str(r.get("REPORT_DATE") or "")[:10],
            "period": str(r.get("REPORT_DATE_NAME") or ""),
            "eps": _num(r.get("EPSJB")),
            "bps": _num(r.get("BPS")),
            "ocfps": _num(r.get("MGJYXJJE")),
            "revenue": _num(r.get("TOTALOPERATEREVE")),
            "net_profit": _num(r.get("PARENTNETPROFIT")),
            "revenue_yoy": _num(r.get("TOTALOPERATEREVETZ")),
            "net_profit_yoy": _num(r.get("PARENTNETPROFITTZ")),
            "gross_margin": _num(r.get("XSMLL")),
            "net_margin": _num(r.get("XSJLL")),
            "roe": _num(r.get("ROEJQ")),
            "debt_ratio": _num(r.get("ZCFZL")),
        })
    return out, "" if out else "主要财务指标无数据"


def _fetch_balance_annual(secu):
    # 东财 F10 报表按公司类型分模板：G 一般工商、B 银行、I 保险、S 证券
    rows = []
    for tpl in ("G", "B", "I", "S"):
        rows = _dc_get(f"RPT_F10_FINANCE_{tpl}BALANCE",
                       f'(SECUCODE="{secu}")(REPORT_TYPE="年报")',
                       ps=5, extra="&sortColumns=REPORT_DATE&sortTypes=-1")
        if rows:
            break
    return [{
        "date": str(r.get("REPORT_DATE") or "")[:10],
        "total_assets": _num(r.get("TOTAL_ASSETS")),
        "parent_equity": _num(r.get("TOTAL_PARENT_EQUITY")),
        "total_liab": _num(r.get("TOTAL_LIABILITIES")),
        "goodwill": _num(r.get("GOODWILL"), 0.0),
        "accounts_rece": _num(r.get("ACCOUNTS_RECE"), 0.0),
        "note_rece": _num(r.get("NOTE_ACCOUNTS_RECE"), 0.0),
        "monetary": _num(r.get("MONETARYFUNDS"), 0.0),
        "inventory": _num(r.get("INVENTORY"), 0.0),
        "contract_liab": _num(r.get("CONTRACT_LIAB"), 0.0),
        "fixed_asset": _num(r.get("FIXED_ASSET"), 0.0),
        "intangible": _num(r.get("INTANGIBLE_ASSET"), 0.0),
        "accounts_payable": _num(r.get("ACCOUNTS_PAYABLE"), 0.0),
    } for r in rows], "" if rows else "资产负债表无数据"


def _fetch_cashflow_annual(secu):
    rows = []
    for tpl in ("G", "B", "I", "S"):
        rows = _dc_get(f"RPT_F10_FINANCE_{tpl}CASHFLOW",
                       f'(SECUCODE="{secu}")(REPORT_TYPE="年报")',
                       ps=5, extra="&sortColumns=REPORT_DATE&sortTypes=-1")
        if rows:
            break
    return [{
        "date": str(r.get("REPORT_DATE") or "")[:10],
        "netcash_operate": _num(r.get("NETCASH_OPERATE")),
        "netcash_invest": _num(r.get("NETCASH_INVEST")),
        "netcash_finance": _num(r.get("NETCASH_FINANCE")),
    } for r in rows], "" if rows else "现金流量表无数据"


def _fetch_mainop(secu):
    rows = _dc_get("RPT_F10_FN_MAINOP", f'(SECUCODE="{secu}")',
                   ps=80, extra="&sortColumns=REPORT_DATE&sortTypes=-1")
    if not rows:
        return {}, "主营构成无数据"
    latest = str(rows[0].get("REPORT_DATE") or "")[:10]
    groups = {"1": [], "2": [], "3": []}
    for r in rows:
        if str(r.get("REPORT_DATE") or "")[:10] != latest:
            continue
        t = str(r.get("MAINOP_TYPE") or "")
        if t not in groups:
            continue
        groups[t].append({
            "item": str(r.get("ITEM_NAME") or ""),
            "income": _num(r.get("MAIN_BUSINESS_INCOME")),
            "ratio": (_num(r.get("MBI_RATIO")) or 0) * 100,      # EM 返回小数，转百分比
            "gross_margin": (_num(r.get("GROSS_RPOFIT_RATIO")) or 0) * 100,
        })
    for lst in groups.values():
        lst.sort(key=lambda x: -(x["income"] or 0))
    return {"date": latest, "by_industry": groups["1"],
            "by_product": groups["2"], "by_region": groups["3"]}, ""


def _fetch_holders(secu):
    out = {"top10": [], "holder_num": {}}
    err = []
    try:
        rows = _dc_get("RPT_F10_EH_HOLDERS", f'(SECUCODE="{secu}")',
                       ps=10, extra="&sortColumns=HOLDER_RANK&sortTypes=1")
        for r in rows:
            out["top10"].append({
                "rank": _num(r.get("HOLDER_RANK")),
                "name": str(r.get("HOLDER_NAME") or ""),
                "ratio": _num(r.get("HOLD_NUM_RATIO")),
                "date": str(r.get("REPORT_DATE") or r.get("END_DATE") or "")[:10],
            })
    except Exception as exc:
        err.append(f"十大股东：{exc}")
    try:
        code = secu.split(".")[0]
        rows2 = _dc_get("RPT_HOLDERNUMLATEST", f'(SECURITY_CODE="{code}")', ps=5)
        if not rows2:
            rows2 = _dc_get("RPT_HOLDERNUM_DET", f'(SECURITY_CODE="{code}")',
                            ps=5, extra="&sortColumns=END_DATE&sortTypes=-1")
        nums = [{"date": str(r.get("END_DATE") or "")[:10],
                 "num": _num(r.get("HOLDER_NUM")),
                 "change_ratio": _num(r.get("HOLDER_NUM_RATIO"))} for r in rows2]
        out["holder_num"] = {"latest": nums[0] if nums else None, "trend": nums}
    except Exception as exc:
        err.append(f"股东户数：{exc}")
    return out, "；".join(err)


def _fetch_valuation_history(secu):
    rows = []
    for pn in range(1, 6):
        try:
            part = _dc_get("RPT_VALUEANALYSIS_DET", f'(SECUCODE="{secu}")',
                           ps=500, pn=pn, extra="&sortColumns=TRADE_DATE&sortTypes=-1")
        except Exception:
            break
        if not part:
            break
        rows.extend(part)
        if len(part) < 500:
            break
    if not rows:
        return {}, "估值历史无数据"
    cur = rows[0]
    cur_pe, cur_pb, cur_ps = _num(cur.get("PE_TTM")), _num(cur.get("PB_MRQ")), _num(cur.get("PS_TTM"))
    pe_list = [_num(r.get("PE_TTM")) for r in rows]
    pb_list = [_num(r.get("PB_MRQ")) for r in rows]
    ps_list = [_num(r.get("PS_TTM")) for r in rows]

    def _quarts(lst):
        vals = sorted(v for v in lst if v and v > 0)
        if not vals:
            return {}
        out = {}
        for q, k in ((0.2, "p20"), (0.3, "p30"), (0.5, "p50"), (0.7, "p70")):
            out[k] = round(vals[min(int(len(vals) * q), len(vals) - 1)], 2)
        return out

    def _stats(lst, cv):
        vals = [v for v in lst if v and v > 0]
        return {"current": round(cv, 2) if cv else None,
                "percentile": _pct_rank(lst, cv) if cv else None,
                "median": round(median(vals), 2) if vals else None,
                "min": round(min(vals), 2) if vals else None,
                "max": round(max(vals), 2) if vals else None}
    pe_q = _quarts(pe_list)
    return {
        "as_of": str(cur.get("TRADE_DATE") or "")[:10],
        "span": f"{str(rows[-1].get('TRADE_DATE') or '')[:10]} ~ {str(rows[0].get('TRADE_DATE') or '')[:10]}",
        "days": len(rows),
        "pe": _stats(pe_list, cur_pe), "pb": _stats(pb_list, cur_pb),
        "ps": _stats(ps_list, cur_ps), "peg": _num(cur.get("PEG_CAR")),
        "pe_quartiles": pe_q,
        "total_cap": _num(cur.get("TOTAL_MARKET_CAP")),
    }, ""


def _fetch_predict(secu):
    rows = _dc_get("RPT_WEB_RESPREDICT", f'(SECUCODE="{secu}")', ps=1)
    if not rows:
        return {}, "无机构盈利预测"
    r = rows[0]
    years = []
    for i in range(1, 5):
        y = r.get(f"YEAR{i}")
        eps = _num(r.get(f"EPS{i}"))
        if y and eps is not None:
            years.append({"year": int(_num(y, 0)), "mark": str(r.get(f"YEAR_MARK{i}") or ""),
                          "eps": round(eps, 2)})
    return {
        "org_num": _num(r.get("RATING_ORG_NUM"), 0),
        "buy_num": _num(r.get("RATING_BUY_NUM"), 0),
        "add_num": _num(r.get("RATING_ADD_NUM"), 0),
        "neutral_num": _num(r.get("RATING_NEUTRAL_NUM"), 0),
        "reduce_num": _num(r.get("RATING_REDUCE_NUM"), 0),
        "years": years,
        "industry_board": str(r.get("INDUSTRY_BOARD") or ""),
        "target_max": _num(r.get("DEC_AIMPRICEMAX")),
        "target_min": _num(r.get("DEC_AIMPRICEMIN")),
    }, ""


def _latest_report_date() -> str:
    """最近一个已充分披露的季度末（距今≥45 天），用于同行名单取数。"""
    today = _dt.date.today()
    quarters = []
    for year in (today.year, today.year - 1):
        quarters += [_dt.date(year, 12, 31), _dt.date(year, 9, 30),
                     _dt.date(year, 6, 30), _dt.date(year, 3, 31)]
    for q in quarters:
        if (today - q).days >= 45:
            return q.isoformat()
    return quarters[0].isoformat()


def _peers_from_datacenter(industry: str, self_code: str) -> list[dict]:
    """同行名单备用源：东财 datacenter 业绩报表按 BOARD_NAME 过滤（clist 被风控时使用）。"""
    if not industry:
        return []
    rdate = _latest_report_date()
    board = urllib.parse.quote(industry, safe="")
    cols = ("SECUCODE%2CSECURITY_CODE%2CSECURITY_NAME_ABBR"
            "%2CBOARD_NAME%2CPARENT_NETPROFIT")
    rows: list[dict] = []
    for pn in range(1, 6):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD"
               f"&columns={cols}&filter=(REPORTDATE%3D%27{rdate}%27)(BOARD_NAME%3D%22{board}%22)"
               f"&pageSize=100&pageNumber={pn}"
               "&sortColumns=PARENT_NETPROFIT&sortTypes=-1")
        data = fetch_json(url, encoding="utf-8", referer="https://data.eastmoney.com/",
                          retries=2)
        part = ((data or {}).get("result") or {}).get("data") or []
        rows += part
        if len(part) < 100:
            break
    if not rows:
        return []
    peers = []
    for r in rows[:15]:
        code = str(r.get("SECURITY_CODE") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        secu = str(r.get("SECUCODE") or "")
        market = secu.split(".")[-1].lower() if "." in secu else (
            "bj" if code.startswith(("43", "83", "87", "92")) else
            "sz" if code.startswith(("00", "30")) else "sh")
        item = {"code": code, "name": str(r.get("SECURITY_NAME_ABBR") or ""),
                "price": None, "change_pct": None, "pe": None, "pb": None,
                "cap": None, "is_self": code == self_code}
        try:
            q = get_quote(market + code)
            item.update({"price": q.price, "change_pct": q.change_pct,
                         "pe": q.pe or None, "pb": q.pb or None,
                         "cap": q.total_cap or None})
        except Exception:  # noqa: BLE001 - 单只行情失败不影响名单
            pass
        peers.append(item)
    peers.sort(key=lambda x: -(x["cap"] or 0))
    return peers


def _fetch_peers(industry_board, self_code):
    if not industry_board:
        return [], "无行业板块信息"
    try:
        from .api import _board_bk_code
        bk = _board_bk_code(industry_board)
    except Exception:
        bk = None
    if not bk:
        return [], f"行业板块「{industry_board}」未匹配到BK代码"
    try:
        last_err = ""
        # 东财 clist 有 IP 级风控，失败换主机并短暂退避；仅重试 2 次避免触发更长时间封禁
        for i, host in enumerate(("push2", "push2delay")):
            try:
                url = (f"https://{host}.eastmoney.com/api/qt/clist/get?pn=1&pz=30&po=1&np=1"
                       "&fltt=2&invt=2&fid=f20&fs=b:" + bk +
                       "&fields=f12,f14,f2,f3,f9,f23,f20")
                data = fetch_json(url, encoding="utf-8",
                                  referer="https://quote.eastmoney.com/", retries=1)
                diff = ((data or {}).get("data") or {}).get("diff") or []
                if diff:
                    peers = [{"code": str(d.get("f12") or ""), "name": str(d.get("f14") or ""),
                              "price": _num(d.get("f2")), "change_pct": _num(d.get("f3")),
                              "pe": _num(d.get("f9")), "pb": _num(d.get("f23")),
                              "cap": _num(d.get("f20")),
                              "is_self": str(d.get("f12") or "") == self_code} for d in diff]
                    peers.sort(key=lambda x: -(x["cap"] or 0))
                    return peers[:15], ""
                last_err = "板块成分返回空数据"
            except Exception as exc:  # noqa: BLE001 - 单主机风控失败换主机重试
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5 * (i + 1))
        # 备用源：datacenter 按行业名过滤，不走 push2 clist
        peers = _peers_from_datacenter(industry_board, self_code)
        if peers:
            return peers[:15], ""
        return [], f"同行板块行情获取失败（{last_err}）"
    except Exception as exc:
        return [], f"{exc}"


def _fetch_peer_fin(peers):
    """同行财务对比：取市值前5同行最新年报 营收/归母净利/同比/净利率/ROE。"""
    if not peers:
        return [], "无同行列表"
    rows = []
    for p in [x for x in peers if not x.get("is_self")][:5]:
        code = str(p.get("code") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        if code.startswith(("43", "83", "87", "92")):
            suffix = ".BJ"
        elif code.startswith(("00", "30")):
            suffix = ".SZ"
        else:
            suffix = ".SH"
        secu = code + suffix
        ck = f"dd:peerfin:{secu}"
        hit = _CACHE.get(ck)
        if hit and time.time() - hit[0] <= CACHE_TTL:
            mf = hit[1]
        else:
            mf, _err = _fetch_mainfin(secu)
            _CACHE[ck] = (time.time(), mf)
        annual = [f for f in (mf or []) if str(f["date"]).endswith("-12-31")]
        if not annual:
            continue
        a = annual[0]
        rows.append({"code": code, "name": p.get("name") or "", "is_self": False,
                     "revenue": a.get("revenue"), "revenue_yoy": a.get("revenue_yoy"),
                     "net_profit": a.get("net_profit"), "net_profit_yoy": a.get("net_profit_yoy"),
                     "net_margin": a.get("net_margin"), "roe": a.get("roe")})
    return rows, "" if rows else "同行财务数据获取失败"


def _fetch_board_flow(industry_board):
    if not industry_board:
        return None, ""
    try:
        from .api import _board_flow_cached
        return _board_flow_cached(industry_board), ""
    except Exception:
        return None, "板块资金流获取失败"


# --------------------------------------------------------------------------- #
# 规则化研判
# --------------------------------------------------------------------------- #
def _judge(fin, balance, cashflow, valuation, predict, holders, quote_pe, industry=""):
    risks, bright = [], []
    # 银行/保险/证券为高杠杆经营，资产负债率与 PE 口径不能按工商企业标准评判
    is_fin = any(k in (industry or "") for k in ("银行", "保险", "证券", "多元金融"))
    annual = [f for f in fin if str(f["date"]).endswith("-12-31")]
    latest = fin[0] if fin else {}

    rev_cagr = _cagr(annual[0]["revenue"] if len(annual) >= 1 else None,
                     annual[2]["revenue"] if len(annual) >= 3 else None, 2)
    np_cagr = _cagr(annual[0]["net_profit"] if len(annual) >= 1 else None,
                    annual[2]["net_profit"] if len(annual) >= 3 else None, 2)
    if np_cagr is not None:
        if np_cagr >= 15:
            bright.append({"title": "净利润持续增长", "detail": f"近两年净利润 CAGR {np_cagr}%"})
        elif np_cagr < 0:
            risks.append({"level": "high", "title": "净利润趋势性下滑",
                          "detail": f"近两年净利润 CAGR {np_cagr}%"})
    if len(annual) >= 2:
        yoy = annual[0].get("net_profit_yoy")
        if yoy is not None and yoy < -30:
            risks.append({"level": "high", "title": "最新年报净利润大幅下滑",
                          "detail": f"{annual[0]['date'][:4]} 年报同比 {yoy}%"})
    ly = latest.get("net_profit_yoy")
    if ly is not None and ly < -20 and latest.get("period") != "年报":
        risks.append({"level": "mid", "title": "最新一期净利润同比下滑",
                      "detail": f"{latest.get('period')} 同比 {ly}%"})

    if annual and cashflow:
        cf_map = {c["date"][:4]: c for c in cashflow}
        ratios = []
        for a in annual[:3]:
            c = cf_map.get(a["date"][:4])
            if c and a.get("net_profit") and a["net_profit"] > 0 and c.get("netcash_operate") is not None:
                ratios.append(c["netcash_operate"] / a["net_profit"])
        if ratios:
            avg_ratio = sum(ratios) / len(ratios)
            if avg_ratio >= 0.8:
                bright.append({"title": "盈利含金量高",
                               "detail": f"近三年经营现金流/净利润均值 {avg_ratio:.2f}"})
            elif avg_ratio < 0.5:
                risks.append({"level": "high", "title": "盈利含金量不足",
                              "detail": f"近三年经营现金流/净利润均值仅 {avg_ratio:.2f}，利润或停留在账面"})

    roe = latest.get("roe") or (annual[0].get("roe") if annual else None)
    if roe is not None:
        if roe >= 15:
            bright.append({"title": "高 ROE", "detail": f"ROE（加权）{roe}%"})
        elif roe < 5:
            risks.append({"level": "mid", "title": "ROE 偏低", "detail": f"ROE（加权）{roe}%"})

    dr = latest.get("debt_ratio")
    if dr is not None and not is_fin:
        if dr > 65:
            risks.append({"level": "high", "title": "资产负债率过高",
                          "detail": f"资产负债率 {dr}%（>65%）"})
        elif dr < 30:
            bright.append({"title": "低杠杆", "detail": f"资产负债率仅 {dr}%"})
    if balance:
        b = balance[0]
        gw, eq = b.get("goodwill") or 0, b.get("parent_equity") or 0
        if gw > 0 and eq > 0 and gw / eq > 0.3:
            risks.append({"level": "high", "title": "商誉占净资产比例高",
                          "detail": f"商誉 {gw/1e8:.1f} 亿，占归属净资产 {gw/eq*100:.0f}%"})
        if (not is_fin and b.get("monetary") and b.get("total_liab")
                and b["monetary"] < b["total_liab"] * 0.2 and (dr or 0) > 50):
            risks.append({"level": "mid", "title": "货币资金对负债覆盖偏弱",
                          "detail": f"货币资金 {b['monetary']/1e8:.1f} 亿 vs 总负债 {b['total_liab']/1e8:.1f} 亿"})

    pe_pct = ((valuation.get("pe") or {}).get("percentile"))
    pb_pct = ((valuation.get("pb") or {}).get("percentile"))
    if pe_pct is not None:
        if pe_pct >= 85:
            risks.append({"level": "mid", "title": "PE 处于历史高位",
                          "detail": f"PE-TTM 历史分位 {pe_pct}%（{valuation.get('span')}）"})
        elif pe_pct <= 20:
            bright.append({"title": "PE 处于历史低位",
                           "detail": f"PE-TTM 历史分位仅 {pe_pct}%"})
    if pb_pct is not None and pb_pct >= 85:
        risks.append({"level": "low", "title": "PB 处于历史高位",
                      "detail": f"PB-MRQ 历史分位 {pb_pct}%"})
    if is_fin and pb_pct is not None and pb_pct <= 20:
        bright.append({"title": "PB 处于历史低位",
                       "detail": f"PB-MRQ 历史分位仅 {pb_pct}%"})

    if predict:
        if not predict.get("org_num"):
            risks.append({"level": "low", "title": "无机构覆盖",
                          "detail": "近半年无券商盈利预测，市场关注度低"})
        else:
            buy = (predict.get("buy_num") or 0) + (predict.get("add_num") or 0)
            total = predict.get("org_num") or 1
            if buy / total >= 0.6:
                bright.append({"title": "机构评级积极",
                               "detail": f"{int(total)} 家机构覆盖，买入/增持 {int(buy)} 家"})

    hn = holders.get("holder_num") or {}
    trend = hn.get("trend") or []
    if len(trend) >= 2 and trend[0].get("change_ratio") is not None:
        chg = trend[0]["change_ratio"]
        if chg >= 30:
            risks.append({"level": "mid", "title": "股东户数激增",
                          "detail": f"最新报告期股东户数环比 +{chg}%，筹码快速分散"})
        elif chg <= -15:
            bright.append({"title": "筹码集中",
                           "detail": f"股东户数环比 {chg}%，筹码趋于集中"})
    top10 = holders.get("top10") or []
    if top10:
        top1 = top10[0]
        if (top1.get("ratio") or 0) >= 50:
            bright.append({"title": "股权集中",
                           "detail": f"第一大股东持股 {top1['ratio']}%（{top1['name']}）"})

    score = 50.0
    if np_cagr is not None:
        score += max(-15, min(15, np_cagr * 0.6))
    if roe is not None:
        score += max(-10, min(10, (roe - 8) * 0.8))
    if dr is not None and not is_fin:
        score += -8 if dr > 65 else (4 if dr < 40 else 0)
    # 金融机构以 PB 分位衡量估值高低（PE 受拨备/投资收益扰动大）
    val_pct = pb_pct if (is_fin and pb_pct is not None) else pe_pct
    if val_pct is not None:
        score += 8 if val_pct <= 20 else (-8 if val_pct >= 85 else (4 - val_pct * 0.04))
    for r in risks:
        score -= {"high": 6, "mid": 3, "low": 1}.get(r["level"], 1)
    for b in bright:
        score += 2
    score = round(max(5, min(95, score)), 1)

    if score >= 75:
        grade, grade_cn = "A", "优质"
    elif score >= 60:
        grade, grade_cn = "B", "良好"
    elif score >= 45:
        grade, grade_cn = "C", "中性"
    elif score >= 30:
        grade, grade_cn = "D", "谨慎"
    else:
        grade, grade_cn = "E", "回避"

    return {"risks": sorted(risks, key=lambda r: {"high": 0, "mid": 1, "low": 2}[r["level"]]),
            "bright": bright, "rev_cagr_2y": rev_cagr, "np_cagr_2y": np_cagr,
            "score": score, "grade": grade, "grade_cn": grade_cn}


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def fundamental_report(raw: str) -> dict:
    key = f"dd:{(raw or '').strip().lower()}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] <= CACHE_TTL:
        return hit[1]

    symbol, resolved_name, via = resolve_input(raw)
    symbol = _resolve_bj_new_code(symbol)
    secu = _secu_code(symbol)
    code6 = symbol[2:]
    errors = []

    def _step(name, fn, *args):
        try:
            data, err = fn(*args)
            if err:
                errors.append(f"{name}：{err}")
            return data
        except Exception as exc:
            errors.append(f"{name}：{type(exc).__name__} {exc}")
            return None

    quote = None
    try:
        quote = get_quote(symbol)
    except Exception as exc:
        errors.append(f"实时行情：{exc}")

    basic = _step("基本资料", _fetch_basic, secu) or {}
    fin = _step("财务指标", _fetch_mainfin, secu) or []
    balance = _step("资产负债表", _fetch_balance_annual, secu) or []
    cashflow = _step("现金流量表", _fetch_cashflow_annual, secu) or []
    mainop = _step("主营构成", _fetch_mainop, secu) or {}
    holders = _step("股东结构", _fetch_holders, secu) or {"top10": [], "holder_num": {}}
    valuation = _step("估值历史", _fetch_valuation_history, secu) or {}
    predict = _step("机构预测", _fetch_predict, secu) or {}

    industry_board = predict.get("industry_board") or (basic.get("industry_em", "").split("-")[-1] if basic.get("industry_em") else "")
    peers = _step("同行对比", _fetch_peers, industry_board, code6) or []
    board_flow = _step("板块资金", _fetch_board_flow, industry_board)

    judge = _judge(fin, balance, cashflow, valuation, predict, holders,
                   quote.pe if quote else 0, industry_board)
    name = (quote.name if quote and quote.name else resolved_name) or code6
    price = quote.price if quote else 0

    # ---- 同行财务对比（含自身，按归母净利降序）----
    peer_fin = _step("同行财务", _fetch_peer_fin, peers) or []
    _ann_full = [f for f in fin if str(f["date"]).endswith("-12-31")]
    if _ann_full:
        a0 = _ann_full[0]
        peer_fin.append({"code": code6, "name": name, "is_self": True,
                         "revenue": a0.get("revenue"), "revenue_yoy": a0.get("revenue_yoy"),
                         "net_profit": a0.get("net_profit"), "net_profit_yoy": a0.get("net_profit_yoy"),
                         "net_margin": a0.get("net_margin"), "roe": a0.get("roe")})
    peer_fin.sort(key=lambda x: -(x.get("net_profit") or 0))

    annual = [f for f in fin if str(f["date"]).endswith("-12-31")][:3]
    latest_period = (fin[0] if fin and fin[0]["date"] != (annual[0]["date"] if annual else None)
                     and not str(fin[0]["date"]).endswith("-12-31") else None)
    brief_rows = annual + ([latest_period] if latest_period else [])

    target_note = ""
    if predict.get("target_max") and price:
        up = (predict["target_max"] / price - 1) * 100
        lo = (predict["target_min"] / price - 1) * 100 if predict.get("target_min") else None
        target_note = (f"机构目标价区间 {predict['target_min']}~{predict['target_max']} 元，较现价 {price} 元空间 {lo:+.0f}%~{up:+.0f}%"
                       if lo is not None else f"机构目标价 {predict['target_max']} 元，较现价空间 {up:+.0f}%")
        if up < 0:
            judge["risks"].append({"level": "mid", "title": "目标价倒挂",
                                   "detail": f"现价高于机构最高目标价（{predict['target_max']} 元）"})

    value_points = [f"{b['title']}：{b['detail']}" for b in judge["bright"][:5]]
    rc = judge.get("rev_cagr_2y")
    if rc is not None:
        lifecycle = (f"公司营收近两年 CAGR {rc}%，" +
                     ("处于较快扩张通道（成长期特征）" if rc >= 15 else
                      "增长平稳（成熟期特征）" if rc >= 5 else
                      "基本停滞（成熟后期/平台期特征）" if rc >= -5 else
                      "收入收缩（需警惕行业或公司衰退风险）"))
    else:
        lifecycle = "营收增速数据不足，暂无法研判生命周期"

    self_rank = next((i + 1 for i, p in enumerate(peers) if p.get("is_self")), None)

    # ---- 业务定位推论 ----
    biz_position = ""
    if mainop.get("by_product"):
        top = mainop["by_product"][0]
        biz_position = (f"核心产品为「{top['item']}」，收入占比 {round(top['ratio'], 1)}%，"
                        f"毛利率 {round(top['gross_margin'], 1)}%。")
    if mainop.get("by_region"):
        top_r = mainop["by_region"][0]
        biz_position += f"主要市场在{top_r['item']}（收入占比 {round(top_r['ratio'], 1)}%）。"

    # ---- 采购/销售推论（基于报表科目）----
    purchase_note = ""
    if balance:
        b = balance[0]
        ap = b.get("accounts_payable") or 0
        inv = b.get("inventory") or 0
        if ap > 0 and inv > 0:
            purchase_note = (f"报表口径：应付账款 {ap/1e8:.1f} 亿、存货 {inv/1e8:.1f} 亿，"
                             f"存货/营收（最新一期）{(inv/(fin[0]['revenue'] or 1))*100:.1f}%。"
                             f"具体供应商集中度、采购账期需查阅年报「前五名供应商」附注。")
    sales_note = ""
    if balance:
        b = balance[0]
        cl = b.get("contract_liab") or 0
        ar = (b.get("accounts_rece") or 0) + (b.get("note_rece") or 0)
        sales_note = (f"报表口径：合同负债（预收款）{cl/1e8:.1f} 亿、应收+票据 {ar/1e8:.1f} 亿。"
                      f"具体客户集中度、销售模式、定价策略需查阅年报「前五名客户」附注。")

    # ---- 技术研发推论 ----
    rd_note = ""
    # 研发费用在 MAINFINADATA 中可能有字段，但标准表通常不含；这里提示
    rd_note = "公开 F10 接口不含研发费用率、专利数等字段，需查阅年报「研发投入」章节补充。"

    # ---- 波特五力 & 产业链数据准备（基于报表科目）----
    latest = fin[0] if fin else {}
    bl0 = balance[0] if balance else {}
    latest_rev = latest.get("revenue") or 0
    ar = bl0.get("accounts_rece", 0) or 0        # 应收账款（对下游议价）
    cl = bl0.get("contract_liab", 0) or 0        # 合同负债（预收能力）
    ap = bl0.get("accounts_payable", 0) or 0     # 应付账款（对上游占款）
    inv = bl0.get("inventory", 0) or 0           # 存货
    ar_ratio = round(ar / latest_rev * 100, 1) if latest_rev else None
    cl_ratio = round(cl / latest_rev * 100, 1) if latest_rev else None
    ap_ratio = round(ap / latest_rev * 100, 1) if latest_rev else None
    inv_ratio = round(inv / latest_rev * 100, 1) if latest_rev else None
    gm = latest.get("gross_margin")              # 毛利率
    roe = latest.get("roe")                      # ROE

    # 供应商议价能力（对上游）：应付/营收高→占款能力强→上游议价弱
    if ap_ratio is not None:
        if ap_ratio >= 15:
            sup_power = "较弱"
            sup_evidence = f"应付账款/营收 {ap_ratio}%，公司对上游供应商占款能力较强，供应商议价能力较弱。"
        elif ap_ratio <= 5:
            sup_power = "较强"
            sup_evidence = f"应付账款/营收仅 {ap_ratio}%，公司对上游占款能力弱，供应商议价能力较强。"
        else:
            sup_power = "中等"
            sup_evidence = f"应付账款/营收 {ap_ratio}%，对上游议价能力中等。"
    else:
        sup_power, sup_evidence = "待评估", "缺少财务数据。"

    # 购买者议价能力（对下游）：应收/营收高→被下游占款→客户议价强；合同负债/营收高→预收→客户议价弱
    buyer_parts = []
    if ar_ratio is not None:
        if ar_ratio >= 20:
            buyer_parts.append(f"应收账款/营收 {ar_ratio}% 偏高，客户占款明显，购买者议价能力较强")
        elif ar_ratio <= 5:
            buyer_parts.append(f"应收账款/营收仅 {ar_ratio}%，几乎无赊销，购买者议价能力弱")
        else:
            buyer_parts.append(f"应收账款/营收 {ar_ratio}%，赊销适中")
    if cl_ratio is not None:
        if cl_ratio >= 10:
            buyer_parts.append(f"合同负债/营收 {cl_ratio}% 较高，先收款后交货，公司对下游话语权强")
        elif cl_ratio > 0:
            buyer_parts.append(f"合同负债/营收 {cl_ratio}%")
    if buyer_parts:
        buyer_evidence = "；".join(buyer_parts) + "。"
        buyer_power = "较强" if (ar_ratio or 0) >= 20 and (cl_ratio or 0) < 5 else (
            "较弱" if (cl_ratio or 0) >= 10 and (ar_ratio or 0) <= 5 else "中等")
    else:
        buyer_power, buyer_evidence = "待评估", "缺少财务数据。"

    # 潜在进入者威胁：高毛利率+高ROE→壁垒高但吸引进入；低毛利→壁垒低
    if gm is not None and roe is not None:
        if gm >= 40 and roe >= 15:
            entry_threat = "中等（高盈利吸引资本，但壁垒可能较高）"
            entry_evidence = f"毛利率 {gm:.1f}%、ROE {roe:.1f}%，盈利能力强，会吸引潜在进入者；" \
                             f"但高毛利通常意味着品牌、技术或资源壁垒。需结合行业具体进入门槛判断。"
        elif gm < 15:
            entry_threat = "较高"
            entry_evidence = f"毛利率仅 {gm:.1f}%，行业壁垒不高，潜在进入者威胁较大。"
        else:
            entry_threat = "中等"
            entry_evidence = f"毛利率 {gm:.1f}%、ROE {roe:.1f}%，进入壁垒中等。"
    else:
        entry_threat, entry_evidence = "待评估", "缺少财务数据。"

    # 替代品威胁（无直接数据，提示）
    sub_evidence = "替代品威胁需结合行业技术路线与消费趋势判断，公开 F10 接口不含此类信息。"

    # 行业内竞争程度（用同行数量辅助）
    if peers and len(peers) > 1:
        if self_rank and self_rank <= 3:
            rivalry = "激烈但公司处于第一梯队"
            rivalry_evidence = f"「{industry_board}」板块共 {len(peers)} 家公司，公司市值排名第 {self_rank}，处于第一梯队。"
        else:
            rivalry = "激烈"
            rivalry_evidence = f"「{industry_board}」板块共 {len(peers)} 家公司，公司市值排名第 {self_rank or '—'}。"
    else:
        rivalry = "待评估"
        rivalry_evidence = "同行对比数据缺失，竞争激烈程度需行业研报补充。"

    industry_chain = {
        "upstream": "上游供应商议价能力评估：" + sup_evidence,
        "midstream": f"公司在产业链中的位置：所属{basic.get('industry_em') or '—'}行业，"
                     f"主营{mainop.get('summary') or '—'}。",
        "downstream": "下游客户议价能力评估：" + buyer_evidence,
        "position": ("应收/营收 " + (f"{ar_ratio}%" if ar_ratio is not None else "—") + "；合同负债/营收 " +
                     (f"{cl_ratio}%" if cl_ratio is not None else "—") + "；应付/营收 " +
                     (f"{ap_ratio}%" if ap_ratio is not None else "—") + "。综合反映公司在产业链中的话语权。")
    }

    five_forces = {
        "supplier_power": {"level": sup_power, "evidence": sup_evidence},
        "buyer_power": {"level": buyer_power, "evidence": buyer_evidence},
        "new_entrant": {"level": entry_threat, "evidence": entry_evidence},
        "substitute": {"level": "待评估", "evidence": sub_evidence},
        "rivalry": {"level": rivalry, "evidence": rivalry_evidence},
    }

    # ---- 盈利预测情景与估值敏感性（规则化测算，非公司指引）----
    shares = (quote.total_cap / price) if quote and quote.total_cap and price else None
    np_base, fc_source = None, ""
    fwd = next((y for y in (predict.get("years") or []) if y.get("mark") != "A" and y.get("eps")), None)
    if fwd and shares:
        np_base = fwd["eps"] * shares
        fc_source = (f"{int(predict.get('org_num') or 0)} 家机构一致预期：{fwd['year']} 年 EPS {fwd['eps']} 元"
                     f" × 总股本 {shares/1e8:.2f} 亿股")
    elif _ann_full and _ann_full[0].get("net_profit"):
        np_base = _ann_full[0]["net_profit"] * (1 + (judge.get("np_cagr_2y") or 0) / 100)
        fc_source = f"最新年报净利 ×（1+净利 CAGR {judge.get('np_cagr_2y')}%）外推"
    scen_rows = []
    if np_base:
        total_cap = quote.total_cap if quote and quote.total_cap else None
        for label, mult, note in (("悲观", 0.95, "基准 -5%：需求疲软/提价传导不畅"),
                                  ("基准", 1.00, "一致预期或历史增速外推"),
                                  ("乐观", 1.08, "基准 +8%：需求修复/提价落地")):
            np_v = np_base * mult
            scen_rows.append({"name": label, "np": round(np_v / 1e8, 1),
                              "pe": round(total_cap / np_v, 1) if total_cap else None,
                              "note": note})
    val_matrix = []
    if np_base and shares:
        q = (valuation.get("pe_quartiles") or {})
        for ak in ("p30", "p50", "p70"):
            av = q.get(ak)
            if not av:
                continue
            row = {"pe_label": f"{ak} {av}倍", "cells": []}
            for r in scen_rows:
                cap = r["np"] * 1e8 * av
                row["cells"].append({"name": r["name"], "cap_ty": round(cap / 1e12, 2),
                                     "price": round(cap / shares, 0)})
            val_matrix.append(row)

    payload = {
        "ok": True,
        "meta": {"symbol": symbol, "code": code6, "name": name, "via": via,
                 "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        "quote": quote.to_dict() if quote else None,
        "summary": {
            "business_brief": (f"{name}，{basic.get('address') or '—'}；行业：{basic.get('industry_em') or '—'}；"
                               f"董事长：{basic.get('chairman') or '—'}；员工 {int(basic['staff_num']) if basic.get('staff_num') else '—'} 人。"),
            "main_business": basic.get("main_business") or "",
            "fin_brief": brief_rows,
            "value_points": value_points,
            "score": judge["score"], "grade": judge["grade"], "grade_cn": judge["grade_cn"],
        },
        # 1 主营业务
        "ch1": {
            "1.1": _sec("1.1", {"name": name, "address": basic.get("address"),
                                "reg_capital": basic.get("reg_capital"),
                                "staff_num": basic.get("staff_num"),
                                "industry": basic.get("industry_em"),
                                "main_business": basic.get("main_business"),
                                "chairman": basic.get("chairman")},
                       f"{name} 注册地{basic.get('address') or '—'}，注册资本 {basic.get('reg_capital') and fmt_wan_yi(basic.get('reg_capital')) or '—'} 亿元，"
                       f"员工 {int(basic['staff_num']) if basic.get('staff_num') else '—'} 人，所属{basic.get('industry_em') or '—'}行业。"),
            "1.2": _sec("1.2", {"found_date": basic.get("found_date")},
                        f"公司成立于 {basic.get('found_date') or '—'}。完整发展历程、关键阶段、业务与资产演变需查阅年报「公司简介」与「历史沿革」章节。"),
            "1.3": _sec("1.3", {"mainop": mainop}, biz_position),
            "1.4": _sec("1.4", None, purchase_note,
                        "采购明细（供应商名单、采购额占比、账期、定价）公开 F10 接口不含，需查阅年报「供应商与客户」附注。"),
            "1.5": _sec("1.5", {"fixed_asset": balance[0].get("fixed_asset") if balance else None,
                                "intangible": balance[0].get("intangible") if balance else None},
                        (f"报表口径：固定资产 {(balance[0].get('fixed_asset') or 0)/1e8:.1f} 亿、"
                         f"无形资产 {(balance[0].get('intangible') or 0)/1e8:.1f} 亿。" if balance else "") +
                        "产能/产量、生产工艺、质量控制、环保等需查阅年报「经营情况」与在建工程附注。"),
            "1.6": _sec("1.6", None, sales_note,
                        "销售明细（客户名单、销售额占比、销售模式、定价策略）需查阅年报「前五名客户」附注。"),
            "1.7": _sec("1.7", None, rd_note),
            "1.8": _sec("1.8", None,
                        "小结：需结合上述各节，评价商业模式可持续性与核心竞争力。当前自动研判要点：" +
                        ("盈利含金量高、ROE 较强，商业模式具备造血能力；" if any("盈利含金量" in b["title"] for b in judge["bright"]) else "") +
                        ("高杠杆或盈利下滑需警惕模式持续性。" if any(r["level"] == "high" for r in judge["risks"]) else "未见重大红旗。")),
        },
        # 2 行业分析
        "ch2": {
            "2.1": _sec("2.1", {"em_industry": basic.get("industry_em"), "board": industry_board},
                        f"东财行业分类：{basic.get('industry_em') or '—'}；细分板块：{industry_board or '—'}。"),
            "2.2": _sec("2.2", None,
                        "行业监管体制与政策要点需查阅证监会/行业协会公开文件及年报「行业竞争格局与政策环境」章节。"),
            "2.3": _sec("2.3", None,
                        "行业发展概况、有利/不利因素需结合行业研报补充。当前可观测：板块近2周" +
                        (f"主力净流入 {board_flow['net']} 亿、涨幅 {board_flow['pct']}%" if board_flow else "资金数据缺失") + "。"),
            "2.4": _sec("2.4", None, lifecycle),
            "2.5": _sec("2.5", None,
                        "市场容量、集中度、利润水平需行业研报数据。可结合主营构成推断公司在细分市场的地位。"),
            "2.6": _sec("2.6", {"industry_chain": industry_chain,
                                "ar_ratio": ar_ratio, "cl_ratio": cl_ratio,
                                "ap_ratio": ap_ratio, "inv_ratio": inv_ratio},
                        industry_chain["upstream"] + industry_chain["midstream"] + industry_chain["downstream"] +
                        industry_chain["position"]),
            "2.7": _sec("2.7", None,
                        f"小结：{lifecycle}。产业链地位：对上游供应商议价能力「{sup_power}」、"
                        f"对下游客户议价能力「{buyer_power}」。需从市场规模、增长率、进入壁垒、"
                        "议价能力、替代品威胁综合给出盈利能力判断（详见第3章波特五力模型）。"),
        },
        # 3 竞争分析（波特五力模型）
        "ch3": {
            "3.1": _sec("3.1", {"level": five_forces["supplier_power"]["level"],
                                "ap_ratio": ap_ratio},
                        "【供应商议价能力】" + five_forces["supplier_power"]["evidence"] +
                        "原材料集中度、供应商转换成本需查阅年报「供应商与采购」附注。"),
            "3.2": _sec("3.2", {"level": five_forces["buyer_power"]["level"],
                                "ar_ratio": ar_ratio, "cl_ratio": cl_ratio},
                        "【购买者议价能力】" + five_forces["buyer_power"]["evidence"] +
                        "客户集中度、产品差异化程度需查阅年报「前五名客户」附注。"),
            "3.3": _sec("3.3", {"level": five_forces["new_entrant"]["level"],
                                "gross_margin": gm, "roe": roe},
                        "【潜在进入者威胁】" + five_forces["new_entrant"]["evidence"] +
                        "资金壁垒、技术壁垒、牌照资质、规模效应等具体门槛需行业研报补充。"),
            "3.4": _sec("3.4", {"level": five_forces["substitute"]["level"]},
                        "【替代品威胁】" + five_forces["substitute"]["evidence"] +
                        "需关注技术迭代、消费习惯变迁、跨行业替代产品等因素。"),
            "3.5": _sec("3.5", {"peers": peers, "self_rank": self_rank,
                                "level": five_forces["rivalry"]["level"],
                                "peer_fin": peer_fin},
                        "【行业内现有竞争】" + five_forces["rivalry"]["evidence"] +
                        (f"按总市值在「{industry_board}」板块内排名第 {self_rank}/{len(peers)}。"
                         if self_rank else "板块成分股数据缺失（东财 clist 接口风控）。") +
                        ("同行最新年报财务对比见下表（按归母净利降序，来源为各公司年报口径）。"
                         if peer_fin else "")),
            "3.6": _sec("3.6", {"bright": judge["bright"]},
                        "【核心竞争优势】" +
                        ("、".join(b["title"] for b in judge["bright"][:3]) if judge["bright"] else "暂无突出亮点") +
                        "。需与主要竞争对手差异化比较后下结论。"
                        "波特五力综合研判：行业竞争结构决定公司长期盈利能力上限，"
                        "需结合五力强弱综合判断投资吸引力。"),
        },
        # 4 团队治理
        "ch4": {
            "4.1": _sec("4.1", None, "组织结构图、董事会/监事会名单需查阅年报「公司治理」章节。"),
            "4.2": _sec("4.2", {"top10": holders.get("top10")},
                        f"前十大股东合计持股 {sum((h.get('ratio') or 0) for h in (holders.get('top10') or [])[:10]):.1f}%，"
                        f"第一大股东 {(holders.get('top10') or [{}])[0].get('name') or '—'} "
                        f"持股 {(holders.get('top10') or [{}])[0].get('ratio') or 0}%。"),
            "4.3": _sec("4.3", None,
                        f"实际控制人（推定第一大股东）：{(holders.get('top10') or [{}])[0].get('name') or '—'}。"
                        "事业心、才能、执行力、道德品性需结合公开履历与访谈综合评价。"),
            "4.4": _sec("4.4", {"chairman": basic.get("chairman"), "president": basic.get("president"),
                                "secretary": basic.get("secretary"), "legal_person": basic.get("legal_person")},
                        f"董事长 {basic.get('chairman') or '—'}、总经理 {basic.get('president') or '—'}、"
                        f"董秘 {basic.get('secretary') or '—'}。详细履历需查阅年报「董事、监事、高级管理人员」章节。"),
            "4.5": _sec("4.5", {"staff_num": basic.get("staff_num")},
                        f"员工总数 {int(basic['staff_num']) if basic.get('staff_num') else '—'} 人。"
                        "部门/年龄/学历/收入结构需查阅年报「员工情况」章节。"),
            "4.6": _sec("4.6", None, "激励约束机制、薪酬、社保缴纳情况需查阅年报「董事、监事、高管报酬」与「员工情况」章节。"),
            "4.7": _sec("4.7", None,
                        "小结：治理结构稳定性可由第一大股东持股比例（" +
                        f"{(holders.get('top10') or [{}])[0].get('ratio') or 0}%）辅助判断。"
                        "团队专业性、稳定性需结合高管履历与变动情况评价。"),
        },
        # 5 财务情况
        "ch5": {
            "5.1": _sec("5.1", None, "合并范围（子公司清单）需查阅年报「合并范围」附注。"),
            "5.2": _sec("5.2", None, "审计意见类型需查阅年报审计报告。"),
            "5.3": _sec("5.3", None,
                        "环境/竞争/战略与财务特征对应：高毛利率反映差异化或成本优势；"
                        "应收/预收反映对下游议价；应付/预付反映对上游议价；周转率反映运营效率。"),
            "5.4": _sec("5.4", None, "纳税调整、会计政策变更需查阅年报「重要会计政策」与「税项」附注。"),
            "5.5": _sec("5.5", None,
                        "财务与业务融合：主营构成对应收入结构；存货周转对应生产销售节奏；"
                        "合同负债对应订单/预收情况。"),
            "5.6": _sec("5.6", {"balance": balance, "cashflow": cashflow},
                        "主要报表项目已在下方资产负债表、现金流量表中列示，需从业务角度解读形成原因。"),
            "5.7": _sec("5.7", {"peers": peers},
                        "同行对比已在第3章列示。盈利能力、偿债能力、运营能力需与同行横向比较后下结论。"),
            "5.8": _sec("5.8", None,
                        f"小结：综合评分 {judge['score']}（{judge['grade']} 档·{judge['grade_cn']}）。" +
                        ("财务真实性与竞争力需结合审计意见、毛利率/ROE 趋势、现金流质量综合判断。" if not any(r["level"] == "high" for r in judge["risks"])
                         else "存在高风险项，财务真实性或竞争力存疑，需重点核查。")),
        },
        # 6 历史沿革
        "ch6": {
            "6.1": _sec("6.1", {"top10": holders.get("top10")},
                        "当前股权结构见十大股东。完整股权结构图（含实际控制人至子公司）需查阅年报「公司治理」。"),
            "6.2": _sec("6.2", {"found_date": basic.get("found_date")},
                        f"公司成立于 {basic.get('found_date') or '—'}。重大股权变更与资产重组历史需查阅年报「历史沿革」与公告。"),
            "6.3": _sec("6.3", None, "关联方、关联交易、同业竞争需查阅年报「关联交易」章节。"),
            "6.4": _sec("6.4", None,
                        "小结：历史沿革合规性（股权变动、资产重组、出资真实性）直接影响上市与再融资可行性，"
                        "需逐一核查工商登记与公告，存在问题的需明确解决方案。"),
        },
        # 7 成长路径
        "ch7": {
            "7.1": _sec("7.1", {"rev_cagr": judge.get("rev_cagr_2y"), "np_cagr": judge.get("np_cagr_2y")},
                        f"历史增速：营收 CAGR {judge.get('rev_cagr_2y')}%，净利 CAGR {judge.get('np_cagr_2y')}%。"
                        "短期发展规划（产能扩张、新产品、新市场）需查阅年报「经营计划」与公告。"),
            "7.2": _sec("7.2", None,
                        "长期路径：现有业务自然增长（行业趋向+企业角色）、上下游拓展、并购整合。"
                        "需结合行业研报与公司战略规划评估可行性。"),
            "7.3": _sec("7.3", None,
                        "小结：成长路径是盈利预测与收益测算的核心依据，必须结合核心竞争力务实分析，"
                        "避免简单外推。当前历史增速可作为基准，未来增速需审慎下调。"),
        },
        # 8 盈利预测与估值
        "ch8": {
            "8.1": _sec("8.1", {"predict": predict, "rev_cagr": judge.get("rev_cagr_2y"),
                                "np_cagr": judge.get("np_cagr_2y"),
                                "scenarios": scen_rows, "fc_source": fc_source},
                        (f"机构一致预期：{predict.get('org_num', 0)} 家覆盖，" +
                         "；".join(f"{y['year']}年 EPS {y['eps']}" for y in predict.get("years", [])) +
                         "。" if predict.get("org_num") else "无机构盈利预测。") +
                        f"历史净利 CAGR {judge.get('np_cagr_2y')}% 可作为预测基准。" +
                        (f"情景测算（{fc_source}）：悲观/基准/乐观 归母净利 "
                         f"{scen_rows[0]['np']}/{scen_rows[1]['np']}/{scen_rows[2]['np']} 亿元，"
                         f"对应隐含 PE {scen_rows[0]['pe']}/{scen_rows[1]['pe']}/{scen_rows[2]['pe']} 倍。"
                         if scen_rows else "") +
                        "情景为规则化敏感性测算，非公司指引，概率需自行判断。"),
            "8.2": _sec("8.2", {"valuation": valuation, "target_note": target_note,
                                "matrix": val_matrix},
                        f"PE-TTM {valuation.get('pe', {}).get('current')}（历史分位 {valuation.get('pe', {}).get('percentile')}%）、"
                        f"PB-MRQ {valuation.get('pb', {}).get('current')}（分位 {valuation.get('pb', {}).get('percentile')}%）、"
                        f"PEG {round(valuation.get('peg'), 2) if valuation.get('peg') is not None else '—'}。{target_note}" +
                        (f" 估值敏感性矩阵见下表：以 PE 历史分位（p30/p50/p70）为锚，"
                         f"乘以各情景净利润得到目标市值与目标价。" if val_matrix else "")),
            "8.3": _sec("8.3", None, "未来5-10年展望需结合行业空间、公司份额、利润率假设测算。"),
        },
        # 数据来源声明（附件）
        "annex": {
            "sources": ["东方财富 datacenter F10 接口", "东方财富 push2 行情接口",
                        "机构一致预期（RPT_WEB_RESPREDICT）", "估值历史（RPT_VALUEANALYSIS_DET）"],
            "note": "本报告由本地程序自动生成，数据取自公开 F10 接口。标注「需查阅年报」的章节，"
                    "公开接口不含相应明细，需结合公司年报/招股书/公告人工补充。"
                    "评分与结论为规则化研判，仅供研究参考，不构成投资建议。",
        },
        "errors": errors,
    }

    # ---- 章节结论（参照专业尽调报告「结论先行」风格，规则化生成）----
    def _risk_txt(n=2):
        rs = judge["risks"][:n]
        return "；".join(r["title"] for r in rs) if rs else "未见显著风险项"

    _t1 = (holders.get("top10") or [{}])[0]
    _t1r = _t1.get("ratio") or 0
    _roe_v = fin[0].get("roe") if fin else None
    _dr_v = fin[0].get("debt_ratio") if fin else None
    _gm_v = fin[0].get("gross_margin") if fin else None
    _pe_pct = (valuation.get("pe") or {}).get("percentile")
    _bright2 = "；".join(f"{b['title']}（{b['detail']}）" for b in judge["bright"][:2]) or "—"
    ch_conclusions = {
        "ch1": (f"结论：{name} 主营聚焦，{biz_position or '主营构成见 1.3。'}"
                f"最新毛利率 {(round(_gm_v, 1) if _gm_v is not None else '—')}%、"
                f"ROE {(round(_roe_v, 1) if _roe_v is not None else '—')}%，"
                + ("盈利质量突出，商业模式成立；" if (_gm_v or 0) >= 30 else "盈利能力中等，需与同行横向对比；") +
                f"需跟踪风险：{_risk_txt(1)}。"),
        "ch2": (f"结论：{lifecycle.split('，')[-1] if '，' in lifecycle else lifecycle}。"
                f"产业链地位：对上游议价「{sup_power}」、对下游议价「{buyer_power}」。"
                "主要变量为行业政策与终端需求。"),
        "ch3": (f"结论：行业竞争「{five_forces['rivalry']['level']}」"
                + (f"，公司市值排名板块第 {self_rank} 位（共 {len(peers)} 家）；" if self_rank else "；") +
                f"核心优势：{'、'.join(b['title'] for b in judge['bright'][:2]) or '待年报补充'}。"),
        "ch4": (f"结论：第一大股东持股 {_t1r}%（{_t1.get('name') or '—'}），"
                + ("股权高度集中，控制权稳定；" if _t1r >= 30 else "股权相对分散；") +
                f"员工 {int(basic['staff_num']) if basic.get('staff_num') else '—'} 人。治理细节需以年报披露为准。"),
        "ch5": (f"结论：ROE {(round(_roe_v, 1) if _roe_v is not None else '—')}%、"
                f"资产负债率 {(round(_dr_v, 1) if _dr_v is not None else '—')}%，"
                + ("盈利与杠杆结构健康" if (_roe_v or 0) >= 12 and (_dr_v or 0) < 60 else "需关注盈利持续性或杠杆水平") +
                f"；{_risk_txt(1)}。"),
        "ch6": (f"结论：公司 {basic.get('found_date') or '—'} 成立，股权结构见 6.1。"
                "历史沿革合规性（重组、代持、出资真实性）需核查工商登记与公告，公开接口未见红旗项。"),
        "ch7": (f"结论：营收 CAGR {judge.get('rev_cagr_2y')}%、净利 CAGR {judge.get('np_cagr_2y')}%，"
                + ("具备成长性，可作为盈利预测基准；" if (judge.get("np_cagr_2y") or 0) >= 10 else "增长平淡，预测需审慎下调；") +
                (f"{int(predict.get('org_num') or 0)} 家机构覆盖提供一致预期锚点。" if predict.get("org_num")
                 else "无机构覆盖，预测缺少外部锚点。")),
        "ch8": (f"结论：PE-TTM 历史分位 {_pe_pct if _pe_pct is not None else '—'}%"
                + ("（历史低位，安全边际相对充足）" if _pe_pct is not None and _pe_pct <= 30 else
                   "（历史高位，警惕估值压缩）" if _pe_pct is not None and _pe_pct >= 70 else "（历史中位）") +
                (f"。机构目标价：{target_note}。" if target_note else "。无机构目标价参考。")),
    }
    for _k, _v in ch_conclusions.items():
        if _k in payload:
            payload[_k]["conclusion"] = _v
    payload["summary"]["conclusion"] = (f"综合研判：{judge['grade']} 档·{judge['grade_cn']}（评分 {judge['score']}/100）。"
                                        f"看多逻辑：{_bright2}。主要风险：{_risk_txt(2)}。")
    payload["summary"]["risk_points"] = [{"title": r["title"], "detail": r["detail"]} for r in judge["risks"][:3]]

    _CACHE[key] = (time.time(), payload)
    if len(_CACHE) > 60:
        for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:20]:
            _CACHE.pop(k, None)
    return payload


def fmt_wan_yi(v):
    """注册资本单位为万元，转亿元。"""
    if not v:
        return None
    return round(v / 10000.0, 2)
