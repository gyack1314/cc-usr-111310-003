"""桥表查询视图：根贡献、逐段分摊路径、损耗去向与守恒账。

路径不是可达性关系：每段都带实际数量折算因子，路径数量之和必须与
桥表 (root, descendant) 的贡献量逐格相等，同根多路径相加不去重。
"""
import json
from fractions import Fraction

from .engine import LedgerError
from .quant import format_quantity


def _factor(warehouse, event, input_lot, output_amount):
    """单段因子：输入批的单位量经过本事件后落到该输出（或损耗）的比例。"""
    return (event.inputs[input_lot] / warehouse.produced[input_lot]) * (
        output_amount / event.total_input())


def trace_paths(warehouse, lot):
    """枚举 lot 到每个原料根的全部逐段分摊路径及精确数量。"""
    if lot not in warehouse.produced:
        raise KeyError(f'未知批: {lot}')

    def expand(current):
        """返回 (root, segments, factor 积) 列表，segments 为事件段。"""
        if current in warehouse.raw_lots:
            return [(current, [], 1)]
        event = warehouse.active_events[warehouse.producer[current]]
        found = []
        for input_lot in event.inputs:
            segment_factor = _factor(warehouse, event, input_lot,
                                     event.outputs[current])
            for root, segments, upstream_factor in expand(input_lot):
                found.append((root, segments + [{
                    'event_id': event.event_id,
                    'from_lot': input_lot,
                    'to_lot': current,
                    'factor': segment_factor,
                }], upstream_factor * segment_factor))
        return found

    produced = warehouse.produced[lot]
    paths = []
    for root, segments, factor_product in expand(lot):
        quantity = warehouse.raw_lots[root] * factor_product
        paths.append({
            'root_lot': root,
            'descendant_lot': lot,
            'segments': segments,
            'quantity': quantity,
            'ratio': quantity / produced,
        })
    paths.sort(key=lambda row: (row['root_lot'],
                                [s['event_id'] for s in row['segments']]))
    return paths


def trace_loss_paths(warehouse, event_id=None):
    """枚举损耗去向：每条从原料根到事件损耗的路径与精确数量。"""
    events = (list(warehouse.active_events.values()) if event_id is None
              else [warehouse.active_events[event_id]])
    result = []
    for event in events:
        if event.loss <= 0:
            continue
        for input_lot in event.inputs:
            loss_factor = _factor(warehouse, event, input_lot, event.loss)
            for path in trace_paths(warehouse, input_lot):
                result.append({
                    'event_id': event.event_id,
                    'root_lot': path['root_lot'],
                    'segments': path['segments'] + [{
                        'event_id': event.event_id,
                        'from_lot': input_lot,
                        'to_lot': None,
                        'factor': loss_factor,
                    }],
                    'quantity': path['quantity'] * loss_factor,
                })
    result.sort(key=lambda row: (row['event_id'], row['root_lot']))
    return result


def _serialize(value):
    if isinstance(value, Fraction):
        return format_quantity(value)
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    return value


def build_report(warehouse):
    """整仓核对报告：桥表、余额、路径核对、损耗、守恒账、受影响批。"""
    warehouse.verify()

    bridge = warehouse.bridge_rows()
    balances = []
    for lot in sorted(warehouse.produced):
        consumed = warehouse.produced[lot] - warehouse.balances[lot]
        balances.append({
            'lot': lot,
            'produced': warehouse.produced[lot],
            'consumed': consumed,
            'balance': warehouse.balances[lot],
            'is_raw': lot in warehouse.raw_lots,
        })

    # 路径数量与桥表逐格核对：同根不同路径相加，必须等于桥贡献量
    path_check = {}
    for lot in warehouse.composition:
        for path in trace_paths(warehouse, lot):
            key = (path['root_lot'], lot)
            path_check[key] = path_check.get(key, 0) + path['quantity']
    for row in bridge:
        key = (row['root_lot'], row['descendant_lot'])
        if path_check.get(key) != row['quantity']:
            raise LedgerError(
                f'路径核对失败: {key} 路径之和不等于桥贡献量')

    loss_total = sum((row['loss'] for row in warehouse.loss_ledger),
                     Fraction(0))
    balance_total = sum(warehouse.balances.values(), Fraction(0))
    initial_total = sum(warehouse.raw_lots.values(), Fraction(0))

    return {
        'unit': warehouse.unit,
        'warehouse_version': warehouse.warehouse_version,
        'source_digest': warehouse.source_digest,
        'published_versions': dict(warehouse.versions),
        'active_events': sorted(warehouse.active_events),
        'withdrawn_events': sorted(warehouse.withdrawn),
        'bridge': bridge,
        'balances': balances,
        'loss_ledger': warehouse.loss_ledger,
        'loss_paths': trace_loss_paths(warehouse),
        'conservation': {
            'initial_raw': initial_total,
            'current_balances': balance_total,
            'cumulative_loss': loss_total,
            'balanced': initial_total == balance_total + loss_total,
        },
        'affected_lots': list(warehouse.last_affected),
    }


def report_json(warehouse, indent=2):
    return json.dumps(
        _serialize(build_report(warehouse)),
        ensure_ascii=False, indent=indent)
