import re

from apps.oa.materials.resources import image_locations


_NAME = r"[^\W\d_]+?(?:[ ·•'’-][^\W\d_]+?)*?"
_NAME_END = r'(?=\s*(?:[，,。；;！？!?、：:（）()|\n]|(?:与|和|而非)报销人|$))'
_UNKNOWN_NAME = re.compile(r'未知|未见|未显示|无姓名|缺失|不清|模糊|昵称|缩写|待核|无法|不明|不详|未确认|疑似|可能')
_NAME_QUALIFIER = re.compile(r'\s*[（(]([^）)]*)[）)]')
_RELATIVE_UPLOADER = {'本人', '报销人本人', '申请人本人', '其本人', '他人', '其他人', '第三方', '报销人', '申请人', '同一人', '非本人'}
_WATERMARK_NAME = re.compile(rf'(?:^|[：:\n])\s*({_NAME})\s*[·•]\s*油补里程上报')
_UPLOADER_NAME = re.compile(rf'上传[人者](?:姓名)?\s*(?:[:：]|(?:均|分别|识别|显示|登记|记载)?为)\s*({_NAME}){_NAME_END}')
_UPLOADED_BY = re.compile(rf'由\s*({_NAME})\s*上传{_NAME_END}')
_OIL_PHOTO = re.compile(r'油补(?:里程)?照片|里程照片')
_OIL_HEADING = re.compile(r'^\s*(?:[✅❌⚠️]+\s*)?(?:6[、.．]\s*)?油补\s*[:：]')
_REVIEW_ITEM = re.compile(r'^\s*(?:[✅❌⚠️]+\s*)?\d+[、.．]')
_DIFFERENCE = re.compile(r'不一致|不相符|不符|不相同|不同|不匹配|有(?:差异|出入)|不是同一人')
_CLAIMANT_COMPARISON = re.compile(
    rf'(?:与|和)报销人(?:姓名)?\s*(?:为|[:：])?\s*({_NAME})?\s*(?:姓名)?(?:{_DIFFERENCE.pattern})')
_UPLOADER_COMPARISON = re.compile(rf'(?:上传[人者]|姓名).*?(?:{_DIFFERENCE.pattern})')
_NON_NAME_COMPARISON = re.compile(r'日期|时间|期间|周期|区间|行程|里程数|照片归属')
_PROXY_UPLOAD = re.compile(r'代(?:为)?上传|代传')
_NEGATED_PROXY = re.compile(r'(?:未见|未发现|没有|不涉及|并非|无)(?:代(?:为)?上传|代传)')


def _name(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value not in _RELATIVE_UPLOADER and re.fullmatch(_NAME, value) and not _UNKNOWN_NAME.search(value):
        return value
    return None


def _matched_names(pattern, text):
    names = set()
    for match in pattern.finditer(text):
        name = _name(match[1] or '')
        qualifier = _NAME_QUALIFIER.match(text, match.end())
        if (qualifier and _UNKNOWN_NAME.search(qualifier[1])
                and ('姓名' in qualifier[1] or not _NON_NAME_COMPARISON.search(qualifier[1]))):
            continue
        if name:
            names.add(name)
    return names


def _evidence_names(content, *, image):
    content = content.replace('**', '').replace('`', '')
    names = _matched_names(_WATERMARK_NAME, content) if image else set()
    if _OIL_PHOTO.search(content):
        names.update(_matched_names(_UPLOADER_NAME, content) | _matched_names(_UPLOADED_BY, content))
    return names


def _bound_uploaders(material, recognition, observations):
    images = {}
    evidence = {}
    for location, page in image_locations((material.get('prepared') or {}).get('images', [])):
        for resource in {location['resource_id'], page.get('resource_id')} - {None, ''}:
            images.setdefault(resource, []).append(location['evidence_ref'])
        evidence[location['evidence_ref']] = set()
    for part in (recognition or {}).get('parts', []):
        if part['evidence_ref'] in evidence:
            evidence[part['evidence_ref']].update(_evidence_names(part['answer'], image=True))
        elif part['kind'] == 'text' and '上传记录' in part['answer']:
            evidence[part['evidence_ref']] = _evidence_names(part['answer'], image=False)

    resources = {resource.get('resource_id') for resource in material.get('resources', [])}
    for observation in observations or []:
        resource, locator, content = (observation.get(key) for key in ('resource_id', 'locator', 'content'))
        if not all(isinstance(value, str) and value.strip() for value in (resource, locator, content)):
            continue
        if observation.get('status', 'read') != 'read':
            continue
        pages = images.get(resource, [])
        page = locator.split('#', 1)[0]
        if page not in pages:
            page = pages[0] if len(pages) == 1 else None
        if page is not None:
            evidence[page].update(_evidence_names(content, image=True))
        elif resource in resources and not pages and '上传记录' in content:
            evidence.setdefault((resource, locator), set()).update(_evidence_names(content, image=False))
    return {next(iter(names)) for names in evidence.values() if len(names) == 1}


def _oil_sentences(opinion):
    in_oil_item = False
    for line in opinion.replace('**', '').replace('`', '').splitlines():
        if _OIL_HEADING.match(line):
            in_oil_item = True
        elif _REVIEW_ITEM.match(line):
            in_oil_item = False
        if in_oil_item or _OIL_PHOTO.search(line):
            yield from re.split(r'[。；;！？!?]', line)


def validate_uploader_opinion(material, recognition, observations, opinion):
    sentences = list(_oil_sentences(opinion))
    if not sentences:
        return
    claimants = {name for field in material.get('declared_fields', [])
                 if field.get('name') == '报销人' and str(field.get('path') or '').startswith('main[')
                 and field.get('read_status', 'present') == 'present'
                 and (name := _name(field.get('value', '')))}
    claimant = next(iter(claimants)) if len(claimants) == 1 else None
    uploaders = _bound_uploaders(material, recognition, observations)

    for sentence in sentences:
        reported = _matched_names(_UPLOADER_NAME, sentence) | _matched_names(_UPLOADED_BY, sentence)
        if reported - uploaders:
            raise ValueError('travel_mileage_uploader_conflict')
        name_clauses = [_NON_NAME_COMPARISON.split(clause, maxsplit=1)[0]
                        for clause in re.split(r'[，,]', sentence)]
        claimant_comparisons = [clause for clause in name_clauses if _CLAIMANT_COMPARISON.search(clause)]
        flags_difference = (any(_UPLOADER_COMPARISON.search(clause) for clause in name_clauses)
                            or (reported and claimant_comparisons)
                            or _PROXY_UPLOAD.search(_NEGATED_PROXY.sub('', sentence)))
        if flags_difference and (not claimant or not reported - {claimant}
                                 or any(_matched_names(_CLAIMANT_COMPARISON, clause) - {claimant}
                                        for clause in claimant_comparisons)):
            raise ValueError('travel_mileage_uploader_conflict')
