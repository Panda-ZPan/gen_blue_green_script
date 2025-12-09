#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝绿发布资源自动生成脚本
根据基线YAML文件自动生成Blue/Green/Baseline三套资源
"""

import os
import sys
import yaml
import argparse
import shutil
from typing import Dict, List, Any
from copy import deepcopy


class BlueGreenGenerator:
    """蓝绿发布资源生成器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.lane = config.get('lane', 'key2')
        self.agent_mode = config.get('agent_mode', 'java_lite')
        self.mse_ns = config.get('mse_ns', 'mse-default')
        
        # 资源存储
        self.deployments = []
        self.services = []
        self.ingresses = []
        
        # 资源名称映射
        self.resource_names = {
            'deployment': None,
            'service': None,
            'ingress': None
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
                    if not self.resource_names['ingress']:
                        self.resource_names['ingress'] = doc['metadata']['name']
        
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
    
    def generate_blue_green_deployment(self, env: str) -> Dict:
        """生成蓝绿环境的Deployment"""
        base_deploy = deepcopy(self.deployments[0])
        deploy_name = self.resource_names['deployment']
        
        # 修改名称
        base_deploy['metadata']['name'] = f"{deploy_name}-{env}"
        
        # 添加标签
        labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
        if 'app' not in labels:
            labels['app'] = deploy_name
        labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
        labels['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"
        labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns
        
        # 设置副本数
        base_deploy['spec']['replicas'] = '${replicas}'
        
        # 更新selector
        base_deploy['spec']['selector']['matchLabels']['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"
        
        # 添加环境变量
        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            # 添加env
            env_vars = container.setdefault('env', [])
            env_vars.append({
                'name': 'NACOS_SUFFIX',
                'value': f'.{env}'
            })
            
            # 添加envFrom
            env_from = container.setdefault('envFrom', [])
            env_from.append({
                'configMapRef': {
                    'name': 'mse-publish-gray'
                }
            })
            
            # 设置镜像版本
            container['image'] = '${imageversion}'
        
        return base_deploy
    
    def generate_blue_green_service(self, env: str) -> Dict:
        """生成蓝绿环境的Service"""
        base_svc = deepcopy(self.services[0])
        svc_name = self.resource_names['service']
        
        # 修改名称
        base_svc['metadata']['name'] = f"{svc_name}-{env}"
        
        # 更新selector
        base_svc['spec']['selector']['sidecar.mesh.io/lane'] = f"{self.lane}-{env}"
        
        return base_svc
    
    def generate_ingress(self, ingress_type: str, env: str) -> Dict:
        """生成不同类型的Ingress"""
        base_ingress = deepcopy(self.ingresses[0])
        ingress_name = self.resource_names['ingress']
        reenv = 'green' if env == 'blue' else 'blue'
        
        annotations = base_ingress['metadata'].setdefault('annotations', {})
        
        if ingress_type == 'user_switch':
            # 友好用户切流
            base_ingress['metadata']['name'] = f"{ingress_name}-{env}"
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
            # 更新backend
            self._update_ingress_backend(base_ingress, f"{self.resource_names['service']}-{env}")
        
        elif ingress_type == 'uid_switch':
            # 用户ID切流
            base_ingress['metadata']['name'] = f"{ingress_name}-{env}"
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
            self._update_ingress_backend(base_ingress, f"{self.resource_names['service']}-{env}")
        
        elif ingress_type == 'nouid_switch':
            # 无ID切流
            base_ingress['metadata']['name'] = f"{ingress_name}-{reenv}"
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
            self._update_ingress_backend(base_ingress, f"{self.resource_names['service']}-{reenv}")
        
        elif ingress_type == 'all_switch':
            # 全量切流
            base_ingress['metadata']['name'] = f"{ingress_name}-{env}"
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
            self._update_ingress_backend(base_ingress, f"{self.resource_names['service']}-{env}")
        
        elif ingress_type == 'close':
            # 关闭旧环境
            base_ingress['metadata']['name'] = f"{ingress_name}-{reenv}"
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
            self._update_ingress_backend(base_ingress, f"{self.resource_names['service']}-{reenv}")
        
        return base_ingress
    
    def _update_ingress_backend(self, ingress: Dict, service_name: str):
        """更新Ingress的backend service名称"""
        rules = ingress.get('spec', {}).get('rules', [])
        for rule in rules:
            paths = rule.get('http', {}).get('paths', [])
            for path in paths:
                if 'backend' in path:
                    # 兼容不同版本的Ingress格式
                    if 'serviceName' in path['backend']:
                        path['backend']['serviceName'] = service_name
                    elif 'service' in path['backend']:
                        path['backend']['service']['name'] = service_name
    
    def generate_baseline_deployment(self, with_agent: bool = True) -> Dict:
        """生成基线Deployment"""
        base_deploy = deepcopy(self.deployments[0])
        
        if with_agent:
            # 添加标签
            labels = base_deploy['spec']['template']['metadata'].setdefault('labels', {})
            if 'app' not in labels:
                labels['app'] = self.resource_names['deployment']
            labels['sidecar.mesh.io/data-plane-mode'] = self.agent_mode
            labels['sidecar.mesh.io/lane'] = 'mse-base'
            labels['sidecar.mesh.io/mse-namespace'] = self.mse_ns
            
            # 更新selector
            base_deploy['spec']['selector']['matchLabels']['sidecar.mesh.io/lane'] = 'mse-base'
            
            # 添加envFrom
            containers = base_deploy['spec']['template']['spec']['containers']
            for container in containers:
                env_from = container.setdefault('envFrom', [])
                env_from.append({
                    'configMapRef': {
                        'name': 'mse-publish-gray'
                    }
                })
                container['image'] = '${imageversion}'
        
        # 设置副本数
        base_deploy['spec']['replicas'] = '${replicas}'
        
        # 设置镜像版本
        containers = base_deploy['spec']['template']['spec']['containers']
        for container in containers:
            container['image'] = '${imageversion}'
        
        return base_deploy
    
    def generate_baseline_service(self, with_agent: bool = True) -> Dict:
        """生成基线Service"""
        base_svc = deepcopy(self.services[0])
        
        if with_agent:
            base_svc['spec']['selector']['sidecar.mesh.io/lane'] = 'mse-base'
        
        return base_svc
    
    def generate_baseline_ingress(self) -> Dict:
        """生成基线Ingress"""
        base_ingress = deepcopy(self.ingresses[0])
        
        annotations = base_ingress['metadata'].setdefault('annotations', {})
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
        
        return base_ingress
    
    def save_yaml(self, data: Dict, filepath: str):
        """保存YAML文件"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        
        print(f"   ✓ {os.path.basename(filepath)}")
    
    def generate_all(self, output_dir: str):
        """生成所有资源文件"""
        print("\n🚀 开始生成资源文件...")
        
        # 清空输出目录
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        
        deploy_name = self.resource_names['deployment']
        svc_name = self.resource_names['service']
        ingress_name = self.resource_names['ingress']
        
        # 生成Blue环境
        print("\n📘 生成 Blue 环境资源:")
        blue_dir = os.path.join(output_dir, 'blue')
        
        # Deployment
        blue_deploy = self.generate_blue_green_deployment('blue')
        self.save_yaml(blue_deploy, os.path.join(blue_dir, f'[blue]{deploy_name}.yaml'))
        
        # 回收Green的Deployment (副本数设为0)
        green_recycle = self.generate_blue_green_deployment('green')
        green_recycle['spec']['replicas'] = 0
        self.save_yaml(green_recycle, os.path.join(blue_dir, f'[green]{deploy_name}.yaml'))
        
        # Service
        blue_svc = self.generate_blue_green_service('blue')
        self.save_yaml(blue_svc, os.path.join(blue_dir, f'[blue]{svc_name}.yaml'))
        
        # Ingress
        self.save_yaml(self.generate_ingress('user_switch', 'blue'), 
                      os.path.join(blue_dir, f'[blue]user_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('uid_switch', 'blue'),
                      os.path.join(blue_dir, f'[blue]uid_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('nouid_switch', 'blue'),
                      os.path.join(blue_dir, f'[blue]nouid_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('all_switch', 'blue'),
                      os.path.join(blue_dir, f'[blue]all_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('close', 'blue'),
                      os.path.join(blue_dir, f'[green]close_{ingress_name}.yaml'))
        
        # 生成Green环境
        print("\n📗 生成 Green 环境资源:")
        green_dir = os.path.join(output_dir, 'green')
        
        # Deployment
        green_deploy = self.generate_blue_green_deployment('green')
        self.save_yaml(green_deploy, os.path.join(green_dir, f'[green]{deploy_name}.yaml'))
        
        # 回收Blue的Deployment
        blue_recycle = self.generate_blue_green_deployment('blue')
        blue_recycle['spec']['replicas'] = 0
        self.save_yaml(blue_recycle, os.path.join(green_dir, f'[blue]{deploy_name}.yaml'))
        
        # Service
        green_svc = self.generate_blue_green_service('green')
        self.save_yaml(green_svc, os.path.join(green_dir, f'[green]{svc_name}.yaml'))
        
        # Ingress
        self.save_yaml(self.generate_ingress('user_switch', 'green'),
                      os.path.join(green_dir, f'[green]user_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('uid_switch', 'green'),
                      os.path.join(green_dir, f'[green]uid_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('nouid_switch', 'green'),
                      os.path.join(green_dir, f'[green]nouid_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('all_switch', 'green'),
                      os.path.join(green_dir, f'[green]all_switch_{ingress_name}.yaml'))
        self.save_yaml(self.generate_ingress('close', 'green'),
                      os.path.join(green_dir, f'[blue]close_{ingress_name}.yaml'))
        
        # 生成基线环境
        print("\n📙 生成基线环境资源:")
        base_dir = os.path.join(output_dir, 'mse-base')
        
        # Deployment
        self.save_yaml(self.generate_baseline_deployment(with_agent=True),
                      os.path.join(base_dir, f'[mse-base]{deploy_name}.yaml'))
        self.save_yaml(self.generate_baseline_deployment(with_agent=False),
                      os.path.join(base_dir, f'[mse-base]nojavaagent-{deploy_name}.yaml'))
        
        # Service
        self.save_yaml(self.generate_baseline_service(with_agent=True),
                      os.path.join(base_dir, f'[mse-base]{svc_name}.yaml'))
        self.save_yaml(self.generate_baseline_service(with_agent=False),
                      os.path.join(base_dir, f'[mse-base]nojavaagent-{svc_name}.yaml'))
        
        # Ingress
        self.save_yaml(self.generate_baseline_ingress(),
                      os.path.join(base_dir, f'[mse-base]{ingress_name}.yaml'))
        
        print(f"\n✅ 所有资源文件已生成到: {output_dir}")
        print(f"   Blue 环境: 8 个文件")
        print(f"   Green 环境: 8 个文件")
        print(f"   基线环境: 5 个文件")


def load_config(config_file: str) -> Dict[str, Any]:
    """加载配置文件"""
    if not os.path.exists(config_file):
        print(f"⚠️  配置文件不存在: {config_file}, 使用默认配置")
        return {
            'lane': 'key2',
            'agent_mode': 'IPTABLES',
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
    
    parser.add_argument('-b', '--baseline', default='./',
                       help='基线资源目录路径')
    parser.add_argument('-o', '--output', default='./output',
                       help='输出目录路径 (默认: ./output)')
    parser.add_argument('-c', '--config', default='./config.yaml',
                       help='配置文件路径 (默认: ./config.yaml)')
    
    args = parser.parse_args()
    
    try:
        print("=" * 60)
        print("🎯 蓝绿发布资源自动生成脚本")
        print("=" * 60)
        
        # 加载配置
        config = load_config(args.config)
        print(f"\n⚙️  配置信息:")
        print(f"   泳道: {config.get('lane')}")
        print(f"   Agent模式: {config.get('agent_mode')}")
        print(f"   MSE命名空间: {config.get('mse_ns')}")
        
        # 创建生成器
        generator = BlueGreenGenerator(config)
        
        # 加载基线
        generator.load_baseline(args.baseline)
        
        # 生成所有资源
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
