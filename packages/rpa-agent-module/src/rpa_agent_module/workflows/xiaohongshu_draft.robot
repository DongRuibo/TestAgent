# 本流程使用专用 Chrome 填写并验证小红书草稿；任何情况下都不执行最终发布。
*** Settings ***
Documentation       上传 Agent 已审核图文，等待自动保存，刷新后在草稿箱验证标题。
Library             RPA.Browser.Selenium    auto_close=${FALSE}
Library             rpa_agent_module.workflows.xhs_keywords.XhsWorkflowLibrary    ${INPUT_FILE}    ${RESULT_FILE}
Test Teardown       保存失败现场

*** Test Cases ***
填写并保存小红书草稿
    [Documentation]    上传图片、填写标题正文标签，并刷新后验证草稿箱记录。
    执行小红书草稿流程

*** Keywords ***
执行小红书草稿流程
    [Documentation]    串联页面、登录检查、上传、填写、自动保存和草稿箱验证步骤。
    ${target_url}=    Get Target Url
    ${debug_port}=    Get Debug Port
    ${debug_target_id}=    Get Debug Target Id
    ${login_selector}=    Get Selector    login
    ${challenge_selector}=    Get Selector    challenge
    ${file_selector}=    Get Selector    file_input
    ${title_selector}=    Get Selector    title
    ${body_selector}=    Get Selector    body
    ${upload_done_selector}=    Get Selector    upload_done
    ${draft_save_selector}=    Get Selector    draft_save
    ${draft_saved_selector}=    Get Selector    draft_saved
    ${draft_box_selector}=    Get Selector    draft_box
    ${draft_image_tab_selector}=    Get Selector    draft_image_tab
    ${draft_title_selector}=    Get Selector    draft_title
    ${image_upload_value}=    Get Image Upload Value
    ${title}=    Get Post Title
    ${body}=    Get Post Body
    Update Job Step    opening_target_page
    # 模块负责启动并持有专用 Chrome；每个 Robot 任务只附着现有会话。
    Attach Chrome Browser    ${debug_port}
    Switch Window    ${debug_target_id}
    Go To    ${target_url}
    Maximize Browser Window
    Update Job Step    checking_login
    ${challenge_visible}=    Set Variable    ${FALSE}
    IF    $challenge_selector != ""
        ${challenge_visible}=    Run Keyword And Return Status
        ...    Wait Until Page Contains Element
        ...    ${challenge_selector}
        ...    3s
    END
    IF    '${challenge_selector}' != '' and ${challenge_visible}
        Mark Needs User Action    页面出现验证码或风控提示，请在专用 Chrome 中人工处理；完成后重新运行，无需关闭窗口
        RETURN
    END
    ${logged_in}=    Run Keyword And Return Status
    ...    Wait Until Element Is Visible
    ...    ${login_selector}
    ...    15s
    IF    not ${logged_in}
        Mark Needs User Action    未检测到小红书登录状态，请先在专用 Chrome 中登录；完成后重新运行，无需关闭窗口
        RETURN
    END
    Update Job Step    opening_composer
    Wait Until Page Contains Element    ${file_selector}    45s
    Update Job Step    uploading_images
    # 直接给网页文件控件传递校验后的绝对路径，不操作系统文件对话框。
    Choose File    ${file_selector}    ${image_upload_value}
    Wait Until Page Contains Element    ${upload_done_selector}    30s
    Update Job Step    filling_content
    Input Text    ${title_selector}    ${title}
    Input Text    ${body_selector}    ${body}
    Press Keys    ${body_selector}    TAB
    Update Job Step    saving_draft
    # 默认依赖页面自动保存；只有部署人员确认独立草稿按钮后才允许点击。
    IF    $draft_save_selector != ""
        Click Element    ${draft_save_selector}
    END
    Sleep    12s
    Update Job Step    verifying_draft
    Wait Until Element Contains    ${draft_saved_selector}    编辑于    20s
    Sleep    3s
    Update Job Step    opening_draft_box
    # 刷新后重新进入编辑器，以证明内容已落入草稿而非仅存在页面内存。
    Reload Page
    Wait Until Page Contains Element    ${file_selector}    45s
    Click Element    ${draft_box_selector}
    Wait Until Element Is Visible    css:.draft-drawer    15s
    点击可见的图文草稿页签    ${draft_image_tab_selector}
    Update Job Step    verifying_draft_box
    Wait Until Page Contains Element    ${draft_title_selector}    20s
    Element Should Contain    ${draft_title_selector}    ${title}
    Capture Page Screenshot    ${OUTPUT DIR}${/}draft-box.png
    ${current_url}=    Get Location
    Mark Draft Saved    ${current_url}

点击可见的图文草稿页签
    [Documentation]    等待抽屉动画结束，并重试点击当前可见的图文页签。
    [Arguments]    ${selector}
    Wait Until Keyword Succeeds    10s    500ms    点击当前可见的图文草稿页签    ${selector}

点击当前可见的图文草稿页签
    [Documentation]    每次重新取元素，只点击当前可见且可操作的图文笔记页签。
    [Arguments]    ${selector}
    @{tab_elements}=    Get WebElements    ${selector}
    FOR    ${tab_element}    IN    @{tab_elements}
        ${is_visible}=    Run Keyword And Return Status    Element Should Be Visible    ${tab_element}
        ${is_enabled}=    Run Keyword And Return Status    Element Should Be Enabled    ${tab_element}
        IF    ${is_visible} and ${is_enabled}
            Click Element    ${tab_element}
            RETURN
        END
    END
    Fail    未找到可操作的图文草稿页签

保存失败现场
    [Documentation]    失败时截图；专用 Chrome 由模块管理并跨任务复用。
    IF    '${TEST STATUS}' == 'FAIL'
        Run Keyword And Ignore Error    Capture Page Screenshot    ${OUTPUT DIR}${/}failure.png
    END
