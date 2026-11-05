"""取数次数、持久缓存和恢复路径的回归；不访问真实服务。"""
from types import SimpleNamespace

import pytest

from test_content_workflow import workflow_env, bind
from test_weekly_planning import weekly_env, finish, say, state
from test_content_research import _valid_result
from tools.content_research import ContentResearchTools
from tools.social_research import SocialResearchTools
from web_agent.content_workflow import ContentWorkflow, WorkflowAction, public_workflow_state
from web_agent.research_session import research_limits


def test_smaller_research_limit():
    caps = research_limits('搜索最多1次；详情最多两篇；评论最多1次，每篇5条')
    assert caps['social_search_posts'] == caps['social_get_comments'] == 1
    assert caps['social_get_post_detail'] == 2 and caps['comment_limit'] == 5
    assert research_limits('搜索最多99次')['social_search_posts'] == 2


def test_budget_cache_and_recovery_survive_restart(workflow_env):
    env = workflow_env
    bind(env)
    calls, results = [], []
    def fetch(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'items': [{'title': '离线样本'}]}
    def run(identifier, goal_path, configure):
        agent = SimpleNamespace(tool_handlers={'social_search_posts': fetch, 'social_get_comments': fetch,
            'social_get_creator_stats': fetch, 'save_content_research_result': lambda **kwargs: {'success': False}})
        configure(agent)
        for word in ['样本甲', '样本甲', '样本乙', '样本丙']:
            results.append(agent.tool_handlers['social_search_posts'](keyword=word))
        results.append(agent.tool_handlers['social_get_comments'](url='https://www.xiaohongshu.com/explore/test', limit=11))
        results.append(agent.tool_handlers['social_get_creator_stats'](url='https://www.xiaohongshu.com/user/profile/test'))
    workflow = ContentWorkflow(env.store, run)
    current = env.store.content_workflow(env.identifier)
    current = workflow.act(env.identifier, WorkflowAction(action='research', expected_revision=current['revision'], confirm_research=True))
    assert len(calls) == 2
    assert results[0] == results[1]
    assert all(item['error_type'] == 'RESEARCH_BUDGET_EXCEEDED' for item in results[3:])
    assert 'research_calls' not in public_workflow_state(current)
    workflow = ContentWorkflow(env.store, run)
    current = workflow.act(env.identifier, WorkflowAction(action='continue_research', expected_revision=current['revision']))
    assert len(calls) == 2
    assert current['goal_ref'] == env.goal['goal_ref']
    assert current['research_history'] and current['research_cache_only']


def test_pending_query_uses_query_only_and_blocks_new_create(workflow_env, monkeypatch):
    env = workflow_env
    bind(env)
    def run(identifier, goal_path, configure):
        agent = SimpleNamespace(tool_handlers={'social_search_posts': lambda **kw: {'success': False, 'error_type': 'PROVIDER_RESULT_PENDING'},
            'save_content_research_result': lambda **kw: {'success': False}})
        configure(agent)
        agent.tool_handlers['social_search_posts'](keyword='原任务')
    workflow = ContentWorkflow(env.store, run)
    current = env.store.content_workflow(env.identifier)
    current = workflow.act(env.identifier, WorkflowAction(action='research', expected_revision=current['revision'], confirm_research=True))
    with pytest.raises(ValueError, match='查询取数进度'):
        workflow.act(env.identifier, WorkflowAction(action='restart_research', expected_revision=current['revision'], confirm_research=True))
    def query(self, keyword, limit=10):
        assert self.provider.query_only is True and keyword == '原任务'
        return {'success': True, 'items': []}
    monkeypatch.setattr(SocialResearchTools, 'search_posts', query)
    current = workflow.act(env.identifier, WorkflowAction(action='query_research', expected_revision=current['revision']))
    assert current['research_fetch_stop'] is None
    assert '继续研究' in current['progress_message']
    assert next(iter(current['research_calls'].values()))['status'] == 'success'


def test_failed_research_can_restart_in_same_chat_after_fee_confirmation(weekly_env):
    env = weekly_env
    env.tracker.mode = 'research_error'
    finish(env)
    before = state(env)
    assert '同意费用' in say(env, '重新研究')
    assert env.tracker.research_calls == 1
    env.tracker.mode = 'success'
    assert '下载周运营计划' in say(env, '同意费用并启动本次研究')
    assert state(env)['weekly_inputs']['plan_id'] == before['weekly_inputs']['plan_id']
    assert state(env)['goal_ref'] == before['goal_ref']


def test_two_evidenced_topics_need_not_be_padded(tmp_path):
    payload = _valid_result()
    payload['candidate_topics'] = payload['candidate_topics'][:2]
    assert ContentResearchTools(output_dir=tmp_path).save_result(**payload)['success']


def test_partial_revision_and_semantic_checks(weekly_env):
    env = weekly_env
    answer = finish(env)
    assert '测试员工甲交柜体整体照片' in answer and '2026-09-18 10:00' in answer
    from web_agent.weekly_planning import WeeklyPlanning
    workflow = ContentWorkflow(env.store, lambda *a: None)
    current = state(env)
    saved = current['weekly_saved']['markdown']
    with pytest.raises(ValueError, match='已确认依据'):
        workflow.weekly.persist(env.identifier, current, saved.replace('待核实：缺件时延后', '已确认：这是虚构的案例照片'), workflow._research(current))
    def patch(identifier, stage, configure):
        agent = SimpleNamespace(tool_handlers={})
        configure(agent)
        old = next(line for line in saved.splitlines() if '2026-09-19 | 18:00' in line)
        result = agent.tool_handlers['save_weekly_plan'](changes=[{'old': old, 'new': old.replace('| 18:00', '| 19:00')}])
        assert result['success'], result
    after = workflow.act(env.identifier, WorkflowAction(action='revise_weekly_plan', expected_revision=current['revision'], plan_notes='发布时间改为19:00'), run_plan=patch)
    assert after['weekly_saved']['version'] == 2
    assert after['weekly_saved']['markdown'] == saved.replace('2026-09-19 | 18:00', '2026-09-19 | 19:00')
    assert env.tracker.research_calls == 1


def test_query_adapter_executes_only_existing_task_getters(tmp_path):
    import json
    from tools.guaikei import GuaikeiCli
    from test_social_research import _cli_root
    root = _cli_root(tmp_path)
    (root / 'src/config').mkdir()
    (root / 'src/config/constants.js').write_text('module.exports = {};')
    (root / 'src/api').mkdir()
    (root / 'src/validate').mkdir()
    (root / 'src/validate/keyword.js').write_text('module.exports.cleanKeyword = value => value.trim();')
    (root / 'src/validate/url.js').write_text('module.exports.normalizeUrl = value => value.trim();')
    for module, getter in [('search', 'getSearchTask'), ('detail', 'getDetailTask'), ('comment', 'getCommentTask')]:
        (root / f'src/api/{module}.js').write_text(
            f'module.exports.{getter} = async (token, ...args) => ({{args}});'
            'module.exports.createTask = () => {throw new Error("MUST NEVER CREATE");};')
    cli = GuaikeiCli(root, 'synthetic-test-token', query_only=True)
    assert cli.search('原词')['results']['args'] == ['原词', 0, 0, 0, 10]
    assert cli.detail('https://www.xiaohongshu.com/explore/test')['results']['args'] == ['https://www.xiaohongshu.com/explore/test']
    assert cli.comments('https://www.xiaohongshu.com/explore/test', 5)['results']['args'] == ['https://www.xiaohongshu.com/explore/test', 5]


def test_plan_rejects_added_consultation_promise_and_wrong_time_margin(weekly_env, monkeypatch):
    env = weekly_env
    finish(env)
    workflow = ContentWorkflow(env.store, lambda *a: None)
    current = state(env)
    goal = workflow._goal(current)
    goal['goal_snapshot']['operation_goal']['summary'] += '本周不承诺咨询数量。'
    monkeypatch.setattr(workflow, '_goal', lambda state: goal)
    markdown = current['weekly_saved']['markdown']
    broken = markdown.replace('## 7. 预期数据', '## 7. 预期数据\n\n方向性预期是至少能产出有效咨询记录。')
    with pytest.raises(ValueError, match='不能新增至少产生咨询'):
        workflow.weekly.persist(env.identifier, current, broken, workflow._research(current))
    line = next(line for line in markdown.splitlines() if '2026-09-19 | 18:00' in line)
    cells = line.split('|')
    cells[2] = ' 2026-09-19 12:00 '
    cells[5] = ' 终稿到发布留1天以上 '
    broken = markdown.replace(line, '|'.join(cells))
    with pytest.raises(ValueError, match='实际相隔 6 小时'):
        workflow.weekly.persist(env.identifier, current, broken, workflow._research(current))


@pytest.mark.parametrize('duties', [
    '图片检查和图文制作交给后续环节',
    '只承担素材整理与咨询记录，不加派拍照、制作或审图任务',
])
def test_delegating_image_checks_is_not_a_human_assignment(weekly_env, duties):
    env = weekly_env
    finish(env)
    workflow = ContentWorkflow(env.store, lambda *a: None)
    current = state(env)
    markdown = current['weekly_saved']['markdown'].replace('记录由本篇引来的有效咨询',
        '记录由本篇引来的有效咨询；' + duties)
    saved = workflow.weekly.persist(env.identifier, current, markdown, workflow._research(current))
    assert saved['status'] == 'plan_ready'


def test_failed_save_returns_current_draft_for_targeted_repair(weekly_env):
    env = weekly_env
    finish(env)
    workflow = ContentWorkflow(env.store, lambda *a: None)
    current = state(env)
    original = current['weekly_saved']['markdown']
    company = workflow._research(current)['company_name']
    def repair(identifier, stage, configure):
        agent = SimpleNamespace(tool_handlers={})
        configure(agent)
        result = agent.tool_handlers['save_weekly_plan'](markdown=original.replace(company, '错误企业'))
        assert not result['success'] and '错误企业' in result['draft_for_repair']
        row = next(line for line in result['draft_for_repair'].splitlines() if '| 企业 |' in line)
        result = agent.tool_handlers['save_weekly_plan'](changes=[{'old':row, 'new':row.replace('错误企业',company)}])
        assert result['success'], result
    after = workflow.act(env.identifier, WorkflowAction(action='revise_weekly_plan', expected_revision=current['revision'], plan_notes='修正'), run_plan=repair)
    assert after['weekly_saved']['markdown'] == original
    assert after['weekly_saved']['version'] == 2
    assert env.tracker.research_calls == 1
