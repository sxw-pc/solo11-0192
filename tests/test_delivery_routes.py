import json
import unittest
from pathlib import Path

from grainrelease.carriers import carriers
from grainrelease.delivery import (assign, assignments, build_graph, plan,
                                   replan, route)
from grainrelease.route_capture import capture, segment_state

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


class CarrierLinkage(unittest.TestCase):
    """delivery.assign 必须随车位与路段封闭联动变化。"""

    def setUp(self):
        self.fixture = load('delivery-ok.json')

    def test_depot_change_moves_arrival(self):
        events = [dict(e) for e in self.fixture['events']]
        events[0]['depot'] = 'B'  # C1 的可用车位从 A 改到 B
        located = carriers(events)
        self.assertEqual(located['C1']['depot'], 'B')
        base = assignments(self.fixture['events'], self.fixture['travel'])
        moved = assignments(events, self.fixture['travel'])
        self.assertEqual(assign(self.fixture['events'], self.fixture['travel']), {'D1': 'C1'})
        self.assertEqual(base['D1']['minutes'], 30)
        self.assertEqual(moved['D1']['depot'], 'B')
        self.assertEqual(moved['D1']['minutes'], 40)  # 可达时刻联动变化

    def test_closure_switches_choice(self):
        events = [dict(e, axle_class='heavy') if e['carrier_id'] == 'C1'
                  else dict(e, axle_class='light') for e in self.fixture['events']]
        travel = dict(self.fixture['travel'])
        travel['depart_after'] = '2026-07-05T07:30:00'
        travel['closures'] = [{'from': 'A', 'to': 'D1', 'axle_class': 'heavy',
                               'effective_from': '2026-07-05T07:00:00',
                               'effective_until': '2026-07-05T09:00:00'}]
        # 发车时段 A->D1 对重轴封闭：C1 让位于轻轴 C2
        self.assertEqual(assign(events, travel), {'D1': 'C2'})
        # 两条进场段都封闭：不得假定任何车辆可达
        travel['closures'].append({'from': 'B', 'to': 'D1', 'axle_class': 'all',
                                   'effective_from': '2026-07-05T07:00:00',
                                   'effective_until': '2026-07-05T09:00:00'})
        self.assertEqual(assign(events, travel), {})


class RouteEvidence(unittest.TestCase):
    """地图寻路、单车互斥、休息时长与到货窗口。"""

    def setUp(self):
        self.fixture = load('delivery-windows.json')
        self.reports = self.fixture['reports']
        self.roads = self.fixture['roads']
        self.customers = self.fixture['customers']
        self.depart = self.fixture['depart_after']
        self.initial = self.fixture['announcements']['initial']
        self.permit_path = self.fixture['announcements']['permit_path']
        self.extension_path = self.fixture['announcements']['extension_path']

    def test_oneway_segment_blocks_reverse(self):
        graph = build_graph(self.roads['segments'])
        ledger = capture(self.reports)
        forward = route(graph, ledger, 'D1', 'D2', 'light', 'C2', self.depart, self.depart)
        self.assertEqual(forward['path'], ['D1', 'D2'])  # D1->D2 单向可通
        reverse = route(graph, ledger, 'D2', 'D1', 'light', 'C2', self.depart, self.depart)
        self.assertEqual(reverse['path'], ['D2', 'B', 'D1'])  # 反向必须绕行
        self.assertEqual(reverse['arrive_at'], '2026-07-05T08:45:00')

    def test_axle_limit_bars_heavy_truck(self):
        graph = build_graph(self.roads['segments'])
        ledger = capture(self.reports)
        light = route(graph, ledger, 'A', 'D2', 'light', 'C2', self.depart, self.depart)
        heavy = route(graph, ledger, 'A', 'D2', 'heavy', 'C1', self.depart, self.depart)
        self.assertEqual(light['path'], ['A', 'D2'])  # 轻轴可走限轴段
        self.assertNotEqual(heavy['path'], ['A', 'D2'])  # 重轴不得走限轴段
        self.assertNotIn('A->D2', [f"{leg['from']}->{leg['to']}" for leg in heavy['legs']])

    def test_shift_rest_and_cleaning_gate_chaining(self):
        # 仅 C2 可用：前单卸货、清扫、司机休息必须计入下一段可出发时刻
        reports = [r for r in self.reports if r.get('carrier_id') != 'C1']
        customers = [self.customers[0],
                     dict(self.customers[1], window_until='2026-07-05T23:59:00',
                          dock_close='2026-07-05T23:59:00')]
        result = plan(reports, self.roads, customers, self.depart)
        chain = result['chains']['C2']
        self.assertEqual([leg['customer'] for leg in chain], ['D1', 'D2'])
        # 08:10 到 D1，卸货 60 + 清扫 10 + 休息 30 后才能再出发
        self.assertEqual(chain[1]['depart_at'], '2026-07-05T09:50:00')
        self.assertEqual(chain[1]['arrive_at'], '2026-07-05T10:35:00')

    def test_baseline_promises(self):
        result = plan(self.reports, self.roads, self.customers, self.depart)
        promises = result['promises']
        self.assertEqual(promises['D1']['carrier'], 'C1')
        self.assertEqual(promises['D1']['arrive_at'], '2026-07-05T08:00:00')
        self.assertEqual(promises['D2']['carrier'], 'C2')
        self.assertEqual(promises['D2']['arrive_at'], '2026-07-05T08:05:00')
        self.assertEqual({p['status'] for p in promises.values()}, {'kept'})
        self.assertEqual(result['missed'], {})

    def test_permit_path_keeps_both_promises(self):
        base = plan(self.reports, self.roads, self.customers, self.depart)
        result = replan(base, self.reports + self.initial + self.permit_path,
                        self.roads, self.customers, self.depart)
        promises = result['promises']
        self.assertEqual(promises['D1']['status'], 'kept')
        self.assertEqual(promises['D1']['promise_id'], 'promise-D1')  # 旧承诺保留
        self.assertEqual(promises['D1']['carrier'], 'C1')
        self.assertEqual(promises['D1']['arrive_at'], '2026-07-05T08:00:00')
        self.assertEqual(promises['D1']['permits'], ['P-1'])  # 凭许可走 D1
        self.assertIn('N-1', promises['D1']['evidence'])
        self.assertEqual(promises['D2']['status'], 'kept')
        self.assertEqual(promises['D2']['carrier'], 'C2')
        self.assertEqual(result['rebookings'], [])  # 两个承诺均保留，无需改约
        self.assertEqual(len(promises['D1']['history']), 2)  # 重建留痕

    def test_extension_path_rebooks_d2(self):
        base = plan(self.reports, self.roads, self.customers, self.depart)
        result = replan(base, self.reports + self.initial + self.extension_path,
                        self.roads, self.customers, self.depart)
        promises = result['promises']
        # C1 绕行错过 D1，由 C2 优先交 D1
        self.assertEqual(promises['D1']['status'], 'kept')
        self.assertEqual(promises['D1']['carrier'], 'C2')
        self.assertEqual(promises['D1']['arrive_at'], '2026-07-05T08:10:00')
        # D2 不得静默覆盖：旧承诺原样保留并发出可追改约通知
        self.assertEqual(promises['D2']['status'], 'rebooked')
        self.assertEqual(promises['D2']['promise_id'], 'promise-D2')
        self.assertEqual(promises['D2']['arrive_at'], '2026-07-05T08:05:00')
        self.assertEqual(len(result['rebookings']), 1)
        notice = result['rebookings'][0]
        self.assertEqual(notice['promise_id'], 'promise-D2')
        self.assertEqual(notice['original']['carrier'], 'C2')
        self.assertEqual(notice['original']['arrive_at'], '2026-07-05T08:05:00')
        self.assertEqual(notice['earliest_arrival'], '2026-07-05T10:35:00')
        self.assertIn('N-2', notice['evidence'])
        # 单车时序：C1 未被重复承诺，C2 的 D2 段出发计入休息
        self.assertNotIn('C1', result['chains'])
        self.assertEqual(result['missed']['D2']['attempt']['depart_at'],
                         '2026-07-05T09:50:00')

    def test_ineffective_permit_not_honoured(self):
        # 延行路径附带未生效许可：不得当作通行证据
        ledger = capture(self.reports + self.initial + self.extension_path)
        state = segment_state(ledger, 'A', 'D1', 'heavy', 'C1',
                              '2026-07-05T07:30:00', self.depart)
        self.assertFalse(state['open'])
        self.assertEqual(state['reopen_at'], '2026-07-05T12:00:00')
        # 发车前签发且生效的许可才可用
        ledger = capture(self.reports + self.initial + self.permit_path)
        state = segment_state(ledger, 'A', 'D1', 'heavy', 'C1',
                              '2026-07-05T07:30:00', self.depart)
        self.assertTrue(state['open'])
        self.assertEqual(state['permits'], ['P-1'])
        # 发车后签发的许可无效
        late = [dict(self.permit_path[0], issued_at='2026-07-05T08:30:00')]
        ledger = capture(self.reports + self.initial + late)
        state = segment_state(ledger, 'A', 'D1', 'heavy', 'C1',
                              '2026-07-05T07:30:00', self.depart)
        self.assertFalse(state['open'])
        # 轻轴车不受重轴禁行影响
        ledger = capture(self.reports + self.initial)
        state = segment_state(ledger, 'A', 'D1', 'light', 'C2',
                              '2026-07-05T07:30:00', self.depart)
        self.assertTrue(state['open'])

    def test_unconfirmed_vehicle_not_assumed_on_time(self):
        # 抽走 C1 位置回报：旧承诺不得默认 C1 仍能准时
        reports = [r for r in self.reports
                   if not (r.get('kind') == 'position' and r['carrier_id'] == 'C1')]
        base = plan(self.reports, self.roads, self.customers, self.depart)
        result = replan(base, reports, self.roads, self.customers, self.depart)
        self.assertIn('C1', result['unconfirmed_carriers'])
        self.assertNotEqual(result['promises']['D1']['carrier'], 'C1')
        # 全无位置回报：任何客户都不得假定准时
        reports = [r for r in self.reports if r.get('kind') != 'position']
        result = plan(reports, self.roads, self.customers, self.depart)
        self.assertEqual(result['promises'], {})
        self.assertEqual(result['unconfirmed_carriers'], ['C1', 'C2'])
        self.assertEqual(result['unconfirmed_stops'], ['D1', 'D2'])

    def test_independent_nearest_would_double_promise(self):
        # 逐点独立选最近车会把 C2 同时承诺给 D1 和 D2
        events = [r for r in self.reports if r.get('carrier_id') == 'C2']
        travel = {'stops': ['D1', 'D2'], 'B': {'D1': 40, 'D2': 35},
                  'depart_after': self.depart}
        self.assertEqual(assign(events, travel), {'D1': 'C2', 'D2': 'C2'})
        # 同一车次序列规划发现连锁冲突：D2 必须改约而非重复承诺
        result = plan(self.reports + self.initial + self.extension_path,
                      self.roads, self.customers, self.depart)
        self.assertEqual(result['promises']['D1']['carrier'], 'C2')
        self.assertIn('D2', result['missed'])

    def test_two_announcement_paths_diverge_on_d2(self):
        base = plan(self.reports, self.roads, self.customers, self.depart)
        permitted = replan(base, self.reports + self.initial + self.permit_path,
                           self.roads, self.customers, self.depart)
        extended = replan(base, self.reports + self.initial + self.extension_path,
                          self.roads, self.customers, self.depart)
        # 同一初始公告，两路补充都守约束，却改变 D2 是否按约收粮
        self.assertEqual(permitted['promises']['D1']['status'], 'kept')
        self.assertEqual(extended['promises']['D1']['status'], 'kept')
        self.assertEqual(permitted['promises']['D2']['status'], 'kept')
        self.assertEqual(extended['promises']['D2']['status'], 'rebooked')
        self.assertEqual(permitted['rebookings'], [])
        self.assertEqual(len(extended['rebookings']), 1)


if __name__ == '__main__':
    unittest.main()
