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
