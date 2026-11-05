# 固定只读流程：仅核对账号、筛选首发日期、下载官方数据，不发布、不投流。
*** Settings ***
Library    RPA.Browser.Selenium    auto_close=${FALSE}    run_on_failure=${NONE}
Library    rpa_agent_module.workflows.xhs_metrics_keywords.XhsMetricsLibrary    ${INPUT_FILE}    ${RESULT_FILE}

*** Tasks ***
采集小红书笔记表现
    Collect Metrics
