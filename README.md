# Blue-Green Release Config Generator

> 针对 Kubernetes + MSE 服务网格场景的蓝绿发布资源批量生成工具

## 背景

在 Kubernetes 全链路灰度/蓝绿发布方案落地过程中，每个业务系统需要手动配置
Deployment、Service、Ingress 共 200+ 个参数项，人工操作耗时 2-3 人天，
且因配置错误频繁导致发布窗口延误。

本工具通过读取 Excel 中的业务系统定义，自动生成符合 MSE 服务网格规范的
全套 Kubernetes YAML 资源文件，并按环境打包为 zip 供直接导入发布平台。

## 效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 单服务配置耗时 | 2-3 人天 | ~1 小时 |
| 配置出错率 | 高（人工填写） | 接近 0 |
| 批量处理能力 | 逐个手动 | Excel 批量输入 |

## 生成的资源结构
```
output/
└── {service_name}/
    ├── blue/
    │   ├── resource/
    │   │   ├── Deployment/
    │   │   ├── Service/
    │   │   └── Ingress/        # user_switch / uid_switch / nouid_switch / all_switch / close
    │   └── global_var.xls
    ├── green/                  # 同上
    └── mse-base/               # 基线环境资源
```

## 支持的流量切分模式

| Ingress 类型 | 说明 |
|---|---|
| `user_switch` | 按用户白名单（x-yun-uni Header）切流 |
| `uid_switch` | 按用户 ID 百分比切流 |
| `nouid_switch` | 按无 UID 流量百分比切流 |
| `all_switch` | 全量切换至目标环境 |
| `close` | 关闭灰度，流量回退基线 |

## 快速开始
```bash
# 安装依赖
pip install -r requirements.txt

# 运行（使用默认路径）
python gen_blue_green_script_v3.3.py

# 自定义路径
python gen_blue_green_script_v3.3.py \
  --excel ./config/services.xlsx \
  --output ./output \
  --config ./config/config.yaml
```

## 配置文件示例
```yaml
# config/config.yaml
lane: key2
agent_mode: java_lite
mse_ns: mse-default
blue_name: blue
green_name: green
```

## 技术栈

Python 3.x · Kubernetes · MSE 服务网格 · APISIX Ingress · YAML · pandas
