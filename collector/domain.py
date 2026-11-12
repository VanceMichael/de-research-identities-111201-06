import unicodedata


def normalize_label(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split()).casefold()


def source_identity(source, identifier):
    if not source or not identifier:
        raise ValueError('来源与本地标识不能为空')
    return f'{source}:{identifier}'


def collect(payload):
    result = []
    for row in payload['records']:
        revision = int(row['revision'])
        if revision < 1:
            raise ValueError('来源修订号必须为正')
        result.append(dict(key=source_identity(row['source'], row['id']),
            institution=normalize_label(row['institution']), equipment=normalize_label(row['equipment']),
            model=normalize_label(row['model']), serial=row.get('serial'),
            revision=revision, observed=row['observed']))
    return result


def collect_resource_feed(payload):
    """适配资源共享中心名录：记录、机构别名（含生效时间）与人工决定三段式输入。"""
    records = []
    for row in payload.get('records', []):
        observed = int(row['observed'])
        if observed < 0:
            raise ValueError('观察时间不能为负')
        records.append(dict(key=source_identity(row['source'], str(row['id'])),
            institution=normalize_label(row['institution']), model=normalize_label(row['model']),
            serial=row.get('serial'), observed=observed))
    aliases = []
    for row in payload.get('aliases', []):
        aliases.append(dict(old=normalize_label(row['old']), new=normalize_label(row['new']),
            effective=int(row['effective'])))
    decisions = []
    for row in payload.get('decisions', []):
        decision = dict(row)
        if decision.get('type') not in ('merge', 'split'):
            raise ValueError(f"未知决定类型: {decision.get('type')}")
        decisions.append(decision)
    return records, aliases, decisions
