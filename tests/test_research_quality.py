"""Observed semantic regressions and counterexamples; no external calls."""
import pytest
from tools.research_quality import photo_limit, research_quality_errors, weekly_quality_errors

URL='https://www.xiaohongshu.com/explore/sample'


def payload(text='所读图1展示柜体。', count=3):
    return {'research_summary':text,'visual_analyses':[{'status':'success','post_url':URL,
        'available_image_count':6,'image_indexes':[1,2,6]}],
        'source_references':[{'source_id':'s1','url':URL}],
        'candidate_topics':[{'planned_photo_count':count,'evidence':[{'fact':text,'source_refs':['s1']}],
            'required_materials':['3张现有照片，未收到前待核实。']}]}


@pytest.mark.parametrize('notes,expected',[
    ('我们只有三张照片，不补拍。',3),('最多3张衣柜照片。',3),
    ('小周周二给三张衣柜照片，不补拍。',3),('至少3张小周的照片，最终再看。',None),
    ('至少3张照片，不补拍。',None),('小周给3张照片，小陈给2张照片，不补拍。',None),
    ('参考笔记每篇最多3张图片。',None),('我们只有三张照片。参考笔记每篇最多2张图片。',3),
    ('3至5张照片，不补拍。',5),('之前只有3张照片，现在最多5张照片。',None),
])
def test_photo_caps_only_from_unambiguous_input(notes,expected):
    assert photo_limit(notes)==expected


@pytest.mark.parametrize('text', ['全文未出现尺寸图。','整篇采用由远到近的图序。','所有配图都没有结构图。','全文未出现尺寸图，但材质无法确认。'])
def test_subset_cannot_support_whole_post_claim(text):
    assert research_quality_errors(payload(text))


@pytest.mark.parametrize('text', ['仅所读3张中未见尺寸图。','不能据此断言全文未出现尺寸图。',
    '整篇图序尚未核实。','其余图片未核实，不判断整篇图序。','建议我们用全景到细节的顺序。','全部图片没有尺寸图的说法无法确认。'])
def test_scoped_observations_caveats_and_own_suggestions_allowed(text):
    assert not research_quality_errors(payload(text))


def test_full_coverage_and_unknown_legacy_coverage():
    data=payload('全文未出现尺寸图。')
    data['visual_analyses'][0]['image_indexes']=list(range(1,7))
    assert not research_quality_errors(data)
    data['visual_analyses'][0].pop('available_image_count')
    assert research_quality_errors(data)


def test_inspected_other_note_cannot_support_whole_claim():
    data=payload('全文未出现尺寸图。')
    data['visual_analyses'][0].update(image_indexes=[1],available_image_count=1,post_url=URL+'-other')
    assert research_quality_errors(data)


def test_photo_total_and_material_prose_both_checked():
    data=payload(count=6)
    assert research_quality_errors(data,limit=3)[0]['field'].endswith('planned_photo_count')
    data['candidate_topics'][0]['planned_photo_count']=3
    data['candidate_topics'][0]['required_materials']=['6至7张现有照片']
    assert research_quality_errors(data,limit=3)[0]['field'].endswith('required_materials.0')
    data['candidate_topics'][0]['required_materials']=['3张现有照片，缺画面就用文字说明。']
    assert not research_quality_errors(data,limit=3)


def test_weekly_distinct_photo_rows_cannot_exceed_supply():
    rows=[{'选题':'案例','图片需求':'柜体整体照片','数量和画面要求':'2张'},
          {'选题':'案例','图片需求':'铰链近照','数量和画面要求':'2张'}]
    assert weekly_quality_errors('所读图片仅作表达参考。',payload(),rows,limit=3)
    rows[0]['数量和画面要求']='1张'
    assert not weekly_quality_errors('所读图片仅作表达参考。',payload(),rows,limit=3)
