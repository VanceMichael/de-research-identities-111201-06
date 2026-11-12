"""实体归并：把多来源采集记录归并到稳定资源号，支持人工确认与可撤销拆分。

不变量：每条采集记录恰好属于一个活跃实体；资源号只增不复用；
人工决定（合并/拆分）作为持久约束参与归并，不会被后续抓取覆盖。
"""
import json
from itertools import combinations

MASK_MARKS = ('*', '?', '？')


class VersionConflict(Exception):
    """人工决定的基准版本已过时：自动更新已先行改变实体。"""

    def __init__(self, current):
        self.current = current
        super().__init__(f"版本冲突，当前版本: {current}")


def usable_serial(serial):
    if serial is None:
        return False
    text = str(serial).strip()
    return bool(text) and not any(mark in text for mark in MASK_MARKS)


def norm_serial(serial):
    return ' '.join(str(serial).split()).casefold()


def alias_map(aliases):
    """old -> (new, effective)，同一旧名取生效时间最新的一条。"""
    result = {}
    for row in aliases:
        old = row['old']
        if old not in result or row['effective'] > result[old][1]:
            result[old] = (row['new'], row['effective'])
    return result


def canonical_institution(name, amap):
    """沿别名链归一机构身份；机构更名不改变其同一性。"""
    seen = set()
    current = name
    while current in amap and current not in seen:
        seen.add(current)
        current = amap[current][0]
    return current


def _union_find(keys):
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    return find, union


def compute_components(records, amap, human_edges, separations):
    """连通分量 = 自动证据（同机构+同型号+同序列号）+ 人工合并边，减去人工隔离。

    返回按首成员排序的分量列表，每个分量携带成员键与置信依据。
    """
    by_key = {r['key']: r for r in records}
    find, union = _union_find(by_key)
    separated = {tuple(sorted((l, r))) for l, r, _ in separations}
    evidence = []
    groups = {}
    for rec in records:
        key = (canonical_institution(rec['institution'], amap), rec['model'])
        groups.setdefault(key, []).append(rec['key'])
    for keys in groups.values():
        for left, right in combinations(sorted(keys), 2):
            s1, s2 = by_key[left].get('serial'), by_key[right].get('serial')
            if not (usable_serial(s1) and usable_serial(s2)) or norm_serial(s1) != norm_serial(s2):
                continue
            if (left, right) in separated:
                continue  # 人工裁定分离，自动证据不生效
            union(left, right)
            evidence.append(dict(type='serial', keys=[left, right], serial=s1))
    for left, right, decision_id in human_edges:
        if left in by_key and right in by_key:
            union(left, right)
            evidence.append(dict(type='human', keys=[left, right], decision=decision_id))
    components = {}
    for key in by_key:
        components.setdefault(find(key), []).append(key)
    result = []
    for members in components.values():
        keys = sorted(members)
        owned = [e for e in evidence if e['keys'][0] in keys and e['keys'][1] in keys]
        result.append(dict(keys=keys, evidence=sorted(owned, key=lambda e: json.dumps(e, sort_keys=True))))
    return sorted(result, key=lambda c: c['keys'][0])


def attribute(keys, by_key, amap):
    """当前机构归属取观察时间最新的成员；晚到的旧快照不会推翻它。"""
    winner = min(keys, key=lambda k: (-int(by_key[k].get('observed', -1)), k))
    raw = by_key[winner]['institution']
    return dict(current=raw, canonical=canonical_institution(raw, amap),
                source=winner, observed=by_key[winner].get('observed'))


def recompute(store, hint=None):
    """按当前记录与约束重算实体，保持稳定资源号；返回变更列表。调用方需持有事务。"""
    records = store.records()
    by_key = {r['key']: r for r in records}
    amap = alias_map(store.aliases())
    components = compute_components(records, amap, store.human_edges(), store.separations())
    active = {e['resource']: e for e in store.entities(status='active')}
    claims = {resource: set() for resource in active}
    for index, comp in enumerate(components):
        keys = set(comp['keys'])
        for resource, entity in active.items():
            if keys & set(entity['members']):
                claims[resource].add(index)
    changes = []
    handled = set()
    for index, comp in enumerate(components):
        keys, evidence = comp['keys'], comp['evidence']
        attribution = attribute(keys, by_key, amap)
        claimants = sorted((r for r in claims if index in claims[r] and r not in handled),
                           key=lambda r: int(r.split('-')[1]))
        if not claimants:
            resource = store.create_entity(keys, evidence, attribution)
            detail = dict(members=keys, evidence=evidence, cause=_cause(hint, keys))
            store.add_entity_event(resource, 1, 'create', detail)
            for key in keys:
                store.add_membership_event(key, resource, 'join', dict(reason=detail['cause']['reason']))
            changes.append(dict(action='create', resource=resource, members=keys))
        elif len(claimants) == 1:
            resource = claimants[0]
            entity = active[resource]
            if entity['members'] != keys or entity['attribution'] != attribution or entity['evidence'] != evidence:
                version = store.update_entity(resource, keys, evidence, attribution)
                added = sorted(set(keys) - set(entity['members']))
                removed = sorted(set(entity['members']) - set(keys))
                store.add_entity_event(resource, version, 'update',
                    dict(members=keys, added=added, removed=removed, evidence=evidence,
                         cause=_cause(hint, keys)))
                for key in added:
                    store.add_membership_event(key, resource, 'join', dict(reason='auto'))
                for key in removed:
                    store.add_membership_event(key, resource, 'leave', dict(reason='auto'))
                changes.append(dict(action='update', resource=resource, version=version,
                                    added=added, removed=removed))
            handled.add(resource)
        else:
            survivor, absorbed = claimants[0], claimants[1:]
            for resource in absorbed:
                entity = active[resource]
                store.add_entity_event(resource, entity['version'], 'retire',
                    dict(reason='absorbed', into=survivor, members=entity['members'],
                         cause=_cause(hint, keys)))
                store.retire_entity(resource)
                for key in entity['members']:
                    store.add_membership_event(key, resource, 'leave',
                        dict(reason='absorbed', into=survivor))
            entity = active[survivor]
            version = store.update_entity(survivor, keys, evidence, attribution)
            store.add_entity_event(survivor, version, 'merge',
                dict(members=keys, absorbed=absorbed, evidence=evidence, cause=_cause(hint, keys)))
            for key in keys:
                if key not in entity['members']:
                    store.add_membership_event(key, survivor, 'join', dict(reason='merge'))
            changes.append(dict(action='merge', resource=survivor, version=version,
                                absorbed=absorbed, members=keys))
            handled.update(claimants)
    return changes


def _cause(hint, keys):
    if hint and hint.get('cause') == 'split' and set(keys) <= set(hint['members']):
        return dict(reason='split', decision=hint['decision'], **{'from': hint['resource']})
    if hint and hint.get('cause') == 'decision':
        return dict(reason='decision', decision=hint['decision'])
    return dict(reason='auto')


def ingest_feed(store, source, raw, records, aliases=()):
    """入库一批名录记录并触发重算；同一事务提交，返回摘要与被拦截的旧快照。"""
    with store.transaction():
        digest, skipped = store.ingest_locked(source, raw, records, aliases)
        changes = recompute(store)
        return dict(digest=digest, skipped=skipped, changes=changes)


def _check_version(expected, involved):
    if expected is None:
        raise ValueError('决定必须携带基准版本 version')
    if isinstance(expected, dict):
        stale = [e for e in involved if e['version'] != expected.get(e['resource'])]
    else:
        stale = [e for e in involved if e['version'] != expected]
    if stale:
        raise VersionConflict([dict(resource=e['resource'], version=e['version']) for e in involved])


def submit_decision(store, decision):
    """应用人工决定（merge/split），带乐观版本检查；冲突时抛 VersionConflict。"""
    dtype = decision.get('type')
    with store.transaction():
        recompute(store)  # 先落定自动状态，再校验版本
        if dtype == 'merge':
            keys = sorted(set(decision.get('sources') or []))
            if len(keys) < 2:
                raise ValueError('合并至少需要两条记录')
            known = {r['key'] for r in store.records()}
            missing = [k for k in keys if k not in known]
            if missing:
                raise ValueError(f"记录不存在: {missing}")
            involved = [e for e in store.entities(status='active') if set(e['members']) & set(keys)]
            _check_version(decision.get('version'), involved)
            decision_id = store.add_decision('merge', dict(
                sources=keys, entities=[e['resource'] for e in involved], version=decision.get('version')))
            store.remove_separations_among(keys)  # 新的人工决定覆盖旧的分离裁定
            store.add_human_edges(list(combinations(keys, 2)), decision_id)
            changes = recompute(store, hint=dict(cause='decision', decision=decision_id))
        elif dtype == 'split':
            resource = decision.get('entity')
            entity = store.entity(resource) if resource else None
            if entity is None or entity['status'] != 'active':
                raise ValueError(f"实体不存在或已退役: {resource}")
            _check_version(decision.get('version'), [entity])
            members = entity['members']
            decision_id = store.add_decision('split', dict(
                entity=resource, members=members, version=decision.get('version')))
            store.remove_human_edges_among(members)  # 撤销形成该实体的人工地合并
            groups = [c['keys'] for c in compute_components(
                [r for r in store.records() if r['key'] in members], alias_map(store.aliases()), [], [])]
            for left_group, right_group in combinations(groups, 2):
                store.add_separations([(a, b) for a in left_group for b in right_group], decision_id)
            store.add_entity_event(resource, entity['version'], 'retire',
                dict(reason='split', decision=decision_id, members=members, groups=groups))
            store.retire_entity(resource)  # 退役不删除：号码保留、历史可查、永不复用
            for key in members:
                store.add_membership_event(key, resource, 'leave', dict(reason='split', decision=decision_id))
            changes = recompute(store, hint=dict(cause='split', decision=decision_id,
                                                 resource=resource, members=members))
        else:
            raise ValueError(f"未知决定类型: {dtype}")
        return dict(decision=decision_id, changes=changes, entities=list_entities(store))


def entity_view(store, entity):
    """实体详情：字段级来源、置信依据与冲突值。"""
    by_key = {r['key']: r for r in store.records()}
    amap = alias_map(store.aliases())
    members = [by_key[k] for k in entity['members'] if k in by_key]
    fields = {}
    for field in ('institution', 'model', 'serial'):
        values, missing = {}, []
        for rec in members:
            value = rec.get(field)
            if value is None or (field == 'serial' and not usable_serial(value)):
                missing.append(rec['key'])
                continue
            values.setdefault(str(value), []).append(rec['key'])
        fields[field] = [dict(value=v, sources=sorted(s)) for v, s in sorted(values.items())]
        if missing:
            fields[field + '_missing'] = sorted(missing)
    conflicts = []
    for field in ('model', 'serial'):
        if len(fields[field]) > 1:
            conflicts.append(dict(field=field, values=fields[field]))
    institutions = {canonical_institution(v['value'], amap) for v in fields['institution']}
    if len(institutions) > 1:
        conflicts.append(dict(field='institution', values=fields['institution']))
    names = {rec['institution'] for rec in members}
    aliases = [a for a in store.aliases() if a['old'] in names or a['new'] in names]
    return dict(resource=entity['resource'], status=entity['status'], version=entity['version'],
                members=entity['members'], institution=entity['attribution'], fields=fields,
                conflicts=conflicts, evidence=entity['evidence'], aliases=aliases)


def list_entities(store, include_retired=True):
    entities = store.entities()
    if not include_retired:
        entities = [e for e in entities if e['status'] == 'active']
    return [entity_view(store, e) for e in entities]


def explain_candidates(store):
    """候选队列：同机构同型号但证据不足或存在冲突的记录对，附解释与当前版本。"""
    records = store.records()
    by_key = {r['key']: r for r in records}
    amap = alias_map(store.aliases())
    key_to_entity = {}
    for entity in store.entities(status='active'):
        for key in entity['members']:
            key_to_entity[key] = entity
    separated = {tuple(sorted((l, r))) for l, r, _ in store.separations()}
    candidates, seen = [], set()

    def entry(left, right, assessment, reason, alias=None):
        pair = tuple(sorted((left, right)))
        if pair in seen:
            return
        seen.add(pair)
        entities = []
        for key in pair:
            entity = key_to_entity.get(key)
            if entity:
                entities.append(dict(resource=entity['resource'], version=entity['version']))
        candidates.append(dict(records=list(pair), model=by_key[left]['model'],
            institution=canonical_institution(by_key[left]['institution'], amap),
            institution_names={k: by_key[k]['institution'] for k in pair},
            alias=alias, serials={k: by_key[k].get('serial') for k in pair},
            assessment=assessment, reason=reason, entities=entities))

    groups = {}
    for rec in records:
        groups.setdefault((canonical_institution(rec['institution'], amap), rec['model']), []).append(rec['key'])
    for (institution, model), keys in groups.items():
        for left, right in combinations(sorted(keys), 2):
            if key_to_entity.get(left) is key_to_entity.get(right):
                continue  # 已在同一实体
            if tuple(sorted((left, right))) in separated:
                continue  # 人工已裁定分离
            s1, s2 = by_key[left].get('serial'), by_key[right].get('serial')
            alias = _alias_between(by_key[left]['institution'], by_key[right]['institution'], store.aliases())
            if usable_serial(s1) and usable_serial(s2):
                if norm_serial(s1) == norm_serial(s2):
                    continue  # 证据充分，已被自动归并
                entry(left, right, 'serial-conflict',
                      f"型号相同但序列号不一致（{s1} ≠ {s2}），倾向不同设备，不自动合并", alias)
            else:
                missing = [k for k, s in ((left, s1), (right, s2)) if not usable_serial(s)]
                entry(left, right, 'insufficient-evidence',
                      f"机构与型号相同，但 {', '.join(missing)} 序列号缺失或已脱敏，证据不足，需人工确认", alias)
    serial_index = {}
    for rec in records:
        if usable_serial(rec.get('serial')):
            serial_index.setdefault((norm_serial(rec['serial']), rec['model']), []).append(rec['key'])
    for (serial, model), keys in serial_index.items():
        for left, right in combinations(sorted(keys), 2):
            if canonical_institution(by_key[left]['institution'], amap) == \
                    canonical_institution(by_key[right]['institution'], amap):
                continue  # 同机构序列号一致已自动归并
            if key_to_entity.get(left) is key_to_entity.get(right):
                continue
            if tuple(sorted((left, right))) in separated:
                continue
            entry(left, right, 'possible-relocation',
                  f"序列号一致（{serial}）但机构不同，可能为设备迁址，需人工确认")
    return sorted(candidates, key=lambda c: c['records'])


def _alias_between(name1, name2, aliases):
    for row in aliases:
        if {row['old'], row['new']} == {name1, name2}:
            return row
    return None


def record_history(store, key):
    """记录的资源号映射史：误合并撤销后可据此解释每次归属变化。"""
    return store.membership_history(key)


def integrity_check(store):
    """证明采集记录没有丢失或重复计数：每条记录恰好出现在一个活跃实体中。"""
    records = store.records()
    active = store.entities(status='active')
    membership = [key for entity in active for key in entity['members']]
    known = {r['key'] for r in records}
    return dict(records=len(records), active_entities=len(active),
                memberships=len(membership), unique_members=len(set(membership)),
                missing=sorted(known - set(membership)),
                unknown=sorted(set(membership) - known),
                ok=sorted(membership) == sorted(known) and len(membership) == len(set(membership)))
