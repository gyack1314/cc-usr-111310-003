"""核对原始配置及数量，不执行仓库变换。"""
from .io import unique
def summarize(v,root):
    ids=unique(v['events'],'event_id')
    if not isinstance(v['raw_lots'],dict):raise ValueError('原料清单无效')
    for e in v['events']:
        if not e['inputs'] or not e['outputs']:raise ValueError('输入输出不能为空')
    return {'events':len(ids),'raw_lots':len(v['raw_lots']),'batches':len(v['batches'])}
