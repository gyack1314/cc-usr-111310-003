"""冻结基准单位下的有理数量：字符串与 Fraction 互转。

资料约定：非负有理数字符串（如 "100"、"1/3"），禁止浮点、负值、零分母。
JSON 里的 bool 不是合法数量，显式拒绝。
"""
import re
from fractions import Fraction

_TOKEN = re.compile(r'\d+(?:/\d+)?')


def parse_quantity(value, *, positive=False):
    """把清单字符串转成 Fraction；positive=True 时要求严格为正。"""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError('数量必须是有理数字符串，禁止浮点或布尔')
    text = value.strip()
    if not _TOKEN.fullmatch(text):
        raise ValueError(f'数量格式无效: {value!r}')
    if '/' in text:
        numerator, denominator = text.split('/')
        if int(denominator) <= 0:
            raise ValueError('分母必须为正')
        amount = Fraction(int(numerator), int(denominator))
    else:
        amount = Fraction(int(text))
    if positive and amount <= 0:
        raise ValueError('输入与输出量必须为正')
    if amount < 0:
        raise ValueError('数量不能为负')
    return amount


def format_quantity(amount):
    """Fraction 转回契约字符串：整数无分母，其余保留精确分数。"""
    amount = Fraction(amount)
    if amount.denominator == 1:
        return str(amount.numerator)
    return f'{amount.numerator}/{amount.denominator}'
