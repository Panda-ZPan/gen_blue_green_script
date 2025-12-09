#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝绿发布资源自动生成脚本 - 强制 null 值版
"""

import os
import sys
import yaml
import argparse
import shutil
import re
from typing import Dict, List, Any
from copy import deepcopy
import zipfile
from pathlib import Path


class BlueGreenGenerator:
    """蓝绿发布资源生成器"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.lane = config.get('lane', 'key2')
        self.agent_mode = config.get('agent_mode', 'java_lite')
        self.mse_ns = config.get('mse_ns', 'mse-default')

        # Excel源文件路径
        self.base_xls_path = './config/base_global_var.xls'
        self.bg_xls_path = './config/bg_global_var.xls'

        # 资源存储
        self.deployments: List[Dict] = []
        self.services: List[Dict] = []
        self.ingresses: List[Dict] = []

        # 资源名称映射
        self.resource_names = {
            'deployment': None,
            'service': None,
            'ingress': []
        }

    def load_baseline(self, baseline_dir: str):
        """加载基线YAML文件"""
        print(f"📂 加载基线文件: {baseline_dir}")

        if not os.path.exists(baseline_dir):
            raise FileNotFoundError(f"基线目录不存在: {baseline_dir}")

        for filename in os.listdir(baseline_dir):
            if not filename.endswith(('.yaml', '.yml')):
                continue

            filepath = os.path.join(baseline_dir, filename)
            print(f"   读取: {filename}")

            with open(filepath, 'r', encoding='utf-8') as f:
                docs = list(yaml.safe_load_all(f))

            for doc in docs:
                if not doc or 'kind' not in doc:
                    continue

                kind = doc['kind']
                if kind == 'Deployment':
                    self.deployments.append(doc)
                    if not self.resource_names['deployment']:
                        self.resource_names['deployment'] = doc['metadata']['name']
                elif kind == 'Service':
                    self.services.append(doc)
                    if not self.resource_names['service']:
                        self.resource_names['service'] = doc['metadata']['name']
                elif kind == 'Ingress':
                    self.ingresses.append(doc)
                    self.resource_names['ingress'].append(doc['metadata']['name'])

        # 校验
        if not self.deployments:
            raise ValueError("未找到 Deployment 资源")
        if not self.services:
            raise ValueError("未找到 Service 资源")
        if not self.ingresses:
            raise ValueError("未找到 Ingress 资源")

        print(f"✅ 加载完成: Deployment({len(self.deployments)}), "
              f"Service({len(self.services)}), Ingress({len(self.ingresses)})")
        print(f"   资源名称: deployment={self.resource_names['deployment']}, "
              f"service={self.resource_names['service']}, ingress={self.resource_names['ingress']}")

    def _update_image_tag(self, image: str) -> str:
        """仅替换镜像版本号为 ${imageversion}"""
        pattern = r'^(.*:).*$'
        match = re.match(pattern, image)
        if match:
            return f"{match.group(1)}${{imageversion}}"
        return f"{image}:${{imageversion}}"

    def _add_configmap_ref(self, container: Dict):
        """添加 configMapRef（去重）"""
        env_from = container.setdefault('envFrom', [])
        for item in env_from:
            if 'configMapRef' in item and item['configMapRef'].get('name') == 'mse-publish-gray':
                return
        env_from.append({'configMapRef': {'name': 'mse-publish-gray'}})

    def _clean_cluster_ip(self, service_spec: Dict):
        """清理 clusterIP 的值，仅保留 key"""
        if 'clusterIP' in service_spec and service_spec['clusterIP']:
            service_spec['clusterIP'] = ''
        if 'clusterIPs' in service_spec and service_spec['clusterIPs']:
            service_spec['clusterIPs'] = []

    def generate_blue_green_deployment(self, env: str) -> Dict:
        """生成蓝绿环境的Deployment"""
        base_deploy = deepcopy(self.deployments[0])
        deploy_name = self.resource_names['deployment']

        base_deploy['metadata']['name'] = f"{deploy_name}-{env}"

        labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
        if 'app' not in labels:
            labels['app'] = deploy_name
        labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
        labels['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"
        labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns

        base_deploy['spec']['replicas'] = '${replicas}'
        base_deploy['spec']['selector']['matchLabels']['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"

        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            env_vars = container.setdefault('env', [])
            env_vars.append({
                'name': 'NACOS_SUFFIX',
                'value': f'.{env}'
            })

            self._add_configmap_ref(container)
            container['image'] = self._update_image_tag(container['image'])

        return base_deploy

    def generate_blue_green_service(self, env: str) -> Dict:
        """生成蓝绿环境的Service"""
        base_svc = deepcopy(self.services[0])
        svc_name = self.resource_names['service']

        base_svc['metadata']['name'] = f"{svc_name}-{env}"
        base_svc['spec']['selector']['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"
        self._clean_cluster_ip(base_svc['spec'])

        return base_svc

    def generate_ingress(self, base_ingress: Dict, ingress_type: str, env: str) -> Dict:
        """生成不同类型的Ingress (强制 null 值)"""
        ingress = deepcopy(base_ingress)
        ingress_name = ingress['metadata']['name']
        reenv = 'green' if env == 'blue' else 'blue'

        annotations = ingress['metadata'].setdefault('annotations', {})

        if ingress_type == 'user_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
                'k8s.apisix.apache.org/priority': '20',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{env}"}}',
                'k8s.apisix.apache.org/filter-headers': '{"x-yun-gray":["1"],"x-yun-uni":${users}}',
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })

        elif ingress_type == 'uid_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
                'k8s.apisix.apache.org/priority': '20',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{env}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': '[{"x-yun-uni":"${uid_precentage}"}]',
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })

        elif ingress_type == 'nouid_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{reenv}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
                'k8s.apisix.apache.org/priority': '5',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{reenv}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': f'{{"baggage": "meshEnv={self.lane}-{env}"}}',
                'k8s.apisix.apache.org/traffic-split-percentage': '${nouid_precentage}',
                'k8s.apisix.apache.org/traffic-split-service': f"{self.resource_names['service']}-{env}"
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{reenv}")

        elif ingress_type == 'all_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
                'k8s.apisix.apache.org/priority': '10',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{env}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{env}")

        elif ingress_type == 'close':
            ingress['metadata']['name'] = f"{ingress_name}-{reenv}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'false',
                'k8s.apisix.apache.org/priority': '20',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{reenv}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{reenv}")

        return ingress

    def _update_ingress_backend(self, ingress: Dict, service_name: str):
        """更新Ingress的backend service名称"""
        rules = ingress.get('spec', {}).get('rules', [])
        for rule in rules:
            paths = rule.get('http', {}).get('paths', [])
            for path in paths:
                if 'backend' in path:
                    if 'serviceName' in path['backend']:
                        path['backend']['serviceName'] = service_name
                    elif 'service' in path['backend']:
                        path['backend']['service']['name'] = service_name

    def generate_baseline_deployment(self, with_agent: bool = True) -> Dict:
        """生成基线Deployment"""
        base_deploy = deepcopy(self.deployments[0])

        if with_agent:
            labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
            if 'app' not in labels:
                labels['app'] = self.resource_names['deployment']
            labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
            labels['sidecar.mesh.io/lane'] = 'mse-base'
            labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns

            base_deploy['spec']['selector']['matchLabels']['sidecar.mesh.io/lane'] = 'mse-base'

            containers = base_deploy['spec']['template']['spec']['containers']
            for container in containers:
                self._add_configmap_ref(container)
                container['image'] = self._update_image_tag(container['image'])

        base_deploy['spec']['replicas'] = '${replicas}'

        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            container['image'] = self._update_image_tag(container['image'])

        return base_deploy

    def generate_baseline_service(self, with_agent: bool = True) -> Dict:
        """生成基线Service"""
        base_svc = deepcopy(self.services[0])

        if with_agent:
            base_svc['spec']['selector']['sidecar.mesh.io/lane'] = 'mse-base'

        self._clean_cluster_ip(base_svc['spec'])

        return base_svc

    def generate_baseline_ingress(self, base_ingress: Dict) -> Dict:
        """生成基线Ingress (强制 null 值)"""
        ingress = deepcopy(base_ingress)

        annotations = ingress['metadata'].setdefault('annotations', {})
        annotations.update({
            'k8s.apisix.apache.org/enable': '${apisix_switch}',
            'k8s.apisix.apache.org/priority': '0',
            'k8s.apisix.apache.org/set-headers': None,
            'k8s.apisix.apache.org/filter-headers': None,
            'k8s.apisix.apache.org/filter-headers-expr': None,
            'k8s.apisix.apache.org/traffic-split-headers': None,
            'k8s.apisix.apache.org/traffic-split-percentage': None,
            'k8s.apisix.apache.org/traffic-split-service': None
        })

        return ingress

    def save_yaml(self, data: Dict, filepath: str):
        """保存YAML文件 (强制 null 值显示为 null)"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # 关键：强制 None 显示为 null
        def null_representer(dumper, data):
            return dumper.represent_scalar('tag:yaml.org,2002:null', 'null')

        yaml.add_representer(type(None), null_representer)

        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        print(f"   ✓ {os.path.relpath(filepath, os.path.dirname(os.path.dirname(filepath)))}")

    def generate_all(self, output_dir: str):
        """生成所有资源文件"""
        print("\n🚀 开始生成资源文件...")

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)

        deploy_name = self.resource_names['deployment']
        svc_name = self.resource_names['service']

        # 生成Blue环境
        print("\n📘 生成 Blue 环境资源:")
        blue_dir = os.path.join(output_dir, 'blue', 'resource')

        blue_deploy = self.generate_blue_green_deployment('blue')
        self.save_yaml(blue_deploy, os.path.join(blue_dir, f'Deployment/[blue]{deploy_name}.yaml'))

        green_recycle = self.generate_blue_green_deployment('green')
        green_recycle['spec']['replicas'] = 0
        self.save_yaml(green_recycle, os.path.join(blue_dir, f'Deployment/[green]{deploy_name}.yaml'))

        blue_svc = self.generate_blue_green_service('blue')
        self.save_yaml(blue_svc, os.path.join(blue_dir, f'Service/[blue]{svc_name}.yaml'))

        for idx, ingress_name in enumerate(self.resource_names['ingress']):
            base_ingress = self.ingresses[idx]
            self.save_yaml(self.generate_ingress(base_ingress, 'user_switch', 'blue'),
                           os.path.join(blue_dir, f'Ingress/[blue]user_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'uid_switch', 'blue'),
                           os.path.join(blue_dir, f'Ingress/[blue]uid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'nouid_switch', 'blue'),
                           os.path.join(blue_dir, f'Ingress/[blue]nouid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'all_switch', 'blue'),
                           os.path.join(blue_dir, f'Ingress/[blue]all_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'close', 'blue'),
                           os.path.join(blue_dir, f'Ingress/[green]close_{ingress_name}.yaml'))

        # 生成Green环境
        print("\n📗 生成 Green 环境资源:")
        green_dir = os.path.join(output_dir, 'green', 'resource')

        green_deploy = self.generate_blue_green_deployment('green')
        self.save_yaml(green_deploy, os.path.join(green_dir, f'Deployment/[green]{deploy_name}.yaml'))

        blue_recycle = self.generate_blue_green_deployment('blue')
        blue_recycle['spec']['replicas'] = 0
        self.save_yaml(blue_recycle, os.path.join(green_dir, f'Deployment/[blue]{deploy_name}.yaml'))

        green_svc = self.generate_blue_green_service('green')
        self.save_yaml(green_svc, os.path.join(green_dir, f'Service/[green]{svc_name}.yaml'))

        for idx, ingress_name in enumerate(self.resource_names['ingress']):
            base_ingress = self.ingresses[idx]
            self.save_yaml(self.generate_ingress(base_ingress, 'user_switch', 'green'),
                           os.path.join(green_dir, f'Ingress/[green]user_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'uid_switch', 'green'),
                           os.path.join(green_dir, f'Ingress/[green]uid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'nouid_switch', 'green'),
                           os.path.join(green_dir, f'Ingress/[green]nouid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'all_switch', 'green'),
                           os.path.join(green_dir, f'Ingress/[green]all_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'close', 'green'),
                           os.path.join(green_dir, f'Ingress/[blue]close_{ingress_name}.yaml'))

        # 生成基线环境
        print("\n📙 生成基线环境资源:")
        base_dir = os.path.join(output_dir, 'mse-base', 'resource')

        self.save_yaml(self.generate_baseline_deployment(with_agent=True),
                       os.path.join(base_dir, f'Deployment/[mse-base]{deploy_name}.yaml'))
        self.save_yaml(self.generate_baseline_deployment(with_agent=False),
                       os.path.join(base_dir, f'Deployment/[mse-base]nojavaagent-{deploy_name}.yaml'))

        self.save_yaml(self.generate_baseline_service(with_agent=True),
                       os.path.join(base_dir, f'Service/[mse-base]{svc_name}.yaml'))
        self.save_yaml(self.generate_baseline_service(with_agent=False),
                       os.path.join(base_dir, f'Service/[mse-base]nojavaagent-{svc_name}.yaml'))

        for idx, ingress_name in enumerate(self.resource_names['ingress']):
            base_ingress = self.ingresses[idx]
            self.save_yaml(self.generate_baseline_ingress(base_ingress),
                           os.path.join(base_dir, f'Ingress/[mse-base]{ingress_name}.yaml'))

        # 统计文件数
        def count_files(directory):
            if not os.path.exists(directory):
                return 0
            return sum(len(files) for _, _, files in os.walk(directory))

        blue_count = count_files(os.path.join(output_dir, 'blue'))
        green_count = count_files(os.path.join(output_dir, 'green'))
        base_count = count_files(os.path.join(output_dir, 'mse-base'))

        print(f"\n✅ 所有资源文件已生成到: {output_dir}")
        print(f"   Blue 环境: {blue_count} 个文件")
        print(f"   Green 环境: {green_count} 个文件")
        print(f"   基线环境: {base_count} 个文件")

        # 新增：复制全局变量Excel文件
        print("\n📊 复制全局变量文件...")
        self._copy_global_var_files(output_dir)

        print("\n📦 开始打包 zip 压缩包...")
        for env in ['blue', 'green', 'mse-base']:
            env_path = Path(output_dir) / env
            if env_path.is_dir():
                self._zip_environment(env_path, env)

    def _copy_global_var_files(self, output_dir: str):
        """复制全局变量Excel文件到对应目录"""
        # 定义目标环境目录
        env_dirs = {
            'blue': os.path.join(output_dir, 'blue'),
            'green': os.path.join(output_dir, 'green'),
            'mse-base': os.path.join(output_dir, 'mse-base')
        }

        # 为blue和green环境复制bg_global_var.xls
        if os.path.exists(self.bg_xls_path):
            for env in ['blue', 'green']:
                target_path = os.path.join(env_dirs[env], 'global_var.xls')
                shutil.copy2(self.bg_xls_path, target_path)
                print(f"   ✓ 复制 {self.bg_xls_path} -> {target_path}")
        else:
            print(f"   ⚠️  警告: {self.bg_xls_path} 不存在，跳过blue/green环境")

        # 为mse-base环境复制base_global_var.xls
        if os.path.exists(self.base_xls_path):
            target_path = os.path.join(env_dirs['mse-base'], 'global_var.xls')
            shutil.copy2(self.base_xls_path, target_path)
            print(f"   ✓ 复制 {self.base_xls_path} -> {target_path}")
        else:
            print(f"   ⚠️  警告: {self.base_xls_path} 不存在，跳过mse-base环境")

    def _zip_environment(self, env_dir: Path, env_name: str):
        """将 env_dir 目录下的 resource/ 与 global_var.xls 打成 env_name.zip"""
        zip_path = env_dir.with_name(f'{env_name}.zip')  # 同级目录生成 xxx.zip
        resource_dir = env_dir / 'resource'
        xls_file = env_dir / 'global_var.xls'

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # ① 打包 resource 整个目录
            for root, _, files in os.walk(resource_dir):
                for file in files:
                    full_path = Path(root) / file
                    arc_path = full_path.relative_to(env_dir)  # 保持目录层级
                    zf.write(full_path, arc_path)

            # ② 打包 global_var.xls（若存在）
            if xls_file.exists():
                zf.write(xls_file, xls_file.name)
            else:
                print(f"   ⚠️  {xls_file.name} 不存在，zip 中将不包含该文件")

        print(f"   ✓ 压缩包已生成: {zip_path}")


def load_config(config_file: str) -> Dict[str, Any]:
    """加载配置文件"""
    if not os.path.exists(config_file):
        print(f"⚠️  配置文件不存在: {config_file}, 使用默认配置")
        return {
            'lane': 'key2',
            'agent_mode': 'java_lite',
            'mse_ns': 'mse-default'
        }

    with open(config_file, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)



def main():
    parser = argparse.ArgumentParser(
        description='蓝绿发布资源自动生成脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --baseline ./baseline --output ./output --config ./config.yaml
  %(prog)s -b ./baseline -o ./output
        """
    )

    parser.add_argument('-b', '--baseline', default='./k8s_yaml/',
                        help='基线资源目录路径')
    parser.add_argument('-o', '--output', default='./output',
                        help='输出目录路径 (默认: ./output)')
    parser.add_argument('-c', '--config', default='./config/config.yaml',
                        help='配置文件路径 (默认: ./config.yaml)')

    args = parser.parse_args()

    try:
        print("=" * 60)
        print("🎯 蓝绿发布资源自动生成脚本")
        print("=" * 60)

        config = load_config(args.config)
        print(f"\n⚙️  配置信息:")
        print(f"   泳道: {config.get('lane')}")
        print(f"   Agent模式: {config.get('agent_mode')}")
        print(f"   MSE命名空间: {config.get('mse_ns')}")

        generator = BlueGreenGenerator(config)
        generator.load_baseline(args.baseline)
        generator.generate_all(args.output)

        print("\n" + "=" * 60)
        print("🎉 生成完成!")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ 错误: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()