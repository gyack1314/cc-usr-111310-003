"""生产事件账、批余额与带数量的批次谱系桥。

设计要点（对应数据契约）：

* Event 不可变，携带 event_id、version、实际输入消耗量、实际输出产量、明确损耗；
* 量全部用 Fraction，事件按 总输入 == 总输出 + 损耗 核对；
* 每个输出批只有一个有效生产事件，原料批无父事件，生产依赖是 DAG；
* 均匀混合：输入批各自的 root 贡献先按实际消耗量折算，再按
  输出量/总输入量 均摊到每个输出；损耗同比例分摊，不重新归一；
* 桥表保存 (root_lot, descendant_lot) 的精确贡献量，同根不同路径
  相加而不是去重；可达性关系不充当贡献比例；
* 退回/更正只能撤回原事件再加入新事件；撤回先形成候选账并整体验证，
  下游若失去合法输入则整批更改拒绝，余额与桥不会只改一半；
* 新事件或撤回只沿实际后代闭包重算，无关分支不扫。
"""
import copy
import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction

from .quant import format_quantity, parse_quantity


class LedgerError(ValueError):
    """事件账不满足守恒、余额或谱系约束时抛出。"""


@dataclass(frozen=True)
class Event:
    """不可变转换事件，所有量已是同一冻结基准单位的 Fraction。"""
    event_id: str
    version: int
    inputs: dict           # lot_id -> 实际消耗量（正）
    outputs: dict          # lot_id -> 实际产量（正）
    loss: Fraction         # 明确损耗（非负）

    def total_input(self):
        return sum(self.inputs.values(), Fraction(0))

    def total_output(self):
        return sum(self.outputs.values(), Fraction(0))


def parse_event(row):
    """解析并核对单条事件的形状与自身守恒，返回不可变 Event。"""
    if not isinstance(row, dict):
        raise ValueError('事件必须是对象')
    event_id = row.get('event_id')
    if not isinstance(event_id, str) or not event_id:
        raise ValueError('event_id 缺失或重复')
    version = row.get('version')
    if type(version) is not int or version <= 0:
        raise ValueError('version 必须是正整数')

    def parse_map(key, positive):
        section = row.get(key)
        if not isinstance(section, dict) or not section:
            raise ValueError(f'事件 {event_id} 的 {key} 不能为空')
        amounts = {}
        for lot, value in section.items():
            if not isinstance(lot, str) or not lot:
                raise ValueError(f'事件 {event_id} 的 {key} 批身份无效')
            if lot in amounts:
                raise ValueError(f'事件 {event_id} 的 {key} 中批 {lot} 重复')
            amounts[lot] = parse_quantity(value, positive=positive)
        return amounts

    inputs = parse_map('inputs', True)
    outputs = parse_map('outputs', True)
    loss = parse_quantity(row.get('loss'), positive=False)
    event = Event(event_id, version, inputs, outputs, loss)
    if event.total_input() != event.total_output() + loss:
        raise LedgerError(
            f'事件 {event_id} 守恒失败: 总输入 != 总输出 + 损耗')
    return event


def parse_manifest(manifest):
    """解析整份清单：形状、身份、撤回引用与发布分组。"""
    if not isinstance(manifest, dict):
        raise ValueError('顶层必须为对象')
    if manifest.get('kind') != 'lot-bridge':
        raise ValueError('kind 必须是 lot-bridge')

    raw_section = manifest.get('raw_lots')
    if not isinstance(raw_section, dict) or not raw_section:
        raise ValueError('raw_lots 必须是非空对象')
    raw_lots = {}
    for lot, value in raw_section.items():
        if not isinstance(lot, str) or not lot:
            raise ValueError('原料批身份无效')
        if lot in raw_lots:
            raise ValueError(f'原料批 {lot} 重复')
        raw_lots[lot] = parse_quantity(value, positive=True)

    rows = manifest.get('events')
    if not isinstance(rows, list) or not rows:
        raise ValueError('events 必须是非空数组')
    events = []
    event_ids = set()
    for row in rows:
        event = parse_event(row)
        if event.event_id in event_ids:
            raise ValueError(f'event_id 重复: {event.event_id}')
        event_ids.add(event.event_id)
        events.append(event)

    withdrawals = manifest.get('withdraw_events', [])
    if not isinstance(withdrawals, list):
        raise ValueError('withdraw_events 必须是数组')
    for event_id in withdrawals:
        if not isinstance(event_id, str) or not event_id or event_id not in event_ids:
            raise ValueError(f'撤回事件不存在: {event_id!r}')
    if len(set(withdrawals)) != len(withdrawals):
        raise ValueError('withdraw_events 存在重复')

    batches = manifest.get('batches')
    if not isinstance(batches, list):
        raise ValueError('batches 必须是数组')
    grouped = set()
    for group in batches:
        if not isinstance(group, list) or not group:
            raise ValueError('batches 分组必须是非空数组')
        for event_id in group:
            if not isinstance(event_id, str) or not event_id or event_id not in event_ids:
                raise ValueError(f'batches 引用未知事件: {event_id!r}')
            if event_id in grouped:
                raise ValueError(f'事件 {event_id} 出现在多个发布分组')
            grouped.add(event_id)

    return {
        'raw_lots': raw_lots,
        'events': events,
        'withdrawals': withdrawals,
        'unit': manifest.get('unit'),
        'warehouse_version': manifest.get('warehouse_version'),
    }


def _canonical_event(event):
    """事件规范载荷：event_id 与 version 纳入摘要，重放同一事件不重复消耗。"""
    return {
        'event_id': event.event_id,
        'version': event.version,
        'inputs': {k: format_quantity(v) for k, v in sorted(event.inputs.items())},
        'outputs': {k: format_quantity(v) for k, v in sorted(event.outputs.items())},
        'loss': format_quantity(event.loss),
    }


class Warehouse:
    """事件账、批余额、正反向依赖与传递桥的内存事实仓。"""

    def __init__(self, raw_lots, unit=None, warehouse_version=None):
        # 统一走数量解析：仓内只允许 Fraction，字符串即时归一
        self.raw_lots = {
            lot: (amount if isinstance(amount, Fraction)
                  else parse_quantity(amount, positive=True))
            for lot, amount in raw_lots.items()
        }
        self.unit = unit
        self.warehouse_version = warehouse_version
        self.events = {}                         # event_id -> Event（含已撤回，不可变）
        self.active_events = {}                  # event_id -> Event（有效事件）
        self.withdrawn = set()                   # 已撤回 event_id
        self.producer = {}                       # 输出批 -> 生产它的 event_id
        self.consumers = {}                      # 批 -> 消耗它的有效 event_id 集合
        self.balances = dict(self.raw_lots)      # 批当前可用余额（已产生未消耗）
        self.produced = dict(self.raw_lots)      # 批累计已产生量
        # 原料批无父事件，对自身的 root 贡献就是其初量
        self.composition = {lot: {lot: amount}
                            for lot, amount in self.raw_lots.items()}
        self.loss_ledger = []                    # 每次有效事件的损耗守恒账
        self.versions = {}                       # 发布版本 -> 源事件集摘要
        self.source_digest = None                # 当前有效源事件集摘要
        self.last_affected = []                  # 最近一次更改影响的批清单

    # ---- 内部工具 --------------------------------------------------------

    def _descendants_of(self, event):
        """事件输出批的有效后代闭包（含其直接输出批）。"""
        seen = set()
        stack = list(event.outputs)
        while stack:
            lot = stack.pop()
            if lot in seen:
                continue
            seen.add(lot)
            for event_id in sorted(self.consumers.get(lot, ())):
                stack.extend(self.active_events[event_id].outputs)
        return seen

    def _recompute_closure(self, closure):
        """对闭包内仍存在的批重算余额与 root 贡献；只触碰闭包，无关分支不扫。"""
        pending = {lot for lot in closure if lot in self.produced}

        # 余额重算与顺序无关：已产生 - 全部有效事件消耗
        for lot in pending:
            consumed = Fraction(0)
            for event_id in self.consumers.get(lot, ()):
                consumed += self.active_events[event_id].inputs.get(lot, Fraction(0))
            self.balances[lot] = self.produced[lot] - consumed

        # Kahn 拓扑（按批的生产者 DAG）。初始就绪：原料批，以及闭包内输入
        # 依赖为空的非原料批（其输入全在闭包外，贡献已在既有 composition 中，
        # 不重扫无关分支）。
        deps = {}
        for lot in pending:
            if lot in self.raw_lots:
                continue
            producer_event = self.active_events[self.producer[lot]]
            deps[lot] = [inp for inp in producer_event.inputs if inp in pending]
        ready = [lot for lot in pending
                 if lot in self.raw_lots or not deps.get(lot)]
        ordered = []
        while ready:
            lot = ready.pop()
            ordered.append(lot)
            # lot 就绪后通知所有以它为输入的有效事件，闭包内输出解除一条依赖
            for event_id in self.consumers.get(lot, ()):
                if event_id not in self.active_events:
                    continue
                for out in self.active_events[event_id].outputs:
                    if out not in pending:
                        continue
                    if out in deps and lot in deps[out]:
                        deps[out].remove(lot)
                        if not deps[out]:
                            ready.append(out)
        stuck = [lot for lot, unresolved in deps.items() if unresolved]
        if stuck:
            raise LedgerError(f'闭包内存在环，无法重算: {sorted(stuck)}')

        for lot in ordered:
            if lot in self.raw_lots:
                self.composition[lot] = {lot: self.raw_lots[lot]}
            else:
                producer_event = self.active_events[self.producer[lot]]
                self.composition[lot] = self._output_composition(producer_event, lot)

    def _event_root_inputs(self, event):
        """事件各输入批按实际消耗量折算后的 root 输入量，路径相加不去重。"""
        root_inputs = {}
        for lot, consumed in event.inputs.items():
            source = self.composition[lot]
            total = self.produced[lot]
            for root, root_amount in source.items():
                share = root_amount * consumed / total
                root_inputs[root] = root_inputs.get(root, Fraction(0)) + share
        return root_inputs

    def _output_composition(self, event, output_lot):
        """单个输出批的 root 贡献：root 输入量 * 产量 / 总输入量。"""
        root_inputs = self._event_root_inputs(event)
        total_input = event.total_input()
        scale = event.outputs[output_lot] / total_input
        return {root: amount * scale for root, amount in root_inputs.items()}

    def _loss_composition(self, event):
        """损耗按同一均摊比例归属各 root；损耗不重新摊给幸存品。"""
        root_inputs = self._event_root_inputs(event)
        total_input = event.total_input()
        scale = event.loss / total_input
        return {root: amount * scale for root, amount in root_inputs.items()}

    # ---- 写操作 ----------------------------------------------------------

    def add_event(self, event):
        """加入一个有效转换事件；任何核对失败都抛出且账内状态不变。"""
        if event.event_id in self.events:
            state = '已撤回' if event.event_id in self.withdrawn else '有效'
            raise LedgerError(
                f'event_id {event.event_id} 已存在（{state}），事件不可变；'
                '更正请撤回后以新 event_id 与新批身份提交')

        # 余额核对：输入消耗不能超过已产生且未被其他有效事件消耗的量
        for lot, consumed in event.inputs.items():
            if lot not in self.produced:
                raise LedgerError(
                    f'事件 {event.event_id} 输入批 {lot} 尚无合法来源')
            if consumed > self.balances.get(lot, Fraction(0)):
                raise LedgerError(
                    f'事件 {event.event_id} 对批 {lot} 超额消耗: '
                    f'需要 {format_quantity(consumed)}，'
                    f'余额 {format_quantity(self.balances.get(lot, Fraction(0)))}')

        # 输出批身份：原料批无父事件，一个输出批只有一个有效生产者
        for lot in event.outputs:
            if lot in self.raw_lots:
                raise LedgerError(f'原料批 {lot} 不能再由事件生产')
            if lot in self.producer:
                raise LedgerError(f'批 {lot} 已有生产者 {self.producer[lot]}，禁止重复生产')
            if lot in event.inputs:
                raise LedgerError(f'事件 {event.event_id} 出现自环: 批 {lot} 同时是输入与输出')

        # DAG 环核对：新输出不得是任一输入批的祖先
        ancestors = set()
        stack = list(event.inputs)
        while stack:
            lot = stack.pop()
            if lot in ancestors:
                continue
            ancestors.add(lot)
            producer_id = self.producer.get(lot)
            if producer_id is not None:
                stack.extend(self.active_events[producer_id].inputs)
        for lot in event.outputs:
            if lot in ancestors:
                raise LedgerError(f'事件 {event.event_id} 会形成环: 批 {lot} 已是输入祖先')

        # 全部核对通过后才落账（失败不留半成品）
        self.events[event.event_id] = event
        self.active_events[event.event_id] = event
        for lot, amount in event.outputs.items():
            self.producer[lot] = event.event_id
            self.produced[lot] = amount
            self.balances[lot] = amount
        for lot, consumed in event.inputs.items():
            self.consumers.setdefault(lot, set()).add(event.event_id)
            self.balances[lot] -= consumed

        # 受影响清单只含实际后代；输入批参与重算但不是新事件的后代
        affected = self._descendants_of(event)
        recompute_scope = set(affected)
        recompute_scope.update(event.inputs)
        self._recompute_closure(recompute_scope)

        self.loss_ledger.append({
            'event_id': event.event_id,
            'loss': event.loss,
            'by_root': self._loss_composition(event),
        })
        self._refresh_digest()
        self.last_affected = sorted(affected)
        return list(self.last_affected)

    def withdraw_event(self, event_id):
        """撤回事件：先形成候选账整体验证，下游失去来源则整次拒绝。"""
        if event_id not in self.events:
            raise LedgerError(f'待撤回事件不存在: {event_id}')
        if event_id in self.withdrawn:
            raise LedgerError(f'事件已撤回，不能重复撤回: {event_id}')
        event = self.events[event_id]

        # 闭包含事件输入批（撤回要恢复其余额）与全部实际后代
        closure = self._descendants_of(event)
        closure.update(event.inputs)

        # 撤回后仍有有效事件消耗该事件的输出批 => 下游失去合法输入
        orphaned = set()
        for lot in event.outputs:
            for consumer_id in self.consumers.get(lot, ()):
                if consumer_id != event_id:
                    orphaned.add(consumer_id)
        if orphaned:
            raise LedgerError(
                f'撤回 {event_id} 会使下游事件失去合法输入: '
                + ', '.join(sorted(orphaned))
                + '；请先撤回下游或用新批身份提交更正事件')

        # 在候选副本上重算并验证，成功才整体替换（余额与桥同生共死）
        candidate = copy.deepcopy(self)
        candidate.withdrawn.add(event_id)
        del candidate.active_events[event_id]
        for lot in event.outputs:
            del candidate.producer[lot]
            del candidate.produced[lot]
            del candidate.balances[lot]
            del candidate.composition[lot]
        for lot in event.inputs:
            candidate.consumers[lot].discard(event_id)
            if not candidate.consumers[lot]:
                del candidate.consumers[lot]
        candidate.loss_ledger = [
            row for row in candidate.loss_ledger if row['event_id'] != event_id
        ]
        candidate._recompute_closure(closure)
        candidate.verify()

        self.__dict__.update(candidate.__dict__)
        self._refresh_digest()
        self.last_affected = sorted(closure)
        return list(self.last_affected)

    # ---- 发布与核对 ------------------------------------------------------

    def _refresh_digest(self):
        active = []
        for event in self.active_events.values():
            payload = _canonical_event(event)
            payload['state'] = 'active'
            active.append(payload)
        payload = {
            'active': sorted(active, key=lambda row: row['event_id']),
            'withdrawn': sorted(self.withdrawn),
        }
        self.source_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf8')
        ).hexdigest()
        return self.source_digest

    def publish(self, version):
        """把发布版本绑定到当前有效源事件集摘要。"""
        if not isinstance(version, str) or not version:
            raise LedgerError('发布版本号无效')
        if version in self.versions:
            raise LedgerError(f'发布版本已存在: {version}')
        self.versions[version] = self._refresh_digest()
        return self.versions[version]

    def verify(self):
        """全量不变量核对；任何一项失败都抛 LedgerError。"""
        # 1. 每个非原料批恰有一个有效生产者，原料批无父事件
        for lot in self.produced:
            if lot in self.raw_lots:
                if lot in self.producer:
                    raise LedgerError(f'原料批 {lot} 不应有生产事件')
            elif lot not in self.producer or self.producer[lot] not in self.active_events:
                raise LedgerError(f'批 {lot} 缺少有效生产者')

        # 2. DAG：生产者深度可有限展开即无环
        self._assert_acyclic()

        # 3. 余额非负，且 已产生 == 已消耗 + 余额
        for lot, produced in self.produced.items():
            consumed = Fraction(0)
            for event_id in self.consumers.get(lot, ()):
                consumed += self.active_events[event_id].inputs[lot]
            if self.balances[lot] < 0:
                raise LedgerError(f'批 {lot} 余额为负')
            if produced != consumed + self.balances[lot]:
                raise LedgerError(f'批 {lot} 余额账不平')

        # 4. 每个输出批的 root 贡献总和恰等于它的产量（损耗另入守恒账）
        for lot, roots in self.composition.items():
            total = sum(roots.values(), Fraction(0))
            if total != self.produced[lot]:
                raise LedgerError(
                    f'批 {lot} 根贡献之和 {format_quantity(total)} '
                    f'!= 产量 {format_quantity(self.produced[lot])}')

        # 5. 每个有效事件的 root 守恒：输入 root 量 == 输出 root 量 + 损耗 root 量
        for event in self.active_events.values():
            root_in = self._event_root_inputs(event)
            root_out = {}
            for lot in event.outputs:
                for root, amount in self.composition[lot].items():
                    root_out[root] = root_out.get(root, Fraction(0)) + amount
            root_loss = self._loss_composition(event)
            roots = set(root_in) | set(root_out) | set(root_loss)
            for root in roots:
                lhs = root_in.get(root, Fraction(0))
                rhs = root_out.get(root, Fraction(0)) + root_loss.get(root, Fraction(0))
                if lhs != rhs:
                    raise LedgerError(
                        f'事件 {event.event_id} 的 root {root} 守恒失败')

        # 6. 全局守恒：原料初量 == 现存批余额 + 累计损耗
        loss_total = sum((row['loss'] for row in self.loss_ledger), Fraction(0))
        balance_total = sum(self.balances.values(), Fraction(0))
        initial_total = sum(self.raw_lots.values(), Fraction(0))
        if initial_total != balance_total + loss_total:
            raise LedgerError('全局守恒失败: 原料初量 != 现存余额 + 累计损耗')

    def _assert_acyclic(self):
        color = {}

        def visit(lot):
            state = color.get(lot)
            if state == 1:
                raise LedgerError(f'生产依赖存在环，涉及批 {lot}')
            if state == 2:
                return
            color[lot] = 1
            producer_id = self.producer.get(lot)
            if producer_id is not None:
                for upstream in self.active_events[producer_id].inputs:
                    visit(upstream)
            color[lot] = 2

        for lot in self.produced:
            visit(lot)

    # ---- 读视图 ----------------------------------------------------------

    def bridge_rows(self):
        """桥表行 (root_lot, descendant_lot, 贡献量, 比例)，路径已相加。"""
        rows = []
        for lot in sorted(self.composition):
            produced = self.produced[lot]
            for root in sorted(self.composition[lot]):
                amount = self.composition[lot][root]
                rows.append({
                    'root_lot': root,
                    'descendant_lot': lot,
                    'quantity': amount,
                    'ratio': (amount / produced) if produced else Fraction(0),
                })
        return rows

    def descendant_closure(self, lot):
        """批的实际后代闭包（增量维护的影响范围）。"""
        seen = set()
        stack = [lot]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for event_id in self.consumers.get(current, ()):
                stack.extend(self.active_events[event_id].outputs)
        return seen


def build_warehouse(manifest):
    """从清单构建并发布事实仓；任何一步失败都抛出，不产生部分结果。"""
    parsed = parse_manifest(manifest)
    warehouse = Warehouse(
        parsed['raw_lots'],
        unit=parsed['unit'],
        warehouse_version=parsed['warehouse_version'],
    )
    apply_parsed(warehouse, parsed)
    return warehouse


def _same_event_payload(left, right):
    return _canonical_event(left) == _canonical_event(right)


def apply_parsed(warehouse, parsed):
    """按清单顺序加入事件再执行撤回，返回受影响批清单。

    同批 ID 重放不重复消耗：已存在且载荷（含 version）完全一致的事件
    跳过；同 ID 但载荷不同按事件不可变违规拒绝。
    """
    affected = set()
    for event in parsed['events']:
        existing = warehouse.events.get(event.event_id)
        if existing is not None:
            if not _same_event_payload(existing, event):
                raise LedgerError(
                    f'event_id {event.event_id} 已存在且载荷不同，'
                    '事件不可变；请撤回后以新事件表达更正')
            continue
        affected.update(warehouse.add_event(event))
    for event_id in parsed['withdrawals']:
        if event_id in warehouse.withdrawn:
            continue
        affected.update(warehouse.withdraw_event(event_id))
    warehouse.verify()
    if parsed['warehouse_version']:
        if parsed['warehouse_version'] not in warehouse.versions:
            warehouse.publish(parsed['warehouse_version'])
    warehouse.last_affected = sorted(affected)
    return sorted(affected)


def apply_manifest(warehouse, manifest):
    """对已存在的仓增量应用一份清单（同批 ID 重放不重复消耗）。"""
    parsed = parse_manifest(manifest)
    return apply_parsed(warehouse, parsed)
