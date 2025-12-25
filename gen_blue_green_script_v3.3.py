#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝绿发布资源自动生成脚本 - Excel批量版（支持自定义蓝/绿环境名称）
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

try:
    import pandas as pd
except ImportError:
    print("❌ 缺少依赖库：pandas")
    print("请安装：pip install pandas openpyxl")
    sys.exit(1)


# ---- 新增：强制单引号字符串 ----
class SingleQuotedStr(str):
    pass


def single_quoted_str_representer(dumper, data):
    return dumper.represent_scalar(
        'tag:yaml.org,2002:str',
        data,
        style="'"
    )
yaml.add_representer(SingleQuotedStr, single_quoted_str_representer)

# --------------------------------

# ---------- 数据类 ----------
class ServiceConfig:
    def __init__(self, service_name: str, lane: str, mse_ns: str,
                 deployment_yaml: str, service_yaml: str, ingress_yaml: str):
        self.service_name = service_name
        self.lane = lane
        self.mse_ns = mse_ns
        self.deployment_yaml = deployment_yaml
        self.service_yaml = service_yaml
        self.ingress_yaml = ingress_yaml


# ---------- 生成器 ----------
class BlueGreenGenerator:
    def __init__(self, config: Dict[str, Any], service_config: ServiceConfig):
        self.service_config = service_config
        self.lane = service_config.lane
        self.agent_mode = config.get('agent_mode', 'java_lite')
        self.mse_ns = service_config.mse_ns
        # ↓↓↓ 新增：读取可配置的环境名称 ↓↓↓
        self.blue_name = config.get('blue_name', 'blue')
        self.green_name = config.get('green_name', 'green')
        self.blue_env = config.get('blue_env', 'blue')
        self.green_env = config.get('green_env', 'green')

        self.base_xls_path = './config/base_global_var.xls'
        self.bg_xls_path = './config/bg_global_var.xls'

        self.deployments: List[Dict] = []
        self.services: List[Dict] = []
        self.ingresses: List[Dict] = []

        self.resource_names = {
            'deployment': None,
            'service': None,
            'ingress': []
        }

    # ---------------- 以下所有方法与您 v3.1 完全一致 ----------------
    def load_from_excel(self):
        print(f"📂 解析服务: {self.service_config.service_name}")
        try:
            deploy_docs = list(yaml.safe_load_all(self.service_config.deployment_yaml))
            for doc in deploy_docs:
                if doc and doc.get('kind') == 'Deployment':
                    self.deployments.append(doc)
                    self.resource_names['deployment'] = doc['metadata']['name']
                    break
        except yaml.YAMLError as e:
            raise ValueError(f"Deployment YAML解析失败: {e}")
        try:
            service_docs = list(yaml.safe_load_all(self.service_config.service_yaml))
            for doc in service_docs:
                if doc and doc.get('kind') == 'Service':
                    self.services.append(doc)
                    self.resource_names['service'] = doc['metadata']['name']
                    break
        except yaml.YAMLError as e:
            raise ValueError(f"Service YAML解析失败: {e}")
        try:
            ingress_docs = list(yaml.safe_load_all(self.service_config.ingress_yaml))
            for doc in ingress_docs:
                if doc and doc.get('kind') == 'Ingress':
                    self.ingresses.append(doc)
                    self.resource_names['ingress'].append(doc['metadata']['name'])
        except yaml.YAMLError as e:
            raise ValueError(f"Ingress YAML解析失败: {e}")
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
        pattern = r'^(.*:).*$'
        match = re.match(pattern, image)
        if match:
            return f"{match.group(1)}${{imageversion}}"
        return f"{image}:${{imageversion}}"

    def _add_configmap_ref(self, container: Dict):
        env_from = container.setdefault('envFrom', [])
        for item in env_from:
            if 'configMapRef' in item and item['configMapRef'].get('name') == 'mse-publish-gray':
                return
        env_from.append({'configMapRef': {'name': 'mse-publish-gray'}})

    def _clean_cluster_ip(self, service_spec: Dict):
        if 'clusterIP' in service_spec:
            service_spec['clusterIP'] = ''
        if 'clusterIPs' in service_spec:
            service_spec['clusterIPs'] = []

    def generate_blue_green_deployment(self, env: str, laneenv: str) -> Dict:
        base_deploy = deepcopy(self.deployments[0])
        deploy_name = self.resource_names['deployment']
        base_deploy['metadata']['name'] = f"{deploy_name}-{env}"
        labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
        if 'app' not in labels:
            labels['app'] = deploy_name
        labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
        labels['sidecar.mesh.io/lane'] = f"{self.lane}-{laneenv}"
        labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns
        base_deploy['spec']['replicas'] = '${replicas}'
        base_deploy['spec']['selector']['matchLabels']['sidecar.mesh.io/lane'] = f"{self.lane}-{laneenv}"
        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            env_vars = container.setdefault('env', [])
            env_vars.append({'name': 'NACOS_SUFFIX', 'value': f'.{env}'})
            self._add_configmap_ref(container)
            container['image'] = self._update_image_tag(container['image'])
        return base_deploy

    def generate_blue_green_service(self, env: str, laneenv: str) -> Dict:
        base_svc = deepcopy(self.services[0])
        svc_name = self.resource_names['service']
        base_svc['metadata']['name'] = f"{svc_name}-{env}"
        base_svc['spec']['selector']['sidecar.mesh.io/lane'] = f"{self.lane}-{laneenv}"
        self._clean_cluster_ip(base_svc['spec'])
        return base_svc

    def generate_ingress(self, base_ingress: Dict, ingress_type: str, env: str, laneenv: str) -> Dict:
        ingress = deepcopy(base_ingress)
        ingress_name = ingress['metadata']['name']
        reenv = self.green_env if env == self.blue_env else self.blue_env
        relaneenv = self.green_name if laneenv == self.blue_name else self.blue_name
        annotations = ingress['metadata'].setdefault('annotations', {})
        if ingress_type == 'user_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
		'k8s.apisix.apache.org/priority': '20',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{laneenv}"}}',
                'k8s.apisix.apache.org/filter-headers': '{"x-yun-gray":["1"],"x-yun-uni":${users}}',
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{env}")
        elif ingress_type == 'uid_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
		'k8s.apisix.apache.org/priority': '20',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{laneenv}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': '[{"x-yun-uni":"${uid_precentage}"}]',
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{env}")
        elif ingress_type == 'nouid_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{reenv}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
		'k8s.apisix.apache.org/priority': '5',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{relaneenv}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': f'{{"baggage": "meshEnv={self.lane}-{laneenv}"}}',
                'k8s.apisix.apache.org/traffic-split-percentage': SingleQuotedStr('${nouid_precentage}'),
                'k8s.apisix.apache.org/traffic-split-service': f"{self.resource_names['service']}-{env}"
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{reenv}")
        elif ingress_type == 'all_switch':
            ingress['metadata']['name'] = f"{ingress_name}-{env}"
            annotations.update({
                'k8s.apisix.apache.org/enable': 'true',
		'k8s.apisix.apache.org/priority': '10',
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{laneenv}"}}',
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
                'k8s.apisix.apache.org/set-headers': f'{{"baggage": "meshEnv={self.lane}-{relaneenv}"}}',
                'k8s.apisix.apache.org/filter-headers': None,
                'k8s.apisix.apache.org/filter-headers-expr': None,
                'k8s.apisix.apache.org/traffic-split-headers': None,
                'k8s.apisix.apache.org/traffic-split-percentage': None,
                'k8s.apisix.apache.org/traffic-split-service': None
            })
            self._update_ingress_backend(ingress, f"{self.resource_names['service']}-{reenv}")
        return ingress

    def _update_ingress_backend(self, ingress: Dict, service_name: str):
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
        base_deploy = deepcopy(self.deployments[0])
        labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
        match_labels = base_deploy['spec']['selector'].setdefault('matchLabels', {})
        if with_agent:
            if 'app' not in labels:
                labels['app'] = self.resource_names['deployment']
            labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
            labels['sidecar.mesh.io/lane'] = 'mse-base'
            labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns
            match_labels['sidecar.mesh.io/lane'] = 'mse-base'
            for container in base_deploy['spec']['template']['spec']['containers']:
                self._add_configmap_ref(container)
        else:
            agent_label_keys = [
                'sidecar.mesh.io/data-plane-mode',
                'sidecar.mesh.io/lane',
                'sidecar.mesh.io/mse-namespace'
            ]
            for key in agent_label_keys:
                labels.pop(key, None)
            match_labels.pop('sidecar.mesh.io/lane', None)
            for container in base_deploy['spec']['template']['spec']['containers']:
                self._remove_agent_env_and_configmap(container)
        base_deploy['spec']['replicas'] = '${replicas}'
        for container in base_deploy['spec']['template']['spec']['containers']:
            container['image'] = self._update_image_tag(container['image'])
        return base_deploy

    def generate_baseline_service(self, with_agent: bool = True) -> Dict:
        base_svc = deepcopy(self.services[0])
        selector = base_svc['spec'].setdefault('selector', {})
        if with_agent:
            selector['sidecar.mesh.io/lane'] = 'mse-base'
        else:
            selector.pop('sidecar.mesh.io/lane', None)
        self._clean_cluster_ip(base_svc['spec'])
        return base_svc

    def generate_baseline_ingress(self, base_ingress: Dict) -> Dict:
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

        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        def null_representer(dumper, data):
            return dumper.represent_scalar('tag:yaml.org,2002:null', 'null')
        yaml.add_representer(type(None), null_representer)
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"   ✓ {os.path.relpath(filepath, os.path.dirname(os.path.dirname(filepath)))}")

    # ---------------- 唯一目录名改动点 ----------------
    def generate_all(self, output_root: str):
        print("\n🚀 开始生成资源文件...")
        output_dir = os.path.join(output_root, self.service_config.service_name)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        deploy_name = self.resource_names['deployment']
        svc_name = self.resource_names['service']

        # Blue 环境目录名改用 self.blue_name
        print(f"\n📘 生成 {self.blue_name} 环境资源:")
        blue_dir = os.path.join(output_dir, self.blue_env, 'resource')
        blue_deploy = self.generate_blue_green_deployment(self.blue_env, self.blue_name)
        self.save_yaml(blue_deploy, os.path.join(blue_dir, f'Deployment/[blue]{deploy_name}.yaml'))
        green_recycle = self.generate_blue_green_deployment(self.green_env, self.green_name)
        green_recycle['spec']['replicas'] = 0
        self.save_yaml(green_recycle, os.path.join(blue_dir, f'Deployment/[green]{deploy_name}.yaml'))
        blue_svc = self.generate_blue_green_service(self.blue_env, self.blue_name)
        self.save_yaml(blue_svc, os.path.join(blue_dir, f'Service/[blue]{svc_name}.yaml'))
        for idx, ingress_name in enumerate(self.resource_names['ingress']):
            base_ingress = self.ingresses[idx]
            self.save_yaml(self.generate_ingress(base_ingress, 'user_switch', self.blue_env, self.blue_name),
                           os.path.join(blue_dir, f'Ingress/[blue]user_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'uid_switch', self.blue_env, self.blue_name),
                           os.path.join(blue_dir, f'Ingress/[blue]uid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'nouid_switch', self.blue_env, self.blue_name),
                           os.path.join(blue_dir, f'Ingress/[blue]nouid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'all_switch', self.blue_env, self.blue_name),
                           os.path.join(blue_dir, f'Ingress/[blue]all_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'close', self.blue_env, self.blue_name),
                           os.path.join(blue_dir, f'Ingress/[green]close_{ingress_name}.yaml'))

        # Green 环境目录名改用 self.green_name
        print(f"\n📗 生成 {self.green_name} 环境资源:")
        green_dir = os.path.join(output_dir, self.green_env, 'resource')
        green_deploy = self.generate_blue_green_deployment(self.green_env, self.green_name)
        self.save_yaml(green_deploy, os.path.join(green_dir, f'Deployment/[green]{deploy_name}.yaml'))
        blue_recycle = self.generate_blue_green_deployment(self.blue_env, self.blue_name)
        blue_recycle['spec']['replicas'] = 0
        self.save_yaml(blue_recycle, os.path.join(green_dir, f'Deployment/[blue]{deploy_name}.yaml'))
        green_svc = self.generate_blue_green_service(self.green_env, self.green_name)
        self.save_yaml(green_svc, os.path.join(green_dir, f'Service/[green]{svc_name}.yaml'))
        for idx, ingress_name in enumerate(self.resource_names['ingress']):
            base_ingress = self.ingresses[idx]
            self.save_yaml(self.generate_ingress(base_ingress, 'user_switch', self.green_env, self.green_name),
                           os.path.join(green_dir, f'Ingress/[green]user_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'uid_switch', self.green_env, self.green_name),
                           os.path.join(green_dir, f'Ingress/[green]uid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'nouid_switch', self.green_env, self.green_name),
                           os.path.join(green_dir, f'Ingress/[green]nouid_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'all_switch', self.green_env, self.green_name),
                           os.path.join(green_dir, f'Ingress/[green]all_switch_{ingress_name}.yaml'))
            self.save_yaml(self.generate_ingress(base_ingress, 'close', self.green_env, self.green_name),
                           os.path.join(green_dir, f'Ingress/[blue]close_{ingress_name}.yaml'))

        # 基线
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

        # 统计
        def count_files(directory):
            return sum(len(files) for _, _, files in os.walk(directory)) if os.path.exists(directory) else 0
        blue_count = count_files(os.path.join(output_dir, self.blue_env))
        green_count = count_files(os.path.join(output_dir, self.green_env))
        base_count = count_files(os.path.join(output_dir, 'mse-base'))
        print(f"\n✅ 所有资源文件已生成到: {output_dir}")
        print(f"   {self.blue_env} 环境: {blue_count} 个文件")
        print(f"   {self.green_env} 环境: {green_count} 个文件")
        print(f"   基线环境: {base_count} 个文件")

        # 复制全局变量
        print(f"\n📊 复制全局变量文件到 {self.service_config.service_name}...")
        self._copy_global_var_files(output_dir)

        # 打包 zip（目录名用实际名称）
        print(f"\n📦 开始打包 {self.service_config.service_name} 的 zip 压缩包...")
        for env_key in [self.blue_env, self.green_env, 'mse-base']:
            env_path = Path(output_dir) / env_key
            if env_path.is_dir():
                self._zip_environment(env_path, env_key)

    # ---------------- 后续方法与您原脚本完全一致 ----------------
    def _copy_global_var_files(self, output_dir: str):
        env_dirs = {self.blue_env: os.path.join(output_dir, self.blue_env),
                    self.green_env: os.path.join(output_dir, self.green_env),
                    'mse-base': os.path.join(output_dir, 'mse-base')}
        if os.path.exists(self.bg_xls_path):
            for env in [self.blue_env, self.green_env]:
                target_path = os.path.join(env_dirs[env], 'global_var.xls')
                shutil.copy2(self.bg_xls_path, target_path)
                print(f"   ✓ 复制到 {env}: {target_path}")
        if os.path.exists(self.base_xls_path):
            target_path = os.path.join(env_dirs['mse-base'], 'global_var.xls')
            shutil.copy2(self.base_xls_path, target_path)
            print(f"   ✓ 复制到 mse-base: {target_path}")

    def _zip_environment(self, env_dir: Path, env_name: str):
        zip_path = env_dir.with_name(f'{env_name}.zip')
        resource_dir = env_dir / 'resource'
        xls_file = env_dir / 'global_var.xls'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(resource_dir):
                for file in files:
                    full_path = Path(root) / file
                    zf.write(full_path, full_path.relative_to(env_dir))
            if xls_file.exists():
                zf.write(xls_file, xls_file.name)
        print(f"   ✓ 压缩包已生成: {zip_path}")

    def _remove_agent_env_and_configmap(self, container: Dict):
        env_list = container.get('env', [])
        container['env'] = [env for env in env_list if env.get('name') != 'NACOS_SUFFIX']
        env_from_list = container.get('envFrom', [])
        container['envFrom'] = [
            item for item in env_from_list
            if not ('configMapRef' in item and item['configMapRef'].get('name') == 'mse-publish-gray')
        ]


# ---------- 配置加载 ----------
def load_config(config_file: str) -> Dict[str, Any]:
    if not os.path.exists(config_file):
        print(f"⚠️  配置文件不存在: {config_file}, 使用默认配置")
        return {
            'lane': 'key2',
            'agent_mode': 'java_lite',
            'mse_ns': 'mse-default',
            'blue_name': 'blue',
            'green_name': 'green'
        }
    with open(config_file, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault('blue_name', 'blue')
    cfg.setdefault('green_name', 'green')
    return cfg


# ---------- Excel 批处理 ----------
def process_excel(excel_path: str, config: Dict[str, Any], output_root: str):
    print(f"\n📊 读取Excel文件: {excel_path}")
    try:
        df = pd.read_excel(excel_path, header=0, skiprows=[1])
    except Exception as e:
        print(f"❌ Excel读取失败: {e}")
        sys.exit(1)
    required = ['service_name', 'lane', 'mse_ns', 'deployment_yaml', 'service_yaml', 'ingress_yaml']
    missing = [col for col in required if col not in df.columns]
    if missing:
        print(f"❌ Excel缺少必要列: {missing}")
        sys.exit(1)

    for idx, row in df.iterrows():
        service_name = row['service_name']
        if pd.isna(service_name):
            print(f"⚠️  第{idx + 3}行 service_name 为空，跳过")
            continue
        print("\n" + "=" * 60)
        print(f"🚀 开始处理服务 [{service_name}]")
        print("=" * 60)
        try:
            service_config = ServiceConfig(
                service_name=str(service_name).strip(),
                lane=str(row['lane']),
                mse_ns=str(row['mse_ns']),
                deployment_yaml=str(row['deployment_yaml']),
                service_yaml=str(row['service_yaml']),
                ingress_yaml=str(row['ingress_yaml'])
            )
            generator = BlueGreenGenerator(config, service_config)
            generator.load_from_excel()
            generator.generate_all(output_root)
            print(f"\n✅ 服务 [{service_name}] 处理完成")
        except Exception as e:
            print(f"\n❌ 服务 [{service_name}] 处理失败: {e}")
            import traceback
            traceback.print_exc()
            continue


# ---------- 入口 ----------
def main():
    parser = argparse.ArgumentParser(
        description='蓝绿发布资源自动生成脚本 - Excel批量版（支持自定义蓝/绿环境名称）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --excel ./services.xlsx --output ./output --config ./config.yaml
  %(prog)s -e ./services.xlsx -o ./output
        """
    )
    parser.add_argument('-e', '--excel', default='./config/services.xlsx',
                        help='服务定义Excel文件路径（含deployment/service/ingress YAML）')
    parser.add_argument('-o', '--output', default='./output',
                        help='输出根目录路径 (默认: ./output)')
    parser.add_argument('-c', '--config', default='./config/config.yaml',
                        help='配置文件路径 (默认: ./config/config.yaml)')
    args = parser.parse_args()

    try:
        print("=" * 60)
        print("🎯 蓝绿发布资源自动生成脚本 - Excel批量版（支持自定义蓝/绿环境名称）")
        print("=" * 60)
        config = load_config(args.config)
        print(f"\n⚙️  全局配置:")
        print(f"   Agent模式: {config.get('agent_mode')}")
        print(f"   蓝环境名称: {config.get('blue_name')}")
        print(f"   绿环境名称: {config.get('green_name')}")
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