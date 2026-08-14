# 本流程使用专用 Chrome 操作已适配查询页，下载并归档 CSV；不操作用户日常 Chrome。
*** Settings ***
Documentation       查询固定业务表单，下载、校验、重命名并归档 CSV。
Library             RPA.Browser.Selenium
Library             rpa_agent_module.workflows.csv_keywords.CsvWorkflowLibrary    ${INPUT_FILE}    ${RESULT_FILE}
Test Teardown       保存失败现场并安全关闭浏览器

*** Test Cases ***
查询网页并归档 CSV
    [Documentation]    填写固定查询表单，下载唯一 CSV，校验后移动并按需在 Finder 中定位。
    执行 CSV 查询与归档

*** Keywords ***
执行 CSV 查询与归档
    [Documentation]    串联页面打开、表单查询、无数据判断和文件归档步骤。
    ${target_url}=    Get Target Url
    ${download_dir}=    Prepare Download Directory
    ${profile_dir}=    Get Profile Directory
    Update Job Step    opening_target_page
    Set Download Directory    ${download_dir}
    Open Available Browser
    ...    ${target_url}
    ...    use_profile=${TRUE}
    ...    headless=${FALSE}
    ...    maximized=${TRUE}
    ...    browser_selection=Chrome
    ...    profile_name=Default
    ...    profile_path=${profile_dir}
    ...    download=${TRUE}
    Wait Until Element Is Visible    css:[data-testid="query-button"]    20s
    填写并提交查询表单
    ${has_error}=    Run Keyword And Return Status
    ...    Element Should Be Visible
    ...    css:[data-testid="validation-error"]
    IF    ${has_error}
        ${message}=    Get Text    css:[data-testid="validation-error"]
        Fail    ${message}
    END
    ${no_data}=    Run Keyword And Return Status
    ...    Element Should Be Visible
    ...    css:[data-testid="empty-state"]
    IF    ${no_data}
        Mark No Data
    ELSE
        下载校验并归档 CSV
    END

填写并提交查询表单
    [Documentation]    使用稳定 data-testid 填写已经由模块校验的五个查询字段。
    Update Job Step    filling_form
    ${start_date}=    Get Query Value    start_date
    ${end_date}=    Get Query Value    end_date
    ${region}=    Get Query Value    region
    ${status}=    Get Query Value    status
    ${keyword}=    Get Query Value    keyword
    设置日期字段    start-date    ${start_date}
    设置日期字段    end-date    ${end_date}
    Select From List By Label    css:[data-testid="region"]    ${region}
    Select From List By Label    css:[data-testid="status"]    ${status}
    Input Text    css:[data-testid="keyword"]    ${keyword}
    Update Job Step    querying_data
    Click Element    css:[data-testid="query-button"]
    Wait Until Element Is Not Visible    css:[data-testid="loading-state"]    20s
    Wait Until Page Contains Element    css:[data-testid="result-count"]    20s

设置日期字段
    [Documentation]    通过 DOM 写入 ISO 日期并派发事件，兼容 Chrome 日期控件。
    [Arguments]    ${test_id}    ${value}
    Execute Javascript
    ...    const element = document.querySelector(arguments[0]); element.value = arguments[1]; element.dispatchEvent(new Event('input', {bubbles: true})); element.dispatchEvent(new Event('change', {bubbles: true}));
    ...    ARGUMENTS
    ...    input[data-testid="${test_id}"]
    ...    ${value}

下载校验并归档 CSV
    [Documentation]    下载唯一 CSV，等待写入稳定，验证表头并按防覆盖规则归档。
    Wait Until Element Is Visible    css:[data-testid="result-table"]    20s
    Wait Until Element Is Enabled    css:[data-testid="export-button"]    20s
    Update Job Step    downloading_csv
    Click Element    css:[data-testid="export-button"]
    ${downloaded_path}=    Wait For Download Completion    60
    Update Job Step    moving_file
    ${final_path}=    Archive Downloaded Csv    ${downloaded_path}
    Update Job Step    opening_finder
    Open Finder And Write Result    ${final_path}
    Update Job Step    verifying_result

保存失败现场并安全关闭浏览器
    [Documentation]    失败时保存页面截图，并只关闭当前 Robot 创建的浏览器实例。
    IF    '${TEST STATUS}' == 'FAIL'
        Run Keyword And Ignore Error    Capture Page Screenshot    ${OUTPUT DIR}${/}failure.png
    END
    Run Keyword And Ignore Error    Close All Browsers
