import json
import socket
import threading
import unittest
from pathlib import Path
import tempfile

from collector import resolution
from collector.api import serve
from collector.domain import collect_resource_feed
from collector.resolution import VersionConflict
from collector.storage import Store
from collector.transport import Client

FIXTURE = json.loads(Path('fixtures/resources.json').read_text())


def feed(payload=None):
    return collect_resource_feed(payload if payload is not None else FIXTURE)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'store.sqlite3')

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def ingest_fixture(self):
        records, aliases, _ = feed()
        return resolution.ingest_feed(self.store, '共享中心', json.dumps(FIXTURE).encode(), records, aliases)

    def entity(self, resource):
        return self.store.entity(resource)

    def active(self):
        return {e['resource']: e for e in self.store.entities(status='active')}

    def test_rename_alias_offers_candidate_not_auto_merge(self):
        self.ingest_fixture()
        active = self.active()
        # 三条记录三个实体：更名只靠别名不能自动合并，序列号缺失属证据不足
        self.assertEqual(len(active), 3)
        self.assertEqual(active['resource-1']['members'], ['a:7'])
        self.assertEqual(active['resource-2']['members'], ['a:9'])
        self.assertEqual(active['resource-3']['members'], ['b:2'])
        candidates = resolution.explain_candidates(self.store)
        pair = next(c for c in candidates if c['records'] == ['a:7', 'b:2'])
        self.assertEqual(pair['assessment'], 'insufficient-evidence')
        self.assertEqual(pair['institution'], '北港研究院')  # 旧名经别名归一
        self.assertEqual(pair['alias'], {'old': '北港研究所', 'new': '北港研究院', 'effective': 15})
        self.assertEqual(pair['entities'],
                         [{'resource': 'resource-1', 'version': 1}, {'resource': 'resource-3', 'version': 1}])

    def test_same_model_different_serials_stay_separate(self):
        self.ingest_fixture()
        pair = next(c for c in resolution.explain_candidates(self.store) if c['records'] == ['a:7', 'a:9'])
        self.assertEqual(pair['assessment'], 'serial-conflict')
        # 重复抓取不合并、不重复计数
        self.ingest_fixture()
        self.assertEqual(len(self.active()), 3)
        check = resolution.integrity_check(self.store)
        self.assertTrue(check['ok'])
        self.assertEqual(check['memberships'], 3)

    def test_merge_then_attribution_uses_latest_observation(self):
        self.ingest_fixture()
        resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        merged = self.entity('resource-1')
        self.assertEqual(merged['members'], ['a:7', 'b:2'])
        self.assertEqual(merged['version'], 2)
        self.assertEqual(merged['attribution']['current'], '北港研究院')  # 观察时间最新者胜出
        self.assertEqual(self.entity('resource-3')['status'], 'retired')  # 被吸收方退役不删除
        view = resolution.entity_view(self.store, merged)
        serial = next(f for f in view['fields']['serial'] if f['value'] == 'X7')
        self.assertEqual(serial['sources'], ['a:7'])  # 字段级来源
        self.assertEqual(view['fields']['serial_missing'], ['b:2'])
        self.assertEqual(view['conflicts'], [])

    def test_late_old_snapshot_does_not_overturn_attribution(self):
        self.ingest_fixture()
        resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        stale = dict(FIXTURE)
        stale['records'] = [dict(r) for r in FIXTURE['records']]
        stale['records'][0]['observed'] = 5  # 晚到的旧快照，机构仍是旧名
        records, aliases, _ = feed(stale)
        outcome = resolution.ingest_feed(self.store, '共享中心', json.dumps(stale).encode(), records, aliases)
        self.assertEqual(outcome['skipped'], ['a:7'])  # 旧快照被拦截，原文仍登记
        merged = self.entity('resource-1')
        self.assertEqual(merged['attribution']['current'], '北港研究院')
        self.assertEqual(merged['version'], 2)  # 归属与版本均未被动摇
        self.assertEqual(self.store.provenance('a:7')['source'], '共享中心')

    def test_human_merge_survives_recrawl(self):
        self.ingest_fixture()
        resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        events_before = len(self.store.entity_events())
        self.ingest_fixture()  # 下一次抓取
        self.assertEqual(self.entity('resource-1')['members'], ['a:7', 'b:2'])
        self.assertEqual(self.entity('resource-1')['version'], 2)
        self.assertEqual(len(self.store.entity_events()), events_before)  # 无变化不产生新事件

    def test_split_undoes_merge_explainably(self):
        self.ingest_fixture()
        resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        resolution.submit_decision(self.store, {'type': 'split', 'entity': 'resource-1', 'version': 2})
        # 误合并实体退役但行保留，成员获得新号码，号码不复用
        retired = self.entity('resource-1')
        self.assertEqual(retired['status'], 'retired')
        active = self.active()
        self.assertEqual(active['resource-4']['members'], ['a:7'])
        self.assertEqual(active['resource-5']['members'], ['b:2'])
        self.assertNotIn('resource-1', active)
        # 映射史可解释：a:7 从 resource-1 拆回 resource-4
        history = resolution.record_history(self.store, 'a:7')
        self.assertEqual([(h['resource'], h['action']) for h in history],
                         [('resource-1', 'join'), ('resource-1', 'leave'), ('resource-4', 'join')])
        self.assertEqual(history[1]['detail']['reason'], 'split')
        # 事件流同样完整
        actions = [(e['resource'], e['action']) for e in self.store.entity_events()]
        self.assertIn(('resource-1', 'merge'), actions)
        self.assertIn(('resource-1', 'retire'), actions)
        # 无丢失、无重复计数
        check = resolution.integrity_check(self.store)
        self.assertTrue(check['ok'])
        self.assertEqual((check['records'], check['active_entities'], check['memberships']), (3, 3, 3))

    def test_split_survives_recrawl_even_with_matching_serial(self):
        self.ingest_fixture()
        resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        resolution.submit_decision(self.store, {'type': 'split', 'entity': 'resource-1', 'version': 2})
        # 下一次抓取中 b:2 补上了与 a:7 相同的序列号：人工分离裁定仍然有效
        update = dict(FIXTURE)
        update['records'] = [dict(r) for r in FIXTURE['records']]
        update['records'][1]['serial'] = 'X7'
        update['records'][1]['observed'] = 21
        records, aliases, _ = feed(update)
        resolution.ingest_feed(self.store, '共享中心', json.dumps(update).encode(), records, aliases)
        active = self.active()
        self.assertEqual(active['resource-4']['members'], ['a:7'])
        self.assertEqual(active['resource-5']['members'], ['b:2'])
        pairs = [c['records'] for c in resolution.explain_candidates(self.store)]
        self.assertNotIn(['a:7', 'b:2'], pairs)  # 已裁定对不再进入候选队列

    def test_version_conflict_between_auto_update_and_human_decision(self):
        self.ingest_fixture()
        # 自动更新先到：同序列号新记录并入 resource-1
        extra = dict(FIXTURE)
        extra['records'] = [dict(r) for r in FIXTURE['records']] + [
            dict(source='a', id='8', institution='北港研究院', model='M1', serial='X7', observed=11)]
        records, aliases, _ = feed(extra)
        resolution.ingest_feed(self.store, '共享中心', json.dumps(extra).encode(), records, aliases)
        self.assertEqual(self.entity('resource-1')['version'], 2)
        self.assertEqual(self.entity('resource-1')['members'], ['a:7', 'a:8'])
        # 人工仍拿着旧版本提交合并：被拒绝并告知当前版本
        with self.assertRaises(VersionConflict) as raised:
            resolution.submit_decision(self.store, {'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1})
        self.assertIn({'resource': 'resource-1', 'version': 2}, raised.exception.current)
        # 重新获取版本后按各实体当前版本重试成功
        result = resolution.submit_decision(self.store, {
            'type': 'merge', 'sources': ['a:7', 'b:2'],
            'version': {'resource-1': 2, 'resource-3': 1}})
        self.assertEqual(self.entity('resource-1')['members'], ['a:7', 'a:8', 'b:2'])
        self.assertTrue(resolution.integrity_check(self.store)['ok'])

    def test_masked_serial_is_insufficient_evidence(self):
        payload = dict(FIXTURE)
        payload['records'] = [dict(r) for r in FIXTURE['records']]
        payload['records'][1]['serial'] = '***'  # 脱敏序列号
        records, aliases, _ = feed(payload)
        resolution.ingest_feed(self.store, '共享中心', json.dumps(payload).encode(), records, aliases)
        self.assertEqual(len(self.active()), 3)
        pair = next(c for c in resolution.explain_candidates(self.store) if c['records'] == ['a:7', 'b:2'])
        self.assertEqual(pair['assessment'], 'insufficient-evidence')

    def test_fixture_decisions_run_end_to_end(self):
        records, aliases, decisions = feed()
        resolution.ingest_feed(self.store, '共享中心', json.dumps(FIXTURE).encode(), records, aliases)
        for decision in decisions:
            resolution.submit_decision(self.store, decision)
        active = self.active()
        self.assertEqual({r: tuple(e['members']) for r, e in active.items()},
                         {'resource-2': ('a:9',), 'resource-4': ('a:7',), 'resource-5': ('b:2',)})
        self.assertTrue(resolution.integrity_check(self.store)['ok'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temporary.name) / 'api.sqlite3')
        store = Store(self.database)
        records, aliases, _ = feed()
        resolution.ingest_feed(store, '共享中心', json.dumps(FIXTURE).encode(), records, aliases)
        store.close()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.thread = threading.Thread(target=serve, args=(self.database, self.port), daemon=True)
        self.thread.start()
        for unused in range(100):
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1):
                    break
            except OSError:
                pass

    def tearDown(self):
        self.temporary.cleanup()

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def test_candidate_confirm_split_entries(self):
        client = Client()
        status, _, body = client.request(self.url('/candidates'))
        self.assertEqual(status, 200)
        self.assertTrue(any(c['records'] == ['a:7', 'b:2'] for c in json.loads(body)))
        # 确认合并
        status, _, body = client.request(self.url('/decisions'), method='POST',
            body=json.dumps({'type': 'merge', 'sources': ['a:7', 'b:2'], 'version': 1}).encode())
        self.assertEqual(status, 200)
        # 过期版本被 409 拒绝
        status, _, body = client.request(self.url('/decisions'), method='POST',
            body=json.dumps({'type': 'split', 'entity': 'resource-1', 'version': 1}).encode())
        self.assertEqual(status, 409)
        self.assertIn({'resource': 'resource-1', 'version': 2}, json.loads(body)['current'])
        # 正确版本拆分
        status, _, body = client.request(self.url('/decisions'), method='POST',
            body=json.dumps({'type': 'split', 'entity': 'resource-1', 'version': 2}).encode())
        self.assertEqual(status, 200)
        status, _, body = client.request(self.url('/entities/resource-1'))
        self.assertEqual(json.loads(body)['status'], 'retired')
        status, _, body = client.request(self.url('/records/a:7/history'))
        self.assertEqual([h['resource'] for h in json.loads(body)], ['resource-1', 'resource-1', 'resource-4'])
        status, _, body = client.request(self.url('/integrity'))
        self.assertTrue(json.loads(body)['ok'])


if __name__ == '__main__':
    unittest.main()
