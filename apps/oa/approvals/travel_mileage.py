import re
from dataclasses import dataclass, field

from apps.oa.materials.resources import image_locations


_NAME = r"[^\W\d_]+?(?:[ ·•'’-][^\W\d_]+?)*?"
_NAME_END = r'(?=\s*(?:[，,。；;！？!?、：:（）()|\n]|(?:与|和|而非)报销人|$))'
_UNKNOWN_NAME = re.compile(r'未知|未见|未显示|无姓名|缺失|不清|模糊|昵称|缩写|待核|无法|不明|不详|未确认|疑似|可能')
_ALTERNATIVE_NAMES = re.compile(r'[^\W\d_]或(?:者)?[^\W\d_]')
_NAME_QUALIFIER = re.compile(r'\s*[（(]([^）)]*)[）)]')
_RELATIVE_UPLOADER = {'本人', '报销人本人', '申请人本人', '其本人', '他人', '其他人', '第三方', '报销人', '申请人', '同一人', '非本人'}
_WATERMARK_NAME = re.compile(rf'(?:^|[：:\n])\s*({_NAME})\s*[·•]\s*油补里程上报')
_UPLOADER_NAME = re.compile(rf'上传[人者](?:姓名)?\s*(?:[:：]|(?:均|分别|识别|显示|登记|记载)?为)\s*({_NAME}){_NAME_END}')
_UPLOADED_BY = re.compile(rf'由\s*({_NAME})\s*上传{_NAME_END}')
_OIL_PHOTO = re.compile(r'油补(?:里程)?照片|里程照片')
_OIL_UPLOADER = re.compile(rf'(?:{_OIL_PHOTO.pattern})(?:的)?(?:水印|上传记录)?[，,：:\s]*'
                           rf'(?:{_UPLOADER_NAME.pattern}|{_UPLOADED_BY.pattern})')
_WATERMARK_CLAIM = re.compile(rf'照片(?:的)?水印(?:显示|姓名[:：]|姓名为)\s*({_NAME}){_NAME_END}')
_SENTENCE = re.compile(r'([^。；;！？!?\n]+)([。；;！？!?\n]?)')
_CLAUSE = re.compile(r'[，,、]|且')
_LEGACY_PAGE = re.compile(r'^\s*第\s*(\d+)\s*页')
_PAGE_REF = re.compile(r'material:prepared\.images\.[^\s，,。；;！？!?、）)#]+\.\d+')
_PHOTO_DATE = re.compile(r'(?<!\d)(?:(\d{4})[年/.-])?(\d{1,2})月(\d{1,2})日'
                         r'|(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)')
_OIL_HEADING = re.compile(r'^\s*(?:[✅❌⚠️]+\s*)?(?:6[、.．]\s*)?油补\s*[:：]')
_REVIEW_ITEM = re.compile(r'^\s*(?:[✅❌⚠️]+\s*)?\d+[、.．]')
_DIFFERENCE = re.compile(r'不一致|不相符|不符|不相同|不同|不匹配|有(?:差异|出入)|不是同一人')
_CLAIMANT_COMPARISON = re.compile(
    rf'(?:与|和)报销人(?:姓名)?\s*(?:为|[:：])?\s*({_NAME})?\s*(?:姓名)?(?:{_DIFFERENCE.pattern})')
_UPLOADER_COMPARISON = re.compile(rf'(?:上传[人者]|姓名).*?(?:{_DIFFERENCE.pattern}|(?:不是|并非)报销人)')
_NON_NAME_COMPARISON = re.compile(r'拍摄日|日期|时间|月份|所属月|期间|周期|区间|行程|里程数|照片归属')
_NAME_DOUBT = re.compile(r'看不清|字迹不清|模糊|猜测|疑似|可能|无法(?:确认|确定|辨认|识别)|未(?:显示|见到)姓名')
_NAME_DOUBT_FOLLOWUP = re.compile(r'^\s*(?:水印|姓名|上传[人者])')
_PROXY_UPLOAD = re.compile(r'代(?:为)?上传|代传')
_NEGATED_PROXY = re.compile(r'(?:未见|未发现|没有|不涉及|并非|无)(?:代(?:为)?上传|代传)')


def _name(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (value not in _RELATIVE_UPLOADER and re.fullmatch(_NAME, value)
            and not _UNKNOWN_NAME.search(value) and not _ALTERNATIVE_NAMES.search(value)):
        return value
    return None


def _matched_names(pattern, text):
    names = set()
    for match in pattern.finditer(text):
        name = _name(next((value for value in match.groups() if value), ''))
        qualifier = _NAME_QUALIFIER.match(text, match.end())
        if (qualifier and _UNKNOWN_NAME.search(qualifier[1])
                and ('姓名' in qualifier[1] or not _NON_NAME_COMPARISON.search(qualifier[1]))):
            continue
        if name:
            names.add(name)
    return names


@dataclass
class _PhotoReading:
    names: set[str] = field(default_factory=set)
    dates: set[tuple[int, int, int]] = field(default_factory=set)


def _dates(text):
    return {(int(year or iso_year or 0), int(month or iso_month), int(day or iso_day))
            for year, month, day, iso_year, iso_month, iso_day in _PHOTO_DATE.findall(text)}


def _clauses(sentence):
    clauses = []
    for clause in _CLAUSE.split(sentence):
        if clauses and _PHOTO_DATE.fullmatch(clauses[-1].strip()):
            clauses[-1] += ' ' + clause
        else:
            clauses.append(clause)
    return clauses


def _name_doubt(clauses):
    return any(_NAME_DOUBT.search(_NON_NAME_COMPARISON.split(clause, maxsplit=1)[0]) for clause in clauses)


def _read_photo(content, *, image):
    reading = _PhotoReading()
    content = content.replace('**', '').replace('`', '')
    patterns = (_OIL_UPLOADER, _WATERMARK_NAME) if image else (_OIL_UPLOADER,)
    statements = _SENTENCE.findall(content) + [('', '')]
    for (sentence, ending), (following, _) in zip(statements, statements[1:]):
        if ending in {'!', '?', '！', '？'}:
            continue
        clauses = _clauses(sentence)
        if _name_doubt(clauses) or (_NAME_DOUBT_FOLLOWUP.match(following) and _name_doubt(_clauses(following))):
            continue
        names = set().union(*(_matched_names(pattern, sentence) for pattern in patterns))
        if not names:
            continue
        reading.names.update(names)
        dated = [clause for clause in clauses if any(pattern.search(clause) for pattern in patterns)
                 or '里程照片上传记录' in clause or '油补照片上传记录' in clause]
        dates = set().union(*(_dates(clause) for clause in dated))
        if len(dates) == 1:
            reading.dates.update(dates)
    return reading


def _bound_uploaders(material, recognition, observations):
    images = {}
    pages = set()
    evidence = {}

    def add(ref, content, *, image):
        observed = _read_photo(content, image=image)
        if observed.names or _OIL_PHOTO.search(content):
            reading = evidence.setdefault(ref, _PhotoReading())
            reading.names.update(observed.names)
            reading.dates.update(observed.dates)

    for location, page in image_locations((material.get('prepared') or {}).get('images', [])):
        for resource in {location['resource_id'], page.get('resource_id')} - {None, ''}:
            images.setdefault(resource, []).append(location['evidence_ref'])
        pages.add(location['evidence_ref'])
    for part in (recognition or {}).get('parts', []):
        if part['evidence_ref'] in pages:
            add(part['evidence_ref'], part['answer'], image=True)
        elif part['kind'] == 'text' and '上传记录' in part['answer']:
            add(part['evidence_ref'], part['answer'], image=False)

    resources = {resource.get('resource_id') for resource in material.get('resources', [])}
    for observation in observations or []:
        resource, locator, content = (observation.get(key) for key in ('resource_id', 'locator', 'content'))
        if not all(isinstance(value, str) and value.strip() for value in (resource, locator, content)):
            continue
        if observation.get('status', 'read') != 'read':
            continue
        resource_pages = images.get(resource, [])
        page = locator.split('#', 1)[0]
        if page in resource_pages:
            add(page, content, image=True)
        elif len(resource_pages) == 1 and not page.startswith('material:'):
            number = _LEGACY_PAGE.match(page)
            if not number or int(number[1]) in {1, int(resource_pages[0].rsplit('.', 1)[1])}:
                add(resource_pages[0], content, image=True)
        elif (resource in resources and not resource_pages and '上传记录' in content
              and not page.startswith('material:prepared.images.')):
            add((resource, locator), content, image=False)
    return evidence


def _photo_names(evidence, clause, sentence_refs):
    refs = _PAGE_REF.findall(clause) or sentence_refs
    readings = [reading for ref, reading in evidence.items() if not refs or ref in refs]
    dates = _dates(clause)
    if len(refs) > 1 or len(dates) > 1:
        return set()
    if dates and any(reading.dates for reading in readings):
        year, month, day = next(iter(dates))
        readings = [reading for reading in readings if any(
            (month, day) == (read_month, read_day) and (not year or not read_year or year == read_year)
            for read_year, read_month, read_day in reading.dates)]
    if not readings or any(len(reading.names) != 1 for reading in readings):
        return set()
    names = set.union(*(reading.names for reading in readings))
    return names if len(names) == 1 else set()


def _oil_sentences(opinion):
    in_oil_item = False
    for line in opinion.replace('**', '').replace('`', '').splitlines():
        heading = _OIL_HEADING.match(line)
        if heading:
            in_oil_item = True
            line = line[heading.end():]
        elif _REVIEW_ITEM.match(line):
            in_oil_item = False
        if in_oil_item or _OIL_PHOTO.search(line):
            yield from (sentence for sentence, _ in _SENTENCE.findall(line))
        else:
            yield ''


def validate_uploader_opinion(material, recognition, observations, opinion):
    sentences = list(_oil_sentences(opinion))
    if not any(sentences):
        return
    claimants = {name for field in material.get('declared_fields', [])
                 if field.get('name') == '报销人' and str(field.get('path') or '').startswith('main[')
                 and field.get('read_status', 'present') == 'present'
                 and (name := _name(field.get('value', '')))}
    claimant = next(iter(claimants)) if len(claimants) == 1 else None
    uploaders = _bound_uploaders(material, recognition, observations)

    previous_difference = False
    for sentence in sentences:
        clauses = _clauses(sentence)
        comparison_clauses = [_NON_NAME_COMPARISON.split(clause, maxsplit=1)[0] for clause in clauses]
        claimant_comparisons = [clause for clause in comparison_clauses if _CLAIMANT_COMPARISON.search(clause)]
        refs = _PAGE_REF.findall(sentence)
        patterns = (_UPLOADER_NAME, _UPLOADED_BY, _WATERMARK_CLAIM) if claimant_comparisons else (_UPLOADER_NAME, _UPLOADED_BY)
        reported = set()
        for clause in clauses:
            names = set().union(*(_matched_names(pattern, clause) for pattern in patterns))
            if names and names - _photo_names(uploaders, clause, refs):
                raise ValueError('travel_mileage_uploader_conflict')
            reported.update(names)

        differs = claimant is not None and len(reported) == 1 and claimant not in reported
        compares_uploader = any(_UPLOADER_COMPARISON.search(clause) for clause in comparison_clauses)
        names_uploader = any(pattern.search(clause) for clause in clauses for pattern in patterns)
        if (compares_uploader or (claimant_comparisons and names_uploader)) and (
                not differs or any(_matched_names(_CLAIMANT_COMPARISON, clause) - {claimant}
                                   for clause in claimant_comparisons)):
            raise ValueError('travel_mileage_uploader_conflict')
        continued = previous_difference and not (reported or _dates(sentence) or refs)
        if _PROXY_UPLOAD.search(_NEGATED_PROXY.sub('', sentence)) and not (differs or continued):
            raise ValueError('travel_mileage_uploader_conflict')
        previous_difference = differs
