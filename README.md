# 制造批次谱系桥表的数量闭包维护

此项目保存生产与供应链协同的湖仓原始资料。`warehouse_data.io`读取JSON清单及核对文件身份，`warehouse_data.domain`检查基本形状并输出数量，`warehouse_data.cli`展示摘要。[资料约定](docs/data-contract.md)定义字段、类型、时间、数量和版本范围。设备、批次、组织、事件和成本均为合成，不连接真实工厂或生产仓库。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse_data tests
python -m warehouse_data.cli inspect data/normal.json
python -m warehouse_data.cli build data/normal.json
```

`inspect`只读原始资料，不生成仓库新版本或计算领域结果。`build`构建带数量的批次谱系事实仓，输出桥表（root_lot, descendant_lot, 精确贡献量与比例）、批余额、逐段分摊路径、损耗去向、守恒账与受影响批清单；构建失败（超额消耗、撤回使下游失去来源、环、重复生产者等）以退出码2拒绝且不改账。`build 新清单.json --apply-to 基线清单.json`在既有仓上增量应用，同批ID重放幂等、不重复消耗。桥表语义见[资料约定](docs/data-contract.md)。`invalid-config.json`有基本形状错误；其余清单有完整资料身份，但可能包含需要业务操作判断的冲突、缺口或不满足条件的边界。null与缺字段分别保留，有理数和decimal用字符串存储，不能先经float丢精度。

Dockerfile固定Linux镜像，`docker build -t warehouse-material .`构建；`docker run --rm warehouse-material`显示摘要；`docker run --rm warehouse-material python -m unittest discover -s tests -v`运行已有检查。所有资料本地可读，不需要账号、外部数据库或在线数据源。
