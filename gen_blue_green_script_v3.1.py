#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝绿发布资源自动生成脚本 - Excel批量版
"""

import os
import sys
import yaml
import argparse
import shutil
import re
from typing import Dict, List, Any, Optional
from copy import deepcopy
import zipfile
from pathlib import Path

# 新增：导入pandas处理Excel
try:
    import pandas as pd
except ImportError:
    print("❌ 缺少依赖库：pandas")
    print("请安装：pip install pandas openpyxl")
    sys.exit(1)


class ServiceConfig:
    """服务配置数据类"""

    def __init__(self, service_name: str, lane: str, mse_ns: str,
                 deployment_yaml: str, service_yaml: str, ingress_yaml: str):
        self.service_name = service_name
        self.lane = lane
        self.mse_ns = mse_ns
        self.deployment_yaml = deployment_yaml
        self.service_yaml = service_yaml
        self.ingress_yaml = ingress_yaml


class BlueGreenGenerator:
    """蓝绿发布资源生成器"""

    def __init__(self, config: Dict[str, Any], service_config: ServiceConfig):
        # 从Excel行数据初始化
        self.service_config = service_config
        self.lane = service_config.lane
        self.agent_mode = config.get('agent_mode', 'java_lite')
        self.mse_ns = service_config.mse_ns

        # Excel源文件路径（保持与之前一致）
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

    def load_from_excel(self):
        """从Excel字段加载YAML内容"""
        print(f"📂 解析服务: {self.service_config.service_name}")

        # 解析Deployment
        try:
            deploy_docs = list(yaml.safe_load_all(self.service_config.deployment_yaml))
            for doc in deploy_docs:
                if doc and doc.get('kind') == 'Deployment':
                    self.deployments.append(doc)
                    self.resource_names['deployment'] = doc['metadata']['name']
                    break
        except yaml.YAMLError as e:
            raise ValueError(f"Deployment YAML解析失败: {e}")

        # 解析Service
        try:
            service_docs = list(yaml.safe_load_all(self.service_config.service_yaml))
            for doc in service_docs:
                if doc and doc.get('kind') == 'Service':
                    self.services.append(doc)
                    self.resource_names['service'] = doc['metadata']['name']
                    break
        except yaml.YAMLError as e:
            raise ValueError(f"Service YAML解析失败: {e}")

        # 解析Ingress（支持多个，用---分隔）
        try:
            # ingress_yaml可能包含多个文档
            ingress_docs = list(yaml.safe_load_all(self.service_config.ingress_yaml))
            for doc in ingress_docs:
                if doc and doc.get('kind') == 'Ingress':
                    self.ingresses.append(doc)
                    self.resource_names['ingress'].append(doc['metadata']['name'])
        except yaml.YAMLError as e:
            raise ValueError(f"Ingress YAML解析失败: {e}")

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
        if 'clusterIP' in service_spec:
            service_spec['clusterIP'] = ''
        if 'clusterIPs' in service_spec:
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

        labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
        match_labels = base_deploy['spec']['selector'].setdefault('matchLabels', {})

        if with_agent:
            # 有 Agent 模式：添加配置
            if 'app' not in labels:
                labels['app'] = self.resource_names['deployment']
            labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
            labels['sidecar.mesh.io/lane'] = 'mse-base'
            labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns

            match_labels['sidecar.mesh.io/lane'] = 'mse-base'

            containers = base_deploy['spec']['template']['spec']['containers']
            for container in containers:
                self._add_configmap_ref(container)
        else:
            # 无 Agent 模式：清理配置
            # 1. 删除 Pod 模板中的 Agent 标签
            agent_label_keys = [
                'sidecar.mesh.io/data-plane-mode',
                'sidecar.mesh.io/lane',
                'sidecar.mesh.io/mse-namespace'
            ]
            for key in agent_label_keys:
                labels.pop(key, None)

            # 2. 删除 selector 中的 lane 标签
            match_labels.pop('sidecar.mesh.io/lane', None)

            # 3. 删除容器中的 NACOS_SUFFIX 环境变量和 configMapRef
            containers = base_deploy['spec']['template']['spec']['containers']
            for container in containers:
                self._remove_agent_env_and_configmap(container)

        # 统一设置副本数和镜像版本（与 Agent 无关）
        base_deploy['spec']['replicas'] = '${replicas}'
        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            container['image'] = self._update_image_tag(container['image'])

        return base_deploy

    def generate_baseline_service(self, with_agent: bool = True) -> Dict:
        """生成基线Service"""
        base_svc = deepcopy(self.services[0])
        selector = base_svc['spec'].setdefault('selector', {})

        if with_agent:
            # 有 Agent 模式：添加 lane 标签
            selector['sidecar.mesh.io/lane'] = 'mse-base'
        else:
            # 无 Agent 模式：删除 lane 标签
            selector.pop('sidecar.mesh.io/lane', None)

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

        def null_representer(dumper, data):
            return dumper.represent_scalar('tag:yaml.org,2002:null', 'null')

        yaml.add_representer(type(None), null_representer)

        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        print(f"   ✓ {os.path.relpath(filepath, os.path.dirname(os.path.dirname(filepath)))}")

    def generate_all(self, output_root: str):
        """生成所有资源文件（改造为按服务隔离目录）"""
        print("\n🚀 开始生成资源文件...")

        # 新增：按service_name创建隔离目录
        output_dir = os.path.join(output_root, self.service_config.service_name)

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

        # 复制全局变量Excel文件
        print(f"\n📊 复制全局变量文件到 {self.service_config.service_name}...")
        self._copy_global_var_files(output_dir)

        # 打包zip
        print(f"\n📦 开始打包 {self.service_config.service_name} 的 zip 压缩包...")
        for env in ['blue', 'green', 'mse-base']:
            env_path = Path(output_dir) / env
            if env_path.is_dir():
                self._zip_environment(env_path, env)

    def _copy_global_var_files(self, output_dir: str):
        """复制全局变量Excel文件到对应目录（保持原有逻辑）"""
        env_dirs = {
            'blue': os.path.join(output_dir, 'blue'),
            'green': os.path.join(output_dir, 'green'),
            'mse-base': os.path.join(output_dir, 'mse-base')
        }

        # blue/green环境
        if os.path.exists(self.bg_xls_path):
            for env in ['blue', 'green']:
                target_path = os.path.join(env_dirs[env], 'global_var.xls')
                shutil.copy2(self.bg_xls_path, target_path)
                print(f"   ✓ 复制到 {os.path.basename(env)}: {target_path}")
        else:
            print(f"   ⚠️  警告: {self.bg_xls_path} 不存在")

        # mse-base环境
        if os.path.exists(self.base_xls_path):
            target_path = os.path.join(env_dirs['mse-base'], 'global_var.xls')
            shutil.copy2(self.base_xls_path, target_path)
            print(f"   ✓ 复制到 mse-base: {target_path}")
        else:
            print(f"   ⚠️  警告: {self.base_xls_path} 不存在")

    def _zip_environment(self, env_dir: Path, env_name: str):
        """将 env_dir 目录下的 resource/ 与 global_var.xls 打成 env_name.zip（保持原有逻辑）"""
        zip_path = env_dir.with_name(f'{env_name}.zip')
        resource_dir = env_dir / 'resource'
        xls_file = env_dir / 'global_var.xls'

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(resource_dir):
                for file in files:
                    full_path = Path(root) / file
                    arc_path = full_path.relative_to(env_dir)
                    zf.write(full_path, arc_path)

            if xls_file.exists():
                zf.write(xls_file, xls_file.name)
            else:
                print(f"   ⚠️  {xls_file.name} 不存在，zip 中将不包含该文件")

        print(f"   ✓ 压缩包已生成: {zip_path}")

    def _remove_agent_env_and_configmap(self, container: Dict):
        """移除容器中的 NACOS_SUFFIX 环境变量和 mse-publish-gray configMapRef"""
        # 移除 NACOS_SUFFIX 环境变量
        env_list = container.get('env', [])
        container['env'] = [env for env in env_list if env.get('name') != 'NACOS_SUFFIX']

        # 移除 mse-publish-gray configMapRef
        env_from_list = container.get('envFrom', [])
        container['envFrom'] = [
            item for item in env_from_list
            if not ('configMapRef' in item and item['configMapRef'].get('name') == 'mse-publish-gray')
        ]

def load_config(config_file: str) -> Dict[str, Any]:
    """加载配置文件（保持不变）"""
    if not os.path.exists(config_file):
        print(f"⚠️  配置文件不存在: {config_file}, 使用默认配置")
        return {
            'lane': 'key2',
            'agent_mode': 'java_lite',
            'mse_ns': 'mse-default'
        }

    with open(config_file, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def process_excel(excel_path: str, config: Dict[str, Any], output_root: str):
    """主处理函数：读取Excel并批量生成服务"""
    print(f"\n📊 读取Excel文件: {excel_path}")

    try:
        # 读取Excel，跳过第2行（说明行）
        df = pd.read_excel(excel_path, header=0, skiprows=[1])
    except Exception as e:
        print(f"❌ Excel读取失败: {e}")
        sys.exit(1)

    # 校验必要列
    required_columns = ['service_name', 'lane', 'mse_ns', 'deployment_yaml', 'service_yaml', 'ingress_yaml']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        print(f"❌ Excel缺少必要列: {missing_columns}")
        sys.exit(1)

    # 逐行处理每个服务
    for idx, row in df.iterrows():
        service_name = row['service_name']
        if pd.isna(service_name):
            print(f"⚠️  第{idx + 3}行 service_name 为空，跳过")
            continue

        print("\n" + "=" * 60)
        print(f"🚀 开始处理服务 [{service_name}]")
        print("=" * 60)

        try:
            # 创建服务配置对象
            service_config = ServiceConfig(
                service_name=str(service_name).strip(),
                lane=str(row['lane']),
                mse_ns=str(row['mse_ns']),
                deployment_yaml=str(row['deployment_yaml']),
                service_yaml=str(row['service_yaml']),
                ingress_yaml=str(row['ingress_yaml'])
            )

            # 生成器实例化并执行
            generator = BlueGreenGenerator(config, service_config)
            generator.load_from_excel()  # 从Excel加载YAML
            generator.generate_all(output_root)

            print(f"\n✅ 服务 [{service_name}] 处理完成")

        except Exception as e:
            print(f"\n❌ 服务 [{service_name}] 处理失败: {e}")
            import traceback
            traceback.print_exc()
            continue


def main():
    parser = argparse.ArgumentParser(
        description='蓝绿发布资源自动生成脚本 - Excel批量版',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --excel ./services.xlsx --output ./output --config ./config.yaml
  %(prog)s -e ./services.xlsx -o ./output
        """
    )

    # 参数改为excel文件路径
    parser.add_argument('-e', '--excel', default='./config/services.xlsx',
                        help='服务定义Excel文件路径（含deployment/service/ingress YAML）')
    parser.add_argument('-o', '--output', default='./output',
                        help='输出根目录路径 (默认: ./output)')
    parser.add_argument('-c', '--config', default='./config/config.yaml',
                        help='配置文件路径 (默认: ./config/config.yaml)')

    args = parser.parse_args()

    try:
        print("=" * 60)
        print("🎯 蓝绿发布资源自动生成脚本 - Excel批量版")
        print("=" * 60)

        # 加载通用配置（agent_mode等）
        config = load_config(args.config)
        print(f"\n⚙️  全局配置:")
        print(f"   Agent模式: {config.get('agent_mode')}")

        # 处理Excel批量生成
        process_excel(args.excel, config, args.output)

        print("\n" + "=" * 60)
        print("🎉 全部服务处理完成!")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ 错误: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()