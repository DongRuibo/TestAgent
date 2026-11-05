"""Narrow checks for observed evidence/material failures; no model or provider calls."""
import re
from urllib.parse import urlsplit

NUMBER = r"(?:\d+|[零一二两三四五六七八九十]{1,3})"
QUANTITY = re.compile(rf"(?P<low>{NUMBER})(?:\s*(?:至|到|[-—~～])\s*(?P<high>{NUMBER}))?\s*张")
PHOTO = re.compile(QUANTITY.pattern + r"[^，。；\n]{0,8}?(?:照片|图片|图(?!文))")


def integer(value):
    if value.isdigit():
        return int(value)
    digits = dict(zip('零一二三四五六七八九', range(10))) | {'两': 2}
    if '十' in value:
        left, right = value.split('十')
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(value)


def photo_limit(notes):
    """Only an explicit cap, or one unambiguous supply count plus no reshoot, is a cap."""
    if not isinstance(notes, str):
        return None
    no_reshoot = bool(re.search(r'不(?:再|用|需要)?补拍|不能补拍', notes))
    caps = []
    candidates = []
    for match in PHOTO.finditer(notes):
        prefix = notes[max(0, match.start()-12):match.start()]
        if re.search(r'参考|笔记|每篇|看图|读取|分析|至少|不少于|起码', prefix):
            continue
        amount = integer(match['high'] or match['low'])
        if amount is None:
            continue
        candidates.append(amount)
        if re.search(r'(?:只有|仅有|最多|不超过|只用|就这|总共|一共)\s*$', prefix):
            caps.append(amount)
    if caps and len(set(caps)) == 1:
        return caps[0]
    if not caps and no_reshoot and len(candidates) == 1:
        return candidates[0]
    # E.g. multiple people supply photos, or contradictory old/new amounts: don't add or guess.
    return None


def quantity_values(text):
    return [integer(m['high'] or m['low']) for m in QUANTITY.finditer(text)
            if integer(m['high'] or m['low']) is not None]


WHOLE_IMAGE = re.compile(r'(?:全文|全篇|整篇|全部(?:图片|配图)|所有(?:图片|配图))[^。；，\n|]{0,35}'
    r'(?:未出现|没有|均无|都没有|不含|图序|顺序|由远到近|从远到近|尺寸图|结构图)')
CAUTION = re.compile(r'不能|无法|不足以|不应|不要|未能|不可|尚不|不代表|不等于|尚未核实|不判断|不评价|不推断')


def whole_image_claim(text):
    """Do not reject explicit caveats like '不能断言全文未出现尺寸图'."""
    for clause in re.split(r'[。；\n|]', text):
        for match in WHOLE_IMAGE.finditer(clause):
            before = clause[:match.start()].rsplit('，', 1)[-1]
            after = clause[match.end():].split('，', 1)[0]
            if not CAUTION.search(before) and not CAUTION.search(after) and not re.search(r'建议我们|本计划建议', before):
                return True
    return False


def image_coverage_complete(receipts):
    return bool(receipts) and all(r.get('status') == 'success' and r.get('available_image_count') is not None
        and set(r.get('image_indexes', [])) == set(range(1, r['available_image_count']+1)) for r in receipts)


def research_quality_errors(payload, *, limit=None):
    """New-save checks only; old artifacts stay readable and are never silently rewritten."""
    errors = []
    if not isinstance(payload, dict):
        return errors
    receipts = payload.get('visual_analyses', [])
    source_posts = {s.get('source_id'): urlsplit(s.get('url', '')).path.rstrip('/')
        for s in payload.get('source_references', []) if isinstance(s, dict)}

    def check_scope(text, field, source_refs=None):
        if not isinstance(text, str) or not whole_image_claim(text):
            return
        posts = {source_posts.get(ref) for ref in source_refs or []}
        relevant = [r for r in receipts if urlsplit(r.get('post_url','')).path.rstrip('/') in posts] if source_refs else receipts
        covered_posts = {urlsplit(r.get('post_url','')).path.rstrip('/') for r in relevant}
        if not image_coverage_complete(relevant) or (posts and not posts <= covered_posts):
            errors.append({'field': field, 'message': '只核实了部分图片或覆盖范围未知，不能断言整篇图序或全篇没有某类图片；请改成所读图号中的观察。'})

    check_scope(payload.get('research_summary'), 'research_summary')
    for group in ('key_findings','user_needs','competitor_findings','platform_findings'):
        for i, finding in enumerate(payload.get(group, [])):
            if isinstance(finding, dict):
                check_scope(finding.get('statement'), f'{group}.{i}.statement', finding.get('source_refs'))
    for i, topic in enumerate(payload.get('candidate_topics', [])):
        if not isinstance(topic, dict):
            continue
        for j, evidence in enumerate(topic.get('evidence', [])):
            if isinstance(evidence, dict):
                check_scope(evidence.get('fact'), f'candidate_topics.{i}.evidence.{j}.fact', evidence.get('source_refs'))
        refs = [ref for e in topic.get('evidence', []) if isinstance(e,dict) for ref in e.get('source_refs',[])]
        for field in ('why_fit_company','visual_borrowing'):
            check_scope(topic.get(field), f'candidate_topics.{i}.{field}', refs)
        if limit is not None:
            count = topic.get('planned_photo_count')
            if type(count) is not int or not 0 <= count <= limit:
                errors.append({'field': f'candidate_topics.{i}.planned_photo_count',
                    'message': f'用户本轮最多使用{limit}张现有照片；请填写0至{limit}的实际所需照片总数，并按此缩小选题，不把补拍作为前提。'})
            for j, material in enumerate(topic.get('required_materials', [])):
                if isinstance(material, str) and any(n > limit for n in quantity_values(material)):
                    errors.append({'field': f'candidate_topics.{i}.required_materials.{j}',
                        'message': f'素材文字要求超过本轮{limit}张照片上限，请缩减到实际可提供范围；额外素材仅可写入未来建议或风险。'})
    return errors


def weekly_quality_errors(markdown, research, material_rows, *, limit=None):
    errors = []
    # Unstructured plan prose cannot reliably associate clauses to one source: require bounded wording.
    if whole_image_claim(markdown) and not image_coverage_complete(research.get('visual_analyses', [])):
        errors.append('参考图片未完整核实，不能断言整篇图序或全篇没有某类图片；请改成所读图片的观察。')
    if limit is not None:
        per_topic = {}
        for row in material_rows:
            if re.search(r'可选|复用|同一张', row.get('数量和画面要求','')):
                continue
            if not re.search(r'照片|近照|实拍|全景照|细节照', row.get('图片需求','')):
                continue
            quantities = quantity_values(row.get('数量和画面要求',''))
            if quantities:
                # Rows represent distinct materials; an upper range bounds planned demand.
                per_topic[row.get('选题','')] = per_topic.get(row.get('选题',''),0) + max(quantities)
        for topic, count in per_topic.items():
            if count > limit:
                errors.append(f'{topic}的照片需求合计{count}张，超过用户明确的{limit}张上限；请缩减素材或角度，不依赖补拍。')
    return errors
